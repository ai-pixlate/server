"""실행·통합 테스트용 격리 DB.

PIXLATE_TEST_DATABASE_URL(CREATE DATABASE 권한이 있는 접속, 예: 로컬 postgres:16)이 있을 때만 쓴다.
모듈(또는 테스트)마다 새 데이터베이스를 만들어 `alembic upgrade head` 한 뒤 app.db 의 세션을 그 DB에 묶고,
끝나면 지운다. 운영 주소인지 자동으로 판별하지 않으므로 운영 DB 를 지정하지 않는다.

  $env:PIXLATE_TEST_DATABASE_URL = "postgresql+psycopg://<user>:<pw>@localhost:55432/postgres"
  python -m pytest tests/test_exec_*.py -q
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ADMIN_URL = os.getenv("PIXLATE_TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(not ADMIN_URL, reason="PIXLATE_TEST_DATABASE_URL 없음")
ROOT = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


@contextmanager
def fresh_database(revision: str = "head"):
    """새 DB를 만들고 revision까지 올린 URL을 준다. 끝나면 지운다."""
    name = f"px_exec_{uuid.uuid4().hex[:10]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url  # migrations/env.py 가 읽는다
    try:
        command.upgrade(alembic_config(), revision)
        yield url
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def bound_app_db(url: str):
    """app.db 의 engine·SessionLocal 을 테스트 DB로 바꿔 끼운다(라우터·워커가 같은 DB를 쓰게)."""
    from app import db as app_db

    eng = create_engine(url, pool_pre_ping=True, future=True)
    old_engine = app_db.engine
    app_db.engine = eng
    app_db.SessionLocal.configure(bind=eng)
    try:
        yield eng
    finally:
        app_db.SessionLocal.configure(bind=old_engine)
        app_db.engine = old_engine
        eng.dispose()


def seed_job(conn, *, regulatory_class: str | None = "cosmetic", name_ko: str = "구달", name_en: str = "goodal") -> dict:
    """셀러·브랜드·작업 한 벌. 반환: {seller, brand, job}."""
    email = f"exec-{uuid.uuid4().hex[:8]}@example.com"
    seller = conn.execute(
        text("INSERT INTO seller (cognito_sub, email) VALUES (:sub, :e) RETURNING id"),
        {"sub": f"local:{email}", "e": email},
    ).scalar()
    brand = conn.execute(
        text("INSERT INTO brand (seller_id, name_ko, name_en) VALUES (:s, :ko, :en) RETURNING id"),
        {"s": seller, "ko": name_ko, "en": name_en},
    ).scalar()
    job = conn.execute(
        text(
            "INSERT INTO job (seller_id, brand_id, product_name, target_country, regulatory_class, target_lang) "
            "VALUES (:s, :b, '수분 크림', 'US', :rc, 'en') RETURNING id"
        ),
        {"s": seller, "b": brand, "rc": regulatory_class},
    ).scalar()
    return {"seller": seller, "brand": brand, "job": job, "email": email}
