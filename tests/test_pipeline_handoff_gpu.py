"""GPU 실행기 — BE 제어 인터페이스 대역으로 권한 갱신 · 업로드 · 인계 · 정리 · 전체 취소를 검증한다(실제 GPU · 네트워크 아님)."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from pipeline.handoff import examples as ex
from pipeline.handoff.gpu import GpuInpaintRunner, HandoffAck, Lease, LeaseLost, UploadTarget


class FakeControl:
    def __init__(self, lease: Lease, *, lose_when=None, status: str = "verified"):
        self.lease, self.lose_when, self.status = lease, lose_when, status
        self.beats = 0
        self.uploads: list[tuple[str, str]] = []
        self.handoffs: list[dict] = []

    def acquire(self, task_id):
        assert task_id == self.lease.task_id
        return self.lease

    def heartbeat(self, lease):
        self.beats += 1
        if self.lose_when is not None and self.lose_when():
            raise LeaseLost("만료(합성)")

    def upload_target(self, lease, kind, part_key):
        return UploadTarget(url=f"https://upload.test/{kind}/{part_key}", object_key=f"staging/{lease.job_id}/{lease.task_id}/{lease.lease_epoch}/{kind}/{part_key}")

    def register_handoff(self, lease, envelope):
        assert "lease_token" not in repr(envelope) and lease.lease_token not in repr(envelope)  # 토큰은 본문에 넣지 않는다
        self.handoffs.append(envelope)
        return HandoffAck(handoff_id="h-1", status="received")

    def handoff_status(self, task_id, handoff_id):
        return self.status


class SlowModel(ex.FakeInpaintModel):
    """추론 중 권한 상실을 흉내 — abort가 오면 예외로 끝난다."""

    def __init__(self):
        self.aborted = threading.Event()
        self.started = threading.Event()

    def inpaint(self, image, mask):
        self.started.set()
        if not self.aborted.wait(5):
            raise AssertionError("abort가 오지 않았다")
        raise RuntimeError("중단됨")

    def abort(self, reason):
        self.aborted.set()


@pytest.fixture()
def setup(tmp_path):
    work = tmp_path / "work"
    req = next(r for n, r, _ in ex.cases(tmp_path / "src") if n == "inpaint.completed")
    image = Path(req["section_image"]["path"])
    stage_req = {k: v for k, v in req.items() if k not in ("section_image", "out_dir")}
    lease = Lease(task_id="t-9", job_id="j-1", lease_epoch=3, lease_token="secret-token", heartbeat_interval_s=0.01,
                  stage_request=stage_req, inputs={"section_image": {"sha256": req["section_image"]["sha256"], "download_url": f"file://{image}"}})
    put_log: list[tuple[str, bytes]] = []

    def fetch(url, dest):
        shutil.copyfile(url[len("file://"):], dest)

    def put(target, path):
        put_log.append((target.object_key, path.read_bytes()))

    return lease, work, fetch, put, put_log


def test_normal_run_uploads_registers_and_cleans_after_verified(setup):
    lease, work, fetch, put, put_log = setup
    ctl = FakeControl(lease)
    runner = GpuInpaintRunner(ctl, ex.config(), work_root=work, model_factory=lambda cfg: ex.FakeInpaintModel(), fetch=fetch, put=put)
    out = runner.run_task("t-9")
    assert out.status == "registered" and out.report.outcome == "completed"
    assert {u["kind"] for u in out.uploads} == {"background", "delete_mask", "protect_mask"}
    assert len(put_log) == 3 and ctl.handoffs[0]["lease_epoch"] == 3
    assert out.workdir == work / "j-1" / "t-9" / "3" and out.workdir.exists()
    assert runner.cleanup(out) is True and not out.workdir.exists()


def test_received_only_does_not_clean(setup):
    lease, work, fetch, put, _ = setup
    ctl = FakeControl(lease, status="received")
    runner = GpuInpaintRunner(ctl, ex.config(), work_root=work, model_factory=lambda cfg: ex.FakeInpaintModel(), fetch=fetch, put=put)
    out = runner.run_task("t-9")
    assert runner.cleanup(out) is False and out.workdir.exists()


def test_lease_lost_during_inference_aborts_and_keeps_files(setup):
    lease, work, fetch, put, put_log = setup
    model = SlowModel()
    ctl = FakeControl(lease, lose_when=model.started.is_set)  # 추론이 시작된 뒤 첫 갱신에서 권한 상실
    runner = GpuInpaintRunner(ctl, ex.config(), work_root=work, model_factory=lambda cfg: model, fetch=fetch, put=put)
    out = runner.run_task("t-9")
    assert out.status == "lease_lost" and model.aborted.is_set()
    assert out.report.outcome == "failed" and out.report.failure.kind == "cancelled"
    assert put_log == [] and ctl.handoffs == [] and out.workdir.exists()


def test_input_hash_mismatch_reports_failure_without_compute(setup):
    lease, work, fetch, put, _ = setup
    bad = Lease(**{**lease.__dict__, "inputs": {"section_image": {**lease.inputs["section_image"], "sha256": "0" * 64}}})
    ctl = FakeControl(bad)
    calls = []
    runner = GpuInpaintRunner(ctl, ex.config(), work_root=work, model_factory=lambda cfg: calls.append(1), fetch=fetch, put=put)
    out = runner.run_task("t-9")
    assert out.status == "registered" and out.report.failure.kind == "input_invalid" and calls == []


def test_upload_failure_keeps_result_for_upload_retry(setup):
    lease, work, fetch, _, _ = setup

    def broken(target, path):
        raise OSError("네트워크(합성)")

    ctl = FakeControl(lease)
    runner = GpuInpaintRunner(ctl, ex.config(), work_root=work, model_factory=lambda cfg: ex.FakeInpaintModel(), fetch=fetch, put=broken)
    out = runner.run_task("t-9")
    assert out.status == "upload_failed" and out.report.outcome == "completed" and ctl.handoffs == []
    assert all(Path(a.path).exists() for a in out.report.artifacts)


def test_purge_job_deletes_all_local_material(setup):
    lease, work, fetch, put, _ = setup
    runner = GpuInpaintRunner(FakeControl(lease), ex.config(), work_root=work, model_factory=lambda cfg: ex.FakeInpaintModel(),
                              fetch=fetch, put=put)
    out = runner.run_task("t-9")
    assert out.workdir.exists()
    assert runner.purge_job("j-1") is True and not (work / "j-1").exists()


def test_unsafe_ids_rejected(setup):
    lease, work, fetch, put, _ = setup
    bad = Lease(**{**lease.__dict__, "job_id": "../x"})
    runner = GpuInpaintRunner(FakeControl(bad), ex.config(), work_root=work, fetch=fetch, put=put)
    with pytest.raises(ValueError):
        runner.run_task("t-9")


def test_discard_status_allows_cleanup_but_rejected_does_not(setup):
    lease, work, fetch, put, _ = setup
    for status, deleted in (("discard", True), ("rejected", False)):
        ctl = FakeControl(lease, status=status)
        runner = GpuInpaintRunner(ctl, ex.config(), work_root=work / status, model_factory=lambda cfg: ex.FakeInpaintModel(),
                                  fetch=fetch, put=put)
        out = runner.run_task("t-9")
        assert runner.cleanup(out) is deleted and out.workdir.exists() is not deleted


class TimeoutOnceModel(ex.FakeInpaintModel):
    """첫 호출에서 시간 초과로 닫히는 자식 프로세스 모델 흉내."""

    def __init__(self):
        self.closed = False

    def inpaint(self, image, mask):
        self.closed = True
        raise RuntimeError("시간 초과(합성)")

    def close(self):
        self.closed = True


def test_failed_model_is_not_reused(setup):
    lease, work, fetch, put, _ = setup
    made: list[object] = []

    def factory(cfg):
        m = TimeoutOnceModel() if not made else ex.FakeInpaintModel()
        made.append(m)
        return m

    ctl = FakeControl(lease)
    runner = GpuInpaintRunner(ctl, ex.config(), work_root=work, model_factory=factory, fetch=fetch, put=put)
    first = runner.run_task("t-9")
    assert first.report.failure.kind == "model_call_failed"
    second_lease = Lease(**{**lease.__dict__, "lease_epoch": 4})
    ctl.lease = second_lease
    second = runner.run_task("t-9")
    assert second.report.outcome == "completed" and len(made) == 2  # 닫힌 모델을 버리고 새로 만들었다
