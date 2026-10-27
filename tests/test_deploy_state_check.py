"""deploy/bin/state_check.py — 인자 없는 기존 출력 유지, 유휴 관문, 실제 DB revision, 상태 지문(구현안 6판 2.4).

DB 는 PIXLATE_TEST_DATABASE_URL, Redis 는 PIXLATE_TEST_REDIS_URL 이 있을 때만(없으면 해당 시험 skip — S3 목록 지문 시험은 항상 실행).
Redis 는 다른 시험과 겹치지 않게 db 12 를 쓰고, 큐 키(ocr·cpu·gpu·celery·unacked·unacked_index)만 지운다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.exec_db import ADMIN_URL, bound_app_db, fresh_database, seed_job

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("state_check_under_test", ROOT / "deploy" / "bin" / "state_check.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)

REDIS_BASE = os.getenv("PIXLATE_TEST_REDIS_URL")
KEYS = ("ocr", "cpu", "gpu", "celery", "unacked", "unacked_index")
needs_db_redis = pytest.mark.skipif(not (ADMIN_URL and REDIS_BASE), reason="PIXLATE_TEST_DATABASE_URL·PIXLATE_TEST_REDIS_URL 필요")


def _redis_url() -> str:
    return re.sub(r"/\d+$", "", REDIS_BASE) + "/12"


@pytest.fixture(scope="module")
def dburl():
    if not (ADMIN_URL and REDIS_BASE):
        pytest.skip("PIXLATE_TEST_DATABASE_URL·PIXLATE_TEST_REDIS_URL 필요")
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def env(dburl, monkeypatch):
    import redis

    from app import execution
    from app.storage import MemoryStore, set_store
    from tests.exec_fakes import QueueDispatcher

    r = redis.Redis.from_url(_redis_url())
    r.delete(*KEYS)
    monkeypatch.setenv("DATABASE_URL", dburl)
    monkeypatch.setenv("CELERY_BROKER_URL", _redis_url())
    store = MemoryStore()
    set_store(store)
    q = QueueDispatcher()
    q.drop = True  # 메시지를 실제로 보내지 않는다 — 시도는 pending 으로 남는다
    execution.set_dispatcher(q)
    yield {"redis": r, "store": store}
    execution.set_dispatcher(None)
    set_store(None)
    r.delete(*KEYS)


def _q(sql, **p):
    from app import db as app_db

    with app_db.engine.begin() as c:
        res = c.execute(text(sql), p)
        return [dict(x) for x in res.mappings().all()] if res.returns_rows else None


def _start(env) -> tuple[dict, int]:
    """분석 실행 하나와 그 시도(pending) 하나."""
    from app import db as app_db
    from app.flows.analysis import start_analysis
    from app.manifest import sha256_bytes
    from tests.exec_fakes import sample_source_png

    with app_db.engine.begin() as c:
        ids = seed_job(c)
        data = sample_source_png()
        key = f"source/{ids['job']}/s.png"
        env["store"].put(key, data)
        c.execute(text("INSERT INTO source_image (job_id, upload_order, file_url, width, height, sha256) VALUES (:j, 1, :k, 600, 1000, :h)"),
                  {"j": ids["job"], "k": key, "h": sha256_bytes(data)})
    db = app_db.SessionLocal()
    try:
        r = start_analysis(db, ids["job"], ids["seller"])
    finally:
        db.close()
    return ids, r["taskId"]


def _snap(capsys) -> dict:
    assert sc.main(["--snapshot"]) == 0
    return json.loads(capsys.readouterr().out.strip())


def _diff(a: dict, b: dict) -> set[str]:
    x, y = sc.flatten(a), sc.flatten(b)
    return {k for k in set(x) | set(y) if x.get(k) != y.get(k)}


def _settle():
    """앞 시험이 남긴 활성 시도를 닫아 이 모듈의 각 시험이 빈 상태에서 시작하게 한다."""
    _q("UPDATE job_async_task SET status = 'cancelled', cancelled_at = now() WHERE status IN ('pending','running')")


# ---- 인자 없음: 기존 출력 그대로 -----------------------------------------------------------------------------------
@needs_db_redis
def test_legacy_output_format_unchanged(env, capsys):
    _settle()
    assert sc.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "redis queues: {'ocr': 0, 'cpu': 0, 'gpu': 0, 'celery': 0} | unacked: 0 | unacked_index: 0",
        "db revision: 0008 | pending/running attempts: 0 | open handoffs: 0",
    ]


@needs_db_redis
def test_legacy_failure_prints_type_only(env, capsys, monkeypatch):
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://127.0.0.1:1/0")
    assert sc.main([]) == 1
    out = capsys.readouterr().out
    assert out.startswith("state check failed: ") and "127.0.0.1" not in out


# ---- --gate -------------------------------------------------------------------------------------------------
@needs_db_redis
def test_gate_idle_busy_and_failure(env, capsys, monkeypatch):
    _settle()
    assert sc.main(["--gate"]) == 0
    assert capsys.readouterr().out.strip().endswith("gate=idle")
    env["redis"].rpush("cpu", "x")
    assert sc.main(["--gate"]) == 2
    env["redis"].delete("cpu")
    _start(env)  # 부모 있는 pending 시도 1 + 열린 대표 실행 1
    assert sc.main(["--gate"]) == 2
    out = capsys.readouterr().out
    assert "active attempts: 1" in out and "open runs(참고): 1" in out
    _settle()
    assert sc.main(["--gate"]) == 0
    env["redis"].zadd("unacked_index", {"tag-1": 1.0})  # 인덱스만 남은 경우도 유휴가 아니다
    assert sc.main(["--gate"]) == 2
    assert "unacked_index: 1" in capsys.readouterr().out
    env["redis"].delete("unacked_index")
    assert sc.main(["--gate"]) == 0
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/x")
    assert sc.main(["--gate"]) == 1


@needs_db_redis
def test_gate_redis_failure_is_query_failure(env, capsys, monkeypatch):
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://127.0.0.1:1/0")
    assert sc.main(["--gate"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("gate check failed: ") and "127.0.0.1" not in out


# ---- --db-revision ------------------------------------------------------------------------------------------
@needs_db_redis
def test_db_revision_reads_table_directly(env, capsys):
    assert sc.main(["--db-revision"]) == 0
    assert capsys.readouterr().out.strip() == "0008"
    _q("UPDATE alembic_version SET version_num = '0099_unknown_to_this_image'")
    try:
        assert sc.main(["--db-revision"]) == 0  # 이 이미지의 Alembic 이 모르는 revision 도 그대로 읽는다
        assert capsys.readouterr().out.strip() == "0099_unknown_to_this_image"
        _q("INSERT INTO alembic_version (version_num) VALUES ('0100')")
        assert sc.main(["--db-revision"]) == 1  # 행 2개
        _q("DELETE FROM alembic_version")
        assert sc.main(["--db-revision"]) == 1  # 행 0개
    finally:
        _q("DELETE FROM alembic_version")
        _q("INSERT INTO alembic_version (version_num) VALUES ('0008')")


# ---- --snapshot ----------------------------------------------------------------------------------------------
@needs_db_redis
def test_snapshot_same_without_activity(env, capsys):
    _start(env)
    a = _snap(capsys)
    b = _snap(capsys)
    assert a == b


@needs_db_redis
def test_snapshot_detects_execution_of_existing_attempt(env, capsys):
    from app import execution

    _settle()
    ids, att = _start(env)
    s0 = _snap(capsys)
    lease = execution.acquire(att, "w")  # 새 행 없이 기존 시도의 status·lease_epoch 만 바뀐다
    assert lease is not None
    assert "db.rows:job_async_task" in _diff(s0, _snap(capsys))


@needs_db_redis
def test_snapshot_detects_work_that_finished_quickly(env, capsys):
    """기동 직후 실행되고 바로 끝난 작업 — S0 을 소비자 기동 전에 잡으면 S1 과 달라진다."""
    from app import execution

    _settle()
    ids, att = _start(env)
    s0 = _snap(capsys)
    assert execution.acquire(att, "w") is not None
    _q("UPDATE job_async_task SET status = 'done', finished_at = now() WHERE id = :t", t=att)
    assert "db.rows:job_async_task" in _diff(s0, _snap(capsys))


@needs_db_redis
def test_snapshot_detects_lease_expiry_handling_and_queue_and_delete(env, capsys):
    _settle()
    ids, att = _start(env)
    s0 = _snap(capsys)
    _q("UPDATE job_async_task SET lease_epoch = lease_epoch + 1 WHERE id = :t", t=att)  # 복구의 lease 만료 처리와 같은 변경
    assert "db.rows:job_async_task" in _diff(s0, _snap(capsys))
    s1 = _snap(capsys)
    env["redis"].rpush("ocr", "m")
    assert _diff(s1, _snap(capsys)) == {"redis.queue:ocr"}
    env["redis"].delete("ocr")
    s2 = _snap(capsys)
    _q("UPDATE job SET status = 'archived', updated_at = now() WHERE id = :j", j=ids["job"])
    assert "db.rows:job" in _diff(s2, _snap(capsys))


@needs_db_redis
def test_compare_exit_codes(env, capsys, tmp_path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"db": {"x": 1}, "redis": {"y": 0}}))
    b.write_text(json.dumps({"db": {"x": 1}, "redis": {"y": 0}}))
    assert sc.main(["--compare", str(a), str(b)]) == 0
    b.write_text(json.dumps({"db": {"x": 2}, "redis": {"y": 0}}))
    assert sc.main(["--compare", str(a), str(b)]) == 2
    assert "db.x" in capsys.readouterr().out
    assert sc.main(["--compare", str(a), str(tmp_path / "none.json")]) == 1


# ---- S3 목록 지문(가짜 목록 — 새 의존성 없음) -------------------------------------------------------------------------
class FakeS3:
    def __init__(self, objs: dict[str, int]):
        self.objs = dict(objs)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        objs = self.objs

        class P:
            def paginate(self, Bucket):  # noqa: N803 — boto3 이름
                keys = sorted(objs)
                for i in range(0, max(len(keys), 1), 2):  # 두 개씩 여러 쪽으로
                    yield {"Contents": [{"Key": k, "Size": objs[k]} for k in keys[i:i + 2]]} if keys else {}

        return P()


def test_s3_list_digest_detects_delete_not_noop_redelete():
    s3 = FakeS3({"jobs/1/a.png": 10, "jobs/1/b.png": 20, "jobs/2/c.png": 30})
    d0 = sc.s3_list_digest(s3, "bucket")
    assert d0["objects"] == 3 and re.fullmatch(r"[0-9a-f]{64}", d0["digest"])
    assert "jobs/" not in json.dumps(d0)  # 키·버킷 이름을 출력하지 않는다
    s3.objs.pop("jobs/9/none.png", None)  # 이미 없는 접두사 재삭제 — 목록 불변(감지 한계)
    assert sc.s3_list_digest(s3, "bucket") == d0
    s3.objs.pop("jobs/2/c.png")
    assert sc.s3_list_digest(s3, "bucket") != d0
    assert sc.s3_list_digest(FakeS3({}), "bucket")["objects"] == 0
