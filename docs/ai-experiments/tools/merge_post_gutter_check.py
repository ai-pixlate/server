"""③ OCR 후처리 점검 — 같은 후보 영역 안의 칸(gutter · 높이비로 갈린 같은 가로줄)을 LLM이 합칠 때 줄 순서가 섞이는 위험
(open-questions #86, pipeline.md 7.2절). 새 LLM 호출 없이 고정 입력본만 쓴다.

사용:
    python docs/ai-experiments/tools/merge_post_gutter_check.py --input pipeline/samples/local/blocks-input-v1 --out RUN_DIR

점검 1(노출): 섹션마다 `merge.xycut=true` 휴리스틱(LLM 없음)을 돌려, 같은 후보 영역 안에서 가로줄 범위가 교차하는 블록 쌍을
센다. 이런 쌍을 LLM이 한 묶음으로 합치면 줄이 (가로줄 순번, x)로 다시 정렬돼 칸끼리 번갈아 섞인다. 쌍이 공유하는 가로줄에서
두 블록 조각 사이에 gutter가 있으면 `gutter`, 없으면 `h_ratio`(높이비로 갈림)로 분류한다.
점검 2(대리 지표): 입력본의 ③ 최종 블록(후처리 끔 · llm_assist v1 실제 출력 1회)을 "그 실행이 한 문단으로 본 범위"로 보고,
그 영역들이 xycut 휴리스틱에서 같은 후보 영역의 교차 블록 여러 개에 걸치면 — xycut에서도 LLM이 같은 판단을 하면 섞일 사례 —
xycut 재조립 결과(merge.assemble)와 끈 상태의 원문을 나란히 남긴다. 후보 영역을 넘는 경우(분리 대상)도 센다.
LLM 출력은 정답이 아니며, 섞인 순서가 틀렸는지는 사람이 섹션 이미지로 판단한다(cases.md).
출력: RUN_DIR/summary.json, RUN_DIR/cases.json, RUN_DIR/cases.md. 입력본은 읽기만 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import merge  # noqa: E402


def region_keys(b) -> list[str]:
    return [r.region_key for ln in b.source_lines for r in ln.regions]


def row_span(b, layout) -> tuple[int, int]:
    rows = [layout.line_rank[ln.line_key][0] for ln in b.source_lines]
    return min(rows), max(rows)


def split_cause(a, b, layout, gutters) -> str:
    """두 블록이 공유하는 가로줄에서 이웃 조각 사이에 gutter가 있으면 gutter, 아니면 h_ratio. 공유 가로줄이 없으면 nested."""
    rows_a = {layout.line_rank[ln.line_key][0]: ln for ln in a.source_lines}
    rows_b = {layout.line_rank[ln.line_key][0]: ln for ln in b.source_lines}
    shared = sorted(set(rows_a) & set(rows_b))
    if not shared:
        return "nested"  # 범위만 교차(한 블록이 다른 블록의 줄 사이에 끼임)
    for row in shared:
        la, lb = sorted((rows_a[row], rows_b[row]), key=lambda ln: ln.bbox.x)
        if any(la.bbox.x2 <= g0 and g1 <= lb.bbox.x for g0, g1 in gutters):
            return "gutter"
    return "h_ratio"


def check_section(sec, ocr_res, final, cfg_on) -> tuple[dict, list[dict]]:
    m = cfg_on["merge"]
    heur, layout, _, _ = merge._heuristic(sec, ocr_res, cfg_on)
    gutter_regions = [r for r in ocr_res.regions if merge.region_kind(r) in ("text", "empty")]
    gutters = merge.find_gutters(gutter_regions, sec.width, m) if m["gutter"] else []
    by_key = {b.block_key: b for b in heur.blocks}
    sendable = [b for b in heur.blocks if merge._sendable(b)]
    pairs: list[dict] = []
    for i, a in enumerate(sendable):
        for b in sendable[i + 1:]:
            if layout.block_region[a.block_key] != layout.block_region[b.block_key]:
                continue
            (a0, a1), (b0, b1) = row_span(a, layout), row_span(b, layout)
            if a0 <= b1 and b0 <= a1:
                pairs.append({"blocks": [a.block_key, b.block_key], "region": layout.block_region[a.block_key],
                              "cause": split_cause(a, b, layout, gutters), "texts": [a.source_ko, b.source_ko]})
    owner = {rk: b.block_key for b in heur.blocks for rk in region_keys(b)}
    cases: list[dict] = []
    cross = 0
    for fb in final.blocks:
        members = sorted({owner[rk] for rk in region_keys(fb)}, key=lambda k: by_key[k].block_order)
        if len(members) < 2 or not all(merge._sendable(by_key[k]) for k in members):
            continue
        regions = {layout.block_region[k] for k in members}
        if len(regions) > 1:
            cross += 1
            continue
        spans = sorted(row_span(by_key[k], layout) for k in members)
        if not any(nxt[0] <= cur[1] for cur, nxt in zip(spans, spans[1:])):
            continue
        ids = {f"b{i}": by_key[k] for i, k in enumerate(members, start=1)}
        res, _, notes = merge.assemble(sec, heur, ids, [(list(ids), fb.role)], layout, m)
        merged = next(b for b in res.blocks if set(region_keys(b)) >= set(region_keys(by_key[members[0]])))
        causes = sorted({split_cause(by_key[x], by_key[y], layout, gutters)
                         for i, x in enumerate(members) for y in members[i + 1:]
                         if (lambda s, t: s[0] <= t[1] and t[0] <= s[1])(row_span(by_key[x], layout), row_span(by_key[y], layout))})
        # exact: xycut 블록들의 영역 합이 끈 상태 문단과 정확히 같다(같은 범위의 순서만 비교 가능). 아니면 범위 자체가 다르다
        exact = set(region_keys(merged)) == set(region_keys(fb))
        cases.append({"final_block": fb.block_key, "off_text": fb.source_ko, "xycut_blocks": members,
                      "xycut_block_texts": [by_key[k].source_ko for k in members], "xycut_merged_text": merged.source_ko,
                      "causes": causes, "exact_cover": exact,
                      "same_text_order": merged.source_ko.split() == fb.source_ko.split(),
                      "bbox": [merged.bbox.x, merged.bbox.y, merged.bbox.w, merged.bbox.h],
                      "regions": [[r.region_key, r.bbox.x, r.bbox.y, r.bbox.w, r.bbox.h]
                                  for ln in merged.source_lines for r in ln.regions],
                      "off_regions": region_keys(fb)})
    stat = {"blocks_off": len(final.blocks), "blocks_xycut": len(heur.blocks), "candidate_regions": layout.n_regions,
            "interleaving_pairs": len(pairs), "pairs_by_cause": {c: sum(p["cause"] == c for p in pairs)
                                                                 for c in ("gutter", "h_ratio", "nested")},
            "final_blocks_cross_region": cross, "proxy_cases": len(cases)}
    return {"stat": stat, "pairs": pairs}, cases


def draw_case(image_path: Path, case: dict, dest: Path, margin: int = 40) -> str:
    """사례 블록 둘레를 잘라 영역마다 xycut 재조립 순번(빨강)과 끈 상태 순번(파랑)을 적는다. 사람 확인용."""
    from PIL import Image, ImageDraw

    x, y, w, h = case["bbox"]
    with Image.open(image_path) as im:
        box = (max(0, x - margin), max(0, y - margin), min(im.width, x + w + margin), min(im.height, y + h + margin))
        crop = im.convert("RGB").crop(box)
    d = ImageDraw.Draw(crop)
    off_pos = {k: i for i, k in enumerate(case["off_regions"], start=1)}
    for i, (key, rx, ry, rw, rh) in enumerate(case["regions"], start=1):
        x0, y0 = rx - box[0], ry - box[1]
        d.rectangle([x0, y0, x0 + rw, y0 + rh], outline=(220, 0, 0), width=2)
        d.text((x0 + 2, y0 - 11), f"{i}", fill=(220, 0, 0))
        if key in off_pos:
            d.text((x0 + rw - 14, y0 - 11), f"{off_pos[key]}", fill=(0, 0, 220))
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
    cfg_on = cfgmod.load_config()
    cfg_on["merge"]["xycut"] = True
    merge.validate_config(cfg_on)
    sections: dict[str, dict] = {}
    all_cases: list[dict] = []
    for img in sorted(p for p in inp.iterdir() if p.is_dir() and (p / "split.json").is_file()):
        split = jsonio.load_split(img / "split.json")
        for ocr_path in sorted((img / "ocr").glob("*.json")):
            ocr_res = jsonio.load_ocr(ocr_path)
            sec = next(s for s in split.sections if s.section_key == ocr_res.section_key)
            final = jsonio.load_merge(img / "merge" / ocr_path.name)
            res, cases = check_section(sec, ocr_res, final, cfg_on)
            name = f"{img.name}/{sec.section_key}"
            sections[name] = res
            for n, c in enumerate(cases, start=1):
                c["crop"] = draw_case(img / sec.image_path, c, out / "crops" / f"{img.name}_{sec.section_key}_{n}.png")
            all_cases += [{"section": name, **c} for c in cases]
    stats = [s["stat"] for s in sections.values()]
    summary = {
        "input": str(inp), "sections": len(sections),
        "sections_with_interleaving_pairs": sum(s["interleaving_pairs"] > 0 for s in stats),
        "interleaving_pairs": sum(s["interleaving_pairs"] for s in stats),
        "pairs_by_cause": {c: sum(s["pairs_by_cause"][c] for s in stats) for c in ("gutter", "h_ratio", "nested")},
        "blocks_off": sum(s["blocks_off"] for s in stats), "blocks_xycut": sum(s["blocks_xycut"] for s in stats),
        "candidate_regions": sum(s["candidate_regions"] for s in stats),
        "final_blocks_cross_region": sum(s["final_blocks_cross_region"] for s in stats),
        "proxy_cases": len(all_cases),
        "proxy_cases_exact_cover": sum(c["exact_cover"] for c in all_cases),
        "proxy_cases_exact_same_text_order": sum(c["exact_cover"] and c["same_text_order"] for c in all_cases),
        "proxy_cases_by_cause": {c: sum(c in x["causes"] for x in all_cases) for c in ("gutter", "h_ratio", "nested")},
        "proxy_cases_exact_by_cause": {c: sum(c in x["causes"] for x in all_cases if x["exact_cover"])
                                       for c in ("gutter", "h_ratio", "nested")},
    }
    (out / "summary.json").write_text(json.dumps({"summary": summary, "sections": sections}, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    (out / "cases.json").write_text(json.dumps(all_cases, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 대리 사례 — 끈 상태 최종 문단이 xycut에서 같은 영역의 교차 블록에 걸침", "",
             "crop: 빨강 = xycut 재조립 순번, 파랑 = 끈 상태 순번. 같은 범위(exact)가 아니면 순서 비교가 아니라 범위 차이다.", ""]
    for c in all_cases:
        lines += [f"## {c['section']} {c['final_block']} ({', '.join(c['causes'])}"
                  f"{'' if c['exact_cover'] else ' · 범위 다름'}{' · 순서 같음' if c['same_text_order'] else ''})", "",
                  f"crop: `{c['crop']}`", "",
                  "끔(off) 원문:", "```", c["off_text"], "```", "xycut 재조립:", "```", c["xycut_merged_text"], "```", ""]
    (out / "cases.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
