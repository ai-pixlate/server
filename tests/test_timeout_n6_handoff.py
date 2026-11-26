"""N6 최종 렌더 — 인계 등록 뒤 · 채택 전 시간 초과(README 7.8 ⑤).

N6 은 신뢰된 EC2 워커가 common.submit 안에서 동기로 register_handoff(커밋) → fix_bytes(산출물마다 커밋) → adopt 를 부른다.
세 트랜잭션 사이에 처리 시간 점검(recovery._processing_timeout)이 작업을 잠가 시도를 닫을 수 있다.
기존 N6 시험은 「등록 전(acquire 전 · 렌더 도중)」과 「채택 뒤」만 다룬다. 이 파일은 그 사이 두 틈을 고정한다.

  (i)  register_handoff 커밋 직후 · fix_bytes 전 시간 초과
  (ii) fix_bytes 끝 · adopt 전 시간 초과

공통 기대: 작업 failed/N6, 시도 failed(PROCESSING_TIMEOUT), 인계 rejected, 산출물 unadopted, 산출물(deliverable) 미생성,
저장 409, 늦은 인수(claim_adoption)도 거절. 그 뒤 [다시 시도](FIN-01)는 새 시도만 채택한다.
reject_reason 과 「점검 직후 열린 인계 0」은 (i) 에서 9962e5c 의 점검 내 정리에 달려 있다.
"""
from __future__ import annotations

import pytest

from app import execution, recovery
from tests.exec_db import requires_db
from tests.test_job_state_timers import _age_state, _q, _state, _to_n5, dburl, env  # noqa: F401 — fixture 재사용

pytestmark = requires_db


def _n6_handoffs(job):
    return _q("SELECT h.id, h.task_id, h.state, h.reject_reason FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
              "WHERE a.job_id = :j AND a.stage = 'final_render' ORDER BY h.id", j=job)


def _arts(hid):
    return [r["state"] for r in _q("SELECT state FROM task_artifact WHERE handoff_id = :h ORDER BY id", h=hid)]


def _open(job):
    return _q("SELECT count(*) AS n FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j "
              "AND h.state IN ('received','verifying','verified')", j=job)[0]["n"]


@pytest.mark.parametrize("window", ["after_register", "before_adopt"])
def test_n6_timeout_between_register_and_adopt_closes_handoff_unadopted(env, monkeypatch, window):
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    fired: dict = {}

    def _timeout_now():
        _age_state(job, 1801)
        fired["sweep"] = recovery.sweep()
        fired["state"] = _state(ids)
        fired["handoffs"] = _n6_handoffs(job)
        fired["open"] = _open(job)

    if window == "after_register":
        orig = execution.register_handoff

        def reg_then_timeout(*a, **kw):
            h = orig(*a, **kw)  # 인계 등록 커밋됨(received)
            if not fired:
                _timeout_now()
            return h

        monkeypatch.setattr(execution, "register_handoff", reg_then_timeout)
    else:
        orig = execution.adopt

        def timeout_then_adopt(*a, **kw):
            if not fired:  # 산출물 고정까지 끝, 채택 직전
                _timeout_now()
            return orig(*a, **kw)

        monkeypatch.setattr(execution, "adopt", timeout_then_adopt)

    env["q"].drain()  # 최종 렌더 실행(권한 → 렌더 → 등록 → [시간 초과] → 고정/채택 시도). 대역은 한 번만 끼어든다(fired)

    assert fired["sweep"]["timed_out"] == 1 and fired["state"] == ("failed", "N6", "failed")
    assert len(fired["handoffs"]) == 1 and fired["handoffs"][0]["state"] in ("received", "verifying", "rejected"), fired["handoffs"]

    hs = _n6_handoffs(job)
    assert len(hs) == 1
    h = hs[0]
    assert h["state"] == "rejected"
    arts = _arts(h["id"])
    assert arts and all(s == "unadopted" for s in arts)
    assert _open(job) == 0
    att = _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=h["task_id"])[0]
    assert att == {"status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    assert _state(ids) == ("failed", "N6", "failed")
    assert _q("SELECT count(*) AS n FROM deliverable WHERE job_id = :j AND render_status = 'done'", j=job)[0]["n"] == 0
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409
    assert execution.claim_adoption(h["id"], "be-adopter-late") is None  # 늦은 인수 거절
    assert _q("SELECT state FROM task_handoff WHERE id = :h", h=h["id"])[0]["state"] == "rejected"

    # 엄격 기대(9962e5c): 시간 초과 점검 자체가 인계를 processing_timeout 으로 닫는다
    assert fired["open"] == 0, f"점검 직후 열린 인계 {fired['open']}"
    assert h["reject_reason"] == execution.TIMED_OUT_REJECT

    # 재시도 → 새 시도만 채택
    r = c.post(f"/v1/jobs/{job}/tasks/{h['task_id']}/retry")
    assert r.status_code == 200, r.text
    new_id = r.json()["taskId"]
    env["q"].drain()
    assert _state(ids) == ("review", "N6", "awaiting_save")
    assert _q("SELECT state FROM task_handoff WHERE task_id = :t", t=new_id) == [{"state": "adopted"}]
    assert _q("SELECT state, reject_reason FROM task_handoff WHERE id = :h", h=h["id"])[0]["state"] == "rejected"
    assert c.post(f"/v1/jobs/{job}/save").status_code == 200
