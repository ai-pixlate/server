"""역할별 스타일 기본값 참고 렌더 — 실제 Pretendard 파일로 줄바꿈 · 넘침을 재고 PNG를 만든다(검증용, 운영 렌더 아님).

    python -m pipeline.handoff.style_reference --out pipeline/out/style-defaults-check

`pipeline/samples/style_defaults/cases.json`의 사례마다 흰 캔버스(원본 폭 × 섹션 높이)에 조판 대상 블록을 그리고
사례별 PNG와 `layout.json`(적용값 · 줄 · 줄 폭 · 넘침)을 쓴다. DB · BE · S3를 쓰지 않는다.

운영 렌더(BE `app/typeset.py`)를 대신하지 않는다. 이 렌더의 성공은 운영 연결 완료가 아니다. 알려진 차이:
- Pillow 기본 배치(Layout.BASIC, raqm 없음)라 OpenType GPOS 커닝 · 합자를 적용하지 않는다. 브라우저(HarfBuzz)와 줄 폭이 다를 수 있다.
- 자간은 글자(코드 포인트) 사이에만 넣는다 — 줄 폭 = 글리프 폭 합 + 자간 × (글자 수 − 1). CSS letter-spacing은 엔진에 따라
  마지막 글자 뒤에도 더한다. 폭 경계 사례의 결과가 엔진마다 다를 수 있다.
- 줄 높이는 line_height_px, 세로 위치는 CSS처럼 반행간을 위아래에 나눈다. ascent · descent는 FreeType 값(hhea)이며
  브라우저는 OS/2 typo 값을 쓸 수 있다. 넘침 높이 판정은 줄 수 × line_height_px만 쓰므로 이 차이에 영향받지 않는다.
- 줄바꿈: 원문 개행(\\n)은 유지, 나머지 공백은 하나로 접고 단어 단위로 감는다(CSS white-space: pre-line에 해당).
  한 단어가 영역 폭보다 길면 자르지 않고 폭 넘침으로 표시한다. 자동 축소 · 문구 축약을 하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import PIL
from PIL import Image, ImageDraw, ImageFont

from pipeline.handoff.style_defaults import (BlockInput, ResolvedBlock, StyleDefaults, font_path, load_style_defaults,
                                             render_inputs, resolve_block, verify_fonts)

REFERENCE_RENDERER = f"pixlate-style-reference/1+pillow-{PIL.__version__}+basic"
CASES_PATH = Path(__file__).resolve().parents[1] / "samples" / "style_defaults" / "cases.json"


@dataclass(frozen=True)
class Layout:
    lines: list[str]
    line_widths: list[float]
    height: float
    overflow_width: bool
    overflow_height: bool

    @property
    def overflow(self) -> bool:
        return self.overflow_width or self.overflow_height

    def to_json(self) -> dict[str, Any]:
        return {"lines": self.lines, "line_widths": [round(w, 3) for w in self.line_widths], "height": self.height,
                "overflow_width": self.overflow_width, "overflow_height": self.overflow_height, "overflow": self.overflow}


def load_font(d: StyleDefaults, face: str, size_px: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path(d, face)), size=size_px, layout_engine=ImageFont.Layout.BASIC)


def text_width(font: ImageFont.FreeTypeFont, s: str, tracking_px: float) -> float:
    return font.getlength(s) + tracking_px * (len(s) - 1) if s else 0.0


def layout(text: str, applied: dict[str, Any], box: dict[str, int], font: ImageFont.FreeTypeFont) -> Layout:
    t = applied["letter_spacing_px"]
    lines: list[str] = []
    too_wide = False
    for para in text.split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        too_wide |= text_width(font, cur, t) > box["w"]
        for w in words[1:]:
            too_wide |= text_width(font, w, t) > box["w"]
            trial = f"{cur} {w}"
            if text_width(font, trial, t) <= box["w"]:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    widths = [text_width(font, s, t) for s in lines]
    height = applied["line_height_px"] * len(lines)
    return Layout(lines=lines, line_widths=widths, height=height, overflow_width=too_wide, overflow_height=height > box["h"])


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def draw_block(draw: ImageDraw.ImageDraw, lay: Layout, applied: dict[str, Any], box: dict[str, int],
               font: ImageFont.FreeTypeFont) -> None:
    ascent, descent = font.getmetrics()
    lh, t = applied["line_height_px"], applied["letter_spacing_px"]
    fill = _rgb(applied["font_color"])
    for i, (line, lw) in enumerate(zip(lay.lines, lay.line_widths)):
        if applied["align"] == "center":
            x0 = box["x"] + (box["w"] - lw) / 2
        elif applied["align"] == "right":
            x0 = box["x"] + box["w"] - lw
        else:
            x0 = box["x"]
        baseline = box["y"] + i * lh + (lh - (ascent + descent)) / 2 + ascent
        for k, ch in enumerate(line):  # 자간: k번째 글자 = 앞 글자들의 폭 + 자간 × k
            draw.text((x0 + font.getlength(line[:k]) + t * k, baseline), ch, font=font, fill=fill, anchor="ls")


def run_case(case: dict[str, Any], d: StyleDefaults) -> tuple[Image.Image, dict[str, Any]]:
    inp = case["input"]
    width, height = inp["source_width_px"], inp["section_height_px"]
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    out: dict[str, Any] = {}
    for b in inp["blocks"]:
        r: ResolvedBlock = resolve_block(to_block_input(b), width, d)
        rec = r.to_json()
        if r.render:
            box = b["box"]
            font = load_font(d, r.applied["font_face"], r.applied["font_size_px"])
            lay = layout(b["text"], r.applied, box, font)
            draw.rectangle([box["x"], box["y"], box["x"] + box["w"] - 1, box["y"] + box["h"] - 1], outline=(200, 200, 255))
            draw_block(draw, lay, r.applied, box, font)
            rec["layout"] = lay.to_json()
        out[str(b["id"])] = rec
    return img, out


def to_block_input(b: dict[str, Any]) -> BlockInput:
    return BlockInput(id=b["id"], role=b["role"], is_excluded=b["is_excluded"], source_ko=b["source_ko"], text=b["text"],
                      style=b["style"], style_stage=b["style_stage"], align_basis=b.get("align_basis"))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description="역할별 스타일 기본값 참고 렌더(검증용, 운영 렌더 아님)")
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    d = load_style_defaults()
    verify_fonts(d)  # 폰트가 없거나 다르면 여기서 멈춘다(대체 폰트로 그리지 않음)
    cases = json.loads(a.cases.read_text(encoding="utf-8"))["cases"]
    a.out.mkdir(parents=True, exist_ok=True)
    summary = {"renderer": REFERENCE_RENDERER, "render_inputs": render_inputs(d), "cases": {}}
    for c in cases:
        img, rec = run_case(c, d)
        img.save(a.out / f"{c['id']}.png")
        summary["cases"][c["id"]] = rec
        print(f"{c['id']}: {a.out / (c['id'] + '.png')}")
    (a.out / "layout.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"기록: {a.out / 'layout.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
