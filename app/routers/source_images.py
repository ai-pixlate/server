"""SRC — 원본 이미지 (API-SRC-01~04, 🟢9월). 실제 S3 + DB(source_image).

업로드·목록·재정렬 응답의 원본 이미지는 모두 계약 `SourceImage`(id·uploadOrder·imageType·fileUrl·width·height·createdAt)다.
fileUrl 은 응답할 때 발급하는 presigned 주소(5분)이고 DB 에는 S3 키만 둔다.
"""
import hashlib
import io
from typing import Any

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import s3
from app.db import get_db
from app.security import get_current_seller

router = APIRouter(tags=["SourceImages"])

_COLUMNS = "id, upload_order, image_type, file_url, width, height, created_at"


def _job_owned(db: Session, job_id: int, seller_id: int) -> bool:
    return db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first() is not None


def _source_image(r) -> dict[str, Any]:
    """계약 SourceImage."""
    return {
        "id": r["id"],
        "uploadOrder": r["upload_order"],
        "imageType": r["image_type"],
        "fileUrl": s3.presigned_get(r["file_url"]),  # 조회 시 presigned 발급
        "width": r["width"],
        "height": r["height"],
        "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
    }


def _list(db: Session, job_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        text(f"SELECT {_COLUMNS} FROM source_image WHERE job_id = :j ORDER BY upload_order, id"),
        {"j": job_id},
    ).mappings().all()
    return [_source_image(r) for r in rows]


def _reorder_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@router.get("/jobs/{job_id}/source-images", summary="API-SRC-01 원본 이미지 목록 (S3+DB)")
def list_source_images(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    if not _job_owned(db, job_id, seller_id):
        raise HTTPException(status_code=404, detail="job not found")
    return _list(db, job_id)


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
                "INSERT INTO source_image (job_id, upload_order, image_type, file_url, width, height, sha256) "
                f"VALUES (:j, :o, 'detail', :k, :w, :h, :sha) RETURNING {_COLUMNS}"
            ),
            # 분석 실행의 고정 입력(5.28): BE 가 받은 바이트로 직접 잰 해시
            {"j": job_id, "o": order, "k": key, "w": width, "h": height, "sha": hashlib.sha256(content).hexdigest()},
        ).mappings().one()
        results.append({"fileName": f.filename, "ok": True, "sourceImageId": row["id"], "errorCode": None})
        images.append(_source_image(row))

    db.commit()
    return {"results": results, "sourceImages": images}


@router.patch("/jobs/{job_id}/source-images/reorder", summary="API-SRC-03 원본 순서 재정렬 (DB)")
def reorder_source_images(job_id: int, raw: Any = Body(default=None), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """`order` = 그 작업의 현재 원본 이미지 id 전체를 정확히 한 번씩 담은 배열. 검증에 실패하면 아무것도 바꾸지 않는다.
    - 작업 없음·남의 작업, 이미지 id 가 없거나 다른 작업 소속: 404 NOT_FOUND
    - order 누락·타입 오류·중복·현재 이미지 일부 누락: 400 VALIDATION_ERROR
    작업 행을 잠근 한 트랜잭션에서 순서를 1..N 으로 다시 매기고, 바뀐 원본 이미지 배열을 돌려준다."""
    if db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s FOR UPDATE"), {"j": job_id, "s": seller_id}
    ).first() is None:
        db.rollback()
        raise _reorder_error(404, "NOT_FOUND", "job not found")
    order = raw.get("order") if isinstance(raw, dict) else None
    if not isinstance(order, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in order):
        db.rollback()
        raise _reorder_error(400, "VALIDATION_ERROR", "order must be an array of sourceImageId integers")
    if len(set(order)) != len(order):
        db.rollback()
        raise _reorder_error(400, "VALIDATION_ERROR", "order has duplicate ids")
    current = {r[0] for r in db.execute(text("SELECT id FROM source_image WHERE job_id = :j"), {"j": job_id}).all()}
    if set(order) - current:
        db.rollback()
        raise _reorder_error(404, "NOT_FOUND", "source image not found")
    if set(order) != current:
        db.rollback()
        raise _reorder_error(400, "VALIDATION_ERROR", "order must include every current source image exactly once")
    for i, sid in enumerate(order, start=1):
        db.execute(
            text("UPDATE source_image SET upload_order = :o WHERE id = :id AND job_id = :j"),
            {"o": i, "id": sid, "j": job_id},
        )
    images = _list(db, job_id)
    db.commit()
    return images


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
