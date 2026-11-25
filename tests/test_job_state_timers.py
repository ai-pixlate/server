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
    # 저장 대기에서 수동 재렌더는 막는다(PM 11-25) — 409 · 새 실행 · 시도 없음 · 상태 그대로
    before = _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'final_render'", j=job)[0]["n"]
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'final_render'", j=job)[0]["n"] == before
    assert _state(ids) == ("review", "N6", "awaiting_save") and env["q"].messages == []
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


def _final_attempts(job):
    return _q("SELECT id, status, error_code, retry_count, max_retry, is_current, supersedes_task_id FROM job_async_task "
              "WHERE job_id = :j AND stage = 'final_render' ORDER BY id", j=job)


def _final_run(job):
    return _q("SELECT id, status FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND run_kind = 'final_render' "
              "ORDER BY id", j=job)


def _fresh(job):
    return _q("SELECT now() - state_entered_at < interval '1 minute' AS fresh FROM job WHERE id = :j", j=job)[0]["fresh"]


def test_n6_timeout_keeps_run_for_user_retry_and_sweep_does_not_rerender(env):
    """PM 11-25: 시간 초과 → failed · N6, 시도만 failed(PROCESSING_TIMEOUT) · 실행은 남긴다(사용자 재시도용).
    사용자가 재시도하기 전에는 일반 점검이 새 렌더(시도 · 메시지)를 만들지 않는다."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []  # 렌더러가 응답하지 않는다
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert _state(ids) == ("failed", "N6", "failed")
    assert [r["status"] for r in _final_run(job)] == ["running"]
    atts = _final_attempts(job)
    assert [(a["status"], a["error_code"], a["is_current"]) for a in atts] == [("failed", "PROCESSING_TIMEOUT", True)]
    items = [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskType"] == "render"]
    assert items and items[0]["retryable"] is True and items[0]["errorCode"] == "PROCESSING_TIMEOUT"
    for _ in range(3):  # 일반 점검을 여러 번 돌려도 새 시도 · 메시지 · 상태 변화 없음
        st = recovery.sweep()
        assert st["timed_out"] == 0 and st["redispatched"] == 0
    assert _final_attempts(job) == atts and env["q"].messages == []
    assert _state(ids) == ("failed", "N6", "failed")
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409


def test_n6_timeout_first_then_late_render_result_rejected_and_retry_adopts_new_attempt(env):
    """순서 ① 시간 초과가 먼저 확정 → 뒤늦게 도는 옛 렌더 메시지는 권한을 얻지 못해 결과 · 상태를 바꾸지 않는다.
    PM 11-25: 횟수가 남았으면 재시도 가능 — [다시 시도] → 새 시도 · 최종 렌더 중 · 30분 새로 셈 → 새 시도 결과만 채택."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    held = list(env["q"].messages)  # 렌더 메시지를 붙잡아 둔다(아직 처리 안 함)
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    env["q"].messages = list(held)
    env["q"].drain()  # 늦게 도는 옛 렌더
    assert _state(ids) == ("failed", "N6", "failed")
    assert _q("SELECT count(*) AS n FROM deliverable WHERE job_id = :j AND render_status = 'done'", j=job)[0]["n"] == 0
    att = [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskType"] == "render"]
    assert att and att[0]["retryable"] is True and att[0]["retryCount"] < att[0]["maxRetry"]
    old_id = att[0]["taskId"]
    r = c.post(f"/v1/jobs/{job}/tasks/{old_id}/retry")
    assert r.status_code == 200, r.text
    assert _state(ids) == ("processing", "N6", "rendering") and _fresh(job) is True
    new_id = r.json()["taskId"]
    assert new_id != old_id
    # 재시도 중에 옛 시도 메시지가 다시 와도(재전달) 권한을 얻지 못한다 — 새 시도는 그대로 진행
    env["q"].messages = list(held) + env["q"].messages
    env["q"].drain()
    assert _state(ids) == ("review", "N6", "awaiting_save")
    old = _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=old_id)[0]
    assert old == {"status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=new_id)[0]["status"] == "done"
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t AND state = 'adopted'", t=old_id)[0]["n"] == 0
    assert c.post(f"/v1/jobs/{job}/save").status_code == 200


def test_n6_timeout_retry_restarts_clock_and_can_time_out_again(env):
    """재시도로 다시 처리 중이 되면 30분을 새로 센다 — 29분 59초에는 그대로, 30분이 넘으면 새 시도도 시간 초과."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    r = c.post(f"/v1/jobs/{job}/render")  # FIN-01 [다시 시도]
    assert r.status_code == 202 and _state(ids) == ("processing", "N6", "rendering") and _fresh(job) is True
    env["q"].messages = []  # 재시도한 렌더도 응답하지 않는다
    _age_state(job, 1799)
    assert recovery.sweep()["timed_out"] == 0 and _state(ids)[0] == "processing"
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N6", "failed")
    atts = _final_attempts(job)
    assert [(a["status"], a["error_code"], a["retry_count"]) for a in atts] == [
        ("failed", "PROCESSING_TIMEOUT", 0), ("failed", "PROCESSING_TIMEOUT", 1)]
    assert [r["status"] for r in _final_run(job)] == ["running"]


def test_n6_duplicate_retry_requests_make_at_most_one_new_attempt(env):
    """새 시도는 최대 1개: FIN-01 은 진행 중 재시도 시도를 돌려주고(202 · 같은 id), JOB-06 은 이미 대체된 옛 시도 재요청을 409 로 거절한다."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    old_id = _final_attempts(job)[0]["id"]
    r1 = c.post(f"/v1/jobs/{job}/render")
    r2 = c.post(f"/v1/jobs/{job}/render")
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["renderTaskId"] == r2.json()["renderTaskId"] != old_id
    r3 = c.post(f"/v1/jobs/{job}/tasks/{old_id}/retry")
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    atts = _final_attempts(job)
    assert len(atts) == 2 and [a["is_current"] for a in atts] == [False, True]
    assert atts[1]["supersedes_task_id"] == old_id


def test_n6_concurrent_fin01_and_job06_retry_after_timeout_make_one_attempt(env, dburl):
    """동시 경합(실제 행 잠금이 겹침): 시간 초과 뒤 FIN-01 2건 · 옛 시도 JOB-06 1건을 작업 행 잠금을 잡아 둔 채 동시에 보내고 풀면
    새 시도는 1개. FIN-01 은 모두 202 · 그 새 시도 id, JOB-06 은 먼저 잡았으면 200(같은 새 시도) · 늦었으면 409 RETRY_NOT_ALLOWED."""
    import threading
    import time

    from sqlalchemy import create_engine

    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    old_id = _final_attempts(job)[0]["id"]
    out: dict = {}

    def call(name, path):
        r = c.post(path)
        out[name] = (r.status_code, r.json())

    eng = create_engine(dburl)
    try:
        with eng.connect() as holder:
            holder.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": job})  # 작업 행 잠금을 먼저 잡는다
            ts = [threading.Thread(target=call, args=("fin1", f"/v1/jobs/{job}/render")),
                  threading.Thread(target=call, args=("fin2", f"/v1/jobs/{job}/render")),
                  threading.Thread(target=call, args=("job06", f"/v1/jobs/{job}/tasks/{old_id}/retry"))]
            [t.start() for t in ts]
            waiting = 0
            for _ in range(200):  # 세 요청이 모두 작업 행 잠금에서 기다릴 때까지
                with eng.connect() as probe:
                    waiting = probe.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                                 "AND datname = current_database()")).scalar()
                if waiting >= 3:
                    break
                time.sleep(0.05)
            assert waiting >= 3, waiting
            holder.rollback()  # 잠금을 푼다 → 세 요청이 차례로 잠금을 얻는다
            [t.join(timeout=30) for t in ts]
    finally:
        eng.dispose()
    atts = _final_attempts(job)
    assert len(atts) == 2 and [a["is_current"] for a in atts] == [False, True]  # 새 시도 최대 1개
    new_id = atts[1]["id"]
    assert out["fin1"][0] == 202 and out["fin1"][1]["renderTaskId"] == new_id
    assert out["fin2"][0] == 202 and out["fin2"][1]["renderTaskId"] == new_id
    s, body = out["job06"]
    assert (s == 200 and body["taskId"] == new_id) or (s == 409 and body["error"]["code"] == "RETRY_NOT_ALLOWED"), out["job06"]
    assert _state(ids) == ("processing", "N6", "rendering")


def test_n6_timeout_at_retry_limit_stays_not_retryable(env):
    """누적 한도를 소진한 시도의 시간 초과는 지금 규칙(기존 누적 한도)대로 재시도 불가 — 한도를 넘는 재시도 허용 여부는 PM 확인 중."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []
    max_retry = _final_attempts(job)[0]["max_retry"]
    for n in range(max_retry):  # 시간 초과 → [다시 시도]를 한도까지 반복(재시도 시도도 응답하지 않음)
        _age_state(job, 1801)
        assert recovery.sweep()["timed_out"] == 1
        assert c.post(f"/v1/jobs/{job}/render").status_code == 202
        env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert _final_attempts(job)[-1]["retry_count"] == max_retry  # 누적 한도에 닿은 시도가 시간 초과
    items = [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskType"] == "render"]
    assert items and items[0]["retryable"] is False
    before = _final_attempts(job)
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert _final_attempts(job) == before and _state(ids) == ("failed", "N6", "failed")


def test_n4_timeout_translate_retry_restarts_clock_inpaint_not_user_retryable(env):
    """N4: 시간 초과 뒤 번역 시도는 JOB-06 으로 재시도 → 처리 중 · 30분 새로 셈(기존 경로). 인페인트 시도는 JOB-06 대상(translate · render)이
    아니라 409 — N4 인페인트 시간 초과의 사용자 재시도 경로는 계약 경계(PM 확인 항목)."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint", "app.tasks.run_translate"})
    tr = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current AND status = 'pending' ORDER BY id", j=job)
    inp = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current AND status = 'pending' ORDER BY id", j=job)
    assert tr and inp
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N4", "failed")
    r = c.post(f"/v1/jobs/{job}/tasks/{inp[0]['id']}/retry")
    assert r.status_code == 409
    r = c.post(f"/v1/jobs/{job}/tasks/{tr[0]['id']}/retry")
    assert r.status_code == 200, r.text
    assert _state(ids) == ("processing", "N4", "translating") and _fresh(job) is True
    _age_state(job, 1799)
    assert recovery.sweep()["timed_out"] == 0 and _state(ids)[0] == "processing"
    env["q"].messages = []
    assert c.delete(f"/v1/jobs/{job}").status_code == 200  # 처리 중 · 30분 직전으로 남겨 두면 다음 시험의 점검에 걸린다 — 정리


def test_n6_running_render_then_timeout_then_late_success_rejected(env, monkeypatch):
    """순서 ①' 이미 실행 중인 최종 렌더(권한 보유) → 그 도중 시간 초과 확정 → 렌더가 성공 결과를 들고 끝남:
    권한 세대가 바뀌어 결과를 채택하지 않고, 산출물 · 상태(오류)를 바꾸지 않는다."""
    from app.flows import downstream

    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    orig = downstream._render_section
    fired: dict = {}

    def render_then_timeout(*a, **kw):
        out = orig(*a, **kw)  # 렌더 계산은 성공한다
        if not fired:  # 첫 섹션을 그린 직후(아직 결과 제출 전) 시간 초과가 확정된다
            _age_state(job, 1801)
            fired["sweep"] = recovery.sweep()
            fired["state"] = _state(ids)
        return out

    monkeypatch.setattr(downstream, "_render_section", render_then_timeout)
    env["q"].drain()  # 최종 렌더 실행(권한 획득 → 렌더 → 그 도중 시간 초과 → 제출)
    assert fired["sweep"]["timed_out"] == 1 and fired["state"] == ("failed", "N6", "failed")
    assert _state(ids) == ("failed", "N6", "failed")
    assert _q("SELECT count(*) AS n FROM deliverable WHERE job_id = :j AND render_status = 'done'", j=job)[0]["n"] == 0
    att = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=job)[0]
    assert att == {"status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    assert _q("SELECT count(*) AS n FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j "
              "AND a.stage = 'final_render' AND h.state = 'adopted'", j=job)[0]["n"] == 0
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409


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
