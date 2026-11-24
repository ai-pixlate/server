"""용어집 마이그레이션(0005)·적재 — 격리된 테스트 DB에서만 실행.

GLOSSARY_TEST_DATABASE_URL(CREATE DATABASE 권한이 있는 접속, 예: 로컬 postgres:16)이 있을 때만 돈다.
테스트마다 새 데이터베이스를 만들고 지운다. DATABASE_URL 만 설정된 경우에는 실행하지 않는다.
운영 주소인지 자동으로 판별하지 않으므로, 이 환경변수에 운영 DB 를 지정하지 않는다.

  $env:GLOSSARY_TEST_DATABASE_URL = "postgresql+psycopg://<user>:<pw>@localhost:5432/postgres"
  python -m pytest tests/test_glossary_db.py -q
"""
import itertools
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.glossary_ingest import PHRASE_REVIEW_COLUMNS, main
from tests.glossary_fixtures import base_row, write_book
from tests.test_glossary_ingest import _real_files, _real_v3_phrase

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
    command.upgrade(_alembic(), "0005")
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
    command.upgrade(_alembic(), "0005")
    _insert_legacy(db_url, "가" * 201)
    with pytest.raises(Exception) as e:
        command.downgrade(_alembic(), "0004")
    assert "200자" in str(e.value)
    assert _revision(db_url) == "0005"


def test_downgrade_without_long_values(db_url):
    command.upgrade(_alembic(), "0005")
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


# ── 문구 라벨·대표여부: 입력 헤더 × 현재 저장 값 ─────────────────────────
# 저장 상태는 시험에서 직접 만든다. 입력 형식 이력은 어디에도 없다.
# NULL↔값 전환 중단은 2026-11-06 승인된 B1 제한이며 계약에 원래 있던 규칙이 아니다.

STORED = {"값·값": ("라벨1", "대표"), "NULL·NULL": (None, None), "값·NULL": ("라벨1", None), "NULL·값": (None, "대표")}
EQUAL_STATE = {False: "NULL·NULL", True: "값·값"}  # 입력 헤더(두 열 있음 여부)별로 저장 값이 입력과 같은 상태


def _phrase_book(tmp_path, rows, review):
    path = tmp_path / f"p{uuid.uuid4().hex[:8]}.xlsx"
    return write_book(path, "phrase", rows, drop_columns=() if review else PHRASE_REVIEW_COLUMNS)


def _load_phrase(url, tmp_path, rows, review, mode="load"):
    return main([mode, "--phrase", str(_phrase_book(tmp_path, rows, review))], database_url=url)


def _snapshot(url):
    return [r[0] for r in _q(url, "SELECT g::text FROM glossary g ORDER BY id")]


def _review_values(url, external_id):
    return tuple(_q(url, "SELECT label, representative FROM glossary WHERE external_id = :e", e=external_id)[0])


def _store(url, tmp_path, state, version=2):
    assert _load_phrase(url, tmp_path, [base_row("phrase", 1, version=version)], review=False) == 0
    label, rep = STORED[state]
    _q(url, "UPDATE glossary SET label = :l, representative = :r WHERE external_id = 'GL-M001'", l=label, r=rep)


def _judge_both_modes(url, tmp_path, row, review, capsys):
    """plan 은 언제나 쓰지 않고, 거부면 load 도 쓰지 않는다. (load 종료 코드, plan 출력)을 돌려준다."""
    before = _snapshot(url)
    plan_rc = _load_phrase(url, tmp_path, [row], review, mode="plan")
    out = capsys.readouterr().out
    assert _snapshot(url) == before
    load_rc = _load_phrase(url, tmp_path, [row], review)
    capsys.readouterr()
    assert plan_rc == load_rc
    if load_rc != 0:
        assert _snapshot(url) == before
    return load_rc, out


@pytest.mark.parametrize("review,state", [(False, "NULL·NULL"), (True, "값·값")])
def test_review_same_values_are_unchanged(head, tmp_path, capsys, review, state):
    _store(head, tmp_path, state)
    before = _snapshot(head)
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=2), review, capsys)
    assert rc == 0 and "삽입 0 · 갱신 0 · 변경 없음 1" in out and _snapshot(head) == before


@pytest.mark.parametrize("review,state", [
    (r, s) for r, s in itertools.product((False, True), STORED) if s != EQUAL_STATE[r]
])
def test_review_values_differ_at_same_version_is_the_existing_rule(head, tmp_path, capsys, review, state):
    _store(head, tmp_path, state)
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=2), review, capsys)
    assert rc == 1 and "같은 version" in out and "B1 제한" not in out


@pytest.mark.parametrize("review,state", itertools.product((False, True), STORED))
def test_other_content_at_same_version_is_the_existing_rule(head, tmp_path, capsys, review, state):
    _store(head, tmp_path, state)
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=2, target_text="changed"), review, capsys)
    assert rc == 1 and "같은 version" in out and "term_target" in out and "B1 제한" not in out


@pytest.mark.parametrize("review,state", itertools.product((False, True), STORED))
def test_lower_version_is_the_existing_rule(head, tmp_path, capsys, review, state):
    _store(head, tmp_path, state)
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=1), review, capsys)
    assert rc == 1 and "version 이 낮아짐" in out and "B1 제한" not in out


@pytest.mark.parametrize("review,state", [(False, "NULL·NULL"), (True, "값·값")])
def test_higher_version_updates_when_no_null_transition(head, tmp_path, capsys, review, state):
    _store(head, tmp_path, state)
    pk = _q(head, "SELECT id FROM glossary WHERE external_id = 'GL-M001'")[0][0]
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=3, target_text="new"), review, capsys)
    assert rc == 0 and "갱신 1" in out
    assert _q(head, "SELECT id, term_target, version FROM glossary WHERE external_id = 'GL-M001'")[0] == (pk, "new", 3)
    assert _review_values(head, "GL-M001") == STORED[state]


def test_higher_version_replaces_value_with_other_value(head, tmp_path, capsys):
    _store(head, tmp_path, "값·값")
    rc, _ = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=3, 라벨="다른라벨", 대표여부="대안"), True, capsys)
    assert rc == 0 and _review_values(head, "GL-M001") == ("다른라벨", "대안")


@pytest.mark.parametrize("review,state,needles", [
    (False, "값·값", ["label(값→NULL)", "representative(값→NULL)"]),
    (False, "값·NULL", ["label(값→NULL)"]),
    (False, "NULL·값", ["representative(값→NULL)"]),
    (True, "NULL·NULL", ["label(NULL→값)", "representative(NULL→값)"]),
    (True, "값·NULL", ["representative(NULL→값)"]),
    (True, "NULL·값", ["label(NULL→값)"]),
])
def test_higher_version_with_null_transition_stops(head, tmp_path, capsys, review, state, needles):
    _store(head, tmp_path, state)
    before = _snapshot(head)
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=3, target_text="new"), review, capsys)
    assert rc == 1 and "B1 제한" in out and "GL-M001" in out
    for needle in needles:
        assert needle in out
    assert _snapshot(head) == before


@pytest.mark.parametrize("review,state,expected", [
    (False, "NULL·NULL", 0), (False, "값·값", 1), (True, "값·값", 0), (True, "NULL·NULL", 1),
])
def test_stored_version_null_follows_the_same_rule(head, tmp_path, capsys, review, state, expected):
    _store(head, tmp_path, state)
    _q(head, "UPDATE glossary SET version = NULL WHERE external_id = 'GL-M001'")
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=3), review, capsys)
    assert rc == expected and ("B1 제한" in out) == (expected == 1)


def test_natural_key_rule_comes_before_null_transition(head, tmp_path, capsys):
    _store(head, tmp_path, "값·값")
    rc, out = _judge_both_modes(head, tmp_path, base_row("phrase", 1, version=3, source_ko="다른 한국어"), False, capsys)
    assert rc == 1 and "번호 재사용" in out and "B1 제한" not in out


@pytest.mark.parametrize("kind", ["certification", "ingredient"])
def test_other_kinds_do_not_get_the_null_transition_stop(head, tmp_path, kind):
    # 성분·인증 파일에는 두 열이 없고 현행 version 규칙이 그대로다(수동으로 값이 들어간 행도 갱신된다)
    assert _load(head, tmp_path, [base_row(kind, 1)], kind=kind) == 0
    ext = base_row(kind, 1)["id"]
    _q(head, "UPDATE glossary SET label = 'x' WHERE external_id = :e", e=ext)
    assert _load(head, tmp_path, [base_row(kind, 1)], kind=kind) == 1  # 같은 version 내용 다름
    assert _load(head, tmp_path, [base_row(kind, 1, version=2)], kind=kind) == 0
    assert _review_values(head, ext) == (None, None)


# ── B1 제한의 거부가 다른 행의 쓰기까지 막는지 ───────────────────────────

@pytest.mark.parametrize("review", [False, True])
def test_b1_stop_blocks_every_other_row_in_the_file(head, tmp_path, capsys, review):
    # 1=정상 갱신 · 2=무변경 · 3=B1 제한 거부 · 4=신규
    assert _load_phrase(head, tmp_path, [base_row("phrase", 1), base_row("phrase", 2)], review) == 0
    assert _load_phrase(head, tmp_path, [base_row("phrase", 3)], not review) == 0
    before = _snapshot(head)
    capsys.readouterr()
    rows = [base_row("phrase", 1, version=2, target_text="new"), base_row("phrase", 2),
            base_row("phrase", 3, version=2), base_row("phrase", 4)]
    for mode in ("plan", "load"):
        assert _load_phrase(head, tmp_path, rows, review, mode=mode) == 1
        out = capsys.readouterr().out
        assert "삽입 1 · 갱신 1 · 변경 없음 1" in out  # 나머지 행은 쓸 수 있는 상태였다
        assert "GL-M003" in out and "B1 제한" in out and "DB에 쓰지 않음" in out
        assert _snapshot(head) == before
    assert _q(head, "SELECT count(*) FROM glossary WHERE external_id = 'GL-M004'")[0][0] == 0
    assert _q(head, "SELECT term_target, version FROM glossary WHERE external_id = 'GL-M001'")[0] == ("phrase english 1", 1)
    # 거부 행만 빼면 같은 입력이 정상 적재된다(거부가 상태를 남기지 않았다)
    assert _load_phrase(head, tmp_path, [rows[0], rows[1], rows[3]], review) == 0
    assert _q(head, "SELECT count(*) FROM glossary")[0][0] == 4


def test_b1_stop_blocks_other_kinds_in_the_same_run(head, tmp_path, capsys):
    assert _load(head, tmp_path, [base_row("certification", 1)]) == 0
    assert _load_phrase(head, tmp_path, [base_row("phrase", 3)], review=True) == 0
    before = _snapshot(head)
    capsys.readouterr()
    cert = write_book(tmp_path / "c2.xlsx", "certification",
                      [base_row("certification", 1, version=2, target_text="new"), base_row("certification", 2)])
    ingr = write_book(tmp_path / "i2.xlsx", "ingredient", [base_row("ingredient", 1)])
    phrase = _phrase_book(tmp_path, [base_row("phrase", 3, version=2)], review=False)
    args = ["--certification", str(cert), "--ingredient", str(ingr), "--phrase", str(phrase)]
    for mode in ("plan", "load"):
        assert main([mode, *args], database_url=head) == 1
        out = capsys.readouterr().out
        assert "삽입 2 · 갱신 1 · 변경 없음 0" in out and "B1 제한" in out
        assert _snapshot(head) == before


def test_every_b1_stop_is_reported(head, tmp_path, capsys):
    assert _load_phrase(head, tmp_path, [base_row("phrase", 1), base_row("phrase", 2)], review=True) == 0
    capsys.readouterr()
    rows = [base_row("phrase", 1, version=2), base_row("phrase", 2, version=2)]
    assert _load_phrase(head, tmp_path, rows, review=False) == 1
    out = capsys.readouterr().out
    assert "GL-M001" in out and "GL-M002" in out and "오류 2건" in out


# ── 추가 적재 호환성 확인 ────────────────────────────────────────────────
# 행 집합을 추가로 넣어도 로더가 충돌 없이 동작하는지만 본다. 교체 검증이 아니며 두 행 집합의 공존을 승인하지 않는다.
# 기존 문구 제외·참조 보존·복구는 B2 이고, "모두 신규"는 이 시험 데이터 기준이다(실제 dev ID 는 확인하지 않았다).

def test_additive_17col_load_keeps_existing_rows(head, tmp_path, capsys):
    old = [base_row("phrase", n) for n in (1, 2, 3)]
    assert _load_phrase(head, tmp_path, old, review=True) == 0
    before = _snapshot(head)
    capsys.readouterr()
    new = [base_row("phrase", n, id=f"GL-M01{n:03d}", internal_category="G01") for n in range(1, 6)]
    assert _load_phrase(head, tmp_path, new, review=False, mode="plan") == 0
    assert "삽입 5 · 갱신 0 · 변경 없음 0 · 이번 파일에 없는 기존 행(유지) 3" in capsys.readouterr().out
    assert _snapshot(head) == before
    assert _load_phrase(head, tmp_path, new, review=False) == 0
    after = _snapshot(head)
    assert after[:3] == before and len(after) == 8
    assert _q(head, "SELECT count(*) FROM glossary WHERE internal_category = 'G01' "
                    "AND label IS NULL AND representative IS NULL")[0][0] == 5
    capsys.readouterr()
    assert _load_phrase(head, tmp_path, new, review=False) == 0
    assert "삽입 0 · 갱신 0 · 변경 없음 5" in capsys.readouterr().out
    assert _snapshot(head) == after


@pytest.mark.skipif(_real_v3_phrase() is None, reason="GLOSSARY_V3_PHRASE_XLSX 없음")
def test_real_v3_phrase_additive_load_on_empty_db(head, capsys):
    args = ["--phrase", _real_v3_phrase()]
    assert main(["load", *args], database_url=head) == 0
    assert "삽입 555 · 갱신 0 · 변경 없음 0" in capsys.readouterr().out
    pk = dict(_q(head, "SELECT external_id, id FROM glossary"))
    assert _q(head, "SELECT count(*) FROM glossary WHERE label IS NULL AND representative IS NULL")[0][0] == 555
    assert main(["load", *args], database_url=head) == 0
    assert "삽입 0 · 갱신 0 · 변경 없음 555" in capsys.readouterr().out
    assert dict(_q(head, "SELECT external_id, id FROM glossary")) == pk


@pytest.mark.skipif(_real_v3_phrase() is None or _real_files() is None,
                    reason="GLOSSARY_V3_PHRASE_XLSX · GLOSSARY_XLSX_DIR 없음")
def test_real_v3_phrase_additive_plan_over_v2_delivery_file(head, capsys):
    # 기존 행은 v2 전달본 파일로 만든 시험 데이터다. 실제 dev 행이 아니다.
    assert main(["load", "--phrase", _real_files()["phrase"]], database_url=head) == 0
    before = _snapshot(head)
    assert len(before) == 65
    capsys.readouterr()
    args = ["--phrase", _real_v3_phrase()]
    assert main(["plan", *args], database_url=head) == 0
    assert "삽입 555 · 갱신 0 · 변경 없음 0 · 이번 파일에 없는 기존 행(유지) 65" in capsys.readouterr().out
    assert _snapshot(head) == before
    assert main(["load", *args], database_url=head) == 0
    after = _snapshot(head)
    assert len(after) == 620 and after[:65] == before
