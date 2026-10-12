"""작업 카테고리 → 내부 분류 해석(PR #57 카테고리 연결) — DB 없이 해석 규칙과 작업 생성 API의 충돌 처리를 확인한다(합성 값)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.category_mapping import CategoryMappingConflict, interpret_mappings, resolve_internal_category
from app.db import get_db
from app.main import app
from app.security import get_current_seller

SUN = "Sunscreens & Tanning Products"


class FakeDB:
    """category_mapping(category_id → internal_category 행 목록)만 아는 가짜 세션. 실행한 SQL과 rollback을 기록한다."""

    def __init__(self, mapping: dict[int, list[str]]):
        self.mapping = mapping
        self.sql: list[str] = []
        self.rolled_back = False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sql.append(sql)
        if "FROM category_mapping" in sql:
            return _Rows([(v,) for v in self.mapping.get(params["c"], [])])
        raise AssertionError(f"예상하지 않은 SQL: {sql}")

    def rollback(self):
        self.rolled_back = True


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


# category 10 = 하위(선크림, 직접 매핑), 20 = 하위(매핑 없음, 부모 30에만 매핑), 40 = 같은 값 중복, 50 = 서로 다른 값
MAPPING = {10: [SUN], 30: [SUN], 40: [SUN, SUN], 50: [SUN, "Skincare"]}


@pytest.mark.parametrize("cid, want", [(10, SUN), (None, None), (99, None), (40, SUN), (20, None)])
def test_resolve_direct_mapping_only(cid, want):
    db = FakeDB(MAPPING)
    assert resolve_internal_category(db, cid) == want
    assert all("parent_id" not in s for s in db.sql)  # 상위 카테고리를 탐색하지 않는다
    if cid is None:
        assert db.sql == []  # 미선택이면 조회하지 않는다


def test_conflicting_mappings_are_detected_not_first_row():
    with pytest.raises(CategoryMappingConflict) as e:
        resolve_internal_category(FakeDB(MAPPING), 50)
    assert e.value.category_id == 50 and e.value.values == ["Skincare", SUN]


def test_interpret_keeps_value_as_stored():
    assert interpret_mappings(1, ["Sunscreens & Tanning"]) == "Sunscreens & Tanning"  # 표기 정규화를 더하지 않는다
    with pytest.raises(CategoryMappingConflict):
        interpret_mappings(1, ["Sunscreens & Tanning", SUN])  # 다른 문자열은 다른 분류


@pytest.fixture
def client_with():
    db = FakeDB(MAPPING)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_seller] = lambda: 1
    yield TestClient(app), db
    app.dependency_overrides.clear()


def test_create_job_rejects_mapping_conflict_without_insert(client_with):
    client, db = client_with
    r = client.post("/v1/jobs", json={"brandId": 1, "productName": "선크림", "categoryId": 50})
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"  # 기존 오류 계약 재사용
    assert db.rolled_back and not any("INSERT" in s for s in db.sql)  # 작업을 만들지 않는다
