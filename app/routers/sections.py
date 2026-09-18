"""SEC — 섹션 (API-SEC-01~04, 🟢9월) + INP-01 인페인팅 조회.

SEC-01(목록)·SEC-02(상세)·SEC-03(제외/되살리기)는 실제 DB(section·section_verdict).
SEC-04(이대로 진행)은 번역 워커(cpu 큐), INP-01은 인페인팅 워커(gpu 큐) 결과를 실제 DB에서 조회.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import s3
from app.db import get_db
from app.schemas import SectionBucket
from app.security import get_current_seller

router = APIRouter(tags=["Sections"])


def _require_job_owned(db: Session, job_id: int, seller_id: int) -> None:
    if not db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first():
        raise HTTPException(status_code=404, detail="job not found")


def _to_section(r) -> dict:
    return {
        "id": r["id"],
        "bucket": r["bucket"],
        "exclusionReason": r["exclusion_reason"],
        "excludedStage": r["excluded_stage"],
        "warningBadge": r["warning_badge"],
        "sectionOrder": r["section_order"],
    }


class SectionAction(BaseModel):
    action: Literal["restore", "exclude"] = "exclude"


# ── SEC-01~03: 실제 DB ────────────────────────────────────────────
@router.get("/jobs/{job_id}/sections", summary="API-SEC-01 섹션 목록(버킷·배지) (DB)")
def list_sections(job_id: int, bucket: Optional[SectionBucket] = Query(default=None), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    rows = db.execute(
        text(
            "SELECT id, bucket, exclusion_reason, excluded_stage, warning_badge, section_order "
            "FROM section WHERE job_id = :j ORDER BY section_order, id"
        ),
        {"j": job_id},
    ).mappings().all()

    if bucket is not None:
        return [_to_section(r) for r in rows if r["bucket"] == bucket.value]
    return {
        "include": [_to_section(r) for r in rows if r["bucket"] == "include"],
        "exclude": [_to_section(r) for r in rows if r["bucket"] == "exclude"],
    }


@router.get("/jobs/{job_id}/sections/{section_id}", summary="API-SEC-02 섹션 상세(판정 근거) (DB)")
def get_section(job_id: int, section_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    r = db.execute(
        text(
            "SELECT id, bucket, exclusion_reason, excluded_stage, warning_badge, section_order, "
            "top_offset, height, content_findings "
            "FROM section WHERE id = :id AND job_id = :j"
        ),
        {"id": section_id, "j": job_id},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="section not found")

    verdicts = db.execute(
        text(
            "SELECT verdict_status, verdict_type, basis_article, evidence_url, reason "
            "FROM section_verdict WHERE section_id = :id"
        ),
        {"id": section_id},
    ).mappings().all()

    detail = _to_section(r)
    detail.update({
        "topOffset": r["top_offset"],
        "height": r["height"],
        "contentFindings": r["content_findings"],
        "verdicts": [dict(v) for v in verdicts],
    })
    return detail


@router.patch("/jobs/{job_id}/sections/{section_id}", summary="API-SEC-03 섹션 되살리기/제외(N3·N5 공용) (DB)")
def update_section(job_id: int, section_id: int, body: SectionAction, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    job = db.execute(
        text("SELECT current_step FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    if body.action == "exclude":
        r = db.execute(
            text(
                "UPDATE section SET bucket='exclude', exclusion_reason='user_manual', "
                "excluded_stage=:st, updated_at=now() "
                "WHERE id=:id AND job_id=:j "
                "RETURNING id, bucket, exclusion_reason, excluded_stage, warning_badge, section_order"
            ),
            {"st": job["current_step"], "id": section_id, "j": job_id},
        ).mappings().first()
    else:  # restore
        r = db.execute(
            text(
                "UPDATE section SET bucket='include', exclusion_reason='restored_by_user', "
                "excluded_stage=NULL, updated_at=now() "
                "WHERE id=:id AND job_id=:j "
                "RETURNING id, bucket, exclusion_reason, excluded_stage, warning_badge, section_order"
            ),
            {"id": section_id, "j": job_id},
        ).mappings().first()

    if not r:
        raise HTTPException(status_code=404, detail="section not found")
    db.commit()
    return _to_section(r)


# ── SEC-04: 번역 큐 연결 (N3→N4) ──────────────────────────────────
@router.post("/jobs/{job_id}/sections/proceed", status_code=202, summary="API-SEC-04 이대로 진행(N3→N4) (Celery 번역 큐)")
def proceed(job_id: int, response: Response, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    job = db.execute(
        text("SELECT current_step FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    counts = db.execute(
        text(
            "SELECT count(*) FILTER (WHERE bucket='include') AS inc, count(*) AS total "
            "FROM section WHERE job_id = :j"
        ),
        {"j": job_id},
    ).mappings().one()
    if counts["total"] == 0 or counts["inc"] == 0:
        raise HTTPException(status_code=409, detail="ALL_SECTIONS_EXCLUDED")

    from app.tasks import run_translate  # 지연 임포트
    result = run_translate.delay(job_id)  # ← 번역 태스크 큐 등록
    response.status_code = 202
    return {"jobId": job_id, "accepted": True, "celeryTaskId": result.id}


@router.get("/jobs/{job_id}/sections/{section_id}/inpaint", tags=["Inpaint"], summary="API-INP-01 섹션 인페인팅 결과 조회 (DB+S3)")
def get_inpaint(job_id: int, section_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    r = db.execute(
        text(
            "SELECT inpaint_status, residual_ratio, warning_badge, inpaint_image_url "
            "FROM section WHERE id = :sid AND job_id = :j"
        ),
        {"sid": section_id, "j": job_id},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="section not found")
    return {
        "sectionId": section_id,
        "inpaintStatus": r["inpaint_status"],
        "residualRatio": float(r["residual_ratio"]) if r["residual_ratio"] is not None else None,
        "warningBadge": r["warning_badge"],
        "inpaintImageUrl": s3.presigned_get(r["inpaint_image_url"]),
    }
