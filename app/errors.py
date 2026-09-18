"""에러 응답 표준화.

모든 에러 응답을 하나의 형식으로 통일한다:
    {"error": {"code": "<CODE>", "message": "<사람이 읽는 설명>", ...추가필드}}

기존 라우터의 raise HTTPException(...) 을 거의 그대로 두고, 예외 핸들러에서
detail(dict/str)을 표준 코드로 정규화한다. 새 코드가 필요하면 명시적 dict detail
(예: {"code": "REVISION_CONFLICT", "current": 3})로 raise 하면 그대로 전달된다.
"""
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# 표준 에러 코드(문서화용) — 상태코드별 대표 코드
ERROR_CODES = {
    "VALIDATION_ERROR": 422,   # 요청 본문/쿼리 검증 실패(FastAPI 자동)
    "INVALID_INPUT": 400,      # 비즈니스 입력 검증 실패
    "UNAUTHORIZED": 401,       # 토큰 없음
    "INVALID_TOKEN": 401,      # 토큰 서명/형식 오류
    "TOKEN_EXPIRED": 401,      # 토큰 만료
    "JOB_NOT_FOUND": 404,
    "BRAND_NOT_FOUND": 404,
    "SECTION_NOT_FOUND": 404,
    "BLOCK_NOT_FOUND": 404,
    "LOGO_NOT_FOUND": 404,
    "SOURCE_IMAGE_NOT_FOUND": 404,
    "ARTIFACT_NOT_FOUND": 404,
    "SELLER_NOT_FOUND": 404,
    "REVISION_CONFLICT": 409,      # 낙관적 잠금 충돌
    "ALL_SECTIONS_EXCLUDED": 409,  # 포함 섹션 0개로 진행 불가
    "RETRY_NOT_ALLOWED": 409,      # 실패 아닌 태스크 재시도
    "RETRY_LIMIT_EXCEEDED": 409,   # 재시도 한도 초과
    "INTERNAL_ERROR": 500,
}

# 라우터가 쓰는 문자열 detail → (코드, 메시지) 매핑
_STRING_TO_ERROR = {
    "job not found": ("JOB_NOT_FOUND", "job not found"),
    "brand not found": ("BRAND_NOT_FOUND", "brand not found"),
    "section not found": ("SECTION_NOT_FOUND", "section not found"),
    "block not found": ("BLOCK_NOT_FOUND", "block not found"),
    "logo not found": ("LOGO_NOT_FOUND", "logo not found"),
    "source image not found": ("SOURCE_IMAGE_NOT_FOUND", "source image not found"),
    "artifact not found": ("ARTIFACT_NOT_FOUND", "artifact not found"),
    "ALL_SECTIONS_EXCLUDED": ("ALL_SECTIONS_EXCLUDED", "all sections are excluded"),
    "RETRY_NOT_ALLOWED": ("RETRY_NOT_ALLOWED", "only failed tasks can be retried"),
    "RETRY_LIMIT_EXCEEDED": ("RETRY_LIMIT_EXCEEDED", "retry limit exceeded"),
    "productName is required": ("INVALID_INPUT", "productName is required"),
    "nameEn is required": ("INVALID_INPUT", "nameEn is required"),
    "nameEn must not be empty": ("INVALID_INPUT", "nameEn must not be empty"),
}

_STATUS_DEFAULT = {
    400: "BAD_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
    404: "NOT_FOUND", 409: "CONFLICT", 422: "VALIDATION_ERROR", 500: "INTERNAL_ERROR",
}


def _normalize(status_code: int, detail) -> tuple[str, str, dict]:
    """(status, detail) → (code, message, extra) 표준화."""
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


def install_error_handlers(app) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request, exc: StarletteHTTPException):
        code, message, extra = _normalize(exc.status_code, exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": message, **extra}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request, exc: RequestValidationError):
        fields = [
            {"loc": list(e.get("loc", [])), "msg": str(e.get("msg", ""))}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION_ERROR", "message": "request validation failed", "fields": fields}},
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "INTERNAL_ERROR", "message": "internal server error"}},
        )
