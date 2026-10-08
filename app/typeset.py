"""조판 렌더 — 배경(⑥ 결과 또는 원본) 위에 번역문을 블록 배치 영역에 그린다(⑨ 미리보기·N6 최종 렌더 공통).

- 배치 영역: text_block.auto_adjust.layout_bbox 가 있으면 그것, 없으면 원본 bbox(contract.md 6.1). 섹션 로컬 px.
- 스타일: ⑦ 측정값(font_color · est_font_px · align)을 쓰고, 측정 NULL·⑦ 실패는 **승인된 역할별 기본값**으로 채운다(D9-3).
  기본값은 디자인→PM 확인 값만 쓴다(PIXLATE_STYLE_DEFAULTS JSON). 없으면 StyleDefaultsUnavailable — 임의 값으로 그리지 않는다.
  실제 적용값과 출처(measured / role_default)는 측정값과 분리해 렌더 기록에 남긴다(5.12).
- overflow: 실제 조판 결과 기준. 폭(가장 긴 단어가 영역 폭 초과) 또는 높이(줄 합계가 영역 높이 초과)면 true(6.2).
  문장을 축약하지 않는다. 글자 수 제한(char_limit)과는 별개다.
- 폰트: Pillow 내장 TrueType(ImageFont.load_default). 역할별 폰트 매핑·Playwright 조판은 후속(README 현재 상태).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from dataclasses import dataclass, field
from typing import Any

import PIL
from PIL import Image, ImageDraw, ImageFont

RENDERER_VERSION = f"pillow-typeset/1+pillow-{PIL.__version__}"
ROLES = ("title", "body", "caption", "price", "caution")
STYLE_FIELDS = ("font_color", "est_font_px", "align")
LINE_SPACING = 1.2  # 줄 높이 = 글자 크기 × 1.2 (조판 구현 상수, 렌더러 버전에 포함)


class StyleDefaultsUnavailable(RuntimeError):
    code = "STYLE_DEFAULTS_UNAPPROVED"


@dataclass(frozen=True)
class RoleDefaults:
    version: str
    roles: dict[str, dict[str, Any]]
    sha256: str


def load_role_defaults() -> RoleDefaults | None:
    """승인된 역할별 기본값. {"version": "...", "roles": {"title": {"font_color": "#RRGGBB", "est_font_px": 40, "align": "left"}, ...}}"""
    path = os.getenv("PIXLATE_STYLE_DEFAULTS")
    if not path:
        return None
    raw = open(path, "rb").read()
    data = json.loads(raw)
    roles = data.get("roles") or {}
    for r in ROLES:
        v = roles.get(r)
        if not isinstance(v, dict) or not all(k in v and v[k] is not None for k in STYLE_FIELDS):
            raise ValueError(f"역할 기본값 {r} 에 {STYLE_FIELDS} 가 모두 있어야 한다")
    return RoleDefaults(version=str(data["version"]), roles=roles, sha256=hashlib.sha256(raw).hexdigest())


@dataclass
class RenderBlock:
    id: int
    role: str
    text: str
    box: dict[str, int]
    style: dict[str, Any] | None  # ⑦ 측정값(4키) 또는 None(⑦ 실패·미실행)


@dataclass
class BlockRender:
    overflow: bool
    applied: dict[str, Any]
    source: dict[str, str]
    lines: int


@dataclass
class RenderResult:
    png: bytes
    width: int
    height: int
    blocks: dict[int, BlockRender] = field(default_factory=dict)


def needs_defaults(blocks: list[RenderBlock]) -> bool:
    return any(b.style is None or any(b.style.get(k) is None for k in STYLE_FIELDS) for b in blocks)


def resolve_style(b: RenderBlock, defaults: RoleDefaults | None) -> tuple[dict[str, Any], dict[str, str]]:
    applied, source = {}, {}
    for k in STYLE_FIELDS:
        v = (b.style or {}).get(k)
        if v is not None:
            applied[k], source[k] = v, "measured"
            continue
        if defaults is None:
            raise StyleDefaultsUnavailable("승인된 역할별 스타일 기본값이 없다 — 측정 NULL·스타일 실패 블록을 조판하지 않는다")
        applied[k], source[k] = defaults.roles[b.role][k], "role_default"
    return applied, source


def _font(px: float):
    size = max(1, int(round(px)))
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 구버전 Pillow
        return ImageFont.load_default()


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> tuple[list[str], bool]:
    """단어 단위 줄바꿈. 원문 줄바꿈은 유지한다. 한 단어가 폭을 넘으면 그대로 두고 폭 초과로 표시한다."""
    lines, too_wide = [], False
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        too_wide |= draw.textlength(cur, font=font) > max_w
        for w in words[1:]:
            too_wide |= draw.textlength(w, font=font) > max_w
            trial = f"{cur} {w}"
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines, too_wide


def render(background_png: bytes, blocks: list[RenderBlock], defaults: RoleDefaults | None) -> RenderResult:
    with Image.open(io.BytesIO(background_png)) as im:
        img = im.convert("RGB")
    draw = ImageDraw.Draw(img)
    out = RenderResult(png=b"", width=img.width, height=img.height)
    for b in blocks:
        applied, source = resolve_style(b, defaults)
        font = _font(float(applied["est_font_px"]))
        bx = b.box
        lines, too_wide = _wrap(draw, b.text, font, max(1, bx["w"]))
        line_h = float(applied["est_font_px"]) * LINE_SPACING
        total_h = line_h * len(lines)
        y = bx["y"]
        for line in lines:
            lw = draw.textlength(line, font=font)
            if applied["align"] == "center":
                x = bx["x"] + (bx["w"] - lw) / 2
            elif applied["align"] == "right":
                x = bx["x"] + bx["w"] - lw
            else:
                x = bx["x"]
            draw.text((x, y), line, font=font, fill=_rgb(applied["font_color"]))
            y += line_h
        out.blocks[b.id] = BlockRender(overflow=bool(too_wide or total_h > bx["h"]), applied=applied, source=source, lines=len(lines))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out.png = buf.getvalue()
    return out
