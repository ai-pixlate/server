"""에러 응답 표준화 테스트 — DB 불필요(작은 앱으로 검증)."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.errors import install_error_handlers


def _client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    class Body(BaseModel):
        name: str  # 필수 → 누락 시 422

    @app.get("/str404")
    def str404():
        raise HTTPException(status_code=404, detail="job not found")

    @app.get("/dict409")
    def dict409():
        raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "current": 3})

    @app.get("/nested409")
    def nested409():
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "m", "details": {"warnings": [1]}})

    @app.get("/retry503")
    def retry503():
        raise HTTPException(status_code=503, detail={"code": "SERVICE_UNAVAILABLE", "message": "down"})

    @app.get("/explicit409")
    def explicit409():
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "m", "retryable": True})

    @app.get("/boom")
    def boom():
        raise RuntimeError("x")

    @app.get("/code409")
    def code409():
        raise HTTPException(status_code=409, detail="ALL_SECTIONS_EXCLUDED")

    @app.post("/validate")
    def validate(body: Body):
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_string_detail_mapped_to_code():
    r = _client().get("/str404")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert r.json()["error"]["message"] == "job not found"


def test_dict_detail_keeps_extra_fields():
    r = _client().get("/dict409")
    err = r.json()["error"]
    assert r.status_code == 409
    assert err["code"] == "REVISION_CONFLICT"
    assert err["details"] == {"current": 3} and "current" not in err  # 부가 정보는 details 아래


def test_code_like_string_used_as_code():
    r = _client().get("/code409")
    assert r.json()["error"]["code"] == "ALL_SECTIONS_EXCLUDED"


def test_validation_error_envelope():
    r = _client().post("/validate", json={})  # name 누락
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert isinstance(err["details"]["fields"], list)


def _contract_shape(err):
    """계약 Error: code·message·retryable(bool)·traceId(str) 필수, details 는 객체 또는 null(FE parseApiError 와 같은 조건)."""
    assert isinstance(err["code"], str) and isinstance(err["message"], str)
    assert isinstance(err["retryable"], bool) and isinstance(err["traceId"], str) and err["traceId"]
    assert err["details"] is None or isinstance(err["details"], dict)


def test_every_error_has_contract_shape():
    c = _client()
    for r in (c.get("/str404"), c.get("/dict409"), c.get("/code409"), c.post("/validate", json={}), c.get("/boom"),
              c.get("/nested409"), c.get("/retry503")):
        _contract_shape(r.json()["error"])
    assert c.get("/str404").json()["error"]["details"] is None


def test_retryable_defaults_and_explicit_value():
    c = _client()
    assert c.get("/dict409").json()["error"]["retryable"] is False
    assert c.get("/retry503").json()["error"]["retryable"] is True
    assert c.get("/boom").json()["error"]["retryable"] is True  # INTERNAL_ERROR
    assert c.get("/explicit409").json()["error"]["retryable"] is True


def test_details_not_nested_twice():
    assert _client().get("/nested409").json()["error"]["details"] == {"warnings": [1]}


def test_trace_id_uses_request_header():
    assert _client().get("/str404", headers={"X-Request-ID": "req-123"}).json()["error"]["traceId"] == "req-123"
