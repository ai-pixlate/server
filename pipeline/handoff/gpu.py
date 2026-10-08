"""GPU 워커 실행기 — BE 제어 인터페이스 소비 · 실행 권한 갱신 · 산출물 업로드 · 인계 등록 · 로컬 정리 [통합 5.28~5.30 · 5.32 · D9-4].

AI는 BE의 내부 경로 · 인증 형식을 정하지 않는다. `ControlClient`는 5.32가 GPU에 허용한 동작(권한 획득 · 실행 입력 조회 · 갱신 ·
업로드 URL 요청 · 인계 등록 · 상태 조회)만 표현한 Protocol이며, 실제 구현(HTTPS 내부 API 등)은 BE가 제공한다. 테스트는 대역을 쓴다.

흐름(한 시도):
1. acquire → 권한(세대 · 토큰)과 고정 실행 입력. 토큰은 인계 본문 · 로그 · 진단에 넣지 않는다(인증은 ControlClient 몫).
2. 입력 파일을 시도 전용 작업 폴더에 내려받아 SHA-256 대조(불일치 = input_invalid 보고, 계산하지 않음).
3. 갱신 루프를 계산과 **독립 스레드**로 돌린다(간격은 BE가 권한에 실어 준다). 갱신 실패 = 권한 상실 → 취소 신호 + 진행 중 추론 중단 요청.
4. 단계 어댑터 실행(현재 ⑥). 권한을 잃었으면 업로드 · 인계 등록을 하지 않고 로컬 파일을 보존한다(복구는 BE 복구자가 인수, 5.30).
5. 산출물마다 업로드 직전에 URL을 받아 올린다. 실패하면 파일을 보존하고 upload_failed — 계산을 다시 하지 않는다.
6. 인계 등록 = 수신 확인 요청. 수신 확인만으로 로컬을 지우지 않는다. `cleanup()`은 BE가 verified(원격 복구 가능) · adopted를 확인한 뒤만 지운다.
전체 작업 취소는 `purge_job()` — 채택 여부와 무관하게 그 작업의 로컬 입력 · 산출물 · 진단을 지운다(D9-4).
"""
from __future__ import annotations

import shutil
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from pipeline.handoff.canonical import sha256_file
from pipeline.handoff.downstream import run_inpaint
from pipeline.handoff.envelope import StageReport, failed_report, identity_fallback

GPU_RUNNER_VERSION = "gpu-runner@2026-10-08.1"
DELETABLE_STATUSES = ("verified", "adopted")  # BE 확인: 원격 복구 가능 · DB 채택(5.30). received만으로는 지우지 않는다


class LeaseLost(Exception):
    """실행 권한이 없다(만료 · 회수 · 취소). 새 업로드 · 직접 완료를 멈춘다."""


@dataclass(frozen=True)
class Lease:
    task_id: str
    job_id: str
    lease_epoch: int
    lease_token: str = field(repr=False)  # 원문은 ControlClient 안에서만 쓴다
    heartbeat_interval_s: float
    stage_request: dict[str, Any]  # 단계 어댑터 요청(입력 파일 경로는 실행기가 채운다)
    inputs: dict[str, dict[str, Any]]  # 이름 → {"sha256", "download_url"}(BE가 발급한 내려받기 참조)


@dataclass(frozen=True)
class UploadTarget:
    url: str
    object_key: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HandoffAck:
    handoff_id: str
    status: str


class ControlClient(Protocol):
    def acquire(self, task_id: str) -> Lease: ...
    def heartbeat(self, lease: Lease) -> None: ...  # 권한 없음이면 LeaseLost
    def upload_target(self, lease: Lease, kind: str, part_key: str) -> UploadTarget: ...  # 권한 없음이면 LeaseLost
    def register_handoff(self, lease: Lease, envelope: dict[str, Any]) -> HandoffAck: ...
    def handoff_status(self, task_id: str, handoff_id: str) -> str: ...  # received · verifying · verified · adopted · rejected


def http_get(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:  # noqa: S310 — BE가 발급한 내려받기 URL
        shutil.copyfileobj(r, f)


def http_put(target: UploadTarget, path: Path) -> None:
    data = path.read_bytes()
    req = urllib.request.Request(target.url, data=data, method="PUT", headers=target.headers)
    with urllib.request.urlopen(req) as r:  # noqa: S310 — BE가 발급한 업로드 URL
        if r.status not in (200, 201, 204):
            raise OSError(f"업로드 응답 {r.status}")


@dataclass
class RunOutcome:
    status: str  # registered · lease_lost · upload_failed · register_failed
    task_id: str
    report: StageReport | None
    workdir: Path
    handoff_id: str | None = None
    uploads: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _safe(component: str) -> str:
    if not component or any(c in component for c in "/\\:\0") or component in (".", ".."):
        raise ValueError(f"경로 구성요소로 쓸 수 없는 식별자 {component!r}")
    return component


class _Heartbeat:
    def __init__(self, control: ControlClient, lease: Lease, on_lost: Callable[[], None]):
        self.control, self.lease, self.on_lost = control, lease, on_lost
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.error: str | None = None
        self.beats = 0
        self.thread = threading.Thread(target=self._loop, name="pixlate-gpu-heartbeat", daemon=True)

    def _loop(self) -> None:
        while not self.stop.wait(self.lease.heartbeat_interval_s):
            try:
                self.control.heartbeat(self.lease)
                self.beats += 1
            except Exception as e:  # noqa: BLE001 — 갱신 실패는 권한 상실로 본다(새 업로드 · 완료 중지)
                self.error = f"{e.__class__.__name__}: {e}"
                self.lost.set()
                self.on_lost()
                return

    def __enter__(self) -> "_Heartbeat":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.thread.join(self.lease.heartbeat_interval_s + 5)


class GpuInpaintRunner:
    """⑥ 인페인트 시도 실행기. 모델(자식 프로세스)은 시도 사이에 재사용하고 권한 상실 시 중단 · 폐기한다."""

    def __init__(self, control: ControlClient, cfg: dict[str, Any], *, work_root: Path,
                 model_factory: Callable[[dict[str, Any]], Any] | None = None,
                 fetch: Callable[[str, Path], None] = http_get, put: Callable[[UploadTarget, Path], None] = http_put):
        self.control, self.cfg, self.work_root = control, cfg, Path(work_root)
        self.model_factory = model_factory
        self.fetch, self.put = fetch, put
        self._model: Any | None = None
        self._lock = threading.Lock()

    # -- 모델 수명 --------------------------------------------------------
    def _shared_model(self, cfg: dict[str, Any]) -> Any:
        with self._lock:
            if self._model is None:
                from pipeline.stages import inpaint as inpaint_stage

                self._model = (self.model_factory or inpaint_stage.build_model)(cfg)
            return self._model

    def _abort_model(self, reason: str) -> None:
        with self._lock:
            m, self._model = self._model, None
        if m is not None:
            for name in ("abort", "close"):
                fn = getattr(m, name, None)
                if fn is not None:
                    try:
                        fn(reason) if name == "abort" else fn()
                    except Exception:  # noqa: BLE001
                        pass
                    break

    def close(self) -> None:
        with self._lock:
            m, self._model = self._model, None
        if m is not None and hasattr(m, "close"):
            m.close()

    # -- 한 시도 ----------------------------------------------------------
    def workdir_of(self, lease: Lease) -> Path:
        return self.work_root / _safe(lease.job_id) / _safe(lease.task_id) / str(int(lease.lease_epoch))

    def run_task(self, task_id: str) -> RunOutcome:
        lease = self.control.acquire(task_id)
        wd = self.workdir_of(lease)
        inputs_dir, out_dir = wd / "inputs", wd / "out"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        req = dict(lease.stage_request)
        cancel = threading.Event()

        def on_lost() -> None:
            cancel.set()
            self._abort_model("실행 권한 상실")

        with _Heartbeat(self.control, lease, on_lost) as hb:
            try:
                local = self._prepare_inputs(lease, inputs_dir)
                req["section_image"] = {"path": str(local["section_image"]), "sha256": lease.inputs["section_image"]["sha256"]}
                req["out_dir"] = str(out_dir)
                report = run_inpaint(req, self.cfg, model_factory=self._shared_model, close_model=False, cancel=cancel)
            except _InputMismatch as e:
                report = failed_report(identity_fallback(req, "inpaint"), "input_invalid", str(e))
        if hb.lost.is_set():  # 권한 상실 — 업로드 · 인계하지 않고 파일 보존(복구자 인수 대상)
            return RunOutcome("lease_lost", lease.task_id, report, wd, error=hb.error)
        uploads: list[dict[str, Any]] = []
        try:
            for a in report.artifacts:
                if sha256_file(a.path) != a.sha256:
                    raise OSError(f"{a.kind}/{a.part_key}: 업로드 직전 파일 해시가 보고와 다르다")
                target = self.control.upload_target(lease, a.kind, a.part_key)
                self.put(target, Path(a.path))
                uploads.append({"kind": a.kind, "part_key": a.part_key, "object_key": target.object_key, "sha256": a.sha256, "bytes": a.bytes})
        except LeaseLost as e:
            return RunOutcome("lease_lost", lease.task_id, report, wd, uploads=uploads, error=str(e))
        except Exception as e:  # noqa: BLE001 — 계산 결과는 보존하고 업로드만 다시 시도할 수 있다
            return RunOutcome("upload_failed", lease.task_id, report, wd, uploads=uploads, error=f"{e.__class__.__name__}: {e}")
        envelope = {"runner": GPU_RUNNER_VERSION, "task_id": lease.task_id, "lease_epoch": lease.lease_epoch,
                    "report": report.model_dump(mode="json"), "uploads": uploads}
        try:
            ack = self.control.register_handoff(lease, envelope)
        except Exception as e:  # noqa: BLE001 — 통지 유실은 같은 본문 재통지로 복구(5.32 U4)
            return RunOutcome("register_failed", lease.task_id, report, wd, uploads=uploads, error=f"{e.__class__.__name__}: {e}")
        return RunOutcome("registered", lease.task_id, report, wd, handoff_id=ack.handoff_id, uploads=uploads)

    def _prepare_inputs(self, lease: Lease, inputs_dir: Path) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for name, ref in lease.inputs.items():
            dest = inputs_dir / _safe(name)
            self.fetch(ref["download_url"], dest)
            got = sha256_file(dest)
            if got != ref["sha256"]:
                raise _InputMismatch(f"입력 {name} SHA-256 불일치 — 고정 {ref['sha256'][:12]}… · 받음 {got[:12]}…")
            out[name] = dest
        if "section_image" not in out:
            raise _InputMismatch("실행 입력에 section_image가 없다")
        return out

    # -- 정리 ------------------------------------------------------------
    def cleanup(self, outcome: RunOutcome) -> bool:
        """BE가 verified · adopted를 확인한 인계만 로컬 자료를 지운다. 그 밖(received · rejected · 미등록)은 보존. 지웠으면 True."""
        if outcome.handoff_id is None or outcome.report is None:
            return False
        status = self.control.handoff_status(outcome.task_id, outcome.handoff_id)
        if status not in DELETABLE_STATUSES:
            return False
        shutil.rmtree(outcome.workdir, ignore_errors=False)
        return True

    def purge_job(self, job_id: str) -> bool:
        """전체 작업 취소(D9-4) — 그 작업의 로컬 입력 · 산출물 · 진단을 모두 지운다. 진행 중 모델은 중단한다."""
        self._abort_model("작업 전체 취소")
        root = self.work_root / _safe(job_id)
        if root.exists():
            shutil.rmtree(root)
            return True
        return False


class _InputMismatch(Exception):
    pass
