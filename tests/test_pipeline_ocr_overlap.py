"""② 호출별 명백한 중복 제거(#40) · 겹친 검출 재인식(#32)의 판정 함수 — 실제 모델 없이 결정적으로 확인한다.

타일 흐름(재인식 교체 → 대응 재계산 → 보완 OCR → 최종 판정)과의 연결은 tests/test_pipeline_ocr_tile.py "겹친 검출 재인식" 절.
여기 사례는 합성이다 — 실제 #40 효과 · 실제 원본의 교정 여부는 docs/ai-experiments 기록을 따른다.
"""
import numpy as np
import pytest

from pipeline import config as cfgmod
from pipeline.stages import ocr, ocr_overlap, ocr_tile
from pipeline.types import BBox, OcrRegion


@pytest.fixture
def cfg():
    return cfgmod.load_config()


@pytest.fixture
def ov(cfg):
    return ocr_overlap.params_of(cfg)


def reg(text, poly, score=0.9, key="reg_0001"):
    poly = [tuple(p) for p in poly]
    return OcrRegion(region_key=key, text=text, score=score, poly=poly, bbox=BBox.from_poly(poly))


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


# ---- 설정 ---------------------------------------------------------------------------------
def test_default_overlap_config_is_valid_and_validate_config_checks_it(cfg):
    p = ocr_overlap.params_of(cfg)
    assert p.rec_max_calls >= 0 and p.rec_max_group >= 2
    ocr.validate_config(cfg)
    bad = cfgmod.load_config()
    bad["ocr"]["overlap_rec_max_group"] = 1
    with pytest.raises(ValueError, match="overlap_rec_max_group"):
        ocr.validate_config(bad)
    missing = cfgmod.load_config()
    del missing["ocr"]["dedup_min_poly_iou"]
    with pytest.raises(ValueError, match="키 없음"):
        ocr.validate_config(missing)


# ---- 1. 명백한 중복 제거 -----------------------------------------------------------------------
def test_same_text_same_polygon_keeps_first_unchanged(ov):
    a = reg("보습크림", rect(10, 10, 110, 40), score=0.71)
    b = reg("보습크림", rect(10, 10, 110, 40), score=0.99)  # 점수가 높아도 뒤 검출이 지워진다(점수로 고르지 않음)
    kept, recs = ocr_overlap.dedup([a, b], ov)
    assert kept == [0]
    assert [(r["action"], r["order"], r["representative"]) for r in recs] == [("removed", 1, 0)]
    assert recs[0]["text"] == "보습크림" and recs[0]["score"] == 0.99 and recs[0]["poly"] == [list(q) for q in b.poly]
    # 대표는 값을 바꾸지 않는다(평균 · 합성 없음)
    assert (a.text, a.score, a.poly) == ("보습크림", 0.71, rect(10, 10, 110, 40))


def test_nearly_same_polygon_is_obvious_duplicate(ov):
    a = reg("SPF50+", rect(10, 10, 110, 40))
    b = reg("SPF50+", [(11, 10), (111, 11), (110, 41), (10, 40)])  # 1px 흔들림
    kept, recs = ocr_overlap.dedup([a, b], ov)
    assert kept == [0] and recs[0]["poly_iou"] >= ov.dedup_min_poly_iou


@pytest.mark.parametrize("second", [
    reg("보습크림", rect(150, 10, 250, 40)),  # 다른 위치에 반복된 같은 문구(가까이 붙음)
    reg("보습크림!", rect(10, 10, 110, 40)),  # 텍스트가 다름
    reg("보습", rect(10, 10, 110, 40)),  # 일부 문구 포함 관계
    reg("보습 크림", rect(10, 10, 110, 40)),  # 내부 공백만 다름 — 정규화하지 않는다
])
def test_not_obvious_duplicates_are_kept(ov, second):
    kept, recs = ocr_overlap.dedup([reg("보습크림", rect(10, 10, 110, 40)), second], ov)
    assert kept == [0, 1] and not [r for r in recs if r["action"] == "removed"]


def test_empty_text_is_never_removed(ov):
    kept, recs = ocr_overlap.dedup([reg("", rect(10, 10, 110, 40)), reg("", rect(10, 10, 110, 40))], ov)
    assert kept == [0, 1] and recs == []


def test_bbox_overlap_with_different_polygon_is_not_removed(ov):
    # 같은 bbox [0, 0, 100, 30]이지만 서로 엇갈린 평행사변형 — 실제 polygon은 거의 겹치지 않는다
    a = reg("가나다", [(0, 0), (20, 0), (100, 30), (80, 30)])
    b = reg("가나다", [(80, 0), (100, 0), (20, 30), (0, 30)])
    assert a.bbox == b.bbox
    kept, recs = ocr_overlap.dedup([a, b], ov)
    assert kept == [0, 1] and not recs


def test_chain_overlap_compares_each_with_representative_directly(ov):
    # 폭 40 박스: 2px 이동은 IoU 0.905(중복), 4px 이동은 0.818(별개). A ↔ B 중복, B ↔ C 중복이지만 A ↔ C는 별개다.
    a, b, c = (reg("문구", rect(x, 0, x + 40, 30)) for x in (0, 2, 4))
    kept, recs = ocr_overlap.dedup([a, b, c], ov)
    assert kept == [0, 2]
    assert [(r["action"], r["order"], r.get("representative")) for r in recs] == [("removed", 1, 0)]


def test_candidate_of_two_representatives_is_kept(ov):
    a, c, d = (reg("문구", rect(x, 0, x + 40, 30)) for x in (0, 4, 2))  # d는 a · c 모두와 중복 후보
    kept, recs = ocr_overlap.dedup([a, c, d], ov)
    assert kept == [0, 1, 2] and recs == [{**recs[0], "action": "kept", "reason": "multiple_representatives"}]


def test_tangled_with_other_text_is_kept(ov):
    a = reg("문구", rect(0, 0, 100, 30))
    other = reg("문구2", rect(0, 0, 100, 30))
    b = reg("문구", rect(0, 0, 100, 30))
    kept, recs = ocr_overlap.dedup([a, other, b], ov)
    assert kept == [0, 1, 2] and recs[0]["reason"] == "tangled_with_other_text"


def test_nonconvex_polygon_is_not_judged(ov):
    concave = [(0, 0), (100, 0), (100, 30), (50, 10), (0, 30)]
    kept, recs = ocr_overlap.dedup([reg("문구", concave), reg("문구", concave)], ov)
    assert kept == [0, 1] and recs[0]["reason"] == "polygon_not_comparable"


# ---- 2. geometry · crop -------------------------------------------------------------------
def test_min_area_rect_orders_corners_like_paddle_and_reports_angle():
    r = ocr_overlap.min_area_rect(rect(100, 1800, 160, 1830) + rect(150, 1795, 260, 1830))
    assert np.allclose(r.box, [[100, 1795], [260, 1795], [260, 1830], [100, 1830]]) and abs(r.angle) < 1e-9
    tilted = ocr_overlap.min_area_rect([(0, 0), (100, 10), (97, 40), (-3, 30)])
    assert 5 < tilted.angle < 6 and tilted.width > tilted.height


def test_build_crop_uses_roi_and_is_identity_for_axis_aligned_box():
    pytest.importorskip("cv2")
    img = (np.arange(60 * 80 * 3, dtype=np.uint32).reshape(60, 80, 3) % 251).astype(np.uint8)
    box = np.array(rect(10, 5, 50, 25), dtype=np.float64)
    crop, info = ocr_overlap.build_crop(img, box)
    assert info["roi"] == [10, 5, 51, 26] and info["size"] == [40, 20] and not info["rotated_90"]
    assert np.array_equal(crop, img[5:25, 10:50])


def test_crop_key_identifies_the_same_crop():
    a = np.array(rect(100, 1800, 260, 1830), dtype=float)
    assert ocr_overlap.crop_key(a) == ocr_overlap.crop_key(a + 0.2)
    assert ocr_overlap.crop_key(a) != ocr_overlap.crop_key(a + np.array([2, 1]))


# ---- 3. 재인식 판독 변화 설명 -----------------------------------------------------------------
@pytest.mark.parametrize("pieces, reread, removed", [
    (["9.99", "%개선"], "9.9%개선", [{"junction": 0, "where": "end_of_left", "chars": "9"}]),  # 이웃 % 조각 → 추가 9
    (["피부에 터치되며", "1뭉침없이 발림"], "피부에 터치되며 뭉침없이 발림",
     [{"junction": 0, "where": "start_of_right", "chars": "1"}]),  # 앞 글자 조각 → 가짜 1
    (["**7", "징크옥사이드"], "** 징크옥사이드", [{"junction": 0, "where": "end_of_left", "chars": "7"}]),  # 별표 유지
])
def test_fragment_digit_at_contact_is_explained(pieces, reread, removed):
    ex = ocr_overlap.explain_change(pieces, [True], reread, 1)
    assert ex["result"] == "fragment_digits" and ex["removed"] == removed


@pytest.mark.parametrize("pieces, reread", [
    (["9.99", "%개선"], "9.99%개선"),  # 진짜 9.99
    (["1일", "7회 사용"], "1일 7회 사용"),  # 진짜 1 · 7
    (["19.349°", "기울기"], "19.349° 기울기"),
])
def test_real_digits_unchanged(pieces, reread):
    assert ocr_overlap.explain_change(pieces, [True], reread, 1) == {"result": "unchanged"}


@pytest.mark.parametrize("pieces, reread, reason", [
    (["**7", "징크옥사이드"], "*징크옥사이드", "lost_symbol_or_unit"),  # 별표 하나 손실
    (["9.99", "%개선"], "9.9개선", "lost_symbol_or_unit"),  # 단위 손실
    (["+3", "℃ 상승"], "3℃ 상승", "lost_symbol_or_unit"),  # 부호 손실
    (["9.9", "%개선"], "9.99%개선", "new_digit_or_symbol"),  # 새 숫자
    (["50", "100"], "50100", "numbers_split_or_merged"),  # 독립 숫자가 붙음
    (["50", "50"], "50", "digit_change_not_at_contact"),  # 50 50 → 50 (토큰 통째 손실)
    (["1회 사용", "7일"], "회 사용7일", "digit_change_not_at_contact"),  # 접촉 부분이 아닌 진짜 1 손실
    (["9.99", "%개션"], "9.9%개선", "text_changed"),  # 다른 문자도 바뀜
])
def test_unexplained_changes_are_rejected(pieces, reread, reason):
    ex = ocr_overlap.explain_change(pieces, [True], reread, 1)
    assert ex["result"] == "unexplained" and ex["diff"]["reason"] == reason


def test_single_piece_with_spaces_keeps_number_boundaries():
    assert ocr_overlap.explain_change(["50 100"], [], "50100", 1)["result"] == "unexplained"
    assert ocr_overlap.explain_change(["50 50"], [], "50", 1)["result"] == "unexplained"


def test_no_contact_means_no_explanation():
    ex = ocr_overlap.explain_change(["9.99", "%개선"], [False], "9.9%개선", 1)
    assert ex["result"] == "unexplained"


def test_whole_detection_vanishing_is_not_explained():
    ex = ocr_overlap.explain_change(["보습", "1", "크림"], [True, True], "보습크림", 1)
    assert ex["result"] == "unexplained"


# ---- 4. 묶음 후보 제외(OverlapRec) --------------------------------------------------------------
def det(i, x0, y0, x1, y1, text, crop=0, origin="tile", **kw):
    poly = rect(x0, y0, x1, y1)
    return ocr_tile.Det(id=i, origin=origin, crop=crop, order=i, text=text, score=0.9, poly=poly, bbox=BBox.from_poly(poly),
                        touch_top=kw.get("touch_top", False), touch_bottom=False)


@pytest.fixture
def rr(cfg, monkeypatch):
    calls = []

    def fake_build(image, box):
        calls.append(box)
        w, h = ocr_overlap.crop_size(box)
        return np.zeros((h, w, 3), np.uint8), {"box": box.tolist(), "roi": [0, 0, 1, 1], "roi_clamped": False,
                                               "size": [w, h], "rotated_90": False}

    monkeypatch.setattr(ocr_overlap, "build_crop", fake_build)
    r = ocr_tile.OverlapRec(np.zeros((4600, 600, 3), np.uint8), ocr_tile.params_of(cfg), ocr_overlap.params_of(cfg),
                            lambda crop: ("9.9%개선", 0.95), ocr_tile._Ids())
    r.ids.take(100)
    r.box_calls = calls
    return r


def reconcile(rr, upper, lower, pool_upper=None):
    sides = [ocr_tile._Side(("tile", 0), upper, (0, 1999)), ocr_tile._Side(("tile", 1), lower, (1649, 3648))]
    unit = {}
    out = rr.reconcile(unit, sides, {("tile", 0): pool_upper or upper, ("tile", 1): lower}, expand=True)
    return unit, sides, out


LOWER = [det(9, 102, 1801, 258, 1829, "9.9%개선", crop=1)]


@pytest.mark.parametrize("upper, extra, reason", [
    ([det(0, 100, 1800, 160, 1830, "9.99"), det(1, 150, 1800, 260, 1830, "%개선"), det(2, 100, 1900, 160, 1930, "다음 줄")],
     [], "multiple_rows"),
    ([det(0, 100, 1800, 200, 1830, "9.99%"), det(1, 110, 1800, 190, 1830, "개선")], [], "complex_overlap"),  # 포함에 가까움
    ([det(0, 100, 1800, 160, 1830, "9.99"), det(1, 150, 1800, 260, 1830, "%개선")],
     [det(3, 120, 1815, 200, 1845, "다른 열")], "foreign_detection_in_crop"),  # crop에 다른 검출이 섞임
    ([det(0, 100, 1800, 160, 1830, "9.99", touch_top=True), det(1, 150, 1800, 260, 1830, "%개선")], [], "truncated_by_cut"),
])
def test_ambiguous_groups_are_excluded_without_model_calls(rr, upper, extra, reason):
    unit, sides, out = reconcile(rr, upper, LOWER, pool_upper=upper + extra)
    assert out == [] and sides[0].rec["excluded"] == reason
    assert unit["decision"] == "skipped" and rr.calls == 0 and rr.box_calls == []


def test_partial_overlap_neighbor_from_same_call_is_added(rr):
    seed = [det(0, 100, 1800, 160, 1830, "9.99")]
    neighbor = det(1, 150, 1800, 260, 1830, "%개선")
    unit, sides, out = reconcile(rr, seed, LOWER, pool_upper=seed + [neighbor])
    assert sides[0].rec["expanded"] == [1] and sides[0].contacts == [True]
    assert [s.call for s, _ in out] == [("tile", 0)] and out[0][1].rerec_of == [0, 1]
    assert out[0][1].text == "9.9%개선" and out[0][1].score == 0.95 and out[0][1].poly == rect(100, 1800, 260, 1830)
