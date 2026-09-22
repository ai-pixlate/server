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
            "SELECT id, section_order, source_image_id, bucket, excluded_stage, height, "
            "image_key, render_image_key "
            "FROM section WHERE job_id = :j ORDER BY section_order, id"
        ),
        {"j": job_id},
    ).mappings().all()

    # 섹션 식별자는 계약(ReviewPreview.sections[].id)대로 `id`로 내보낸다 —
    # `sectionId`로 주면 FE N5 어댑터가 식별자 없음으로 보고 예외를 던진다.
    # height/sectionOrder/excludedStage도 계약 필드라 함께 채운다(FE는
    # section.height * scale로 프리뷰 슬라이스 높이를 잡는다 — 없으면 0이 된다).
    sections = []
    display_top = 0
    for r in rows:
        height = r["height"] or 1500
        sections.append({
            "id": r["id"],
            "sectionOrder": r["section_order"],
            "sourceImageId": r["source_image_id"],
            "bucket": r["bucket"],
            "excludedStage": r["excluded_stage"],
            "width": render.CANVAS_WIDTH,
            "displayTop": display_top,
            "height": height,
            "originalUrl": s3.presigned_get(r["image_key"]),
            "renderedUrl": s3.presigned_get(r["render_image_key"]),
            "signals": [],
        })
        display_top += height

    scale = round(500 / render.CANVAS_WIDTH, 2)
    return {
        "previewWidth": 500,
        "maxOriginalWidth": render.CANVAS_WIDTH,
        "originalHeight": display_top,
        "scale": scale,
        "previewHeight": round(display_top * scale),
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

    # 계약(CFM-04): confirm이 렌더 task를 자동 등록한다 — "FE는 N6에서 JOB-05
    # 폴링만". 등록을 빼면 FE가 N6에 들어간 순간 render task가 없어 폴링을 멈춘다.
    from app.tasks import register_render_task  # 지연 임포트(celery 앱 로드)
    register_render_task(db, job_id)

    return {"jobId": r["id"], "status": r["status"], "currentStep": r["current_step"]}
