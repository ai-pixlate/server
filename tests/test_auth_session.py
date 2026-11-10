"""로그인 세션(S안) — 실제 PostgreSQL + 실제 Redis. CI 필수 시험(건너뜀 = 실패).

app.auth_sessions · app.security · app.routers.auth 의 세션 동작을 실제 API 로 확인한다.
- refresh 회전·동시 요청·응답 유실·유예·재사용
- 비활성 7일·절대 30일과 경계 시각, 활동 = refresh 성공만
- 로그아웃 판정(검증된 자격만)·로그아웃과 refresh 동시 실행
- 한 브라우저 한 계정(로그인 교체)·계정 전환 때 서버가 보장하는 것
- 세션 저장소 장애(503)·이전 형식 토큰 거절
시계는 auth_sessions.now 를 바꿔 끼운다(Redis 의 TTL 은 실제 시계라, 판정은 레코드의 시각으로 한다).
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.exec_db import bound_app_db, fresh_database, requires_db

REDIS_URL = os.getenv("PIXLATE_TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(not REDIS_URL, reason="PIXLATE_TEST_REDIS_URL 없음")
pytestmark = [requires_db, requires_redis]

DAY = 24 * 60 * 60
CSRF = {"X-Requested-With": "XMLHttpRequest", "Origin": "https://fe.pixlate.test"}  # 정상 FE 요청(프록시 경유 — 같은 오리진)


@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def rds(dburl):
    import redis

    from app import auth_sessions

    r = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    auth_sessions.set_client(r)  # 대역이 아니라 실제 Redis(Lua 원자성까지 본다)
    yield r


class Clock:
    def __init__(self, t: int):
        self.t = t

    def __call__(self) -> int:
        return self.t


@pytest.fixture
def clock(monkeypatch, rds):
    import time

    from app import auth_sessions

    c = Clock(int(time.time()))
    monkeypatch.setattr(auth_sessions, "now", c)
    return c


@pytest.fixture
def client(dburl):
    from app.main import app

    return TestClient(app)


def _email() -> str:
    return f"sess-{uuid.uuid4().hex[:10]}@example.com"


def _set_cookies(r) -> list[str]:
    return r.headers.get_list("set-cookie")


def _refresh_cookie(r) -> str | None:
    for h in _set_cookies(r):
        name, _, rest = h.partition("=")
        if name.strip() == "pxl_refresh":
            return rest.split(";", 1)[0]
    return None


def _login(client, email: str, cookie: str | None = None) -> dict:
    headers = {"Cookie": f"pxl_refresh={cookie}"} if cookie else {}
    r = client.post("/v1/auth/login", json={"email": email, "password": "x"}, headers=headers)
    assert r.status_code == 200, r.text
    return {"access": r.json()["accessToken"], "cookie": _refresh_cookie(r), "seller": r.json()["seller"]["id"], "resp": r}


def _refresh(client, cookie: str | None):
    headers = dict(CSRF)
    if cookie is not None:
        headers["Cookie"] = f"pxl_refresh={cookie}"
    return client.post("/v1/auth/refresh", headers=headers)


def _logout(client, access: str | None = None, cookie: str | None = None, csrf: bool = True):
    headers = dict(CSRF) if csrf else {}
    if access is not None:
        headers["Authorization"] = f"Bearer {access}"
    if cookie is not None:
        headers["Cookie"] = f"pxl_refresh={cookie}"
    return client.post("/v1/auth/logout", headers=headers)


def _me(client, access: str):
    return client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"})


def _sid(cookie: str) -> str:
    return cookie.split(".")[0]


def _gen(cookie: str) -> int:
    return int(cookie.split(".")[1])


def _forge(sid: str, gen: int = 1, sig: str = "Z" * 43) -> str:
    """형식은 맞고 서명은 틀린 refresh 값."""
    return f"{sid}.{gen}.{sig}"


def _rec(rds, cookie: str) -> dict:
    from app import auth_sessions

    return {k.decode(): v.decode() for k, v in rds.hgetall(auth_sessions.key(_sid(cookie))).items()}


def _reason(r) -> str | None:
    return (r.json()["error"].get("details") or {}).get("reason")


def _claims(access: str) -> dict:
    from app.security import verify_signature

    return verify_signature(access)


# ── 로그인 · 쿠키 ────────────────────────────────────────────────────────────────────────────────

def test_login_creates_session_and_sets_refresh_cookie(client, clock, rds):
    s = _login(client, _email())
    cookie_header = next(h for h in _set_cookies(s["resp"]) if h.startswith("pxl_refresh="))
    attrs = {a.strip().split("=")[0].lower(): (a.split("=", 1)[1] if "=" in a else True) for a in cookie_header.split(";")[1:]}
    assert attrs["httponly"] is True and attrs["secure"] is True and attrs["samesite"].lower() == "strict"
    assert attrs["path"] == "/auth" and int(attrs["max-age"]) == 30 * DAY

    rec = _rec(rds, s["cookie"])
    t = clock.t
    assert (int(rec["created_at"]), int(rec["last_active"]), int(rec["abs_exp"]), int(rec["idle_exp"])) == (t, t, t + 30 * DAY, t + 7 * DAY)
    assert int(rec["seller_id"]) == s["seller"] and rec["gen"] == "1" and "prev_until" not in rec
    assert s["cookie"].split(".")[2] not in str(rec)  # 토큰(서명)은 저장하지 않는다 — 세대만
    claims = _claims(s["access"])
    assert claims["sid"] == _sid(s["cookie"]) and claims["exp"] == t + 900
    assert _me(client, s["access"]).json()["id"] == s["seller"]


# ── refresh 회전 · 활동 ───────────────────────────────────────────────────────────────────────────

def test_refresh_success_rotates_and_is_the_only_activity(client, clock, rds):
    s = _login(client, _email())
    t0 = clock.t
    for _ in range(5):  # 일반 API 요청은 활동이 아니다
        clock.t += 60
        assert _me(client, s["access"]).status_code == 200
    assert int(_rec(rds, s["cookie"])["last_active"]) == t0

    clock.t = t0 + 3 * DAY
    r = _refresh(client, s["cookie"])
    assert r.status_code == 200, r.text
    new = _refresh_cookie(r)
    assert new and new != s["cookie"] and _sid(new) == _sid(s["cookie"])  # 같은 세션, 새 비밀
    rec = _rec(rds, new)
    assert (int(rec["created_at"]), int(rec["abs_exp"])) == (t0, t0 + 30 * DAY)  # 최초 로그인·절대 만료는 그대로
    assert (int(rec["last_active"]), int(rec["idle_exp"])) == (clock.t, clock.t + 7 * DAY)
    max_age = [h for h in _set_cookies(r) if h.startswith("pxl_refresh=")][0].split("Max-Age=")[1].split(";")[0]
    assert int(max_age) == t0 + 30 * DAY - clock.t  # 쿠키도 절대 만료를 넘지 않는다
    assert _claims(r.json()["accessToken"])["exp"] == clock.t + 900
    assert _refresh(client, new).status_code == 200


@pytest.mark.parametrize("cookie,why", [
    (None, "쿠키 없음"),
    ("garbage", "형식 오류"),
    ("A" * 22 + ".1." + "B" * 43, "모르는 세션·서명 틀림"),
    ("A" * 22 + "." + "B" * 43, "이전 형식(sid.비밀)"),
])
def test_refresh_invalid_without_set_cookie(client, clock, cookie, why):
    r = _refresh(client, cookie)
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED" and _reason(r) == "refresh_invalid", why
    assert _set_cookies(r) == []


def test_refresh_with_wrong_secret_does_not_touch_the_session(client, clock, rds):
    """sid 만 맞고 서명이 틀린 값 — 검증되지 않은 값으로 세션을 폐기·변경하지 않는다."""
    s = _login(client, _email())
    before = _rec(rds, s["cookie"])
    r = _refresh(client, _forge(_sid(s["cookie"])))
    assert r.status_code == 401 and _reason(r) == "refresh_invalid" and _set_cookies(r) == []
    assert _rec(rds, s["cookie"]) == before and _me(client, s["access"]).status_code == 200


def test_refresh_requires_csrf_header(client, clock, rds):
    s = _login(client, _email())
    before = _rec(rds, s["cookie"])
    r = client.post("/v1/auth/refresh", headers={"Cookie": f"pxl_refresh={s['cookie']}"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR" and _set_cookies(r) == []
    assert _rec(rds, s["cookie"]) == before


def test_concurrent_refresh_exactly_one_rotates(client, clock, rds):
    s = _login(client, _email())
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(_refresh(client, s["cookie"]))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ok = [r for r in results if r.status_code == 200]
    assert len(ok) == 1
    assert all(r.status_code == 401 and _reason(r) == "refresh_rotated" and _set_cookies(r) == [] for r in results if r is not ok[0])
    winner = _refresh_cookie(ok[0])
    assert _rec(rds, winner)  # 세션은 살아 있다(경합 거절은 폐기가 아니다)
    assert _refresh(client, winner).status_code == 200


def test_lost_response_within_grace_is_rotated_then_reused_after_grace(client, clock, rds):
    s = _login(client, _email())
    other_browser = _login(client, _email())  # 다른 세션(폐기 범위 확인용)
    lost = _refresh(client, s["cookie"])  # 성공했지만 브라우저가 Set-Cookie 를 받지 못한 경우
    assert lost.status_code == 200
    before = _rec(rds, s["cookie"])

    clock.t += 29  # 유예(30초) 안 — 같은 이전 쿠키로 재시도
    r = _refresh(client, s["cookie"])
    assert r.status_code == 401 and _reason(r) == "refresh_rotated" and _set_cookies(r) == []
    assert _rec(rds, s["cookie"]) == before  # 세션·기한 그대로

    clock.t += 1  # 유예가 끝난 정각
    r = _refresh(client, s["cookie"])
    assert r.status_code == 401 and _reason(r) == "refresh_reused" and _set_cookies(r) == []
    assert _rec(rds, s["cookie"]) == {}  # 그 세션만 폐기
    assert _refresh(client, _refresh_cookie(lost)).status_code == 401  # 성공 쪽 후속 토큰도 함께 끝난다
    assert _me(client, s["access"]).status_code == 401
    assert _me(client, other_browser["access"]).status_code == 200


def test_reuse_revokes_only_that_session_of_the_same_seller(client, clock, rds):
    email = _email()
    a = _login(client, email)
    b = _login(client, email)  # 같은 계정의 다른 브라우저(쿠키 없이 로그인)
    assert a["seller"] == b["seller"] and _sid(a["cookie"]) != _sid(b["cookie"])
    assert _refresh(client, a["cookie"]).status_code == 200
    clock.t += 31
    assert _reason(_refresh(client, a["cookie"])) == "refresh_reused"
    assert _me(client, b["access"]).status_code == 200 and _refresh(client, b["cookie"]).status_code == 200


# ── 비활성 7일 · 절대 30일 ────────────────────────────────────────────────────────────────────────

def test_idle_expiry_boundary_and_no_revival(client, clock, rds):
    from app import auth_sessions

    s = _login(client, _email())
    t0 = clock.t
    clock.t = t0 + 7 * DAY - 1  # 경계 1초 전 — 살아 있고, refresh 성공이 기한을 다시 7일로
    r = _refresh(client, s["cookie"])
    assert r.status_code == 200
    cookie = _refresh_cookie(r)
    last = clock.t

    clock.t = last + 7 * DAY - 1
    assert auth_sessions.get_active(_sid(cookie)) is not None
    clock.t = last + 7 * DAY  # 정각 = 만료
    assert auth_sessions.get_active(_sid(cookie)) is None
    r = _refresh(client, cookie)
    assert r.status_code == 401 and _reason(r) == "refresh_invalid" and _set_cookies(r) == []
    assert _rec(rds, cookie) == {}  # 지워졌다 — 시계를 되돌려도 되살아나지 않는다
    clock.t = last
    assert _refresh(client, cookie).status_code == 401


def test_rejected_refresh_does_not_extend_idle(client, clock, rds):
    s = _login(client, _email())
    t0 = clock.t
    assert _refresh(client, s["cookie"]).status_code == 200  # cur = 새 값, prev = 처음 값
    first_idle = int(_rec(rds, s["cookie"])["idle_exp"])
    clock.t = t0 + 10
    assert _reason(_refresh(client, s["cookie"])) == "refresh_rotated"  # 거절
    assert int(_rec(rds, s["cookie"])["idle_exp"]) == first_idle


def test_absolute_expiry_is_never_extended(client, clock, rds):
    s = _login(client, _email())
    t0 = clock.t
    cookie, access = s["cookie"], s["access"]
    for day in (6, 12, 18, 24, 29):  # 계속 사용(6일마다 갱신)
        clock.t = t0 + day * DAY
        r = _refresh(client, cookie)
        assert r.status_code == 200
        cookie, access = _refresh_cookie(r), r.json()["accessToken"]
    assert int(_rec(rds, cookie)["abs_exp"]) == t0 + 30 * DAY

    clock.t = t0 + 30 * DAY - 60  # 절대 만료 1분 전 갱신 — access 도 절대 만료를 넘지 않는다
    r = _refresh(client, cookie)
    cookie, access = _refresh_cookie(r), r.json()["accessToken"]
    assert _claims(access)["exp"] == t0 + 30 * DAY
    clock.t = t0 + 30 * DAY - 1
    assert _me(client, access).status_code == 200
    clock.t = t0 + 30 * DAY  # 정각
    me = _me(client, access)
    assert me.status_code == 401 and me.json()["error"]["code"] == "TOKEN_EXPIRED"
    r = _refresh(client, cookie)
    assert r.status_code == 401 and _reason(r) == "refresh_invalid"
    assert _rec(rds, cookie) == {}


# ── 로그아웃 ─────────────────────────────────────────────────────────────────────────────────────

def _cleared(r) -> bool:
    return any(h.startswith("pxl_refresh=") and ("Max-Age=0" in h or "max-age=0" in h.lower()) for h in _set_cookies(r))


def test_logout_same_session(client, clock, rds):
    s = _login(client, _email())
    r = _logout(client, s["access"], s["cookie"])
    assert r.status_code == 204 and _cleared(r) and r.content == b""
    assert _rec(rds, s["cookie"]) == {}
    assert _me(client, s["access"]).status_code == 401 and _reason(_refresh(client, s["cookie"])) == "refresh_invalid"


def test_logout_mixed_accounts_ends_exactly_the_two_verified_sessions(client, clock, rds):
    """PM 11-10: Bearer 와 쿠키가 다른 계정이면 두 세션 모두 종료. 그 계정들의 다른 브라우저 세션은 그대로."""
    a_email, b_email = _email(), _email()
    a = _login(client, a_email)
    b = _login(client, b_email)
    a_elsewhere, b_elsewhere = _login(client, a_email), _login(client, b_email)
    r = _logout(client, a["access"], b["cookie"])
    assert r.status_code == 204 and _cleared(r)
    assert _rec(rds, a["cookie"]) == {} and _rec(rds, b["cookie"]) == {}
    assert _me(client, a_elsewhere["access"]).status_code == 200 and _me(client, b_elsewhere["access"]).status_code == 200


def test_logout_same_account_two_sessions(client, clock, rds):
    email = _email()
    a1, a2 = _login(client, email), _login(client, email)
    assert _logout(client, a1["access"], a2["cookie"]).status_code == 204
    assert _rec(rds, a1["cookie"]) == {} and _rec(rds, a2["cookie"]) == {}


def test_logout_with_expired_access_still_works(client, clock, rds):
    """access 가 만료돼도 서명이 맞으면 그 세션을 찾아 로그아웃한다(갱신 없이 한 번에)."""
    s = _login(client, _email())
    clock.t += 901
    assert _me(client, s["access"]).json()["error"]["code"] == "TOKEN_EXPIRED"
    r = _logout(client, s["access"])  # 쿠키 없이 Bearer 만
    assert r.status_code == 204 and _rec(rds, s["cookie"]) == {}


def test_logout_cookie_only(client, clock, rds):
    s = _login(client, _email())
    assert _logout(client, cookie=s["cookie"]).status_code == 204 and _rec(rds, s["cookie"]) == {}


def test_logout_forged_bearer_changes_nothing(client, clock, rds):
    s = _login(client, _email())
    forged = s["access"][:-2] + ("aa" if not s["access"].endswith("aa") else "bb")
    r = _logout(client, forged, s["cookie"])
    assert r.status_code == 401 and _set_cookies(r) == []
    assert _rec(rds, s["cookie"]) and _me(client, s["access"]).status_code == 200


def test_logout_verified_bearer_with_unverified_cookie(client, clock, rds):
    """쿠키가 다른 세션의 sid 를 담았지만 비밀이 틀리다 — Bearer 세션만 폐기, 그 다른 세션은 건드리지 않는다."""
    a, other = _login(client, _email()), _login(client, _email())
    r = _logout(client, a["access"], _forge(_sid(other["cookie"])))
    assert r.status_code == 204
    assert _rec(rds, a["cookie"]) == {} and _rec(rds, other["cookie"]) and _me(client, other["access"]).status_code == 200


@pytest.mark.parametrize("case", ["nothing", "legacy-token", "revoked-session"])
def test_logout_without_verified_credentials_is_401(client, clock, rds, case):
    from app.security import create_token

    s = _login(client, _email())
    if case == "nothing":
        r = _logout(client)
    elif case == "legacy-token":
        r = _logout(client, create_token(s["seller"], None))  # 세션 없는 이전 형식
    else:
        _logout(client, s["access"], s["cookie"])
        r = _logout(client, s["access"], s["cookie"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED" and _set_cookies(r) == []


def test_logout_requires_csrf_header(client, clock, rds):
    s = _login(client, _email())
    r = _logout(client, s["access"], s["cookie"], csrf=False)
    assert r.status_code == 422 and _rec(rds, s["cookie"])


def test_logout_racing_refresh_never_revives(client, clock, rds):
    """로그아웃과 refresh 를 동시에 — 어느 순서로 끝나도 세션은 사라지고, 회전으로 받은 새 토큰도 쓸 수 없다."""
    for _ in range(15):
        s = _login(client, _email())
        out, barrier = {}, threading.Barrier(2)

        def do_refresh():
            barrier.wait()
            out["refresh"] = _refresh(client, s["cookie"])

        def do_logout():
            barrier.wait()
            out["logout"] = _logout(client, s["access"], s["cookie"])

        ts = [threading.Thread(target=do_refresh), threading.Thread(target=do_logout)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert out["logout"].status_code == 204
        assert _rec(rds, s["cookie"]) == {}
        if out["refresh"].status_code == 200:  # 회전이 먼저 끝났다 — 늦게 도착한 새 쿠키·access 도 끝난 세션
            assert _refresh(client, _refresh_cookie(out["refresh"])).status_code == 401
            assert _me(client, out["refresh"].json()["accessToken"]).status_code == 401
        else:
            assert _reason(out["refresh"]) == "refresh_invalid"
        assert _me(client, s["access"]).status_code == 401


# ── 한 브라우저 한 계정 · 계정 전환 ───────────────────────────────────────────────────────────────

def test_login_in_same_browser_replaces_previous_session(client, clock, rds):
    """다른 탭에서 새 계정으로 로그인(브라우저 쿠키가 실려 옴) → 이전 계정 세션 폐기. 이전 탭의 access 로는 더 실행되지 않는다."""
    a_email = _email()
    a = _login(client, a_email)
    a_elsewhere = _login(client, a_email)  # 다른 기기의 A
    b = _login(client, _email(), cookie=a["cookie"])
    assert _rec(rds, a["cookie"]) == {}
    me = _me(client, a["access"])
    assert me.status_code == 401 and me.json()["error"]["code"] == "UNAUTHORIZED"  # 갱신으로 복구되는 TOKEN_EXPIRED 가 아니다
    assert _me(client, a_elsewhere["access"]).status_code == 200  # 다른 기기의 세션은 그대로

    # 이전 탭이 갱신하면 브라우저 쿠키(B)로 B 토큰을 받는다 — 응답에 계정이 없으므로 FE 는 /auth/me 로 계정을 확인한다
    r = _refresh(client, b["cookie"])
    assert r.status_code == 200 and set(r.json()) == {"accessToken"}
    assert _me(client, r.json()["accessToken"]).json()["id"] == b["seller"] != a["seller"]


def test_login_with_unverified_cookie_does_not_revoke(client, clock, rds):
    other = _login(client, _email())
    _login(client, _email(), cookie=_forge(_sid(other["cookie"])))
    assert _rec(rds, other["cookie"])


# ── 이전 형식 토큰 · 저장소 장애 ──────────────────────────────────────────────────────────────────

def test_token_without_session_is_rejected_everywhere(client, clock):
    from app.security import create_token

    s = _login(client, _email())
    old = {"Authorization": "Bearer " + create_token(s["seller"], None)}
    for r in (client.get("/v1/auth/me", headers=old), client.get("/v1/library", headers=old), client.get("/v1/brands", headers=old)):
        assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED" and _reason(r) == "invalid_token"


def test_session_store_outage_is_503_not_auth_failure(client, clock, rds):
    import redis

    from app import auth_sessions

    s = _login(client, _email())
    auth_sessions.set_client(redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2))
    try:
        for r in (_me(client, s["access"]), _refresh(client, s["cookie"]), _logout(client, s["access"], s["cookie"]),
                  client.post("/v1/auth/login", json={"email": _email(), "password": "x"})):
            e = r.json()["error"]
            assert r.status_code == 503 and e["code"] == "SERVICE_UNAVAILABLE" and e["retryable"] is True, r.text
            assert e["details"] is None  # 새 사유(auth_store_unavailable)는 FE 확인 전이라 붙이지 않는다
            assert _set_cookies(r) == []
    finally:
        auth_sessions.set_client(rds)
    assert _me(client, s["access"]).status_code == 200 and _rec(rds, s["cookie"])  # 장애가 세션을 바꾸지 않았다


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# 보완 시험(2026-11-10 코드 검토 뒤) — Origin·재사용 탐지 범위·동시 재로그인·실패 경계
# ═════════════════════════════════════════════════════════════════════════════════════════════════

def _with_origin(origin):
    h = {"X-Requested-With": "XMLHttpRequest"}
    if origin is not None:
        h["Origin"] = origin
    return h


BAD_ORIGINS = [
    (None, "Origin 누락"),
    ("null", "null(샌드박스·파일 등)"),
    ("https://evil.test", "허용 목록 밖"),
    ("https://fe.pixlate.test.evil.test", "접미어만 같은 다른 호스트"),
    ("http://fe.pixlate.test", "스킴이 다름"),
    ("https://fe.pixlate.test:8443", "포트가 다름"),
]


@pytest.mark.parametrize("origin,why", BAD_ORIGINS)
def test_refresh_rejects_bad_origin_without_rotating(client, clock, rds, origin, why):
    s = _login(client, _email())
    before = _rec(rds, s["cookie"])
    r = client.post("/v1/auth/refresh", headers={**_with_origin(origin), "Cookie": f"pxl_refresh={s['cookie']}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHORIZED" and _reason(r) == "origin_not_allowed", why
    assert _set_cookies(r) == [] and _rec(rds, s["cookie"]) == before  # 회전·활동 갱신 없음
    assert _refresh(client, s["cookie"]).status_code == 200  # 같은 쿠키를 그대로 쓸 수 있다


@pytest.mark.parametrize("origin,why", BAD_ORIGINS)
def test_logout_rejects_bad_origin_without_revoking(client, clock, rds, origin, why):
    s = _login(client, _email())
    r = client.post("/v1/auth/logout", headers={**_with_origin(origin), "Authorization": f"Bearer {s['access']}",
                                                "Cookie": f"pxl_refresh={s['cookie']}"})
    assert r.status_code == 401 and _reason(r) == "origin_not_allowed", why
    assert _set_cookies(r) == [] and _rec(rds, s["cookie"]) and _me(client, s["access"]).status_code == 200


@pytest.mark.parametrize("origin", ["https://fe.pixlate.test", "HTTPS://FE.PIXLATE.TEST", "https://fe.pixlate.test/"])
def test_allowed_origin_variants_pass(client, clock, origin):
    s = _login(client, _email())
    assert client.post("/v1/auth/refresh", headers={**_with_origin(origin), "Cookie": f"pxl_refresh={s['cookie']}"}).status_code == 200


@pytest.mark.parametrize("setting", ["", "*"])
def test_without_origin_allowlist_cookie_endpoints_fail_closed(client, clock, rds, monkeypatch, setting):
    """허용 목록이 없거나 `*` 면 정상 FE Origin 도 거절한다(운영 값을 추측하지 않고, 검사를 넓혀 통과시키지 않는다)."""
    s = _login(client, _email())
    monkeypatch.setenv("PIXLATE_AUTH_ORIGINS", setting)
    assert _reason(_refresh(client, s["cookie"])) == "origin_not_allowed"
    assert _reason(_logout(client, s["access"], s["cookie"])) == "origin_not_allowed"
    assert _rec(rds, s["cookie"])


def test_cors_does_not_grant_credentialed_cross_origin_access(client):
    """서버 CORS 기본값(FRONTEND_ORIGINS 미설정 = `*`)은 자격 증명을 허용하지 않는다 — 다른 오리진 스크립트가 쿠키를 실은 요청의 응답을 읽을 수 없다.
    FE 는 같은 오리진 프록시(`/auth/*`)로 부르므로 교차 출처 자격 증명이 필요 없다. 실제 브라우저·Next 프록시 동작은 ② 로컬 연동에서 확인한다."""
    pre = client.options("/v1/auth/refresh", headers={"Origin": "https://evil.test", "Access-Control-Request-Method": "POST",
                                                      "Access-Control-Request-Headers": "x-requested-with"})
    assert pre.headers.get("access-control-allow-credentials") != "true"


# ── refresh 재사용 탐지 범위 ────────────────────────────────────────────────────────────────────

def test_reuse_detection_scope_current_previous_older_random(client, clock, rds):
    """세대 + 서명(11-10 보완): 현재 → 회전 / 직전·유예 안 → rotated / 직전·유예 밖 → reused /
    **더 오래된 실제 발급 토큰(T1 → T2 → T3 뒤 T1) → reused — 그 세션만 폐기** / 형식만 맞는 임의 값 → invalid(아무것도 안 바꿈)."""
    s = _login(client, _email())
    other = _login(client, _email())  # 다른 세션 — 폐기 범위 확인용
    t1 = s["cookie"]
    t2 = _refresh_cookie(_refresh(client, t1))
    clock.t += 60
    t3 = _refresh_cookie(_refresh(client, t2))
    clock.t += 60
    assert (_gen(t1), _gen(t2), _gen(t3)) == (1, 2, 3)
    before = _rec(rds, t3)
    r = _refresh(client, _forge(_sid(t3), gen=3))  # 형식만 맞는 임의 값(현재 세대 번호를 붙여도)
    assert _reason(r) == "refresh_invalid" and _rec(rds, t3) == before

    r = _refresh(client, t1)  # 더 오래된 발급 토큰
    assert r.status_code == 401 and _reason(r) == "refresh_reused" and _set_cookies(r) == []
    assert _rec(rds, t3) == {}  # 그 세션 폐기
    assert _reason(_refresh(client, t3)) == "refresh_invalid" and _me(client, s["access"]).status_code == 401
    assert _rec(rds, other["cookie"]) and _me(client, other["access"]).status_code == 200  # 다른 세션은 그대로


def test_thief_rotating_twice_is_caught_when_victim_presents_old_token(client, clock, rds):
    """보완 전 한계(도둑이 두 번 회전하면 피해자의 T1 이 invalid 로 끝나고 도둑 세션이 살아 있음)가 닫혔다:
    피해자의 T1 = 더 오래된 발급 세대 → reused → 그 세션 폐기 → 도둑의 최신 토큰·access 도 끝난다."""
    s = _login(client, _email())
    r = _refresh(client, s["cookie"])
    thief, thief_access = _refresh_cookie(r), r.json()["accessToken"]
    clock.t += 60
    r = _refresh(client, thief)
    thief, thief_access = _refresh_cookie(r), r.json()["accessToken"]
    clock.t += 60
    assert _reason(_refresh(client, s["cookie"])) == "refresh_reused"
    assert _reason(_refresh(client, thief)) == "refresh_invalid"
    assert _me(client, thief_access).status_code == 401


def test_tampered_generation_or_moved_token_changes_nothing(client, clock, rds):
    """세대 값 변조·다른 세션으로 옮긴 토큰은 서명이 맞지 않아 invalid — 어느 세션도 폐기·변경하지 않는다."""
    a, b = _login(client, _email()), _login(client, _email())
    a2 = _refresh_cookie(_refresh(client, a["cookie"]))
    b_cur = _refresh_cookie(_refresh(client, _refresh_cookie(_refresh(client, b["cookie"]))))  # B 는 세대 3
    clock.t += 60
    before_a, before_b = _rec(rds, a2), _rec(rds, b_cur)
    sid, gen, sig = a2.split(".")
    cases = {
        "세대를 1로(오래된 세대로 위장 — 재사용 폐기 유도)": f"{sid}.1.{sig}",
        "세대를 크게": f"{sid}.99.{sig}",
        "다른 세션 sid 로 옮김": f"{_sid(b['cookie'])}.{gen}.{sig}",
        "A 의 세대 1 토큰을 B(세대 3)로 옮김 — sid 결속이 없으면 B 가 「오래된 세대 재사용」으로 폐기된다":
            f"{_sid(b['cookie'])}.1.{a['cookie'].split('.')[2]}",
        "B 의 서명을 A 에": f"{sid}.{gen}.{b['cookie'].split('.')[2]}",
    }
    for why, token in cases.items():
        r = _refresh(client, token)
        assert r.status_code == 401 and _reason(r) == "refresh_invalid" and _set_cookies(r) == [], why
        assert _logout(client, cookie=token).status_code == 401, why  # 로그아웃 자격으로도 인정하지 않는다
    assert _rec(rds, a2) == before_a and _rec(rds, b_cur) == before_b


def test_authentic_future_generation_is_invalid_not_revoked(client, clock, rds):
    """서명은 맞지만 발급한 적 없는 세대(저장소 복원 등으로 세대가 뒤로 간 경우) — invalid, 세션 그대로."""
    from app import auth_sessions

    s = _login(client, _email())
    before = _rec(rds, s["cookie"])
    r = _refresh(client, auth_sessions._token(_sid(s["cookie"]), 6))
    assert _reason(r) == "refresh_invalid" and _rec(rds, s["cookie"]) == before


def test_access_and_refresh_cannot_stand_in_for_each_other(client, clock, rds):
    s = _login(client, _email())
    before = _rec(rds, s["cookie"])
    r = _refresh(client, s["access"])  # access 를 refresh 쿠키로
    assert r.status_code == 401 and _reason(r) == "refresh_invalid" and _rec(rds, s["cookie"]) == before
    me = _me(client, s["cookie"])  # refresh 를 Bearer 로
    assert me.status_code == 401 and _reason(me) == "invalid_token"
    assert _logout(client, s["cookie"]).status_code == 401  # 로그아웃 Bearer 로도 — 서명 오류라 아무것도 안 바꾼다
    assert _rec(rds, s["cookie"]) == before


def test_refresh_key_rotation_invalidates_refresh_but_not_issued_access(client, clock, rds, monkeypatch):
    """서명 키를 바꾸면 모든 refresh 가 invalid(다음 refresh 때 다시 로그인). 세션 레코드는 남지만 갱신할 수 없고,
    이미 발급된 access 는 JWT_SECRET 이 그대로라 남은 수명 동안 쓰인다. 실제 키 값은 출력하지 않는다."""
    s = _login(client, _email())
    monkeypatch.setenv("PIXLATE_AUTH_REFRESH_SECRET", "test-only-rotated-key")
    r = _refresh(client, s["cookie"])
    assert _reason(r) == "refresh_invalid" and _rec(rds, s["cookie"])
    assert _me(client, s["access"]).status_code == 200
    n = _login(client, _email())  # 새 키로 새 로그인은 정상
    assert _refresh(client, n["cookie"]).status_code == 200


def test_logout_racing_refresh_of_older_token_never_revives(client, clock, rds):
    """로그아웃과 「오래된 세대 refresh(재사용 폐기)」·「정상 회전」을 동시에 — 어느 순서로 끝나도 세션은 사라진다."""
    for _ in range(10):
        s = _login(client, _email())
        cur = _refresh_cookie(_refresh(client, s["cookie"]))
        clock.t += 60
        cur = _refresh_cookie(_refresh(client, cur))
        out, barrier = {}, threading.Barrier(3)

        def old():
            barrier.wait()
            out["old"] = _refresh(client, s["cookie"])

        def rotate():
            barrier.wait()
            out["rot"] = _refresh(client, cur)

        def bye():
            barrier.wait()
            out["bye"] = _logout(client, cookie=cur)

        ts = [threading.Thread(target=f) for f in (old, rotate, bye)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert _rec(rds, cur) == {}
        assert out["old"].status_code == 401
        if out["rot"].status_code == 200:
            assert _refresh(client, _refresh_cookie(out["rot"])).status_code == 401
            assert _me(client, out["rot"].json()["accessToken"]).status_code == 401


# ── 동시 재로그인 · 교체 실패 경계 ───────────────────────────────────────────────────────────────

def _session_keys(rds) -> set:
    from app import auth_sessions

    return {k.decode() for k in rds.scan_iter(auth_sessions.key("*"))}


def test_concurrent_relogin_with_same_cookie_leaves_two_new_sessions(client, clock, rds):
    """순차 재로그인과 다르다: 같은 이전 쿠키로 로그인 두 건이 겹치면 이전 세션은 한 번 폐기되지만 새 세션은 둘 다 남는다.
    개발용 간이 로그인의 한계로 기록한다(브라우저에 남는 쿠키는 하나 — 다른 하나는 그 응답을 받은 탭의 access 가 끝날 때까지 쓰인다).
    C 의 소셜 로그인은 브라우저 결속 기준의 교체로 해결해야 한다(계약 정리 v7)."""
    old = _login(client, _email())
    email = _email()
    out, barrier = [], threading.Barrier(2)

    def go():
        barrier.wait()
        out.append(_login(client, email, cookie=old["cookie"]))

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert _rec(rds, old["cookie"]) == {}  # 이전 세션은 폐기
    assert len({_sid(o["cookie"]) for o in out}) == 2
    assert all(_rec(rds, o["cookie"]) for o in out)  # 한계: 둘 다 살아 있다
    assert all(_me(client, o["access"]).status_code == 200 for o in out)


class _FailingEval:
    """세션 생성 스크립트에서만 실패를 흉내 낸다. ran=True 면 실제로 실행한 뒤 응답을 잃은 경우."""

    def __init__(self, real, ran: bool):
        self.real, self.ran = real, ran

    def eval(self, script, *a):
        import redis

        from app import auth_sessions

        if script == auth_sessions._CREATE_REPLACING:
            if self.ran:
                self.real.eval(script, *a)
            raise redis.ConnectionError("시험: 응답 유실" if self.ran else "시험: 연결 실패")
        return self.real.eval(script, *a)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.parametrize("ran", [False, True])
def test_relogin_store_failure_boundaries(client, clock, rds, ran):
    """새 세션 생성과 이전 세션 폐기는 한 스크립트다 — 「생성은 됐는데 폐기는 실패」한 상태가 생기지 않는다.
    ran=False(스크립트 실행 전 실패): 503, 이전 세션 그대로, 새 세션 없음.
    ran=True(실행 뒤 응답 유실): 503, 이전 세션 폐기됨, 새 세션은 서버에 남지만 토큰을 받은 곳이 없다(비활성 7일 뒤 사라짐) → 다시 로그인."""
    from app import auth_sessions

    old = _login(client, _email())
    keys = _session_keys(rds)
    auth_sessions.set_client(_FailingEval(rds, ran))
    try:
        r = client.post("/v1/auth/login", json={"email": _email(), "password": "x"}, headers={"Cookie": f"pxl_refresh={old['cookie']}"})
    finally:
        auth_sessions.set_client(rds)
    assert r.status_code == 503 and _set_cookies(r) == []
    after = _session_keys(rds)
    if ran:
        assert _rec(rds, old["cookie"]) == {} and len(after - keys) == 1
        orphan = next(iter(after - keys))
        assert 0 < rds.ttl(orphan) <= 7 * DAY + 5  # 아무도 쓰지 않으면 비활성 7일에 사라진다(Redis 실제 시계 기준)
    else:
        assert _rec(rds, old["cookie"]) and after == keys
    assert _login(client, _email()).get("access")  # 다시 로그인할 수 있다


# ── refresh 실패 경계 ────────────────────────────────────────────────────────────────────────────

def test_refresh_db_failure_before_rotation_keeps_cookie_usable(client, clock, rds, monkeypatch):
    from app.main import app
    from app.routers import auth as auth_router

    s = _login(client, _email())
    before = _rec(rds, s["cookie"])

    def boom(*a, **k):
        raise RuntimeError("시험: DB 장애")

    with monkeypatch.context() as m:  # 시계(clock)의 바꿔 끼움은 그대로 두고 이것만 되돌린다
        m.setattr(auth_router, "_seller_by_id", boom)
        r = TestClient(app, raise_server_exceptions=False).post("/v1/auth/refresh", headers={**CSRF, "Cookie": f"pxl_refresh={s['cookie']}"})
    assert r.status_code == 500 and _set_cookies(r) == []
    assert _rec(rds, s["cookie"]) == before  # 회전하지 않았다
    assert _refresh(client, s["cookie"]).status_code == 200  # 같은 쿠키로 복구


def test_refresh_failure_after_rotation_is_same_as_lost_response(client, clock, rds, monkeypatch):
    """회전 뒤(access 발급·응답 전송) 실패 — 클라이언트는 후속 토큰을 받지 못한다. R2 응답 유실과 같은 복구 정책:
    유예(30초) 안 이전 쿠키 → refresh_rotated(세션 그대로) / 유예 뒤 → refresh_reused(그 세션 폐기) → 다시 로그인."""
    from app.main import app
    from app.routers import auth as auth_router

    s = _login(client, _email())

    def boom(*a, **k):
        raise RuntimeError("시험: 발급 뒤 실패")

    with monkeypatch.context() as m:
        m.setattr(auth_router, "issue_access", boom)
        r = TestClient(app, raise_server_exceptions=False).post("/v1/auth/refresh", headers={**CSRF, "Cookie": f"pxl_refresh={s['cookie']}"})
    assert r.status_code == 500 and _set_cookies(r) == []
    assert _rec(rds, s["cookie"])["gen"] == "2"  # 서버는 이미 회전했다
    clock.t += 10
    assert _reason(_refresh(client, s["cookie"])) == "refresh_rotated"
    clock.t += 30
    assert _reason(_refresh(client, s["cookie"])) == "refresh_reused"
    assert _rec(rds, s["cookie"]) == {}
