"""⑥ 원격 GPU 실행 — BE 제어 서비스(WorkerControl) · GPU 측 실행기 · BE 채택(integration-decisions.md 5.28~5.32).

경계(5.32 1안, 사용자 결정 2026-10-08: 서비스 계층만 구현)
- GPU 워커는 DB·AWS 자격증명을 쓰지 않는다. 실행 입력 조회·갱신·업로드 URL·인계 등록·상태 조회는 모두 WorkerControl 을 거친다.
  HTTP 경로·서비스 인증 형식은 api-tracking 명세 확정 후 바인딩한다. 지금은 InProcessControlClient(같은 프로세스 호출)만 있다 —
  학교 GPU 운영 배포에는 HTTP 바인딩이 먼저 필요하다.
- worker_id 는 인증된 서비스 주체다(요청 본문 주장값이 아니라 서비스 인증에서 정해진다).
- 시도 토큰은 권한 획득 응답으로만 받는다. 로그·큐·인계 본문에 넣지 않는다.
- 파일은 BE 가 발급한 정확한 스테이징 키 하나에 업로드하고(presigned PUT), BE 가 내려받아 측정한 같은 바이트만 검증 키에 고정한다.
- GPU 로컬 입력·출력은 인계가 '검증됨' 또는 '채택'으로 확인된 뒤에만 지운다(원격 복구 가능 확인, 5.30). 수신 확인만으로 지우지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import text

from app import ai_adapters, artifacts, execution
from app import db as app_db
from app.execution import AdoptionRejected, ArtifactSpec, Envelope, Lease
from app.flows import common
from app.flows.common import block_key, json_value, section_key
from app.manifest import sha256_bytes, stable_hash
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
        return {"attempt_id": attempt_id, "epoch": lease.epoch, "token": lease.token, "ttl_s": execution.lease_ttl_s(),
                "fingerprint": lease.fingerprint, "inputs": self._inputs(lease)}

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
# GPU 측 실행기
# ---------------------------------------------------------------------------------------------------------
class _RemoteLeaseHeartbeat(common.Heartbeat):
    def __init__(self, client: ControlClient, attempt_id: int, epoch: int, token: str, ttl_s: int):
        self.client, self.attempt_id, self.epoch, self.token = client, attempt_id, epoch, token
        self.lost = False
        import threading

        self.interval = max(1.0, ttl_s / 3)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                if not self.client.heartbeat(self.attempt_id, self.epoch, self.token):
                    self.lost = True
                    return
            except Exception:  # noqa: BLE001
                self.lost = True
                return


def _work_dir(attempt_id: int, epoch: int) -> Path:
    base = Path(os.getenv("PIXLATE_GPU_WORK_DIR", os.path.join(os.path.expanduser("~"), ".pixlate-gpu-work"))).resolve()
    d = base / str(attempt_id) / str(epoch)
    d.mkdir(parents=True, exist_ok=True)
    return d


def execute_inpaint_remote(attempt_id: int) -> dict[str, Any]:
    from pipeline.types import LabelResult, LogoResult, MergeResult, Section, TextBlock

    client = _client()
    got = client.acquire(attempt_id)
    if got is None:
        return {"attemptId": attempt_id, "ran": False}
    epoch, token, inp = got["epoch"], got["token"], got["inputs"]
    work = _work_dir(attempt_id, epoch)
    base = {"input_fingerprint": got["fingerprint"], "contract_version": execution.CONTRACT_VERSION}
    if inp["targets"] == 0:  # 처리 대상 블록 없음 — 호출 없이 정상 생략(D1)
        env = {**base, "outcome": "skipped", "skip_reason": "no_targets", "target_count": 0, "payload": {"status": "no_targets"},
               "artifacts": []}
        return _finish(client, attempt_id, epoch, token, env, work)
    data = client.fetch(inp["section_image"]["url"], inp["section_image"]["key"])
    if sha256_bytes(data) != inp["section_image"]["sha256"]:
        env = {**base, "outcome": "failed", "target_count": inp["targets"],
               "payload": {"error_code": "INPUT_CHANGED", "message": "섹션 이미지 해시 불일치", "retryable": False}, "artifacts": []}
        return _finish(client, attempt_id, epoch, token, env, work)
    img = work / "section.png"
    img.write_bytes(data)
    sec_d = dict(inp["section"])
    sec_d["image_path"] = str(img)
    section = Section.model_validate(sec_d)
    merged = MergeResult(section_key=section.section_key, blocks=[TextBlock.model_validate(b) for b in inp["blocks"]])
    label = LabelResult.model_validate(inp["label"])
    logo, record = LogoResult.model_validate(inp["logo"]["logo"]), inp["logo"]["logo_record"]
    painter = ai_adapters.inpainter()
    with _RemoteLeaseHeartbeat(client, attempt_id, epoch, token, got["ttl_s"]) as hb:
        try:
            out = painter.inpaint(inp["image_id"], section, merged, label, logo, record)
        except Exception as e:  # noqa: BLE001 — 입력 오류·모델 사용 불가: 실패 봉투(재시도 버튼 없음, D9-3)
            out = ai_adapters.InpaintOut("failed", None, None, None, None, None, None, None, error=f"{e.__class__.__name__}: {e}")
    if hb.lost:  # 권한 상실 — 새 업로드·직접 완료 중지. 로컬 파일은 지우지 않는다(5.30)
        return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
    payload = {"status": out.status, "counts": out.counts, "regions": out.regions, "protected_blocks": out.protected_blocks,
               "model": out.model}
    if out.status == "failed":
        env = {**base, "outcome": "failed", "target_count": inp["targets"],
               "payload": {**payload, "error_code": "INPAINT_FAILED", "message": out.error or "", "retryable": False},
               "artifacts": []}
        return _finish(client, attempt_id, epoch, token, env, work)
    arts = []
    files = {"delete_mask": out.final_mask_png, "protect_mask": out.protect_mask_png}
    if out.status == "inpainted":
        files["background"] = out.background_png
    for kind, blob in files.items():
        (work / f"{kind}.png").write_bytes(blob)  # 원격 복구 가능 확인 전까지 보존
        u = client.upload_url(attempt_id, epoch, token, kind, section.section_key)
        client.put(u["url"], u["key"], blob)
        arts.append({"kind": kind, "part_key": section.section_key, "staging_key": u["key"], "declared_sha256": sha256_bytes(blob)})
    if out.status == "unchanged":  # 마스크 0 — 원본 배경을 검증된 기존 참조로
        arts.append({"kind": "background", "part_key": section.section_key,
                     "source_ref": {"key": inp["section_image"]["key"], "sha256": inp["section_image"]["sha256"]}})
    env = {**base, "outcome": "done", "target_count": inp["targets"], "impl_version": painter.impl_version, "payload": payload,
           "artifacts": arts}
    return _finish(client, attempt_id, epoch, token, env, work)


def _finish(client: ControlClient, attempt_id: int, epoch: int, token: str, env: dict[str, Any], work: Path) -> dict[str, Any]:
    reg = client.register(attempt_id, epoch, token, env)
    st = client.status(attempt_id, epoch, token)
    if st.get("remote_recoverable") or st.get("discard"):
        shutil.rmtree(work, ignore_errors=True)
    return {"attemptId": attempt_id, "ran": True, "handoff": reg, "localKept": not (st.get("remote_recoverable") or st.get("discard"))}


def cleanup_confirmed(client: ControlClient, attempt_id: int, epoch: int, token: str) -> bool:
    """재통지·상태 조회로 원격 복구 가능이 확인되면 로컬 자료를 지운다."""
    st = client.status(attempt_id, epoch, token)
    if st.get("remote_recoverable") or st.get("discard"):
        shutil.rmtree(_work_dir(attempt_id, epoch), ignore_errors=True)
        return True
    return False


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
