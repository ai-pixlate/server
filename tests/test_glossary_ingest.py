"""용어집 로더 — 파일 검증(DB 불필요).

실제 전달본 검증은 GLOSSARY_XLSX_DIR(glossary_certification*.xlsx 등 3개가 있는 폴더)이
있을 때만 돈다. 건수는 이번 전달본(2026-09-28)의 검증값이며 로더의 고정 규칙이 아니다.
문구 v3 555행 파일 검증은 GLOSSARY_V3_PHRASE_XLSX(파일 경로)가 있을 때만 돈다. 건수는 그 파일의 검증값이다.
"""
import datetime as dt
import glob
import hashlib
import os

import pytest

from app.glossary_ingest import PHRASE_REVIEW_COLUMNS, SPECS, main, validate_files
from tests.glossary_fixtures import base_row, write_book


def _validate(tmp_path, kind, rows, **kw):
    path = write_book(tmp_path / f"{kind}.xlsx", kind, rows, **kw)
    return validate_files({kind: str(path)})


def _issue_columns(vr):
    return [i.column for i in vr.issues]


def test_phrase_selects_zone_a_and_load_flag_y(tmp_path):
    rows = [
        base_row("phrase", 1),
        base_row("phrase", 2, 적재여부="N"),
        base_row("phrase", 3, 구역="C", 적재여부="N"),
        base_row("phrase", 4, 구역="보류", 적재여부="N"),
        base_row("phrase", 5, 구역="이관", 적재여부="N"),
    ]
    vr = _validate(tmp_path, "phrase", rows)
    assert vr.issues == []
    f = vr.files[0]
    assert [r["external_id"] for r in f.records] == ["GL-M001"]
    assert f.excluded == {"구역=A·적재여부=N": 1, "구역=C": 1, "구역=보류": 1, "구역=이관": 1}
    assert f.excluded_ids["GL-M002"] == "구역=A·적재여부=N"


def test_ingredient_and_certification_use_zone_only(tmp_path):
    vr = _validate(tmp_path, "certification", [
        base_row("certification", 1), base_row("certification", 2, 구역="B"), base_row("certification", 3, 구역="C"),
    ])
    assert vr.issues == []
    assert len(vr.records) == 1 and vr.files[0].excluded == {"구역=B": 1, "구역=C": 1}


def test_values_are_preserved_and_only_two_mappings_apply(tmp_path):
    long_en = "X" * 1886
    rows = [
        base_row("phrase", 43, target_text="hyperpigmentation; discoloration", internal_category="Sunscreens & Tanning",
                 frequency=12, corpus_size=84, corpus_version="v2", version=2.0,
                 verified_at=dt.datetime(2026, 9, 18), 대표여부="대안"),
        base_row("phrase", 38, target_text="for oily/combination skin", verified_at="2026-09-18"),
    ]
    vr = _validate(tmp_path, "phrase", rows)
    assert vr.issues == []
    a, b = vr.records
    assert a["term_target"] == "hyperpigmentation; discoloration"  # 나누지 않음
    assert b["term_target"] == "for oily/combination skin"
    assert a["enforcement"] == "reference"
    assert a["internal_category"] == "Sunscreens & Tanning Products"
    assert (a["frequency"], a["corpus_size"], a["version"]) == (12, 84, 2)
    assert a["verified_at"] == dt.date(2026, 9, 18) and b["verified_at"] == dt.date(2026, 9, 18)
    assert a["representative"] == "대안" and a["load_flag"] == "Y" and a["zone"] == "A"
    assert b["frequency"] is None and b["corpus_size"] is None  # 빈칸은 0이 아니라 NULL

    vr = _validate(tmp_path, "ingredient", [base_row("ingredient", 1, id="GL-I00001", target_text=long_en,
                                                     source_ko="가" * 294)])
    rec = vr.records[0]
    assert rec["term_target"] == long_en and len(rec["term_ko"]) == 294
    assert rec["enforcement"] == "enforced" and rec["label"] is None


@pytest.mark.parametrize("kind,over,column", [
    ("certification", {"enforcement": "필수"}, "enforcement"),
    ("certification", {"id": "GL-M001"}, "id"),
    ("certification", {"term_kind": "marketing"}, "term_kind"),
    ("certification", {"source_ko": "  "}, "source_ko"),
    ("certification", {"target_text": None}, "target_text"),
    ("certification", {"version": 1.5}, "version"),
    ("certification", {"version": None}, "version"),
    ("certification", {"frequency": "많음"}, "frequency"),
    ("certification", {"verified_at": dt.datetime(2026, 9, 18, 13, 5)}, "verified_at"),
    ("certification", {"verified_at": "9월 18일"}, "verified_at"),
    ("certification", {"target_lang": "english-us"}, "target_lang"),
    ("phrase", {"대표여부": "Y"}, "대표여부"),
    ("phrase", {"라벨": None}, "라벨"),
    ("phrase", {"구역": "C", "적재여부": "Y"}, "적재여부"),
    ("phrase", {"적재여부": "예"}, "적재여부"),
])
def test_invalid_row_is_reported_with_row_and_id(tmp_path, kind, over, column):
    vr = _validate(tmp_path, kind, [base_row(kind, 1), base_row(kind, 2, **over)])
    assert column in _issue_columns(vr)
    issue = next(i for i in vr.issues if i.column == column)
    assert issue.row == 3  # 머리글 1행 + 두 번째 데이터 행


@pytest.mark.parametrize("kind,zone", [
    ("phrase", "A "),       # 공백이 붙은 A — 제외로 조용히 빠지면 안 된다
    ("phrase", "B"),        # 문구에는 B 구역이 없다
    ("ingredient", "B"),    # 성분은 A·C 뿐
    ("certification", "보류"),
    ("certification", None),
])
def test_unknown_zone_is_an_error_not_an_exclusion(tmp_path, kind, zone):
    flag = {"적재여부": "N"} if SPECS[kind].phrase else {}
    vr = _validate(tmp_path, kind, [base_row(kind, 1), base_row(kind, 2, 구역=zone, **flag)])
    assert "구역" in _issue_columns(vr)
    assert sum(vr.files[0].excluded.values()) == 0


def test_excluded_rows_need_only_id_and_zone(tmp_path):
    # 대응 미정(C) 행은 번역어·버전 등이 비어 있어도 된다
    vr = _validate(tmp_path, "certification", [
        base_row("certification", 1),
        base_row("certification", 2, 구역="C", target_text=None, version=None, enforcement=None),
    ])
    assert vr.issues == [] and vr.files[0].excluded == {"구역=C": 1}


@pytest.mark.parametrize("rows,needle", [
    ([base_row("certification", 1), base_row("certification", 2, 구역="C", id="C-002")], "형식"),
    ([base_row("certification", 1), base_row("certification", 1, 구역="C", source_ko="다른 말")], "원본 ID 중복"),
    ([base_row("certification", 2, 구역="B"), base_row("certification", 2, 구역="C", source_ko="다른 말")],
     "원본 ID 중복"),
])
def test_ids_are_checked_on_excluded_rows_too(tmp_path, rows, needle):
    vr = _validate(tmp_path, "certification", rows)
    assert any(needle in i.message for i in vr.issues)


def test_missing_column_is_an_error(tmp_path):
    vr = _validate(tmp_path, "phrase", [base_row("phrase", 1)], drop_columns=("대표여부",))
    assert "대표여부" in _issue_columns(vr) and vr.records == []


# ── 문구 입력 헤더(라벨·대표여부) ──────────────────────────────────────

def test_phrase_without_review_columns_stores_null(tmp_path):
    vr = _validate(tmp_path, "phrase", [base_row("phrase", 1), base_row("phrase", 2, 적재여부="N")],
                   drop_columns=PHRASE_REVIEW_COLUMNS)
    assert vr.issues == []
    rec = vr.records[0]
    assert rec["label"] is None and rec["representative"] is None  # 다른 값으로 추정해 채우지 않는다
    assert rec["load_flag"] == "Y" and rec["term_target"] == "phrase english 1"
    assert vr.files[0].excluded == {"구역=A·적재여부=N": 1}


@pytest.mark.parametrize("over,column", [
    ({"적재여부": "예"}, "적재여부"),
    ({"구역": "C", "적재여부": "Y"}, "적재여부"),
    ({"enforcement": "필수"}, "enforcement"),
    ({"source_ko": "  "}, "source_ko"),
    ({"target_text": None}, "target_text"),
    ({"version": None}, "version"),
    ({"verified_at": "9월 18일"}, "verified_at"),
    ({"term_kind": "certification"}, "term_kind"),
])
def test_phrase_without_review_columns_keeps_other_checks(tmp_path, over, column):
    vr = _validate(tmp_path, "phrase", [base_row("phrase", 1), base_row("phrase", 2, **over)],
                   drop_columns=PHRASE_REVIEW_COLUMNS)
    assert column in _issue_columns(vr)


def test_phrase_load_flag_stays_required_without_review_columns(tmp_path):
    vr = _validate(tmp_path, "phrase", [base_row("phrase", 1)], drop_columns=(*PHRASE_REVIEW_COLUMNS, "적재여부"))
    assert _issue_columns(vr) == ["적재여부"] and vr.records == []


@pytest.mark.parametrize("dropped,missing", [("라벨", "라벨"), ("대표여부", "대표여부")])
def test_phrase_with_one_review_column_is_an_error(tmp_path, dropped, missing):
    vr = _validate(tmp_path, "phrase", [base_row("phrase", 1)], drop_columns=(dropped,))
    assert _issue_columns(vr) == [missing] and vr.records == []


def test_phrase_with_review_columns_keeps_values_and_checks(tmp_path):
    vr = _validate(tmp_path, "phrase", [base_row("phrase", 1, 대표여부="대안")])
    assert vr.issues == []
    assert vr.records[0]["label"] == "라벨1" and vr.records[0]["representative"] == "대안"


@pytest.mark.parametrize("kind", ["certification", "ingredient"])
def test_other_kinds_are_not_affected_by_phrase_header_rule(tmp_path, kind):
    vr = _validate(tmp_path, kind, [base_row(kind, 1)])
    assert vr.issues == [] and vr.records[0]["label"] is None and vr.records[0]["representative"] is None
    vr = _validate(tmp_path, kind, [base_row(kind, 1)], drop_columns=("note",))
    assert _issue_columns(vr) == ["note"] and vr.records == []


def test_duplicate_id_and_natural_key_across_files(tmp_path):
    cert = write_book(tmp_path / "c.xlsx", "certification", [
        base_row("certification", 1), base_row("certification", 1, source_ko="다른 말"),
    ])
    phrase = write_book(tmp_path / "p.xlsx", "phrase", [
        # 정규화 후 인증 2번과 같은 자연키
        base_row("phrase", 1, source_ko="같은 말", internal_category="Sunscreens & Tanning"),
    ])
    cert2 = write_book(tmp_path / "c2.xlsx", "certification", [
        base_row("certification", 2, source_ko="같은 말", internal_category="Sunscreens & Tanning Products"),
    ])
    vr = validate_files({"certification": str(cert)})
    assert any("원본 ID 중복" in i.message for i in vr.issues)
    vr = validate_files({"certification": str(cert2), "phrase": str(phrase)})
    assert any("자연키 중복" in i.message for i in vr.issues)


def test_one_bad_row_fails_the_whole_file(tmp_path, capsys):
    path = write_book(tmp_path / "c.xlsx", "certification", [
        base_row("certification", 1), base_row("certification", 2, enforcement="?"),
    ])
    assert main(["validate", "--certification", str(path)]) == 1
    assert "결과: 실패" in capsys.readouterr().out


def test_cli_requires_a_file():
    with pytest.raises(SystemExit):
        main(["validate"])


# ── 실제 전달본 (있을 때만) ────────────────────────────────────────────

def _real_files():
    d = os.getenv("GLOSSARY_XLSX_DIR")
    if not d:
        return None
    found = {}
    for kind in ("certification", "ingredient", "phrase"):
        m = sorted(glob.glob(os.path.join(d, f"glossary_{kind}*.xlsx")))
        if not m:
            return None
        found[kind] = m[0]
    return found


@pytest.mark.skipif(_real_files() is None, reason="GLOSSARY_XLSX_DIR 없음")
def test_real_delivery_2026_09_28():
    vr = validate_files(_real_files())
    assert vr.issues == []
    counts = {f.kind: len(f.records) for f in vr.files}
    assert counts == {"certification": 22, "ingredient": 20566, "phrase": 65}
    phrase = next(f for f in vr.files if f.kind == "phrase")
    assert phrase.excluded == {"구역=C": 16, "구역=보류": 8, "구역=이관": 1, "구역=A·적재여부=N": 2}
    assert sorted(e for e, r in phrase.excluded_ids.items() if r == "구역=A·적재여부=N") == ["GL-M004", "GL-M050"]
    recs = {r["external_id"]: r for r in vr.records}
    assert len(recs) == 20653
    assert sum(1 for r in recs.values() if len(r["term_target"]) > 200) == 41
    assert {e for e, r in recs.items() if len(r["term_ko"]) > 200} == {"GL-I17747", "GL-I18240"}
    assert sorted(e for f in vr.files for e in f.category_normalized) == [
        "GL-C010", "GL-C013", "GL-M015", "GL-M018", "GL-M019", "GL-M039", "GL-M040", "GL-M042"]
    assert recs["GL-M043"]["term_target"] == "hyperpigmentation; discoloration"
    assert recs["GL-M056"]["term_target"] == "breakouts; blemishes"
    assert recs["GL-M038"]["term_target"] == "for oily/combination skin"
    assert recs["GL-M039"]["term_target"] == "protects against UVA/UVB rays"
    assert all(r["verified_at"] is None for r in recs.values())


V3_PHRASE_SHA256 = "db4f5b74e2e59904f6e749ca25cd84d0190e65ba4603d74c7e95efb882ecb753"


def _real_v3_phrase():
    p = os.getenv("GLOSSARY_V3_PHRASE_XLSX")
    return p if p and os.path.exists(p) else None


@pytest.mark.skipif(_real_v3_phrase() is None, reason="GLOSSARY_V3_PHRASE_XLSX 없음")
def test_real_v3_phrase_file_passes_validation():
    path = _real_v3_phrase()
    with open(path, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == V3_PHRASE_SHA256
    vr = validate_files({"phrase": path})
    assert vr.issues == []
    f = vr.files[0]
    assert f.total_rows == 555 and len(f.records) == 555 and f.excluded == {}
    cats: dict[str, int] = {}
    for r in f.records:
        cats[r["internal_category"]] = cats.get(r["internal_category"], 0) + 1
    assert cats == {"G01": 62, "G02": 31, "G03": 54, "G04": 62, "G05": 46, "G06": 86,
                    "G07": 62, "G08": 46, "G09": 35, "G10": 53, "G11": 18}
    assert all(r["label"] is None and r["representative"] is None for r in f.records)
    assert all(r["enforcement"] == "reference" and r["verified_at"] == dt.date(2026, 10, 28) for r in f.records)
