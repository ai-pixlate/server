"""가짜 카카오·Google — 브라우저가 보는 로그인 화면(127.0.0.1 전용 웹 앱) + 서버가 부르는 제공자 HTTP(httpx 대역).

- 브라우저: BE 가 준 authorizeUrl 로 이 화면에 오면 시험 계정을 고른다 → FE 콜백(`redirect_uri?code&state`)으로 302.
  「취소」는 `error=access_denied&state` 로 돌려보낸다.
- 서버(BE 프로세스 안): 토큰 교환·access_token_info·user/me·Google 토큰·v1 인증서를 실제 주소 그대로 받지만, httpx 대역이
  같은 프로세스 안에서 답한다(네트워크로 나가지 않음). 대역에 없는 주소는 실패시킨다.
- 실제 제공자처럼 확인한다: 코드 일회성, redirect_uri·client_id 일치, PKCE S256, client_secret.
- 시험 조작(127.0.0.1 전용): `POST /_control` 로 호출 지연·실패 상태·세션 저장소 장애를 켜고 끈다. `GET /_control` = 현재 조작·호출 기록.
값은 모두 시험 값이다(실제 키·토큰 없음). 로그·기록에 코드·토큰 값을 남기지 않는다.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import html
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

log = logging.getLogger("pixlate.local_oauth")

# 시험 계정(실제 계정 아님). 이메일 도메인은 예약 도메인 .test
KAKAO_ACCOUNTS = {
    "kakao-a": {"label": "카카오 시험 A", "id": 9100001, "nickname": "카카오A", "email": "kakao-a@local.test", "valid": True, "verified": True},
    "kakao-b": {"label": "카카오 시험 B", "id": 9100002, "nickname": "카카오B", "email": "kakao-b@local.test", "valid": True, "verified": True},
    "kakao-noemail": {"label": "카카오 시험 C (이메일 동의 안 함)", "id": 9100003, "nickname": "카카오C", "email": None, "needs_agreement": True},
}
GOOGLE_ACCOUNTS = {
    "google-a": {"label": "Google 시험 A", "sub": "local-google-a", "name": "구글 A", "email": "google-a@local.test", "email_verified": True},
    "google-b": {"label": "Google 시험 B", "sub": "local-google-b", "name": "구글 B", "email": "google-b@local.test", "email_verified": True},
    "google-unverified": {"label": "Google 시험 C (이메일 미확인)", "sub": "local-google-c", "name": "구글 C", "email": "google-c@local.test", "email_verified": False},
}
# 조작 가능한 서버 측 호출 이름
CALLS = ("kakao_token", "kakao_token_info", "kakao_me", "google_token", "google_certs")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _self_signed(priv) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pixlate-local-fake-google")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(priv.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=7)).sign(priv, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@dataclass
class Settings:
    """BE 와 같은 환경 값(시험 값)으로 만든다 — 대역이 실제 제공자처럼 client_id·secret·앱 ID 를 대조한다."""
    kakao_client_id: str
    kakao_secret: str | None
    kakao_app_id: int
    google_client_id: str
    google_secret: str


@dataclass
class FakeProviders:
    settings: Settings
    codes: dict = field(default_factory=dict)          # code → 시도 정보(사용 여부 포함)
    kakao_tokens: dict = field(default_factory=dict)   # access token → 계정
    delays: dict = field(default_factory=dict)         # 호출 이름 → 지연 초
    fail: dict = field(default_factory=dict)           # 호출 이름 → HTTP 상태(5xx 등)
    store_down: bool = False                           # BE 세션 저장소 장애 흉내(serve 가 연결)
    log_: list = field(default_factory=list)           # (시각, 호출 이름, 결과) — 값 없음
    lock: threading.RLock = field(default_factory=threading.RLock)  # 재진입 — 잠근 채 기록(_note)을 부른다

    def __post_init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"local-fake-{uuid.uuid4().hex[:8]}"
        self.pem = _self_signed(self.key)

    # ── 기록 ──
    def _note(self, call: str, result: str) -> None:
        with self.lock:
            self.log_.append({"t": round(time.time(), 3), "call": call, "result": result})
            del self.log_[:-200]

    def control_state(self) -> dict:
        with self.lock:
            return {"delays": dict(self.delays), "fail": dict(self.fail), "store_down": self.store_down,
                    "codes_issued": len(self.codes), "calls": list(self.log_[-50:])}

    # ── 브라우저 쪽: 코드 발급 ──
    def issue_code(self, provider: str, query: dict, account_key: str) -> str:
        accounts = KAKAO_ACCOUNTS if provider == "kakao" else GOOGLE_ACCOUNTS
        if account_key not in accounts:
            raise ValueError("unknown account")
        code = f"local-fake-code-{uuid.uuid4().hex}"
        with self.lock:
            self.codes[code] = {"provider": provider, "account": account_key, "redirect_uri": query.get("redirect_uri"),
                                "client_id": query.get("client_id"), "challenge": query.get("code_challenge"),
                                "method": query.get("code_challenge_method"), "nonce": query.get("nonce"), "used": False}
        self._note(f"{provider}_authorize", "code_issued")
        return code

    # ── 서버 쪽: httpx 대역 ──
    def _pre(self, call: str):
        d = self.delays.get(call)
        if d:
            time.sleep(float(d))
        st = self.fail.get(call)
        if st:
            self._note(call, f"forced_{st}")
            return httpx.Response(int(st), json={"error": "local_fake_forced", "error_code": "LOCAL"})
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "kauth.kakao.com" and path == "/oauth/token":
            return self._pre("kakao_token") or self._token("kakao", request)
        if host == "oauth2.googleapis.com" and path == "/token":
            return self._pre("google_token") or self._token("google", request)
        if host == "kapi.kakao.com" and path == "/v1/user/access_token_info":
            r = self._pre("kakao_token_info")
            if r:
                return r
            acct = self.kakao_tokens.get(request.headers.get("authorization", "")[7:])
            if acct is None:
                self._note("kakao_token_info", "401")
                return httpx.Response(401, json={"msg": "this access token does not exist", "code": -401})
            self._note("kakao_token_info", "200")
            return httpx.Response(200, json={"id": acct["id"], "app_id": self.settings.kakao_app_id, "expires_in": 21599})
        if host == "kapi.kakao.com" and path == "/v2/user/me":
            r = self._pre("kakao_me")
            if r:
                return r
            acct = self.kakao_tokens.get(request.headers.get("authorization", "")[7:])
            if acct is None:
                return httpx.Response(401, json={"msg": "this access token does not exist", "code": -401})
            ka = {"profile": {"nickname": acct["nickname"]}}
            if acct.get("needs_agreement"):
                ka["email_needs_agreement"] = True
            else:
                ka.update(email=acct["email"], is_email_valid=acct["valid"], is_email_verified=acct["verified"], email_needs_agreement=False)
            self._note("kakao_me", "200")
            return httpx.Response(200, json={"id": acct["id"], "kakao_account": ka})
        if host == "www.googleapis.com" and path == "/oauth2/v1/certs":
            r = self._pre("google_certs")
            if r:
                return r
            self._note("google_certs", "200")
            return httpx.Response(200, json={self.kid: self.pem}, headers={"cache-control": "public, max-age=3600"})
        self._note("blocked", host)
        raise RuntimeError(f"로컬 가짜 제공자: 대역에 없는 외부 호출 차단 — {request.method} {host}{path}")

    def _token(self, provider: str, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

        def bad(code, status=400, err="invalid_grant"):
            self._note(f"{provider}_token", f"{status}_{code}")
            return httpx.Response(status, json={"error": err, "error_description": "local fake", "error_code": code})
        with self.lock:
            c = self.codes.get(form.get("code", ""))
            if c is None or c["used"] or c["provider"] != provider:
                return bad("KOE320")
            c["used"] = True  # 일회용 — 실패한 교환도 코드를 소비한다
        cid = self.settings.kakao_client_id if provider == "kakao" else self.settings.google_client_id
        if form.get("grant_type") != "authorization_code" or form.get("redirect_uri") != c["redirect_uri"] \
                or form.get("client_id") != c["client_id"] or form.get("client_id") != cid:
            return bad("KOE303")
        if c["method"] != "S256" or _b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != c["challenge"]:
            return bad("KOE320")
        secret = self.settings.kakao_secret if provider == "kakao" else self.settings.google_secret
        if secret and form.get("client_secret") != secret:
            return bad("KOE010", 401, "invalid_client")
        if provider == "kakao":
            acct = KAKAO_ACCOUNTS[c["account"]]
            at = f"local-fake-kat-{uuid.uuid4().hex}"
            with self.lock:
                self.kakao_tokens[at] = acct
            self._note("kakao_token", "200")
            return httpx.Response(200, json={"access_token": at, "token_type": "bearer", "expires_in": 21599})
        acct = GOOGLE_ACCOUNTS[c["account"]]
        now = int(time.time())
        claims = {"iss": "https://accounts.google.com", "aud": cid, "sub": acct["sub"], "iat": now - 2, "exp": now + 3600,
                  "nonce": c["nonce"], "email": acct["email"], "email_verified": acct["email_verified"], "name": acct["name"]}
        h = _b64(json.dumps({"alg": "RS256", "kid": self.kid, "typ": "JWT"}).encode())
        p = _b64(json.dumps(claims).encode())
        sig = self.key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        self._note("google_token", "200")
        return httpx.Response(200, json={"access_token": "local-fake-gat", "id_token": f"{h}.{p}.{_b64(sig)}", "expires_in": 3599})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# ───────────────────────────── 브라우저가 보는 가짜 로그인 화면 ─────────────────────────────

_PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>로컬 가짜 {name} 로그인</title>
<style>body{{font-family:sans-serif;max-width:520px;margin:40px auto;padding:0 16px}}
.warn{{background:#fff3cd;border:1px solid #e0c36b;padding:8px 12px;border-radius:6px}}
button{{display:block;width:100%;margin:8px 0;padding:12px;font-size:15px;cursor:pointer}}
.cancel{{background:#eee}}</style></head><body>
<p class="warn">로컬 시험 전용 가짜 {name} — 실제 {name} 이 아닙니다. 시험 계정만 고를 수 있습니다.</p>
<h2>{name} 계정 선택</h2>
<form method="post">{hidden}{buttons}
<button class="cancel" name="account" value="__deny__">취소 (access_denied)</button></form></body></html>"""


def make_page_app(fake: FakeProviders) -> FastAPI:
    app = FastAPI(title="local fake oauth", docs_url=None, redoc_url=None, openapi_url=None)
    names = {"kakao": "카카오", "google": "Google"}

    def _check(provider: str, q: dict):
        cid = fake.settings.kakao_client_id if provider == "kakao" else fake.settings.google_client_id
        if q.get("client_id") != cid or q.get("response_type") != "code" or not q.get("state") or not q.get("redirect_uri"):
            return "client_id·response_type·state·redirect_uri 가 BE 설정과 맞지 않습니다"
        if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
            return "PKCE(S256) 없음"
        if provider == "google" and not q.get("nonce"):
            return "Google nonce 없음"
        return None

    @app.get("/{provider}/authorize", response_class=HTMLResponse)
    def authorize_page(provider: str, request: Request):
        if provider not in names:
            return HTMLResponse("unknown provider", status_code=404)
        q = dict(request.query_params)
        err = _check(provider, q)
        if err:
            return HTMLResponse(f"<p>잘못된 인가 요청: {html.escape(err)}</p>", status_code=400)
        accounts = KAKAO_ACCOUNTS if provider == "kakao" else GOOGLE_ACCOUNTS
        hidden = "".join(f'<input type="hidden" name="q_{html.escape(k)}" value="{html.escape(v)}">' for k, v in q.items())
        buttons = "".join(f'<button name="account" value="{html.escape(k)}">{html.escape(a["label"])}</button>' for k, a in accounts.items())
        return _PAGE.format(name=names[provider], hidden=hidden, buttons=buttons)

    @app.post("/{provider}/authorize")
    async def authorize_choose(provider: str, request: Request):
        form = await request.form()
        q = {k[2:]: str(v) for k, v in form.items() if k.startswith("q_")}
        err = _check(provider, q) if provider in names else "unknown provider"
        if err:
            return HTMLResponse(f"<p>잘못된 인가 요청: {html.escape(err)}</p>", status_code=400)
        account = str(form.get("account", ""))
        if account == "__deny__":
            fake._note(f"{provider}_authorize", "access_denied")
            return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'error': 'access_denied', 'state': q['state']})}", status_code=302)
        try:
            code = fake.issue_code(provider, q, account)
        except ValueError:
            return HTMLResponse("<p>알 수 없는 시험 계정</p>", status_code=400)
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", status_code=302)

    @app.get("/_control")
    def control_get():
        return fake.control_state()

    @app.post("/_control")
    async def control_set(request: Request):
        """{"delays": {"kakao_token_info": 5}, "fail": {"google_certs": 503}, "store_down": true, "reset": true}"""
        body = await request.json()
        with fake.lock:
            if body.get("reset"):
                fake.delays.clear()
                fake.fail.clear()
                fake.store_down = False
            for k, v in (body.get("delays") or {}).items():
                if k in CALLS:
                    if v:
                        fake.delays[k] = min(float(v), 120.0)
                    else:
                        fake.delays.pop(k, None)
            for k, v in (body.get("fail") or {}).items():
                if k in CALLS:
                    if v:
                        fake.fail[k] = int(v)
                    else:
                        fake.fail.pop(k, None)
            if "store_down" in body:
                fake.store_down = bool(body["store_down"])
        return JSONResponse(fake.control_state())

    return app
