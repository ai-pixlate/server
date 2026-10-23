"""간이 인증(JWT) 단위 테스트 — DB 불필요."""
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
    tok = s.create_token(42, "x@y.com")
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)
    assert s.get_current_seller(cred) == 42
