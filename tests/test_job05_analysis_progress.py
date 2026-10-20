"""JOB-05 analysisProgress — N2 분석 진행 표시(실제 PostgreSQL + AI 대역, Redis 스냅샷은 가짜 read)."""
from __future__ import annotations

import pytest

from app import ai_adapters, analysis_progress, execution
from tests.exec_db import requires_db
from tests.exec_fakes import FakeAnalyzer, FakeJudge
from tests.test_exec_analysis import _client, _job, _q, dburl, env  # noqa: F401 — 픽스처 재사용

pytestmark = requires_db

ZERO = "0" * 64


def _start(env):
    ids = _job(env)
    c = _client(ids)
    r = c.post(f"/v1/jobs/{ids['job']}/analyze")
    assert r.status_code == 202, r.text
    return ids, c, r.json()["taskId"]


def _ap(c, ids):
    return c.get(f"/v1/jobs/{ids['job']}/tasks").json().get("analysisProgress")


def _fake_read(monkeypatch, value):
    monkeypatch.setattr(analysis_progress, "read", lambda attempt_id, client=None: value)


def _snap(event, epoch, **kw):
    s = {"event": event, "source_index": 1, "source_total": 2, "source_image_id": 11, "region_index": 2,
         "region_count_in_source": 3, "regions_done": 1, "regions_known": 3, "sources_split": 1}
    s.update(kw)
    return {"epoch": epoch, "seq": 1, "event": event, "snap": s}


def _running(att_id):
    lease = execution.acquire(att_id, "test-worker")
    assert lease is not None
    return lease


def _handoff(ids, att_id, epoch, outcome, state="received", late=False):
    _q("INSERT INTO task_handoff (task_id, job_id, lease_epoch, late, contract_version, input_fingerprint, outcome, target_count, "
       "payload, manifest_sha256, state) VALUES (:t, :j, :e, :l, '1', :z, :o, 1, CAST('{}' AS jsonb), :z, :s)",
       t=att_id, j=ids["job"], e=epoch, l=late, z=ZERO, o=outcome, s=state)


def test_not_n2_has_no_key(env):
    ids, c, _ = _start(env)
    env["q"].drain()
    body = c.get(f"/v1/jobs/{ids['job']}/tasks").json()
    assert body["currentStep"] == "N3" and "analysisProgress" not in body


def test_analyze_waiting(env, monkeypatch):
    _fake_read(monkeypatch, None)
    ids, c, _ = _start(env)
    ap = _ap(c, ids)
    assert ap == {"phase": "analyze", "status": "waiting", "sourceTotal": 1, "activity": None, "current": None,
                  "regions": None, "judge": None}


@pytest.mark.parametrize("event,activity,region_index", [
    ("split_start", "split", None), ("ocr_start", "ocr", 2), ("region_done", "merge", 2),
])
def test_analyze_running_snapshot_mapping(env, monkeypatch, event, activity, region_index):
    ids, c, att = _start(env)
    lease = _running(att)
    _fake_read(monkeypatch, _snap(event, lease.epoch))
    ap = _ap(c, ids)
    assert ap["phase"] == "analyze" and ap["status"] == "running" and ap["activity"] == activity
    assert ap["current"] == {"sourceIndex": 1, "sourceImageId": 11, "regionIndex": region_index, "regionCountInSource": 3}
    assert ap["regions"] == {"done": 1, "known": 3, "totalFixed": False}
    assert ap["judge"] is None


def test_failed_handoff_has_no_activity(env, monkeypatch):
    ids, c, att = _start(env)
    lease = _running(att)
    _fake_read(monkeypatch, _snap("region_done", lease.epoch))
    _handoff(ids, att, lease.epoch, "failed")
    ap = _ap(c, ids)
    assert (ap["activity"], ap["current"], ap["regions"]) == (None, None, None)


def test_done_handoff_with_merge_snapshot_is_finalize_without_regions(env, monkeypatch):
    ids, c, att = _start(env)
    lease = _running(att)
    _fake_read(monkeypatch, _snap("merge_start", lease.epoch))
    _handoff(ids, att, lease.epoch, "done", state="verifying")
    ap = _ap(c, ids)
    assert (ap["activity"], ap["current"], ap["regions"]) == ("finalize", None, None)


def test_analyze_done_snapshot_is_finalize(env, monkeypatch):
    ids, c, att = _start(env)
    lease = _running(att)
    _fake_read(monkeypatch, {"epoch": lease.epoch, "seq": 9, "event": "analyze_done",
                             "snap": {"regions_done": 4, "regions_known": 4, "sources_split": 1, "source_total": 1}})
    ap = _ap(c, ids)
    assert ap["activity"] == "finalize" and ap["current"] is None
    assert ap["regions"] == {"done": 4, "known": 4, "totalFixed": True}


def test_lease_expired_hides_activity(env, monkeypatch):
    ids, c, att = _start(env)
    lease = _running(att)
    _fake_read(monkeypatch, _snap("ocr_start", lease.epoch))
    _q("UPDATE job_async_task SET lease_expires_at = now() - interval '1 second' WHERE id = :t", t=att)
    ap = _ap(c, ids)
    assert ap["status"] == "running" and (ap["activity"], ap["current"], ap["regions"]) == (None, None, None)


def test_epoch_mismatch_snapshot_ignored(env, monkeypatch):
    ids, c, att = _start(env)
    lease = _running(att)
    _fake_read(monkeypatch, _snap("ocr_start", lease.epoch - 1))
    ap = _ap(c, ids)
    assert (ap["activity"], ap["current"], ap["regions"]) == (None, None, None)


def test_judge_partial_exclusion(env, monkeypatch):
    _fake_read(monkeypatch, None)
    judge = FakeJudge()
    ai_adapters.set_adapters(judge=judge)
    ids, c, _ = _start(env)
    env["q"].drain(limit=1)
    sec_ids = sorted(r["id"] for r in _q("SELECT id FROM section WHERE job_id = :j", j=ids["job"]))
    assert len(sec_ids) >= 2
    judge.local_fail = {f"sec_{s}" for s in sec_ids}
    env["q"].drain(limit=1)  # 판정 1건만 처리 → 현지 판정 실패 제외 1, 나머지 대기
    ap = _ap(c, ids)
    n = len(sec_ids)
    assert ap["phase"] == "judge" and ap["status"] == "waiting" and ap["activity"] is None and ap["current"] is None
    assert ap["regions"] == {"done": n, "known": n, "totalFixed": True}
    assert ap["judge"] == {"total": n, "done": 0, "excluded": 1, "running": 0, "waiting": n - 1, "retrying": 0,
                           "failed": 0, "cancelled": 0}


def test_judge_run_failure_counts_cancelled(env, monkeypatch):
    _fake_read(monkeypatch, None)
    from app import db as app_db
    from app.flows.analysis import fail_run

    ids, c, _ = _start(env)
    env["q"].drain(limit=2)  # 분석 + 판정 1건
    n = _q("SELECT count(*) AS n FROM section WHERE job_id = :j", j=ids["job"])[0]["n"]
    run = _q("SELECT * FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL", j=ids["job"])[0]
    with app_db.SessionLocal() as db:
        fail_run(db, run, "REGULATORY_JUDGE_FAILED", None)  # 판정 도중 실행 실패 → 남은 판정 cancelled
        db.commit()
    body = c.get(f"/v1/jobs/{ids['job']}/tasks").json()
    assert (body["jobStatus"], body["currentStep"]) == ("failed", "N2")
    ap = body["analysisProgress"]
    assert ap["phase"] == "judge" and ap["status"] == "failed"
    assert ap["judge"] == {"total": n, "done": 1, "excluded": 0, "running": 0, "waiting": 0, "retrying": 0,
                           "failed": 0, "cancelled": n - 1}


class _BrokenRedis:
    def eval(self, *a, **k):
        raise ConnectionError("redis down")

    def get(self, *a, **k):
        raise ConnectionError("redis down")


@pytest.mark.parametrize("broken_ctor", [False, True])
def test_analysis_succeeds_when_progress_redis_fails(env, monkeypatch, broken_ctor):
    def fake_client(timeout):
        if broken_ctor:
            raise ConnectionError("redis down")
        return _BrokenRedis()

    monkeypatch.setattr(analysis_progress, "_client", fake_client)
    ai_adapters.set_adapters(analyzer=FakeAnalyzer())
    ids, c, _ = _start(env)
    env["q"].drain()
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert (job["status"], job["currentStep"]) == ("review", "N3")
