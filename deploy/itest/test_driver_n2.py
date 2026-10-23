"""driver.Monitor·decide 상태별 모의 시험 — 운영 접속 없음(DB·API·시계 모두 가짜). python test_driver.py"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["ITEST_EV"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import driver_n2 as d  # noqa: E402

T0 = datetime(2026, 10, 13, 3, 0, 0, tzinfo=timezone.utc)
RUN = 100
JOB = 9


def att(id, stage="analyze", status="pending", attempt_no=1, retry_origin=None, started=None, lease=None,
        dispatched=0, parent=RUN, error=None, unit=JOB):
    return {"id": id, "parent_task_id": parent, "run_kind": None, "stage": stage, "unit_id": unit, "status": status,
            "attempt_no": attempt_no, "is_current": True, "retry_count": attempt_no - 1, "max_retry": 2,
            "retry_origin": retry_origin, "error_code": error, "dispatch_count": 1,
            "last_dispatched_at": T0 + timedelta(seconds=dispatched), "started_at": started, "finished_at": None,
            "heartbeat_at": started, "lease_expires_at": lease, "leased": lease is not None, "cancelled_at": None}


def run_row(status="running"):
    return {"id": RUN, "parent_task_id": None, "run_kind": "analysis", "stage": None, "unit_id": JOB, "status": status,
            "attempt_no": None, "is_current": True, "retry_count": 0, "max_retry": 0, "retry_origin": None, "error_code": None,
            "dispatch_count": 0, "last_dispatched_at": None, "started_at": T0, "finished_at": None, "heartbeat_at": None,
            "lease_expires_at": None, "leased": False, "cancelled_at": None}


def rec(sec, attempts, step="N2", status="processing", queues=None, others=()):
    return {"db_now": T0 + timedelta(seconds=sec), "job": {"status": status, "current_step": step},
            "attempts": [run_row("cancelled" if status == "draft" else "running")] + attempts,
            "queues": queues or {"ocr": 0, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0},
            "global_active": list(others)}


class Fake:
    """틱마다 정해진 응답(레코드 또는 예외)을 돌려준다. 마지막 응답은 계속 반복."""
    def __init__(self, seq, abort_result=200, after_abort=None, stop_at=None):
        self.seq, self.i, self.t = seq, 0, 0.0
        self.abort_result, self.after_abort, self.aborts = abort_result, after_abort, 0
        self.stop_at, self.logs, self.states = stop_at, [], []

    def fetch(self, label):
        src = self.after_abort if (self.aborts and self.after_abort is not None) else self.seq
        item = src[min(self.i, len(src) - 1)]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return item

    def abort(self):
        self.aborts += 1
        self.i = 0
        if isinstance(self.abort_result, Exception):
            return f"error:{type(self.abort_result).__name__}"
        return self.abort_result

    def stop_text(self):
        return "학교 ocr 워커 종료" if self.stop_at is not None and self.i > self.stop_at else None

    def monitor(self):
        return d.Monitor(self.fetch, self.abort, self.stop_text, clock=lambda: self.t, sleep=self._sleep,
                         log=self.logs.append, state=lambda **kw: self.states.append(kw))

    def _sleep(self, s):
        self.t += s


def lease_ok(sec):
    return T0 + timedelta(seconds=sec + 120)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def pending_running_n3():
    seq = [rec(0, [att(101, dispatched=0)]),
           rec(10, [att(101, status="running", started=T0, lease=lease_ok(10))]),
           rec(20, [att(101, status="done", started=T0, lease=None)]
               + [att(102, "judge", "running", started=T0, lease=lease_ok(20), unit=1)]),
           rec(30, [att(101, status="done", started=T0), att(102, "judge", "done", started=T0, unit=1)], step="N3", status="review")]
    f = Fake(seq)
    assert f.monitor().run(JOB, RUN) == "N3" and f.aborts == 0, (f.logs, f.aborts)
    assert f.states[-1]["phase"] == "N3" and f.states[-1]["all_done"] is True


@case
def n3_but_not_all_done_reported():
    f = Fake([rec(0, [att(101, status="done", started=T0), att(102, "judge", "failed", started=T0, error="X", unit=1)],
                  step="N3", status="review")])
    assert f.monitor().run(JOB, RUN) == "N3" and f.states[-1]["all_done"] is False


@case
def pending_not_started_5min():
    seq = [rec(s, [att(101, dispatched=0)]) for s in (0, 100, 200, 301)] + [rec(320, [att(101, status="cancelled")], status="draft", step="N1")]
    f = Fake(seq, after_abort=[rec(330, [att(101, status="cancelled")], status="draft", step="N1")])
    out = f.monitor().run(JOB, RUN)
    assert out == "aborted-settled" and f.aborts == 1, (out, f.logs)
    assert any("pending 미시작" in l for l in f.logs)


@case
def lease_expired_db_time():
    f = Fake([rec(50, [att(101, status="running", started=T0, lease=T0 + timedelta(seconds=50))])],  # == now → 만료
             after_abort=[rec(60, [att(101, status="cancelled")], status="draft", step="N1")])
    assert f.monitor().run(JOB, RUN) == "aborted-settled" and f.aborts == 1
    assert any("lease 만료" in l for l in f.logs)


@case
def lease_null_while_running():
    f = Fake([rec(5, [att(101, status="running", started=T0, lease=None)])],
             after_abort=[rec(15, [att(101, status="cancelled")], status="draft", step="N1")])
    f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and any("lease_expires_at=None" in l for l in f.logs)


@case
def lease_valid_no_stop():
    f = Fake([rec(5, [att(101, status="running", started=T0, lease=T0 + timedelta(seconds=6))]),
              rec(15, [att(101, status="done", started=T0)], step="N3", status="review")])
    assert f.monitor().run(JOB, RUN) == "N3" and f.aborts == 0


@case
def failed_attempt():
    f = Fake([rec(5, [att(101, status="failed", started=T0, error="OCR_ERROR")])],
             after_abort=[rec(15, [att(101, status="failed")], status="draft", step="N1")])
    f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and any("S1 실패" in l for l in f.logs)


@case
def retry_attempt():
    f = Fake([rec(5, [att(101, status="failed", started=T0), att(103, attempt_no=2, retry_origin="auto", dispatched=5)])],
             after_abort=[rec(15, [att(101, status="failed"), att(103, attempt_no=2, status="cancelled")], status="draft", step="N1")])
    f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and any("S1 재시도" in l for l in f.logs)


@case
def abort_fails_not_repeated_then_uncontrolled():
    base = [att(101, status="failed", started=T0)]
    f = Fake([rec(5, base)], abort_result=500,
             after_abort=[rec(15, base), rec(25, base + [att(104, attempt_no=2, retry_origin="recovery", dispatched=25)])])
    out = f.monitor().run(JOB, RUN)
    assert out == "uncontrolled" and f.aborts == 1, (out, f.aborts)
    assert any(s.get("phase") == "abort-failed" for s in f.states)


@case
def abort_timeout_not_repeated():
    base = [att(101, status="failed", started=T0)]
    f = Fake([rec(5, base)], abort_result=TimeoutError(), after_abort=[rec(15, [att(101, status="failed")])])
    out = f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and out.startswith("abort-failed-"), out


@case
def abort_409_at_n3():
    f = Fake([rec(5, [att(101, status="failed", started=T0)])], abort_result=409,
             after_abort=[rec(15, [att(101, status="done")], step="N3", status="review")])
    assert f.monitor().run(JOB, RUN) == "N3" and f.aborts == 1


@case
def single_query_error_recovers():
    f = Fake([rec(0, [att(101)]), RuntimeError("db"), rec(20, [att(101, status="done", started=T0)], step="N3", status="review")])
    assert f.monitor().run(JOB, RUN) == "N3" and f.aborts == 0
    assert any("0건으로 보지 않음" in l for l in f.logs)


@case
def three_query_errors_stop_once():
    f = Fake([RuntimeError("db")] * 3, after_abort=[rec(40, [att(101, status="cancelled")], status="draft", step="N1")])
    out = f.monitor().run(JOB, RUN)
    assert out == "aborted-settled" and f.aborts == 1, (out, f.logs)
    assert any("감시 불능" in l for l in f.logs)


@case
def errors_after_abort_never_settle():
    f = Fake([rec(5, [att(101, status="failed", started=T0)])], after_abort=[RuntimeError("db")])
    out = f.monitor().run(JOB, RUN)
    assert out == "aborted-unsettled" and f.aborts == 1, out


@case
def unsettled_while_queue_nonzero():
    q = {"ocr": 1, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0}
    f = Fake([rec(5, [att(101, status="failed", started=T0)])],
             after_abort=[rec(15, [att(101, status="failed")], status="draft", step="N1", queues=q)])
    assert f.monitor().run(JOB, RUN) == "aborted-unsettled" and f.aborts == 1


@case
def local_stop_flag():
    f = Fake([rec(0, [att(101)]), rec(10, [att(101, status="running", started=T0, lease=lease_ok(10))])], stop_at=1,
             after_abort=[rec(20, [att(101, status="cancelled")], status="draft", step="N1")])
    f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and any("S4" in l for l in f.logs)


@case
def other_job_stops_verdict_held():
    other = [{"job_id": 77, "stage": "analyze", "status": "running", "n": 1}]
    f = Fake([rec(0, [att(101)]), rec(10, [att(101)], others=other)],
             after_abort=[rec(20, [att(101, status="cancelled")], status="draft", step="N1", others=other)])
    out = f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and any("판정 보류" in l and "건드리지 않음" in l for l in f.logs), (out, f.logs)


@case
def abort_once_marker_blocks_second_send():
    class FakeApi:
        n = 0
        def call(self, method, path, timeout=30, **kw):
            FakeApi.n += 1
            class R: status_code = 200
            return R()
    a = FakeApi()
    for fn in ("ABORT_SENT",):
        try: os.remove(os.path.join(d.EV, fn))
        except FileNotFoundError: pass
    r1 = d.abort_once(a, "driver"); r2 = d.abort_once(a, "local-watcher")
    assert (r1, r2, FakeApi.n) == (200, "already-sent", 1), (r1, r2, FakeApi.n)


def _clear(*names):
    for n in names:
        try:
            os.remove(os.path.join(d.EV, n))
        except FileNotFoundError:
            pass


@case
def abort_once_atomic_under_race():
    import threading
    _clear("ABORT_SENT", "ABORT_RESULT")
    calls = []
    gate = threading.Barrier(8)

    class SlowApi:
        def call(self, method, path, timeout=30, **kw):
            calls.append(path)
            class R: status_code = 200
            return R()
    results = []

    def go(i):
        gate.wait()                      # 8개가 동시에 '파일 없음'을 확인하려 들게 한다
        results.append(d.abort_once(SlowApi(), f"t{i}"))
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(calls) == 1 and results.count(200) == 1 and results.count("already-sent") == 7, (calls, results)
    assert "요청 시도권 사용" in open(os.path.join(d.EV, "ABORT_SENT"), encoding="utf-8").read()


@case
def abort_result_recorded_separately():
    for kind, api_exc in (("timeout", None), ("error", RuntimeError("x"))):
        _clear("ABORT_SENT", "ABORT_RESULT")

        class Api:
            def call(self, method, path, timeout=30, **kw):
                if api_exc:
                    raise api_exc
                import time as _t
                _t.sleep(13)             # with_timeout(12s) 초과
        r = d.abort_once(Api(), "driver")
        res = open(os.path.join(d.EV, "ABORT_RESULT"), encoding="utf-8").read()
        assert r.startswith(kind) and f"result={r}" in res, (kind, r, res)
        assert os.path.exists(os.path.join(d.EV, "ABORT_SENT"))   # 시도권은 사용됨(성공 아님)


@case
def wait_watcher_ready_timeout_stop():
    _clear("WATCHER_READY", "STOP")
    t = [0.0]
    clock = lambda: t[0]
    def sleep(s): t[0] += s
    assert d.wait_watcher(clock, sleep) == "timeout"
    t[0] = 0.0
    open(os.path.join(d.EV, "WATCHER_READY"), "w").close()
    assert d.wait_watcher(clock, sleep) == "ready"
    open(os.path.join(d.EV, "STOP"), "w").write("S4 test")
    assert d.wait_watcher(clock, sleep) == "stop"       # STOP 이 먼저면 준비 표시가 있어도 발행 안 함
    _clear("WATCHER_READY", "STOP")


@case
def previous_run_rows_ignored():
    old = [att(17, status="cancelled", parent=16)]
    f = Fake([rec(0, old + [att(101)]), rec(10, old + [att(101, status="done", started=T0)], step="N3", status="review")])
    assert f.monitor().run(JOB, RUN) == "N3" and f.aborts == 0


@case
def unexpected_stage():
    f = Fake([rec(0, [att(101, stage="label")])], after_abort=[rec(10, [att(101, stage="label", status="cancelled")], status="draft", step="N1")])
    f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and any("예상 밖 단계" in l for l in f.logs)


@case
def n2_total_limit():
    seq = [rec(s, [att(101, status="running", started=T0 + timedelta(seconds=s), lease=lease_ok(s))]) for s in range(0, 1000, 10)]
    f = Fake(seq, after_abort=[rec(1000, [att(101, status="cancelled")], status="draft", step="N1")])
    f.monitor().run(JOB, RUN)
    assert f.aborts == 1 and any("N2 전체 제한" in l for l in f.logs)


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
