"""N4 감시(driver_n4)·N5 판정(n5_verify) 모의 시험 — 운영 접속 없음. python test_n4.py"""
import copy
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["ITEST_EV"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import driver_n4 as d  # noqa: E402
import n5_verify as v  # noqa: E402

T0 = datetime(2026, 10, 13, 5, 0, 0, tzinfo=timezone.utc)
RUN = 300
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def att(id, stage, unit, status="pending", attempt_no=1, origin=None, started=0, lease=120, disp=0, err=None):
    st = None if status == "pending" else T0 + timedelta(seconds=started)
    return {"id": id, "parent_task_id": RUN, "run_kind": None, "stage": stage, "unit_id": unit, "status": status,
            "attempt_no": attempt_no, "is_current": True, "retry_count": attempt_no - 1, "max_retry": 1, "retry_origin": origin,
            "error_code": err, "dispatch_count": 1, "last_dispatched_at": T0 + timedelta(seconds=disp), "started_at": st,
            "finished_at": None, "heartbeat_at": st, "lease_expires_at": (T0 + timedelta(seconds=lease)) if lease is not None else None,
            "leased": True, "cancelled_at": None}


def runrow(status="running"):
    return {"id": RUN, "parent_task_id": None, "run_kind": "downstream", "stage": None, "unit_id": 9, "status": status,
            "attempt_no": None, "is_current": True, "retry_count": 0, "max_retry": 0, "retry_origin": None, "error_code": None,
            "dispatch_count": 0, "last_dispatched_at": None, "started_at": T0, "finished_at": None, "heartbeat_at": None,
            "lease_expires_at": None, "leased": False, "cancelled_at": None}


OLD = [{**runrow("done"), "id": 22, "run_kind": "analysis"}, {**att(23, "analyze", 9, "done"), "parent_task_id": 22},
       {**att(24, "judge", 10, "done"), "parent_task_id": 22}]


def rec(sec, kids, step="N4", status="processing", queues=None, others=(), handoffs=(), run_status="running"):
    return {"db_now": T0 + timedelta(seconds=sec), "job": {"status": status, "current_step": step},
            "attempts": OLD + [runrow(run_status)] + kids, "handoffs": list(handoffs),
            "queues": queues or {"ocr": 0, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0}, "global_active": list(others)}


class Fake:
    def __init__(self, seq, abort_result=200, after_abort=None, stop_at=None):
        self.seq, self.i, self.t = seq, 0, 0.0
        self.abort_result, self.after_abort, self.aborts, self.max_ids = abort_result, after_abort, 0, []
        self.stop_at, self.logs, self.states = stop_at, [], []

    def fetch(self, label):
        src = self.after_abort if (self.aborts and self.after_abort is not None) else self.seq
        item = src[min(self.i, len(src) - 1)]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return item

    resp_max = None   # 설정하면 (결과, 응답 시점 최대 id) 를 돌려준다

    def abort(self, mx):
        self.aborts += 1
        self.max_ids.append(mx)
        self.i = 0
        return (self.abort_result, self.resp_max) if self.resp_max is not None else self.abort_result

    def stop_text(self):
        return "S4 test" if self.stop_at is not None and self.i > self.stop_at else None

    def mon(self):
        return d.Monitor(self.fetch, self.abort, self.stop_text, clock=lambda: self.t, sleep=self._sleep,
                         log=self.logs.append, state=lambda **kw: self.states.append(kw))

    def _sleep(self, s):
        self.t += s


def flow_done(sec=200):
    ks = []
    i = 400
    for u in (10, 11):
        for s in ("label", "logo", "inpaint", "style", "translate", "preview"):
            ks.append(att(i, s, u, "done", started=sec - 50, lease=None))
            i += 1
    return ks


# ================= driver_n4 =================
@case
def normal_flow_followups_then_n5_run_row_running():
    seq = [rec(0, [att(400, "label", 10), att(401, "label", 11)]),
           rec(10, [att(400, "label", 10, "running", lease=130), att(401, "label", 11, "running", lease=130)]),
           rec(20, [att(400, "label", 10, "done", lease=None), att(401, "label", 11, "done", lease=None),
                    att(402, "logo", 10, disp=20), att(403, "logo", 11, disp=20)]),           # 정상 후속 시도
           rec(30, [att(402, "logo", 10, "done", lease=None), att(404, "inpaint", 10, "running", started=30, lease=150),
                    att(405, "style", 10, disp=30), att(406, "translate", 10, "running", started=30, lease=150)]),
           rec(200, flow_done(), step="N5", status="review")]                                 # 대표 행 running 유지
    f = Fake(seq)
    out = f.mon().run(9, RUN)
    assert out == "N5-settled" and f.aborts == 0, (out, f.logs)
    assert f.states[-1]["phase"] == "N5-settled" and f.states[-1]["all_done"] is True


@case
def n5_waits_for_queue_then_settles():
    q = {"ocr": 0, "cpu": 1, "gpu": 0, "celery": 0, "unacked": 0}
    f = Fake([rec(200, flow_done(), step="N5", status="review", queues=q), rec(210, flow_done(), step="N5", status="review", queues=q),
              rec(220, flow_done(), step="N5", status="review")])
    assert f.mon().run(9, RUN) == "N5-settled" and f.aborts == 0


@case
def preview_style_defaults_failure_reaches_n5_no_abort_but_recorded():
    ks = flow_done()
    for a in ks:
        if a["stage"] == "preview":
            a["status"], a["error_code"] = "failed", "STYLE_DEFAULTS_UNAPPROVED"
    f = Fake([rec(200, ks, step="N5", status="review")])
    out = f.mon().run(9, RUN)
    assert out == "N5-settled" and f.aborts == 0
    s = f.states[-1]
    assert s["all_done"] is False and all(x[3] == "STYLE_DEFAULTS_UNAPPROVED" for x in s["failed"]) and len(s["failed"]) == 2


@case
def label_retry_aborts_once():
    f = Fake([rec(20, [att(400, "label", 10, "failed", err="LLM"), att(407, "label", 10, attempt_no=2, origin="auto", disp=20)])],
             after_abort=[rec(30, [att(400, "label", 10, "failed"), att(407, "label", 10, "cancelled", attempt_no=2)], step="N3", status="review")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("S1 재시도" in l for l in f.logs)


@case
def inpaint_lease_expired_db_time():
    f = Fake([rec(150, [att(404, "inpaint", 10, "running", started=20, lease=150)])],
             after_abort=[rec(160, [att(404, "inpaint", 10, "cancelled")], step="N3", status="review")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("계산 권한 만료" in l for l in f.logs)


@case
def inpaint_within_cold_start_limit_no_abort():
    f = Fake([rec(500, [att(404, "inpaint", 10, "running", started=0, lease=560)]), rec(600, flow_done(), step="N5", status="review")])
    assert f.mon().run(9, RUN) == "N5-settled" and f.aborts == 0


@case
def job_failed_n4_aborts():
    f = Fake([rec(40, [att(400, "label", 10, "failed", err="LABEL_FAILED")], status="failed")],
             after_abort=[rec(50, [att(400, "label", 10, "failed")], step="N3", status="review")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("작업 failed" in l for l in f.logs)


@case
def abort_409_at_n5_no_more_abort_checks_residual():
    f = Fake([rec(100, [att(404, "inpaint", 10, "failed", err="INPAINT_FAILED")])], abort_result=409,
             after_abort=[rec(110, flow_done(), step="N5", status="review")])
    out = f.mon().run(9, RUN)
    assert out == "N5-settled" and f.aborts == 1, (out, f.logs)
    assert any("더 중단하지 않고 잔여 작업 확인" in l for l in f.logs)


@case
def other_job_representative_row_is_inflow():
    other = [{"job_id": 77, "stage": "downstream", "status": "running", "n": 1}]
    f = Fake([rec(10, [att(400, "label", 10, "running", lease=130)], others=other)],
             after_abort=[rec(20, [att(400, "label", 10, "cancelled")], step="N3", status="review", others=other)])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("S3 다른 작업 유입" in l and "판정 보류" in l for l in f.logs)


@case
def unexpected_unit_aborts():
    f = Fake([rec(10, [att(400, "label", 99)])], after_abort=[rec(20, [att(400, "label", 99, "cancelled")], step="N3", status="review")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("예상 밖 단위" in l for l in f.logs)


@case
def query_errors_three_abort_once_then_unsettled_on_errors():
    f = Fake([RuntimeError("db")] * 3, after_abort=[RuntimeError("db")])
    out = f.mon().run(9, RUN)
    assert f.aborts == 1 and out == "aborted-unsettled" and f.max_ids == [None], (out, f.max_ids)


@case
def abort_failed_then_new_attempt_uncontrolled():
    base = [att(400, "label", 10, "failed")]
    f = Fake([rec(10, base)], abort_result=500, after_abort=[rec(20, base), rec(30, base + [att(410, "label", 10, attempt_no=2, origin="recovery")])])
    assert f.mon().run(9, RUN) == "uncontrolled" and f.aborts == 1 and f.max_ids == [max(a["id"] for a in rec(10, base)["attempts"])]


@case
def abort_ok_run_cancelled_settles_but_running_run_row_does_not():
    f = Fake([rec(150, [att(404, "inpaint", 10, "running", started=20, lease=150)])],
             after_abort=[rec(160, [att(404, "inpaint", 10, "cancelled")], step="N3", status="review", run_status="cancelled")])
    assert f.mon().run(9, RUN) == "aborted-settled"
    g = Fake([rec(150, [att(404, "inpaint", 10, "running", started=20, lease=150)])],
             after_abort=[rec(160, [att(404, "inpaint", 10, "cancelled")], step="N3", status="review", run_status="running")])
    assert g.mon().run(9, RUN) == "aborted-unsettled"       # N5 가 아니면 running 대표 행은 진행 중


@case
def n4_run_row_running_counts_as_active():
    r = rec(50, [att(400, "label", 10, "done", lease=None)])
    assert d.settled(r, RUN) is False


@case
def n5_with_previous_runs_rows_ignored_and_run_row_running_settled():
    r = rec(200, flow_done(), step="N5", status="review")
    assert d.settled(r, RUN) is True and d.reached_n5(r, RUN)["all_done"] is True
    r2 = copy.deepcopy(r)
    r2["handoffs"] = [{"id": 1, "state": "received"}]
    assert d.settled(r2, RUN) is False


@case
def abort_once_records_max_id_and_check_after_abort():
    for n in ("ABORT_SENT", "ABORT_RESULT"):
        try:
            os.remove(os.path.join(d.EV, n))
        except FileNotFoundError:
            pass

    class Api:
        def call(self, *a, **k):
            class R: status_code = 200
            return R()
    assert d.abort_once(Api(), "driver", 405, after=lambda: (407, "cancelled")) == (200, 407)
    assert d.abort_once(Api(), "local-watcher", 409) == ("already-sent", None)
    assert d.abort_max_id() == 405 and d.abort_resp_max_id() == 407
    res = open(os.path.join(d.EV, "ABORT_RESULT"), encoding="utf-8").read()
    assert "result=200" in res and "run_status=cancelled" in res and "max_attempt_id_after_response=407" in res


# ---- lease 와 LaMa 첫 로딩 제한(10분)은 독립 조건
@case
def inpaint_lease_null_within_cold_start_aborts():
    f = Fake([rec(60, [att(404, "inpaint", 10, "running", started=0, lease=None)])],
             after_abort=[rec(70, [att(404, "inpaint", 10, "cancelled")], step="N3", status="review", run_status="cancelled")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("계산 권한 만료" in l and "None" in l for l in f.logs) and not any("단계 제한" in l for l in f.logs)


@case
def inpaint_lease_valid_beyond_cold_start_aborts():
    f = Fake([rec(610, [att(404, "inpaint", 10, "running", started=0, lease=700)])],
             after_abort=[rec(620, [att(404, "inpaint", 10, "cancelled")], step="N3", status="review", run_status="cancelled")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("단계 제한 시간" in l for l in f.logs) and not any("계산 권한 만료" in l for l in f.logs)


# ---- 중단 요청 이후 관측된 새 시도: 요청~응답 사이(정상 후속 가능) vs 응답 이후 생성
@case
def window_followup_cancelled_is_not_uncontrolled():
    base = [att(400, "label", 10, "failed", err="X")]
    f = Fake([rec(10, base)], after_abort=[rec(20, base + [att(402, "logo", 11, "cancelled")], step="N3", status="review",
                                              run_status="cancelled")])
    f.resp_max = 402                                                   # 402 는 요청~응답 사이에 생김
    assert f.mon().run(9, RUN) == "aborted-settled" and any("요청~응답 사이" in l for l in f.logs)


@case
def attempt_after_response_is_uncontrolled():
    base = [att(400, "label", 10, "failed", err="X")]
    f = Fake([rec(10, base)], after_abort=[rec(20, base + [att(405, "logo", 11)], step="N3", status="review", run_status="cancelled")])
    f.resp_max = 402
    assert f.mon().run(9, RUN) == "uncontrolled" and any("응답 이후 생성 [405]" in l for l in f.logs)


@case
def window_attempt_still_live_after_200_is_uncontrolled():
    base = [att(400, "label", 10, "failed", err="X")]
    f = Fake([rec(10, base)], after_abort=[rec(20, base + [att(402, "logo", 11, "running", lease=200)], step="N3",
                                              status="review", run_status="cancelled")])
    f.resp_max = 402
    assert f.mon().run(9, RUN) == "uncontrolled" and any("진행 중인 새 시도 [402]" in l for l in f.logs)


@case
def check_reports_request_response_split():
    class FakeDb:
        def queues(self):
            return {"ocr": 0, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0}

        def consistent(self, fn):
            return fn(self.q)

        def q(self, sql, **p):
            if "FROM job WHERE" in sql:
                return [{"status": "review", "current_step": "N3"}]
            if "now()" in sql:
                return [{"t": T0}]
            if "FROM task_handoff" in sql:
                return []
            if "parent_task_id = :r" in sql:
                return [{"id": 300, "parent_task_id": None, "stage": None, "unit_id": 9, "status": "cancelled", "attempt_no": None, "retry_origin": None},
                        {"id": 400, "parent_task_id": 300, "stage": "label", "unit_id": 10, "status": "failed", "attempt_no": 1, "retry_origin": None},
                        {"id": 402, "parent_task_id": 300, "stage": "logo", "unit_id": 11, "status": "cancelled", "attempt_no": 1, "retry_origin": None},
                        {"id": 405, "parent_task_id": 300, "stage": "logo", "unit_id": 10, "status": "pending", "attempt_no": 1, "retry_origin": None}]
            return [{"n": 0}]
    import json as _j
    open(os.path.join(d.EV, "state.json"), "w", encoding="utf-8").write(_j.dumps({"run_id": 300}))
    open(os.path.join(d.EV, "ABORT_SENT"), "w", encoding="utf-8").write("t 요청 시도권 사용: driver max_attempt_id=400\n")
    open(os.path.join(d.EV, "ABORT_RESULT"), "w", encoding="utf-8").write("t driver result=200 max_attempt_id_after_response=402 run_status=cancelled\n")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d.check(FakeDb())
    c = _j.loads(buf.getvalue().split("CHECK ", 1)[1])
    assert c["after_request"] == [402, 405] and c["after_response"] == [405] and c["after_request_live"] == [405], c
    assert c["after_abort"] == [405] and c["active"] == [405], c


# ---- 원격 인계 전환(이번 #31 재현): 권한 만료 + 수신·검증 인계 있음 = 채택 대기(정상)
def ho(id, task, state, recv):
    return {"id": id, "task_id": task, "state": state, "received_at": T0 + timedelta(seconds=recv), "outcome": "done",
            "lease_epoch": 1, "late": False, "adopted_at": None, "reject_reason": None, "skip_reason": None, "target_count": 1}


def r31(sec, h_state, att_status="running", extra=()):
    # 04:22:45.8 시작, 04:22:54.0 인계(만료 시각 = 인계 시각), 인계 #12 수신 04:22:54.088 — T0 를 04:22:00 으로 보고 초 단위 재현
    kids = [att(27, "label", 10, "done", lease=None), att(29, "logo", 10, "done", lease=None),
            att(31, "inpaint", 10, att_status, started=45.8, lease=54.0)] + list(extra)
    hs = [ho(12, 31, h_state, 54.088)] if h_state else []
    return rec(sec, kids, handoffs=hs)


@case
def r31_handoff_received_is_adoption_wait_not_abort():
    seq = [r31(56.2, "received"), r31(60, "verifying"), r31(65, "verified"), r31(70, "adopted", att_status="done"),
           rec(200, flow_done(), step="N5", status="review")]
    f = Fake(seq)
    out = f.mon().run(9, RUN)
    assert out == "N5-settled" and f.aborts == 0, (out, f.logs)
    ls, info = d.lease_state(r31(56.2, "received")["attempts"][-1], r31(56.2, "received")["handoffs"], T0 + timedelta(seconds=56.2))
    assert ls == "adoption_wait" and info["handoff"] == 12 and abs(info["wait_s"] - 2.112) < 0.01, (ls, info)


@case
def expired_without_handoff_aborts():
    f = Fake([r31(56.2, None)], after_abort=[rec(60, [att(31, "inpaint", 10, "cancelled")], step="N3", status="review", run_status="cancelled")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("계산 권한 만료(수신·검증 인계 없음)" in l for l in f.logs)


@case
def expired_with_only_rejected_handoff_aborts():
    f = Fake([r31(56.2, "rejected")], after_abort=[rec(60, [att(31, "inpaint", 10, "cancelled")], step="N3", status="review", run_status="cancelled")])
    f.mon().run(9, RUN)
    assert f.aborts == 1 and any("계산 권한 만료" in l and "rejected" in l for l in f.logs), f.logs


@case
def adoption_wait_measured_from_received_at():
    # GPU 계산 시작(45.8)부터는 300s 넘었지만 인계 수신(54.088)부터는 299s → 중단 안 함, 그 뒤 301s → 채택 지연 중단
    ok = r31(54.088 + 299, "received")
    late = r31(54.088 + 301, "received")
    assert d.decide(ok, RUN, 10) == [], d.decide(ok, RUN, 10)
    rs = d.decide(late, RUN, 10)
    assert len(rs) == 1 and "인계 채택 지연" in rs[0] and "#12(received)" in rs[0], rs


@case
def stage_limit_not_applied_during_adoption_wait():
    r = r31(54.088 + 100, "verified")
    r["attempts"][-1]["started_at"] = T0 - timedelta(seconds=700)    # 계산 시작은 10분 넘게 전(인계 뒤 대기 중)
    assert d.decide(r, RUN, 10) == []


@case
def pre_abort_recheck_cancels_false_alarm():
    # 첫 조회: 만료·인계 없음 → 중단 직전 재조회: 인계가 막 등록됨 → 중단하지 않고 계속
    seq = [r31(55.0, None), r31(56.2, "received"), r31(70, "adopted", att_status="done"), rec(200, flow_done(), step="N5", status="review")]
    f = Fake(seq)
    assert f.mon().run(9, RUN) == "N5-settled" and f.aborts == 0
    assert any("중단 직전 재확인에서 사유 해소" in l for l in f.logs)


def r31_adopter(sec, h_state, started=45.8, lease_from=None):
    # cpu 채택자가 인수(claim_adoption): 시도에 새 유효 권한(now+120s)·heartbeat, 인계 verifying
    r = r31(sec, h_state)
    a = r["attempts"][-1]
    a["started_at"] = T0 + timedelta(seconds=started)
    t = lease_from if lease_from is not None else sec - 1
    a["lease_expires_at"], a["heartbeat_at"] = T0 + timedelta(seconds=t + 120), T0 + timedelta(seconds=t)
    return r


@case
def adopter_takeover_valid_lease_is_still_adoption_not_compute():
    # 계산 시작은 700초 전(inpaint 계산 제한 600초 초과)이지만, 인계 뒤 채택자가 유효 권한을 받은 verifying·verified 상태
    for st in ("verifying", "verified"):
        r = r31_adopter(54.088 + 100, st, started=54.088 + 100 - 700)
        ls, info = d.lease_state(r["attempts"][-1], r["handoffs"], r["db_now"])
        assert ls == "adoption_wait" and info["adopter_lease_valid"] is True, (st, ls, info)
        assert d.decide(r, RUN, 10) == [], (st, d.decide(r, RUN, 10))


@case
def adopter_takeover_adoption_delay_still_from_received_at():
    r = r31_adopter(54.088 + 301, "verifying")          # 채택자 권한은 유효하지만 인계 수신 후 301초
    rs = d.decide(r, RUN, 10)
    assert len(rs) == 1 and "인계 채택 지연" in rs[0] and "단계 제한" not in rs[0], rs


@case
def r31_full_sequence_with_adopter_lease_renewal():
    # received(권한 만료) → verifying(채택자 유효 권한) → verified(유효) → adopted(done) → N5. 계산 시작은 700초 전으로 둬서
    # 채택 중에 계산 제한을 잘못 적용하면 중단되도록 한다
    seq = [r31(56.2, "received"), r31_adopter(57.0, "verifying", started=-700), r31_adopter(58.0, "verified", started=-700),
           r31(60.0, "adopted", att_status="done"), rec(200, flow_done(), step="N5", status="review")]
    for x in seq[:1]:
        x["attempts"][-1]["started_at"] = T0 - timedelta(seconds=700)
    f = Fake(seq)
    out = f.mon().run(9, RUN)
    assert out == "N5-settled" and f.aborts == 0, (out, f.logs)


@case
def recheck_failure_reason_is_safe_stop_not_confirmed_expiry():
    f = Fake([r31(56.2, None), RuntimeError("db")],
             after_abort=[rec(60, [att(31, "inpaint", 10, "cancelled")], step="N3", status="review", run_status="cancelled")])
    f.mon().run(9, RUN)
    stop_line = next(l for l in f.logs if l.startswith("중단 판정"))
    assert f.aborts == 1 and "중단 직전 재확인 실패—안전 중단" in stop_line and "미확정" in stop_line, stop_line
    assert not stop_line.startswith("중단 판정: S2 계산 권한 만료"), stop_line


@case
def check_uses_same_lease_judgement():
    class FakeDb:
        def __init__(self, hs):
            self.hs = hs

        def queues(self):
            return {"ocr": 0, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0}

        def consistent(self, fn):
            return fn(self.q)

        def q(self, sql, **p):
            if "FROM job WHERE" in sql:
                return [{"status": "processing", "current_step": "N4"}]
            if "now()" in sql:
                return [{"t": T0 + timedelta(seconds=56.2)}]
            if "FROM task_handoff" in sql:
                return self.hs
            if "parent_task_id = :r" in sql:
                return [{"id": 300, "parent_task_id": None, "stage": None, "unit_id": 9, "status": "running", "attempt_no": None,
                         "retry_origin": None, "lease_expires_at": None},
                        {"id": 31, "parent_task_id": 300, "stage": "inpaint", "unit_id": 10, "status": "running", "attempt_no": 1,
                         "retry_origin": None, "lease_expires_at": T0 + timedelta(seconds=54.0)}]
            return [{"n": 0}]
    import json as _j, io, contextlib
    open(os.path.join(d.EV, "state.json"), "w", encoding="utf-8").write(_j.dumps({"run_id": 300}))
    for hs, want in (([ho(12, 31, "received", 54.088)], "adoption_wait"), ([], "expired"), ([ho(12, 31, "rejected", 54.088)], "expired"),
                     ([ho(12, 31, "verifying", 54.088)], "adoption_wait")):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            d.check(FakeDb(hs))
        c = _j.loads(buf.getvalue().split("CHECK ", 1)[1])
        assert c["lease_states"]["31"] == want and (31 in c["expired"]) == (want == "expired"), (hs, c["lease_states"])


# ================= n5_verify.evaluate =================
def vdata(**over):
    blocks = [{"id": i, "section_id": 10 if i < 4 else 11, "is_product_label": False, "is_brand_logo": False, "is_excluded": False,
               "style": {"font_color": "#000000", "bg_color": "#ffffff", "est_font_px": 20, "align": "left"},
               "trans_1": f"t{i}", "source_ko": f"s{i}"} for i in range(7)]
    atts, hs, arts = [], [], []
    hid = 1
    for u in (10, 11):
        for s in v.STAGES:
            aid = len(atts) + 500
            atts.append({"id": aid, "stage": s, "unit_id": u, "status": "done", "attempt_no": 1, "retry_origin": None,
                         "error_code": None, "is_current": True})
            h = {"id": hid, "task_id": aid, "state": "adopted", "outcome": "done", "ai_outcome": None, "status": None,
                 "final_px": None, "model": None, "verify_failed": []}
            if s == "inpaint":
                h.update(ai_outcome="completed", status="inpainted", final_px=1234, model="lama")
                for k in ("background", "delete_mask", "protect_mask"):
                    arts.append({"id": len(arts) + 1, "handoff_id": hid, "kind": k, "verified_key": "k", "source_ref": None,
                                 "sha256": "x", "declared_sha256": "x", "width": 600, "height": 560 if u == 10 else 440, "state": "adopted"})
            if s == "preview":
                arts.append({"id": len(arts) + 1, "handoff_id": hid, "kind": "render_image", "verified_key": "k", "source_ref": None,
                             "sha256": "y", "declared_sha256": "y", "width": 600, "height": 0, "state": "adopted"})
            hs.append(h)
            hid += 1
    secs = {10: {"w": 600, "h": 560, "inpaint_status": "done", "warning_badge": None, "render_image_key_set": True, "mask_matches_delete_mask": True},
            11: {"w": 600, "h": 440, "inpaint_status": "done", "warning_badge": None, "render_image_key_set": True, "mask_matches_delete_mask": True}}
    s3 = {u: {"background": "match", "delete_mask": "match", "protect_mask": "match", "mask_nonzero": 1234} for u in (10, 11)}
    dd = {"job": {"status": "review", "current_step": "N5"}, "attempts": atts, "handoffs": hs, "artifacts": arts,
          "sections": secs, "blocks": blocks, "s3": s3}
    for k, fn in over.items():
        fn(dd)
    return dd


@case
def verify_all_good_success():
    r = v.evaluate(vdata())
    assert r["overall"] == "성공" and r["stages"]["inpaint"]["lama_verified_sections"] == [10, 11], r["overall"]
    assert "logo: 브랜드명 일치(로고 판정 True) 경로" in r["unverified"]


@case
def verify_empty_claim_but_mask_has_pixels_is_integrity_failure():
    # 보고는 빈 마스크(final_px=0·unchanged)인데 실제로 읽은 마스크에 픽셀이 있으면 미검증이 아니라 산출물 검증 실패
    def f(dd):
        for h in dd["handoffs"]:
            if h["status"] == "inpainted":
                h.update(ai_outcome="skipped", status="unchanged", final_px=0)
    r = v.evaluate(vdata(x=f))
    assert r["overall"] == "실패(산출물 검증 실패)" and len(r["stages"]["inpaint"]["integrity_failed"]) == 2, r["overall"]


@case
def verify_preview_style_defaults_failure_partial():
    def f(dd):
        for a in dd["attempts"]:
            if a["stage"] == "preview":
                a["status"], a["error_code"] = "failed", "STYLE_DEFAULTS_UNAPPROVED"
        for s in dd["sections"].values():
            s["render_image_key_set"] = False
        dd["artifacts"][:] = [x for x in dd["artifacts"] if x["kind"] != "render_image"]
    r = v.evaluate(vdata(x=f))
    assert r["overall"] == "부분 검증" and r["stages"]["preview"]["style_defaults_failure"] == [10, 11] and r["notes"]


@case
def verify_bg_color_null_only_is_renderable():
    def f(dd):
        dd["blocks"][0]["style"]["bg_color"] = None
    r = v.evaluate(vdata(x=f))
    assert r["stages"]["style"]["renderable_without_defaults"] is True and r["stages"]["style"]["bg_color_null"] == 1


@case
def verify_render_field_null_on_drawn_block_vs_excluded_block():
    def f(dd):
        dd["blocks"][1]["style"]["est_font_px"] = None                     # 그릴 블록 → 렌더 기본값 필요
        dd["blocks"][2].update(is_product_label=True, is_brand_logo=None, is_excluded=True, style=None)  # 제외 블록 → 조건 아님
    r = v.evaluate(vdata(x=f))
    s = r["stages"]["style"]
    assert s["render_missing"] == [1] and s["renderable_without_defaults"] is False and s["contract_ok"] is True, s


@case
def verify_mask_pixel_mismatch_is_integrity_failure():
    def f(dd):
        dd["s3"][10]["mask_nonzero"] = 999
    r = v.evaluate(vdata(x=f))
    inp = r["stages"]["inpaint"]
    assert inp["lama_verified_sections"] == [11] and r["overall"] == "실패(산출물 검증 실패)", r["overall"]
    assert inp["integrity_failed"][0][0] == 10 and "마스크 픽셀 999" in inp["integrity_failed"][0][1]
    assert any("일부 단계 성공" in n and "[10]" in n for n in r["notes"]), r["notes"]


@case
def verify_hash_mismatch_is_integrity_failure():
    def f(dd):
        dd["s3"][11]["background"] = "mismatch"
    r = v.evaluate(vdata(x=f))
    assert r["overall"] == "실패(산출물 검증 실패)" and "background 해시 불일치" in r["stages"]["inpaint"]["integrity_failed"][0][1]


@case
def verify_size_mismatch_is_integrity_failure():
    def f(dd):
        for x in dd["artifacts"]:
            if x["kind"] == "protect_mask" and x["height"] == 440:
                x["width"] = 599
    r = v.evaluate(vdata(x=f))
    assert r["overall"] == "실패(산출물 검증 실패)" and "크기 불일치" in r["stages"]["inpaint"]["integrity_failed"][0][1]


@case
def verify_unreadable_is_not_success_nor_integrity_failure():
    def f(dd):
        dd["s3"][10]["delete_mask"] = "error:ClientError"
        dd["s3"][10]["mask_nonzero"] = None
    r = v.evaluate(vdata(x=f))
    inp = r["stages"]["inpaint"]
    assert r["overall"] == "부분 검증" and inp["unreadable"] and not inp["integrity_failed"], (r["overall"], inp["unreadable"])


@case
def verify_empty_mask_unverified_not_integrity_failure():
    def f(dd):
        for h in dd["handoffs"]:
            if h["status"] == "inpainted":
                h.update(ai_outcome="skipped", status="unchanged", final_px=0)
        for u in (10, 11):
            dd["s3"][u]["mask_nonzero"] = 0
    r = v.evaluate(vdata(x=f))
    assert r["overall"] == "부분 검증" and not r["stages"]["inpaint"]["integrity_failed"]
    assert "inpaint: LaMa 실제 추론(모든 섹션 생략 또는 실패)" in r["unverified"]


@case
def verify_not_n5_is_failure():
    def f(dd):
        dd["job"] = {"status": "review", "current_step": "N3"}
    assert v.evaluate(vdata(x=f))["overall"] == "실패(N5 미도달)"


@case
def verify_retry_makes_partial():
    def f(dd):
        dd["attempts"].append({"id": 999, "stage": "translate", "unit_id": 10, "status": "failed", "attempt_no": 1,
                               "retry_origin": None, "error_code": "X", "is_current": False})
        dd["attempts"][4]["attempt_no"], dd["attempts"][4]["retry_origin"] = 2, "auto"
    r = v.evaluate(vdata(x=f))
    assert r["overall"] == "부분 검증" and r["retries"]


@case
def verify_bad_logo_combo_fails_logo():
    def f(dd):
        dd["blocks"][0].update(is_product_label=True, is_brand_logo=False)
    r = v.evaluate(vdata(x=f))
    assert r["stages"]["logo"]["bad_combos"] == 1 and r["stages"]["logo"]["ok"] is False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # -I 는 PYTHONIOENCODING 을 무시하므로 직접 고정
    bad, ran = 0, 0
    for c in CASES:
        ran += 1
        try:
            c()
            print(f"ok   {c.__name__}")
        except Exception as e:  # noqa: BLE001 — AssertionError 외 예외도 FAIL 로 센다(실행기가 중간에 멈추지 않게)
            bad += 1
            print(f"FAIL {c.__name__}: {type(e).__name__}: {str(e)[:300]}")
    assert ran == len(CASES)
    print(f"RESULT ran={ran} pass={ran - bad} fail={bad}")
    sys.exit(1 if bad else 0)
