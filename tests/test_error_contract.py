"""오류 코드·작업 취소 계약(FE 확정 2026-11-06) — 실제 라우터 응답으로 확인한다.

HTTP 상태·메시지를 유지한 채 `error.code` 15종과 `details.reason` 으로 나가는지, 기존 details·retryable·traceId 를 잃지 않는지,
없는 리소스와 남의 리소스가 같은 응답인지(존재 은닉) 본다. 명세 스키마와의 전체 대조는 응답 대조 절차에서 한다.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.errors import install_error_handlers
from tests.exec_db import requires_db, seed_job
from tests.test_exec_downstream import _q, _text_block, _to_n5, dburl, env  # noqa: F401

CONTRACT_CODES = {
    "VALIDATION_ERROR", "UNAUTHORIZED", "TOKEN_EXPIRED", "NOT_FOUND", "DUPLICATE", "UNSUPPORTED_FORMAT", "FILE_TOO_LARGE", "INVALID_STATE",
    "ALL_SECTIONS_EXCLUDED", "RETRY_NOT_ALLOWED", "REVISION_CONFLICT", "CSV_SCHEMA_MISMATCH", "RATE_LIMITED", "INTERNAL_ERROR", "SERVICE_UNAVAILABLE",
}
MISSING = 999_999_999


def _err(r, status, code, message=None):
    assert r.status_code == status, r.text
    e = r.json()["error"]
    assert e["code"] == code and e["code"] in CONTRACT_CODES
    assert e["traceId"]
    assert isinstance(e["retryable"], bool)
    if message is not None:
        assert e["message"] == message
    return e


def _same_but_trace(a, b):
    """traceId 를 뺀 응답이 같은가 — 존재 은닉."""
    ja, jb = a.json()["error"], b.json()["error"]
    ja.pop("traceId"), jb.pop("traceId")
    assert a.status_code == b.status_code and ja == jb


# ── 정규화 단위 — 실제 경로로 만들기 어려운 조합 ────────────────────────────────────────────────
def _small_app():
    app = FastAPI()
    install_error_handlers(app)
    cases = {
        "with-details": (404, {"code": "NOT_FOUND", "message": "job not found", "details": {"hint": 1}}),
        "own-reason": (404, {"code": "NOT_FOUND", "message": "job not found", "details": {"reason": "own"}}),
        "null-details": (404, {"code": "JOB_NOT_FOUND", "message": "job not found", "details": None}),
        "extra-key": (404, {"code": "JOB_NOT_FOUND", "message": "job not found", "hint": 1}),
        "retryable": (409, {"code": "RETRY_LIMIT_EXCEEDED", "message": "m", "retryable": True}),
        "details-retryable": (404, {"code": "JOB_NOT_FOUND", "message": "job not found", "retryable": True, "details": {"hint": 1}}),
        "unrelated-message": (404, {"code": "NOT_FOUND", "message": "something else"}),
        "code-string": (409, "RETRY_LIMIT_EXCEEDED"),
    }

    @app.get("/{name}")
    def raise_case(name: str):
        status, detail = cases[name]
        raise HTTPException(status_code=status, detail=detail)

    return TestClient(app)


@pytest.mark.parametrize("name,code,message,retryable,details", [
    ("with-details", "NOT_FOUND", "job not found", False, {"reason": "job_not_found", "hint": 1}),  # 다시 감싸지 않는다
    ("own-reason", "NOT_FOUND", "job not found", False, {"reason": "own"}),  # 이미 있는 사유를 덮지 않는다
    ("null-details", "NOT_FOUND", "job not found", False, {"reason": "job_not_found"}),
    ("extra-key", "NOT_FOUND", "job not found", False, {"reason": "job_not_found", "hint": 1}),
    ("retryable", "RETRY_NOT_ALLOWED", "m", True, {"reason": "retry_limit_exceeded"}),  # 라우터가 정한 retryable 유지
    ("details-retryable", "NOT_FOUND", "job not found", True, {"reason": "job_not_found", "hint": 1}),
    ("unrelated-message", "NOT_FOUND", "something else", False, None),  # 합의된 사유 밖에는 붙이지 않는다
    ("code-string", "RETRY_NOT_ALLOWED", "retry limit exceeded", False, {"reason": "retry_limit_exceeded"}),
])
def test_normalize_keeps_details_retryable_and_trace(name, code, message, retryable, details):
    r = _small_app().get(f"/{name}", headers={"X-Request-ID": "trace-1"})
    e = r.json()["error"]
    assert (e["code"], e["message"], e["retryable"], e["details"], e["traceId"]) == (code, message, retryable, details, "trace-1")


# ── 실제 라우터 ───────────────────────────────────────────────────────────────────────────────

def _stranger():
    from app import db as app_db
    from app.main import app

    with app_db.engine.begin() as conn:
        seeded = seed_job(conn)
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer " + c.post("/v1/auth/login", json={"email": seeded["email"], "password": "x"}).json()["accessToken"]
    return c, seeded


@requires_db
@pytest.mark.parametrize("method,path,body,message,reason", [
    ("get", "/v1/jobs/{missing}", None, "job not found", "job_not_found"),
    ("patch", "/v1/jobs/{missing}", {"productName": "x"}, "job not found", "job_not_found"),  # 전에도 NOT_FOUND 이던 경로
    ("post", "/v1/jobs/{missing}/analyze", None, "job not found", "job_not_found"),  # 흐름의 StartRejected
    ("post", "/v1/jobs/{missing}/sections/proceed", None, "job not found", "job_not_found"),  # 흐름의 ProceedRejected
    ("post", "/v1/jobs/{missing}/confirm", {"acknowledgedWarnings": []}, "job not found", "job_not_found"),  # 흐름의 ConfirmRejected
    ("patch", "/v1/jobs/{missing}/source-images/reorder", {"order": []}, "job not found", "job_not_found"),  # 전에도 NOT_FOUND 이던 경로
    ("get", "/v1/brands/{missing}", None, "brand not found", "brand_not_found"),
    ("get", "/v1/jobs/{job}/sections/{missing}", None, "section not found", "section_not_found"),
    ("patch", "/v1/jobs/{job}/blocks/{missing}", {"trans1": "x", "revision": 0}, "block not found", "block_not_found"),
    ("delete", "/v1/brands/{brand}/logos/{missing}", None, "logo not found", "logo_not_found"),
    ("delete", "/v1/jobs/{job}/source-images/{missing}", None, "source image not found", "source_image_not_found"),
    ("get", "/v1/jobs/{job}/exports/{missing}/download", None, "artifact not found", "artifact_not_found"),
])
def test_not_found_uses_common_code_and_reason(env, method, path, body, message, reason):
    ids, _ = _to_n5(env)
    c = ids["c"]
    brand = c.get(f"/v1/jobs/{ids['job']}").json()["brandId"]
    url = path.format(missing=MISSING, job=ids["job"], brand=brand)
    r = getattr(c, method)(url, **({"json": body} if body is not None else {}))
    e = _err(r, 404, "NOT_FOUND", message)
    assert e["details"] == {"reason": reason} and e["retryable"] is False


@requires_db
def test_create_job_with_missing_brand_gets_reason(env):
    c, seeded = _stranger()
    r = c.post("/v1/jobs", json={"brandId": MISSING, "productName": "x", "targetCountry": "US", "targetLang": "en", "regulatoryClass": "cosmetic"})
    e = _err(r, 404, "NOT_FOUND", "brand not found")
    assert e["details"] == {"reason": "brand_not_found"}


@requires_db
def test_missing_task_has_no_new_reason(env):
    """`task_not_found` 는 합의되지 않았다 — 코드만 NOT_FOUND 다."""
    ids, _ = _to_n5(env)
    e = _err(ids["c"].post(f"/v1/jobs/{ids['job']}/tasks/{MISSING}/retry"), 404, "NOT_FOUND", "task not found")
    assert e["details"] is None


@requires_db
def test_unknown_route_has_no_reason(env):
    ids, _ = _to_n5(env)
    e = _err(ids["c"].get("/v1/no-such-route"), 404, "NOT_FOUND")
    assert e["details"] is None


@requires_db
@pytest.mark.parametrize("make", [
    lambda c, ids: c.get(f"/v1/jobs/{ids['job']}"),
    lambda c, ids: c.get(f"/v1/jobs/{ids['job']}/sections"),
    lambda c, ids: c.post(f"/v1/jobs/{ids['job']}/analyze"),
    lambda c, ids: c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}),
    lambda c, ids: c.patch(f"/v1/jobs/{ids['job']}", json={"productName": "x"}),
    lambda c, ids: c.delete(f"/v1/jobs/{ids['job']}"),
])
def test_other_sellers_job_looks_missing(env, make):
    ids, _ = _to_n5(env)
    stranger, _seeded = _stranger()
    own_missing = make(stranger, {"job": MISSING})
    other = make(stranger, ids)
    _err(other, 404, "NOT_FOUND", "job not found")
    _same_but_trace(other, own_missing)


@requires_db
def test_other_sellers_brand_block_and_artifact_look_missing(env):
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    stranger, seeded = _stranger()
    brand = c.get(f"/v1/jobs/{job}").json()["brandId"]
    _same_but_trace(stranger.get(f"/v1/brands/{brand}"), stranger.get(f"/v1/brands/{MISSING}"))
    # 남의 작업의 블록·섹션은 작업 단계에서 막힌다 — 자기 작업에서 남의 블록을 부르면 없는 블록과 같다
    b = _text_block(c, job)
    body = {"trans1": "x", "revision": 0}
    _same_but_trace(stranger.patch(f"/v1/jobs/{seeded['job']}/blocks/{b['id']}", json=body),
                    stranger.patch(f"/v1/jobs/{seeded['job']}/blocks/{MISSING}", json=body))
    _same_but_trace(stranger.get(f"/v1/jobs/{seeded['job']}/sections/{b['sectionId']}"),
                    stranger.get(f"/v1/jobs/{seeded['job']}/sections/{MISSING}"))


@requires_db
def test_seller_not_found_and_tokens(env):
    from app.main import app
    from app.security import create_token

    c = TestClient(app)
    ghost = {"Authorization": "Bearer " + create_token(MISSING, "ghost@example.com")}
    for r in (c.get("/v1/auth/me", headers=ghost), c.post("/v1/auth/refresh", headers=ghost)):
        assert _err(r, 404, "NOT_FOUND", "seller not found")["details"] == {"reason": "seller_not_found"}

    malformed = _err(c.get("/v1/auth/me", headers={"Authorization": "Bearer nope"}), 401, "UNAUTHORIZED", "malformed token")
    assert malformed["details"] == {"reason": "invalid_token"} and malformed["retryable"] is False
    good = ghost["Authorization"].removeprefix("Bearer ")
    forged = good[:-2] + ("aa" if not good.endswith("aa") else "bb")
    bad_sig = _err(c.get("/v1/auth/me", headers={"Authorization": f"Bearer {forged}"}), 401, "UNAUTHORIZED", "bad signature")
    assert bad_sig["details"] == {"reason": "invalid_token"}
    # 토큰 없음·만료는 이번 매핑 대상이 아니다
    assert _err(c.get("/v1/auth/me"), 401, "UNAUTHORIZED")["details"] is None


@requires_db
def test_expired_token_keeps_code_and_retryable(env, monkeypatch):
    import time

    from app.main import app
    from app.security import create_token

    token = create_token(MISSING, "ghost@example.com")
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + 10 * 365 * 86400)
    e = _err(TestClient(app).get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"}), 401, "TOKEN_EXPIRED")
    assert e["retryable"] is True


@requires_db
def test_retry_limit_uses_retry_not_allowed_with_reason(env):
    """레거시(0008 이전) 실행 행의 한도 초과 — 새 INSERT 는 트리거가 막으므로 시험 안에서만 트리거를 끄고 만든다."""
    from app import db as app_db

    c, seeded = _stranger()
    with app_db.engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        tid = conn.execute(text(
            "INSERT INTO job_async_task (job_id, task_type, unit_type, status, retry_count, max_retry, execution_schema_version) "
            "VALUES (:j, 'translate', 'job', 'failed', 1, 1, 0) RETURNING id"), {"j": seeded["job"]}).scalar_one()
    e = _err(c.post(f"/v1/jobs/{seeded['job']}/tasks/{tid}/retry"), 409, "RETRY_NOT_ALLOWED", "retry limit exceeded")
    assert e["details"] == {"reason": "retry_limit_exceeded"} and e["retryable"] is False


@requires_db
def test_business_validation_is_400_with_fields(env):
    from app.main import app

    c = TestClient(app)
    e = _err(c.post("/v1/auth/login", json={"email": "   ", "password": "x"}), 400, "VALIDATION_ERROR", "email is required")
    assert e["details"] == {"fields": [{"loc": ["body", "email"], "msg": "email is required"}]}

    stranger, seeded = _stranger()
    e = _err(stranger.post("/v1/brands", json={"nameEn": "  ", "nameKo": "x"}), 400, "VALIDATION_ERROR", "nameEn is required")
    assert e["details"] == {"fields": [{"loc": ["body", "nameEn"], "msg": "nameEn is required"}]}
    e = _err(stranger.patch(f"/v1/brands/{seeded['brand']}", json={"nameEn": " "}), 400, "VALIDATION_ERROR", "nameEn must not be empty")
    assert e["details"] == {"fields": [{"loc": ["body", "nameEn"], "msg": "nameEn must not be empty"}]}


@requires_db
def test_format_validation_stays_422(env):
    stranger, _seeded = _stranger()
    e = _err(stranger.post("/v1/brands", json={"nameKo": "x"}), 422, "VALIDATION_ERROR", "request validation failed")
    assert e["details"]["fields"] and all(set(f) == {"loc", "msg"} for f in e["details"]["fields"])


@requires_db
def test_existing_details_survive_on_real_routes(env):
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    stale = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Changed", "revision": b["revision"] + 5},
                    headers={"X-Request-ID": "trace-conflict"})
    e = _err(stale, 409, "REVISION_CONFLICT")
    assert e["details"]["current"]["revision"] == b["revision"] and "reason" not in e["details"]
    assert e["traceId"] == "trace-conflict"
    assert c.delete(f"/v1/jobs/{job}").status_code == 200
    late = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "x", "revision": b["revision"]})
    assert _err(late, 409, "INVALID_STATE")["details"] == {"reason": "job_not_editable"}


# ── 작업 취소 ─────────────────────────────────────────────────────────────────────────────────
@requires_db
def test_cancel_is_200_and_repeatable(env):
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    first = c.delete(f"/v1/jobs/{job}")
    assert first.status_code == 200 and first.json() == {"jobId": job, "status": "archived"}
    again = c.delete(f"/v1/jobs/{job}")
    assert again.status_code == 200 and again.json() == first.json()
    assert _q("SELECT status FROM job WHERE id = :j", j=job)[0]["status"] == "archived"


@requires_db
def test_cancel_missing_or_other_sellers_job_is_404_and_changes_nothing(env):
    ids, _ = _to_n5(env)
    job = ids["job"]
    before = _q("SELECT status FROM job WHERE id = :j", j=job)[0]["status"]
    stranger, _seeded = _stranger()
    other = stranger.delete(f"/v1/jobs/{job}")
    assert _err(other, 404, "NOT_FOUND", "job not found")["details"] == {"reason": "job_not_found"}
    _same_but_trace(other, stranger.delete(f"/v1/jobs/{MISSING}"))
    assert _q("SELECT status FROM job WHERE id = :j", j=job)[0]["status"] == before
