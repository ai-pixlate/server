"""단계 간 타입 계약 검증."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.types import (
    AnalyzeResult,
    BBox,
    Line,
    MergeResult,
    OcrRegion,
    OcrResult,
    SplitResult,
    TextBlock,
    ocr_confidence_of,
)

SAMPLE = Path(__file__).resolve().parent.parent / "pipeline" / "samples" / "synthetic_01" / "expected"


def _region(key="reg_0001", score=0.9, x=10, y=20, w=100, h=30, text="t"):
    poly = [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
    return OcrRegion(region_key=key, text=text, score=score, poly=poly, bbox=BBox(x=x, y=y, w=w, h=h))


def test_bbox_from_poly_and_union():
    b = BBox.from_poly([(10, 20), (130, 20), (130, 44), (10, 44)])
    assert b.model_dump() == {"x": 10, "y": 20, "w": 120, "h": 24}
    u = BBox.union([b, BBox(x=0, y=30, w=20, h=50)])
    assert (u.x, u.y, u.x2, u.y2) == (0, 20, 130, 80)


def test_ocr_confidence_is_min_score_or_none():
    l1 = Line(line_key="line_001", text="a", bbox=BBox(x=0, y=0, w=1, h=1), regions=[_region(score=0.9), _region("reg_0002", 0.55)])
    l2 = Line(line_key="line_002", text="b", bbox=BBox(x=0, y=0, w=1, h=1), regions=[_region("reg_0003", 0.7)])
    assert ocr_confidence_of([l1, l2]) == 0.55
    assert ocr_confidence_of([]) is None


def test_role_is_restricted_to_five_values():
    line = Line(line_key="line_001", text="a", bbox=BBox(x=0, y=0, w=1, h=1), regions=[_region()])
    kwargs = dict(block_key="blk_001", section_key="sec_1_01", block_order=1, source_ko="a", source_lines=[line], bbox=line.bbox)
    TextBlock(role="caution", **kwargs)
    with pytest.raises(ValidationError):
        TextBlock(role="product_name", **kwargs)


def test_unknown_keys_are_rejected():
    with pytest.raises(ValidationError):
        OcrResult(section_key="s", regions=[], range=[0, 10])


def test_poly_round_trips_as_lists():
    r = _region()
    data = json.loads(r.model_dump_json())
    assert data["poly"][0] == [10, 20]
    assert OcrRegion.model_validate(data) == r


def test_split_ocr_merge_chain_on_sample():
    split = SplitResult.model_validate_json((SAMPLE / "split.json").read_text(encoding="utf-8"))
    assert [s.section_order for s in split.sections] == [1, 2]
    assert split.sections[0].top_offset == 0
    assert sum(s.height for s in split.sections) == split.source_height
    for sec in split.sections:
        ocr = OcrResult.model_validate_json((SAMPLE / "ocr" / f"{sec.section_key}.json").read_text(encoding="utf-8"))
        merge = MergeResult.model_validate_json((SAMPLE / "merge" / f"{sec.section_key}.json").read_text(encoding="utf-8"))
        assert ocr.section_key == merge.section_key == sec.section_key
        # ③은 ②의 원시 영역을 잃지 않는다 [계약 2.5]
        keys_in_blocks = {r.region_key for b in merge.blocks for ln in b.source_lines for r in ln.regions}
        assert keys_in_blocks == {r.region_key for r in ocr.regions}
        for b in merge.blocks:
            assert b.section_key == sec.section_key
            assert b.ocr_confidence == ocr_confidence_of(b.source_lines)
            assert 0 <= b.bbox.y and b.bbox.y2 <= sec.height  # 섹션 로컬
        assert [b.block_order for b in merge.blocks] == list(range(1, len(merge.blocks) + 1))


def test_analyze_result_accepts_empty_blocks_with_warning():
    res = AnalyzeResult(sections=[], blocks=[], warnings=[{"code": "NO_TEXT_DETECTED", "source_image_id": 1, "message": "없음"}])
    assert res.warnings[0].code == "NO_TEXT_DETECTED"
