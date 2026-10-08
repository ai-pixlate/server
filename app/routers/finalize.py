"""FIN — 최종 산출물 (API-FIN-01~06, 🟢9월).

FIN-01(렌더 큐)·FIN-02(산출물 목록)·FIN-03(검증)·FIN-04(묶음=content.csv 생성)·
FIN-05(다운로드 presigned)·FIN-06(저장) 실제 DB/S3.
"""
import csv
import io
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import s3
from app.db import get_db
from app.schemas import ArtifactType
from app.security import get_current_seller

router = APIRouter(tags=["Finalize"])
log = logging.getLogger(__name__)


class ExportRequest(BaseModel):
    components: list[str] = ["content.csv"]


def _job_owned(db: Session, job_id: int, seller_id: int) -> bool:
    return db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first() is not None


def _require_job_owned(db: Session, job_id: int, seller_id: int) -> None:
    if not _job_owned(db, job_id, seller_id):
        raise HTTPException(status_code=404, detail="job not found")


# ── FIN-01·02: Celery 큐 / 실제 DB ────────────────────────────────
@router.post("/jobs/{job_id}/render", status_code=202, summary="API-FIN-01 최종 이미지 렌더링 (Celery 큐)")
def render(job_id: int, response: Response, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """N6 수동 재렌더. 진행 중이면 그 시도, 실패면 새 재시도 시도(누적 한도), 완료면 새 최종 렌더 실행을 만든다.
    renderTaskId 는 job_async_task.id(시도) — JOB-05 폴링 대상이다."""
    from app import execution
    from app.flows.final import start_final_render

    _require_job_owned(db, job_id, seller_id)
    job = execution.lock_job(db, job_id)
    if job["current_step"] != "N6" or job["status"] not in ("review", "done"):
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "최종 렌더는 N6 에서만"})
    try:
        task_id = start_final_render(db, job_id)
    except execution.RetryNotAllowed as e:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "RETRY_NOT_ALLOWED", "message": str(e)}) from e
    db.commit()
    execution.dispatch([task_id])
    response.status_code = 202
    return {"jobId": job_id, "accepted": True, "renderTaskId": task_id}


# 계약(DeliverableList.components[].type)의 닫힌 4종. N6 내보내기 패널은 이
# 목록으로 행을 그리므로, 아직 생성 전인 타입도 artifactId=null·status='pending'
# 으로 함께 내려준다(계약이 artifactId nullable·status pending을 허용하는 이유).
_COMPONENT_TYPES = ("images", "csv", "html", "psd")


def _components(db: Session, job_id: int) -> list[dict]:
    rows = db.execute(
        text(
            "SELECT id, artifact_type, is_generated, is_active FROM export_artifact "
            "WHERE job_id = :j ORDER BY id"
        ),
        {"j": job_id},
    ).mappings().all()
    by_type = {r["artifact_type"]: r for r in rows}

    out = []
    for t in _COMPONENT_TYPES:
        r = by_type.get(t)
        is_generated = bool(r["is_generated"]) if r else False
        out.append({
            "artifactId": r["id"] if r else None,
            "type": t,
            "status": "generated" if is_generated else "pending",
            "isGenerated": is_generated,
            # 행이 없으면 export_artifact.is_active의 스키마 기본값(true)을 따른다.
            "isActive": bool(r["is_active"]) if r else True,
            "failedCount": 0,
            "retryAction": None,
        })
    return out


@router.get("/jobs/{job_id}/deliverables", summary="API-FIN-02 산출물 목록 (DB)")
def deliverables(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    rows = db.execute(
        text(
            "SELECT id, source_image_id, usage_type, image_url, format, color_space, "
            "file_size, render_status, validation_result FROM deliverable "
            "WHERE job_id = :j ORDER BY id"
        ),
        {"j": job_id},
    ).mappings().all()
    return {
        "deliverables": [
            {
                "id": r["id"],
                "sourceImageId": r["source_image_id"],
                "usageType": r["usage_type"],
                "imageUrl": s3.presigned_get(r["image_url"]),
                "format": r["format"],
                "colorSpace": r["color_space"],
                "fileSize": r["file_size"],
                "renderStatus": r["render_status"],
                "validationResult": r["validation_result"],
            }
            for r in rows
        ],
        # 계약 필수 필드. 빠져 있으면 FE N6 내보내기 패널이 행 0개로 비어
        # 선택할 산출물이 없어 "선택 항목 내보내기"가 영구 비활성된다.
        "components": _components(db, job_id),
    }


# ── FIN-03·04·05·06: 실제 DB / S3 ─────────────────────────────────
@router.get("/jobs/{job_id}/validation", summary="API-FIN-03 규격 검증 상세 (DB)")
def validation(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    rows = db.execute(
        text(
            "SELECT id, usage_type, validation_result FROM deliverable "
            "WHERE job_id = :j ORDER BY id"
        ),
        {"j": job_id},
    ).mappings().all()
    # 계약(FIN-03)은 ValidationDetail[] 배열이다 — {deliverables:[...]} 래퍼가 아니다.
    # validation_result(JSONB)는 렌더 엔진이 채운다(checks[]). 없으면 빈 배열.
    # scope는 계약 enum(detail·thumbnail_main·thumbnail_sub·all)이라 deliverable의
    # usage_type을 쓴다 — checks[].scope('image')는 계약 enum 값이 아니다.
    out = []
    for r in rows:
        vr = r["validation_result"] or {}
        for c in vr.get("checks", []):
            actual = c.get("actual")
            out.append({
                "itemKey": c.get("key"),
                "scope": r["usage_type"],
                "passed": c.get("passed"),
                "measuredValue": None if actual is None else str(actual),
                "severity": c.get("severity", "error"),
            })
    return out


@router.post("/jobs/{job_id}/export", status_code=201, summary="API-FIN-04 산출물 묶음 생성(content.csv · images → S3)")
def export(job_id: int, body: ExportRequest, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """content.csv(현재 포함 섹션의 블록)와 images(최종 렌더 이미지 zip). 일부 이미지를 읽지 못하면 성공분만 묶고 실패를 알린다(D9-3)."""
    import zipfile

    from app.flows.common import VISIBLE
    from app.storage import ObjectMissing, get_store

    _require_job_owned(db, job_id, seller_id)
    wanted = set(body.components or ["content.csv"])
    out = {}
    if "content.csv" in wanted or "csv" in wanted:
        blocks = db.execute(
            text(
                "SELECT tb.section_id, tb.block_order, tb.role, tb.source_ko, tb.trans_1 "
                "FROM text_block tb JOIN section s ON s.id = tb.section_id JOIN job j ON j.id = s.job_id "
                "WHERE s.job_id = :j AND s.bucket = 'include' AND " + VISIBLE + " ORDER BY tb.section_id, tb.block_order, tb.id"
            ),
            {"j": job_id},
        ).mappings().all()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["sectionId", "blockOrder", "role", "sourceKo", "trans1"])
        for b in blocks:
            w.writerow([b["section_id"], b["block_order"], b["role"], b["source_ko"] or "", b["trans_1"] or ""])
        data = buf.getvalue().encode("utf-8-sig")  # 엑셀 한글 대비
        key = s3.make_key(f"export/{job_id}", "content.csv")
        s3.upload_fileobj(io.BytesIO(data), key, content_type="text/csv")
        row = db.execute(
            text("INSERT INTO export_artifact (job_id, artifact_type, file_url, is_distributable, is_generated) "
                 "VALUES (:j, 'csv', :k, true, true) RETURNING id"),
            {"j": job_id, "k": key},
        ).mappings().one()
        out = {"artifactId": row["id"], "artifactType": "csv", "components": ["csv"], "rows": len(blocks)}
    if "images" in wanted:
        rows = db.execute(text("SELECT id, image_url FROM deliverable WHERE job_id = :j AND render_status = 'done' ORDER BY id"),
                          {"j": job_id}).mappings().all()
        zbuf, failed = io.BytesIO(), []
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            for r in rows:
                try:
                    z.writestr(f"deliverable_{r['id']}.png", get_store().get(r["image_url"]))
                except ObjectMissing:
                    failed.append(r["id"])
        key = s3.make_key(f"export/{job_id}", "images.zip")
        s3.upload_fileobj(io.BytesIO(zbuf.getvalue()), key, content_type="application/zip")
        row = db.execute(
            text("INSERT INTO export_artifact (job_id, artifact_type, file_url, is_distributable, is_generated) "
                 "VALUES (:j, 'images', :k, true, true) RETURNING id"),
            {"j": job_id, "k": key},
        ).mappings().one()
        if failed:  # 성공분만 묶는다. 실패 수를 내려줄 계약 필드는 아직 없다(FIN-02 failedCount 저장 위치 미정)
            log.warning("export images: job=%s 읽지 못한 산출물 %s", job_id, failed)
        out.setdefault("artifactId", row["id"])
        out.setdefault("artifactType", "images")
        out["components"] = out.get("components", []) + ["images"]
    db.commit()
    return out


@router.get("/jobs/{job_id}/exports/{artifact_id}/download", summary="API-FIN-05 산출물 다운로드(presigned) (DB+S3)")
def download(job_id: int, artifact_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    r = db.execute(
        text(
            "SELECT file_url, artifact_type FROM export_artifact "
            "WHERE id = :a AND job_id = :j AND is_distributable = true AND is_generated = true"
        ),
        {"a": artifact_id, "j": job_id},
    ).mappings().first()
    if not r or not r["file_url"]:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {
        "url": s3.presigned_get(r["file_url"]),
        "fileName": f"pixlate_export_{job_id}.{r['artifact_type']}",
        "expiresIn": s3.PRESIGN_TTL,
    }


@router.get("/jobs/{job_id}/export/download", summary="API-FIN-05 산출물 개별 다운로드(타입별 presigned) (DB+S3)")
def download_by_type(
    job_id: int,
    artifactType: ArtifactType = Query(default="zip"),
    db: Session = Depends(get_db),
    seller_id: int = Depends(get_current_seller),
):
    """N6 행별 개별 다운로드. 계약에 있는데 구현이 없어 FE "개별 다운로드"가 404였다."""
    _require_job_owned(db, job_id, seller_id)
    r = db.execute(
        text(
            "SELECT file_url, artifact_type FROM export_artifact "
            "WHERE job_id = :j AND artifact_type = :t "
            "AND is_distributable = true AND is_generated = true "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"j": job_id, "t": artifactType},
    ).mappings().first()
    if not r or not r["file_url"]:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {
        "url": s3.presigned_get(r["file_url"]),
        "fileName": f"pixlate_export_{job_id}.{r['artifact_type']}",
        "expiresIn": s3.PRESIGN_TTL,
    }


@router.post("/jobs/{job_id}/save", summary="API-FIN-06 저장(보관함) (DB)")
def save(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """최종 렌더 성공 후에만 저장·완료한다(D9-3). 실패·진행 중이면 409.
    전체 취소(archived)와 경쟁하지 않도록 job 행을 잠근 뒤 같은 트랜잭션에서 상태·저장 가능 여부를 판정한다(D9-4)."""
    from app import execution
    from app.flows.final import can_save

    _require_job_owned(db, job_id, seller_id)
    job = execution.lock_job(db, job_id)
    if job["status"] not in ("review", "done") or job["current_step"] != "N6":
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE",
                                                     "message": f"N6 에서만 저장한다({job['current_step']}/{job['status']})"})
    if not can_save(db, job_id):
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "최종 렌더가 성공하지 않았다"})
    r = db.execute(
        text(
            "UPDATE job SET is_saved = true, saved_at = now(), status = 'done', "
            "user_facing_status = 'done', updated_at = now() "
            "WHERE id = :j AND seller_id = :s AND status <> 'archived' AND current_step = 'N6' RETURNING id, is_saved, saved_at"
        ),
        {"j": job_id, "s": seller_id},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="job not found")
    db.commit()
    return {
        "jobId": r["id"], "status": "done", "isSaved": r["is_saved"],
        "savedAt": r["saved_at"].isoformat() if r["saved_at"] else None,
    }
