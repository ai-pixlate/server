"""③ OCR 후처리 켬/끔 비교 — LLM 없이 휴리스틱만으로 블록 수 · 역할 · 영역 분리 차이를 본다(open-questions #86, 3단계 연결 검증).

사용:
    python docs/ai-experiments/tools/merge_post_compare.py --input pipeline/samples/local/blocks-input-v1 --out RUN_DIR

섹션마다 같은 OCR로 `merge.xycut=false`(끔)와 `true`(켬) 휴리스틱을 돌려 비교한다. 새 LLM 호출 없음, 입력본은 읽기만 한다.
- 불변식(켬): 모든 원시 영역이 정확히 한 블록에 한 번 · 값 불변 · block_order 1부터 연속 · 입력 순서를 섞어도 결과 같음.
- 대응: 끔 블록과 켬 블록을 공유 원시 영역으로 이은 연결 성분마다 `same`(영역 집합 같음, 1:1) · `split`(끔 1 → 켬 여럿) ·
  `merge`(끔 여럿 → 켬 1) · `mixed`(여럿 ↔ 여럿)로 분류한다. split은 켬 블록들이 서로 다른 후보 영역에 있으면 `split_cut`
  (XY-cut이 끔 문단을 잘랐다 — 과분할 후보), 같은 영역이면 `split_inner`(가로줄 · 높이비 · gutter · 블록 병합 차이)로 나눈다.
- same 블록의 텍스트 · 줄 수 · 역할 차이, 블록 순서 바뀜(same 블록끼리의 상대 순서 역전 쌍), 역할 분포.
출력: RUN_DIR/summary.json · RUN_DIR/cases.json · RUN_DIR/cases.md · RUN_DIR/crops/(split_cut · merge · mixed · 역할 바뀐 same).
켬 결과는 정답이 아니며 끔 결과도 정답이 아니다(`blocks-input-v1`은 정답 데이터가 아님, #57). 어느 쪽이 맞는지는 사람이 본다.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import merge  # noqa: E402
from pipeline.types import OcrResult  # noqa: E402

FONT = ImageFont.load_default(size=14)
CROP_KINDS = ("split_cut", "merge", "mixed", "role_changed")


def rkeys(b) -> list[str]:
    return [r.region_key for ln in b.source_lines for r in ln.regions]


def check_invariants(sec, ocr_res: OcrResult, on, cfg_on) -> list[str]:
    problems = []
    out = [r for b in on.blocks for ln in b.source_lines for r in ln.regions]
    if Counter(r.region_key for r in out) != Counter(r.region_key for r in ocr_res.regions):
        problems.append("영역이 정확히 한 번이 아니다")
    by_key = {r.region_key: r for r in ocr_res.regions}
    if any(r.model_dump() != by_key[r.region_key].model_dump() for r in out):
        problems.append("원시 영역 값이 바뀌었다")
    if [b.block_order for b in on.blocks] != list(range(1, len(on.blocks) + 1)):
        problems.append("block_order가 연속이 아니다")
    shuffled = list(ocr_res.regions)
    random.Random(7).shuffle(shuffled)
    again = merge.heuristic(sec, OcrResult(section_key=ocr_res.section_key, regions=shuffled), cfg_on)
    if again != on:
        problems.append("입력 순서에 따라 결과가 다르다")
    return problems


def components(off, on) -> list[tuple[list, list]]:
    """공유 영역으로 이은 (끔 블록들, 켬 블록들) 연결 성분. 끔 블록 순서로 정렬."""
    off_of = {rk: b.block_key for b in off.blocks for rk in rkeys(b)}
    on_of = {rk: b.block_key for b in on.blocks for rk in rkeys(b)}
    adj: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for rk in off_of:
        a, b = ("off", off_of[rk]), ("on", on_of[rk])
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    seen: set[tuple[str, str]] = set()
    off_by, on_by = {b.block_key: b for b in off.blocks}, {b.block_key: b for b in on.blocks}
    comps = []
    for b in off.blocks:
        start = ("off", b.block_key)
        if start in seen:
            continue
        stack, members = [start], []
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            members.append(n)
            stack += adj.get(n, ())
        offs = sorted((off_by[k] for s, k in members if s == "off"), key=lambda x: x.block_order)
        ons = sorted((on_by[k] for s, k in members if s == "on"), key=lambda x: x.block_order)
        comps.append((offs, ons))
    return comps


def kind_of(offs, ons, layout) -> str:
    if len(offs) == 1 and len(ons) == 1:
        return "same"
    if len(offs) == 1:
        return "split_cut" if len({layout.block_region[b.block_key] for b in ons}) > 1 else "split_inner"
    if len(ons) == 1:
        return "merge"
    return "mixed"


def inversions(same_pairs, layout) -> tuple[int, int, set[str]]:
    """same 블록끼리 끔 순서와 켬 순서가 뒤바뀐 쌍 수 (서로 다른 후보 영역 사이, 같은 후보 영역 안)와 관련된 켬 블록 키."""
    seq = [b_on for _, b_on in sorted(same_pairs, key=lambda p: p[0].block_order)]
    cross = inner = 0
    moved: set[str] = set()
    for i in range(len(seq)):
        for j in range(i + 1, len(seq)):
            if seq[i].block_order > seq[j].block_order:
                same_region = layout.block_region[seq[i].block_key] == layout.block_region[seq[j].block_key]
                inner += same_region
                cross += not same_region
                moved |= {seq[i].block_key, seq[j].block_key}
    return cross, inner, moved


def draw_order(src: Path, off, on, layout, dest: Path, width: int = 520) -> str:
    """섹션 전체 축소: 켬 블록마다 빨강 x<켬 순서>/o<끔 순서>(같은 영역 집합일 때), 후보 영역 경계는 초록."""
    off_of = {frozenset(rkeys(b)): b.block_order for b in off.blocks}
    with Image.open(src) as im:
        sec = im.convert("RGB")
    s = width / sec.width
    img = sec.resize((width, max(1, round(sec.height * s))), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    for region in range(1, layout.n_regions + 1):
        bs = [b for b in on.blocks if layout.block_region[b.block_key] == region]
        if bs:
            x0, y0 = min(b.bbox.x for b in bs), min(b.bbox.y for b in bs)
            x1, y1 = max(b.bbox.x + b.bbox.w for b in bs), max(b.bbox.y + b.bbox.h for b in bs)
            d.rectangle([x0 * s - 3, y0 * s - 3, x1 * s + 3, y1 * s + 3], outline=(0, 160, 0), width=1)
    for b in on.blocks:
        x, y = b.bbox.x * s, b.bbox.y * s
        d.rectangle([x, y, x + b.bbox.w * s, y + b.bbox.h * s], outline=(220, 0, 0), width=1)
        o = off_of.get(frozenset(rkeys(b)))
        text = f"x{b.block_order}" + (f"/o{o}" if o is not None else "")
        l, t, r, bt = d.textbbox((x + 1, y + 1), text, font=FONT)
        d.rectangle([l - 1, t - 1, r + 1, bt + 1], fill=(255, 255, 255))
        d.text((x + 1, y + 1), text, fill=(220, 0, 0), font=FONT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return str(dest)


def draw(src: Path, offs, ons, dest: Path, margin: int = 30) -> str:
    """파랑 = 끔 블록(번호 = 끔 block_order), 빨강 = 켬 블록(번호 = 켬 block_order)."""
    boxes = [b.bbox for b in offs + ons]
    x0, y0 = min(b.x for b in boxes), min(b.y for b in boxes)
    x1, y1 = max(b.x + b.w for b in boxes), max(b.y + b.h for b in boxes)
    with Image.open(src) as im:
        sec = im.convert("RGB")
    box = (max(0, x0 - margin), max(0, y0 - margin), min(sec.width, x1 + margin), min(sec.height, y1 + margin))
    crop = sec.crop(box)
    k = 2 if crop.width < 400 else 1
    if k > 1:
        crop = crop.resize((crop.width * k, crop.height * k), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    for blocks, color, tag, dx in ((offs, (0, 0, 220), "o", 0), (ons, (220, 0, 0), "x", 3)):
        for b in blocks:
            bx, by = (b.bbox.x - box[0]) * k + dx, (b.bbox.y - box[1]) * k + dx
            d.rectangle([bx, by, bx + b.bbox.w * k, by + b.bbox.h * k], outline=color, width=2)
            text = f"{tag}{b.block_order}"
            l, t, r, bt = d.textbbox((bx + 2, by + 2), text, font=FONT)
            d.rectangle([l - 1, t - 1, r + 1, bt + 1], fill=(255, 255, 255))
            d.text((bx + 2, by + 2), text, fill=color, font=FONT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest)
    return str(dest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True, help="blocks-input-v1 폴더")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    inp, out = Path(a.input), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg_off = cfgmod.load_config()
    cfg_on = cfgmod.load_config()
    cfg_on["merge"]["xycut"] = True
    merge.validate_config(cfg_off)
    merge.validate_config(cfg_on)
    kinds: Counter = Counter()
    roles_off: Counter = Counter()
    roles_on: Counter = Counter()
    role_changes: Counter = Counter()
    totals = Counter()
    problems: dict[str, list[str]] = {}
    cases: list[dict] = []
    per_section: dict[str, dict] = {}
    for img in sorted(p for p in inp.iterdir() if p.is_dir() and (p / "split.json").is_file()):
        split = jsonio.load_split(img / "split.json")
        for ocr_path in sorted((img / "ocr").glob("*.json")):
            ocr_res = jsonio.load_ocr(ocr_path)
            sec = next(s for s in split.sections if s.section_key == ocr_res.section_key)
            src = img / sec.image_path
            name = f"{img.name}/{sec.section_key}"
            off = merge.heuristic(sec, ocr_res, cfg_off)
            on, layout, _, _ = merge._heuristic(sec, ocr_res, cfg_on)
            p = check_invariants(sec, ocr_res, on, cfg_on)
            if p:
                problems[name] = p
            roles_off.update(b.role for b in off.blocks)
            roles_on.update(b.role for b in on.blocks)
            same_pairs = []
            sec_kinds: Counter = Counter()
            for n, (offs, ons) in enumerate(components(off, on), start=1):
                kind = kind_of(offs, ons, layout)
                sec_kinds[kind] += 1
                detail = {}
                if kind == "same":
                    b0, b1 = offs[0], ons[0]
                    same_pairs.append((b0, b1))
                    detail = {"text_same": b0.source_ko == b1.source_ko, "lines": [len(b0.source_lines), len(b1.source_lines)],
                              "role": [b0.role, b1.role]}
                    totals["same_text_diff"] += b0.source_ko != b1.source_ko
                    totals["same_lines_diff"] += len(b0.source_lines) != len(b1.source_lines)
                    if b0.role != b1.role:
                        totals["same_role_diff"] += 1
                        role_changes[f"{b0.role}→{b1.role}"] += 1
                        kind_rec = "role_changed"
                    else:
                        kind_rec = None
                else:
                    kind_rec = kind if kind in CROP_KINDS else None
                if kind_rec or (kind != "same"):
                    case = {"section": name, "kind": kind if kind != "same" else "role_changed",
                            "off": [{"order": b.block_order, "role": b.role, "text": b.source_ko} for b in offs],
                            "on": [{"order": b.block_order, "role": b.role, "region": layout.block_region[b.block_key],
                                    "text": b.source_ko} for b in ons], **detail}
                    if case["kind"] in CROP_KINDS:
                        case["crop"] = draw(src, offs, ons, out / "crops" / f"{img.name}_{sec.section_key}_{n}_{case['kind']}.png")
                    cases.append(case)
            cross, inner, moved = inversions(same_pairs, layout)
            inv = cross + inner
            totals["order_inverted_pairs"] += inv
            totals["order_inverted_pairs_cross_region"] += cross
            totals["order_inverted_pairs_same_region"] += inner
            totals["sections_with_inversion"] += inv > 0
            totals["sections_with_inner_inversion"] += inner > 0
            totals["same_blocks_moved"] += len(moved)
            order_crop = None
            if inv:
                order_crop = draw_order(src, off, on, layout, out / "order" / f"{img.name}_{sec.section_key}.png")
            totals["blocks_off"] += len(off.blocks)
            totals["blocks_on"] += len(on.blocks)
            totals["candidate_regions"] += layout.n_regions
            totals["sections"] += 1
            totals["sections_block_count_changed"] += len(off.blocks) != len(on.blocks)
            kinds.update(sec_kinds)
            per_section[name] = {"blocks": [len(off.blocks), len(on.blocks)], "regions": layout.n_regions,
                                 "kinds": dict(sec_kinds), "order_inverted_pairs": [cross, inner], "order_crop": order_crop}
    summary = {"input": str(inp), **totals, "components": dict(kinds), "roles_off": dict(roles_off),
               "roles_on": dict(roles_on), "same_role_changes": dict(role_changes),
               "invariant_problems": problems}
    (out / "summary.json").write_text(json.dumps({"summary": summary, "sections": per_section}, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    (out / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 켬/끔 차이 사례(LLM 없음)", "", "crop: 파랑 o = 끔 블록 순서, 빨강 x = 켬 블록 순서. 어느 쪽도 정답이 아니다.", ""]
    for kind in ("split_cut", "merge", "mixed", "role_changed", "split_inner"):
        sel = [c for c in cases if c["kind"] == kind]
        lines += [f"## {kind} ({len(sel)})", ""]
        for c in sel:
            lines += [f"### {c['section']}" + (f" — 역할 {c['role'][0]} → {c['role'][1]}" if kind == "role_changed" else ""), ""]
            if c.get("crop"):
                lines += [f"crop: `{c['crop']}`", ""]
            lines += ["끔:", "```", *[f"[o{x['order']} {x['role']}] {x['text']}" for x in c["off"]], "```",
                      "켬:", "```", *[f"[x{x['order']} {x['role']} r{x['region']}] {x['text']}" for x in c["on"]], "```", ""]
    (out / "cases.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
