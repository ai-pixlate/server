"""① 섹션 경계 재검사(F-SEC-01 수용 ③) 조사 도구(2026-11-03) — 개발용, 운영 경로 아님. 제품 코드 · 설정은 바꾸지 않는다.

    python docs/ai-experiments/tools/section_boundary_probe.py \
        --baseline docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --fixed <레포 루트>/pipeline/samples/local/merge-input-v1 \
        --images pipeline/samples/evaluation/gs-v1 \
        --out docs/ai-experiments/runs/2026-11-03_01-split_boundary-recheck

입력(읽기만):
- baseline : 베이스라인 회차의 `evidence/<GS>.json`(section · text_block). 영역 = text_block.source_lines[].regions(섹션 로컬).
  GS-03은 분석 실패로 섹션이 없다.
- fixed    : 레포 밖 ③ 고정 입력 `merge-input-v1`(① split 버전 2 + 섹션별 OcrResult). OCR 없는 품질 표본 섹션은 영역 미상.
- images   : 원본 상세페이지(`<GS>/<이미지>.jpg`). 화소 검사 · 대조 그림에 쓴다.

경계 = 같은 원본 안에서 앞 섹션 끝 = 뒤 섹션 시작(원본 y). 원본 이미지 사이의 경계(장 경계)는 섹션 경계가 아니라 세지 않는다.

경계별 기록(조사용 신호 — 운영 규칙 · 임계값 제안이 아니다):
- 화소: 경계 위 행(y-1)과 경계 행(y)의 행 색 표준편차(① `section_split.row_profile`과 같은 식), 각각 ≤ ① `blank_row_std`(균일 행)인지.
- 영역 접촉: 앞 섹션 영역 중 하단이 섹션 끝에서 TOUCH_PX 이내 · 뒤 섹션 영역 중 상단이 TOUCH_PX 이내인 것.
  OCR 상자가 경계에 닿으면 그 줄이 경계에서 잘렸을 수 있다(OCR 상자는 글자보다 넓게 잡히기도 하므로 후보일 뿐이다).
- 범위 밖 영역: bbox가 섹션 밖(y < 0 · y + h > 섹션 높이)인 영역 — 좌표 변환 확인용.
- 가장 가까운 줄: 경계 위 가장 아래 영역 · 경계 아래 가장 위 영역의 간격 합 / 두 영역 높이 중앙값(gap_ratio)과 가로 겹침.
  간격이 줄 높이보다 작고 가로로 겹치면 한 문단이 경계에서 나뉘었을 수 있는 후보다.
출력: `boundaries.json` · `summary.json` · `crops/<n>.png`(경계 ±CROP_PAD 원본, 빨간 선 = 경계, 초록 = 앞 섹션 영역, 파랑 = 뒤 섹션 영역).
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

TOUCH_PX = 2
CROP_PAD = 90
GROUPS = ("GS-01", "GS-02", "GS-03")


def _rect(b: dict) -> tuple[int, int, int, int]:
    return int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])


def load_baseline(run: Path) -> list[dict]:
    """[{image, sections:[{key, top, height, regions:[{text, x, y, w, h}] | None}]}] — 원본 이미지 단위."""
    out = []
    for g in GROUPS:
        d = json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))
        regs = collections.defaultdict(list)
        for b in d["text_block"]:
            for ln in b["source_lines"] or []:
                for r in ln["regions"]:
                    x, y, w, h = _rect(r["bbox"])
                    regs[b["section_id"]].append({"text": r["text"], "x": x, "y": y, "w": w, "h": h, "block": b["id"]})
        for src in sorted(d["source_image"], key=lambda s: s["upload_order"]):
            secs = sorted((s for s in d["section"] if s["source_image_id"] == src["id"]), key=lambda s: s["section_order"])
            if not secs:
                continue
            out.append({"dataset": "baseline-v1.0.1", "image": Path(src["file_url"]).stem, "gs": g, "height": src["height"],
                        "sections": [{"key": f"section#{s['id']}", "top": s["top_offset"], "height": s["height"],
                                      "regions": regs.get(s["id"], [])} for s in secs]})
    return out


def load_fixed(root: Path) -> list[dict]:
    out = []
    for split_path in sorted(root.glob("*/split.json")):
        img = split_path.parent
        sp = json.loads(split_path.read_text(encoding="utf-8"))
        secs = []
        for s in sorted(sp["sections"], key=lambda s: s["section_order"]):
            ocr = img / "ocr" / f"{s['section_key']}.json"
            regions = None
            if ocr.exists():
                regions = [{"text": r["text"], **dict(zip("xywh", _rect(r["bbox"])))}
                           for r in json.loads(ocr.read_text(encoding="utf-8"))["regions"]]
            secs.append({"key": s["section_key"], "top": s["top_offset"], "height": s["height"], "regions": regions})
        out.append({"dataset": "fixed-v1", "image": img.name, "gs": img.name[:5], "height": sp["source_height"], "sections": secs})
    return out


def _row_std(im: Image.Image, y: int) -> float:
    arr = np.asarray(im.crop((0, y, im.width, y + 1)).convert("RGB"), dtype=np.float32)[0]
    return float(arr.std(axis=0).mean())


def boundaries(pages: list[dict], images: Path, blank_std: float) -> list[dict]:
    rows = []
    for p in pages:
        im = Image.open(images / p["gs"] / f"{p['image']}.jpg")
        assert im.height == p["height"], (p["image"], im.height, p["height"])
        for a, b in zip(p["sections"], p["sections"][1:]):
            y = b["top"]
            assert a["top"] + a["height"] == y, (p["image"], a["key"], b["key"])
            rec = {"dataset": p["dataset"], "image": p["image"], "y": y, "above": a["key"], "below": b["key"],
                   "above_h": a["height"], "below_h": b["height"],
                   "std_prev": round(_row_std(im, y - 1), 2), "std_at": round(_row_std(im, y), 2)}
            rec["uniform_prev"], rec["uniform_at"] = rec["std_prev"] <= blank_std, rec["std_at"] <= blank_std
            ra, rb = a["regions"], b["regions"]
            rec["ocr_above"], rec["ocr_below"] = ra is not None, rb is not None
            if ra is not None:
                rec["touch_above"] = [r["text"] for r in ra if r["y"] + r["h"] >= a["height"] - TOUCH_PX]
                rec["oob_above"] = [r["text"] for r in ra if r["y"] < 0 or r["y"] + r["h"] > a["height"]]
                near_a = max(ra, key=lambda r: r["y"] + r["h"], default=None)
                rec["gap_above"] = a["height"] - (near_a["y"] + near_a["h"]) if near_a else None
            if rb is not None:
                rec["touch_below"] = [r["text"] for r in rb if r["y"] <= TOUCH_PX]
                rec["oob_below"] = [r["text"] for r in rb if r["y"] < 0 or r["y"] + r["h"] > b["height"]]
                near_b = min(rb, key=lambda r: r["y"], default=None)
                rec["gap_below"] = near_b["y"] if near_b else None
            if ra and rb:
                xo = max(0, min(near_a["x"] + near_a["w"], near_b["x"] + near_b["w"]) - max(near_a["x"], near_b["x"]))
                rec["near_text"] = [near_a["text"], near_b["text"]]
                rec["near_x_overlap"] = round(xo / max(1, min(near_a["w"], near_b["w"])), 3)
                rec["gap_ratio"] = round((rec["gap_above"] + rec["gap_below"]) / statistics.median([near_a["h"], near_b["h"]]), 3)
            rec["touch"] = bool(rec.get("touch_above") or rec.get("touch_below"))
            rec["para_candidate"] = (not rec["touch"] and rec.get("gap_ratio") is not None and rec["gap_ratio"] <= 1.0
                                     and rec["near_x_overlap"] >= 0.5)
            rows.append(rec)
    return rows


def crops(rows: list[dict], pages: list[dict], images: Path, out: Path, per_sheet: int = 6) -> dict[str, list[int]]:
    """고유 (이미지, y)마다 한 장면. 두 자료의 영역을 함께 그린다(앞 섹션 초록 · 뒤 섹션 파랑, 고정 입력은 점선 대신 얇은 선)."""
    out.mkdir(parents=True, exist_ok=True)
    regs = {}
    for p in pages:
        for s in p["sections"]:
            regs[(p["dataset"], p["image"], s["key"])] = (s["top"], s["regions"] or [])
    uniq = sorted({(r["image"], r["y"]) for r in rows})
    index = {f"{i}": [] for i in range(0, len(uniq), per_sheet)}
    font = ImageFont.load_default(size=14)
    tiles = []
    for n, (img, y) in enumerate(uniq):
        gs = img[:5]
        im = Image.open(images / gs / f"{img}.jpg").convert("RGB")
        top, bot = max(0, y - CROP_PAD), min(im.height, y + CROP_PAD)
        c = im.crop((0, top, im.width, bot))
        dr = ImageDraw.Draw(c)
        for r in rows:
            if (r["image"], r["y"]) != (img, y):
                continue
            for side, color in (("above", (0, 170, 0)), ("below", (0, 90, 255))):
                stop, rs = regs[(r["dataset"], img, r[side])]
                for g in rs:
                    gy0, gy1 = stop + g["y"] - top, stop + g["y"] + g["h"] - top
                    if gy1 < 0 or gy0 > c.height:
                        continue
                    width = 2 if r["dataset"].startswith("baseline") else 1
                    dr.rectangle((g["x"], gy0, g["x"] + g["w"], gy1), outline=color, width=width)
        dr.line((0, y - top, c.width, y - top), fill=(255, 0, 0), width=1)
        c.thumbnail((900, 400))
        label = f"#{n} {img} y={y} " + " ".join(sorted({r['dataset'][:8] for r in rows if (r['image'], r['y']) == (img, y)}))
        tiles.append((n, label, c))
    for i in range(0, len(tiles), per_sheet):
        chunk = tiles[i:i + per_sheet]
        sheet = Image.new("RGB", (900, sum(t.height + 22 for _, _, t in chunk)), "white")
        d, yy = ImageDraw.Draw(sheet), 0
        for n, label, t in chunk:
            d.text((4, yy + 3), label, fill=(0, 0, 0), font=font)
            sheet.paste(t, (0, yy + 20))
            yy += t.height + 22
        sheet.save(out / f"sheet_{i // per_sheet:02d}.png")
        index[f"{i}"] = [n for n, _, _ in chunk]
    return {"unique_boundaries": [{"n": n, "image": img, "y": y} for n, (img, y) in enumerate(uniq)]}


def summarize(rows: list[dict]) -> dict:
    s = {}
    for ds in sorted({r["dataset"] for r in rows}):
        sub = [r for r in rows if r["dataset"] == ds]
        both = [r for r in sub if r["ocr_above"] and r["ocr_below"]]
        s[ds] = {"boundaries": len(sub), "images_with_boundary": len({r["image"] for r in sub}),
                 "ocr_both_sides": len(both), "ocr_one_side": sum(r["ocr_above"] != r["ocr_below"] for r in sub),
                 "nonuniform_both_rows": sum(not r["uniform_prev"] and not r["uniform_at"] for r in sub),
                 "nonuniform_any_row": sum(not r["uniform_prev"] or not r["uniform_at"] for r in sub),
                 "touch": sum(r["touch"] for r in sub),
                 "touch_above": sum(bool(r.get("touch_above")) for r in sub),
                 "touch_below": sum(bool(r.get("touch_below")) for r in sub),
                 "touch_both": sum(bool(r.get("touch_above")) and bool(r.get("touch_below")) for r in sub),
                 "oob_regions": sum(len(r.get("oob_above", [])) + len(r.get("oob_below", [])) for r in sub),
                 "para_candidate": sum(r["para_candidate"] for r in sub)}
    keys = collections.defaultdict(set)
    for r in rows:
        keys[r["image"]].add((r["dataset"], r["y"]))
    common = {}
    for img, ks in keys.items():
        a = {y for d, y in ks if d.startswith("baseline")}
        b = {y for d, y in ks if d == "fixed-v1"}
        if a and b or (a or b) and img in {r["image"] for r in rows if r["dataset"].startswith("baseline")}:
            common[img] = {"same": len(a & b), "baseline_only": sorted(a - b), "fixed_only": sorted(b - a)}
    s["baseline_vs_fixed"] = common
    s["unique_boundaries"] = len({(r["image"], r["y"]) for r in rows})
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--fixed", type=Path, required=True)
    ap.add_argument("--images", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    import tomllib
    blank_std = float(tomllib.loads((ROOT / "pipeline/config/default.toml").read_text(encoding="utf-8"))["section"]["blank_row_std"])
    pages = load_baseline(a.baseline) + load_fixed(a.fixed)
    rows = boundaries(pages, a.images, blank_std)
    a.out.mkdir(parents=True, exist_ok=True)
    idx = crops(rows, pages, a.images, a.out / "crops")
    s = summarize(rows)
    (a.out / "boundaries.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (a.out / "crop_index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    (a.out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
