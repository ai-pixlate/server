"""넘침 판정 = 글리프 잉크 기준(contract.md 6.2 · 조판 D-01 · D-02의 서버 Pillow 근사, #89).

판정만 바뀌고 그림(줄바꿈 · 크기 · 행간 · 정렬 · 위치)은 그대로다. 단위 시험 — DB 없음.
기대값은 시험 안에서 같은 폰트 객체로 잉크 상자를 다시 계산해 정한다(폰트 파일 · Pillow 버전이 바뀌어도 규칙만 본다).
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app import typeset
from tests.exec_fakes import png_bytes

PX = 20.0


def _defaults():
    return typeset.load_role_defaults()


def _style(align="left", px=PX):
    return {"font_color": "#111111", "bg_color": None, "est_font_px": px, "align": align}


def _blk(i, text, box, *, align="left", px=PX, scale=1.0, role="body"):
    return typeset.RenderBlock(id=i, role=role, text=text, box=box, style=_style(align, px), style_stage="done",
                               source_ko="원문", font_scale=scale)


def _render(text, box, *, size=(600, 400), **k):
    return typeset.render(png_bytes(*size), [_blk(1, text, box, **k)], _defaults()).blocks[1]


def _ink(text, x, y, *, px=PX, role="body"):
    """한 줄을 (x, y)에 그렸을 때의 잉크 상자 — 조판과 같은 폰트 · 같은 기준점."""
    font = typeset._font(_defaults(), role, px)
    return ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((x, y), text, font=font)


def _line_box_rule(lines, px, h):  # 이전(렌더러 /2) 높이 판정 — 비교 설명용
    return lines * px * typeset.LINE_SPACING > h


# ── 줄 상자는 넘지만 잉크는 영역 안: 넘침 아님(이전 판정의 오탐) ──────────────────────────────────────

def test_single_line_inside_ink_is_not_overflow_even_if_line_box_exceeds():
    """dev 경고만 35 의 대표 모양: 글자 크기 = 영역 높이 × 0.9 → 줄 상자 1.08배."""
    h = PX / 0.9
    r = _render("DAILY CARE", {"x": 10, "y": 10, "w": 300, "h": h})  # 하강부 없는 글자(하강부는 아래 시험)
    assert r.lines == 1 and _line_box_rule(1, PX, h)  # 이전 규칙이면 true
    assert r.overflow is False


def test_multi_line_inside_ink_is_not_overflow():
    y = 10.0
    l2 = _ink("Awards", 10, y + PX * typeset.LINE_SPACING)
    h = l2[3] - y + 0.5  # 둘째 줄 잉크 아래 + 여유 0.5
    r = _render("Olive\nAwards", {"x": 10, "y": y, "w": 300, "h": h})
    assert r.lines == 2 and _line_box_rule(2, PX, h)
    assert r.overflow is False


# ── 실제로 영역 밖: 넘침 ───────────────────────────────────────────────────────────────────

def test_wrapped_lines_below_box_are_overflow():
    r = _render("word word word word word word", {"x": 0, "y": 0, "w": 60, "h": 30})
    assert r.lines > 1 and r.overflow is True


def test_normal_multi_line_with_room_is_not_overflow():
    r = _render("word word word word", {"x": 0, "y": 0, "w": 120, "h": 120})
    assert r.lines == 2 and r.overflow is False


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_long_word_overflows_horizontally_for_every_align(align):
    r = _render("Supercalifragilistic", {"x": 100, "y": 10, "w": 40, "h": 60}, align=align)
    assert r.lines == 1 and r.overflow is True


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_short_word_fits_for_every_align(align):
    r = _render("on", {"x": 100, "y": 10, "w": 200, "h": 30}, align=align)
    assert r.overflow is False


def test_glyph_overhang_past_advance_is_ink_overflow():
    """advance 폭 안이어도 글리프가 오른쪽으로 튀어나오면 잉크 넘침이다(advance 만으로 판정하지 않는다 — D-01).
    오른쪽 정렬로 advance 끝을 영역 오른쪽에 맞춘 경우, 잉크가 advance 보다 넓은 단어만 넘침."""
    font = typeset._font(_defaults(), "body", PX)
    d = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    over, flush = "ok", "on"
    assert _ink(over, 0, 0)[2] > d.textlength(over, font=font)  # k 오른쪽 돌출(이 폰트 · 크기에서)
    assert _ink(flush, 0, 0)[2] <= d.textlength(flush, font=font)
    box = {"x": 100, "y": 10, "w": 200, "h": 30}
    assert _render(over, box, align="right").overflow is True
    assert _render(flush, box, align="right").overflow is False


def test_descender_only_below_box_is_overflow():
    y = 10.0
    plain, desc = _ink("ace", 10, y), _ink("gyp", 10, y)
    assert desc[3] > plain[3]  # 하강부가 더 내려간다
    h = (plain[3] + desc[3]) / 2 - y  # 두 아래 경계 사이
    assert _render("ace", {"x": 10, "y": y, "w": 300, "h": h}).overflow is False
    assert _render("gyp", {"x": 10, "y": y, "w": 300, "h": h}).overflow is True


# ── 경계 · 소수 좌표 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("x,y", [(10.0, 10.0), (10.25, 7.6), (33.5, 0.125)])
def test_ink_equal_to_box_edge_is_not_overflow_and_any_excess_is(x, y):
    """잉크 오른쪽 · 아래 = 영역 경계 → 정상, 경계를 0.01 줄이면 넘침(허용 오차 없음, 소수 그대로)."""
    word = "Hello"
    ink = _ink(word, x, y)
    w, h = ink[2] - x, ink[3] - y
    assert _render(word, {"x": x, "y": y, "w": w, "h": h}).overflow is False
    assert _render(word, {"x": x, "y": y, "w": w - 0.01, "h": h}).overflow is True
    assert _render(word, {"x": x, "y": y, "w": w, "h": h - 0.01}).overflow is True


def test_right_align_ink_equal_to_left_edge_is_not_overflow():
    """오른쪽 정렬에서 잉크 왼쪽이 영역 왼쪽과 같으면 정상, 영역을 조금 좁히면(왼쪽으로 나감) 넘침."""
    word, y = "Hello", 10.0
    font = typeset._font(_defaults(), "body", PX)
    adv = ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(word, font=font)
    g = _ink(word, 0, y)  # 원점 기준 잉크
    # 오른쪽 정렬: x0 = bx + w - adv, 잉크 왼쪽 = x0 + g0. 잉크 왼쪽 = bx 가 되게 w = adv - g0
    bx, w = 50.5, adv - g[0]
    assert w >= g[2] - g[0]
    assert _render(word, {"x": bx, "y": y, "w": w, "h": 60}, align="right").overflow is False
    # 영역을 줄이면 오른쪽 정렬 기준점이 왼쪽으로 와 잉크가 영역 왼쪽 밖으로 나간다
    assert _render(word, {"x": bx, "y": y, "w": w - 0.01, "h": 60}, align="right").overflow is True


# ── 섹션 밖으로 잘림 ─────────────────────────────────────────────────────────────────────

def test_text_cut_at_section_bottom_is_overflow_and_still_drawn():
    r = _render("word word word word word word", {"x": 0, "y": 380, "w": 60, "h": 20}, size=(300, 400))
    assert r.overflow is True and r.ink_visible is True


def test_clipped_ink_is_overflow_even_if_box_extends_past_section():
    """영역이 섹션을 벗어난 입력(계약상 섹션 밖 박스는 저장 거절)에서도 섹션 밖 잘림을 넘침으로 둔다."""
    box = {"x": 0, "y": 80, "w": 300, "h": 100}  # 섹션 높이 100 을 넘는 영역
    r = _render("ok\nok", box, size=(300, 100))
    assert r.lines == 2 and r.overflow is True
    assert _render("ok", {"x": 0, "y": 10, "w": 300, "h": 100}, size=(300, 100)).overflow is False  # 같은 입력, 섹션 안


# ── 배율 · 빈 줄 · 그림 불변 ────────────────────────────────────────────────────────────

def test_font_scale_is_used_for_ink_check():
    box = {"x": 0, "y": 0, "w": 200, "h": 30}
    assert _render("ok", box).overflow is False
    assert _render("ok", box, scale=2.0).overflow is True


def test_blank_line_takes_space_but_has_no_ink():
    y = 0.0
    third = _ink("ok", 0, y + 2 * PX * typeset.LINE_SPACING)
    h = third[3] - y  # 셋째 줄 잉크 아래 = 경계
    r = _render("ok\n\nok", {"x": 0, "y": y, "w": 200, "h": h})
    assert r.lines == 3 and r.overflow is False
    assert _render("ok\n\nok", {"x": 0, "y": y, "w": 200, "h": h - 0.01}).overflow is True


def test_only_judgement_changes_drawing_is_same_as_plain_draw():
    """그림은 판정과 무관하다 — 같은 위치에 직접 그린 것과 바이트가 같다(넘침 · 정상 블록 모두)."""
    bg = png_bytes(400, 200)
    blocks = [_blk(1, "DAILY CARE", {"x": 10.5, "y": 10.25, "w": 300, "h": PX / 0.9}, align="center"),
              _blk(2, "word word word word word word", {"x": 0, "y": 60, "w": 60, "h": 30})]
    res = typeset.render(bg, blocks, _defaults())
    assert (res.blocks[1].overflow, res.blocks[2].overflow) == (False, True)
    import io
    with Image.open(io.BytesIO(bg)) as im:
        img = im.convert("RGB")
    d = ImageDraw.Draw(img)
    font = typeset._font(_defaults(), "body", PX)
    lw = d.textlength("DAILY CARE", font=font)
    d.text((10.5 + (300 - lw) / 2, 10.25), "DAILY CARE", font=font, fill=(0x11, 0x11, 0x11))
    lines, _ = typeset._wrap(d, "word word word word word word", font, 60)
    for i, ln in enumerate(lines):
        d.text((0, 60 + i * PX * typeset.LINE_SPACING), ln, font=font, fill=(0x11, 0x11, 0x11))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    assert res.png == buf.getvalue()


def test_renderer_version_marks_overflow_rule_change():
    assert typeset.PILLOW_RENDERER_VERSION.startswith("pillow-typeset/3+pillow-")
