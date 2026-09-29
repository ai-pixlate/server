"""용어집 마이그레이션(0005)·적재 — 격리된 테스트 DB에서만 실행.

GLOSSARY_TEST_DATABASE_URL(CREATE DATABASE 권한이 있는 접속, 예: 로컬 postgres:16)이 있을 때만 돈다.
테스트마다 새 데이터베이스를 만들고 지운다. DATABASE_URL 만 설정된 경우에는 실행하지 않는다.
운영 주소인지 자동으로 판별하지 않으므로, 이 환경변수에 운영 DB 를 지정하지 않는다.

  $env:GLOSSARY_TEST_DATABASE_URL = "postgresql+psycopg://<user>:<pw>@localhost:5432/postgres"
  python -m pytest tests/test_glossary_db.py -q
"""
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.glossary_ingest import main
from tests.glossary_fixtures import base_row, write_book
from tests.test_glossary_ingest import _real_files

ADMIN_URL = os.getenv("GLOSSARY_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="GLOSSARY_TEST_DATABASE_URL 없음")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_url(monkeypatch):
    name = f"gl_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)  # migrations/env.py 가 읽는다
    yield url
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _alembic():
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def _q(url, sql, **params):
    eng = create_engine(url)
    try:
        with eng.begin() as c:
            res = c.execute(text(sql), params)
            return res.all() if res.returns_rows else None
    finally:
        eng.dispose()


def _revision(url):
    return _q(url, "SELECT version_num FROM alembic_version")[0][0]


def _insert_legacy(url, term_ko, enforcement="reference", category="common"):
    return _q(url, "INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement) "
                   "VALUES (:k, 'legacy', 'en', :c, :e) RETURNING id", k=term_ko, c=category, e=enforcement)[0][0]


# ── 마이그레이션 ─────────────────────────────────────────────────────────

def test_upgrade_empty_database_to_0005(db_url):
    command.upgrade(_alembic(), "head")
    assert _revision(db_url) == "0005"
    cols = dict(_q(db_url, "SELECT column_name, data_type FROM information_schema.columns "
                           "WHERE table_name = 'glossary'"))
    assert cols["term_ko"] == "text" and cols["term_target"] == "text"
    assert cols["verified_at"] == "date" and cols["version"] == "integer" and cols["frequency"] == "integer"
    assert cols["external_id"] == "character varying"
    nullable = dict(_q(db_url, "SELECT column_name, is_nullable FROM information_schema.columns "
                               "WHERE table_name = 'glossary'"))
    assert nullable["term_ko"] == "NO" and nullable["external_id"] == "YES" and nullable["version"] == "YES"
    cons = {r[0] for r in _q(db_url, "SELECT conname FROM pg_constraint WHERE conrelid = 'glossary'::regclass")}
    assert {"uq_glossary_external_id", "uq_glossary_natural_key", "ck_glossary_enforcement"} <= cons


def test_upgrade_keeps_existing_rows_and_pk(db_url):
    command.upgrade(_alembic(), "0004")
    pk = _insert_legacy(db_url, "기존 용어", enforcement="enforced")
    command.upgrade(_alembic(), "0005")
    row = _q(db_url, "SELECT id, term_ko, enforcement, external_id, version FROM glossary")[0]
    assert tuple(row) == (pk, "기존 용어", "enforced", None, None)


@pytest.mark.parametrize("setup,needle", [
    ("dup", "중복 조합"),
    ("enforcement", "enforced/reference"),
])
def test_upgrade_stops_on_existing_violations(db_url, setup, needle):
    command.upgrade(_alembic(), "0004")
    if setup == "dup":
        _insert_legacy(db_url, "겹침")
        _insert_legacy(db_url, "겹침")
    else:
        _insert_legacy(db_url, "강제값", enforcement="강제")
    with pytest.raises(Exception) as e:
        command.upgrade(_alembic(), "0005")
    assert needle in str(e.value)
    assert _revision(db_url) == "0004"  # 트랜잭션 전체 취소
    cols = {r[0] for r in _q(db_url, "SELECT column_name FROM information_schema.columns WHERE table_name='glossary'")}
    assert "external_id" not in cols


def test_downgrade_refuses_to_truncate_long_values(db_url):
    command.upgrade(_alembic(), "head")
    _insert_legacy(db_url, "가" * 201)
    with pytest.raises(Exception) as e:
        command.downgrade(_alembic(), "0004")
    assert "200자" in str(e.value)
    assert _revision(db_url) == "0005"


def test_downgrade_without_long_values(db_url):
    command.upgrade(_alembic(), "head")
    _insert_legacy(db_url, "짧은 용어")
    command.downgrade(_alembic(), "0004")
    assert _revision(db_url) == "0004"
    t = dict(_q(db_url, "SELECT column_name, character_maximum_length FROM information_schema.columns "
                        "WHERE table_name='glossary' AND column_name IN ('term_ko','term_target')"))
    assert t == {"term_ko": 200, "term_target": 200}


# ── 적재 ─────────────────────────────────────────────────────────────────

def _load(db_url, tmp_path, rows, mode="load", name="c.xlsx", kind="certification"):
    path = write_book(tmp_path / name, kind, rows)
    return main([mode, f"--{kind}", str(path)], database_url=db_url)


def _rows(db_url):
    return {r[1]: r for r in _q(db_url, "SELECT id, external_id, term_target, version, zone FROM glossary")}


@pytest.fixture
def head(db_url):
    command.upgrade(_alembic(), "head")
    return db_url


def test_plan_does_not_block_or_wait_for_writers(head, tmp_path):
    # 다른 세션이 쓰기 트랜잭션을 연 상태에서도 plan 은 잠금을 기다리지 않고 끝나야 한다
    eng = create_engine(head)
    try:
        with eng.connect() as other:
            tx = other.begin()
            other.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category) "
                               "VALUES ('진행 중', 'x', 'en', 'common')"))
            other.execute(text("SET LOCAL lock_timeout = '1s'"))
            other.execute(text("LOCK TABLE glossary IN ROW EXCLUSIVE MODE"))
            import time
            t0 = time.monotonic()
            assert _load(head, tmp_path, [base_row("certification", 1)], mode="plan") == 0
            assert time.monotonic() - t0 < 5
            tx.rollback()
    finally:
        eng.dispose()


def test_plan_writes_nothing(head, tmp_path, capsys):
    assert _load(head, tmp_path, [base_row("certification", 1)], mode="plan") == 0
    assert _rows(head) == {}
    assert "삽입 1" in capsys.readouterr().out


def test_reload_is_idempotent_and_keeps_pk(head, tmp_path, capsys):
    rows = [base_row("certification", 1), base_row("certification", 2)]
    assert _load(head, tmp_path, rows) == 0
    before = _rows(head)
    assert _load(head, tmp_path, rows) == 0
    assert _rows(head) == before
    assert "삽입 0 · 갱신 0 · 변경 없음 2" in capsys.readouterr().out


def test_higher_version_updates_in_place(head, tmp_path):
    _load(head, tmp_path, [base_row("certification", 1)])
    pk = _rows(head)["GL-C001"][0]
    assert _load(head, tmp_path, [base_row("certification", 1, target_text="new", version=2)]) == 0
    assert tuple(_rows(head)["GL-C001"]) == (pk, "GL-C001", "new", 2, "A")


@pytest.mark.parametrize("over,needle", [
    ({"target_text": "changed"}, "같은 version"),
    ({"version": 0}, "version 이 낮아짐"),
    ({"source_ko": "다른 한국어", "version": 2}, "번호 재사용"),
])
def test_reload_policy_violations_write_nothing(head, tmp_path, capsys, over, needle):
    _load(head, tmp_path, [base_row("certification", 1)])
    before = _rows(head)
    assert _load(head, tmp_path, [base_row("certification", 1, **over)]) == 1
    assert needle in capsys.readouterr().out
    assert _rows(head) == before


def test_same_natural_key_with_other_id_is_rejected(head, tmp_path, capsys):
    _load(head, tmp_path, [base_row("certification", 1)])
    assert _load(head, tmp_path, [base_row("certification", 9, source_ko="certification-한국어-1")]) == 1
    assert "다른 원본 ID" in capsys.readouterr().out


def test_legacy_row_without_id_is_not_linked_silently(head, tmp_path, capsys):
    _insert_legacy(head, "certification-한국어-1")
    assert _load(head, tmp_path, [base_row("certification", 1)]) == 1
    assert "원본 ID 없는 기존 행" in capsys.readouterr().out


def test_demoted_row_stops_the_load(head, tmp_path, capsys):
    _load(head, tmp_path, [base_row("phrase", 1)], kind="phrase", name="p.xlsx")
    demoted = [base_row("phrase", 1, 구역="C", 적재여부="N")]
    assert _load(head, tmp_path, demoted, kind="phrase", name="p.xlsx") == 1
    assert "적재 제외" in capsys.readouterr().out
    assert "GL-M001" in _rows(head)


def test_pk_fingerprint_is_stable_across_reload(head, tmp_path, capsys):
    rows = [base_row("certification", 1), base_row("certification", 2)]
    _load(head, tmp_path, rows)
    first = [l for l in capsys.readouterr().out.splitlines() if "PK 지문" in l]
    _load(head, tmp_path, rows, mode="plan")
    second = [l for l in capsys.readouterr().out.splitlines() if "PK 지문" in l]
    assert first and first == second and "2/2" in first[0]


def test_value_over_index_limit_rolls_back_everything(head, tmp_path, capsys):
    # 압축이 안 되는 긴 한국어(약 3,000바이트)는 자연키 인덱스 한도를 넘는다 — DB가 거부하고 전부 롤백
    import random
    rnd = random.Random(0)
    huge = "".join(chr(0xAC00 + rnd.randrange(11000)) for _ in range(1000))
    rows = [base_row("certification", 1), base_row("certification", 2, source_ko=huge)]
    assert _load(head, tmp_path, rows) == 1
    out = capsys.readouterr().out
    assert "DB 오류" in out and "index row size" in out
    assert _rows(head) == {}


def test_rows_missing_from_new_file_are_kept(head, tmp_path, capsys):
    _load(head, tmp_path, [base_row("certification", 1), base_row("certification", 2)])
    assert _load(head, tmp_path, [base_row("certification", 1)]) == 0
    assert "이번 파일에 없는 기존 행(유지) 1" in capsys.readouterr().out
    assert set(_rows(head)) == {"GL-C001", "GL-C002"}


@pytest.mark.skipif(_real_files() is None, reason="GLOSSARY_XLSX_DIR 없음")
def test_real_delivery_loads_twice(head, capsys):
    files = _real_files()
    args = [a for k, p in files.items() for a in (f"--{k}", p)]
    assert main(["load", *args], database_url=head) == 0
    pk_before = dict(_q(head, "SELECT external_id, id FROM glossary"))
    assert main(["load", *args], database_url=head) == 0
    assert "삽입 0 · 갱신 0 · 변경 없음 20653" in capsys.readouterr().out
    assert dict(_q(head, "SELECT external_id, id FROM glossary")) == pk_before
    by_prefix = dict(_q(head, "SELECT left(external_id, 4), count(*) FROM glossary GROUP BY 1"))
    assert by_prefix == {"GL-C": 22, "GL-I": 20566, "GL-M": 65}
    assert _q(head, "SELECT count(*) FROM glossary WHERE length(term_target) > 200")[0][0] == 41
    assert _q(head, "SELECT count(*) FROM glossary WHERE external_id IN ('GL-M004','GL-M050')")[0][0] == 0
    cats = dict(_q(head, "SELECT internal_category, count(*) FROM glossary GROUP BY 1"))
    assert cats == {"common": 20645, "Sunscreens & Tanning Products": 8}
