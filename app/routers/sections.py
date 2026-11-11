"""SEC — 섹션 (API-SEC-01~04, 🟢9월) + INP-01 인페인팅 조회.

SEC-01(목록)·SEC-02(상세)·SEC-03(제외/되살리기)는 실제 DB(section·section_verdict), 현재 채택한 분석 실행의 섹션만.
SEC-04(이대로 진행)은 하류 실행(④⑤ → ⑥⑦⑧ → 미리보기, app.flows.downstream), INP-01은 채택된 ⑥ 결과를 조회.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import edit_idempotency as idem
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


# Section.verdicts 정렬(2026-10-14 확정): regulated → policy → irrelevant → needs_fix → conditional.
# 계약 불변식(regulated→policy→irrelevant)에 AI 설계 기록의 심각도 순(irrelevant > needs_fix > conditional)을 이었다. 같은 값은 id 순
_VERDICT_ORDER = {"regulated": 0, "policy": 1, "irrelevant": 2, "needs_fix": 3, "conditional": 4}
_SECTION_COLUMNS = (
    "s.id, s.section_order, s.source_image_id, s.top_offset, s.height, s.bbox, s.category, s.bucket, "
    "s.exclusion_reason, s.excluded_stage, s.inpaint_status, s.warning_badge, s.content_findings, s.revision"
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
            "revision": r["revision"],  # 섹션 낙관적 잠금 버전(포함 상태가 실제로 바뀔 때만 +1)
            "_contentFindings": r["content_findings"],
        })
    return out


def _public(sec: dict) -> dict:
    return {k: v for k, v in sec.items() if not k.startswith("_")}


class SectionAction(BaseModel):
    """요청 형식(계약 SectionPatch): action 필수, 정의되지 않은 키 거절 — 어기면 422.
    revision 은 정수 값(0·0.0 같음, false·"0"·0.5·null 은 422). Idempotency-Key 가 있으면 revision 도 필수(라우터에서 422).
    키 없는 요청의 revision 생략은 호환 기간의 동작이다 — 그 요청에는 revision 충돌 검사가 없다."""
    model_config = ConfigDict(extra="forbid")

    action: Literal["restore", "exclude"]
    revision: Optional[int] = None

    @field_validator("revision", mode="before")
    @classmethod
    def _integer_value(cls, v):
        return idem.integer_value(v)  # 생략은 기본값 None(검사 안 함), 명시한 null 은 여기서 422


# ── SEC-01~03: 실제 DB ────────────────────────────────────────────
@router.get("/jobs/{job_id}/sections", summary="API-SEC-01 섹션 목록(버킷·배지) (DB)")
def list_sections(job_id: int, bucket: Optional[SectionBucket] = Query(default=None), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    _require_job_owned(db, job_id, seller_id)
    # 계약 sectionList(PRD v3.5.10 F-SEC-02 J74④ · F-SEC-04 J76⑤): 버킷과 관계없이 섹션마다 저장된 판정 전부를
    # 상세(SEC-02)와 같은 정렬·필드로 싣는다. 조회만 한다 — 판정을 새로 만들거나 고치지 않는다
    secs = [_public(x) for x in _sections(db, job_id)]
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


_OP_SECTION_PATCH = "section_patch"
_AUTO_REASONS = ("auto_regulatory", "auto_channel", "auto_local", "auto_local_failed")


def _section(db: Session, job_id: int, section_id: int) -> dict:
    sec = next(x for x in _sections(db, job_id) if x["id"] == section_id)
    return _public(sec)


def _restore_target(row) -> tuple[Optional[str], Optional[str]]:
    """복원할 때의 사유(계약 sectionPatch 「복원의 의미」). (사유, None) 또는 (None, 거절 reason).
    직접 제외(user_manual) = 제외 전 상태로 — 최초 판정이 포함이면 사유 없음, 제외(N3 에서 되살렸던 섹션)면 restored_by_user.
    자동 제외(auto_*) = restored_by_user. 최초 판정 근거가 없으면 정책을 정하지 않고 거절한다(PM-R8 — 조사·결정 전)."""
    reason = row["exclusion_reason"]
    if reason in _AUTO_REASONS:
        return "restored_by_user", None
    if reason == "user_manual":
        ov = row["original_verdict"]
        bucket = ov.get("bucket") if isinstance(ov, dict) else None
        if bucket == "include":
            return None, None
        if bucket == "exclude":
            return "restored_by_user", None
    return None, "restore_basis_missing"  # 근거 없는 직접 제외(또는 알 수 없는 사유)


@router.patch("/jobs/{job_id}/sections/{section_id}", summary="API-SEC-03 섹션 되살리기/제외(N3·N5 공용) (DB·revision·멱등)")
def update_section(
    job_id: int,
    section_id: int,
    body: SectionAction,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128,
                                            pattern=idem.KEY_PATTERN),
    db: Session = Depends(get_db),
    seller_id: int = Depends(get_current_seller),
):
    """최종 포함 상태만 바꾼다(bucket·exclusion_reason·excluded_stage). AI 판정 원본(content_findings·original_verdict)·판정·배지는 그대로(D4·D6).
    판정 실패로 제외된 섹션도 되살릴 수 있고 재판정하지 않는다(D9-1). N4 진행 중에는 바꾸지 않는다(중단 후 N3 에서, D2).
    처리 순서(계약 blockPatch 「저장 공통」과 같음): 형식(422) → 소유(404) → 작업 행 잠금(한도 초과 503) → 작업 상태(409 job_not_editable) →
    멱등 기록(같은 요청 재전송 / 다른 요청 409 DUPLICATE) → 섹션(404) → revision(409 REVISION_CONFLICT — 같은 상태 요청이어도 먼저) →
    N5 의 N3 제외 섹션 복원(409 n3_excluded_section) → 같은 상태면 200 no-op → 복원 근거 없음(409 restore_basis_missing) → 적용(revision+1).
    응답 = 전체 Section. 성공 응답(no-op 포함)은 변경과 같은 트랜잭션에서 멱등 기록한다. 제외는 진행 중인 미리보기·검사 시도를 취소하지 않는다."""
    from app import execution

    if idempotency_key is not None and body.revision is None:
        raise HTTPException(status_code=422, detail={"code": "VALIDATION_ERROR", "message": "request validation failed",
                                                     "fields": [{"loc": ["body", "revision"], "msg": "revision is required with Idempotency-Key"}]})
    _require_job_owned(db, job_id, seller_id)
    sha = idem.request_sha256(_OP_SECTION_PATCH, {"jobId": job_id, "sectionId": section_id}, body.model_dump(exclude_unset=True))
    with idem.lock_window(db):  # 잠금 대기 한도 초과 → 트랜잭션 정리 후 503
        job = execution.lock_job(db, job_id)
        if job["current_step"] not in ("N3", "N5") or job["status"] != "review":
            raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "N3·N5 검토 중에만 바꾼다",
                                                         "reason": "job_not_editable"})
        cur = db.execute(
            text("SELECT s.revision, s.bucket, s.exclusion_reason, s.excluded_stage, s.original_verdict FROM section s "
                 "JOIN job j ON j.id = s.job_id WHERE s.id = :id AND s.job_id = :j AND " + VISIBLE + " FOR UPDATE OF s"),
            {"id": section_id, "j": job_id},
        ).mappings().first()
    replayed = idem.replay_or_reject(db, seller_id=seller_id, job_id=job_id, operation=_OP_SECTION_PATCH, key=idempotency_key, sha=sha)
    if replayed is not None:
        return replayed
    if not cur:
        db.rollback()
        raise HTTPException(status_code=404, detail="section not found")
    if body.revision is not None and body.revision != cur["revision"]:
        latest = _section(db, job_id, section_id)
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "REVISION_CONFLICT", "message": "section was changed", "current": latest})
    if job["current_step"] == "N5" and body.action == "restore" and cur["excluded_stage"] == "N3":
        db.rollback()  # N5 에서는 N3 제외 섹션을 되살리지 않는다(하류 결과가 없다) — 같은 동작 판정보다 먼저
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "N3 에서 제외한 섹션은 N5 에서 되살릴 수 없다",
                                                     "reason": "n3_excluded_section"})
    target = None  # None = 이미 같은 상태(no-op)
    if body.action == "exclude" and cur["bucket"] != "exclude":
        target = ("exclude", "user_manual", job["current_step"])
    elif body.action == "restore" and cur["bucket"] != "include":
        reason, refused = _restore_target(cur)
        if refused:
            db.rollback()
            raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "이 섹션은 복원 근거(최초 판정 기록)가 없다",
                                                         "reason": refused})
        target = ("include", reason, None)
    if target is not None:
        db.execute(
            text("UPDATE section SET bucket = :b, exclusion_reason = :r, excluded_stage = :st, revision = revision + 1, updated_at = now() "
                 "WHERE id = :id"),
            {"b": target[0], "r": target[1], "st": target[2], "id": section_id},
        )
    render_task = check_task = None
    if target is not None and target[0] == "include" and job["current_step"] == "N5":
        # N5 복원: 현재 입력의 유효한 미리보기 시도를 재사용하고 없을 때만 확보한다(같은 입력의 최종 실패·재시도 횟수 보존).
        # 검사 시도도 같은 기준 — 현재 입력의 유효한 시도가 있으면 그대로, 없을 때만 등록(등록 보장 ④)
        from app.flows.downstream import ensure_preview_for_restore
        from app.flows.text_check import ensure_for_job

        render_task = ensure_preview_for_restore(db, job_id, section_id)
        check_task = ensure_for_job(db, job_id, section_id, origin="restore")
    resp = _section(db, job_id, section_id)
    if idempotency_key is not None:
        idem.record(db, seller_id=seller_id, job_id=job_id, operation=_OP_SECTION_PATCH, key=idempotency_key, sha=sha,
                    status_code=200, response=resp)
    db.commit()
    execution.dispatch([t for t in (render_task, check_task) if t])
    return resp


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
