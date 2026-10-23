"""JOB — 작업 (API-JOB-02~07, 🟢9월) + ANL-01 분석 시작.

JOB-02~07·ANL-01 실제 DB/Celery. seller_id는 Bearer 토큰에서 추출(get_current_seller).
N1 은 draft-first(계약 jobCreate): POST /jobs(brandId 만) → SRC-02 업로드 → PATCH /jobs/{jobId}(N1 값) → analyze.
생성·조회·PATCH 응답은 모두 계약 `Job`(식별자 `id`)이다.
"""
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import idempotency
from app.category_mapping import CategoryMappingConflict, resolve_internal_category
from app.db import get_db
from app.security import get_current_seller

router = APIRouter(tags=["Jobs"])
log = logging.getLogger(__name__)

RegulatoryClass = Literal["cosmetic", "otc", "combination", "unknown"]
SpecType = Literal["original", "site", "custom"]
ImageContinuity = Literal["multi_section", "single"]


class JobCreate(BaseModel):
    """계약 JobCreate — brandId 만 필수. 나머지 N1 값은 업로드 뒤 PATCH 로 세팅하고, 필수값 검증은 analyze 게이트에서 한다."""
    brandId: int = Field(examples=[1])
    productName: Optional[str] = Field(default=None, examples=["수분 크림 50ml"])
    productCode: Optional[str] = Field(default=None, examples=["SKU-1001"])
    targetCountry: Optional[str] = Field(default="US", examples=["US"])
    regulatoryClass: Optional[str] = Field(default=None, examples=["cosmetic"])
    targetLanguage: Optional[str] = Field(default="en", examples=["en"])
    specType: str = Field(default="original", examples=["original"])
    channelSpecId: Optional[int] = None
    categoryId: Optional[int] = None
    keywords: list[str] = Field(default_factory=list, examples=[["moisture", "cream"]])
    imageType: Optional[ImageContinuity] = None
    consentId: Optional[str] = None  # consent 연동은 추후(현재 미저장)


class JobPatch(BaseModel):
    """계약 JobPatch — 보낸 필드만 반영(생략 = 그대로, null = 비움). 모르는 필드는 무시한다."""
    model_config = ConfigDict(extra="ignore")
    productName: Optional[str] = None
    productCode: Optional[str] = None
    targetCountry: Optional[str] = None
    regulatoryClass: Optional[RegulatoryClass] = None
    targetLanguage: Optional[str] = None
    specType: Optional[SpecType] = None
    channelSpecId: Optional[int] = None
    categoryId: Optional[int] = None
    keywords: Optional[list[str]] = None
    imageType: Optional[ImageContinuity] = None
    consentId: Optional[int] = None  # 계약 필드. 현재 저장하지 않는다(생성과 같음)


# 계약 필드 → job 컬럼(값을 그대로 쓰는 것). categoryId·keywords·consentId 는 따로 처리한다
_PATCH_COLUMNS = {
    "productName": "product_name", "productCode": "product_code", "targetCountry": "target_country",
    "regulatoryClass": "regulatory_class", "targetLanguage": "target_lang", "specType": "spec_type",
    "channelSpecId": "channel_spec_id", "imageType": "image_type",
}
_NOT_NULL = {"specType", "keywords"}  # DB NOT NULL(spec_type) · 계약상 배열(keywords) — null 은 받지 않는다


def _validation_error(message: str, fields: list[dict[str, Any]] | None = None) -> HTTPException:
    detail: dict[str, Any] = {"code": "VALIDATION_ERROR", "message": message}
    if fields:
        detail["fields"] = fields
    return HTTPException(status_code=400, detail=detail)


def job_payload(db: Session, job_id: int, seller_id: int) -> Optional[dict[str, Any]]:
    """계약 `Job`. 남의 작업이면 None."""
    r = db.execute(
        text(
            "SELECT id, status, current_step, user_facing_status, product_name, product_code, "
            "brand_id, target_country, regulatory_class, target_lang, spec_type, "
            "channel_spec_id, display_category_id, internal_category, image_type, is_saved, "
            "created_at, updated_at "
            "FROM job WHERE id = :id AND seller_id = :s"
        ),
        {"id": job_id, "s": seller_id},
    ).mappings().first()
    if not r:
        return None
    keywords = [
        kw["keyword"]
        for kw in db.execute(
            text("SELECT keyword FROM job_keyword WHERE job_id = :id ORDER BY order_no"),
            {"id": job_id},
        ).mappings().all()
    ]
    return {
        "id": r["id"],
        "status": r["status"],
        "currentStep": r["current_step"],
        "userFacingStatus": r["user_facing_status"],
        "productName": r["product_name"],
        "productCode": r["product_code"],
        "brandId": r["brand_id"],
        "targetCountry": r["target_country"],
        "regulatoryClass": r["regulatory_class"],
        "targetLanguage": r["target_lang"],
        "specType": r["spec_type"],
        "channelSpecId": r["channel_spec_id"],
        "categoryId": r["display_category_id"],
        "internalCategory": r["internal_category"],
        "keywords": keywords,
        "imageType": r["image_type"],
        "isSaved": r["is_saved"],
        "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
        "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


def _category(db: Session, category_id: Optional[int]) -> Optional[str]:
    """선택 카테고리의 직접 매핑 → 내부 분류(용어집 적용 분류). 작업과 같은 트랜잭션에서 저장한다(통합 D10)."""
    try:
        return resolve_internal_category(db, category_id)
    except CategoryMappingConflict as e:
        db.rollback()
        log.error("작업 N1 값 거절 — 카테고리 매핑 충돌: category=%s values=%s", e.category_id, e.values)
        # 데이터·설정 오류. 전용 오류 코드가 계약에 없어 기존 INTERNAL_ERROR(500)를 쓴다
        raise HTTPException(status_code=500, detail={"code": "INTERNAL_ERROR", "message": "category mapping is inconsistent"}) from e


def _replace_keywords(db: Session, job_id: int, keywords: list[str]) -> None:
    db.execute(text("DELETE FROM job_keyword WHERE job_id = :j"), {"j": job_id})
    for i, kw in enumerate(keywords, start=1):
        db.execute(
            text("INSERT INTO job_keyword (job_id, keyword, order_no) VALUES (:j, :k, :o)"),
            {"j": job_id, "k": kw, "o": i},
        )


# ── JOB-02·03·04: 실제 DB ─────────────────────────────────────────
def _insert(db: Session, body: JobCreate, seller_id: int) -> tuple[int, dict[str, Any]]:
    """작업 행·키워드를 넣고 응답 본문을 만든다(커밋하지 않음). 남의·없는 브랜드면 404 — 아무것도 넣지 않는다."""
    if db.execute(
        text("SELECT 1 FROM brand WHERE id = :b AND seller_id = :s"), {"b": body.brandId, "s": seller_id}
    ).first() is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "brand not found"})
    internal_category = _category(db, body.categoryId)
    job_id = db.execute(
        text(
            "INSERT INTO job (seller_id, brand_id, product_name, product_code, "
            "target_country, regulatory_class, target_lang, spec_type, "
            "channel_spec_id, display_category_id, internal_category, image_type, user_facing_status) "
            "VALUES (:s, :brand, :pname, :pcode, :country, :regclass, :lang, :spec, "
            ":chspec, :cat, :icat, :itype, 'draft') RETURNING id"
        ),
        {
            "s": seller_id, "brand": body.brandId, "pname": body.productName,
            "pcode": body.productCode, "country": body.targetCountry,
            "regclass": body.regulatoryClass, "lang": body.targetLanguage,
            "spec": body.specType, "chspec": body.channelSpecId, "cat": body.categoryId,
            "icat": internal_category, "itype": body.imageType,
        },
    ).scalar_one()
    _replace_keywords(db, job_id, body.keywords)
    return job_id, job_payload(db, job_id, seller_id)


def _committed(job_id: int, seller_id: int) -> Optional[bool]:
    """커밋 결과가 불명할 때 새 연결로 작업 행이 있는지 본다. 확인하지 못하면 None."""
    from app import db as app_db

    try:
        with app_db.SessionLocal() as s:
            return s.execute(text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"), {"j": job_id, "s": seller_id}).first() is not None
    except Exception:
        log.exception("작업 생성 커밋 결과 확인 실패 job=%s", job_id)
        return None


_IDEM_OP = "post_jobs"


@router.post("/jobs", status_code=201, summary="API-JOB-02 작업 생성(draft) (DB)")
def create_job(
    body: JobCreate,
    db: Session = Depends(get_db),
    seller_id: int = Depends(get_current_seller),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
):
    if not idempotency_key:
        _, payload = _insert(db, body, seller_id)
        db.commit()
        return payload

    # 상태·장애 구간 처리는 app.idempotency 머리말
    try:
        rec = idempotency.begin(seller_id, _IDEM_OP, idempotency_key)
    except idempotency.InFlight:
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE", "message": "same Idempotency-Key request is in progress"})
    except idempotency.Unavailable:
        raise HTTPException(status_code=503, detail={"code": "SERVICE_UNAVAILABLE", "message": "idempotency store unavailable"})
    if rec.state == "done":
        return rec.body  # 완료된 같은 키 → 최초 응답(본문이 달라도)
    if rec.state == "committing":
        if _committed(rec.job_id, seller_id):
            idempotency.complete(seller_id, _IDEM_OP, idempotency_key, rec.body)
            return rec.body
        # 커밋이 아직 진행 중이거나 실패·확인 불가 — 같은 키로는 새로 만들지 않는다
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE", "message": "same Idempotency-Key request is not settled"})

    try:  # 커밋 시도 전 실패는 저장되지 않았음이 확실하다 → 키 삭제
        job_id, payload = _insert(db, body, seller_id)
        idempotency.mark_committing(seller_id, _IDEM_OP, idempotency_key, job_id, payload)
    except idempotency.Unavailable:
        db.rollback()
        idempotency.release(seller_id, _IDEM_OP, idempotency_key)
        raise HTTPException(status_code=503, detail={"code": "SERVICE_UNAVAILABLE", "message": "idempotency store unavailable"})
    except BaseException:
        db.rollback()
        idempotency.release(seller_id, _IDEM_OP, idempotency_key)
        raise
    try:
        db.commit()
    except Exception:
        # 커밋 결과 불명: 작업 행이 확인될 때만 확정한다. 없거나 확인하지 못하면 committing 을 남긴다(키 삭제 안 함)
        if _committed(job_id, seller_id):
            idempotency.complete(seller_id, _IDEM_OP, idempotency_key, payload)
            return payload
        raise
    idempotency.complete(seller_id, _IDEM_OP, idempotency_key, payload)  # 실패해도 committing 이 남아 재요청 때 확정
    return payload


@router.get("/jobs/{job_id}", summary="API-JOB-03 작업 조회(상태+N1 입력값) (DB)")
def get_job(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    r = job_payload(db, job_id, seller_id)
    if r is None:
        raise HTTPException(status_code=404, detail="job not found")
    return r


@router.patch("/jobs/{job_id}", summary="API-JOB-02 N1 입력값 세팅/수정 (draft-first, DB)")
def patch_job(
    job_id: int,
    raw: Any = Body(default=None),
    db: Session = Depends(get_db),
    seller_id: int = Depends(get_current_seller),
):
    """보낸 필드만 갱신한다(생략은 그대로, null 은 비움). draft/N1 에서만 허용, 아니면 409 INVALID_STATE.
    남의 작업·없는 작업은 404 NOT_FOUND. 검증 실패는 400 VALIDATION_ERROR 이고 아무것도 바꾸지 않는다."""
    job = db.execute(
        text("SELECT id, status, current_step FROM job WHERE id = :j AND seller_id = :s FOR UPDATE"),
        {"j": job_id, "s": seller_id},
    ).mappings().first()
    if job is None:
        db.rollback()
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "job not found"})
    if not isinstance(raw, dict):
        db.rollback()
        raise _validation_error("request body must be a JSON object")
    try:
        body = JobPatch.model_validate(raw)
    except ValidationError as e:
        db.rollback()
        raise _validation_error("request validation failed",
                                [{"loc": list(x.get("loc", [])), "msg": str(x.get("msg", ""))} for x in e.errors()]) from e
    sent = body.model_fields_set
    nulls = sorted(f for f in sent & _NOT_NULL if getattr(body, f) is None)
    if nulls:
        db.rollback()
        raise _validation_error("field must not be null", [{"loc": ["body", f], "msg": "null not allowed"} for f in nulls])
    if not (job["status"] == "draft" and job["current_step"] == "N1"):
        db.rollback()
        raise HTTPException(status_code=409, detail={
            "code": "INVALID_STATE", "message": f"N1 값은 draft/N1 에서만 바꿀 수 있다({job['current_step']}/{job['status']})"})

    sets, params = [], {"j": job_id}
    for f in sorted(sent & _PATCH_COLUMNS.keys()):
        sets.append(f"{_PATCH_COLUMNS[f]} = :{f}")
        params[f] = getattr(body, f)
    if "categoryId" in sent:
        sets += ["display_category_id = :categoryId", "internal_category = :icat"]
        params["categoryId"] = body.categoryId
        params["icat"] = _category(db, body.categoryId)
    if "keywords" in sent:
        _replace_keywords(db, job_id, body.keywords)
    if sets or "keywords" in sent:
        sets.append("updated_at = now()")
        db.execute(text(f"UPDATE job SET {', '.join(sets)} WHERE id = :j"), params)
    payload = job_payload(db, job_id, seller_id)
    db.commit()
    return payload


@router.delete("/jobs/{job_id}", summary="API-JOB-04 작업 취소 (DB · archived · 콘텐츠 삭제)")
def cancel_job(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """✕ 전체 취소(D9-4): 진행 실행을 중단하고 원본·섹션·번역·판정 근거·산출물·인계 자료 등 콘텐츠를 지운다.
    콘텐츠 없는 행위자·시각·대상 감사 기록만 남긴다. 오류 화면의 [중단](JOB-07, 입력 유지)과 다르다."""
    from app.flows.cancel import cancel_job as do_cancel

    r = do_cancel(db, job_id, seller_id)
    if r is None:
        raise HTTPException(status_code=404, detail="job not found")
    return r


# ── ANL-01 · JOB-05: Celery 큐 / 실제 DB ──────────────────────────
@router.post("/jobs/{job_id}/analyze", status_code=202, summary="API-ANL-01 분석 시작 (Celery 큐)")
def analyze(job_id: int, response: Response, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """분석 실행(대표 행 + ①②③ 시도)을 만들고 N2로 올린 뒤 큐에 보낸다. 이미 진행 중인 분석이 있으면 그 시도를 돌려준다.
    N1(최초) 또는 N2 오류([다시 시도])에서만 시작한다. 규제 분류 미선택은 시작하지 않는다(D8)."""
    from app.flows.analysis import StartRejected, start_analysis  # 지연 임포트(파이프라인 설정 로드)

    try:
        r = start_analysis(db, job_id, seller_id)
    except StartRejected as e:
        db.rollback()
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message}) from e
    response.status_code = 202
    return {"jobId": job_id, "accepted": True, "taskId": r["taskId"]}


@router.get("/jobs/{job_id}/tasks", summary="API-JOB-05 비동기 큐 상태 조회 (DB)")
def get_tasks(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    from app.flows.status import task_status

    job = db.execute(
        text("SELECT id, status, current_step, user_facing_status FROM job WHERE id = :id AND seller_id = :s"),
        {"id": job_id, "s": seller_id},
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return task_status(db, dict(job))


# ── JOB-06/07: 실제 DB (재시도·중단) ──────────────────────────────
@router.post("/jobs/{job_id}/tasks/{task_id}/retry", summary="API-JOB-06 실패 작업 재시도 (DB)")
def retry_task(job_id: int, task_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """실패한 현재 시도의 재시도는 새 시도 행으로 만든다(이전 행은 이력, D2). 응답의 taskId 는 새 시도다.
    계약: task_type ∈ {translate, render} ∧ failed ∧ 누적 retry_count < max_retry. 그 밖은 409."""
    from app import execution
    from app.flows import retry_plan
    from app.flows.status import USER_RETRY_TYPES

    if not db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first():
        raise HTTPException(status_code=404, detail="job not found")
    t = db.execute(
        text("SELECT * FROM job_async_task WHERE id = :t AND job_id = :j"),
        {"t": task_id, "j": job_id},
    ).mappings().first()
    if not t:
        raise HTTPException(status_code=404, detail="task not found")
    if t["execution_schema_version"] == 0:  # 0008 이전 레거시 행
        if t["status"] != "failed":
            raise HTTPException(status_code=409, detail="RETRY_NOT_ALLOWED")
        if t["retry_count"] >= t["max_retry"]:
            raise HTTPException(status_code=409, detail="RETRY_LIMIT_EXCEEDED")
        r = db.execute(
            text(
                "UPDATE job_async_task SET status='pending', retry_count = retry_count + 1, "
                "started_at = NULL, finished_at = NULL, error_code = NULL, error_message = NULL "
                "WHERE id = :t RETURNING id, status, retry_count"
            ),
            {"t": task_id},
        ).mappings().one()
        db.commit()
        return {"taskId": r["id"], "status": r["status"], "retryCount": r["retry_count"], "uiStatus": "retrying"}
    if t["parent_task_id"] is None or t["task_type"] not in USER_RETRY_TYPES:
        raise HTTPException(status_code=409, detail="RETRY_NOT_ALLOWED")
    try:
        plan = retry_plan(db, dict(t))
        new_id = execution.create_retry(db, task_id, "user", **plan)
        job = db.execute(text("SELECT status, current_step FROM job WHERE id = :j"), {"j": job_id}).mappings().one()
        if job["status"] == "failed" and job["current_step"] == "N4":  # N4 오류 화면 [다시 시도] → 진행 중으로
            from app.flows.common import set_job_state

            set_job_state(db, job_id, status="processing", step="N4", ufs="translating")
    except execution.RetryNotAllowed as e:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "RETRY_NOT_ALLOWED", "message": str(e)}) from e
    except execution.NotFound as e:
        db.rollback()
        raise HTTPException(status_code=404, detail="task not found") from e
    db.commit()
    execution.dispatch([new_id])
    n = db.execute(text("SELECT * FROM job_async_task WHERE id = :t"), {"t": new_id}).mappings().one()
    return {
        "taskId": n["id"], "taskType": n["task_type"], "unitType": n["unit_type"], "unitId": n["unit_id"],
        "status": n["status"], "uiStatus": "retrying", "retryCount": n["retry_count"], "maxRetry": n["max_retry"],
        "retryable": False, "errorCode": None, "revision": n["revision"],
    }


@router.post("/jobs/{job_id}/abort", summary="API-JOB-07 처리 중단·복귀 (DB)")
def abort_job(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """오류·처리 중 화면의 [중단](D9-3): N2 → N1, N4 → N3. 입력·버킷 선택은 유지한다(콘텐츠 삭제는 전체 취소 JOB-04).
    진행 중 실행은 대표 행 기준으로 원자적으로 중단하고, 이후 늦은 결과·재시도·후속 생성·화면 전이를 막는다."""
    from app import execution
    from app.flows.analysis import abort_analysis
    from app.flows.common import set_job_state

    if not db.execute(text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"), {"j": job_id, "s": seller_id}).first():
        raise HTTPException(status_code=404, detail="job not found")
    job = execution.lock_job(db, job_id)
    step = job["current_step"]
    if job["status"] == "archived" or step not in ("N2", "N4"):
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": f"중단할 수 없는 단계({step})"})
    if step == "N2":
        abort_analysis(db, job_id)
        return_to, status, ufs = "N1", "draft", "draft"
    else:
        from app.flows.downstream import abort_downstream

        abort_downstream(db, job_id)
        return_to, status, ufs = "N3", "review", "section_review"
    # 0008 이전 레거시 진행 행
    db.execute(
        text("UPDATE job_async_task SET status='cancelled', finished_at=now() "
             "WHERE job_id = :j AND execution_schema_version = 0 AND status IN ('pending', 'running')"),
        {"j": job_id},
    )
    set_job_state(db, job_id, status=status, step=return_to, ufs=ufs)
    db.commit()
    return {"returnTo": return_to, "jobId": job_id}
