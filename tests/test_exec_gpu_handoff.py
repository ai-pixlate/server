"""⑥ 원격 GPU — AI 실행기(pipeline.handoff.gpu.GpuInpaintRunner) + BE ControlClient 구현(BeControlClient) + register_report.

InProcess 제어 대역·메모리 저장소·가짜 모델로 확인한다. 실제 학교 GPU·HTTP 바인딩·S3 연결 검증이 아니다.
"""
from __future__ import annotations

import pytest

from app import ai_adapters, execution
from tests.exec_db import requires_db
from tests.exec_fakes import FakeLabeler
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401 — 픽스처 재사용

pytestmark = requires_db


def test_empty_mask_is_adopted_as_unchanged_with_original_background(env):
    # 모든 블록이 라벨 → ⑥ 최종 마스크 0 → AI skipped(empty_mask) → BE done·원본 배경 참조(5.31·5.32 K5)
    ai_adapters.set_adapters(labeler=FakeLabeler(label_words=("",)))
    ids = _to_n3(env)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    secs = _q("SELECT image_key, inpaint_status, inpaint_image_url, mask_image_url FROM section WHERE job_id = :j", j=ids["job"])
    assert all(s["inpaint_status"] == "done" and s["inpaint_image_url"] == s["image_key"] and s["mask_image_url"] for s in secs)
    assert env["model"].calls == 0  # 빈 마스크는 모델을 부르지 않는다
    hs = _q("SELECT h.payload FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j AND a.stage = 'inpaint'",
            j=ids["job"])
    assert all(h["payload"]["status"] == "unchanged" and h["payload"]["ai_report"]["skip_reason"] == "empty_mask" for h in hs)


def test_register_report_rejects_upload_hash_mismatch_and_keeps_local(env, monkeypatch):
    from app.flows import gpu_worker

    ids = _to_n3(env)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    held = [m for m in env["q"].drain(skip={"app.tasks.run_inpaint"}) if m[0] == "app.tasks.run_inpaint"]
    orig = gpu_worker.InProcessControlClient.register_report

    def tamper(self, attempt_id, epoch, token, envelope):
        envelope = {**envelope, "uploads": [{**u, "sha256": "0" * 64} for u in envelope["uploads"]]}
        return orig(self, attempt_id, epoch, token, envelope)

    monkeypatch.setattr(gpu_worker.InProcessControlClient, "register_report", tamper)
    out = gpu_worker.execute_inpaint_remote(held[0][1][0])
    assert out["status"] == "register_failed" and out["localKept"] is True  # 거절 — 계산 결과는 저장 복구용으로 보존
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t", t=held[0][1][0])[0]["n"] == 0


def test_register_report_rejects_wrong_attempt_identity(env):
    from app.flows.gpu_worker import InProcessControlClient

    ids = _to_n3(env)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    tid = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' ORDER BY id LIMIT 1", j=ids["job"])[0]["id"]
    cl = InProcessControlClient("gpu-report")
    got = cl.acquire(tid)
    report = {"contract_version": "handoff@2026-10-08.1", "execution_id": "x", "attempt_id": str(tid + 999), "stage": "inpaint",
              "input_manifest": None, "input_manifest_sha256": None, "outcome": "failed", "target_count": 1,
              "failure": {"kind": "model_call_failed", "retryable": True, "message": "oom"}}
    with pytest.raises(execution.AuthRejected):
        cl.register_report(tid, got["epoch"], got["token"], {"task_id": str(tid), "lease_epoch": got["epoch"], "report": report,
                                                             "uploads": []})
