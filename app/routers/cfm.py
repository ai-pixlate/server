"""CFM — 검수/텍스트 블록 (API-CFM-01~04, 🟢9월).

CFM-01(블록 표·signals)·CFM-02(셀 수정·낙관적 잠금·재렌더)·CFM-03(프리뷰)·CFM-04(확정 → N6 최종 렌더) 실제 DB(+S3 presigned).
현재 채택한 분석 실행의 섹션만 보인다(app.flows.common.VISIBLE).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import render, s3
from app.db import get_db
from app.flows.common import VISIBLE
from app.security import get_current_seller

router = APIRouter(tags=["Review"])


def _require_job_owned(db: Session, job_id: int, seller_id: int) -> None:
    if not db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first():
        raise HTTPException(status_code=404, detail="job not found")


def _block_dict(r, signals: dict) -> dict:
    return {
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
        "signals": signals.get(r["id"], []),
        "revision": r["revision"],
    }


# ── CFM-01: 실제 DB ───────────────────────────────────────────────
@router.get("/jobs/{job_id}/blocks", summary="API-CFM-01 텍스트 블록 표(대조) (DB)")
def list_blocks(job_id: int, sectionId: Optional[int] = Query(default=None), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    from app.flows.final import block_signals

    _require_job_owned(db, job_id, seller_id)

    sql = (
        "SELECT tb.id, tb.section_id, tb.block_order, tb.role, "
        "tb.is_product_label, tb.is_brand_logo, tb.is_excluded, "
        "tb.source_ko, tb.trans_1, tb.trans_2, tb.block_status, tb.bbox, "
        "tb.char_count, tb.char_limit, tb.overflow, tb.auto_adjust, tb.revision "
        "FROM text_block tb JOIN section s ON s.id = tb.section_id JOIN job j ON j.id = s.job_id "
        "WHERE s.job_id = :j AND " + VISIBLE
    )
    params = {"j": job_id}
    if sectionId is not None:
        sql += " AND tb.section_id = :sid"
        params["sid"] = sectionId
    sql += " ORDER BY tb.section_id, tb.block_order, tb.id"

    rows = db.execute(text(sql), params).mappings().all()
    signals = block_signals(db, job_id)
    return [_block_dict(r, signals) for r in rows]


# ── CFM-02: 셀 수정 (DB · 낙관적 잠금) ────────────────────────────
class BlockUpdate(BaseModel):
    trans1: str = "Helps care for the look of wrinkles"
    revision: int = 0  # 낙관적 잠금: 현재 블록 revision과 일치해야 함


@router.patch("/jobs/{job_id}/blocks/{block_id}", summary="API-CFM-02 번역문 셀 수정 (DB·낙관적 잠금)")
def update_block(job_id: int, block_id: int, body: BlockUpdate, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """N5 에서만 수정한다. revision 이 같아야 하고, 수정하면 revision+1 · edited · 수정 전후 기록(edit_signal).
    같은 트랜잭션에서 그 섹션의 미리보기 재렌더 시도를 현재 revision 으로 만든다(옛 렌더 결과는 거절된다)."""
    from app import execution
    from app.flows.downstream import rerender_section

    _require_job_owned(db, job_id, seller_id)
    job = execution.lock_job(db, job_id)
    cur = db.execute(
        text(
            "SELECT tb.revision, tb.trans_1, tb.section_id, tb.is_excluded FROM text_block tb JOIN section s ON s.id = tb.section_id "
            "JOIN job j ON j.id = s.job_id WHERE tb.id = :b AND s.job_id = :j AND " + VISIBLE + " FOR UPDATE OF tb"
        ),
        {"b": block_id, "j": job_id},
    ).mappings().first()
    if not cur:
        raise HTTPException(status_code=404, detail="block not found")
    if job["current_step"] != "N5" or job["status"] != "review":
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "번역문은 N5 검수 중에만 수정한다"})
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
    db.execute(
        text("INSERT INTO edit_signal (job_id, text_block_id, signal_type, before_text, after_text) "
             "VALUES (:j, :b, 'user_edited', :bt, :at)"),
        {"j": job_id, "b": block_id, "bt": cur["trans_1"], "at": body.trans1},
    )
    rerender = rerender_section(db, job_id, cur["section_id"])
    db.commit()
    if rerender:
        execution.dispatch([rerender])
    return {
        "block": {"id": r["id"], "trans1": r["trans_1"], "blockStatus": r["block_status"], "revision": r["revision"]},
        "rerenderTaskId": rerender,
    }


@router.get("/jobs/{job_id}/preview", summary="API-CFM-03 검수 뷰어 프리뷰(다폭) (DB+S3)")
def preview(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """N3 제외 섹션은 빼고 N5 제외는 bucket=exclude 로 포함한다(계약). 미리보기 실패·미완료면 renderedUrl=null(원본 표시)."""
    _require_job_owned(db, job_id, seller_id)
    rows = db.execute(
        text(
            "SELECT s.id, s.section_order, s.source_image_id, s.bucket, s.excluded_stage, s.height, "
            "s.image_key, s.render_image_key, si.width AS src_width "
            "FROM section s JOIN job j ON j.id = s.job_id LEFT JOIN source_image si ON si.id = s.source_image_id "
            "WHERE s.job_id = :j AND " + VISIBLE + " AND NOT (s.bucket = 'exclude' AND s.excluded_stage IS DISTINCT FROM 'N5') "
            "ORDER BY si.upload_order NULLS LAST, s.section_order, s.id"
        ),
        {"j": job_id},
    ).mappings().all()

    # 섹션 식별자는 계약(ReviewPreview.sections[].id)대로 `id`로 내보낸다 —
    # `sectionId`로 주면 FE N5 어댑터가 식별자 없음으로 보고 예외를 던진다.
    sections = []
    display_top = 0
    max_w = max([r["src_width"] or render.CANVAS_WIDTH for r in rows] or [render.CANVAS_WIDTH])
    for r in rows:
        height = r["height"] or 1500
        sections.append({
            "id": r["id"],
            "sectionOrder": r["section_order"],
            "sourceImageId": r["source_image_id"],
            "bucket": r["bucket"],
            "excludedStage": r["excluded_stage"],
            "width": r["src_width"] or render.CANVAS_WIDTH,
            "displayTop": display_top,
            "height": height,
            "originalUrl": s3.presigned_get(r["image_key"]),
            "renderedUrl": s3.presigned_get(r["render_image_key"]),
            "signals": [],
        })
        display_top += height

    scale = round(500 / max_w, 2)
    return {
        "previewWidth": 500,
        "maxOriginalWidth": max_w,
        "originalHeight": display_top,
        "scale": scale,
        "previewHeight": round(display_top * scale),
        "align": "left",
        "sections": sections,
    }


class Ack(BaseModel):
    blockId: int
    code: str


class ConfirmBody(BaseModel):
    acknowledgedWarnings: list[Ack] = Field(default_factory=list)


@router.post("/jobs/{job_id}/confirm", summary="API-CFM-04 검수 확정(N5→N6) (DB)")
def confirm(job_id: int, body: Optional[ConfirmBody] = None, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """미해결 경고(번역하지 못한 칸)를 확인받고 확정한다(D9-3). 확정 트랜잭션에서 최종 렌더 시도를 서버가 등록한다."""
    from app.flows.final import ConfirmRejected
    from app.flows.final import confirm as do_confirm

    acks = [a.model_dump() for a in (body.acknowledgedWarnings if body else [])]
    try:
        r = do_confirm(db, job_id, seller_id, acks)
    except ConfirmRejected as e:
        db.rollback()
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message, **e.details}) from e
    return {"jobId": r["jobId"], "status": r["status"], "currentStep": r["currentStep"]}
