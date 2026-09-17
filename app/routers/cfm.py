"""CFM — 검수/텍스트 블록 (API-CFM-01~04, 🟢9월).

CFM-01(블록 표)은 실제 DB(text_block ⋈ section). CFM-02(셀 수정)·CFM-03(프리뷰)·
CFM-04(확정)는 재렌더 큐/S3/전이 로직이 얽혀 아직 mock.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import BlockStatus, CurrentStep, JobStatus

router = APIRouter(tags=["Review"])

MOCK_SELLER_ID = 1


# ── CFM-01: 실제 DB ───────────────────────────────────────────────
@router.get("/jobs/{job_id}/blocks", summary="API-CFM-01 텍스트 블록 표(대조) (DB)")
def list_blocks(job_id: int, sectionId: Optional[int] = Query(default=None), db: Session = Depends(get_db)):
    # job 소유 확인
    job = db.execute(
        text("SELECT id FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": MOCK_SELLER_ID},
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

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


# ── CFM-02·03·04: 재렌더 큐/S3/전이 → 아직 mock ────────────────────
class BlockUpdate(BaseModel):
    trans1: str = "Helps care for the look of wrinkles"
    revision: int = 1


@router.patch("/jobs/{job_id}/blocks/{block_id}", summary="API-CFM-02 번역문 셀 수정 (mock·재렌더 큐 예정)")
def update_block(job_id: int, block_id: int, body: BlockUpdate):
    return {
        "block": {"id": block_id, "trans1": body.trans1, "blockStatus": BlockStatus.edited, "revision": body.revision + 1},
        "rerenderTaskId": "task-rerender-001",
    }


@router.get("/jobs/{job_id}/preview", summary="API-CFM-03 검수 뷰어 프리뷰(다폭) (mock·S3 예정)")
def preview(job_id: int):
    return {
        "previewWidth": 500, "maxOriginalWidth": 1000, "scale": 0.5, "previewHeight": 1500, "align": "left",
        "sections": [{
            "sectionId": 1, "sourceImageId": 1, "width": 1000, "displayTop": 0, "bucket": "include",
            "originalUrl": "https://example-bucket.s3.amazonaws.com/src/sec.jpg?presigned=mock",
            "renderedUrl": "https://example-bucket.s3.amazonaws.com/render/sec.png?presigned=mock",
            "signals": [],
        }],
    }


@router.post("/jobs/{job_id}/confirm", summary="API-CFM-04 검수 확정(N5→N6) (mock)")
def confirm(job_id: int):
    return {"jobId": job_id, "status": JobStatus.review, "currentStep": CurrentStep.N6}
