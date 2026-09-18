"""API 스모크 테스트 — 실제 DB 필요(없으면 모듈 전체 skip).

컨테이너 안에서 실행: docker exec pixlate-api python -m pytest tests -q
로컬(PostgreSQL 미연결)에서는 자동으로 건너뛴다.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


def _db_ok() -> bool:
    try:
        from app.db import engine
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="database not reachable")

from app.main import app  # noqa: E402

client = TestClient(app)


def _token(email: str | None = None) -> str:
    email = email or f"pytest-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/v1/auth/login", json={"email": email, "password": "x"})
    assert r.status_code == 200
    return r.json()["accessToken"]


def _auth(email: str | None = None) -> dict:
    return {"Authorization": f"Bearer {_token(email)}"}


def test_protected_endpoint_requires_token():
    r = client.get("/v1/brands")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_master_countries_is_public():
    r = client.get("/v1/master/countries")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_login_and_me():
    email = f"pytest-{uuid.uuid4().hex[:8]}@example.com"
    tok = _token(email)
    r = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["email"] == email


def test_brand_crud_roundtrip():
    h = _auth()
    r = client.post("/v1/brands", json={"nameKo": "테스트", "nameEn": "TestBrand"}, headers=h)
    assert r.status_code == 201
    brand_id = r.json()["id"]

    r = client.get(f"/v1/brands/{brand_id}", headers=h)
    assert r.status_code == 200
    assert r.json()["nameEn"] == "TestBrand"


def test_brand_missing_name_is_validation_error():
    r = client.post("/v1/brands", json={"nameKo": "이름만"}, headers=_auth())
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_brand_is_standard_404():
    r = client.get("/v1/brands/99999999", headers=_auth())
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BRAND_NOT_FOUND"


def test_cross_seller_isolation():
    # 셀러 A가 만든 브랜드를 셀러 B가 조회하면 404
    ha = _auth("owner-a@example.com")
    r = client.post("/v1/brands", json={"nameKo": "A", "nameEn": "BrandA"}, headers=ha)
    brand_id = r.json()["id"]
    hb = _auth("intruder-b@example.com")
    r = client.get(f"/v1/brands/{brand_id}", headers=hb)
    assert r.status_code == 404
