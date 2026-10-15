"""⑦ 글자 크기 환산 비율(style.em_ratio) 실측 도구(2026-10-15) — 개발용, 운영 경로 아님.

    # 1) OCR 영역 bbox 높이 대비 실제 글자 잉크 높이 비율 집계
    python docs/ai-experiments/tools/style_em_ratio_probe.py measure \
        --input pipeline/samples/local/downstream-input-v1 --out docs/ai-experiments/runs/<이름>/measure.json

    # 2) em_ratio 후보별 비교 이미지 — 원본 | 후보마다 인페인트 배경 위에 원문(한글)을 Pretendard로 다시 그린 것
    python docs/ai-experiments/tools/style_em_ratio_probe.py compare \
        --input pipeline/samples/local/downstream-input-v1 --inpaint-bg pipeline/samples/local/inpaint-bg-v1_2026-09-30 \
        --section GS-01_002/sec_1_01 [--section ...] --ratio 1.35 --ratio 0.9 [--translations FILE] --out <png>

measure: MANIFEST `images[].sections` 대상만. ⑦ 처리 대상(④ is_product_label=false · ⑤ is_brand_logo=false) 블록의 비공백 영역에서
영역 bbox를 crop → ⑦과 같은 명도식 · Otsu · 외곽 다수결로 글자 집단을 고르고, 글자 픽셀이 crop 폭의 1% 이상인 행의 처음~끝을 잉크 높이로 본다.
색을 나눌 수 없는 영역(single_class · border_tie)은 뺀다. 문자 구성(한글만 · 라틴 대문자만 · 그 밖)별 bbox.h / 잉크 높이 분위수와
Pretendard(설정 자료의 Regular · SemiBold) 글꼴의 잉크 높이 / em을 함께 출력한다.

compare: 크기는 `pipeline.stages.style._block_style`로 후보 비율마다 다시 계산한다(⑦ 계산을 복제하지 않음). 원문은 영역마다 영역 bbox
왼쪽 · 세로 가운데에 블록 est_font_px · 블록 글자색(없으면 #222222)으로 그린다 — 추정 크기가 원본 글자 크기와 맞는지 보는 용도이며 운영 조판
(`app/typeset.py`)과 배치 방식이 다르다. `--translations`({"<원본>/<섹션>": {"<block_key>": "번역문"}})를 주면 그 섹션은 운영 조판
`app.typeset.render`로 번역문을 그린 줄을 더한다(역할 기본값 없음 — 측정 NULL 블록은 그리지 않는다).
입력본은 읽기만 한다.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline.stages.style import _block_style, border_mask, luma, otsu_threshold  # noqa: E402
from pipeline.types import TextBlock  # noqa: E402

FONT_DIR = ROOT / "pipeline/data/fonts/pretendard"
HANGUL = re.compile(r"^[가-힣\s]+$")
LATIN_UPPER = re.compile(r"^[A-Z\s]+$")
ROW_MIN_FG = 0.01  # 글자 픽셀이 crop 폭의 이 비율 이상인 행만 잉크로 본다(외곽 잡음 행 제외)


def read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def targets(inp: Path, wanted: list[str] | None = None):
    """(원본, 섹션 메타, 처리 대상 TextBlock 목록). MANIFEST 대상만, ⑦ 처리 대상 블록만."""
    manifest = read_json(inp / "MANIFEST.json")
    for img in manifest["images"]:
        prod = img["image"]
        split = read_json(inp / prod / "split.json")
        for s in img["sections"]:
            key = s["section_key"]
            if wanted is not None and f"{prod}/{key}" not in wanted:
                continue
            meta = next(x for x in split["sections"] if x["section_key"] == key)
            merge = read_json(inp / prod / "merge" / f"{key}.json")
            label = {d["block_key"]: d["is_product_label"] for d in read_json(inp / prod / "label" / f"{key}.json")["labels"]}
            logo = {d["block_key"]: d["is_brand_logo"] for d in read_json(inp / prod / "logo" / f"{key}.json")["decisions"]}
            blocks = [TextBlock.model_validate(b) for b in merge["blocks"]
                      if label.get(b["block_key"]) is False and logo.get(b["block_key"]) is False]
            yield prod, meta, blocks


def ink_height(crop: np.ndarray) -> int | None:
    y = luma(crop)
    t = otsu_threshold(y)
    if t is None:
        return None
    dark = y <= t
    border = border_mask(*y.shape)
    bd = int((dark & border).sum())
    bl = int(border.sum()) - bd
    if bd == bl:
        return None
    fg = ~dark if bd > bl else dark
    rows = np.where(fg.mean(axis=1) >= ROW_MIN_FG)[0]
    return int(rows[-1] - rows[0] + 1) if len(rows) else None


def font_ink_per_em(face: str, text: str, size: int = 200) -> float:
    l, t, r, b = ImageFont.truetype(str(FONT_DIR / f"Pretendard-{face}.otf"), size).getbbox(text)
    return round((b - t) / size, 4)


def cmd_measure(a) -> int:
    inp = Path(a.input)
    rows = []
    skipped = 0
    for prod, meta, blocks in targets(inp):
        img = np.array(Image.open(inp / prod / meta["image_path"]).convert("RGB"))
        for b in blocks:
            for ln in b.source_lines:
                for r in ln.regions:
                    if not r.text.strip() or r.bbox.w == 0 or r.bbox.h == 0:
                        continue
                    bb = r.bbox
                    ih = ink_height(img[bb.y:bb.y2, bb.x:bb.x2])
                    if not ih:
                        skipped += 1
                        continue
                    kind = "hangul" if HANGUL.match(r.text) else "latin_upper" if LATIN_UPPER.match(r.text) else "other"
                    rows.append({"image": prod, "section_key": meta["section_key"], "block_key": b.block_key,
                                 "region_key": r.region_key, "text": r.text, "kind": kind, "score": r.score,
                                 "bbox_h": bb.h, "ink_h": ih, "ratio": round(bb.h / ih, 4)})
    summary = {}
    for kind in ("hangul", "latin_upper", "other", "all"):
        v = [x["ratio"] for x in rows if kind == "all" or x["kind"] == kind]
        if v:
            q = np.percentile(v, [10, 25, 50, 75, 90])
            summary[kind] = {"n": len(v), **{f"p{p}": round(float(x), 3) for p, x in zip((10, 25, 50, 75, 90), q)}}
    fonts = {face: {"hangul_ink_per_em": font_ink_per_em(face, "진정집중민감피부"),
                    "latin_cap_ink_per_em": font_ink_per_em(face, "HEMI")} for face in ("Regular", "SemiBold")}
    out = {"input": str(inp), "row_min_fg": ROW_MIN_FG, "regions": len(rows), "skipped_color_split": skipped,
           "summary_bbox_h_over_ink_h": summary, "pretendard": fonts, "rows": rows}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"regions={len(rows)} skipped(color split 불가)={skipped}")
    for k, s in summary.items():
        print(f"  {k:11s} n={s['n']:4d}  bbox.h/ink  p10={s['p10']} p25={s['p25']} p50={s['p50']} p75={s['p75']} p90={s['p90']}")
    for face, f in fonts.items():
        print(f"  Pretendard-{face}: 한글 잉크/em={f['hangul_ink_per_em']}  라틴 대문자 잉크/em={f['latin_cap_ink_per_em']}")
    return 0


def _label(im: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, im.width, 64], fill=(0, 0, 0))
    d.text((16, 8), text, fill=(255, 230, 0), font=ImageFont.truetype(str(FONT_DIR / "Pretendard-SemiBold.otf"), 40))


def _redraw_korean(bg: Image.Image, src: np.ndarray, blocks: list[TextBlock], ratio: float) -> Image.Image:
    im = bg.copy()
    d = ImageDraw.Draw(im)
    for b in blocks:
        st = _block_style(b, src, ratio, 0.12)
        if st.est_font_px is None:
            continue
        font = ImageFont.truetype(str(FONT_DIR / "Pretendard-Regular.otf"), max(1, round(st.est_font_px)))
        color = st.font_color or "#222222"
        for ln in b.source_lines:
            for r in ln.regions:
                if r.text.strip():
                    d.text((r.bbox.x, r.bbox.y + r.bbox.h / 2), r.text, font=font, fill=color, anchor="lm")
    return im


def _typeset(bg_png: bytes, src: np.ndarray, blocks: list[TextBlock], texts: dict[str, str], ratio: float) -> Image.Image:
    from app import typeset

    rbs = []
    for i, b in enumerate(blocks):
        if b.block_key not in texts:
            continue
        st = _block_style(b, src, ratio, 0.12)
        style = {"font_color": st.font_color, "bg_color": st.bg_color, "est_font_px": st.est_font_px, "align": st.align}
        if any(style[k] is None for k in typeset.STYLE_FIELDS):
            continue
        rbs.append(typeset.RenderBlock(id=i, role=b.role, text=texts[b.block_key], box=b.bbox.model_dump(), style=style))
    return Image.open(io.BytesIO(typeset.render(bg_png, rbs, None).png)).convert("RGB")


def cmd_compare(a) -> int:
    inp, bgdir = Path(a.input), Path(a.inpaint_bg)
    translations = read_json(Path(a.translations)) if a.translations else {}
    rows_img = []
    for prod, meta, blocks in targets(inp, a.section):
        key = f"{prod}/{meta['section_key']}"
        src = np.array(Image.open(inp / prod / meta["image_path"]).convert("RGB"))
        bg_path = bgdir / f"{prod}__{meta['section_key']}.png"
        bg_png = bg_path.read_bytes()
        bg = Image.open(io.BytesIO(bg_png)).convert("RGB")
        orig = Image.fromarray(src)
        _label(orig, f"{key} 원본")
        row = [orig]
        for ratio in a.ratio:
            im = _redraw_korean(bg, src, blocks, ratio)
            _label(im, f"원문 재조판 em_ratio={ratio}")
            row.append(im)
        rows_img.append(row)
        if key in translations:
            trow = [Image.new("RGB", orig.size, (255, 255, 255))]
            for ratio in a.ratio:
                im = _typeset(bg_png, src, blocks, translations[key], ratio)
                _label(im, f"운영 조판(번역) em_ratio={ratio}")
                trow.append(im)
            rows_img.append(trow)
    gap = 16
    crop_h = a.crop_h
    widths = [sum(p.width for p in r) + gap * (len(r) - 1) for r in rows_img]
    heights = [min(crop_h or max(p.height for p in r), max(p.height for p in r)) for r in rows_img]
    sheet = Image.new("RGB", (max(widths), sum(heights) + gap * (len(rows_img) - 1)), (255, 255, 255))
    y = 0
    for r, h in zip(rows_img, heights):
        x = 0
        for p in r:
            sheet.paste(p.crop((0, 0, p.width, min(h, p.height))), (x, y))
            x += p.width + gap
        y += h + gap
    if a.scale != 1:
        sheet = sheet.resize((round(sheet.width * a.scale), round(sheet.height * a.scale)), Image.LANCZOS)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(a.out)
    print(f"saved {a.out} {sheet.size}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--input", required=True)
    m.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--input", required=True)
    c.add_argument("--inpaint-bg", required=True)
    c.add_argument("--section", action="append", required=True, help="<원본>/<섹션>, 여러 번")
    c.add_argument("--ratio", action="append", type=float, required=True)
    c.add_argument("--translations")
    c.add_argument("--crop-h", type=int, default=0, help="섹션 위쪽부터 이 높이만(0 = 전체)")
    c.add_argument("--scale", type=float, default=0.5)
    c.add_argument("--out", required=True)
    a = p.parse_args(argv)
    return cmd_measure(a) if a.cmd == "measure" else cmd_compare(a)


if __name__ == "__main__":
    sys.exit(main())
