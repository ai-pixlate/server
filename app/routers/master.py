"""MST — 마스터 데이터 (API-MST-01~07, 🟢9월).

MST-01(국가)·MST-02(언어)는 실제 DB(master_country·master_language)에서 조회한다.
나머지(MST-03~07)는 아직 mock 응답.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter(tags=["Master"])


@router.get("/master/countries", summary="API-MST-01 타겟 국가 목록 (DB)")
def countries(db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT code, name, has_regulatory_dictionary "
            "FROM master_country ORDER BY code"
        )
    ).mappings().all()
    return [
        {
            "code": r["code"],
            "name": r["name"],
            "hasRegulatoryDictionary": r["has_regulatory_dictionary"],
        }
        for r in rows
    ]


@router.get("/master/languages", summary="API-MST-02 도착 언어 목록 (DB)")
def languages(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT code, name FROM master_language ORDER BY code")
    ).mappings().all()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


# ── 아래는 아직 mock (추후 DB로 교체) ─────────────────────────────
@router.get("/master/regulatory-classes", summary="API-MST-03 규제 분류 목록(국가별)")
def regulatory_classes(country: str = Query(..., examples=["US"])):
    return [
        {"code": "cosmetic", "nameKo": "화장품", "country": country},
        {"code": "otc", "nameKo": "일반의약품", "country": country},
    ]


@router.get("/master/categories", summary="API-MST-04 카테고리 트리")
def categories():
    return [
        {"id": "cat-face", "name": "Face", "children": [
            {"id": "cat-face-serum", "name": "Serum", "children": []},
        ]},
        {"id": "cat-lip", "name": "Lip Care", "children": []},
    ]


@router.get("/master/channel-specs", summary="API-MST-05 규격(채널) 목록")
def channel_specs():
    return [
        {"id": "spec-original", "specType": "original", "name": "원본 규격"},
        {"id": "spec-coupang", "specType": "site", "name": "쿠팡 상세"},
    ]


@router.get("/master/channel-specs/{spec_id}/upload-guide", summary="API-MST-06 규격 종속 업로드 안내")
def upload_guide(spec_id: str):
    return {
        "channelSpecId": spec_id,
        "format": ["jpg", "png"],
        "maxSizeMb": 20,
        "recommendedCount": 10,
        "guideText": "JPG/PNG, 파일당 20MB 이하, 권장 10장",
    }


@router.get("/master/versions", summary="API-MST-07 마스터 버전 조회")
def versions():
    return {
        "master_country": {"version": "v3.4.2", "loadedAt": "2026-09-16T00:00:00Z"},
        "master_language": {"version": "v3.4.2", "loadedAt": "2026-09-16T00:00:00Z"},
        "category_master": {"version": "v3.4.2", "loadedAt": "2026-09-16T00:00:00Z"},
    }
