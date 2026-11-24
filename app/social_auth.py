"""소셜 로그인(카카오·Google) — 로그인 시도·브라우저 결속·제공자 호출 (C, 2026-11-10 사용자 착수 승인).

공개 계약은 `docs/openapi.yaml` 공통 규칙 「소셜 로그인」·authSocialStart·authSocial 이다. 이 모듈은 그 계약을 지키는 서버 내부 구현이다.
아래 값·키 이름·스크립트는 공개 계약이 아니다.

■ 설정(환경 변수 — 값은 로그·응답에 남기지 않는다)
    PIXLATE_OAUTH_PROVIDERS          켤 제공자(쉼표: kakao,google). 비었거나 없으면 모두 꺼짐 → 두 경로 404(의도적 비활성)
    PIXLATE_OAUTH_REDIRECT_BASE      FE 오리진(`https://<host>` 또는 `http://localhost[:port]`). redirect_uri = 이 값 + /login/callback/{provider}
    PIXLATE_OAUTH_KAKAO_CLIENT_ID    카카오 REST API 키 · PIXLATE_OAUTH_KAKAO_APP_ID 앱 ID(숫자, access_token_info 대조)
    PIXLATE_OAUTH_KAKAO_CLIENT_SECRET  선택 — 카카오 「Client Secret 사용」이 켜져 있으면 필요(카카오 문서). 없으면 보내지 않는다
    PIXLATE_OAUTH_GOOGLE_CLIENT_ID · PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET  Google 웹 클라이언트
    PIXLATE_OAUTH_ATTEMPT_TTL_S      로그인 시도 수명(초, C 초기 기본값 600)
    PIXLATE_OAUTH_CLAIM_TTL_S        시도 점유 상한(초, C 초기 기본값 60)
    PIXLATE_OAUTH_HTTP_TIMEOUT_S     제공자 호출 하나의 제한 시간(초, 기본 10, 재시도 없음)
  켠 제공자의 필수 값이 빠졌거나 형식이 틀리면 404 로 숨기지 않는다 → 503(사유 없음) + 서버 로그(설정 이름만, 값 없음).
  점유 상한은 「호출 수 × 호출 제한 시간 + 10초」 이상이어야 한다(아니면 설정 오류). 카카오 = 토큰 교환·access_token_info·user/me 3회,
  Google = 토큰 교환 + 인증서(캐시가 비었거나 만료, 또는 모르는 kid) 1회 = 최대 2회.
  공식 문서(2026-11-10 확인): 카카오·Google 모두 인가 코드 유효 시간을 적지 않았다(둘 다 일회용). 시도 수명 10분과의 관계는 실제 제공자 시험에서 확인한다.

■ Redis(세션 저장소와 같은 연결 — app.auth_sessions.client())
    pixlate:oauth:att:<sha256(state)>   시도 — provider · bind(결속 해시) · verifier · nonce · exp · status(pending|claimed|done|failed|expired)
                                        · claim_tok · claim_at · claim_until            TTL = 시도 수명 + 점유 상한
    pixlate:oauth:bind:<sha256(결속)>   브라우저 결속 — attempt(이 브라우저의 마지막 시도) · cur_sid(이 결속으로 만든 현재 세션) · logout_at
  시각은 밀리초. Lua 안에서 Redis TIME 으로 잰다(여러 API 서버의 시계 차이와 무관하게 시도·점유·로그아웃 순서를 한 시계로 비교).
  시험은 clock_ms 를 바꿔 끼워 같은 시계를 고정값으로 준다.

■ 결속 쿠키 `pxl_oauth_bind`(HttpOnly·Secure·SameSite=Strict·Path=/auth, 수명 = 시도 수명)
  start 가 만들고(이미 있으면 그대로 쓴다) 완료 성공 때 지운다. 서버에는 해시만 둔다.
  같은 브라우저의 시도는 결속 하나에 「마지막 시도」 하나뿐이다 — 다시 start 하면 이전 시도는 완료할 수 없다(state_invalid).
  로그아웃은 결속 쿠키가 실려 오면 그 결속의 로그아웃 시각을 적고 진행 중 시도·현재 세션을 끝낸다(app.routers.auth.logout).

■ 세션 발급(issue) — 아래를 Lua 하나로 한다(설계 의도: 사이에 점유 만료·로그아웃·다른 시작이 끼어들지 않게. 보장 여부는 tests/test_auth_social.py).
  ① 이 요청의 점유가 아직 유효(같은 점유 표식·점유 상한 전·시도 수명 전) ② 결속의 마지막 시도가 이 시도 ③ 결속의 로그아웃 시각 < 점유 시각
  ④ 결속의 현재 세션 + 요청 refresh 쿠키의 세션(서명이 맞으면 세대와 관계없이) 폐기 ⑤ 새 세션 생성 · 결속의 현재 세션 = 새 세션 ⑥ 시도 닫음.
  하나라도 아니면 아무 세션도 만들지 않는다(state_invalid). 제공자 호출은 스크립트 밖이라 ①이 늦은 응답을 막는 지점이다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote, urlencode, urlsplit

import httpx
import redis
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import exceptions as google_exceptions
from google.auth import jwt as google_jwt
from google.auth import transport as google_transport
from google.oauth2 import id_token as google_id_token

from app import auth_sessions

log = logging.getLogger("pixlate.auth.social")

PROVIDERS = ("kakao", "google")
BIND_COOKIE = "pxl_oauth_bind"
_ATT = "pixlate:oauth:att:"
_BIND = "pixlate:oauth:bind:"
_NONE_KEY = "pixlate:oauth:none"  # 지울 세션이 없을 때 KEYS 자리를 채우는 이름(만들지 않는다)
_BIND_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

KAKAO_AUTHORIZE = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN = "https://kauth.kakao.com/oauth/token"
KAKAO_TOKEN_INFO = "https://kapi.kakao.com/v1/user/access_token_info"
KAKAO_ME = "https://kapi.kakao.com/v2/user/me"
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPES = {"kakao": "profile_nickname account_email", "google": "openid email profile"}
_MAX_TEXT = 255  # seller.email · display_name 길이 — 넘으면 저장하지 않는다(자르지 않음)

# 시험이 바꿔 끼운다: 제공자 HTTP 전송(httpx.MockTransport 등) · 시계(밀리초, None = Redis TIME)
_transport: Optional[httpx.BaseTransport] = None
clock_ms: Callable[[], Optional[int]] = lambda: None


def set_transport(t: Optional[httpx.BaseTransport]) -> None:
    global _transport
    _transport = t
    _CERTS.clear()


# ───────────────────────────── 오류 ─────────────────────────────

class Disabled(Exception):
    """의도적 비활성(켜지 않은 제공자·모르는 제공자) → 404."""


class Misconfigured(Exception):
    """켰지만 필수 설정이 빠졌거나 잘못됨 → 503(사유 없음). 메시지에는 설정 이름만 담는다(값 없음)."""


class AuthFailed(Exception):
    """제공자가 코드를 거절했거나 회원 확인 실패 → 401 provider_auth_failed."""


class ProviderUnavailable(Exception):
    """제공자 일시 장애(시간 초과·네트워크·5xx·요청 한도) → 503 provider_unavailable."""


# ───────────────────────────── 설정 ─────────────────────────────

@dataclass(frozen=True)
class Config:
    provider: str
    client_id: str
    client_secret: Optional[str]
    app_id: Optional[str]
    redirect_uri: str
    attempt_ttl_s: int
    claim_ttl_s: int
    http_timeout_s: float


def _int_env(name: str, default: int, problems: list[str]) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        problems.append(name)
        return default
    if v <= 0:
        problems.append(name)
    return v


def _redirect_base(problems: list[str]) -> str:
    raw = (os.getenv("PIXLATE_OAUTH_REDIRECT_BASE") or "").strip().rstrip("/")
    u = urlsplit(raw)
    ok = bool(raw) and not u.path and not u.query and not u.fragment and u.hostname and (
        u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")))
    if not ok:
        problems.append("PIXLATE_OAUTH_REDIRECT_BASE")
    return raw


def enabled_providers() -> set[str]:
    raw = os.getenv("PIXLATE_OAUTH_PROVIDERS", "")
    names = {p.strip().lower() for p in raw.split(",") if p.strip()}
    unknown = names - set(PROVIDERS)
    if unknown:
        log.error("oauth.config_unknown_provider count=%d", len(unknown))  # 이름만 셈 — 켜지지 않는다
    return names & set(PROVIDERS)


def config(provider: str) -> Config:
    """Disabled(404) · Misconfigured(503) · Config."""
    if provider not in PROVIDERS or provider not in enabled_providers():
        raise Disabled(provider)
    problems: list[str] = []
    up = provider.upper()
    client_id = (os.getenv(f"PIXLATE_OAUTH_{up}_CLIENT_ID") or "").strip()
    secret = (os.getenv(f"PIXLATE_OAUTH_{up}_CLIENT_SECRET") or "").strip() or None
    app_id = ((os.getenv("PIXLATE_OAUTH_KAKAO_APP_ID") or "").strip() or None) if provider == "kakao" else None
    if not client_id:
        problems.append(f"PIXLATE_OAUTH_{up}_CLIENT_ID")
    if provider == "google" and not secret:
        problems.append("PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET")
    if provider == "kakao" and not (app_id and app_id.isdigit()):
        problems.append("PIXLATE_OAUTH_KAKAO_APP_ID")
    base = _redirect_base(problems)
    attempt = _int_env("PIXLATE_OAUTH_ATTEMPT_TTL_S", 600, problems)
    claim = _int_env("PIXLATE_OAUTH_CLAIM_TTL_S", 60, problems)
    timeout = _int_env("PIXLATE_OAUTH_HTTP_TIMEOUT_S", 10, problems)
    if claim < 3 * timeout + 10:  # 호출 3회가 점유 안에 끝나야 한다
        problems.append("PIXLATE_OAUTH_CLAIM_TTL_S<3*PIXLATE_OAUTH_HTTP_TIMEOUT_S+10")
    if problems:
        log.error("oauth.config_invalid provider=%s settings=%s", provider, ",".join(sorted(set(problems))))
        raise Misconfigured(provider)
    return Config(provider, client_id, secret, app_id, f"{base}/login/callback/{provider}", attempt, claim, float(timeout))


# ───────────────────────────── Redis 스크립트 ─────────────────────────────

_NOW = "local now = ARGV[1] ~= '' and tonumber(ARGV[1]) or (function() local t = redis.call('TIME') return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000) end)()\n"

_START = _NOW + """
-- KEYS: 시도, 결속 · ARGV: now, 시도id, provider, 결속해시, verifier, nonce, 시도수명ms, 점유상한ms
local exp = now + tonumber(ARGV[7])
redis.call('HSET', KEYS[1], 'provider', ARGV[3], 'bind', ARGV[4], 'verifier', ARGV[5], 'nonce', ARGV[6],
           'created', now, 'exp', exp, 'status', 'pending')
redis.call('PEXPIREAT', KEYS[1], exp + tonumber(ARGV[8]))
redis.call('HSET', KEYS[2], 'attempt', ARGV[2])
redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[7]) + tonumber(ARGV[8]) + 60000)
return exp
"""

_CLAIM = _NOW + """
-- KEYS: 시도, 결속 · ARGV: now, 시도id, provider, 결속해시, 점유상한ms, 점유표식
local a = redis.call('HMGET', KEYS[1], 'provider', 'bind', 'exp', 'status', 'claim_until', 'verifier', 'nonce')
if not a[1] then return {'missing'} end
if a[1] ~= ARGV[3] or a[2] ~= ARGV[4] then return {'mismatch'} end  -- 잘못된 요청이 정상 시도를 바꾸지 못한다
if now >= tonumber(a[3]) then
  if a[4] == 'pending' or a[4] == 'claimed' then redis.call('HSET', KEYS[1], 'status', 'expired') end
  return {'expired'}
end
if redis.call('HGET', KEYS[2], 'attempt') ~= ARGV[2] then return {'superseded'} end
if a[4] == 'claimed' then
  if now < tonumber(a[5]) then return {'busy'} end
  redis.call('HSET', KEYS[1], 'status', 'failed')
  return {'claim_expired'}
end
if a[4] ~= 'pending' then return {'used'} end
redis.call('HSET', KEYS[1], 'status', 'claimed', 'claim_tok', ARGV[6], 'claim_at', now, 'claim_until', now + tonumber(ARGV[5]))
return {'ok', a[6], a[7] or ''}
"""

_FAIL = """
-- KEYS: 시도 · ARGV: 점유표식 — 이 요청의 점유일 때만 실패로 닫는다
if redis.call('HGET', KEYS[1], 'status') == 'claimed' and redis.call('HGET', KEYS[1], 'claim_tok') == ARGV[1] then
  redis.call('HSET', KEYS[1], 'status', 'failed')
  return 1
end
return 0
"""

_ISSUE = _NOW + """
-- KEYS: 시도, 결속, 결속의 현재 세션, 쿠키의 세션, 새 세션
-- ARGV: now, 시도id, 점유표식, 기대한 결속 현재 sid('' = 없음), seller, 세션 created, abs, idle, 새 sid
local a = redis.call('HMGET', KEYS[1], 'status', 'claim_tok', 'claim_until', 'exp', 'claim_at')
if a[1] ~= 'claimed' or a[2] ~= ARGV[3] then return {'stale'} end
if now >= tonumber(a[3]) or now >= tonumber(a[4]) then
  redis.call('HSET', KEYS[1], 'status', 'failed')
  return {'late'}
end
local b = redis.call('HMGET', KEYS[2], 'attempt', 'logout_at', 'cur_sid')
if b[1] ~= ARGV[2] then
  redis.call('HSET', KEYS[1], 'status', 'failed')
  return {'superseded'}
end
if b[2] and tonumber(b[2]) >= tonumber(a[5]) then
  redis.call('HSET', KEYS[1], 'status', 'failed')
  return {'logged_out'}
end
if (b[3] or '') ~= ARGV[4] then return {'retry'} end
redis.call('DEL', KEYS[3], KEYS[4])
redis.call('HSET', KEYS[5], 'seller_id', ARGV[5], 'created_at', ARGV[6], 'abs_exp', ARGV[7],
           'last_active', ARGV[6], 'idle_exp', ARGV[8], 'gen', 1)
redis.call('EXPIREAT', KEYS[5], math.min(tonumber(ARGV[7]), tonumber(ARGV[8])))
redis.call('HSET', KEYS[2], 'cur_sid', ARGV[9])
redis.call('HDEL', KEYS[2], 'attempt')
redis.call('HSET', KEYS[1], 'status', 'done')
return {'ok'}
"""

_LOGOUT = _NOW + """
-- KEYS: 결속, 결속의 현재 세션 · ARGV: now, 기대한 현재 sid, 결속 보관ms, 기록 있는 결속만('1' = 서버 기록이 없으면 아무것도 쓰지 않음)
if ARGV[4] == '1' and redis.call('EXISTS', KEYS[1]) == 0 then return {'ok', '', 'absent'} end
local cur = redis.call('HGET', KEYS[1], 'cur_sid') or ''
if cur ~= ARGV[2] then return {'retry'} end
redis.call('DEL', KEYS[2])
redis.call('HSET', KEYS[1], 'logout_at', now)
redis.call('HDEL', KEYS[1], 'attempt', 'cur_sid')
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
return {'ok', cur}
"""


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else ("" if v is None else str(v))


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now_arg() -> str:
    v = clock_ms()
    return "" if v is None else str(int(v))


def _eval(script: str, keys: list[str], *args):
    try:
        return auth_sessions.client().eval(script, len(keys), *keys, *args)
    except redis.RedisError as e:
        raise auth_sessions.Unavailable(str(e)) from e


def valid_binding(value: Optional[str]) -> Optional[str]:
    return value if value and _BIND_RE.match(value) else None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# ───────────────────────────── 시작 ─────────────────────────────

@dataclass(frozen=True)
class Started:
    authorize_url: str
    binding: str
    max_age_s: int


def start(cfg: Config, binding_cookie: Optional[str]) -> Started:
    """새 시도. 이 브라우저의 결속 쿠키가 있으면 그대로 쓰고(마지막 시도만 유효), 없으면 새로 만든다. 저장소 장애 = Unavailable."""
    binding = valid_binding(binding_cookie) or secrets.token_urlsafe(32)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)  # 43자 — RFC 7636 범위(43~128)
    nonce = secrets.token_urlsafe(32) if cfg.provider == "google" else ""
    _eval(_START, [_ATT + _h(state), _BIND + _h(binding)], _now_arg(), _h(state), cfg.provider, _h(binding),
          verifier, nonce, cfg.attempt_ttl_s * 1000, cfg.claim_ttl_s * 1000)
    q = {"response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri, "scope": SCOPES[cfg.provider],
         "state": state, "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    if nonce:
        q["nonce"] = nonce
    base = KAKAO_AUTHORIZE if cfg.provider == "kakao" else GOOGLE_AUTHORIZE
    log.info("oauth.start provider=%s attempt=%s", cfg.provider, _h(state)[:8])
    return Started(f"{base}?{urlencode(q, quote_via=quote)}", binding, cfg.attempt_ttl_s)


# ───────────────────────────── 점유 ─────────────────────────────

@dataclass(frozen=True)
class Claim:
    status: str                  # ok | missing | mismatch | expired | superseded | busy | claim_expired | used
    attempt_id: str = ""
    binding_key: str = ""
    token: str = ""
    verifier: str = ""
    nonce: str = ""


def claim(cfg: Config, state: str, binding_cookie: Optional[str]) -> Claim:
    binding = valid_binding(binding_cookie)
    if binding is None:
        return Claim("mismatch")  # 결속 쿠키 없음 — 어떤 시도도 바꾸지 않는다
    att, tok = _h(state), secrets.token_urlsafe(16)
    res = _eval(_CLAIM, [_ATT + att, _BIND + _h(binding)], _now_arg(), att, cfg.provider, _h(binding), cfg.claim_ttl_s * 1000, tok)
    status = _s(res[0])
    if status != "ok":
        log.info("oauth.claim_rejected provider=%s attempt=%s why=%s", cfg.provider, att[:8], status)
        return Claim(status)
    return Claim("ok", att, _BIND + _h(binding), tok, _s(res[1]), _s(res[2]))


def fail(c: Claim) -> None:
    """점유 뒤 실패 — 그 시도를 닫는다(같은 시도의 다음 요청 = state_invalid). 저장소 장애면 점유 상한 뒤 저절로 쓸 수 없게 된다."""
    try:
        _eval(_FAIL, [_ATT + c.attempt_id], c.token)
    except auth_sessions.Unavailable:
        log.warning("oauth.fail_mark_unavailable attempt=%s", c.attempt_id[:8])


# ───────────────────────────── 세션 발급 ─────────────────────────────

def issue(c: Claim, seller_id: int, refresh_cookie: Optional[str]):
    """→ (상태, Session|None, refresh 토큰|None). 상태 ok | stale | late | superseded | logged_out. 저장소 장애 = Unavailable."""
    cookie = auth_sessions.parse_refresh(refresh_cookie)
    cookie_key = auth_sessions.key(cookie[0]) if cookie else _NONE_KEY
    for _ in range(5):
        try:
            cur = _s(auth_sessions.client().hget(c.binding_key, "cur_sid"))
        except redis.RedisError as e:
            raise auth_sessions.Unavailable(str(e)) from e
        t = auth_sessions.now()
        sid = secrets.token_urlsafe(16)
        abs_exp, idle_exp = t + auth_sessions.ABSOLUTE_TTL_S, t + auth_sessions.IDLE_TTL_S
        res = _eval(_ISSUE, [_ATT + c.attempt_id, c.binding_key, auth_sessions.key(cur) if cur else _NONE_KEY, cookie_key,
                             auth_sessions.key(sid)],
                    _now_arg(), c.attempt_id, c.token, cur, seller_id, t, abs_exp, idle_exp, sid)
        status = _s(res[0])
        if status == "retry":  # 결속의 현재 세션이 읽은 뒤 바뀜 — 다시 읽는다
            continue
        if status != "ok":
            log.info("oauth.issue_rejected attempt=%s why=%s", c.attempt_id[:8], status)
            return status, None, None
        return "ok", auth_sessions.Session(sid, seller_id, t, abs_exp, idle_exp), auth_sessions._token(sid, 1)
    log.warning("oauth.issue_retry_exhausted attempt=%s", c.attempt_id[:8])
    return "stale", None, None


def logout_binding(binding_cookie: Optional[str], attempt_ttl_s: int = 600, claim_ttl_s: int = 60, require_record: bool = False) -> int:
    """로그아웃 때 결속 처리 — 로그아웃 시각 기록, 진행 중 시도 무효, 결속의 현재 세션 폐기. 돌려주는 값 = 폐기한 세션 수(0·1).
    require_record=True: 서버에 그 결속의 기록이 있을 때만 처리한다(검증 불가 refresh 쿠키 로그아웃 — 형식만 맞는 값으로는 아무것도
    쓰거나 지우지 않는다). 폐기 대상은 이 결속 기록의 cur_sid(이 결속으로 발급한 세션)뿐이다 — 요청의 다른 값으로 세션을 고르지 않는다.
    저장소 장애 = auth_sessions.Unavailable(호출하는 쪽이 503)."""
    binding = valid_binding(binding_cookie)
    if binding is None:
        return 0
    bkey = _BIND + _h(binding)
    keep = (attempt_ttl_s + claim_ttl_s) * 1000 + 60000
    for _ in range(5):
        try:
            cur = _s(auth_sessions.client().hget(bkey, "cur_sid"))
        except redis.RedisError as e:
            raise auth_sessions.Unavailable(str(e)) from e
        res = _eval(_LOGOUT, [bkey, auth_sessions.key(cur) if cur else _NONE_KEY], _now_arg(), cur, keep, "1" if require_record else "0")
        if _s(res[0]) == "ok":
            return 1 if _s(res[1]) else 0
    raise auth_sessions.Unavailable("binding logout retry exhausted")


def binding_ttls() -> tuple[int, int]:
    """로그아웃 쪽 결속 보관 시간 계산용(설정이 틀려도 로그아웃은 막지 않는다 — 기본값으로)."""
    p: list[str] = []
    return _int_env("PIXLATE_OAUTH_ATTEMPT_TTL_S", 600, p), _int_env("PIXLATE_OAUTH_CLAIM_TTL_S", 60, p)


# ───────────────────────────── 제공자 호출 ─────────────────────────────

@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    # 프로필 판정: (값, 상태) — 상태 set(확인된 값) | clear(제공자가 철회·무효를 명시) | keep(누락·조회 실패 — 기존 값 유지)
    email: tuple[Optional[str], str]
    name: tuple[Optional[str], str]


def _client(cfg: Config) -> httpx.Client:
    return httpx.Client(timeout=cfg.http_timeout_s, transport=_transport, follow_redirects=False)


def _call(cfg: Config, what: str, method: str, url: str, **kw) -> httpx.Response:
    try:
        with _client(cfg) as c:
            r = c.request(method, url, **kw)
    except httpx.TimeoutException:
        log.warning("oauth.provider_timeout provider=%s call=%s", cfg.provider, what)
        raise ProviderUnavailable(what)
    except httpx.HTTPError as e:
        log.warning("oauth.provider_network provider=%s call=%s err=%s", cfg.provider, what, type(e).__name__)
        raise ProviderUnavailable(what)
    if r.status_code >= 500 or r.status_code == 429:
        log.warning("oauth.provider_status provider=%s call=%s status=%d", cfg.provider, what, r.status_code)
        raise ProviderUnavailable(what)
    return r


def _json(cfg: Config, what: str, r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        log.warning("oauth.provider_bad_json provider=%s call=%s status=%d", cfg.provider, what, r.status_code)
        raise ProviderUnavailable(what)
    if not isinstance(body, dict):
        raise ProviderUnavailable(what)
    return body


# 카카오 문서(문제 해결): KOE101 = 앱 키 오류, KOE010 = client_secret 누락·오류 → 우리 설정 문제
_KAKAO_CONFIG_ERRORS = {"KOE101", "KOE010"}
_KAKAO_RATE_LIMIT = {"KOE237"}


def _token_error(cfg: Config, r: httpx.Response) -> Exception:
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    err, code = str(body.get("error") or ""), str(body.get("error_code") or "")
    log.warning("oauth.token_rejected provider=%s status=%d error=%s error_code=%s", cfg.provider, r.status_code, err[:40], code[:16])
    if code in _KAKAO_RATE_LIMIT:
        return ProviderUnavailable("token")
    if code in _KAKAO_CONFIG_ERRORS or (cfg.provider == "google" and err in ("invalid_client", "unauthorized_client")):
        return Misconfigured(cfg.provider)
    return AuthFailed("token")


def _exchange(cfg: Config, code: str, verifier: str) -> dict:
    data = {"grant_type": "authorization_code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
            "code": code, "code_verifier": verifier}
    if cfg.client_secret:
        data["client_secret"] = cfg.client_secret
    url = KAKAO_TOKEN if cfg.provider == "kakao" else GOOGLE_TOKEN
    r = _call(cfg, "token", "POST", url, data=data,
              headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8", "Accept": "application/json"})
    if r.status_code != 200:
        raise _token_error(cfg, r)
    return _json(cfg, "token", r)


def _text(v) -> Optional[str]:
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v if v and len(v) <= _MAX_TEXT else None


def kakao_identity(cfg: Config, code: str, verifier: str, has_account: Callable[[str], bool]) -> Identity:
    tok = _exchange(cfg, code, verifier)
    access = tok.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthFailed("token")
    auth = {"Authorization": f"Bearer {access}"}
    r = _call(cfg, "token_info", "GET", KAKAO_TOKEN_INFO, headers=auth)
    if r.status_code != 200:
        log.warning("oauth.kakao_token_info status=%d", r.status_code)
        raise AuthFailed("token_info")
    info = _json(cfg, "token_info", r)
    if str(info.get("app_id")) != cfg.app_id:
        log.error("oauth.kakao_app_id_mismatch")  # 우리 클라이언트로 교환한 토큰이 다른 앱 — 앱 ID 설정 확인 필요
        raise Misconfigured("kakao")
    sub = info.get("id")
    if not isinstance(sub, int) or isinstance(sub, bool) or sub <= 0:
        raise AuthFailed("token_info")
    subject = str(sub)
    email, name = (None, "keep"), (None, "keep")
    try:
        r = _call(cfg, "me", "GET", KAKAO_ME, headers=auth,
                  params={"property_keys": json.dumps(["kakao_account.email", "kakao_account.profile"])})
        if r.status_code != 200:
            log.warning("oauth.kakao_me status=%d", r.status_code)
            raise ProviderUnavailable("me")
        me = _json(cfg, "me", r)
        if str(me.get("id")) != subject:
            raise AuthFailed("me")
        acct = me.get("kakao_account") if isinstance(me.get("kakao_account"), dict) else {}
        email = _kakao_email(acct)
        name = _kakao_name(acct)
    except ProviderUnavailable:
        if not has_account(subject):  # 신규 계정은 확인되지 않은 프로필로 만들지 않는다 — 새 로그인부터
            raise
        log.warning("oauth.kakao_me_unavailable existing_account=1 profile=kept")
    return Identity("kakao", subject, email, name)


def _kakao_email(acct: dict) -> tuple[Optional[str], str]:
    if acct.get("email_needs_agreement") is True or acct.get("is_email_valid") is False or acct.get("is_email_verified") is False:
        return None, "clear"
    if acct.get("is_email_valid") is True and acct.get("is_email_verified") is True:
        v = _text(acct.get("email"))
        if v:
            return v, "set"
    return None, "keep"


def _kakao_name(acct: dict) -> tuple[Optional[str], str]:
    if acct.get("profile_nickname_needs_agreement") is True:
        return None, "clear"
    prof = acct.get("profile") if isinstance(acct.get("profile"), dict) else {}
    v = _text(prof.get("nickname"))
    return (v, "set") if v else (None, "keep")


# ── Google ID 토큰 — google-auth 의 verify_oauth2_token 으로 검증(Google 가이드 권장 방식) ──
# 라이브러리(google-auth 2.61.0 — constraints.txt 고정)가 하는 것: RS256 서명(cryptography) · aud(audience 를 반드시 넘긴다 — None 이면
# 검사하지 않음) · iat·exp(시계 차이 60초 허용, 둘 다 없으면 거절) · iss(accounts.google.com 두 형태).
# 우리가 더 하는 것: 헤더 alg=RS256·kid 필수(ES256 등 다른 알고리즘 차단) · iss·aud 를 한 번 더(aud 는 문자열 하나만) · nonce · sub.
# 인증서 GET 은 라이브러리가 _CertsRequest(아래)를 거쳐 하므로 기존 httpx 경로(제한 시간·시험 대역·실제 호출 차단)를 그대로 쓴다.
# 인증서는 Cache-Control 동안 캐시하고, 모르는 kid 면 다시 받는다 — 단 마지막으로 받은 지 _CERTS_REFRESH_MIN_S 안이면 다시 받지 않는다.
# 인증서 조회 실패(네트워크·5xx·4xx·JSON·인증서 형식)는 ProviderUnavailable 이고, 사용자 토큰 거절(AuthFailed)로 바꾸지 않는다.
# 알려진 한계(30초 재조회 제한): 이 서버 프로세스가 인증서를 받은 지 30초 안에, 그때 받은 목록에 없던 새 키로 서명된 정상 토큰이 오면
#   다시 받지 않고 401 provider_auth_failed(재시도 불가)로 거절하고 그 시도를 닫는다(Google 이 새 키를 쓰기 시작하는 시점이 그 목록보다
#   앞선 경우). 범위는 프로세스마다 마지막 조회 뒤 30초다. 복구 = 사용자가 새 로그인(start 부터)을 하면 그 요청에서 30초가 지났으면
#   목록을 다시 받아 통과한다 — 서버 조치는 필요 없다. Google 이 키를 언제 목록에 올리는지는 확인하지 않았다(실제 제공자 시험 범위).
# 검증 근거: 제공자 대역(cryptography·OpenSSL 서명, 자체 서명 PEM 인증서)과 설치된 라이브러리 시험까지다. 실제 Google ID 토큰·실제 v1 인증서
#   응답·브라우저 연동은 미검증이다.

GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v1/certs"  # verify_oauth2_token 이 요청하는 주소(라이브러리 상수 — 시험이 대조)
_CERTS_REFRESH_MIN_S = 30
_CERTS: dict = {}  # {"body": bytes, "kids": frozenset, "until": epoch, "fetched": epoch}


def _max_age(r: httpx.Response, default: int) -> int:
    m = re.search(r"max-age=(\d+)", r.headers.get("cache-control", ""))
    return min(int(m.group(1)), 86400) if m else default


def _fetch_google_certs(cfg: Config) -> None:
    r = _call(cfg, "certs", "GET", GOOGLE_CERTS)
    if r.status_code != 200:
        log.warning("oauth.google_certs_status status=%d", r.status_code)
        raise ProviderUnavailable("certs")
    raw = _json(cfg, "certs", r)
    good = {}
    for kid, pem in raw.items():
        try:
            pub = x509.load_pem_x509_certificate(pem.encode("ascii")).public_key()
        except (AttributeError, TypeError, ValueError):
            continue
        if isinstance(pub, rsa.RSAPublicKey) and pub.key_size >= 2048:
            good[kid] = pem
    if not good:
        log.warning("oauth.google_certs_unusable entries=%d", len(raw))
        raise ProviderUnavailable("certs")
    now = time.time()
    _CERTS.update(body=json.dumps(good).encode(), kids=frozenset(good), until=now + _max_age(r, 3600), fetched=now)


def _google_certs_body(cfg: Config, kid: str) -> bytes:
    now = time.time()
    if not _CERTS or _CERTS["until"] <= now:
        _fetch_google_certs(cfg)
    elif kid not in _CERTS["kids"] and now - _CERTS["fetched"] >= _CERTS_REFRESH_MIN_S:  # 인증서 교체 — 한 번만 다시 받는다
        log.info("oauth.google_certs_refresh reason=unknown_kid")
        _fetch_google_certs(cfg)
    return _CERTS["body"]


class _CertsResponse(google_transport.Response):
    def __init__(self, body: bytes):
        self._body = body

    @property
    def status(self):
        return 200

    @property
    def headers(self):
        return {}

    @property
    def data(self):
        return self._body


class _CertsRequest(google_transport.Request):
    """verify_oauth2_token 의 인증서 GET 하나만 받는다. 다른 요청은 코드 오류로 본다."""

    def __init__(self, cfg: Config, kid: str):
        self.cfg, self.kid = cfg, kid

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        if method != "GET" or url != GOOGLE_CERTS:
            log.error("oauth.google_unexpected_request method=%s", method)  # 라이브러리 버전이 바뀌어 주소가 달라진 경우 등
            raise Misconfigured("google")
        return _CertsResponse(_google_certs_body(self.cfg, self.kid))


def verify_google_id_token(cfg: Config, id_token: str, nonce: str) -> dict:
    """실패 = AuthFailed(토큰 거절) · ProviderUnavailable(인증서 조회 실패) · Misconfigured(라이브러리 요청이 예상과 다름)."""
    try:
        header = google_jwt.decode_header(id_token)
    except (ValueError, TypeError, google_exceptions.GoogleAuthError):
        raise AuthFailed("id_token_format")
    if not isinstance(header, dict) or header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
        raise AuthFailed("id_token_header")
    try:
        claims = google_id_token.verify_oauth2_token(id_token, _CertsRequest(cfg, header["kid"]),
                                                     audience=cfg.client_id, clock_skew_in_seconds=60)
    except (ProviderUnavailable, Misconfigured):
        raise
    except google_exceptions.TransportError:
        raise ProviderUnavailable("certs")
    except (ValueError, TypeError, KeyError, google_exceptions.GoogleAuthError) as e:
        log.info("oauth.google_id_token_rejected err=%s", type(e).__name__)  # 메시지에는 토큰 값이 들어갈 수 있어 남기지 않는다
        raise AuthFailed("id_token")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise AuthFailed("id_token_iss")
    if claims.get("aud") != cfg.client_id:
        raise AuthFailed("id_token_aud")
    if not nonce or not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(claims["nonce"], nonce):
        raise AuthFailed("id_token_nonce")
    if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
        raise AuthFailed("id_token_sub")
    return dict(claims)


def google_identity(cfg: Config, code: str, verifier: str, nonce: str) -> Identity:
    tok = _exchange(cfg, code, verifier)
    id_token = tok.get("id_token")
    if not isinstance(id_token, str):
        raise AuthFailed("token")
    c = verify_google_id_token(cfg, id_token, nonce)
    if c.get("email_verified") is False:
        email = (None, "clear")
    elif c.get("email_verified") is True and _text(c.get("email")):
        email = (_text(c.get("email")), "set")
    else:
        email = (None, "keep")
    name = (_text(c.get("name")), "set") if _text(c.get("name")) else (None, "keep")
    return Identity("google", c["sub"], email, name)
