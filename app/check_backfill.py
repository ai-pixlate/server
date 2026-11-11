"""N5 작업의 규칙 검사 일괄 등록 — 배포 절차 단계(OpenAPI confirmReview 「규칙 검사 연결」 등록 보장 ⑤, be-handoff 8.8).

  python -m app.check_backfill plan [--job ID ...]        읽기만. N5 검수 중 · 진행 중 하류 실행이 있는 작업의 검사 상태
  python -m app.check_backfill apply --job ID [--job ID]   지정한 작업만 등록(같은 입력의 유효한 시도는 그대로 둔다)

- apply 는 작업을 하나씩 잠그고 커밋한다. 등록한 시도는 커밋 뒤 큐로 보낸다. 한 작업의 미처리가 다른 작업을 막지 않는다.
- 섹션별 결과를 구분한다: 등록(registered) · 유효 시도 재사용(reused) · 대상 없음(no_targets) · 해당 없음(not_applicable)
  · 미등록(unresolved_supply · unresolved_keys — 공급 · 키 미정). 미등록은 성공으로 세지 않는다.
- 활성 검사 기록(run_scope.active_checks)이 없는 실행은 등록하지 않는다. 기존 실행의 run_scope 는 트리거가 변경을 막으므로
  활성 기록 위치가 정해질 때까지(BE 결정 대기) 미처리로 보고한다 — 실행 상태에서 활성 여부를 추정하지 않는다.
- 종료 코드: 0 미처리 없음 · 1 실패 · 2 사용법 · 3 미처리 대상 있음(마지막에 작업 · 섹션 · 사유 목록)
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from sqlalchemy import text

from app import db as app_db
from app import execution
from app.flows import common, text_check
from app.flows.common import json_value

N5_JOBS = ("SELECT j.id FROM job j WHERE j.status = 'review' AND j.current_step = 'N5' "
           "AND EXISTS (SELECT 1 FROM job_async_task r WHERE r.job_id = j.id AND r.parent_task_id IS NULL "
           "AND r.execution_schema_version = 1 AND r.run_kind = 'downstream' AND r.status = 'running') ORDER BY j.id")
NO_ACTIVE_RECORD = "no_active_record"


def _sections(db, job_id: int, run: dict) -> list[int]:
    scope = json_value(run["run_scope"]) or {}
    return [s["id"] for s in common.visible_sections(db, job_id, include_only=True) if s["id"] in scope.get("section_ids", [])]


def _report(unresolved: list[tuple[int, int | None, str, str]]) -> int:
    if not unresolved:
        return 0
    print(f"미처리 {len(unresolved)}건")
    for j, sid, status, reason in unresolved:
        print(f"  job {j} section {sid if sid is not None else '-'}: {status} — {reason}")
    return 3


def plan(job_ids: Optional[list[int]]) -> int:
    db = app_db.SessionLocal()
    unresolved: list[tuple[int, int | None, str, str]] = []
    try:
        db.execute(text("SET TRANSACTION READ ONLY"))
        ids = job_ids or [r[0] for r in db.execute(text(N5_JOBS)).all()]
        for j in ids:
            run = execution.active_run(db, j, "downstream")
            if run is None:
                print(f"job {j}: 진행 중인 하류 실행 없음 — 대상 아님")
                continue
            if "rule" not in text_check.run_active_checks(run):
                unresolved.append((j, None, NO_ACTIVE_RECORD, "활성 검사 기록 없음(기록 위치 결정 대기)"))
                continue
            count: dict[str, int] = {}
            for sid in _sections(db, j, run):
                try:
                    for v in text_check.section_states(db, run, sid).values():
                        count[v["state"]] = count.get(v["state"], 0) + 1
                except text_check.SupplyBasisUnresolved as e:
                    unresolved.append((j, sid, text_check.UNRESOLVED_SUPPLY, str(e)))
                except text_check.InputKeysUnavailable as e:
                    unresolved.append((j, sid, text_check.UNRESOLVED_KEYS, str(e)))
            print(f"job {j} run {run['id']}: 블록 상태 {dict(sorted(count.items()))}")
        return _report(unresolved)
    finally:
        db.rollback()
        db.close()


def apply(job_ids: list[int]) -> int:
    unresolved: list[tuple[int, int | None, str, str]] = []
    for j in job_ids:
        db = app_db.SessionLocal()
        ids: list[int] = []
        try:
            job = execution.lock_job(db, j)
            run = execution.active_run(db, j, "downstream")
            if job["status"] != "review" or job["current_step"] != "N5" or run is None:
                unresolved.append((j, None, "not_n5", "N5 검수 중 · 진행 중 하류 실행이 아니다"))
                db.rollback()
                continue
            run = execution.lock_task(db, run["id"])
            if "rule" not in text_check.run_active_checks(run):
                unresolved.append((j, None, NO_ACTIVE_RECORD, "활성 검사 기록 없음(기록 위치 결정 대기)"))
                db.rollback()
                continue
            tally: dict[str, int] = {}
            for sid in _sections(db, j, run):
                r = text_check.register(db, run, sid, origin="backfill")
                tally[r.status] = tally.get(r.status, 0) + 1
                if r.status == text_check.REGISTERED:
                    ids.append(r.attempt_id)
                elif r.status in text_check.UNRESOLVED:
                    unresolved.append((j, sid, r.status, r.reason or ""))
            db.commit()
            print(f"job {j} run {run['id']}: {dict(sorted(tally.items()))}")
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        execution.dispatch(ids)
    return _report(unresolved)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.check_backfill", description=__doc__.splitlines()[0])
    p.add_argument("mode", choices=["plan", "apply"])
    p.add_argument("--job", type=int, action="append", dest="jobs")
    a = p.parse_args(argv)
    if a.mode == "apply" and not a.jobs:
        p.error("apply 는 --job 으로 대상 작업을 지정한다")
    try:
        return plan(a.jobs) if a.mode == "plan" else apply(a.jobs)
    except Exception as e:  # noqa: BLE001
        print(f"실패 — {e.__class__.__name__}: {e}")
        return 1


if __name__ == "__main__":
    # 프로세스 시작 때 한 번 — API(import) · 워커(시작 신호)와 같은 함수 · 같은 공급자(설정이 없으면 등록하지 않음 — 등록은 미해결로 보고된다).
    # main() 안에서 부르면 이미 등록된 공급자(시험 대역 등)를 덮어쓰므로 진입점에만 둔다.
    from app.text_check_supply import install_from_env

    install_from_env()
    sys.exit(main())
