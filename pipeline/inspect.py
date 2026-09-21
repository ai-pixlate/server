"""결과 JSON을 섹션 이미지 위에 그려 사람이 확인하는 도구.

- OcrResult  → 영역 poly(초록) + region_key·score
- MergeResult → 블록 bbox(역할별 색) + block_key·role, 줄 bbox(연한 선)
- SplitResult → 원본 위에 섹션 경계선 + section_key

라벨은 키·숫자만 그린다(한글 폰트가 없는 환경에서도 깨지지 않게). 원문은 JSON에서 본다.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pipeline.types import MergeResult, OcrResult, SplitResult

ROLE_COLORS = {
    "title": (220, 20, 60),  # crimson
    "body": (30, 100, 220),  # blue
    "caption": (120, 120, 120),  # gray
    "price": (200, 120, 0),  # orange
    "caution": (150, 0, 180),  # purple
}
REGION_COLOR = (0, 160, 60)
LINE_COLOR = (120, 200, 160)
SECTION_COLOR = (255, 0, 0)


def _font(size: int = 12):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 오래된 Pillow
        return ImageFont.load_default()


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, color, font) -> None:
    x, y = xy
    tb = draw.textbbox((x, y), text, font=font)
    draw.rectangle(tb, fill=(255, 255, 255))
    draw.text((x, y), text, fill=color, font=font)


def overlay_ocr(image_path: str | Path, res: OcrResult, out: str | Path) -> Path:
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = _font()
    for r in res.regions:
        draw.polygon([tuple(p) for p in r.poly], outline=REGION_COLOR, width=2)
        _label(draw, (r.bbox.x, max(0, r.bbox.y - 15)), f"{r.region_key} {r.score:.2f}", REGION_COLOR, font)
    return _save(im, out)


def overlay_merge(image_path: str | Path, res: MergeResult, out: str | Path) -> Path:
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = _font()
    for b in res.blocks:
        color = ROLE_COLORS[b.role]
        for ln in b.source_lines:
            draw.rectangle((ln.bbox.x, ln.bbox.y, ln.bbox.x2, ln.bbox.y2), outline=LINE_COLOR, width=1)
        draw.rectangle((b.bbox.x, b.bbox.y, b.bbox.x2, b.bbox.y2), outline=color, width=3)
        conf = "" if b.ocr_confidence is None else f" {b.ocr_confidence:.2f}"
        _label(draw, (b.bbox.x, max(0, b.bbox.y - 15)), f"{b.block_key} {b.role}{conf}", color, font)
    return _save(im, out)


def overlay_split(image_path: str | Path, res: SplitResult, out: str | Path) -> Path:
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = _font(14)
    for s in res.sections:
        draw.line((0, s.top_offset, im.width, s.top_offset), fill=SECTION_COLOR, width=3)
        _label(draw, (4, s.top_offset + 4), f"{s.section_key} top={s.top_offset} h={s.height}", SECTION_COLOR, font)
    return _save(im, out)


def _save(im: Image.Image, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out
