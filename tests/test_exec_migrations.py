"""0007(is_excluded 3값) · 0008(실행·시도·인계 제약) — 실제 PostgreSQL에서 제약이 막는 것을 확인한다."""
import json

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.exec_db import alembic_config, fresh_database, requires_db, seed_job

pytestmark = requires_db

FP = "a" * 64


@pytest.fixture(scope="module")
def url():
    with fresh_database() as u:
        yield u


@pytest.fixture
def conn(url):
    eng = create_engine(url)
    c = eng.connect()
    tx = c.begin()
    yield c
    tx.rollback()
    c.close()
    eng.dispose()


def _savepoint_fails(conn, sql, params=None, exc=(IntegrityError, DBAPIError)):
    sp = conn.begin_nested()
    with pytest.raises(exc):
        conn.execute(text(sql), params or {})
    sp.rollback()


def _run(conn, job, kind="downstream", task_type="translate", status="running"):
    return conn.execute(
        text(
            "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, run_kind, run_scope) "
            "VALUES (:j, :tt, 'job', :j, :st, :k, '{}'::jsonb) RETURNING id"
        ),
        {"j": job, "tt": task_type, "st": status, "k": kind},
    ).scalar()


def _attempt(conn, job, run, *, stage="label", task_type="section", unit_type="section", unit_id=1, attempt_no=1,
             supersedes=None, current=True, status="pending", retry_count=0):
    return conn.execute(
        text(
            "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, parent_task_id, stage, attempt_no, "
            "is_current, supersedes_task_id, input_manifest, input_fingerprint, target_count, retry_count) "
            "VALUES (:j, :tt, :ut, :u, :st, :p, :stage, :n, :cur, :sup, '{}'::jsonb, :fp, 0, :rc) RETURNING id"
        ),
        {"j": job, "tt": task_type, "ut": unit_type, "u": unit_id, "st": status, "p": run, "stage": stage, "n": attempt_no,
         "cur": current, "sup": supersedes, "fp": FP, "rc": retry_count},
    ).scalar()


# ---- 0007 -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "label,logo,expected",
    [(True, None, True), (False, True, True), (False, False, False), (None, None, None), (False, None, None), (None, False, None),
     (None, True, True)],
)
def test_is_excluded_is_three_valued_or(conn, label, logo, expected):
    ids = seed_job(conn)
    sec = conn.execute(
        text("INSERT INTO section (job_id, source_image_id, section_order) VALUES (:j, 0, 1) RETURNING id"), {"j": ids["job"]}
    ).scalar()
    got = conn.execute(
        text(
            "INSERT INTO text_block (section_id, block_order, is_product_label, is_brand_logo) "
            "VALUES (:s, 1, :l, :g) RETURNING is_excluded"
        ),
        {"s": sec, "l": label, "g": logo},
    ).scalar()
    assert got is expected


def test_0007_downgrade_restores_old_expression():
    with fresh_database("0007") as u:
        command.downgrade(alembic_config(), "0006")
        eng = create_engine(u)
        with eng.begin() as c:
            ids = seed_job(c)
            sec = c.execute(text("INSERT INTO section (job_id, source_image_id, section_order) VALUES (:j, 0, 1) RETURNING id"),
                            {"j": ids["job"]}).scalar()
            got = c.execute(text("INSERT INTO text_block (section_id, block_order) VALUES (:s, 1) RETURNING is_excluded"),
                            {"s": sec}).scalar()
        eng.dispose()
        assert got is False  # 0003 식: NULL → false


# ---- 0008: 대표·시도 -----------------------------------------------------------------------------------------
def test_new_rows_must_be_version_1(conn):
    ids = seed_job(conn)
    _savepoint_fails(
        conn,
        "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, execution_schema_version, run_kind, run_scope) "
        "VALUES (:j, 'ocr', 'job', :j, 'pending', 0, 'analysis', '{}'::jsonb)",
        {"j": ids["job"]},
    )


def test_legacy_shape_insert_is_rejected(conn):
    # 0008 이전 코드처럼 대표/시도 필드 없이 넣으면 K4 위반
    ids = seed_job(conn)
    _savepoint_fails(
        conn,
        "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status) VALUES (:j, 'render', 'job', :j, 'pending')",
        {"j": ids["job"]},
    )


def test_u1_one_active_run_per_kind(conn):
    ids = seed_job(conn)
    _run(conn, ids["job"])
    _savepoint_fails(
        conn,
        "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, run_kind, run_scope) "
        "VALUES (:j, 'translate', 'job', :j, 'pending', 'downstream', '{}'::jsonb)",
        {"j": ids["job"]},
    )
    # 다른 종류·종료된 실행은 허용
    _run(conn, ids["job"], kind="analysis", task_type="ocr")
    _run(conn, ids["job"], status="cancelled")


def test_run_kind_task_type_mapping(conn):
    ids = seed_job(conn)
    _savepoint_fails(
        conn,
        "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, run_kind, run_scope) "
        "VALUES (:j, 'render', 'job', :j, 'pending', 'analysis', '{}'::jsonb)",
        {"j": ids["job"]},
    )


def test_u2_u3_and_supersedes_chain(conn):
    ids = seed_job(conn)
    run = _run(conn, ids["job"])
    a1 = _attempt(conn, ids["job"], run, status="failed")
    # 같은 논리 작업의 현재 시도 2개 금지(U2)
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):
        _attempt(conn, ids["job"], run, attempt_no=2, supersedes=a1)
    sp.rollback()
    conn.execute(text("UPDATE job_async_task SET is_current = false WHERE id = :a"), {"a": a1})
    a2 = _attempt(conn, ids["job"], run, attempt_no=2, supersedes=a1, retry_count=1)
    assert a2
    # 대체된 시도를 현재로 되돌릴 수 없음
    _savepoint_fails(conn, "UPDATE job_async_task SET is_current = true WHERE id = :a", {"a": a1})
    # supersedes는 직전 시도만
    conn.execute(text("UPDATE job_async_task SET is_current = false WHERE id = :a"), {"a": a2})
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError):
        _attempt(conn, ids["job"], run, attempt_no=3, supersedes=a1)
    sp.rollback()


def test_k1_attempt_requires_fields_and_stage_mapping(conn):
    ids = seed_job(conn)
    run = _run(conn, ids["job"])
    _savepoint_fails(
        conn,
        "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, parent_task_id, stage, attempt_no, "
        "input_manifest, input_fingerprint) VALUES (:j, 'section', 'section', 1, 'pending', :p, 'label', 1, '{}'::jsonb, :fp)",
        {"j": ids["job"], "p": run, "fp": FP},
    )  # target_count 누락
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):
        _attempt(conn, ids["job"], run, stage="inpaint", task_type="section")  # inpaint ↔ inpaint만
    sp.rollback()
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError):
        _attempt(conn, ids["job"], run, stage="analyze", task_type="ocr", unit_type="job", unit_id=ids["job"])  # 실행 종류 불일치
    sp.rollback()


def test_parent_must_be_same_job_run(conn):
    a = seed_job(conn)
    b = seed_job(conn)
    run_a = _run(conn, a["job"])
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError):
        _attempt(conn, b["job"], run_a)
    sp.rollback()


def test_k2_skip_reason_only_on_done(conn):
    ids = seed_job(conn)
    run = _run(conn, ids["job"])
    a = _attempt(conn, ids["job"], run, status="running")
    _savepoint_fails(conn, "UPDATE job_async_task SET skip_reason = 'no_targets' WHERE id = :a", {"a": a})
    conn.execute(text("UPDATE job_async_task SET status = 'done', skip_reason = 'no_targets' WHERE id = :a"), {"a": a})


def test_identity_and_manifest_are_immutable(conn):
    ids = seed_job(conn)
    run = _run(conn, ids["job"])
    a = _attempt(conn, ids["job"], run)
    _savepoint_fails(conn, "UPDATE job_async_task SET input_manifest = CAST(:m AS jsonb) WHERE id = :a", {"a": a, "m": '{"x": 1}'})
    _savepoint_fails(conn, "UPDATE job_async_task SET retry_count = 5 WHERE id = :a", {"a": a})
    _savepoint_fails(conn, "UPDATE job_async_task SET lease_epoch = -1 WHERE id = :a", {"a": a})
    conn.execute(text("UPDATE job_async_task SET status = 'done' WHERE id = :a"), {"a": a})
    _savepoint_fails(conn, "UPDATE job_async_task SET status = 'running' WHERE id = :a", {"a": a})


def test_legacy_rows_keep_working(url):
    """0008 이전에 있던 행(버전 0)은 그대로 갱신할 수 있다."""
    with fresh_database("0007") as u:
        eng = create_engine(u)
        with eng.begin() as c:
            ids = seed_job(c)
            c.execute(text("INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status) "
                           "VALUES (:j, 'render', 'job', :j, 'failed')"), {"j": ids["job"]})
        command.upgrade(alembic_config(), "0008")
        with eng.begin() as c:
            v, = c.execute(text("SELECT execution_schema_version FROM job_async_task")).one()
            assert v == 0
            c.execute(text("UPDATE job_async_task SET status = 'pending', retry_count = retry_count + 1"))
        command.downgrade(alembic_config(), "0007")
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM job_async_task")).scalar() == 1
        eng.dispose()


# ---- 0008: grant · handoff · artifact ----------------------------------------------------------------------
def _grant(conn, job, task, epoch=1, worker="gpu-1", token=FP):
    return conn.execute(
        text(
            "INSERT INTO task_lease_grant (task_id, job_id, generation_epoch, worker_id, token_hash, initial_expires_at) "
            "VALUES (:t, :j, :e, :w, :h, now() + interval '1 minute') RETURNING id"
        ),
        {"t": task, "j": job, "e": epoch, "w": worker, "h": token},
    ).scalar()


def _handoff(conn, job, task, epoch=1, grant=None, outcome="done", state="received"):
    return conn.execute(
        text(
            "INSERT INTO task_handoff (task_id, job_id, lease_epoch, producer_grant_id, contract_version, input_fingerprint, "
            "outcome, target_count, payload, manifest_sha256, state) "
            "VALUES (:t, :j, :e, :g, '1', :fp, :o, 1, '{}'::jsonb, :fp, :st) RETURNING id"
        ),
        {"t": task, "j": job, "e": epoch, "g": grant, "fp": FP, "o": outcome, "st": state},
    ).scalar()


def test_grant_rules(conn):
    ids = seed_job(conn)
    run = _run(conn, ids["job"])
    a = _attempt(conn, ids["job"], run, stage="inpaint", task_type="inpaint")
    g = _grant(conn, ids["job"], a)
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):  # U7
        _grant(conn, ids["job"], a, worker="gpu-2")
    sp.rollback()
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError):  # 대표 행에는 발급 금지
        _grant(conn, ids["job"], run, epoch=1)
    sp.rollback()
    _savepoint_fails(conn, "UPDATE task_lease_grant SET token_hash = :h WHERE id = :g", {"h": "b" * 64, "g": g})
    _savepoint_fails(conn, "UPDATE task_lease_grant SET token_hash = NULL WHERE id = :g", {"g": g})  # 정리 시각 없이 NULL 금지
    conn.execute(text("UPDATE task_lease_grant SET token_hash = NULL, verification_purged_at = now() WHERE id = :g"), {"g": g})
    _savepoint_fails(conn, "UPDATE task_lease_grant SET worker_id = 'x' WHERE id = :g", {"g": g})


def test_handoff_u4_u5_k3_k6(conn):
    a_ids = seed_job(conn)
    b_ids = seed_job(conn)
    run = _run(conn, a_ids["job"])
    t = _attempt(conn, a_ids["job"], run, stage="inpaint", task_type="inpaint")
    g = _grant(conn, a_ids["job"], t, epoch=1)
    h1 = _handoff(conn, a_ids["job"], t, epoch=1, grant=g)
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):  # U4
        _handoff(conn, a_ids["job"], t, epoch=1, grant=g)
    sp.rollback()
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError):  # K6: 세대 불일치
        _handoff(conn, a_ids["job"], t, epoch=2, grant=g)
    sp.rollback()
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError):  # K3: 다른 job
        _handoff(conn, b_ids["job"], t, epoch=3)
    sp.rollback()
    conn.execute(text("UPDATE task_handoff SET state='adopted', adopted_at=now(), adopted_by='be', adopted_epoch=2 WHERE id=:h"),
                 {"h": h1})
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):  # U5
        _handoff(conn, a_ids["job"], t, epoch=4, state="adopted")
    sp.rollback()
    _savepoint_fails(conn, "UPDATE task_handoff SET state = 'rejected' WHERE id = :h", {"h": h1})
    _savepoint_fails(conn, "UPDATE task_handoff SET manifest_sha256 = :x WHERE id = :h", {"x": "c" * 64, "h": h1})


def test_artifact_k5_and_unique_verified_key(conn):
    ids = seed_job(conn)
    run = _run(conn, ids["job"])
    t = _attempt(conn, ids["job"], run, stage="inpaint", task_type="inpaint")
    h = _handoff(conn, ids["job"], t)

    def art(part, **kw):
        cols = {"handoff_id": h, "job_id": ids["job"], "kind": "background", "part_key": part, **kw}
        names = ", ".join(cols)
        vals = ", ".join(f":{k}" for k in cols)
        return conn.execute(text(f"INSERT INTO task_artifact ({names}) VALUES ({vals}) RETURNING id"), cols).scalar()

    a1 = art("s1")
    # 검증됨인데 키·측정값 없음 → K5
    _savepoint_fails(conn, "UPDATE task_artifact SET state = 'verified' WHERE id = :a", {"a": a1})
    conn.execute(text("UPDATE task_artifact SET state='verified', verified_key='verified/k1', byte_size=3, sha256=:s, "
                      "media_type='image/png' WHERE id=:a"), {"a": a1, "s": FP})
    _savepoint_fails(conn, "UPDATE task_artifact SET verified_key = 'verified/k2' WHERE id = :a", {"a": a1})
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):  # 다른 산출물이 같은 최종 키
        art("s2", verified_key="verified/k1")
    sp.rollback()
    sp = conn.begin_nested()
    with pytest.raises(IntegrityError):  # verified_key 와 source_ref 둘 다
        art("s3", state="verified", verified_key="verified/k3", source_ref=json.dumps({"x": 1}), byte_size=1, sha256=FP,
            media_type="image/png")
    sp.rollback()


def test_current_analysis_and_section_link(conn):
    a = seed_job(conn)
    b = seed_job(conn)
    run_a = _run(conn, a["job"], kind="analysis", task_type="ocr", status="done")
    down = _run(conn, a["job"])
    _savepoint_fails(conn, "UPDATE job SET current_analysis_task_id = :t WHERE id = :j", {"t": down, "j": a["job"]})
    _savepoint_fails(conn, "UPDATE job SET current_analysis_task_id = :t WHERE id = :j", {"t": run_a, "j": b["job"]})
    conn.execute(text("UPDATE job SET current_analysis_task_id = :t WHERE id = :j"), {"t": run_a, "j": a["job"]})
    sec = conn.execute(text("INSERT INTO section (job_id, source_image_id, section_order, analysis_task_id) "
                            "VALUES (:j, 0, 1, :t) RETURNING id"), {"j": a["job"], "t": run_a}).scalar()
    _savepoint_fails(conn, "UPDATE section SET analysis_task_id = NULL WHERE id = :s", {"s": sec})
    _savepoint_fails(conn, "INSERT INTO section (job_id, source_image_id, section_order, analysis_task_id) VALUES (:j, 0, 1, :t)",
                     {"j": b["job"], "t": run_a})


def test_job_delete_cascades_everything(conn):
    ids = seed_job(conn)
    run = _run(conn, ids["job"], kind="analysis", task_type="ocr", status="done")
    conn.execute(text("UPDATE job SET current_analysis_task_id = :t WHERE id = :j"), {"t": run, "j": ids["job"]})
    conn.execute(text("INSERT INTO section (job_id, source_image_id, section_order, analysis_task_id) VALUES (:j, 0, 1, :t)"),
                 {"j": ids["job"], "t": run})
    down = _run(conn, ids["job"])
    t = _attempt(conn, ids["job"], down, stage="inpaint", task_type="inpaint")
    g = _grant(conn, ids["job"], t)
    _handoff(conn, ids["job"], t, grant=g)
    conn.execute(text("DELETE FROM job WHERE id = :j"), {"j": ids["job"]})
    for table in ("job_async_task", "task_lease_grant", "task_handoff", "section"):
        assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE job_id = :j"), {"j": ids["job"]}).scalar() == 0


def test_full_downgrade_and_upgrade_roundtrip():
    with fresh_database() as u:
        command.downgrade(alembic_config(), "0006")
        command.upgrade(alembic_config(), "head")
        eng = create_engine(u)
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0008"
        eng.dispose()
