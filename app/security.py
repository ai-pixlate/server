"""access 토큰(JWT HS256, stdlib 구현)과 요청 인증.

외부 의존성 없이 표준 라이브러리(hmac/hashlib)로 HS256 JWT를 발급·검증한다.
비밀키는 환경변수 JWT_SECRET 로 주입(기본값은 개발용이므로 실서비스 전 반드시 교체).

세션(S안, app.auth_sessions): access 는 세션 id(`sid`)를 담고, 요청마다 그 세션이 살아 있는지 확인한다.
- 만료(exp)는 TOKEN_EXPIRED(갱신 대상)로, 서명·형식 오류·세션 없는 토큰·폐기·만료된 세션은 UNAUTHORIZED(invalid_token)로 나간다.
- `sid` 가 없는 이전 형식 토큰(세션 도입 전 발급)은 거절한다 — 배포 뒤 기존 로그인은 다시 로그인해야 한다.
- 세션 저장소를 쓸 수 없으면 503 SERVICE_UNAVAILABLE(인증 만료와 구분).
- access 만료 = min(발급 + JWT_TTL, 세션 abs_exp) — access 가 세션의 절대 만료를 넘지 않는다.
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
JWT_TTL = int(os.getenv("JWT_TTL", "900"))  # 액세스 토큰 유효기간(초), 기본 15분(세션 도입 전 기본 24h)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(segment: str) -> str:
    sig = hmac.new(JWT_SECRET.encode(), segment.encode(), hashlib.sha256).digest()
    return _b64url(sig)


def create_token(seller_id: int, email: Optional[str], sid: Optional[str] = None, exp: Optional[int] = None) -> str:
    """seller_id·email·세션 id·만료시각을 담은 HS256 JWT 발급. email 은 소셜 계정이면 None 일 수 있다(검증 쪽은 sub·sid 만 쓴다).
    sid 없이 만든 토큰은 요청 인증에서 거절된다(이전 형식) — 세션 발급은 issue_access 를 쓴다."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {"sub": seller_id, "email": email, "exp": int(exp if exp is not None else time.time() + JWT_TTL)}
    if sid is not None:
        claims["sid"] = sid
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    segment = f"{header}.{payload}"
    return f"{segment}.{_sign(segment)}"


def issue_access(session, email: Optional[str]) -> str:
    """세션의 access — 만료는 min(지금 + JWT_TTL, 세션 abs_exp)."""
    from app import auth_sessions

    return create_token(session.seller_id, email, sid=session.sid, exp=min(auth_sessions.now() + JWT_TTL, session.abs_exp))


def verify_signature(token: str) -> dict:
    """서명·형식만 검증하고 payload 를 돌려준다(만료는 보지 않는다). 실패 시 401 invalid_token."""
    try:
        header_b64, payload_b64, sig = token.split(".")
    except ValueError:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "malformed token"})
    if not hmac.compare_digest(_sign(f"{header_b64}.{payload_b64}"), sig):
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "bad signature"})
    try:
        payload = json.loads(_b64url_decode(payload_b64))
        int(payload["sub"]), int(payload.get("exp", 0))
    except Exception:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "bad payload"})
    return payload


def decode_token(token: str) -> dict:
    """서명·만료를 검증하고 payload(dict)를 반환. 실패 시 401."""
    payload = verify_signature(token)
    if int(payload.get("exp", 0)) <= _now():
        raise HTTPException(status_code=401, detail={"code": "TOKEN_EXPIRED", "message": "token expired"})
    return payload


def _now() -> int:
    from app import auth_sessions

    return auth_sessions.now()


def session_unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={"code": "SERVICE_UNAVAILABLE", "message": "session store unavailable"})


def active_session(payload: dict):
    """payload 의 sid 세션이 살아 있고 같은 판매자면 그 세션. 아니면 401 invalid_token. 저장소 장애는 503."""
    from app import auth_sessions

    sid = payload.get("sid")
    if not isinstance(sid, str):
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "token has no session"})
    try:
        s = auth_sessions.get_active(sid)
    except auth_sessions.Unavailable:
        raise session_unavailable()
    if s is None or s.seller_id != int(payload["sub"]):
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "session not active"})
    return s


# HTTPBearer: Swagger UI에 🔓 Authorize 버튼을 띄운다. auto_error=False 로 두어
# 토큰이 없을 때 기본 403 대신 우리 표준 401(UNAUTHORIZED) 을 반환한다.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="로그인(POST /v1/auth/login)으로 받은 accessToken 값을 그대로 입력",
)


def get_current_seller(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> int:
    """Bearer 토큰(JWT)에서 인증된 seller_id를 추출하는 의존성. 세션이 살아 있어야 한다(활동 시각은 바꾸지 않는다)."""
    if cred is None or not cred.credentials:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "missing bearer token"},
        )
    payload = decode_token(cred.credentials)
    return active_session(payload).seller_id
