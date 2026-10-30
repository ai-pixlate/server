"""⑨ 한 줄 블록 넘침 판정 · 정렬 조사 도구(2026-10-30) — 개발용, 운영 경로 아님. 운영 조판 코드는 바꾸지 않는다.

    python docs/ai-experiments/tools/typeset_overflow_probe.py \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --out docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow \
        [--crop GS-01:186 --crop GS-01:110 ...]

입력: 베이스라인 회차의 `evidence/<GS>.json`(DB 행 보존본 — text_block · section · job_async_task · task_handoff)과
`objects/`(object-index.json으로 키 → SHA-256 파일). 읽기만 한다.

재계산: 조판 대상 블록(`_render_inputs`와 같은 조건 — is_excluded=false · 원문 · 번역문 비어 있지 않음)을 운영 조판 함수
(`app.typeset.resolve_style` · `_font` · `_wrap` · `LINE_SPACING`)로 다시 배치하고, 기록된 overflow(text_block · 미리보기 payload ·
최종 렌더 payload)와 대조한다. 같은 크기 · 같은 영역으로 원문(한글)도 배치해 대조군으로 쓴다.

분류(블록당 1개, 번역문 기준):
- fit     : 넘침 아님
- width   : 한 단어의 진행 폭이 영역 폭보다 넓다 — 글자가 실제로 영역 밖
- wrap    : 줄 수 × 행간 초과이고 글리프 잉크 하단이 영역 높이의 3%를 넘게 아래로 나간다 — 글자가 실제로 영역 밖
- edge    : 글리프 잉크 하단이 영역을 0 초과 3% 이하로 넘는다 — 경계(판단 불가)
- leading : 줄 수 × 행간만 영역 높이를 넘고 글리프 잉크는 영역 안 — 판정 기준 산물
글리프 잉크 = Pillow `font.getbbox`(줄마다, 운영 조판과 같은 x · y)의 합집합.

대안 기준 시뮬레이션(규칙 결정이 아님): A = (줄 수 − 1) × 행간 + 글자 크기 > 영역 높이(폭 판정은 그대로).

정렬 후보: 블록 영역 중심이 섹션 중심에서 섹션 폭의 2% 이내이고 영역 폭이 섹션 폭의 80% 이하인 블록 중, 적용 정렬이 left이고
렌더된 줄 중심이 영역 중심에서 섹션 폭의 2% 이상 벗어난 것. 섹션 중앙 정렬만 잡는 하한 성격의 자동 판정이다(사람 검수 아님).

출력: `blocks.json`(블록별 재계산 값) · `summary.json`(집계) · `--crop`을 주면 `crops.png`(원본 | 렌더, 빨간 상자 = 배치 영역).
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app import typeset as t  # noqa: E402

GROUPS = ("GS-01", "GS-02")
EDGE = 0.03
CENTER_TOL = 0.02
NARROW = 0.8
SHIFT_MIN = 0.02


def _layout(draw, text, font, box, align, px):
    lines, too_wide = t._wrap(draw, text, font, max(1, box["w"]))
    line_h = px * t.LINE_SPACING
    ink, centers, y = None, [], box["y"]
    for line in lines:
        lw = draw.textlength(line, font=font)
        x = box["x"] + ((box["w"] - lw) / 2 if align == "center" else (box["w"] - lw) if align == "right" else 0)
        if line:
            centers.append(x + lw / 2)
        bb = font.getbbox(line) if line else None
        if bb and bb[2] > bb[0]:
            r = (x + bb[0], y + bb[1], x + bb[2], y + bb[3])
            ink = r if ink is None else (min(ink[0], r[0]), min(ink[1], r[1]), max(ink[2], r[2]), max(ink[3], r[3]))
        y += line_h
    return {"lines": len(lines), "too_wide": too_wide, "line_h": line_h, "total_h": line_h * len(lines), "ink": ink,
            "centers": centers, "overflow": bool(too_wide or line_h * len(lines) > box["h"])}


def _classify(L, box):
    if not L["overflow"]:
        return "fit"
    if L["too_wide"]:
        return "width"
    over = (L["ink"][3] - (box["y"] + box["h"])) / box["h"] if L["ink"] else 0
    return "leading" if over <= 0 else "edge" if over <= EDGE else "wrap"


def collect(run: Path) -> list[dict]:
    defaults = t.load_role_defaults()
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    rows = []
    for g in GROUPS:
        d = json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))
        secs = {s["id"]: s for s in d["section"]}
        tasks = d["job_async_task"]
        style_stage = {a["unit_id"]: a["status"] for a in tasks if a["stage"] == "style" and a["is_current"]}
        preview = {a["id"] for a in tasks if a["stage"] == "preview" and a["is_current"] and a["status"] == "done"}
        rec, final = {}, {}
        for h in d["task_handoff"]:
            p = h.get("payload") or {}
            if h["task_id"] in preview and h["outcome"] == "done":
                rec.update({int(k): v for k, v in p["render"]["blocks"].items()})
            for r in (p.get("renders") or {}).values():
                final.update({int(k): v for k, v in (r.get("overflow") or {}).items()})
        for b in d["text_block"]:
            if b["is_excluded"] is not False or not (b["source_ko"] or "").strip() or not (b["trans_1"] or "").strip():
                continue
            W = secs[b["section_id"]]["bbox"]["w"]
            box = (b["auto_adjust"] or {}).get("layout_bbox") or b["bbox"]
            box = {k: int(box[k]) for k in ("x", "y", "w", "h")}
            rb = t.RenderBlock(id=b["id"], role=b["role"], text=b["trans_1"], box=box, style=b["style"],
                               style_stage=style_stage.get(b["section_id"]), source_ko=b["source_ko"])
            applied, source = t.resolve_style(rb, defaults, W)
            px = float(applied["est_font_px"])
            font = t._font(defaults, b["role"], px)
            tr = _layout(draw, b["trans_1"], font, box, applied["align"], px)
            src = _layout(draw, b["source_ko"], font, box, applied["align"], px)
            box_cx = box["x"] + box["w"] / 2
            ink = tr["ink"]
            rows.append({
                "gs": g, "id": b["id"], "section_id": b["section_id"], "role": b["role"],
                "src_lines": len(b["source_lines"] or []), "tr_lines": tr["lines"], "same_text": b["trans_1"].strip() == b["source_ko"].strip(),
                "box": box, "section_w": W, "font_px": px, "align": applied["align"], "align_source": source["align"],
                "db_overflow": b["overflow"], "preview_overflow": (rec.get(b["id"]) or {}).get("overflow"),
                "preview_lines": (rec.get(b["id"]) or {}).get("lines"), "final_overflow": final.get(b["id"]),
                "re_overflow": tr["overflow"], "cls": _classify(tr, box), "src_overflow": src["overflow"], "src_cls": _classify(src, box),
                "one_line_h_ratio": px * t.LINE_SPACING / box["h"],
                "alt_a_overflow": tr["too_wide"] or (tr["lines"] - 1) * tr["line_h"] + px > box["h"],
                "ink_w_ratio": ((ink[2] - ink[0]) / box["w"]) if ink else None,
                "ink_bottom_over": ((ink[3] - (box["y"] + box["h"])) / box["h"]) if ink else None,
                "section_centered": abs(box_cx - W / 2) / W <= CENTER_TOL and box["w"] <= NARROW * W,
                "center_shift": max(abs(c - box_cx) for c in tr["centers"]) / W if tr["centers"] else 0.0,
                "source_ko": b["source_ko"], "trans_1": b["trans_1"],
            })
    return rows


def summarize(rows: list[dict]) -> dict:
    C = collections.Counter
    one = [r for r in rows if r["src_lines"] == 1]
    multi = [r for r in rows if r["src_lines"] >= 2]
    tr1 = [r for r in rows if r["tr_lines"] == 1]
    ratios = sorted(r["one_line_h_ratio"] for r in tr1)
    align = [r for r in rows if r["section_centered"] and r["align"] == "left" and r["center_shift"] >= SHIFT_MIN]

    def split(sub):
        return {"total": len(sub),
                "tr_1_line": dict(C(r["cls"] for r in sub if r["tr_lines"] == 1)),
                "tr_multi_line": dict(C(r["cls"] for r in sub if r["tr_lines"] >= 2)),
                "same_text": dict(C(r["cls"] for r in sub if r["same_text"])),
                "source_control": dict(C(r["src_cls"] for r in sub))}

    return {
        "blocks": len(rows),
        "match": {"db_vs_recompute": sum(r["db_overflow"] == r["re_overflow"] for r in rows),
                  "db_vs_preview": sum(r["db_overflow"] == r["preview_overflow"] for r in rows),
                  "db_vs_final": sum(r["db_overflow"] == r["final_overflow"] for r in rows),
                  "lines_vs_preview": sum(r["tr_lines"] == r["preview_lines"] for r in rows)},
        "cls": dict(C(r["cls"] for r in rows)),
        "source_1_line": split(one),
        "source_multi_line": split(multi),
        "tr_1_line_h_ratio_quantiles": [round(ratios[int(q * (len(ratios) - 1))], 3) for q in (0, .25, .5, .75, 1)] if ratios else [],
        "width_tr_1_line_ink_w_le_1_10": sum(r["ink_w_ratio"] <= 1.10 for r in tr1 if r["cls"] == "width"),
        "alt_a_flips": {f"{k[0]}:{k[1]}->{k[2]}": v for k, v in C((r["cls"], r["re_overflow"], r["alt_a_overflow"]) for r in rows).items()
                        if k[1] != k[2]},
        "alt_a_source_1_line_overflow": sum(r["alt_a_overflow"] for r in one),
        "align_candidates": {"total": len(align),
                             "source_1_line": sum(r["src_lines"] == 1 for r in align),
                             "source_multi_line": sum(r["src_lines"] >= 2 for r in align),
                             "cls": dict(C(r["cls"] for r in align)),
                             "align_source": dict(C(r["align_source"] for r in align))},
    }


def crops(run: Path, rows: list[dict], picks: list[str], out: Path) -> None:
    idx = json.loads((run / "object-index.json").read_text(encoding="utf-8"))
    secs = {g: {s["id"]: s for s in json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))["section"]} for g in GROUPS}
    by = {(r["gs"], r["id"]): r for r in rows}
    tiles = []
    for p in picks:
        g, bid = p.split(":")
        r = by[(g, int(bid))]
        sec = secs[g][r["section_id"]]
        ren, org = (Image.open(run / "objects" / idx[k]["sha256"]).convert("RGB") for k in (sec["render_image_key"], sec["image_key"]))
        b, pad = r["box"], max(20, r["box"]["h"])
        c = (max(0, b["x"] - pad), max(0, b["y"] - pad), min(ren.width, b["x"] + b["w"] + pad), min(ren.height, b["y"] + b["h"] + pad * 3))
        a, z = org.crop(c), ren.crop(c)
        for im in (a, z):
            ImageDraw.Draw(im).rectangle((b["x"] - c[0], b["y"] - c[1], b["x"] - c[0] + b["w"] - 1, b["y"] - c[1] + b["h"] - 1),
                                         outline=(255, 0, 0), width=2)
        tile = Image.new("RGB", (a.width * 2 + 10, a.height), "white")
        tile.paste(a, (0, 0))
        tile.paste(z, (a.width + 10, 0))
        tile.thumbnail((900, 400))
        tiles.append((f"{g} #{bid} {r['cls']} src_lines={r['src_lines']} tr_lines={r['tr_lines']} align={r['align']}", tile))
    sheet = Image.new("RGB", (900, sum(tl.height + 24 for _, tl in tiles)), "white")
    d, y = ImageDraw.Draw(sheet), 0
    for label, tl in tiles:
        d.text((4, y + 4), label, fill=(0, 0, 0))
        sheet.paste(tl, (0, y + 20))
        y += tl.height + 24
    sheet.save(out / "crops.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--crop", action="append", default=[], help="GS-01:186 형식")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    rows = collect(a.run)
    s = summarize(rows)
    (a.out / "blocks.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (a.out / "summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.crop:
        crops(a.run, rows, a.crop, a.out)
    print(json.dumps(s, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
