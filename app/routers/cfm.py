"""CFM — 검수/텍스트 블록 (API-CFM-01~04, 🟢9월). 모두 mock 응답."""
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.schemas import Bbox, BlockRole, BlockStatus, CurrentStep, JobStatus

router = APIRouter(tags=["Review"])


class Signal(BaseModel):
    code: str = "prohibited_expression"
    reason: str = "규제 금지 표현"
    basisArticle: Optional[str] = "화장품법 제13조"
    evidenceUrl: Optional[str] = "https://example.com/evidence/1"
    taskId: Optional[str] = None
    retryable: Optional[bool] = None


class TextBlock(BaseModel):
    id: str = "blk-001"
    sectionId: str = "sec-001"
    displayTop: int = 0
    blockOrder: int = 1
    role: BlockRole = BlockRole.body
    isExcluded: bool = False
    sourceKo: str = "주름 개선에 효과적입니다"
    trans1: str = "Effective for wrinkle improvement"
    trans2: Optional[str] = None
    blockStatus: BlockStatus = BlockStatus.machine
    bbox: Bbox = Bbox()
    charCount: int = 33
    charLimit: Optional[int] = None
    overflow: bool = False
    autoAdjust: dict = {"fontScale": 1.0, "lineBreakApplied": False}
    signals: list[Signal] = [Signal()]
    alternativeExpression: Optional[str] = "Helps care for the look of wrinkles"
    revision: int = 1


class BlockUpdate(BaseModel):
    trans1: str = "Helps care for the look of wrinkles"
    revision: int = 1  # 낙관적 잠금 버전


@router.get("/jobs/{job_id}/blocks", response_model=list[TextBlock], summary="API-CFM-01 텍스트 블록 표(대조)")
def list_blocks(job_id: str, sectionId: Optional[str] = Query(default=None)):
    return [TextBlock(id="blk-001"), TextBlock(id="blk-002", sourceKo="촉촉한 사용감", trans1="Moisturizing feel", signals=[])]


@router.patch("/jobs/{job_id}/blocks/{block_id}", summary="API-CFM-02 번역문 셀 수정")
def update_block(job_id: str, block_id: str, body: BlockUpdate):
    block = TextBlock(id=block_id, trans1=body.trans1, blockStatus=BlockStatus.edited, revision=body.revision + 1)
    return {"block": block.model_dump(), "rerenderTaskId": "task-rerender-001"}


@router.get("/jobs/{job_id}/preview", summary="API-CFM-03 검수 뷰어 프리뷰(다폭)")
def preview(job_id: str):
    return {
        "previewWidth": 500,
        "maxOriginalWidth": 1000,
        "scale": 0.5,
        "previewHeight": 1500,
        "align": "left",
        "sections": [
            {
                "sectionId": "sec-001", "sourceImageId": "src-001", "width": 1000,
                "displayTop": 0, "bucket": "include",
                "originalUrl": "https://example-bucket.s3.amazonaws.com/src/sec-001.jpg?presigned=mock",
                "renderedUrl": "https://example-bucket.s3.amazonaws.com/render/sec-001.png?presigned=mock",
                "signals": [],
            }
        ],
    }


@router.post("/jobs/{job_id}/confirm", summary="API-CFM-04 검수 확정(N5→N6)")
def confirm(job_id: str):
    return {"jobId": job_id, "status": JobStatus.review, "currentStep": CurrentStep.N6}
