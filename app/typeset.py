"""조판 렌더 — 배경(⑥ 결과 또는 원본) 위에 번역문을 블록 배치 영역에 그린다(⑨ 미리보기·N6 최종 렌더 공통).

- 배치 영역: text_block.auto_adjust.layout_bbox 가 있으면 그것, 없으면 원본 bbox(contract.md 6.1). 섹션 로컬 px.
- 스타일: ⑦ 측정값(font_color · est_font_px · align)을 쓰고, 측정 NULL·⑦ 실패는 **승인된 역할별 기본값**으로 채운다(D9-3·D11).
  기본값은 사용자 승인 설정 자료(pipeline/data/style_defaults.json, `PIXLATE_STYLE_DEFAULTS` 로 대체 경로)와 같은 폰트 파일을
  `pipeline.handoff.style_defaults` 로 읽고 검증해 쓴다(be-handoff 6.6 ①②⑤⑥). 적용 규칙은 그 모듈의 `resolve_block`
  (측정값이 있는 항목은 그대로, NULL 항목만 기본값, ⑦ 실패면 세 항목 모두 기본값, 계약 위반은 STYLE_RESULT_INVALID).
  자료·폰트를 쓸 수 없으면 StyleDefaultsUnavailable — 임의 값이나 내장 기본값으로 그리지 않는다.
  실제 적용값과 출처(measured / role_default)는 측정값과 분리해 렌더 기록에 남긴다(5.12).
- overflow: 실제 조판 결과 기준. 폭(가장 긴 단어가 영역 폭 초과) 또는 높이(줄 합계가 영역 높이 초과)면 true(6.2).
  문장을 축약하지 않는다. 글자 수 제한(char_limit)과는 별개다.
- 폰트: 승인 폰트(Pretendard) 역할별 굵기 — 검증을 마친 설정 자료의 `role_spec` → `font_path` OTF(title·price SemiBold,
  body·caption·caution Regular)를 적용 크기(소수 허용)로 연다(be-handoff 6.2·6.6 ③). 블록마다 폰트 객체 하나로 줄바꿈·폭 측정·
  그리기를 한다. 시스템·내장 폰트로 대체하지 않는다(검증 실패는 StyleDefaultsUnavailable).
  자간·행간 적용과 넘침 기준 변경(6.6 ③④ 나머지), 렌더 기록 6.4 형식 확장(⑦)은 아직 하지 않았다 — 기본값은 글자색·크기·정렬
  세 항목에만 쓴다.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from typing import Any

import PIL
from PIL import Image, ImageDraw, ImageFont

from pipeline.handoff import style_defaults as sd

RENDERER_VERSION = f"pillow-typeset/2+pillow-{PIL.__version__}"  # 2: 역할별 Pretendard·소수 크기(1: Pillow 내장 폰트·정수 크기)
ROLES = ("title", "body", "caption", "price", "caution")
STYLE_FIELDS = ("font_color", "est_font_px", "align")
LINE_SPACING = 1.2  # 줄 높이 = 글자 크기 × 1.2 (조판 구현 상수, 렌더러 버전에 포함)


class StyleDefaultsUnavailable(RuntimeError):
    code = "STYLE_DEFAULTS_UNAPPROVED"


class StyleInputInvalid(RuntimeError):
    """⑦ 결과와 블록 스타일이 계약과 어긋남(⑦ 완료인데 style 없음, ⑦ 실패인데 style 있음, 형식 밖 값 등). 기본값으로 덮지 않는다."""
    code = "STYLE_RESULT_INVALID"


@dataclass(frozen=True)
class RoleDefaults:
    """검증을 마친 승인 설정 자료(`sd.StyleDefaults`)와 렌더 입력 지문(`sd.render_inputs`)."""
    data: Any
    inputs: dict[str, Any]

    @property
    def version(self) -> str:
        return self.data.version

    @property
    def sha256(self) -> str:
        return self.data.sha256


def load_role_defaults() -> RoleDefaults:
    """승인된 역할별 기본값. 경로 기본값은 저장소 정본, `PIXLATE_STYLE_DEFAULTS` 는 대체 경로(be-handoff 6.6 ①).
    자료를 읽을 수 없거나 폰트 파일·라이선스가 자료와 다르면 StyleDefaultsUnavailable(조용히 넘기지 않는다)."""
    path = os.getenv("PIXLATE_STYLE_DEFAULTS") or None
    try:
        d = sd.load_style_defaults(path)
        sd.verify_fonts(d)
    except (sd.StyleDefaultsError, sd.FontAssetError) as e:
        raise StyleDefaultsUnavailable(f"승인된 역할별 스타일 기본값을 쓸 수 없다: {e}") from e
    return RoleDefaults(data=d, inputs=sd.render_inputs(d))


def style_inputs() -> dict[str, Any]:
    """렌더 고정 입력에 넣을 스타일 자료 지문(설정 버전·SHA-256·폰트 SHA-256·규칙 버전 — 6.6 ⑤).
    쓸 수 없으면 {"unavailable": 이유} 를 남기고 렌더 때 StyleDefaultsUnavailable 로 실패시킨다."""
    try:
        return load_role_defaults().inputs
    except StyleDefaultsUnavailable as e:
        return {"unavailable": str(e)}


@dataclass
class RenderBlock:
    id: int
    role: str
    text: str
    box: dict[str, int]
    style: dict[str, Any] | None  # ⑦ 측정값(4키) 또는 None(⑦ 실패·미실행)
    style_stage: str  # 섹션의 ⑦ 단계 결과(done | failed)
    source_ko: str | None  # 원문(빈 원문은 조판하지 않는다 — resolve_block 규칙)


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


def resolve_style(b: RenderBlock, defaults: RoleDefaults, source_width_px: int) -> tuple[dict[str, Any], dict[str, str]] | None:
    """블록 적용값(글자색·크기·정렬)과 출처. 규칙은 `sd.resolve_block`(6.3): 측정값이 있는 항목은 그대로(measured),
    NULL 항목만 역할 기본값(role_default, 크기는 기준 크기 × 원본 폭 / 기준 폭), ⑦ 실패면 세 항목 모두 기본값.
    조판 대상이 아니면(빈 원문·빈 번역문) None — 그리지 않는다."""
    try:
        r = sd.resolve_block(sd.BlockInput(id=b.id, role=b.role, is_excluded=False, source_ko=b.source_ko, text=b.text,
                                           style=b.style, style_stage=b.style_stage),  # type: ignore[arg-type]
                             source_width_px, defaults.data)
    except sd.StyleInputInvalid as e:
        raise StyleInputInvalid(str(e)) from e
    if not r.render:
        return None
    a, s = r.applied, r.source
    return ({"font_color": a["font_color"], "est_font_px": a["font_size_px"], "align": a["align"]},
            {"font_color": s["font_color"], "est_font_px": s["font_size_px"], "align": s["align"]})


def _font(defaults: RoleDefaults, role: str, px: float) -> ImageFont.FreeTypeFont:
    """역할 굵기의 승인 OTF(load_role_defaults 가 검증한 파일)를 적용 크기로 연다. 크기는 반올림하지 않는다(1px 미만만 1px).
    검증 뒤 파일을 열 수 없으면 StyleDefaultsUnavailable — 다른 폰트로 넘어가지 않는다."""
    path = sd.font_path(defaults.data, sd.role_spec(defaults.data, role).face)
    try:
        return ImageFont.truetype(str(path), size=max(1.0, float(px)))
    except OSError as e:
        raise StyleDefaultsUnavailable(f"승인 폰트 파일을 열 수 없다({path}): {e}") from e


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


def render(background_png: bytes, blocks: list[RenderBlock], defaults: RoleDefaults) -> RenderResult:
    """섹션 배경 위에 블록을 그린다. 원본 폭(기본 크기 환산 기준)은 섹션 배경 폭 = 섹션 bbox.w(MVP 섹션은 원본 전체 폭)."""
    with Image.open(io.BytesIO(background_png)) as im:
        img = im.convert("RGB")
    draw = ImageDraw.Draw(img)
    out = RenderResult(png=b"", width=img.width, height=img.height)
    for b in blocks:
        resolved = resolve_style(b, defaults, img.width)
        if resolved is None:
            continue
        applied, source = resolved
        font = _font(defaults, b.role, float(applied["est_font_px"]))
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
