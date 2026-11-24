"""현지 묶음(_local_part) — v9 판정값 대응 공급(사용자 2026-11-19 결정: 2차 유지, 이름 이관 3차).

v9 사전(must_fix · needs_check)만 있는 환경에서도 의미 대응(contract.md 4.2)으로 서비스 판정값(irrelevant · needs_fix)을
공급해 현지 판정이 멈추지 않는지, 옛 값 · 공급 불가 규칙은 그대로인지 본다. DB 없이 행 사전만으로 확인한다.
"""
from __future__ import annotations

from app.dictionary_bundle import LOCAL_V9_SERVICE, _local_part

# PRD F-LOC-02 예시와 같은 배치: must_fix 5 · needs_check 3 (AI 지원 대응표 LC-01~08)
V9 = {"LC-01": "needs_check", "LC-02": "must_fix", "LC-03": "needs_check", "LC-04": "must_fix",
      "LC-05": "must_fix", "LC-06": "must_fix", "LC-07": "needs_check", "LC-08": "must_fix"}
OLD = {"must_fix": "irrelevant", "needs_check": "needs_fix"}


def _row(pk: int, ext: str, verdict: str, **over) -> dict:
    row = {"id": pk, "external_id": ext, "dict_type": "local", "target_country": "US", "regulatory_class": "common",
           "source_expression": f"항목 {ext}", "variant_ko": [f"패턴 {ext}"], "forbidden_en": None, "alternative_expression": None,
           "verdict_status": verdict, "source_verdict_status": verdict, "reason": "셀러 안내 문장", "confirmed_date": None,
           "exclusion_context": "제외 맥락", "keep_context": "유지 맥락"}
    row.update(over)
    return row


def _rows(values: dict[str, str]) -> list[dict]:
    return [_row(i, ext, v) for i, (ext, v) in enumerate(sorted(values.items()), 1)]


def test_mapping_is_the_documented_meaning_correspondence():
    assert LOCAL_V9_SERVICE == {"must_fix": "irrelevant", "needs_check": "needs_fix"}


def test_v9_only_dictionary_is_supplied_with_service_values():
    local = _local_part(_rows(V9), {}, None)
    assert local["status"] == "ok" and local["error"] is None and local["skipped"] == []
    got = {e["external_id"]: e["verdict_status"] for e in local["entries"]}
    assert got == {k: OLD[v] for k, v in V9.items()}
    # DB 원값 열은 그대로(추정 복원 · 덮어쓰기 없음), pk 로 원래 행을 찾을 수 있다
    assert {e["external_id"]: e["source_verdict_status"] for e in local["entries"]} == V9
    assert all(isinstance(e["pk"], int) for e in local["entries"])


def test_old_value_dictionary_is_unchanged():
    old_rows = _rows({k: OLD[v] for k, v in V9.items()})
    local = _local_part(old_rows, {}, None)
    assert local["status"] == "ok"
    assert {e["external_id"]: e["verdict_status"] for e in local["entries"]} == {k: OLD[v] for k, v in V9.items()}


def test_cultural_stays_skipped():
    rows = _rows(V9) + [_row(99, "LC-90", "cultural")]
    local = _local_part(rows, {}, None)
    assert local["status"] == "ok" and local["skipped"] == ["LC-90"] and len(local["entries"]) == 8


def test_missing_item_still_makes_local_unavailable():
    partial = dict(V9)
    partial.pop("LC-08")
    local = _local_part(_rows(partial), {}, None)
    assert local["status"] == "unavailable" and local["entries"] == [] and "지원 대응표" in local["error"]


def test_required_field_problem_still_makes_local_unavailable():
    rows = _rows(V9)
    rows[0]["keep_context"] = None
    local = _local_part(rows, {}, None)
    assert local["status"] == "unavailable" and "필수값 누락" in local["error"]


def test_lookup_failure_and_empty_rows_are_unchanged():
    assert _local_part(None, {}, "현지 사전 조회 오류: X")["status"] == "unavailable"
    empty = _local_part([], {}, None)
    assert empty["status"] == "unavailable" and empty["error"] == "현지 사전 행 없음"
