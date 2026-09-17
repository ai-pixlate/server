"""JOB — 작업 (API-JOB-02~07, 🟢9월) + ANL-01 분석 시작.

JOB-02(생성)·JOB-03(조회)·JOB-04(취소)는 실제 DB(job·job_keyword).
JOB-05/06/07·ANL-01 은 워커/오케스트레이터(Celery+Redis)가 필요해 아직 mock.
인증 도입 전이라 seller_id는 임시 상수(MOCK_SELLER_ID).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import CurrentStep, JobStatus, TaskStatus, TaskType, UserFacingStatus

router = APIRouter(tags=["Jobs"])

MOCK_SELLER_ID = 1  # 인증(Cognito) 도입 전 임시 셀러


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
def create_job(body: JobCreate, db: Session = Depends(get_db)):
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
            "s": MOCK_SELLER_ID, "brand": body.brandId, "pname": body.productName,
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
def get_job(job_id: int, db: Session = Depends(get_db)):
    r = db.execute(
        text(
            "SELECT id, status, current_step, user_facing_status, product_name, product_code, "
            "brand_id, target_country, regulatory_class, target_lang, spec_type, "
            "channel_spec_id, display_category_id, internal_category, is_saved, "
            "created_at, updated_at "
            "FROM job WHERE id = :id AND seller_id = :s"
        ),
        {"id": job_id, "s": MOCK_SELLER_ID},
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


@router.delete("/jobs/{job_id}", summary="API-JOB-04 작업 취소 (DB · archived)")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    r = db.execute(
        text(
            "UPDATE job SET status = 'archived', updated_at = now() "
            "WHERE id = :id AND seller_id = :s RETURNING id, status"
        ),
        {"id": job_id, "s": MOCK_SELLER_ID},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="job not found")
    db.commit()
    return {"jobId": r["id"], "status": r["status"]}


# ── JOB-05/06/07 · ANL-01: 워커/오케스트레이터 필요 → 아직 mock ──────
@router.get("/jobs/{job_id}/tasks", summary="API-JOB-05 비동기 큐 상태 조회 (mock·워커 예정)")
def get_tasks(job_id: int):
    return {
        "jobStatus": JobStatus.processing,
        "currentStep": CurrentStep.N2,
        "userFacingStatus": UserFacingStatus.analyzing,
        "progress": 0.5, "total": 2, "done": 1, "failedCount": 0,
        "stages": [
            {"key": "ocr", "label": "OCR", "status": "done"},
            {"key": "section", "label": "섹션 분해", "status": "running"},
        ],
        "items": [
            {"taskId": "task-001", "taskType": TaskType.ocr, "unitType": "source_image",
             "unitId": "src-001", "status": TaskStatus.done, "uiStatus": "done",
             "retryCount": 0, "maxRetry": 3, "retryable": False, "errorCode": None, "revision": 1},
        ],
    }


@router.post("/jobs/{job_id}/tasks/{task_id}/retry", summary="API-JOB-06 실패 작업 재시도 (mock·워커 예정)")
def retry_task(job_id: int, task_id: str):
    return {"taskId": task_id, "status": TaskStatus.pending, "retryCount": 1, "uiStatus": "retrying"}


@router.post("/jobs/{job_id}/abort", summary="API-JOB-07 처리 중단·복귀 (mock·워커 예정)")
def abort_job(job_id: int):
    return {"returnTo": "N1", "jobId": job_id}


@router.post("/jobs/{job_id}/analyze", status_code=202, summary="API-ANL-01 분석 시작 (mock·워커 예정)")
def analyze(job_id: int, response: Response):
    response.status_code = 202
    return {"jobId": job_id, "accepted": True}
