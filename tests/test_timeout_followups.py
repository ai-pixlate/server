"""처리 시간 상한(README 7.8 ⑤) 뒤 정합성 — #157 독립 검토 결함 후보 3건의 재현 · 회귀 시험.

① N4 인페인트 시도가 시간 초과로 닫힌 뒤 번역 [다시 시도]로 N5 까지 가면, 인페인트 실패와 같은 대체 기록
   (원본 배경 · inpaint_status failed · warning_badge processing_failed)이 남아야 한다. 시간 초과 처리에서 자동 재시도 ·
   다음 단계 진행은 만들지 않는다(오류 상태 · 수동 재시도 원칙). 다시 처리해 성공하면 옛 경고는 남지 않는다.
② 인계 등록 뒤 · 채택 전에 시간 초과로 닫힌 시도의 인계는 채택 메시지 처리 여부와 무관하게 닫혀야 한다(배포 점검 gate 가
   열린 인계를 busy 로 센다). 재시도로 그 시도가 현재 시도가 아니게 돼도 열린 채 남지 않는다.
③ N6 누적 한도를 소진한 시도가 시간 초과되면 실행은 running 으로 남는다(지금 규칙 — 한도 경계는 PM 결정 대기).
   그 영향 범위(점검 · 집계 · 배포 점검)를 고정한다.

대역 · 준비는 test_job_state_timers 와 같다.
"""
from __future__ import annotations

from sqlalchemy import text

from app import execution, recovery
from tests.exec_db import requires_db
from tests.test_job_state_timers import _age_state, _q, _state, _to_n3, _to_n5, dburl, env  # noqa: F401 — fixture 재사용

pytestmark = requires_db

ADOPT = execution.ADOPT_TASK


def _sections(job):
    return _q("SELECT id, inpaint_status, warning_badge FROM section WHERE job_id = :j ORDER BY id", j=job)


def _open_handoffs(job=None):
    sql = "SELECT count(*) AS n FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE h.state IN ('received','verifying','verified')"
    return _q(sql + (" AND a.job_id = :j" if job else ""), **({"j": job} if job else {}))[0]["n"]


def _gate_counts():
    """deploy/bin/state_check.py gate 와 같은 집계(큐 제외)."""
    return {
        "active": _q("SELECT count(*) AS n FROM job_async_task WHERE parent_task_id IS NOT NULL AND status IN ('pending','running')")[0]["n"],
        "handoffs": _open_handoffs(),
        "runs": _q("SELECT count(*) AS n FROM job_async_task WHERE parent_task_id IS NULL AND status = 'running'")[0]["n"],
    }


def _inpaint_timeout(env):
    """N4: 번역 · 인페인트만 멈춘 채 30분 초과 → 오류. (ids, 번역 시도 id 목록, 인페인트 시도 id 목록)"""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint", "app.tasks.run_translate"})
    tr = [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current "
                              "AND status = 'pending' ORDER BY id", j=job)]
    inp = [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current "
                               "AND status = 'pending' ORDER BY id", j=job)]
    assert tr and inp
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1 and _state(ids) == ("failed", "N4", "failed")
    return ids, tr, inp


# ---- ① 인페인트 시간 초과 → 대체 기록 ------------------------------------------------------------------------------
def test_inpaint_timeout_records_fallback_without_auto_retry_or_progress(env):
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    secs = {s["id"]: s for s in _sections(job)}
    inp_units = {r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=inp)}
    for sid in inp_units:  # 인페인트 실패와 같은 대체 기록
        assert (secs[sid]["inpaint_status"], secs[sid]["warning_badge"]) == ("failed", "processing_failed")
    # 자동 재시도 · 후속(미리보기) · 다음 단계 없음
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'inpaint'", j=job)[0]["n"] == len(inp)
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=job)[0]["n"] == 0
    assert env["q"].messages == []
    assert _state(ids) == ("failed", "N4", "failed")
    for _ in range(2):
        recovery.sweep()
    assert _state(ids) == ("failed", "N4", "failed") and env["q"].messages == []


def test_inpaint_timeout_then_translate_retry_reaches_n5_with_fallback_badge(env):
    ids, tr, inp = _inpaint_timeout(env)
    c, job = ids["c"], ids["job"]
    for t in tr:
        r = c.post(f"/v1/jobs/{job}/tasks/{t}/retry")
        assert r.status_code == 200, r.text
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    inp_units = {r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=inp)}
    body = c.get(f"/v1/jobs/{job}/sections").json()
    shown = {i["id"]: i["warningBadge"] for i in body["include"]}
    for sid in inp_units:
        assert shown.get(sid) == "processing_failed"  # 원본 배경으로 진행한 섹션은 경고가 보인다
    # 미리보기는 원본 배경(인페인트 결과 없음)
    for sid in inp_units:
        m = _q("SELECT input_manifest FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND unit_id = :s AND is_current",
               j=job, s=sid)[0]["input_manifest"]
        src = _q("SELECT image_key FROM section WHERE id = :s", s=sid)[0]["image_key"]
        assert src in str(m)


def test_inpaint_timeout_badge_cleared_after_abort_and_successful_reprocess(env):
    ids, tr, inp = _inpaint_timeout(env)
    c, job = ids["c"], ids["job"]
    r = c.post(f"/v1/jobs/{job}/abort")
    assert r.status_code == 200 and r.json()["returnTo"] == "N3"
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")
    units = {r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=inp)}
    secs = {s["id"]: s for s in _sections(job)}
    assert all(secs[u]["warning_badge"] is None for u in units)  # 다시 처리해 성공하면 옛 경고가 남지 않는다
    assert all(secs[u]["inpaint_status"] == "done" for u in units)


# ---- ② 인계 등록 뒤 · 채택 전 시간 초과 ------------------------------------------------------------------------------
def _translate_handoff_then_timeout(env):
    """원격(GPU) 인페인트 결과가 인계 등록까지 됐지만 채택 메시지가 처리되지 않은 채 30분 초과.
    (번역 · 미리보기는 프로세스 안 채택이라 인계 행이 없다 — 원격 인계 단계는 분석 · 인페인트 · 최종 렌더)"""
    from app.flows.gpu_worker import InProcessControlClient

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    cl = InProcessControlClient("gpu-slow-adopt")
    tr = [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current "
                              "AND status = 'pending' ORDER BY id", j=job)]
    assert tr
    for t in tr:
        got = cl.acquire(t)
        body = {"outcome": "failed", "target_count": 1, "input_fingerprint": got["fingerprint"],
                "payload": {"error_code": "INPAINT_FAILED", "message": "x", "retryable": False}, "artifacts": []}
        cl.register(t, got["epoch"], got["token"], body)
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = ANY(:t) AND state IN ('received','verifying','verified')",
              t=tr)[0]["n"] == len(tr), "인계가 등록되지 않음 — 준비 실패"
    env["q"].messages = []  # 채택 메시지가 아직 처리되지 않았다(채택 서버 지연 · 유실)
    _age_state(job, 1801)
    return ids, tr


def test_handoff_registered_before_timeout_is_closed_by_timeout_itself(env):
    ids, tr = _translate_handoff_then_timeout(env)
    job = ids["job"]
    assert recovery.sweep()["timed_out"] == 1
    env["q"].messages = []  # 채택 메시지가 유실됐다고 본다(채택 서버 중단 등)
    assert _open_handoffs(job) == 0
    rows = _q("SELECT h.state, h.reject_reason FROM task_handoff h WHERE h.task_id = ANY(:t)", t=tr)
    assert rows and all(r == {"state": "rejected", "reject_reason": "processing_timeout"} for r in rows)
    assert _q("SELECT count(*) AS n FROM task_artifact t JOIN task_handoff h ON h.id = t.handoff_id WHERE h.task_id = ANY(:t) "
              "AND t.state IN ('registered', 'verified')", t=tr)[0]["n"] == 0


def _artifacts(handoff_ids):
    return _q("SELECT id, handoff_id, state FROM task_artifact WHERE handoff_id = ANY(:h) ORDER BY id", h=list(handoff_ids))


def test_timeout_rejects_registered_artifacts_keeps_adopted_and_late_adopt_is_refused(env):
    """실제 산출물이 등록된 인계(원격 인페인트 성공 결과 — 배경 · 마스크 업로드 · 등록)로 확인한다.
    ① 섹션 A: 시간 초과 전에 정상 채택(산출물 adopted) ② 섹션 B: 등록까지 됐고 채택 메시지는 처리 전 → 30분 초과.
    시간 초과 처리 뒤: B 의 인계는 rejected(processing_timeout) · B 산출물은 모두 unadopted, A 의 인계 · 산출물 · 섹션 결과는 그대로.
    늦게 온 B 채택 메시지(run_adopt) · 인수 재시도도 채택하지 않는다."""
    from app import tasks
    from app.flows import gpu_worker
    from app.flows.gpu_worker import InProcessControlClient

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint", "app.tasks.run_translate"})  # 번역을 멈춰 작업이 처리 중으로 남게
    inp = [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current "
                               "AND status = 'pending' ORDER BY id", j=job)]
    assert len(inp) >= 2, "섹션 2개 이상 필요"
    a_tid, b_tid = inp[0], inp[1]
    env["q"].messages = []
    gpu_worker.set_client_factory(lambda: InProcessControlClient("gpu-real"))
    try:
        assert gpu_worker.execute_inpaint_remote(a_tid)["ran"] is True  # A: 계산 · 업로드 · 등록
        env["q"].messages = [m for m in env["q"].messages if m[0] == ADOPT]
        env["q"].drain()  # A 채택
        assert gpu_worker.execute_inpaint_remote(b_tid)["ran"] is True  # B: 등록까지
    finally:
        gpu_worker.set_client_factory(None)
    env["q"].messages = []  # B 의 채택 메시지는 아직 처리되지 않음
    a_h = [r["id"] for r in _q("SELECT id FROM task_handoff WHERE task_id = :t", t=a_tid)]
    b_h = [r["id"] for r in _q("SELECT id FROM task_handoff WHERE task_id = :t", t=b_tid)]
    assert _q("SELECT state FROM task_handoff WHERE id = ANY(:h)", h=a_h) == [{"state": "adopted"}]
    assert [r["state"] for r in _q("SELECT state FROM task_handoff WHERE id = ANY(:h)", h=b_h)] in (["received"], ["verified"], ["verifying"])
    a_arts0, b_arts0 = _artifacts(a_h), _artifacts(b_h)
    assert a_arts0 and all(r["state"] == "adopted" for r in a_arts0)
    assert b_arts0 and all(r["state"] in ("registered", "verified") for r in b_arts0)  # 정리 대상 산출물이 실제로 있다
    a_unit = _q("SELECT unit_id FROM job_async_task WHERE id = :t", t=a_tid)[0]["unit_id"]
    b_unit = _q("SELECT unit_id FROM job_async_task WHERE id = :t", t=b_tid)[0]["unit_id"]
    a_sec0 = _q("SELECT inpaint_status, inpaint_image_url, warning_badge FROM section WHERE id = :s", s=a_unit)[0]
    assert a_sec0["inpaint_status"] == "done" and a_sec0["inpaint_image_url"]

    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert _state(ids) == ("failed", "N4", "failed")
    assert _q("SELECT state, reject_reason FROM task_handoff WHERE id = ANY(:h)", h=b_h) == [
        {"state": "rejected", "reject_reason": "processing_timeout"}]
    b_arts1 = _artifacts(b_h)
    assert [r["id"] for r in b_arts1] == [r["id"] for r in b_arts0] and all(r["state"] == "unadopted" for r in b_arts1)
    assert _q("SELECT state FROM task_handoff WHERE id = ANY(:h)", h=a_h) == [{"state": "adopted"}]  # 이미 채택된 결과는 그대로
    assert _artifacts(a_h) == a_arts0
    assert _q("SELECT inpaint_status, inpaint_image_url, warning_badge FROM section WHERE id = :s", s=a_unit)[0] == a_sec0
    b_sec = _q("SELECT inpaint_status, inpaint_image_url, warning_badge FROM section WHERE id = :s", s=b_unit)[0]
    assert (b_sec["inpaint_status"], b_sec["warning_badge"]) == ("failed", "processing_failed")
    assert b_sec["inpaint_image_url"] is None  # B 의 늦은 결과(배경)를 쓰지 않는다

    for h in b_h:  # 늦은 채택 메시지 · 인수 재시도
        tasks.run_adopt(h)
        assert execution.claim_adoption(h, "be-adopter-late") is None
    assert _q("SELECT state, reject_reason FROM task_handoff WHERE id = ANY(:h)", h=b_h) == [
        {"state": "rejected", "reject_reason": "processing_timeout"}]
    assert all(r["state"] == "unadopted" for r in _artifacts(b_h))
    assert _q("SELECT inpaint_status, inpaint_image_url, warning_badge FROM section WHERE id = :s", s=b_unit)[0] == b_sec
    assert _state(ids) == ("failed", "N4", "failed")
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=job)[0]["n"] == 0


def test_timeout_does_not_touch_handoffs_of_other_attempts(env):
    """시간 초과가 닫는 것은 그 실행의 끝나지 않은 시도의 인계뿐 — 이미 끝난(채택된) 시도의 인계는 그대로."""
    ids, tr = _translate_handoff_then_timeout(env)
    job = ids["job"]
    before = _q("SELECT h.id, h.state FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j "
                "AND NOT (h.task_id = ANY(:t)) ORDER BY h.id", j=job, t=tr)
    assert recovery.sweep()["timed_out"] == 1
    after = _q("SELECT h.id, h.state FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j "
               "AND NOT (h.task_id = ANY(:t)) ORDER BY h.id", j=job, t=tr)
    assert after == before
    assert before and all(r["state"] == "adopted" for r in before)  # 분석(N2) 인계는 채택된 채로 남는다


def test_late_adoption_message_after_timeout_rejects_handoff(env):
    """채택 메시지가 시간 초과 뒤에 처리돼도 채택하지 않는다(이미 닫혔으면 그대로)."""
    ids, tr = _translate_handoff_then_timeout(env)
    job = ids["job"]
    hids = [r["id"] for r in _q("SELECT id FROM task_handoff WHERE task_id = ANY(:t)", t=tr)]
    assert recovery.sweep()["timed_out"] == 1
    from app import tasks

    for h in hids:
        tasks.run_adopt(h)
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE id = ANY(:h) AND state = 'adopted'", h=hids)[0]["n"] == 0
    assert _state(ids) == ("failed", "N4", "failed")
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=job)[0]["n"] == 0


# ---- ③ N6 한도 소진 뒤 시간 초과 — 실행 running 영향 고정 -------------------------------------------------------------
def test_n6_timeout_at_limit_run_stays_running_and_effects(env):
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].messages = []
    run = _q("SELECT id FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND run_kind = 'final_render'", j=job)[0]["id"]
    mx = _q("SELECT max_retry FROM job_async_task WHERE parent_task_id = :r AND is_current", r=run)[0]["max_retry"]
    for _ in range(mx):
        _age_state(job, 1801)
        assert recovery.sweep()["timed_out"] == 1
        assert c.post(f"/v1/jobs/{job}/render").status_code == 202
        env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert _q("SELECT status FROM job_async_task WHERE id = :r", r=run)[0]["status"] == "running"  # 실행은 남는다(지금 규칙)
    g0 = _gate_counts()
    for _ in range(3):  # 점검을 반복해도 새 시도 · 메시지 · 상태 변화 없음
        st = recovery.sweep()
        assert st["timed_out"] == 0
    assert [m for m in env["q"].messages if m[0] != ADOPT] == [] and _state(ids) == ("failed", "N6", "failed")
    mine = lambda: _q("SELECT count(*) FILTER (WHERE parent_task_id IS NOT NULL AND status IN ('pending','running')) AS active, "  # noqa: E731
                      "count(*) FILTER (WHERE parent_task_id IS NULL AND status = 'running') AS runs FROM job_async_task WHERE job_id = :j",
                      j=job)[0]
    assert mine()["active"] == 0 and _open_handoffs(job) == 0  # gate busy 항목(진행 시도 · 열린 인계)에는 이 작업이 안 잡힌다
    assert mine()["runs"] >= 1  # 「open runs(참고)」에만 남는다(N6 실행 + 끝난 하류 실행 여부와 무관하게 최종 렌더 실행 1)
    assert g0["runs"] >= 1
    # 출구: 취소(JOB-04)하면 실행이 닫힌다
    assert c.delete(f"/v1/jobs/{job}").status_code == 200
    assert _q("SELECT status FROM job_async_task WHERE id = :r", r=run)[0]["status"] == "cancelled"
