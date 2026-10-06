"""규제사전·현지부적합 로더 — 파일 검증(DB 불필요).

실제 전달본 검증은 DICT_XLSX_DIR 폴더에 해당 파일이 있을 때만 돈다. 건수는 각 전달본의 검증값이며
로더의 고정 규칙이 아니다.
- regulation_dict.xlsx (2026-09-28): 대표 근거 날짜 누락으로 거부되는지(회귀)
- regulation_dict_20260930.xlsx (정정본): 통과하고 24·16 으로 적재되는지
- locale_unsuitable_dict.xlsx: 두 경우 공통
"""
import datetime as dt
import os

import pytest

from app.dictionary_ingest import main, validate_files
from tests.dictionary_fixtures import basic_regulatory, ev_row, local_row, reg_row, write_local, write_regulatory


def _cols(vr):
    return [i.column for i in vr.issues]


def test_regulatory_mapping_and_shared_evidence(tmp_path):
    vr = validate_files(str(basic_regulatory(tmp_path / "r.xlsx")), None)
    assert vr.issues == []
    f = vr.files[0]
    recs = {r["external_id"]: r for r in f.records}
    assert set(recs) == {"RG-001", "RG-002", "RG-003", "RG-004", "RG-005"}
    r1 = recs["RG-001"]
    assert r1["variant_ko"] == ["표현1", "변형1"] and r1["forbidden_en"] == ["claim 1", "claims 1"]
    assert r1["confirmed_date"] == dt.date(2026, 9, 16)  # 본문 verified_at → confirmed_date
    assert r1["exclusion_context"] is None and r1["dict_type"] == "regulatory"
    assert recs["RG-002"]["verdict_status"] == "regulated" and f.converted == ["RG-002"]  # rewritable → regulated
    assert recs["RG-002"]["source_verdict_status"] == "rewritable"  # 시트 원값 보존
    assert recs["RG-001"]["source_verdict_status"] == "regulated" and recs["RG-004"]["source_verdict_status"] == "conditional"
    # 공유 근거: 연결은 항목마다, 원본 근거 ID 는 같다
    assert f.evidence["RG-003"]["external_id"] == f.evidence["RG-004"]["external_id"] == "MN-001"
    ev = f.evidence["RG-001"]
    assert ev["evidence_quote"] == "quote of WL-001" and ev["is_primary"] is True
    assert ev["evidence_document"] is None and "verified_at" not in ev  # 근거 verified_at 은 저장 안 함
    assert f.evidence_skipped == 1


def test_local_mapping(tmp_path):
    vr = validate_files(None, str(write_local(tmp_path / "l.xlsx", [local_row(1), local_row(2, 판정="needs_fix")])))
    assert vr.issues == []
    rec = vr.records[0]
    assert (rec["dict_type"], rec["target_country"], rec["regulatory_class"]) == ("local", "US", "common")
    assert rec["variant_ko"] == ["패턴1", "패턴1b"] and rec["forbidden_en"] is None
    assert rec["exclusion_context"] == "제외 맥락 1" and rec["keep_context"] == "유지 맥락 1"
    assert rec["confirmed_date"] == dt.date(2026, 9, 22)
    assert [r["source_verdict_status"] for r in vr.records] == ["irrelevant", "needs_fix"]  # 판정 원값 보존
    assert "판단 근거" not in rec and "kr_freq" not in rec


def test_hangul_in_alternative_is_reported_not_rejected(tmp_path):
    rows = [reg_row(1, "WL-001", verdict_status="conditional", alternative_expression="SPF [시험 결과값]")]
    vr = validate_files(str(write_regulatory(tmp_path / "r.xlsx", rows, [ev_row("WL-001", "RG-001")])), None)
    assert vr.issues == [] and vr.files[0].unresolved == ["RG-001"]


@pytest.mark.parametrize("over,column", [
    ({"confidence": "medium"}, "confidence"),
    ({"version": None}, "version"),
    ({"version": "v2"}, "version"),
    ({"internal_category": "Face"}, "internal_category"),
    ({"dict_type": "local"}, "dict_type"),
    ({"regulatory_class": "combination"}, "regulatory_class"),
    ({"verdict_status": "banned"}, "verdict_status"),
    ({"verdict_status": "allowed"}, "alternative_expression"),            # allowed 인데 대체 표현 있음
    ({"verdict_status": "rewritable", "alternative_expression": None}, "alternative_expression"),
    ({"verdict_status": "conditional", "alternative_expression": None}, "alternative_expression"),
    ({"verified_at": None}, "verified_at"),
    ({"variant_expressions.ko": "a; ; b"}, "variant_expressions.ko"),
    ({"variant_expressions.en": None}, "variant_expressions.en"),
    ({"evidence_url": "https://other"}, "evidence_url"),
    ({"evidence_id": "WL-777"}, "evidence_id"),                           # 근거 탭에 없음
    ({"id": "RX-001"}, "id"),
])
def test_invalid_regulatory_row(tmp_path, over, column):
    rows = [reg_row(1, "WL-001", **over)]
    vr = validate_files(str(write_regulatory(tmp_path / "r.xlsx", rows, [ev_row("WL-001", "RG-001")])), None)
    assert column in _cols(vr) and vr.records == []


@pytest.mark.parametrize("ev_over,needle", [
    ({"is_primary": "N"}, "is_primary=Y"),
    ({"verified_at": None}, "필수값 오류"),          # R11: 대표 근거 필수 날짜
    ({"quote": None}, "필수값 오류"),
    ({"source_type": "FDA 공식 문서"}, "Warning Letter 이어야"),
])
def test_invalid_primary_evidence(tmp_path, ev_over, needle):
    vr = validate_files(str(write_regulatory(tmp_path / "r.xlsx", [reg_row(1, "WL-001")],
                                             [ev_row("WL-001", "RG-001", **ev_over)])), None)
    assert any(needle in i.message for i in vr.issues) and vr.records == []


def test_shared_evidence_error_is_reported_once(tmp_path):
    rows = [reg_row(3, "MN-001", regulatory_class="otc"), reg_row(4, "MN-001", regulatory_class="otc")]
    vr = validate_files(str(write_regulatory(tmp_path / "r.xlsx", rows,
                                             [ev_row("MN-001", "RG-003; RG-004", verified_at=None)])), None)
    assert sum(1 for i in vr.issues if i.external_id == "MN-001") == 1
    assert {i.external_id for i in vr.issues if i.column == "evidence_id"} == {"RG-003", "RG-004"}


def test_market_practice_only_for_allowed(tmp_path):
    vr = validate_files(str(write_regulatory(tmp_path / "r.xlsx", [reg_row(5, "MP-001")],
                                             [ev_row("MP-001", "RG-005")])), None)
    assert any("시장 관행" in i.message for i in vr.issues)


def test_two_primaries_for_one_item_is_an_error(tmp_path):
    ev = [ev_row("WL-001", "RG-001"), ev_row("WL-002", "RG-001")]
    vr = validate_files(str(write_regulatory(tmp_path / "r.xlsx", [reg_row(1, "WL-001")], ev)), None)
    assert any("하나여야" in i.message for i in vr.issues)


def test_duplicate_ids_and_content_keys(tmp_path):
    ev = [ev_row("WL-001", "RG-001"), ev_row("WL-001", "RG-002")]
    vr = validate_files(str(write_regulatory(tmp_path / "r.xlsx", [reg_row(1, "WL-001")], ev)), None)
    assert any("근거 ID 중복" in i.message for i in vr.issues)
    rows = [reg_row(1, "WL-001"), reg_row(1, "WL-001")]
    vr = validate_files(str(write_regulatory(tmp_path / "r2.xlsx", rows, [ev_row("WL-001", "RG-001")])), None)
    assert any("항목 ID 중복" in i.message for i in vr.issues)
    rows = [reg_row(1, "WL-001"), reg_row(2, "WL-002", source_expression="표현1")]
    ev = [ev_row("WL-001", "RG-001"), ev_row("WL-002", "RG-002")]
    vr = validate_files(str(write_regulatory(tmp_path / "r3.xlsx", rows, ev)), None)
    assert any("내용 키 중복" in i.message for i in vr.issues)


@pytest.mark.parametrize("over,column", [
    ({"판정": "must_fix"}, "판정"),
    ({"제외하는 맥락": None}, "제외하는 맥락"),
    ({"제외하지 않는 맥락": " "}, "제외하지 않는 맥락"),
    ({"셀러 문장": None}, "셀러 문장"),
    ({"verified_at": "9/22"}, "verified_at"),
    ({"id": "LC-A"}, "id"),
])
def test_invalid_local_row(tmp_path, over, column):
    vr = validate_files(None, str(write_local(tmp_path / "l.xlsx", [local_row(1, **over)])))
    assert column in _cols(vr)


def test_one_bad_row_fails_everything(tmp_path, capsys):
    reg = basic_regulatory(tmp_path / "r.xlsx")
    loc = write_local(tmp_path / "l.xlsx", [local_row(1), local_row(2, 판정="?")])
    assert main(["validate", "--regulatory", str(reg), "--local", str(loc)]) == 1
    assert "결과: 실패" in capsys.readouterr().out


# ── 실제 전달본 (있을 때만) ────────────────────────────────────────────

def _delivery(reg_name: str):
    """DICT_XLSX_DIR 의 (규제사전 파일, 현지부적합 파일) 또는 없으면 None."""
    d = os.getenv("DICT_XLSX_DIR")
    if not d:
        return None
    reg, loc = os.path.join(d, reg_name), os.path.join(d, "locale_unsuitable_dict.xlsx")
    return (reg, loc) if os.path.exists(reg) and os.path.exists(loc) else None


FIRST = "regulation_dict.xlsx"             # 2026-09-28 전달본 — 대표 근거 날짜 누락
CORRECTED = "regulation_dict_20260930.xlsx"  # 2026-09-30 정정본 — MN 날짜 보완


@pytest.mark.skipif(_delivery(FIRST) is None, reason="DICT_XLSX_DIR 에 2026-09-28 전달본 없음")
def test_real_delivery_is_blocked_by_missing_evidence_dates():
    reg, loc = _delivery(FIRST)
    vr = validate_files(reg, loc)
    ev_ids = sorted({i.external_id for i in vr.issues if i.column == "verified_at"})
    assert ev_ids == ["MN-001", "MN-002", "MN-003", "MN-004", "MN-006", "MN-007", "MN-008"]
    blocked = sorted({i.external_id for i in vr.issues if i.column == "evidence_id"})
    assert blocked == ["RG-003", "RG-011", "RG-012", "RG-013", "RG-014", "RG-015", "RG-016",
                       "RG-020", "RG-021", "RG-022"]


@pytest.mark.skipif(_delivery(CORRECTED) is None, reason="DICT_XLSX_DIR 에 2026-09-30 정정본 없음")
def test_corrected_delivery_passes():
    reg, loc = _delivery(CORRECTED)
    vr = validate_files(reg, loc)
    assert vr.issues == []  # 대표 근거 날짜 오류 0
    rf, lf = vr.files
    assert (len(rf.records), len(rf.evidence), len({e["external_id"] for e in rf.evidence.values()})) == (16, 16, 13)
    assert len(lf.records) == 8
    assert sorted(rf.converted) == ["RG-008", "RG-030"]
    assert sorted(rf.unresolved) == ["RG-021", "RG-022"]  # 한국어 자리표시자 — 적재 가능, 번역 사용 불가
    assert sorted(r["external_id"] for r in rf.records if r["source_verdict_status"] == "rewritable") == [
        "RG-008", "RG-030"]
    from collections import Counter
    assert Counter(r["verdict_status"] for r in rf.records) == {"regulated": 10, "conditional": 4, "allowed": 2}
    assert Counter(r["verdict_status"] for r in lf.records) == {"irrelevant": 5, "needs_fix": 3}
