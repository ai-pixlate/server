"""합성 샘플 생성기 — 실제 상세페이지 없이 형식·도구를 확인하기 위한 것.

    python -m pipeline.samples.make_synthetic

pipeline/samples/synthetic_01/ 에 source.png(원본 1장, 배경색이 바뀌는 2구간)과
expected/split.json · expected/sections/*.png · expected/ocr/*.json · expected/merge/*.json 을 만든다.
텍스트·좌표를 한 곳(LAYOUT)에서 정의하므로 이미지와 JSON이 항상 맞는다.
한글 폰트가 없는 환경에서도 돌게 영문 텍스트를 쓴다. 실제 한국어 표본은 samples/local/(git 제외)에 둔다.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pipeline.stages.section_split import crop_sections
from pipeline.types import (
    BBox,
    Line,
    MergeResult,
    OcrRegion,
    OcrResult,
    SourceImage,
    TextBlock,
    block_key,
    line_key,
    ocr_confidence_of,
    region_key,
)

WIDTH, HEIGHT = 600, 1000
BOUNDARY = 560  # 배경색이 바뀌는 지점 = 섹션 경계

# (섹션 번호, 블록 role, 줄 목록[(text, x, y, font_px)])
LAYOUT = [
    (1, "title", [("Deep Moisture Cream", 60, 80, 40)]),
    (1, "body", [("Locks in hydration for 24 hours", 60, 160, 22), ("with ceramide and hyaluronic acid.", 60, 196, 22)]),
    (1, "caption", [("* Tested on 30 adults, 4 weeks", 60, 260, 16)]),
    (1, "price", [("$ 29.00", 60, 420, 36)]),
    (2, "title", [("How to use", 60, 620, 34)]),
    (2, "body", [("Apply a small amount to clean skin", 60, 700, 22), ("morning and night.", 60, 736, 22)]),
    (2, "caution", [("Avoid contact with eyes.", 60, 860, 18)]),
]


def _font(px: int):
    for cand in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(cand, px)
        except OSError:
            continue
    return ImageFont.load_default(size=px)


def main(root: Path | None = None) -> Path:
    root = root or Path(__file__).parent / "synthetic_01"
    exp = root / "expected"
    exp.mkdir(parents=True, exist_ok=True)

    im = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, BOUNDARY, WIDTH, HEIGHT), fill=(236, 240, 245))

    # 그리면서 원본 좌표의 bbox를 기록
    drawn: list[tuple[int, str, list[tuple[str, BBox]]]] = []
    for sec_no, role, lines in LAYOUT:
        boxes = []
        for text, x, y, px in lines:
            f = _font(px)
            draw.text((x, y), text, fill=(20, 20, 20), font=f)
            l, t, r, b = draw.textbbox((x, y), text, font=f)
            boxes.append((text, BBox(x=l, y=t, w=r - l, h=b - t)))
        drawn.append((sec_no, role, boxes))
    src_path = root / "source.png"
    im.save(src_path)

    src = SourceImage(source_image_id=1, upload_order=1, path=str(src_path))
    split = crop_sections(src, im, [BOUNDARY], exp / "sections")
    # image_path를 레포 상대경로로 기록(어느 PC에서 열어도 같게)
    for s in split.sections:
        s.image_path = str(Path(s.image_path).relative_to(root.parent.parent.parent))
    (exp / "split.json").write_text(split.model_dump_json(indent=2), encoding="utf-8")

    for sec in split.sections:
        regions: list[OcrRegion] = []
        blocks: list[TextBlock] = []
        rn = ln = 0
        for sec_no, role, boxes in drawn:
            if sec_no != sec.section_order:
                continue
            lines: list[Line] = []
            for text, ob in boxes:
                rn += 1
                ln += 1
                bb = BBox(x=ob.x, y=ob.y - sec.top_offset, w=ob.w, h=ob.h)  # 섹션 로컬로 변환
                poly = [(bb.x, bb.y), (bb.x2, bb.y), (bb.x2, bb.y2), (bb.x, bb.y2)]
                reg = OcrRegion(region_key=region_key(rn), text=text, score=0.97, poly=poly, bbox=bb)
                regions.append(reg)
                lines.append(Line(line_key=line_key(ln), text=text, bbox=bb, regions=[reg]))
            blocks.append(
                TextBlock(
                    block_key=block_key(len(blocks) + 1),
                    section_key=sec.section_key,
                    block_order=len(blocks) + 1,
                    source_ko=" ".join(l.text for l in lines),
                    source_lines=lines,
                    bbox=BBox.union([l.bbox for l in lines]),
                    role=role,
                    ocr_confidence=ocr_confidence_of(lines),
                )
            )
        (exp / "ocr").mkdir(exist_ok=True)
        (exp / "merge").mkdir(exist_ok=True)
        (exp / "ocr" / f"{sec.section_key}.json").write_text(
            OcrResult(section_key=sec.section_key, regions=regions).model_dump_json(indent=2), encoding="utf-8"
        )
        (exp / "merge" / f"{sec.section_key}.json").write_text(
            MergeResult(section_key=sec.section_key, blocks=blocks).model_dump_json(indent=2), encoding="utf-8"
        )
    return root


if __name__ == "__main__":
    print(main(Path(sys.argv[1]) if len(sys.argv) > 1 else None))
