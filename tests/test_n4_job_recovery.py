"""N4 작업 단위 복구(downstream.job_retry_plan · retry_job · translate_logical_successes) — PM 결정 2026-11-27 반영, 공개 API 미연결.

PM 결정: 시간 초과 = N4 오류(점검이 N5 로 넘기지 않음). [다시 시도] 때 시간 초과로 닫힌 처리는 단계별 기존 누적 한도(MAX_RETRY) 안에서
다시 실행(인페인트 · 스타일 · 라벨 · 로고 포함), 소진이면 실패 규칙(인페인트 = 원본 배경 · 실행 실패 표시, 번역 = 성공분 보존 · 실패 칸 빈 칸,
스타일 = 역할 기본값)으로 마무리해 N5 판정. 재시도 가능한 일반 실패도 함께. 시간 초과라고 새 횟수를 주지 않는다(한도 = 가).
PM 2차 회신(11-27): 번역은 작업 단위 — 실패로 끝난 섹션은 빈 칸으로 진행하고 작업 전체 번역 성공 0 일 때만 막힌다(일반 흐름 포함).
라벨 · 로고 소진은 확정 정책(일부 섹션 실패 = 원본 + 배지로 N5)의 후순위 구현 전까지 지금 동작(막힘)이 임시 동작이다.
대역 · 준비는 test_job_state_timers · test_timeout_followups 와 같다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app import ai_adapters, execution, recovery
from app.flows import downstream
from tests.exec_db import requires_db
from tests.exec_fakes import FakeTranslator
from tests.test_job_state_timers import _age_state, _fresh, _q, _state, _to_n3, dburl, env  # noqa: F401 — fixture 재사용
from tests.test_timeout_followups import _inpaint_timeout, _sections

pytestmark = requires_db


def _session():
    from app import db as app_db

    return app_db.SessionLocal()


def _plan(job, **kw):
    db = _session()
    try:
        return downstream.job_retry_plan(db, job, **kw)
    finally:
        db.close()


def _retry_job(job):
    db = _session()
    try:
        return downstream.retry_job(db, job)
    finally:
        db.close()


def _attempts(job):
    return _q("SELECT id, stage, unit_id, status, error_code, is_current, retry_count, max_retry, supersedes_task_id "
              "FROM job_async_task WHERE job_id = :j AND parent_task_id IS NOT NULL ORDER BY id", j=job)


def _job_row(job):
    return _q("SELECT status, current_step, user_facing_status, state_entered_at FROM job WHERE id = :j", j=job)[0]


def _cleanup(ids, env):
    env["q"].messages = []
    assert ids["c"].delete(f"/v1/jobs/{ids['job']}").status_code == 200  # 처리 중으로 남기면 다음 시험의 점검에 걸린다


def _stage_timeout(env, hang: set[str]):
    """N4: `hang` 작업만 멈춘 채 30분 초과 → 오류. ids."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip=hang)
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N4", "failed")
    return ids


def _timed_out(job, stage):
    return [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = :s AND is_current "
                                "AND error_code = 'PROCESSING_TIMEOUT' ORDER BY id", j=job, s=stage)]


# ---- 시간 초과: 남은 횟수 안에서 다시 실행 · 30분 새로 셈 · 새 횟수 없음 ----------------------------------------------------
def test_timed_out_inpaint_and_translate_retry_within_existing_limits_then_n5(env):
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    before = _attempts(job)
    p = _plan(job)
    assert p["state"] == "ok" and p["available"] is True and p["blocking"] == [] and p["fallback"] == []
    assert {i["taskId"] for i in p["retry"]} == set(tr) | set(inp)
    for i in p["retry"]:  # 단계별 기존 한도 그대로(시간 초과라고 늘리지 않음)
        assert i["maxRetry"] == downstream.MAX_RETRY[i["stage"]] and i["retryCount"] < i["maxRetry"]
    assert _attempts(job) == before and env["q"].messages == []  # 판정만 — 시도 · 메시지 없음
    r = _retry_job(job)
    assert len(r["retriedTaskIds"]) == len(tr) + len(inp) and r["status"] == "processing"
    assert _state(ids) == ("processing", "N4", "translating") and _fresh(job) is True  # 30분 새로 셈
    old = {a["id"]: a for a in before}
    for a in _attempts(job):
        if a["supersedes_task_id"] in old and a["is_current"]:
            o = old[a["supersedes_task_id"]]
            assert (a["retry_count"], a["max_retry"]) == (o["retry_count"] + 1, o["max_retry"])  # 누적 · 한도 승계
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    units = {r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=inp)}
    secs = {s["id"]: s for s in _sections(job)}
    for u in units:  # 다시 실행해 성공 → 대체 기록은 지워진다
        assert (secs[u]["inpaint_status"], secs[u]["warning_badge"]) == ("done", None)


# ---- 소진: 실패 규칙으로 마무리 ------------------------------------------------------------------------------------------
def test_exhausted_inpaint_closes_with_original_background_and_failure_badge(env, monkeypatch):
    monkeypatch.setitem(downstream.MAX_RETRY, "inpaint", 0)  # 시도 행 한도는 생성 때 고정 — 소진 상태를 만든다
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    p = _plan(job)
    assert {i["taskId"] for i in p["fallback"]} == set(inp) and {i["reason"] for i in p["fallback"]} == {"fallback"}
    assert {i["taskId"] for i in p["retry"]} == set(tr) and p["blocking"] == []
    r = _retry_job(job)
    assert {(f["stage"], f["sectionId"]) for f in r["fallbackClosed"]} == {("inpaint", i["sectionId"]) for i in p["fallback"]}
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'inpaint'", j=job)[0]["n"] == len(inp)
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    secs = {s["id"]: s for s in _sections(job)}
    for i in p["fallback"]:
        assert (secs[i["sectionId"]]["inpaint_status"], secs[i["sectionId"]]["warning_badge"]) == ("failed", "processing_failed")


@pytest.mark.parametrize("exhausted", [False, True])
def test_timed_out_style_retries_or_closes_with_role_defaults(env, monkeypatch, exhausted):
    if exhausted:
        monkeypatch.setitem(downstream.MAX_RETRY, "style", 0)
    ids = _stage_timeout(env, {"app.tasks.run_style"})
    job = ids["job"]
    sty = _timed_out(job, "style")
    assert sty
    p = _plan(job)
    want = "fallback" if exhausted else "retry"
    assert {i["taskId"] for i in p[want] if i["stage"] == "style"} == set(sty) and p["blocking"] == []
    r = _retry_job(job)
    new_style = [t for t in r["retriedTaskIds"] if _q("SELECT stage FROM job_async_task WHERE id = :t", t=t)[0]["stage"] == "style"]
    assert len(new_style) == (0 if exhausted else len(sty))  # 소진이면 새 스타일 시도 없음
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    units = [r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=sty)]
    for u in units:
        st = _q("SELECT status FROM job_async_task WHERE job_id = :j AND stage = 'style' AND unit_id = :u AND is_current", j=job, u=u)[0]
        assert st["status"] == ("failed" if exhausted else "done")  # 소진 = 실패 시도가 최종 → 렌더가 역할 기본값 적용
        pv = _q("SELECT status FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND unit_id = :u AND is_current", j=job, u=u)
        assert pv and pv[0]["status"] == "done"


def test_translate_exhausted_with_successes_proceeds_with_blank_failed_block(env, monkeypatch):
    monkeypatch.setitem(downstream.MAX_RETRY, "translate", 1)  # 부분 실패 뒤 JOB-06 1회로 한도 소진
    ids, sid, target, cur = _translate_partial_then_timeout(env)
    job = ids["job"]
    assert cur["retry_count"] == cur["max_retry"] == 1
    s = _successes(cur, sid)
    assert s["hasSuccess"] is True
    p = _plan(job)
    assert [(i["taskId"], i["reason"]) for i in p["fallback"] if i["stage"] == "translate"] == [(cur["id"], "blank_kept")]
    assert p["blocking"] == [] and p["available"] is True
    before = {r["id"]: r["trans_1"] for r in _q("SELECT id, trans_1 FROM text_block WHERE section_id = :s AND trans_1 IS NOT NULL",
                                                 s=sid)}
    assert before and target not in before
    r = _retry_job(job)
    assert all(_q("SELECT stage FROM job_async_task WHERE id = :t", t=t)[0]["stage"] != "translate" for t in r["retriedTaskIds"])
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s",
              j=job, s=sid)[0]["n"] == 2  # 소진 — 새 번역 시도 없음(처음 + JOB-06 1회)
    row = _q("SELECT trans_1, block_status FROM text_block WHERE id = :b", b=target)[0]
    assert row["trans_1"] is None  # 실패 칸은 빈 칸
    after = {r["id"]: r["trans_1"] for r in _q("SELECT id, trans_1 FROM text_block WHERE id = ANY(:b)", b=list(before))}
    assert after == before  # 성공분 보존 — 개수가 아니라 블록별 값 그대로


# ---- 원자성 · 정해지지 않은 경우는 지금처럼 막힘 ------------------------------------------------------------------------
def test_translate_exhausted_with_zero_successes_job_wide_stays_blocked_and_creates_nothing(env, monkeypatch):
    # PM 11-27: 작업 전체 번역 성공 0 일 때만 막힘. 여기서는 모든 섹션 번역이 시간 초과 · 한도 0 → 작업 전체 0
    monkeypatch.setitem(downstream.MAX_RETRY, "translate", 0)
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    p = _plan(job)
    assert {b["taskId"] for b in p["blocking"]} == set(tr) and {b["reason"] for b in p["blocking"]} == {"job_translation_zero"}
    assert p["available"] is False and {i["taskId"] for i in p["retry"]} == set(inp)
    before_att, before_job, before_sec = _attempts(job), _job_row(job), _sections(job)
    with pytest.raises(downstream.JobRetryBlocked) as e:
        _retry_job(job)
    assert {b["taskId"] for b in e.value.blocking} == set(tr)
    assert _attempts(job) == before_att and _job_row(job) == before_job and _sections(job) == before_sec
    assert env["q"].messages == []
    _cleanup(ids, env)


def test_timed_out_label_retries_within_limit_then_n5(env):
    ids = _stage_timeout(env, {"app.tasks.run_label"})
    job = ids["job"]
    labels = _timed_out(job, "label")
    assert labels
    p = _plan(job)
    assert {i["taskId"] for i in p["retry"]} == set(labels) and p["blocking"] == []
    r = _retry_job(job)
    assert len(r["retriedTaskIds"]) == len(labels) and _state(ids) == ("processing", "N4", "translating")
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")


def test_exhausted_label_stays_blocked(env, monkeypatch):
    monkeypatch.setitem(downstream.MAX_RETRY, "label", 0)  # 「원본 그대로」의 「전부 실패」 범위 확인 전 — 지금 동작 유지
    ids = _stage_timeout(env, {"app.tasks.run_label"})
    job = ids["job"]
    labels = _timed_out(job, "label")
    p = _plan(job)
    assert {b["taskId"] for b in p["blocking"]} == set(labels) and {b["reason"] for b in p["blocking"]} == {"retry_limit"}
    before = _attempts(job)
    with pytest.raises(downstream.JobRetryBlocked):
        _retry_job(job)
    assert _attempts(job) == before and env["q"].messages == []
    _cleanup(ids, env)


# ---- 재시도 가능한 일반 실패도 함께 ---------------------------------------------------------------------------------------
def test_general_translate_failure_retried_together_with_timed_out_inpaint(env):
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id",
              j=job)
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
    ai_adapters.set_adapters(translator=FakeTranslator(fail_keys={f"blk_{target}"}))  # 일반 실패(부분 실패)
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N4", "failed")
    sid = _q("SELECT section_id FROM text_block WHERE id = :b", b=target)[0]["section_id"]
    part = _q("SELECT id, error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current",
              j=job, s=sid)[0]
    assert part["error_code"] == "TRANSLATE_PARTIAL"
    inp = _timed_out(job, "inpaint")
    p = _plan(job)
    retry = {i["taskId"] for i in p["retry"]}
    assert part["id"] in retry and set(inp) <= retry and p["blocking"] == []
    ai_adapters.set_adapters(translator=FakeTranslator())
    _retry_job(job)
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    assert _q("SELECT trans_1 FROM text_block WHERE id = :b", b=target)[0]["trans_1"] is not None


# ---- 중복 · 동시 호출: 새 시도 한 벌 -------------------------------------------------------------------------------------
def test_concurrent_and_duplicate_calls_make_one_set_of_attempts(env, dburl):
    import threading
    import time

    from sqlalchemy import create_engine

    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    out: dict = {}

    def call(name):
        try:
            out[name] = ("ok", _retry_job(job))
        except downstream.JobRetryRejected as ex:
            out[name] = ("rejected", ex.code)

    eng = create_engine(dburl)
    try:
        with eng.connect() as holder:
            holder.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": job})
            ts = [threading.Thread(target=call, args=(n,)) for n in ("a", "b")]
            [t.start() for t in ts]
            waiting = 0
            for _ in range(200):
                with eng.connect() as probe:
                    waiting = probe.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                                 "AND datname = current_database()")).scalar()
                if waiting >= 2:
                    break
                time.sleep(0.05)
            assert waiting >= 2, waiting
            holder.rollback()
            [t.join(timeout=30) for t in ts]
    finally:
        eng.dispose()
    assert sorted(v[0] for v in out.values()) == ["ok", "rejected"], out
    assert [v[1] for v in out.values() if v[0] == "rejected"] == ["INVALID_STATE"]
    with pytest.raises(downstream.JobRetryRejected) as e:  # 이미 처리 중 — 다시 불러도 새 시도 없음
        _retry_job(job)
    assert e.value.code == "INVALID_STATE"
    atts = _attempts(job)
    trs = [a for a in atts if a["stage"] == "translate"]
    assert len(trs) == 2 * len(tr)  # 옛 시도 + 새 시도 한 벌
    assert sorted(a["supersedes_task_id"] for a in trs if a["is_current"]) == sorted(tr)
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")


# ---- 번역 성공분: 논리 작업 단위 ----------------------------------------------------------------------------------------
def _translate_partial_then_timeout(env):
    """한 블록만 번역 실패(부분 실패) → JOB-06 재시도(남은 1블록) → 그 재시도가 시간 초과. (ids, 섹션, 실패 블록, 시간 초과 시도)"""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id",
              j=job)
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
    ai_adapters.set_adapters(translator=FakeTranslator(fail_keys={f"blk_{target}"}))
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    sid = _q("SELECT section_id FROM text_block WHERE id = :b", b=target)[0]["section_id"]
    part = _q("SELECT id, error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current",
              j=job, s=sid)[0]
    assert part["error_code"] == "TRANSLATE_PARTIAL"
    r = c.post(f"/v1/jobs/{job}/tasks/{part['id']}/retry")
    assert r.status_code == 200, r.text
    env["q"].messages = []  # 재시도 번역이 응답하지 않는다
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N4", "failed")
    cur = _q("SELECT id, error_code, retry_count, max_retry, target_count FROM job_async_task WHERE job_id = :j AND stage = 'translate' "
             "AND unit_id = :s AND is_current", j=job, s=sid)[0]
    assert cur["error_code"] == "PROCESSING_TIMEOUT" and cur["target_count"] == 1
    return ids, sid, target, cur


def _successes(cur, sid):
    run = _q("SELECT parent_task_id FROM job_async_task WHERE id = :t", t=cur["id"])[0]["parent_task_id"]
    db = _session()
    try:
        return downstream.translate_logical_successes(db, run, sid)
    finally:
        db.close()


def test_translate_successes_counted_per_logical_unit_after_timeout(env):
    ids, sid, target, cur = _translate_partial_then_timeout(env)
    job = ids["job"]
    s = _successes(cur, sid)
    # 마지막 시도(대상 1블록)는 채택 기록 없이 시간 초과 — 그래도 앞 시도의 성공분(고정 입력 preserved)은 0 이 아니다
    assert s["attemptId"] == cur["id"] and s["applied"] == 0 and s["preserved"] >= 1 and s["remaining"] == 1 and s["hasSuccess"] is True
    p = _plan(job)  # 한도 남음 → 실패 블록만 재시도
    assert [(i["taskId"], i["reason"]) for i in p["retry"] if i["stage"] == "translate"] == [(cur["id"], "retry")]
    r = _retry_job(job)
    new = [t for t in r["retriedTaskIds"] if _q("SELECT stage FROM job_async_task WHERE id = :t", t=t)[0]["stage"] == "translate"]
    assert len(new) == 1
    m = _q("SELECT input_manifest, target_count FROM job_async_task WHERE id = :t", t=new[0])[0]
    assert m["target_count"] == 1 and [t["block_id"] for t in m["input_manifest"]["targets"]] == [target]
    assert len(m["input_manifest"]["preserved"]) == s["preserved"]
    _cleanup(ids, env)


# ---- 시간 초과로 닫힌 옛 시도의 늦은 결과는 계속 거절 ------------------------------------------------------------------------
def _gpu_inpaint_timeout(env):
    from app.flows.gpu_worker import InProcessControlClient

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})  # 인페인트(GPU)만 멈춤 — 번역 · 스타일은 끝남
    cl = InProcessControlClient("gpu-late-recovery")
    leases = {r["id"]: cl.acquire(r["id"]) for r in _q(
        "SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current AND status = 'pending'", j=job)}
    assert leases
    env["q"].messages = [m for m in env["q"].messages if m[0] != "app.tasks.run_inpaint"]
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N4", "failed")
    return ids, cl, leases


def _late_register(cl, tid, got):
    body = {"outcome": "failed", "target_count": 1, "input_fingerprint": got["fingerprint"],
            "payload": {"error_code": "INPAINT_FAILED", "message": "late", "retryable": False}, "artifacts": []}
    try:
        cl.register(tid, got["epoch"], got["token"], body)
    except execution.ExecutionError:
        pass


@pytest.mark.parametrize("exhausted", [False, True])
def test_late_result_of_timed_out_attempt_rejected_after_job_retry(env, monkeypatch, exhausted):
    if exhausted:
        monkeypatch.setitem(downstream.MAX_RETRY, "inpaint", 0)
    ids, cl, leases = _gpu_inpaint_timeout(env)
    job = ids["job"]
    p = _plan(job)
    want = "fallback" if exhausted else "retry"
    assert {i["taskId"] for i in p[want]} == set(leases) and p["blocking"] == []
    r = _retry_job(job)
    assert len(r["retriedTaskIds"]) == (0 if exhausted else len(leases))
    if exhausted:
        assert r["createdTaskIds"]  # 대체로 끝난 섹션의 미리보기를 이때 만든다
    for tid, got in leases.items():  # 옛 권한으로 늦게 온 결과
        _late_register(cl, tid, got)
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    for tid in leases:
        assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t AND state = 'adopted'", t=tid)[0]["n"] == 0
        assert _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=tid)[0] == {
            "status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    units = {_q("SELECT unit_id FROM job_async_task WHERE id = :t", t=t)[0]["unit_id"] for t in leases}
    secs = {s["id"]: s for s in _sections(job)}
    for u in units:
        expect = ("failed", "processing_failed") if exhausted else ("done", None)
        assert (secs[u]["inpaint_status"], secs[u]["warning_badge"]) == expect


def test_plan_outside_failed_n4_is_not_available(env):
    ids = _to_n3(env)
    p = _plan(ids["job"])
    assert p["available"] is False and p["state"] == "invalid_state"
    with pytest.raises(downstream.JobRetryRejected) as e:
        _retry_job(ids["job"])
    assert e.value.code == "INVALID_STATE"


# ---- 판정 경계(시도 하나 → 결정 · 사유) — 소진 / 소진 아닌 재시도 불가 / 대상 0 / 로고 한도 0 ---------------------------------
def _att(stage, *, error_code="PROCESSING_TIMEOUT", retry_count=0, max_retry=1, target_count=1):
    return {"id": 1, "stage": stage, "unit_id": 1, "status": "failed", "error_code": error_code,
            "retry_count": retry_count, "max_retry": max_retry, "target_count": target_count}


@pytest.mark.parametrize("att, expected", [
    (_att("logo", max_retry=0), (downstream.BLOCK, "retry_limit")),  # 로고 기본 한도 0 — 시간 초과여도 막힘
    (_att("label", retry_count=1, max_retry=1), (downstream.BLOCK, "retry_limit")),
    # 시간 초과 아닌 라벨 · 로고 실패 — 지금처럼 막힘(사용자 재시도 종류 아님), 사유만 소진 / 소진 아님
    (_att("label", error_code="LABEL_FAILED", retry_count=1, max_retry=1), (downstream.BLOCK, "retry_limit")),  # 자동 재시도로 소진
    (_att("label", error_code="LABEL_FAILED"), (downstream.BLOCK, "not_retryable")),
    (_att("logo", error_code="LOGO_FAILED", max_retry=0), (downstream.BLOCK, "retry_limit")),
    # 번역(PM 11-27): 다시 할 수 없으면 오류 종류와 관계없이 섹션 단위로는 빈 칸 진행 — 작업 전체 0 은 job_retry_plan 이 따로 막는다
    (_att("translate", error_code="AI_ADAPTER_UNAVAILABLE", retry_count=2, max_retry=2), (downstream.FALLBACK, "blank_kept")),
    # 인페인트 · 스타일 일반 실패는 사용자 재시도 종류(JOB-06 translate · render)가 아니다 — 횟수가 남아도 지금 대체 그대로
    (_att("inpaint", error_code="INPAINT_FAILED"), (downstream.FALLBACK, "fallback")),
    (_att("style", error_code="STYLE_FAILED"), (downstream.FALLBACK, "fallback")),
    (_att("translate", target_count=0), (downstream.FALLBACK, "partial_kept")),  # 대상 0 — 다시 할 것 없음
    (_att("translate", error_code="AI_ADAPTER_UNAVAILABLE"), (downstream.FALLBACK, "blank_kept")),  # 연결 불가 — 다시 하지 않고 빈 칸
    (_att("translate", error_code="TRANSLATE_FAILED", retry_count=2, max_retry=2), (downstream.FALLBACK, "blank_kept")),
    (_att("translate", retry_count=2, max_retry=2), (downstream.FALLBACK, "blank_kept")),  # 시간 초과 소진(성공분 여부는 작업 단위로)
    (_att("translate", error_code="TRANSLATE_PARTIAL", retry_count=2, max_retry=2), (downstream.FALLBACK, "partial_kept")),
    (_att("translate", error_code="TRANSLATE_FAILED", retry_count=0, max_retry=2), (downstream.RETRY, "retry")),  # 일반 실패 함께
    (_att("inpaint", retry_count=1, max_retry=1), (downstream.FALLBACK, "fallback")),
    (_att("style", retry_count=0, max_retry=1), (downstream.RETRY, "timeout_retry")),
])
def test_classify_boundaries(att, expected):
    # 이 경우들은 DB 를 읽지 않는다(시간 초과 + 소진 번역의 성공분 판정만 DB — 위 통합 시험이 다룸)
    assert downstream._classify(None, {"id": 1}, att, downstream._POLICY) == expected


@pytest.mark.parametrize("retry_count, blocked, expected", [
    (0, None, (downstream.RETRY, "retry")),                                   # 남은 횟수 — 다시 그림
    (2, None, (downstream.FALLBACK, "original_shown")),                       # 소진 — 원본 표시로 N5(기존 진입 조건)
    (0, "renderer_changed", (downstream.FALLBACK, "not_retryable_rerender")),  # 소진 아닌 재시도 불가 — progress 가 새 입력으로 다시 그림
])
def test_classify_preview_branches(monkeypatch, retry_count, blocked, expected):
    monkeypatch.setattr(downstream.execution, "retry_blocked", lambda att: blocked)
    att = _att("preview", error_code="RENDER_FAILED", retry_count=retry_count, max_retry=2)
    assert downstream._classify(None, {"id": 1}, att, downstream._POLICY) == expected


def test_translate_success_kept_after_user_edits_preserved_block(env):
    """보존분을 N5 처럼 사용자가 고치면(revision 바뀜) preserved 에서 빠지지만 직접 입력으로 세어 성공분 판정이 뒤집히지 않는다."""
    ids, sid, target, cur = _translate_partial_then_timeout(env)
    s0 = _successes(cur, sid)
    kept = _q("SELECT id FROM text_block WHERE section_id = :s AND trans_1 IS NOT NULL AND id <> :t ORDER BY id", s=sid, t=target)
    assert kept and s0["preserved"] >= 1
    for r in kept:  # 보존분 전부를 고친 상태로 만든다
        _q("UPDATE text_block SET trans_1 = 'edited by user', block_status = 'edited', revision = revision + 1 "
           "WHERE id = :b RETURNING id", b=r["id"])
    s1 = _successes(cur, sid)
    assert s1["preserved"] == 0 and s1["userEdited"] == s0["preserved"] and s1["hasSuccess"] is True  # 한 블록은 한 번만
    _cleanup(ids, env)


def test_translate_success_not_counted_when_preserved_blocks_are_stale(env):
    """무효가 된 과거 결과는 성공으로 세지 않는다(독립 검토 11-27 보완): 보존분 블록의 revision 이 바뀌었는데 셀러 입력도 아니면
    (기계 번역 그대로 · 대조 탈락) preserved · userEdited 어디에도 들지 않는다."""
    ids, sid, target, cur = _translate_partial_then_timeout(env)
    s0 = _successes(cur, sid)
    assert s0["preserved"] >= 1 and s0["hasSuccess"] is True
    _q("UPDATE text_block SET revision = revision + 1 WHERE section_id = :s AND trans_1 IS NOT NULL AND block_status = 'machine' "
       "RETURNING id", s=sid)
    s1 = _successes(cur, sid)
    assert (s1["preserved"], s1["applied"], s1["userEdited"], s1["hasSuccess"]) == (0, 0, 0, False)
    _cleanup(ids, env)


def test_translate_general_failure_exhausted_with_successes_proceeds_with_blank(env, monkeypatch):
    """PM 11-27: 시간 초과가 아닌 일반 실패로 한도를 다 쓴 번역도 같은 작업 단위 기준 — 성공분이 있으면 빈 칸으로 N5."""
    monkeypatch.setitem(downstream.MAX_RETRY, "translate", 1)
    ids, sid, target, cur = _translate_partial_then_timeout(env)
    job = ids["job"]
    _q("UPDATE job_async_task SET error_code = 'TRANSLATE_FAILED' WHERE id = :t RETURNING id", t=cur["id"])  # 일반 실패로 바꿔 둠
    assert _successes(cur, sid)["hasSuccess"] is True
    p = _plan(job)
    assert [(i["taskId"], i["reason"]) for i in p["fallback"] if i["stage"] == "translate"] == [(cur["id"], "blank_kept")]
    assert p["blocking"] == [] and p["available"] is True
    _retry_job(job)
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    assert _q("SELECT trans_1 FROM text_block WHERE id = :b", b=target)[0]["trans_1"] is None  # 실패 칸은 빈 칸


def test_normal_flow_one_section_all_failed_translation_does_not_block_others(env):
    """PM 11-27: 섹션 하나의 번역 성공분 0 이 다른 섹션의 성공분까지 막지 않는다 — 재시도 전 일반 흐름에서도.
    (모든 섹션이 0 이면 N4 — test_exec_downstream.test_translation_failed_with_other_code_is_still_n4_error)"""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    secs = [r["s"] for r in _q("SELECT DISTINCT tb.section_id AS s FROM text_block tb JOIN section s ON s.id = tb.section_id "
                               "WHERE s.job_id = :j ORDER BY 1", j=job)]
    assert len(secs) >= 2
    bad = secs[0]
    keys = {f"blk_{r['id']}" for r in _q("SELECT id FROM text_block WHERE section_id = :s", s=bad)}
    ai_adapters.set_adapters(translator=FakeTranslator(fail_keys=keys))  # 그 섹션 블록 전부 실패(자동 재시도도 실패)
    try:
        assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
        env["q"].drain()
        assert _state(ids) == ("review", "N5", "reviewing")
        tr = _q("SELECT status, target_count FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s "
                "AND is_current", j=job, s=bad)[0]
        assert tr["status"] == "failed" and tr["target_count"] > 0
        assert all(r["trans_1"] is None for r in _q("SELECT trans_1 FROM text_block WHERE section_id = :s", s=bad))  # 빈 칸
        assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND unit_id = :s",
                  j=job, s=bad)[0]["n"] >= 1  # 그 섹션도 미리보기까지 진행
        others = _q("SELECT count(*) AS n FROM text_block WHERE section_id = ANY(:s) AND trans_1 IS NOT NULL", s=secs[1:])[0]["n"]
        assert others > 0  # 다른 섹션 성공분은 그대로
    finally:
        ai_adapters.set_adapters(translator=FakeTranslator())


def test_zero_success_section_waits_while_other_section_translation_is_running(env):
    """PM 11-27 경계(통합): 다른 섹션 번역이 아직 진행 중이면, 성공분 0 으로 끝난 섹션은 미리보기를 만들지 않고 기다린다.
    다른 섹션이 성공하면 그 섹션도 빈 칸으로 진행해 N5. (큐에서 섹션 A 번역만 먼저 처리하고 섹션 B 번역은 잡아 둔다)"""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    secs = [r["s"] for r in _q("SELECT DISTINCT tb.section_id AS s FROM text_block tb JOIN section s ON s.id = tb.section_id "
                               "WHERE s.job_id = :j ORDER BY 1", j=job)]
    assert len(secs) >= 2
    a = secs[0]
    keys = {f"blk_{r['id']}" for r in _q("SELECT id FROM text_block WHERE section_id = :s", s=a)}
    ai_adapters.set_adapters(translator=FakeTranslator(fail_keys=keys))
    held: list = []

    def unit_of(args):
        r = _q("SELECT unit_id FROM job_async_task WHERE id = :t", t=args[0])
        return r[0]["unit_id"] if r else None

    try:
        assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
        while True:  # 섹션 A 번역(자동 재시도 포함)만 처리하고, 다른 섹션 번역 메시지는 잡아 둔다
            skipped = env["q"].drain(skip={"app.tasks.run_translate"})
            tr_msgs = [m for m in skipped if m[0] == "app.tasks.run_translate"]
            mine = [m for m in tr_msgs if unit_of(m[1]) == a]
            held += [m for m in tr_msgs if m not in mine]
            if not mine:
                break
            import app.tasks as tasks
            for _, args in mine:
                tasks.run_translate(*args)
        tr_a = _q("SELECT status FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current",
                  j=job, s=a)[0]
        assert tr_a["status"] == "failed" and held  # A 는 성공분 0 으로 끝났고, 다른 섹션 번역은 아직 대기
        assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND unit_id = :s",
                  j=job, s=a)[0]["n"] == 0  # 기다림 — 미리보기를 만들지 않음
        assert _state(ids)[:2] == ("processing", "N4")  # 막지도(N4 오류) 않음
        env["q"].messages = held + env["q"].messages  # 잡아 둔 다른 섹션 번역을 처리 → 성공분 생김
        env["q"].drain()
        assert _state(ids) == ("review", "N5", "reviewing")
        assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND unit_id = :s",
                  j=job, s=a)[0]["n"] >= 1  # 그 뒤 A 도 빈 칸으로 진행
        assert all(r["trans_1"] is None for r in _q("SELECT trans_1 FROM text_block WHERE section_id = :s", s=a))
    finally:
        ai_adapters.set_adapters(translator=FakeTranslator())


def test_zero_target_retry_section_successes_kept_in_job_wide_count(env, monkeypatch):
    """독립 검토(11-27) 재현: 섹션 A 의 현재 번역 시도가 「이번 재시도 대상 0」(실패 블록을 셀러가 직접 입력 → 정상 생략 done)이고
    A 에는 앞 시도의 유효 성공분이 있는데, 다른 섹션은 모두 번역 성공 0 으로 한도를 다 썼다.
    현재 시도의 target_count(= 0)만 보고 A 를 작업 단위 집계에서 빼면 「작업 전체 성공 0」으로 거짓 막힘이 된다 —
    A 를 대상 섹션으로 세어 다른 섹션은 빈 칸 대체 진행, [다시 시도] 가 받아져야 한다."""
    import app.tasks as tasks

    # 번역 한도 1: 다른 섹션은 자동 재시도 1회로 한도를 다 쓰고(성공분 0), A 의 부분 실패는 자동 재시도가 없어 사용자 재시도 1회가 남는다
    monkeypatch.setitem(downstream.MAX_RETRY, "translate", 1)
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    rows = _q("SELECT tb.id, tb.section_id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id "
              "WHERE s.job_id = :j ORDER BY tb.id", j=job)
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]
    a = target["section_id"]
    others = sorted({r["section_id"] for r in rows if r["section_id"] != a})
    assert others
    keys = {f"blk_{target['id']}"} | {f"blk_{r['id']}" for r in rows if r["section_id"] != a}
    ai_adapters.set_adapters(translator=FakeTranslator(fail_keys=keys))  # A = 부분 실패, 나머지 섹션 = 전부 실패
    try:
        assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
        env["q"].drain(skip={"app.tasks.run_inpaint"})  # 인페인트만 멈춤 → 30분 초과 N4 오류
        env["q"].messages = [m for m in env["q"].messages if m[0] != "app.tasks.run_inpaint"]
        _age_state(job, 1801)
        assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N4", "failed")

        part = _q("SELECT id, error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current",
                  j=job, s=a)[0]
        assert part["error_code"] == "TRANSLATE_PARTIAL"
        # 셀러가 A 의 실패 블록을 직접 입력 → 그 뒤 만든 재시도는 다시 할 실패 블록이 없어 대상 0
        _q("UPDATE text_block SET trans_1 = '셀러 입력', block_status = 'edited', revision = revision + 1 WHERE id = :b RETURNING id",
           b=target["id"])
        db = _session()
        try:
            att = dict(db.execute(text("SELECT * FROM job_async_task WHERE id = :t"), {"t": part["id"]}).mappings().one())
            plan = downstream.translate_retry_plan(db, att)
            assert plan["target_count"] == 0
            new = execution.create_retry(db, part["id"], "user", **plan)
            db.commit()
        finally:
            db.close()
        tasks.run_translate(new)
        cur = _q("SELECT id, status, target_count FROM job_async_task WHERE id = :t", t=new)[0]
        assert (cur["status"], cur["target_count"]) == ("done", 0)
        assert _successes(cur, a)["hasSuccess"] is True  # 앞 시도 보존분(+ 직접 입력)이 유효
        oth = _q("SELECT status, retry_count, max_retry, target_count FROM job_async_task WHERE job_id = :j AND stage = 'translate' "
                 "AND is_current AND unit_id = ANY(:s)", j=job, s=others)
        assert oth and all(o["status"] == "failed" and o["retry_count"] >= o["max_retry"] and o["target_count"] > 0 for o in oth)
        assert _state(ids) == ("failed", "N4", "failed")

        p = _plan(job)
        assert p["blocking"] == [] and p["available"] is True  # 거짓 「작업 전체 성공 0」 막힘 없음
        assert {i["sectionId"] for i in p["fallback"] if i["stage"] == "translate" and i["reason"] == "blank_kept"} == set(others)
        _retry_job(job)
        env["q"].drain()
        assert _state(ids) == ("review", "N5", "reviewing")
        assert all(r["trans_1"] is None for r in _q("SELECT trans_1 FROM text_block WHERE section_id = ANY(:s)", s=others))  # 빈 칸
        assert _q("SELECT trans_1 FROM text_block WHERE id = :b", b=target["id"])[0]["trans_1"] == "셀러 입력"
    finally:
        ai_adapters.set_adapters(translator=FakeTranslator())


# ---- 작업 단위 번역 판정(PM 11-27) — 입력별 경계 ----------------------------------------------------------------------
def _tr(sid, status, *, target=2, code=None, expected=None):
    a = {"stage": "translate", "unit_id": sid, "status": status, "target_count": target, "error_code": code}
    if expected is not None:  # 재시도 시도: target_count = 이번 실패 블록 수, expected = 섹션 번역 대상
        a["input_manifest"] = {"expected": expected}
    return a


def _lab(sid, status):
    return {"stage": "label", "unit_id": sid, "status": status, "target_count": 0, "error_code": None}


@pytest.mark.parametrize("atts, logical, expected", [
    # 보존분 · 채택분 · 사용자 입력만 있는 실패 섹션도 성공으로 센다(앞 시도의 유효 성공분 보존) → ok
    ([_tr(1, "failed", code="PROCESSING_TIMEOUT"), _tr(2, "failed", code="TRANSLATE_FAILED")], {1: True}, "ok"),
    # 모든 번역 대상 섹션이 끝났고 성공 0 → zero(이때만 N4)
    ([_tr(1, "failed", code="PROCESSING_TIMEOUT"), _tr(2, "failed", code="AI_ADAPTER_UNAVAILABLE")], {}, "zero"),
    # 번역 대상 0 섹션(정상 생략 done)과 성공 0 섹션이 섞임 → 대상 0 은 셈에서 빠져 zero
    ([_tr(1, "done", target=0), _tr(2, "failed", code="TRANSLATE_FAILED")], {}, "zero"),
    # 번역 대상 섹션이 하나도 없음 → none(막지 않음)
    ([_tr(1, "done", target=0), _tr(2, "done", target=0)], {}, "none"),
    # 다른 섹션 번역 진행 중(재시도 포함) → pending(기다림)
    ([_tr(1, "failed", code="TRANSLATE_FAILED"), _tr(2, "pending")], {}, "pending"),
    # 진행 중 재시도에 앞 시도 보존분이 있으면 → ok
    ([_tr(1, "failed", code="TRANSLATE_FAILED"), _tr(2, "running")], {2: True}, "ok"),
    # 부분 실패는 성공분 있음
    ([_tr(1, "failed", code="TRANSLATE_PARTIAL"), _tr(2, "failed", code="TRANSLATE_FAILED")], {}, "ok"),
    # 라벨이 최종 실패한 섹션(번역 시도 없음)은 셈에서 빠진다 — 그 섹션은 라벨 임시 막힘으로 따로 막힌다
    ([_lab(1, "failed"), _tr(2, "failed", code="TRANSLATE_FAILED")], {}, "zero"),
    ([_lab(1, "failed"), _tr(2, "done")], {}, "ok"),
    # 라벨이 아직 진행 중인 섹션(번역 시도가 곧 생김) → pending
    ([_lab(1, "pending"), _tr(2, "failed", code="TRANSLATE_FAILED")], {}, "pending"),
    # 이번 재시도 대상 0(실패 블록 없음 → 정상 생략 done)이어도 섹션 번역 대상이 있으면 센다 — 유효 성공분이 있으면 ok(독립 검토 11-27)
    ([_tr(1, "done", target=0, expected=[11, 12]), _tr(2, "failed", code="TRANSLATE_FAILED")], {1: True}, "ok"),
    # 같은 경우라도 그 섹션의 유효 성공분이 없으면(무효가 된 보존분 등) 성공으로 세지 않는다 → zero
    ([_tr(1, "done", target=0, expected=[11, 12]), _tr(2, "failed", code="TRANSLATE_FAILED")], {}, "zero"),
    # 섹션 번역 대상 자체가 없는 시도(expected 없음)는 성공분이 있다고 나와도 셈에서 빠진다 → 대상 섹션은 2 뿐
    ([_tr(1, "done", target=0, expected=[]), _tr(2, "failed", code="TRANSLATE_FAILED")], {1: True}, "zero"),
])
def test_translation_outcome_job_level(monkeypatch, atts, logical, expected):
    monkeypatch.setattr(downstream, "translate_logical_successes",
                        lambda db, run_id, sid: {"hasSuccess": logical.get(sid, False)})
    by = {(a["stage"], a["unit_id"]): a for a in atts}
    assert downstream._translation_outcome(None, 1, [1, 2], by) == expected


@pytest.mark.parametrize("stages, expected", [
    ([], False),
    (["inpaint", "style", "preview"], False),       # 번역 결과를 바꾸지 않는 재시도
    (["translate"], True),                          # 번역 재시도 — 결과를 기다린다
    (["label"], True),                              # 라벨 재시도 → 채택 뒤 그 섹션 번역이 새로 생긴다(검토 지적 1)
    (["logo", "inpaint"], True),
])
def test_job_retry_waits_when_translation_may_still_change(stages, expected):
    assert downstream._translation_may_change([{"stage": s} for s in stages]) is expected
