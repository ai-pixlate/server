"""큐 유실·권한 만료·채택 유실 복구와 실행 경쟁 — 실제 PostgreSQL. app.recovery.sweep 을 직접 부른다."""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, text

from app import ai_adapters, execution, recovery
from app.manifest import sha256_bytes
from app.storage import MemoryStore, set_store
from tests.exec_db import bound_app_db, fresh_database, requires_db, seed_job
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

    store = MemoryStore()
    set_store(store)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    monkeypatch.setenv("PIXLATE_DISPATCH_STALE_S", "0")
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


def _start(env):
    from app import db as app_db
    from app.flows.analysis import start_analysis

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


def _expire(task_id: int):
    _q("UPDATE job_async_task SET lease_expires_at = now() - interval '1 second' WHERE id = :t", t=task_id)


def _job(ids):
    return _q("SELECT status, current_step FROM job WHERE id = :j", j=ids["job"])[0]


def test_broker_loss_redispatch_same_attempt(env):
    env["q"].drop = True
    ids, att = _start(env)
    assert env["q"].messages == []
    assert _q("SELECT dispatch_count FROM job_async_task WHERE id = :t", t=att)[0]["dispatch_count"] == 1  # 보냈다고 기록됐지만 유실
    env["q"].drop = False
    st = recovery.sweep()
    assert st["locked"] and st["redispatched"] == 1
    env["q"].drain()
    assert _job(ids)["current_step"] == "N3"
    a = _q("SELECT dispatch_count, retry_count, attempt_no FROM job_async_task WHERE id = :t", t=att)[0]
    assert a == {"dispatch_count": 2, "retry_count": 0, "attempt_no": 1}  # 같은 시도 id, 재시도 횟수 불변


def test_send_failure_keeps_pending_then_recovers(env):
    env["q"].fail_send = True
    ids, att = _start(env)
    assert _q("SELECT status, last_dispatched_at FROM job_async_task WHERE id = :t", t=att)[0] == {"status": "pending", "last_dispatched_at": None}
    env["q"].fail_send = False
    recovery.sweep()
    env["q"].drain()
    assert _job(ids)["current_step"] == "N3"


def test_lease_expiry_without_result_creates_recovery_attempt(env):
    ids, att = _start(env)
    env["q"].messages.clear()
    lease = execution.acquire(att, "crashed-worker")  # 계산 중 워커가 죽었다
    assert lease is not None
    _expire(att)
    recovery.sweep()
    rows = _q("SELECT id, attempt_no, status, error_code, retry_origin, is_current FROM job_async_task WHERE job_id = :j AND stage = 'analyze' "
              "ORDER BY attempt_no", j=ids["job"])
    assert rows[0]["status"] == "failed" and rows[0]["error_code"] == "LEASE_EXPIRED" and not rows[0]["is_current"]
    assert rows[1]["retry_origin"] == "recovery" and rows[1]["status"] == "pending"
    # 옛 워커가 돌아와도 결과를 등록할 수 없다
    with pytest.raises(execution.LeaseLost):
        execution.register_handoff(lease, execution.Envelope(outcome="failed", target_count=1, payload={},
                                                             input_fingerprint=lease.fingerprint))
    env["q"].drain()
    assert _job(ids)["current_step"] == "N3"


def test_valid_lease_is_not_taken_over(env):
    ids, att = _start(env)
    env["q"].messages.clear()
    lease = execution.acquire(att, "slow-but-alive")
    recovery.sweep()
    assert _q("SELECT status, lease_owner FROM job_async_task WHERE id = :t", t=att)[0] == {"status": "running", "lease_owner": "slow-but-alive"}
    assert execution.heartbeat(lease)


def test_crash_after_fixing_artifacts_is_adopted_by_recovery(env):
    """EC2 워커가 산출물을 검증 키에 고정한 뒤 채택 전에 죽음 → 권한 만료 → 복구자가 인수·채택(새 계산 없음)."""
    from pipeline.types import SourceImage

    from app import artifacts
    from app.dictionary_bundle import build_bundle
    from app.execution import ArtifactSpec, Envelope

    analyzer = FakeAnalyzer()
    ai_adapters.set_adapters(analyzer=analyzer)
    ids, att = _start(env)
    env["q"].messages.clear()
    lease = execution.acquire(att, "worker-dies")
    m = lease.manifest
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "s.png"
        p.write_bytes(env["store"].get(m["sources"][0]["key"]))
        res = analyzer.analyze([SourceImage(source_image_id=m["sources"][0]["source_image_id"], upload_order=1, path=str(p))], Path(tmp) / "o")
        files = {("section_image", s.section_key): Path(s.image_path).read_bytes() for s in res.sections}
    payload = res.model_dump(mode="json")
    for s in payload["sections"]:
        s["image_path"] = f"artifact:section_image/{s['section_key']}"
    env_ = Envelope(outcome="done", target_count=1, input_fingerprint=lease.fingerprint,
                    payload={"analyze_result": payload, "bundle": build_bundle("US", "cosmetic")},
                    artifacts=[ArtifactSpec(kind=k, part_key=pk, data=d) for (k, pk), d in files.items()])
    h = execution.register_handoff(lease, env_)
    for a in artifacts.artifacts_of(h["id"]):
        artifacts.fix_bytes(lease, a["id"], files[(a["kind"], a["part_key"])])
    # 여기서 워커 종료(채택 안 함)
    _expire(att)
    st = recovery.sweep()
    assert st["adoptions"] == 1
    env["q"].drain()
    assert analyzer.calls == 1  # 계산을 다시 하지 않았다
    hh = _q("SELECT state, adopted_by, lease_epoch, adopted_epoch FROM task_handoff WHERE id = :h", h=h["id"])[0]
    assert hh["state"] == "adopted" and hh["adopted_by"].startswith("be-adopter") and hh["adopted_epoch"] > hh["lease_epoch"]
    assert _job(ids)["current_step"] == "N3"


def test_remote_handoff_adoption_message_lost(env):
    from app.flows.gpu_worker import execute_inpaint_remote

    ids, _ = _start(env)
    env["q"].drain()
    from app import db as app_db
    from app.flows.downstream import proceed

    db = app_db.SessionLocal()
    try:
        proceed(db, ids["job"], ids["seller"])
    finally:
        db.close()
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    tids = [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint'", j=ids["job"])]
    env["q"].drop = True  # 채택 메시지 유실
    for t in tids:
        out = execute_inpaint_remote(t)
        assert out["localKept"] is True  # 수신만 확인 — 로컬 자료 보존
    env["q"].drop = False
    _q("UPDATE task_handoff SET received_at = now() - interval '1 hour' WHERE job_id = :j", j=ids["job"])
    st = recovery.sweep()
    assert st["adoptions"] == len(tids)
    env["q"].drain()
    assert _job(ids) == {"status": "review", "current_step": "N5"}
    assert all(h["state"] == "adopted" for h in _q("SELECT state FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
                                                    "WHERE a.job_id = :j AND a.stage = 'inpaint'", j=ids["job"]))


def test_advisory_lock_single_recovery(env, dburl):
    eng = create_engine(dburl)
    with eng.connect() as c:
        assert c.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": recovery.LOCK_KEY}).scalar()
        assert recovery.sweep()["locked"] is False  # 다른 복구 프로세스가 돌고 있다
        c.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": recovery.LOCK_KEY})
    eng.dispose()


def test_concurrent_user_retries_create_one_attempt(env):
    from app import db as app_db

    ids2, _ = _start(env)
    env["q"].drain(limit=1)  # analyze 채택 → 섹션별 판정 시도 대기
    judge = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'judge' ORDER BY id LIMIT 1", j=ids2["job"])[0]["id"]
    env["q"].messages.clear()
    with app_db.engine.begin() as c:  # 실패한 현재 시도(자동 재시도 전)를 만든다
        c.execute(text("UPDATE job_async_task SET status = 'failed', error_code = 'X' WHERE id = :t"), {"t": judge})
    made, errs = [], []

    def go():
        db = app_db.SessionLocal()
        try:
            made.append(execution.create_retry(db, judge, "user"))
            db.commit()
        except Exception as e:  # noqa: BLE001
            db.rollback()
            errs.append(e)
        finally:
            db.close()

    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(made) == 1 and len(errs) == 2  # U2 + 잠금 재확인
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE parent_task_id = (SELECT parent_task_id FROM job_async_task WHERE id = :t) "
              "AND stage = 'judge' AND unit_id = (SELECT unit_id FROM job_async_task WHERE id = :t) AND is_current", t=judge)[0]["n"] == 1


def test_double_adoption_is_noop(env):
    ids, att = _start(env)
    env["q"].messages.clear()
    from app.flows.analysis import execute_analyze

    execute_analyze(att)
    h = _q("SELECT id FROM task_handoff WHERE task_id = :t", t=att)[0]["id"]
    n_before = _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=ids["job"])[0]["n"]
    lease = execution.Lease(attempt_id=att, job_id=ids["job"], run_id=0, stage="analyze", unit_type="job", unit_id=ids["job"], epoch=1,
                            owner="x", manifest={}, fingerprint="")
    assert execution.adopt(h, lease) is None  # 이미 채택 — 후속을 다시 만들지 않는다
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=ids["job"])[0]["n"] == n_before


def test_security_revoked_grant_blocks_registration_and_adoption(env):
    ids, _ = _start(env)
    env["q"].drain()
    from app import db as app_db
    from app.flows.downstream import proceed
    from app.flows.gpu_worker import InProcessControlClient

    db = app_db.SessionLocal()
    try:
        proceed(db, ids["job"], ids["seller"])
    finally:
        db.close()
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    tid = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' ORDER BY id LIMIT 1", j=ids["job"])[0]["id"]
    cl = InProcessControlClient("gpu-sec")
    got = cl.acquire(tid)
    _q("UPDATE task_lease_grant SET security_revoked_at = now(), security_revoke_reason = 'leaked' WHERE task_id = :t", t=tid)
    assert cl.heartbeat(tid, got["epoch"], got["token"]) is False
    with pytest.raises(execution.AuthRejected):
        cl.register(tid, got["epoch"], got["token"], {"outcome": "failed", "target_count": 1, "payload": {},
                                                      "input_fingerprint": got["fingerprint"]})
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t", t=tid)[0]["n"] == 0


def test_late_gpu_result_after_n5_fallback_is_rejected(env):
    """⑥ 권한 만료 → 복구 재시도까지 만료 → 대체(원본 배경)로 N5 진입 → 그 뒤 도착한 늦은 결과는 채택하지 않는다(D9-3)."""
    from app import db as app_db
    from app.flows.downstream import proceed
    from app.flows.gpu_worker import InProcessControlClient

    ids, _ = _start(env)
    env["q"].drain()
    db = app_db.SessionLocal()
    try:
        proceed(db, ids["job"], ids["seller"])
    finally:
        db.close()
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    cl = InProcessControlClient("gpu-late")
    leases = {}
    for _round in range(2):  # 첫 시도 + 복구 재시도 모두 계산 중 사라짐
        for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current AND status = 'pending'",
                    j=ids["job"]):
            leases[r["id"]] = cl.acquire(r["id"])
            _expire(r["id"])
        recovery.sweep()
        env["q"].messages = [m for m in env["q"].messages if m[0] != "app.tasks.run_inpaint"]
        env["q"].drain()
    assert _job(ids) == {"status": "review", "current_step": "N5"}
    last = max(leases)
    got = leases[last]
    body = {"outcome": "failed", "target_count": 1, "input_fingerprint": got["fingerprint"],
            "payload": {"error_code": "INPAINT_FAILED", "message": "late", "retryable": False}, "artifacts": []}
    reg = cl.register(last, got["epoch"], got["token"], body)
    assert reg["late"] is True
    env["q"].drain()
    h = _q("SELECT state, reject_reason FROM task_handoff WHERE task_id = :t", t=last)[0]
    assert h["state"] == "rejected"
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=last)[0]["status"] == "failed"
