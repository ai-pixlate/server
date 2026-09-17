"""FIN — 최종 산출물 (API-FIN-01~06, 🟢9월). 모두 mock 응답."""
from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.schemas import ArtifactType, JobStatus

router = APIRouter(tags=["Finalize"])


class ExportRequest(BaseModel):
    components: list[str] = ["images/", "content.csv", "html"]


@router.post("/jobs/{job_id}/render", status_code=202, summary="API-FIN-01 최종 이미지 렌더링(N6 진입 시 호출)")
def render(job_id: str, response: Response):
    response.status_code = 202
    return {"renderTaskId": "task-render-001"}


@router.get("/jobs/{job_id}/deliverables", summary="API-FIN-02 산출물 목록+검증+구성요소 상태")
def deliverables(job_id: str):
    return {
        "deliverables": [
            {
                "id": "dlv-001", "usageType": "detail", "renderStatus": "done",
                "imageUrl": "https://example-bucket.s3.amazonaws.com/deliverable/dlv-001.png?presigned=mock",
            }
        ],
        "validationSummary": {"passed": True, "errorCount": 0, "warningCount": 1},
        "components": [
            {"artifactId": "art-img", "type": "images", "status": "generated", "isGenerated": True,
             "isActive": True, "failedCount": 0, "retryAction": None},
            {"artifactId": "art-csv", "type": "csv", "status": "generated", "isGenerated": True,
             "isActive": True, "failedCount": 0, "retryAction": None},
        ],
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
        "fileName": "pixate_export.zip",
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
