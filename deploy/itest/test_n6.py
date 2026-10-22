"""N6 감시(driver_n6)·판정(n6_verify) 모의 시험 — 운영 접속 없음. python -I test_n6.py"""
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["ITEST_EV"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import driver_n6 as d  # noqa: E402
import n6_verify as v  # noqa: E402

T0 = datetime(2026, 10, 13, 6, 0, 0, tzinfo=timezone.utc)
FR = 60
CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def row(id, parent, stage, status, run_kind=None, attempt_no=1, origin=None, started=0, lease=120, err=None, current=True):
    st = None if status == "pending" else T0 + timedelta(seconds=started)
    return {"id": id, "parent_task_id": parent, "run_kind": run_kind, "stage": stage, "unit_id": 9, "status": status,
            "attempt_no": attempt_no if parent else None, "is_current": current, "retry_count": (attempt_no or 1) - 1, "max_retry": 2,
            "retry_origin": origin, "error_code": err, "dispatch_count": 1, "last_dispatched_at": T0, "started_at": st,
            "finished_at": None, "heartbeat_at": st, "lease_expires_at": (T0 + timedelta(seconds=lease)) if lease is not None else None,
            "leased": True, "cancelled_at": None}


def ho(id, task, state, recv=5):
    return {"id": id, "task_id": task, "state": state, "outcome": "done", "received_at": T0 + timedelta(seconds=recv),
            "adopted_at": None, "reject_reason": None}


def rec(sec, kids, run_status="running", ds="done", step="N6", handoffs=(), queues=None, others=()):
    att = [row(37, None, None, ds, "downstream", started=0, lease=None), row(FR, None, None, run_status, "final_render", lease=None)] + kids
    return {"db_now": T0 + timedelta(seconds=sec), "job": {"status": "review", "current_step": step}, "attempts": att,
            "handoffs": list(handoffs), "queues": queues or {"ocr": 0, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0},
            "global_active": list(others)}


class Fake:
    def __init__(self, seq):
        self.seq, self.i, self.t, self.logs, self.states = seq, 0, 0.0, [], []

    def fetch(self, label):
        item = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        if isinstance(item, Exception):
            raise item
        return item

    def mon(self):
        return d.Monitor(self.fetch, lambda: None, clock=lambda: self.t, sleep=self._sleep, log=self.logs.append,
                         state=lambda **kw: self.states.append(kw))

    def _sleep(self, s):
        self.t += s


DONE = rec(30, [row(61, FR, "final_render", "done", lease=None)], run_status="done", handoffs=[ho(1, 61, "adopted")])


# ================= driver_n6 =================
@case
def success_requires_run37_final_run_attempt_handoff():
    f = Fake([rec(5, [row(61, FR, "final_render", "running", lease=125)]), DONE])
    m = f.mon()
    assert m.run(FR) == "N6-done" and m.requests == 0 and f.states[-1]["phase"] == "N6-done"


@case
def not_done_while_run37_still_running():
    r = rec(30, [row(61, FR, "final_render", "done", lease=None)], run_status="done", ds="running", handoffs=[ho(1, 61, "adopted")])
    assert d.final_state(r, FR) is None


@case
def not_done_until_handoff_adopted():
    r = rec(30, [row(61, FR, "final_render", "done", lease=None)], run_status="done", handoffs=[ho(1, 61, "verified")])
    assert d.final_state(r, FR) is None


@case
def not_done_without_adopted_handoff_even_if_quiet():
    # 시도 done·대표 done·정리됨이어도 채택된 인계가 없거나 rejected 면 완료로 보지 않는다
    for hs in ([], [ho(1, 61, "rejected")]):
        r = rec(30, [row(61, FR, "final_render", "done", lease=None)], run_status="done", handoffs=hs)
        assert d.final_state(r, FR) is None, hs


@case
def adoption_wait_not_flagged():
    r = rec(20, [row(61, FR, "final_render", "running", lease=10)], handoffs=[ho(1, 61, "received", recv=10)])
    assert d.decide(r, FR, 20) == []


@case
def retryable_failure_auto_retry_recorded_then_done():
    kids1 = [row(61, FR, "final_render", "failed", err="RENDER_IO", current=False), row(62, FR, "final_render", "running", attempt_no=2, origin="auto", lease=200)]
    done2 = rec(60, [row(61, FR, "final_render", "failed", err="RENDER_IO", current=False), row(62, FR, "final_render", "done", attempt_no=2, origin="auto", lease=None)],
                run_status="done", handoffs=[ho(2, 62, "adopted")])
    f = Fake([rec(20, kids1), done2])
    assert f.mon().run(FR) == "N6-done"
    st = f.states[-1]
    assert any("재시도" in r and "auto" in r for r in st["reasons"]) and any("렌더 실패" in r for r in st["reasons"]), st


@case
def non_retryable_failure_preserved_not_infinite_wait():
    r = rec(20, [row(61, FR, "final_render", "failed", err="STYLE_DEFAULTS_UNAPPROVED", lease=None)])
    f = Fake([r])
    assert f.mon().run(FR) == "N6-render-failed-preserved" and f.t < d.TICK * 3


@case
def failure_waits_for_quiet_queue_then_preserved():
    q = {"ocr": 0, "cpu": 1, "gpu": 0, "celery": 0, "unacked": 0}
    fl = [row(61, FR, "final_render", "failed", err="X", lease=None)]
    f = Fake([rec(20, fl, queues=q), rec(30, fl, queues=q), rec(40, fl)])
    assert f.mon().run(FR) == "N6-render-failed-preserved"


@case
def recovery_retry_recorded_separately():
    r = rec(200, [row(61, FR, "final_render", "failed", err="LEASE_EXPIRED", current=False),
                  row(62, FR, "final_render", "pending", attempt_no=2, origin="recovery")])
    rs = d.decide(r, FR, 200)
    assert any("복구 프로세스 lease 만료 재시도" in x for x in rs), rs


@case
def never_settles_is_unsettled_bounded():
    f = Fake([rec(5, [row(61, FR, "final_render", "running", lease=10 ** 6)])])
    out = f.mon().run(FR)
    assert out == "N6-unsettled" and f.t <= d.TOTAL_LIMIT_S + d.OBSERVE_S + d.TICK * 2, (out, f.t)


@case
def other_job_inflow_recorded_no_requests():
    other = [{"job_id": 77, "stage": "downstream", "status": "running", "n": 1}]
    f = Fake([rec(5, [row(61, FR, "final_render", "running", lease=125)], others=other), DONE])
    m = f.mon()
    assert m.run(FR) == "N6-done" and m.requests == 0 and any("S3 다른 작업 유입" in r for r in f.states[-1]["reasons"])


@case
def expired_lease_without_handoff_flagged():
    r = rec(200, [row(61, FR, "final_render", "running", lease=150)])
    assert any("계산 권한 만료" in x for x in d.decide(r, FR, 200))


@case
def query_errors_do_not_count_as_done():
    f = Fake([RuntimeError("db")] * 3 + [DONE])
    assert f.mon().run(FR) == "N6-done"
    assert any("0건으로 보지 않음" in l for l in f.logs) and any("관찰 전환" in l for l in f.logs)


class ConfirmApi:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def call(self, method, path, timeout=30, **kw):
        self.calls += 1
        assert path == "/jobs/9/confirm" and kw.get("json") == {"acknowledgedWarnings": []}
        if isinstance(self.result, Exception):
            raise self.result

        class R:
            status_code = self.result
        return R()


def _clear():
    for n in ("CONFIRM_SENT", "CONFIRM_RESULT"):
        try:
            os.remove(os.path.join(d.EV, n))
        except FileNotFoundError:
            pass


@case
def confirm_ok():
    _clear()
    a = ConfirmApi(200)
    assert d.send_confirm(a, lambda m: ("N6", 60), 50) == ("ok", 60) and a.calls == 1


@case
def confirm_rejected_not_resent():
    _clear()
    a = ConfirmApi(409)
    assert d.send_confirm(a, lambda m: ("N5", None), 50) == ("rejected:409", None) and a.calls == 1


@case
def confirm_lost_response_reflected_not_resent():
    _clear()
    a = ConfirmApi(TimeoutError())
    assert d.send_confirm(a, lambda m: ("N6", 60), 50) == ("reflected-after-unknown", 60) and a.calls == 1


@case
def confirm_lost_response_not_reflected_not_resent():
    _clear()
    a = ConfirmApi(TimeoutError())
    assert d.send_confirm(a, lambda m: ("N5", None), 50) == ("not-reflected-after-unknown", None) and a.calls == 1


@case
def confirm_second_attempt_blocked():
    _clear()
    a = ConfirmApi(200)
    d.send_confirm(a, lambda m: ("N6", 60), 50)
    assert d.send_confirm(a, lambda m: ("N6", 60), 50) == ("already-sent", None) and a.calls == 1


@case
def check_reports_failed_preserved_without_active():
    class Db:
        def snapshot(self, label):
            return rec(20, [row(61, FR, "final_render", "failed", err="X", lease=None)])
    open(os.path.join(d.EV, "state.json"), "w", encoding="utf-8").write(json.dumps({"run_id": FR}))
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d.check(Db())
    c = json.loads(buf.getvalue().split("CHECK ", 1)[1])
    assert c["active"] == [] and c["final_state"] == "failed-preserved" and c["downstream_run"] == "done", c


# ================= n6_verify.evaluate =================
def vd(**over):
    dd = {"run_id": FR, "job": {"status": "review", "current_step": "N6"},
          "runs": [{"id": 37, "status": "done", "run_kind": "downstream"}, {"id": FR, "status": "done", "run_kind": "final_render"}],
          "attempts": [{"id": 61, "stage": "final_render", "status": "done", "attempt_no": 1, "retry_origin": None, "error_code": None, "is_current": True}],
          "handoffs": [{"id": 1, "task_id": 61, "state": "adopted"}],
          "artifacts": [{"kind": "render_image", "part_key": p, "sha256": "h" + p, "declared_sha256": "h" + p, "width": 600, "height": hh, "state": "adopted"}
                        for p, hh in (("sec_10", 560), ("sec_11", 440), ("src_4", 1000))],
          "sections": {10: {"id": 10, "w": 600, "h": 560}, 11: {"id": 11, "w": 600, "h": 440}},
          "deliverables": [{"id": 1, "source_image_id": 4, "format": "png", "file_size": 5000, "render_status": "done", "image_key_matches_src": True}],
          "deliverable_sections": [{"section_id": 10, "stack_offset": 0, "order_no": 1}, {"section_id": 11, "stack_offset": 560, "order_no": 2}],
          "blocks": [{"id": i, "section_id": 10 if i < 4 else 11, "block_order": i, "role": "body", "overflow": True} for i in range(7)],
          "render_overflow": {str(i): True for i in range(7)},
          "s3": {"sec_10": {"hash": "match", "size": [600, 560]}, "sec_11": {"hash": "match", "size": [600, 440]},
                 "src_4": {"hash": "match", "size": [600, 1000]}, "deliverable": {"bytes": 5000, "hash": "match"}}}
    for fn in over.values():
        fn(dd)
    return dd


N5 = {"blocks": [{"id": i, "overflow": True} for i in range(7)]}


@case
def verify_processing_success_quality_separate():
    r = v.evaluate(vd(), N5)
    assert r["processing"] == "처리 성공" and r["quality"]["overflow_count"] == 7 and r["quality"]["same_as_n5"] is True, r


@case
def verify_failed_render_preserved():
    def f(dd):
        dd["attempts"][0].update(status="failed", error_code="X")
        dd["runs"][1]["status"] = "running"
    assert v.evaluate(vd(x=f))["processing"] == "처리 실패(렌더 실패 보존)"


@case
def verify_src_hash_mismatch_is_integrity_failure():
    def f(dd):
        dd["s3"]["src_4"]["hash"] = "mismatch"
    r = v.evaluate(vd(x=f))
    assert r["processing"] == "처리 검증 실패(산출물 무결성)" and r["integrity"], r


@case
def verify_deliverable_size_mismatch_is_integrity_failure():
    def f(dd):
        dd["s3"]["deliverable"]["bytes"] = 4999
    assert v.evaluate(vd(x=f))["processing"] == "처리 검증 실패(산출물 무결성)"


@case
def verify_wrong_stack_offsets_not_success():
    def f(dd):
        dd["deliverable_sections"][1]["stack_offset"] = 440
    r = v.evaluate(vd(x=f))
    assert r["processing"] != "처리 성공" and r["checks"]["deliverable_section 2건 순서·위치"] is False


@case
def verify_run37_not_done_not_success():
    def f(dd):
        dd["runs"][0]["status"] = "running"
    assert v.evaluate(vd(x=f))["processing"] != "처리 성공"


@case
def verify_retry_not_success():
    def f(dd):
        dd["attempts"] = [{"id": 61, "stage": "final_render", "status": "failed", "attempt_no": 1, "retry_origin": None, "error_code": "X", "is_current": False},
                          {"id": 62, "stage": "final_render", "status": "done", "attempt_no": 2, "retry_origin": "auto", "error_code": None, "is_current": True}]
        dd["handoffs"] = [{"id": 2, "task_id": 62, "state": "adopted"}]
    r = v.evaluate(vd(x=f))
    assert r["processing"] != "처리 성공" and r["retries"] == [62]


@case
def verify_unreadable_is_undetermined():
    def f(dd):
        dd["s3"]["sec_11"] = {"hash": "error:ClientError"}
    r = v.evaluate(vd(x=f))
    assert r["processing"] == "처리 미확정" and r["notes"]


@case
def verify_quality_change_vs_n5_reported():
    def f(dd):
        dd["render_overflow"]["3"] = False
    r = v.evaluate(vd(x=f), N5)
    assert r["quality"]["overflow_count"] == 6 and r["quality"]["same_as_n5"] is False


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    bad, ran = 0, 0
    for c in CASES:
        ran += 1
        try:
            c()
            print(f"ok   {c.__name__}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"FAIL {c.__name__}: {type(e).__name__}: {str(e)[:300]}")
    assert ran == len(CASES)
    print(f"RESULT ran={ran} pass={ran - bad} fail={bad}")
    sys.exit(1 if bad else 0)
