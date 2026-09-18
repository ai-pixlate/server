"""FIN — 최종 산출물 (API-FIN-01~06, 🟢9월). 모두 mock 응답."""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import ArtifactType, JobStatus

router = APIRouter(tags=["Finalize"])

MOCK_SELLER_ID = 1


class ExportRequest(BaseModel):
    components: list[str] = ["images/", "content.csv", "html"]


# ── FIN-01·02: Celery 큐 / 실제 DB ────────────────────────────────
@router.post("/jobs/{job_id}/render", status_code=202, summary="API-FIN-01 최종 이미지 렌더링 (Celery 큐)")
def render(job_id: int, response: Response, db: Session = Depends(get_db)):
    job = db.execute(
        text("SELECT id FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": MOCK_SELLER_ID},
    ).mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    from app.tasks import run_render  # 지연 임포트
    result = run_render.delay(job_id)  # ← 렌더 태스크 큐 등록
    response.status_code = 202
    return {"jobId": job_id, "accepted": True, "renderTaskId": result.id}


@router.get("/jobs/{job_id}/deliverables", summary="API-FIN-02 산출물 목록 (DB)")
def deliverables(job_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT id, usage_type, image_url, render_status, created_at "
            "FROM deliverable WHERE job_id = :j ORDER BY id"
        ),
        {"j": job_id},
    ).mappings().all()
    return {
        "deliverables": [
            {
                "id": r["id"],
                "usageType": r["usage_type"],
                "imageUrl": r["image_url"],
                "renderStatus": r["render_status"],
            }
            for r in rows
        ]
    }


@router.get("/jobs/{job_id}/validation", summary="API-FIN-03 규격 검증 상세")
def validation(job_id: str):
    return {
        "results": [
            {"rule": "max_width", "scope": "detail", "severity": "error", "passed": True,
             "expected": 1000, "actual": 1000},
            {"rule": "max_file_size", "scope": "detail", "severity": "warning", "passed": True,
             "expected": "20MB", "actual": "8MB"},
        ]
    }


@router.post("/jobs/{job_id}/export", status_code=201, summary="API-FIN-04 산출물 묶음 생성")
def export(job_id: str, body: ExportRequest):
    return {
        "artifactId": "art-zip-001",
        "artifactType": ArtifactType.zip,
        "components": body.components,
    }


@router.get("/jobs/{job_id}/exports/{artifact_id}/download", summary="API-FIN-05 산출물 다운로드(presigned)")
def download(job_id: str, artifact_id: str):
    return {
        "url": f"https://example-bucket.s3.amazonaws.com/export/{artifact_id}.zip?presigned=mock",
        "fileName": "pixlate_export.zip",
        "expiresAt": "2026-09-16T00:05:00Z",
    }


@router.post("/jobs/{job_id}/save", summary="API-FIN-06 저장(보관함 카드 생성)")
def save(job_id: str):
    return {
        "jobId": job_id,
        "status": JobStatus.done,
        "isSaved": True,
        "savedAt": "2026-09-16T00:00:00Z",
    }
