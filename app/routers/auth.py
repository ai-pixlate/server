"""AUTH — 인증/셀러 (API-AUTH-01~05, 🟢9월).

간이 인증(JWT HS256). 로그인은 이메일로 셀러를 find-or-create 후 토큰 발급한다.
비밀번호는 간이 인증이라 검증하지 않는다(seller 테이블에 비번 컬럼 없음). 정식 이메일 로그인이 아니다(개발용).

세션(S안, app.auth_sessions — 2026-11-10 2차 시연용 구현 방향 결정. 정본 「refresh 서버 상태 없음」을 바꾼다):
- 로그인 성공 = 새 세션 + access(본문) + refresh(Set-Cookie). 요청에 실려 온 검증된 refresh 쿠키의 세션은 새 세션 생성과 같은
  스크립트에서 폐기한다 — 이전 계정 탭의 access 로 요청이 실행되지 않게 하는 BE 구현 선택(PM 11-10 「한 브라우저 한 계정」을 돕는다).
  완전한 보장은 아니다: 같은 이전 쿠키로 로그인 두 건이 겹치면 새 세션이 둘 남는다(개발용 한계 — 소셜 로그인(C)의 결속 기준 교체는 간이 로그인에 적용되지 않아 그대로 남는다. 간이 로그인을 환경별로 끄는 전환으로 해소).
- refresh·logout 은 Origin 허용 목록(PIXLATE_AUTH_ORIGINS) 정확 일치를 먼저 본다. 누락·null·목록 밖·설정 없음 → 401 origin_not_allowed.
- refresh = 쿠키의 refresh 로만(바디·Bearer 아님, CSRF 헤더 X-Requested-With 필수). 성공만 활동 시각을 갱신한다.
  거절 응답은 Set-Cookie 를 보내지 않는다(늦게 도착한 거절이 새 쿠키를 지우지 않게).
- 로그아웃 = 요청의 자격이 가리키는 살아 있는 세션만 폐기 + 쿠키 삭제 → 204(끝낼 세션이 없어도 204). 판정은 logout() 설명.

계정 식별(0015): 간이 로그인은 `auth_provider = 'local'` 계정만 찾고 만든다. 소셜 계정(카카오·Google — 제공자 + 회원번호로 식별)은
이메일이 같아도 이 경로로 찾지 않는다. 이메일만으로 소셜 계정의 토큰을 받는 길을 막기 위해서다(PRD v3.5.10 F-ACC-05 ④).
"""
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth_sessions, social_auth
from app.db import get_db
from app.errors import error_body
from app.security import active_session, get_current_seller, issue_access, session_unavailable, verify_signature

router = APIRouter(tags=["Auth"])
log = logging.getLogger("pixlate.auth")

REFRESH_COOKIE = "pxl_refresh"


def _cookie_path() -> str:
    # 브라우저가 보는 경로 기준(FE 프록시 `/auth/*` → 서버 `/v1/auth/*`) — 정본 Path=/auth
    return os.getenv("PIXLATE_AUTH_COOKIE_PATH", "/auth")


def _cookie_secure() -> bool:
    return os.getenv("PIXLATE_AUTH_COOKIE_SECURE", "1") != "0"


def _set_refresh_cookie(response: Response, token: str, session) -> None:
    response.set_cookie(REFRESH_COOKIE, token, max_age=max(0, session.abs_exp - auth_sessions.now()), path=_cookie_path(),
                        secure=_cookie_secure(), httponly=True, samesite="strict")


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=_cookie_path(), secure=_cookie_secure(), httponly=True, samesite="strict")


def _allowed_origins() -> set[str]:
    """쿠키 엔드포인트(refresh·logout)가 받는 Origin — 설정 PIXLATE_AUTH_ORIGINS(쉼표, `scheme://host[:port]` 정확 일치).
    설정이 없으면 빈 집합 = 모두 거절(운영 Origin 을 추측해 넣지 않는다). `*` 는 허용 목록으로 쓰지 않는다."""
    raw = os.getenv("PIXLATE_AUTH_ORIGINS", "")
    return {o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip() and o.strip() != "*"}


def _require_allowed_origin(request: Request) -> None:
    """Origin 누락·`null`·허용 목록 밖이면 401 + origin_not_allowed. 아무것도 바꾸기 전에 부른다(회전·폐기·쿠키 변경 없음).
    HTTP 상태(401)는 BE 제안 — FE는 사유만 수용했고 상태는 확인 전(초안 403, FORBIDDEN 은 15종 밖)."""
    origin = (request.headers.get("origin") or "").strip().rstrip("/").lower()
    if not origin or origin == "null" or origin not in _allowed_origins():
        log.warning("auth.origin_rejected path=%s origin_present=%s", request.url.path, bool(origin))
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "origin not allowed", "reason": "origin_not_allowed"})


def _refresh_rejected(reason: str) -> HTTPException:
    # 오류 응답에는 Set-Cookie 가 없다(errors 처리기가 본문만 만든다)
    return HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "refresh rejected", "reason": reason})


class LoginRequest(BaseModel):
    email: str = Field(examples=["seller@example.com"])
    password: str = Field(default="", examples=["password123!"])  # 간이 인증: 미검증


class ConsentRequest(BaseModel):
    agreed: bool = True


def _seller_by_id(db: Session, seller_id: int):
    return db.execute(
        text("SELECT id, email, account_type FROM seller WHERE id = :s"),
        {"s": seller_id},
    ).mappings().first()


@router.post("/auth/login", summary="API-AUTH-01 셀러 로그인/JWT 발급 (DB)")
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": "email is required",
                                                    "fields": [{"loc": ["body", "email"], "msg": "email is required"}]})

    r = db.execute(  # local 계정만 — 같은 이메일의 소셜 계정은 고르지 않는다(같은 이메일 local 이 여럿일 때의 선택은 이 변경 전과 같음)
        text("SELECT id, email FROM seller WHERE auth_provider = 'local' AND email = :e"), {"e": email},
    ).mappings().first()
    if not r:  # 간이 인증: 최초 로그인 시 셀러 자동 생성(개발 편의)
        r = db.execute(
            text(
                "INSERT INTO seller (cognito_sub, email, account_type, auth_provider) "
                "VALUES (:sub, :e, 'individual', 'local') RETURNING id, email"
            ),
            {"sub": f"local:{email}", "e": email},
        ).mappings().one()
        db.commit()

    try:  # 새 세션 생성 + 이 브라우저의 이전 세션 폐기를 한 스크립트로(함께 일어나거나 함께 안 일어난다)
        session, refresh_token, replaced_seller = auth_sessions.create(r["id"], replacing=request.cookies.get(REFRESH_COOKIE))
    except auth_sessions.Unavailable:
        raise session_unavailable()
    if replaced_seller is not None:
        log.info("auth.login_replaced_browser_session same_seller=%s", replaced_seller == r["id"])
    _set_refresh_cookie(response, refresh_token, session)
    return {"accessToken": issue_access(session, r["email"]), "seller": {"id": r["id"], "email": r["email"]}}


@router.post("/auth/refresh", summary="API-AUTH-02 액세스 토큰 갱신 (세션)")
def refresh(request: Request, response: Response, x_requested_with: str = Header(..., alias="X-Requested-With"),
            db: Session = Depends(get_db)):
    """쿠키의 refresh 로 회전한다. 성공만 활동 시각을 갱신한다.
    401 UNAUTHORIZED + reason: refresh_rotated(직전 세대·유예 안 — 세션 그대로) ·
    refresh_reused(직전 세대·유예 밖 또는 더 오래된 발급 세대 — 그 세션만 폐기) ·
    refresh_invalid(쿠키 없음·형식·서명 오류·발급한 적 없는 세대·폐기·비활성 7일·절대 30일 지남). 저장소 장애 503."""
    _require_allowed_origin(request)
    cookie = request.cookies.get(REFRESH_COOKIE)
    # 회전 전에 판매자를 먼저 읽는다 — DB 장애면 회전하지 않고 실패하므로 같은 쿠키로 다시 시도할 수 있다.
    # 회전 뒤에 남는 실패 경로(access 발급·응답 전송)는 응답 유실과 같다: 이전 쿠키는 유예 안 refresh_rotated, 유예 뒤 refresh_reused.
    parsed = auth_sessions.parse_refresh(cookie)
    r = None
    try:
        current = auth_sessions.get_active(parsed[0]) if parsed else None
    except auth_sessions.Unavailable:
        raise session_unavailable()
    if current is not None:
        r = _seller_by_id(db, current.seller_id)
        if not r:
            try:
                gone = auth_sessions.match_refresh(cookie)  # 검증된 쿠키일 때만 그 세션을 지운다
                if gone is not None:
                    auth_sessions.revoke(gone.sid)
            except auth_sessions.Unavailable:
                pass
            raise HTTPException(status_code=404, detail={"code": "SELLER_NOT_FOUND", "message": "seller not found"})
    try:
        rot = auth_sessions.rotate(cookie)
    except auth_sessions.Unavailable:
        raise session_unavailable()
    if rot.status != "ok":
        if rot.status in ("reused", "reused_old"):
            log.warning("auth.refresh_reused session_revoked=1 older_generation=%s", rot.status == "reused_old")
        reason = {"rotated": "refresh_rotated", "reused": "refresh_reused", "reused_old": "refresh_reused"}.get(rot.status, "refresh_invalid")
        raise _refresh_rejected(reason)
    if r is None or r["id"] != rot.session.seller_id:  # 위 조회 뒤 경합으로 바뀐 경우만(실제로는 생기지 않는다)
        r = _seller_by_id(db, rot.session.seller_id) or {"email": None}
    _set_refresh_cookie(response, rot.refresh_token, rot.session)
    return {"accessToken": issue_access(rot.session, r["email"])}


def _bearer_credential(request: Request) -> auth_sessions.Credential:
    """로그아웃용 Bearer 분류(만료 시각은 보지 않는다 — access 만료 상태에서도 로그아웃할 수 있게).
    absent = Authorization 없음 / invalid = 형식·서명 오류, 세션과 판매자 불일치 /
    ended = 서명은 맞지만 세션이 이미 끝났거나 세션 없는 이전 형식 / live = 살아 있는 세션. 저장소 장애는 503."""
    auth = request.headers.get("authorization") or ""
    if not auth:
        return auth_sessions.Credential("absent")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return auth_sessions.Credential("invalid")
    try:
        payload = verify_signature(token)
    except HTTPException:
        return auth_sessions.Credential("invalid")
    sid = payload.get("sid")
    if not isinstance(sid, str):
        return auth_sessions.Credential("ended")  # 세션 도입 전 형식 — 끝낼 세션이 없다
    try:
        s = auth_sessions.get_active(sid)
    except auth_sessions.Unavailable:
        raise session_unavailable()
    if s is None:
        return auth_sessions.Credential("ended")
    if s.seller_id != int(payload["sub"]):
        return auth_sessions.Credential("invalid")
    return auth_sessions.Credential("live", s)


def _logout_rejected(message: str) -> HTTPException:
    # 검증할 수 없는 자격 — 아무것도 바꾸지 않는다(쿠키 삭제도 없음). FE 는 Authorization 없이 한 번 다시 보낸다.
    return HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": message})


@router.post("/auth/logout", summary="API-AUTH-03 로그아웃/세션 폐기", status_code=204)
def logout(request: Request, response: Response, x_requested_with: str = Header(..., alias="X-Requested-With")):
    """요청의 자격이 가리키는 **살아 있는 세션만** 폐기하고 refresh 쿠키를 지운다 → 204.
    판정(FE 11-10 회신 반영):
    - Bearer·쿠키를 각각 absent / invalid / ended / live 로 분류한다(쿠키는 서명이 맞으면 세대와 관계없이 그 세션을 식별).
    - Bearer 가 invalid(형식·서명 오류) → 401 invalid_token, 아무것도 바꾸지 않음(쿠키가 유효해도 — FE 는 Authorization 없이 재요청).
    - 쿠키가 invalid 이고 Authorization 이 없음(쿠키만) → 401 refresh_invalid + refresh 쿠키 삭제(FE 11-10 최종 합의).
      그 쿠키가 어느 세션인지 믿을 수 없으므로 어떤 세션도 폐기하지 않는다 — FE 는 「로그아웃 대기」를 풀고 G1 로 가지만 정상 로그아웃과 구분한다.
    - 쿠키가 invalid 이고 Bearer 는 서명이 맞지만 끝낼 세션이 없음(ended) → 401 invalid_token, 아무것도 바꾸지 않음(FE 는 Authorization 없이 재요청).
    - 그 밖: live 세션을 모두 폐기(다른 계정이면 둘 다 — PM 11-10). 끝낼 세션이 없어도(이미 종료·자격 없음) 정상 재로그아웃으로 보고 204.
    - Origin·CSRF 실패(401 origin_not_allowed·422)와 저장소 장애(503)는 204 로 바꾸지 않는다. 계정의 다른 브라우저·기기 세션은 건드리지 않는다."""
    _require_allowed_origin(request)
    bearer = _bearer_credential(request)
    if bearer.kind == "invalid":
        raise _logout_rejected("bearer token is malformed or unverifiable")
    try:
        cookie = auth_sessions.identify_refresh(request.cookies.get(REFRESH_COOKIE))
    except auth_sessions.Unavailable:
        raise session_unavailable()
    if cookie.kind == "invalid" and bearer.kind == "absent":
        log.info("auth.logout bearer=absent cookie=invalid revoked=0 cookie_cleared=1")
        out = JSONResponse(status_code=401, content=error_body(request, "UNAUTHORIZED", "refresh cookie is unverifiable",
                                                                {"reason": "refresh_invalid"}))
        _clear_refresh_cookie(out)  # 오류 응답 중 이 경우만 쿠키를 지운다 — 같은 쿠키로 대기가 끝나지 않는 것을 막는다
        return out
    if cookie.kind == "invalid" and bearer.kind != "live":
        raise _logout_rejected("refresh cookie is unverifiable")
    live = [c.session for c in (bearer, cookie) if c.kind == "live"]
    bind = request.cookies.get(social_auth.BIND_COOKIE)
    try:
        # 소셜 로그인 결속(C): 로그아웃 시각 기록 · 진행 중 시도 무효 · 그 결속으로 만든 현재 세션 폐기 — 늦게 끝나는 로그인 완료가 세션을 만들지 않게
        bind_revoked = social_auth.logout_binding(bind, *social_auth.binding_ttls())
        auth_sessions.revoke(*[x.sid for x in live])
    except auth_sessions.Unavailable:
        raise session_unavailable()
    if bind_revoked:
        log.info("auth.logout_binding_session_revoked")
    if social_auth.valid_binding(bind):
        response.delete_cookie(social_auth.BIND_COOKIE, path=_cookie_path(), secure=_cookie_secure(), httponly=True, samesite="strict")
    if len({x.sid for x in live}) == 2:
        log.warning("auth.logout_session_mismatch same_seller=%s", live[0].seller_id == live[1].seller_id)
    log.info("auth.logout bearer=%s cookie=%s revoked=%d", bearer.kind, cookie.kind, len({x.sid for x in live}))
    _clear_refresh_cookie(response)
    response.status_code = 204
    return response


@router.get("/auth/me", summary="API-AUTH-04 현재 셀러 조회 (DB)")
def me(seller_id: int = Depends(get_current_seller), db: Session = Depends(get_db)):
    r = _seller_by_id(db, seller_id)
    if not r:
        raise HTTPException(status_code=404, detail={"code": "SELLER_NOT_FOUND", "message": "seller not found"})
    brand_id = db.execute(
        text("SELECT id FROM brand WHERE seller_id = :s ORDER BY id LIMIT 1"),
        {"s": seller_id},
    ).scalar()
    return {"id": r["id"], "email": r["email"], "accountType": r["account_type"], "brandId": brand_id}


@router.post("/consents", summary="API-AUTH-05 책임 한계 동의 기록")
def create_consent(body: ConsentRequest, seller_id: int = Depends(get_current_seller)):
    # 동의 저장 테이블 미도입 — 간이 응답(추후 consent 테이블 연결).
    return {"consentId": f"consent-{seller_id}", "consentType": "liability_limit", "agreed": body.agreed}
