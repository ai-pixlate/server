"""SRC — 원본 이미지 (API-SRC-01~04, 🟢9월). 실제 S3 + DB(source_image)."""
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import s3
from app.db import get_db
from app.security import get_current_seller

router = APIRouter(tags=["SourceImages"])


class ReorderRequest(BaseModel):
    orderedIds: list[int] = [2, 1]


def _job_owned(db: Session, job_id: int, seller_id: int) -> bool:
    return db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first() is not None


@router.get("/jobs/{job_id}/source-images", summary="API-SRC-01 원본 이미지 목록 (S3+DB)")
def list_source_images(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    if not _job_owned(db, job_id, seller_id):
        raise HTTPException(status_code=404, detail="job not found")
    rows = db.execute(
        text(
            "SELECT id, upload_order, image_type, file_url, width, height "
            "FROM source_image WHERE job_id = :j ORDER BY upload_order, id"
        ),
        {"j": job_id},
    ).mappings().all()
    return [
        {
            "id": r["id"],
            "uploadOrder": r["upload_order"],
            "imageType": r["image_type"],
            "width": r["width"],
            "height": r["height"],
            "url": s3.presigned_get(r["file_url"]),  # 조회 시 presigned 발급
        }
        for r in rows
    ]


@router.post("/jobs/{job_id}/source-images", summary="API-SRC-02 원본 이미지 업로드(다중) (S3+DB)")
def upload_source_images(job_id: int, files: list[UploadFile] = File(...), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    if not _job_owned(db, job_id, seller_id):
        raise HTTPException(status_code=404, detail="job not found")

    base_order = db.execute(
        text("SELECT COALESCE(MAX(upload_order), 0) FROM source_image WHERE job_id = :j"),
        {"j": job_id},
    ).scalar()

    results = []
    images = []
    order = int(base_order or 0)
    for f in files:
        content = f.file.read()
        # 서버가 직접 이미지 치수 측정 (클라이언트 보고값 미신뢰)
        try:
            with Image.open(io.BytesIO(content)) as im:
                width, height = im.size
        except Exception:
            results.append({"fileName": f.filename, "ok": False, "errorCode": "INVALID_IMAGE"})
            continue

        order += 1
        key = s3.make_key(f"source/{job_id}", f.filename)
        s3.upload_fileobj(io.BytesIO(content), key, content_type=f.content_type)

        row = db.execute(
            text(
                "INSERT INTO source_image (job_id, upload_order, image_type, file_url, width, height) "
                "VALUES (:j, :o, 'detail', :k, :w, :h) RETURNING id"
            ),
            {"j": job_id, "o": order, "k": key, "w": width, "h": height},
        ).mappings().one()
        sid = row["id"]
        results.append({"fileName": f.filename, "ok": True, "sourceImageId": sid, "errorCode": None})
        images.append({
            "id": sid, "uploadOrder": order, "width": width, "height": height,
            "url": s3.presigned_get(key),
        })

    db.commit()
    return {"results": results, "sourceImages": images}


@router.patch("/jobs/{job_id}/source-images/reorder", summary="API-SRC-03 원본 순서 재정렬 (DB)")
def reorder_source_images(job_id: int, body: ReorderRequest, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    if not _job_owned(db, job_id, seller_id):
        raise HTTPException(status_code=404, detail="job not found")
    for i, sid in enumerate(body.orderedIds, start=1):
        db.execute(
            text("UPDATE source_image SET upload_order = :o WHERE id = :id AND job_id = :j"),
            {"o": i, "id": sid, "j": job_id},
        )
    db.commit()
    return {"orderedIds": body.orderedIds, "success": True}


@router.delete("/jobs/{job_id}/source-images/{image_id}", status_code=204, summary="API-SRC-04 원본 이미지 삭제 (S3+DB)")
def delete_source_image(job_id: int, image_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    if not _job_owned(db, job_id, seller_id):
        raise HTTPException(status_code=404, detail="job not found")
    r = db.execute(
        text("SELECT file_url FROM source_image WHERE id = :id AND job_id = :j"),
        {"id": image_id, "j": job_id},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="source image not found")
    db.execute(text("DELETE FROM source_image WHERE id = :id"), {"id": image_id})
    db.commit()
    s3.delete_object(r["file_url"])  # S3 오브젝트도 삭제
    return None
