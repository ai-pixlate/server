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


def sweep() -> dict[str, Any]:
    stats = {"redispatched": 0, "adoptions": 0, "expired": 0, "cancelled": 0, "progressed": 0, "purged_jobs": 0, "locked": False,
             "_adopt_sent": set()}
    conn = app_db.engine.connect()
    try:
        if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
            return stats
        stats["locked"] = True
        try:
            _cancelled_leftovers(stats)
            _expired(stats)
            _stuck_handoffs(stats)
            _pending(stats)
            _stuck_runs(stats)
            _purge_archived(stats)
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


def _cancelled_leftovers(stats: dict[str, Any]) -> None:
    for r in _rows(
        "SELECT DISTINCT a.parent_task_id AS run_id, a.job_id FROM job_async_task a JOIN job_async_task r ON r.id = a.parent_task_id "
        "WHERE a.status IN ('pending', 'running') AND r.status = 'cancelled'"
    ):
        db = app_db.SessionLocal()
        try:
            execution.lock_job(db, r["job_id"])
            stats["cancelled"] += execution.cancel_run(db, r["run_id"])
            db.commit()
        finally:
            db.close()


def _expired(stats: dict[str, Any]) -> None:
    for r in _rows(
        "SELECT a.id FROM job_async_task a WHERE a.execution_schema_version = 1 AND a.parent_task_id IS NOT NULL AND a.is_current "
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
            job, run, att = execution.lock_chain(db, r["id"])
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
            if run["status"] == "running" and job["status"] != "archived" and att["retry_count"] < att["max_retry"]:
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
        "AND j.status <> 'archived' AND (a.last_dispatched_at IS NULL OR a.last_dispatched_at <= now() - make_interval(secs => :s)) "
        "ORDER BY a.id",
        s=stale_dispatch_s(),
    )
    stats["redispatched"] += len(execution.dispatch([r["id"] for r in rows]))


def _stuck_runs(stats: dict[str, Any]) -> None:
    from app.flows import analysis, downstream

    for r in _rows(
        "SELECT r.id, r.job_id, r.run_kind FROM job_async_task r JOIN job j ON j.id = r.job_id "
        "WHERE r.parent_task_id IS NULL AND r.execution_schema_version = 1 AND r.status = 'running' AND j.status <> 'archived' "
        "AND r.run_kind IN ('analysis', 'downstream') AND NOT EXISTS (SELECT 1 FROM job_async_task a WHERE a.parent_task_id = r.id "
        "AND a.is_current AND a.status IN ('pending', 'running'))"
    ):
        db = app_db.SessionLocal()
        try:
            execution.lock_job(db, r["job_id"])
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


def _purge_archived(stats: dict[str, Any]) -> None:
    from app.flows.cancel import purge_storage

    for r in _rows("SELECT id FROM job WHERE status = 'archived' AND updated_at >= now() - make_interval(hours => :h)",
                   h=purge_window_h()):
        purge_storage(r["id"])
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
    raise SystemExit(main())
