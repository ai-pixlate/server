"""SEC — 섹션 (API-SEC-01~04, 🟢9월) + INP-01 인페인팅 조회.

SEC-01(목록)·SEC-02(상세)·SEC-03(제외/되살리기)는 실제 DB(section·section_verdict), 현재 채택한 분석 실행의 섹션만.
SEC-04(이대로 진행)은 하류 실행(④⑤ → ⑥⑦⑧ → 미리보기, app.flows.downstream), INP-01은 채택된 ⑥ 결과를 조회.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import s3
from app.db import get_db
from app.flows.common import VISIBLE
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
    """PATCH(SEC-03) 응답용 요약. 목록·상세는 _sections() 가 계약 Section 전체를 만든다."""
    return {
        "id": r["id"],
        "bucket": r["bucket"],
        "exclusionReason": r["exclusion_reason"],
        "excludedStage": r["excluded_stage"],
        "warningBadge": r["warning_badge"],
        "sectionOrder": r["section_order"],
    }


# Section.verdicts 정렬(2026-10-14 확정): regulated → policy → irrelevant → needs_fix → conditional.
# 계약 불변식(regulated→policy→irrelevant)에 AI 설계 기록의 심각도 순(irrelevant > needs_fix > conditional)을 이었다. 같은 값은 id 순
_VERDICT_ORDER = {"regulated": 0, "policy": 1, "irrelevant": 2, "needs_fix": 3, "conditional": 4}
_SECTION_COLUMNS = (
    "s.id, s.section_order, s.source_image_id, s.top_offset, s.height, s.bbox, s.category, s.bucket, "
    "s.exclusion_reason, s.excluded_stage, s.inpaint_status, s.warning_badge, s.content_findings"
)


def _verdict(v) -> dict:
    return {
        "id": v["id"],
        "verdictStatus": v["verdict_status"],
        "verdictType": v["verdict_type"],
        "problemText": v["problem_text"],
        "alternativeExpression": v["alternative_expression"],
        "basisArticle": v["basis_article"],
        "evidenceUrl": v["evidence_url"],
        "reason": v["reason"],
    }


def _sections(db: Session, job_id: int) -> list[dict]:
    """현재 보이는 섹션 전체를 계약 Section 으로(정렬 = section_order). displayTop 은 앞선 include 섹션 height 합(include 만)."""
    rows = db.execute(
        text(f"SELECT {_SECTION_COLUMNS} FROM section s JOIN job j ON j.id = s.job_id "
             "WHERE s.job_id = :j AND " + VISIBLE + " ORDER BY s.section_order, s.id"),
        {"j": job_id},
    ).mappings().all()
    verdicts: dict[int, list[dict]] = {}
    if rows:
        for v in db.execute(
            text("SELECT id, section_id, verdict_status, verdict_type, problem_text, alternative_expression, "
                 "basis_article, evidence_url, reason FROM section_verdict WHERE section_id = ANY(:ids) ORDER BY id"),
            {"ids": [r["id"] for r in rows]},
        ).mappings().all():
            verdicts.setdefault(v["section_id"], []).append(v)
    out, top = [], 0
    for r in rows:
        display_top = None
        if r["bucket"] == "include":
            display_top = top
            top += r["height"] or 0
        vs = sorted(verdicts.get(r["id"], []), key=lambda v: (_VERDICT_ORDER.get(v["verdict_status"], 9), v["id"]))
        out.append({
            "id": r["id"],
            "sectionOrder": r["section_order"],
            "sourceImageId": r["source_image_id"],
            "topOffset": r["top_offset"],
            "displayTop": display_top,
            "height": r["height"],
            "bbox": r["bbox"],
            "category": r["category"],
            "bucket": r["bucket"],
            "exclusionReason": r["exclusion_reason"],
            "excludedStage": r["excluded_stage"],
            "inpaintStatus": r["inpaint_status"],
            "warningBadge": r["warning_badge"],
            "verdicts": [_verdict(v) for v in vs],
            "signals": [],  # 섹션 단위 신호는 아직 만들지 않는다(블록 신호는 CFM-01)
            "_contentFindings": r["content_findings"],
        })
    return out


def _public(sec: dict) -> dict:
    return {k: v for k, v in sec.items() if not k.startswith("_")}


class SectionAction(BaseModel):
    action: Literal["restore", "exclude"] = "exclude"


# ── SEC-01~03: 실제 DB ────────────────────────────────────────────
@router.get("/jobs/{job_id}/sections", summary="API-SEC-01 섹션 목록(버킷·배지) (DB)")
def list_sections(job_id: int, bucket: Optional[SectionBucket] = Query(default=None), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    secs = [_public(x) for x in _sections(db, job_id)]
    for x in secs:  # 계약 sectionList: 제외 버킷=판정 목록 전부, 포함=썸네일만(판정 목록은 상세 SEC-02 에서)
        if x["bucket"] != "exclude":
            x["verdicts"] = []
    if bucket is not None:
        return [x for x in secs if x["bucket"] == bucket.value]
    return {
        "include": [x for x in secs if x["bucket"] == "include"],
        "exclude": [x for x in secs if x["bucket"] == "exclude"],
    }


@router.get("/jobs/{job_id}/sections/{section_id}", summary="API-SEC-02 섹션 상세(판정 근거) (DB)")
def get_section(job_id: int, section_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    sec = next((x for x in _sections(db, job_id) if x["id"] == section_id), None)
    if sec is None:
        raise HTTPException(status_code=404, detail="section not found")
    detail = _public(sec)
    detail["contentFindings"] = sec["_contentFindings"]  # 계약 밖 추가 필드(기존 유지 — 판정 근거 원문)
    return detail


@router.patch("/jobs/{job_id}/sections/{section_id}", summary="API-SEC-03 섹션 되살리기/제외(N3·N5 공용) (DB)")
def update_section(job_id: int, section_id: int, body: SectionAction, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """최종 포함 상태만 바꾼다(bucket·exclusion_reason·excluded_stage). AI 판정 원본(content_findings·original_verdict)은 그대로(D4·D6).
    판정 실패로 제외된 섹션도 되살릴 수 있고 재판정하지 않는다(D9-1). N4 진행 중에는 바꾸지 않는다(중단 후 N3 에서, D2)."""
    job = db.execute(
        text("SELECT current_step, status FROM job WHERE id = :j AND seller_id = :s FOR UPDATE"),
        {"j": job_id, "s": seller_id},
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job["current_step"] not in ("N3", "N5") or job["status"] != "review":
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "N3·N5 검토 중에만 바꾼다"})
    if job["current_step"] == "N5" and body.action == "restore":
        prev = db.execute(text("SELECT excluded_stage FROM section WHERE id = :id AND job_id = :j"), {"id": section_id, "j": job_id}).scalar()
        if prev == "N3":  # N5 에서는 N3 제외 섹션을 되살리지 않는다(하류 결과가 없다)
            raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "N3 에서 제외한 섹션은 N5 에서 되살릴 수 없다"})

    if body.action == "exclude":
        r = db.execute(
            text(
                "UPDATE section s SET bucket='exclude', exclusion_reason='user_manual', "
                "excluded_stage=:st, updated_at=now() FROM job j "
                "WHERE s.id=:id AND s.job_id=:j AND j.id = s.job_id AND " + VISIBLE + " "
                "RETURNING s.id, s.bucket, s.exclusion_reason, s.excluded_stage, s.warning_badge, s.section_order"
            ),
            {"st": job["current_step"], "id": section_id, "j": job_id},
        ).mappings().first()
    else:  # restore
        r = db.execute(
            text(
                "UPDATE section s SET bucket='include', exclusion_reason='restored_by_user', "
                "excluded_stage=NULL, updated_at=now() FROM job j "
                "WHERE s.id=:id AND s.job_id=:j AND j.id = s.job_id AND " + VISIBLE + " "
                "RETURNING s.id, s.bucket, s.exclusion_reason, s.excluded_stage, s.warning_badge, s.section_order"
            ),
            {"id": section_id, "j": job_id},
        ).mappings().first()

    if not r:
        raise HTTPException(status_code=404, detail="section not found")
    db.commit()
    return _to_section(r)


# ── SEC-04: N3→N4 하류 실행 ───────────────────────────────────────
@router.post("/jobs/{job_id}/sections/proceed", status_code=202, summary="API-SEC-04 이대로 진행(N3→N4) (Celery 큐)")
def proceed(job_id: int, response: Response, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """job 잠금 아래 포함 섹션 확인 → 하류 대표 실행 + 섹션별 ④ 시도 → N4 를 한 트랜잭션으로(D1·D2). 중복 요청은 기존 실행을 돌려준다."""
    from app.flows.downstream import ProceedRejected
    from app.flows.downstream import proceed as start_downstream

    try:
        r = start_downstream(db, job_id, seller_id)
    except ProceedRejected as e:
        db.rollback()
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message}) from e
    response.status_code = 202
    return {"jobId": job_id, "accepted": True, "taskId": r["taskId"]}


@router.get("/jobs/{job_id}/sections/{section_id}/inpaint", tags=["Inpaint"], summary="API-INP-01 섹션 인페인팅 결과 조회 (DB+S3)")
def get_inpaint(job_id: int, section_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    r = db.execute(
        text(
            "SELECT s.inpaint_status, s.residual_ratio, s.warning_badge, s.inpaint_image_url "
            "FROM section s JOIN job j ON j.id = s.job_id WHERE s.id = :sid AND s.job_id = :j AND " + VISIBLE
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
