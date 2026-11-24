"""⑨ 조판 정렬 문제 빈도 조사 도구(2026-11-03) — 개발용, 운영 경로 아님. 운영 조판 · 스타일 코드는 바꾸지 않는다.

    # 1) 블록별 정렬 근거 · 조판값 수집 + 맹검 라벨용 섹션 시트
    python docs/ai-experiments/tools/typeset_align_probe.py features \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --out docs/ai-experiments/runs/2026-11-03_09-typeset_align-frequency
    # 2) 원본 정렬 라벨(TSV)과 대조해 집계
    python docs/ai-experiments/tools/typeset_align_probe.py aggregate \
        --out docs/ai-experiments/runs/2026-11-03_09-typeset_align-frequency \
        --labels docs/ai-experiments/2026-11-03_09-typeset_align-frequency_labels.tsv
    # 3) 원본 | 렌더 대조 시트(라벨 뒤 확인용)
    python docs/ai-experiments/tools/typeset_align_probe.py crops --run <회차> --out <위 out> --name multi_0 GS-01:42 GS-01:53

입력: 베이스라인 회차의 `evidence/<GS>.json`(DB 행 보존본)과 `objects/`. 읽기만 한다. 조판 대상 블록과 적용 정렬은
`typeset_overflow_probe.collect`(PR #96, 운영 `app.typeset.resolve_style`)를 그대로 쓴다. ⑦ 정렬 근거(`align_basis` ·
`align_diag` · NULL 사유)는 현재 스타일 인계 payload(`task_handoff`, block_key = `blk_<text_block.id>`)에서 붙인다.

조사용 자동 신호(운영 규칙 · 정렬 임계값 제안이 아니다):
- row_fragments : 블록의 source_lines 중 세로로 겹치는 쌍(겹침 / 낮은 쪽 높이 ≥ 0.5)이 있다 — 한 가로줄이 ③에서 여러 줄로 잡힘
- section_centered : 블록 중심이 섹션 중심에서 섹션 폭 2% 이내이고 블록 폭 ≤ 섹션 폭 80%(PR #96 정렬 후보와 같은 정의)
- shift_vs_label : 라벨 정렬로 놓았을 때와 적용 정렬로 놓았을 때 번역 줄 x 위치 차의 최댓값(px, 섹션 폭 비율) — 오류의 눈에 띄는 정도.
  오류 여부를 정하지 않는다(오류 = 적용 정렬 ≠ 원본 정렬 라벨).

라벨 TSV 열: gs · id · label(left|center|right|unknown) · basis_note(근거 메모). 라벨은 원본 이미지만 보고 붙인다
(시트에는 블록 번호만 있고 적용 정렬은 없다).

출력: `features.json`(블록별) · `regroup.json`(줄 조각 블록의 현재 ③ 가로줄 묶기 민감도) · `sheets/<GS>_sec<id>_orig.png`(원본 + 블록 번호) ·
aggregate는 `align_summary.json` · `align_rows.json` · crops는 `crops/<name>.png`. numpy가 필요하다(regroup이 ③ · ⑦ 모듈을 읽는다).
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import typeset as t  # noqa: E402
import typeset_overflow_probe as probe  # noqa: E402

LABELS = ("left", "center", "right", "unknown")
ROW_OVERLAP = 0.5


def _style_payload(d: dict) -> dict[int, dict]:
    tasks = {a["id"]: a for a in d["job_async_task"]}
    out = {}
    for h in d["task_handoff"]:
        a = tasks.get(h["task_id"])
        p = h.get("payload") or {}
        if a and a["stage"] == "style" and a["is_current"] and h["outcome"] == "done" and p.get("style"):
            for b in p["style"]["blocks"]:
                out[int(b["block_key"].split("_")[1])] = b
    return out


def _row_fragments(lines: list[dict]) -> int:
    n = 0
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a, b = lines[i]["bbox"], lines[j]["bbox"]
            ov = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
            if ov > 0 and ov / max(1, min(a["h"], b["h"])) >= ROW_OVERLAP:
                n += 1
    return n


def features(run: Path, out: Path) -> list[dict]:
    rows = probe.collect(run)
    by = {(r["gs"], r["id"]): r for r in rows}
    idx = json.loads((run / "object-index.json").read_text(encoding="utf-8"))
    feats = []
    for g in probe.GROUPS:
        d = json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))
        st = _style_payload(d)
        blocks = {b["id"]: b for b in d["text_block"]}
        for (gg, bid), r in by.items():
            if gg != g:
                continue
            b, s = blocks[bid], st.get(bid)
            feats.append({
                "gs": g, "id": bid, "section_id": r["section_id"], "role": r["role"], "src_lines": r["src_lines"],
                "tr_lines": r["tr_lines"], "same_text": r["same_text"], "box": r["box"], "orig_bbox": b["bbox"],
                "section_w": r["section_w"], "font_px": r["font_px"], "applied_align": r["align"], "align_source": r["align_source"],
                "db_align": (b["style"] or {}).get("align"),
                "style_align": s["align"] if s else None, "align_basis": s["align_basis"] if s else None,
                "align_null_reason": (s["null_reasons"] or {}).get("align") if s else None,
                "align_diag": s["align_diag"] if s else None, "style_status": s["status"] if s else None,
                "row_fragments": _row_fragments(b["source_lines"] or []),
                "section_centered": r["section_centered"], "center_shift": r["center_shift"], "cls": r["cls"],
                "source_ko": r["source_ko"], "trans_1": r["trans_1"],
            })
        _sheets(run, d, idx, [f for f in feats if f["gs"] == g], out / "sheets")
    feats.sort(key=lambda f: (f["gs"], f["section_id"], f["orig_bbox"]["y"], f["orig_bbox"]["x"]))
    return feats


def regroup(run: Path, feats: list[dict]) -> list[dict]:
    """row_fragments 블록만: 블록 구성은 그대로 두고 블록의 영역을 현재 develop ③ `merge.reading_lines`(xycut 가로줄 묶기,
    현재 config)로 다시 묶은 뒤 현재 ⑦ `style.estimate_align`으로 정렬을 다시 추정한다. 민감도 확인용 근사다 — 허용치의
    영역 높이 중앙값을 후보 영역이 아니라 블록 영역에서 구하고, gutter 분할 · OCR(타일 · 겹침 재인식) · LLM 병합 변경은 반영하지 않는다."""
    import tomllib
    from types import SimpleNamespace as NS

    from pipeline.stages import merge, style

    cfg = tomllib.loads((ROOT / "pipeline/config/default.toml").read_text(encoding="utf-8"))
    out = []
    blocks = {}
    for g in probe.GROUPS:
        for b in json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))["text_block"]:
            blocks[(g, b["id"])] = b
    for f in feats:
        if not f["row_fragments"]:
            continue
        b = blocks[(f["gs"], f["id"])]
        regs = [NS(region_key=r["region_key"], text=r["text"],
                   bbox=NS(x=r["bbox"]["x"], y=r["bbox"]["y"], w=r["bbox"]["w"], h=r["bbox"]["h"], x2=r["bbox"]["x"] + r["bbox"]["w"]))
                for ln in b["source_lines"] for r in ln["regions"]]
        rows = merge.reading_lines(regs, cfg["merge"])
        blk = NS(bbox=NS(w=b["bbox"]["w"]), source_lines=[NS(regions=rr) for _, rr in rows])
        ln = style.estimate_line_align(blk, cfg["style"]["align_tolerance"])  # 당시 규칙(현재는 줄 진단 — 2026-11-05)
        align, basis, reason = ln.align, ln.basis, ln.null_reason
        out.append({"gs": f["gs"], "id": f["id"], "src_lines": f["src_lines"], "regrouped_lines": len(rows),
                    "v101_align": f["applied_align"], "v101_basis": f["align_basis"] or f"null:{f['align_null_reason']}",
                    "regroup_align": align, "regroup_basis": basis or f"null:{reason}"})
    return out


def _sheets(run: Path, d: dict, idx: dict, feats: list[dict], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    secs = {s["id"]: s for s in d["section"]}
    by_sec = collections.defaultdict(list)
    for f in feats:
        by_sec[f["section_id"]].append(f)
    for sid, fs in by_sec.items():
        sec = secs[sid]
        im = Image.open(run / "objects" / idx[sec["image_key"]]["sha256"]).convert("RGB")
        dr = ImageDraw.Draw(im)
        font = ImageFont.load_default(size=max(14, im.width // 45))
        for f in fs:
            b = f["orig_bbox"]
            dr.rectangle((b["x"], b["y"], b["x"] + b["w"] - 1, b["y"] + b["h"] - 1), outline=(255, 0, 0), width=2)
            tx, ty = b["x"] + b["w"] + 2, b["y"]
            label = str(f["id"])
            tb = dr.textbbox((tx, ty), label, font=font)
            if tb[2] > im.width:
                tx = max(0, b["x"] - (tb[2] - tb[0]) - 4)
                tb = dr.textbbox((tx, ty), label, font=font)
            dr.rectangle(tb, fill=(255, 255, 0))
            dr.text((tx, ty), label, fill=(200, 0, 0), font=font)
        im.save(out / f"{f['gs']}_sec{sid}_orig.png")


def _x_positions(draw, f: dict, align: str, font) -> list[float]:
    lines, _ = t._wrap(draw, f["trans_1"], font, max(1, f["box"]["w"]))
    xs = []
    for line in lines:
        if not line:
            continue
        lw = draw.textlength(line, font=font)
        bx = f["box"]
        xs.append(bx["x"] + ((bx["w"] - lw) / 2 if align == "center" else (bx["w"] - lw) if align == "right" else 0))
    return xs


def aggregate(out: Path, labels_path: Path) -> dict:
    feats = json.loads((out / "features.json").read_text(encoding="utf-8"))
    with labels_path.open(encoding="utf-8", newline="") as fh:
        rd = list(csv.DictReader(fh, delimiter="\t"))
    labels = {}
    for r in rd:
        key = (r["gs"], int(r["id"]))
        if key in labels:
            raise SystemExit(f"라벨 중복: {key}")
        if r["label"] not in LABELS:
            raise SystemExit(f"라벨 값 오류: {key} {r['label']!r}")
        labels[key] = r
    missing = [(f["gs"], f["id"]) for f in feats if (f["gs"], f["id"]) not in labels]
    extra = set(labels) - {(f["gs"], f["id"]) for f in feats}
    if missing or extra:
        raise SystemExit(f"라벨 누락 {len(missing)} · 대상 밖 {len(extra)}")
    defaults = t.load_role_defaults()
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    rows = []
    for f in feats:
        lab = labels[(f["gs"], f["id"])]
        r = {**{k: f[k] for k in ("gs", "id", "section_id", "role", "src_lines", "tr_lines", "same_text", "applied_align",
                                   "align_source", "align_basis", "align_null_reason", "row_fragments", "section_centered", "cls")},
             "label": lab["label"], "basis_note": lab.get("basis_note", ""), "source_ko": f["source_ko"], "trans_1": f["trans_1"]}
        r["evaluable"] = lab["label"] != "unknown"
        r["error"] = r["evaluable"] and lab["label"] != f["applied_align"]
        if r["error"]:
            font = t._font(defaults, f["role"], f["font_px"])
            a, b = _x_positions(draw, f, f["applied_align"], font), _x_positions(draw, f, lab["label"], font)
            shift = max((abs(x - y) for x, y in zip(a, b)), default=0.0)
            r["shift_px"], r["shift_section_ratio"] = round(shift, 1), round(shift / f["section_w"], 4)
        rows.append(r)
    C = collections.Counter
    ev = [r for r in rows if r["evaluable"]]
    err = [r for r in rows if r["error"]]

    def basis(r):
        return r["align_basis"] or f"null:{r['align_null_reason']}"

    def by(sub, key):
        return {str(k): v for k, v in sorted(C(key(r) for r in sub).items(), key=lambda kv: str(kv[0]))}

    shifts = sorted(r["shift_section_ratio"] for r in err)
    summary = {
        "blocks": len(rows), "evaluable": len(ev), "unknown": len(rows) - len(ev), "errors": len(err),
        "label": by(rows, lambda r: r["label"]),
        "by_basis": {k: {"blocks": sum(basis(r) == k for r in rows), "evaluable": sum(basis(r) == k for r in ev),
                         "errors": sum(basis(r) == k for r in err)} for k in sorted({basis(r) for r in rows})},
        "by_src_lines": {k: {"blocks": sum((r["src_lines"] >= 2) == (k == "multi") for r in rows),
                             "evaluable": sum((r["src_lines"] >= 2) == (k == "multi") for r in ev),
                             "errors": sum((r["src_lines"] >= 2) == (k == "multi") for r in err)} for k in ("single", "multi")},
        "error_pairs": by(err, lambda r: f"{r['label']}->{r['applied_align']}"),
        "error_basis": by(err, basis),
        "error_row_fragments": sum(r["row_fragments"] > 0 for r in err),
        "row_fragment_blocks": {"blocks": sum(r["row_fragments"] > 0 for r in rows),
                                "evaluable": sum(r["row_fragments"] > 0 for r in ev),
                                "errors": sum(r["row_fragments"] > 0 for r in err)},
        "error_shift_section_ratio_quantiles": [shifts[int(q * (len(shifts) - 1))] for q in (0, .25, .5, .75, 1)] if shifts else [],
        "error_shift_ge_2pct": sum(s >= 0.02 for s in shifts),
        "section_centered_left": {"blocks": sum(r["section_centered"] and r["applied_align"] == "left" for r in rows),
                                  "errors": sum(r["section_centered"] and r["applied_align"] == "left" for r in err)},
    }
    (out / "align_rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "align_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def crops(run: Path, out: Path, picks: list[str], name: str) -> None:
    """원본 | 렌더 대조 시트(빨간 상자 = 배치 영역). 캡션에 라벨 · 적용 정렬 · 근거를 적는다 — 맹검 라벨 뒤 확인용."""
    rows = {(r["gs"], r["id"]): r for r in json.loads((out / "align_rows.json").read_text(encoding="utf-8"))}
    feats = {(f["gs"], f["id"]): f for f in json.loads((out / "features.json").read_text(encoding="utf-8"))}
    idx = json.loads((run / "object-index.json").read_text(encoding="utf-8"))
    secs = {}
    for g in probe.GROUPS:
        secs[g] = {s["id"]: s for s in json.loads((run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))["section"]}
    tiles = []
    for p in picks:
        g, bid = p.split(":")
        r, f = rows[(g, int(bid))], feats[(g, int(bid))]
        sec = secs[g][f["section_id"]]
        org, ren = (Image.open(run / "objects" / idx[k]["sha256"]).convert("RGB") for k in (sec["image_key"], sec["render_image_key"]))
        b, pad = f["box"], max(30, f["box"]["h"])
        c = (0, max(0, b["y"] - pad), org.width, min(org.height, b["y"] + b["h"] + pad))
        a, z = org.crop(c), ren.crop(c)
        for im in (a, z):
            ImageDraw.Draw(im).rectangle((b["x"], b["y"] - c[1], b["x"] + b["w"] - 1, b["y"] - c[1] + b["h"] - 1), outline=(255, 0, 0), width=2)
        tile = Image.new("RGB", (a.width * 2 + 10, a.height), "white")
        tile.paste(a, (0, 0))
        tile.paste(z, (a.width + 10, 0))
        tile.thumbnail((1000, 320))
        tiles.append((f"{g} #{bid} label={r['label']} applied={r['applied_align']} basis={r['align_basis'] or r['align_null_reason']} "
                      f"src_lines={r['src_lines']} frag={r['row_fragments']}", tile))
    sheet = Image.new("RGB", (1000, sum(tl.height + 24 for _, tl in tiles)), "white")
    d, y = ImageDraw.Draw(sheet), 0
    for label, tl in tiles:
        d.text((4, y + 4), label, fill=(0, 0, 0))
        sheet.paste(tl, (0, y + 20))
        y += tl.height + 24
    (out / "crops").mkdir(exist_ok=True)
    sheet.save(out / "crops" / f"{name}.png")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crops")
    c.add_argument("--run", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--name", required=True)
    c.add_argument("pick", nargs="+", help="GS-01:186 형식")
    f = sub.add_parser("features")
    f.add_argument("--run", type=Path, required=True)
    f.add_argument("--out", type=Path, required=True)
    g = sub.add_parser("aggregate")
    g.add_argument("--out", type=Path, required=True)
    g.add_argument("--labels", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    if a.cmd == "crops":
        crops(a.run, a.out, a.pick, a.name)
    elif a.cmd == "features":
        feats = features(a.run, a.out)
        (a.out / "features.json").write_text(json.dumps(feats, ensure_ascii=False, indent=1), encoding="utf-8")
        rg = regroup(a.run, feats)
        (a.out / "regroup.json").write_text(json.dumps(rg, ensure_ascii=False, indent=1), encoding="utf-8")
        C = collections.Counter
        print(json.dumps({"regroup": dict(C(f"{r['v101_align']}/{r['v101_basis']} -> {r['regroup_align']}/{r['regroup_basis']}"
                                           for r in rg))}, ensure_ascii=False, indent=1))
        print(json.dumps({"blocks": len(feats),
                          "basis": dict(C(f["align_basis"] or f"null:{f['align_null_reason']}" for f in feats)),
                          "applied": dict(C(f"{f['applied_align']}/{f['align_source']}" for f in feats)),
                          "db_vs_applied": sum(f["db_align"] in (None, f["applied_align"]) for f in feats),
                          "row_fragments": sum(f["row_fragments"] > 0 for f in feats),
                          "src_lines": dict(C(min(f["src_lines"], 3) for f in feats))}, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(aggregate(a.out, a.labels), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
