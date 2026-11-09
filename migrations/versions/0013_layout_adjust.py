"""text_block.auto_adjust → layout_adjust 이름 변경과 저장 모양 변환(PRD 11 B76 · 계약 blockPatch 「블록 배치」).

이 열에 값을 쓰는 코드는 없었다. 값이 있다면 코드 밖에서 들어온 것이고, 문서상 뜻이 둘이라(OpenAPI AutoAdjust = 시스템 산출물,
docs/ai/contract.md 6.1 = 셀러 조정) 모양으로 나눈다. 확인되지 않는 값을 셀러 조정으로 옮기지 않는다.

| 이전 값 | 판별 | 새 값 |
|---|---|---|
| NULL | | NULL |
| 셀러 조정 | 객체 · 키가 schema_version·layout_bbox·font_scale·updated_by·updated_at 안 · updated_by = user · layout_bbox(있으면) 좌표 4개가 유한한 수 · font_scale(있으면) 양수 · 둘 중 하나 이상 | user |
| 시스템 산출물 | 객체 · 키가 schema_version·font_scale·line_break_applied·updated_by·updated_at 안 · updated_by 없음 또는 system · 값 형식 맞음 · 하나 이상 | system |
| 그 밖 | 출처 불명·훼손 | legacy 에 원본 그대로 |

legacy 의 렌더(app/layout_adjust.py): 이전 서버의 적용 규칙을 유지한다 — 형식이 맞는 layout_bbox 는 계속 그 박스로 그리고(`layoutAdjust`·content.csv 에는
셀러 조정으로 나오지 않는다), 객체가 아니거나 layout_bbox 형식이 틀리면 전처럼 RENDER_INPUT_INVALID, layout_bbox 가 없는 객체는 원본 bbox.
이전과 달라지는 렌더 = updated_by: user 로 확인된 font_scale — 전에는 적용하지 않았고 이제 셀러 배율로 적용한다(입력 지문도 바뀐다).

downgrade 복원 범위:
| 새 값 | 복원 | 한계 |
|---|---|---|
| NULL | NULL | |
| legacy 만 | 원본 그대로 | |
| user | {schema_version: "1", layout_bbox, font_scale, updated_by: "user", updated_at} 중 값이 있는 키 | 이전 서버 코드는 배율을 적용하지 않는다. schema_version 은 "1" 표기로 복원 |
| system 만 | {font_scale, line_break_applied} | schema_version·updated_by·updated_at 표기는 복원 안 됨 |
| 둘 이상 | user 가 있으면 위 형태 + 나머지를 system·legacy 키로 보존 | 이전 코드는 보존 키를 쓰지 않는다. 다시 올리면 출처 불명으로 분류 |
| legacy + 셀러 배율만 | 위 형태(layout_bbox 없음) + legacy 키 | 이전 코드는 legacy 박스 대신 원본 bbox 로 그린다. 다시 올려도 legacy 박스는 돌아오지 않는다 |
| legacy + 셀러의 명시적 해제 | {legacy, legacy_released} | 이전 코드도 원본 bbox 로 그린다. 다시 올리면 출처 불명으로 분류(그 값에는 layout_bbox 가 없어 원본 bbox) |

배포: 열 이름이 바뀌므로 이전 서버 코드와 호환되지 않는다. 서버 코드 전환과 함께 적용하고, 롤백도 이전 코드 복귀와 함께 한다.
revision 번호: develop head(0012 glossary_retired_at, #115) 뒤. 진행 중인 #103 은 원격 0009, 로컬 미커밋 작업에서 0012 로 옮기는 중이라
번호가 다시 겹칠 수 있다 — 나중에 머지하는 쪽이 최신 develop head 뒤로 다시 맞춘다.

Revision ID: 0013
Revises: 0012
"""
import json
import math
from typing import Any, Union

from alembic import op
from sqlalchemy import text

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels = None
depends_on = None

_USER_KEYS = {"schema_version", "layout_bbox", "font_scale", "updated_by", "updated_at"}
_SYSTEM_KEYS = {"schema_version", "font_scale", "line_break_applied", "updated_by", "updated_at"}


def _number(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and (not isinstance(v, float) or math.isfinite(v))


def _box(v: Any) -> bool:
    return isinstance(v, dict) and set(v) == {"x", "y", "w", "h"} and all(_number(x) for x in v.values())


def _scale(v: Any) -> bool:
    return _number(v) and v > 0


def to_v2(old: Any) -> dict[str, Any]:
    """이전 값(NULL 아님) → 새 저장값."""
    if isinstance(old, dict):
        box, scale = old.get("layout_bbox"), old.get("font_scale")
        if (set(old) <= _USER_KEYS and old.get("updated_by") == "user" and (box is not None or scale is not None)
                and (box is None or _box(box)) and (scale is None or _scale(scale))):
            user = {"layout_bbox": box, "font_scale": scale}
            if isinstance(old.get("updated_at"), str):
                user["updated_at"] = old["updated_at"]
            return {"schema_version": 2, "user": user, "system": None}
        lb = old.get("line_break_applied")
        if (set(old) <= _SYSTEM_KEYS and old.get("updated_by") in (None, "system") and ("font_scale" in old or "line_break_applied" in old)
                and ("font_scale" not in old or _scale(scale)) and ("line_break_applied" not in old or isinstance(lb, bool))):
            return {"schema_version": 2, "user": None,
                    "system": {k: old[k] for k in ("font_scale", "line_break_applied") if k in old}}
    return {"schema_version": 2, "user": None, "system": None, "legacy": old}


def from_v2(cur: Any) -> Any:
    """새 저장값 → 이전 값. 지금 형식이 아니면 그대로 둔다."""
    if not (isinstance(cur, dict) and cur.get("schema_version") == 2):
        return cur
    user, system, legacy = cur.get("user"), cur.get("system"), cur.get("legacy")
    if cur.get("legacy_released") and not user and not system:
        return {"legacy": legacy, "legacy_released": True}  # 원본 값을 그대로 돌리면 해제한 박스가 다시 적용된다
    if user:
        out = {"schema_version": "1", "updated_by": "user"}
        out.update({k: user[k] for k in ("layout_bbox", "font_scale", "updated_at") if user.get(k) is not None})
        if system:
            out["system"] = system
        if legacy is not None:
            out["legacy"] = legacy
        if cur.get("legacy_released"):
            out["legacy_released"] = True
        return out
    if system:
        if legacy is None:
            return dict(system)
        return {**system, "legacy": legacy, **({"legacy_released": True} if cur.get("legacy_released") else {})}
    return legacy


def _convert(column: str, fn) -> None:
    conn = op.get_bind()
    # 텍스트로 읽어 직접 파싱한다 — 드라이버는 JSONB 문자열 값을 그대로 주므로 문서와 값을 구별할 수 없다
    rows = conn.execute(text(f"SELECT id, {column}::text AS v FROM text_block WHERE {column} IS NOT NULL")).mappings().all()
    for r in rows:
        new = fn(json.loads(r["v"]))
        conn.execute(text(f"UPDATE text_block SET {column} = CAST(:v AS jsonb) WHERE id = :i"),
                     {"v": None if new is None else json.dumps(new, ensure_ascii=False), "i": r["id"]})


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE text_block RENAME COLUMN auto_adjust TO layout_adjust")
    _convert("layout_adjust", to_v2)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _convert("layout_adjust", from_v2)
    op.execute("ALTER TABLE text_block RENAME COLUMN layout_adjust TO auto_adjust")
