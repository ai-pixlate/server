"""⑧ BE 용어집 공급 선별(select_glossary) — DB 없이 공급 형식·지문·실패 구분을 확인한다(통합 D10, 합성 값)."""
from __future__ import annotations

from app.flows.downstream import select_glossary
from pipeline import glossary as gl

SEL = {"rules": gl.SELECT_RULES_VERSION, "target_lang": "en", "categories": ["common"]}


def G(i, ko, en, enf="enforced", cat="common", ver=1):
    return {"id": i, "term_ko": ko, "term_target": en, "enforcement": enf, "internal_category": cat, "version": ver}


def T(i, src):
    return {"id": i, "source_ko": src}


ROWS = [G(1, "병풀", "Centella Asiatica"), G(2, "병풀추출물", "Centella Asiatica Extract"), G(3, "녹차", "Green Tea"),
        G(4, "산뜻한", "lightweight", "reference")]


def test_supply_contains_only_relevant_terms_in_contract_shape():
    s = select_glossary(ROWS, [T(10, "병풀추출물 함유"), T(11, "산뜻한 사용감")], SEL)
    assert s["status"] == "ok" and s["reason"] is None and s["version"].startswith("db-sha256:")
    assert s["terms"] == [
        {"glossary_id": "2", "term_ko": "병풀추출물", "term_target": "Centella Asiatica Extract", "enforcement": "enforced"},
        {"glossary_id": "4", "term_ko": "산뜻한", "term_target": "lightweight", "enforcement": "reference"},
    ]


def test_zero_hits_is_ok_empty_not_failure():
    s = select_glossary(ROWS, [T(10, "관련 없는 문장")], SEL)
    assert s["status"] == "ok" and s["terms"] == []


def test_bad_data_is_failed_supply_not_empty_success():
    s = select_glossary(ROWS + [G(5, "자외선차단", "sunscreen"), G(6, "자외선 차단", "UV protection", cat="Sunscreens & Tanning Products")],
                        [T(10, "자외선 차단 크림")], {**SEL, "categories": ["Sunscreens & Tanning Products", "common"]})
    assert s["status"] == "failed" and s["terms"] == [] and s["version"] is None and "임의로 고르지 않는다" in s["reason"]
    s = select_glossary([G(1, "  ", "x")], [T(10, "아무거나")], SEL)
    assert s["status"] == "failed"


def test_fingerprint_is_deterministic_and_tracks_selection_inputs():
    a = select_glossary(ROWS, [T(10, "병풀 녹차")], SEL)
    assert a == select_glossary(list(ROWS), [T(10, "병풀 녹차")], dict(SEL))
    # 무관한 행이 바뀌어도 공급은 같다(선택된 행 내용만 지문에 들어간다)
    assert a == select_glossary([*ROWS[:3], G(4, "산뜻한", "fresh", "reference", ver=2)], [T(10, "병풀 녹차")], SEL)
    # 선택된 행의 원본 version · 분류, 적용 분류, 규칙 버전이 바뀌면 지문이 바뀐다
    for rows, sel in [([G(1, "병풀", "Centella Asiatica", ver=2), *ROWS[1:]], SEL),
                      (ROWS, {**SEL, "categories": ["Sunscreens & Tanning Products", "common"]}),
                      (ROWS, {**SEL, "rules": "glossary-select@other"})]:
        b = select_glossary(rows, [T(10, "병풀 녹차")], sel)
        assert b["terms"] == a["terms"] and b["version"] != a["version"]


def test_retry_targets_only_failed_blocks():
    first = select_glossary(ROWS, [T(10, "병풀 크림"), T(11, "녹차 크림")], SEL)
    retry = select_glossary(ROWS, [T(11, "녹차 크림")], SEL)  # 실패 블록만 재시도 — 그 블록 원문만 검색
    assert [t["glossary_id"] for t in first["terms"]] == ["1", "3"]
    assert [t["glossary_id"] for t in retry["terms"]] == ["3"]
