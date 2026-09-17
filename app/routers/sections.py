"""SEC — 섹션 (API-SEC-01~04, 🟢9월) + INP-01 인페인팅 조회. 모두 mock 응답."""
from typing import Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.schemas import CurrentStep, JobStatus, SectionBucket, VerdictStatus

router = APIRouter(tags=["Sections"])


class Verdict(BaseModel):
    verdictStatus: VerdictStatus = VerdictStatus.regulated
    verdictType: str = "regulatory"
    basisArticle: Optional[str] = "화장품법 제13조"
    evidenceUrl: Optional[str] = "https://example.com/evidence/1"


class Section(BaseModel):
    id: str = "sec-001"
    bucket: SectionBucket = SectionBucket.include
    exclusionReason: Optional[str] = None
    excludedStage: Optional[str] = None
    warningBadge: Optional[str] = None
    verdicts: list[Verdict] = [Verdict()]


class SectionAction(BaseModel):
    action: Literal["restore", "exclude"] = "exclude"


@router.get("/jobs/{job_id}/sections", summary="API-SEC-01 섹션 목록(버킷·배지)")
def list_sections(job_id: str, bucket: Optional[SectionBucket] = Query(default=None)):
    return {
        "include": [Section(id="sec-001", bucket=SectionBucket.include).model_dump()],
        "exclude": [
            Section(
                id="sec-002", bucket=SectionBucket.exclude,
                exclusionReason="auto_regulatory", excludedStage="N3",
            ).model_dump()
        ],
    }


@router.get("/jobs/{job_id}/sections/{section_id}", response_model=Section, summary="API-SEC-02 섹션 상세(판정 근거)")
def get_section(job_id: str, section_id: str):
    return Section(id=section_id)


@router.patch("/jobs/{job_id}/sections/{section_id}", response_model=Section, summary="API-SEC-03 섹션 되살리기/제외(N3·N5 공용)")
def update_section(job_id: str, section_id: str, body: SectionAction):
    if body.action == "exclude":
        return Section(id=section_id, bucket=SectionBucket.exclude, exclusionReason="user_manual", excludedStage="N5")
    return Section(id=section_id, bucket=SectionBucket.include, exclusionReason="restored_by_user")


@router.post("/jobs/{job_id}/sections/proceed", status_code=202, summary="API-SEC-04 이대로 진행(N3→N4)")
def proceed(job_id: str):
    return {"jobId": job_id, "status": JobStatus.processing, "currentStep": CurrentStep.N4, "accepted": True}


@router.get("/jobs/{job_id}/sections/{section_id}/inpaint", tags=["Inpaint"], summary="API-INP-01 섹션 인페인팅 결과 조회")
def get_inpaint(job_id: str, section_id: str):
    return {
        "sectionId": section_id,
        "inpaintStatus": "done",
        "residualRatio": 0.02,
        "warningBadge": None,
        "inpaintImageUrl": "https://example-bucket.s3.amazonaws.com/inpaint/sec-001.png?presigned=mock",
    }
