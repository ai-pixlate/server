"""③ OCR 후처리 검증 — open-questions #86, pipeline.md 7.1 · 7.2절. 기대값은 규칙에서 손으로 계산했다.

- `merge.xycut`: 여백(XY-cut) 후보 영역 → 세로 중심 가로줄 → gutter로만 Line 조각(높이비 안 봄, 2026-10-23) → 기존 블록 병합.
  최소 여백 = xycut_gap_ratio(0.8) × 섹션 텍스트 높이 중앙값, 가로줄 허용치 = max(8, 0.5 × 영역 높이 중앙값).
- `merge.fullwidth`: Line.text · source_ko의 U+FF01~FF5E · U+3000만 반각. 원시 영역은 그대로.
- `merge.num_o`: Line.text · source_ko의 숫자 덩어리 안 영문 o/O → 0(전각 변환 뒤, 2026-10-26). 원시 영역은 그대로.
기본 config 값은 test_pipeline_merge.py 머리 참고(높이 20 → 블록 세로 간격 [-6, 12]).
"""
import json
import math
import random
from collections import Counter

import pytest

from pipeline import config as cfgmod
from pipeline.stages import merge
from pipeline.types import BBox, OcrRegion, OcrResult, Section
from pipeline.vlm import LlmReply


def R(key: str, x: int, y: int, w: int, h: int, text: str = "가", score: float = 0.9) -> OcrRegion:
    return OcrRegion(
        region_key=key, text=text, score=score,
        poly=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)], bbox=BBox(x=x, y=y, w=w, h=h),
    )


def SEC(width: int = 1000, height: int = 2000) -> Section:
    return Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=height,
                   width=width, image_path="unused.png")


def CFG(**over):
    cfg = cfgmod.load_config()
    cfg["merge"].update(over)
    return cfg


# 기존(꺼진) 경로를 검사할 때 명시한다 — 기본값은 xycut · num_o 켬, fullwidth 끔(2026-10-26)
OFF = {"xycut": False, "fullwidth": False, "num_o": False}


def OCR(regions) -> OcrResult:
    return OcrResult(section_key="sec_1_01", regions=regions)


def run(regions, width: int = 1000, **over):
    return merge.run(SEC(width), OCR(regions), CFG(**over), use_llm=False)


def texts(res):
    return [b.source_ko for b in res.blocks]


def keys(res):
    return [[r.region_key for ln in b.source_lines for r in ln.regions] for b in res.blocks]


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def test_switch_defaults_turn_on_xycut_and_num_o_only():
    m = CFG()["merge"]  # 2026-10-26 기본 활성화 결정: fullwidth는 현재 인식 모델에서 효과가 없어 끈다
    assert m["xycut"] is True and m["num_o"] is True and m["fullwidth"] is False
    assert (m["xycut_gap_ratio"], m["line_center_tol"], m["line_center_tol_min_px"]) == (0.8, 0.5, 8)


@pytest.mark.parametrize("over", [{"xycut": True}, {"fullwidth": True}, {"num_o": True}, {"line_center_tol_min_px": 0},
                                  {"xycut_gap_ratio": 0.01}, {"line_center_tol": 3}])
def test_postprocess_config_boundaries_are_accepted(over):
    merge.validate_config(CFG(**over))


@pytest.mark.parametrize(
    "over",
    [
        {"xycut": 1}, {"xycut": "true"}, {"xycut": None}, {"fullwidth": 0}, {"fullwidth": "false"}, {"num_o": 1},
        {"num_o": "true"},
        {"xycut_gap_ratio": True}, {"xycut_gap_ratio": "0.8"}, {"xycut_gap_ratio": math.nan},
        {"line_center_tol": math.inf}, {"line_center_tol_min_px": -math.inf}, {"line_center_tol_min_px": False},
        {"xycut_gap_ratio": 0}, {"xycut_gap_ratio": -0.1}, {"line_center_tol": 0}, {"line_center_tol_min_px": -1},
    ],
)
def test_postprocess_config_bad_values_are_rejected(over):
    with pytest.raises(ValueError):
        merge.validate_config(CFG(**over))


@pytest.mark.parametrize("k", ["xycut", "fullwidth", "num_o", "xycut_gap_ratio", "line_center_tol", "line_center_tol_min_px"])
def test_postprocess_config_missing_key_is_rejected(k):
    cfg = CFG()
    del cfg["merge"][k]
    with pytest.raises(ValueError, match=k):
        merge.validate_config(cfg)


# ---------------------------------------------------------------------------
# 전각 변환
# ---------------------------------------------------------------------------
def test_to_halfwidth_converts_only_ff01_ff5e_and_ideographic_space():
    assert merge.to_halfwidth("ＴＯＥ　ＢＲＯＮＺＥ １００％，ＳＰＦ５０＋") == "TOE BRONZE 100%,SPF50+"
    assert merge.to_halfwidth("！～") == "!~"  # 범위 양 끝 U+FF01 · U+FF5E
    keep = "①™ㄱㅏ￦｡ｱ가나"  # 원문자 · 상표 · 한글 자모 · U+FFE6 · 반각 가타카나(범위 밖)는 그대로
    assert merge.to_halfwidth(keep) == keep


def test_fullwidth_changes_line_text_and_source_ko_but_not_raw_regions():
    regs = [R("reg_0001", 0, 0, 200, 20, "ＴＯＥ　ＢＲＯＮＺＥ"), R("reg_0002", 210, 0, 100, 20, "１００％")]
    off = run(regs)
    on = run(regs, fullwidth=True)
    assert texts(off) == ["ＴＯＥ　ＢＲＯＮＺＥ １００％"]
    assert texts(on) == ["TOE BRONZE 100%"]
    assert on.blocks[0].source_lines[0].text == "TOE BRONZE 100%"
    assert [r.model_dump() for r in on.blocks[0].source_lines[0].regions] == [r.model_dump() for r in regs]  # 원시 그대로
    assert len(on.blocks[0].source_ko) == len(off.blocks[0].source_ko)  # 같은 조립 문자열이면 길이 유지


def test_ideographic_space_only_region_stays_empty_text_block():
    regs = [R("reg_0001", 0, 0, 100, 20, "가나"), R("reg_0002", 0, 200, 50, 20, "　　", 0.0)]
    res = run(regs, fullwidth=True)
    assert texts(res) == ["가나", ""]  # strip()으로 이미 빈 텍스트 — 변환과 무관하게 단독 블록


def test_fullwidth_is_independent_of_xycut():
    regs = [R("reg_0001", 0, 0, 100, 20, "Ａ"), R("reg_0002", 0, 300, 100, 20, "Ｂ")]
    assert texts(run(regs, fullwidth=True, xycut=True)) == ["A", "B"]
    assert texts(run(regs, fullwidth=False, xycut=True)) == ["Ａ", "Ｂ"]


# ---------------------------------------------------------------------------
# 숫자 속 o/O 교정(2026-10-26 채택 — 참고 구현 NUM_WITH_O에서 시작 조건만 좁힘)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    ("SPF 5O+", "SPF 50+"), ("1oo%", "100%"), ("13.O", "13.0"), ("1o,ooo", "10,000"), ("(2o17))", "(2017))"),
    ("MATRIXYL°300O*3%", "MATRIXYL°3000*3%"), ("*MATRIXTYL@300O:팔미토일", "*MATRIXTYL@3000:팔미토일"),
    ("1,5O0", "1,500"), ("3.5O%", "3.50%"), ("SPF 50O", "SPF 500"),  # 덩어리 안의 . , 뒤 o/O는 교정
    # 바꾸지 않는 것: 한글이 붙은 숫자 · 정상 단위 · 숫자 없는 o
    ("3o개", "3o개"), ("2oz", "2oz"), ("10 Oz", "10 Oz"), ("No.1", "No.1"), ("goodal", "goodal"), ("o50U5S0N", "o50U5S0N"),
    # 경계: 영문자 · 숫자 · 점 · 쉼표 바로 뒤에서 시작하는 덩어리는 바꾸지 않는다 — 참고 구현(영문자만 막음)은
    # 영문에 붙은 숫자열의 다음 숫자부터 다시 매칭해 SPF50O → SPF500, B60O1 → B6001, AB12O → AB120으로 바꿨다
    ("SPF5O", "SPF5O"), ("B6O1", "B6O1"), ("SPF50O", "SPF50O"), ("B60O1", "B60O1"), ("AB12O", "AB12O"),
    ("v1.2O", "v1.2O"), ("No.1O", "No.1O"), ("x,2O", "x,2O"),
])
def test_fix_num_o_rule_and_boundaries(raw, want):
    assert merge.fix_num_o(raw) == want
    assert len(merge.fix_num_o(raw)) == len(raw)  # 1:1 치환


def test_num_o_changes_line_text_and_source_ko_but_not_raw_regions():
    regs = [R("reg_0001", 0, 0, 200, 20, "특허 성분MATRIXYL°300O*3%")]
    off, on = run(regs, num_o=False), run(regs, num_o=True)
    assert texts(off) == ["특허 성분MATRIXYL°300O*3%"] and texts(on) == ["특허 성분MATRIXYL°3000*3%"]
    assert on.blocks[0].source_lines[0].regions[0].text == "특허 성분MATRIXYL°300O*3%"  # 원시 그대로
    assert [b.role for b in on.blocks] == [b.role for b in off.blocks]


def test_num_o_runs_after_fullwidth_and_is_independent():
    regs = [R("reg_0001", 0, 0, 100, 20, "５Ｏ％")]
    assert texts(run(regs, num_o=True)) == ["５Ｏ％"]  # 전각은 정규식 밖 — 전각 변환을 켜야 반각이 된 뒤 고친다
    assert texts(run(regs, fullwidth=True, num_o=False)) == ["5O%"]
    assert texts(run(regs, fullwidth=True, num_o=True)) == ["50%"]
    assert texts(run(regs, num_o=True, xycut=True)) == ["５Ｏ％"]


# ---------------------------------------------------------------------------
def test_widest_gap_picks_widest_and_earliest_on_tie():
    assert merge.widest_gap([(0, 10), (30, 40), (60, 70)]) == (20.0, 20.0)
    assert merge.widest_gap([(0, 10), (5, 20), (50, 60)]) == (30.0, 35.0)  # 덮인 구간(reach)부터 잰다
    assert merge.widest_gap([(0, 10), (5, 20)]) == (0.0, None)


def test_cut_tree_keeps_branches_and_sends_cut_value_to_high_side():
    # 루트: y 여백 280(100~380) > x 여백 200 → y = 240. 위쪽 A · B · C: x 여백 200 > y 여백 60 → x = 200.
    # 왼쪽 A · B: y 여백 60 → y = 50. 잎 순서(왼쪽 · 위 먼저): A, B, C, D
    A, B = R("a", 0, 0, 100, 20), R("b", 0, 80, 100, 20)
    C, D = R("c", 300, 0, 100, 20), R("d", 300, 380, 100, 20)
    tree, leaves = merge.build_cut_tree([A, B, C, D], 16)
    assert [[r.region_key for r in leaf] for leaf in leaves] == [["a"], ["b"], ["c"], ["d"]]
    assert (tree.axis, tree.value) == ("y", 240)
    assert merge.locate(tree, 350, 60) == 2  # 위쪽 → x ≥ 200 → C. 왼쪽 가지의 y = 50 절단을 적용하지 않는다
    assert merge.locate(tree, 50, 50) == 1 and merge.locate(tree, 50, 49) == 0  # 절단값과 같으면 아래(high)
    assert merge.locate(tree, 200, 0) == 2 and merge.locate(tree, 0, 240) == 3


def test_column_groups_join_leaves_only_through_y_cuts():
    # 위 트리(루트 y → 위쪽 x → 왼쪽 y): A · B 사이 경로는 왼쪽 y 절단뿐 → 같은 칸 묶음. A · C는 위쪽 x 절단,
    # D와 나머지는 경로에 위쪽 x 절단이 끼어 각각 다른 묶음 → [1, 1, 2, 3]
    A, B = R("a", 0, 0, 100, 20), R("b", 0, 80, 100, 20)
    C, D = R("c", 300, 0, 100, 20), R("d", 300, 380, 100, 20)
    assert merge.column_groups(merge.build_cut_tree([A, B, C, D], 16)[0]) == [1, 1, 2, 3]
    # y 절단만: 잎 셋이 한 묶음. x 절단 아래 칸마다 y 절단: 칸마다 한 묶음
    ys = [R(f"y{i}", 0, i * 100, 100, 20) for i in range(3)]
    assert merge.column_groups(merge.build_cut_tree(ys, 16)[0]) == [1, 1, 1]
    cols = [R("l1", 0, 0, 100, 20), R("l2", 0, 100, 100, 20), R("r1", 400, 0, 100, 20), R("r2", 400, 100, 100, 20)]
    assert merge.column_groups(merge.build_cut_tree(cols, 16)[0]) == [1, 1, 2, 2]
    assert merge.column_groups(0) == [1]  # 절단 없는 트리(잎 하나)


def full_width_title_over_two_columns():
    # 전폭 제목 아래 두 칸: 루트 y 절단(제목 | 아래), 아래는 x 절단(왼칸 | 오른칸) — 혼합 절단
    return [R("t", 0, 0, 900, 20, "제목"), R("l", 0, 200, 300, 20, "왼칸"), R("r", 600, 200, 300, 20, "오른칸")]


def test_column_group_needs_neighbouring_leaves_not_only_y_path():
    # 제목(위) · 두 칸(가운데, x 절단) · 문단(아래): 루트 y(제목 | 나머지) → y(칸 | 문단). 제목과 문단 사이 경로는 y 절단뿐
    # 이지만 읽기 순서에서 그 사이에 칸이 끼므로 잇지 않는다(실험 yonly와 같은 정의) → 모두 다른 칸 묶음
    regs = [R("t", 0, 0, 900, 20, "제목"), R("l", 0, 200, 300, 20, "왼칸"), R("r", 600, 200, 300, 20, "오른칸"),
            R("b", 0, 400, 900, 20, "문단")]
    tree, leaves = merge.build_cut_tree(regs, 16)
    assert [[r.region_key for r in leaf] for leaf in leaves] == [["t"], ["l"], ["r"], ["b"]]
    assert merge.column_groups(tree) == [1, 2, 3, 4]


def test_title_over_columns_is_not_one_column_group():
    # 제목과 아래 칸 사이 경로(공통 조상 루트 y → 아래 x)에 x 절단이 끼므로 제목 · 왼칸 · 오른칸 모두 다른 칸 묶음
    tree, leaves = merge.build_cut_tree(full_width_title_over_two_columns(), 16)
    assert [[r.region_key for r in leaf] for leaf in leaves] == [["t"], ["l"], ["r"]]
    assert merge.column_groups(tree) == [1, 2, 3]


def test_columns_are_read_left_region_first_even_if_right_starts_higher():
    # 오른쪽 칸이 10px 위에서 시작. 기본은 블록 (y, x) 순이라 오른쪽이 먼저, xycut은 x 여백 100 ≥ 16으로 칸을 나눠 왼쪽 먼저
    regs = [R("l1", 50, 10, 400, 20, "왼1"), R("l2", 50, 38, 400, 20, "왼2"),
            R("r1", 550, 0, 400, 20, "오1"), R("r2", 550, 28, 400, 20, "오2")]
    assert texts(run(regs, **OFF)) == ["오1\n오2", "왼1\n왼2"]
    assert texts(run(regs, xycut=True)) == ["왼1\n왼2", "오1\n오2"]


def test_many_regions_do_not_hit_recursion_limit():
    regs = [R(f"reg_{i:04d}", 0, i * 40, 100, 20) for i in range(1500)]  # 줄마다 y 여백 20 ≥ 16 → 잎 1500개
    res = run(regs, xycut=True)
    assert len(res.blocks) == 1500 and keys(res)[0] == ["reg_0000"] and keys(res)[-1] == ["reg_1499"]


# ---------------------------------------------------------------------------
# 세로 중심 가로줄 · Line 조각
# ---------------------------------------------------------------------------
def lip_stain_set():
    # 같은 가로줄의 세 단어(위쪽 y 422 · 427 · 423, 가로 간격 284 · 277 > 14)와 그 아래 전폭 줄(x 여백을 막는다).
    # 세로 여백 460 − 447 = 13 < 16이라 후보 영역은 하나
    return [R("lip", 0, 422, 100, 20, "Lip"), R("stain", 384, 427, 100, 20, "Stain"), R("set", 761, 423, 100, 20, "Set"),
            R("next", 0, 460, 900, 20, "아래 문장")]


def test_same_row_words_with_wide_gaps_keep_left_to_right_order():
    # 기본: 가로 간격 상한에 막혀 단어마다 블록, (y, x) 순이라 Lip · Set · Stain으로 뒤집힌다
    assert texts(run(lip_stain_set(), **OFF)) == ["Lip", "Set", "Stain", "아래 문장"]
    # xycut: 세로 중심 432 · 433 · 437은 기준 432와의 차 ≤ 10 → 한 가로줄, x순 · 높이비 1 · gutter 없음 → 한 Line
    assert texts(run(lip_stain_set(), xycut=True)) == ["Lip Stain Set", "아래 문장"]


def test_row_base_is_the_first_center_and_is_not_recomputed():
    # 중심 10 · 19 · 28: 기준 10 → 19(차 9 ≤ 10)는 같은 줄, 28(차 18)은 새 줄. 이동 평균이면 28도 붙을 수 있다
    regs = [R("a", 0, 0, 100, 20, "가"), R("b", 200, 9, 100, 20, "나"), R("c", 400, 18, 100, 20, "다")]
    rows = merge.reading_lines(regs, CFG()["merge"])
    assert [(base, [r.region_key for r in rs]) for base, rs in rows] == [(10.0, ["a", "b"]), (28.0, ["c"])]


def test_height_ratio_does_not_split_a_row():
    # 같은 중심 20의 "98"(높이 40)과 "%"(높이 20): 높이비 2지만 xycut의 Line 나누기는 높이비를 보지 않는다(2026-10-23) → 한 Line
    regs = [R("num", 100, 0, 60, 40, "98"), R("pct", 165, 10, 20, 20, "%")]
    assert texts(run(regs, xycut=True)) == ["98 %"]
    assert texts(run(regs, **OFF)) == ["98", "%"]  # 끄면 기존 줄 병합(높이비 1.5 상한) 그대로


def test_superscript_marker_stays_at_the_end_of_its_row():
    # 줄(높이 40, 중심 20) 끝 위쪽에 붙은 각주 기호(높이 15, 중심 7.5). 허용치 = max(8, 0.5 × 중앙값 27.5) = 13.75,
    # 차 12.5 → 같은 가로줄. 높이비 2.7이지만 끊지 않는다 → 기호가 대응 줄 끝에 붙는다
    regs = [R("body", 0, 0, 400, 40, "트리플 징크 콤플렉스"), R("mark", 405, 0, 20, 15, "**")]
    assert texts(run(regs, xycut=True)) == ["트리플 징크 콤플렉스 **"]


def test_small_overlapping_fragment_does_not_break_the_row():
    # 큰 글자(높이 87 · 79 · 80) 사이에 낀 작은 중첩 영역(높이 45, #40 조각 후보 — 텍스트 그대로 보존).
    # 중심 318.5 · 318.5 · 319.5 · 319 → 한 가로줄, gutter 없음 → 한 Line(조각 텍스트는 줄 안에 남는다)
    regs = [R("r2", 151, 275, 141, 87, "이런"), R("r3", 297, 279, 279, 79, "분들에게"),
            R("r5", 545, 297, 55, 45, "I", 0.28), R("r4", 573, 279, 271, 80, "추천해요")]
    res = run(regs, xycut=True)
    assert texts(res) == ["이런 분들에게 I 추천해요"]
    assert [r.region_key for r in res.blocks[0].source_lines[0].regions] == ["r2", "r3", "r5", "r4"]


def two_columns_inside_one_region():
    # 폭 320, 높이 50 글자 → 최소 여백 40. 칸 사이 30px(≥ gutter 최소 20, < 40) — 같은 후보 영역 안의 gutter 두 칸
    return [R("l1", 0, 0, 140, 50, "왼1"), R("r1", 170, 0, 130, 50, "오1"),
            R("l2", 0, 60, 140, 50, "왼2"), R("r2", 170, 60, 130, 50, "오2")]


def test_gutter_splits_a_row_and_columns_stay_separate_blocks():
    res = run(two_columns_inside_one_region(), width=320, xycut=True)
    assert texts(res) == ["왼1\n왼2", "오1\n오2"]


# ---------------------------------------------------------------------------
# 단독 블록 배치 · 보존
# ---------------------------------------------------------------------------
def test_standalone_block_goes_after_rows_whose_base_is_not_below_its_center():
    # 텍스트 두 줄(y 0 · 100) 사이 여백 80 → y = 60 절단. 빈 영역 E(y 50, 중심 60)는 위 영역, k = 1 → 첫 줄 뒤.
    # 크기 0 영역 Z(y 60)는 절단값과 같아 아래 영역, 중심 70 < 기준 110 → k = 0 → 둘째 줄 앞
    regs = [R("t1", 0, 0, 100, 20, "첫"), R("t2", 0, 100, 100, 20, "둘"),
            R("e", 300, 50, 40, 20, "", 0.0), R("z", 300, 60, 0, 20, "글", 0.5)]
    res = run(regs, xycut=True)
    assert keys(res) == [["t1"], ["e"], ["z"], ["t2"]]


def test_standalone_does_not_enter_a_multi_line_paragraph():
    # 두 줄 문단(y 0 · 28, 블록 하나) 사이 높이의 빈 영역은 문단 뒤로 간다 — 블록 시작 가로줄 기준 배치
    regs = [R("p1", 0, 0, 400, 20, "문단 첫 줄"), R("p2", 0, 28, 400, 20, "문단 둘째 줄"), R("e", 500, 20, 40, 10, "", 0.0)]
    res = run(regs, xycut=True)
    assert keys(res) == [["p1", "p2"], ["e"]]


def test_section_without_text_regions_keeps_current_order():
    regs = [R("e1", 300, 50, 40, 20, "", 0.0), R("z1", 100, 50, 0, 20, "a", 0.5), R("e2", 0, 10, 40, 30, " ", 0.0)]
    assert run(regs, xycut=True) == run(regs)  # (위쪽 y, x, region_key) — 기존 블록 순서와 같다


def mixed_regions():
    return lip_stain_set() + two_columns_inside_one_region() + [
        R("e1", 950, 430, 30, 10, "", 0.0), R("z1", 0, 1500, 0, 30, "x", 0.2),
        R("big", 0, 900, 600, 60, "큰 제목"), R("small", 0, 980, 300, 12, "작은 설명"),
    ]


def test_every_region_is_preserved_once_and_unchanged_with_xycut():
    regs = mixed_regions()
    res = run(regs, xycut=True, fullwidth=True)
    out = [r for b in res.blocks for ln in b.source_lines for r in ln.regions]
    assert Counter(r.region_key for r in out) == Counter(r.region_key for r in regs)
    by_key = {r.region_key: r for r in regs}
    assert all(r.model_dump() == by_key[r.region_key].model_dump() for r in out)
    assert [b.block_order for b in res.blocks] == list(range(1, len(res.blocks) + 1))


def test_xycut_result_does_not_depend_on_input_order():
    regs = mixed_regions()
    shuffled = regs[:]
    random.Random(7).shuffle(shuffled)
    assert run(shuffled, xycut=True) == run(regs, xycut=True)


# ---------------------------------------------------------------------------
# llm_assist 연결
# ---------------------------------------------------------------------------
class FakeLlm:
    """payload 텍스트로 묶음을 정한다. plan: [(텍스트 목록, 역할)]."""

    def __init__(self, plan):
        self.config = {"model": "fake", "temperature": 0, "timeout_s": 60}
        self.plan = plan
        self.calls = []

    def __call__(self, prompt, payload):
        self.calls.append((prompt, payload))
        by_text = {b["text"]: b["id"] for b in json.loads(payload)["blocks"]}
        blocks = [{"members": [by_text[t] for t in texts_], "role": role} for texts_, role in self.plan]
        return LlmReply(json.dumps({"blocks": blocks}, ensure_ascii=False), {"total_token_count": 1})


def run_llm(regions, plan, width=1000, **over):
    llm, recs = FakeLlm(plan), []
    res = merge.run(SEC(width), OCR(regions), CFG(**over), llm=llm, recorder=recs.append)
    return res, llm, recs[-1]


def regions_in_two_regions():
    # 높이 중앙값 20 → 최소 여백 16. 제목(높이 40) 아래 여백 160 → y 절단. 아래 영역은 본문 두 줄:
    # 세로 간격 14는 블록 상한 12를 넘어 블록 둘이지만 최소 여백 16보다 작아 같은 후보 영역
    return [R("t", 0, 0, 300, 40, "피부 진정"), R("b1", 0, 200, 400, 20, "첫째 줄"), R("b2", 0, 234, 400, 20, "둘째 줄")]


def two_columns():
    # x 여백 100 ≥ 16 → 칸 두 개(x 절단). 칸 안 세로 간격 8 → 칸마다 블록 하나
    return [R("l1", 50, 10, 400, 20, "왼1"), R("l2", 50, 38, 400, 20, "왼2"),
            R("r1", 550, 0, 400, 20, "오1"), R("r2", 550, 28, 400, 20, "오2")]


def test_payload_and_prompt_carry_region_only_when_xycut_is_on():
    _, llm, rec = run_llm(regions_in_two_regions(), [(["피부 진정"], "title"), (["첫째 줄"], "body"), (["둘째 줄"], "body")],
                          xycut=True, num_o=False)
    prompt, payload = llm.calls[0]
    # region은 칸 묶음 번호 — 후보 영역 둘(제목 | 본문)이 y 절단으로만 나뉘어 한 묶음(2026-10-26)
    assert [b["region"] for b in json.loads(payload)["blocks"]] == [1, 1, 1]
    assert prompt.endswith((merge.REPO_ROOT / merge.REGION_PROMPT_PATH).read_text(encoding="utf-8"))
    assert rec["postprocess"] == {"xycut": True, "fullwidth": False, "candidate_regions": 2}
    assert rec["result"]["region_splits"] == [] and rec["result"]["interleaved"] == []
    _, llm, rec = run_llm(regions_in_two_regions(), [(["피부 진정"], "title"), (["첫째 줄"], "body"), (["둘째 줄"], "body")],
                          **OFF)
    prompt, payload = llm.calls[0]
    assert all("region" not in b for b in json.loads(payload)["blocks"])
    assert prompt == (merge.REPO_ROOT / CFG()["merge"]["prompt_path"]).read_text(encoding="utf-8")
    assert "postprocess" not in rec and set(rec["result"]) == {"groups", "block_map"}  # 끄면 기록 형식도 기존과 같다
    _, llm, _ = run_llm(two_columns(), [(["왼1\n왼2"], "body"), (["오1\n오2"], "body")], xycut=True)
    assert [b["region"] for b in json.loads(llm.calls[0][1])["blocks"]] == [1, 2]  # 칸(x 절단)은 다른 번호


def test_same_region_merge_keeps_llm_role():
    res, _, rec = run_llm(regions_in_two_regions(), [(["피부 진정"], "title"), (["첫째 줄", "둘째 줄"], "caption")],
                          xycut=True)
    assert [(b.source_ko, b.role) for b in res.blocks] == [("피부 진정", "title"), ("첫째 줄\n둘째 줄", "caption")]
    assert rec["result"]["region_splits"] == []


def test_default_config_runs_xycut_and_num_o_llm_path():
    # 기본 설정 그대로(2026-10-26 기본 활성화): 숫자 교정된 글자가 LLM 입력으로 가고, region · 보충 프롬프트가 붙는다
    regs = [R("t", 0, 0, 300, 40, "SPF 5O+"), R("b1", 0, 200, 400, 20, "첫째 줄")]
    res, llm, rec = run_llm(regs, [(["SPF 50+"], "title"), (["첫째 줄"], "body")])
    prompt, payload = llm.calls[0]
    assert [(b["text"], b["region"]) for b in json.loads(payload)["blocks"]] == [("SPF 50+", 1), ("첫째 줄", 1)]
    assert prompt.endswith((merge.REPO_ROOT / merge.REGION_PROMPT_PATH).read_text(encoding="utf-8"))
    assert rec["postprocess"]["xycut"] is True and rec["postprocess"]["num_o"] is True
    assert texts(res) == ["SPF 50+", "첫째 줄"]
    assert res.blocks[0].source_lines[0].regions[0].text == "SPF 5O+"  # 원시 그대로


def test_y_cut_regions_can_be_merged_and_keep_llm_role():
    # 제목 | 본문 후보 영역은 y 절단으로만 나뉘어 같은 칸 묶음 → 묶음이 나뉘지 않고 LLM 역할 그대로(2026-10-26)
    res, _, rec = run_llm(regions_in_two_regions(), [(["피부 진정", "첫째 줄"], "caption"), (["둘째 줄"], "body")],
                          xycut=True)
    assert [(b.source_ko, b.role) for b in res.blocks] == [("피부 진정\n첫째 줄", "caption"), ("둘째 줄", "body")]
    assert rec["result"]["region_splits"] == [] and rec["result"]["interleaved"] == []


def test_cross_column_group_is_split_and_pieces_get_heuristic_roles():
    regs = two_columns()
    heur = merge.heuristic(SEC(), OCR(regs), CFG(xycut=True))
    heur_roles = {b.source_ko: b.role for b in heur.blocks}
    res, _, rec = run_llm(regs, [(["왼1\n왼2", "오1\n오2"], "caption")], xycut=True)
    assert texts(res) == ["왼1\n왼2", "오1\n오2"]  # 칸(x 절단)을 넘는 묶음은 칸 묶음별로 나뉜다
    assert [b.role for b in res.blocks] == [heur_roles["왼1\n왼2"], heur_roles["오1\n오2"]]
    (split,) = rec["result"]["region_splits"]
    assert split["members"] == ["blk_001", "blk_002"] and split["llm_role"] == "caption"
    assert split["parts"] == [{"region": 1, "members": ["blk_001"], "role": heur_roles["왼1\n왼2"]},
                              {"region": 2, "members": ["blk_002"], "role": heur_roles["오1\n오2"]}]


def test_title_over_columns_group_is_split():
    # 혼합 절단: 전폭 제목과 아래 왼칸은 다른 칸 묶음 → 묶음이 나뉘고 조각은 휴리스틱 역할
    regs = full_width_title_over_two_columns()
    heur_roles = {b.source_ko: b.role for b in merge.heuristic(SEC(), OCR(regs), CFG(xycut=True)).blocks}
    res, llm, rec = run_llm(regs, [(["제목", "왼칸"], "title"), (["오른칸"], "body")], xycut=True)
    assert [b["region"] for b in json.loads(llm.calls[0][1])["blocks"]] == [1, 2, 3]
    assert [(b.source_ko, b.role) for b in res.blocks] == [
        ("제목", heur_roles["제목"]), ("왼칸", heur_roles["왼칸"]), ("오른칸", "body")]
    (split,) = rec["result"]["region_splits"]
    assert [p["region"] for p in split["parts"]] == [1, 2]


def upper_rows_and_lower_rows():
    # 위 후보 영역: 세로 간격 14(블록 상한 12 초과 · 최소 여백 16 미만) → 한 영역에 블록 셋(가로줄 1 · 2 · 3).
    # 아래 영역(y 절단, 여백 212): 블록 둘(가로줄 1 · 2). 영역마다 가로줄 순번이 1부터 다시 시작한다
    return [R("u1", 0, 0, 300, 20, "위1"), R("u2", 0, 34, 300, 20, "위2"), R("u3", 0, 68, 300, 20, "위3"),
            R("d1", 0, 300, 300, 20, "아래1"), R("d2", 0, 334, 300, 20, "아래2")]


def test_cross_region_group_uses_region_first_reading_order():
    # 위 영역 가로줄 3의 블록 + 아래 영역 가로줄 1의 블록. 가로줄 순번만 보면 아래1이 앞선다 — 영역을 맨 앞에 둬야
    # 묶음 안 줄은 위 영역이 먼저, 묶음은 위 영역 블록 자리(위2 뒤 · 아래2 앞)에 놓인다
    plan = [(["위1"], "body"), (["위2"], "body"), (["위3", "아래1"], "body"), (["아래2"], "body")]
    res, _, rec = run_llm(upper_rows_and_lower_rows(), plan, xycut=True)
    assert texts(res) == ["위1", "위2", "위3\n아래1", "아래2"]
    assert rec["result"]["region_splits"] == [] and rec["result"]["interleaved"] == []  # 다른 영역끼리는 교차가 아니다
    plan = [(["위1"], "body"), (["위2", "아래2"], "body"), (["위3"], "body"), (["아래1"], "body")]
    res, _, _ = run_llm(upper_rows_and_lower_rows(), plan, xycut=True)
    assert texts(res) == ["위1", "위2\n아래2", "위3", "아래1"]  # 묶음 자리 = 가장 앞선 멤버(위2)


def test_other_response_errors_still_fail_with_xycut():
    regs = regions_in_two_regions()
    with pytest.raises(merge.LlmResponseError, match="빠진 ID"):
        run_llm(regs, [(["피부 진정", "첫째 줄"], "title")], xycut=True)


def test_merged_group_lines_are_resorted_by_row_not_block_concatenation():
    # 같은 영역의 gutter 두 칸을 LLM이 합치면 줄은 (가로줄 순번, x)로 다시 정렬돼 칸이 번갈아 섞인다 — 알려진 위험,
    # 가로줄 범위가 교차하는 묶음으로 기록된다
    res, _, rec = run_llm(two_columns_inside_one_region(), [(["왼1\n왼2", "오1\n오2"], "body")], width=320, xycut=True)
    assert texts(res) == ["왼1\n오1\n왼2\n오2"]
    assert rec["result"]["interleaved"] == [["blk_001", "blk_002"]]


def test_same_row_pieces_reach_llm_as_one_block():
    # 높이비로 갈리던 같은 줄 조각은 이제 휴리스틱에서 한 Line · 한 블록이라 LLM 묶음이 필요 없다
    res, llm, rec = run_llm([R("num", 100, 0, 60, 40, "98"), R("pct", 165, 10, 20, 20, "%")], [(["98 %"], "title")],
                            xycut=True)
    assert [b["text"] for b in json.loads(llm.calls[0][1])["blocks"]] == ["98 %"]
    assert [(b.source_ko, b.role) for b in res.blocks] == [("98 %", "title")]
    assert rec["result"]["interleaved"] == []


def test_xycut_llm_invariants_hold():
    regs = mixed_regions()
    heur = merge.heuristic(SEC(), OCR(regs), CFG(xycut=True))
    plan = [([b.source_ko], b.role) for b in heur.blocks if merge._sendable(b)]
    res, _, _ = run_llm(regs, plan, xycut=True)
    assert res == heur  # 합치지 않는 응답이면 휴리스틱 결과 그대로


# ---------------------------------------------------------------------------
# 후처리 진단(merge_post_debug) — LLM 사용 여부와 관계없이
# ---------------------------------------------------------------------------
def run_diag(regions, width=1000, use_llm=False, llm=None, **over):
    diags = []
    res = merge.run(SEC(width), OCR(regions), CFG(**over), use_llm=use_llm, llm=llm, post_recorder=diags.append)
    return res, diags


def test_no_diagnostics_when_both_switches_are_off():
    _, diags = run_diag(two_columns_inside_one_region(), width=320, **OFF)
    assert diags == []


def test_no_llm_run_records_layout_diagnostics():
    res, (d,) = run_diag(two_columns_inside_one_region(), width=320, xycut=True, num_o=False)
    assert d["use_llm"] is False and d["section_key"] == "sec_1_01"
    assert d["postprocess"] == {"xycut": True, "fullwidth": False, "candidate_regions": 1}
    assert d["config"]["xycut_gap_ratio"] == 0.8 and d["config"]["gutter"] is True
    assert d["cuts"] == [{"path": "", "region": 1}]  # 절단 없음 — 후보 영역 하나
    assert d["regions"] == [{"region": 1, "group": 1, "rows": 2, "row_bases": [25.0, 85.0],
                             "blocks": ["blk_001", "blk_002"]}]
    assert [(b["block_key"], b["region"], b["rows"], b["sent_to_llm"]) for b in d["blocks"]] == [
        ("blk_001", 1, [1, 2], True), ("blk_002", 1, [1, 2], True)]
    assert d["interleaving_pairs"] == [["blk_001", "blk_002"]]  # LLM이 합치면 칸이 번갈아 섞일 후보
    assert [b["text"] for b in d["blocks"]] == texts(res)


def test_diagnostics_record_cuts_standalone_slots_and_fullwidth_lines():
    regs = [R("t1", 0, 0, 100, 20, "첫"), R("t2", 0, 100, 100, 20, "ＡＢ"), R("e", 300, 50, 40, 20, "", 0.0)]
    _, (d,) = run_diag(regs, xycut=True, fullwidth=True)
    assert d["cuts"] == [{"path": "", "axis": "y", "value": 60.0}, {"path": "L", "region": 1}, {"path": "H", "region": 2}]
    standalone = next(b for b in d["blocks"] if b["block_key"] == "blk_002")
    assert (standalone["region"], standalone["rows"], standalone["after_rows"], standalone["sent_to_llm"]) == (1, None, 1, False)
    assert d["fullwidth_changed_lines"] == ["line_003"] and d["postprocess"]["fullwidth_changed_lines"] == 1
    assert [r["group"] for r in d["regions"]] == [1, 1] and {b["group"] for b in d["blocks"]} == {1}  # y 절단뿐 → 한 묶음


def test_diagnostics_record_column_groups_for_mixed_cuts():
    _, (d,) = run_diag(full_width_title_over_two_columns(), xycut=True)
    assert [(r["region"], r["group"]) for r in d["regions"]] == [(1, 1), (2, 2), (3, 3)]
    assert [(b["region"], b["group"]) for b in d["blocks"]] == [(1, 1), (2, 2), (3, 3)]


def test_num_o_records_changed_lines_only_when_on():
    regs = [R("a", 0, 0, 100, 20, "SPF 5O+"), R("b", 0, 300, 100, 20, "가나")]
    _, (d,) = run_diag(regs, num_o=True, xycut=False)
    assert d["postprocess"] == {"xycut": False, "fullwidth": False, "num_o": True, "num_o_changed_lines": 1}
    assert d["num_o_changed_lines"] == ["line_001"] and d["config"]["num_o"] is True
    _, (d,) = run_diag(regs, xycut=True, num_o=False)
    assert "num_o" not in d["postprocess"] and "num_o_changed_lines" not in d and "num_o" not in d["config"]


def test_fullwidth_only_diagnostics_have_no_layout():
    _, (d,) = run_diag([R("a", 0, 0, 100, 20, "Ａ")], fullwidth=True, xycut=False, num_o=False)
    assert d["postprocess"] == {"xycut": False, "fullwidth": True, "fullwidth_changed_lines": 1}
    assert "cuts" not in d and "blocks" not in d


def test_diagnostics_are_written_before_llm_and_survive_its_failure():
    from pipeline.vlm import VlmError

    class Failing:
        config = {"model": "fake", "temperature": 0, "timeout_s": 60}

        def __call__(self, prompt, payload):
            raise VlmError("시간 초과")

    diags = []
    with pytest.raises(VlmError):
        merge.run(SEC(), OCR(regions_in_two_regions()), CFG(xycut=True), llm=Failing(), post_recorder=diags.append)
    assert len(diags) == 1 and diags[0]["use_llm"] is True


def test_cli_merge_no_llm_writes_post_debug_only_when_switched_on(tmp_path):
    from pathlib import Path

    from pipeline import run as cli

    exp = Path(__file__).resolve().parent.parent / "pipeline" / "samples" / "synthetic_01" / "expected"
    base = ["merge", "--split", str(exp / "split.json"), "--ocr", str(exp / "ocr/sec_1_01.json"), "--no-llm"]
    off = ["--set", "merge.xycut=false", "--set", "merge.num_o=false"]
    assert cli.main([*base, "--out", str(tmp_path / "off"), *off]) == 0
    assert not (tmp_path / "off" / "merge_post_debug").exists()
    assert cli.main([*base, "--out", str(tmp_path / "default")]) == 0  # 기본값(xycut · num_o 켬)이면 진단을 쓴다
    d = json.loads((tmp_path / "default" / "merge_post_debug" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert d["use_llm"] is False and d["postprocess"]["xycut"] is True and d["postprocess"]["num_o"] is True


@pytest.mark.parametrize("over, written", [
    (OFF, False), ({**OFF, "fullwidth": True}, True), ({**OFF, "num_o": True}, True), ({}, True),  # {} = 기본값
])
def test_analyze_no_llm_writes_post_debug_and_block_keys(monkeypatch, tmp_path, over, written):
    from pipeline import analyze as an
    from pipeline.types import SourceImage, SplitResult

    sec = SEC(height=500)
    split = SplitResult(source_image_id=1, source_width=1000, source_height=500, sections=[sec])
    monkeypatch.setattr(an.section_split, "run", lambda src, cfg, out: split)
    monkeypatch.setattr(an.ocr, "run", lambda s, cfg, **_: OCR([R("reg_0001", 0, 0, 100, 20, "Ａ")]))
    res = an.analyze([SourceImage(source_image_id=1, upload_order=1, path="unused.png")], CFG(**over), tmp_path,
                     use_llm=False)
    post = tmp_path / "merge_post_debug"
    assert post.exists() is written
    assert not (tmp_path / "merge_debug").exists()
    if written:
        d = json.loads((post / "sec_1_01.json").read_text(encoding="utf-8"))
        if over.get("fullwidth"):
            assert d["fullwidth_changed_lines"] == ["line_001"]
        else:  # num_o만 켜면 전각 Ａ는 그대로 — 바뀐 줄 없음
            assert d["num_o_changed_lines"] == [] and d["fullwidth_changed_lines"] == []
        assert json.loads((post / "block_keys.json").read_text(encoding="utf-8")) == [
            {"section_key": "sec_1_01", "section_block_key": "blk_001", "run_block_key": res.blocks[0].block_key}]
