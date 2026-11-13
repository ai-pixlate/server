"""간이 로그인 환경별 차단(U7) — PIXLATE_AUTH_LOCAL_LOGIN = enabled | disabled, 미설정 = enabled. 실제 라우터 · PostgreSQL.

- disabled 면 `POST /v1/auth/login` 은 404 NOT_FOUND 이고 새 판매자 · 세션 · refresh 쿠키를 만들지 않는다.
  라우트 의존성이라 본문 필드 검증보다 먼저다(빈 본문 · 필드 누락 · 형식 오류도 404). JSON 문법 오류는 본문 읽기 단계라 기존 422 그대로다.
- 이미 발급된 local 세션은 이 설정으로 폐기되지 않는다 — me · 보호 API · refresh · logout 은 그대로 동작한다.
- 허용 값이 아니면(빈 문자열 포함) API 를 불러오는 단계에서 실패한다. 메시지에 입력값은 없다.
세션 저장소는 conftest 의 설정(PIXLATE_TEST_REDIS_URL 이면 실제 Redis, 없으면 메모리 대역)을 그대로 쓴다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests import exec_db
from tests.exec_db import bound_app_db, fresh_database

ROOT = Path(__file__).resolve().parents[1]
ENV = "PIXLATE_AUTH_LOCAL_LOGIN"
CSRF = {"X-Requested-With": "XMLHttpRequest", "Origin": "https://fe.pixlate.test"}  # conftest 의 시험 허용 Origin


@pytest.fixture(scope="module")
def dburl():
    # DB 가 필요한 시험만 건너뛴다(설정 검증 import 시험은 DB 없이 돈다). CI 필수 시험 판정은 건너뜀을 실패로 본다.
    if not exec_db.ADMIN_URL:
        pytest.skip("PIXLATE_TEST_DATABASE_URL 없음")
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def client(dburl):
    from app.main import app

    return TestClient(app)


def _email() -> str:
    return f"gate-{uuid.uuid4().hex[:10]}@example.com"


def _q(sql, **p):
    from app import db as app_db

    with app_db.engine.begin() as c:
        return c.execute(text(sql), p).scalar()


def _sellers() -> int:
    return _q("SELECT count(*) FROM seller")


def _sessions() -> int:
    from app import auth_sessions

    c = auth_sessions.client()
    if hasattr(c, "h"):  # tests/auth_session_fake.py 대역
        return sum(1 for k in c.h if k.startswith(auth_sessions._KEY))
    return sum(1 for _ in c.scan_iter(match=auth_sessions._KEY + "*", count=500))


def _refresh_cookie(r) -> str | None:
    for h in r.headers.get_list("set-cookie"):
        name, _, rest = h.partition("=")
        if name.strip() == "pxl_refresh":
            return rest.split(";", 1)[0]
    return None


def _login(client, email: str, cookie: str | None = None):
    headers = {"Cookie": f"pxl_refresh={cookie}"} if cookie else {}
    return client.post("/v1/auth/login", json={"email": email, "password": "x"}, headers=headers)


def _assert_blocked(r, sellers_before: int, sessions_before: int) -> None:
    assert r.status_code == 404, r.text
    err = r.json()["error"]
    assert err["code"] == "NOT_FOUND" and err["details"] is None
    assert "accessToken" not in r.text
    assert r.headers.get_list("set-cookie") == []
    assert _sellers() == sellers_before
    assert _sessions() == sessions_before


# ───────────────────────────── 허용(기존 동작) ─────────────────────────────

@pytest.mark.parametrize("value", [None, "enabled"], ids=["미설정", "enabled"])
def test_unset_or_enabled_keeps_existing_login(client, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    sellers, sessions = _sellers(), _sessions()
    r = _login(client, _email())
    assert r.status_code == 200, r.text
    assert r.json()["accessToken"] and _refresh_cookie(r)
    assert _sellers() == sellers + 1
    assert _sessions() == sessions + 1


# ───────────────────────────── disabled — 새 발급 차단 ─────────────────────────────

BLOCKED_BODIES = {
    "정상 본문": {"json": {"email": "x@example.com", "password": "x"}},
    "빈 객체": {"json": {}},
    "본문 없음": {},
    "email 누락": {"json": {"password": "x"}},
    "email 형식 오류(숫자)": {"json": {"email": 123}},
    "빈 email": {"json": {"email": "   "}},
}


@pytest.mark.parametrize("kw", list(BLOCKED_BODIES.values()), ids=list(BLOCKED_BODIES))
def test_disabled_blocks_new_login_before_body_validation(client, monkeypatch, kw):
    monkeypatch.setenv(ENV, "disabled")
    sellers, sessions = _sellers(), _sessions()
    r = client.post("/v1/auth/login", **kw)
    _assert_blocked(r, sellers, sessions)


def test_disabled_blocks_existing_local_email_too(client, monkeypatch):
    """이미 있는 local 판매자의 이메일이어도 새 세션을 주지 않는다."""
    email = _email()
    monkeypatch.setenv(ENV, "enabled")
    assert _login(client, email).status_code == 200
    monkeypatch.setenv(ENV, "disabled")
    sellers, sessions = _sellers(), _sessions()
    _assert_blocked(_login(client, email), sellers, sessions)


def test_disabled_with_old_refresh_cookie_does_not_replace_session(client, monkeypatch):
    """이전 쿠키를 실은 차단 요청은 그 세션을 폐기 · 교체하지 않는다(핸들러 미실행)."""
    monkeypatch.setenv(ENV, "enabled")
    old = _login(client, _email())
    access, cookie = old.json()["accessToken"], _refresh_cookie(old)
    monkeypatch.setenv(ENV, "disabled")
    sellers, sessions = _sellers(), _sessions()
    _assert_blocked(_login(client, _email(), cookie=cookie), sellers, sessions)
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"}).status_code == 200


@pytest.mark.parametrize("value", ["enabled", "disabled"])
def test_malformed_json_stays_422_without_side_effects(client, monkeypatch, value):
    """JSON 문법 오류는 본문 읽기 단계에서 기존 422 — 어느 값에서도 판매자 · 세션 · 쿠키가 생기지 않는다."""
    monkeypatch.setenv(ENV, value)
    sellers, sessions = _sellers(), _sessions()
    r = client.post("/v1/auth/login", content=b'{"email": ', headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.headers.get_list("set-cookie") == []
    assert _sellers() == sellers and _sessions() == sessions


# ───────────────────────────── 이미 발급된 local 세션은 그대로 ─────────────────────────────

def test_existing_local_session_is_not_revoked_by_disabling(client, monkeypatch):
    """차단은 새 발급만이다. 전환 전에 받은 local 세션의 me · 보호 API · refresh · logout 은 그대로 동작한다(자동 폐기 없음)."""
    monkeypatch.setenv(ENV, "enabled")
    r = _login(client, _email())
    access, cookie = r.json()["accessToken"], _refresh_cookie(r)

    monkeypatch.setenv(ENV, "disabled")
    auth = {"Authorization": f"Bearer {access}"}
    assert client.get("/v1/auth/me", headers=auth).status_code == 200
    assert client.get("/v1/library", headers=auth).status_code == 200

    rf = client.post("/v1/auth/refresh", headers={**CSRF, "Cookie": f"pxl_refresh={cookie}"})
    assert rf.status_code == 200, rf.text
    new_access, new_cookie = rf.json()["accessToken"], _refresh_cookie(rf)
    assert new_cookie and new_cookie != cookie
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"}).status_code == 200

    lo = client.post("/v1/auth/logout", headers={**CSRF, "Authorization": f"Bearer {new_access}", "Cookie": f"pxl_refresh={new_cookie}"})
    assert lo.status_code == 204, lo.text
    assert client.get("/v1/auth/me", headers={"Authorization": f"Bearer {new_access}"}).status_code == 401


# ───────────────────────────── 잘못된 값 = 기동 실패 ─────────────────────────────

def _import_app(value: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != ENV}
    if value is not None:
        env[ENV] = value
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


@pytest.mark.parametrize("value", ["", "Enabled", "DISABLED", " disabled", "disabled ", "true", "off", "zq-bad-7781"],
                         ids=["빈 문자열", "Enabled", "DISABLED", "앞 공백", "뒤 공백", "true", "off", "임의 값"])
def test_invalid_value_fails_api_import_without_echoing_value(value):
    p = _import_app(value)
    assert p.returncode != 0
    assert "LocalLoginConfigError" in p.stderr and ENV in p.stderr
    assert "허용 값: enabled · disabled" in p.stderr
    if value == "zq-bad-7781":  # 다른 글자와 겹치지 않는 값으로 입력값 비노출을 본다
        assert value not in p.stderr


@pytest.mark.parametrize("value", [None, "enabled", "disabled"], ids=["미설정", "enabled", "disabled"])
def test_valid_values_import(value):
    p = _import_app(value)
    assert p.returncode == 0, p.stderr[-2000:]
