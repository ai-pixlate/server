"""CFM — 검수/텍스트 블록 (API-CFM-01~04, 🟢9월).

CFM-01(블록 표)·CFM-02(셀 수정·낙관적 잠금)·CFM-03(프리뷰)·CFM-04(확정)
모두 실제 DB(+S3 presigned). 재렌더 큐 연결(CFM-02→rerenderTaskId)만 렌더 엔진
확장 시 붙일 예정.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import render, s3
from app.db import get_db
from app.security import get_current_seller

router = APIRouter(tags=["Review"])


def _require_job_owned(db: Session, job_id: int, seller_id: int) -> None:
    if not db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first():
        raise HTTPException(status_code=404, detail="job not found")


# ── CFM-01: 실제 DB ───────────────────────────────────────────────
@router.get("/jobs/{job_id}/blocks", summary="API-CFM-01 텍스트 블록 표(대조) (DB)")
def list_blocks(job_id: int, sectionId: Optional[int] = Query(default=None), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)

    sql = (
        "SELECT tb.id, tb.section_id, tb.block_order, tb.role, "
        "tb.is_product_label, tb.is_brand_logo, tb.is_excluded, "
        "tb.source_ko, tb.trans_1, tb.trans_2, tb.block_status, tb.bbox, "
        "tb.char_count, tb.char_limit, tb.overflow, tb.auto_adjust, tb.revision "
        "FROM text_block tb JOIN section s ON s.id = tb.section_id "
        "WHERE s.job_id = :j"
    )
    params = {"j": job_id}
    if sectionId is not None:
        sql += " AND tb.section_id = :sid"
        params["sid"] = sectionId
    sql += " ORDER BY tb.section_id, tb.block_order, tb.id"

    rows = db.execute(text(sql), params).mappings().all()
    return [
        {
            "id": r["id"],
            "sectionId": r["section_id"],
            "blockOrder": r["block_order"],
            "role": r["role"],
            "isProductLabel": r["is_product_label"],
            "isBrandLogo": r["is_brand_logo"],
            "isExcluded": r["is_excluded"],
            "sourceKo": r["source_ko"],
            "trans1": r["trans_1"],
            "trans2": r["trans_2"],
            "blockStatus": r["block_status"],
            "bbox": r["bbox"],
            "charCount": r["char_count"],
            "charLimit": r["char_limit"],
            "overflow": r["overflow"],
            "autoAdjust": r["auto_adjust"],
            "revision": r["revision"],
        }
        for r in rows
    ]


# ── CFM-02: 셀 수정 (DB · 낙관적 잠금) ────────────────────────────
class BlockUpdate(BaseModel):
    trans1: str = "Helps care for the look of wrinkles"
    revision: int = 0  # 낙관적 잠금: 현재 블록 revision과 일치해야 함


@router.patch("/jobs/{job_id}/blocks/{block_id}", summary="API-CFM-02 번역문 셀 수정 (DB·낙관적 잠금)")
def update_block(job_id: int, block_id: int, body: BlockUpdate, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    cur = db.execute(
        text(
            "SELECT tb.revision FROM text_block tb JOIN section s ON s.id = tb.section_id "
            "WHERE tb.id = :b AND s.job_id = :j"
        ),
        {"b": block_id, "j": job_id},
    ).mappings().first()
    if not cur:
        raise HTTPException(status_code=404, detail="block not found")
    if cur["revision"] != body.revision:
        # 낙관적 잠금 충돌 — 최신 revision을 details로 반환
        raise HTTPException(
            status_code=409,
            detail={"code": "REVISION_CONFLICT", "current": cur["revision"]},
        )

    r = db.execute(
        text(
            "UPDATE text_block SET trans_1 = :t, block_status = 'edited', "
            "char_count = char_length(:t), revision = revision + 1, updated_at = now() "
            "WHERE id = :b RETURNING id, trans_1, block_status, revision"
        ),
        {"t": body.trans1, "b": block_id},
    ).mappings().one()
    db.commit()
    return {
        "block": {"id": r["id"], "trans1": r["trans_1"], "blockStatus": r["block_status"], "revision": r["revision"]},
        "rerenderTaskId": None,  # 재렌더 큐 연결은 렌더 엔진 단계에서
    }


@router.get("/jobs/{job_id}/preview", summary="API-CFM-03 검수 뷰어 프리뷰(다폭) (DB+S3)")
def preview(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    rows = db.execute(
        text(
            "SELECT id, section_order, source_image_id, bucket, height, "
            "image_key, render_image_key "
            "FROM section WHERE job_id = :j ORDER BY section_order, id"
        ),
        {"j": job_id},
    ).mappings().all()

    sections = []
    display_top = 0
    for r in rows:
        sections.append({
            "sectionId": r["id"],
            "sourceImageId": r["source_image_id"],
            "width": render.CANVAS_WIDTH,
            "displayTop": display_top,
            "bucket": r["bucket"],
            "originalUrl": s3.presigned_get(r["image_key"]),
            "renderedUrl": s3.presigned_get(r["render_image_key"]),
            "signals": [],
        })
        display_top += r["height"] or 1500

    return {
        "previewWidth": 500,
        "maxOriginalWidth": render.CANVAS_WIDTH,
        "scale": round(500 / render.CANVAS_WIDTH, 2),
        "previewHeight": display_top,
        "align": "left",
        "sections": sections,
    }


@router.post("/jobs/{job_id}/confirm", summary="API-CFM-04 검수 확정(N5→N6) (DB)")
def confirm(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    counts = db.execute(
        text(
            "SELECT count(*) FILTER (WHERE bucket='include') AS inc, count(*) AS total "
            "FROM section WHERE job_id = :j"
        ),
        {"j": job_id},
    ).mappings().one()
    if counts["total"] == 0 or counts["inc"] == 0:
        raise HTTPException(status_code=409, detail="ALL_SECTIONS_EXCLUDED")

    r = db.execute(
        text(
            "UPDATE job SET status='review', current_step='N6', user_facing_status='reviewing', "
            "updated_at=now() WHERE id=:j AND seller_id=:s RETURNING id, status, current_step"
        ),
        {"j": job_id, "s": seller_id},
    ).mappings().first()
    db.commit()
    return {"jobId": r["id"], "status": r["status"], "currentStep": r["current_step"]}
