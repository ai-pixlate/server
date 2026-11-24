"""인증·성분 교체 CLI(app.glossary_replace) — 격리 DB 와 메모리 저장소에서만 실행한다. 실제 S3 에 접속하지 않는다.

GLOSSARY_TEST_DATABASE_URL 이 있을 때만 돈다. 실제 전달본 시험은 GLOSSARY_XLSX_DIR(09-28 전달본)과
GLOSSARY_REPLACE_FINAL_DIR(인증 v5 · 성분 v3.1, certification.xlsx · ingredient.xlsx)이 함께 있을 때만 돈다.
"""
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import glossary_replace as gr
from app.flows import downstream
from app.glossary_ingest import main as ingest
from app.storage import MemoryStore, set_store
from tests.glossary_fixtures import base_row, write_book
from tests.test_glossary_db import ADMIN_URL, _alembic, _q, _snapshot, db_url, head  # noqa: F401  (픽스처)
from tests.test_glossary_ingest import _real_files

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="GLOSSARY_TEST_DATABASE_URL 없음")
CODE = "testcode"
RUN = "run-1"
SUN = "Sunscreens & Tanning"


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _old_files(tmp):
    cert = [base_row("certification", 1), base_row("certification", 10, internal_category=SUN)]
    ing = [base_row("ingredient", n) for n in range(1, 5)]
    phrase = [base_row("phrase", 1, source_ko="기존 문구")]
    return {
        "certification": write_book(tmp / "old_c.xlsx", "certification", cert),
        "ingredient": write_book(tmp / "old_i.xlsx", "ingredient", ing),
        "phrase": write_book(tmp / "old_p.xlsx", "phrase", phrase),
    }


def _new_files(tmp, *, cert=None, ing=None):
    cert = cert if cert is not None else [
        base_row("certification", 1, version=2, note="근거 갱신"),
        base_row("certification", 10, version=2, internal_category="common", target_text="certification new 10"),
        base_row("certification", 2, source_ko="새 인증"),
    ]
    ing = ing if ing is not None else [
        base_row("ingredient", 1, verified_at="2026-11-06"),
        base_row("ingredient", 2, version=2, target_text="ingredient new 2", verified_at="2026-11-06"),
        base_row("ingredient", 3),
    ]
    return {
        "certification": str(write_book(tmp / "new_c.xlsx", "certification", cert)),
        "ingredient": str(write_book(tmp / "new_i.xlsx", "ingredient", ing)),
    }


def _approve(monkeypatch, files):
    monkeypatch.setitem(gr.APPROVED_VERIFIED_AT_ONLY, "ingredient", _sha(files["ingredient"]))
    monkeypatch.setitem(gr.APPROVED_CATEGORY_CHANGES, "certification", {
        "sha256": _sha(files["certification"]),
        "changes": {"GL-C010": ("Sunscreens & Tanning Products", "common")},
    })


def _args(files):
    return [x for k, p in files.items() for x in (f"--{k}", str(p))]


@pytest.fixture
def store():
    s = MemoryStore()
    set_store(s)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("S3_BUCKET", "test-bucket")
        yield s
    set_store(None)


@pytest.fixture
def ready(head, tmp_path, store, monkeypatch):
    old = _old_files(tmp_path)
    for kind, p in old.items():
        assert ingest(["load", f"--{kind}", str(p)], database_url=head) == 0
    files = _new_files(tmp_path)
    _approve(monkeypatch, files)
    return head, files


def _plan(url, files, run=RUN):
    return gr.main(["plan", "--run-id", run, "--code-sha", CODE, *_args(files)], database_url=url)


def _spec_sha(store, run=RUN):
    return hashlib.sha256(store.get(gr._key(run, "spec"))).hexdigest()


def _apply(url, files, store, run=RUN, sha=None):
    return gr.main(["apply", "--run-id", run, "--code-sha", CODE, "--spec-sha", sha or _spec_sha(store, run), *_args(files)],
                   database_url=url)


def _restore(url, store, run=RUN):
    return gr.main(["restore", "--run-id", run, "--spec-sha", _spec_sha(store, run)], database_url=url)


def _status(url, store, run=RUN, capsys=None):
    rc = gr.main(["status", "--run-id", run, "--spec-sha", _spec_sha(store, run)], database_url=url)
    return rc, capsys.readouterr().out if capsys else ""


def _audit(url):
    return _q(url, "SELECT count(*) FROM audit_log WHERE action_type LIKE 'glossary_%'")[0][0]


def _row(url, eid):
    return _q(url, "SELECT id, term_target, internal_category, verified_at, version, retired_at, note "
                   "FROM glossary WHERE external_id = :e", e=eid)[0]


# ── plan ──────────────────────────────────────────────────────────────────

def test_plan_stores_spec_and_does_not_write_db(ready, store):
    url, files = ready
    before = _snapshot(url)
    assert _plan(url, files) == 0
    assert _snapshot(url) == before and _audit(url) == 0
    spec = json.loads(store.get(gr._key(RUN, "spec")))
    assert gr._counts(spec) == {"inserts": 1, "retires": 1, "unchanged": 1, "updates": 4, "updates_category_change": 1,
                                "updates_verified_at_only": 1, "updates_version_up": 2}
    assert spec["files"]["ingredient"]["sha256"] == _sha(files["ingredient"])
    assert {r["external_id"] for r in spec["before"]} == {"GL-C001", "GL-C010", "GL-I001", "GL-I002", "GL-I003", "GL-I004"}
    assert all("term_target" in r and "retired_at" in r for r in spec["before"])  # 변경 전 전체 열


def test_plan_same_key_same_content_is_ok_and_different_content_is_not_overwritten(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    first = store.get(gr._key(RUN, "spec"))
    assert _plan(url, files) == 0
    _q(url, "UPDATE glossary SET note = '변경' WHERE external_id = 'GL-M001'")  # 규칙 위반은 아니지만 명세 내용이 달라진다
    assert _plan(url, files) == 1
    assert store.get(gr._key(RUN, "spec")) == first


def test_plan_store_failure_stops_and_apply_cannot_start(ready, store, monkeypatch):
    url, files = ready
    before = _snapshot(url)

    def boom(*a, **k):
        raise OSError("저장 실패")

    monkeypatch.setattr(store, "put_if_absent", boom)
    assert _plan(url, files) == 1
    monkeypatch.undo()
    _approve(monkeypatch, files)
    assert gr.main(["apply", "--run-id", RUN, "--code-sha", CODE, "--spec-sha", "0" * 64, *_args(files)], database_url=url) == 1
    assert _snapshot(url) == before


def test_plan_read_back_mismatch_stops(ready, store, monkeypatch):
    url, files = ready
    real = store.get
    monkeypatch.setattr(store, "get", lambda k: real(k) + b"x")
    assert _plan(url, files) == 1


# ── 빈 입력 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("empty", ["ingredient", "certification"])
def test_empty_input_with_normal_other_kind_stops_plan(ready, store, tmp_path, monkeypatch, empty):
    url, files = ready
    before = _snapshot(url)
    bad = dict(files, **{empty: str(write_book(tmp_path / f"empty_{empty}.xlsx", empty, []))})
    assert _plan(url, bad) == 1
    assert not store.exists(gr._key(RUN, "spec"))
    assert _snapshot(url) == before


def test_header_only_single_kind_stops_plan(ready, store, tmp_path):
    url, _ = ready
    before = _snapshot(url)
    empty = {"ingredient": str(write_book(tmp_path / "empty.xlsx", "ingredient", []))}
    assert _plan(url, empty) == 1
    assert not store.exists(gr._key(RUN, "spec")) and _snapshot(url) == before
    assert _q(url, "SELECT count(*) FROM glossary WHERE retired_at IS NOT NULL")[0][0] == 0


def test_only_excluded_rows_stop_plan(ready, store, tmp_path):
    url, _ = ready
    only_c = {"ingredient": str(write_book(tmp_path / "c.xlsx", "ingredient", [base_row("ingredient", 50, 구역="C")]))}
    assert _plan(url, only_c) == 1
    assert not store.exists(gr._key(RUN, "spec"))


def test_empty_input_cannot_pass_apply_with_existing_spec(ready, store, tmp_path):
    url, files = ready
    assert _plan(url, files) == 0
    before = _snapshot(url)
    bad = dict(files, ingredient=str(write_book(tmp_path / "empty.xlsx", "ingredient", [])))
    assert _apply(url, bad, store) == 1
    assert _snapshot(url) == before and _audit(url) == 0


# ── 대상 고정 ─────────────────────────────────────────────────────────────

def _other_host(url):
    # 같은 서버 · 같은 DB 이름 · 같은 revision · 같은 내용이지만 접속 대상이 다르게 식별되는 경우
    return make_url(url).set(host="127.0.0.1").render_as_string(hide_password=False)


def test_spec_holds_no_secret_and_binds_target(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    raw = store.get(gr._key(RUN, "spec")).decode("utf-8")
    target = json.loads(raw)["target"]
    assert target["s3_bucket"] == "test-bucket" and len(target["locator_sha256"]) == 64
    assert make_url(url).password not in raw and "localhost" not in raw and "postgresql" not in raw


@pytest.mark.parametrize("mode", ["apply", "restore", "status"])
def test_other_database_location_is_rejected(ready, store, mode):
    url, files = ready
    assert _plan(url, files) == 0
    sha = _spec_sha(store)
    if mode == "restore":
        assert _apply(url, files, store) == 0
    before, audit = _snapshot(url), _audit(url)
    other = _other_host(url)
    if mode == "apply":
        rc = _apply(other, files, store, sha=sha)
    else:
        rc = gr.main([mode, "--run-id", RUN, "--spec-sha", sha], database_url=other)
    assert rc == 1 and _snapshot(url) == before and _audit(url) == audit


@pytest.mark.parametrize("mode", ["apply", "restore", "status"])
def test_other_bucket_is_rejected(ready, store, mode, monkeypatch):
    url, files = ready
    assert _plan(url, files) == 0
    sha = _spec_sha(store)
    if mode == "restore":
        assert _apply(url, files, store) == 0
    before = _snapshot(url)
    monkeypatch.setenv("S3_BUCKET", "other-bucket")
    if mode == "apply":
        rc = _apply(url, files, store, sha=sha)
    else:
        rc = gr.main([mode, "--run-id", RUN, "--spec-sha", sha], database_url=url)
    assert rc == 1 and _snapshot(url) == before


def test_applied_run_on_other_location_does_not_resume(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    sha = _spec_sha(store)
    assert _apply(url, files, store) == 0
    assert _apply(_other_host(url), files, store, sha=sha) == 1


def test_missing_bucket_setting_stops(ready, store, monkeypatch):
    url, files = ready
    monkeypatch.delenv("S3_BUCKET")
    assert _plan(url, files) == gr.EXIT_ENV
    assert not store.exists(gr._key(RUN, "spec"))


# ── apply ─────────────────────────────────────────────────────────────────

def test_apply_keeps_pk_marks_retired_and_writes_audit_and_result(ready, store):
    url, files = ready
    pk = dict(_q(url, "SELECT external_id, id FROM glossary"))
    phrase_before = _q(url, "SELECT g::text FROM glossary g WHERE external_id = 'GL-M001'")
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    after = dict(_q(url, "SELECT external_id, id FROM glossary"))
    assert {k: after[k] for k in pk} == pk and "GL-C002" in after  # PK 유지, 빠진 행도 남음, 신규 삽입
    assert _row(url, "GL-I004").retired_at is not None
    assert _row(url, "GL-I002").term_target == "ingredient new 2" and _row(url, "GL-I002").version == 2
    i1 = _row(url, "GL-I001")
    assert str(i1.verified_at) == "2026-11-06" and i1.version == 1  # 파일 version 그대로
    c10 = _row(url, "GL-C010")
    assert c10.internal_category == "common" and c10.version == 2
    assert _q(url, "SELECT g::text FROM glossary g WHERE external_id = 'GL-M001'") == phrase_before
    audit = _q(url, "SELECT action_type, target_id, detail FROM audit_log WHERE action_type LIKE 'glossary_%' ORDER BY id")
    assert audit[0][0] == gr.ACT_APPLIED and audit[0][2]["spec_sha256"] == _spec_sha(store)
    rows = {a[2]["external_id"]: a for a in audit[1:]}
    assert set(rows) == {"GL-C001", "GL-C010", "GL-I002", "GL-I004", "GL-C002"}  # verified_at 만 바뀐 행은 실행 기록·명세로
    assert rows["GL-I002"][2]["before"]["term_target"] == "ingredient english 2"
    result = json.loads(store.get(gr._key(RUN, "result")))
    assert result["status"] == "applied" and result["inserted"]["GL-C002"] == after["GL-C002"]


@pytest.mark.parametrize("case", ["wrong_sha", "tampered", "code", "file"])
def test_apply_stops_without_write_when_inputs_differ(ready, store, tmp_path, case):
    url, files = ready
    assert _plan(url, files) == 0
    before = _snapshot(url)
    sha = _spec_sha(store)
    if case == "wrong_sha":
        rc = _apply(url, files, store, sha="a" * 64)
    elif case == "tampered":
        store.objects[gr._key(RUN, "spec")] = store.objects[gr._key(RUN, "spec")] + b" "
        rc = _apply(url, files, store, sha=sha)
    elif case == "code":
        rc = gr.main(["apply", "--run-id", RUN, "--code-sha", "other", "--spec-sha", sha, *_args(files)], database_url=url)
    else:
        changed = dict(files, ingredient=str(write_book(tmp_path / "x.xlsx", "ingredient", [base_row("ingredient", 1)])))
        rc = _apply(url, changed, store, sha=sha)
    assert rc == 1 and _snapshot(url) == before and _audit(url) == 0


@pytest.mark.parametrize("sql", [
    "UPDATE glossary SET note = '끼어듦' WHERE external_id = 'GL-I003'",
    "UPDATE glossary SET note = '끼어듦' WHERE external_id = 'GL-M001'",
    "UPDATE glossary SET note = '끼어듦' WHERE external_id = 'GL-I002'",  # 갱신 대상: 적용 뒤 검증으로는 잡히지 않는다
    "UPDATE glossary SET note = '끼어듦' WHERE external_id = 'GL-I001'",
    "UPDATE glossary SET note = '끼어듦' WHERE external_id = 'GL-I004'",  # 빠질 행
    "INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, term_kind, version) "
    "VALUES ('끼어듦', 'x', 'en', 'common', 'enforced', 'GL-I099', 'ingredient', 1)",
])
def test_apply_rechecks_after_lock(ready, store, sql):
    url, files = ready
    assert _plan(url, files) == 0
    _q(url, sql)
    before = _snapshot(url)
    assert _apply(url, files, store) == 1
    assert _snapshot(url) == before and _audit(url) == 0


def test_audit_failure_rolls_back_all_changes(ready, store, monkeypatch):
    url, files = ready
    assert _plan(url, files) == 0
    before = _snapshot(url)

    def boom(*a, **k):
        raise RuntimeError("감사 실패")

    monkeypatch.setattr(gr, "_insert_audit", boom)
    assert _apply(url, files, store) == 1
    assert _snapshot(url) == before and _audit(url) == 0
    assert not store.exists(gr._key(RUN, "result"))


def test_post_write_mismatch_rolls_back(ready, store, monkeypatch):
    url, files = ready
    assert _plan(url, files) == 0
    before = _snapshot(url)
    real = gr._matches
    monkeypatch.setattr(gr, "_matches", lambda *a: ["id=1 강제 불일치"])
    assert _apply(url, files, store) == 1
    monkeypatch.setattr(gr, "_matches", real)
    assert _snapshot(url) == before and _audit(url) == 0


# ── 결과 파일 ─────────────────────────────────────────────────────────────

def test_result_save_failure_is_reported_and_resumed_without_db_change(ready, store, monkeypatch, capsys):
    url, files = ready
    assert _plan(url, files) == 0
    sha = _spec_sha(store)
    real = store.put_if_absent

    def fail_result(key, *a, **k):
        if key.endswith("/result.json"):
            raise OSError("저장 실패")
        return real(key, *a, **k)

    monkeypatch.setattr(store, "put_if_absent", fail_result)
    capsys.readouterr()
    assert _apply(url, files, store, sha=sha) == gr.EXIT_RESULT_PENDING
    assert "DB 적용 완료 · 결과 파일 저장 미완료" in capsys.readouterr().out
    after, audit = _snapshot(url), _audit(url)
    assert _row(url, "GL-I004").retired_at is not None
    monkeypatch.setattr(store, "put_if_absent", real)
    assert _apply(url, files, store, sha=sha) == 0
    assert "이미 적용된 실행" in capsys.readouterr().out
    assert _snapshot(url) == after and _audit(url) == audit and store.exists(gr._key(RUN, "result"))


def test_result_key_with_other_content_is_not_overwritten(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    store.objects[gr._key(RUN, "result")] = b"other"
    assert _apply(url, files, store) == gr.EXIT_RESULT_PENDING
    assert store.get(gr._key(RUN, "result")) == b"other"
    assert _row(url, "GL-I004").retired_at is not None


# ── 커밋 응답 유실 · 반복 · 동시 실행 ─────────────────────────────────────

def test_commit_response_lost_after_commit(ready, store, monkeypatch, capsys):
    url, files = ready
    assert _plan(url, files) == 0

    def lost(trans):
        trans.commit()
        raise OperationalError("commit", {}, Exception("connection lost"))

    real_commit = gr._commit
    monkeypatch.setattr(gr, "_commit", lost)
    assert _apply(url, files, store) == gr.EXIT_UNKNOWN
    assert not store.exists(gr._key(RUN, "result"))
    capsys.readouterr()
    rc, out = _status(url, store, capsys=capsys)
    assert rc == 0 and "applied" in out
    monkeypatch.setattr(gr, "_commit", real_commit)
    after, audit = _snapshot(url), _audit(url)
    assert _apply(url, files, store) == 0
    assert _snapshot(url) == after and _audit(url) == audit and store.exists(gr._key(RUN, "result"))


def test_commit_response_lost_without_commit(ready, store, monkeypatch, capsys):
    url, files = ready
    assert _plan(url, files) == 0
    before = _snapshot(url)

    def lost(trans):
        trans.rollback()
        raise OperationalError("commit", {}, Exception("connection lost"))

    real_commit = gr._commit
    monkeypatch.setattr(gr, "_commit", lost)
    assert _apply(url, files, store) == gr.EXIT_UNKNOWN
    capsys.readouterr()
    rc, out = _status(url, store, capsys=capsys)
    assert rc == 0 and "not_applied" in out and _snapshot(url) == before
    monkeypatch.setattr(gr, "_commit", real_commit)
    assert _apply(url, files, store) == 0
    assert _q(url, "SELECT count(*) FROM audit_log WHERE action_type = :a", a=gr.ACT_APPLIED)[0][0] == 1


def test_repeat_apply_does_not_change_db(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    after, audit = _snapshot(url), _audit(url)
    assert _apply(url, files, store) == 0
    assert _snapshot(url) == after and _audit(url) == audit


def test_concurrent_apply_changes_once(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    sha = _spec_sha(store)
    results = []
    barrier = threading.Barrier(3)

    def run():
        barrier.wait()
        results.append(_apply(url, files, store, sha=sha))

    threads = [threading.Thread(target=run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [0, 0, 0]
    assert _q(url, "SELECT count(*) FROM audit_log WHERE action_type = :a", a=gr.ACT_APPLIED)[0][0] == 1
    assert _q(url, "SELECT count(*) FROM glossary WHERE external_id = 'GL-C002'")[0][0] == 1


def test_applied_record_with_different_db_is_unknown_and_not_changed(ready, store, capsys):
    url, files = ready
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    _q(url, "UPDATE glossary SET note = '이후 수정' WHERE external_id = 'GL-I003'")
    before = _snapshot(url)
    assert _apply(url, files, store) == gr.EXIT_UNKNOWN
    assert _snapshot(url) == before
    capsys.readouterr()
    assert _status(url, store, capsys=capsys)[0] == gr.EXIT_UNKNOWN


def test_duplicate_run_records_are_unknown(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    _q(url, "INSERT INTO audit_log (actor_type, action_type, detail) SELECT actor_type, action_type, detail FROM audit_log "
            "WHERE action_type = :a", a=gr.ACT_APPLIED)
    before = _snapshot(url)
    assert _apply(url, files, store) == gr.EXIT_UNKNOWN
    assert _snapshot(url) == before


# ── 예외 범위 ─────────────────────────────────────────────────────────────

def _plan_rc(ready, store, tmp_path, monkeypatch, *, cert=None, ing=None, approve=True):
    url, _ = ready
    files = _new_files(tmp_path, cert=cert, ing=ing)
    monkeypatch.undo()
    if approve:
        _approve(monkeypatch, files)
    return _plan(url, files)


OK_ING = [base_row("ingredient", 1), base_row("ingredient", 2), base_row("ingredient", 3), base_row("ingredient", 4)]
OK_CERT = [base_row("certification", 1), base_row("certification", 10, internal_category=SUN)]


def test_verified_at_exception_requires_approved_file_hash(ready, store, tmp_path, monkeypatch):
    ing = [base_row("ingredient", 1, verified_at="2026-11-06"), *OK_ING[1:]]
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=OK_CERT, ing=ing, approve=False) == 1


def test_verified_at_exception_only_for_ingredient(ready, store, tmp_path, monkeypatch):
    cert = [base_row("certification", 1, verified_at="2026-11-06"), OK_CERT[1]]
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=cert, ing=OK_ING) == 1


@pytest.mark.parametrize("over", [{"note": "다름"}, {"source": "다름"}, {"target_text": "다름"},
                                  {"verified_at": "2026-11-06", "note": "다름"}])
def test_same_version_other_change_stops_even_with_approved_hash(ready, store, tmp_path, monkeypatch, over):
    ing = [base_row("ingredient", 1, **over), *OK_ING[1:]]
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=OK_CERT, ing=ing) == 1


def test_category_change_limited_to_approved_row_and_values(ready, store, tmp_path, monkeypatch):
    cert = [base_row("certification", 1, version=2, internal_category="G01"), OK_CERT[1]]
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=cert, ing=OK_ING) == 1


def test_category_change_to_other_value_stops(ready, store, tmp_path, monkeypatch):
    cert = [OK_CERT[0], base_row("certification", 10, version=2, internal_category="G04")]
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=cert, ing=OK_ING) == 1


def test_category_change_needs_approved_hash(ready, store, tmp_path, monkeypatch):
    cert = [OK_CERT[0], base_row("certification", 10, version=2, internal_category="common")]
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=cert, ing=OK_ING, approve=False) == 1


@pytest.mark.parametrize("ing", [
    [base_row("ingredient", 1, version=2, source_ko="바뀐 원문"), *OK_ING[1:]],
    [base_row("ingredient", 5, source_ko="ingredient-한국어-1"), *OK_ING[1:]],
])
def test_source_change_and_natural_key_collision_stop(ready, store, tmp_path, monkeypatch, ing):
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=OK_CERT, ing=ing) == 1


def test_version_decrease_stops(ready, store, tmp_path, monkeypatch):
    url, _ = ready
    _q(url, "UPDATE glossary SET version = 3 WHERE external_id = 'GL-I001'")
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=OK_CERT, ing=OK_ING) == 1


def test_missing_external_id_row_stops(ready, store, tmp_path, monkeypatch):
    url, _ = ready
    _q(url, "INSERT INTO glossary (term_ko, term_target, target_lang, internal_category) VALUES ('옛값', 'x', 'en', 'common')")
    assert _plan_rc(ready, store, tmp_path, monkeypatch, cert=OK_CERT, ing=OK_ING) == 1


def test_retired_id_reappearing_stops_replace(ready, store, tmp_path, monkeypatch):
    url, files = ready
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    again = _new_files(tmp_path, ing=[base_row("ingredient", 4)])
    _approve(monkeypatch, again)
    assert _plan(url, again, run="run-2") == 1


def test_phrase_is_not_a_replace_target(head, store):
    with pytest.raises(SystemExit):
        gr.main(["plan", "--run-id", RUN, "--code-sha", CODE, "--phrase", "p.xlsx"], database_url=head)


# ── 일반 로더 ─────────────────────────────────────────────────────────────

def test_general_load_keeps_existing_checks(ready, store, tmp_path, capsys):
    url, files = ready
    before = _snapshot(url)
    capsys.readouterr()
    assert ingest(["load", "--ingredient", files["ingredient"]], database_url=url) == 1
    assert "같은 version" in capsys.readouterr().out
    assert ingest(["load", "--certification", files["certification"]], database_url=url) == 1
    assert "자연키가 바뀜" in capsys.readouterr().out
    assert _snapshot(url) == before


def test_general_load_stops_on_retired_id_and_does_not_count_it(ready, store, tmp_path, capsys):
    url, files = ready
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    old = write_book(tmp_path / "again.xlsx", "ingredient", [base_row("ingredient", 4)])
    before = _snapshot(url)
    capsys.readouterr()
    assert ingest(["load", "--ingredient", str(old)], database_url=url) == 1
    assert "활성에서 제외된 원본 ID" in capsys.readouterr().out
    assert _snapshot(url) == before
    assert ingest(["plan", "--ingredient", files["ingredient"]], database_url=url) == 0
    assert "이번 파일에 없는 기존 행(유지) 0" in capsys.readouterr().out


# ── 공급 ──────────────────────────────────────────────────────────────────

def _supply(url, monkeypatch, src, cats=("common",)):
    monkeypatch.setattr(downstream, "_glossary_categories", lambda db, sid: list(cats))
    eng = create_engine(url)
    try:
        with Session(eng) as db:
            supply, selection = downstream._glossary_supply(db, "en", 1, [{"id": 1, "source_ko": src}])
    finally:
        eng.dispose()
    return supply, selection


def test_supply_excludes_retired_and_is_unchanged_when_no_row_is_retired(ready, store, monkeypatch):
    url, files = ready
    src = "ingredient-한국어-4 그리고 ingredient-한국어-3 기존 문구"
    first, _ = _supply(url, monkeypatch, src)
    ids = {t["term_ko"] for t in first["terms"]}
    assert {"ingredient-한국어-4", "ingredient-한국어-3", "기존 문구"} <= ids
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    after, _ = _supply(url, monkeypatch, src)
    assert first["status"] == after["status"] == "ok"
    assert {t["term_ko"] for t in after["terms"]} == {"ingredient-한국어-3", "기존 문구"}


def test_verified_at_only_change_keeps_supply_fingerprint(ready, store, monkeypatch):
    url, files = ready
    src = "ingredient-한국어-1"
    first, _ = _supply(url, monkeypatch, src)
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    after, _ = _supply(url, monkeypatch, src)
    assert first == after


def test_middle_state_new_certification_and_ingredient_with_old_phrase(ready, store, monkeypatch):
    # 실제 운영될 중간 상태: 새 성분·새 인증 + 기존 문구. 분류 NULL 작업은 common 만 받는다.
    url, files = ready
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    src = "certification-한국어-10 새 인증 기존 문구 ingredient-한국어-2"
    supply, selection = _supply(url, monkeypatch, src)
    assert supply["status"] == "ok" and selection["categories"] == ["common"]
    by = {t["term_ko"]: t for t in supply["terms"]}
    assert by["certification-한국어-10"]["term_target"] == "certification new 10"  # common 으로 바뀌어 공급
    assert by["ingredient-한국어-2"]["term_target"] == "ingredient new 2" and "새 인증" in by and "기존 문구" in by


# ── 복구 ──────────────────────────────────────────────────────────────────

def test_restore_returns_all_columns_and_retires_inserted_rows(ready, store):
    url, files = ready
    before = {r[0]: r[1] for r in _q(url, "SELECT external_id, g::text FROM glossary g")}
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    assert _restore(url, store) == 0
    now = {r[0]: r[1] for r in _q(url, "SELECT external_id, g::text FROM glossary g")}
    assert {k: now[k] for k in before} == before  # 전체 열 일치
    assert _row(url, "GL-C002").retired_at is not None  # 삽입 행은 지우지 않음
    assert json.loads(store.get(gr._key(RUN, "restore-result")))["status"] == "restored"
    audit, snap = _audit(url), _snapshot(url)
    assert _restore(url, store) == 0  # 반복해도 DB 불변
    assert _audit(url) == audit and _snapshot(url) == snap


def test_restore_stops_when_values_changed_after_apply(ready, store):
    url, files = ready
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    _q(url, "UPDATE glossary SET note = '이후 수정' WHERE external_id = 'GL-I002'")
    snap, audit = _snapshot(url), _audit(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == snap and _audit(url) == audit


def test_restore_stops_when_new_flag_refers_target(ready, store):
    url, files = ready
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    pk = _row(url, "GL-I002").id
    eng = create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(text("SET session_replication_role = replica"))  # 시험용: 다른 FK 없이 행 하나만 넣는다
            c.execute(text("INSERT INTO text_block (section_id, block_order, compliance_flags) VALUES (1, 0, CAST(:f AS jsonb))"),
                      {"f": json.dumps([{"type": "mandatory_term_unapplied", "glossary_id": str(pk)}])})
    finally:
        eng.dispose()
    snap = _snapshot(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == snap


def _flag_block(url, flags, *, revision=0):
    eng = create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(text("SET session_replication_role = replica"))  # 시험용: FK 없이 블록 행만 만든다
            return c.execute(text("INSERT INTO text_block (section_id, block_order, revision, compliance_flags) "
                                  "VALUES (1, 0, :r, CAST(:f AS jsonb)) RETURNING id"),
                             {"r": revision, "f": json.dumps(flags)}).scalar_one()
    finally:
        eng.dispose()


def _flag(pk, **over):
    return {"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": 3, "glossary_id": str(pk), **over}


def _planned_with_flag(url, files, store):
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_flag(pk)], revision=3)
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    return pk, block


def test_restore_passes_when_flag_items_are_unchanged(ready, store):
    url, files = ready
    pk, block = _planned_with_flag(url, files, store)
    spec = json.loads(store.get(gr._key(RUN, "spec")))
    assert spec["flag_refs"] == [{"block_id": block, "revision": 3, "flag": _flag(pk)}]
    assert _restore(url, store) == 0


@pytest.mark.parametrize("change", ["revision", "content", "moved_block", "other_pk"])
def test_restore_stops_when_flag_item_changed_with_same_count(ready, store, change):
    url, files = ready
    pk, block = _planned_with_flag(url, files, store)
    if change == "revision":
        _q(url, "UPDATE text_block SET revision = 4 WHERE id = :b", b=block)
    elif change == "content":
        _q(url, "UPDATE text_block SET compliance_flags = CAST(:f AS jsonb) WHERE id = :b",
           f=json.dumps([_flag(pk, producer="다른 생산자")]), b=block)
    elif change == "moved_block":
        _q(url, "UPDATE text_block SET compliance_flags = NULL WHERE id = :b", b=block)
        _flag_block(url, [_flag(pk)], revision=3)
    else:
        other = _row(url, "GL-I003").id
        _q(url, "UPDATE text_block SET compliance_flags = CAST(:f AS jsonb) WHERE id = :b",
           f=json.dumps([_flag(other)]), b=block)
    spec = json.loads(store.get(gr._key(RUN, "spec")))
    assert len(spec["flag_refs"]) == 1  # 건수는 같다
    snap, audit = _snapshot(url), _audit(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == snap and _audit(url) == audit


def test_apply_stops_when_flag_item_changed_after_plan(ready, store):
    url, files = ready
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_flag(pk)], revision=3)
    assert _plan(url, files) == 0
    _q(url, "UPDATE text_block SET revision = 4 WHERE id = :b", b=block)
    before = _snapshot(url)
    assert _apply(url, files, store) == 1
    assert _snapshot(url) == before and _audit(url) == 0


# ── flag 참조: 숫자형(계약 예시) · 문자열(공급 목록) glossary_id ─────────────

FORMS = {"number": int, "string": str}
form_cases = pytest.mark.parametrize("form", list(FORMS))
change_cases = pytest.mark.parametrize("change", ["revision", "content", "moved_block", "other_pk"])


def _typed(form, pk, **over):
    return _flag(pk, **{"glossary_id": FORMS[form](pk), **over})


def _spec(store):
    return json.loads(store.get(gr._key(RUN, "spec")))


def _set_flags(url, block, flags):
    _q(url, "UPDATE text_block SET compliance_flags = CAST(:f AS jsonb) WHERE id = :b", f=json.dumps(flags), b=block)


def _raw_flags(url, block):
    return _q(url, "SELECT compliance_flags::text FROM text_block WHERE id = :b", b=block)[0][0]


def _ref_count(url, pks):
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return len(gr._flag_refs(c, pks))
    finally:
        eng.dispose()


def _change_flag(url, form, pk, block, change):
    """건수를 유지한 채 항목 하나를 바꾼다."""
    if change == "revision":
        _q(url, "UPDATE text_block SET revision = 4 WHERE id = :b", b=block)
    elif change == "content":
        _set_flags(url, block, [_typed(form, pk, producer="다른 생산자")])
    elif change == "moved_block":
        _q(url, "UPDATE text_block SET compliance_flags = NULL WHERE id = :b", b=block)
        _flag_block(url, [_typed(form, pk)], revision=3)
    else:
        _set_flags(url, block, [_typed(form, _row(url, "GL-I003").id)])


@form_cases
def test_flag_refs_collect_each_form_and_keep_original(ready, store, form):
    url, files = ready
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_typed(form, pk)], revision=3)
    raw = _raw_flags(url, block)
    assert _plan(url, files) == 0
    refs = _spec(store)["flag_refs"]
    assert refs == [{"block_id": block, "revision": 3, "flag": _typed(form, pk)}]
    assert type(refs[0]["flag"]["glossary_id"]) is FORMS[form]  # 저장 형식 유지
    assert _raw_flags(url, block) == raw  # DB 에 저장된 flag 도 바꾸지 않는다


def test_flag_refs_collect_both_forms_of_same_pk_separately(ready, store):
    url, files = ready
    pk = _row(url, "GL-I002").id
    _flag_block(url, [_typed("number", pk), _typed("string", pk)], revision=3)
    assert _plan(url, files) == 0
    ids = [r["flag"]["glossary_id"] for r in _spec(store)["flag_refs"]]
    assert len(ids) == 2 and {type(i) for i in ids} == {int, str} and {str(i) for i in ids} == {str(pk)}


def test_flag_refs_identify_integer_valued_float_as_number_and_keep_original(ready, store):
    url, files = ready
    pk = _row(url, "GL-I002").id
    _flag_block(url, [_flag(pk, glossary_id=float(pk))], revision=3)
    assert _plan(url, files) == 0
    ref = _spec(store)["flag_refs"][0]["flag"]["glossary_id"]
    assert type(ref) is float and ref == pk


@pytest.mark.parametrize("value", [lambda pk: f"0{pk}", lambda pk: f" {pk}", lambda pk: f"{pk}.0", lambda pk: pk + 0.5,
                                   lambda pk: True, lambda pk: [pk], lambda pk: {"id": pk}, lambda pk: "G-2"])
def test_flag_refs_do_not_identify_other_values_as_pk(ready, store, value):
    url, files = ready
    pk = _row(url, "GL-I002").id
    _flag_block(url, [_flag(pk, glossary_id=value(pk))], revision=3)
    assert _plan(url, files) == 0
    assert _spec(store)["flag_refs"] == []


@form_cases
def test_flag_refs_ignore_non_target_pk_in_each_form(ready, store, form):
    url, files = ready
    _flag_block(url, [_typed(form, _row(url, "GL-M001").id)], revision=3)  # 문구 행은 이번 교체 대상이 아니다
    assert _plan(url, files) == 0
    assert _spec(store)["flag_refs"] == []


@form_cases
@change_cases
def test_apply_stops_when_flag_item_changed_after_plan_each_form(ready, store, form, change):
    url, files = ready
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_typed(form, pk)], revision=3)
    assert _plan(url, files) == 0
    spec = _spec(store)
    _change_flag(url, form, pk, block, change)
    assert _ref_count(url, [r["id"] for r in spec["before"]]) == len(spec["flag_refs"]) == 1  # 건수는 같다
    before = _snapshot(url)
    assert _apply(url, files, store) == 1
    assert _snapshot(url) == before and _audit(url) == 0


@form_cases
def test_apply_stops_when_new_reference_added_after_plan_each_form(ready, store, form):
    url, files = ready
    assert _plan(url, files) == 0
    _flag_block(url, [_typed(form, _row(url, "GL-I002").id)], revision=3)
    before = _snapshot(url)
    assert _apply(url, files, store) == 1
    assert _snapshot(url) == before and _audit(url) == 0


@form_cases
@change_cases
def test_restore_stops_when_flag_item_changed_after_apply_each_form(ready, store, form, change):
    url, files = ready
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_typed(form, pk)], revision=3)
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    spec = _spec(store)
    _change_flag(url, form, pk, block, change)
    assert _ref_count(url, [r["id"] for r in spec["before"]]) == len(spec["flag_refs"]) == 1  # 건수는 같다
    snap, audit = _snapshot(url), _audit(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == snap and _audit(url) == audit


@form_cases
def test_restore_stops_when_new_reference_added_after_apply_each_form(ready, store, form):
    url, files = ready
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    _flag_block(url, [_typed(form, _row(url, "GL-I002").id)], revision=3)
    snap, audit = _snapshot(url), _audit(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == snap and _audit(url) == audit


@form_cases
def test_restore_stops_when_inserted_row_gets_reference_each_form(ready, store, form):
    url, files = ready
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    _flag_block(url, [_typed(form, _row(url, "GL-C002").id)], revision=3)  # 이번 교체로 삽입된 행
    snap, audit = _snapshot(url), _audit(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == snap and _audit(url) == audit


@form_cases
def test_unchanged_flag_passes_apply_and_restore_each_form(ready, store, form):
    url, files = ready
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_typed(form, pk)], revision=3)
    raw = _raw_flags(url, block)
    assert _plan(url, files) == 0
    assert _apply(url, files, store) == 0
    assert _restore(url, store) == 0
    assert _raw_flags(url, block) == raw  # 적용 · 복구 어디서도 flag 를 고치지 않는다
    assert _row(url, "GL-I002").version == 1


@pytest.mark.parametrize("form,other", [("number", "string"), ("string", "number")])
def test_flag_form_change_alone_stops_apply_and_restore(ready, store, form, other):
    url, files = ready
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_typed(form, pk)], revision=3)
    assert _plan(url, files) == 0
    _set_flags(url, block, [_typed(other, pk)])
    before = _snapshot(url)
    assert _apply(url, files, store) == 1  # 같은 PK 라도 JSON 값의 형이 달라지면 변경이다
    assert _snapshot(url) == before and _audit(url) == 0
    _set_flags(url, block, [_typed(form, pk)])
    assert _apply(url, files, store) == 0
    _set_flags(url, block, [_typed(other, pk)])
    snap, audit = _snapshot(url), _audit(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == snap and _audit(url) == audit


@form_cases
@pytest.mark.parametrize("over,changed", [({"revision": 3}, {"revision": 3.0}), ({"resolved": 1}, {"resolved": True})])
def test_restore_compares_canonical_json_not_python_equality(ready, store, form, over, changed):
    url, files = ready
    pk = _row(url, "GL-I002").id
    block = _flag_block(url, [_typed(form, pk, **over)], revision=3)
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0
    _set_flags(url, block, [_typed(form, pk, **changed)])
    snap, audit = _snapshot(url), _audit(url)
    assert _restore(url, store) == 1  # 파이썬 == 로는 같지만 정규 직렬화가 다르다
    assert _snapshot(url) == snap and _audit(url) == audit


def test_restore_without_apply_and_apply_after_restore_stop(ready, store):
    url, files = ready
    assert _plan(url, files) == 0
    before = _snapshot(url)
    assert _restore(url, store) == 1
    assert _snapshot(url) == before
    assert _apply(url, files, store) == 0 and _restore(url, store) == 0
    snap = _snapshot(url)
    assert _apply(url, files, store) == 1
    assert _snapshot(url) == snap


def test_restore_commit_lost_then_resume(ready, store, monkeypatch):
    url, files = ready
    assert _plan(url, files) == 0 and _apply(url, files, store) == 0

    def lost(trans):
        trans.commit()
        raise OperationalError("commit", {}, Exception("connection lost"))

    real_commit = gr._commit
    monkeypatch.setattr(gr, "_commit", lost)
    assert _restore(url, store) == gr.EXIT_UNKNOWN
    monkeypatch.setattr(gr, "_commit", real_commit)
    snap = _snapshot(url)
    assert _restore(url, store) == 0
    assert _snapshot(url) == snap and store.exists(gr._key(RUN, "restore-result"))


# ── 마이그레이션 ──────────────────────────────────────────────────────────

def test_migration_keeps_values_and_downgrade_stops_with_retired_rows(db_url, tmp_path):  # noqa: F811
    command.upgrade(_alembic(), "0011")
    _q(db_url, "INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id) "
               "VALUES ('행', 'x', 'en', 'common', 'enforced', 'GL-I001')")
    before = _q(db_url, "SELECT id, term_ko, external_id FROM glossary")
    command.upgrade(_alembic(), "0012")
    assert _q(db_url, "SELECT id, term_ko, external_id FROM glossary") == before
    assert _q(db_url, "SELECT retired_at FROM glossary")[0][0] is None
    cons = {r[0] for r in _q(db_url, "SELECT conname FROM pg_constraint WHERE conrelid = 'glossary'::regclass")}
    assert {"uq_glossary_external_id", "uq_glossary_natural_key"} <= cons
    _q(db_url, "UPDATE glossary SET retired_at = now()")
    with pytest.raises(Exception, match="활성에서 제외된"):
        command.downgrade(_alembic(), "0011")
    _q(db_url, "UPDATE glossary SET retired_at = NULL")
    command.downgrade(_alembic(), "0011")
    assert _q(db_url, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'glossary' AND column_name = 'retired_at'")[0][0] == 0


# ── 실제 전달본 ───────────────────────────────────────────────────────────

def _final_files():
    d = os.getenv("GLOSSARY_REPLACE_FINAL_DIR")
    if not d:
        return None
    files = {k: os.path.join(d, f"{k}.xlsx") for k in gr.KINDS}
    return files if all(os.path.exists(p) for p in files.values()) else None


@pytest.mark.skipif(_final_files() is None or _real_files() is None, reason="실제 전달본 경로 없음")
def test_real_final_files_over_0928_state(head, store, monkeypatch, capsys):
    url = head
    for kind, p in _real_files().items():
        assert ingest(["load", f"--{kind}", p], database_url=url) == 0
    files = _final_files()
    assert {_sha(files[k])[:8] for k in gr.KINDS} == {"1d4c6812", "bd7aae6a"}
    phrase = _q(url, "SELECT g::text FROM glossary g WHERE external_id LIKE 'GL-M%' ORDER BY id")
    original = _snapshot(url)
    assert _plan(url, files) == 0
    spec = json.loads(store.get(gr._key(RUN, "spec")))
    assert gr._counts(spec) == {"inserts": 12, "retires": 8, "unchanged": 0, "updates": 20580,
                                "updates_category_change": 2, "updates_verified_at_only": 20540, "updates_version_up": 38}
    assert sorted(r["external_id"] for r in spec["retires"]) == ["GL-I02635", "GL-I02636", "GL-I02637", "GL-I02639",
                                                                 "GL-I04885", "GL-I04886", "GL-I04887", "GL-I18720"]
    assert _apply(url, files, store) == 0
    assert _q(url, "SELECT count(*) FROM glossary WHERE retired_at IS NULL AND external_id LIKE 'GL-I%'")[0][0] == 20558
    assert _q(url, "SELECT count(*) FROM glossary WHERE retired_at IS NULL AND external_id LIKE 'GL-C%'")[0][0] == 34
    assert _q(url, "SELECT g::text FROM glossary g WHERE external_id LIKE 'GL-M%' ORDER BY id") == phrase
    assert _row(url, "GL-C010").internal_category == "common"
    # 중간 상태: 새 성분·새 인증 + 문구 v2 의 common 조회에 충돌이 없다
    terms = [r[0] for r in _q(url, "SELECT term_ko FROM glossary WHERE retired_at IS NULL AND internal_category = 'common' "
                                   "AND external_id IN ('GL-C010','GL-C009','GL-C012','GL-C026')")]
    supply, _ = _supply(url, monkeypatch, " ".join(terms))
    assert supply["status"] == "ok" and {t["term_ko"] for t in supply["terms"]} >= set(terms)
    assert _apply(url, files, store) == 0  # 반복해도 DB 불변
    assert _restore(url, store) == 0
    now = [r for r in _snapshot(url)]
    inserted = {r[0] for r in _q(url, "SELECT g::text FROM glossary g WHERE retired_at IS NOT NULL")}
    assert [r for r in now if r not in inserted] == original
    assert len(inserted) == 12
