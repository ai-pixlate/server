"""BE 실행 모듈 — 대표 실행 · 시도 · 실행 권한(lease) · 발급 기록 · 인계 · 채택 · 재시도 · 중단.

근거: docs/ai/integration-decisions.md 5.28~5.32(BE·AI 합의 확정), D2. 저장 구조는 migrations 0008.

원칙
- 한 proceed/분석/최종 렌더 = 대표 행(parent_task_id IS NULL). 개별 단계 = 시도 행. 재시도는 새 시도 행(이전 행은 이력).
- 잠금 순서: job → 대표 실행 → 시도 → task_lease_grant → task_handoff → task_artifact (`SELECT … FOR UPDATE`).
  모델 호출·파일 다운로드·이미지 디코딩 동안에는 잠금을 잡지 않는다.
- 실행 권한(lease)은 시도 단위이며 DB 시계(now()) 기준으로 만료한다. 권한 획득·갱신·인계·채택은 세대(lease_epoch)와
  소유자를 조건으로 한다. 원격 워커는 시도 토큰을 받고 BE에는 해시만 남긴다(task_lease_grant).
- 브로커 메시지에는 시도 id만 담는다. 브로커는 깨우기 신호일 뿐 진실 원천이 아니다(복구는 app.recovery).
- 결과는 인계 등록(수신 확인) → 검증 → 채택(업무 반영) 순서다. 채택만 업무 컬럼을 바꾼다.
- 단계별 업무 반영·실패 후속은 등록된 StageHandler 가 같은 트랜잭션 안에서 처리한다(app.flows).

운영 수치(TTL·주기·횟수)는 미정이다. 아래 기본값은 로컬 개발용이며 환경변수로 바꾼다(운영 실측 후 확정).
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import db as app_db
from app.manifest import canonical_json, fingerprint, stable_hash, stable_json

log = logging.getLogger(__name__)

# ---- 내부 값(사용자 승인 2026-10-08, API 비노출) -------------------------------------------------------------
RUN_KINDS = ("analysis", "downstream", "final_render")
RUN_TASK_TYPE = {"analysis": "ocr", "downstream": "translate", "final_render": "render"}
# stage → (API TaskType, unit_type). ④⑤⑦ → section(사용자 결정 2026-10-08)
STAGE_TASK = {
    "analyze": ("ocr", "job"),
    "judge": ("section", "section"),
    "label": ("section", "section"),
    "logo": ("section", "section"),
    "style": ("section", "section"),
    "inpaint": ("inpaint", "section"),
    "translate": ("translate", "section"),
    "preview": ("render", "section"),
    "final_render": ("render", "job"),
}
STAGE_RUN_KIND = {
    "analyze": "analysis", "judge": "analysis",
    "label": "downstream", "logo": "downstream", "inpaint": "downstream", "style": "downstream",
    "translate": "downstream", "preview": "downstream",
    "final_render": "final_render",
}
CONTRACT_VERSION = "1"  # 인계 봉투 형식 버전(5.24 공통 인계 봉투의 BE 구현 1판)
ADOPT_PENDING_OWNER = "adopt-pending"  # 원격 인계 등록 직후 BE 채택자가 인수하기 전의 소유자 표시

TASK_BY_STAGE = {
    "analyze": "app.tasks.run_analyze",
    "judge": "app.tasks.run_judge",
    "label": "app.tasks.run_label",
    "logo": "app.tasks.run_logo",
    "inpaint": "app.tasks.run_inpaint",
    "style": "app.tasks.run_style",
    "translate": "app.tasks.run_translate",
    "preview": "app.tasks.run_preview",
    "final_render": "app.tasks.run_render",
}
ADOPT_TASK = "app.tasks.run_adopt"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def lease_ttl_s() -> int:
    """실행 권한 TTL(초). 개발 기본값 — 운영값은 단계 실측 후 정한다(5.32)."""
    return _env_int("PIXLATE_LEASE_TTL_S", 120)


def upload_url_ttl_s() -> int:
    return _env_int("PIXLATE_UPLOAD_URL_TTL_S", 300)


def worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# ---- 오류 -------------------------------------------------------------------------------------------------
class ExecutionError(Exception):
    code = "EXECUTION_ERROR"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotFound(ExecutionError):
    code = "NOT_FOUND"


class LeaseLost(ExecutionError):
    """현재 실행 권한이 아니다(취소·대체·만료·다른 소유자). 결과·갱신을 받지 않는다."""

    code = "LEASE_LOST"


class HandoffConflict(ExecutionError):
    """같은 시도·생성 세대에 본문이 다른 인계 — 기존 인계를 유지하고 거절한다."""

    code = "HANDOFF_CONFLICT"


class HandoffRejected(ExecutionError):
    code = "HANDOFF_REJECTED"


class AuthRejected(ExecutionError):
    """서비스 인증 · 발급 기록 대조 실패. 업무 인계로 받지 않는다(토큰 원문은 기록하지 않는다)."""

    code = "AUTH_REJECTED"


class AdoptionRejected(ExecutionError):
    """채택 검증 실패(구조·참조·입력 불일치). 인계는 rejected, 현재 시도면 failed 로 기록한다."""

    code = "ADOPTION_REJECTED"

    def __init__(self, message: str, code: str = "ADOPTION_REJECTED", retryable: bool = False):
        super().__init__(message, code)
        self.retryable = retryable


class RetryNotAllowed(ExecutionError):
    code = "RETRY_NOT_ALLOWED"


# ---- 모델 -------------------------------------------------------------------------------------------------
@dataclass
class Lease:
    attempt_id: int
    job_id: int
    run_id: int
    stage: str
    unit_type: str
    unit_id: int
    epoch: int
    owner: str
    manifest: dict[str, Any]
    fingerprint: str
    token: str | None = None  # 원격 워커에만. 로그·큐·인계 본문에 넣지 않는다


@dataclass
class ArtifactSpec:
    """인계 산출물 한 건. 신뢰된 EC2 워커는 data(바이트)를, 원격 워커는 staging_key(+declared_sha256)를, 기존 파일 참조는 source_ref."""

    kind: str
    part_key: str
    data: bytes | None = None
    staging_key: str | None = None
    declared_sha256: str | None = None
    source_ref: dict[str, Any] | None = None

    def body(self) -> dict[str, Any]:
        """인계 본문 해시에 들어가는 부분(바이트는 해시로)."""
        return {
            "kind": self.kind,
            "part_key": self.part_key,
            "data_sha256": hashlib.sha256(self.data).hexdigest() if self.data is not None else None,
            "staging_key": self.staging_key,
            "declared_sha256": self.declared_sha256,
            "source_ref": self.source_ref,
        }


@dataclass
class Envelope:
    """5.24 공통 인계 봉투의 BE 구현. outcome: done / skipped / failed."""

    outcome: str
    target_count: int
    payload: dict[str, Any]
    input_fingerprint: str
    impl_version: str | None = None
    skip_reason: str | None = None
    artifacts: list[ArtifactSpec] = field(default_factory=list)
    contract_version: str = CONTRACT_VERSION

    def body(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "impl_version": self.impl_version,
            "input_fingerprint": self.input_fingerprint,
            "outcome": self.outcome,
            "target_count": self.target_count,
            "skip_reason": self.skip_reason,
            "payload": self.payload,
            "artifacts": [a.body() for a in sorted(self.artifacts, key=lambda a: (a.kind, a.part_key))],
        }

    def manifest_sha256(self) -> str:
        return stable_hash(self.body())


@dataclass
class AdoptContext:
    job: dict[str, Any]
    run: dict[str, Any]
    attempt: dict[str, Any]
    handoff: dict[str, Any]
    artifacts: list[dict[str, Any]]
    adopter: str
    epoch: int


@dataclass
class AdoptOutcome:
    """채택 결과의 시도 집계. status: done / failed. dispatch: 커밋 후 큐에 보낼 새 시도 id."""

    status: str
    skip_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    verify_result: dict[str, Any] | None = None
    retryable: bool = False
    dispatch: list[int] = field(default_factory=list)


class StageHandler(Protocol):
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome: ...

    def after_failure(self, db: Session, attempt: dict[str, Any], error_code: str, retryable: bool) -> list[int]:
        """시도가 failed 로 바뀐 같은 트랜잭션에서 호출. 자동 재시도·실행 실패·대체 진행을 처리하고 보낼 시도 id를 돌려준다."""
        ...


_handlers: dict[str, StageHandler] = {}


def register_handler(stage: str, handler: StageHandler) -> None:
    _handlers[stage] = handler


def handler_for(stage: str) -> StageHandler:
    if not _handlers:
        import app.flows  # noqa: F401 — 단계 핸들러 등록
    try:
        return _handlers[stage]
    except KeyError as e:
        raise ExecutionError(f"등록된 단계 핸들러 없음: {stage}") from e


# ---- 전달(dispatch) -------------------------------------------------------------------------------------
Dispatcher = Callable[[str, list[Any]], None]


def _celery_send(task_name: str, args: list[Any]) -> None:
    from app.celery_app import celery_app

    celery_app.send_task(task_name, args=args)


_dispatcher: Dispatcher = _celery_send


def set_dispatcher(fn: Dispatcher | None) -> None:
    global _dispatcher
    _dispatcher = fn or _celery_send


def dispatch(attempt_ids: Iterable[int]) -> list[int]:
    """커밋된 대기 시도를 큐에 보낸다. 보낸 뒤 dispatch_count·last_dispatched_at 을 기록한다.
    전송 실패는 시도를 pending 으로 둔다(복구 프로세스가 같은 시도 id로 다시 보낸다). 보낸 id 목록을 돌려준다."""
    sent: list[int] = []
    ids = [int(i) for i in attempt_ids]
    if not ids:
        return sent
    db = app_db.SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id, stage, status FROM job_async_task WHERE id = ANY(:ids) ORDER BY id"), {"ids": ids}
        ).mappings().all()
        for r in rows:
            if r["status"] != "pending" or r["stage"] not in TASK_BY_STAGE:
                continue
            try:
                _dispatcher(TASK_BY_STAGE[r["stage"]], [r["id"]])
            except Exception:  # noqa: BLE001 — 브로커 장애: 행은 pending 으로 남고 복구 대상
                log.exception("dispatch 실패 attempt=%s", r["id"])
                continue
            db.execute(
                text("UPDATE job_async_task SET dispatch_count = dispatch_count + 1, last_dispatched_at = now() "
                     "WHERE id = :t AND status = 'pending'"),
                {"t": r["id"]},
            )
            db.commit()
            sent.append(r["id"])
    finally:
        db.close()
    return sent


def dispatch_adoption(handoff_id: int) -> bool:
    try:
        _dispatcher(ADOPT_TASK, [int(handoff_id)])
        return True
    except Exception:  # noqa: BLE001 — 복구 프로세스가 '수신·미채택' 인계를 다시 시작한다
        log.exception("adoption dispatch 실패 handoff=%s", handoff_id)
        return False


# ---- 잠금 -------------------------------------------------------------------------------------------------
def _one(db: Session, sql: str, **params) -> dict[str, Any] | None:
    r = db.execute(text(sql), params).mappings().first()
    return dict(r) if r is not None else None


def lock_job(db: Session, job_id: int) -> dict[str, Any]:
    job = _one(db, "SELECT * FROM job WHERE id = :j FOR UPDATE", j=job_id)
    if job is None:
        raise NotFound(f"job {job_id} 없음")
    return job


def lock_task(db: Session, task_id: int) -> dict[str, Any]:
    row = _one(db, "SELECT * FROM job_async_task WHERE id = :t FOR UPDATE", t=task_id)
    if row is None:
        raise NotFound(f"task {task_id} 없음")
    return row


def lock_chain(db: Session, attempt_id: int) -> tuple[dict, dict, dict]:
    """job → 대표 → 시도 순으로 잠그고 잠근 뒤의 값을 돌려준다."""
    head = _one(db, "SELECT job_id, parent_task_id FROM job_async_task WHERE id = :t", t=attempt_id)
    if head is None:
        raise NotFound(f"attempt {attempt_id} 없음")
    if head["parent_task_id"] is None:
        raise ExecutionError(f"{attempt_id}는 대표 실행이다(시도가 아님)")
    job = lock_job(db, head["job_id"])
    run = lock_task(db, head["parent_task_id"])
    attempt = lock_task(db, attempt_id)
    return job, run, attempt


# ---- 생성 -------------------------------------------------------------------------------------------------
def create_run(db: Session, job_id: int, run_kind: str, run_scope: dict[str, Any]) -> int:
    """대표 실행 생성. 호출자가 job 을 잠근 상태여야 한다(U1 과 이중 방어)."""
    if run_kind not in RUN_KINDS:
        raise ExecutionError(f"run_kind {run_kind}")
    canonical_json(run_scope)  # 직렬화 가능한 고정 범위인지 확인
    return db.execute(
        text(
            "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, started_at, run_kind, run_scope, max_retry) "
            "VALUES (:j, :tt, 'job', :j, 'running', now(), :k, CAST(:scope AS jsonb), 0) RETURNING id"
        ),
        {"j": job_id, "tt": RUN_TASK_TYPE[run_kind], "k": run_kind, "scope": canonical_json(run_scope)},
    ).scalar_one()


def create_attempt(
    db: Session,
    *,
    run: dict[str, Any],
    stage: str,
    unit_id: int,
    manifest: dict[str, Any],
    target_count: int,
    supersedes: dict[str, Any] | None = None,
    retry_origin: str | None = None,
    max_retry: int = 2,
    revision: int | None = None,
    count_retry: bool = True,
) -> int:
    """시도 생성(pending). 재시도면 supersedes(직전 시도, 이미 잠긴 행)를 주고 그 행을 현재에서 내린다.
    retry_count 는 시도 간 누적(직전 + 1), max_retry 는 직전 값을 승계한다(D2).
    count_retry=False 는 실패 재시도가 아닌 입력 변경 재계산(N5 수정 후 재렌더)이다 — 누적 한도를 쓰지 않는다."""
    if STAGE_RUN_KIND.get(stage) != run["run_kind"]:
        raise ExecutionError(f"stage {stage}는 실행 {run['run_kind']}에 속하지 않는다")
    task_type, unit_type = STAGE_TASK[stage]
    if unit_type == "job":
        unit_id = run["job_id"]
    attempt_no, retry_count = 1, 0
    if supersedes is not None:
        if (supersedes["parent_task_id"], supersedes["stage"], supersedes["unit_id"]) != (run["id"], stage, unit_id):
            raise ExecutionError("supersedes 는 같은 논리 작업의 시도여야 한다")
        attempt_no = supersedes["attempt_no"] + 1
        retry_count = supersedes["retry_count"] + (1 if count_retry else 0)
        max_retry = supersedes["max_retry"]
        db.execute(text("UPDATE job_async_task SET is_current = false WHERE id = :t"), {"t": supersedes["id"]})
    fp = fingerprint(manifest)
    return db.execute(
        text(
            "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, parent_task_id, stage, attempt_no, "
            "is_current, supersedes_task_id, retry_origin, retry_count, max_retry, input_manifest, input_fingerprint, "
            "target_count, revision) "
            "VALUES (:j, :tt, :ut, :u, 'pending', :p, :stage, :n, true, :sup, :origin, :rc, :mr, CAST(:m AS jsonb), :fp, :tc, :rev) "
            "RETURNING id"
        ),
        {"j": run["job_id"], "tt": task_type, "ut": unit_type, "u": unit_id, "p": run["id"], "stage": stage, "n": attempt_no,
         "sup": supersedes["id"] if supersedes else None, "origin": retry_origin, "rc": retry_count, "mr": max_retry,
         "m": canonical_json(manifest), "fp": fp, "tc": target_count, "rev": revision},
    ).scalar_one()


def create_retry(db: Session, attempt_id: int, origin: str, *, manifest: dict[str, Any] | None = None,
                 target_count: int | None = None) -> int:
    """실패한 현재 시도의 재시도 시도를 만든다(잠금 순서 아래). 호출자가 커밋·dispatch 한다.
    - 현재 failed · 대표 미취소·실행 중 · 누적 한도 미만만 허용. 동시 요청은 잠금 재확인과 U2 로 하나만 생긴다.
    - manifest 를 주면 새 고정 입력(⑧ 실패 대상 재시도 등). 없으면 직전 시도와 같은 입력."""
    job, run, att = lock_chain(db, attempt_id)
    if job["status"] == "archived":
        raise RetryNotAllowed("작업이 취소됐다")
    if run["status"] != "running" or run["cancelled_at"] is not None:
        raise RetryNotAllowed("실행이 진행 중이 아니다(중단·종료)")
    if att["status"] != "failed" or not att["is_current"]:
        raise RetryNotAllowed("현재 실패 시도만 재시도한다")
    if att["retry_count"] >= att["max_retry"]:
        raise RetryNotAllowed("재시도 한도 초과", code="RETRY_LIMIT_EXCEEDED")
    return create_attempt(
        db, run=run, stage=att["stage"], unit_id=att["unit_id"],
        manifest=manifest if manifest is not None else att["input_manifest"],
        target_count=target_count if target_count is not None else att["target_count"],
        supersedes=att, retry_origin=origin, revision=att["revision"],
    )


# ---- 실행 권한 ---------------------------------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def acquire(attempt_id: int, owner: str, *, remote_worker_id: str | None = None) -> Lease | None:
    """대기 시도의 실행 권한을 얻는다. 0행(이미 실행 중·취소·대체·완료)이면 None — 실행하지 않는다.
    원격 워커면 같은 트랜잭션에서 토큰 해시와 발급 기록을 남기고, 커밋 후에만 토큰을 돌려준다."""
    db = app_db.SessionLocal()
    try:
        job, run, att = lock_chain(db, attempt_id)
        if (att["status"] != "pending" or not att["is_current"] or run["status"] != "running"
                or run["cancelled_at"] is not None or job["status"] == "archived"):
            db.rollback()
            return None
        epoch = att["lease_epoch"] + 1
        token = secrets.token_urlsafe(32) if remote_worker_id else None
        token_hash = _hash_token(token) if token else None
        row = db.execute(
            text(
                "UPDATE job_async_task SET status = 'running', started_at = coalesce(started_at, now()), finished_at = NULL, "
                "lease_owner = :o, lease_epoch = :e, lease_token_hash = :h, "
                "lease_expires_at = now() + make_interval(secs => :ttl), heartbeat_at = now() "
                "WHERE id = :t AND status = 'pending' AND lease_epoch = :prev RETURNING id"
            ),
            {"o": remote_worker_id or owner, "e": epoch, "h": token_hash, "ttl": lease_ttl_s(), "t": attempt_id,
             "prev": att["lease_epoch"]},
        ).first()
        if row is None:
            db.rollback()
            return None
        if remote_worker_id:
            db.execute(
                text(
                    "INSERT INTO task_lease_grant (task_id, job_id, generation_epoch, worker_id, token_hash, initial_expires_at) "
                    "VALUES (:t, :j, :e, :w, :h, now() + make_interval(secs => :ttl))"
                ),
                {"t": attempt_id, "j": att["job_id"], "e": epoch, "w": remote_worker_id, "h": token_hash, "ttl": lease_ttl_s()},
            )
        db.commit()
        return Lease(attempt_id=attempt_id, job_id=att["job_id"], run_id=run["id"], stage=att["stage"],
                     unit_type=att["unit_type"], unit_id=att["unit_id"], epoch=epoch, owner=remote_worker_id or owner,
                     manifest=att["input_manifest"], fingerprint=att["input_fingerprint"], token=token)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _current_lease_sql(remote: bool) -> str:
    cond = (
        "a.id = :t AND a.status = 'running' AND a.is_current AND a.lease_epoch = :e AND a.lease_owner = :o "
        "AND a.lease_expires_at > now() "
        "AND EXISTS (SELECT 1 FROM job_async_task r JOIN job jb ON jb.id = r.job_id "
        "WHERE r.id = a.parent_task_id AND r.status = 'running' AND r.cancelled_at IS NULL AND jb.status <> 'archived')"
    )
    if remote:
        cond += (
            " AND a.lease_token_hash = :h AND EXISTS (SELECT 1 FROM task_lease_grant g WHERE g.task_id = a.id "
            "AND g.generation_epoch = a.lease_epoch AND g.worker_id = :o AND g.security_revoked_at IS NULL)"
        )
    return cond


def heartbeat(lease: Lease) -> bool:
    """만료 전 권한 연장. 권한을 잃었으면 False — 호출자는 새 업로드·직접 완료를 멈추고 계산 중단을 요청한다."""
    db = app_db.SessionLocal()
    try:
        params = {"t": lease.attempt_id, "e": lease.epoch, "o": lease.owner, "ttl": lease_ttl_s()}
        if lease.token:
            params["h"] = _hash_token(lease.token)
        row = db.execute(
            text(
                "UPDATE job_async_task a SET lease_expires_at = now() + make_interval(secs => :ttl), heartbeat_at = now() "
                f"WHERE {_current_lease_sql(bool(lease.token))} RETURNING a.id"
            ),
            params,
        ).first()
        db.commit()
        return row is not None
    finally:
        db.close()


def check_lease(db: Session, lease: Lease) -> bool:
    params = {"t": lease.attempt_id, "e": lease.epoch, "o": lease.owner}
    if lease.token:
        params["h"] = _hash_token(lease.token)
    return db.execute(
        text(f"SELECT 1 FROM job_async_task a WHERE {_current_lease_sql(bool(lease.token))}"), params
    ).first() is not None


def fail_attempt(lease: Lease, error_code: str, error_message: str, *, retryable: bool) -> list[int]:
    """인계를 만들 수 없는 실패(예기치 않은 예외 등)를 현재 권한으로 기록한다. 권한이 없으면 아무것도 바꾸지 않는다.
    같은 트랜잭션에서 단계 핸들러의 실패 후속(자동 재시도·실행 실패)을 처리하고, 커밋 후 새 시도를 보낸다."""
    db = app_db.SessionLocal()
    dispatch_ids: list[int] = []
    try:
        job, run, att = lock_chain(db, lease.attempt_id)
        if not check_lease(db, lease):
            db.rollback()
            return []
        att = _mark_failed(db, att, error_code, error_message)
        dispatch_ids = handler_for(att["stage"]).after_failure(db, att, error_code, retryable)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    dispatch(dispatch_ids)
    return dispatch_ids


def _mark_failed(db: Session, att: dict[str, Any], error_code: str, error_message: str | None) -> dict[str, Any]:
    row = db.execute(
        text(
            "UPDATE job_async_task SET status = 'failed', finished_at = now(), error_code = :c, error_message = :m, "
            "lease_token_hash = NULL, lease_expires_at = NULL WHERE id = :t RETURNING *"
        ),
        {"c": error_code[:50], "m": (error_message or "")[:4000], "t": att["id"]},
    ).mappings().one()
    _close_open_grants(db, att["id"], "superseded")
    return dict(row)


def _close_open_grants(db: Session, attempt_id: int, reason: str) -> None:
    db.execute(
        text("UPDATE task_lease_grant SET closed_at = now(), close_reason = :r WHERE task_id = :t AND closed_at IS NULL"),
        {"r": reason, "t": attempt_id},
    )


# ---- 인계 등록 -------------------------------------------------------------------------------------------
def _verify_past_grant(db: Session, attempt_id: int, epoch: int, worker_id: str, token: str | None) -> dict[str, Any]:
    """이전/현재 생성 세대의 발급 사실·생성자 신원 확인(5.32 이전 생성 세대의 인증). 실패면 AuthRejected."""
    if not token:
        raise AuthRejected("시도 토큰 없음")
    g = _one(db, "SELECT * FROM task_lease_grant WHERE task_id = :t AND generation_epoch = :e FOR UPDATE", t=attempt_id, e=epoch)
    if g is None:
        raise AuthRejected("발급 기록 없음")
    if g["worker_id"] != worker_id:
        raise AuthRejected("발급 주체 불일치")
    if g["security_revoked_at"] is not None:
        raise AuthRejected("보안상 폐기된 발급")
    if g["token_hash"] is None:
        raise AuthRejected("검증 정보가 정리된 발급")
    if not secrets.compare_digest(g["token_hash"], _hash_token(token)):
        raise AuthRejected("토큰 불일치")
    return g


def register_handoff(lease: Lease, envelope: Envelope, *, remote_worker_id: str | None = None) -> dict[str, Any]:
    """인계 등록 = 수신 확인(행 커밋). 같은 시도·세대의 같은 본문은 멱등(기존 행), 다른 본문은 HandoffConflict.

    신뢰된 EC2 워커(remote_worker_id 없음): 현재 권한이 있어야 한다. 이후 같은 워커가 그대로 채택한다.
    원격 워커: 서비스 주체(remote_worker_id)와 발급 기록·토큰을 대조한다. 현재 권한이면 생성 grant 를 정상 종료하고
    토큰 해시를 지운 뒤 세대를 올려 BE 채택자 인수 대기로 둔다. 권한이 만료·회수된 세대면 late=true 이력으로만 받는다.
    전체 취소된 작업에는 콘텐츠를 남기지 않도록 아무것도 기록하지 않는다(D9-4)."""
    if envelope.outcome not in ("done", "skipped", "failed"):
        raise HandoffRejected(f"outcome {envelope.outcome}")
    if (envelope.outcome == "skipped") != (envelope.skip_reason is not None):
        raise HandoffRejected("정상 생략은 skip_reason 이 있어야 하고, 그 밖에는 없어야 한다")
    if remote_worker_id and any(a.data is not None for a in envelope.artifacts):
        raise HandoffRejected("원격 인계는 바이트를 직접 싣지 않는다(staging_key 로 업로드)")
    body_sha = envelope.manifest_sha256()
    db = app_db.SessionLocal()
    try:
        job, run, att = lock_chain(db, lease.attempt_id)
        if job["status"] == "archived":
            db.rollback()
            raise HandoffRejected("전체 취소된 작업 — 인계를 기록하지 않는다", code="JOB_ARCHIVED")
        grant = None
        if remote_worker_id:
            grant = _verify_past_grant(db, lease.attempt_id, lease.epoch, remote_worker_id, lease.token)
        existing = _one(db, "SELECT * FROM task_handoff WHERE task_id = :t AND lease_epoch = :e FOR UPDATE",
                        t=lease.attempt_id, e=lease.epoch)
        if existing is not None:
            db.rollback()
            if existing["manifest_sha256"] == body_sha:
                return existing  # 중복 재통지 — 새 인수·후속 생성·결과 쓰기 없음
            raise HandoffConflict("같은 세대의 다른 본문 — 기존 인계 유지")
        current = check_lease(db, lease)
        if not remote_worker_id and not current:
            db.rollback()
            raise LeaseLost("현재 실행 권한이 아니다")
        reject_reason = None
        if not current:
            if not att["is_current"]:
                reject_reason = "superseded"
            elif att["status"] == "cancelled" or run["status"] != "running":
                reject_reason = "cancelled"
            elif att["status"] == "done":
                reject_reason = "already_done"
        if envelope.input_fingerprint != att["input_fingerprint"]:
            reject_reason = reject_reason or "input_fingerprint_mismatch"
        state = "rejected" if reject_reason else "received"
        h = db.execute(
            text(
                "INSERT INTO task_handoff (task_id, job_id, lease_epoch, producer_grant_id, late, contract_version, impl_version, "
                "input_fingerprint, outcome, target_count, skip_reason, payload, manifest_sha256, state, reject_reason) "
                "VALUES (:t, :j, :e, :g, :late, :cv, :iv, :fp, :o, :tc, :sr, CAST(:p AS jsonb), :ms, :st, :rr) RETURNING *"
            ),
            {"t": lease.attempt_id, "j": att["job_id"], "e": lease.epoch, "g": grant["id"] if grant else None,
             "late": not current, "cv": envelope.contract_version, "iv": envelope.impl_version, "fp": envelope.input_fingerprint,
             "o": envelope.outcome, "tc": envelope.target_count, "sr": envelope.skip_reason,
             "p": stable_json(envelope.payload), "ms": body_sha, "st": state, "rr": reject_reason},
        ).mappings().one()
        h = dict(h)
        for a in envelope.artifacts:
            db.execute(
                text(
                    "INSERT INTO task_artifact (handoff_id, job_id, kind, part_key, staging_key, declared_sha256, source_ref, state) "
                    "VALUES (:h, :j, :k, :pk, :sk, :ds, CAST(:sr AS jsonb), :st)"
                ),
                {"h": h["id"], "j": att["job_id"], "k": a.kind, "pk": a.part_key, "sk": a.staging_key, "ds": a.declared_sha256,
                 "sr": stable_json(a.source_ref) if a.source_ref is not None else None,
                 "st": "unadopted" if reject_reason else "registered"},
            )
        if remote_worker_id and current and not reject_reason:
            # 생성자는 더 이상 계산·갱신하지 않는다. 세대를 올려 BE 채택자 인수 대기로 둔다
            _close_open_grants(db, lease.attempt_id, "handed_off")
            db.execute(
                text(
                    "UPDATE job_async_task SET lease_epoch = lease_epoch + 1, lease_owner = :o, lease_token_hash = NULL, "
                    "lease_expires_at = now() WHERE id = :t"
                ),
                {"o": ADOPT_PENDING_OWNER, "t": lease.attempt_id},
            )
        if reject_reason == "input_fingerprint_mismatch" and current:
            # 현재 시도 자체의 입력 불일치: 권한 확인 아래 failed 와 오류 기록
            att = _mark_failed(db, att, "INPUT_MISMATCH", "인계 입력 지문이 시도의 고정 입력과 다르다")
            ids = handler_for(att["stage"]).after_failure(db, att, "INPUT_MISMATCH", False)
            db.commit()
            dispatch(ids)
            return h
        db.commit()
        if remote_worker_id and h["state"] == "received":
            dispatch_adoption(h["id"])  # 늦은 인계도 채택자가 인수 가능 여부를 판단한다(claim_adoption)
        return h
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---- 채택 -------------------------------------------------------------------------------------------------
def claim_adoption(handoff_id: int, adopter: str) -> Lease | None:
    """BE 채택자가 인계를 채택할 권한을 인수한다. 유효한 다른 채택자/계산자가 있으면 인수하지 않는다(None).
    현재 시도·미취소·running(또는 늦은 인계로 권한 만료 후 failed 이면서 대체 시도 없음)일 때만."""
    db = app_db.SessionLocal()
    try:
        h = _one(db, "SELECT * FROM task_handoff WHERE id = :h", h=handoff_id)
        if h is None:
            raise NotFound(f"handoff {handoff_id}")
        job, run, att = lock_chain(db, h["task_id"])
        h = _one(db, "SELECT * FROM task_handoff WHERE id = :h FOR UPDATE", h=handoff_id)
        if h["state"] not in ("received", "verifying", "verified"):
            db.rollback()
            return None
        if job["status"] == "archived" or run["status"] != "running" or run["cancelled_at"] is not None or not att["is_current"]:
            _reject_handoff(db, h, "cancelled" if att["is_current"] else "superseded")
            db.commit()
            return None
        if h["producer_grant_id"] is not None:
            g = _one(db, "SELECT security_revoked_at FROM task_lease_grant WHERE id = :g", g=h["producer_grant_id"])
            if g and g["security_revoked_at"] is not None:
                _reject_handoff(db, h, "grant_revoked")
                db.commit()
                return None
        live_owner = db.execute(
            text("SELECT lease_owner IS NOT NULL AND lease_owner <> :pending AND lease_expires_at > now() "
                 "FROM job_async_task WHERE id = :t"),
            {"pending": ADOPT_PENDING_OWNER, "t": att["id"]},
        ).scalar()
        if att["status"] == "running" and live_owner and att["lease_owner"] != adopter:
            db.rollback()
            return None  # 유효한 권한 소유자가 있다 — 시간만으로 빼앗지 않는다
        if att["status"] == "failed":
            # 권한 만료로 실패 처리됐으나 대체 시도가 없는 현재 시도의 늦은 인계(5.32 복구표) — 저장 복구로 되살린다.
            # 단, 화면이 이미 다음 단계로 넘어갔으면(N5 예외 진입 등) 이전 시도의 채택 권한은 끝났다(D9-3)
            if not h["late"] or job["status"] != "processing" or job["current_step"] not in ("N2", "N4"):
                _reject_handoff(db, h, "late_after_transition") if h["late"] else None
                db.commit()
                return None
        elif att["status"] != "running":
            _reject_handoff(db, h, "already_" + att["status"])
            db.commit()
            return None
        if h["input_fingerprint"] != att["input_fingerprint"]:
            _reject_handoff(db, h, "input_fingerprint_mismatch")
            db.commit()
            return None
        epoch = att["lease_epoch"] + 1
        db.execute(
            text(
                "UPDATE job_async_task SET status = 'running', finished_at = NULL, error_code = NULL, error_message = NULL, "
                "lease_owner = :o, lease_epoch = :e, lease_token_hash = NULL, "
                "lease_expires_at = now() + make_interval(secs => :ttl), heartbeat_at = now() WHERE id = :t"
            ),
            {"o": adopter, "e": epoch, "ttl": lease_ttl_s(), "t": att["id"]},
        )
        _close_open_grants(db, att["id"], "expired")
        db.execute(text("UPDATE task_handoff SET state = 'verifying' WHERE id = :h AND state = 'received'"), {"h": handoff_id})
        db.commit()
        return Lease(attempt_id=att["id"], job_id=att["job_id"], run_id=run["id"], stage=att["stage"], unit_type=att["unit_type"],
                     unit_id=att["unit_id"], epoch=epoch, owner=adopter, manifest=att["input_manifest"],
                     fingerprint=att["input_fingerprint"])
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _reject_handoff(db: Session, h: dict[str, Any], reason: str) -> None:
    db.execute(
        text("UPDATE task_handoff SET state = 'rejected', reject_reason = :r WHERE id = :h AND state NOT IN ('adopted', 'rejected')"),
        {"r": reason, "h": h["id"]},
    )
    db.execute(
        text("UPDATE task_artifact SET state = 'unadopted' WHERE handoff_id = :h AND state IN ('registered', 'verified')"),
        {"h": h["id"]},
    )


def adopt(handoff_id: int, lease: Lease) -> AdoptOutcome | None:
    """인계를 채택한다. 단계 핸들러가 업무 결과를 같은 트랜잭션에서 반영한다.
    - 권한·현재 시도·미취소·입력 지문·grant 보안상 폐기를 잠금 아래 재검사한다. 실패하면 인계 거절(미채택).
    - AdoptionRejected(구조·참조·입력 불일치): 인계 rejected, 시도 failed + 실패 후속.
    - 그 밖의 예외(일시적 DB 오류 등): 전체 롤백, 인계 상태 유지, adopt_attempts+1 (계산 재시도 횟수는 그대로)."""
    db = app_db.SessionLocal()
    outcome: AdoptOutcome | None = None
    try:
        h0 = _one(db, "SELECT task_id FROM task_handoff WHERE id = :h", h=handoff_id)
        if h0 is None:
            raise NotFound(f"handoff {handoff_id}")
        job, run, att = lock_chain(db, h0["task_id"])
        h = _one(db, "SELECT * FROM task_handoff WHERE id = :h FOR UPDATE", h=handoff_id)
        arts = [dict(r) for r in db.execute(
            text("SELECT * FROM task_artifact WHERE handoff_id = :h ORDER BY id FOR UPDATE"), {"h": handoff_id}
        ).mappings().all()]
        if h["state"] == "adopted":
            db.rollback()
            return None  # 이미 채택(응답 유실 후 반복) — 결과·후속을 다시 만들지 않는다
        if h["state"] == "rejected":
            db.rollback()
            return None
        if not check_lease(db, lease):
            if not att["is_current"] or run["status"] != "running" or run["cancelled_at"] is not None or job["status"] == "archived":
                _reject_handoff(db, h, "superseded" if not att["is_current"] else "cancelled")
                db.commit()
            else:
                db.rollback()
            return None
        if h["producer_grant_id"] is not None:
            g = _one(db, "SELECT security_revoked_at FROM task_lease_grant WHERE id = :g", g=h["producer_grant_id"])
            if g and g["security_revoked_at"] is not None:
                _reject_handoff(db, h, "grant_revoked")
                db.commit()
                return None
        ctx = AdoptContext(job=job, run=run, attempt=att, handoff=h, artifacts=arts, adopter=lease.owner, epoch=lease.epoch)
        handler = handler_for(att["stage"])
        sp = db.begin_nested()
        try:
            outcome = handler.adopt(db, ctx)
            sp.commit()
        except AdoptionRejected as e:
            sp.rollback()
            _reject_handoff(db, h, f"{e.code}: {e}"[:2000])
            att = _mark_failed(db, att, e.code, str(e))
            ids = handler.after_failure(db, att, e.code, e.retryable)
            db.commit()
            dispatch(ids)
            return AdoptOutcome(status="failed", error_code=e.code, error_message=str(e), dispatch=ids)
        db.execute(
            text(
                "UPDATE task_handoff SET state = 'adopted', verified_at = coalesce(verified_at, now()), adopted_at = now(), "
                "adopted_by = :by, adopted_epoch = :e, verify_result = CAST(:vr AS jsonb) WHERE id = :h"
            ),
            {"by": lease.owner, "e": lease.epoch, "h": handoff_id,
             "vr": stable_json(outcome.verify_result) if outcome.verify_result is not None else None},
        )
        db.execute(
            text("UPDATE task_artifact SET state = 'adopted' WHERE handoff_id = :h AND state = 'verified'"), {"h": handoff_id}
        )
        if outcome.status == "done":
            db.execute(
                text(
                    "UPDATE job_async_task SET status = 'done', finished_at = now(), skip_reason = :sr, error_code = NULL, "
                    "error_message = NULL, lease_token_hash = NULL, lease_expires_at = NULL WHERE id = :t"
                ),
                {"sr": outcome.skip_reason, "t": att["id"]},
            )
            _close_open_grants(db, att["id"], "handed_off")
            outcome.dispatch = list(outcome.dispatch) + handler_after_done(db, handler, att)
        else:
            att = _mark_failed(db, att, outcome.error_code or "FAILED", outcome.error_message)
            outcome.dispatch = list(outcome.dispatch) + handler.after_failure(
                db, att, outcome.error_code or "FAILED", outcome.retryable)
        db.commit()
    except Exception:
        db.rollback()
        _bump_adopt_attempts(handoff_id)
        raise
    finally:
        db.close()
    dispatch(outcome.dispatch)
    return outcome


def handler_after_done(db: Session, handler: StageHandler, att: dict[str, Any]) -> list[int]:
    fn = getattr(handler, "after_done", None)
    if fn is None:
        return []
    att = dict(db.execute(text("SELECT * FROM job_async_task WHERE id = :t"), {"t": att["id"]}).mappings().one())
    return fn(db, att)


def _bump_adopt_attempts(handoff_id: int) -> None:
    db = app_db.SessionLocal()
    try:
        db.execute(text("UPDATE task_handoff SET adopt_attempts = adopt_attempts + 1 WHERE id = :h AND state <> 'adopted'"),
                   {"h": handoff_id})
        db.commit()
    except Exception:  # noqa: BLE001 — 기록 실패는 원래 오류를 가리지 않는다
        db.rollback()
    finally:
        db.close()


def reject_and_fail(handoff_id: int, lease: Lease, err: AdoptionRejected) -> list[int]:
    """채택 전 단계(산출물 고정 등)의 검증 실패: 인계 거절, 현재 권한이면 시도 failed + 실패 후속."""
    db = app_db.SessionLocal()
    ids: list[int] = []
    try:
        h0 = _one(db, "SELECT task_id FROM task_handoff WHERE id = :h", h=handoff_id)
        if h0 is None:
            raise NotFound(f"handoff {handoff_id}")
        job, run, att = lock_chain(db, h0["task_id"])
        h = _one(db, "SELECT * FROM task_handoff WHERE id = :h FOR UPDATE", h=handoff_id)
        if h["state"] in ("adopted", "rejected"):
            db.rollback()
            return []
        if err.retryable and check_lease(db, lease):
            db.rollback()  # 일시 오류: 인계를 유지하고 권한 소유자/복구자가 다시 시도한다
            return []
        _reject_handoff(db, h, f"{err.code}: {err}"[:2000])
        if check_lease(db, lease):
            att = _mark_failed(db, att, err.code, str(err))
            ids = handler_for(att["stage"]).after_failure(db, att, err.code, err.retryable)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    dispatch(ids)
    return ids


def mark_verified(handoff_id: int, lease: Lease) -> bool:
    """원격 복구 가능 확인 — 필수 파일이 검증 키/검증된 참조로 고정되고 구조화 결과가 영속됨을 권한 조건으로 기록한다."""
    db = app_db.SessionLocal()
    try:
        h0 = _one(db, "SELECT task_id FROM task_handoff WHERE id = :h", h=handoff_id)
        if h0 is None:
            return False
        lock_chain(db, h0["task_id"])
        if not check_lease(db, lease):
            db.rollback()
            return False
        pending = db.execute(
            text("SELECT count(*) FROM task_artifact WHERE handoff_id = :h AND state <> 'verified'"), {"h": handoff_id}
        ).scalar()
        if pending:
            db.rollback()
            return False
        r = db.execute(
            text("UPDATE task_handoff SET state = 'verified', verified_at = now() WHERE id = :h AND state IN ('received', 'verifying') "
                 "RETURNING id"), {"h": handoff_id}
        ).first()
        db.commit()
        return r is not None
    finally:
        db.close()


# ---- 중단 -------------------------------------------------------------------------------------------------
def cancel_run(db: Session, run_id: int) -> int:
    """대표 실행 중단(호출자가 job 을 잠근 상태). 미종료 시도를 cancelled 로, 세대 +1 로 기존 권한을 무효화한다.
    이후 이 실행의 결과 채택·재시도·후속 생성·화면 전이는 조건 검사에서 거절된다. 바뀐 시도 수를 돌려준다."""
    run = lock_task(db, run_id)
    if run["parent_task_id"] is not None:
        raise ExecutionError("대표 실행만 중단한다")
    if run["status"] in ("pending", "running"):
        db.execute(
            text("UPDATE job_async_task SET status = 'cancelled', cancelled_at = now(), finished_at = now() WHERE id = :r"),
            {"r": run_id},
        )
    ids = [r[0] for r in db.execute(
        text("SELECT id FROM job_async_task WHERE parent_task_id = :r AND status IN ('pending', 'running') ORDER BY id FOR UPDATE"),
        {"r": run_id},
    ).all()]
    if ids:
        db.execute(
            text(
                "UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
                "lease_token_hash = NULL, lease_expires_at = NULL WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        db.execute(
            text("UPDATE task_lease_grant SET closed_at = now(), close_reason = 'cancelled' WHERE task_id = ANY(:ids) AND closed_at IS NULL"),
            {"ids": ids},
        )
    # 아직 채택되지 않은 인계는 미채택으로 정리 대상
    db.execute(
        text(
            "UPDATE task_handoff SET state = 'rejected', reject_reason = 'cancelled' "
            "WHERE task_id IN (SELECT id FROM job_async_task WHERE parent_task_id = :r) AND state IN ('received', 'verifying', 'verified')"
        ),
        {"r": run_id},
    )
    db.execute(
        text(
            "UPDATE task_artifact SET state = 'unadopted' WHERE state IN ('registered', 'verified') AND handoff_id IN "
            "(SELECT h.id FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.parent_task_id = :r)"
        ),
        {"r": run_id},
    )
    return len(ids)


def finish_run(db: Session, run_id: int, status: str, error_code: str | None = None, error_message: str | None = None) -> None:
    """대표 실행을 done/failed 로 닫는다(호출자가 잠금). 이미 종료면 그대로."""
    db.execute(
        text(
            "UPDATE job_async_task SET status = :s, finished_at = now(), error_code = :c, error_message = :m "
            "WHERE id = :r AND status IN ('pending', 'running')"
        ),
        {"s": status, "c": error_code, "m": error_message, "r": run_id},
    )


def current_attempts(db: Session, run_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(
        text("SELECT * FROM job_async_task WHERE parent_task_id = :r AND is_current ORDER BY id"), {"r": run_id}
    ).mappings().all()]


def active_run(db: Session, job_id: int, run_kind: str) -> dict[str, Any] | None:
    return _one(
        db,
        "SELECT * FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND execution_schema_version = 1 "
        "AND run_kind = :k AND status IN ('pending', 'running') ORDER BY id DESC LIMIT 1",
        j=job_id, k=run_kind,
    )


def latest_run(db: Session, job_id: int, run_kind: str) -> dict[str, Any] | None:
    return _one(
        db,
        "SELECT * FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND execution_schema_version = 1 "
        "AND run_kind = :k ORDER BY id DESC LIMIT 1",
        j=job_id, k=run_kind,
    )
