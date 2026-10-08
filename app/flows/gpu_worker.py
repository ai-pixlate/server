"""⑥ 원격 GPU 실행 — BE 제어 서비스(WorkerControl) · GPU 측 실행기 · BE 채택(integration-decisions.md 5.28~5.32).

경계(5.32 1안, 사용자 결정 2026-10-08: 서비스 계층만 구현)
- GPU 워커는 DB·AWS 자격증명을 쓰지 않는다. 실행 입력 조회·갱신·업로드 URL·인계 등록·상태 조회는 모두 WorkerControl 을 거친다.
  HTTP 경로·서비스 인증 형식은 api-tracking 명세 확정 후 바인딩한다. 지금은 InProcessControlClient(같은 프로세스 호출)만 있다 —
  학교 GPU 운영 배포에는 HTTP 바인딩이 먼저 필요하다.
- worker_id 는 인증된 서비스 주체다(요청 본문 주장값이 아니라 서비스 인증에서 정해진다).
- 시도 토큰은 권한 획득 응답으로만 받는다. 로그·큐·인계 본문에 넣지 않는다.
- 파일은 BE 가 발급한 정확한 스테이징 키 하나에 업로드하고(presigned PUT), BE 가 내려받아 측정한 같은 바이트만 검증 키에 고정한다.
- GPU 로컬 입력·출력은 인계가 '검증됨' 또는 '채택'으로 확인된 뒤에만 지운다(원격 복구 가능 확인, 5.30). 수신 확인만으로 지우지 않는다.
- GPU 측 실행기는 AI 인계 계층의 pipeline.handoff.gpu.GpuInpaintRunner 하나다(독립 갱신·권한 상실 시 추론 중단·purge_job).
  BE 는 그 ControlClient Protocol 구현(BeControlClient)만 제공하고, 인계 등록 본문은 AI StageReport + 업로드 목록을
  register_report 로 받아 BE 인계로 옮긴다(PR #55 BE 확인 6). register(BE 봉투)는 내부·시험용 저수준 경로다.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text

from app import ai_adapters, artifacts, execution
from app import db as app_db
from app.execution import AdoptionRejected, ArtifactSpec, Envelope, Lease
from app.flows.common import block_key, json_value, section_key
from app.manifest import stable_hash
from app.storage import get_store

log = logging.getLogger(__name__)
INPAINT_KINDS = ("background", "delete_mask", "protect_mask")


# ---------------------------------------------------------------------------------------------------------
# BE 제어 서비스
# ---------------------------------------------------------------------------------------------------------
class WorkerControl:
    """GPU 워커에 제공하는 BE 동작. 각 메서드는 서비스 인증으로 확인된 worker_id 를 받는다."""

    def _lease(self, worker_id: str, attempt_id: int, epoch: int, token: str) -> Lease:
        db = app_db.SessionLocal()
        try:
            a = db.execute(text("SELECT * FROM job_async_task WHERE id = :t"), {"t": attempt_id}).mappings().first()
        finally:
            db.close()
        if a is None:
            raise execution.AuthRejected("시도 없음")
        return Lease(attempt_id=attempt_id, job_id=a["job_id"], run_id=a["parent_task_id"], stage=a["stage"], unit_type=a["unit_type"],
                     unit_id=a["unit_id"], epoch=epoch, owner=worker_id, manifest=a["input_manifest"],
                     fingerprint=a["input_fingerprint"], token=token)

    def acquire(self, worker_id: str, attempt_id: int) -> dict[str, Any] | None:
        lease = execution.acquire(attempt_id, worker_id, remote_worker_id=worker_id)
        if lease is None:
            return None
        if lease.stage != "inpaint":
            raise execution.AuthRejected("이 제어 경로는 ⑥만 다룬다")
        return {"attempt_id": attempt_id, "job_id": lease.job_id, "run_id": lease.run_id, "epoch": lease.epoch, "token": lease.token,
                "ttl_s": execution.lease_ttl_s(), "fingerprint": lease.fingerprint, "inputs": self._inputs(lease)}

    def _inputs(self, lease: Lease) -> dict[str, Any]:
        """고정 입력 복원: 섹션 이미지(다운로드 URL·해시), ③ 블록, 채택된 ④ 결과, ⑤ 결과와 logo_record 실제 payload."""
        from app.flows import downstream

        m = lease.manifest
        db = app_db.SessionLocal()
        try:
            sec = downstream._section_row(db, m["section_id"])
            rows = downstream._block_rows(db, m["section_id"])
            label = downstream._adopted_payload(db, lease.run_id, "label", m["section_id"])
            logo = downstream._adopted_payload(db, lease.run_id, "logo", m["section_id"])
        finally:
            db.close()
        if downstream._blocks_fp(rows) != m["blocks_fp"]:
            raise AdoptionRejected("블록이 고정 입력과 다르다", code="INPUT_CHANGED")
        targets = sum(1 for r in rows if r["is_excluded"] is False)
        return {
            "image_id": downstream._image_id(sec),
            "section": downstream._pipeline_section(sec, "").model_dump(mode="json"),
            "section_image": {"url": get_store().presign_get(sec["image_key"], execution.upload_url_ttl_s()),
                              "key": sec["image_key"], "sha256": m["section_image_sha256"]},
            "blocks": [b.model_dump(mode="json") for b in downstream._text_blocks(sec["id"], rows)],
            "label": downstream._check_ref(m, "label", label)["label"] if label else None,
            "logo": downstream._check_ref(m, "logo", logo) if logo else None,
            "targets": targets,
        }

    def heartbeat(self, worker_id: str, attempt_id: int, epoch: int, token: str) -> bool:
        return execution.heartbeat(self._lease(worker_id, attempt_id, epoch, token))

    def upload_url(self, worker_id: str, attempt_id: int, epoch: int, token: str, kind: str, part_key: str) -> dict[str, str]:
        lease = self._lease(worker_id, attempt_id, epoch, token)
        if kind not in artifacts.STAGE_KINDS.get(lease.stage, ()):
            raise execution.AuthRejected(f"이 단계에 허용하지 않는 산출 종류 {kind}")
        db = app_db.SessionLocal()
        try:
            if not execution.check_lease(db, lease):
                raise execution.LeaseLost("현재 실행 권한이 아니다 — 업로드 URL 을 발급하지 않는다")
        finally:
            db.close()
        key = artifacts.staging_key(lease.job_id, attempt_id, epoch, kind, part_key)
        return {"key": key, "url": get_store().presign_put(key, execution.upload_url_ttl_s(), "image/png")}

    def register(self, worker_id: str, attempt_id: int, epoch: int, token: str, envelope: dict[str, Any]) -> dict[str, Any]:
        lease = self._lease(worker_id, attempt_id, epoch, token)
        env = Envelope(
            outcome=envelope["outcome"], target_count=int(envelope["target_count"]), payload=envelope["payload"],
            input_fingerprint=envelope["input_fingerprint"], impl_version=envelope.get("impl_version"),
            skip_reason=envelope.get("skip_reason"), contract_version=envelope.get("contract_version", execution.CONTRACT_VERSION),
            artifacts=[ArtifactSpec(kind=a["kind"], part_key=a["part_key"], staging_key=a.get("staging_key"),
                                    declared_sha256=a.get("declared_sha256"), source_ref=a.get("source_ref"))
                       for a in envelope.get("artifacts", [])],
        )
        for a in env.artifacts:  # 발급한 스테이징 키 형식만 받는다(다른 job·시도·세대 키 거절)
            if a.staging_key is not None and a.staging_key != artifacts.staging_key(lease.job_id, attempt_id, epoch, a.kind, a.part_key):
                raise execution.AuthRejected("발급하지 않은 업로드 키")
        h = execution.register_handoff(lease, env, remote_worker_id=worker_id)
        return {"handoff_id": h["id"], "state": h["state"], "late": h["late"]}

    def register_report(self, worker_id: str, attempt_id: int, epoch: int, token: str, envelope: dict[str, Any]) -> dict[str, Any]:
        """GPU 실행기(GpuInpaintRunner)의 인계 등록 본문 — {runner, task_id, lease_epoch, report: StageReport, uploads[]} — 을
        BE 인계로 옮겨 등록한다. 산출물은 업로드 목록의 발급 키·주장 해시로만 받고, 내용 검증·고정은 채택 단계에서 BE 가 다시 한다."""
        from pydantic import ValidationError

        from pipeline.handoff.envelope import StageReport

        lease = self._lease(worker_id, attempt_id, epoch, token)
        try:
            rep = StageReport.model_validate(envelope["report"])
        except (KeyError, ValidationError) as e:
            raise execution.AuthRejected(f"인계 보고 형식 오류: {e}") from e
        if rep.stage != "inpaint" or rep.attempt_id != str(attempt_id) or int(envelope.get("lease_epoch", -1)) != epoch:
            raise execution.AuthRejected("인계 보고의 단계·시도·세대가 권한과 다르다")
        ups = {(u["kind"], u["part_key"]): u for u in envelope.get("uploads", [])}
        arts = []
        for a in rep.artifacts:
            u = ups.pop((a.kind, a.part_key), None)
            if u is None or u.get("sha256") != a.sha256:
                raise execution.AuthRejected(f"산출물 {a.kind}/{a.part_key} 의 업로드 기록이 없거나 해시가 다르다")
            arts.append({"kind": a.kind, "part_key": a.part_key, "staging_key": u["object_key"], "declared_sha256": a.sha256})
        if ups:
            raise execution.AuthRejected(f"보고에 없는 업로드 {sorted(ups)}")
        if rep.source_refs:
            db = app_db.SessionLocal()
            try:
                sec_key = db.execute(text("SELECT image_key FROM section WHERE id = :s"), {"s": lease.manifest["section_id"]}).scalar()
            finally:
                db.close()
            for r in rep.source_refs:  # 원본 섹션 배경 참조 — BE 가 아는 섹션 이미지 키로 바꾸고 해시는 채택 때 다시 대조
                arts.append({"kind": r.kind, "part_key": r.part_key, "source_ref": {"key": sec_key, "sha256": r.sha256}})
        p = dict(rep.payload or {})
        p["model"] = (rep.implementation or {}).get("model")
        if rep.outcome == "failed":
            f = rep.failure
            body = {"outcome": "failed", "target_count": rep.target_count,
                    "payload": {**p, "error_code": "INPAINT_FAILED", "failure_kind": f.kind, "message": f"{f.kind}: {f.message}",
                                "retryable": False}}  # 인페인트 실패는 원본 배경 대체·재시도 버튼 없음(D9-3)
        else:  # completed(inpainted) · skipped(empty_mask = unchanged, 원본 배경 참조)
            body = {"outcome": "done", "target_count": rep.target_count, "payload": p}
        body.update({"input_fingerprint": lease.fingerprint, "impl_version": str((rep.implementation or {}).get("adapter")),
                     "artifacts": arts, "ai_report": {"contract_version": rep.contract_version, "outcome": rep.outcome,
                                                      "skip_reason": rep.skip_reason, "input_manifest_sha256": rep.input_manifest_sha256}})
        body["payload"] = {**body["payload"], "ai_report": body.pop("ai_report")}
        return self.register(worker_id, attempt_id, epoch, token, body)

    def status(self, worker_id: str, attempt_id: int, epoch: int, token: str) -> dict[str, Any]:
        """인계 상태 조회. remote_recoverable=True 이면 GPU 로컬 자료를 지워도 된다(검증됨·채택)."""
        db = app_db.SessionLocal()
        try:
            execution._verify_past_grant(db, attempt_id, epoch, worker_id, token)
            h = db.execute(text("SELECT id, state, reject_reason FROM task_handoff WHERE task_id = :t AND lease_epoch = :e"),
                           {"t": attempt_id, "e": epoch}).mappings().first()
            a = db.execute(text("SELECT a.status, a.is_current, j.status AS job_status FROM job_async_task a JOIN job j ON j.id = a.job_id "
                                "WHERE a.id = :t"), {"t": attempt_id}).mappings().first()
            db.rollback()
        finally:
            db.close()
        gone = a is None or a["job_status"] == "archived" or a["status"] == "cancelled" or not a["is_current"]
        if h is None:
            # 전체 취소·중단·대체된 시도는 로컬 자료도 버린다(D9-4). 그 밖에는 복구 가능 확인 전까지 보존
            return {"state": None, "remote_recoverable": False, "discard": bool(gone)}
        return {"handoff_id": h["id"], "state": h["state"], "remote_recoverable": h["state"] in ("verified", "adopted"),
                "discard": h["state"] == "rejected"}


class ControlClient(Protocol):
    def acquire(self, attempt_id: int) -> dict[str, Any] | None: ...
    def heartbeat(self, attempt_id: int, epoch: int, token: str) -> bool: ...
    def upload_url(self, attempt_id: int, epoch: int, token: str, kind: str, part_key: str) -> dict[str, str]: ...
    def put(self, url: str, key: str, data: bytes) -> None: ...
    def fetch(self, url: str, key: str) -> bytes: ...
    def register(self, attempt_id: int, epoch: int, token: str, envelope: dict[str, Any]) -> dict[str, Any]: ...
    def register_report(self, attempt_id: int, epoch: int, token: str, envelope: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, attempt_id: int, epoch: int, token: str) -> dict[str, Any]: ...


class InProcessControlClient:
    """같은 프로세스에서 WorkerControl 을 부른다(로컬 검증·테스트). 운영 GPU 는 HTTP 바인딩 클라이언트로 바꾼다."""

    def __init__(self, worker_id: str, control: WorkerControl | None = None):
        self.worker_id = worker_id
        self.control = control or WorkerControl()

    def acquire(self, attempt_id):
        return self.control.acquire(self.worker_id, attempt_id)

    def heartbeat(self, attempt_id, epoch, token):
        return self.control.heartbeat(self.worker_id, attempt_id, epoch, token)

    def upload_url(self, attempt_id, epoch, token, kind, part_key):
        return self.control.upload_url(self.worker_id, attempt_id, epoch, token, kind, part_key)

    def put(self, url, key, data):
        get_store().put(key, data, "image/png")  # presigned PUT 대역

    def fetch(self, url, key):
        return get_store().get(key)  # presigned GET 대역

    def register(self, attempt_id, epoch, token, envelope):
        return self.control.register(self.worker_id, attempt_id, epoch, token, envelope)

    def register_report(self, attempt_id, epoch, token, envelope):
        return self.control.register_report(self.worker_id, attempt_id, epoch, token, envelope)

    def status(self, attempt_id, epoch, token):
        return self.control.status(self.worker_id, attempt_id, epoch, token)


_client_factory = None


def set_client_factory(fn) -> None:
    global _client_factory
    _client_factory = fn


def _client() -> ControlClient:
    if _client_factory is not None:
        return _client_factory()
    return InProcessControlClient(os.getenv("PIXLATE_GPU_WORKER_ID", f"gpu:{execution.worker_identity()}"))


# ---------------------------------------------------------------------------------------------------------
# GPU 측 실행기 — AI GpuInpaintRunner + BE ControlClient Protocol 구현
# ---------------------------------------------------------------------------------------------------------
class NotRunnable(Exception):
    """실행 권한을 얻지 못했다(이미 처리 중·완료·취소). 계산하지 않는다."""


class BeControlClient:
    """pipeline.handoff.gpu.ControlClient 구현 — 저수준 BE 제어 클라이언트(InProcess 또는 HTTP 바인딩)를 감싼다.

    acquire 응답을 ⑥ 인계 요청(stage_request)과 내려받기 입력으로 바꾸고, 갱신 실패·권한 상실을 AI LeaseLost 로 옮긴다.
    상태 조회는 BE 상태를 AI 상태 문자열로 옮긴다: 검증됨·채택만 로컬 정리 대상(5.30)."""

    def __init__(self, client: ControlClient):
        self.client = client
        self._leases: dict[str, Any] = {}
        self._keys: dict[str, str] = {}  # 내려받기 URL → 저장소 키(InProcess 대역용)

    def acquire(self, task_id: str):
        from pipeline.handoff.canonical import sha256_canonical
        from pipeline.handoff.envelope import CONTRACT_VERSION
        from pipeline.handoff.gpu import Lease as AiLease

        got = self.client.acquire(int(task_id))
        if got is None:
            raise NotRunnable(task_id)
        inp = got["inputs"]
        label = inp["label"]
        logo, record = inp["logo"]["logo"], inp["logo"]["logo_record"]
        req = {
            "contract_version": CONTRACT_VERSION, "execution_id": str(got["run_id"]), "attempt_id": str(got["attempt_id"]),
            "stage": "inpaint", "image_id": inp["image_id"], "section": inp["section"], "blocks": inp["blocks"],
            "label_result": label, "label_result_sha256": sha256_canonical(label),
            "logo_result": logo, "logo_result_sha256": sha256_canonical(logo),
            "logo_record": record, "logo_record_sha256": sha256_canonical(record),
            "limits": None,  # 운영 시간 제한 미정 — AI가 개발값 사용을 implementation.limits.source=dev_config 로 남긴다
        }
        si = inp["section_image"]
        self._keys[si["url"]] = si["key"]
        lease = AiLease(task_id=str(got["attempt_id"]), job_id=str(got["job_id"]), lease_epoch=int(got["epoch"]),
                        lease_token=got["token"], heartbeat_interval_s=max(1.0, got["ttl_s"] / 3), stage_request=req,
                        inputs={"section_image": {"sha256": si["sha256"], "download_url": si["url"]}})
        self._leases[lease.task_id] = lease
        return lease

    def heartbeat(self, lease) -> None:
        from pipeline.handoff.gpu import LeaseLost as AiLeaseLost

        if not self.client.heartbeat(int(lease.task_id), lease.lease_epoch, lease.lease_token):
            raise AiLeaseLost("실행 권한 상실")

    def upload_target(self, lease, kind: str, part_key: str):
        from pipeline.handoff.gpu import LeaseLost as AiLeaseLost
        from pipeline.handoff.gpu import UploadTarget

        try:
            u = self.client.upload_url(int(lease.task_id), lease.lease_epoch, lease.lease_token, kind, part_key)
        except execution.LeaseLost as e:
            raise AiLeaseLost(str(e)) from e
        return UploadTarget(url=u["url"], object_key=u["key"], headers={"Content-Type": "image/png"})

    def register_handoff(self, lease, envelope: dict[str, Any]):
        from pipeline.handoff.gpu import HandoffAck

        reg = self.client.register_report(int(lease.task_id), lease.lease_epoch, lease.lease_token, envelope)
        return HandoffAck(handoff_id=str(reg["handoff_id"]), status=reg["state"])

    def handoff_status(self, task_id: str, handoff_id: str) -> str:
        lease = self._leases[task_id]
        st = self.client.status(int(task_id), lease.lease_epoch, lease.lease_token)
        return st.get("state") or "received"

    # 내려받기·업로드 — InProcess 대역은 저장소를 직접, HTTP 바인딩은 presigned URL 을 쓴다
    def fetch(self, url: str, dest: Path) -> None:
        dest.write_bytes(self.client.fetch(url, self._keys.get(url, "")))

    def put(self, target, path: Path) -> None:
        self.client.put(target.url, target.object_key, path.read_bytes())


_RUNNERS: dict[tuple[int, str], Any] = {}


def _work_root() -> Path:
    return Path(os.getenv("PIXLATE_GPU_WORK_DIR", os.path.join(os.path.expanduser("~"), ".pixlate-gpu-work"))).resolve()


def _runner(ctl: BeControlClient):
    """GPU 워커 프로세스에서 실행기를 재사용한다(모델 자식 프로세스를 시도마다 다시 만들지 않는다)."""
    from pipeline.handoff.gpu import GpuInpaintRunner

    painter = ai_adapters.inpainter()
    key = (id(painter), str(_work_root()))
    r = _RUNNERS.get(key)
    if r is None:
        r = GpuInpaintRunner(ctl, painter.cfg, work_root=_work_root(), model_factory=getattr(painter, "model_factory", None),
                             fetch=ctl.fetch, put=ctl.put)
        _RUNNERS.clear()
        _RUNNERS[key] = r
    else:
        r.control, r.fetch, r.put = ctl, ctl.fetch, ctl.put
    return r


def _discard_if_gone(ctl: BeControlClient, out) -> bool:
    """BE 가 버려도 된다고 확인한 시도(전체 취소·중단·대체, 거절된 인계)의 로컬 자료를 지운다(D9-4·5.30). 지웠으면 True."""
    import shutil

    lease = ctl._leases.get(out.task_id)
    if lease is None:
        return False
    try:
        st = ctl.client.status(int(out.task_id), lease.lease_epoch, lease.lease_token)
    except Exception:  # noqa: BLE001 — 확인하지 못하면 보존
        return False
    if st.get("discard"):
        shutil.rmtree(out.workdir, ignore_errors=True)
        return True
    return False


def execute_inpaint_remote(attempt_id: int) -> dict[str, Any]:
    ctl = BeControlClient(_client())
    runner = _runner(ctl)
    try:
        out = runner.run_task(str(attempt_id))
    except NotRunnable:
        return {"attemptId": attempt_id, "ran": False}
    if out.status == "lease_lost":  # 업로드·인계하지 않고 로컬 파일 보존(복구자 인수 대상, 5.30) — 취소로 버릴 시도면 삭제
        return {"attemptId": attempt_id, "ran": True, "leaseLost": True, "localKept": not _discard_if_gone(ctl, out)}
    if out.status != "registered":  # upload_failed · register_failed — 계산 결과 보존, 저장 복구 대상
        log.warning("⑥ 인계 미완료 attempt=%s status=%s error=%s", attempt_id, out.status, out.error)
        return {"attemptId": attempt_id, "ran": True, "failed": True, "status": out.status, "localKept": True}
    cleaned = runner.cleanup(out) or _discard_if_gone(ctl, out)
    return {"attemptId": attempt_id, "ran": True, "handoff": {"handoff_id": int(out.handoff_id)}, "localKept": not cleaned}


def purge_job_local(job_id: int) -> bool:
    """전체 작업 취소(D9-4) 시 GPU 워커 로컬 자료 삭제 — AI 실행기의 purge_job."""
    return _runner(BeControlClient(_client())).purge_job(str(job_id))


# ---------------------------------------------------------------------------------------------------------
# BE 채택(cpu 큐)
# ---------------------------------------------------------------------------------------------------------
def adopt_remote_handoff(handoff_id: int) -> dict[str, Any]:
    adopter = f"be-adopter:{execution.worker_identity()}"
    lease = execution.claim_adoption(handoff_id, adopter)
    if lease is None:
        return {"handoffId": handoff_id, "adopted": False}
    try:
        db = app_db.SessionLocal()
        try:
            sec_key = db.execute(text("SELECT image_key FROM section WHERE id = :s"), {"s": lease.unit_id}).scalar()
        finally:
            db.close()
        allowed = {sec_key: lease.manifest.get("section_image_sha256")} if sec_key else {}
        for art in artifacts.artifacts_of(handoff_id):
            if art["state"] == "verified":
                continue
            if art["source_ref"] is not None:
                artifacts.fix_source_ref(lease, art["id"], allowed_keys=allowed)
            else:
                artifacts.fix_staged(lease, art["id"])
        execution.mark_verified(handoff_id, lease)
    except AdoptionRejected as e:
        execution.reject_and_fail(handoff_id, lease, e)
        return {"handoffId": handoff_id, "adopted": False, "rejected": e.code}
    out = execution.adopt(handoff_id, lease)
    return {"handoffId": handoff_id, "adopted": out is not None, "status": out.status if out else None}
