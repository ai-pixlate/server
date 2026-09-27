"""③ heuristic_v2 검증 — pipeline.md 7.1절 규칙에서 손으로 계산한 기대값만 쓴다(구현 출력을 복사하지 않는다).

좌표는 BBox{x,y,w,h}, x2 = x + w. 기본 config 값: line_gap 0.7 · para_gap 0.6 · h_ratio 1.5 · overlap 0.5 ·
line_v_overlap 0.5 · line_gap_min -0.5 · para_gap_min -0.3 · left_align_tol 0.5 · gutter 최소 폭 max(20, W // 16).
높이 20이면 base = 20 → 줄 가로 간격 [-10, 14] · 블록 세로 간격 [-6, 12] · 좌정렬 오차 10.
"""
import itertools
import json
import math
from collections import Counter
from pathlib import Path

import pytest

from pipeline import config as cfgmod
from pipeline import jsonio
from pipeline import run as cli
from pipeline.stages import merge
from pipeline.types import BBox, OcrRegion, OcrResult, Section, SourceImage, SplitResult

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "pipeline" / "samples" / "synthetic_01"


def R(key: str, x: int, y: int, w: int, h: int, text: str = "가", score: float = 0.9) -> OcrRegion:
    return OcrRegion(
        region_key=key, text=text, score=score,
        poly=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)], bbox=BBox(x=x, y=y, w=w, h=h),
    )


def SEC(width: int = 1000, key: str = "sec_1_01", order: int = 1, top: int = 0, height: int = 2000) -> Section:
    return Section(section_key=key, source_image_id=1, section_order=order, top_offset=top, height=height,
                   width=width, image_path="unused.png")


def CFG(**over):
    cfg = cfgmod.load_config()
    cfg["merge"].update(over)
    return cfg


def M(**over):
    return CFG(**over)["merge"]


def run(regions, width: int = 1000, **over):
    return merge.run(SEC(width), OcrResult(section_key="sec_1_01", regions=regions), CFG(**over), use_llm=False)


def lines_of(regions, width: int = 1000, **over):
    m = M(**over)
    return merge.group_lines(regions, m, merge.find_gutters(regions, width, m) if m["gutter"] else [])


def keys(res):
    return [[r.region_key for ln in b.source_lines for r in ln.regions] for b in res.blocks]


# ---------------------------------------------------------------------------
# 줄 병합
# ---------------------------------------------------------------------------
def test_same_line_words_join_with_one_space():
    a, b = R("r1", 100, 0, 100, 20, "피부"), R("r2", 210, 0, 80, 20, "보습")  # 간격 10 ≤ 14
    assert [ln.text for ln in lines_of([a, b])] == ["피부 보습"]
    res = run([a, b])
    assert [x.source_ko for x in res.blocks] == ["피부 보습"]
    assert res.blocks[0].source_lines[0].text == "피부 보습"


def test_region_text_edges_are_stripped_but_inner_spaces_kept():
    a, b = R("r1", 100, 0, 100, 20, " 피부  보습 "), R("r2", 210, 0, 80, 20, "크림 ")
    assert [ln.text for ln in lines_of([a, b])] == ["피부  보습 크림"]


@pytest.mark.parametrize("bx, n_lines", [(214, 1), (215, 2), (190, 1), (189, 2)])
def test_line_horizontal_gap_bounds_are_inclusive(bx, n_lines):
    # a.x2 = 200. 간격 14(상한) · 15 · -10(하한) · -11
    a, b = R("r1", 100, 0, 100, 20), R("r2", bx, 0, 80, 20)
    assert len(lines_of([a, b])) == n_lines
    # 두 줄이 되면 블록 단계에서도 붙지 않는다: 세로 간격 0 - 20 = -20 < -6
    assert len(run([a, b]).blocks) == n_lines


@pytest.mark.parametrize("by, n_lines", [(10, 1), (11, 2)])
def test_line_vertical_overlap_threshold(by, n_lines):
    # 세로 겹침 10/20 = 0.5(통과) · 9/20 = 0.45(분리)
    a, b = R("r1", 0, 0, 100, 20), R("r2", 110, by, 100, 20)
    assert len(lines_of([a, b])) == n_lines
    # by=11: 블록 세로 간격 11 - 20 = -9 < -6 → 블록도 2개
    assert len(run([a, b]).blocks) == n_lines


@pytest.mark.parametrize("bh, n_lines", [(30, 1), (31, 2)])
def test_line_height_ratio_threshold(bh, n_lines):
    # 높이비 30/20 = 1.5(통과) · 31/20 = 1.55(분리). 세로 겹침 20/20, 간격 10 ≤ 14
    a, b = R("r1", 0, 0, 100, 20), R("r2", 110, 0, 100, bh)
    assert len(lines_of([a, b])) == n_lines


# ---------------------------------------------------------------------------
# gutter
# ---------------------------------------------------------------------------
def _gutter_pair():
    # 높이 100, W = 800 → 최소 폭 max(20, 50) = 50. 간격 60 ≤ 70이라 gutter 없이는 한 줄
    return R("r1", 100, 0, 200, 100, "왼쪽"), R("r2", 360, 0, 200, 100, "오른쪽")


def test_gutter_found_only_between_regions():
    a, b = _gutter_pair()
    # [0,100) · [560,800)은 양 끝에 붙어 제외, [300,360) 폭 60 ≥ 50
    assert merge.find_gutters([a, b], 800, M()) == [(300, 360)]
    assert merge.find_gutters([a, b], 800, M(gutter_min_px=61)) == []


def test_gutter_splits_large_text_and_can_be_turned_off():
    a, b = _gutter_pair()
    assert len(lines_of([a, b], 800)) == 2
    assert len(run([a, b], 800).blocks) == 2  # 블록 세로 간격 -100 < -30
    assert [ln.text for ln in lines_of([a, b], 800, gutter=False)] == ["왼쪽 오른쪽"]
    assert len(run([a, b], 800, gutter=False).blocks) == 1


def test_empty_text_region_takes_part_in_gutter():
    a, b = _gutter_pair()
    e = R("r3", 290, 500, 80, 30, "", 0.0)  # [290,370)을 차지 → gutter 사라짐
    res = run([a, b, e], 800)
    assert keys(res) == [["r1", "r2"], ["r3"]]


def test_zero_size_wins_over_empty_text_and_is_left_out_of_gutter():
    a, b = _gutter_pair()
    z = R("r3", 290, 500, 80, 0, "", 0.0)  # 빈 텍스트이면서 높이 0 → 크기 0
    assert merge.region_kind(z) == "zero"
    res = run([a, b, z], 800)
    assert keys(res) == [["r1"], ["r2"], ["r3"]]


# ---------------------------------------------------------------------------
# 블록 병합
# ---------------------------------------------------------------------------
def test_left_aligned_lines_form_one_block_joined_by_newline():
    rs = [R("r1", 100, 0, 400, 20, "첫째"), R("r2", 100, 28, 300, 20, "둘째"), R("r3", 100, 56, 350, 20, "셋째")]
    res = run(rs)
    assert len(res.blocks) == 1
    b = res.blocks[0]
    assert b.source_ko == "첫째\n둘째\n셋째"
    assert [ln.text for ln in b.source_lines] == ["첫째", "둘째", "셋째"]
    assert b.bbox == BBox(x=100, y=0, w=400, h=76)


@pytest.mark.parametrize("y2, n_blocks", [(32, 1), (33, 2), (14, 1), (13, 2)])
def test_block_vertical_gap_bounds_are_inclusive(y2, n_blocks):
    # 세로 간격 12(상한) · 13 · -6(하한) · -7. y2=14 · 13은 세로 겹침 6/20 · 7/20 < 0.5라 줄 단계에서는 따로
    rs = [R("r1", 100, 0, 400, 20), R("r2", 100, y2, 300, 20)]
    assert len(lines_of(rs)) == 2
    assert len(run(rs).blocks) == n_blocks


def test_center_aligned_short_line_merges_when_inside():
    # 겹친 폭 200 ÷ 좁은 폭 200 = 1.0 ≥ 0.5, 세로 간격 8
    rs = [R("r1", 100, 0, 400, 20), R("r2", 200, 28, 200, 20)]
    assert len(run(rs).blocks) == 1


def test_offset_line_splits_when_overlap_and_left_edge_both_fail():
    # 겹친 폭 80 ÷ 200 = 0.4 < 0.5, 왼쪽 끝 차이 320 > 10
    rs = [R("r1", 100, 0, 400, 20), R("r2", 420, 28, 200, 20)]
    assert len(run(rs).blocks) == 2


@pytest.mark.parametrize("lx, n_blocks", [(110, 1), (111, 2)])
def test_left_edge_tolerance_is_inclusive(lx, n_blocks):
    # 윗줄 [100,110)과 아랫줄은 가로로 겹치지 않는다(0 < 0.5). 왼쪽 끝 차이 10(= 0.5·20) · 11
    rs = [R("r1", 100, 0, 10, 20), R("r2", lx, 28, 400, 20)]
    assert len(run(rs).blocks) == n_blocks


def test_two_columns_become_two_blocks_left_first():
    # 줄: 가로 간격 100 > 14라 네 줄. 블록: 오른쪽 첫 줄은 왼쪽 그룹과 세로 간격 0 - 20 = -20 < -6
    rs = [R("r1", 50, 0, 400, 20), R("r2", 550, 0, 400, 20), R("r3", 50, 28, 400, 20), R("r4", 550, 28, 400, 20)]
    assert len(lines_of(rs)) == 4
    assert keys(run(rs)) == [["r1", "r3"], ["r2", "r4"]]


# ---------------------------------------------------------------------------
# 빈 텍스트 · 크기 0 · 보존
# ---------------------------------------------------------------------------
def test_empty_and_whitespace_regions_are_standalone_with_their_own_score():
    t = R("r1", 100, 0, 200, 20, "본문")
    e1 = R("r2", 110, 28, 50, 20, "", 0.0)  # 텍스트였다면 윗줄과 한 블록
    e2 = R("r3", 400, 0, 50, 20, "   ", 0.6)
    res = run([t, e1, e2])
    assert keys(res) == [["r1"], ["r3"], ["r2"]]  # 블록 bbox (y, x) 순
    by_key = {b.source_lines[0].regions[0].region_key: b for b in res.blocks}
    assert by_key["r3"].source_ko == "" and by_key["r3"].ocr_confidence == 0.6
    assert by_key["r2"].source_ko == "" and by_key["r2"].ocr_confidence == 0.0
    assert by_key["r3"].source_lines[0].regions[0].text == "   "  # 원시 텍스트 보존


def test_empty_regions_are_left_out_of_height_stats():
    # 텍스트 높이 [20, 20, 20] → 통계 없음 → 크기 규칙 생략 → body.
    # 빈 영역(높이 100)을 넣었다면 (40.0, 20.0)이 되어 텍스트 블록은 caption이 됐을 것이다.
    ts = [R("r1", 100, 0, 100, 20), R("r2", 100, 200, 100, 20), R("r3", 100, 400, 100, 20)]
    e = R("r4", 100, 600, 100, 100, "", 0.0)
    assert merge.height_stats([20, 20, 20, 100], M()) == (40.0, 20.0)
    res = run(ts + [e])
    assert [b.role for b in res.blocks] == ["body", "body", "body", "body"]


def test_zero_size_text_region_is_standalone():
    t, z = R("r1", 100, 0, 190, 20, "텍스트"), R("r2", 300, 0, 0, 20, "글")
    res = run([t, z])
    assert keys(res) == [["r1"], ["r2"]]
    assert res.blocks[1].source_ko == "글"


def test_every_region_is_preserved_exactly_once_and_unchanged():
    rs = [
        R("r1", 100, 0, 100, 20, "피부"), R("r2", 210, 0, 80, 20, "보습"), R("r3", 100, 28, 300, 20, "둘째 줄"),
        R("r4", 600, 0, 50, 20, "", 0.0), R("r5", 700, 0, 0, 20, "글"), R("r6", 100, 300, 400, 40, "제목", 0.3),
    ]
    res = run(rs)
    out = [r for b in res.blocks for ln in b.source_lines for r in ln.regions]
    assert Counter(r.region_key for r in out) == Counter(r.region_key for r in rs)
    assert all(v == 1 for v in Counter(r.region_key for r in out).values())
    by_key = {r.region_key: r for r in rs}
    assert all(r.model_dump() == by_key[r.region_key].model_dump() for r in out)


def test_no_regions_give_no_blocks():
    assert run([]).blocks == []


# ---------------------------------------------------------------------------
# 결정성 · 키 · 입력 검사
# ---------------------------------------------------------------------------
def test_result_does_not_depend_on_input_array_order():
    # x 동률(100 · 100 · 100 · 210 · 210)과 위치가 같은 두 영역(r4 · r5)을 포함
    rs = [R("r1", 100, 0, 100, 20, "가"), R("r2", 100, 28, 100, 20, "나"), R("r3", 100, 56, 100, 20, "다"),
          R("r4", 210, 0, 100, 20, "라"), R("r5", 210, 0, 100, 20, "마")]
    expected = run(rs).model_dump()
    for perm in itertools.permutations(rs):
        assert run(list(perm)).model_dump() == expected


def test_keys_and_orders_are_sequential_within_section():
    rs = [R("r1", 100, 0, 400, 20), R("r2", 100, 28, 400, 20), R("r3", 100, 300, 400, 20)]
    res = run(rs)
    assert [b.block_key for b in res.blocks] == ["blk_001", "blk_002"]
    assert [b.block_order for b in res.blocks] == [1, 2]
    assert [ln.line_key for b in res.blocks for ln in b.source_lines] == ["line_001", "line_002", "line_003"]


def test_duplicate_region_key_or_other_section_is_rejected():
    with pytest.raises(ValueError, match="중복"):
        run([R("r1", 0, 0, 10, 10), R("r1", 100, 0, 10, 10)])
    with pytest.raises(ValueError, match="섹션"):
        merge.run(SEC(), OcrResult(section_key="sec_9_99", regions=[]), CFG(), use_llm=False)


def test_run_with_llm_is_not_implemented_yet():
    with pytest.raises(NotImplementedError):
        merge.run(SEC(), OcrResult(section_key="sec_1_01", regions=[]), CFG())


# ---------------------------------------------------------------------------
# 역할 판정
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hs, want", [([41, 30, 40, 32], 40), ([5], 5), ([3, 1, 2], 2), ([10, 20], 20)])
def test_font_h_takes_index_n_div_2_of_sorted_heights(hs, want):
    assert merge.font_h([R(f"r{i}", 0, 0, 10, h) for i, h in enumerate(hs)]) == want


def test_height_stats_linear_percentile_and_skips():
    assert merge.height_stats([10, 20, 30, 40], M()) == (32.5, 17.5)  # 위치 2.25 · 0.75
    assert merge.height_stats([], M()) is None
    assert merge.height_stats([20], M()) is None
    assert merge.height_stats([20, 20], M()) is None


STATS = (40.0, 20.0)


@pytest.mark.parametrize(
    "text, n_lines, fh, stats, want",
    [
        ("29,900원", 1, 30, STATS, "price"),
        ("₩ 3,000", 1, 30, STATS, "price"),
        ("가" * 38 + "1원", 1, 30, STATS, "price"),  # 40자
        ("가" * 39 + "1원", 1, 30, STATS, "body"),  # 41자
        ("주의 3,000원", 1, 30, STATS, "price"),  # 가격이 먼저
        ("제목", 2, 40, STATS, "title"),
        ("제목\n둘째\n셋째", 3, 40, STATS, "body"),
        ("가" * 25, 1, 20, STATS, "caption"),
        ("가" * 26, 1, 20, STATS, "body"),
        ("제목", 1, 40, None, "body"),  # 통계 없음 → 크기 규칙 생략
        ("", 1, 40, STATS, "title"),  # 빈 블록도 같은 규칙
    ],
)
def test_classify_rules_and_boundaries(text, n_lines, fh, stats, want):
    assert merge.classify(text, n_lines, fh, stats, M()) == want


@pytest.mark.parametrize(
    "text",
    ["사용 시 주의하세요", "유의사항", "유의 사항", "유의하세요", "이상이 있는 경우", "이상 반응 시", "이상증상",
     "피부과전문의 교수", "구매 문의", "※ 개인차가 있을 수 있습니다", "서늘한곳보관권장"],
)
def test_caution_patterns_hit(text):
    assert merge.CAUTION_RE.search(text)


@pytest.mark.parametrize(
    "text",
    ["특유의 향", "유의한 차이", "유의하게 개선", "이상적인 보습", "3점 이상 긍정 답변", "특수 관리 이상의 효과"],
)
def test_caution_patterns_skip_known_false_hits(text):
    assert not merge.CAUTION_RE.search(text)


@pytest.mark.parametrize("text, hit", [("3,000원", True), ("₩", True), ("98%", False), ("12개월", False), ("100% 증정", False)])
def test_price_pattern_is_currency_only(text, hit):
    assert bool(merge.PRICE_RE.search(text)) is hit


# ---------------------------------------------------------------------------
# 설정 값 검증
# ---------------------------------------------------------------------------
def test_default_config_is_valid():
    merge.validate_config(CFG())


@pytest.mark.parametrize(
    "over",
    [
        {"line_v_overlap": 0}, {"line_v_overlap": 1}, {"overlap": 0}, {"overlap": 1}, {"h_ratio": 1},
        {"left_align_tol": 0}, {"gutter_min_px": 0}, {"caption_max_chars": 0}, {"price_max_chars": 0},
        {"gutter_width_div": 1}, {"title_max_lines": 1}, {"title_pct": 100}, {"caption_pct": 0},
        {"line_gap_min": 0.7}, {"para_gap_min": 0.6}, {"caption_pct": 75}, {"line_gap": 1},
        {"gutter": False},
    ],
)
def test_config_boundaries_are_accepted(over):
    merge.validate_config(CFG(**over))


@pytest.mark.parametrize(
    "over",
    [
        {"line_gap": True}, {"line_gap": "0.7"}, {"line_gap": math.nan}, {"line_gap": math.inf}, {"para_gap": -math.inf},
        {"h_ratio": None}, {"gutter_min_px": 20.0}, {"title_max_lines": 2.0}, {"gutter_width_div": True},
        {"line_v_overlap": 1.01}, {"line_v_overlap": -0.01}, {"overlap": 1.5}, {"h_ratio": 0.99},
        {"left_align_tol": -0.1}, {"gutter_min_px": -1}, {"gutter_width_div": 0}, {"title_max_lines": 0},
        {"caption_max_chars": -1}, {"price_max_chars": -1}, {"title_pct": 100.1}, {"caption_pct": -1},
        {"line_gap_min": 0.71}, {"para_gap_min": 0.61}, {"caption_pct": 76},
        {"gutter": 1}, {"gutter": "true"}, {"llm_split": 0}, {"llm_split": True}, {"llm_split": None},
    ],
)
def test_config_bad_values_are_rejected(over):
    with pytest.raises(ValueError):
        merge.validate_config(CFG(**over))


def test_config_missing_key_is_rejected():
    cfg = CFG()
    del cfg["merge"]["gutter_width_div"]
    with pytest.raises(ValueError, match="gutter_width_div"):
        merge.validate_config(cfg)


def test_run_validates_config_before_reading_input():
    with pytest.raises(ValueError):
        merge.run(SEC(), OcrResult(section_key="sec_9_99", regions=[]), CFG(gutter_width_div=0), use_llm=False)


# ---------------------------------------------------------------------------
# analyze() · CLI
# ---------------------------------------------------------------------------
def test_analyze_rejects_bad_config_before_split_and_ocr(monkeypatch, tmp_path):
    from pipeline import analyze as an

    calls: list[str] = []
    monkeypatch.setattr(an.section_split, "run", lambda *a, **k: calls.append("split"))
    monkeypatch.setattr(an.ocr, "run", lambda *a, **k: calls.append("ocr"))
    src = SourceImage(source_image_id=1, upload_order=1, path="unused.png")
    for bad in ({"gutter_width_div": 0}, {"line_gap": math.nan}, {"llm_split": 0}):
        with pytest.raises(ValueError):
            an.analyze([src], CFG(**bad), tmp_path)
    assert calls == []


def test_analyze_numbers_block_keys_across_the_run(monkeypatch, tmp_path):
    from pipeline import analyze as an

    secs = [SEC(key="sec_1_01", order=1, top=0, height=500), SEC(key="sec_1_02", order=2, top=500, height=500)]
    split = SplitResult(source_image_id=1, source_width=1000, source_height=1000, sections=secs)
    regions = [R("reg_0001", 100, 0, 400, 20, "위"), R("reg_0002", 100, 300, 400, 20, "아래")]  # 섹션마다 블록 2개
    monkeypatch.setattr(an.section_split, "run", lambda src, cfg, out: split)
    monkeypatch.setattr(an.ocr, "run", lambda sec, cfg: OcrResult(section_key=sec.section_key, regions=regions))
    # merge.run은 바꿔 끼우지 않는다 — use_llm이 analyze()에서 merge.run까지 전달되는지가 검증 대상
    res = an.analyze([SourceImage(source_image_id=1, upload_order=1, path="unused.png")], CFG(), tmp_path, use_llm=False)
    assert [b.block_key for b in res.blocks] == ["blk_001", "blk_002", "blk_003", "blk_004"]
    assert [(b.section_key, b.block_order) for b in res.blocks] == [
        ("sec_1_01", 1), ("sec_1_01", 2), ("sec_1_02", 1), ("sec_1_02", 2),
    ]


def test_analyze_with_llm_stops_before_split_and_ocr(monkeypatch, tmp_path):
    from pipeline import analyze as an

    calls: list[str] = []
    monkeypatch.setattr(an.section_split, "run", lambda *a, **k: calls.append("split"))
    monkeypatch.setattr(an.ocr, "run", lambda *a, **k: calls.append("ocr"))
    with pytest.raises(NotImplementedError):
        an.analyze([SourceImage(source_image_id=1, upload_order=1, path="unused.png")], CFG(), tmp_path)
    assert calls == []


@pytest.mark.parametrize("argv_extra, use_llm", [(["--no-llm"], False), ([], True)])
def test_cli_analyze_passes_no_llm(monkeypatch, tmp_path, argv_extra, use_llm):
    import pipeline.analyze as an
    from pipeline.types import AnalyzeResult

    seen: dict[str, bool] = {}

    def fake(sources, cfg, out, *, use_llm=True):
        seen["use_llm"] = use_llm
        return AnalyzeResult(sections=[], blocks=[])

    monkeypatch.setattr(an, "analyze", fake)
    assert cli.main(["analyze", "--source", "unused.png", "--out", str(tmp_path), *argv_extra]) == 0
    assert seen["use_llm"] is use_llm
    assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["use_llm"] is use_llm


def test_cli_merge_no_llm_writes_blocks_and_records_it(tmp_path):
    exp = SAMPLE / "expected"
    rc = cli.main(["merge", "--split", str(exp / "split.json"), "--ocr", str(exp / "ocr/sec_1_01.json"),
                   "--out", str(tmp_path), "--no-llm"])
    assert rc == 0
    res = jsonio.load_merge(tmp_path / "merge" / "sec_1_01.json")
    ocr = jsonio.load_ocr(exp / "ocr/sec_1_01.json")
    assert sorted(r.region_key for b in res.blocks for ln in b.source_lines for r in ln.regions) == sorted(
        r.region_key for r in ocr.regions
    )
    assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))["use_llm"] is False
