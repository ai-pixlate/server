"""BRD — 브랜드/로고 (API-BRD-01~07, 🟢9월).

BRD-01~04(브랜드 CRUD)는 실제 DB(brand 테이블). BRD-05~07(로고)은 S3가 필요해
아직 mock. 인증(Cognito) 도입 전이라 seller_id는 임시 상수(MOCK_SELLER_ID)를 쓴다.
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import s3
from app.db import get_db
from app.schemas import LogoFormat

router = APIRouter(tags=["Brands"])

MOCK_SELLER_ID = 1  # 인증 도입 전 임시 셀러 (Cognito 연동 시 토큰에서 추출)

_BRAND_COLS = "id, name_ko, name_en, brand_overview, core_audience, created_at, updated_at"


def _to_brand(r) -> dict:
    return {
        "id": r["id"],
        "nameKo": r["name_ko"],
        "nameEn": r["name_en"],
        "overview": r["brand_overview"],
        "targetCustomer": r["core_audience"],
        "createdAt": r["created_at"].isoformat() if r["created_at"] else None,
        "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


class BrandCreate(BaseModel):
    nameKo: str = Field(examples=["픽스에이트"])
    nameEn: str = Field(examples=["Pixate"])  # 필수 (누락 시 422)
    overview: Optional[str] = None
    targetCustomer: Optional[str] = None


class BrandUpdate(BaseModel):
    nameKo: Optional[str] = None
    nameEn: Optional[str] = None
    overview: Optional[str] = None
    targetCustomer: Optional[str] = None


# ── BRD-01~04: 실제 DB ────────────────────────────────────────────
@router.get("/brands", summary="API-BRD-01 브랜드 목록 조회 (DB)")
def list_brands(db: Session = Depends(get_db)):
    rows = db.execute(
        text(f"SELECT {_BRAND_COLS} FROM brand WHERE seller_id = :s ORDER BY id"),
        {"s": MOCK_SELLER_ID},
    ).mappings().all()
    return [_to_brand(r) for r in rows]


@router.post("/brands", status_code=201, summary="API-BRD-02 브랜드 등록 (DB)")
def create_brand(body: BrandCreate, db: Session = Depends(get_db)):
    if not body.nameEn.strip():
        raise HTTPException(status_code=400, detail="nameEn is required")
    r = db.execute(
        text(
            "INSERT INTO brand (seller_id, name_ko, name_en, brand_overview, core_audience) "
            "VALUES (:s, :ko, :en, :ov, :tc) "
            f"RETURNING {_BRAND_COLS}"
        ),
        {"s": MOCK_SELLER_ID, "ko": body.nameKo, "en": body.nameEn,
         "ov": body.overview, "tc": body.targetCustomer},
    ).mappings().one()
    db.commit()
    return _to_brand(r)


@router.get("/brands/{brand_id}", summary="API-BRD-03 브랜드 상세 조회 (DB)")
def get_brand(brand_id: int, db: Session = Depends(get_db)):
    r = db.execute(
        text(f"SELECT {_BRAND_COLS} FROM brand WHERE id = :id AND seller_id = :s"),
        {"id": brand_id, "s": MOCK_SELLER_ID},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="brand not found")
    return _to_brand(r)


@router.patch("/brands/{brand_id}", summary="API-BRD-04 브랜드 정보 수정 (DB)")
def update_brand(brand_id: int, body: BrandUpdate, db: Session = Depends(get_db)):
    # nameEn 을 빈값/null 로 바꾸는 요청은 차단 (규격 F-BRD-07)
    if body.nameEn is not None and not body.nameEn.strip():
        raise HTTPException(status_code=400, detail="nameEn must not be empty")

    fields: dict = {}
    if body.nameKo is not None:
        fields["name_ko"] = body.nameKo
    if body.nameEn is not None:
        fields["name_en"] = body.nameEn
    if body.overview is not None:
        fields["brand_overview"] = body.overview
    if body.targetCustomer is not None:
        fields["core_audience"] = body.targetCustomer

    if not fields:  # 바꿀 게 없으면 현재 값 반환
        return get_brand(brand_id, db)

    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params = {**fields, "id": brand_id, "s": MOCK_SELLER_ID}
    r = db.execute(
        text(
            f"UPDATE brand SET {set_clause}, updated_at = now() "
            f"WHERE id = :id AND seller_id = :s RETURNING {_BRAND_COLS}"
        ),
        params,
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="brand not found")
    db.commit()
    return _to_brand(r)


# ── BRD-05~07: 로고 (실제 S3 + DB) ────────────────────────────────
@router.post("/brands/{brand_id}/logos", status_code=201, summary="API-BRD-05 브랜드 로고 추가 (S3+DB)")
def add_logo(brand_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    fmt = "png"
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext in LogoFormat._value2member_map_:
            fmt = ext

    base_order = db.execute(
        text("SELECT COALESCE(MAX(order_no), 0) FROM brand_logo WHERE brand_id = :b"),
        {"b": brand_id},
    ).scalar()
    order_no = int(base_order or 0) + 1

    key = s3.make_key(f"logos/{brand_id}", file.filename)
    s3.upload_fileobj(file.file, key, content_type=file.content_type)

    row = db.execute(
        text(
            "INSERT INTO brand_logo (brand_id, logo_key, format, order_no) "
            "VALUES (:b, :k, :f, :o) RETURNING id"
        ),
        {"b": brand_id, "k": key, "f": fmt, "o": order_no},
    ).mappings().one()
    db.commit()
    return {"id": row["id"], "format": fmt, "orderNo": order_no, "logoUrl": s3.presigned_get(key)}


@router.get("/brands/{brand_id}/logos", summary="API-BRD-06 브랜드 로고 목록 (S3+DB)")
def list_logos(brand_id: int, db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT id, logo_key, format, order_no FROM brand_logo WHERE brand_id = :b ORDER BY order_no"),
        {"b": brand_id},
    ).mappings().all()
    return [
        {"id": r["id"], "format": r["format"], "orderNo": r["order_no"], "logoUrl": s3.presigned_get(r["logo_key"])}
        for r in rows
    ]


@router.delete("/brands/{brand_id}/logos/{logo_id}", status_code=204, summary="API-BRD-07 브랜드 로고 삭제 (S3+DB)")
def delete_logo(brand_id: int, logo_id: int, db: Session = Depends(get_db)):
    r = db.execute(
        text("SELECT logo_key FROM brand_logo WHERE id = :id AND brand_id = :b"),
        {"id": logo_id, "b": brand_id},
    ).mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="logo not found")
    db.execute(text("DELETE FROM brand_logo WHERE id = :id"), {"id": logo_id})
    db.commit()
    s3.delete_object(r["logo_key"])
    return None
