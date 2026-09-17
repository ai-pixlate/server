"""BRD — 브랜드/로고 (API-BRD-01~07, 🟢9월). 모두 mock 응답."""
from typing import Optional

from fastapi import APIRouter, UploadFile, File
from pydantic import BaseModel, Field

from app.schemas import LogoFormat

router = APIRouter(tags=["Brands"])


class Brand(BaseModel):
    id: str = "brand-001"
    nameKo: str = "픽스에이트"
    nameEn: str = "Pixate"
    overview: Optional[str] = "AI 상세페이지 로컬라이제이션 브랜드"
    targetCustomer: Optional[str] = "글로벌 이커머스 셀러"


class BrandCreate(BaseModel):
    nameKo: str = Field(examples=["픽스에이트"])
    nameEn: str = Field(examples=["Pixate"])  # 필수 (400 if null)
    overview: Optional[str] = None
    targetCustomer: Optional[str] = None


class BrandUpdate(BaseModel):
    nameKo: Optional[str] = None
    nameEn: Optional[str] = None  # null·빈값이면 실제로는 400
    overview: Optional[str] = None
    targetCustomer: Optional[str] = None


class Logo(BaseModel):
    id: str = "logo-001"
    format: LogoFormat = LogoFormat.png
    orderNo: int = 1
    logoUrl: str = "https://example-bucket.s3.amazonaws.com/logos/logo-001.png?presigned=mock"


@router.get("/brands", response_model=list[Brand], summary="API-BRD-01 브랜드 목록 조회")
def list_brands():
    return [Brand()]


@router.post("/brands", response_model=Brand, status_code=201, summary="API-BRD-02 브랜드 등록")
def create_brand(body: BrandCreate):
    return Brand(nameKo=body.nameKo, nameEn=body.nameEn, overview=body.overview, targetCustomer=body.targetCustomer)


@router.get("/brands/{brand_id}", response_model=Brand, summary="API-BRD-03 브랜드 상세 조회")
def get_brand(brand_id: str):
    return Brand(id=brand_id)


@router.patch("/brands/{brand_id}", response_model=Brand, summary="API-BRD-04 브랜드 정보 수정")
def update_brand(brand_id: str, body: BrandUpdate):
    base = Brand(id=brand_id)
    data = base.model_dump()
    data.update({k: v for k, v in body.model_dump().items() if v is not None})
    return Brand(**data)


@router.post("/brands/{brand_id}/logos", response_model=Logo, status_code=201, summary="API-BRD-05 브랜드 로고 추가")
def add_logo(brand_id: str, file: UploadFile = File(...)):
    fmt = LogoFormat.png
    if file.filename and "." in file.filename:
        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext in LogoFormat._value2member_map_:
            fmt = LogoFormat(ext)
    return Logo(format=fmt)


@router.get("/brands/{brand_id}/logos", response_model=list[Logo], summary="API-BRD-06 브랜드 로고 목록")
def list_logos(brand_id: str):
    return [Logo(id="logo-001", orderNo=1), Logo(id="logo-002", format=LogoFormat.svg, orderNo=2)]


@router.delete("/brands/{brand_id}/logos/{logo_id}", status_code=204, summary="API-BRD-07 브랜드 로고 삭제")
def delete_logo(brand_id: str, logo_id: str):
    return None
