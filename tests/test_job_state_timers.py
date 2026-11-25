"""작업 상태 10종 · 24시간 자동 취소 · 처리 시간 상한(docs/README.md 7.8) — 실제 PostgreSQL + 메모리 저장소.

대역(라벨 · 번역 · 판정 · 인페인트 모델)은 test_exec_downstream 과 같다. 시각은 DB 열(state_entered_at · last_activity_at)을
과거로 돌려 흉내 낸다(실제 24시간 · 30분을 기다리지 않는다). app.recovery.sweep 을 직접 부른다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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
    from app import s3

    store = MemoryStore()
    set_store(store)
    monkeypatch.setattr(s3, "presigned_get", lambda key, ttl=300, download_name=None: f"https://presigned.test/{key}" if key else None)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    monkeypatch.setenv("PIXLATE_GPU_WORK_DIR", str(tmp_path / "gpu"))
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", style_defaults_file(tmp_path))
    monkeypatch.setenv("PIXLATE_DISPATCH_STALE_S", "0")
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


def _new_job(env):
    """N1 작업(원본 1장) + 로그인한 클라이언트."""
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
    tok = cl.post("/v1/auth/login", json={"email": ids["email"], "password": "x"}).json()["accessToken"]
    cl.headers["Authorization"] = f"Bearer {tok}"
    ids["c"] = cl
    return ids


def _to_n3(env):
    ids = _new_job(env)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/analyze").status_code == 202
    env["q"].drain()
    assert _state(ids)[:2] == ("review", "N3")
    return ids


def _to_n5(env):
    ids = _to_n3(env)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    return ids


def _state(ids):
    j = ids["c"].get(f"/v1/jobs/{ids['job']}").json()
    return j["status"], j["currentStep"], j["userFacingStatus"]


def _age_state(job, seconds):
    _q("UPDATE job SET state_entered_at = now() - make_interval(secs => :s) WHERE id = :j", s=seconds, j=job)


def _age_activity(job, hours):
    _q("UPDATE job SET last_activity_at = now() - make_interval(hours => :h) WHERE id = :j", h=hours, j=job)


def _events(job):
    return _q("SELECT event_type, payload FROM event_log WHERE job_id = :j ORDER BY id", j=job)


# ---- ② · ③ 상태 구분(10번 중 상태 구분) -------------------------------------------------------------------------------
def test_n6_rendering_then_awaiting_save_then_done(env):
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    r = c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 200 and r.json()["status"] == "processing"
    assert _state(ids) == ("processing", "N6", "rendering")
    t = c.get(f"/v1/jobs/{job}/tasks").json()
    assert (t["jobStatus"], t["userFacingStatus"]) == ("processing", "rendering")
    env["q"].drain()
    assert _state(ids) == ("review", "N6", "awaiting_save")
    # 저장 대기에서 수동 재렌더 → 최종 렌더 중 → 다시 저장 대기
    assert c.post(f"/v1/jobs/{job}/render").status_code == 202
    assert _state(ids) == ("processing", "N6", "rendering")
    env["q"].drain()
    assert _state(ids) == ("review", "N6", "awaiting_save")
    assert c.post(f"/v1/jobs/{job}/save").status_code == 200
    assert _state(ids)[:2] == ("done", "N6")


def test_render_failure_goes_to_n6_error_and_retry_returns_to_rendering(env):
    """자동 재시도 없이 렌더 실패 → failed · N6(PRD 05 오류). 한도가 남은 [다시 시도] → 최종 렌더 중. 한도 소진 → 오류 유지 · 409."""
    from app import db as app_db
    from app.flows.final import FinalRenderHandler

    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []  # 렌더 메시지를 버리고 실패를 흉내 낸다

    def fail_current():
        """현재 최종 렌더 시도를 실패(재시도 불가 사유)로 닫고 실패 후속을 부른다 — 렌더러가 실패를 보고한 것과 같다."""
        att = _q("SELECT * FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=job)[0]
        _q("UPDATE job_async_task SET status = 'failed', error_code = 'RENDER_FAILED', finished_at = now() WHERE id = :t", t=att["id"])
        db = app_db.SessionLocal()
        try:
            execution.lock_job(db, job)
            a = dict(db.execute(text("SELECT * FROM job_async_task WHERE id = :t"), {"t": att["id"]}).mappings().one())
            FinalRenderHandler().after_failure(db, a, "RENDER_FAILED", False)
            db.commit()
        finally:
            db.close()
        return att

    att = fail_current()
    assert _state(ids) == ("failed", "N6", "failed")
    for _ in range(att["max_retry"]):  # [다시 시도] → 최종 렌더 중 → 다시 실패, 한도까지
        r = c.post(f"/v1/jobs/{job}/tasks/{att['id']}/retry")
        assert r.status_code == 200, r.text
        assert _state(ids) == ("processing", "N6", "rendering")
        env["q"].messages = []
        att = fail_current()
        assert _state(ids) == ("failed", "N6", "failed")
    assert att["retry_count"] == att["max_retry"]  # 한도 소진
    assert _q("SELECT status FROM job_async_task WHERE id = (SELECT parent_task_id FROM job_async_task WHERE id = :t)", t=att["id"])[0][
        "status"] == "failed"
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409
    assert _state(ids) == ("failed", "N6", "failed")


# ---- ⑤ 처리 시간 상한 ---------------------------------------------------------------------------------------------
def test_n2_timeout_fails_and_late_result_is_not_adopted(env):
    ids = _new_job(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/analyze").status_code == 202  # 메시지는 큐에 남긴다(GPU 꺼짐)
    _age_state(job, 1799)
    recovery.sweep()
    assert _state(ids)[:2] == ("processing", "N2")  # 30분 전에는 그대로
    _age_state(job, 1801)
    st = recovery.sweep()
    assert st["timed_out"] == 1
    assert _state(ids) == ("failed", "N2", "failed")
    items = c.get(f"/v1/jobs/{job}/tasks").json()["items"]
    assert any(i["status"] == "failed" and i["errorCode"] == "PROCESSING_TIMEOUT" for i in items)
    assert [e["payload"]["reason"] for e in _events(job) if e["event_type"] == "job_failed"] == ["PROCESSING_TIMEOUT"]
    # 늦게 도착한 처리 메시지: 권한을 얻지 못해 아무것도 채택하지 않는다
    env["q"].drain()
    assert _state(ids) == ("failed", "N2", "failed")
    assert _q("SELECT current_analysis_task_id FROM job WHERE id = :j", j=job)[0]["current_analysis_task_id"] is None
    # [다시 시도] = 분석 재시작 → 처리 중, 시계는 처음부터
    assert c.post(f"/v1/jobs/{job}/analyze").status_code == 202
    assert _state(ids)[:2] == ("processing", "N2")
    assert _q("SELECT now() - state_entered_at < interval '1 minute' AS fresh FROM job WHERE id = :j", j=job)[0]["fresh"] is True


def test_n4_timeout_rejects_late_gpu_result_and_does_not_reach_n5(env):
    from app.flows.gpu_worker import InProcessControlClient

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})  # 인페인트(GPU)만 멈춤
    cl = InProcessControlClient("gpu-late")
    leases = {r["id"]: cl.acquire(r["id"]) for r in _q(
        "SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current AND status = 'pending'", j=job)}
    assert leases
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert _state(ids) == ("failed", "N4", "failed")
    rows = _q("SELECT status, error_code FROM job_async_task WHERE id = ANY(:ids)", ids=list(leases))
    assert all(r["status"] == "failed" and r["error_code"] == "PROCESSING_TIMEOUT" for r in rows)
    # 늦은 GPU 결과: 권한 세대가 바뀌어 등록 자체가 거절되거나, 받아도 채택하지 않는다
    tid, got = next(iter(leases.items()))
    body = {"outcome": "failed", "target_count": 1, "input_fingerprint": got["fingerprint"],
            "payload": {"error_code": "INPAINT_FAILED", "message": "late", "retryable": False}, "artifacts": []}
    try:
        cl.register(tid, got["epoch"], got["token"], body)
    except execution.ExecutionError:
        pass
    env["q"].messages = [m for m in env["q"].messages if m[0] != "app.tasks.run_inpaint"]
    env["q"].drain()
    recovery.sweep()  # 집계 복구(_stuck_runs)도 다음 단계로 보내지 않는다
    assert _state(ids) == ("failed", "N4", "failed")
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t AND state = 'adopted'", t=tid)[0]["n"] == 0
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=job)[0]["n"] == 0
    # [중단] → N3 (지금 N4 오류와 같다)
    r = c.post(f"/v1/jobs/{job}/abort")
    assert r.status_code == 200 and r.json()["returnTo"] == "N3"


def test_n6_timeout_closes_render_run(env):
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []  # 렌더러가 응답하지 않는다
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert _state(ids) == ("failed", "N6", "failed")
    run = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND run_kind = 'final_render'",
             j=job)[0]
    assert run == {"status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409


def test_n6_timeout_first_then_late_render_result_rejected(env):
    """순서 ① 시간 초과가 먼저 확정 → 뒤늦게 도는 최종 렌더 메시지는 권한을 얻지 못해 결과 · 상태를 바꾸지 않는다.
    횟수가 남아 있어도 실행이 실패로 닫혀 JOB-05 retryable=false · 재시도 409(PM 11-25 답변 근거)."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    held = list(env["q"].messages)  # 렌더 메시지를 붙잡아 둔다(아직 처리 안 함)
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    env["q"].messages = held
    env["q"].drain()  # 늦게 도는 렌더
    assert _state(ids) == ("failed", "N6", "failed")
    assert _q("SELECT count(*) AS n FROM deliverable WHERE job_id = :j AND render_status = 'done'", j=job)[0]["n"] == 0
    att = [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskType"] == "render"]
    assert att and all(i["retryable"] is False for i in att)
    assert all(i["retryCount"] < i["maxRetry"] for i in att)  # 횟수는 남아 있다
    r = c.post(f"/v1/jobs/{job}/tasks/{att[0]['taskId']}/retry")
    assert r.status_code == 409


@pytest.mark.parametrize("step", ["N4", "N6"])
def test_result_adopted_before_timeout_sweep_is_used(env, step):
    """순서 ② 30분이 지났어도 시간 초과 처리(점검)보다 정상 결과 채택이 먼저 끝나면 그 결과를 쓴다 — 점검은 잠근 뒤 상태를 다시 보고
    처리 중이 아니면 건드리지 않는다(README 7.8 ⑤)."""
    if step == "N4":
        ids = _to_n3(env)
        c, job = ids["c"], ids["job"]
        assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
        expect = ("review", "N5", "reviewing")
    else:
        ids = _to_n5(env)
        c, job = ids["c"], ids["job"]
        assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
        expect = ("review", "N6", "awaiting_save")
    _age_state(job, 1801)  # 30분이 지났지만 점검은 아직 돌지 않았다
    env["q"].drain()  # 결과 채택이 먼저 끝남
    assert _state(ids) == expect
    assert recovery.sweep()["timed_out"] == 0
    assert _state(ids) == expect
    assert not [e for e in _events(job) if e["event_type"] == "job_failed"]


def test_timeout_disabled_and_not_processing_untouched(env, monkeypatch):
    ids = _to_n3(env)
    _age_state(ids["job"], 99999)
    assert recovery.sweep()["timed_out"] == 0  # 섹션확인(review)은 대상 아님
    ids2 = _new_job(env)
    assert ids2["c"].post(f"/v1/jobs/{ids2['job']}/analyze").status_code == 202
    _age_state(ids2["job"], 99999)
    monkeypatch.setenv("PIXLATE_PROCESSING_TIMEOUT_S", "0")
    assert recovery.sweep()["timed_out"] == 0
    assert _state(ids2)[:2] == ("processing", "N2")
    env["q"].messages = []


# ---- ④ 24시간 자동 취소 ---------------------------------------------------------------------------------------------
def test_auto_cancel_deletes_like_user_cancel_and_keeps_records(env):
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert _q("SELECT count(*) AS n FROM section WHERE job_id = :j", j=job)[0]["n"] > 0
    _age_activity(job, 23)
    assert recovery.sweep()["auto_cancelled"] == 0
    _age_activity(job, 25)
    assert recovery.sweep()["auto_cancelled"] == 1
    assert _state(ids) == ("cancelled", "N3", "cancelled")  # 단계는 멈춘 곳 유지
    for t in ("section", "source_image", "task_handoff"):
        assert _q(f"SELECT count(*) AS n FROM {t} WHERE job_id = :j", j=job)[0]["n"] == 0
    assert _q("SELECT product_name FROM job WHERE id = :j", j=job)[0]["product_name"] is None
    assert not [k for k in env["store"].objects if k.startswith(f"source/{job}/")]
    ev = [e for e in _events(job) if e["event_type"] == "job_cancelled"]
    assert ev == [{"event_type": "job_cancelled", "payload": {"reason": "inactive_24h", "step": "N3", "from_status": "review"}}]
    audit = _q("SELECT actor_type, detail FROM audit_log WHERE target_type = 'job' AND target_id = :j AND action_type = 'job_content_deleted'",
               j=job)
    assert audit == [{"actor_type": "system", "detail": None}]
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=job)[0]["n"] > 0  # 처리 기록은 남는다


def test_auto_cancelled_job_rejects_operations(env):
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    blk = _q("SELECT tb.id, tb.revision FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
             "AND tb.trans_1 IS NOT NULL ORDER BY tb.id LIMIT 1", j=job)[0]
    _age_activity(job, 25)
    assert recovery.sweep()["auto_cancelled"] == 1
    assert c.get(f"/v1/jobs/{job}").status_code == 200 and _state(ids)[0] == "cancelled"
    assert c.get(f"/v1/jobs/{job}/tasks").json()["jobStatus"] == "cancelled"
    r = c.patch(f"/v1/jobs/{job}/blocks/{blk['id']}", json={"trans1": "x", "revision": blk["revision"]})
    assert r.status_code in (404, 409)  # 블록이 지워졌거나(404) 편집 불가(409) — 어느 쪽도 내용이 생기지 않는다
    for method, path, kw in (
        ("post", f"/v1/jobs/{job}/confirm", {"json": {"acknowledgedWarnings": []}}),
        ("post", f"/v1/jobs/{job}/analyze", {}),
        ("post", f"/v1/jobs/{job}/abort", {}),
        ("post", f"/v1/jobs/{job}/render", {}),
        ("post", f"/v1/jobs/{job}/save", {}),
        ("post", f"/v1/jobs/{job}/export", {"json": {"components": ["content.csv"]}}),
        ("patch", f"/v1/jobs/{job}/source-images/reorder", {"json": {"order": []}}),
    ):
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE", (path, r.status_code, r.text)
    r = c.post(f"/v1/jobs/{job}/source-images", files={"files": ("a.png", sample_source_png(), "image/png")})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert _q("SELECT count(*) AS n FROM source_image WHERE job_id = :j", j=job)[0]["n"] == 0
    assert not [k for k in env["store"].objects if k.startswith(f"source/{job}/")]
    assert c.delete(f"/v1/jobs/{job}").json() == {"jobId": job, "status": "cancelled"}  # 반복 취소 200
    assert len([e for e in _events(job) if e["event_type"] == "job_cancelled"]) == 1  # 이벤트는 처음 한 번


def test_auto_cancel_targets_only_seller_turn_states(env):
    n3 = _to_n3(env)
    done = _to_n5(env)
    dc, dj = done["c"], done["job"]
    assert dc.post(f"/v1/jobs/{dj}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert dc.post(f"/v1/jobs/{dj}/save").status_code == 200
    proc = _new_job(env)  # 처리 중(분석 메시지를 처리하지 않는다 — 위 drain 뒤에 만든다)
    assert proc["c"].post(f"/v1/jobs/{proc['job']}/analyze").status_code == 202
    env["q"].messages = []
    for j in (n3["job"], proc["job"], dj):
        _age_activity(j, 30)
    _age_state(proc["job"], 0)
    st = recovery.sweep()
    assert st["auto_cancelled"] == 1
    assert _state(n3)[0] == "cancelled"
    assert _state(proc)[0] == "processing" and _state(done)[0] == "done"


def test_activity_clock_restarts_on_mutating_request_not_on_get(env):
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    _age_activity(job, 25)
    c.get(f"/v1/jobs/{job}")
    c.get(f"/v1/jobs/{job}/tasks")
    assert _q("SELECT last_activity_at < now() - interval '24 hours' AS old FROM job WHERE id = :j", j=job)[0]["old"] is True
    sec = _q("SELECT id, revision FROM section WHERE job_id = :j ORDER BY id LIMIT 1", j=job)[0]
    r = c.patch(f"/v1/jobs/{job}/sections/{sec['id']}", json={"action": "exclude"})
    assert 200 <= r.status_code < 300, r.text
    assert _q("SELECT last_activity_at > now() - interval '1 minute' AS fresh FROM job WHERE id = :j", j=job)[0]["fresh"] is True
    assert recovery.sweep()["auto_cancelled"] == 0
    # 실패 응답(4xx)은 세지 않는다
    _age_activity(job, 25)
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 409
    assert _q("SELECT last_activity_at < now() - interval '24 hours' AS old FROM job WHERE id = :j", j=job)[0]["old"] is True


def test_user_cancel_keeps_event_log_and_records_reason(env):
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    _q("INSERT INTO event_log (job_id, event_type, payload) VALUES (:j, 'probe', '{}'::jsonb)", j=job)
    assert c.delete(f"/v1/jobs/{job}").json() == {"jobId": job, "status": "cancelled"}
    types = [e["event_type"] for e in _events(job)]
    assert "probe" in types and types[-1] == "job_cancelled"
    assert _events(job)[-1]["payload"]["reason"] == "user"
