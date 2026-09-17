"""SRC — 원본 이미지 (API-SRC-01~04, 🟢9월). 모두 mock 응답."""
from fastapi import APIRouter, File, UploadFile
from pydantic import BaseModel

router = APIRouter(tags=["SourceImages"])


class SourceImage(BaseModel):
    id: str = "src-001"
    fileName: str = "detail_01.jpg"
    orderNo: int = 1
    width: int = 1000
    height: int = 3000
    url: str = "https://example-bucket.s3.amazonaws.com/src/src-001.jpg?presigned=mock"


class ReorderRequest(BaseModel):
    orderedIds: list[str] = ["src-002", "src-001"]


@router.get("/jobs/{job_id}/source-images", response_model=list[SourceImage], summary="API-SRC-01 원본 이미지 목록")
def list_source_images(job_id: str):
    return [SourceImage(id="src-001", orderNo=1), SourceImage(id="src-002", fileName="detail_02.jpg", orderNo=2)]


@router.post("/jobs/{job_id}/source-images", summary="API-SRC-02 원본 이미지 업로드(다중)")
def upload_source_images(job_id: str, files: list[UploadFile] = File(...)):
    results = []
    images = []
    for i, f in enumerate(files, start=1):
        sid = f"src-{i:03d}"
        results.append({"fileName": f.filename, "ok": True, "sourceImageId": sid, "errorCode": None})
        images.append(SourceImage(id=sid, fileName=f.filename or f"image_{i}.jpg", orderNo=i).model_dump())
    return {"results": results, "sourceImages": images}


@router.patch("/jobs/{job_id}/source-images/reorder", summary="API-SRC-03 원본 순서 재정렬")
def reorder_source_images(job_id: str, body: ReorderRequest):
    return {"orderedIds": body.orderedIds, "success": True}


@router.delete("/jobs/{job_id}/source-images/{image_id}", status_code=204, summary="API-SRC-04 원본 이미지 삭제")
def delete_source_image(job_id: str, image_id: str):
    return None
