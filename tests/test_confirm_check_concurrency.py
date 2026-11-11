"""I6 — 규칙 검사 결과 채택 × confirm() 동시 실행(격리 통합 11-10). 실제 PostgreSQL 의 별도 연결 · 트랜잭션, 순서는 이벤트로 제어한다.

순차 실행과 구분하기 위해 한쪽이 job 행 잠금을 쥔 채 멈춘 동안 다른 쪽이 **실제로 잠금 대기 중**인지
pg_stat_activity(wait_event_type = 'Lock')로 확인한 뒤 풀어 준다. 두 쪽 모두 job → 대표 실행 → 시도 순으로 잠근다.
confirm 은 라우터와 같은 함수(app.flows.final.confirm)를 스레드마다 새 세션으로 부른다(HTTP 시험 클라이언트의 스레드 처리와 무관하게).
공급 · inputKey 는 시험 대역(FakeSupply · FakeKeys), 번역은 FakeTranslator. 실제 AI · 워커 · S3 아님.
"""
from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from app import execution
from app import db as app_db
from app.flows import final, text_check
from tests.exec_db import requires_db
from tests.test_confirm_check_integration import (TF, _ack, _blocks, _checks, _failed_n5_checked, _final_attempts, _final_runs)
from tests.test_exec_downstream import _q, dburl, env  # noqa: F401
from tests.test_text_check_connect import TC, rule_on  # noqa: F401

pytestmark = requires_db
T_OUT = 30


def _confirm_direct(s, acks, out):
    db = app_db.SessionLocal()
    try:
        out["r"] = final.confirm(db, s["job"], s["ids"]["seller"], acks)
    except final.ConfirmRejected as e:
        db.rollback()
        out["r"] = ("rejected", e.status, e.code, e.details)
    except Exception as e:  # noqa: BLE001 — 교착 등 예상 밖 오류를 시험이 본다
        db.rollback()
        out["exc"] = e
    finally:
        db.close()


def _lock_waiters():
    with app_db.engine.connect() as c:
        return c.execute(text("SELECT pid, left(query, 120) FROM pg_stat_activity WHERE datname = current_database() "
                              "AND wait_event_type = 'Lock' AND state = 'active'")).all()


def _wait_for_lock_waiter():
    end = time.monotonic() + T_OUT
    while time.monotonic() < end:
        w = _lock_waiters()
        if w:
            return w
        time.sleep(0.05)
    pytest.fail("다른 쪽이 잠금 대기에 들어가지 않았다 — 순차 실행으로 끝났을 수 있다")


def _pending_check_for_edit(s):
    """검사 대상 블록 하나의 번역문을 바꿔 그 섹션 검사 시도(pending)를 만든다. 반환: (블록 id, 시도 id)."""
    b = [x for x in _blocks(s["c"], s["job"]).values() if x["check"] is not None][0]
    r = s["c"].patch(f"/v1/jobs/{s['job']}/blocks/{b['id']}", json={"trans1": "Edited for I6", "revision": b["revision"]})
    assert r.status_code == 200, r.text
    att = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"]
    assert len(att) == 1
    return b["id"], att[0]["id"]


def _join(*ts):
    for t in ts:
        t.join(T_OUT)
        assert not t.is_alive(), "교착 또는 무한 대기"


def _flags(bid):
    return _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=bid)[0]["compliance_flags"]


# ── 채택이 먼저: 채택이 job 잠금을 쥔 동안 confirm 은 대기, 채택 커밋 뒤 최신(settled) 상태로 판정 ────────────
def test_i6_adoption_first_confirm_waits_and_judges_latest_state(env, rule_on, monkeypatch):
    s = _failed_n5_checked(env)
    bid, att = _pending_check_for_edit(s)
    env["q"].messages.clear()  # 큐 대역이 대신 돌리지 않게 — 아래 스레드가 직접 실행
    in_adopt, release = threading.Event(), threading.Event()
    real = text_check.TextCheckHandler.adopt

    def paused(self, db, ctx):
        in_adopt.set()
        assert release.wait(T_OUT)
        return real(self, db, ctx)

    monkeypatch.setattr(text_check.TextCheckHandler, "adopt", paused)
    t1_out: dict = {}
    t1 = threading.Thread(target=lambda: t1_out.update(r=text_check.execute_text_check(att)))
    t1.start()
    assert in_adopt.wait(T_OUT)  # 채택 트랜잭션이 job → 대표 실행 → 시도를 잠근 상태
    t2_out: dict = {}
    t2 = threading.Thread(target=_confirm_direct, args=(s, _ack(s), t2_out))
    t2.start()
    waiters = _wait_for_lock_waiter()
    assert "job" in waiters[0][1] and "FOR UPDATE" in waiters[0][1]  # confirm 이 job 행 잠금을 기다린다
    assert t2.is_alive() and "r" not in t2_out
    release.set()
    _join(t1, t2)
    assert "exc" not in t1_out and "exc" not in t2_out, (t1_out, t2_out)
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=att)[0]["status"] == "done"
    assert isinstance(t2_out["r"], dict) and t2_out["r"]["currentStep"] == "N6", t2_out  # 채택된 최신 상태(settled)로 통과
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)


# ── 확정이 먼저(검사 진행 중): confirm 이 잠금을 쥔 동안 검사 실행은 대기, confirm 은 그 시점 상태로 거절 ─────────
def test_i6_confirm_first_while_check_running_rejects_then_result_adopted_normally(env, rule_on, monkeypatch):
    s = _failed_n5_checked(env)
    bid, att = _pending_check_for_edit(s)
    env["q"].messages.clear()
    locked, release = threading.Event(), threading.Event()
    real = final._require_checks_settled

    def paused(db, job_id):
        locked.set()  # confirm 이 job 잠금을 쥔 상태(lock_job 뒤)
        assert release.wait(T_OUT)
        return real(db, job_id)

    monkeypatch.setattr(final, "_require_checks_settled", paused)
    t2_out: dict = {}
    t2 = threading.Thread(target=_confirm_direct, args=(s, _ack(s), t2_out))
    t2.start()
    assert locked.wait(T_OUT)
    t1_out: dict = {}
    t1 = threading.Thread(target=lambda: t1_out.update(r=text_check.execute_text_check(att)))
    t1.start()
    waiters = _wait_for_lock_waiter()  # 검사 실행(권한 획득 = lock_chain)이 job 잠금을 기다린다
    assert "job" in waiters[0][1]
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=att)[0]["status"] == "pending"
    release.set()
    _join(t2, t1)
    assert "exc" not in t1_out and "exc" not in t2_out, (t1_out, t2_out)
    r = t2_out["r"]
    assert r[0] == "rejected" and r[1] == 409 and r[3]["reason"] == "check_not_settled"  # 판정 시점 상태(in_progress)로 거절
    assert {"blockId": bid, "state": "in_progress"} in r[3]["blocks"] and r[3]["registeredBlockIds"] == []
    assert _final_runs(s["job"]) == 0
    # confirm 이 성공하지 않았으므로 뒤이은 채택은 정상 반영(N5 그대로)
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=att)[0]["status"] == "done"
    assert _q("SELECT current_step FROM job WHERE id = :j", j=s["job"])[0]["current_step"] == "N5"
    assert _blocks(s["c"], s["job"])[bid]["check"]["state"] == "settled"
    out: dict = {}
    _confirm_direct(s, _ack(s), out)
    assert out["r"]["currentStep"] == "N6"
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)


# ── 확정이 먼저(성공) + 늦은 결과: 대체된 시도를 쥔 느린 워커의 결과는 confirm 커밋 뒤 거절, flag 무변경 ──────────────
def test_i6_confirm_success_first_late_stale_result_does_not_change_flags(env, rule_on, monkeypatch):
    from app.execution import Envelope
    from app.flows.common import submit

    s = _failed_n5_checked(env)
    bid, old = _pending_check_for_edit(s)
    env["q"].messages.clear()
    lease = execution.acquire(old, "slow-checker")  # 느린 워커가 옛 시도의 권한을 쥠
    b = _blocks(s["c"], s["job"])[bid]
    s["c"].patch(f"/v1/jobs/{s['job']}/blocks/{bid}", json={"trans1": "Edited again for I6", "revision": b["revision"]})  # 새 시도가 대체
    env["q"].drain()  # 새 시도 실행 · 채택 → settled
    assert _blocks(s["c"], s["job"])[bid]["check"]["state"] == "settled"
    flags_before = _flags(bid)
    locked, release = threading.Event(), threading.Event()
    real = final._require_checks_settled

    def paused(db, job_id):
        locked.set()
        assert release.wait(T_OUT)
        return real(db, job_id)

    monkeypatch.setattr(final, "_require_checks_settled", paused)
    t2_out: dict = {}
    t2 = threading.Thread(target=_confirm_direct, args=(s, _ack(s), t2_out))
    t2.start()
    assert locked.wait(T_OUT)
    env_ = Envelope(outcome="done", target_count=1, input_fingerprint=lease.fingerprint,
                    payload={"text_check": {"blocks": [{"block_key": f"blk_{bid}", "flags": [
                        {"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": 1, "glossary_id": "1"}]}]}})
    t1_out: dict = {}

    def late():
        try:
            t1_out["r"] = submit(lease, env_)
        except Exception as e:  # noqa: BLE001
            t1_out["exc"] = e

    t1 = threading.Thread(target=late)
    t1.start()
    waiters = _wait_for_lock_waiter()  # 늦은 인계 등록(lock_chain)이 confirm 의 job 잠금을 기다린다
    assert "job" in waiters[0][1]
    release.set()
    _join(t2, t1)
    assert "exc" not in t2_out and t2_out["r"]["currentStep"] == "N6", t2_out
    assert isinstance(t1_out.get("exc"), (execution.LeaseLost, execution.HandoffRejected)), t1_out  # 교착이 아니라 거절
    assert _flags(bid) == flags_before
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t", t=old)[0]["n"] == 0
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)


# ── 동시 확정 2건(I8 보강): 먼저 잠근 쪽이 끝날 때까지 다른 쪽이 실제로 대기 ──────────────────────────────────
def test_i8_two_confirms_overlap_on_job_lock_only_one_final(env, rule_on, monkeypatch):
    s = _failed_n5_checked(env)
    locked, release = threading.Event(), threading.Event()
    real = final._require_checks_settled
    first = threading.Lock()

    def paused(db, job_id):
        if first.acquire(blocking=False):  # 첫 번째 confirm 만 멈춘다
            locked.set()
            assert release.wait(T_OUT)
        return real(db, job_id)

    monkeypatch.setattr(final, "_require_checks_settled", paused)
    o1, o2 = {}, {}
    t1 = threading.Thread(target=_confirm_direct, args=(s, _ack(s), o1))
    t1.start()
    assert locked.wait(T_OUT)
    t2 = threading.Thread(target=_confirm_direct, args=(s, _ack(s), o2))
    t2.start()
    _wait_for_lock_waiter()
    release.set()
    _join(t1, t2)
    assert "exc" not in o1 and "exc" not in o2, (o1, o2)
    assert o1["r"]["currentStep"] == "N6"
    assert o2["r"][0] == "rejected" and o2["r"][1] == 409  # 뒤 요청은 잠금 뒤 N6 을 보고 거절(N5 아님)
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)
