"""⑧ 용어집 선별 · 강제 용어 적용 대상 — 통합 D10 규칙(합성 값)."""
from __future__ import annotations

import pytest

from pipeline import glossary as gl
from pipeline.stages import text_check as tc


def E(gid: str, ko: str, en: str = "x", enf: str = "enforced") -> gl.Entry:
    return gl.Entry(gid, ko, en, enf)


def ids(blocks, entries):
    return [e.glossary_id for e in gl.select(blocks, entries)]


def test_only_terms_in_target_blocks_are_selected():
    terms = [E("1", "병풀"), E("2", "녹차"), E("3", "산뜻한", enf="reference")]
    assert ids([("b1", "병풀 진정 크림"), ("b2", "산뜻한 마무리")], terms) == ["1", "3"]


def test_zero_hits_is_empty_success():
    assert gl.select([("b1", "아무 관련 없는 문장")], [E("1", "병풀")]) == []
    assert gl.select([], [E("1", "병풀")]) == []


def test_shared_korean_rules_boundary_whitespace_nfkc_particles():
    terms = [E("1", "원"), E("2", "가짜 치료"), E("3", "ＡＢＣ"), E("4", "녹차")]
    assert ids([("b", "연구원")], terms) == []  # 어절 안쪽은 왼쪽 경계 실패
    assert ids([("b", "가짜치료")], terms) == ["2"]  # 공백 무시
    assert ids([("b", "abc 성분")], terms) == ["3"]  # NFKC · 소문자
    assert ids([("b", "녹차를 담은")], terms) == ["4"]  # 오른쪽 조사 허용(형태소 분석 없음)
    assert ids([("b", "(녹차)")], terms) == ["4"]  # 구두점은 단어 문자가 아니다


def test_longer_enforced_term_suppresses_contained_one_per_position():
    terms = [E("short", "병풀"), E("long", "병풀추출물")]
    hits = gl.block_hits("b", "병풀추출물", terms)
    assert [(h.glossary_id, h.suppressed_by) for h in hits] == [("short", ("long",)), ("long", ())]
    assert ids([("b", "병풀추출물")], terms) == ["long"]
    # 같은 블록 다른 위치의 독립 출현은 유지
    assert ids([("b", "병풀추출물, 병풀")], terms) == ["short", "long"]
    # 다른 블록에서 독립 출현해도 합집합에 들어간다
    assert ids([("b1", "병풀추출물"), ("b2", "병풀 크림")], terms) == ["short", "long"]


def test_reference_terms_are_not_suppressed_and_do_not_suppress():
    assert ids([("b", "병풀추출물")], [E("s", "병풀"), E("l", "병풀추출물", enf="reference")]) == ["s", "l"]
    assert ids([("b", "병풀추출물")], [E("s", "병풀", enf="reference"), E("l", "병풀추출물")]) == ["s", "l"]


def test_same_span_and_partial_overlap_are_kept():
    # 같은 구간은 포함 억제하지 않는다. 같은 구간에 맞는 서로 다른 용어는 정규화 원문이 같으므로 충돌 오류(임의 선택 없음)
    with pytest.raises(gl.GlossaryDataError):
        gl.select([("b", "가짜 치료")], [E("1", "가짜치료"), E("2", "가짜 치료")])
    # 부분 겹침은 둘 다 유지
    assert ids([("b", "알로에베라겔")], [E("1", "알로에베라"), E("2", "베라겔")]) == ["1"]  # '베라겔'은 왼쪽 경계 실패
    assert ids([("b", "알로에 베라겔")], [E("1", "알로에베라"), E("2", "베라겔")]) == ["1", "2"]


def test_same_id_dedup_only_and_conflicts_rejected():
    assert ids([("b", "병풀")], [E("1", "병풀"), E("1", "병풀")]) == ["1"]
    with pytest.raises(gl.GlossaryDataError):
        gl.select([("b", "병풀")], [E("1", "병풀"), E("1", "병풀", en="other")])
    # 분류가 달라 따로 적재된 같은 원문 두 행 — 하나를 고르지 않고 실패
    with pytest.raises(gl.GlossaryDataError, match="임의로 고르지 않는다"):
        gl.select([("b", "자외선차단")], [E("10", "자외선차단", "sunscreen"), E("20", "자외선 차단", "UV protection")])
    # 선택되지 않으면 충돌이 아니다
    assert ids([("b", "녹차")], [E("10", "자외선차단"), E("20", "자외선 차단"), E("30", "녹차")]) == ["30"]


@pytest.mark.parametrize("bad", [E("1", "  "), E("1", "병풀", en=" "), E("1", "병풀", enf="강제"), E("", "병풀")])
def test_invalid_data_is_error_not_empty(bad):
    with pytest.raises(gl.GlossaryDataError):
        gl.select([("b", "병풀")], [bad])


def test_deterministic_order_follows_input():
    terms = [E("9", "녹차"), E("2", "병풀")]
    assert ids([("b", "병풀 녹차")], terms) == ["9", "2"]
    assert ids([("b", "병풀 녹차")], terms) == ids([("b", "병풀 녹차")], list(terms))


def test_text_check_uses_same_suppression():
    terms = [tc.GlossaryTerm("short", "병풀", "Centella", "enforced"),
             tc.GlossaryTerm("long", "병풀추출물", "Gotu Kola Extract", "enforced")]
    r = tc.check_block("b", 0, "with Gotu Kola Extract", "병풀추출물", [], terms)
    assert r["terms"] == [{"glossary_id": "long", "status": "applied"}]  # 포함된 짧은 용어는 적용 대상 아님(오탐 없음)
    assert r["flags"] == []
    r = tc.check_block("b", 0, "with Gotu Kola Extract", "병풀추출물, 병풀", [], terms)
    assert {t["glossary_id"]: t["status"] for t in r["terms"]} == {"short": "unapplied", "long": "applied"}
    assert r["rules"]["terms"] == tc.TERM_RULES_VERSION and r["rules"]["glossary_select"] == gl.SELECT_RULES_VERSION


def test_selection_matches_check_applicability():
    """BE 공급(선별) 결과와 AI 검사의 적용 대상이 블록마다 같다 — 선별에서 빠진 강제 용어를 검사가 요구하지 않는다."""
    entries = [E("1", "병풀"), E("2", "병풀추출물"), E("3", "녹차"), E("4", "녹차추출물", enf="reference")]
    terms = [tc.GlossaryTerm(e.glossary_id, e.term_ko, e.term_target, e.enforcement) for e in entries]
    for src in ["병풀추출물", "병풀추출물과 병풀", "녹차추출물", "관련 없음"]:
        supplied = {e.glossary_id for e in gl.select([("b", src)], entries) if e.enforcement == "enforced"}
        checked = {t["glossary_id"] for t in tc.check_block("b", 0, "none", src, [], terms)["terms"]}
        assert supplied == checked, src


def test_text_check_rejects_normalized_conflict_like_supply():
    terms = [tc.GlossaryTerm("1", "자외선차단", "sunscreen", "enforced"), tc.GlossaryTerm("2", "자외선 차단", "UV protection", "enforced")]
    with pytest.raises(gl.GlossaryDataError):
        tc.check_block("b", 0, "sunscreen", "자외선 차단 크림", [], terms)
