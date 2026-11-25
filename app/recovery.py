"""DB 기준 복구 프로세스(integration-decisions.md 5.29·5.32 DB 기준 복구 프로세스). Redis 를 복구 트리거로 쓰지 않는다.

EC2 에서 BE 가 단일 프로세스로 돌린다: 재기동 시와 주기적으로 sweep(). pg_try_advisory_lock 으로 한 인스턴스만 돈다.

| DB 상태 | 처리 |
|---|---|
| 현재 시도 pending, 미전달 또는 오래된 전달 | 같은 시도 id 로 재전달(dispatch_count+1, retry_count 불변) |
| running, 권한 유효 | 건드리지 않는다(시간만으로 권한을 빼앗지 않음) |
| running, 권한 만료, 수신·검증 인계 있음 | 채택 작업 재전달 → claim_adoption 이 권한을 회수·인수해 저장 복구 |
| running, 권한 만료, 인계 없음 | failed(LEASE_EXPIRED) — 누적 한도 안이면 새 시도(retry_origin=recovery), 아니면 단계 실패 후속 |
| 대표 cancelled 인데 남은 pending/running | cancelled · 세대 +1 |
| 진행 중 하류 실행인데 대기·진행 시도 없음 | 정해진 집계(progress)를 다시 실행 — 후속 생성은 run_scope·U2 아래에서만 |
| 최근 전체 취소 작업 | S3 접두사 재삭제(늦은 업로드 포함) |
DB 에 접근할 수 없으면 아무것도 시작하지 않는다(새 권한·채택 없음).

  python -m app.recovery --once
  python -m app.recovery --interval 60
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Any

from sqlalchemy import text

from app import db as app_db
from app import execution

log = logging.getLogger(__name__)
LOCK_KEY = 7_301_008  # pg_try_advisory_lock 키(복구 프로세스 중복 실행 방지)


def stale_dispatch_s() -> int:
    return int(os.getenv("PIXLATE_DISPATCH_STALE_S", "300"))  # 개발 기본값


def purge_window_h() -> int:
    return int(os.getenv("PIXLATE_PURGE_WINDOW_H", "24"))  # 개발 기본값


def processing_timeout_s() -> int:
    """처리 시간 상한(초, README 7.8 ⑤ — PM 30분). 0 이하면 점검하지 않는다."""
    return int(os.getenv("PIXLATE_PROCESSING_TIMEOUT_S", "1800"))


def auto_cancel_h() -> int:
    """24시간 자동 취소(시간, README 7.8 ④ — PM 24시간). 0 이하면 점검하지 않는다."""
    return int(os.getenv("PIXLATE_AUTO_CANCEL_H", "24"))


PROCESSING_TIMEOUT = execution.PROCESSING_TIMEOUT  # task errorCode(자유 텍스트 값 공간) — PM 11-25 승인. 채택 배제 판정과 같은 값
_TIMEOUT_RUN_KIND = {"N2": "analysis", "N4": "downstream", "N6": "final_render"}


def sweep() -> dict[str, Any]:
    stats = {"redispatched": 0, "adoptions": 0, "expired": 0, "cancelled": 0, "progressed": 0, "purged_jobs": 0, "purge_failed_jobs": 0,
             "purged_edit_idempotency": 0, "timed_out": 0, "auto_cancelled": 0, "skipped_locked": 0, "locked": False,
             "_adopt_sent": set()}
    conn = app_db.engine.connect()
    try:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            return stats
        stats["locked"] = True
        try:
            _cancelled_leftovers(stats)
            _processing_timeout(stats)  # 만료 처리 · 재전달보다 먼저 — 시간 초과 작업에 새 시도를 만들지 않게
            _expired(stats)
            _stuck_handoffs(stats)
            _pending(stats)
            _stuck_runs(stats)
            _auto_cancel(stats)
            _purge_archived(stats)
            _purge_edit_idempotency(stats)
            stats.pop("_adopt_sent", None)
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            conn.commit()
    finally:
        conn.close()
    stats.pop("_adopt_sent", None)
    return stats


def _rows(sql: str, **p) -> list[dict[str, Any]]:
    db = app_db.SessionLocal()
    try:
        return [dict(r) for r in db.execute(text(sql), p).mappings().all()]
    finally:
        db.close()


def _skip_locked(db, job_id: int, stats: dict[str, Any]) -> bool:
    """작업 행을 기다리지 않고 잡는다. 다른 트랜잭션(내보내기 · 편집 요청 등)이 잡고 있으면 이번 주기에서 건너뛴다(True) — 다음 주기에 다시 본다.
    한 작업 때문에 점검 주기 전체(뒤 작업 · 다음 주기)가 멈추지 않게 한다. 작업 행이 없어도 건너뛴다."""
    if execution.try_lock_job(db, job_id) is not None:
        return False
    db.rollback()
    stats["skipped_locked"] += 1
    return True


def _cancelled_leftovers(stats: dict[str, Any]) -> None:
    for r in _rows(
        "SELECT DISTINCT a.parent_task_id AS run_id, a.job_id FROM job_async_task a JOIN job_async_task r ON r.id = a.parent_task_id "
        "WHERE a.status IN ('pending', 'running') AND r.status = 'cancelled'"
    ):
        db = app_db.SessionLocal()
        try:
            if _skip_locked(db, r["job_id"], stats):
                continue
            stats["cancelled"] += execution.cancel_run(db, r["run_id"])
            db.commit()
        finally:
            db.close()


def _expired(stats: dict[str, Any]) -> None:
    for r in _rows(
        "SELECT a.id, a.job_id FROM job_async_task a WHERE a.execution_schema_version = 1 AND a.parent_task_id IS NOT NULL AND a.is_current "
        "AND a.status = 'running' AND (a.lease_expires_at IS NULL OR a.lease_expires_at <= now()) ORDER BY a.id"
    ):
        handoff = _rows("SELECT id FROM task_handoff WHERE task_id = :t AND state IN ('received', 'verifying', 'verified') "
                        "ORDER BY id DESC LIMIT 1", t=r["id"])
        if handoff:
            if execution.dispatch_adoption(handoff[0]["id"]):
                stats["adoptions"] += 1
                stats["_adopt_sent"].add(handoff[0]["id"])
            continue
        db = app_db.SessionLocal()
        ids: list[int] = []
        try:
            if _skip_locked(db, r["job_id"], stats):
                continue
            job, run, att = execution.lock_chain(db, r["id"])  # 작업 행은 위에서 잡았다 — 같은 트랜잭션이라 다시 기다리지 않는다
            # 잠금 아래 재확인: 인계가 막 등록됐거나 권한이 갱신됐으면 건드리지 않는다
            again = db.execute(text("SELECT status, lease_expires_at IS NULL OR lease_expires_at <= now() FROM job_async_task WHERE id = :t"),
                               {"t": r["id"]}).first()
            fresh = db.execute(text("SELECT 1 FROM task_handoff WHERE task_id = :t AND state IN ('received', 'verifying', 'verified')"),
                               {"t": r["id"]}).first()
            if again[0] != "running" or not again[1] or fresh:
                db.rollback()
                continue
            db.execute(text("UPDATE job_async_task SET lease_epoch = lease_epoch + 1 WHERE id = :t"), {"t": r["id"]})
            att = execution._mark_failed(db, att, "LEASE_EXPIRED", "실행 권한 만료 — 결과 없음")
            db.execute(text("UPDATE task_lease_grant SET closed_at = coalesce(closed_at, now()), close_reason = coalesce(close_reason, 'expired') "
                            "WHERE task_id = :t AND closed_at IS NULL"), {"t": r["id"]})
            if (run["status"] == "running" and job["status"] != "cancelled" and att["retry_count"] < att["max_retry"]
                    and not execution.retry_blocked(att)):  # 같은 고정 입력으로 성공할 수 없으면 복구 재시도를 만들지 않는다
                ids = [execution.create_retry(db, att["id"], "recovery")]
            else:
                ids = execution.handler_for(att["stage"]).after_failure(db, att, "LEASE_EXPIRED", False)
            db.commit()
            stats["expired"] += 1
        except Exception:  # noqa: BLE001 — 한 건 실패가 전체 복구를 멈추지 않게
            db.rollback()
            log.exception("만료 처리 실패 attempt=%s", r["id"])
        finally:
            db.close()
        execution.dispatch(ids)


def _stuck_handoffs(stats: dict[str, Any]) -> None:
    for r in _rows(
        "SELECT h.id FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
        "WHERE h.state IN ('received', 'verifying', 'verified') AND a.status IN ('running', 'failed') AND a.is_current "
        "AND (a.lease_expires_at IS NULL OR a.lease_expires_at <= now()) AND h.received_at <= now() - make_interval(secs => :s)",
        s=stale_dispatch_s(),
    ):
        if r["id"] in stats["_adopt_sent"]:
            continue
        if execution.dispatch_adoption(r["id"]):
            stats["adoptions"] += 1
            stats["_adopt_sent"].add(r["id"])


def _pending(stats: dict[str, Any]) -> None:
    rows = _rows(
        "SELECT a.id FROM job_async_task a JOIN job_async_task r ON r.id = a.parent_task_id JOIN job j ON j.id = a.job_id "
        "WHERE a.execution_schema_version = 1 AND a.is_current AND a.status = 'pending' AND r.status = 'running' "
        "AND j.status <> 'cancelled' AND (a.last_dispatched_at IS NULL OR a.last_dispatched_at <= now() - make_interval(secs => :s)) "
        "ORDER BY a.id",
        s=stale_dispatch_s(),
    )
    stats["redispatched"] += len(execution.dispatch([r["id"] for r in rows]))


def _stuck_runs(stats: dict[str, Any]) -> None:
    from app.flows import analysis, downstream

    for r in _rows(
        "SELECT r.id, r.job_id, r.run_kind FROM job_async_task r JOIN job j ON j.id = r.job_id "
        "WHERE r.parent_task_id IS NULL AND r.execution_schema_version = 1 AND r.status = 'running' AND j.status <> 'cancelled' "
        "AND r.run_kind IN ('analysis', 'downstream') AND NOT EXISTS (SELECT 1 FROM job_async_task a WHERE a.parent_task_id = r.id "
        "AND a.is_current AND a.status IN ('pending', 'running'))"
    ):
        db = app_db.SessionLocal()
        try:
            if _skip_locked(db, r["job_id"], stats):
                continue
            execution.lock_task(db, r["id"])
            ids = (downstream.progress(db, r["id"]) if r["run_kind"] == "downstream" else analysis._progress(db, r["id"]))
            db.commit()
            if ids:
                log.warning("집계 복구로 후속 시도 생성 run=%s %s", r["id"], ids)
            stats["progressed"] += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("집계 복구 실패 run=%s", r["id"])
            ids = []
        finally:
            db.close()
        execution.dispatch(ids)


def _purge_edit_idempotency(stats: dict[str, Any]) -> None:
    """보관 기한(24시간)이 지난 편집 저장 멱등 기록을 지운다. 조회는 기한을 보므로 지우기 전에도 재전송에 쓰이지 않는다."""
    from app.edit_idempotency import purge_expired

    db = app_db.SessionLocal()
    try:
        stats["purged_edit_idempotency"] = purge_expired(db)
        db.commit()
    finally:
        db.close()


def _processing_timeout(stats: dict[str, Any]) -> None:
    """처리 중(processing · N2 · N4 · N6)에 들어간 지 상한을 넘긴 작업 → 오류(README 7.8 ⑤). 원인을 따지지 않는다(GPU 꺼짐 포함).
    그 단계 실행의 끝나지 않은 시도(대기 · 실행 중)를 failed(PROCESSING_TIMEOUT)로 닫고 권한 세대를 올린다 — 늦게 온 결과는
    acquire · check_lease · claim_adoption 에서 거절되고, 작업이 처리 중이 아니라 다음 단계 전이(N3 · N5 · 저장 대기)도 없다.
    N2 · N6 은 실행을 failed 로 닫고, N4 는 지금 N4 오류처럼 실행을 남겨 JOB-06 [다시 시도]가 가능하다."""
    s = processing_timeout_s()
    if s <= 0:
        return
    from app.flows import analysis, common

    for r in _rows("SELECT id FROM job WHERE status = 'processing' AND state_entered_at <= now() - make_interval(secs => :s) "
                   "ORDER BY id", s=s):
        db = app_db.SessionLocal()
        try:
            job = execution.try_lock_job(db, r["id"])
            if job is None:  # 다른 트랜잭션(내보내기 등)이 잡고 있다 — 기다리지 않고 다음 주기에(주기 전체를 멈추지 않게)
                db.rollback()
                stats["skipped_locked"] += 1
                continue
            elapsed = db.execute(text("SELECT extract(epoch FROM now() - state_entered_at) FROM job WHERE id = :j"),
                                 {"j": job["id"]}).scalar()
            if job["status"] != "processing" or elapsed is None or elapsed < s:  # 잠근 뒤 다시 확인(그사이 끝났거나 재시도로 다시 셈)
                db.rollback()
                continue
            step = job["current_step"]
            kind = _TIMEOUT_RUN_KIND.get(step)
            run = execution.active_run(db, job["id"], kind) if kind else None
            msg = f"처리 시간 상한({s // 60}분) 초과"
            if run is not None:
                ids = [x[0] for x in db.execute(
                    text("SELECT id FROM job_async_task WHERE parent_task_id = :r AND status IN ('pending', 'running') ORDER BY id FOR UPDATE"),
                    {"r": run["id"]}).all()]
                if ids:
                    db.execute(text("UPDATE job_async_task SET status = 'failed', error_code = :c, error_message = :m, finished_at = now(), "
                                    "lease_epoch = lease_epoch + 1, lease_token_hash = NULL, lease_expires_at = NULL WHERE id = ANY(:ids)"),
                               {"c": PROCESSING_TIMEOUT, "m": msg, "ids": ids})
                    db.execute(text("UPDATE task_lease_grant SET closed_at = now(), close_reason = 'cancelled' "
                                    "WHERE task_id = ANY(:ids) AND closed_at IS NULL"), {"ids": ids})
                if step == "N2":
                    analysis.fail_run(db, run, PROCESSING_TIMEOUT, msg)  # 실행 failed · 숨은 섹션 삭제 · failed · N2
                elif step == "N6":
                    execution.finish_run(db, run["id"], "failed", PROCESSING_TIMEOUT, msg)
            common.set_job_state(db, job["id"], status="failed", step=step, ufs="failed")
            common.write_event(db, job["id"], common.EV_JOB_FAILED,
                               {"reason": PROCESSING_TIMEOUT, "step": step, "elapsed_s": int(elapsed)})
            db.commit()
            stats["timed_out"] += 1
            log.warning("처리 시간 초과 → 오류 job=%s step=%s %.0f초", job["id"], step, elapsed)
        except Exception:  # noqa: BLE001
            db.rollback()
            log.exception("처리 시간 상한 처리 실패 job=%s", r["id"])
        finally:
            db.close()


def _auto_cancel(stats: dict[str, Any]) -> None:
    """셀러 차례(생성됨 · 섹션확인 · 검수 · 저장 대기 · 오류)에서 마지막 움직임 뒤 24시간이 지난 작업 → 취소(README 7.8 ④).
    처리는 JOB-04 취소와 같다(cancel.cancel_locked — 콘텐츠 삭제, 작업 기록 · 이벤트 · 감사 행위는 남김). 사유는 작업 이벤트에 남긴다."""
    h = auto_cancel_h()
    if h <= 0:
        return
    from app.flows import cancel, common

    for r in _rows("SELECT id FROM job WHERE status IN ('draft', 'review', 'failed') "
                   "AND last_activity_at <= now() - make_interval(hours => :h) ORDER BY id", h=h):
        db = app_db.SessionLocal()
        keys: list[str] | None = None
        try:
            job = execution.try_lock_job(db, r["id"])
            if job is None:  # 다른 트랜잭션(내보내기 · 편집 요청 등)이 잡고 있다 — 그 요청이 커밋하면 활동 시각이 새로 찍힌다. 다음 주기에 다시 본다
                db.rollback()
                stats["skipped_locked"] += 1
                continue
            due = db.execute(text("SELECT last_activity_at <= now() - make_interval(hours => :h) FROM job WHERE id = :j"),
                             {"h": h, "j": job["id"]}).scalar()
            if not due or not cancel.auto_cancel_eligible(job):  # 잠근 뒤 다시 확인(그사이 움직였거나 처리 중으로 바뀜)
                db.rollback()
                continue
            keys = cancel.cancel_locked(db, job, reason=common.CANCEL_INACTIVE, actor_type=common.ACTOR_SYSTEM, actor_id=None)
            db.commit()
            stats["auto_cancelled"] += 1
            log.warning("24시간 무활동 자동 취소 job=%s step=%s status=%s", job["id"], job["current_step"], job["status"])
        except Exception:  # noqa: BLE001
            db.rollback()
            keys = None
            log.exception("자동 취소 실패 job=%s", r["id"])
        finally:
            db.close()
        if keys is not None:
            cancel.purge_storage(r["id"], keys)  # 실패분은 _purge_archived 가 기간 안에 다시 지운다


def _purge_archived(stats: dict[str, Any]) -> None:
    from app.flows.cancel import purge_storage

    """감시 기간(PIXLATE_PURGE_WINDOW_H) 안의 취소 작업만 다시 지운다. 지우지 못한 작업은 purge_failed_jobs 로 세고(성공으로 세지 않음)
    다음 주기에 다시 시도한다. 기간 마지막 1시간에도 실패하면 기간 뒤에는 재삭제가 없음을 오류 로그로 남긴다(운영자 처리)."""
    h = purge_window_h()
    for r in _rows("SELECT id, updated_at < now() - make_interval(hours => :last) AS closing FROM job "
                   "WHERE status = 'cancelled' AND updated_at >= now() - make_interval(hours => :h)", h=h, last=max(h - 1, 0)):
        res = purge_storage(r["id"])
        if res["failed"]:
            stats["purge_failed_jobs"] += 1
            if r["closing"]:
                log.error("취소 작업 콘텐츠 재삭제 감시 기간 종료 임박 — 남은 실패 %s건, 기간 뒤 자동 재삭제 없음 job=%s", res["failed"], r["id"])
        else:
            stats["purged_jobs"] += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pixlate DB 기준 작업 복구")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            log.info("recovery %s", sweep())
        except Exception:  # noqa: BLE001 — DB 접근 불가 등: 아무것도 시작하지 않고 다음 주기에
            log.exception("recovery sweep 실패")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    # 프로세스 시작 때 한 번 — 만료 처리(after_failure → 번역 뒤 검사 등록)가 API · 워커와 같은 공급자를 쓰게 한다(설정이 없으면 등록 안 함)
    from app.text_check_supply import install_from_env

    install_from_env()
    raise SystemExit(main())
