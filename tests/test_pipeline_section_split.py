"""① 섹션 분해 — 색 전환 경계 · 최소 높이 · 긴 구간 VLM 경계 선택 · 여백 보정 · 실패 전파."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pipeline import config as cfgmod
from pipeline.stages import section_split as ss
from pipeline.types import SourceImage
from pipeline.vlm import VlmError, parse_boundaries

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "pipeline/samples/synthetic_01"
WHITE, GRAY, DARK = (255, 255, 255), (236, 240, 245), (40, 40, 40)


@pytest.fixture
def cfg():
    return cfgmod.load_config()


def _image(height: int, bands: list[tuple[int, int, tuple[int, int, int]]], width: int = 600) -> Image.Image:
    """bands: (top, bottom, color) 구간을 흰 바탕 위에 칠한다."""
    im = Image.new("RGB", (width, height), WHITE)
    d = ImageDraw.Draw(im)
    for top, bottom, color in bands:
        d.rectangle((0, top, width, bottom - 1), fill=color)
    return im


def _text_rows(im: Image.Image, y: int, h: int = 20, x0: int = 60, x1: int = 540) -> None:
    """글자 대신 어두운 가로 띠(행 폭의 80%)로 텍스트 행을 흉내낸다."""
    ImageDraw.Draw(im).rectangle((x0, y, x1, y + h - 1), fill=DARK)


# ---- 색 전환 경계 -------------------------------------------------------------------
def test_synthetic_sample_matches_expected_split(cfg, tmp_path):
    src = SourceImage(source_image_id=1, upload_order=1, path=str(SAMPLE / "source.png"))
    res = ss.run(src, cfg, tmp_path / "sections", vlm=lambda *_: pytest.fail("짧은 원본에는 VLM을 부르지 않는다"))
    got = json.loads(res.model_dump_json())
    exp = json.loads((SAMPLE / "expected/split.json").read_text(encoding="utf-8"))
    for d in (got, exp):
        for s in d["sections"]:
            s.pop("image_path")
    assert got == exp
    assert Image.open(res.sections[1].image_path).size == (600, 440)


def test_color_transition_is_first_row_of_new_color(cfg):
    im = _image(1200, [(700, 1200, GRAY)])
    assert ss.decide_boundaries(im, cfg["section"]) == [700]


def test_short_color_bump_from_title_text_is_not_a_boundary(cfg):
    im = _image(1000, [])
    _text_rows(im, 300, h=40)  # 큰 제목: 행 폭 80%를 덮어 행 중앙값이 잠깐 어두워진다
    assert ss.decide_boundaries(im, cfg["section"]) == []


def test_gentle_gradient_is_not_cut(cfg):
    im = Image.new("RGB", (600, 1400), WHITE)
    d = ImageDraw.Draw(im)
    for y in range(1400):
        v = 255 - round(60 * y / 1400)  # 1400px에 걸쳐 60 단계 어두워짐
        d.line((0, y, 600, y), fill=(v, v, v))
    assert ss.color_boundaries(*ss.row_profile(im), 8, 12, 200, 6.0) == []


def test_transition_inside_photo_is_not_a_boundary(cfg):
    # 400-800 사진(행마다 무작위 색, 폭 전체): 사진 위쪽 경계는 흰 균일 행에 닿아 인정, 사진 내부 색 변화는 아님
    im = _image(1400, [])
    px = im.load()
    rng = __import__("random").Random(0)
    for y in range(400, 800):
        base = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for x in range(600):
            px[x, y] = tuple(min(255, max(0, c + rng.randrange(-60, 60))) for c in base)
    bounds = ss.decide_boundaries(im, cfg["section"])
    assert all(b in (400, 800) for b in bounds), bounds


def test_min_section_drops_later_boundary(cfg):
    # 흰 0-500 · 회색 500-620 · 흰 620-1200: 두 번째 전환은 120px 뒤라 버린다
    im = _image(1200, [(500, 620, GRAY)])
    assert ss.decide_boundaries(im, cfg["section"]) == [500]


def test_enforce_min_section_respects_fixed_and_edges():
    # 150: 위 끝과 150 · 700/760: 고정 600과 100/160 · 1900: 아래 끝과 100 → 모두 200 미만이라 버린다
    assert ss.enforce_min_section([150, 700, 760, 1900], 2000, 200, fixed=[600]) == [600]
    assert ss.enforce_min_section([400, 1000], 2000, 200, fixed=[600]) == [400, 600, 1000]
    assert ss.enforce_min_section([1900], 2000, 200) == []


# ---- 긴 구간 VLM · 보정 --------------------------------------------------------------
def _long_image() -> Image.Image:
    """흰 배경 3000px, 텍스트 띠 사이에 여백. 색 전환 없음 → 전체가 긴 구간."""
    im = _image(3000, [])
    for y in range(100, 3000, 300):  # 텍스트 띠 100-160, 400-460, …  여백 160-400, 460-700, …
        _text_rows(im, y, h=60)
    return im


def test_long_segment_calls_vlm_and_snaps_to_blank_center(cfg):
    calls: list[tuple[tuple[int, int], str]] = []

    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        calls.append((image.size, prompt))
        scale = image.height / 3000
        return [round(1220 * scale)]  # 원본 1220: 여백 1060-1300 안 → 중앙 1180으로 보정

    bounds = ss.decide_boundaries(_long_image(), cfg["section"], vlm=fake_vlm)
    assert bounds == [1180]
    (size, prompt), = calls
    assert size[1] == 1536 and size[0] == round(600 * 1536 / 3000) + ss.RULER_W  # 긴 변 1536 + 눈금 띠
    assert "1536" in prompt and "{{HEIGHT}}" not in prompt


def test_vlm_boundary_without_nearby_blank_is_discarded(cfg):
    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        scale = image.height / 3000
        return [round(1030 * scale)]  # 텍스트 띠 1000-1060 한가운데: 가장 가까운 여백까지 30px

    sc = {**cfg["section"], "snap_radius_px": 20}
    assert ss.decide_boundaries(_long_image(), sc, vlm=fake_vlm) == []


def test_vlm_result_ignores_out_of_range_and_keeps_min_section(cfg):
    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        scale = image.height / 3000
        return [0, round(1220 * scale), round(1240 * scale), round(2420 * scale), image.height]

    assert ss.decide_boundaries(_long_image(), cfg["section"], vlm=fake_vlm) == [1180, 2380]


def test_short_segments_do_not_call_vlm(cfg):
    im = _image(2600, [(1300, 2600, GRAY)])  # 두 구간 모두 1300px ≤ long_section_px
    assert ss.decide_boundaries(im, cfg["section"], vlm=lambda *_: pytest.fail("VLM 호출 금지")) == [1300]


def test_vlm_failure_propagates_unchanged(cfg):
    def failing_vlm(image, prompt):
        raise VlmError("timeout")

    with pytest.raises(VlmError):
        ss.decide_boundaries(_long_image(), cfg["section"], vlm=failing_vlm)


def test_default_picker_requires_api_key(cfg, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(VlmError, match="GEMINI_API_KEY"):
        ss.decide_boundaries(_long_image(), cfg["section"])


def test_parse_boundaries_validates_shape():
    assert parse_boundaries('{"boundaries": [300, 100, 300]}') == [100, 300]
    for bad in ("nope", '{"x": 1}', '{"boundaries": [1.5]}', '{"boundaries": "1"}'):
        with pytest.raises(VlmError):
            parse_boundaries(bad)


# ---- CLI ---------------------------------------------------------------------------
def test_cli_split_writes_sections_and_run_record(tmp_path):
    from pipeline import run as cli

    assert cli.main(["split", "--source", str(SAMPLE / "source.png"), "--out", str(tmp_path)]) == 0
    split = json.loads((tmp_path / "split.json").read_text(encoding="utf-8"))
    assert [s["top_offset"] for s in split["sections"]] == [0, 560]
    assert (tmp_path / "sections/sec_1_02.png").exists()
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert "section_boundary.md" in record["prompt_hashes"]
