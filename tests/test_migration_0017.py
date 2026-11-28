"""0017(작업 상태 10종 · 자동 취소 · 처리 시간 상한) 마이그레이션의 기존 데이터 변환과 되돌림 — 실제 PostgreSQL.

head 에서 실제 흐름으로 작업 상태를 만든 뒤 0016 으로 내려 이전 값이 되는지, 다시 0017 로 올려 새 값으로 바뀌는지 본다
(운영 적용 = 0016 의 기존 행을 0017 로 올리는 경로). 이 모듈 전용 DB 를 쓴다(다른 시험 DB 와 섞지 않음).
"""
from __future__ import annotations

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import ai_adapters, execution
from app.manifest import sha256_bytes
from app.storage import MemoryStore, set_store
from tests.exec_db import alembic_config, bound_app_db, fresh_database, requires_db, seed_job
from tests.exec_fakes import (FakeAnalyzer, FakeInpaintModel, FakeJudge, FakeLabeler, FakeTranslator, QueueDispatcher,
                              sample_source_png, seed_dictionary, style_defaults_file)

pytestmark = requires_db


@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def env(dburl, tmp_path, monkeypatch):
    from app import db as app_db
    from app import s3

    store = MemoryStore()
    set_store(store)
    monkeypatch.setattr(s3, "presigned_get", lambda key, ttl=300, download_name=None: f"https://presigned.test/{key}" if key else None)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    monkeypatch.setenv("PIXLATE_GPU_WORK_DIR", str(tmp_path / "gpu"))
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", style_defaults_file(tmp_path))
    ai_adapters.reset_adapters()
    model = FakeInpaintModel()
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(), judge=FakeJudge(), labeler=FakeLabeler(), translator=FakeTranslator(),
                             inpainter=ai_adapters.PipelineInpainter(model_factory=lambda cfg: model))
    with app_db.engine.begin() as c:
        c.execute(text("DELETE FROM expression_dictionary"))
        seed_dictionary(c)
    yield {"store": store, "q": q}
    execution.set_dispatcher(None)
    set_store(None)
    ai_adapters.reset_adapters()


def _q(sql, **p):
    from app import db as app_db

    with app_db.engine.begin() as c:
        res = c.execute(text(sql), p)
        return [dict(r) for r in res.mappings().all()] if res.returns_rows else None


def _to_n5(env):
    from app import db as app_db
    from app.main import app

    with app_db.engine.begin() as c:
        ids = seed_job(c)
        data = sample_source_png()
        key = f"source/{ids['job']}/src.png"
        env["store"].put(key, data)
        c.execute(text("INSERT INTO source_image (job_id, upload_order, file_url, width, height, sha256) VALUES (:j, 1, :k, 600, 1000, :h)"),
                  {"j": ids["job"], "k": key, "h": sha256_bytes(data)})
    cl = TestClient(app)
    cl.headers["Authorization"] = "Bearer " + cl.post("/v1/auth/login", json={"email": ids["email"], "password": "x"}).json()["accessToken"]
    assert cl.post(f"/v1/jobs/{ids['job']}/analyze").status_code == 202
    env["q"].drain()
    assert cl.post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    env["q"].drain()
    ids["c"] = cl
    return ids


def _row(job):
    return _q("SELECT status, current_step, user_facing_status FROM job WHERE id = :j", j=job)[0]


def _cols():
    return {r["column_name"] for r in _q("SELECT column_name FROM information_schema.columns WHERE table_name = 'job' "
                                         "AND column_name IN ('state_entered_at', 'last_activity_at')")}


def test_0017_downgrade_then_upgrade_maps_existing_rows(env, dburl):
    from app import db as app_db
    from app.flows.final import FinalRenderHandler

    n5 = _to_n5(env)
    cancelled = _to_n5(env)
    assert cancelled["c"].delete(f"/v1/jobs/{cancelled['job']}").status_code == 200
    awaiting = _to_n5(env)
    assert awaiting["c"].post(f"/v1/jobs/{awaiting['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    rendering = _to_n5(env)
    assert rendering["c"].post(f"/v1/jobs/{rendering['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []
    failed = _to_n5(env)
    assert failed["c"].post(f"/v1/jobs/{failed['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []
    att = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=failed["job"])[0]["id"]
    _q("UPDATE job_async_task SET status = 'failed', error_code = 'RENDER_FAILED', finished_at = now() WHERE id = :t", t=att)
    db = app_db.SessionLocal()
    try:
        execution.lock_job(db, failed["job"])
        a = dict(db.execute(text("SELECT * FROM job_async_task WHERE id = :t"), {"t": att}).mappings().one())
        FinalRenderHandler().after_failure(db, a, "RENDER_FAILED", False)
        db.commit()
    finally:
        db.close()

    new = {
        n5["job"]: {"status": "review", "current_step": "N5", "user_facing_status": "reviewing"},
        cancelled["job"]: {"status": "cancelled", "current_step": "N5", "user_facing_status": "cancelled"},
        awaiting["job"]: {"status": "review", "current_step": "N6", "user_facing_status": "awaiting_save"},
        rendering["job"]: {"status": "processing", "current_step": "N6", "user_facing_status": "rendering"},
        failed["job"]: {"status": "failed", "current_step": "N6", "user_facing_status": "failed"},
    }
    assert {j: _row(j) for j in new} == new and _cols() == {"state_entered_at", "last_activity_at"}

    app_db.engine.dispose()
    command.downgrade(alembic_config(), "0016")
    old = {
        n5["job"]: new[n5["job"]],
        cancelled["job"]: {"status": "archived", "current_step": "N5", "user_facing_status": "archived"},
        awaiting["job"]: {"status": "review", "current_step": "N6", "user_facing_status": "reviewing"},
        rendering["job"]: {"status": "review", "current_step": "N6", "user_facing_status": "reviewing"},
        failed["job"]: {"status": "review", "current_step": "N6", "user_facing_status": "reviewing"},
    }
    assert {j: _row(j) for j in old} == old and _cols() == set()

    app_db.engine.dispose()
    command.upgrade(alembic_config(), "0017")
    assert {j: _row(j) for j in new} == new and _cols() == {"state_entered_at", "last_activity_at"}
    # 기존 행의 두 시각은 적용 시각 — 적용 직후 한꺼번에 시간 초과 · 자동 취소되지 않는다
    fresh = _q("SELECT bool_and(now() - state_entered_at < interval '1 minute' AND now() - last_activity_at < interval '1 minute') AS ok "
               "FROM job WHERE id = ANY(:ids)", ids=list(new))[0]["ok"]
    assert fresh is True
