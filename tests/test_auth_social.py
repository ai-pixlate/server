"""소셜 로그인(C) — start · 완료 계약, 브라우저 결속·세션 교체, 점유·늦은 응답·로그아웃·다른 로그인 경합, 실패 경로.

제공자는 대역(httpx.MockTransport)이다 — 실제 카카오·Google 을 부르지 않는다(conftest 가 실제 호출을 막는다).
대역은 실제 제공자처럼 PKCE(verifier ↔ challenge)·redirect_uri·client_id·코드 일회성을 검사한다. 실제 제공자 동작(특히 카카오 PKCE 강제 여부)은 별도 시험이다.
Google ID 토큰은 시험이 만든 RSA 키로 서명한다(서버는 표준 라이브러리로 RS256 을 검증).
시계: 세션(auth_sessions.now)과 시도·점유(Lua — social_auth.clock_ms)를 같은 가짜 시계로 맞춘다. Redis TIME 경로는 따로 한 번 본다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.exec_db import bound_app_db, fresh_database, requires_db

REDIS_URL = os.getenv("PIXLATE_TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(not REDIS_URL, reason="PIXLATE_TEST_REDIS_URL 없음")
pytestmark = [requires_db, requires_redis]

ORIGIN = "https://fe.pixlate.test"
HDR = {"X-Requested-With": "XMLHttpRequest", "Origin": ORIGIN}
KAKAO_APP_ID = 990001  # 시험 값 — 실제 앱 ID 아님
SECRETS = {"PIXLATE_OAUTH_KAKAO_CLIENT_SECRET": "test-kakao-secret-xyz", "PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET": "test-google-secret-xyz"}


# ───────────────────────────── RSA(시험용 키) ─────────────────────────────

def _is_probable_prime(n: int, rnd: random.Random) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(24):
        x = pow(rnd.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rnd: random.Random) -> int:
    while True:
        c = rnd.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(c, rnd):
            return c


class RsaKey:
    def __init__(self, seed: int):
        rnd = random.Random(seed)
        self.e = 65537
        while True:
            p, q = _prime(1024, rnd), _prime(1024, rnd)
            phi = (p - 1) * (q - 1)
            if p != q and phi % self.e:
                break
        self.n, self.d = p * q, pow(self.e, -1, phi)
        self.kid = f"test-kid-{seed}"

    def sign(self, msg: bytes) -> bytes:
        k = (self.n.bit_length() + 7) // 8
        t = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(msg).digest()
        em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(k, "big")

    def jwk(self) -> dict:
        b = lambda i: _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))
        return {"kty": "RSA", "kid": self.kid, "alg": "RS256", "use": "sig", "n": b(self.n), "e": b(self.e)}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def rsa_key():
    return RsaKey(20261110)


def _jwt(key: RsaKey, claims: dict, kid: str | None = None) -> str:
    h = _b64(json.dumps({"alg": "RS256", "kid": kid or key.kid, "typ": "JWT"}).encode())
    p = _b64(json.dumps(claims).encode())
    return f"{h}.{p}.{_b64(key.sign(f'{h}.{p}'.encode()))}"


# ───────────────────────────── 제공자 대역 ─────────────────────────────

class Providers:
    """카카오·Google 대역. 코드는 시험이 등록한다(제공자 화면에서 로그인한 결과에 해당)."""

    def __init__(self, key: RsaKey):
        self.key = key
        self.codes: dict[str, dict] = {}     # code → {provider, challenge, redirect_uri, client_id, profile, used}
        self.tokens: dict[str, dict] = {}    # 카카오 access token → profile
        self.requests: list[httpx.Request] = []
        self.hook: dict[str, callable] = {}  # 호출 이름 → 응답 전에 실행(지연·경합 흉내). 값을 돌려주면 그 응답을 쓴다
        self.google_aud: str | None = None
        self.app_id = KAKAO_APP_ID

    def issue_code(self, provider: str, authorize_url: str, profile: dict) -> tuple[str, str]:
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = f"code-{uuid.uuid4().hex}"
        self.codes[code] = {"provider": provider, "challenge": q["code_challenge"], "method": q["code_challenge_method"],
                            "redirect_uri": q["redirect_uri"], "client_id": q["client_id"], "nonce": q.get("nonce"),
                            "profile": profile, "used": False}
        return code, q["state"]

    def _run_hook(self, name):
        f = self.hook.get(name)
        return f() if f else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "kauth.kakao.com" and path == "/oauth/token":
            return self._token("kakao", request)
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._token("google", request)
        if host == "kapi.kakao.com" and path == "/v1/user/access_token_info":
            r = self._run_hook("kakao_token_info")
            if r is not None:
                return r
            prof = self.tokens.get(request.headers.get("authorization", "")[7:])
            if prof is None:
                return httpx.Response(401, json={"msg": "this access token does not exist", "code": -401})
            return httpx.Response(200, json={"id": prof["id"], "app_id": self.app_id, "expires_in": 21599})
        if host == "kapi.kakao.com" and path == "/v2/user/me":
            r = self._run_hook("kakao_me")
            if r is not None:
                return r
            prof = self.tokens.get(request.headers.get("authorization", "")[7:])
            assert json.loads(request.url.params["property_keys"]) == ["kakao_account.email", "kakao_account.profile"]
            return httpx.Response(200, json={"id": prof["id"], "kakao_account": prof.get("kakao_account", {})})
        if host == "accounts.google.com" and path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"jwks_uri": "https://www.googleapis.com/oauth2/v3/certs"})
        if host == "www.googleapis.com" and path == "/oauth2/v3/certs":
            return httpx.Response(200, json={"keys": [self.key.jwk()]}, headers={"cache-control": "public, max-age=3600"})
        raise AssertionError(f"대역에 없는 호출 {request.method} {request.url}")

    def _token(self, provider: str, request: httpx.Request) -> httpx.Response:
        r = self._run_hook(f"{provider}_token")
        if r is not None:
            return r
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        c = self.codes.get(form.get("code", ""))
        bad = lambda code: httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad", "error_code": code})
        if c is None or c["used"] or c["provider"] != provider:
            return bad("KOE320")
        c["used"] = True  # 일회용 — 실패한 교환도 코드를 소비한다
        if form.get("redirect_uri") != c["redirect_uri"] or form.get("client_id") != c["client_id"] or form.get("grant_type") != "authorization_code":
            return bad("KOE303")
        verifier = form.get("code_verifier", "")
        if c["method"] != "S256" or _b64(hashlib.sha256(verifier.encode()).digest()) != c["challenge"]:
            return bad("KOE320")  # PKCE 불일치
        secret = SECRETS[f"PIXLATE_OAUTH_{provider.upper()}_CLIENT_SECRET"]
        if form.get("client_secret") != secret:
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "Bad client credentials", "error_code": "KOE010"})
        prof = c["profile"]
        if provider == "kakao":
            at = f"kat-{uuid.uuid4().hex}"
            self.tokens[at] = prof
            return httpx.Response(200, json={"access_token": at, "token_type": "bearer", "expires_in": 21599})
        claims = {"iss": "https://accounts.google.com", "aud": self.google_aud or c["client_id"], "sub": prof["sub"],
                  "exp": 4102444800, "iat": 1700000000, "nonce": c["nonce"], **prof.get("claims", {})}
        return httpx.Response(200, json={"access_token": "gat", "id_token": _jwt(self.key, claims), "expires_in": 3599})


# ───────────────────────────── 고정물 ─────────────────────────────

class Clock:
    def __init__(self):
        self.t = 2_000_000_000


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
    auth_sessions.set_client(r)
    yield r


@pytest.fixture
def clock(monkeypatch, rds):
    from app import auth_sessions, social_auth

    c = Clock()
    monkeypatch.setattr(auth_sessions, "now", lambda: c.t)
    monkeypatch.setattr(social_auth, "clock_ms", lambda: c.t * 1000)
    monkeypatch.setattr("app.security._now", lambda: c.t, raising=False)
    yield c


@pytest.fixture
def env(monkeypatch):
    vals = {"PIXLATE_OAUTH_PROVIDERS": "kakao,google", "PIXLATE_OAUTH_REDIRECT_BASE": ORIGIN,
            "PIXLATE_OAUTH_KAKAO_CLIENT_ID": "test-kakao-rest-key", "PIXLATE_OAUTH_KAKAO_APP_ID": str(KAKAO_APP_ID),
            "PIXLATE_OAUTH_GOOGLE_CLIENT_ID": "test-google-client.apps.example", **SECRETS}
    for k in [k for k in os.environ if k.startswith("PIXLATE_OAUTH_")]:
        monkeypatch.delenv(k)
    for k, v in vals.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


@pytest.fixture
def prov(rsa_key):
    from app import social_auth

    p = Providers(rsa_key)
    social_auth.set_transport(httpx.MockTransport(p))
    yield p


@pytest.fixture
def client(dburl, env, clock, prov):
    from app.main import app

    with TestClient(app, base_url="https://testserver") as c:
        yield c


# ───────────────────────────── 도우미 ─────────────────────────────

def _cookies(r) -> dict[str, str]:
    out = {}
    for h in r.headers.get_list("set-cookie"):
        name, _, rest = h.partition("=")
        out[name.strip()] = h
    return out


def _value(set_cookie: str) -> str:
    return set_cookie.split("=", 1)[1].split(";", 1)[0]


def _attrs(set_cookie: str) -> dict:
    return {x.strip().split("=", 1)[0].lower(): (x.strip().split("=", 1) + [""])[1] for x in set_cookie.split(";")[1:]}


def _post(client, url, cookies: dict | None = None, headers: dict | None = None, **kw):
    client.cookies.clear()  # 쿠키는 시험이 명시한 것만 — 브라우저 하나하나를 시험이 흉내 낸다
    h = dict(HDR if headers is None else headers)
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    return client.post(url, headers=h, **kw)


class Browser:
    """쿠키 두 개(결속·refresh)를 들고 다니는 브라우저 하나."""

    def __init__(self, client, prov):
        self.c, self.p = client, prov
        self.bind: str | None = None
        self.refresh: str | None = None

    def jar(self):
        return {"pxl_oauth_bind": self.bind, "pxl_refresh": self.refresh}

    def absorb(self, r):
        ck = _cookies(r)
        for name, attr in (("pxl_oauth_bind", "bind"), ("pxl_refresh", "refresh")):
            if name in ck:
                setattr(self, attr, None if _attrs(ck[name]).get("max-age") == "0" else _value(ck[name]))

    def start(self, provider="kakao"):
        r = _post(self.c, f"/v1/auth/social/{provider}/start", self.jar())
        assert r.status_code == 200, r.text
        self.absorb(r)
        return r

    def complete(self, provider, code, state, absorb=True):
        r = _post(self.c, "/v1/auth/social", self.jar(), json={"provider": provider, "code": code, "state": state})
        if absorb:
            self.absorb(r)
        return r

    def login(self, provider="kakao", profile=None):
        url = self.start(provider).json()["authorizeUrl"]
        code, state = self.p.issue_code(provider, url, profile or _kakao(1001))
        r = self.complete(provider, code, state)
        assert r.status_code == 200, r.text
        return r

    def logout(self, access=None):
        h = dict(HDR)
        if access:
            h["Authorization"] = f"Bearer {access}"
        r = _post(self.c, "/v1/auth/logout", self.jar(), headers=h)
        self.absorb(r)
        return r


def _kakao(uid: int, email="k@example.com", nickname="카카오닉", valid=True, verified=True, **acct) -> dict:
    a = {"profile": {"nickname": nickname} if nickname is not None else {}, "email": email,
         "is_email_valid": valid, "is_email_verified": verified, "email_needs_agreement": False, **acct}
    if email is None:
        a.pop("email")
    return {"id": uid, "kakao_account": a}


def _google(sub: str, **claims) -> dict:
    return {"sub": sub, "claims": {"email": "g@example.com", "email_verified": True, "name": "구글 이름", **claims}}


def _uid() -> int:
    return random.randint(10**9, 10**10)


def _reason(r):
    return ((r.json().get("error") or {}).get("details") or {}).get("reason")


def _err(r):
    return r.json()["error"]


def _sessions(rds) -> set:
    return {k.decode() for k in rds.scan_iter("pixlate:auth:sess:*")}


def _seller(dburl, provider, subject):
    import sqlalchemy as sa

    eng = sa.create_engine(dburl)
    try:
        with eng.connect() as c:
            return c.execute(sa.text("SELECT id, email, display_name, auth_provider, cognito_sub FROM seller "
                                     "WHERE auth_provider = :p AND provider_subject = :s"), {"p": provider, "s": str(subject)}).mappings().all()
    finally:
        eng.dispose()


def _me(client, access):
    client.cookies.clear()
    return client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"})


def _no_cookies(r):
    return r.headers.get_list("set-cookie") == []


@pytest.fixture
def live_logs(monkeypatch):
    """migrations/env.py 의 fileConfig 가 이미 만들어진 로거를 끈다(같은 프로세스에서 마이그레이션을 돌린 다른 시험 뒤).
    로그 내용을 보는 시험은 대상 로거를 다시 켜서, 아무것도 안 찍혀 통과하는 일을 막는다."""
    for name in ("pixlate.auth", "pixlate.auth.social", "httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)


# ═════════════════════════════ start ═════════════════════════════

@pytest.mark.parametrize("provider", ["kakao", "google"])
def test_start_returns_authorize_url_and_binding_cookie(client, prov, rds, provider):
    r = _post(client, f"/v1/auth/social/{provider}/start")
    assert r.status_code == 200 and set(r.json()) == {"authorizeUrl"} and r.headers["cache-control"] == "no-store"
    u = urlsplit(r.json()["authorizeUrl"])
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert (u.scheme, u.netloc) == (("https", "kauth.kakao.com") if provider == "kakao" else ("https", "accounts.google.com"))
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["redirect_uri"] == f"{ORIGIN}/login/callback/{provider}"
    assert q["scope"] == {"kakao": "profile_nickname account_email", "google": "openid email profile"}[provider]
    assert len(q["state"]) >= 43 and (("nonce" in q) == (provider == "google"))
    ck = _cookies(r)
    assert set(ck) == {"pxl_oauth_bind"}  # refresh 는 주지 않는다
    a = _attrs(ck["pxl_oauth_bind"])
    assert a.get("max-age") == "600" and a.get("path") == "/auth" and "httponly" in a and "secure" in a and a.get("samesite", "").lower() == "strict"
    # 서버에는 state·결속 원문이 아니라 해시만 키로 남는다
    keys = {k.decode() for k in rds.scan_iter("pixlate:oauth:*")}
    assert not any(q["state"] in k or _value(ck["pxl_oauth_bind"]) in k for k in keys)


def test_start_in_same_browser_reuses_binding_and_only_last_attempt_is_valid(client, prov):
    b = Browser(client, prov)
    url1 = b.start().json()["authorizeUrl"]
    first_bind = b.bind
    url2 = b.start().json()["authorizeUrl"]
    assert b.bind == first_bind
    code1, state1 = prov.issue_code("kakao", url1, _kakao(_uid()))
    code2, state2 = prov.issue_code("kakao", url2, _kakao(_uid()))
    r = b.complete("kakao", code1, state1)
    assert r.status_code == 401 and _reason(r) == "state_invalid" and _no_cookies(r)
    assert b.complete("kakao", code2, state2).status_code == 200


# ═════════════════════════════ 비활성 404 · 설정 오류 503 ═════════════════════════════

@pytest.mark.parametrize("setting", ["", "google", "naver"])
def test_disabled_provider_is_404_on_both_endpoints(client, env, prov, setting):
    env.setenv("PIXLATE_OAUTH_PROVIDERS", setting)
    r1 = _post(client, "/v1/auth/social/kakao/start")
    r2 = _post(client, "/v1/auth/social", json={"provider": "kakao", "code": "c", "state": "s"})
    for r in (r1, r2):
        assert r.status_code == 404 and _err(r)["code"] == "NOT_FOUND" and _err(r)["details"] is None and _no_cookies(r)
    assert prov.requests == []


def test_unknown_provider_path_is_404(client):
    r = _post(client, "/v1/auth/social/naver/start")
    assert r.status_code == 404 and _err(r)["details"] is None


@pytest.mark.parametrize("change", [
    ("PIXLATE_OAUTH_KAKAO_CLIENT_ID", None),
    ("PIXLATE_OAUTH_KAKAO_APP_ID", "not-a-number"),
    ("PIXLATE_OAUTH_REDIRECT_BASE", "http://fe.example.com"),     # localhost 밖 http
    ("PIXLATE_OAUTH_REDIRECT_BASE", f"{ORIGIN}/login"),           # 경로 포함
    ("PIXLATE_OAUTH_CLAIM_TTL_S", "20"),                          # 호출 3회 × 10초 + 10 보다 짧음
    ("PIXLATE_OAUTH_ATTEMPT_TTL_S", "abc"),
])
def test_enabled_but_misconfigured_is_503_without_leaking_settings(client, env, prov, caplog, live_logs, change):
    name, value = change
    env.delenv(name) if value is None else env.setenv(name, value)
    with caplog.at_level(logging.ERROR, logger="pixlate.auth.social"):
        r1 = _post(client, "/v1/auth/social/kakao/start")
        r2 = _post(client, "/v1/auth/social", json={"provider": "kakao", "code": "c", "state": "s"})
    for r in (r1, r2):
        e = _err(r)
        assert r.status_code == 503 and e["code"] == "SERVICE_UNAVAILABLE" and e["retryable"] is True and e["details"] is None
        assert _no_cookies(r) and "PIXLATE" not in r.text and "test-kakao" not in r.text
    logged = " ".join(m.getMessage() for m in caplog.records)
    assert name.split("<")[0] in logged and "test-kakao-rest-key" not in logged and "test-kakao-secret" not in logged
    assert prov.requests == []


def test_google_missing_secret_is_config_error_but_kakao_still_works(client, env, prov):
    env.delenv("PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET")
    assert _post(client, "/v1/auth/social/google/start").status_code == 503
    Browser(client, prov).login("kakao", _kakao(_uid()))


# ═════════════════════════════ Origin · CSRF ═════════════════════════════

@pytest.mark.parametrize("headers,status", [
    ({"X-Requested-With": "XMLHttpRequest"}, 401),
    ({"X-Requested-With": "XMLHttpRequest", "Origin": "https://evil.test"}, 401),
    ({"Origin": ORIGIN}, 422),
])
def test_origin_and_csrf_checks(client, prov, rds, headers, status):
    before = {k for k in rds.scan_iter("pixlate:oauth:att:*")}
    r1 = _post(client, "/v1/auth/social/kakao/start", headers=headers)
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    r2 = _post(client, "/v1/auth/social", b.jar(), headers=headers, json={"provider": "kakao", "code": code, "state": state})
    for r in (r1, r2):
        assert r.status_code == status and _no_cookies(r)
        if status == 401:
            assert _reason(r) == "origin_not_allowed"
    assert len({k for k in rds.scan_iter("pixlate:oauth:att:*")} - before) == 1  # 거절된 start 는 시도를 만들지 않았다
    assert b.complete("kakao", code, state).status_code == 200  # 거절된 완료는 시도·코드를 쓰지 않았다


# ═════════════════════════════ 성공 · 계정 · 프로필 ═════════════════════════════

def test_kakao_first_login_creates_account_and_session(client, prov, rds, dburl):
    uid = _uid()
    b = Browser(client, prov)
    before = _sessions(rds)
    r = b.login("kakao", _kakao(uid, email="first@example.com", nickname="첫닉"))
    body = r.json()
    assert set(body) == {"accessToken", "seller"} and body["seller"]["email"] == "first@example.com"
    ck = _cookies(r)
    assert _attrs(ck["pxl_oauth_bind"]).get("max-age") == "0"  # 결속 쿠키 삭제
    ra = _attrs(ck["pxl_refresh"])
    assert ra.get("path") == "/auth" and "httponly" in ra and "secure" in ra and ra.get("samesite", "").lower() == "strict"
    assert len(_sessions(rds) - before) == 1
    rows = _seller(dburl, "kakao", uid)
    assert len(rows) == 1 and rows[0]["email"] == "first@example.com" and rows[0]["display_name"] == "첫닉" and rows[0]["cognito_sub"] is None
    me = _me(client, body["accessToken"])
    assert me.status_code == 200 and me.json()["id"] == body["seller"]["id"]
    # 교환 요청: 같은 시도의 verifier · 같은 redirect_uri · client_secret · access_token_info 로 앱 확인
    tok = [q for q in prov.requests if q.url.path == "/oauth/token"][-1]
    form = {k: v[0] for k, v in parse_qs(tok.content.decode()).items()}
    assert form["redirect_uri"] == f"{ORIGIN}/login/callback/kakao" and form["client_secret"] == SECRETS["PIXLATE_OAUTH_KAKAO_CLIENT_SECRET"]
    assert any(q.url.path == "/v1/user/access_token_info" for q in prov.requests)


def test_relogin_finds_same_account_by_provider_and_subject_not_email(client, prov, dburl):
    uid = _uid()
    first = Browser(client, prov).login("kakao", _kakao(uid, email="same@example.com")).json()["seller"]["id"]
    again = Browser(client, prov).login("kakao", _kakao(uid, email="same@example.com")).json()["seller"]["id"]
    g = Browser(client, prov).login("google", _google(str(uid), email="same@example.com")).json()["seller"]["id"]
    local = _post(client, "/v1/auth/login", json={"email": "same@example.com", "password": "x"}).json()["seller"]["id"]
    assert first == again and len({first, g, local}) == 3  # 같은 이메일이어도 제공자가 다르면 다른 계정


@pytest.mark.parametrize("second,expect_email,expect_name", [
    (dict(email="new@example.com", nickname="새닉"), "new@example.com", "새닉"),            # ① 확인된 새 값 → 덮어씀
    (dict(email_needs_agreement=True, email=None), None, "카카오닉"),                       # ② 철회 명시 → 이메일 지움
    (dict(valid=False), None, "카카오닉"),                                                  # ② 무효 명시
    (dict(verified=False), None, "카카오닉"),                                               # ② 미인증 명시
    (dict(nickname=None, profile_nickname_needs_agreement=True), "k@example.com", None),    # ② 닉네임 동의 철회 → 이름만 지움
    (dict(email=None, nickname=None), "k@example.com", "카카오닉"),                          # ③ 키 누락 → 그대로
    (dict(nickname="x" * 256), "k@example.com", "카카오닉"),                                 # 255자 초과 → 저장 안 함(그대로)
])
def test_kakao_profile_update_rules(client, prov, dburl, second, expect_email, expect_name):
    uid = _uid()
    Browser(client, prov).login("kakao", _kakao(uid))
    kw = {"email": "k@example.com", "nickname": "카카오닉", "valid": True, "verified": True}
    extra = {k: v for k, v in second.items() if k not in kw}
    kw.update({k: v for k, v in second.items() if k in kw})
    Browser(client, prov).login("kakao", _kakao(uid, **kw, **extra))
    row = _seller(dburl, "kakao", uid)[0]
    assert (row["email"], row["display_name"]) == (expect_email, expect_name)


def test_kakao_unverified_email_is_not_stored_on_first_login(client, prov, dburl):
    uid = _uid()
    body = Browser(client, prov).login("kakao", _kakao(uid, email="x@example.com", verified=False)).json()
    assert body["seller"]["email"] is None and _seller(dburl, "kakao", uid)[0]["email"] is None


def test_kakao_user_info_outage_new_account_fails_existing_account_logs_in(client, prov, dburl, rds):
    uid = _uid()
    prov.hook["kakao_me"] = lambda: httpx.Response(503)
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(uid))
    r = b.complete("kakao", code, state)
    assert r.status_code == 503 and _reason(r) == "provider_unavailable" and _no_cookies(r)
    assert _seller(dburl, "kakao", uid) == []  # 확인되지 않은 프로필로 계정을 만들지 않았다
    prov.hook.clear()
    Browser(client, prov).login("kakao", _kakao(uid, email="keep@example.com", nickname="유지"))
    prov.hook["kakao_me"] = lambda: httpx.Response(500)
    r = Browser(client, prov).login("kakao", _kakao(uid, email="other@example.com", nickname="바뀜"))
    row = _seller(dburl, "kakao", uid)[0]
    assert r.json()["seller"]["email"] == "keep@example.com" and (row["email"], row["display_name"]) == ("keep@example.com", "유지")


def test_google_login_verifies_id_token_and_stores_verified_profile(client, prov, dburl):
    sub = f"g-{uuid.uuid4().hex[:12]}"
    body = Browser(client, prov).login("google", _google(sub, email="gg@example.com", name="지 이름")).json()
    row = _seller(dburl, "google", sub)[0]
    assert body["seller"]["email"] == "gg@example.com" and (row["email"], row["display_name"]) == ("gg@example.com", "지 이름")
    Browser(client, prov).login("google", _google(sub, email="gg2@example.com", email_verified=False, name=None))
    row = _seller(dburl, "google", sub)[0]
    assert (row["email"], row["display_name"]) == (None, "지 이름")  # email_verified false = 지움, name 누락 = 그대로


@pytest.mark.parametrize("tamper", ["signature", "aud", "iss", "exp", "nonce", "kid", "alg"])
def test_google_bad_id_token_is_provider_auth_failed_and_closes_attempt(client, prov, rsa_key, tamper):
    b = Browser(client, prov)
    url = b.start("google").json()["authorizeUrl"]
    sub = f"g-{uuid.uuid4().hex[:12]}"
    code, state = prov.issue_code("google", url, _google(sub))
    nonce = prov.codes[code]["nonce"]
    claims = {"iss": "https://accounts.google.com", "aud": "test-google-client.apps.example", "sub": sub, "exp": 4102444800, "nonce": nonce}
    if tamper == "signature":
        tok = _jwt(RsaKey(7), claims, kid=rsa_key.kid)
    elif tamper == "kid":
        tok = _jwt(rsa_key, claims, kid="unknown-kid")
    elif tamper == "alg":
        h = _b64(json.dumps({"alg": "none", "kid": rsa_key.kid}).encode())
        tok = f"{h}.{_b64(json.dumps(claims).encode())}."
    else:
        claims[tamper] = {"aud": "other-client", "iss": "https://evil.example", "exp": 1000, "nonce": "other-nonce"}[tamper]
        tok = _jwt(rsa_key, claims)
    prov.hook["google_token"] = lambda: httpx.Response(200, json={"access_token": "gat", "id_token": tok})
    r = b.complete("google", code, state)
    assert r.status_code == 401 and _reason(r) == "provider_auth_failed" and _no_cookies(r), tamper
    prov.hook.clear()
    again = b.complete("google", code, state)
    assert again.status_code == 401 and _reason(again) == "state_invalid"  # 실패로 닫힌 시도는 다시 받지 않는다


# ═════════════════════════════ 제공자 실패 경로 ═════════════════════════════

@pytest.mark.parametrize("response,status,reason", [
    (httpx.Response(400, json={"error": "invalid_grant", "error_code": "KOE320"}), 401, "provider_auth_failed"),
    (httpx.Response(400, json={"error": "invalid_grant", "error_code": "KOE303"}), 401, "provider_auth_failed"),
    (httpx.Response(401, json={"error": "invalid_client", "error_code": "KOE010"}), 503, None),  # 우리 설정 문제
    (httpx.Response(401, json={"error": "invalid_client", "error_code": "KOE101"}), 503, None),
    (httpx.Response(400, json={"error": "invalid_request", "error_code": "KOE237"}), 503, "provider_unavailable"),  # 요청 한도
    (httpx.Response(502, text="bad gateway"), 503, "provider_unavailable"),
    (httpx.Response(200, text="not json"), 503, "provider_unavailable"),
    ("timeout", 503, "provider_unavailable"),
    ("network", 503, "provider_unavailable"),
])
def test_kakao_token_failures(client, prov, rds, response, status, reason):
    def hook():
        if response == "timeout":
            raise httpx.ReadTimeout("t")
        if response == "network":
            raise httpx.ConnectError("c")
        return response
    prov.hook["kakao_token"] = hook
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    before = _sessions(rds)
    r = b.complete("kakao", code, state)
    e = _err(r)
    assert r.status_code == status and _reason(r) == reason and _no_cookies(r), r.text
    assert e["retryable"] is (status == 503)
    if reason is None:
        assert e["details"] is None and "KOE" not in r.text
    assert _sessions(rds) == before
    prov.hook.clear()
    assert _reason(b.complete("kakao", code, state)) == "state_invalid"  # 같은 코드 재전송은 받지 않는다 — start 부터


def test_kakao_app_id_mismatch_is_config_error(client, prov):
    prov.app_id = 123
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    r = b.complete("kakao", code, state)
    assert r.status_code == 503 and _err(r)["details"] is None


def test_pkce_verifier_of_another_attempt_is_rejected(client, prov):
    """다른 시도(다른 브라우저)의 인가 코드를 이 시도로 완료하면 verifier 가 달라 교환되지 않는다."""
    a, b = Browser(client, prov), Browser(client, prov)
    url_a = a.start().json()["authorizeUrl"]
    url_b = b.start().json()["authorizeUrl"]
    code_a, _ = prov.issue_code("kakao", url_a, _kakao(_uid()))
    _, state_b = prov.issue_code("kakao", url_b, _kakao(_uid()))
    r = b.complete("kakao", code_a, state_b)
    assert r.status_code == 401 and _reason(r) == "provider_auth_failed"


# ═════════════════════════════ state_invalid · 결속 ═════════════════════════════

def test_state_invalid_cases_do_not_consume_the_real_attempt(client, prov):
    owner, attacker = Browser(client, prov), Browser(client, prov)
    url = owner.start().json()["authorizeUrl"]
    attacker.start()
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    cases = {
        "모르는 state": _post(client, "/v1/auth/social", owner.jar(), json={"provider": "kakao", "code": code, "state": "nope"}),
        "결속 쿠키 없음": _post(client, "/v1/auth/social", {"pxl_refresh": owner.refresh}, json={"provider": "kakao", "code": code, "state": state}),
        "다른 브라우저(로그인 CSRF)": attacker.complete("kakao", code, state, absorb=False),
        "제공자 불일치": owner.complete("google", code, state, absorb=False),
    }
    for why, r in cases.items():
        assert r.status_code == 401 and _reason(r) == "state_invalid" and _no_cookies(r), why
    assert not prov.codes[code]["used"]  # 거절된 요청은 제공자를 부르지 않았다
    assert owner.complete("kakao", code, state).status_code == 200


def test_attempt_lifetime_expired_and_used_attempt(client, prov, clock):
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    clock.t += 600
    r = b.complete("kakao", code, state)
    assert r.status_code == 401 and _reason(r) == "state_invalid" and not prov.codes[code]["used"]
    b2 = Browser(client, prov)
    url = b2.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    jar = b2.jar()
    assert b2.complete("kakao", code, state).status_code == 200
    r = _post(client, "/v1/auth/social", jar, json={"provider": "kakao", "code": code, "state": state})  # 성공한 요청을 다시 보냄
    assert r.status_code == 401 and _reason(r) == "state_invalid"


# ═════════════════════════════ 점유 · 동시 실행 · 늦은 응답 ═════════════════════════════

def test_concurrent_completion_of_same_attempt_one_wins_other_409(client, prov, rds):
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    jar = b.jar()
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(10)
    prov.hook["kakao_token"] = slow
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("first", _post(client, "/v1/auth/social", jar,
                                                                        json={"provider": "kakao", "code": code, "state": state})))
    t.start()
    assert entered.wait(10)
    second = _post(client, "/v1/auth/social", jar, json={"provider": "kakao", "code": code, "state": state})
    release.set()
    t.join(10)
    del prov.hook["kakao_token"]
    assert second.status_code == 409 and _err(second)["code"] == "DUPLICATE" and _reason(second) == "login_in_progress" and _no_cookies(second)
    assert out["first"].status_code == 200
    assert _reason(_post(client, "/v1/auth/social", jar, json={"provider": "kakao", "code": code, "state": state})) == "state_invalid"


def test_409_does_not_last_forever_claim_expiry_makes_state_invalid(client, prov, clock):
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    jar = b.jar()
    entered, release = threading.Event(), threading.Event()
    prov.hook["kakao_token"] = lambda: (entered.set(), release.wait(10)) and None
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("first", _post(client, "/v1/auth/social", jar,
                                                                        json={"provider": "kakao", "code": code, "state": state})))
    t.start()
    assert entered.wait(10)
    body = {"provider": "kakao", "code": code, "state": state}
    clock.t += 59
    assert _post(client, "/v1/auth/social", jar, json=body).status_code == 409
    clock.t += 1  # 점유 상한(60초) 도달
    r = _post(client, "/v1/auth/social", jar, json=body)
    assert r.status_code == 401 and _reason(r) == "state_invalid"
    release.set()
    t.join(10)
    assert out["first"].status_code == 401 and _reason(out["first"]) == "state_invalid"  # 늦게 끝난 첫 요청도 세션을 만들지 않는다


@pytest.mark.parametrize("delay,ok", [(30, True), (59, True), (60, False), (61, False), (600, False)])
def test_late_provider_response_never_creates_a_session(client, prov, rds, clock, delay, ok):
    """제공자 대역이 점유 상한(60초)을 넘겨 응답 → 세션 키 생성 없음·쿠키 없음·state_invalid·시도는 닫힘. 상한 안의 지연은 성공."""
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))

    def slow():
        clock.t += delay
    prov.hook["kakao_me"] = lambda: slow()
    before = _sessions(rds)
    r = b.complete("kakao", code, state)
    del prov.hook["kakao_me"]
    if ok:
        assert r.status_code == 200 and len(_sessions(rds) - before) == 1
    else:
        assert r.status_code == 401 and _reason(r) == "state_invalid" and _no_cookies(r)
        assert _sessions(rds) == before
        assert _reason(b.complete("kakao", code, state)) == "state_invalid"


def test_logout_during_processing_blocks_the_late_completion(client, prov, rds):
    """처리 중 같은 브라우저(결속)에서 로그아웃 → 그 완료는 세션을 만들지 않는다."""
    b = Browser(client, prov)
    b.login("kakao", _kakao(_uid()))
    old_refresh = b.refresh
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    jar = b.jar()
    other_tab = {}

    def logout_now():
        other_tab["r"] = _post(client, "/v1/auth/logout", jar)
    prov.hook["kakao_token_info"] = lambda: logout_now()
    before = _sessions(rds)
    r = _post(client, "/v1/auth/social", jar, json={"provider": "kakao", "code": code, "state": state})
    del prov.hook["kakao_token_info"]
    assert other_tab["r"].status_code == 204
    assert r.status_code == 401 and _reason(r) == "state_invalid" and _no_cookies(r)
    assert not (_sessions(rds) - before)
    assert _post(client, "/v1/auth/refresh", {"pxl_refresh": old_refresh}).status_code == 401  # 이전 세션도 로그아웃으로 끝남


def test_logout_after_lost_completion_response_ends_the_binding_session(client, prov, rds):
    """완료 응답을 잃어 쿠키를 못 받았어도(결속 쿠키가 남음) 로그아웃이 그 결속으로 만든 세션을 끝낸다."""
    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    lost = b.complete("kakao", code, state, absorb=False)  # 응답 유실 — 브라우저 쿠키는 그대로
    assert lost.status_code == 200
    new_refresh = _value(_cookies(lost)["pxl_refresh"])
    assert b.logout().status_code == 204
    assert _post(client, "/v1/auth/refresh", {"pxl_refresh": new_refresh}).status_code == 401
    assert b.bind is None  # 로그아웃이 결속 쿠키도 지웠다


def test_new_start_during_processing_supersedes_the_old_completion(client, prov, rds):
    """처리 중 같은 브라우저에서 다른 로그인 시작 → 이전 완료는 세션을 만들지 않고, 새 시도만 완료된다(세션 하나)."""
    b = Browser(client, prov)
    url1 = b.start().json()["authorizeUrl"]
    code1, state1 = prov.issue_code("kakao", url1, _kakao(_uid()))
    jar = b.jar()
    later = {}

    def restart():
        later["url"] = _post(client, "/v1/auth/social/kakao/start", jar).json()["authorizeUrl"]
    prov.hook["kakao_token_info"] = lambda: restart()
    before = _sessions(rds)
    r = _post(client, "/v1/auth/social", jar, json={"provider": "kakao", "code": code1, "state": state1})
    del prov.hook["kakao_token_info"]
    assert r.status_code == 401 and _reason(r) == "state_invalid" and not (_sessions(rds) - before)
    code2, state2 = prov.issue_code("kakao", later["url"], _kakao(_uid()))
    assert _post(client, "/v1/auth/social", jar, json={"provider": "kakao", "code": code2, "state": state2}).status_code == 200
    assert len(_sessions(rds) - before) == 1


def test_relogin_in_same_browser_replaces_current_session_any_generation(client, prov, rds):
    """한 브라우저 한 세션: 새 로그인은 이 브라우저의 refresh 쿠키 세션을 끝낸다 — 오래된 세대 쿠키여도."""
    b = Browser(client, prov)
    first = b.login("kakao", _kakao(_uid()))
    old = b.refresh
    for _ in range(2):  # 세대 3 으로 회전 — 브라우저에는 오래된 세대(1)가 남았다고 가정
        r = _post(client, "/v1/auth/refresh", {"pxl_refresh": b.refresh})
        b.refresh = _value(_cookies(r)["pxl_refresh"])
    b.refresh = old
    b.login("google", _google(f"g-{uuid.uuid4().hex[:10]}"))  # 다른 계정으로 로그인
    assert _me(client, first.json()["accessToken"]).status_code == 401  # 이전 계정 세션 종료


def test_concurrent_first_login_of_same_subject_creates_one_account(client, prov, dburl):
    uid = _uid()
    browsers = [Browser(client, prov) for _ in range(4)]
    reqs = []
    for b in browsers:
        url = b.start().json()["authorizeUrl"]
        reqs.append((b.jar(), *prov.issue_code("kakao", url, _kakao(uid))))
    barrier, out = threading.Barrier(len(reqs)), []

    def go(jar, code, state):
        barrier.wait()
        out.append(_post(client, "/v1/auth/social", jar, json={"provider": "kakao", "code": code, "state": state}))
    ts = [threading.Thread(target=go, args=x) for x in reqs]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert [r.status_code for r in out] == [200] * 4
    assert len(_seller(dburl, "kakao", uid)) == 1 and len({r.json()["seller"]["id"] for r in out}) == 1


# ═════════════════════════════ 저장소 장애 · Redis TIME · 로그 ═════════════════════════════

def test_store_outage_before_claim_is_503_and_consumes_nothing(client, prov, rds):
    import redis

    from app import auth_sessions

    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    auth_sessions.set_client(redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2))
    try:
        for r in (_post(client, "/v1/auth/social/kakao/start"), b.complete("kakao", code, state, absorb=False)):
            assert r.status_code == 503 and _err(r)["details"] is None and _no_cookies(r)
    finally:
        auth_sessions.set_client(rds)
    assert not prov.codes[code]["used"]


def test_store_outage_at_issue_is_503_without_session(client, prov, rds):
    from app import social_auth

    b = Browser(client, prov)
    url = b.start().json()["authorizeUrl"]
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    real = social_auth._eval

    def failing(script, keys, *args):
        if script == social_auth._ISSUE:
            from app import auth_sessions
            raise auth_sessions.Unavailable("down")
        return real(script, keys, *args)
    social_auth._eval = failing
    before = _sessions(rds)
    try:
        r = b.complete("kakao", code, state)
    finally:
        social_auth._eval = real
    assert r.status_code == 503 and _err(r)["details"] is None and _no_cookies(r) and _sessions(rds) == before
    assert _reason(b.complete("kakao", code, state)) == "state_invalid"


def test_redis_time_clock_path(client, prov, monkeypatch):
    """시험 시계를 빼고 Lua 의 Redis TIME 으로 — 같은 흐름이 동작한다."""
    from app import social_auth

    monkeypatch.setattr(social_auth, "clock_ms", lambda: None)
    b = Browser(client, prov)
    r = b.login("kakao", _kakao(_uid()))
    assert r.status_code == 200


def test_secrets_codes_and_tokens_are_not_logged(client, prov, caplog, live_logs):
    with caplog.at_level(logging.DEBUG):
        b = Browser(client, prov)
        url = b.start().json()["authorizeUrl"]
        code, state = prov.issue_code("kakao", url, _kakao(_uid()))
        r = b.complete("kakao", code, state)
    text = " ".join(m.getMessage() for m in caplog.records)
    assert "oauth.start" in text and "oauth.login" in text  # 로그가 실제로 잡혔다(비어서 통과하지 않음)
    for secret in (code, state, b.refresh, r.json()["accessToken"], SECRETS["PIXLATE_OAUTH_KAKAO_CLIENT_SECRET"]):
        assert secret and secret not in text
