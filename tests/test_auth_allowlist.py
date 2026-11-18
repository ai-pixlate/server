"""로그인 허용 목록(PM 11-18, 시험 기간 운영 설정) — 스위치 PIXLATE_AUTH_ALLOWLIST · 목록 PIXLATE_AUTH_ALLOWED_EMAILS.

- 스위치 미설정·disabled = 기존 동작(누구나). enabled + 빈 목록 = 아무도 안 됨. 허용 값 밖이면 API 기동 실패(입력값 비노출).
- 소셜 완료: 목록 밖이면 계정을 만들거나 고치지 않고 세션·쿠키도 없다 → 401 account_not_allowed, 시도는 닫힌다.
  판정 이메일 = 이번에 확인된 이메일 > (값을 못 받았으면) 저장된 계정 이메일 > 없음(거절).
- refresh: 세션의 판매자 이메일이 목록 밖이면 회전하지 않고 세션 폐기 → 401 account_not_allowed(Set-Cookie 없음). 다음 refresh 는 refresh_invalid.
- 간이 로그인: 목록 밖 이메일이면 판매자·세션을 만들지 않는다 → 401 account_not_allowed.
제공자 대역 · 실제 PostgreSQL · 실제 Redis 는 tests/test_auth_social.py 의 고정물을 그대로 쓴다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from tests.exec_db import requires_db
from tests.test_auth_social import (  # noqa: F401 — 고정물은 이름으로 가져와야 이 모듈에서 쓸 수 있다
    Browser, _google, _kakao, _no_cookies, _post, _reason, _seller, _sessions, _uid,
    client, clock, dburl, env, other_key, prov, rds, requires_redis, rsa_key,
)

ROOT = Path(__file__).resolve().parents[1]
SWITCH, EMAILS = "PIXLATE_AUTH_ALLOWLIST", "PIXLATE_AUTH_ALLOWED_EMAILS"


def stack(f):
    """실제 PostgreSQL · Redis 가 필요한 시험(판정 함수 · 기동 시험은 없어도 돈다)."""
    return requires_db(requires_redis(f))


@pytest.fixture(autouse=True)
def _clean_allowlist_env(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.delenv(EMAILS, raising=False)


def _on(monkeypatch, emails: str | None):
    monkeypatch.setenv(SWITCH, "enabled")
    if emails is None:
        monkeypatch.delenv(EMAILS, raising=False)
    else:
        monkeypatch.setenv(EMAILS, emails)


def _attempt(b: Browser, provider: str, profile: dict):
    url = b.start(provider).json()["authorizeUrl"]
    code, state = b.p.issue_code(provider, url, profile)
    return code, state, b.complete(provider, code, state)


# ───────────────────────────── 판정 함수 ─────────────────────────────

def test_is_allowed_rules(monkeypatch):
    from app import auth_allowlist as al

    assert al.is_allowed("anyone@example.com") and al.is_allowed(None)  # 미설정 = 기존 동작
    monkeypatch.setenv(SWITCH, "disabled")
    assert al.is_allowed("anyone@example.com")
    _on(monkeypatch, None)
    assert not al.is_allowed("a@example.com")  # 켜짐 + 목록 없음 = 아무도 안 됨
    _on(monkeypatch, " , ,")
    assert not al.is_allowed("a@example.com")  # 켜짐 + 빈 항목뿐 = 아무도 안 됨
    _on(monkeypatch, " A@Example.com , b@example.com ")
    assert al.is_allowed("a@example.com") and al.is_allowed("  B@EXAMPLE.COM ")
    assert not al.is_allowed("c@example.com") and not al.is_allowed(None) and not al.is_allowed("")
    assert not al.is_allowed("a@example.com.evil")  # 부분 일치 아님


def _import_app(value: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != SWITCH}
    if value is not None:
        env[SWITCH] = value
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


@pytest.mark.parametrize("value", ["", "Enabled", "on", "true", "zq-bad-5521"], ids=["빈 문자열", "Enabled", "on", "true", "임의 값"])
def test_invalid_switch_fails_api_import_without_echoing_value(value):
    p = _import_app(value)
    assert p.returncode != 0
    assert "AllowlistConfigError" in p.stderr and SWITCH in p.stderr and "허용 값: enabled · disabled" in p.stderr
    if value == "zq-bad-5521":
        assert value not in p.stderr


@pytest.mark.parametrize("value", [None, "enabled", "disabled"], ids=["미설정", "enabled", "disabled"])
def test_valid_switch_imports(value):
    p = _import_app(value)
    assert p.returncode == 0, p.stderr[-2000:]


# ───────────────────────────── 소셜 로그인 완료 ─────────────────────────────

@stack
@pytest.mark.parametrize("provider", ["kakao", "google"])
def test_social_not_in_list_creates_nothing_and_closes_attempt(client, prov, rds, dburl, monkeypatch, provider):
    _on(monkeypatch, "member@example.com")
    subject = _uid() if provider == "kakao" else f"g-{uuid.uuid4().hex[:12]}"
    profile = _kakao(subject, email="outsider@example.com") if provider == "kakao" else _google(subject, email="outsider@example.com")
    before = _sessions(rds)
    b = Browser(client, prov)
    code, state, r = _attempt(b, provider, profile)
    assert r.status_code == 401 and _reason(r) == "account_not_allowed" and _no_cookies(r), r.text
    assert _seller(dburl, provider, subject) == [] and _sessions(rds) == before
    again = b.complete(provider, code, state)  # 시도는 닫혔다 — 같은 코드 재전송은 처음부터
    assert again.status_code == 401 and _reason(again) == "state_invalid"


@stack
def test_social_in_list_logs_in_case_insensitive(client, prov, dburl, monkeypatch):
    _on(monkeypatch, "other@example.com, Member@Example.com")
    uid, sub = _uid(), f"g-{uuid.uuid4().hex[:12]}"
    assert Browser(client, prov).login("kakao", _kakao(uid, email="member@example.com")).status_code == 200
    assert Browser(client, prov).login("google", _google(sub, email="MEMBER@example.com")).status_code == 200
    assert len(_seller(dburl, "kakao", uid)) == 1 and len(_seller(dburl, "google", sub)) == 1


@stack
def test_social_switch_on_with_empty_list_rejects_everyone(client, prov, dburl, monkeypatch):
    _on(monkeypatch, None)
    uid = _uid()
    _, _, r = _attempt(Browser(client, prov), "kakao", _kakao(uid, email="member@example.com"))
    assert r.status_code == 401 and _reason(r) == "account_not_allowed" and _seller(dburl, "kakao", uid) == []


@stack
def test_social_switch_off_keeps_existing_behavior(client, prov, dburl, monkeypatch):
    monkeypatch.setenv(SWITCH, "disabled")
    monkeypatch.setenv(EMAILS, "member@example.com")
    uid = _uid()
    assert Browser(client, prov).login("kakao", _kakao(uid, email="outsider@example.com")).status_code == 200


@stack
def test_social_unverified_email_is_rejected_even_if_listed(client, prov, dburl, monkeypatch):
    _on(monkeypatch, "member@example.com")
    sub, uid = f"g-{uuid.uuid4().hex[:12]}", _uid()
    _, _, r = _attempt(Browser(client, prov), "google", _google(sub, email="member@example.com", email_verified=False))
    assert r.status_code == 401 and _reason(r) == "account_not_allowed" and _seller(dburl, "google", sub) == []
    _, _, r = _attempt(Browser(client, prov), "kakao", _kakao(uid, email="member@example.com", verified=False))
    assert r.status_code == 401 and _reason(r) == "account_not_allowed" and _seller(dburl, "kakao", uid) == []


@stack
def test_social_existing_account_without_fresh_email_uses_stored_email(client, prov, dburl, monkeypatch):
    """카카오 사용자 정보 조회 실패(기존 계정은 로그인 — 프로필 keep)면 저장된 이메일로 판정한다. 목록 밖이면 계정을 고치지 않는다."""
    uid = _uid()
    Browser(client, prov).login("kakao", _kakao(uid, email="member@example.com"))  # 스위치 꺼진 상태에서 만든 계정
    prov.hook["kakao_me"] = lambda: httpx.Response(500)
    _on(monkeypatch, "member@example.com")
    assert Browser(client, prov).login("kakao", _kakao(uid, email="whatever@example.com")).status_code == 200
    _on(monkeypatch, "someone-else@example.com")
    _, _, r = _attempt(Browser(client, prov), "kakao", _kakao(uid, email="whatever@example.com"))
    assert r.status_code == 401 and _reason(r) == "account_not_allowed"
    assert _seller(dburl, "kakao", uid)[0]["email"] == "member@example.com"
    prov.hook.clear()


# ───────────────────────────── refresh ─────────────────────────────

@stack
def test_refresh_not_in_list_revokes_session_without_set_cookie(client, prov, rds, monkeypatch):
    b = Browser(client, prov)
    b.login("kakao", _kakao(_uid(), email="was-member@example.com"))  # 스위치 꺼진 상태에서 로그인
    before = _sessions(rds)
    _on(monkeypatch, "member@example.com")
    r = _post(client, "/v1/auth/refresh", b.jar())
    assert r.status_code == 401 and _reason(r) == "account_not_allowed" and _no_cookies(r), r.text
    assert len(before - _sessions(rds)) == 1  # 그 세션만 폐기
    again = _post(client, "/v1/auth/refresh", b.jar())
    assert again.status_code == 401 and _reason(again) == "refresh_invalid"


@stack
def test_refresh_in_list_rotates(client, prov, monkeypatch):
    b = Browser(client, prov)
    b.login("kakao", _kakao(_uid(), email="member@example.com"))
    _on(monkeypatch, "member@example.com")
    r = _post(client, "/v1/auth/refresh", b.jar())
    assert r.status_code == 200 and r.json()["accessToken"], r.text


@stack
def test_refresh_account_without_email_is_rejected_when_on(client, prov, rds, monkeypatch):
    b = Browser(client, prov)
    b.login("kakao", _kakao(_uid(), email="x@example.com", verified=False))  # 확인된 이메일 없음 → email NULL
    _on(monkeypatch, "member@example.com")
    r = _post(client, "/v1/auth/refresh", b.jar())
    assert r.status_code == 401 and _reason(r) == "account_not_allowed"


# ───────────────────────────── 간이 로그인 ─────────────────────────────

@stack
def test_local_login_not_in_list_creates_nothing(client, rds, dburl, monkeypatch):
    import sqlalchemy as sa

    _on(monkeypatch, "member@example.com")
    email = f"out-{uuid.uuid4().hex[:8]}@example.com"
    before = _sessions(rds)
    r = _post(client, "/v1/auth/login", json={"email": email, "password": "x"})
    assert r.status_code == 401 and _reason(r) == "account_not_allowed" and _no_cookies(r), r.text
    eng = sa.create_engine(dburl)
    try:
        with eng.connect() as c:
            assert c.execute(sa.text("SELECT count(*) FROM seller WHERE email = :e"), {"e": email}).scalar() == 0
    finally:
        eng.dispose()
    assert _sessions(rds) == before
    ok = _post(client, "/v1/auth/login", json={"email": "Member@example.com", "password": "x"})
    assert ok.status_code == 200, ok.text
