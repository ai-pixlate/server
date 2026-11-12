"""AUTH-06 소셜 로그인(카카오·Google) — start · 완료 (C).

공개 계약: docs/openapi.yaml authSocialStart · authSocial · 공통 규칙 「소셜 로그인」. 서버 내부 구현은 app.social_auth.
- 판정 순서: 요청 형식(422) → 제공자 켜짐(404 의도적 비활성 / 503 설정 오류) → Origin(401 origin_not_allowed) → 시도 점유 → 제공자 → 계정 → 세션 발급.
- 오류 응답에는 Set-Cookie 가 없다. 성공 응답만 refresh 쿠키를 주고 결속 쿠키를 지운다.
- 계정: (제공자, 회원번호)로 찾고 없으면 만든다(자동 가입). 이메일로 찾거나 합치지 않는다.
  이메일·이름 저장 = 확인된 값만(카카오 is_email_valid·is_email_verified, Google email_verified), 제공자가 철회·무효를 명시하면 지우고,
  필드 누락·조회 실패면 기존 값을 그대로 둔다(계약 정리 2.1 BE 안). 255자를 넘는 값은 저장하지 않는다.
"""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import auth_sessions, social_auth
from app.db import get_db
from app.routers.auth import REFRESH_COOKIE, _cookie_path, _cookie_secure, _require_allowed_origin, _set_refresh_cookie
from app.security import issue_access, session_unavailable

router = APIRouter(tags=["Auth"])
log = logging.getLogger("pixlate.auth.social")


class SocialLoginRequest(BaseModel):
    provider: Literal["kakao", "google"]
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=512)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "not found"})


def _unavailable(reason: Optional[str] = None) -> HTTPException:
    detail = {"code": "SERVICE_UNAVAILABLE", "message": "provider unavailable" if reason else "service unavailable"}
    if reason:
        detail["reason"] = reason
    return HTTPException(status_code=503, detail=detail)


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "login failed", "reason": reason})


def _config(provider: str) -> social_auth.Config:
    try:
        return social_auth.config(provider)
    except social_auth.Disabled:
        raise _not_found()
    except social_auth.Misconfigured:
        raise _unavailable()


def _cookie_kw() -> dict:
    return {"path": _cookie_path(), "secure": _cookie_secure(), "httponly": True, "samesite": "strict"}


@router.post("/auth/social/{provider}/start", summary="API-AUTH-06 소셜 로그인 시작")
def social_start(provider: str, request: Request, x_requested_with: str = Header(..., alias="X-Requested-With")):
    cfg = _config(provider)
    _require_allowed_origin(request)
    try:
        started = social_auth.start(cfg, request.cookies.get(social_auth.BIND_COOKIE))
    except auth_sessions.Unavailable:
        raise session_unavailable()
    resp = JSONResponse({"authorizeUrl": started.authorize_url}, headers={"Cache-Control": "no-store"})
    resp.set_cookie(social_auth.BIND_COOKIE, started.binding, max_age=started.max_age_s, **_cookie_kw())
    return resp


def _find(db: Session, provider: str, subject: str):
    return db.execute(text("SELECT id, email FROM seller WHERE auth_provider = :p AND provider_subject = :s"),
                      {"p": provider, "s": subject}).mappings().first()


def _account(db: Session, ident: social_auth.Identity):
    """(제공자, 회원번호) 계정 — 없으면 만든다. 동시 첫 로그인은 유일 인덱스(uq_seller_provider_subject)로 한 행만 남는다."""
    (email, email_st), (name, name_st) = ident.email, ident.name
    row = _find(db, ident.provider, ident.subject)
    if row is None:
        row = db.execute(text(
            "INSERT INTO seller (cognito_sub, email, account_type, auth_provider, provider_subject, display_name) "
            "VALUES (NULL, :e, 'individual', :p, :s, :n) "
            "ON CONFLICT (auth_provider, provider_subject) WHERE provider_subject IS NOT NULL DO NOTHING RETURNING id, email"),
            {"e": email if email_st == "set" else None, "p": ident.provider, "s": ident.subject,
             "n": name if name_st == "set" else None}).mappings().first()
        if row is not None:
            db.commit()
            log.info("oauth.account_created provider=%s", ident.provider)
            return row
        row = _find(db, ident.provider, ident.subject)  # 다른 요청이 먼저 만들었다 — 그 행에 아래 갱신 규칙을 적용
    sets, params = [], {"id": row["id"]}
    if email_st in ("set", "clear"):
        sets.append("email = :e")
        params["e"] = email if email_st == "set" else None
    if name_st in ("set", "clear"):
        sets.append("display_name = :n")
        params["n"] = name if name_st == "set" else None
    if sets:
        row = db.execute(text(f"UPDATE seller SET {', '.join(sets)} WHERE id = :id RETURNING id, email"), params).mappings().one()
    db.commit()
    return row


@router.post("/auth/social", summary="API-AUTH-06 소셜 로그인 완료(첫 로그인이면 계정 생성)")
def social_complete(body: SocialLoginRequest, request: Request, x_requested_with: str = Header(..., alias="X-Requested-With"),
                    db: Session = Depends(get_db)):
    cfg = _config(body.provider)
    _require_allowed_origin(request)
    try:
        c = social_auth.claim(cfg, body.state, request.cookies.get(social_auth.BIND_COOKIE))
    except auth_sessions.Unavailable:
        raise session_unavailable()  # 점유 전 — 시도·코드를 쓰지 않았다
    if c.status == "busy":
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE", "message": "login is in progress", "reason": "login_in_progress"})
    if c.status != "ok":
        raise _unauthorized("state_invalid")

    try:  # 점유 뒤 — 어떤 실패든 이 시도를 닫는다(같은 코드 재전송은 받지 않는다)
        if cfg.provider == "kakao":
            ident = social_auth.kakao_identity(cfg, body.code, c.verifier, lambda sub: _find(db, "kakao", sub) is not None)
        else:
            ident = social_auth.google_identity(cfg, body.code, c.verifier, c.nonce)
        seller = _account(db, ident)
    except social_auth.AuthFailed:
        social_auth.fail(c)
        raise _unauthorized("provider_auth_failed")
    except social_auth.ProviderUnavailable:
        social_auth.fail(c)
        raise _unavailable("provider_unavailable")
    except social_auth.Misconfigured:
        social_auth.fail(c)
        raise _unavailable()
    except SQLAlchemyError as e:
        db.rollback()
        social_auth.fail(c)
        log.error("oauth.account_store_failed err=%s", type(e).__name__)
        raise _unavailable()
    except Exception:
        social_auth.fail(c)
        raise

    try:
        status, session, refresh_token = social_auth.issue(c, seller["id"], request.cookies.get(REFRESH_COOKIE))
    except auth_sessions.Unavailable:
        social_auth.fail(c)
        raise session_unavailable()
    if status != "ok":  # 점유 상한·시도 수명이 지났거나, 그 사이 로그아웃·새 시작이 있었다 — 세션을 만들지 않았다
        raise _unauthorized("state_invalid")
    log.info("oauth.login provider=%s", cfg.provider)
    resp = JSONResponse({"accessToken": issue_access(session, seller["email"]), "seller": {"id": seller["id"], "email": seller["email"]}},
                        headers={"Cache-Control": "no-store"})
    _set_refresh_cookie(resp, refresh_token, session)
    resp.delete_cookie(social_auth.BIND_COOKIE, **_cookie_kw())
    return resp
