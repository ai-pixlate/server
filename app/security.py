"""간이 인증 (JWT HS256, stdlib 구현).

Cognito 도입 전 임시 인증. 외부 의존성 없이 표준 라이브러리(hmac/hashlib)로
HS256 JWT를 발급·검증한다. 프로덕션에선 Cognito(또는 PyJWT+RS256)로 교체.
비밀키는 환경변수 JWT_SECRET 로 주입(기본값은 개발용이므로 실서비스 전 반드시 교체).
"""
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.getenv("JWT_SECRET", "pixlate_dev_jwt_secret_change_me")
JWT_TTL = int(os.getenv("JWT_TTL", "86400"))  # 액세스 토큰 유효기간(초), 기본 24h


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(segment: str) -> str:
    sig = hmac.new(JWT_SECRET.encode(), segment.encode(), hashlib.sha256).digest()
    return _b64url(sig)


def create_token(seller_id: int, email: str) -> str:
    """seller_id·email·만료시각을 담은 HS256 JWT 발급."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(
        {"sub": seller_id, "email": email, "exp": int(time.time()) + JWT_TTL},
        separators=(",", ":"),
    ).encode())
    segment = f"{header}.{payload}"
    return f"{segment}.{_sign(segment)}"


def decode_token(token: str) -> dict:
    """서명·만료를 검증하고 payload(dict)를 반환. 실패 시 401."""
    try:
        header_b64, payload_b64, sig = token.split(".")
    except ValueError:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "malformed token"})
    if not hmac.compare_digest(_sign(f"{header_b64}.{payload_b64}"), sig):
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "bad signature"})
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "bad payload"})
    if int(payload.get("exp", 0)) < time.time():
        raise HTTPException(status_code=401, detail={"code": "TOKEN_EXPIRED", "message": "token expired"})
    return payload


# HTTPBearer: Swagger UI에 🔓 Authorize 버튼을 띄운다. auto_error=False 로 두어
# 토큰이 없을 때 기본 403 대신 우리 표준 401(UNAUTHORIZED) 을 반환한다.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="로그인(POST /v1/auth/login)으로 받은 accessToken 값을 그대로 입력",
)


def get_current_seller(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> int:
    """Bearer 토큰(JWT)에서 인증된 seller_id를 추출하는 의존성."""
    if cred is None or not cred.credentials:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "missing bearer token"},
        )
    payload = decode_token(cred.credentials)
    return int(payload["sub"])
