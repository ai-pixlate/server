"""LIB — 보관함 (API-LIB-01, 🟢9월). 실제 DB(job.is_saved 투영)."""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter(tags=["Library"])

MOCK_SELLER_ID = 1


@router.get("/library", summary="API-LIB-01 보관함 카드 목록 (DB)")
def library(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT j.id, j.product_name, b.name_en AS brand_name, j.target_country, "
            "j.target_lang, j.spec_type, j.saved_at "
            "FROM job j LEFT JOIN brand b ON b.id = j.brand_id "
            "WHERE j.seller_id = :s AND j.is_saved = true "
            "ORDER BY j.saved_at DESC NULLS LAST, j.id DESC"
        ),
        {"s": MOCK_SELLER_ID},
    ).mappings().all()
    return [
        {
            "jobId": r["id"],
            "productName": r["product_name"],
            "brandName": r["brand_name"],
            "targetCountry": r["target_country"],
            "targetLanguage": r["target_lang"],
            "specType": r["spec_type"],
            "thumbnailUrl": None,  # 대표 썸네일(deliverable)은 렌더 단계 이후 연결
            "savedAt": r["saved_at"].isoformat() if r["saved_at"] else None,
        }
        for r in rows
    ]
