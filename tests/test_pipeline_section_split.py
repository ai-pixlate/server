"""① 섹션 분해 — 색 전환 경계 · 배경다움 · 빈 구간 병합 · 최소 높이 · 긴 구간 VLM(폭 기준·창 분할) · 여백 보정 · 실패 전파."""
from __future__ import annotations

import json
import random
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


def _text_lines(im: Image.Image, top: int, bottom: int, line_h: int = 16, gap: int = 14, margin: int = 40) -> None:
    """구간 [top, bottom)에 본문 줄을 채운다(줄 16px · 행간 14px → 균일 행 비율 ≈ 0.47, 줄 폭 40%)."""
    y = top + margin
    while y + line_h <= bottom - margin:
        _text_rows(im, y, h=line_h, x0=80, x1=320)
        y += line_h + gap


def _photo(im: Image.Image, top: int, bottom: int, seed: int = 0) -> None:
    """행마다 색이 다른 폭 전체 사진. 행 표준편차가 커서 균일 행이 없다."""
    px = im.load()
    rng = random.Random(seed)
    for y in range(top, bottom):
        base = (rng.randrange(256), rng.randrange(256), rng.randrange(256))
        for x in range(im.width):
            px[x, y] = tuple(min(255, max(0, c + rng.randrange(-60, 60))) for c in base)


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
    _text_lines(im, 0, 700)
    _text_lines(im, 700, 1200)
    assert ss.decide_boundaries(im, cfg["section"]) == [700]


def test_short_color_bump_from_title_text_is_not_a_boundary(cfg):
    im = _image(1000, [])
    _text_lines(im, 0, 1000)
    _text_rows(im, 300, h=40)  # 큰 제목: 행 폭 80%를 덮어 행 중앙값이 잠깐 어두워진다
    assert ss.decide_boundaries(im, cfg["section"]) == []


def test_gentle_gradient_is_not_cut(cfg):
    im = Image.new("RGB", (600, 1400), WHITE)
    d = ImageDraw.Draw(im)
    for y in range(1400):
        v = 255 - round(60 * y / 1400)  # 1400px에 걸쳐 60 단계 어두워짐
        d.line((0, y, 600, y), fill=(v, v, v))
    kept, _ = ss.color_boundaries(*ss.row_profile(im), 8, 12, 200, 6.0, 0.25)
    assert kept == []


def test_photo_band_between_title_and_caption_stays_in_one_section(cfg):
    # 실측 유형 A: 제목(흰) / 사진 띠 400-800 / 각주(흰). 사진 위아래 여백이 있어도 사진 쪽 구간에 균일 행이 없다.
    im = _image(1400, [])
    _text_lines(im, 0, 400)
    _photo(im, 400, 800)
    _text_lines(im, 800, 1400)
    diag: dict = {}
    assert ss.decide_boundaries(im, cfg["section"], diag=diag) == []
    # 무작위 사진은 내부 전체가 전환 구간이라 후보 위치는 사진 안쪽에 잡힌다. 기각 사유가 배경다움(bg_ratio)인지만 본다.
    assert diag["color_candidates"] and all(r["reason"] == "bg_ratio" for r in diag["color_candidates"])


def test_colored_band_with_text_is_a_section(cfg):
    # 색 배경 안에 글줄이 있으면 배경다움이 유지되어 잘린다(사진 띠와 구분)
    im = _image(1400, [(400, 800, GRAY)])
    _text_lines(im, 0, 400)
    _text_lines(im, 400, 800)
    _text_lines(im, 800, 1400)
    assert ss.decide_boundaries(im, cfg["section"]) == [400, 800]


def test_median_uses_neighbor_segments_not_fixed_window(cfg):
    # 실측 유형 C: 노란 배경 위 흰 바(18px) 4개. 바 경계는 배경 전환이 아니다.
    yellow = (250, 230, 120)
    im = _image(900, [(0, 900, yellow)])
    for top in (144, 260, 376, 492):
        ImageDraw.Draw(im).rectangle((40, top, 560, top + 17), fill=WHITE)
        _text_rows(im, top + 4, h=10, x0=60, x1=300)
    _text_lines(im, 520, 900)
    assert ss.decide_boundaries(im, cfg["section"]) == []


def test_min_section_keeps_stronger_boundary(cfg):
    # 흰 0-500 · 연회색 500-620 · 진회색 620-1200: 500↔620은 120px. 강한 전환(620, 거리 큼)이 남는다.
    im = _image(1200, [(500, 620, GRAY), (620, 1200, (180, 186, 196))])
    _text_lines(im, 0, 500)
    _text_lines(im, 620, 1200)
    assert ss.decide_boundaries(im, cfg["section"]) == [620]


def test_min_section_tie_keeps_upper_boundary(cfg):
    im = _image(1200, [(500, 620, GRAY)])
    _text_lines(im, 0, 500)
    _text_lines(im, 620, 1200)
    assert ss.decide_boundaries(im, cfg["section"]) == [500]


def test_enforce_min_section_respects_fixed_edges_and_strength():
    # 150: 위 끝과 150 · 700/760: 고정 600과 100/160 · 1900: 아래 끝과 100 → 모두 200 미만이라 버린다
    assert ss.enforce_min_section([150, 700, 760, 1900], 2000, 200, fixed=[600]) == [600]
    assert ss.enforce_min_section([400, 1000], 2000, 200, fixed=[600]) == [400, 600, 1000]
    assert ss.enforce_min_section([1900], 2000, 200) == []
    # 강도: 1000(약) 1100(강) → 1100이 남는다. 강도 없이는 앞(1000)이 남는다.
    assert ss.enforce_min_section([1000, 1100], 2000, 200, strengths={1000: 5.0, 1100: 50.0}) == [1100]
    assert ss.enforce_min_section([1000, 1100], 2000, 200) == [1000]


# ---- 빈 구간 병합 -------------------------------------------------------------------
def test_empty_band_is_merged_into_previous_segment(cfg):
    # 실측 유형 D: 흰(글) 0-600 · 노란 빈 띠 600-840 · 흰(글) 840-1400 → 빈 띠는 앞 구간에 붙는다
    im = _image(1400, [(600, 840, (250, 230, 120))])
    _text_lines(im, 0, 600)
    _text_lines(im, 840, 1400)
    diag: dict = {}
    assert ss.decide_boundaries(im, cfg["section"], diag=diag) == [840]
    assert diag["empty_merged"] == [{"top": 600, "bottom": 840, "dropped": 600}]


def test_empty_first_segment_is_merged_into_next(cfg):
    im = _image(1400, [(0, 300, GRAY)])
    _text_lines(im, 300, 1400)
    assert ss.decide_boundaries(im, cfg["section"]) == []


# ---- 긴 구간 VLM · 보정 --------------------------------------------------------------
def _long_image(height: int = 3000) -> Image.Image:
    """흰 배경, 텍스트 띠 사이에 여백. 색 전환 없음 → 전체가 긴 구간."""
    im = _image(height, [])
    for y in range(100, height, 300):  # 텍스트 띠 100-160, 400-460, …  여백 160-400, 460-700, …
        _text_rows(im, y, h=60)
    return im


def test_long_segment_calls_vlm_width_based_and_snaps_to_blank_center(cfg):
    calls: list[tuple[tuple[int, int], str]] = []

    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        calls.append((image.size, prompt))
        return [1220]  # 폭 600 ≤ 768 → 무축소. 원본 1220: 여백 1060-1300 안 → 중앙 1180으로 보정

    assert ss.decide_boundaries(_long_image(), cfg["section"], vlm=fake_vlm) == [1180]
    (size, prompt), = calls
    assert size == (600 + ss.RULER_W, 3000)  # 폭 기준이라 3000px 높이는 그대로
    assert "3000" in prompt and "{{HEIGHT}}" not in prompt


def test_wide_segment_is_scaled_by_width(cfg):
    im = _image(2000, [], width=1536)
    for y in range(100, 2000, 300):
        _text_rows(im, y, h=60, x0=100, x1=1400)
    seen: list[tuple[int, int]] = []

    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        seen.append(image.size)
        return [610]  # 축소 좌표(scale 0.5) → 원본 1220 → 여백 1060-1300 중앙 1180

    assert ss.decide_boundaries(im, cfg["section"], vlm=fake_vlm) == [1180]
    assert seen == [(768 + ss.RULER_W, 1000)]


def test_tall_segment_is_split_into_overlapping_windows(cfg):
    im = _long_image(9000)  # 4000 창 · 300 겹침 → [0,4000) [3700,7700) [7400,9000)
    windows: list[tuple[int, int]] = []

    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        windows.append(image.size)
        return [1220] if len(windows) == 1 else []  # 첫 창에서만 하나 → 원본 1220

    assert ss.vlm_windows(9000, 4000, 300) == [(0, 4000), (3700, 7700), (7400, 9000)]
    assert ss.decide_boundaries(im, cfg["section"], vlm=fake_vlm) == [1180]
    assert [h for _, h in windows] == [4000, 4000, 1600]


def test_window_results_are_mapped_and_deduplicated(cfg):
    im = _long_image(9000)

    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        # 창 [0,4000)의 3820과 창 [3700,7700)의 120은 같은 원본 3820 → 여백 3760-4000 중앙 3880 하나
        return [3820] if image.height == 4000 and not fake_vlm.second else ([120] if image.height == 4000 else [])

    fake_vlm.second = False
    calls = {"n": 0}

    def wrapper(image, prompt):
        calls["n"] += 1
        fake_vlm.second = calls["n"] == 2
        return fake_vlm(image, prompt)

    assert ss.decide_boundaries(im, cfg["section"], vlm=wrapper) == [3880]


def test_vlm_boundary_without_nearby_blank_is_dropped_and_recorded(cfg):
    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        return [1030]  # 텍스트 띠 1000-1060 한가운데: 가장 가까운 여백까지 30px

    sc = {**cfg["section"], "snap_radius_px": 20}
    diag: dict = {}
    assert ss.decide_boundaries(_long_image(), sc, vlm=fake_vlm, diag=diag) == []
    assert diag["vlm"][0]["dropped"] == [1030] and diag["vlm"][0]["snapped"] == []
    assert diag["vlm"][0]["calls"][0]["raw"] == [1030]


def test_vlm_result_ignores_out_of_range_and_keeps_min_section(cfg):
    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        return [0, 1220, 1240, 2420, image.height]

    assert ss.decide_boundaries(_long_image(), cfg["section"], vlm=fake_vlm) == [1180, 2380]


def test_short_segments_do_not_call_vlm(cfg):
    im = _image(2600, [(1300, 2600, GRAY)])  # 두 구간 모두 1300px ≤ long_section_px
    _text_lines(im, 0, 1300)
    _text_lines(im, 1300, 2600)
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


# ---- 회귀 -----------------------------------------------------------------------------
def test_snap_searches_blank_rows_within_current_color_segment_only(cfg):
    # 흰 0-3000 · 회색 3000-5000. 텍스트 띠 100-160, 3600-3660. 원본 전체로 여백을 찾으면 [160, 3600)이
    # 한 구간이 되어 두 번째 구간의 VLM 후보(약 3300)가 그 중앙 1880으로 옮겨진 뒤 구간 밖이라 버려졌다.
    im = _image(5000, [(3000, 5000, GRAY)])
    _text_rows(im, 100, h=60)
    _text_rows(im, 3600, h=60)
    seen: list[int] = []

    def fake_vlm(image: Image.Image, prompt: str) -> list[int]:
        seen.append(image.height)
        return [] if len(seen) == 1 else [300]  # 두 번째 구간(2000px) 로컬 300 → 원본 3300

    assert ss.decide_boundaries(im, cfg["section"], vlm=fake_vlm) == [3000, 3300]
    assert seen == [3000, 2000]  # 두 구간 모두 긴 구간이라 VLM 호출(폭 기준이라 무축소)


class _BrokenGenai:
    @staticmethod
    def Client(api_key):  # noqa: N802 — SDK 이름 그대로
        raise ValueError("Unsupported proxy scheme")


def _install_broken_genai(monkeypatch):
    import sys

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setitem(sys.modules, "google.genai", _BrokenGenai)
    monkeypatch.setattr(__import__("google"), "genai", _BrokenGenai, raising=False)


def test_gemini_client_init_failure_becomes_vlm_error(monkeypatch):
    from pipeline.vlm import GeminiBoundaryPicker

    _install_broken_genai(monkeypatch)
    with pytest.raises(VlmError, match="VLM 호출 실패"):
        GeminiBoundaryPicker("gemini-3.8-flash", 0)(Image.new("RGB", (10, 10)), "p")


# ---- CLI ---------------------------------------------------------------------------
def test_cli_split_writes_sections_debug_and_run_record(tmp_path):
    from pipeline import run as cli

    assert cli.main(["split", "--source", str(SAMPLE / "source.png"), "--out", str(tmp_path)]) == 0
    split = json.loads((tmp_path / "split.json").read_text(encoding="utf-8"))
    assert [s["top_offset"] for s in split["sections"]] == [0, 560]
    assert (tmp_path / "sections/sec_1_02.png").exists()
    debug = json.loads((tmp_path / "split_debug.json").read_text(encoding="utf-8"))
    assert debug["boundaries"] == [560] and debug["vlm"] == []
    assert any(r["y"] == 560 and r["accepted"] for r in debug["color_candidates"])
    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert "section_boundary.md" in record["prompt_hashes"]
    assert record["started_at"] and record["duration_s"] >= 0


def test_cli_returns_4_when_vlm_client_init_fails(tmp_path, monkeypatch):
    from pipeline import run as cli

    _install_broken_genai(monkeypatch)
    src = tmp_path / "long.png"
    _long_image().save(src)  # 3000px 흰 배경 → 전체가 긴 구간 → VLM 호출
    assert cli.main(["split", "--source", str(src), "--out", str(tmp_path / "out")]) == 4
