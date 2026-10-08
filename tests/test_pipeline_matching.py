"""통합 매칭 · 겹침 억제 — integration-decisions.md 5.23 기대 결과표를 그대로 옮긴 테스트(합성 값)."""
from __future__ import annotations

import pytest

from pipeline.matching import (
    MatchConflictError,
    PatternError,
    PatternSpec,
    RawMatch,
    find_matches,
    normalize_pattern,
    resolve_overlaps,
)


def M(key: str, ext: str, start: int, end: int, block: str = "b1") -> RawMatch:
    return RawMatch(ext, block, "ko", start, end, "x" * (end - start), [ext], key)


def survivors(ms, allowed=()):
    live, sup = resolve_overlaps(ms, set(allowed))
    return [m.match_key for m in live], {s.match_key: (s.rule, s.by) for s in sup}


@pytest.mark.parametrize(
    "spans, allowed, live, sup",
    [
        ([("A", 0, 10), ("B", 2, 6)], (), ["A"], {"B": ("contained", ("A",))}),
        ([("A", 0, 6), ("B", 4, 10)], (), ["A", "B"], {}),
        ([("A", 0, 4), ("B", 4, 8)], (), ["A", "B"], {}),  # 접점만 — 비겹침
        ([("A", 0, 6), ("B", 0, 6)], (), ["A", "B"], {}),  # 같은 구간의 다른 금지 항목은 모두 유지
        ([("K", 0, 10), ("A", 2, 6)], ("K",), [], {"A": ("allowed_wrap", ("K",))}),
        ([("K", 0, 6), ("A", 4, 10)], ("K",), ["A"], {}),  # 일부 겹침은 억제하지 않는다
        ([("A", 0, 12), ("K", 2, 10), ("B", 4, 8)], ("K",), ["A"], {"B": ("allowed_wrap", ("K",))}),
        ([("A", 0, 12), ("B", 2, 10), ("C", 4, 8)], (), ["A"], {"B": ("contained", ("A",)), "C": ("contained", ("A",))}),
        ([("A", 0, 6), ("B", 0, 10), ("C", 4, 12)], (), ["B", "C"], {"A": ("contained", ("B",))}),
    ],
)
def test_overlap_table(spans, allowed, live, sup):
    ms = [M(k, k, s, e) for k, s, e in spans]
    got_live, got_sup = survivors(ms, allowed)
    assert got_live == live
    assert got_sup == sup


def test_same_span_allowed_and_forbidden_is_conflict():
    with pytest.raises(MatchConflictError):
        resolve_overlaps([M("K", "K", 0, 6), M("A", "A", 0, 6)], {"K"})


def test_input_order_does_not_change_result():
    ms = [M("C", "C", 4, 8), M("B", "B", 2, 10), M("A", "A", 0, 12)]
    assert survivors(ms)[0] == ["A"]


def test_different_blocks_do_not_interact():
    live, sup = resolve_overlaps([M("A", "A", 0, 10, "b1"), M("B", "B", 2, 6, "b2")], set())
    assert [m.match_key for m in live] == ["A", "B"] and sup == []


def spans(text, pats, lang="ko", price=False):
    return [(m.start, m.end, m.matched_text) for m in find_matches("b", text, [PatternSpec("X", p, price) for p in pats], lang)]


def test_korean_left_boundary_rejects_inside_word():
    assert spans("연구원", ["원"]) == []
    assert spans("연구원", ["원"], price=True) == []  # 가격 예외도 임의 단어 뒤 '원'은 허용하지 않는다


def test_korean_price_suffix_exception():
    assert spans("가격 3만원", ["원", "만원"], price=True) == [(4, 6, "만원"), (5, 6, "원")]
    assert spans("1,000원", ["원"], price=True) == [(5, 6, "원")]
    assert spans("3만원", ["원"]) == []  # 가격 예외가 아닌 항목은 경계 규칙 그대로


def test_korean_ignores_whitespace_and_keeps_raw_span():
    assert spans("ab cd", ["abcd"]) == [(0, 5, "ab cd")]
    assert spans("ab\ncd", ["abcd"]) == [(0, 5, "ab\ncd")]  # LF 보존, 구간에 포함
    assert spans("가짜 치료", ["가짜치료"]) == [(0, 5, "가짜 치료")]


def test_korean_right_side_particles_allowed():
    assert spans("가짜치료를", ["가짜치료"]) == [(0, 4, "가짜치료")]


def test_english_rules():
    assert spans("ab cd", ["abcd"], "en") == []
    assert spans("ab\ncd", ["ab cd"], "en") == [(0, 5, "ab\ncd")]
    assert spans("ALPHA-BETA", ["alpha beta"], "en") == [(0, 10, "ALPHA-BETA")]
    assert spans("alphabet", ["alpha"], "en") == []
    assert spans("Fake  cure!", ["fake cure"], "en") == [(0, 10, "Fake  cure")]


def test_partial_expansion_is_not_matched():
    assert spans("10㎖", ["m"], "en") == []  # ㎖ → ml 의 일부만 매칭하지 않는다
    assert spans("10㎖", ["ml"], "en") == []  # 숫자에 붙은 단어 경계
    assert spans("10 ㎖", ["ml"], "en") == [(3, 4, "㎖")]


def test_same_item_same_span_counted_once_with_all_patterns():
    ms = find_matches("b", "가짜 방수", [PatternSpec("X", "가짜방수"), PatternSpec("X", "가짜 방수")], "ko")
    assert len(ms) == 1 and ms[0].patterns == ["가짜방수", "가짜 방수"]


def test_empty_pattern_rejected():
    with pytest.raises(PatternError):
        normalize_pattern("  \n", "ko")
    with pytest.raises(PatternError):
        normalize_pattern(" - ", "en")


def test_patterns_are_literal_not_regex():
    assert spans("a.b", ["a.b"], "en") == [(0, 3, "a.b")]
    assert spans("axb", ["a.b"], "en") == []
