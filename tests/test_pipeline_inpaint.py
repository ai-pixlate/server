"""⑥ 인페인팅 단독 개발 v1 — 입력 검증 · 마스크 · 합성 (합성 입력 · 가짜 모델, 실제 LaMa 없음).

기대 마스크는 구현 계산을 복제하지 않고 작은 픽셀 배열을 손으로 적는다(규칙은 pipeline/stages/inpaint.py 모듈 설명).
"""
from __future__ import annotations

import copy

import numpy as np
import pytest

from pipeline import config as cfgmod
from pipeline.stages import inpaint
from pipeline.stages.inpaint import InpaintInputError, InpaintModelError
from pipeline.stages.label import input_fingerprint
from pipeline.stages.logo import sha256_json
from pipeline.types import (
    BBox, LabelChecked, LabelDecision, LabelResult, Line, LogoDecision, LogoResult, MergeResult, OcrRegion, Section, TextBlock,
)

IMAGE_ID = "IMG-01"
KEY = "sec_1_01"


def grid(*rows: str) -> np.ndarray:
    """'#' = True, '.' = False."""
    return np.array([[c == "#" for c in r] for r in rows], dtype=bool)


def rect(x: int, y: int, w: int, h: int) -> list[list[int]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def region(key: str, poly, score: float = 0.9, text: str = "글자") -> dict:
    return {"key": key, "poly": poly, "score": score, "text": text}


def make_block(n: int, regions: list[dict], *, ocr_confidence: float | None = None) -> TextBlock:
    ocr_regions = [
        OcrRegion(region_key=r["key"], text=r["text"], score=r["score"], poly=r["poly"], bbox=BBox.from_poly([tuple(p) for p in r["poly"]]))
        for r in regions
    ]
    box = BBox.union([o.bbox for o in ocr_regions])
    return TextBlock(
        block_key=f"blk_{n:03d}", section_key=KEY, block_order=n, source_ko=" ".join(r["text"] for r in regions),
        source_lines=[Line(line_key=f"line_{n:03d}", text="x", bbox=box, regions=ocr_regions)], bbox=box, role="body",
        ocr_confidence=ocr_confidence if ocr_confidence is not None else min(r["score"] for r in regions),
    )


def make_inputs(blocks: list[TextBlock], flags: list[tuple[bool, bool | None]], W: int = 12, H: int = 10):
    """flags[i] = (④ is_product_label, ⑤ is_brand_logo). ④ true면 ⑤는 None(생략)으로 준다."""
    section = Section(section_key=KEY, source_image_id=1, section_order=1, top_offset=0, height=H, width=W, image_path="unused.png")
    merged = MergeResult(section_key=KEY, blocks=blocks)
    label = LabelResult(
        section_key=KEY, status="ok",
        labels=[LabelDecision(block_key=b.block_key, is_product_label=f[0], basis="vlm") for b, f in zip(blocks, flags)],
        checked=LabelChecked(input_fingerprint=input_fingerprint(blocks), llm_called=True, sent_block_keys=[b.block_key for b in blocks]),
    )
    decisions = []
    for b, (is_label, is_logo) in zip(blocks, flags):
        basis = "product_label" if is_label else ("exact_match" if is_logo else "no_match")
        decisions.append(LogoDecision(block_key=b.block_key, is_brand_logo=None if is_label else is_logo, basis=basis))
    logo = LogoResult(image_id=IMAGE_ID, section_key=KEY, status="ok", decisions=decisions)
    record = logo_record_for(merged, label, logo)
    return section, merged, label, logo, record


def logo_record_for(merged: MergeResult, label: LabelResult, logo: LogoResult) -> dict:
    return {
        "stage": "logo", "image_id": logo.image_id, "section_key": logo.section_key, "status": "ok",
        "blocks_fingerprint": input_fingerprint(merged.blocks), "label_fingerprint": sha256_json(label.model_dump(mode="json")),
        "decisions": [{"block_key": d.block_key, "is_brand_logo": d.is_brand_logo, "basis": d.basis} for d in logo.decisions],
    }


@pytest.fixture
def cfg():
    return cfgmod.load_config()


def plan_of(cfg, section, merged, label, logo, record, size=None):
    return inpaint.validate_inputs(IMAGE_ID, section, size or (section.width, section.height), merged, label, logo, record, cfg)


def masks_of(cfg, blocks, flags, W=12, H=10):
    s, m, l, g, r = make_inputs(blocks, flags, W, H)
    plan = plan_of(cfg, s, m, l, g, r)
    return plan, inpaint.build_masks(plan)


class FakeModel:
    """호출 횟수와 입력을 기록하는 가짜 모델 — 정상 모델 동작이 아니라 테스트용이다. fill 값으로 채운 배열을 돌려준다."""

    def __init__(self, fill: int = 7, output=None, exc: Exception | None = None, mutate_input: bool = False):
        self.calls: list[tuple[np.ndarray, np.ndarray]] = []
        self.fill, self.output, self.exc, self.mutate_input = fill, output, exc, mutate_input

    def describe(self):
        return {"name": "fake"}

    def inpaint(self, image, mask):
        self.calls.append((image.copy(), mask.copy()))
        if self.mutate_input:
            image[:] = 0
            mask[:] = 0
        if self.exc:
            raise self.exc
        if self.output is not None:
            return self.output
        return np.full_like(image, self.fill)


def image_of(W=12, H=10) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(10, 250, size=(H, W, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# 설정 · 반경
# ---------------------------------------------------------------------------
def test_default_config_values_unchanged(cfg):
    assert cfg["inpaint"] == {"model": "lama", "score_min": 0.5, "require_text": True, "dilate_ratio": 0.15, "dilate_retry": False,
                              "init_timeout_s": 60, "infer_timeout_s": 30, "kill_grace_s": 5}
    inpaint.validate_config(cfg)


@pytest.mark.parametrize("key,value", [("dilate_retry", True), ("model", "mat"), ("score_min", 1.5), ("require_text", 1),
                                       ("dilate_ratio", -0.1), ("score_min", True)])
def test_config_rejects_bad_values(cfg, key, value):
    cfg["inpaint"][key] = value
    with pytest.raises(InpaintInputError):
        inpaint.validate_config(cfg)


def test_config_missing_key(cfg):
    del cfg["inpaint"]["score_min"]
    with pytest.raises(InpaintInputError, match="score_min"):
        inpaint.validate_config(cfg)


@pytest.mark.parametrize("h,r", [(0, 0), (1, 1), (6, 1), (7, 2), (20, 3), (40, 6), (100, 15), (101, 16)])
def test_dilate_radius_default_ratio(h, r):
    assert inpaint.dilate_radius(h, 0.15) == r


def test_dilate_radius_exact_fraction_for_other_ratio():
    # 기본값 0.15에서는 부동소수와 차이가 없다. 0.07은 부동소수 100 × 0.07 = 7.000000000000001 → 8이지만 정확한 분수로 7
    assert inpaint.dilate_radius(100, 0.07) == 7


def test_dilate_radius_larger_than_array():
    # dilate_ratio 2.0 · 2×2 영역처럼 반경이 배열보다 커도 예외 없이 배열 전체를 덮는다
    m = np.zeros((2, 3), dtype=bool)
    m[0, 0] = True
    assert inpaint.dilate(m, 4).all()
    m2 = np.zeros((1, 1), dtype=bool)
    m2[0, 0] = True
    assert inpaint.dilate(m2, 5).all()


def test_large_dilate_ratio_masks_without_error(cfg):
    cfg["inpaint"]["dilate_ratio"] = 2.0
    _, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(0, 0, 2, 2))])], [(False, False)], W=3, H=3)
    assert mk.final.all()  # r = 4 ≥ 영상 크기 — 3×3 전체


# ---------------------------------------------------------------------------
# 래스터 · 커널 (손으로 적은 기대값)
# ---------------------------------------------------------------------------
def test_fill_rect_is_bbox_pixels():
    got = inpaint.fill_polygon([(1, 1), (4, 1), (4, 3), (1, 3)], 0, 0, 6, 5)
    assert (got == grid("......", ".###..", ".###..", "......", "......")).all()


def test_fill_triangle_includes_centers_on_edge():
    # 중심 (x+.5, y+.5)가 x+y ≤ 4 — 빗변 위 중심(x+y = 3)도 포함
    got = inpaint.fill_polygon([(0, 0), (4, 0), (0, 4)], 0, 0, 4, 4)
    assert (got == grid("####", "###.", "##..", "#...")).all()


def test_fill_orientation_independent():
    a = inpaint.fill_polygon([(0, 0), (4, 0), (0, 4)], 0, 0, 4, 4)
    b = inpaint.fill_polygon([(0, 4), (4, 0), (0, 0)], 0, 0, 4, 4)
    assert (a == b).all()


def test_disk_kernel_radius_2():
    m = np.zeros((5, 5), dtype=bool)
    m[2, 2] = True
    assert (inpaint.dilate(m, 2) == grid("..#..", ".###.", "#####", ".###.", "..#..")).all()


def test_disk_kernel_radius_1_and_0():
    m = np.zeros((3, 3), dtype=bool)
    m[1, 1] = True
    assert (inpaint.dilate(m, 1) == grid(".#.", "###", ".#.")).all()
    assert (inpaint.dilate(m, 0) == m).all()


def test_dilate_clips_at_array_edge_without_error():
    m = np.zeros((3, 4), dtype=bool)
    m[0, 0] = True
    assert (inpaint.dilate(m, 1) == grid("##..", "#...", "....")).all()


def test_dilate_does_not_mutate_input():
    m = np.zeros((3, 3), dtype=bool)
    m[1, 1] = True
    before = m.copy()
    inpaint.dilate(m, 1)
    assert (m == before).all()


# ---------------------------------------------------------------------------
# 마스크 규칙
# ---------------------------------------------------------------------------
def test_single_target_mask_hand_computed(cfg):
    # rect x=2..6, y=2..4 → 픽셀 열 2~5 · 행 2~3, bbox.h=2 → r=ceil(0.3)=1(십자 커널)
    _, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(2, 2, 4, 2))])], [(False, False)])
    want = grid(
        "............",
        "..####......",
        ".######.....",
        ".######.....",
        "..####......",
        "............", "............", "............", "............", "............",
    )
    assert (mk.final == want).all()
    assert (mk.delete == want).all()
    assert not mk.protect.any()
    assert mk.counts["final_px"] == 20


def test_score_boundary_and_blank_text(cfg):
    blocks = [make_block(1, [
        region("reg_0001", rect(0, 0, 2, 1), score=0.5),  # 경계값 포함 → 대상
        region("reg_0002", rect(4, 0, 2, 1), score=0.4999),  # 저신뢰 → 보호
        region("reg_0003", rect(8, 0, 2, 1), score=0.9, text=""),  # 빈 문자열 → 보호
        region("reg_0004", rect(0, 5, 2, 1), score=0.9, text="  \n"),  # 공백 → 보호
    ])]
    plan, mk = masks_of(cfg, blocks, [(False, False)])
    kinds = {r.region_key: (r.kind, r.reasons) for r in plan.regions}
    assert kinds == {
        "reg_0001": ("target", ()), "reg_0002": ("protected_region", ("low_score",)),
        "reg_0003": ("protected_region", ("blank_text",)), "reg_0004": ("protected_region", ("blank_text",)),
    }
    both = make_block(1, [region("reg_0001", rect(0, 0, 2, 1), score=0.1, text=" ")])
    plan2, _ = masks_of(cfg, [both], [(False, False)])
    assert plan2.regions[0].reasons == ("low_score", "blank_text")


def test_require_text_false_makes_blank_region_target(cfg):
    cfg["inpaint"]["require_text"] = False
    plan, _ = masks_of(cfg, [make_block(1, [region("reg_0001", rect(0, 0, 2, 1), text="")])], [(False, False)])
    assert plan.regions[0].kind == "target"


def test_block_confidence_does_not_replace_region_score(cfg):
    hi_block_lo_region = make_block(1, [region("reg_0001", rect(0, 0, 2, 2), score=0.3)], ocr_confidence=0.95)
    lo_block_hi_region = make_block(2, [region("reg_0002", rect(6, 6, 2, 2), score=0.9)], ocr_confidence=0.05)
    plan, _ = masks_of(cfg, [hi_block_lo_region, lo_block_hi_region], [(False, False), (False, False)])
    assert [(r.region_key, r.kind) for r in plan.regions] == [("reg_0001", "protected_region"), ("reg_0002", "target")]


def test_protection_overlap_hand_computed(cfg):
    # 대상 rect 열 2~5 · 행 2~3(r=1) + 저신뢰 영역 rect 열 5~6 · 행 2~3(보호)
    blocks = [make_block(1, [region("reg_0001", rect(2, 2, 4, 2)), region("reg_0002", rect(5, 2, 2, 2), score=0.2)])]
    plan, mk = masks_of(cfg, blocks, [(False, False)])
    want_final = grid(
        "............",
        "..###.......",  # 열 5는 보호와 겹치지 않음 — 보호는 행 2~3뿐이다
        ".####.......",
        ".####.......",
        "..###.......",
        "............", "............", "............", "............", "............",
    )
    # 위 두 행(1 · 4)의 열 5도 삭제 대상이지만 보호(행 2~3)와 겹치지 않으므로 남는다
    want_final[1, 5] = True
    want_final[4, 5] = True
    assert (mk.final == want_final).all()
    # 충돌 = 삭제 합집합 ∩ 보호 = 행 2~3의 열 5 · 6 → 4픽셀
    assert mk.counts["conflict_px"] == 4
    assert mk.region_pixels["reg_0001"].conflict_px == 4
    assert mk.counts["final_px"] == mk.counts["delete_px"] - 4


def test_label_and_logo_bbox_protection(cfg):
    target = make_block(1, [region("reg_0001", rect(0, 4, 12, 2))])  # h=2 → r=1, 행 3~6
    label_block = make_block(2, [region("reg_0002", rect(2, 0, 2, 10))])  # ④ true — bbox 열 2~3 전체 보호
    logo_block = make_block(3, [region("reg_0003", rect(8, 3, 2, 4))])  # ⑤ true — bbox 열 8~9 · 행 3~6 보호
    plan, mk = masks_of(cfg, [target, label_block, logo_block], [(False, False), (True, None), (False, True)])
    assert [(p.block_key, p.reason) for p in plan.protected_blocks] == [("blk_002", "product_label"), ("blk_003", "brand_logo")]
    assert {r.region_key: r.kind for r in plan.regions} == {"reg_0001": "target", "reg_0002": "in_protected_block",
                                                           "reg_0003": "in_protected_block"}
    want = grid(
        "............", "............", "............",
        "##..####..##",
        "##..####..##",
        "##..####..##",
        "##..####..##",
        "............", "............", "............",
    )
    assert (mk.final == want).all()
    assert mk.counts["conflict_px"] == 16  # 열 2~3 · 8~9 × 행 3~6


def test_duplicate_regions_do_not_accumulate_dilation(cfg):
    one = masks_of(cfg, [make_block(1, [region("reg_0001", rect(4, 4, 2, 2))])], [(False, False)])[1]
    dup = masks_of(cfg, [make_block(1, [region("reg_0001", rect(4, 4, 2, 2)), region("reg_0002", rect(4, 4, 2, 2))])],
                   [(False, False)])[1]
    assert (one.final == dup.final).all()
    assert dup.counts["final_px"] == one.counts["final_px"] == 12  # 2×2 + 반경 1 십자 = 4 + 8


def test_small_glyph_height_one(cfg):
    _, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(5, 5, 1, 1))])], [(False, False)])
    want = np.zeros((10, 12), dtype=bool)
    want[5, 5] = want[4, 5] = want[6, 5] = want[5, 4] = want[5, 6] = True  # r=ceil(0.15)=1
    assert (mk.final == want).all()


def test_zero_area_target_not_deleted(cfg):
    plan, mk = masks_of(cfg, [make_block(1, [region("reg_0001", [[1, 1], [5, 1], [5, 1], [1, 1]])])], [(False, False)])
    assert plan.regions[0].kind == "zero_area"
    assert not mk.final.any()
    diag = inpaint.region_diagnostics(plan, mk)
    assert diag[0]["kind"] == "zero_area" and diag[0]["final_px"] == 0


def test_zero_area_protected_region_aborts_section(cfg):
    blocks = [make_block(1, [region("reg_0001", rect(0, 0, 2, 2)), region("reg_0002", [[1, 1], [5, 1], [5, 1], [1, 1]], score=0.1)])]
    s, m, l, g, r = make_inputs(blocks, [(False, False)])
    with pytest.raises(InpaintInputError, match="보호를 보장할 수 없다"):
        plan_of(cfg, s, m, l, g, r)


def test_zero_area_label_bbox_aborts_section(cfg):
    blocks = [make_block(1, [region("reg_0001", [[1, 1], [5, 1], [5, 1], [1, 1]])])]
    s, m, l, g, r = make_inputs(blocks, [(True, None)])
    with pytest.raises(InpaintInputError, match="보호를 보장할 수 없다"):
        plan_of(cfg, s, m, l, g, r)


def test_poly_touching_image_edge_is_allowed(cfg):
    # x = W · y = H는 가장자리 접촉(허용) — 마지막 열 · 행까지 채운다
    plan, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(10, 8, 2, 2))])], [(False, False)], W=12, H=10)
    assert mk.final[9, 11] and mk.final[8, 10]
    assert mk.final.shape == (10, 12)


@pytest.mark.parametrize("poly", [rect(11, 0, 2, 2), [[0, -1], [2, -1], [2, 1], [0, 1]], rect(0, 9, 2, 2)])
def test_out_of_bounds_poly_is_error(cfg, poly):
    s, m, l, g, r = make_inputs([make_block(1, [region("reg_0001", poly)])], [(False, False)], W=12, H=10)
    with pytest.raises(InpaintInputError, match="밖"):
        plan_of(cfg, s, m, l, g, r)


def test_bbox_poly_mismatch_is_error(cfg):
    blk = make_block(1, [region("reg_0001", rect(0, 0, 2, 2))])
    raw = blk.model_dump()
    raw["source_lines"][0]["regions"][0]["bbox"]["h"] = 5
    blk = TextBlock.model_validate(raw)
    s, m, l, g, r = make_inputs([blk], [(False, False)])
    with pytest.raises(InpaintInputError, match="외접"):
        plan_of(cfg, s, m, l, g, r)


# ---------------------------------------------------------------------------
# 판정 · 식별자 · 지문 검증
# ---------------------------------------------------------------------------
def two_blocks():
    return [make_block(1, [region("reg_0001", rect(0, 0, 2, 2))]), make_block(2, [region("reg_0002", rect(6, 6, 2, 2))])]


def test_label_true_with_logo_null_is_normal_skip(cfg):
    s, m, l, g, r = make_inputs(two_blocks(), [(True, None), (False, False)])
    plan = plan_of(cfg, s, m, l, g, r)
    assert plan.protected_blocks[0].reason == "product_label"


@pytest.mark.parametrize("flags", [[(True, False), (False, False)], [(False, None), (False, False)]])
def test_inconsistent_label_logo_pairs_rejected(cfg, flags):
    blocks = two_blocks()
    s, m, l, _, _ = make_inputs(blocks, [(False, False), (False, False)])
    decisions = []
    for b, (is_label, is_logo) in zip(blocks, flags):
        basis = "product_label" if is_logo is None else "no_match"
        decisions.append(LogoDecision.model_construct(block_key=b.block_key, is_brand_logo=is_logo, basis=basis))
    label = LabelResult(section_key=KEY, status="ok",
                        labels=[LabelDecision(block_key=b.block_key, is_product_label=f[0], basis="vlm") for b, f in zip(blocks, flags)],
                        checked=l.checked)
    logo = LogoResult.model_construct(schema_version="1", image_id=IMAGE_ID, section_key=KEY, status="ok", decisions=decisions, error=None)
    with pytest.raises(InpaintInputError):
        plan_of(cfg, s, m, label, logo, logo_record_for(m, label, logo))


def test_missing_duplicate_unknown_decisions_rejected(cfg):
    s, m, l, g, r = make_inputs(two_blocks(), [(False, False), (False, False)])
    missing = l.model_copy(update={"labels": l.labels[:1]})
    with pytest.raises(InpaintInputError, match="④ 판정 누락"):
        plan_of(cfg, s, m, missing, g, logo_record_for(m, missing, g))
    dup = LabelResult.model_construct(**{**l.__dict__, "labels": [l.labels[0], l.labels[0]]})
    with pytest.raises(InpaintInputError, match="④ 판정 중복"):
        plan_of(cfg, s, m, dup, g, logo_record_for(m, dup, g))
    extra_logo = g.model_copy(update={"decisions": g.decisions + [LogoDecision(block_key="blk_099", is_brand_logo=False, basis="no_match")]})
    with pytest.raises(InpaintInputError, match="③에 없는 블록의 ⑤ 판정"):
        plan_of(cfg, s, m, l, extra_logo, logo_record_for(m, l, extra_logo))


def test_failed_label_or_logo_not_treated_as_false(cfg):
    s, m, l, g, r = make_inputs(two_blocks(), [(False, False), (False, False)])
    failed = LabelResult(section_key=KEY, status="failed", labels=None, checked=l.checked, error="x")
    with pytest.raises(InpaintInputError, match="④ 결과가 ok가 아니다"):
        plan_of(cfg, s, m, failed, g, r)
    lfail = LogoResult(image_id=IMAGE_ID, section_key=KEY, status="failed", decisions=None, error="x")
    with pytest.raises(InpaintInputError, match="⑤ 결과가 ok가 아니다"):
        plan_of(cfg, s, m, l, lfail, r)


def test_fingerprint_mismatch_rejected(cfg):
    blocks = two_blocks()
    s, m, l, g, r = make_inputs(blocks, [(False, False), (False, False)])
    changed = m.model_copy(deep=True)
    changed.blocks[0].source_ko = "바뀐 글자"
    with pytest.raises(InpaintInputError, match="④ 입력 지문"):
        plan_of(cfg, s, changed, l, g, r)
    bad_record = {**r, "label_fingerprint": "0" * 64}
    with pytest.raises(InpaintInputError, match="④ 결과 지문"):
        plan_of(cfg, s, m, l, g, bad_record)
    bad_rows = copy.deepcopy(r)
    bad_rows["decisions"][0]["is_brand_logo"] = True
    with pytest.raises(InpaintInputError, match="판정 목록"):
        plan_of(cfg, s, m, l, g, bad_rows)


def test_key_mismatches_rejected(cfg):
    s, m, l, g, r = make_inputs(two_blocks(), [(False, False), (False, False)])
    with pytest.raises(InpaintInputError, match="image_id"):
        inpaint.validate_inputs("OTHER", s, (12, 10), m, l, g, r, cfg)
    other_section = s.model_copy(update={"section_key": "sec_1_02"})
    with pytest.raises(InpaintInputError, match="section_key"):
        plan_of(cfg, other_section, m, l, g, r)


def test_image_size_mismatch_rejected(cfg):
    s, m, l, g, r = make_inputs(two_blocks(), [(False, False), (False, False)])
    with pytest.raises(InpaintInputError, match="크기"):
        plan_of(cfg, s, m, l, g, r, size=(12, 11))


def test_validate_does_not_mutate_inputs(cfg):
    s, m, l, g, r = make_inputs(two_blocks(), [(True, None), (False, False)])
    before = [x.model_dump(mode="json") for x in (s, m, l, g)], copy.deepcopy(r)
    plan = plan_of(cfg, s, m, l, g, r)
    inpaint.build_masks(plan)
    assert ([x.model_dump(mode="json") for x in (s, m, l, g)], r) == before


# ---------------------------------------------------------------------------
# 모델 호출 · 합성
# ---------------------------------------------------------------------------
def test_empty_mask_calls_model_zero_times_and_returns_original(cfg):
    blocks = [make_block(1, [region("reg_0001", rect(0, 0, 2, 2), score=0.1)])]  # 저신뢰뿐 → 빈 마스크
    plan, mk = masks_of(cfg, blocks, [(False, False)])
    img = image_of()
    model = FakeModel()
    res = inpaint.apply(img, plan, mk, model)
    assert model.calls == [] and res.model_called is False
    assert (res.background == img).all() and res.background is not img


def test_only_mask_pixels_come_from_model(cfg):
    plan, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(2, 2, 4, 2))])], [(False, False)])
    img = image_of()
    before = img.copy()
    model = FakeModel(fill=7, mutate_input=True)
    res = inpaint.apply(img, plan, mk, model)
    assert len(model.calls) == 1
    sent_img, sent_mask = model.calls[0]
    assert (sent_img == before).all()
    assert set(np.unique(sent_mask)) == {0, 255} and ((sent_mask == 255) == mk.final).all()
    assert (res.background[mk.final] == 7).all()
    assert (res.background[~mk.final] == before[~mk.final]).all()
    assert (img == before).all()  # 모델이 받은 복사본을 바꿔도 원본은 그대로
    assert res.background.shape == img.shape and res.background.dtype == np.uint8


@pytest.mark.parametrize("output", [
    np.zeros((10, 12, 3), dtype=np.float32), np.zeros((10, 11, 3), dtype=np.uint8), np.zeros((10, 12), dtype=np.uint8),
    [[0]], None,
])
def test_invalid_model_output_rejected(cfg, output):
    plan, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(2, 2, 4, 2))])], [(False, False)])
    model = FakeModel(output=output if output is not None else "not an array")
    with pytest.raises(InpaintModelError):
        inpaint.apply(image_of(), plan, mk, model)


def test_model_exception_wrapped(cfg):
    plan, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(2, 2, 4, 2))])], [(False, False)])
    with pytest.raises(InpaintModelError, match="MemoryError"):
        inpaint.apply(image_of(), plan, mk, FakeModel(exc=MemoryError("oom")))


def test_non_rgb_image_rejected(cfg):
    plan, mk = masks_of(cfg, [make_block(1, [region("reg_0001", rect(2, 2, 4, 2))])], [(False, False)])
    with pytest.raises(InpaintInputError):
        inpaint.apply(np.zeros((10, 12, 4), dtype=np.uint8), plan, mk, FakeModel())
