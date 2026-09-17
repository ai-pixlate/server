"""JOB — 작업 (API-JOB-02~07, 🟢9월) + ANL-01 분석 시작. 모두 mock 응답."""
from typing import Optional

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.schemas import CurrentStep, JobStatus, TaskStatus, TaskType, UserFacingStatus

router = APIRouter(tags=["Jobs"])


class JobCreate(BaseModel):
    brandId: str = Field(examples=["brand-001"])
    productName: str = Field(examples=["수분 크림 50ml"])  # 필수
    productCode: Optional[str] = Field(default=None, examples=["SKU-1001"])
    targetCountry: str = Field(examples=["US"])
    regulatoryClass: str = Field(examples=["cosmetic"])
    targetLanguage: str = Field(examples=["en"])
    specType: str = Field(default="original", examples=["original"])
    channelSpecId: Optional[str] = None
    categoryId: str = Field(examples=["cat-face-serum"])
    keywords: list[str] = Field(default_factory=lambda: ["moisture", "cream"])
    consentId: str = Field(examples=["consent-001"])


class JobSummary(BaseModel):
    jobId: str = "job-001"
    status: JobStatus = JobStatus.draft
    userFacingStatus: UserFacingStatus = UserFacingStatus.draft
    currentStep: CurrentStep = CurrentStep.N1
    productName: str = "수분 크림 50ml"
    productCode: Optional[str] = "SKU-1001"


class JobDetail(JobSummary):
    brandId: str = "brand-001"
    targetCountry: str = "US"
    regulatoryClass: str = "cosmetic"
    targetLanguage: str = "en"
    specType: str = "original"
    channelSpecId: Optional[str] = None
    categoryId: str = "cat-face-serum"
    internalCategory: Optional[str] = "Face"
    keywords: list[str] = ["moisture", "cream"]
    isSaved: bool = False
    createdAt: str = "2026-09-16T00:00:00Z"
    updatedAt: str = "2026-09-16T00:00:00Z"


@router.post("/jobs", response_model=JobSummary, status_code=201, summary="API-JOB-02 작업 생성(draft) = N1 완료")
def create_job(body: JobCreate):
    return JobSummary(productName=body.productName, productCode=body.productCode)


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="API-JOB-03 작업 조회(상태+N1 입력값)")
def get_job(job_id: str):
    return JobDetail(jobId=job_id)


@router.delete("/jobs/{job_id}", summary="API-JOB-04 작업 취소")
def cancel_job(job_id: str):
    return {"jobId": job_id, "status": JobStatus.archived}


@router.get("/jobs/{job_id}/tasks", summary="API-JOB-05 비동기 큐 상태 조회")
def get_tasks(job_id: str):
    return {
        "jobStatus": JobStatus.processing,
        "currentStep": CurrentStep.N2,
        "userFacingStatus": UserFacingStatus.analyzing,
        "progress": 0.5,
        "total": 2,
        "done": 1,
        "failedCount": 0,
        "stages": [
            {"key": "ocr", "label": "OCR", "status": "done"},
            {"key": "section", "label": "섹션 분해", "status": "running"},
        ],
        "items": [
            {
                "taskId": "task-001", "taskType": TaskType.ocr, "unitType": "source_image",
                "unitId": "src-001", "status": TaskStatus.done, "uiStatus": "done",
                "retryCount": 0, "maxRetry": 3, "retryable": False, "errorCode": None, "revision": 1,
            },
            {
                "taskId": "task-002", "taskType": TaskType.section, "unitType": "job",
                "unitId": "job-001", "status": TaskStatus.running, "uiStatus": "running",
                "retryCount": 0, "maxRetry": 3, "retryable": False, "errorCode": None, "revision": 1,
            },
        ],
    }


@router.post("/jobs/{job_id}/tasks/{task_id}/retry", summary="API-JOB-06 실패 작업 재시도")
def retry_task(job_id: str, task_id: str):
    return {"taskId": task_id, "status": TaskStatus.pending, "retryCount": 1, "uiStatus": "retrying"}


@router.post("/jobs/{job_id}/abort", summary="API-JOB-07 처리 중단·직전 화면 복귀")
def abort_job(job_id: str):
    return {"returnTo": "N1", "job": JobDetail(jobId=job_id).model_dump()}


@router.post("/jobs/{job_id}/analyze", status_code=202, summary="API-ANL-01 분석 시작(OCR→분해→판정)")
def analyze(job_id: str, response: Response):
    response.status_code = 202
    return {"jobId": job_id, "accepted": True}
