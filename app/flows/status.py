"""JOB-05 진행 상태 — 현재 단계의 실행에서 현재 시도만 집계한다(대표·대체된 시도·취소 제외, D2).

OpenAPI JobTaskStatus: progress·stages·uiStatus 는 파생값이다.
- stages: N2 ocr→section→verify · N4 inpaint→translate→verify→render · N6 render (step별 고정).
- 레거시(0008 이전) 행만 있는 작업은 기존처럼 모든 행을 보인다.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

STAGE_KEYS = {
    "N2": ["ocr", "section", "verify"],
    "N3": ["ocr", "section", "verify"],
    "N4": ["inpaint", "translate", "verify", "render"],
    "N5": ["inpaint", "translate", "verify", "render"],
    "N6": ["render"],
}
STEP_RUN = {"N1": "analysis", "N2": "analysis", "N3": "analysis", "N4": "downstream", "N5": "downstream", "N6": "final_render"}
USER_RETRY_TYPES = ("translate", "render")  # JOB-06 계약: task_type ∈ {translate, render}


def _run_for(db: Session, job: dict[str, Any]) -> dict[str, Any] | None:
    kind = STEP_RUN.get(job["current_step"])
    if kind is None:
        return None
    sql = ("SELECT * FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND execution_schema_version = 1 "
           "AND run_kind = :k ORDER BY id DESC LIMIT 1")
    r = db.execute(text(sql), {"j": job["id"], "k": kind}).mappings().first()
    if r is None and kind == "final_render":
        r = db.execute(text(sql), {"j": job["id"], "k": "downstream"}).mappings().first()
    return dict(r) if r else None


def _ui(item: dict[str, Any]) -> str:
    s = item["status"]
    if s == "pending":
        return "retrying" if item.get("retry_origin") else "waiting"
    return {"running": "running", "done": "done", "failed": "failed"}.get(s, s)


def retryable(item: dict[str, Any], run: dict[str, Any] | None) -> bool:
    return bool(
        item["status"] == "failed" and item.get("is_current", True) and item["task_type"] in USER_RETRY_TYPES
        and item["retry_count"] < item["max_retry"] and (run is None or run["status"] == "running")
        and item.get("error_code") not in ("AI_ADAPTER_UNAVAILABLE",)
    )


def task_status(db: Session, job: dict[str, Any]) -> dict[str, Any]:
    run = _run_for(db, job)
    if run is not None:
        rows = [dict(r) for r in db.execute(
            text("SELECT * FROM job_async_task WHERE parent_task_id = :r AND is_current AND status <> 'cancelled' ORDER BY id"),
            {"r": run["id"]},
        ).mappings().all()]
    else:
        has_v1 = db.execute(
            text("SELECT 1 FROM job_async_task WHERE job_id = :j AND execution_schema_version = 1 LIMIT 1"), {"j": job["id"]}
        ).first()
        rows = [] if has_v1 else [dict(r) for r in db.execute(
            text("SELECT * FROM job_async_task WHERE job_id = :j AND status <> 'cancelled' ORDER BY id"), {"j": job["id"]}
        ).mappings().all()]
    items = [
        {
            "taskId": r["id"], "taskType": r["task_type"], "unitType": r["unit_type"], "unitId": r["unit_id"],
            "status": r["status"], "uiStatus": _ui(r), "retryCount": r["retry_count"], "maxRetry": r["max_retry"],
            "retryable": retryable(r, run), "errorCode": r["error_code"], "revision": r["revision"],
        }
        for r in rows
    ]
    stages = []
    for key in STAGE_KEYS.get(job["current_step"], []):
        st = [i["status"] for i in items if i["taskType"] == key]
        if not st:
            status = "done" if run is not None and run["status"] == "done" else "pending"
        elif "failed" in st:
            status = "failed"
        elif "running" in st:
            status = "running"
        elif all(s == "done" for s in st):
            status = "done"
        elif all(s == "pending" for s in st):
            status = "pending"
        else:
            status = "running"
        stages.append({"key": key, "label": key, "status": status})
    total = len(items)
    done = sum(1 for i in items if i["status"] == "done")
    failed = sum(1 for i in items if i["status"] == "failed")
    return {
        "jobStatus": job["status"], "currentStep": job["current_step"], "userFacingStatus": job["user_facing_status"],
        "total": total, "done": done, "failedCount": failed, "progress": round(done / total, 2) if total else 0,
        "stages": stages, "items": items,
    }
