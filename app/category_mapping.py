"""작업 카테고리 → 내부 분류 해석(PR #57 · 통합 D10 용어집 적용 분류의 입력).

작업 생성 때 한 번 해석해 `job.internal_category`에 저장한다. 용어집 공급은 저장된 값만 쓰고 매핑을 다시 해석하지 않는다.
- 선택한 categoryId의 **직접 매핑만** 본다. `parent_id`로 상위 카테고리 매핑을 찾지 않는다.
- 카테고리 미선택 · 매핑 없음 → NULL(공급은 common만).
- 같은 내부 분류를 가리키는 중복 행은 하나로 본다(`category_mapping`에 유일 제약이 없다).
- 서로 다른 내부 분류가 여럿이면 `CategoryMappingConflict` — 첫 행을 고르거나 common으로 숨기지 않는다.
- 값은 저장된 문자열 그대로 비교·저장한다(표기 정규화를 더하지 않는다).
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


class CategoryMappingConflict(RuntimeError):
    """한 카테고리의 직접 매핑이 서로 다른 내부 분류를 가리킨다 — 데이터 · 설정 오류."""

    def __init__(self, category_id: int, values: list[str]):
        super().__init__(f"category {category_id}: 서로 다른 내부 분류 매핑 {values}")
        self.category_id = category_id
        self.values = values


def interpret_mappings(category_id: int, values: Iterable[str]) -> Optional[str]:
    """직접 매핑 값들 → 내부 분류 하나 또는 NULL. 서로 다른 값이 여럿이면 충돌."""
    distinct = sorted(set(values))
    if not distinct:
        return None
    if len(distinct) > 1:
        raise CategoryMappingConflict(category_id, distinct)
    return distinct[0]


def resolve_internal_category(db: Session, category_id: Optional[int]) -> Optional[str]:
    """작업 생성 트랜잭션 안에서 부른다. categoryId가 없으면 조회하지 않고 NULL."""
    if category_id is None:
        return None
    rows = db.execute(text("SELECT internal_category FROM category_mapping WHERE category_id = :c"), {"c": category_id}).all()
    return interpret_mappings(category_id, (r[0] for r in rows))
