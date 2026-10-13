"""② OCR 임시 분할(#31 · #32) — 대역 엔진으로 타일 계획 · 좌표 복원 · 중복 정리 · 보완 OCR · 실패를 검증한다.

대역 엔진(LayoutEngine)은 섹션 좌표의 정답 박스를 받아, 넘겨받은 crop이 원본의 어느 행 구간인지(같은 메모리의 행 뷰)로
보이는 부분만 crop 로컬 좌표로 돌려준다. 잘린 박스의 인식 결과 · 한 영역 대 여러 영역 · 서로 다른 판독은 박스별 함수로
흉내 낸다. 여기 결과는 대역 검증이며 실제 PaddleOCR 검증과 구분한다.
"""
import json

import numpy as np
import pytest
from PIL import Image

from pipeline import config as cfgmod
from pipeline import jsonio
from pipeline import run as cli
from pipeline.errors import AnalyzeError
from pipeline.stages import ocr, ocr_tile
from pipeline.types import BBox, Section, SplitResult

W = 600


@pytest.fixture
def cfg():
    return cfgmod.load_config()


def P(cfg) -> ocr_tile.TileParams:
    return ocr_tile.params_of(cfg)


# ---- 대역 ----------------------------------------------------------------------------------
def box(x0, y0, x1, y1, text, **kw):
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": text, **kw}


def draw(h, boxes, *, noise=False, seed=0) -> np.ndarray:
    """흰 배경(균일 행) + 박스 안 줄무늬(비균일 행). noise=True면 전체를 잡음으로 채워 여백이 없게 한다."""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, (h, W, 3), dtype=np.uint8) if noise else np.full((h, W, 3), 255, np.uint8)
    for b in boxes:
        reg = img[b["y0"]:b["y1"], b["x0"]:b["x1"]]
        reg[:, ::2] = 0  # 세로 줄무늬 → 행 표준편차가 커서 균일 행이 아니다
    return np.ascontiguousarray(img)


class LayoutEngine:
    """정답 박스 → crop에 보이는 영역. 박스의 선택 키:
    partial(text, y0, y1, n) → 잘려 보일 때 텍스트(기본 "") · render(y0, y1, n) → [(x0, y0, x1, y1, text), ...]로 직접 결정
    (섹션 좌표, None이면 기본 동작). n은 엔진 호출 순번(0부터)."""

    def __init__(self, boxes, min_visible=4):
        self.boxes = boxes
        self.min_visible = min_visible
        self.full: np.ndarray | None = None
        self.calls: list[tuple[int, int]] = []
        self.info = {"engine": "layout-fake"}

    def offset(self, crop: np.ndarray) -> int:
        assert self.full is not None and np.shares_memory(crop, self.full), "crop은 원본 배열의 행 뷰여야 한다"
        return (crop.ctypes.data - self.full.ctypes.data) // self.full.strides[0]

    def __call__(self, crop):
        y0 = self.offset(crop)
        y1 = y0 + crop.shape[0]
        n = len(self.calls)
        self.calls.append((y0, y1))
        out = []
        for b in self.boxes:
            pieces = b["render"](y0, y1, n) if "render" in b else None
            if pieces is None:
                vy0, vy1 = max(b["y0"], y0), min(b["y1"], y1)
                if vy1 - vy0 < self.min_visible:
                    continue
                full = b["y0"] >= y0 and b["y1"] <= y1
                text = b["text"] if full else b.get("partial", lambda t, a, c, k: "")(b["text"], y0, y1, n)
                pieces = [(b["x0"], vy0, b["x1"], vy1, text)]
            out.extend(pieces)
        return {"rec_polys": [[[x0, a - y0], [x1, a - y0], [x1, c - y0], [x0, c - y0]] for x0, a, x1, c, _ in out],
                "rec_texts": [t for *_, t in out], "rec_scores": [0.9] * len(out)}


def run_ocr(monkeypatch, cfg, img, eng, tmp_path, diag=None, key="sec_1_01"):
    path = tmp_path / f"{key}.png"
    Image.new("RGB", (img.shape[1], img.shape[0])).save(path)  # 크기 확인용 파일(내용은 대역 배열을 쓴다)
    sec = Section(section_key=key, source_image_id=3, section_order=1, top_offset=0, height=img.shape[0],
                  width=img.shape[1], image_path=str(path))
    eng.full = img
    monkeypatch.setattr(ocr, "load_section_image", lambda s: img)
    return ocr.run(sec, cfg, engine=eng, diag=diag)


def texts(res):
    return [r.text for r in res.regions]


def role(n):
    """첫 경계(겹침 띠 ~[1650, 2000)) 근처 박스를 보는 호출의 역할. 타일을 위에서부터 모두 부른 뒤 보완 OCR을 부르고,
    셋째 타일은 이 띠를 덮지 않으므로 호출 0 = 위 타일, 1 = 아래 타일, 그 뒤 = 보완 crop이다."""
    return "upper" if n == 0 else "lower" if n == 1 else "supplement"


# ---- 1. 타일 계획 --------------------------------------------------------------------------
def test_threshold_boundary_4000_single_call_4001_tiled(monkeypatch, cfg, tmp_path):
    eng = LayoutEngine([box(10, 3990, 100, 3999, "끝")])
    run_ocr(monkeypatch, cfg, draw(4000, eng.boxes), eng, tmp_path)
    assert eng.calls == [(0, 4000)]
    eng2 = LayoutEngine([box(10, 3990, 100, 4000, "끝")])
    d = {}
    res = run_ocr(monkeypatch, cfg, draw(4001, eng2.boxes), eng2, tmp_path, d, key="sec_1_02")
    assert len(eng2.calls) == len(d["tiles"]) >= 2 and d["tiled"] and texts(res) == ["끝"]
    assert d["tiles"][0]["y0"] == 0 and d["tiles"][-1]["y1"] == 4001


def test_blank_cut_nearest_nominal_end_and_blank_start_keep_overlap(cfg):
    p = P(cfg)
    std = np.full(9000, 50.0, np.float32)
    std[1850:1860] = 0  # 절단 창 [1700, 2000)의 여백 — 명목 끝 2000에 가장 가까운 여백
    std[1720:1730] = 0  # 같은 창의 더 먼 여백
    std[1400:1420] = 0  # 시작 창 [c − 600, c − 300]의 여백
    tiles = ocr_tile.plan_tiles(std, p)
    t0, t1 = tiles[0], tiles[1]
    assert (t0.y0, t0.y1, t0.cut) == (0, 1855, "blank")  # 10행 여백: 글자에서 4행 떨어진 범위 중 2000에 가까운 행
    assert t1.start == "blank" and 1400 <= t1.y0 < 1420
    assert t0.y1 - t1.y0 >= p.overlap_px  # 여백에서 잘라도 겹침 유지


def test_no_blank_rows_fall_back_to_fixed_band(cfg):
    p = P(cfg)
    tiles = ocr_tile.plan_tiles(np.full(6500, 50.0, np.float32), p)
    assert [(t.y0, t.y1, t.cut, t.start) for t in tiles] == [
        (0, 2000, "fixed", "origin"), (1700, 3700, "fixed", "fixed"), (3400, 5400, "fixed", "fixed"),
        (5100, 6500, "end", "fixed")]


def test_short_tail_is_absorbed_and_short_last_tile_kept(cfg):
    p = P(cfg)
    ext = ocr_tile.plan_tiles(np.full(4200, 50.0, np.float32), p)  # 2번째 타일 [1700, 3700) 뒤 꼬리 500 ≥ 300 → 짧은 마지막 타일
    assert [(t.y0, t.y1, t.cut) for t in ext] == [(0, 2000, "fixed"), (1700, 3700, "fixed"), (3400, 4200, "end")]
    tail = ocr_tile.plan_tiles(np.full(3950, 50.0, np.float32), p)  # 1번째 [0,2000) → 2번째 [1700, 3700), H−c=250 < 300 → 연장
    assert [(t.y0, t.y1, t.cut) for t in tail] == [(0, 2000, "fixed"), (1700, 3950, "extended_end")]


@pytest.mark.parametrize("seed", range(40))
def test_plan_invariants_on_random_profiles(cfg, seed):
    p = P(cfg)
    rng = np.random.default_rng(seed)
    h = int(rng.integers(4001, 40000))
    std = np.full(h, 50.0, np.float32)
    for _ in range(int(rng.integers(0, 200))):
        a = int(rng.integers(0, h))
        std[a:a + int(rng.integers(1, 80))] = 0
    tiles = ocr_tile.plan_tiles(std, p)
    assert tiles[0].y0 == 0 and tiles[-1].y1 == h
    for a, b in zip(tiles, tiles[1:]):
        assert b.y0 > a.y0  # 커서 전진
        assert p.overlap_px <= a.y1 - b.y0 <= 2 * p.overlap_px  # 빈틈 없음 · 겹침 유지
    assert all(t.y1 - t.y0 <= p.tile_px + p.overlap_px for t in tiles)
    assert all(t.y1 - t.y0 <= p.threshold_px for t in tiles)


@pytest.mark.parametrize("key, value, match", [
    ("tile_overlap_px", 0, "0보다"), ("tile_px", 900, "3 ×"), ("tile_px", 3800, "분할 기준"),
    ("tile_match_min_v_overlap", 0, r"\(0, 1\]"), ("tile_equiv_min_x_iou", 1.5, r"\(0, 1\]"),
    ("tile_edge_touch_px", -1, "0 이상"), ("tile_px", 2000.5, "정수"),
])
def test_invalid_tile_config_is_rejected(cfg, key, value, match):
    cfg["ocr"][key] = value
    with pytest.raises(ValueError, match=match):
        ocr.validate_config(cfg)


# ---- 2. 좌표 복원 ---------------------------------------------------------------------------
def test_coordinates_restored_to_section_local_including_last_short_tile(monkeypatch, cfg, tmp_path):
    boxes = [box(20, 100, 200, 130, "첫 타일"), box(30, 2600, 300, 2640, "가운데"), box(40, 6450, 400, 6490, "마지막 짧은 타일")]
    eng = LayoutEngine(boxes)
    d = {}
    res = run_ocr(monkeypatch, cfg, draw(6550, boxes), eng, tmp_path, d)
    assert d["tiles"][-1]["height"] < cfg["ocr"]["tile_px"]  # 마지막 짧은 타일
    got = {r.text: r.bbox for r in res.regions}
    for b in boxes:
        assert got[b["text"]] == BBox(x=b["x0"], y=b["y0"], w=b["x1"] - b["x0"], h=b["y1"] - b["y0"])
    assert [r.region_key for r in res.regions] == ["reg_0001", "reg_0002", "reg_0003"]
    assert all(0 <= y <= 6550 for r in res.regions for _, y in r.poly)


# ---- 3·4. 중복 정리 -------------------------------------------------------------------------
def _band_case(boxes, h=4600, noise=False):
    return draw(h, boxes, noise=noise), LayoutEngine(boxes)


def test_duplicate_in_overlap_band_kept_once_without_supplement(monkeypatch, cfg, tmp_path):
    boxes = [box(50, 1800, 300, 1830, "겹침 띠 문구")]
    img, eng = _band_case(boxes)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["겹침 띠 문구"]
    assert d["engine_calls"]["supplements"] == 0 and len(d["dropped"]) == 1
    comp = d["components"][0]
    assert comp["kind"] == "equivalent" and comp["rule"] in ("farther_from_cut", "tie_upper", "not_touching_cut")


def test_one_region_vs_many_regions_selects_one_tile_bundle_whole(monkeypatch, cfg, tmp_path):
    def render(y0, y1, n):  # 위 타일(호출 0)은 한 영역, 아래 타일은 두 영역으로 검출
        if y0 > 1800 or y1 < 1830:
            return None
        if role(n) == "upper":
            return [(100, 1800, 400, 1830, "보습 크림")]
        return [(100, 1801, 230, 1830, "보습"), (260, 1801, 400, 1830, "크림")]
    boxes = [box(100, 1800, 400, 1830, "보습 크림", render=render)]
    img, eng = _band_case(boxes)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    comp = d["components"][0]
    assert comp["kind"] == "equivalent" and comp["upper_count"] == 1 and comp["lower_count"] == 2
    kept = texts(res)
    assert kept in (["보습 크림"], ["보습", "크림"])  # 한쪽 묶음 전체 — 양쪽 박스를 섞지 않는다
    assert d["engine_calls"]["supplements"] == 0


def test_same_cover_length_at_different_position_is_not_equivalent(monkeypatch, cfg, tmp_path):
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1830:
            return None
        if role(n) == "upper":  # 위 타일: 왼쪽 두 조각 / 아래 타일: 같은 길이지만 오른쪽으로 치우친 두 조각
            return [(0, 1800, 100, 1830, "가"), (150, 1800, 250, 1830, "나")]
        if role(n) == "lower":
            return [(50, 1800, 150, 1830, "가"), (500, 1800, 600, 1830, "나")]
        return [(0, 1800, 100, 1830, "가"), (150, 1800, 250, 1830, "나")]  # 보완 OCR
    boxes = [box(0, 1800, 600, 1830, "가나", render=render)]
    img, eng = _band_case(boxes)
    d = {}
    run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    comp = d["components"][0]
    assert comp["rule"] == "x_cover_differs" and comp["kind"] == "conflict"
    assert d["engine_calls"]["supplements"] == 1


def test_repeated_phrase_on_different_rows_is_not_merged_by_text(monkeypatch, cfg, tmp_path):
    def row(y, only):
        def render(y0, y1, n):
            if y0 > y or y1 < y + 30:
                return None
            return [(50, y, 200, y + 30, "보습")] if role(n) == only else []
        return render
    boxes = [box(50, 1760, 200, 1790, "보습", render=row(1760, "upper")),  # 위 타일만 검출
             box(50, 1850, 200, 1880, "보습", render=row(1850, "lower"))]  # 아래 타일만 검출
    img, eng = _band_case(boxes)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["보습", "보습"] and [r.bbox.y for r in res.regions] == [1760, 1850]
    assert d["engine_calls"]["supplements"] == 0  # 한쪽에만 있고 잘림 의심 없음 → 보존


def test_repeated_phrase_on_same_row_stays_two_regions(monkeypatch, cfg, tmp_path):
    boxes = [box(50, 1800, 150, 1830, "보습"), box(400, 1800, 500, 1830, "보습")]
    img, eng = _band_case(boxes)
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path)
    assert texts(res) == ["보습", "보습"] and [r.bbox.x for r in res.regions] == [50, 400]


def test_one_side_only_clean_detection_is_preserved(monkeypatch, cfg, tmp_path):
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1830:
            return None
        return [(50, 1800, 300, 1830, "흐린 글자")] if role(n) == "lower" else []
    boxes = [box(50, 1800, 300, 1830, "흐린 글자", render=render)]
    img, eng = _band_case(boxes)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["흐린 글자"] and d["engine_calls"]["supplements"] == 0 and not d["warnings"]


# ---- 5. 보완 OCR ---------------------------------------------------------------------------
def test_different_readings_trigger_one_supplement_that_confirms_one_tile(monkeypatch, cfg, tmp_path):
    readings = {"upper": "보습크림", "lower": "보숩크림", "supplement": "보습크림"}
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1830:
            return None
        return [(100, 1800, 400, 1830, readings[role(n)])]
    boxes = [box(100, 1800, 400, 1830, "보습크림", render=render), box(50, 3000, 200, 3030, "다른 줄")]
    img, eng = _band_case(boxes)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["보습크림", "다른 줄"]
    n_tiles = len(d["tiles"])
    assert len(eng.calls) == n_tiles + 1 and d["engine_calls"] == {"tiles": n_tiles, "supplements": 1, "supplement_limit": n_tiles - 1}
    sup = d["supplements"][0]
    cl = sup["clusters"][0]
    assert sup["status"] == "resolved" and cl["rule"] == "supplement_confirms_tile" and cl["use_tile"] == 0
    origins = {x["id"]: (x["origin"], x["crop"]) for x in d["detections"]}
    assert [origins[r["det"]] for r in d["regions"]] == [("tile", 0), ("tile", 1)]  # 확인된 위 타일 묶음 · 다른 줄
    ra, rb = sup["crop"]
    assert ra <= 1800 - 150 and rb >= 1830 + 150  # 위아래 여백 밖으로 경계를 옮긴 crop
    assert len(sup["detections"]) == 1  # 확인에 쓴 보완 검출은 진단에만 남는다


def test_punctuation_only_difference_is_confirmation_but_not_next_to_digits():
    f = ocr_tile.punctuation_only_diff
    assert f("큰제목", "큰·제목") and f("보습크림", "보습크림.")
    assert not f("SPF1.5", "SPF15") and not f("50%", "50") and not f("보습", "보숩") and not f("a", "a")


def test_supplement_confirming_clean_tile_modulo_punctuation_resolves(monkeypatch, cfg, tmp_path):
    # 실제 엔진 사례(2026-10-13 합성 고정 절단): 위 타일은 잘린 판독, 아래 타일은 깨끗한 "큰 제목", 보완은 "큰" + "·제목"
    def render(y0, y1, n):
        if y0 > 1850 or y1 < 2100:
            return None if role(n) != "upper" else [(100, 1850, 300, 1989, "크"), (300, 1850, 500, y1, "제목")]
        if role(n) == "lower":
            return [(100, 1850, 500, 2100, "큰 제목")]
        return [(100, 1860, 230, 2090, "큰"), (220, 1860, 500, 2090, "·제목")]
    boxes = [box(100, 1850, 500, 2100, "큰 제목", render=render)]
    img, eng = _band_case(boxes, noise=True)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["큰 제목"] and res.regions[0].bbox == BBox(x=100, y=1850, w=400, h=250)
    cl = d["supplements"][0]["clusters"][0]
    assert cl["rule"] == "supplement_confirms_tile_punctuation_only" and cl["use_tile"] == 1


def test_same_place_recognition_instability_keeps_one_tile_bundle_with_warning(monkeypatch, cfg, tmp_path):
    """세 판독이 모두 잘림 없이 같은 자리 · 같은 줄 구조인데 글자만 다르면(실측: SPF 50+ / SPF EO+ / SPF 504) 분할이 만든
    충돌이 아니라 판독 흔들림 — 대표 규칙으로 한 타일 묶음을 남기고 경고한다. 숫자가 달라도 같다(값 검증은 하지 않는다)."""
    readings = {"upper": "SPF 1.5", "lower": "SPF 15", "supplement": "SPF 1,5"}
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1830:
            return None
        return [(100, 1800, 400, 1830, readings[role(n)])]
    img, eng = _band_case([box(100, 1800, 400, 1830, "x", render=render)])
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) in (["SPF 1.5"], ["SPF 15"])  # 보완 판독(SPF 1,5)은 섞지 않는다
    w = d["warnings"][0]
    assert w["kind"] == "recognition_unstable" and w["supplement_text"] == "SPF1,5"
    assert w["clean_tile_readings"] == {"0": "SPF1.5", "1": "SPF15"}


def crossed(x, y, who):
    """서로 내용을 포함하지 못하는 세 판독(명백한 충돌): 위 타일 [x, x+60), 아래 타일 [x+40, x+120), 보완 [x, x+60)."""
    if who == "upper":
        return [(x, y, x + 60, y + 30, "보습")]
    if who == "lower":
        return [(x + 40, y, x + 120, y + 30, "습크림")]
    return [(x, y, x + 60, y + 30, "보슴")]


def test_no_bundle_covering_all_readings_fails_and_keeps_diag(monkeypatch, cfg, tmp_path):
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1830:
            return None
        return crossed(100, 1800, role(n))
    img, eng = _band_case([box(100, 1800, 220, 1830, "x", render=render)])
    d = {}
    with pytest.raises(AnalyzeError, match="타일 경계 충돌 미해결") as ei:
        run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert ei.value.code == "OCR_FAILED" and ei.value.source_image_id == 3
    assert len(eng.calls) == len(d["tiles"]) + 1  # 같은 경계 보완은 1회 — 다시 부르지 않는다
    assert d["failed"][0]["reason"] == "no_bundle_covers_all_readings"
    json.dumps(d)  # 실패해도 진단은 직렬화 가능


def test_under_detected_tile_covered_by_another_is_resolved(monkeypatch, cfg, tmp_path):
    """실측 사례(작은 영문 본문): 한 타일은 줄 전체, 다른 타일은 앞부분만 검출, 보완은 줄 전체를 다른 글자로 읽음."""
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1814:
            return None
        return {"upper": [(100, 1800, 290, 1814, "cated in cutaseeus aging")],
                "lower": [(103, 1803, 165, 1810, "catod io")],
                "supplement": [(102, 1802, 290, 1812, "cated in cutesecus aging")]}[role(n)]
    img, eng = _band_case([box(100, 1800, 290, 1814, "x", render=render)])
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["cated in cutaseeus aging"]
    cl = d["supplements"][0]["clusters"][0]
    assert cl["rule"] == "same_place_as_supplement" and cl["use_tile"] == 0 and d["warnings"][0]["kind"] == "recognition_unstable"


def test_flaky_tiny_detection_without_supplement_keeps_covering_tile_bundle(monkeypatch, cfg, tmp_path):
    """실측 사례(7px 아이콘 글자): 두 타일이 거의 같은 자리에서 'A' · 'PA+'를 읽고 보완은 못 찾음 → 덮는 묶음 하나 + 경고."""
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1807:
            return None
        return {"upper": [(412, 1800, 431, 1807, "A")], "lower": [(410, 1800, 429, 1807, "PA+")], "supplement": []}[role(n)]
    img, eng = _band_case([box(405, 1800, 435, 1807, "x", render=render)])
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert len(res.regions) == 1 and texts(res)[0] in ("A", "PA+")
    assert d["warnings"][0]["kind"] == "detection_unstable"


def test_all_readings_truncated_fails(monkeypatch, cfg, tmp_path):
    def render(y0, y1, n):  # 500px 글자가 두 절단선을 모두 넘고, 보완 crop에서도 위 끝에 붙는다
        vy0, vy1 = max(1600, y0), min(2100, y1)
        if vy1 - vy0 < 4:
            return None
        return [(100, vy0, 500, vy1, f"조각{n}")] if role(n) != "supplement" else [(100, y0, 500, vy1, "조각")]
    img, eng = _band_case([box(100, 1600, 500, 2100, "x", render=render)], noise=True)
    d = {}
    with pytest.raises(AnalyzeError):
        run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert d["failed"][0]["reason"] == "no_untruncated_reading"


def test_large_text_crossing_fixed_cut_is_resolved_by_moved_boundary(monkeypatch, cfg, tmp_path):
    # 여백이 없는(잡음) 섹션 → 절단선 2000 고정, 다음 타일 1700 시작. 250px 큰 글자가 2000을 가로지른다.
    boxes = [box(100, 1850, 500, 2100, "큰 제목", partial=lambda t, a, c, n: t[:1])]
    img, eng = _band_case(boxes, noise=True)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert [(t["y0"], t["y1"], t["cut"]) for t in d["tiles"]][:2] == [(0, 2000, "fixed"), (1700, 3700, "fixed")]
    assert texts(res) == ["큰 제목"] and res.regions[0].bbox == BBox(x=100, y=1850, w=400, h=250)
    comp = d["components"][0]
    assert comp["kind"] == "conflict" and comp["upper_text"] == "큰" and comp["lower_text"] == "큰제목"
    assert d["supplements"][0]["status"] == "resolved"


def test_text_taller_than_overlap_truncated_in_both_tiles_is_resolved(monkeypatch, cfg, tmp_path):
    boxes = [box(100, 1600, 500, 2100, "초대형", partial=lambda t, a, c, n: "?")]  # 500px > 겹침 300
    img, eng = _band_case(boxes, noise=True)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["초대형"] and res.regions[0].bbox.y == 1600 and res.regions[0].bbox.y2 == 2100
    assert d["supplements"][0]["crop"][0] <= 1600 - 150 and d["supplements"][0]["crop"][1] >= 2100 + 150


def test_one_side_touching_without_supplement_evidence_is_preserved_with_warning(monkeypatch, cfg, tmp_path):
    def render(y0, y1, n):
        if y0 == 1700:  # 아래 타일(잡음 배경이라 고정 시작 1700)의 위 끝에 붙은 검출 — 위 타일 · 보완 OCR은 못 봄
            return [(50, 1700, 300, 1712, "조각")]
        return []
    boxes = [box(50, 1700, 300, 1712, "조각", render=render)]
    img, eng = _band_case(boxes, noise=True)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert texts(res) == ["조각"] and len(eng.calls) == len(d["tiles"]) + 1
    assert d["warnings"][0]["kind"] == "single_touching"


def test_supplement_replaces_only_matched_components_not_appended(monkeypatch, cfg, tmp_path):
    readings = {"upper": "가격 표기", "lower": "가격 표가", "supplement": "가격 표기"}
    def conflict(y0, y1, n):
        if y0 > 1800 or y1 < 1830:
            return None
        return [(100, 1800, 400, 1830, readings[role(n)])]
    def extra(y0, y1, n):  # 보완 OCR에서만 나타나는 검출 — 단순 추가하지 않는다
        return [(50, 1700, 200, 1730, "보완에만")] if role(n) == "supplement" and y0 <= 1700 and y1 >= 1730 else []
    boxes = [box(100, 1800, 400, 1830, "가격 표기", render=conflict), box(450, 1820, 590, 1840, "옆 단어"),
             box(50, 1700, 200, 1730, "보완에만", render=extra)]
    img, eng = _band_case(boxes)
    d = {}
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert sorted(texts(res)) == ["가격 표기", "옆 단어"]
    sup = d["supplements"][0]
    reasons = {x["text"]: x["reason"] for x in sup["discarded"]}
    assert reasons["보완에만"] == "unmatched" and reasons["옆 단어"] == "matches_settled_only"
    assert len(d["supplements"]) == 1 and len(eng.calls) == len(d["tiles"]) + 1


def test_supplement_calls_are_capped_at_one_per_boundary(monkeypatch, cfg, tmp_path):
    boxes = []
    for k in range(4):  # 경계 하나에 명백한 충돌 4개 → 보완 1회 후 실패(다시 부르지 않는다)
        y = 1720 + 60 * k
        def render(y0, y1, n, y=y, k=k):
            if y0 > y or y1 < y + 30:
                return None
            return crossed(10 + 140 * k, y, role(n))
        boxes.append(box(10 + 140 * k, y, 130 + 140 * k, y + 30, "x", render=render))
    img, eng = _band_case(boxes)
    d = {}
    with pytest.raises(AnalyzeError):
        run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert len(eng.calls) == len(d["tiles"]) + 1 and d["engine_calls"]["supplements"] == 1
    assert len(d["failed"]) == 4


def test_many_boundaries_total_calls_bounded(monkeypatch, cfg, tmp_path):
    h = 12000
    boxes = [box(50, 150 + i * 390, 300, 180 + i * 390, "x") for i in range(30)]
    img = draw(h, boxes)
    n_tiles = len(ocr_tile.plan_tiles(ocr_tile.row_std(img), P(cfg)))
    for i, b in enumerate(boxes):
        def render(y0, y1, n, y=b["y0"], i=i):  # 이웃 타일이 서로 포함하지 못하게 읽음 → 경계마다 보완 1회 후 실패
            if y0 > y or y1 < y + 30:
                return None
            if n >= n_tiles:
                return crossed(50, y, "supplement")
            return crossed(50, y, "upper" if y0 <= y and n % 2 == 0 else "lower")
        b["render"] = render
    eng = LayoutEngine(boxes)
    d = {}
    with pytest.raises(AnalyzeError):
        run_ocr(monkeypatch, cfg, img, eng, tmp_path, d)
    assert len(d["tiles"]) == n_tiles
    assert d["engine_calls"]["tiles"] == n_tiles and 1 <= d["engine_calls"]["supplements"] <= n_tiles - 1
    assert len(eng.calls) <= 2 * n_tiles - 1


def test_engine_error_in_tile_is_ocr_failed(monkeypatch, cfg, tmp_path):
    class Boom(LayoutEngine):
        def __call__(self, crop):
            if self.offset(crop) > 0:
                raise RuntimeError("gpu")
            return super().__call__(crop)
    eng = Boom([])
    with pytest.raises(AnalyzeError, match="타일 y") as ei:
        run_ocr(monkeypatch, cfg, draw(4500, []), eng, tmp_path)
    assert ei.value.code == "OCR_FAILED"


def test_order_follows_paddle_sort_rule(monkeypatch, cfg, tmp_path):
    boxes = [box(400, 3005, 500, 3030, "오른쪽"), box(50, 3000, 150, 3030, "왼쪽"), box(50, 100, 150, 130, "위")]
    img, eng = _band_case(boxes)
    res = run_ocr(monkeypatch, cfg, img, eng, tmp_path)
    assert texts(res) == ["위", "왼쪽", "오른쪽"]


# ---- CLI · analyze() --------------------------------------------------------------------------
def _split_with_long_section(tmp_path, boxes, h=4600):
    img = draw(h, boxes)
    d = tmp_path / "input"
    (d / "sections").mkdir(parents=True)
    p = d / "sections" / "sec_1_01.png"
    Image.fromarray(img[:, :, ::-1]).save(p)
    sec = Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=h, width=W, image_path=str(p))
    jsonio.write_model(d / "split.json", SplitResult(source_image_id=1, source_width=W, source_height=h, sections=[sec]))
    return d / "split.json"


def _spy_loader(monkeypatch, eng):
    orig = ocr.load_section_image
    def spy(sec):
        eng.full = orig(sec)
        return eng.full
    monkeypatch.setattr(ocr, "load_section_image", spy)


def test_cli_tiled_section_writes_debug_and_summary(tmp_path, monkeypatch):
    boxes = [box(50, 1800, 300, 1830, "띠 문구")]
    eng = LayoutEngine(boxes)
    _spy_loader(monkeypatch, eng)
    monkeypatch.setattr(ocr, "build_engine", lambda cfg: eng)
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(_split_with_long_section(tmp_path, boxes)), "--out", str(out)]) == 0
    rec = json.loads((out / "run.json").read_text(encoding="utf-8"))["sections"]["sec_1_01"]
    assert rec["status"] == "ok" and rec["tiles"] == 3 and rec["engine_calls"] == 3 and rec["dropped"] == 1
    dbg = json.loads((out / "ocr_debug" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert dbg["tiled"] and len(dbg["regions"]) == 1
    assert [r.text for r in jsonio.load_ocr(out / "ocr" / "sec_1_01.json").regions] == ["띠 문구"]


def test_cli_unresolved_conflict_exits_2_and_keeps_debug(tmp_path, monkeypatch):
    def render(y0, y1, n):
        if y0 > 1800 or y1 < 1830:
            return None
        return crossed(50, 1800, role(n))
    boxes = [box(50, 1800, 300, 1830, "x", render=render)]
    eng = LayoutEngine(boxes)
    _spy_loader(monkeypatch, eng)
    monkeypatch.setattr(ocr, "build_engine", lambda cfg: eng)
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(_split_with_long_section(tmp_path, boxes)), "--out", str(out)]) == 2
    rec = json.loads((out / "run.json").read_text(encoding="utf-8"))["sections"]["sec_1_01"]
    assert rec["status"] == "failed" and rec["code"] == "OCR_FAILED" and rec["supplements"] == 1
    assert json.loads((out / "ocr_debug" / "sec_1_01.json").read_text(encoding="utf-8"))["failed"]
    assert not (out / "ocr" / "sec_1_01.json").exists()
