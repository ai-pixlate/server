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


@router.get("/master/regulatory-classes", summary="API-MST-03 규제 분류 목록(국가별) (DB)")
def regulatory_classes(country: str = Query(..., examples=["US"]), db: Session = Depends(get_db)):
    rows = db.execute(
        text(
            "SELECT code, name, is_escape_hatch FROM master_regulatory_class "
            "WHERE country_code = :c ORDER BY code"
        ),
        {"c": country},
    ).mappings().all()
    return [{"code": r["code"], "name": r["name"], "country": country, "isEscapeHatch": r["is_escape_hatch"]} for r in rows]


@router.get("/master/categories", summary="API-MST-04 카테고리 트리 (DB)")
def categories(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT id, parent_id, name, is_leaf FROM category_master ORDER BY id"),
    ).mappings().all()
    nodes = {r["id"]: {"id": r["id"], "name": r["name"], "isLeaf": r["is_leaf"], "children": []} for r in rows}
    roots = []
    for r in rows:
        node = nodes[r["id"]]
        if r["parent_id"] and r["parent_id"] in nodes:
            nodes[r["parent_id"]]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/master/channel-specs", summary="API-MST-05 규격(채널) 목록 (DB)")
def channel_specs(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT id, code, name, spec_type FROM channel_spec WHERE is_active = true ORDER BY id"),
    ).mappings().all()
    return [{"id": r["id"], "code": r["code"], "name": r["name"], "specType": r["spec_type"]} for r in rows]


@router.get("/master/channel-specs/{spec_id}/upload-guide", summary="API-MST-06 규격 종속 업로드 안내 (DB)")
def upload_guide(spec_id: int, db: Session = Depends(get_db)):
    modules = db.execute(
        text(
            "SELECT module_name, usage_type, char_limit, recommended_count "
            "FROM module_spec WHERE channel_spec_id = :id ORDER BY id"
        ),
        {"id": spec_id},
    ).mappings().all()
    return {
        "channelSpecId": spec_id,
        "format": ["jpg", "png"],
        "maxSizeMb": 20,
        "recommendedCount": sum((m["recommended_count"] or 0) for m in modules) or 10,
        "modules": [
            {"moduleName": m["module_name"], "usageType": m["usage_type"],
             "charLimit": m["char_limit"], "recommendedCount": m["recommended_count"]}
            for m in modules
        ],
        "guideText": "JPG/PNG, 파일당 20MB 이하",
    }


@router.get("/master/versions", summary="API-MST-07 마스터 버전 조회 (DB)")
def versions(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT data_type, version, is_current, loaded_at FROM master_data_version ORDER BY data_type"),
    ).mappings().all()
    return {
        r["data_type"]: {
            "version": r["version"],
            "isCurrent": r["is_current"],
            "loadedAt": r["loaded_at"].isoformat() if r["loaded_at"] else None,
        }
        for r in rows
    }
