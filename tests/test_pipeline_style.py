"""⑦ 스타일 추출 단독 개발 v1 — 합성 사례 검증(pipeline.md 7.7절 · open-questions.md #76).

기대값은 승인 명세에서 손으로 계산해 적는다 — 검증 대상의 명도 · Otsu · 중앙값 · 정렬 함수로 기대값을 만들지 않는다.
입력 지문만 기존 직렬화 · 해시 함수(label.input_fingerprint · logo.sha256_json)로 만들고, 지문 불일치는 따로 검사한다.
합성 사례 통과는 실제 이미지 품질 검증이나 운영 연동이 아니다.
"""
from __future__ import annotations

import copy
import math
import re
from fractions import Fraction

import numpy as np
import pytest
from pydantic import ValidationError

from pipeline import config as cfgmod
from pipeline.stages import style
from pipeline.stages.label import input_fingerprint
from pipeline.stages.logo import sha256_json
from pipeline.stages.style import StyleInputError
from pipeline.types import (
    BBox, BlockStyle, LabelChecked, LabelDecision, LabelResult, Line, LogoDecision, LogoResult, MergeResult, OcrRegion, Section,
    StyleResult, TextBlock,
)

IMAGE_ID = "IMG-01"
KEY = "sec_1_01"
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


# ---------------------------------------------------------------------------
# 합성 입력 도우미
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cfg():
    # 계산식 검증용 — 기대값을 em_ratio 1.35 · align_section_tolerance 0.02로 손 계산했다. 기본값이 바뀌어도 계산식 테스트는
    # 흔들리지 않게 고정한다. 기본값 자체는 test_default_config_is_approved_values가 본다
    return with_style(cfgmod.load_config(), em_ratio=1.35, align_section_tolerance=0.02)


def with_style(cfg, **over):
    c = copy.deepcopy(cfg)
    c["style"].update(over)
    return c


def rect(x, y, w, h):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]


def R(key, x, y, w, h, text="가", score=0.9, bbox=None):
    return OcrRegion(region_key=key, text=text, score=score, poly=rect(x, y, w, h), bbox=bbox or BBox(x=x, y=y, w=w, h=h))


def L(key, *regions, bbox=None):
    return Line(line_key=key, text=" ".join(r.text for r in regions), bbox=bbox or BBox.union([r.bbox for r in regions]),
                regions=list(regions))


def B(n, *lines, key=None, bbox=None, section_key=KEY):
    return TextBlock(block_key=key or f"blk_{n:03d}", section_key=section_key, block_order=n,
                     source_ko="\n".join(ln.text for ln in lines), source_lines=list(lines),
                     bbox=bbox or BBox.union([ln.bbox for ln in lines]), role="body")


def bundle(blocks, excl=None, *, W=64, H=48, labels=None, logo_decisions=None, label_fp=None):
    """excl[block_key] = "label"(④ true → ⑤ product_label/null) 또는 "logo"(④ false · ⑤ true). 나머지는 ④ false · ⑤ false."""
    excl = excl or {}
    section = Section(section_key=KEY, source_image_id=1, section_order=1, top_offset=0, height=H, width=W, image_path="unused.png")
    merged = MergeResult(section_key=KEY, blocks=blocks)
    if labels is None:
        labels = [LabelDecision(block_key=b.block_key, is_product_label=excl.get(b.block_key) == "label", basis="vlm") for b in blocks]
    label = LabelResult(section_key=KEY, status="ok", labels=labels,
                        checked=LabelChecked(input_fingerprint=label_fp or input_fingerprint(blocks), llm_called=True))
    if logo_decisions is None:
        logo_decisions = []
        for b in blocks:
            kind = excl.get(b.block_key)
            if kind == "label":
                logo_decisions.append(LogoDecision(block_key=b.block_key, is_brand_logo=None, basis="product_label"))
            else:
                logo_decisions.append(LogoDecision(block_key=b.block_key, is_brand_logo=kind == "logo",
                                                   basis="exact_match" if kind == "logo" else "no_match"))
    logo = LogoResult(image_id=IMAGE_ID, section_key=KEY, status="ok", decisions=logo_decisions)
    record = {"status": "ok", "image_id": IMAGE_ID, "section_key": KEY, "blocks_fingerprint": input_fingerprint(blocks),
              "label_fingerprint": sha256_json(label.model_dump(mode="json")),
              "decisions": [{"block_key": d.block_key, "is_brand_logo": d.is_brand_logo, "basis": d.basis} for d in logo_decisions]}
    return {"section": section, "merged": merged, "label": label, "logo": logo, "record": record}


def ex(img, bd, cfg, image_id=IMAGE_ID):
    return style.extract(image_id, bd["section"], img, bd["merged"], bd["label"], bd["logo"], bd["record"], cfg)


def canvas(W=64, H=48, color=WHITE):
    return np.full((H, W, 3), color, dtype=np.uint8)


def ink(img, r: OcrRegion, color=BLACK):
    """영역 bbox 가운데 픽셀 하나를 칠한다(흰 바탕이면 글자 = 그 픽셀, 배경 = 흰색)."""
    b = r.bbox
    img[b.y + b.h // 2, b.x + b.w // 2] = color


def one_region(img, x, y, w, h, cfg, **kw):
    """영역 하나짜리 블록 하나를 추출해 (BlockStyle, RegionStyle)."""
    H, W = img.shape[:2]
    r = R("reg_0001", x, y, w, h, **kw)
    res = ex(img, bundle([B(1, L("line_001", r))], W=W, H=H), cfg)
    return res.blocks[0], res.blocks[0].regions[0]


def expect_input_error(needle, fn):
    with pytest.raises(StyleInputError, match=re.escape(needle)):
        fn()


# ---------------------------------------------------------------------------
# 0. 설정 기본값
# ---------------------------------------------------------------------------
def test_default_config_is_approved_values():
    # em_ratio 0.9는 잠정값(사용자 결정 2026-10-15, open-questions #85). align_section_tolerance 0.005는 실측 뒤 확정(사용자 결정 2026-11-05)
    assert cfgmod.load_config()["style"] == {"method": "otsu_border", "em_ratio": 0.9, "align_section_tolerance": 0.005,
                                             "align_tolerance": 0.12}


# ---------------------------------------------------------------------------
# 1. 명도와 Otsu — 작은 계산 함수 직접 검증
# ---------------------------------------------------------------------------
def test_luma_integer_formula():
    # Y = (299R + 587G + 114B + 500) // 1000
    px = np.array([[WHITE, BLACK, (255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30), (1, 0, 0), (2, 0, 0), (29, 29, 29)]],
                  dtype=np.uint8)
    # 흰색 255500//1000=255(uint8 가중합이면 넘침) · 검정 500//1000=0 · 빨강 76745→76 · 초록 150185→150 · 파랑 29570→29 ·
    # (10,20,30) 18650→18 · (1,0,0) 799→0 · (2,0,0) 1098→1 · (29,29,29) 29500→29
    assert style.luma(px).tolist() == [[255, 0, 76, 150, 29, 18, 0, 1, 29]]


def test_otsu_two_values_with_empty_bins_picks_smallest_t():
    # 값 0 · 255만 있으면 t = 0~254가 모두 같은 분할(같은 최적값) → 가장 작은 t = 0
    assert style.otsu_threshold(np.array([0, 0, 255, 255])) == 0
    # 값 10 · 200: t < 10은 A가 비어 후보 아님, t = 10~199는 같은 분할 → 10
    assert style.otsu_threshold(np.array([10, 200, 200])) == 10


def test_otsu_different_partitions_with_equal_optimum_picks_smallest_t():
    # 값 0 · 1 · 2 각 1개. t=0: A={0} B={1,2} → (0·2 − 3·1)²/(1·2) = 9/2
    #                   t=1: A={0,1} B={2} → (1·1 − 2·2)²/(2·1) = 9/2 — 다른 분할, 같은 최적값 → t = 0
    assert style.otsu_threshold(np.array([0, 1, 2])) == 0


def test_otsu_unequal_candidates():
    # 값 0 · 1 · 255×3. t=0: (0·4 − 766·1)²/4 = 146689 / t=1(~254 동일): (1·3 − 765·2)²/6 = 388621.5 → t = 1
    assert style.otsu_threshold(np.array([0, 1, 255, 255, 255])) == 1
    # 값 0 · 10 · 30 · 250 · 250. t=0: 72900 / t=10: 1030²/6 ≈ 176816.7 / t=30(~249): 1420²/6 ≈ 336066.7 → t = 30
    assert style.otsu_threshold(np.array([0, 10, 30, 250, 250])) == 30


def test_otsu_single_value_has_no_candidate():
    assert style.otsu_threshold(np.array([77, 77, 77])) is None
    assert style.otsu_threshold(np.array([0])) is None


def test_border_mask_counts_each_pixel_once():
    # 외곽 수 = 2w + 2h − 4(모서리 한 번), 폭 또는 높이가 1이면 전체
    for (h, w), n in {(1, 1): 1, (1, 5): 5, (5, 1): 5, (2, 2): 4, (3, 3): 8, (4, 4): 12, (3, 5): 12}.items():
        assert int(style.border_mask(h, w).sum()) == n, (h, w)


def test_median_half_up_direct():
    # 짝수 개 = 가운데 두 값의 평균 → floor(m + 0.5)
    cases = {(0, 1): 1, (1, 2): 2, (100, 100, 101, 101): 101, (50, 51): 51, (10, 12): 11, (0, 10, 30): 10, (5,): 5}
    for vals, want in cases.items():
        assert style.half_up(style.median2(np.array(vals))) == want, vals
    assert style.hex_color((101, 51, 8)) == "#653308"


def test_est_font_size_exact_fraction():
    assert style.est_font_size(1, 1.35) == Fraction(27, 20)
    assert style.est_font_size(3, 1.35) == Fraction(81, 20)  # 부동소수 3 × 1.35 = 4.050000000000001
    assert style.est_font_size(20, 1.35) == 27


def test_representative_color_direct():
    a, b, c = (10, 0, 0), (0, 12, 0), (0, 0, 14)
    # 채널별 중앙값 (0, 0, 0)은 실제 색이 아니다. 거리 100 · 144 · 196 → a. 순서를 바꿔도 a
    assert style.representative_color([a, b, c]) == a
    assert style.representative_color([c, b, a]) == a
    t1, t2, t3 = (10, 0, 0), (0, 10, 0), (0, 0, 10)
    # 중앙값 (0, 0, 0)까지 모두 100 → 동률은 입력 순서
    assert style.representative_color([t1, t2, t3]) == t1
    assert style.representative_color([t2, t3, t1]) == t2
    assert style.representative_color([]) is None


# ---------------------------------------------------------------------------
# 1 · 2. 명도 · Otsu · 배경 · 외곽 — extract로 영역 결과 확인
# ---------------------------------------------------------------------------
def test_light_background_dark_text(cfg):
    img = canvas()
    img[1, 1] = BLACK  # 3×3 영역(0,0) 가운데
    blk, r = one_region(img, 0, 0, 3, 3, cfg)
    # 명도 {0×1, 255×8} → t=0, 외곽 8픽셀(9 아님) 모두 밝음 → 배경 흰색
    assert (r.status, r.font_color, r.bg_color) == ("measured", "#000000", "#FFFFFF")
    assert r.otsu.model_dump() == {"threshold": 0, "dark_px": 1, "light_px": 8, "border_px": 8, "border_dark": 0, "border_light": 8}
    assert blk.status == "ok"


def test_dark_background_light_text(cfg):
    img = canvas(color=BLACK)
    img[1, 1] = WHITE
    _, r = one_region(img, 0, 0, 3, 3, cfg)
    assert (r.font_color, r.bg_color) == ("#FFFFFF", "#000000")
    assert (r.otsu.dark_px, r.otsu.light_px, r.otsu.border_dark, r.otsu.border_light) == (8, 1, 8, 0)


def test_single_luma_value_is_single_class(cfg):
    img = canvas()
    _, r = one_region(img, 10, 10, 1, 1, cfg)  # 1×1
    assert (r.status, r.color_error, r.otsu, r.font_color, r.bg_color) == ("color_failed", "single_class", None, None, None)
    assert r.est_font_px == 1.35
    # RGB가 달라도 명도가 같으면(파랑 29 · 회색 29) 나눌 수 없다
    img[0:2, 0:2] = (0, 0, 255)
    img[0, 0] = img[1, 1] = (29, 29, 29)
    _, r = one_region(img, 0, 0, 2, 2, cfg)
    assert (r.status, r.color_error, r.otsu) == ("color_failed", "single_class", None)


def test_border_tie_2x2(cfg):
    img = canvas()
    img[0, 0:2] = BLACK  # 위 행 검정 · 아래 행 흰색, 2×2는 모두 외곽 → 2 : 2
    _, r = one_region(img, 0, 0, 2, 2, cfg)
    assert (r.status, r.color_error, r.font_color, r.bg_color) == ("color_failed", "border_tie", None, None)
    assert r.otsu.model_dump() == {"threshold": 0, "dark_px": 2, "light_px": 2, "border_px": 4, "border_dark": 2, "border_light": 2}
    assert r.est_font_px == 2.7  # 색만 실패, 크기는 계산


def test_2x2_not_tied(cfg):
    img = canvas()
    img[1, 1] = BLACK  # 어두운 1 : 밝은 3
    _, r = one_region(img, 0, 0, 2, 2, cfg)
    assert (r.status, r.font_color, r.bg_color, r.otsu.border_px) == ("measured", "#000000", "#FFFFFF", 4)


def test_corners_counted_once_gives_tie(cfg):
    img = canvas()
    for y, x in ((0, 0), (0, 2), (2, 0), (2, 2)):
        img[y, x] = BLACK  # 3×3 모서리 4개만 어둡고 변 가운데 4개 · 중심은 밝음
    _, r = one_region(img, 0, 0, 3, 3, cfg)
    # 외곽 8픽셀 = 모서리 4(어둠) + 변 4(밝음) → 동률. 모서리를 두 번 세면 8 : 4로 어두운 배경이 된다
    assert (r.color_error, r.otsu.border_px, r.otsu.border_dark, r.otsu.border_light) == ("border_tie", 8, 4, 4)


def test_1xN_horizontal_and_partition_tie(cfg):
    img = canvas()
    img[0, 0:3] = [(0, 0, 0), (1, 1, 1), (2, 2, 2)]  # 명도 0 · 1 · 2, 1×3은 모두 외곽
    _, r = one_region(img, 0, 0, 3, 1, cfg)
    # t=0과 t=1이 같은 최적값(9/2) → t=0: 어둠 {0} · 밝음 {1, 2}. 외곽 1 : 2 → 배경 = 밝음, 중앙값 (1+2)/2 = 1.5 → 2
    assert r.otsu.model_dump() == {"threshold": 0, "dark_px": 1, "light_px": 2, "border_px": 3, "border_dark": 1, "border_light": 2}
    assert (r.font_color, r.bg_color) == ("#000000", "#020202")


def test_Nx1_vertical(cfg):
    img = canvas()
    img[0, 0] = BLACK  # 세로 3×1: 검정 1 · 흰색 2
    _, r = one_region(img, 0, 0, 1, 3, cfg)
    assert (r.font_color, r.bg_color, r.otsu.border_px, r.otsu.dark_px) == ("#000000", "#FFFFFF", 3, 1)
    assert r.est_font_px == 4.05


def test_half_up_0_5(cfg):
    img = canvas()
    img[0, 0:5] = [(0, 0, 0), (1, 1, 1), WHITE, WHITE, WHITE]  # 1×5
    _, r = one_region(img, 0, 0, 5, 1, cfg)
    # t=1(위 직접 검증) → 어둠 {0, 1}, 외곽 2 : 3 → 배경 흰색, 글자 중앙값 0.5 → 1 (은행가 반올림이면 0)
    assert (r.otsu.threshold, r.font_color, r.bg_color) == (1, "#010101", "#FFFFFF")


def test_even_median_half_up_distinguishes_bankers_rounding(cfg):
    img = canvas()
    ring = [(0, 0), (0, 1), (0, 2), (1, 2), (2, 2), (2, 1), (2, 0), (1, 0)]
    for i, (y, x) in enumerate(ring):
        img[y, x] = (100, 50, 7) if i % 2 else (101, 51, 8)  # 외곽 8픽셀 = 4 + 4
    img[1, 1] = WHITE
    _, r = one_region(img, 0, 0, 3, 3, cfg)
    # 명도 (100,50,7)=60 · (101,51,8)=61 · 흰색 255 → t=60: 796²/20 = 31680.8 / t=61: 1556²/8 = 302642 → t=61, 배경 = 어둠 8
    # 배경 채널 중앙값 100.5 · 50.5 · 7.5 → half-up 101 · 51 · 8 = #653308 (은행가 반올림이면 #643208)
    assert (r.otsu.threshold, r.otsu.dark_px, r.bg_color, r.font_color) == (61, 8, "#653308", "#FFFFFF")


def test_odd_median_and_dark_background_by_border(cfg):
    img = canvas()
    img[0, 0:5] = [(0, 0, 0), (10, 10, 10), (30, 30, 30), (250, 250, 250), (250, 250, 250)]
    _, r = one_region(img, 0, 0, 5, 1, cfg)
    # t=30 → 어둠 {0, 10, 30} · 밝음 {250, 250}, 외곽 3 : 2 → 배경 = 어둠, 중앙값 10(홀수 개)
    assert (r.otsu.threshold, r.bg_color, r.font_color) == (30, "#0A0A0A", "#FAFAFA")


def test_crop_has_no_margin(cfg):
    img = canvas()
    img[5:8, 5:8] = BLACK  # 영역 (5,5,3,3) 안은 모두 검정 — 바깥 흰색을 섞으면 두 집단이 된다
    _, r = one_region(img, 5, 5, 3, 3, cfg)
    assert (r.status, r.color_error) == ("color_failed", "single_class")


# ---------------------------------------------------------------------------
# 3. 블록 대표색 집계
# ---------------------------------------------------------------------------
def three_colored_regions(img, fgs, bgs, order):
    """3×3 영역 3개(가로로 x=0, 10, 20): 외곽 = bgs[i], 가운데 = fgs[i]. order는 source_lines에 넣는 영역 순서."""
    regs = []
    for i, (fg, bg) in enumerate(zip(fgs, bgs)):
        x = 10 * i
        img[0:3, x:x + 3] = bg
        img[1, x + 1] = fg
        regs.append(R(f"reg_{i + 1:04d}", x, 0, 3, 3))
    return B(1, L("line_001", *[regs[i] for i in order]))


def test_block_color_nearest_real_region_color_and_independent_bg(cfg):
    fgs = [(10, 0, 0), (0, 12, 0), (0, 0, 14)]  # 명도 3 · 7 · 2 < 배경 → 글자 = 가운데
    bgs = [(200, 200, 200), (210, 210, 210), WHITE]
    for order in ((0, 1, 2), (2, 1, 0), (1, 2, 0)):
        img = canvas()
        res = ex(img, bundle([three_colored_regions(img, fgs, bgs, order)]), cfg)
        blk = res.blocks[0]
        # 글자: 채널 중앙값 (0,0,0) — 실제 색 아님. 거리 100 · 144 · 196 → (10,0,0). 동률이 아니라 순서와 무관
        # 배경: 채널 중앙값 (210,210,210) = 둘째 영역 배경. 글자와 다른 영역에서 골라진다(독립 집계)
        assert (blk.font_color, blk.bg_color) == ("#0A0000", "#D2D2D2"), order
        assert blk.font_color in {r.font_color for r in blk.regions} and blk.bg_color in {r.bg_color for r in blk.regions}


def test_block_color_tie_uses_region_order(cfg):
    fgs = [(10, 0, 0), (0, 10, 0), (0, 0, 10)]  # 중앙값 (0,0,0)까지 모두 100
    for order, want in (((0, 1, 2), "#0A0000"), ((1, 2, 0), "#000A00"), ((2, 0, 1), "#00000A")):
        img = canvas()
        blk = ex(img, bundle([three_colored_regions(img, fgs, [WHITE] * 3, order)]), cfg).blocks[0]
        assert blk.font_color == want, order
        assert [r.region_key for r in blk.regions] == [f"reg_{i + 1:04d}" for i in order]


# ---------------------------------------------------------------------------
# 4. 크기
# ---------------------------------------------------------------------------
def test_region_sizes_and_block_median_odd_even(cfg):
    img = canvas()
    regs = [R("reg_0001", 0, 0, 3, 1), R("reg_0002", 5, 0, 3, 3), R("reg_0003", 10, 0, 3, 20)]
    blk = ex(img, bundle([B(1, L("line_001", *regs))]), cfg).blocks[0]
    assert [r.est_font_px for r in blk.regions] == [1.35, 4.05, 27.0]
    assert blk.est_font_px == 4.05  # 홀수 개 중앙값
    regs.append(R("reg_0004", 15, 0, 3, 20))
    blk = ex(img, bundle([B(1, L("line_001", *regs))]), cfg).blocks[0]
    assert blk.est_font_px == 15.525  # 짝수: (81/20 + 27)/2 = 621/40


def test_size_includes_color_failed_and_excludes_blank_and_excluded(cfg):
    img = canvas()
    uniform = R("reg_0001", 0, 0, 3, 4)  # 흰색뿐 → single_class, 크기 5.4
    inked = R("reg_0002", 5, 0, 3, 2)  # 3×2 가운데 검정 1 : 흰색 5, 크기 2.7
    ink(img, inked)
    blank = R("reg_0003", 10, 0, 3, 30, text="  ")  # 공백 — 크기 집계 제외
    blk = ex(img, bundle([B(1, L("line_001", uniform, inked, blank))]), cfg).blocks[0]
    assert blk.est_font_px == 4.05  # (5.4 + 2.7)/2, 공백 높이 30은 빠짐
    assert blk.regions[2].est_font_px is None
    tall = B(2, L("line_002", R("reg_0010", 20, 0, 3, 40)))
    res = ex(img, bundle([B(1, L("line_001", inked)), tall], {"blk_002": "label"}), cfg)
    assert res.blocks[0].est_font_px == 2.7 and res.blocks[1].est_font_px is None


# ---------------------------------------------------------------------------
# 5. 정렬(섹션 기준) — 블록마다 한 줄 · 영역 하나, 흰 바탕 · 영역 가운데 검정 1픽셀. 기본 허용 = 섹션 폭 × 0.02
# ---------------------------------------------------------------------------
T = "가"


def section_case(spans, cfg, *, W=200, H=120, excl=None):
    """spans[i] = 블록 i+1의 비공백 영역 (x0, x1). text가 공백이면 (x0, x1, " ")."""
    img = canvas(W, H)
    blocks = []
    for i, sp in enumerate(spans):
        x0, x1, text = (*sp, T) if len(sp) == 2 else sp
        r = R(f"reg_{i + 1:04d}", x0, 8 * i, x1 - x0, 5, text=text)
        if text.strip():
            ink(img, r)
        blocks.append(B(i + 1, L(f"line_{i + 1:03d}", r)))
    return ex(img, bundle(blocks, excl, W=W, H=H), cfg).blocks


def aligned(blocks):
    return [(b.align, b.align_basis, b.null_reasons.get("align")) for b in blocks]


def test_section_center_single_block_and_boundary(cfg):
    # 섹션 폭 200 → 허용 4. 블록 [80,120] 중심 100 = 섹션 중앙 → center 지지 1(섹션 중앙만)
    blk, = section_case([(80, 120)], cfg)
    assert aligned([blk]) == [("center", "section", None)]
    d = blk.align_diag
    assert (d.section_width, d.tolerance_px, d.span_x1, d.span_x2, d.section_center) == (200, 4.0, 80, 120, True)
    assert d.scores.model_dump() == {"left": 0, "center": 1, "right": 0} and d.support.model_dump() == {"left": [], "center": [], "right": []}
    # 중심 96 → 차이 4(경계, 포함) / 중심 95.5 → 차이 4.5(밖) → 지지 없음
    assert aligned(section_case([(72, 120)], cfg)) == [("center", "section", None)]
    assert aligned(section_case([(71, 120)], cfg)) == [(None, None, "no_anchor")]


def test_section_single_off_center_block_is_not_judged(cfg):
    # 한 줄 블록 하나가 섹션 왼쪽에 있으면 근거 없음 → None(no_anchor) — 렌더에서 역할별 기본 정렬. 이전 규칙(줄 진단)은 left 기본값
    blk, = section_case([(10, 50)], cfg)
    assert aligned([blk]) == [(None, None, "no_anchor")]
    assert blk.status == "ok" and blk.font_color == "#000000"
    assert (blk.align_diag.lines.align, blk.align_diag.lines.basis) == ("left", "default_single_line")


def test_section_left_peers(cfg):
    # 왼쪽 10 · 10 · 12 → 서로 허용 4 안. 중심 30 · 65 · 46, 오른쪽 50 · 120 · 80은 맞지 않고 섹션 중앙(100)도 아니다
    blocks = section_case([(10, 50), (10, 120), (12, 80)], cfg)
    assert aligned(blocks) == [("left", "section", None)] * 3
    assert blocks[0].align_diag.support.left == ["blk_002", "blk_003"] and blocks[2].align_diag.support.left == ["blk_001", "blk_002"]
    assert [b.align_diag.scores.model_dump() for b in blocks] == [{"left": 2, "center": 0, "right": 0}] * 3


def test_section_right_peers(cfg):
    blocks = section_case([(100, 190), (150, 190)], cfg)  # 오른쪽 190 공유, 왼쪽 · 중심(145 · 170)은 다름
    assert aligned(blocks) == [("right", "section", None)] * 2


def test_section_tie_is_not_judged_and_section_center_breaks_tie(cfg):
    # 같은 범위 두 블록: 왼쪽 · 중심 · 오른쪽이 모두 맞아 1 · 1 · 1 동률 → None(align_tie)
    assert aligned(section_case([(20, 60), (20, 60)], cfg)) == [(None, None, "align_tie")] * 2
    # 같은 범위가 섹션 중앙이면 center만 +1 → 1 · 2 · 1 → center
    blocks = section_case([(80, 120), (80, 120)], cfg)
    assert aligned(blocks) == [("center", "section", None)] * 2
    assert blocks[0].align_diag.scores.model_dump() == {"left": 1, "center": 2, "right": 1}


def test_section_two_columns_center_within_column(cfg):
    # 2단: 왼쪽 칸 [20,80] · [35,65](중심 50), 오른쪽 칸 [120,180] · [135,165](중심 150). 칸 안 중심이 맞아 모두 center
    blocks = section_case([(20, 80), (35, 65), (120, 180), (135, 165)], cfg)
    assert aligned(blocks) == [("center", "section", None)] * 4
    assert [b.align_diag.support.center for b in blocks] == [["blk_002"], ["blk_001"], ["blk_004"], ["blk_003"]]
    assert not any(b.align_diag.section_center for b in blocks)


def test_section_peer_boundary_is_exact_not_float(cfg):
    # 폭 400 × 0.0725 → 정확한 허용 29. 부동소수 400 × 0.0725 = 28.999999999999996이면 경계가 빠진다.
    # [0,10] · [29,40]: 왼쪽 차 29(경계) · 중심 5 ↔ 34.5(29.5) · 오른쪽 30 → left
    c = with_style(cfg, align_section_tolerance=0.0725)
    assert aligned(section_case([(0, 10), (29, 40)], c, W=400)) == [("left", "section", None)] * 2
    assert aligned(section_case([(0, 10), (30, 40)], c, W=400)) == [(None, None, "no_anchor")] * 2


def test_section_excluded_and_no_text_blocks_are_not_peers(cfg):
    # 대상 [10,50] · 라벨 [10,60] · 로고 [10,70] · 공백만 [10,80] → 왼쪽이 같아도 동료가 아니다
    blocks = section_case([(10, 50), (10, 60), (10, 70), (10, 80, " ")], cfg, excl={"blk_002": "label", "blk_003": "logo"})
    assert [b.status for b in blocks] == ["ok", "excluded", "excluded", "no_text"]
    assert aligned([blocks[0], blocks[3]]) == [(None, None, "no_anchor"), (None, None, "no_text_lines")]
    assert blocks[0].align_diag.support.left == [] and blocks[3].align_diag.span_x1 is None


def test_section_span_uses_non_blank_regions_only(cfg):
    # 블록 1 = 글자 [80,120] + 공백 [0,200] → span [80,120](섹션 중앙). 공백 영역을 넣으면 [0,200]이라 왼쪽이 블록 2와 맞았을 것
    img = canvas(200, 120)
    a, blank, b = R("reg_0001", 80, 0, 40, 5), R("reg_0002", 0, 8, 200, 5, text=" "), R("reg_0003", 0, 16, 30, 5)
    ink(img, a)
    ink(img, b)
    res = ex(img, bundle([B(1, L("line_001", a), L("line_002", blank)), B(2, L("line_003", b))], W=200, H=120), cfg)
    d = res.blocks[0].align_diag
    assert (d.span_x1, d.span_x2, res.blocks[0].align) == (80, 120, "center")
    assert d.support.left == [] and res.blocks[1].align is None


# ---------------------------------------------------------------------------
# 5-1. 줄 진단(이전 규칙 — 판정에 쓰지 않음) — 흰 바탕 · 영역 가운데 검정 1픽셀, 줄 높이 5, 줄 간격 8
# ---------------------------------------------------------------------------
def align_block(spans, cfg, *, W=220, H=120):
    """spans[i] = 줄 i의 [(x0, x1, text), ...]. 공백이 아닌 영역에 잉크를 찍는다."""
    img = canvas(W, H)
    lines, n = [], 0
    for i, segs in enumerate(spans):
        regs = []
        for x0, x1, text in segs:
            n += 1
            r = R(f"reg_{n:04d}", x0, 8 * i, x1 - x0, 5, text=text)
            if text.strip():
                ink(img, r)
            regs.append(r)
        lines.append(L(f"line_{i + 1:03d}", *regs))
    return ex(img, bundle([B(1, *lines)], W=W, H=H), cfg).blocks[0]


def line_diag(spans, cfg, **kw):
    return align_block(spans, cfg, **kw).align_diag.lines


def test_line_diag_left_unique(cfg):
    # 줄 [10,50] [10,70] [10,30], 블록 폭 60 → 허용 7.2(분산 51.84). 왼쪽 분산 0 · 중앙 {30,40,20} 200/3 · 오른쪽 {50,70,30} 800/3
    blk = align_block([[(10, 50, T)], [(10, 70, T)], [(10, 30, T)]], cfg)
    d = blk.align_diag.lines
    assert (d.align, d.basis, d.null_reason, d.candidates) == ("left", "estimated", None, ["left"])
    assert d.tolerance_px == 7.2
    assert d.std_left == 0.0
    assert d.std_center == pytest.approx(math.sqrt(200 / 3))
    assert d.std_right == pytest.approx(math.sqrt(800 / 3))
    # 줄 진단은 판정에 쓰지 않는다 — 섹션(폭 220)에 블록 하나, 중심 40 → 근거 없음
    assert blk.status == "ok" and (blk.align, blk.null_reasons["align"]) == (None, "no_anchor")


def test_line_diag_center_unique(cfg):
    # 가운데 100: [80,120] [90,110] [85,115], 폭 40 → 허용 4.8(23.04). 좌 · 우 분산 50/3 ≤ 23.04 → 후보, 중앙 0 → 중앙
    d = line_diag([[(80, 120, T)], [(90, 110, T)], [(85, 115, T)]], cfg)
    assert (d.align, d.basis, d.candidates) == ("center", "estimated", ["left", "center", "right"])


def test_line_diag_right_unique(cfg):
    # 오른쪽 150: [110,150] [90,150] [130,150], 폭 60 → 51.84. 오른쪽 0 · 중앙 200/3 · 왼쪽 800/3
    d = line_diag([[(110, 150, T)], [(90, 150, T)], [(130, 150, T)]], cfg)
    assert (d.align, d.basis, d.candidates) == ("right", "estimated", ["right"])


def test_line_diag_tolerance_boundary_inclusive_and_outside(cfg):
    # 폭 100 → 허용 12(분산 144). [0,40] [24,100]: 왼쪽 차 24 → 분산 144(경계, 포함) · 중앙 20↔62 → 441 · 오른쪽 40↔100 → 900
    d = line_diag([[(0, 40, T)], [(24, 100, T)]], cfg)
    assert (d.align, d.basis, d.candidates, d.std_left) == ("left", "estimated", ["left"], 12.0)
    # 한 칸 밖: [25,100] → 왼쪽 156.25 · 중앙 42.5²/4 = 451.5625 · 오른쪽 900 → 후보 없음
    d = line_diag([[(0, 40, T)], [(25, 100, T)]], cfg)
    assert (d.align, d.basis, d.null_reason, d.candidates) == (None, None, "no_candidate", [])


def test_line_diag_boundary_is_exact_not_float(cfg):
    # align_tolerance 0.29, 폭 100 → 정확한 허용 29(분산 841). 부동소수 100 × 0.29 = 28.999999999999996이면 경계가 빠진다.
    # [0,40] [58,100]: 왼쪽 차 58 → 841(경계) · 오른쪽 차 60 → 900 · 중앙 20↔79 → 870.25
    d = line_diag([[(0, 40, T)], [(58, 100, T)]], with_style(cfg, align_tolerance=0.29))
    assert (d.align, d.basis, d.candidates) == ("left", "estimated", ["left"])


def test_line_diag_no_candidate_but_status_ok(cfg):
    # [0,10] [100,190], 폭 190 → 22.8(519.84). 왼쪽 2500 · 중앙 5↔145 4900 · 오른쪽 8100
    blk = align_block([[(0, 10, T)], [(100, 190, T)]], cfg)
    d = blk.align_diag.lines
    assert (d.align, d.basis, d.null_reason, d.candidates) == (None, None, "no_candidate", [])
    assert blk.status == "ok" and blk.font_color == "#000000" and blk.est_font_px == 6.75  # 높이 5 × 1.35


def test_line_diag_single_line_default(cfg):
    d = line_diag([[(30, 60, T)]], cfg)
    assert d.model_dump() == {"align": "left", "basis": "default_single_line", "null_reason": None, "std_left": 0.0, "std_center": 0.0,
                              "std_right": 0.0, "tolerance_px": 3.6, "candidates": ["left", "center", "right"]}


def test_line_diag_tie_default_left(cfg):
    d = line_diag([[(50, 60, T)], [(50, 60, T)]], cfg)  # 같은 두 줄 → 세 분산 모두 0
    assert (d.align, d.basis, d.candidates) == ("left", "default_tie", ["left", "center", "right"])


def test_line_diag_tie_default_left_even_if_left_not_candidate(cfg):
    # [12,30] [0,34]: 왼쪽 차 12 → 36 · 중앙 21↔17 → 4 · 오른쪽 30↔34 → 4. 폭 34 → 4.08(16.6464)
    # 후보 = 중앙 · 오른쪽, 최솟값 동률 → left(default_tie), left는 후보 밖
    d = line_diag([[(12, 30, T)], [(0, 34, T)]], cfg)
    assert (d.align, d.basis, d.candidates) == ("left", "default_tie", ["center", "right"])


def test_line_diag_excludes_blank_only_line_and_uses_block_bbox_width(cfg):
    # 줄 1 [0,40] · 줄 2 [0,20] · 줄 3 공백만 [100,140]. 공백 줄 제외 → 왼쪽 0 · 중앙 20↔10 → 25 · 오른쪽 40↔20 → 100
    # 허용 오차는 TextBlock.bbox.w = 140(공백 영역 포함) × 0.12 = 16.8 → 셋 다 후보, 왼쪽 최소 하나
    blk = align_block([[(0, 40, T)], [(0, 20, T)], [(100, 140, " ")]], cfg)
    assert blk.align_diag.lines.model_dump() == {"align": "left", "basis": "estimated", "null_reason": None, "std_left": 0.0,
                                                 "std_center": 5.0, "std_right": 10.0, "tolerance_px": 16.8,
                                                 "candidates": ["left", "center", "right"]}
    assert blk.counts.model_dump() == {"regions": 3, "measured": 2, "color_failed": 0, "blank_text": 1}


def test_line_diag_mixed_line_uses_non_blank_union(cfg):
    # 줄 1 = 글자 [0,10] + 공백 [100,140](줄 bbox는 [0,140]) · 줄 2 = 글자 [0,10]. 비공백 합집합이면 두 줄 같음 → 동률
    d = line_diag([[(0, 10, T), (100, 140, " ")], [(0, 10, T)]], cfg)
    assert (d.align, d.basis) == ("left", "default_tie")
    assert (d.std_left, d.std_center, d.std_right) == (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# 6. 상태와 제외
# ---------------------------------------------------------------------------
def test_no_text_block(cfg):
    img = canvas()
    blk = ex(img, bundle([B(1, L("line_001", R("reg_0001", 0, 0, 10, 5, text=" "), R("reg_0002", 20, 0, 5, 5, text="")))]),
             cfg).blocks[0]
    assert blk.status == "no_text"
    assert (blk.font_color, blk.bg_color, blk.est_font_px, blk.align, blk.align_basis) == (None,) * 5
    assert blk.null_reasons == {"font_color": "no_text", "bg_color": "no_text", "est_font_px": "no_text", "align": "no_text_lines"}
    assert blk.align_diag.model_dump() == {
        "section_width": 64, "tolerance_px": 1.28, "span_x1": None, "span_x2": None, "section_center": False,  # 섹션 폭 64 × 0.02
        "support": {"left": [], "center": [], "right": []}, "scores": {"left": 0, "center": 0, "right": 0},
        "lines": {"align": None, "basis": None, "null_reason": "no_text_lines", "std_left": None, "std_center": None, "std_right": None,
                  "tolerance_px": 3.0, "candidates": []}}  # 블록 폭 25 × 0.12
    assert blk.counts.model_dump() == {"regions": 2, "measured": 0, "color_failed": 0, "blank_text": 2}
    assert [r.status for r in blk.regions] == ["blank_text", "blank_text"]


def test_partial_some_and_all_color_failed(cfg):
    img = canvas()
    good = R("reg_0001", 0, 0, 3, 3)
    ink(img, good)
    bad = R("reg_0002", 10, 0, 3, 3)  # 흰색뿐
    blk = ex(img, bundle([B(1, L("line_001", good, bad))]), cfg).blocks[0]
    assert (blk.status, blk.font_color, blk.bg_color, blk.est_font_px) == ("partial", "#000000", "#FFFFFF", 4.05)
    assert blk.counts.model_dump() == {"regions": 2, "measured": 1, "color_failed": 1, "blank_text": 0}
    assert blk.null_reasons == {"align": "no_anchor"}  # 섹션에 블록 하나 · 중앙 아님 → 정렬 근거 없음(색 실패와 무관)
    blk = ex(canvas(), bundle([B(1, L("line_001", R("reg_0001", 0, 0, 3, 3), R("reg_0002", 10, 0, 3, 2)))]), cfg).blocks[0]
    assert (blk.status, blk.font_color, blk.bg_color, blk.est_font_px) == ("partial", None, None, 3.375)  # (81/20 + 54/20)/2 = 27/8
    assert blk.null_reasons == {"font_color": "no_valid_region", "bg_color": "no_valid_region", "align": "no_anchor"}
    assert blk.counts.model_dump() == {"regions": 2, "measured": 0, "color_failed": 2, "blank_text": 0}


def test_label_and_logo_excluded(cfg):
    img = canvas()
    target = R("reg_0001", 0, 0, 3, 3)
    ink(img, target)
    lab = B(2, L("line_002", R("reg_0002", 10, 0, 3, 3), R("reg_0003", 15, 0, 3, 3, text=" ")))
    logo = B(3, L("line_003", R("reg_0004", 20, 0, 3, 3)))
    res = ex(img, bundle([B(1, L("line_001", target)), lab, logo], {"blk_002": "label", "blk_003": "logo"}), cfg)
    assert [b.status for b in res.blocks] == ["ok", "excluded", "excluded"]
    for blk, reason, n in ((res.blocks[1], "product_label", 2), (res.blocks[2], "brand_logo", 1)):
        assert blk.excluded_reason == reason
        assert blk.regions == [] and blk.align_diag is None and blk.align_basis is None
        assert blk.counts.model_dump() == {"regions": n, "measured": 0, "color_failed": 0, "blank_text": 0}
        assert blk.null_reasons == dict.fromkeys(("font_color", "bg_color", "est_font_px", "align"), "excluded")


def test_low_score_including_zero_is_measured(cfg):
    img = canvas()
    r0 = R("reg_0001", 0, 0, 3, 3, score=0.0)
    r1 = R("reg_0002", 10, 0, 3, 3, score=0.1)
    ink(img, r0)
    ink(img, r1)
    blk = ex(img, bundle([B(1, L("line_001", r0, r1))]), cfg).blocks[0]
    assert [(r.status, r.score) for r in blk.regions] == [("measured", 0.0), ("measured", 0.1)]
    assert blk.status == "ok"


def test_result_order_and_identity(cfg):
    img = canvas()
    b2 = B(2, L("line_002", R("reg_0003", 10, 10, 3, 3)), L("line_003", R("reg_0004", 10, 20, 3, 3)))
    b1 = B(1, L("line_001", R("reg_0002", 0, 0, 3, 3), R("reg_0001", 5, 0, 3, 3)))
    res = ex(img, bundle([b2, b1]), cfg)  # 입력 순서와 무관하게 block_order 순
    assert [b.block_key for b in res.blocks] == ["blk_001", "blk_002"]
    assert [(r.line_key, r.region_key) for r in res.blocks[0].regions] == [("line_001", "reg_0002"), ("line_001", "reg_0001")]
    assert [(r.line_key, r.region_key) for r in res.blocks[1].regions] == [("line_002", "reg_0003"), ("line_003", "reg_0004")]
    assert (res.image_id, res.section_key, res.schema_version) == (IMAGE_ID, KEY, "2")
    assert res.config.model_dump() == {"method": "otsu_border", "em_ratio": 1.35, "align_section_tolerance": 0.02, "align_tolerance": 0.12}


# ---------------------------------------------------------------------------
# 7. 입력 오류 — 정상 입력에서 한 조건씩 바꾼다
# ---------------------------------------------------------------------------
def base_blocks():
    t = B(1, L("line_001", R("reg_0001", 0, 0, 3, 3)))
    lab = B(2, L("line_002", R("reg_0002", 10, 0, 3, 3)))
    logo = B(3, L("line_003", R("reg_0003", 20, 0, 3, 3)))
    return [t, lab, logo]


EXCL = {"blk_002": "label", "blk_003": "logo"}


def run_base(cfg, *, blocks=None, img=None, mutate=None, cfg_=None, image_id=IMAGE_ID, **bundle_kw):
    bd = bundle(blocks or base_blocks(), EXCL, **bundle_kw)
    if mutate:
        mutate(bd)
    return ex(canvas() if img is None else img, bd, cfg_ or cfg, image_id=image_id)


def test_base_case_is_valid(cfg):
    assert [b.status for b in run_base(cfg).blocks] == ["partial", "excluded", "excluded"]


@pytest.mark.parametrize("name, mutate, needle", [
    ("④ 실패", lambda bd: bd.update(label=LabelResult(section_key=KEY, status="failed", labels=None, checked=bd["label"].checked,
                                                      error="x")), "④ 결과가 ok가 아니다"),
    ("⑤ 실패", lambda bd: bd.update(logo=LogoResult(image_id=IMAGE_ID, section_key=KEY, status="failed", decisions=None, error="x")),
     "⑤ 결과가 ok가 아니다"),
    ("④ 누락", lambda bd: bd.update(label=bd["label"].model_copy(update={"labels": bd["label"].labels[:2]})), "④ 판정 누락 ['blk_003']"),
    ("⑤ 누락", lambda bd: bd.update(logo=bd["logo"].model_copy(update={"decisions": bd["logo"].decisions[:2]})),
     "⑤ 판정 누락 ['blk_003']"),
    ("④ 중복", lambda bd: bd.update(label=bd["label"].model_copy(update={"labels": bd["label"].labels + bd["label"].labels[:1]})),
     "④ 판정 중복 ['blk_001']"),
    ("④ 미등록", lambda bd: bd.update(label=bd["label"].model_copy(update={"labels": bd["label"].labels + [
        LabelDecision(block_key="blk_999", is_product_label=False, basis="vlm")]})), "③에 없는 블록의 ④ 판정 ['blk_999']"),
    ("④ section_key", lambda bd: bd.update(label=bd["label"].model_copy(update={"section_key": "sec_9_99"})), "④ section_key sec_9_99"),
    ("⑤ section_key", lambda bd: bd.update(logo=bd["logo"].model_copy(update={"section_key": "sec_9_99"})), "⑤ section_key sec_9_99"),
    ("③ section_key", lambda bd: bd.update(merged=bd["merged"].model_copy(update={"section_key": "sec_9_99"})),
     "③ section_key sec_9_99"),
    ("⑤ image_id", lambda bd: bd.update(logo=bd["logo"].model_copy(update={"image_id": "OTHER"})), "⑤ image_id OTHER"),
    ("기록 상태", lambda bd: bd["record"].update(status="failed"), "⑤ 기록의 상태 · 식별자 불일치"),
    ("기록 image_id", lambda bd: bd["record"].update(image_id="OTHER"), "⑤ 기록의 상태 · 식별자 불일치"),
    ("기록 블록 지문", lambda bd: bd["record"].update(blocks_fingerprint="0" * 64), "⑤ 기록의 블록 지문"),
    ("기록 ④ 지문", lambda bd: bd["record"].update(label_fingerprint="0" * 64), "⑤ 기록의 ④ 결과 지문"),
    ("기록 판정", lambda bd: bd["record"]["decisions"][0].update(is_brand_logo=True), "⑤ 기록의 판정 목록"),
    ("기록 판정 순서", lambda bd: bd["record"].update(decisions=bd["record"]["decisions"][::-1]), "⑤ 기록의 판정 목록"),
    ("기록 형식", lambda bd: bd.update(record=["not", "a", "dict"]), "⑤ 기록(logo_debug)이 객체가 아니다"),
])
def test_input_errors_decisions_and_identifiers(cfg, name, mutate, needle):
    expect_input_error(needle, lambda: run_base(cfg, mutate=mutate))


def test_label_fingerprint_mismatch_alone(cfg):
    # ④ 입력 지문만 틀리고 ⑤ 기록은 그 ④ 결과에 맞춘다(한 조건만)
    expect_input_error("④ 입력 지문", lambda: run_base(cfg, label_fp="f" * 64))


def test_label_true_requires_logo_omission(cfg):
    decs = [LogoDecision(block_key="blk_001", is_brand_logo=False, basis="no_match"),
            LogoDecision(block_key="blk_002", is_brand_logo=False, basis="no_match"),  # ④ true인데 ⑤ boolean
            LogoDecision(block_key="blk_003", is_brand_logo=True, basis="exact_match")]
    expect_input_error("blk_002: ④ true인데 ⑤가 생략(product_label/null)이 아니다", lambda: run_base(cfg, logo_decisions=decs))


def test_label_false_requires_logo_boolean(cfg):
    decs = [LogoDecision(block_key="blk_001", is_brand_logo=None, basis="product_label"),  # ④ false인데 ⑤ null
            LogoDecision(block_key="blk_002", is_brand_logo=None, basis="product_label"),
            LogoDecision(block_key="blk_003", is_brand_logo=True, basis="exact_match")]
    expect_input_error("blk_001: ④ false인데 ⑤ 값이 boolean이 아니다", lambda: run_base(cfg, logo_decisions=decs))


def test_block_section_key_mismatch(cfg):
    blocks = base_blocks()
    blocks[0] = blocks[0].model_copy(update={"section_key": "sec_9_99"})
    expect_input_error("③ 블록의 section_key가 섹션과 다르다 ['blk_001']", lambda: run_base(cfg, blocks=blocks))


def test_duplicate_block_key(cfg):
    a = B(1, L("line_001", R("reg_0001", 0, 0, 3, 3)))
    b = B(2, L("line_002", R("reg_0002", 10, 0, 3, 3)), key="blk_001")
    labels = [LabelDecision(block_key="blk_001", is_product_label=False, basis="vlm")]
    decs = [LogoDecision(block_key="blk_001", is_brand_logo=False, basis="no_match")]
    bd = bundle([a, b], labels=labels, logo_decisions=decs)
    expect_input_error("③ block_key 중복 ['blk_001']", lambda: ex(canvas(), bd, cfg))


def test_duplicate_region_key_across_blocks(cfg):
    blocks = [B(1, L("line_001", R("reg_0001", 0, 0, 3, 3))), B(2, L("line_002", R("reg_0001", 10, 0, 3, 3)))]
    expect_input_error("원시 region_key 중복 ['reg_0001']", lambda: ex(canvas(), bundle(blocks), cfg))


def test_duplicate_line_key_in_target_blocks(cfg):
    blocks = [B(1, L("line_001", R("reg_0001", 0, 0, 3, 3))), B(2, L("line_001", R("reg_0002", 10, 0, 3, 3)))]
    expect_input_error("line_key 중복 ['line_001']", lambda: ex(canvas(), bundle(blocks), cfg))


def test_duplicate_line_key_involving_excluded_block(cfg):
    blocks = [B(1, L("line_001", R("reg_0001", 0, 0, 3, 3))), B(2, L("line_001", R("reg_0002", 10, 0, 3, 3)))]
    expect_input_error("line_key 중복 ['line_001']", lambda: ex(canvas(), bundle(blocks, {"blk_002": "label"}), cfg))


def test_image_shape_dtype_channels(cfg):
    expect_input_error("섹션 이미지 크기 (64, 47) ≠ 섹션 메타데이터 (64, 48)", lambda: run_base(cfg, img=canvas()[:-1]))
    expect_input_error("섹션 이미지 크기 (63, 48)", lambda: run_base(cfg, img=canvas()[:, :-1]))
    expect_input_error("uint8 RGB 배열", lambda: run_base(cfg, img=canvas().astype(np.int16)))
    expect_input_error("uint8 RGB 배열", lambda: run_base(cfg, img=np.zeros((48, 64, 4), dtype=np.uint8)))
    expect_input_error("uint8 RGB 배열", lambda: run_base(cfg, img=np.zeros((48, 64), dtype=np.uint8)))
    expect_input_error("uint8 RGB 배열", lambda: run_base(cfg, img=canvas().tolist()))


def test_out_of_bounds_region_line_block_including_excluded(cfg):
    # 영역(처리 대상)
    blocks = base_blocks()
    blocks[0] = B(1, L("line_001", R("reg_0001", 62, 0, 3, 3)))
    expect_input_error("blk_001/reg_0001: 영역 bbox", lambda: run_base(cfg, blocks=blocks))
    # 영역(라벨 제외 블록) — 제외 블록도 거부
    blocks = base_blocks()
    blocks[1] = B(2, L("line_002", R("reg_0002", 10, 46, 3, 3)))
    expect_input_error("blk_002/reg_0002: 영역 bbox", lambda: run_base(cfg, blocks=blocks))
    # 줄 bbox만 밖
    blocks = base_blocks()
    blocks[0] = B(1, L("line_001", R("reg_0001", 0, 0, 3, 3), bbox=BBox(x=0, y=0, w=65, h=3)), bbox=BBox(x=0, y=0, w=3, h=3))
    expect_input_error("blk_001/line_001: 줄 bbox", lambda: run_base(cfg, blocks=blocks))
    # 블록 bbox만 밖(음수 좌표)
    blocks = base_blocks()
    blocks[2] = B(3, L("line_003", R("reg_0003", 20, 0, 3, 3)), bbox=BBox(x=-1, y=0, w=4, h=3))
    expect_input_error("blk_003: 블록 bbox", lambda: run_base(cfg, blocks=blocks))


@pytest.mark.parametrize("i", [0, 1], ids=["처리 대상", "라벨 제외 블록"])
@pytest.mark.parametrize("x, y", [(62, 0), (0, 46), (-2, 0)], ids=["오른쪽 밖", "아래 밖", "음수 x"])
def test_region_out_of_bounds_alone(cfg, i, x, y):
    # 영역 bbox와 poly만 영상(64×48) 밖으로 옮긴다 — bbox = poly 외접 사각형, 줄 · 블록 bbox는 원래(영상 안) 그대로,
    # 판정 · 지문은 bundle이 현재 블록으로 맞춘다. 실패 원인은 영역 경계 밖 하나여야 한다
    blocks = base_blocks()
    keep = blocks[i].bbox
    moved = R(f"reg_000{i + 1}", x, y, 3, 3)
    assert moved.bbox == BBox.from_poly(list(moved.poly))
    assert not (keep.x < 0 or keep.y < 0 or keep.x2 > 64 or keep.y2 > 48)
    blocks[i] = B(i + 1, L(f"line_00{i + 1}", moved, bbox=keep), bbox=keep)
    with pytest.raises(StyleInputError) as ei:
        run_base(cfg, blocks=blocks)
    msg = str(ei.value)
    assert f"blk_00{i + 1}/reg_000{i + 1}: 영역 bbox {{'x': {x}, 'y': {y}, 'w': 3, 'h': 3}}가 영상 [0,64]×[0,48] 밖" in msg
    for other in ("줄 bbox", "블록 bbox", "지문", "판정", "poly 외접", "면적이 0", "중복"):
        assert other not in msg, other


def test_edge_touching_bbox_is_allowed(cfg):
    blocks = base_blocks()
    r = R("reg_0001", 61, 45, 3, 3)  # x2 = W, y2 = H — 가장자리 접촉
    blocks[0] = B(1, L("line_001", r))
    img = canvas()
    ink(img, r)
    assert run_base(cfg, blocks=blocks, img=img).blocks[0].status == "ok"


def test_bbox_poly_mismatch(cfg):
    blocks = base_blocks()
    blocks[0] = B(1, L("line_001", R("reg_0001", 0, 0, 3, 3, bbox=BBox(x=0, y=0, w=3, h=4))))
    expect_input_error("blk_001/reg_0001: bbox", lambda: run_base(cfg, blocks=blocks))


def test_zero_area_target_rejected_but_skipped_allowed(cfg):
    blocks = base_blocks()
    blocks[0] = B(1, L("line_001", R("reg_0001", 5, 5, 3, 0)))
    expect_input_error("blk_001/reg_0001: 측정 대상 영역 bbox의 면적이 0", lambda: run_base(cfg, blocks=blocks))
    blocks[0] = B(1, L("line_001", R("reg_0001", 5, 5, 0, 3)))
    expect_input_error("측정 대상 영역 bbox의 면적이 0", lambda: run_base(cfg, blocks=blocks))
    # 공백 영역 · 제외 블록의 면적 0은 그것만으로 거부하지 않는다
    blocks = base_blocks()
    blocks[0] = B(1, L("line_001", R("reg_0001", 0, 0, 3, 3), R("reg_0004", 30, 30, 0, 0, text="")))
    blocks[1] = B(2, L("line_002", R("reg_0002", 10, 0, 3, 0)))
    res = run_base(cfg, blocks=blocks)
    assert [b.status for b in res.blocks] == ["partial", "excluded", "excluded"]
    assert res.blocks[0].regions[1].status == "blank_text"


@pytest.mark.parametrize("style_over, needle", [
    ({"method": "kmeans"}, "style.method='kmeans'"),
    ({"em_ratio": True}, "style.em_ratio=True"),
    ({"em_ratio": float("nan")}, "style.em_ratio=nan"),
    ({"em_ratio": float("inf")}, "style.em_ratio=inf"),
    ({"em_ratio": 0}, "style.em_ratio=0"),
    ({"em_ratio": -1.35}, "style.em_ratio=-1.35"),
    ({"em_ratio": "1.35"}, "style.em_ratio='1.35'"),
    ({"align_tolerance": False}, "style.align_tolerance=False"),
    ({"align_tolerance": float("nan")}, "style.align_tolerance=nan"),
    ({"align_tolerance": float("-inf")}, "style.align_tolerance=-inf"),
    ({"align_tolerance": -0.01}, "style.align_tolerance=-0.01"),
    ({"align_section_tolerance": True}, "style.align_section_tolerance=True"),
    ({"align_section_tolerance": float("nan")}, "style.align_section_tolerance=nan"),
    ({"align_section_tolerance": -0.01}, "style.align_section_tolerance=-0.01"),
    ({"align_section_tolerance": "0.02"}, "style.align_section_tolerance='0.02'"),
])
def test_invalid_config_values(cfg, style_over, needle):
    expect_input_error(needle, lambda: run_base(cfg, cfg_=with_style(cfg, **style_over)))


def test_missing_config(cfg):
    c = copy.deepcopy(cfg)
    del c["style"]["em_ratio"]
    expect_input_error("없는 키 style.em_ratio", lambda: run_base(cfg, cfg_=c))
    c = copy.deepcopy(cfg)
    del c["style"]["align_section_tolerance"]
    expect_input_error("없는 키 style.align_section_tolerance", lambda: run_base(cfg, cfg_=c))
    c = copy.deepcopy(cfg)
    del c["style"]
    expect_input_error("config에 [style] 표가 없다", lambda: run_base(cfg, cfg_=c))


def test_zero_align_tolerance_is_valid(cfg):
    d = line_diag([[(10, 20, T)], [(10, 30, T)]], with_style(cfg, align_tolerance=0))
    # 허용 0: 왼쪽 분산 0 ≤ 0만 후보
    assert (d.align, d.basis, d.candidates, d.tolerance_px) == ("left", "estimated", ["left"], 0.0)


def test_zero_align_section_tolerance_is_valid(cfg):
    # 허용 0: 정확히 같은 왼쪽만 맞는다(중심 · 오른쪽은 다름)
    blocks = section_case([(10, 50), (10, 60)], with_style(cfg, align_section_tolerance=0))
    assert aligned(blocks) == [("left", "section", None)] * 2 and blocks[0].align_diag.tolerance_px == 0.0


def test_empty_image_id(cfg):
    expect_input_error("원본 이미지 식별자가 비어 있다", lambda: run_base(cfg, image_id=""))


# 수정 회귀: 설정 × 실제 크기의 float 표현
def test_em_ratio_overflow_with_actual_height(cfg):
    blocks = base_blocks()
    blocks[0] = B(1, L("line_001", R("reg_0001", 0, 0, 3, 2)))  # 2 × 1e308 > float 최대
    expect_input_error("blk_001/reg_0001: 영역 높이 2 × style.em_ratio=1e+308",
                       lambda: run_base(cfg, blocks=blocks, cfg_=with_style(cfg, em_ratio=1e308)))


def test_em_ratio_huge_but_representable_is_not_rejected(cfg):
    # 측정 대상 높이 1 → 1e308(표현 가능). 제외 블록 · 공백 영역의 큰 높이는 계산 대상이 아니라 거부 이유가 아니다
    img = canvas()
    r = R("reg_0001", 0, 0, 3, 1)
    ink(img, r)
    blocks = [B(1, L("line_001", r, R("reg_0009", 10, 0, 3, 40, text=" "))),
              B(2, L("line_002", R("reg_0002", 20, 0, 3, 40))), B(3, L("line_003", R("reg_0003", 30, 0, 3, 40)))]
    res = run_base(cfg, blocks=blocks, img=img, cfg_=with_style(cfg, em_ratio=1e308))
    assert res.blocks[0].est_font_px == 1e308 and res.blocks[0].regions[0].est_font_px == 1e308


def test_align_tolerance_overflow_with_actual_width(cfg):
    blocks = base_blocks()  # 처리 대상 블록 폭 3 × 1e308 > float 최대
    expect_input_error("blk_001: 블록 폭 3 × style.align_tolerance=1e+308",
                       lambda: run_base(cfg, blocks=blocks, cfg_=with_style(cfg, align_tolerance=1e308)))


def test_align_tolerance_huge_but_representable_is_not_rejected(cfg):
    img = canvas()
    r = R("reg_0001", 0, 0, 1, 3)  # 폭 1 → 1e308(표현 가능)
    ink(img, r)
    blocks = [B(1, L("line_001", r)), B(2, L("line_002", R("reg_0002", 10, 0, 40, 3))), B(3, L("line_003", R("reg_0003", 0, 10, 60, 3)))]
    res = run_base(cfg, blocks=blocks, img=img, cfg_=with_style(cfg, align_tolerance=1e308))
    assert res.blocks[0].align_diag.lines.tolerance_px == 1e308 and res.blocks[0].status == "ok"


def test_align_section_tolerance_overflow_with_section_width(cfg):
    # 섹션 폭 64 × 1e308 > float 최대 / 64 × 1e306은 표현 가능(모든 차이가 허용 안 → 동률 · 섹션 중앙 판정과 무관하게 결과는 유효)
    expect_input_error("섹션 폭 64 × style.align_section_tolerance=1e+308",
                       lambda: run_base(cfg, cfg_=with_style(cfg, align_section_tolerance=1e308)))
    img = canvas()
    ink(img, base_blocks()[0].source_lines[0].regions[0])
    res = run_base(cfg, img=img, cfg_=with_style(cfg, align_section_tolerance=1e306))
    assert res.blocks[0].align_diag.tolerance_px == 6.4e307 and res.blocks[0].align == "center"  # 동료 없음 · 섹션 중앙만 지지


# ---------------------------------------------------------------------------
# 8. 결과 타입 — 모순을 거부한다
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sample():
    # 섹션 폭 64 → 허용 1.28. blk_001 [10,13] · blk_004 [10,60]은 왼쪽이 맞아 left, blk_002 [40,43]은 근거 없음,
    # blk_005 · blk_006은 같은 범위 [50,53]이라 동률. 줄 진단: blk_001 같은 두 줄 → default_tie, blk_002 한 줄, blk_004 왼쪽 10 고정
    cfg = cfgmod.load_config()
    img = canvas()
    regs = {n: R(f"reg_{n:04d}", *xywh) for n, xywh in {1: (10, 0, 3, 3), 2: (10, 8, 3, 3), 3: (40, 0, 3, 3), 5: (10, 20, 30, 5),
                                                          6: (10, 30, 50, 5), 7: (50, 40, 3, 3), 8: (50, 44, 3, 3)}.items()}
    for r in regs.values():
        ink(img, r)
    blocks = [B(1, L("line_001", regs[1]), L("line_002", regs[2])), B(2, L("line_003", regs[3])),
              B(3, L("line_004", R("reg_0004", 45, 0, 5, 5, text=" "))), B(4, L("line_005", regs[5]), L("line_006", regs[6])),
              B(5, L("line_007", regs[7])), B(6, L("line_008", regs[8]))]
    res = ex(img, bundle(blocks), cfg)
    return {"blocks": {b.block_key: b.model_dump(mode="json") for b in res.blocks}, "result": res.model_dump(mode="json")}


@pytest.fixture
def sample_blocks(sample):
    return copy.deepcopy(sample["blocks"])


def invalid(data, needle):
    with pytest.raises(ValidationError, match=re.escape(needle)):
        BlockStyle.model_validate(data)


def invalid_result(data, needle):
    with pytest.raises(ValidationError, match=re.escape(needle)):
        StyleResult.model_validate(data)


def test_sample_blocks_are_valid(sample_blocks):
    s = sample_blocks
    assert [(s[k]["status"], s[k]["align"], s[k]["align_basis"], s[k]["null_reasons"].get("align")) for k in sorted(s)] == [
        ("ok", "left", "section", None), ("ok", None, None, "no_anchor"), ("no_text", None, None, "no_text_lines"),
        ("ok", "left", "section", None), ("ok", None, None, "align_tie"), ("ok", None, None, "align_tie")]
    assert s["blk_001"]["align_diag"]["support"] == {"left": ["blk_004"], "center": [], "right": []}
    assert s["blk_005"]["align_diag"]["scores"] == {"left": 1, "center": 1, "right": 1}
    lines = {k: (s[k]["align_diag"]["lines"]["align"], s[k]["align_diag"]["lines"]["basis"]) for k in ("blk_001", "blk_002", "blk_004")}
    assert lines == {"blk_001": ("left", "default_tie"), "blk_002": ("left", "default_single_line"), "blk_004": ("left", "estimated")}
    # 줄 진단 blk_004: 왼쪽 10 · 10 → 0, 중앙 25 · 35 → 25, 오른쪽 40 · 60 → 100, 폭 50 → 허용 6(36) → 후보 left · center, 최소 left
    assert s["blk_004"]["align_diag"]["lines"]["candidates"] == ["left", "center"]


def test_type_rejects_ok_with_null_values(sample_blocks):
    d = copy.deepcopy(sample_blocks["blk_002"])
    d.update(font_color=None, bg_color=None, null_reasons={"font_color": "no_valid_region", "bg_color": "no_valid_region",
                                                           "align": "no_anchor"})
    invalid(d, "measured 영역이 있으면 두 대표색이 있어야 한다")
    d = copy.deepcopy(sample_blocks["blk_002"])
    d.update(est_font_px=None, null_reasons={"est_font_px": "no_valid_region", "align": "no_anchor"})
    invalid(d, "ok이면 est_font_px가 있어야 한다")


def test_type_rejects_counts_mismatch(sample_blocks):
    d = copy.deepcopy(sample_blocks["blk_001"])
    d["counts"]["measured"] = 1
    d["counts"]["blank_text"] = 1  # 합은 맞지만 영역 상태와 다름
    invalid(d, "counts가 영역 결과의 상태 수와 다르다")
    d = copy.deepcopy(sample_blocks["blk_001"])
    d["counts"]["regions"] = 3
    invalid(d, "measured + color_failed + blank_text = regions")


def test_type_rejects_null_reasons_mismatch(sample_blocks):
    d = copy.deepcopy(sample_blocks["blk_001"])
    d["null_reasons"] = {"align": "no_anchor"}  # align은 left인데 사유가 있다
    invalid(d, "null_reasons의 키는 값이 None인 측정값 필드와 같아야 한다")
    d = copy.deepcopy(sample_blocks["blk_003"])
    del d["null_reasons"]["est_font_px"]
    invalid(d, "null_reasons의 키는 값이 None인 측정값 필드와 같아야 한다")


def test_type_rejects_section_align_not_unique_winner(sample_blocks):
    d = sample_blocks["blk_001"]
    d["align"] = "center"
    invalid(d, "section이면 지지 수 최댓값 정렬이 align 하나여야 한다")
    d = sample_blocks["blk_005"]  # 동률인데 하나를 골랐다
    d.update(align="left", align_basis="section", null_reasons={})
    invalid(d, "section이면 지지 수 최댓값 정렬이 align 하나여야 한다")


def test_type_rejects_null_align_reason_against_scores(sample_blocks):
    for reason, needle in (("no_anchor", "no_anchor면 모든 지지 수가 0이어야 한다"),
                           ("align_tie", "align_tie면 지지 수 최댓값 정렬이 둘 이상이어야 한다")):
        d = copy.deepcopy(sample_blocks["blk_001"])
        d.update(align=None, align_basis=None, null_reasons={"align": reason})
        invalid(d, needle)


def test_type_rejects_scores_not_matching_support(sample_blocks):
    d = sample_blocks["blk_001"]
    d["align_diag"]["scores"]["left"] = 2
    invalid(d, "scores는 지지 동료 수(center는 섹션 중앙 일치 +1)와 같아야 한다")


def test_type_rejects_line_diag_contradictions(sample_blocks):
    d = copy.deepcopy(sample_blocks["blk_001"])
    d["align_diag"]["lines"]["align"] = "center"
    invalid(d, "default_tie이면 align은 left여야 한다")
    d = copy.deepcopy(sample_blocks["blk_004"])
    d["align_diag"]["lines"]["align"] = "right"
    invalid(d, "estimated면 align이 candidates 안에 있어야 한다")


def test_type_rejects_align_null_basis_mismatch(sample_blocks):
    d = sample_blocks["blk_004"]
    d["align"] = None
    d["null_reasons"] = {"align": "no_anchor"}
    invalid(d, "align이 None이면 align_basis도 None이어야 한다")


def test_type_rejects_no_text_contradictions(sample_blocks):
    d = copy.deepcopy(sample_blocks["blk_003"])
    d["est_font_px"] = 2.0
    del d["null_reasons"]["est_font_px"]
    invalid(d, "no_text는 측정값이 모두 None")
    d = copy.deepcopy(sample_blocks["blk_003"])
    d["align_diag"].update(span_x1=45, span_x2=50)
    invalid(d, "align 사유 no_text_lines는 span이 없을 때만이다")
    d = copy.deepcopy(sample_blocks["blk_003"])
    d["null_reasons"]["align"] = "no_anchor"
    invalid(d, "no_text는 측정값이 모두 None이고 사유가 no_text · 정렬 no_text_lines여야 한다")


def test_result_rejects_inconsistent_peers(sample):
    StyleResult.model_validate(sample["result"])
    for left, needle in ((["blk_002", "blk_004"], "관계가 대칭이 아니다"),  # blk_002는 blk_001을 지지하지 않는다
                         (["blk_001", "blk_004"], "자신이 아닌 비교 대상 블록이어야"),
                         (["blk_003", "blk_004"], "자신이 아닌 비교 대상 블록이어야"),  # 비공백 영역 없는 블록은 동료가 아니다
                         (["blk_004", "blk_002"], "블록 순서여야")):
        r = copy.deepcopy(sample["result"])
        d = r["blocks"][0]["align_diag"]
        d["support"]["left"], d["scores"]["left"] = left, 2
        invalid_result(r, needle)


# ---------------------------------------------------------------------------
# 9. 진입점과 재현성
# ---------------------------------------------------------------------------
def rich_case():
    img = canvas()
    regs = [R("reg_0001", 0, 0, 3, 3), R("reg_0002", 0, 8, 5, 3), R("reg_0003", 20, 0, 3, 3, score=0.0)]
    for r in regs:
        ink(img, r, (40, 80, 120))
    blocks = [B(1, L("line_001", regs[0]), L("line_002", regs[1])), B(2, L("line_003", regs[2], R("reg_0004", 30, 0, 3, 3, text=" "))),
              B(3, L("line_004", R("reg_0005", 40, 0, 4, 4))), B(4, L("line_005", R("reg_0006", 50, 0, 4, 4)))]
    return img, bundle(blocks, {"blk_003": "label", "blk_004": "logo"})


def test_extract_is_repeatable_and_does_not_mutate_inputs(cfg):
    img, bd = rich_case()
    before = ({k: v.model_dump() for k, v in bd.items() if k != "record"}, copy.deepcopy(bd["record"]), img.copy())
    cfg_before = copy.deepcopy(cfg)
    first = ex(img, bd, cfg)
    second = ex(img, bd, cfg)
    assert first.model_dump_json() == second.model_dump_json()
    assert {k: v.model_dump() for k, v in bd.items() if k != "record"} == before[0]
    assert bd["record"] == before[1]
    assert np.array_equal(img, before[2]) and img.dtype == np.uint8
    assert cfg == cfg_before
    assert first.input_fingerprints.model_dump() == {
        "blocks": input_fingerprint(bd["merged"].blocks), "label": sha256_json(bd["label"].model_dump(mode="json")),
        "logo": sha256_json(bd["logo"].model_dump(mode="json")), "logo_record": sha256_json(bd["record"])}
    assert type(first).model_validate_json(first.model_dump_json()) == first


def _run(bd, path, cfg):
    section = bd["section"].model_copy(update={"image_path": str(path)})
    return style.run(IMAGE_ID, section, bd["merged"], bd["label"], bd["logo"], bd["record"], cfg)


def test_run_rgb_file_matches_extract(cfg, tmp_path):
    from PIL import Image

    img, bd = rich_case()
    p = tmp_path / "sec.png"
    Image.fromarray(img).save(p)
    assert _run(bd, p, cfg) == ex(img, bd, cfg)


def test_run_rejects_non_rgb_and_unreadable(cfg, tmp_path):
    from PIL import Image

    img, bd = rich_case()
    rgba = tmp_path / "rgba.png"
    Image.fromarray(np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])).save(rgba)
    expect_input_error("섹션 이미지 모드 RGBA — RGB만 지원", lambda: _run(bd, rgba, cfg))
    gray = tmp_path / "gray.png"
    Image.fromarray(img[..., 0]).save(gray)
    expect_input_error("섹션 이미지 모드 L", lambda: _run(bd, gray, cfg))
    expect_input_error("섹션 이미지를 열 수 없다", lambda: _run(bd, tmp_path / "missing.png", cfg))
    garbage = tmp_path / "garbage.png"
    garbage.write_bytes(b"not an image")
    expect_input_error("섹션 이미지를 열 수 없다", lambda: _run(bd, garbage, cfg))
    good = tmp_path / "good.png"
    Image.fromarray(img).save(good)
    truncated = tmp_path / "truncated.png"
    truncated.write_bytes(good.read_bytes()[:60])
    expect_input_error("섹션 이미지를 열 수 없다", lambda: _run(bd, truncated, cfg))


def test_run_size_mismatch_from_file(cfg, tmp_path):
    from PIL import Image

    img, bd = rich_case()
    p = tmp_path / "small.png"
    Image.fromarray(img[:-2]).save(p)
    expect_input_error("섹션 이미지 크기 (64, 46) ≠ 섹션 메타데이터 (64, 48)", lambda: _run(bd, p, cfg))
