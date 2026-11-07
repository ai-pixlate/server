"""에러 응답 표준화.

모든 에러 응답을 계약 `Error` 형식으로 통일한다(docs/openapi.yaml):
    {"error": {"code": "<CODE>", "message": "<설명>", "retryable": <bool>, "details": {...}|null, "traceId": "<id>"}}
- 코드별 부가 정보는 `details` 아래에 둔다(라우터가 detail dict 에 넣은 code·message·retryable 외 필드).
  라우터가 이미 `details` 를 만들어 넘긴 경우 다시 감싸지 않는다.
- retryable 은 라우터가 정하면 그 값, 아니면 코드별 기본값(재시도로 결과가 바뀔 수 있는 코드만 true).
- traceId 는 요청 헤더 X-Request-ID(있으면, 128자까지) 또는 새로 만든 값.
- 계약 `error.code` 는 15종이다. 서버 내부 코드는 여기서 계약 코드 + `details.reason` 으로 바꾼다(FE 확정 2026-11-06).
  HTTP 상태·message·기존 details·retryable·traceId 는 그대로다.

기존 라우터의 raise HTTPException(...) 을 거의 그대로 두고, 예외 핸들러에서
detail(dict/str)을 표준 코드로 정규화한다. 새 코드가 필요하면 명시적 dict detail
(예: {"code": "REVISION_CONFLICT", "current": 3})로 raise 하면 그대로 전달된다.
"""
import uuid

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# 라우터가 쓰는 문자열 detail → (코드, 메시지) 매핑
_STRING_TO_ERROR = {
    "ALL_SECTIONS_EXCLUDED": ("ALL_SECTIONS_EXCLUDED", "all sections are excluded"),
    "RETRY_NOT_ALLOWED": ("RETRY_NOT_ALLOWED", "only failed tasks can be retried"),
    "RETRY_LIMIT_EXCEEDED": ("RETRY_LIMIT_EXCEEDED", "retry limit exceeded"),
}

# 서버 내부 코드 → (계약 코드, details.reason)
_CONTRACT_CODE = {
    "JOB_NOT_FOUND": ("NOT_FOUND", "job_not_found"),
    "SELLER_NOT_FOUND": ("NOT_FOUND", "seller_not_found"),
    "ARTIFACT_NOT_FOUND": ("NOT_FOUND", "artifact_not_found"),
    "INVALID_TOKEN": ("UNAUTHORIZED", "invalid_token"),
    "RETRY_LIMIT_EXCEEDED": ("RETRY_NOT_ALLOWED", "retry_limit_exceeded"),
}

# NOT_FOUND 의 리소스 종류 — 없음과 남의 것을 나누지 않는다(존재 은닉). 합의된 사유만 둔다("task not found" 는 사유 없음)
_NOT_FOUND_REASON = {
    "job not found": "job_not_found",
    "brand not found": "brand_not_found",
    "section not found": "section_not_found",
    "block not found": "block_not_found",
    "logo not found": "logo_not_found",
    "source image not found": "source_image_not_found",
    "artifact not found": "artifact_not_found",
    "seller not found": "seller_not_found",
}

# 계약 Error.code 주석의 (retryable) 표시 — 재시도(토큰 갱신 포함)로 결과가 바뀔 수 있는 코드
_RETRYABLE = {"TOKEN_EXPIRED", "RATE_LIMITED", "INTERNAL_ERROR", "SERVICE_UNAVAILABLE"}

_STATUS_DEFAULT = {
    400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
    404: "NOT_FOUND", 409: "CONFLICT", 422: "VALIDATION_ERROR", 500: "INTERNAL_ERROR",
}


def _with_reason(extra: dict, reason: str) -> dict:
    """extra 에 details.reason 을 더한다. 이미 만든 details 는 다시 감싸지 않고 그 안에 넣는다(이미 있는 reason 은 그대로)."""
    out = dict(extra)
    if isinstance(out.get("details"), dict):
        out["details"] = {"reason": reason, **out["details"]}
        return out
    if "details" in out and out["details"] is None:
        del out["details"]
    out.setdefault("reason", reason)
    return out


def _normalize(status_code: int, detail) -> tuple[str, str, dict]:
    """(status, detail) → (계약 code, message, extra) 표준화."""
    code, message, extra = _parse_detail(status_code, detail)
    code, reason = _CONTRACT_CODE.get(code, (code, None))
    if code == "NOT_FOUND" and reason is None:
        reason = _NOT_FOUND_REASON.get(message)
    return code, message, _with_reason(extra, reason) if reason else extra


def _parse_detail(status_code: int, detail) -> tuple[str, str, dict]:
    if isinstance(detail, dict):
        code = detail.get("code") or _STATUS_DEFAULT.get(status_code, "ERROR")
        message = detail.get("message") or code.replace("_", " ").lower()
        extra = {k: v for k, v in detail.items() if k not in ("code", "message")}
        return code, message, extra
    if isinstance(detail, str):
        if detail in _STRING_TO_ERROR:
            code, message = _STRING_TO_ERROR[detail]
            return code, message, {}
        if " " not in detail and detail == detail.upper():  # 이미 CODE 형태
            return detail, detail.replace("_", " ").lower(), {}
        return _STATUS_DEFAULT.get(status_code, "ERROR"), detail, {}
    return _STATUS_DEFAULT.get(status_code, "ERROR"), str(detail), {}


def _trace_id(request) -> str:
    rid = request.headers.get("x-request-id") if request is not None else None
    return rid[:128] if rid else uuid.uuid4().hex


def error_body(request, code: str, message: str, extra: dict | None = None) -> dict:
    extra = dict(extra or {})
    retryable = extra.pop("retryable", None)
    if not isinstance(retryable, bool):
        retryable = code in _RETRYABLE
    if set(extra) == {"details"}:  # 이미 details 로 넘어온 것은 다시 감싸지 않는다
        details = extra["details"]
    else:
        details = extra or None
    return {"error": {"code": code, "message": message, "retryable": retryable, "details": details, "traceId": _trace_id(request)}}


def install_error_handlers(app) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request, exc: StarletteHTTPException):
        code, message, extra = _normalize(exc.status_code, exc.detail)
        return JSONResponse(status_code=exc.status_code, content=error_body(request, code, message, extra))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request, exc: RequestValidationError):
        fields = [
            {"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(request, "VALIDATION_ERROR", "request validation failed", {"fields": fields}),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc: Exception):
        return JSONResponse(status_code=500, content=error_body(request, "INTERNAL_ERROR", "internal server error"))
