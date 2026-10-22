"""조판 폰트 — 승인 Pretendard 역할별 굵기(be-handoff 6.2·6.6 ③, D11).

단위 시험은 DB 없이 돈다. 미리보기·최종 렌더 두 경로 시험은 PIXLATE_TEST_DATABASE_URL 이 있을 때만(tests/exec_db.py).
"""
from __future__ import annotations

import io
import os
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import typeset
from pipeline.handoff import style_defaults as sd
from tests.exec_db import requires_db
from tests.exec_fakes import png_bytes

if os.getenv("PIXLATE_TEST_DATABASE_URL"):  # 두 경로 시험은 하류 통합 시험의 DB·대역 픽스처를 그대로 쓴다
    from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401

FACE = {"title": "SemiBold", "price": "SemiBold", "body": "Regular", "caption": "Regular", "caution": "Regular"}
FILE = {"SemiBold": "Pretendard-SemiBold.otf", "Regular": "Pretendard-Regular.otf"}
SPECIAL = ["–", "—", "é", "®", "×", "→"]


def _defaults():
    return typeset.load_role_defaults()


def _blk(i, style, *, role="body", text="ok", stage="done", source="원문", box=None):
    return typeset.RenderBlock(id=i, role=role, text=text, box=box or {"x": 0, "y": 0, "w": 300, "h": 100}, style=style,
                               style_stage=stage, source_ko=source)


def _measured(px=20.0, align="left"):
    return {"font_color": "#111111", "bg_color": None, "est_font_px": px, "align": align}


@pytest.fixture
def font_calls(monkeypatch):
    """조판이 연 폰트(경로·크기)와 측정·그리기에 넘긴 폰트 객체를 기록한다. 내장 폰트를 부르면 실패시킨다."""
    calls = {"open": [], "measure": [], "draw": []}
    real_truetype, real_len, real_text = ImageFont.truetype, ImageDraw.ImageDraw.textlength, ImageDraw.ImageDraw.text

    def truetype(font=None, size=10.0, *a, **k):
        f = real_truetype(font, size, *a, **k)
        calls["open"].append((str(font), size, f))
        return f

    def textlength(self, text, font=None, *a, **k):
        calls["measure"].append((text, font))
        return real_len(self, text, font=font, *a, **k)

    def draw_text(self, xy, text, fill=None, font=None, *a, **k):
        calls["draw"].append((text, font))
        return real_text(self, xy, text, fill=fill, font=font, *a, **k)

    def no_builtin(*a, **k):
        raise AssertionError("Pillow 내장 폰트를 쓰면 안 된다")

    monkeypatch.setattr(ImageFont, "truetype", truetype)
    monkeypatch.setattr(ImageFont, "load_default", no_builtin)
    monkeypatch.setattr(ImageDraw.ImageDraw, "textlength", textlength)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", draw_text)
    return calls


def test_renderer_version_marks_font_change():
    assert typeset.RENDERER_VERSION.startswith("pillow-typeset/2+pillow-")


@pytest.mark.parametrize("role", list(FACE))
def test_role_opens_approved_pretendard_face(role):
    d = _defaults()
    f = typeset._font(d, role, 20.5)
    face = FACE[role]
    assert f.path.replace("\\", "/").endswith(f"pipeline/data/fonts/pretendard/{FILE[face]}")
    assert f.getname() == ("Pretendard", face)
    assert f.size == 20.5  # 소수 크기 그대로(반올림 없음)
    # 연 파일이 검증된 설정 자료의 그 굵기 파일이다(크기·SHA-256·usWeightClass)
    entry = d.data.data.fonts.faces[face]  # RoleDefaults.data(StyleDefaults).data(설정 파일)
    assert sd.sha256_file(f.path) == entry.sha256
    assert sd.read_font_meta(f.path).weight_class == entry.weight == {"SemiBold": 600, "Regular": 400}[face]


def test_wrap_measure_and_draw_use_one_font_object_per_block(font_calls):
    bg = png_bytes(400, 300)
    blocks = [_blk(1, _measured(18.0), role="title", text="Moisture care for all day long", box={"x": 0, "y": 0, "w": 120, "h": 150}),
              _blk(2, _measured(18.0), role="body", text="Moisture care for all day long", box={"x": 200, "y": 0, "w": 120, "h": 150})]
    res = typeset.render(bg, blocks, _defaults())
    assert res.blocks[1].lines > 1 and res.blocks[2].lines > 1  # 줄바꿈이 실제로 일어남
    opened = [f for _, _, f in font_calls["open"]]
    assert len(opened) == 2  # 블록마다 한 번
    used = {id(f) for _, f in font_calls["measure"] + font_calls["draw"]}
    assert used == {id(f) for f in opened}  # 측정·줄바꿈·그리기 모두 연 객체만 쓴다
    title_font, body_font = opened
    assert title_font.getname() == ("Pretendard", "SemiBold") and body_font.getname() == ("Pretendard", "Regular")
    n1 = res.blocks[1].lines
    assert len(font_calls["draw"]) == n1 + res.blocks[2].lines
    assert all(f is title_font for _, f in font_calls["draw"][:n1]) and all(f is body_font for _, f in font_calls["draw"][n1:])


def test_special_characters_and_hangul_have_real_glyphs():
    d = _defaults()
    for role in ("title", "body"):
        f = typeset._font(d, role, 40.0)
        notdef = f.getmask("\U000f0000")  # 보조 사설 영역 — Pretendard 에 없는 글자(.notdef 상자)
        notdef_px = (notdef.size, bytes(notdef))
        for ch in SPECIAL + ["가", "힣", "한", "글"]:
            m = f.getmask(ch)
            assert m.getbbox() is not None, (role, ch)  # 잉크가 있다
            assert (m.size, bytes(m)) != notdef_px, (role, ch)  # .notdef 상자가 아니다


def test_special_characters_and_hangul_render_into_block(font_calls):
    bg = png_bytes(600, 200, color=(255, 255, 255))
    text = "Gentle care – 50% — café ® 2 × 3 → 한글 미리보기"
    res = typeset.render(bg, [_blk(1, _measured(28.0), role="body", text=text, box={"x": 10, "y": 10, "w": 580, "h": 180})], _defaults())
    assert res.blocks[1].overflow is False
    assert "".join(t for t, _ in font_calls["draw"]).replace(" ", "") == text.replace(" ", "")  # 글자를 빼거나 바꾸지 않는다
    with Image.open(io.BytesIO(res.png)) as im:
        assert im.convert("L").getextrema()[0] < 128  # 실제로 어두운 글자 픽셀이 찍혔다


def test_measured_values_kept_with_fractional_size(font_calls):
    bg = png_bytes(1000, 400)
    res = typeset.render(bg, [_blk(1, {"font_color": "#424B55", "bg_color": "#FEFFFF", "est_font_px": 37.25, "align": "right"},
                                   role="price")], _defaults())
    assert res.blocks[1].applied == {"font_color": "#424B55", "est_font_px": 37.25, "align": "right"}
    assert set(res.blocks[1].source.values()) == {"measured"}
    (path, size, f), = font_calls["open"]
    assert size == 37.25 and f.size == 37.25  # 측정 크기 그대로 — 원본 폭 배율·em_ratio 를 다시 곱하지 않는다
    assert f.getname() == ("Pretendard", "SemiBold")


def test_null_fields_and_failed_style_use_defaults_with_role_face(font_calls):
    bg = png_bytes(1000, 400)
    res = typeset.render(bg, [_blk(1, {"font_color": None, "bg_color": None, "est_font_px": 30.5, "align": None}, role="caption"),
                              _blk(2, None, role="title", stage="failed")], _defaults())
    assert res.blocks[1].applied == {"font_color": "#222222", "est_font_px": 30.5, "align": "left"}
    assert res.blocks[1].source == {"font_color": "role_default", "est_font_px": "measured", "align": "role_default"}
    assert res.blocks[2].applied["est_font_px"] == 32 * 1000 / 375
    sizes = {f.getname()[1]: s for _, s, f in font_calls["open"]}
    assert sizes == {"Regular": 30.5, "SemiBold": 32 * 1000 / 375}  # 기본 크기도 반올림 없이


@pytest.mark.parametrize("text,source", [("", "원문"), ("   ", "원문"), ("ok", ""), ("ok", "  ")])
def test_blank_translation_or_source_is_not_typeset(font_calls, text, source):
    res = typeset.render(png_bytes(300, 200), [_blk(1, _measured(), text=text, source=source)], _defaults())
    assert res.blocks == {} and font_calls["open"] == [] and font_calls["draw"] == []


def test_font_mismatch_stops_with_existing_error(monkeypatch, tmp_path):
    # 자료·라이선스는 정본과 같고 SemiBold 파일만 다른 바이트 → 검증 실패로 중단(내장·다른 굵기 폰트로 넘어가지 않는다)
    src = sd.STYLE_DEFAULTS_PATH.parent
    shutil.copy(sd.STYLE_DEFAULTS_PATH, tmp_path / "style_defaults.json")
    shutil.copytree(src / "fonts" / "pretendard", tmp_path / "fonts" / "pretendard")
    shutil.copy(tmp_path / "fonts" / "pretendard" / FILE["Regular"], tmp_path / "fonts" / "pretendard" / FILE["SemiBold"])
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", str(tmp_path / "style_defaults.json"))
    with pytest.raises(typeset.StyleDefaultsUnavailable) as e:
        typeset.load_role_defaults()
    assert e.value.code == "STYLE_DEFAULTS_UNAPPROVED" and "SemiBold" in str(e.value)
    # 같은 사본이 정본과 일치하면 통과하고 사본 위치의 폰트를 연다
    shutil.copy(src / "fonts" / "pretendard" / FILE["SemiBold"], tmp_path / "fonts" / "pretendard" / FILE["SemiBold"])
    f = typeset._font(typeset.load_role_defaults(), "title", 20)
    assert f.path == str(tmp_path / "fonts" / "pretendard" / FILE["SemiBold"])


def test_font_file_gone_after_verification_stops_with_existing_error(monkeypatch, tmp_path):
    d = _defaults()
    monkeypatch.setattr(sd, "font_path", lambda data, face: tmp_path / "gone.otf")
    with pytest.raises(typeset.StyleDefaultsUnavailable):
        typeset.render(png_bytes(300, 200), [_blk(1, _measured())], d)


# ---------------------------------------------------------------------------------------------------------
# 미리보기·최종 렌더 두 경로(실제 PostgreSQL) — 같은 조판 함수·같은 폰트·같은 렌더러 버전
# ---------------------------------------------------------------------------------------------------------
@requires_db
def test_preview_and_final_render_use_pretendard(env, font_calls):
    ids = _to_n3(env)
    c = ids["c"]
    assert c.post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    preview_fonts = {f.getname() for _, _, f in font_calls["open"]}
    pv = _q("SELECT a.status, a.input_manifest, h.impl_version FROM job_async_task a JOIN task_handoff h ON h.task_id = a.id "
            "AND h.state = 'adopted' WHERE a.job_id = :j AND a.stage = 'preview' AND a.is_current", j=ids["job"])
    assert pv and all(p["status"] == "done" for p in pv)
    assert all(p["input_manifest"]["renderer"] == p["impl_version"] == typeset.RENDERER_VERSION for p in pv)
    assert preview_fonts and preview_fonts <= {("Pretendard", "Regular"), ("Pretendard", "SemiBold")}

    font_calls["open"].clear()
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    fin = _q("SELECT a.status, a.input_manifest, h.impl_version FROM job_async_task a JOIN task_handoff h ON h.task_id = a.id "
             "AND h.state = 'adopted' WHERE a.job_id = :j AND a.stage = 'final_render' AND a.is_current", j=ids["job"])
    assert [f["status"] for f in fin] == ["done"]
    assert fin[0]["input_manifest"]["renderer"] == fin[0]["impl_version"] == typeset.RENDERER_VERSION
    final_fonts = {f.getname() for _, _, f in font_calls["open"]}
    assert final_fonts == preview_fonts  # 최종 렌더가 미리보기와 같은 역할별 폰트를 쓴다
