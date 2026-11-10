"""로그인 세션(S안) — Redis 세션 레코드.

정본(인벤토리 v3.4.2 AUTH-02·03)은 「refresh 서버 상태 없음 · 로그아웃 = access 무효화 캐시 + 쿠키 삭제」였다.
2차 시연용 인증 구현에서 이 세션 레코드 방식으로 바꾼다(2026-11-10 사용자 구현 방향 결정 — PM 회신만으로 확정된 것이 아니다).
PM 정책(비활성 7일 · 최초 로그인 후 최대 30일 · 로그아웃 서버 무효화)을 서버가 지키려면 세션마다 서버 상태가 필요하기 때문이다.

세션 하나 = Redis 해시 `pixlate:auth:sess:<sid>`
    seller_id    판매자
    created_at   최초 로그인 시각(초)
    abs_exp      created_at + 30일 — 어떤 회전·활동 갱신으로도 늘지 않는다
    last_active  최근 활동 시각 = 로그인 또는 마지막 refresh 성공 시각
    idle_exp     last_active + 7일
    gen          현재 refresh 세대(로그인 = 1, 회전마다 +1)
    prev_until   직전 세대(gen - 1)를 「경합 거절」로만 다루는 마감 시각(회전 + 30초)
키 TTL = min(abs_exp, idle_exp). 폐기 = 키 삭제 — 같은 sid 로 다시 만드는 경로가 없으므로 되살아나지 않는다.

활동 기준(시연용 허용, 2026-11-10): **유효한 refresh 성공만** 활동 시각을 갱신한다. 다른 API 요청은 활동이 아니다.
화면이 API 를 계속 불러 토큰 갱신이 실제로 성공하면 사람이 없어도 30일까지 이어질 수 있다 —
실제 사용자 조작을 재는 비활성 정책과 다르다. 운영 전체의 영구 정책으로 넓히지 않는다.

refresh 토큰 = `<sid>.<세대>.<서명>`(쿠키 값). 서명 = HMAC-SHA256(refresh 서명 키, "pxl-refresh-v1|<sid>|<세대>").
- 용도(`pxl-refresh-v1`)·세션(sid)·세대가 서명에 묶여 있다. 세대나 sid 를 바꾸면 서명이 맞지 않는다.
- access(JWT, JWT_SECRET 으로 헤더·본문 서명)와 형식·키·용도가 달라 서로 대신 쓸 수 없다.
- 서버는 토큰을 저장하지 않는다 — 세션의 현재 세대만 둔다. 지난 토큰 목록을 쌓지 않으므로 정리할 것이 없고, 세션 키와 함께 사라진다.
판정 순서: 형식 → 서명(Python, Redis 를 보지 않음) → 세대 비교·회전·폐기(Lua 하나, 원자적).
    서명·형식이 틀림            → 무효(invalid) — Redis 를 건드리지 않는다(변조·임의 값·다른 세션으로 옮긴 값으로 세션을 폐기하지 않는다)
    세대 = 현재                 → 회전 성공(활동 갱신)
    세대 = 직전, 유예 안        → 경합 거절(rotated) — 세션·기한 그대로
    세대 = 직전, 유예 밖        → 재사용(reused) — 그 세션만 폐기
    세대 < 직전(더 오래된 발급) → 재사용(reused_old) — 그 세션만 폐기
    세대 > 현재                 → 무효(invalid) — 발급한 적 없는 세대(저장소 복원 등). 아무것도 바꾸지 않는다
    기한이 지난 세션            → 만료(expired) — 키 삭제, 되살리지 않는다

서명 키: 설정 PIXLATE_AUTH_REFRESH_SECRET. 없으면 JWT_SECRET 에서 용도를 붙여 파생한다(같은 키를 두 용도로 그대로 쓰지 않는다).
키를 바꾸면(교체·유출 대응) **모든 refresh 가 무효**가 되어 각 세션은 다음 refresh 때 refresh_invalid → 다시 로그인한다.
세션 레코드는 남지만 아무도 갱신할 수 없어 비활성 7일 뒤 사라진다. 발급된 access 는 JWT_SECRET 이 그대로면 남은 수명 동안 쓰인다.
JWT_SECRET 을 바꾸면 파생 키도 바뀐다(설정 키를 따로 두지 않았다면 refresh 도 함께 무효).
이전 키를 함께 받아 주는 교체 기간은 두지 않았다(필요하면 별도 결정).

Redis 를 쓸 수 없으면 Unavailable — 호출 쪽은 503 으로 바꾼다(인증 만료와 구분).
Redis 데이터가 유실되면(비우기·교체) 모든 세션이 사라져 전원 다시 로그인해야 한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import redis

IDLE_TTL_S = 7 * 24 * 60 * 60
ABSOLUTE_TTL_S = 30 * 24 * 60 * 60
REFRESH_GRACE_S = 30
_KEY = "pixlate:auth:sess:"
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_GEN_RE = re.compile(r"^[1-9][0-9]{0,8}$")
_SIG_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PURPOSE = "pxl-refresh-v1"

_client: redis.Redis | None = None


class Unavailable(Exception):
    """세션 저장소(Redis)를 쓸 수 없다."""


@dataclass(frozen=True)
class Session:
    sid: str
    seller_id: int
    created_at: int
    abs_exp: int
    idle_exp: int


@dataclass(frozen=True)
class Credential:
    """로그아웃용 자격 분류.
    absent  = 요청에 없음
    invalid = 형식·서명이 틀림(변조·임의 값·키 교체 전 값 등) — 어떤 세션도 가리킨다고 보지 않는다
    ended   = 서버가 발급한 값이지만 그 세션은 이미 끝남(폐기·만료) 또는 세션이 없는 이전 형식
    live    = 살아 있는 세션을 가리킴"""
    kind: str
    session: Optional[Session] = None


@dataclass(frozen=True)
class Rotation:
    status: str                       # ok | rotated | reused | invalid | expired
    session: Optional[Session] = None
    refresh_token: Optional[str] = None


def now() -> int:
    """시험이 바꿔 끼우는 시계(초)."""
    return int(time.time())


def _url() -> str:
    return os.getenv("PIXLATE_AUTH_REDIS_URL") or os.getenv("CELERY_RESULT_BACKEND", "redis://pixlate-redis:6379/1")


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(_url(), socket_connect_timeout=2, socket_timeout=2)
    return _client


def set_client(c) -> None:
    """시험용: 격리 Redis(또는 같은 연산을 가진 대역)로 바꿔 끼운다. None 이면 다음 호출 때 설정으로 다시 만든다."""
    global _client
    _client = c


def key(sid: str) -> str:
    return _KEY + sid


def _refresh_key() -> bytes:
    explicit = os.getenv("PIXLATE_AUTH_REFRESH_SECRET")
    if explicit:
        return explicit.encode()
    from app import security

    return hmac.new(security.JWT_SECRET.encode(), b"pixlate-refresh-key-v1", hashlib.sha256).digest()


def _sign(sid: str, gen: int) -> str:
    mac = hmac.new(_refresh_key(), f"{_PURPOSE}|{sid}|{gen}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()  # 43자


def _token(sid: str, gen: int) -> str:
    return f"{sid}.{gen}.{_sign(sid, gen)}"


def parse_refresh(token: Optional[str]) -> Optional[tuple[str, int]]:
    """쿠키 값 → (sid, 세대). 형식이 다르거나 서명이 맞지 않으면 None(Redis 를 보지 않는다)."""
    if not token or token.count(".") != 2:
        return None
    sid, gen, sig = token.split(".")
    if not _SID_RE.match(sid) or not _GEN_RE.match(gen) or not _SIG_RE.match(sig):
        return None
    if not hmac.compare_digest(_sign(sid, int(gen)), sig):
        return None
    return sid, int(gen)


_CREATE_REPLACING = """
-- KEYS[1] = 새 세션, KEYS[2] = 이 브라우저가 들고 온 이전 세션(없으면 빈 이름의 키)
-- ARGV: seller, now, abs, idle, 이전 쿠키의 세대('' = 없음 · 서명은 Python 에서 검증함)
local replaced = ''
if ARGV[5] ~= '' then
  local h = redis.call('HMGET', KEYS[2], 'seller_id', 'gen')
  local g = tonumber(ARGV[5])
  if h[1] and (g == tonumber(h[2]) or g == tonumber(h[2]) - 1) then
    redis.call('DEL', KEYS[2])
    replaced = h[1]
  end
end
redis.call('HSET', KEYS[1], 'seller_id', ARGV[1], 'created_at', ARGV[2], 'abs_exp', ARGV[3],
           'last_active', ARGV[2], 'idle_exp', ARGV[4], 'gen', 1)
redis.call('EXPIREAT', KEYS[1], math.min(tonumber(ARGV[3]), tonumber(ARGV[4])))
return replaced
"""

_ROTATE = """
-- ARGV: 제출한 세대(서명 검증됨), now, idle_ttl, grace
local h = redis.call('HMGET', KEYS[1], 'seller_id', 'created_at', 'abs_exp', 'idle_exp', 'gen', 'prev_until')
if not h[1] then return {'invalid'} end
local now = tonumber(ARGV[2])
local abs = tonumber(h[3])
local idle = tonumber(h[4])
if now >= abs or now >= idle then
  redis.call('DEL', KEYS[1])
  return {'expired'}
end
local g = tonumber(ARGV[1])
local cur = tonumber(h[5])
if g == cur then
  local new_idle = now + tonumber(ARGV[3])
  redis.call('HSET', KEYS[1], 'gen', cur + 1, 'prev_until', now + tonumber(ARGV[4]), 'last_active', now, 'idle_exp', new_idle)
  redis.call('EXPIREAT', KEYS[1], math.min(new_idle, abs))
  return {'ok', h[1], h[2], h[3], tostring(new_idle), tostring(cur + 1)}
end
if g == cur - 1 then
  if h[6] and now < tonumber(h[6]) then return {'rotated'} end
  redis.call('DEL', KEYS[1])
  return {'reused'}
end
if g < cur - 1 then
  redis.call('DEL', KEYS[1])
  return {'reused_old'}
end
return {'invalid'}
"""


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def create(seller_id: int, replacing: Optional[str] = None) -> tuple[Session, str, Optional[int]]:
    """로그인 성공 — 새 세션(세대 1)과 refresh 토큰. 최초 로그인 시각 = 초기 활동 시각 = 지금.
    replacing = 이 브라우저가 들고 온 refresh 쿠키 값. 서명이 맞고 그 세션의 현재·직전 세대면 **같은 스크립트 안에서** 폐기한다
    (새 세션 생성과 이전 세션 폐기가 함께 일어나거나 함께 일어나지 않는다). 돌려주는 셋째 값 = 폐기한 이전 세션의 판매자(없으면 None).
    한계: 같은 이전 쿠키로 로그인 두 건이 겹치면 이전 세션은 한 번 폐기되지만 새 세션은 둘 다 남는다(브라우저에 남는 쿠키는 하나).
    이 함수는 「한 브라우저 한 계정」을 보장하지 않는다 — 소셜 로그인(C)은 브라우저 결속 기준 원자 교체가 활성화 조건이다."""
    t = now()
    sid = secrets.token_urlsafe(16)  # 22자
    abs_exp, idle_exp = t + ABSOLUTE_TTL_S, t + IDLE_TTL_S
    old = parse_refresh(replacing)
    old_key, old_gen = (key(old[0]), old[1]) if old else (_KEY, "")
    try:
        replaced = client().eval(_CREATE_REPLACING, 2, key(sid), old_key, seller_id, t, abs_exp, idle_exp, old_gen)
    except redis.RedisError as e:
        raise Unavailable(str(e)) from e
    replaced = _s(replaced) if replaced not in (None, b"", "") else ""
    return Session(sid, seller_id, t, abs_exp, idle_exp), _token(sid, 1), (int(replaced) if replaced else None)


def get_active(sid: str) -> Optional[Session]:
    """access 검증용. 없거나(폐기·만료로 삭제) 기한이 지났으면 None. 활동 시각은 바꾸지 않는다."""
    if not _SID_RE.match(sid or ""):
        return None
    try:
        seller, created, abs_exp, idle_exp = client().hmget(key(sid), "seller_id", "created_at", "abs_exp", "idle_exp")
    except redis.RedisError as e:
        raise Unavailable(str(e)) from e
    if seller is None:
        return None
    s = Session(sid, int(_s(seller)), int(_s(created)), int(_s(abs_exp)), int(_s(idle_exp)))
    t = now()
    if t >= s.abs_exp or t >= s.idle_exp:
        return None
    return s


def rotate(refresh_token: Optional[str]) -> Rotation:
    """refresh — 위 판정표대로. 형식·서명이 틀리면 Redis 를 보지 않고 invalid.
    status: ok | rotated | reused(직전 세대·유예 밖) | reused_old(더 오래된 세대) | invalid | expired"""
    parsed = parse_refresh(refresh_token)
    if parsed is None:
        return Rotation("invalid")
    sid, gen = parsed
    try:
        res = client().eval(_ROTATE, 1, key(sid), gen, now(), IDLE_TTL_S, REFRESH_GRACE_S)
    except redis.RedisError as e:
        raise Unavailable(str(e)) from e
    status = _s(res[0])
    if status != "ok":
        return Rotation(status)
    seller, created, abs_exp, idle_exp, new_gen = (int(_s(v)) for v in res[1:6])
    return Rotation("ok", Session(sid, seller, created, abs_exp, idle_exp), _token(sid, new_gen))


def match_refresh(refresh_token: Optional[str]) -> Optional[Session]:
    """로그아웃용 확인 — 서명이 맞고 그 세션의 현재 또는 직전 세대인가.
    기한이 지났거나 맞지 않으면 None(그 세션을 건드리지 않는다). 활동 시각·회전은 바꾸지 않는다.
    더 오래된 세대는 여기서 인정하지 않는다(refresh 로 오면 재사용으로 폐기된다)."""
    parsed = parse_refresh(refresh_token)
    if parsed is None:
        return None
    sid, gen = parsed
    try:
        (cur,) = client().hmget(key(sid), "gen")
    except redis.RedisError as e:
        raise Unavailable(str(e)) from e
    if cur is not None and gen in (int(_s(cur)), int(_s(cur)) - 1):
        return get_active(sid)
    return None


def identify_refresh(refresh_token: Optional[str]) -> Credential:
    """로그아웃용 쿠키 분류. 서명이 맞으면 **세대와 관계없이** 그 세션을 가리킨다고 본다
    (현재·직전·더 오래된 세대 모두 — 서명에 sid·세대가 묶여 있어 다른 세션을 가리킬 수 없다). 활동·회전은 바꾸지 않는다.
    서명이 틀리면 invalid(Redis 를 보지 않는다). 저장소 장애는 Unavailable."""
    if not refresh_token:
        return Credential("absent")
    parsed = parse_refresh(refresh_token)
    if parsed is None:
        return Credential("invalid")
    s = get_active(parsed[0])
    return Credential("live", s) if s is not None else Credential("ended")


def revoke(*sids: str) -> int:
    """세션 폐기(키 삭제). 이미 없는 sid 는 무시한다. 돌려주는 값 = 실제로 지운 수."""
    keys = [key(s) for s in dict.fromkeys(sids) if s]
    if not keys:
        return 0
    try:
        return int(client().delete(*keys))
    except redis.RedisError as e:
        raise Unavailable(str(e)) from e
