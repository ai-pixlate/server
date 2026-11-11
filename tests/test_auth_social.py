"""소셜 로그인(C) — start · 완료 계약, 브라우저 결속·세션 교체, 점유·늦은 응답·로그아웃·다른 로그인 경합, 실패 경로.

제공자는 대역(httpx.MockTransport)이다 — 실제 카카오·Google 을 부르지 않는다(conftest 가 실제 호출을 막는다).
대역은 실제 제공자처럼 PKCE(verifier ↔ challenge)·redirect_uri·client_id·코드 일회성을 검사한다. 실제 제공자 동작(특히 카카오 PKCE 강제 여부)은 별도 시험이다.
Google ID 토큰은 시험이 cryptography 로 서명하고 대역 인증서(X.509 PEM)를 준다 — 서버는 google-auth(verify_oauth2_token)로 검증한다.
독립 근거로 OpenSSL 이 만든 키·인증서·서명도 대조한다. 실제 Google ID 토큰 통과는 실제 제공자 시험 범위다(미검증).
시계: 세션(auth_sessions.now)과 시도·점유(Lua — social_auth.clock_ms)를 같은 가짜 시계로 맞춘다. Redis TIME 경로는 따로 한 번 본다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import datetime
import random
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from tests.exec_db import bound_app_db, fresh_database, requires_db

REDIS_URL = os.getenv("PIXLATE_TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(not REDIS_URL, reason="PIXLATE_TEST_REDIS_URL 없음")
pytestmark = [requires_db, requires_redis]

ORIGIN = "https://fe.pixlate.test"
HDR = {"X-Requested-With": "XMLHttpRequest", "Origin": ORIGIN}
KAKAO_APP_ID = 990001  # 시험 값 — 실제 앱 ID 아님
SECRETS = {"PIXLATE_OAUTH_KAKAO_CLIENT_SECRET": "test-kakao-secret-xyz", "PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET": "test-google-secret-xyz"}


# ───────────────────────────── 시험용 서명 키(cryptography) ─────────────────────────────

def _self_signed(priv) -> str:
    """대역 인증서 — Google v1 certs 형식(kid → X.509 PEM)과 같은 자체 서명 인증서."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pixlate-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(priv.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2)).sign(priv, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class RsaKey:
    """시험용 RSA 키 — 서명은 cryptography(RSASSA-PKCS1-v1_5 + SHA-256). 서버 검증은 google-auth 가 한다(검증 코드와 다른 구현)."""

    def __init__(self, name: str, bits: int = 2048):
        self.kid = f"test-kid-{name}"
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=bits)
        self.pem = _self_signed(self.priv)

    def sign(self, msg: bytes) -> bytes:
        return self.priv.sign(msg, padding.PKCS1v15(), hashes.SHA256())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture(scope="module")
def rsa_key():
    return RsaKey("main")


@pytest.fixture(scope="module")
def other_key():
    return RsaKey("other")


def _jwt(key: RsaKey, claims: dict, kid: str | None = None, alg: str = "RS256") -> str:
    h = _b64(json.dumps({"alg": alg, "kid": kid or key.kid, "typ": "JWT"}).encode())
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
        self.cert_keys: list[RsaKey] = [key]  # Google 인증서 목록(kid → PEM)
        self.cert_calls = 0

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
        if host == "www.googleapis.com" and path == "/oauth2/v1/certs":  # google-auth verify_oauth2_token 이 받는 형식
            self.cert_calls += 1
            r = self._run_hook("google_certs")
            if r is not None:
                return r
            return httpx.Response(200, json={k.kid: k.pem for k in self.cert_keys}, headers={"cache-control": "public, max-age=3600"})
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
                  "exp": int(time.time()) + 3600, "iat": int(time.time()) - 5, "nonce": c["nonce"], **prof.get("claims", {})}
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


GOOGLE_CLIENT = "test-google-client.apps.example"
_DROP = object()


def _google_claims(sub: str, nonce: str, /, **over) -> dict:
    """정상 클레임(실제 시각 기준 — google-auth 는 시스템 시계로 iat·exp 를 본다). over 로 하나만 바꾸거나(_DROP = 뺌) 한다."""
    now = int(time.time())
    c = {"iss": "https://accounts.google.com", "aud": GOOGLE_CLIENT, "sub": sub, "iat": now - 5, "exp": now + 3600, "nonce": nonce}
    for k, v in over.items():
        if v is _DROP:
            c.pop(k)
        else:
            c[k] = v
    return c


# 사례마다 하나만 틀리게 만든다 — 같은 시험의 정상 대조(valid)가 통과하므로, 거절 원인은 그 하나다.
def _google_token(case: str, key: RsaKey, other: RsaKey, sub: str, nonce: str) -> str:
    now = int(time.time())
    c = lambda **o: _google_claims(sub, nonce, **o)
    if case == "valid":
        return _jwt(key, c())
    if case == "skew_within_60s":  # 만료 30초 지남 · 발급 시각 30초 미래 — 시계 차이 60초 안이라 받는다
        return _jwt(key, c(exp=now - 30, iat=now + 30))
    if case == "signature_other_key":  # 다른 키로 서명하고 kid 만 맞춤
        return _jwt(other, c(), kid=key.kid)
    if case == "signature_bitflip":
        h, p, s = _jwt(key, c()).split(".")
        sig = bytearray(_b64d(s))
        sig[-1] ^= 1
        return f"{h}.{p}.{_b64(bytes(sig))}"
    if case == "payload_swapped":  # 서명은 원래 클레임, 본문은 다른 사용자
        h, _, s = _jwt(key, c()).split(".")
        return f"{h}.{_b64(json.dumps(c(sub='someone-else')).encode())}.{s}"
    if case == "kid_unknown":
        return _jwt(key, c(), kid="unknown-kid")
    if case == "kid_missing":
        h = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        p = _b64(json.dumps(c()).encode())
        return f"{h}.{p}.{_b64(key.sign(f'{h}.{p}'.encode()))}"
    if case == "alg_none":
        return f"{_b64(json.dumps({'alg': 'none', 'kid': key.kid}).encode())}.{_b64(json.dumps(c()).encode())}."
    if case == "alg_es256":
        return _jwt(key, c(), alg="ES256")
    if case == "alg_hs256":
        return _jwt(key, c(), alg="HS256")
    if case == "not_a_jwt":
        return "not-a-jwt"
    over = {
        "aud_other": {"aud": "other-client"}, "aud_list": {"aud": [GOOGLE_CLIENT, "other-client"]}, "aud_missing": {"aud": _DROP},
        "iss_other": {"iss": "https://evil.example"}, "iss_missing": {"iss": _DROP},
        "exp_past": {"exp": now - 120}, "exp_missing": {"exp": _DROP}, "exp_not_number": {"exp": "soon"},
        "iat_future": {"iat": now + 300}, "iat_missing": {"iat": _DROP},
        "nonce_other": {"nonce": "other-nonce"}, "nonce_missing": {"nonce": _DROP},
        "sub_missing": {"sub": _DROP}, "sub_blank": {"sub": "  "},
    }[case]
    return _jwt(key, c(**over))


GOOGLE_REJECT = ["signature_other_key", "signature_bitflip", "payload_swapped", "kid_unknown", "kid_missing", "alg_none", "alg_es256",
                 "alg_hs256", "not_a_jwt", "aud_other", "aud_list", "aud_missing", "iss_other", "iss_missing", "exp_past", "exp_missing",
                 "exp_not_number", "iat_future", "iat_missing", "nonce_other", "nonce_missing", "sub_missing", "sub_blank"]


def _google_attempt(client, prov):
    b = Browser(client, prov)
    url = b.start("google").json()["authorizeUrl"]
    sub = f"g-{uuid.uuid4().hex[:12]}"
    code, state = prov.issue_code("google", url, _google(sub))
    return b, sub, code, state, prov.codes[code]["nonce"]


@pytest.mark.parametrize("case", ["valid", "skew_within_60s"])
def test_google_id_token_accepted_cases(client, prov, rsa_key, other_key, case):
    b, sub, code, state, nonce = _google_attempt(client, prov)
    tok = _google_token(case, rsa_key, other_key, sub, nonce)
    prov.hook["google_token"] = lambda: httpx.Response(200, json={"access_token": "gat", "id_token": tok})
    r = b.complete("google", code, state)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("case", GOOGLE_REJECT)
def test_google_bad_id_token_is_provider_auth_failed_and_closes_attempt(client, prov, rds, rsa_key, other_key, case):
    b, sub, code, state, nonce = _google_attempt(client, prov)
    tok = _google_token(case, rsa_key, other_key, sub, nonce)
    prov.hook["google_token"] = lambda: httpx.Response(200, json={"access_token": "gat", "id_token": tok})
    before = _sessions(rds)
    r = b.complete("google", code, state)
    assert r.status_code == 401 and _reason(r) == "provider_auth_failed" and _no_cookies(r), (case, r.text)
    assert _err(r)["retryable"] is False and _sessions(rds) == before
    if case == "kid_unknown":
        assert prov.cert_calls == 1  # 방금 받은 인증서 목록에 없는 kid — 다시 받지 않고 토큰 거절
    prov.hook.clear()
    again = b.complete("google", code, state)
    assert again.status_code == 401 and _reason(again) == "state_invalid"  # 실패로 닫힌 시도는 다시 받지 않는다


@pytest.mark.parametrize("case", ["signature_other_key", "signature_bitflip", "payload_swapped", "aud_other", "aud_missing",
                                  "iss_other", "iss_missing", "exp_past", "exp_missing", "iat_future", "iat_missing", "kid_unknown"])
def test_installed_google_auth_itself_rejects(env, prov, rsa_key, other_key, case):
    """우리 쪽 이중 검사와 별개로, 설치된 google-auth(constraints 고정 버전)의 verify_oauth2_token 이 직접 거절하는지 본다
    (서명 · audience · issuer · iat · exp). 같은 호출에서 정상 토큰은 통과한다."""
    from google.auth import exceptions as gexc
    from google.oauth2 import id_token as gid

    from app import social_auth

    cfg = social_auth.config("google")
    verify = lambda t: gid.verify_oauth2_token(t, social_auth._CertsRequest(cfg, rsa_key.kid), audience=cfg.client_id,
                                               clock_skew_in_seconds=60)
    assert verify(_google_token("valid", rsa_key, other_key, "g-lib", "n-lib"))["sub"] == "g-lib"
    with pytest.raises((ValueError, TypeError, KeyError, gexc.GoogleAuthError)) as ei:  # iss 누락은 라이브러리가 KeyError
        verify(_google_token(case, rsa_key, other_key, "g-lib", "n-lib"))
    assert not isinstance(ei.value, (social_auth.ProviderUnavailable, gexc.TransportError))


def test_google_auth_version_and_constants_match_constraints():
    """설치 결과가 constraints.txt(운영 이미지 목록)와 같고, 라이브러리의 인증서 주소·issuer 가 서버 상수와 같다(버전이 바뀌면 여기서 걸린다)."""
    import importlib.metadata as md
    import pathlib
    import re

    from google.oauth2 import id_token as gid

    from app import social_auth

    pins = {}
    for line in (pathlib.Path(__file__).resolve().parents[1] / "constraints.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)$", line.strip())
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    for pkg in ("google-auth", "cryptography", "pyasn1", "pyasn1-modules", "cffi"):
        assert md.version(pkg) == pins[pkg], pkg
    assert gid._GOOGLE_OAUTH2_CERTS_URL == social_auth.GOOGLE_CERTS
    assert set(gid._GOOGLE_ISSUERS) == set(social_auth.GOOGLE_ISSUERS)


def test_google_server_passes_audience_and_skew_to_library(client, prov, monkeypatch):
    """audience 는 반드시 넘긴다(None 이면 라이브러리가 aud 를 검사하지 않음). 우리 쪽 aud 재확인이 있어도 이 전달은 따로 본다."""
    from google.oauth2 import id_token as gid

    seen = []
    real = gid.verify_oauth2_token

    def spy(token, request, audience=None, clock_skew_in_seconds=0):
        seen.append((audience, clock_skew_in_seconds))
        return real(token, request, audience=audience, clock_skew_in_seconds=clock_skew_in_seconds)

    monkeypatch.setattr(gid, "verify_oauth2_token", spy)
    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    assert seen == [(GOOGLE_CLIENT, 60)]


# ── 인증서 캐시 · 키 교체 · 조회 장애 ──

def test_google_certs_cached_by_cache_control_and_refetched_after_expiry(client, prov):
    from app import social_auth

    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    assert prov.cert_calls == 1 and 3500 < social_auth._CERTS["until"] - time.time() <= 3600  # max-age=3600
    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    assert prov.cert_calls == 1  # 캐시
    social_auth._CERTS["until"] = time.time() - 1  # 만료
    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    assert prov.cert_calls == 2


def test_google_key_rotation_refetches_once_then_uses_cache(client, prov, rsa_key, other_key):
    from app import social_auth

    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    prov.cert_keys, prov.key = [rsa_key, other_key], other_key  # Google 이 새 키로 서명하기 시작
    social_auth._CERTS["fetched"] -= 31  # 마지막 조회에서 다시 받기 최소 간격(30초)이 지남
    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    assert prov.cert_calls == 2  # 모르는 kid → 한 번 다시 받음
    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    assert prov.cert_calls == 2  # 새 목록은 캐시


def test_google_unknown_kid_within_refresh_interval_is_not_refetched(client, prov, rsa_key, other_key):
    """마지막 조회 30초 안에 모르는 kid — 다시 받지 않는다(조회 폭주 방지). 방금 확인한 목록에 없는 키라 토큰 거절이다."""
    assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
    prov.cert_keys, prov.key = [rsa_key, other_key], other_key
    b, sub, code, state, nonce = _google_attempt(client, prov)
    r = b.complete("google", code, state)
    assert r.status_code == 401 and _reason(r) == "provider_auth_failed" and prov.cert_calls == 1


CERT_FAILURES = ["status_500", "status_503", "status_429", "status_404", "status_403", "timeout", "network", "not_json",
                 "json_list", "empty", "bad_pem", "small_key", "ec_key"]


def _cert_failure(kind: str, kid: str):
    if kind == "timeout":
        raise httpx.ReadTimeout("t")
    if kind == "network":
        raise httpx.ConnectError("c")
    if kind.startswith("status_"):
        return httpx.Response(int(kind[7:]), json={"error": "x"})
    if kind == "not_json":
        return httpx.Response(200, text="<html>")
    if kind == "json_list":
        return httpx.Response(200, json=[1, 2])
    if kind == "empty":
        return httpx.Response(200, json={})
    if kind == "bad_pem":
        return httpx.Response(200, json={kid: "-----BEGIN CERTIFICATE-----\nnot a cert\n-----END CERTIFICATE-----\n"})
    if kind == "small_key":
        return httpx.Response(200, json={kid: _self_signed(rsa.generate_private_key(public_exponent=65537, key_size=1024))})
    if kind == "ec_key":
        return httpx.Response(200, json={kid: _self_signed(ec.generate_private_key(ec.SECP256R1()))})
    raise AssertionError(kind)


@pytest.mark.parametrize("cached", [False, True], ids=["cold", "rotation"])
@pytest.mark.parametrize("kind", CERT_FAILURES)
def test_google_cert_fetch_failure_is_provider_unavailable_not_token_rejection(client, prov, rds, rsa_key, other_key, kind, cached):
    """인증서 조회 장애는 사용자 토큰 거절(401 provider_auth_failed)로 바꾸지 않는다 → 503 provider_unavailable(재시도 가능).
    cold = 캐시 없음, rotation = 캐시는 있고 모르는 kid 라 다시 받다가 실패."""
    from app import social_auth

    if cached:
        assert Browser(client, prov).login("google", _google(f"g-{uuid.uuid4().hex[:12]}")).status_code == 200
        prov.key = other_key
        social_auth._CERTS["fetched"] -= 31
    prov.hook["google_certs"] = lambda: _cert_failure(kind, prov.key.kid)
    b, sub, code, state, nonce = _google_attempt(client, prov)
    before = _sessions(rds)
    r = b.complete("google", code, state)
    assert r.status_code == 503 and _reason(r) == "provider_unavailable" and _no_cookies(r), (kind, r.text)
    assert _err(r)["retryable"] is True and _sessions(rds) == before


def test_google_unexpected_library_request_is_config_error(client, prov, monkeypatch):
    """라이브러리가 다른 주소를 요청하면(버전 변경 등) 토큰 거절이 아니라 서버 설정 문제로 본다 → 503(사유 없음)."""
    from app import social_auth

    monkeypatch.setattr(social_auth, "GOOGLE_CERTS", "https://www.googleapis.com/oauth2/v9/certs")
    b, sub, code, state, nonce = _google_attempt(client, prov)
    r = b.complete("google", code, state)
    assert r.status_code == 503 and _err(r)["details"] is None and prov.cert_calls == 0


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


# ═════════════════════════════ 독립 근거 — OpenSSL 과 교차 검증 ═════════════════════════════

def _openssl():
    import shutil

    return shutil.which("openssl")


@pytest.mark.skipif(_openssl() is None, reason="openssl CLI 없음")
def test_openssl_signed_id_token_passes_server_verification_both_directions(env, prov, tmp_path):
    """시험 서명기와 서버 검증기가 같은 편끼리만 맞는지 보지 않는다 — 키·인증서·서명을 OpenSSL 이 만든다.
    ① OpenSSL 키·자체 서명 인증서·RS256 서명 → 서버 검증(google-auth) 통과, 서명 1비트·본문 변조는 거절
    ② 시험 서명기(cryptography)의 서명 → OpenSSL 이 인증서로 검증 통과(시험 서명기도 표준과 같다는 근거).
    실제 Google ID 토큰 통과는 이 시험이 대신하지 못한다(실제 제공자 시험 범위)."""
    import subprocess

    from app import social_auth

    ossl = _openssl()
    key, crt, msg, sig = (tmp_path / n for n in ("k.pem", "c.pem", "m.bin", "s.bin"))
    run = lambda *a: subprocess.run([ossl, *a], check=True, capture_output=True, text=True)
    run("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key))
    run("req", "-new", "-x509", "-key", str(key), "-subj", "/CN=pixlate-openssl-test", "-days", "2", "-out", str(crt))
    kid = "openssl-kid"
    prov.hook["google_certs"] = lambda: httpx.Response(200, json={kid: crt.read_text()}, headers={"cache-control": "max-age=60"})
    cfg = social_auth.config("google")
    h = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    p = _b64(json.dumps(_google_claims("g-openssl", "n-openssl")).encode())
    msg.write_bytes(f"{h}.{p}".encode())
    run("dgst", "-sha256", "-sign", str(key), "-out", str(sig), str(msg))
    s = sig.read_bytes()
    assert social_auth.verify_google_id_token(cfg, f"{h}.{p}.{_b64(s)}", "n-openssl")["sub"] == "g-openssl"  # ①
    flipped = s[:-1] + bytes([s[-1] ^ 1])
    p2 = _b64(json.dumps(_google_claims("g-other", "n-openssl")).encode())
    for bad in (f"{h}.{p}.{_b64(flipped)}", f"{h}.{p2}.{_b64(s)}"):
        with pytest.raises(social_auth.AuthFailed):
            social_auth.verify_google_id_token(cfg, bad, "n-openssl")
    # ② cryptography 로 같은 OpenSSL 키를 읽어 서명 → OpenSSL 이 인증서 공개 키로 검증
    priv = serialization.load_pem_private_key(key.read_bytes(), password=None)
    sig.write_bytes(priv.sign(msg.read_bytes(), padding.PKCS1v15(), hashes.SHA256()))
    pub = tmp_path / "p.pem"
    run("x509", "-in", str(crt), "-pubkey", "-noout", "-out", str(pub))
    out = subprocess.run([ossl, "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig), str(msg)], capture_output=True, text=True)
    assert out.returncode == 0 and "Verified OK" in out.stdout, out.stdout + out.stderr


# ═════════════════════════════ reload B안 — 응답 유실 뒤 같은 결속으로 새 로그인 ═════════════════════════════

@pytest.mark.parametrize("had_refresh", [False, True])
def test_lost_success_then_new_start_in_same_browser_leaves_only_the_new_session(client, prov, rds, had_refresh):
    """서버 로그인 성공 → 응답·새 쿠키 유실 → (같은 완료 재전송·refresh 로 성공 추측은 하지 않음) → 같은 결속으로 새 start → 새 로그인 성공.
    응답을 잃은 세션과 그 access 는 폐기되고, 새 세션만 유효하다. 브라우저가 이전 로그인의 refresh 를 들고 있던 경우도 같다.
    실제 document reload·G1 자동 복원 방지·Next 프록시 쿠키 전달은 브라우저 연동 시험 범위다(여기서는 서버 상태만 본다)."""
    b = Browser(client, prov)
    earlier = b.login("kakao", _kakao(_uid())) if had_refresh else None
    before = _sessions(rds)
    url = b.start().json()["authorizeUrl"]
    bind_before = b.bind
    code, state = prov.issue_code("kakao", url, _kakao(_uid()))
    lost = b.complete("kakao", code, state, absorb=False)  # ① 서버는 성공 — 응답·Set-Cookie 를 브라우저가 받지 못함
    assert lost.status_code == 200
    lost_access, lost_refresh = lost.json()["accessToken"], _value(_cookies(lost)["pxl_refresh"])
    assert b.bind == bind_before  # 결속 쿠키는 그대로 남았다(삭제 응답도 잃음)
    # ② 계약: 같은 완료 요청을 다시 보내 성공을 추측하지 않는다 — 다시 보내도 받지 않는다
    again = b.complete("kakao", code, state, absorb=False)
    assert again.status_code == 401 and _reason(again) == "state_invalid" and _no_cookies(again)
    # ③ 같은 결속으로 새 start → 새 로그인
    url2 = b.start().json()["authorizeUrl"]
    assert b.bind == bind_before  # 같은 결속을 다시 쓴다
    code2, state2 = prov.issue_code("kakao", url2, _kakao(_uid()))
    fresh = b.complete("kakao", code2, state2)
    assert fresh.status_code == 200 and b.bind is None
    # ④ 응답을 잃은 세션·access 폐기, 이전 로그인 세션도 폐기, 새 세션만 유효
    assert _me(client, lost_access).status_code == 401
    assert _reason(_post(client, "/v1/auth/refresh", {"pxl_refresh": lost_refresh})) == "refresh_invalid"
    if earlier is not None:
        assert _me(client, earlier.json()["accessToken"]).status_code == 401
    assert _me(client, fresh.json()["accessToken"]).status_code == 200
    assert len(_sessions(rds) - before) == 1
    r = _post(client, "/v1/auth/refresh", {"pxl_refresh": b.refresh})
    assert r.status_code == 200
