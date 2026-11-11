"""0016 판매자 계정 식별(제공자 + 제공자 회원번호)과 간이 로그인의 local 제한 — 실제 PostgreSQL.

PRD v3.5.10 F-ACC-05: 계정은 제공자 + 제공자 회원번호로 구분하고, 같은 이메일이어도 제공자가 다르면 다른 계정이다.
소셜 계정을 만드는 API 는 아직 없으므로 소셜 행은 SQL 로 직접 넣는다.
"""
from __future__ import annotations

import uuid

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.exec_db import alembic_config, bound_app_db, fresh_database, requires_db

pytestmark = requires_db


# ---------------------------------------------------------------------------------------------------------
# head DB + 실제 API
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def client(dburl):
    from app.main import app

    return TestClient(app)


def _q(sql, **p):
    from app import db as app_db

    with app_db.engine.begin() as c:
        res = c.execute(text(sql), p)
        return [dict(r) for r in res.mappings().all()] if res.returns_rows else None


def _email() -> str:
    return f"id-{uuid.uuid4().hex[:10]}@example.com"


def _subject() -> str:
    return str(uuid.uuid4().int)[:12]


def _local(email: str) -> int:
    return _q("INSERT INTO seller (cognito_sub, email) VALUES (:s, :e) RETURNING id", s=f"local:{email}", e=email)[0]["id"]


def _social(provider: str, subject: str, email: str | None = None) -> int:
    return _q("INSERT INTO seller (auth_provider, provider_subject, email) VALUES (:p, :s, :e) RETURNING id",
              p=provider, s=subject, e=email)[0]["id"]


def _login(client, email: str) -> dict:
    r = client.post("/v1/auth/login", json={"email": email, "password": "x"})
    assert r.status_code == 200, r.text
    return r.json()


def _me(client, token: str):
    return client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})


def _session(seller_id: int, email: str | None) -> tuple[str, str]:
    """B(세션) 이후: 소셜 로그인 API 가 아직 없으므로 세션을 직접 만든다 → (access, refresh 쿠키 값)."""
    from app import auth_sessions
    from app.security import issue_access

    session, refresh_token, _ = auth_sessions.create(seller_id)
    return issue_access(session, email), refresh_token


def _refresh(client, refresh_token: str):
    return client.post("/v1/auth/refresh", headers={"X-Requested-With": "test", "Origin": "https://fe.pixlate.test", "Cookie": f"pxl_refresh={refresh_token}"})


def test_same_email_local_kakao_google_stay_separate(client):
    email = _email()
    google = _social("google", _subject(), email)  # 같은 이메일의 Google 계정이 local 보다 먼저 있어도
    kakao = _social("kakao", _subject())  # 카카오는 이메일을 주지 않는다
    local = _local(email)
    assert len({local, kakao, google}) == 3

    body = _login(client, email)
    assert body["seller"] == {"id": local, "email": email}  # 간이 로그인은 local 계정만 고른다
    assert _me(client, body["accessToken"]).json()["id"] == local
    rows = {r["id"]: r for r in _q("SELECT id, auth_provider, provider_subject IS NULL AS no_sub FROM seller WHERE id = ANY(:ids)",
                                   ids=[local, kakao, google])}
    assert [(rows[i]["auth_provider"], rows[i]["no_sub"]) for i in (local, kakao, google)] == [
        ("local", True), ("kakao", False), ("google", False)]


def test_local_login_never_selects_social_account(client):
    email = _email()
    google = _social("google", _subject(), email)  # 이 이메일의 local 계정은 없다
    before = _q("SELECT count(*) AS n FROM seller WHERE email = :e", e=email)[0]["n"]

    body = _login(client, email)
    assert body["seller"]["id"] != google  # 소셜 계정의 토큰을 주지 않는다
    created = _q("SELECT auth_provider, provider_subject, cognito_sub, email FROM seller WHERE id = :i", i=body["seller"]["id"])[0]
    assert created == {"auth_provider": "local", "provider_subject": None, "cognito_sub": f"local:{email}", "email": email}
    assert _q("SELECT count(*) AS n FROM seller WHERE email = :e", e=email)[0]["n"] == before + 1
    assert _login(client, email)["seller"]["id"] == body["seller"]["id"]  # 다시 들어와도 같은 local 계정(새로 만들지 않음)


def test_duplicate_local_emails_still_resolve_to_a_local_account(client):
    """같은 이메일 local 이 여럿일 때 어느 것을 고르는지는 이 변경 범위가 아니다(별도 추적). 소셜 계정으로 가지 않는 것만 본다."""
    email = _email()
    google = _social("google", _subject(), email)
    first, second = _local(email), _local(email)  # 이전 데이터에 같은 이메일 local 이 둘 있을 수 있다(유일 제약 없음)
    before = _q("SELECT count(*) AS n FROM seller WHERE email = :e", e=email)[0]["n"]
    got = _login(client, email)["seller"]["id"]
    assert got in (first, second) and got != google
    assert _q("SELECT count(*) AS n FROM seller WHERE email = :e", e=email)[0]["n"] == before  # 새로 만들지 않음


def test_social_accounts_with_email_are_not_reachable_by_simple_login(client):
    """PM 11-09: 소셜 계정의 이메일은 수집한다. 그래도 개발용 간이 로그인으로 그 계정의 토큰을 받을 수 없어야 한다."""
    email = _email()
    kakao = _social("kakao", _subject(), email)
    google = _social("google", _subject(), email)

    body = _login(client, email)
    assert body["seller"]["id"] not in (kakao, google)
    assert _q("SELECT auth_provider FROM seller WHERE id = :i", i=body["seller"]["id"])[0]["auth_provider"] == "local"
    for sid in (kakao, google):  # 소셜 행은 그대로(이메일 값도 바꾸지 않음)
        assert _q("SELECT email, auth_provider FROM seller WHERE id = :i", i=sid)[0]["email"] == email
    assert _login(client, email.upper())["seller"]["id"] == body["seller"]["id"]  # 대소문자만 다른 입력도 소셜 계정으로 가지 않는다


@pytest.mark.parametrize("label,sql,params", [
    ("소셜 회원번호 누락", "INSERT INTO seller (auth_provider) VALUES ('kakao')", {}),
    ("소셜 회원번호 빈 문자열", "INSERT INTO seller (auth_provider, provider_subject) VALUES ('google', '')", {}),
    ("소셜 회원번호 공백", "INSERT INTO seller (auth_provider, provider_subject) VALUES ('google', '   ')", {}),
    ("소셜인데 cognito_sub 있음", "INSERT INTO seller (auth_provider, provider_subject, cognito_sub) VALUES ('kakao', '1', 'kakao:1')", {}),
    ("local 인데 회원번호 있음", "INSERT INTO seller (cognito_sub, email, provider_subject) VALUES ('local:a@x.test', 'a@x.test', '1')", {}),
    ("local 인데 이메일 없음", "INSERT INTO seller (cognito_sub) VALUES ('local:none')", {}),
    ("local 인데 cognito_sub 없음", "INSERT INTO seller (email) VALUES ('b@x.test')", {}),
    ("모르는 제공자", "INSERT INTO seller (auth_provider, provider_subject) VALUES ('naver', '1')", {}),
    ("이름 빈 문자열", "INSERT INTO seller (auth_provider, provider_subject, display_name) VALUES ('kakao', 'dn-1', '')", {}),
    ("이름 공백만", "INSERT INTO seller (auth_provider, provider_subject, display_name) VALUES ('kakao', 'dn-2', '   ')", {}),
    ("이름 255자 초과", "INSERT INTO seller (auth_provider, provider_subject, display_name) VALUES ('google', 'dn-3', repeat('가', 256))", {}),
])
def test_identity_constraints_reject(dburl, label, sql, params):
    eng = create_engine(dburl)
    try:
        with eng.connect() as c:
            with pytest.raises((IntegrityError, DBAPIError)):
                c.execute(text(sql), params)
            c.rollback()
    finally:
        eng.dispose()


def test_provider_subject_unique_per_provider(dburl):
    sub = _subject()
    _social("kakao", sub)
    eng = create_engine(dburl)
    try:
        with eng.connect() as c:
            with pytest.raises(IntegrityError):  # 같은 제공자의 같은 회원번호는 한 계정
                c.execute(text("INSERT INTO seller (auth_provider, provider_subject) VALUES ('kakao', :s)"), {"s": sub})
            c.rollback()
    finally:
        eng.dispose()
    other = _social("google", sub)  # 제공자가 다르면 같은 번호여도 다른 계정
    assert other


def test_display_name_is_optional_and_kept_as_given(dburl):
    """PM 11-09 저장 정보(닉네임/이름). 제공자가 주지 않으면 NULL — 서버가 이름을 만들지 않는다. 응답(Seller)에는 싣지 않는다."""
    no_name = _social("kakao", _subject())
    named = _q("INSERT INTO seller (auth_provider, provider_subject, display_name) VALUES ('google', :s, :n) RETURNING id",
               s=_subject(), n=" 홍길동 ")[0]["id"]
    rows = {r["id"]: r["display_name"] for r in _q("SELECT id, display_name FROM seller WHERE id = ANY(:i)", i=[no_name, named])}
    assert rows == {no_name: None, named: " 홍길동 "}  # 저장 구조는 값을 바꾸지 않는다(정리 규칙은 C 범위)


def test_display_name_not_in_seller_response(client):
    sid = _q("INSERT INTO seller (auth_provider, provider_subject, display_name, email) VALUES ('kakao', :s, '닉네임', NULL) RETURNING id",
             s=_subject())[0]["id"]
    me = _me(client, _session(sid, None)[0])
    assert me.status_code == 200 and "displayName" not in me.json() and "display_name" not in me.json()


def test_login_token_and_cookie_refresh_work_old_format_rejected(client):
    """A 에서는 「기존 형식 토큰도 계속 동작」을 봤다. B(세션)부터 세션 없는 이전 형식은 거절된다(전환 조건 — 다시 로그인)."""
    from app.security import create_token

    email = _email()
    r = client.post("/v1/auth/login", json={"email": email, "password": "x"})
    sid = r.json()["seller"]["id"]
    me = _me(client, r.json()["accessToken"])
    assert me.status_code == 200 and me.json()["id"] == sid and me.json()["email"] == email
    refreshed = _refresh(client, r.cookies["pxl_refresh"])
    assert refreshed.status_code == 200 and _me(client, refreshed.json()["accessToken"]).json()["id"] == sid
    old = _me(client, create_token(sid, email))  # 세션 도입 전 형식
    assert old.status_code == 401 and old.json()["error"]["details"] == {"reason": "invalid_token"}


def test_social_seller_without_email_reads_null(client):
    kakao = _social("kakao", _subject())
    tok, refresh_token = _session(kakao, None)
    me = _me(client, tok)
    assert me.status_code == 200 and me.json()["id"] == kakao and me.json()["email"] is None
    r = _refresh(client, refresh_token)
    assert r.status_code == 200 and _me(client, r.json()["accessToken"]).json()["email"] is None


# ---------------------------------------------------------------------------------------------------------
# 0015(develop head — 번역 검사 stage) → 0016 업그레이드 · downgrade
# ---------------------------------------------------------------------------------------------------------
def _cols(c) -> dict:
    rows = c.execute(text(
        "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'seller' ORDER BY column_name"
    )).all()
    return {r[0]: r[1] for r in rows}


def _constraints(c) -> set:
    names = {r[0] for r in c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'seller'::regclass")).all()}
    names |= {r[0] for r in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'seller'")).all()}
    return names


def _snapshot(c) -> list:
    return [dict(r) for r in c.execute(text(
        "SELECT s.id, s.cognito_sub, s.email, s.account_type, s.created_at, b.id AS brand, j.id AS job "
        "FROM seller s LEFT JOIN brand b ON b.seller_id = s.id LEFT JOIN job j ON j.seller_id = s.id ORDER BY s.id"
    )).mappings().all()]


def test_upgrade_keeps_existing_rows_and_downgrade_refuses_with_social_rows():
    cfg = alembic_config()
    with fresh_database("0015") as url:
        eng = create_engine(url)
        try:
            with eng.begin() as c:  # 이 저장소에 있었던 생성 경로 세 가지 + 9월 수동 행 모양
                for sub, email in (("local:a@example.com", "a@example.com"), ("baseline:GS-01", "GS-01@baseline.invalid"),
                                   ("mock-seller-1", "mock@example.com"), ("local:A@Example.com", "A@Example.com")):
                    sid = c.execute(text("INSERT INTO seller (cognito_sub, email) VALUES (:s, :e) RETURNING id"),
                                    {"s": sub, "e": email}).scalar()
                    b = c.execute(text("INSERT INTO brand (seller_id, name_ko, name_en) VALUES (:s, '브랜드', 'brand') RETURNING id"),
                                  {"s": sid}).scalar()
                    c.execute(text("INSERT INTO job (seller_id, brand_id) VALUES (:s, :b)"), {"s": sid, "b": b})
            with eng.connect() as c:
                before = _snapshot(c)
                assert _cols(c)["email"] == "NO"

            command.upgrade(cfg, "0016")
            with eng.connect() as c:
                assert _snapshot(c) == before  # 기존 값·연결은 그대로
                rows = c.execute(text("SELECT auth_provider, provider_subject, display_name FROM seller")).all()
                assert {tuple(r) for r in rows} == {("local", None, None)}
                cols, cons = _cols(c), _constraints(c)
                assert cols["email"] == "YES" and cols["cognito_sub"] == "YES" and cols["auth_provider"] == "NO"
                assert cols["display_name"] == "YES"
                assert {"ck_seller_auth_provider", "ck_seller_auth_identity", "ck_seller_display_name",
                        "uq_seller_provider_subject"} <= cons

            # 소셜 행이 있으면 아무것도 바꾸기 전에 거절
            with eng.begin() as c:
                c.execute(text("INSERT INTO seller (auth_provider, provider_subject) VALUES ('kakao', '9001')"))
            with eng.connect() as c:
                cols_before, cons_before, n_before = _cols(c), _constraints(c), c.execute(text("SELECT count(*) FROM seller")).scalar()
            with pytest.raises(Exception, match="0016 downgrade"):
                command.downgrade(cfg, "0015")
            with eng.connect() as c:
                assert _cols(c) == cols_before and _constraints(c) == cons_before
                assert c.execute(text("SELECT count(*) FROM seller")).scalar() == n_before
                assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0016"

            # 소셜 행이 없으면 내려가고, NOT NULL 이 돌아오며, 기존 값은 그대로 — 다시 올려도 같다
            with eng.begin() as c:
                c.execute(text("DELETE FROM seller WHERE auth_provider <> 'local'"))
            command.downgrade(cfg, "0015")
            with eng.connect() as c:
                cols = _cols(c)
                assert not {"auth_provider", "provider_subject", "display_name"} & set(cols)
                assert not {"ck_seller_auth_provider", "ck_seller_auth_identity", "ck_seller_display_name",
                            "uq_seller_provider_subject"} & _constraints(c)
                assert cols["email"] == "NO" and cols["cognito_sub"] == "NO"
                assert _snapshot(c) == before
            command.upgrade(cfg, "0016")
            with eng.connect() as c:
                assert _snapshot(c) == before
        finally:
            eng.dispose()


# ---------------------------------------------------------------------------------------------------------
# revision 연결(2026-11-11): develop 0015_text_check_stage 뒤 0016 — ID 중복 없음, head 하나, 번역 검사 구조와 겹치지 않음
# ---------------------------------------------------------------------------------------------------------
def _check_structure(c) -> tuple:
    """0015_text_check_stage 가 바꾸는 것 — job_async_task 의 두 CHECK 와 guard 함수 정의."""
    cons = dict(c.execute(text(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'job_async_task'::regclass "
        "AND conname IN ('ck_jat_stage', 'ck_jat_k1_attempt')")).all())
    fn = c.execute(text("SELECT prosrc FROM pg_proc WHERE proname = 'job_async_task_guard'")).scalar()
    return cons.get("ck_jat_stage"), cons.get("ck_jat_k1_attempt"), fn


def test_revision_ids_unique_and_single_head_0016():
    import warnings

    from alembic.script import ScriptDirectory

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # 「Revision … is present more than once」 경고가 나면 실패
        sd = ScriptDirectory.from_config(alembic_config())
        revs = list(sd.walk_revisions())
    ids = [r.revision for r in revs]
    assert len(ids) == len(set(ids))
    assert sd.get_heads() == ["0016"]
    assert sd.get_revision("0016").down_revision == "0015"
    assert sd.get_revision("0016").module.__file__.endswith("0016_seller_auth_identity.py")
    assert sd.get_revision("0015").module.__file__.endswith("0015_text_check_stage.py")


def test_empty_database_upgrades_to_head_0016_with_both_changes():
    with fresh_database() as url:  # 빈 DB → head
        eng = create_engine(url)
        try:
            with eng.connect() as c:
                assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0016"
                assert {"auth_provider", "provider_subject", "display_name"} <= set(_cols(c))
                stage, k1, fn = _check_structure(c)
                assert "text_check" in stage and "translation_review" in k1 and "text_check" in fn
        finally:
            eng.dispose()


def test_roundtrip_0016_0015_0016_without_social_rows_keeps_data_and_text_check_structure():
    """소셜 계정이 없는 별도 DB — 0016 → 0015 → 0016 왕복. 기존 판매자·브랜드·작업 연결과 0015 번역 검사 구조가 그대로다."""
    cfg = alembic_config()
    with fresh_database("0015") as url:
        eng = create_engine(url)
        try:
            with eng.begin() as c:
                for sub, email in (("local:r1@example.com", "r1@example.com"), ("mock-seller-2", "m2@example.com")):
                    sid = c.execute(text("INSERT INTO seller (cognito_sub, email) VALUES (:s, :e) RETURNING id"),
                                    {"s": sub, "e": email}).scalar()
                    b = c.execute(text("INSERT INTO brand (seller_id, name_ko, name_en) VALUES (:s, '브랜드', 'brand') RETURNING id"),
                                  {"s": sid}).scalar()
                    c.execute(text("INSERT INTO job (seller_id, brand_id) VALUES (:s, :b)"), {"s": sid, "b": b})
            with eng.connect() as c:
                before, check_before = _snapshot(c), _check_structure(c)
                assert "text_check" in check_before[0]
            command.upgrade(cfg, "0016")
            with eng.connect() as c:
                assert _snapshot(c) == before and _check_structure(c) == check_before
            command.downgrade(cfg, "0015")
            with eng.connect() as c:
                assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0015"
                assert not {"auth_provider", "provider_subject", "display_name"} & set(_cols(c))
                assert _snapshot(c) == before and _check_structure(c) == check_before  # 0016 downgrade 는 번역 검사 구조를 건드리지 않는다
            command.upgrade(cfg, "0016")
            with eng.connect() as c:
                assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0016"
                assert _snapshot(c) == before and _check_structure(c) == check_before
        finally:
            eng.dispose()
