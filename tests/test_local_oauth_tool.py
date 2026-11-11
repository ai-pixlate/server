"""로컬 브라우저 시험 전용 진입점(tools/local_oauth) 자체 시험 — 시작 조건과 가짜 제공자 대역. DB·Redis 없이 돈다.

이 시험은 도구를 검증할 뿐, 브라우저 연동 검증을 대신하지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("tools.local_oauth", reason="운영 이미지에는 시험 전용 진입점이 없다(.dockerignore) — 저장소 체크아웃에서만 돈다")

from tools.local_oauth import serve  # noqa: E402
from tools.local_oauth.fake import FakeProviders, Settings, make_page_app  # noqa: E402

GOOD = {
    "PIXLATE_OAUTH_PROVIDERS": "kakao,google", "PIXLATE_OAUTH_REDIRECT_BASE": "http://localhost:3000",
    "PIXLATE_AUTH_ORIGINS": "http://localhost:3000", "PIXLATE_OAUTH_KAKAO_CLIENT_ID": "local-fake-kakao",
    "PIXLATE_OAUTH_KAKAO_APP_ID": "990001", "PIXLATE_OAUTH_KAKAO_CLIENT_SECRET": "local-fake-ks",
    "PIXLATE_OAUTH_GOOGLE_CLIENT_ID": "local-fake-google", "PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET": "local-fake-gs",
    "JWT_SECRET": "local-fake-jwt", "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:55451/pixlate_local_oauth",
    "PIXLATE_AUTH_REDIS_URL": "redis://127.0.0.1:56451/9", "CELERY_BROKER_URL": "redis://127.0.0.1:56451/10",
    "CELERY_RESULT_BACKEND": "redis://localhost:56451/11",
}


def test_good_environment_passes():
    assert serve.check_environment(GOOD, True) == []


@pytest.mark.parametrize("change,needle", [
    ({}, "명시적 시험 실행"),  # 인자 없음(아래에서 flag=False)
    ({"PIXLATE_OAUTH_KAKAO_CLIENT_ID": "abcd1234realkey"}, "KAKAO_CLIENT_ID"),
    ({"PIXLATE_OAUTH_GOOGLE_CLIENT_ID": "123456789012-x.apps.googleusercontent.com"}, "GOOGLE_CLIENT_ID"),
    ({"PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET": ""}, "GOOGLE_CLIENT_SECRET"),
    ({"PIXLATE_OAUTH_KAKAO_CLIENT_SECRET": "real-secret"}, "KAKAO_CLIENT_SECRET"),
    ({"JWT_SECRET": "pixlate_dev_jwt_secret_change_me"}, "JWT_SECRET"),
    ({"DATABASE_URL": "postgresql+psycopg://u:p@pixlate-db.example.rds.amazonaws.com:5432/pixlate_local_oauth"}, "DATABASE_URL"),
    ({"DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:55451/pixlate"}, "DB 이름"),
    ({"PIXLATE_AUTH_REDIS_URL": ""}, "PIXLATE_AUTH_REDIS_URL"),
    ({"CELERY_BROKER_URL": "redis://pixlate-redis:6379/0"}, "CELERY_BROKER_URL"),
    ({"PIXLATE_OAUTH_REDIRECT_BASE": "https://dev.pixlate.example"}, "REDIRECT_BASE"),
    ({"PIXLATE_AUTH_ORIGINS": "http://localhost:3000,https://pixlate.example"}, "PIXLATE_AUTH_ORIGINS"),
    ({"PIXLATE_AUTH_ORIGINS": "http://localhost:3001"}, "REDIRECT_BASE 가 PIXLATE_AUTH_ORIGINS"),
    ({"PIXLATE_OAUTH_PROVIDERS": ""}, "PIXLATE_OAUTH_PROVIDERS"),
])
def test_bad_environment_refuses(change, needle):
    env = {**GOOD, **change}
    problems = serve.check_environment(env, bool(change))
    assert any(needle in x for x in problems), problems
    secret_values = [v for v in change.values() if v and len(v) > 6]
    assert not any(v in " ".join(problems) for v in secret_values)  # 값은 메시지에 싣지 않는다


def test_main_refuses_without_flag_and_starts_nothing(monkeypatch, capsys):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    assert serve.main(["--port", "18765", "--fake-port", "18766"]) == 2
    assert "명시적 시험 실행" in capsys.readouterr().err


def test_entry_point_is_excluded_from_image_and_not_imported_by_app():
    root = pathlib.Path(__file__).resolve().parents[1]
    ignore = [l.strip() for l in (root / ".dockerignore").read_text(encoding="utf-8").splitlines()]
    assert "tools/local_oauth" in ignore
    for py in (root / "app").rglob("*.py"):
        assert "local_oauth" not in py.read_text(encoding="utf-8"), py


# ── 가짜 제공자 대역 ──

def _fake():
    return FakeProviders(Settings("local-fake-kakao", "local-fake-ks", 990001, "local-fake-google", "local-fake-gs"))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _authorize(page, provider, verifier="v" * 43, account=None, **extra):
    cid = "local-fake-kakao" if provider == "kakao" else "local-fake-google"
    q = {"response_type": "code", "client_id": cid, "redirect_uri": f"http://localhost:3000/login/callback/{provider}",
         "state": "st-1", "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256", **extra}
    if provider == "google":
        q.setdefault("nonce", "n-1")
    assert page.get(f"/{provider}/authorize?{urlencode(q)}").status_code == 200
    form = {f"q_{k}": v for k, v in q.items()}
    form["account"] = account or ("kakao-a" if provider == "kakao" else "google-a")
    r = page.post(f"/{provider}/authorize", data=form, follow_redirects=False)
    assert r.status_code == 302
    return dict(parse_qs(urlsplit(r.headers["location"]).query)), r.headers["location"]


def _exchange(fake, provider, code, verifier="v" * 43, secret=None):
    cid = "local-fake-kakao" if provider == "kakao" else "local-fake-google"
    url = "https://kauth.kakao.com/oauth/token" if provider == "kakao" else "https://oauth2.googleapis.com/token"
    data = {"grant_type": "authorization_code", "client_id": cid, "redirect_uri": f"http://localhost:3000/login/callback/{provider}",
            "code": code, "code_verifier": verifier,
            "client_secret": secret or ("local-fake-ks" if provider == "kakao" else "local-fake-gs")}
    with httpx.Client(transport=fake.transport()) as c:
        return c.post(url, data=data)


def test_page_issues_code_redirects_to_fe_callback_and_deny():
    fake = _fake()
    page = TestClient(make_page_app(fake))
    q, loc = _authorize(page, "kakao")
    assert loc.startswith("http://localhost:3000/login/callback/kakao?") and q["state"] == ["st-1"] and q["code"][0].startswith("local-fake-code-")
    q2, _ = _authorize(page, "kakao", account="__deny__")
    assert q2 == {"error": ["access_denied"], "state": ["st-1"]}


def test_page_rejects_request_that_does_not_match_be_settings():
    page = TestClient(make_page_app(_fake()))
    assert page.get("/kakao/authorize?" + urlencode({"response_type": "code", "client_id": "real-key", "state": "s",
                                                      "redirect_uri": "x", "code_challenge": "c", "code_challenge_method": "S256"})).status_code == 400


def test_token_exchange_checks_pkce_one_time_code_and_secret():
    fake = _fake()
    page = TestClient(make_page_app(fake))
    q, _ = _authorize(page, "kakao")
    assert _exchange(fake, "kakao", q["code"][0], verifier="w" * 43).status_code == 400   # PKCE 불일치(코드도 소비)
    assert _exchange(fake, "kakao", q["code"][0]).status_code == 400                     # 일회용
    q, _ = _authorize(page, "kakao")
    assert _exchange(fake, "kakao", q["code"][0], secret="local-fake-wrong").status_code == 401
    q, _ = _authorize(page, "kakao")
    r = _exchange(fake, "kakao", q["code"][0])
    assert r.status_code == 200
    with httpx.Client(transport=fake.transport()) as c:
        info = c.get("https://kapi.kakao.com/v1/user/access_token_info", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert info.json() == {"id": 9100001, "app_id": 990001, "expires_in": 21599}


def test_google_id_token_passes_the_server_verifier(monkeypatch):
    """가짜 Google 토큰이 서버의 실제 검증 경로(google-auth)를 통과한다 — 대역끼리의 호환일 뿐, 실제 Google 토큰 근거는 아니다."""
    from app import social_auth

    fake = _fake()
    page = TestClient(make_page_app(fake))
    q, _ = _authorize(page, "google", nonce="n-xyz")
    tok = _exchange(fake, "google", q["code"][0]).json()["id_token"]
    monkeypatch.setenv("PIXLATE_OAUTH_PROVIDERS", "google")
    for k, v in GOOD.items():
        if k.startswith("PIXLATE_OAUTH_"):
            monkeypatch.setenv(k, v)
    social_auth.set_transport(fake.transport())
    claims = social_auth.verify_google_id_token(social_auth.config("google"), tok, "n-xyz")
    assert claims["sub"] == "local-google-a" and claims["email_verified"] is True


def test_unknown_hosts_are_blocked_and_control_forces_failures():
    fake = _fake()
    with httpx.Client(transport=fake.transport()) as c:
        with pytest.raises(RuntimeError, match="외부 호출 차단"):
            c.get("https://example.com/")
        with pytest.raises(RuntimeError, match="외부 호출 차단"):
            c.get("https://kauth.kakao.com/oauth/authorize")  # 브라우저 주소는 서버가 부를 일이 없다
    page = TestClient(make_page_app(fake))
    st = page.post("/_control", json={"fail": {"google_certs": 503}, "delays": {"kakao_token": 0.01}, "store_down": True}).json()
    assert st["fail"] == {"google_certs": 503} and st["store_down"] is True
    with httpx.Client(transport=fake.transport()) as c:
        assert c.get("https://www.googleapis.com/oauth2/v1/certs").status_code == 503
    assert page.post("/_control", json={"reset": True}).json()["fail"] == {}
    assert "local-fake-code" not in json.dumps(page.get("/_control").json())  # 기록에 코드 값 없음
