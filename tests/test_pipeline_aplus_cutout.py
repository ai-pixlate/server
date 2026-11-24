"""A+ 이미지 칸 누끼 — 자를 자리 넓히기 · 알파 정리 · 다시 자르기 · 사진별 실패 · 원천 선택(가짜 전경 분리 모델, 실제 rembg 없음).

개발용 v1(open-questions.md #98): 누끼할 사진은 7칸 구성이 정한다 · 글자를 지운 섹션에서 넓혀 자른다(없으면 원본 + 확인 표시) ·
가장자리를 줄이고 작은 조각을 지운 뒤 대상에 맞춰 다시 자른다 · 실패는 그 사진만. 기대값은 합성 그림에서 손으로 계산했다.
"""
import numpy as np
import pytest
from PIL import Image

from pipeline import config as cfgmod
from pipeline.aplus import cutout
from pipeline.types import AplusCutoutResult, AplusImage, AplusItem, AplusModule, AplusPlan, AplusPlanChecked, AplusSlotPlan, BBox, Section

W, H = 400, 600


class DarkIsSubject:
    """가짜 모델: 어두운 픽셀(R < 128)을 대상으로 본다."""

    config = {"adapter": "fake"}

    def __init__(self, fail_on: tuple[int, int] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.fail_on = fail_on

    def __call__(self, image: Image.Image) -> Image.Image:
        self.calls.append(image.size)
        if image.size == self.fail_on:
            raise RuntimeError("모델 오류")
        r = np.asarray(image.convert("RGB"))[..., 0]
        return Image.fromarray(np.where(r < 128, 255, 0).astype(np.uint8))


def cfg(**over):
    c = cfgmod.load_config()
    c["aplus_cutout"].update(over)
    return c


def section(tmp_path, key: str, paint) -> Section:
    a = np.full((H, W, 3), 255, np.uint8)
    paint(a)
    p = tmp_path / f"{key}.png"
    Image.fromarray(a).save(p)
    return Section(section_key=key, source_image_id=1, section_order=int(key[-2:]), top_offset=0, height=H, width=W, image_path=str(p))


def image(key: str, box: tuple[int, int, int, int], *, cut: bool = True, fit: str = "contain") -> AplusImage:
    return AplusImage(section_key=key, bbox=BBox(x=box[0], y=box[1], w=box[2], h=box[3]), fit=fit, cutout=cut)


def plan_of(*mods: tuple[int, AplusModule]) -> AplusPlan:
    by = dict(mods)
    slots = [AplusSlotPlan(slot=n, template=by[n].template if n in by else None, why="", module=by.get(n)) for n in range(1, 8)]
    return AplusPlan(status="ok", slots=slots, checked=AplusPlanChecked(input_fingerprint="f", spec_version="v", llm_called=True))


def test_default_config_is_valid_and_bad_values_are_rejected():
    c = cutout.validate_config(cfgmod.load_config())
    assert c["model"] == "birefnet-general" and cfgmod.flatten(cfgmod.load_config())["aplus_cutout.margin_ratio"] == 0.12
    for key, bad in (("model", ""), ("margin_ratio", 1.5), ("alpha_threshold", 0), ("alpha_threshold", 128.0), ("edge_shrink_px", True),
                     ("min_fragment_ratio", -0.1), ("pad_ratio", "0.04"), ("edge_touch_ratio", 2)):
        with pytest.raises(ValueError, match=f"aplus_cutout.{key}"):
            cutout.validate_config(cfg(**{key: bad}))
    broken = cfgmod.load_config()
    del broken["aplus_cutout"]["pad_ratio"]
    with pytest.raises(ValueError, match="없는 키 aplus_cutout.pad_ratio"):
        cutout.validate_config(broken)


def test_widen_adds_margin_on_each_side_and_stays_inside_section():
    assert cutout.widen(BBox(x=100, y=200, w=100, h=200), 0.12, W, H).model_dump() == {"x": 88, "y": 176, "w": 124, "h": 248}
    assert cutout.widen(BBox(x=0, y=500, w=390, h=100), 0.12, W, H).model_dump() == {"x": 0, "y": 488, "w": 400, "h": 112}  # 섹션 밖으로 안 나감


def test_remove_fragments_keeps_large_and_diagonally_joined_parts():
    m = np.zeros((20, 20), bool)
    m[2:10, 2:10] = True      # 큰 덩어리 64
    m[10, 10] = True          # 대각선으로 닿은 한 점 → 같은 덩어리
    m[15:17, 15:17] = True    # 떨어진 조각 4
    m[0, 19] = True           # 떨어진 조각 1
    kept, n_kept, n_removed = cutout.remove_fragments(m, 0.01)  # 넓이 400의 1% = 4 이상을 남긴다
    assert (n_kept, n_removed) == (2, 1) and kept[10, 10] and kept[15, 15] and not kept[0, 19] and kept.sum() == 69
    kept, n_kept, n_removed = cutout.remove_fragments(m, 0.05)  # 20 이상만
    assert (n_kept, n_removed) == (1, 2) and kept.sum() == 65
    tiny = np.zeros((20, 20), bool)
    tiny[5, 5] = True
    assert cutout.remove_fragments(tiny, 0.5)[1:] == (1, 0)  # 가장 큰 덩어리는 작아도 남긴다
    empty, n_kept, n_removed = cutout.remove_fragments(np.zeros((4, 4), bool), 0.1)
    assert not empty.any() and (n_kept, n_removed) == (0, 0)


def test_shrink_edge_and_clean_alpha():
    a = np.zeros((30, 30), np.uint8)
    a[5:25, 5:25] = 255
    a[0:2, 0:2] = 255  # 모서리의 작은 조각
    s = cutout.shrink_edge(a, 1)
    assert s[6:24, 6:24].min() == 255 and s[5, 5:25].max() == 0 and s.sum() == 18 * 18 * 255 + 255  # 큰 사각형은 한 픽셀씩 줄고, 모서리 조각(2×2)은 안쪽 두 변만 줄어 한 점
    out, stats = cutout.clean_alpha(a, threshold=128, edge_shrink_px=1, min_fragment_ratio=0.01)
    assert stats == {"components_kept": 1, "components_removed": 1, "foreground_ratio": 0.36} and cutout.subject_box(out) == (6, 6, 24, 24)
    out0, stats0 = cutout.clean_alpha(a, threshold=128, edge_shrink_px=0, min_fragment_ratio=0)  # 둘 다 끔
    assert stats0["components_removed"] == 0 and cutout.subject_box(out0) == (0, 0, 25, 25)
    assert cutout.clean_alpha(np.zeros((8, 8), np.uint8), threshold=128, edge_shrink_px=1, min_fragment_ratio=0.01)[1]["components_kept"] == 0


def test_run_cuts_requested_photos_only_and_reports_boxes(tmp_path):
    def paint(a):
        a[200:400, 150:250] = 20   # 제품 100×200
        a[179:184, 138:143] = 0    # 넓힌 자리 안의 떨어진 조각 5×5(줄이면 3×3 — 조각 넓이의 0.3%보다 작다)

    s1 = section(tmp_path, "sec_1_01", paint)
    s2 = section(tmp_path, "sec_1_02", lambda a: None)
    erased = tmp_path / "erased.png"
    Image.open(s1.image_path).save(erased)
    p = plan_of((1, AplusModule(template="1A", image=image("sec_1_01", (140, 190, 120, 220)))),
                (2, AplusModule(template="2A", image=image("sec_1_02", (0, 0, 400, 600), cut=False, fit="cover"))))
    fake, recs = DarkIsSubject(), []
    res = cutout.run(p, [s1, s2], {"sec_1_01": erased}, tmp_path / "out", cfg(), matting=fake, recorder=recs.append)
    assert len(res.items) == 1 and fake.calls == [(148, 272)]  # 120×220을 12%씩 넓힌 조각 한 번만
    x = res.items[0]
    assert (x.slot, x.field, x.status, x.source, x.flags) == (1, "image", "ok", "erased", [])
    assert x.crop_bbox.model_dump() == {"x": 126, "y": 164, "w": 148, "h": 272}
    assert x.subject_bbox.model_dump() == {"x": 151, "y": 201, "w": 98, "h": 198}  # 가장자리 1픽셀씩 줄임, 떨어진 조각은 지움
    png = Image.open(x.image_path)
    assert png.mode == "RGBA" and png.size == (x.width, x.height) == (98 + 2 * 8, 198 + 2 * 8)  # 여백 = round(198 × 0.04) = 8
    a = np.asarray(png)
    assert a[0, 0, 3] == 0 and a[8, 8, 3] == 255 and tuple(a[100, 50, :3]) == (20, 20, 20) and a[..., 3].min() == 0
    assert recs[0]["matting"] == {"adapter": "fake"} and recs[0]["items"][0]["components_removed"] == 1
    assert AplusCutoutResult.model_validate_json(res.model_dump_json()) == res


def test_run_falls_back_to_original_marks_touching_and_keeps_going_after_failure(tmp_path):
    s1 = section(tmp_path, "sec_1_01", lambda a: a.__setitem__((slice(100, 300), slice(0, 100)), 20))  # 왼쪽 끝에 붙은 제품
    s2 = section(tmp_path, "sec_1_02", lambda a: None)                                                # 대상 없음
    s3 = section(tmp_path, "sec_1_03", lambda a: a.__setitem__((slice(50, 150), slice(50, 150)), 20))
    m5 = AplusModule(template="5B", items=[AplusItem(image=image("sec_1_01", (0, 80, 150, 240))),
                                           AplusItem(image=image("sec_1_02", (100, 100, 100, 100))),
                                           AplusItem(image=image("sec_1_03", (40, 40, 200, 120))),
                                           AplusItem(image=image("sec_1_01", (0, 80, 150, 240)))])
    fake = DarkIsSubject(fail_on=(248, 148))  # 세 번째 사진의 넓힌 조각 크기
    res = cutout.run(plan_of((5, m5)), [s1, s2, s3], {}, tmp_path / "out", cfg(), matting=fake)
    a, b, c, d = res.items
    assert [x.field for x in res.items] == ["items[0].image", "items[1].image", "items[2].image", "items[3].image"]
    assert a.status == "ok" and a.source == "original" and a.flags[0].startswith("[확인] 글자를 지운 섹션 이미지가 없어")
    assert a.flags[1] == "[확인] 대상이 자른 자리의 왼쪽 변에 닿음 — 잘렸을 수 있음"
    assert b.status == "failed" and "남은 대상이 없다" in b.error and b.image_path is None
    assert c.status == "failed" and "모델 오류" in c.error
    assert d.status == "ok" and d.image_path == a.image_path and len(fake.calls) == 3  # 같은 자리는 다시 누끼하지 않는다
    assert sorted(f.name for f in (tmp_path / "out").iterdir()) == ["slot5_items0.png"]


def test_run_without_requests_never_builds_model_and_input_errors_raise(tmp_path, monkeypatch):
    s1 = section(tmp_path, "sec_1_01", lambda a: None)
    none = plan_of((1, AplusModule(template="1A", image=image("sec_1_01", (0, 0, 100, 100), cut=False))))

    def boom(model):
        raise AssertionError("모델을 만들면 안 된다")

    monkeypatch.setattr(cutout, "RembgMatting", boom)
    res = cutout.run(none, [s1], {}, tmp_path / "out", cfg())
    assert res.items == [] and res.matting_config is None and not (tmp_path / "out").exists()
    want = plan_of((1, AplusModule(template="1A", image=image("sec_1_01", (0, 0, 100, 100)))))
    with pytest.raises(ValueError, match="sections에 없다"):
        cutout.run(want, [], {}, tmp_path / "out", cfg(), matting=DarkIsSubject())
    small = tmp_path / "small.png"
    Image.new("RGB", (10, 10)).save(small)
    with pytest.raises(ValueError, match="섹션 크기"):
        cutout.run(want, [s1], {"sec_1_01": small}, tmp_path / "out", cfg(), matting=DarkIsSubject())
    (tmp_path / "out").mkdir(exist_ok=True)
    (tmp_path / "out" / "slot1_image.png").write_bytes(b"x")
    with pytest.raises(ValueError, match="이미 있다"):
        cutout.run(want, [s1], {}, tmp_path / "out", cfg(), matting=DarkIsSubject())
    failed = AplusPlan(status="failed", slots=None, error="e", checked=AplusPlanChecked(input_fingerprint="f", spec_version="v", llm_called=True))
    with pytest.raises(ValueError, match="실패 상태"):
        cutout.run(failed, [s1], {}, tmp_path / "out2", cfg(), matting=DarkIsSubject())


def test_cutout_request_requires_contain_fit():
    with pytest.raises(ValueError, match="fit=contain"):
        image("sec_1_01", (0, 0, 10, 10), cut=True, fit="cover")
