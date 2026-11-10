"""access 토큰(JWT)·요청 인증 단위 테스트 — DB 불필요(세션 저장소는 conftest 의 Redis 또는 대역)."""
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import security as s


def test_token_roundtrip():
    tok = s.create_token(7, "a@b.com")
    assert tok.count(".") == 2
    payload = s.decode_token(tok)
    assert payload["sub"] == 7
    assert payload["email"] == "a@b.com"


def test_forged_signature_rejected():
    tok = s.create_token(7, "a@b.com")
    forged = tok[:-2] + ("aa" if not tok.endswith("aa") else "bb")
    with pytest.raises(HTTPException) as e:
        s.decode_token(forged)
    assert e.value.status_code == 401
    assert e.value.detail["code"] == "INVALID_TOKEN"


def test_malformed_token_rejected():
    with pytest.raises(HTTPException) as e:
        s.decode_token("not-a-jwt")
    assert e.value.detail["code"] == "INVALID_TOKEN"


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setattr(s, "JWT_TTL", -10)  # 발급 즉시 만료
    tok = s.create_token(7, "a@b.com")
    with pytest.raises(HTTPException) as e:
        s.decode_token(tok)
    assert e.value.detail["code"] == "TOKEN_EXPIRED"


def test_get_current_seller_missing_credentials():
    with pytest.raises(HTTPException) as e:
        s.get_current_seller(None)
    assert e.value.status_code == 401
    assert e.value.detail["code"] == "UNAUTHORIZED"


def test_get_current_seller_from_bearer():
    from app import auth_sessions

    session, _, _ = auth_sessions.create(42)
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=s.issue_access(session, "x@y.com"))
    assert s.get_current_seller(cred) == 42


def test_token_without_session_is_rejected():
    """세션 도입 전 형식(sid 없음)은 서명이 맞아도 거절 — 배포 뒤 기존 로그인은 다시 로그인한다."""
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=s.create_token(42, "x@y.com"))
    with pytest.raises(HTTPException) as e:
        s.get_current_seller(cred)
    assert (e.value.status_code, e.value.detail["code"], e.value.detail["message"]) == (401, "INVALID_TOKEN", "token has no session")


def test_revoked_session_token_is_rejected():
    from app import auth_sessions

    session, _, _ = auth_sessions.create(42)
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=s.issue_access(session, None))
    auth_sessions.revoke(session.sid)
    with pytest.raises(HTTPException) as e:
        s.get_current_seller(cred)
    assert (e.value.status_code, e.value.detail["message"]) == (401, "session not active")


def test_access_never_outlives_session_absolute_expiry(monkeypatch):
    from app import auth_sessions

    t0 = 1_800_000_000
    monkeypatch.setattr(auth_sessions, "now", lambda: t0)
    session, _, _ = auth_sessions.create(42)
    monkeypatch.setattr(auth_sessions, "now", lambda: session.abs_exp - 60)  # 절대 만료 1분 전 발급
    payload = s.verify_signature(s.issue_access(session, None))
    assert payload["exp"] == session.abs_exp and payload["sid"] == session.sid
