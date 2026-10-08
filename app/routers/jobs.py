"""JOB — 작업 (API-JOB-02~07, 🟢9월) + ANL-01 분석 시작.

JOB-02~07·ANL-01 실제 DB/Celery. seller_id는 Bearer 토큰에서 추출(get_current_seller).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import get_current_seller

router = APIRouter(tags=["Jobs"])


class JobCreate(BaseModel):
    brandId: int = Field(examples=[1])
    productName: str = Field(examples=["수분 크림 50ml"])  # 필수
    productCode: Optional[str] = Field(default=None, examples=["SKU-1001"])
    targetCountry: Optional[str] = Field(default="US", examples=["US"])
    regulatoryClass: Optional[str] = Field(default=None, examples=["cosmetic"])
    targetLanguage: Optional[str] = Field(default="en", examples=["en"])
    specType: str = Field(default="original", examples=["original"])
    channelSpecId: Optional[int] = None
    categoryId: Optional[int] = None
    keywords: list[str] = Field(default_factory=list, examples=[["moisture", "cream"]])
    consentId: Optional[str] = None  # consent 연동은 추후(현재 미저장)


# ── JOB-02·03·04: 실제 DB ─────────────────────────────────────────
@router.post("/jobs", status_code=201, summary="API-JOB-02 작업 생성(draft) = N1 완료 (DB)")
def create_job(body: JobCreate, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    if not body.productName.strip():
        raise HTTPException(status_code=400, detail="productName is required")

    row = db.execute(
        text(
            "INSERT INTO job (seller_id, brand_id, product_name, product_code, "
            "target_country, regulatory_class, target_lang, spec_type, "
            "channel_spec_id, display_category_id, user_facing_status) "
            "VALUES (:s, :brand, :pname, :pcode, :country, :regclass, :lang, :spec, "
            ":chspec, :cat, 'draft') "
            "RETURNING id, status, current_step, user_facing_status, product_name, product_code"
        ),
        {
            "s": seller_id, "brand": body.brandId, "pname": body.productName,
            "pcode": body.productCode, "country": body.targetCountry,
            "regclass": body.regulatoryClass, "lang": body.targetLanguage,
            "spec": body.specType, "chspec": body.channelSpecId, "cat": body.categoryId,
        },
    ).mappings().one()

    job_id = row["id"]
    for i, kw in enumerate(body.keywords, start=1):
        db.execute(
            text("INSERT INTO job_keyword (job_id, keyword, order_no) VALUES (:j, :k, :o)"),
            {"j": job_id, "k": kw, "o": i},
        )
    db.commit()

    return {
        "jobId": job_id,
        "status": row["status"],
        "userFacingStatus": row["user_facing_status"],
        "currentStep": row["current_step"],
        "productName": row["product_name"],
        "productCode": row["product_code"],
    }


@router.get("/jobs/{job_id}", summary="API-JOB-03 작업 조회(상태+N1 입력값) (DB)")
def get_job(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    r = db.execute(
        text(
            "SELECT id, status, current_step, user_facing_status, product_name, product_code, "
            "brand_id, target_country, regulatory_class, target_lang, spec_type, "
            "channel_spec_id, display_category_id, internal_category, is_saved, "
            "created_at, updated_at "
            "FROM job WHERE id = :id AND seller_id = :s"
        ),
        {"id": job_id, "s": seller_id},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="job not found")

    keywords = [
        kw["keyword"]
        for kw in db.execute(
            text("SELECT keyword FROM job_keyword WHERE job_id = :id ORDER BY order_no"),
            {"id": job_id},
        ).mappings().all()
    ]

    return {
        "jobId": r["id"],
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
        "isSaved": r["is_saved"],
        "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
        "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


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
