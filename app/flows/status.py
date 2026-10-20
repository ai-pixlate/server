"""JOB-05 진행 상태 — 현재 단계의 실행에서 현재 시도만 집계한다(대표·대체된 시도·취소 제외, D2).

OpenAPI JobTaskStatus: progress·stages·uiStatus 는 파생값이다.
- stages: N2 ocr→section→verify · N4 inpaint→translate→verify→render · N6 render (step별 고정).
- 레거시(0008 이전) 행만 있는 작업은 기존처럼 모든 행을 보인다.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import analysis_progress, execution

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
        and not (item.get("parent_task_id") and execution.retry_blocked(item))  # create_retry 생성 전 거절과 같은 판정
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
    out = {
        "jobStatus": job["status"], "currentStep": job["current_step"], "userFacingStatus": job["user_facing_status"],
        "total": total, "done": done, "failedCount": failed, "progress": round(done / total, 2) if total else 0,
        "stages": stages, "items": items,
    }
    if job["current_step"] == "N2" and run is not None:
        ap = _analysis_progress(db, run)
        if ap is not None:
            out["analysisProgress"] = ap
    return out


SPLIT_EVENTS = ("split_start", "split_done")
PHASE_OF_EVENT = {"split_start": "split", "split_done": "split", "ocr_start": "ocr", "merge_start": "merge", "region_done": "merge"}


def _fixed_regions(n: int) -> dict[str, Any]:
    return {"done": n, "known": n, "totalFixed": True}


def _analysis_progress(db: Session, run: dict[str, Any]) -> dict[str, Any] | None:
    att = db.execute(
        text("SELECT *, (lease_expires_at > now()) AS lease_alive FROM job_async_task "
             "WHERE parent_task_id = :r AND stage = 'analyze' AND is_current ORDER BY id DESC LIMIT 1"),
        {"r": run["id"]},
    ).mappings().first()
    if att is None:
        return None
    att = dict(att)
    res: dict[str, Any] = {"phase": "analyze", "status": _ui(att), "sourceTotal": att["target_count"],
                           "activity": None, "current": None, "regions": None, "judge": None}
    if att["status"] == "done":
        return {**res, **_judge_progress(db, run)}
    if att["status"] != "running":
        return res
    h = db.execute(
        text("SELECT outcome, state, late FROM task_handoff WHERE task_id = :t AND lease_epoch = :e"),
        {"t": att["id"], "e": att["lease_epoch"]},
    ).mappings().first()
    snap = analysis_progress.read(att["id"])
    if not isinstance(snap, dict) or snap.get("epoch") != att["lease_epoch"]:
        snap = None
    done_snap = snap if snap is not None and snap.get("event") == "analyze_done" else None
    if h is not None:
        if h["outcome"] == "done" and h["state"] in ("received", "verifying", "verified") and not h["late"]:
            res["activity"] = "finalize"
            res["regions"] = _fixed_regions(done_snap["snap"]["regions_done"]) if done_snap else None
        return res
    if not att["lease_alive"]:
        return res
    if done_snap is not None:
        res["activity"] = "finalize"
        res["regions"] = _fixed_regions(done_snap["snap"]["regions_done"])
        return res
    if snap is not None:
        s, ev = snap["snap"], snap["event"]
        res["activity"] = PHASE_OF_EVENT.get(ev)
        res["current"] = {"sourceIndex": s.get("source_index"), "sourceImageId": s.get("source_image_id"),
                          "regionIndex": None if ev in SPLIT_EVENTS else s.get("region_index"),
                          "regionCountInSource": s.get("region_count_in_source")}
        res["regions"] = {"done": s.get("regions_done"), "known": s.get("regions_known"),
                          "totalFixed": s.get("sources_split") == s.get("source_total")}
    return res


def _judge_progress(db: Session, run: dict[str, Any]) -> dict[str, Any]:
    rows = [dict(r) for r in db.execute(
        text("SELECT status, error_code, retry_origin FROM job_async_task WHERE parent_task_id = :r AND stage = 'judge' AND is_current"),
        {"r": run["id"]},
    ).mappings().all()]
    j = dict.fromkeys(("done", "excluded", "running", "waiting", "retrying", "failed", "cancelled"), 0)
    for r in rows:
        if r["status"] == "done":
            j["done"] += 1
        elif r["status"] == "failed":
            j["excluded" if r["error_code"] == "LOCAL_JUDGE_FAILED" else "failed"] += 1
        elif r["status"] == "cancelled":
            j["cancelled"] += 1
        elif r["status"] == "running":
            j["running"] += 1
        elif _ui(r) == "retrying":
            j["retrying"] += 1
        else:
            j["waiting"] += 1
    total = sum(j.values())
    if run["status"] == "failed":
        status = "failed"
    else:
        status = "running" if j["running"] else "retrying" if j["retrying"] else "waiting"
    return {"phase": "judge", "status": status, "regions": _fixed_regions(total), "judge": {"total": total, **j}}
