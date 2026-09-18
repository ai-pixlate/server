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

    @app.get("/code409")
    def code409():
        raise HTTPException(status_code=409, detail="ALL_SECTIONS_EXCLUDED")

    @app.post("/validate")
    def validate(body: Body):
        return {"ok": True}

    return TestClient(app)


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
    assert err["current"] == 3


def test_code_like_string_used_as_code():
    r = _client().get("/code409")
    assert r.json()["error"]["code"] == "ALL_SECTIONS_EXCLUDED"


def test_validation_error_envelope():
    r = _client().post("/validate", json={})  # name 누락
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert isinstance(err["fields"], list)
