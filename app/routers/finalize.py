"""FIN — 최종 산출물 (API-FIN-01~06, 🟢9월).

FIN-01(렌더 큐)·FIN-02(산출물 목록)·FIN-03(검증)·FIN-04(묶음=content.csv 생성)·
FIN-05(다운로드 presigned)·FIN-06(저장) 실제 DB/S3.
"""
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import s3
from app.db import get_db

router = APIRouter(tags=["Finalize"])

MOCK_SELLER_ID = 1


class ExportRequest(BaseModel):
    components: list[str] = ["content.csv"]


def _job_owned(db: Session, job_id: int) -> bool:
    return db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": MOCK_SELLER_ID},
    ).first() is not None


# ── FIN-01·02: Celery 큐 / 실제 DB ────────────────────────────────
@router.post("/jobs/{job_id}/render", status_code=202, summary="API-FIN-01 최종 이미지 렌더링 (Celery 큐)")
def render(job_id: int, response: Response, db: Session = Depends(get_db)):
    if not _job_owned(db, job_id):
        raise HTTPException(status_code=404, detail="job not found")
    from app.tasks import run_render  # 지연 임포트
    result = run_render.delay(job_id)
    response.status_code = 202
    return {"jobId": job_id, "accepted": True, "renderTaskId": result.id}


@router.get("/jobs/{job_id}/deliverables", summary="API-FIN-02 산출물 목록 (DB)")
def deliverables(job_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT id, usage_type, image_url, render_status FROM deliverable "
            "WHERE job_id = :j ORDER BY id"
        ),
        {"j": job_id},
    ).mappings().all()
    return {
        "deliverables": [
            {
                "id": r["id"], "usageType": r["usage_type"],
                "imageUrl": s3.presigned_get(r["image_url"]), "renderStatus": r["render_status"],
            }
            for r in rows
        ]
    }


# ── FIN-03·04·05·06: 실제 DB / S3 ─────────────────────────────────
@router.get("/jobs/{job_id}/validation", summary="API-FIN-03 규격 검증 상세 (DB)")
def validation(job_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT id, usage_type, validation_result FROM deliverable "
            "WHERE job_id = :j ORDER BY id"
        ),
        {"j": job_id},
    ).mappings().all()
    # validation_result(JSONB)는 렌더 엔진이 채운다. 없으면 빈 결과.
    return {
        "deliverables": [
            {"id": r["id"], "usageType": r["usage_type"], "validationResult": r["validation_result"]}
            for r in rows
        ]
    }


@router.post("/jobs/{job_id}/export", status_code=201, summary="API-FIN-04 산출물 묶음 생성(content.csv → S3)")
def export(job_id: int, body: ExportRequest, db: Session = Depends(get_db)):
    if not _job_owned(db, job_id):
        raise HTTPException(status_code=404, detail="job not found")

    blocks = db.execute(
        text(
            "SELECT tb.section_id, tb.block_order, tb.role, tb.source_ko, tb.trans_1 "
            "FROM text_block tb JOIN section s ON s.id = tb.section_id "
            "WHERE s.job_id = :j ORDER BY tb.section_id, tb.block_order, tb.id"
        ),
        {"j": job_id},
    ).mappings().all()

    # content.csv 생성 (엑셀 한글 대비 utf-8-sig)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sectionId", "blockOrder", "role", "sourceKo", "trans1"])
    for b in blocks:
        w.writerow([b["section_id"], b["block_order"], b["role"], b["source_ko"] or "", b["trans_1"] or ""])
    data = buf.getvalue().encode("utf-8-sig")

    key = s3.make_key(f"export/{job_id}", "content.csv")
    s3.upload_fileobj(io.BytesIO(data), key, content_type="text/csv")

    row = db.execute(
        text(
            "INSERT INTO export_artifact (job_id, artifact_type, file_url, is_distributable, is_generated) "
            "VALUES (:j, 'csv', :k, true, true) RETURNING id"
        ),
        {"j": job_id, "k": key},
    ).mappings().one()
    db.commit()
    return {"artifactId": row["id"], "artifactType": "csv", "rows": len(blocks)}


@router.get("/jobs/{job_id}/exports/{artifact_id}/download", summary="API-FIN-05 산출물 다운로드(presigned) (DB+S3)")
def download(job_id: int, artifact_id: int, db: Session = Depends(get_db)):
    r = db.execute(
        text(
            "SELECT file_url, artifact_type FROM export_artifact "
            "WHERE id = :a AND job_id = :j AND is_distributable = true AND is_generated = true"
        ),
        {"a": artifact_id, "j": job_id},
    ).mappings().first()
    if not r or not r["file_url"]:
        raise HTTPException(status_code=404, detail="artifact not found")
    return {
        "url": s3.presigned_get(r["file_url"]),
        "fileName": f"pixlate_export_{job_id}.{r['artifact_type']}",
        "expiresIn": s3.PRESIGN_TTL,
    }


@router.post("/jobs/{job_id}/save", summary="API-FIN-06 저장(보관함) (DB)")
def save(job_id: int, db: Session = Depends(get_db)):
    r = db.execute(
        text(
            "UPDATE job SET is_saved = true, saved_at = now(), status = 'done', "
            "user_facing_status = 'done', updated_at = now() "
            "WHERE id = :j AND seller_id = :s RETURNING id, is_saved, saved_at"
        ),
        {"j": job_id, "s": MOCK_SELLER_ID},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="job not found")
    db.commit()
    return {
        "jobId": r["id"], "status": "done", "isSaved": r["is_saved"],
        "savedAt": r["saved_at"].isoformat() if r["saved_at"] else None,
    }
