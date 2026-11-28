"""⑦ 블록 안 스타일 차이 집계(2026-11-26) — 개발용, 운영 경로 아님. 운영 코드 · config · 저장된 결과는 바꾸지 않는다.

    python docs/ai-experiments/tools/style_intra_block_diff.py \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 --gs GS-01 GS-02 \
        --out docs/ai-experiments/runs/2026-11-26_07-style_intra-block-diff \
        --size-thr 1.2 1.5 2.0 --color-thr 30 60 100 150

질문: ③이 병합한 블록 하나 안에서 ⑦ 영역별 측정값(글자색 · 배경색 · 크기)이 얼마나, 어디서(줄 사이 / 줄 안) 다른가.
⑦은 블록 대표값 하나만 내고 ⑧ 번역 · ⑨ 렌더가 그 값 하나를 블록 전체에 쓴다 — 그 뭉뚱그림의 빈도와 유형을 센다. 집계만 한다.

입력(읽기만): 베이스라인 회차의 `evidence/<GS>.json`(DB 행 보존본 — `text_block`의 역할 · `source_lines` 영역 bbox · 텍스트,
`task_handoff`의 ⑦ 인계 payload — 블록 → 영역별 `font_color` · `bg_color` · `est_font_px` · `otsu` · `line_key`).
⑦ 결과는 당시 현재(`is_current`) · 완료(`done`) 시도만 쓴다. 새로 측정하지 않는다.

대상: ⑦ 블록 상태가 `excluded`(라벨 · 로고)가 아니고 측정 영역(`status == "measured"`)이 2개 이상인 블록.
측정 영역 0 · 1개 블록은 수만 센다. null은 추정해 채우지 않고 항목별로 센다(그 항목의 차이 계산에서만 빠진다).

블록별 차이
- 크기: 영역 `est_font_px`의 최대 / 최소 비율, 블록 대표값(⑦ `est_font_px`) 대비 최대 상대 편차 |영역 − 대표| / 대표.
- 글자색 · 배경색: 영역 색 쌍의 최대 거리. 기본은 sRGB 유클리드(0 ~ 441.7 — ⑦이 대표색을 고를 때 쓰는 거리), 보조로 Lab ΔE(CIE76, D65).

위치(항목 · 기준값마다) — "다르다" = 크기 비율 ≥ 기준, 색 거리 ≥ 기준(경계 포함)
- 줄 안(within): 같은 `line_key`의 측정 영역 쌍 중 다른 쌍이 있다.
- 줄 사이(between): 줄 대표값끼리 다르다. 줄 대표값은 ⑦ 블록 대표값과 같은 방식 — 크기는 중앙값(`median_fraction`), 색은 채널별
  중앙값에 가장 가까운 실제 영역 색(`representative_color`). 운영 함수를 그대로 부른다.
- 영역 쌍만(pair_only): 블록 전체 영역 쌍은 기준을 넘지만 위 둘에는 해당하지 않는다(줄 대표값이 차이를 가린 경우).
- 유형: none · between · within · both · pair_only. 조판에 쓰이는 두 항목(크기 · 글자색)을 합친 유형은 두 항목의 위치를 합쳐 정한다.
  배경색은 조판에 쓰지 않으므로(contract.md 11.1) 합친 유형에 넣지 않고 따로 센다.

측정 오류 의심 표시 — **이 도구가 정한 휴리스틱이다. 정본 기준이 아니며 "의심"일 뿐 오류 판정이 아니다.** 값은 아래 상수.
- 크기 쪽: `script_mix`(차이 나는 영역 쌍의 한쪽만 한글 포함 — bbox 높이가 글자 종류에 따라 달라 같은 크기도 다르게 잡힌다, open-questions #85) ·
  `short_text`(영역 글자 2자 이하) · `low_score`(OCR 점수 < 0.8).
- 색 쪽: `polarity_swap`(한 영역의 글자색 ↔ 다른 영역의 배경색이 서로 가깝다 — Otsu 전경 · 배경 뒤바뀜 의심) ·
  `low_contrast`(영역의 글자색 − 배경색 거리 < 60) · `minor_class`(글자 쪽 Otsu 집단이 영역 픽셀의 3% 미만) · `low_score`.
의심 표시는 "그 기준에서 차이를 만든 영역 쌍" 가운데 하나라도 해당하면 블록에 붙인다.

출력: `<out>/summary.json`(집계) · `<out>/blocks.json`(대상 블록별 영역 · 차이 · 의심 표시). 표준 출력에 요약.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline.stages.style import median_fraction, representative_color  # noqa: E402

ROLES = ("title", "body", "caption", "price", "caution")
ITEMS = ("size", "font_color", "bg_color")
TYPES = ("none", "between", "within", "both", "pair_only")
# 의심 표시 상수(휴리스틱 — 머리말)
LOW_SCORE = 0.8
SHORT_TEXT = 2
LOW_CONTRAST = 60.0
SWAP_NEAR = 60.0
MINOR_CLASS = 0.03
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")


def rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)


def dist(a: str, b: str) -> float:
    """sRGB 유클리드 거리(0 ~ 441.7)."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(rgb(a), rgb(b))))


def lab(hex_color: str) -> tuple[float, float, float]:
    """sRGB → CIE Lab(D65)."""
    def lin(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb(hex_color))
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(a: str, b: str) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(lab(a), lab(b))))


def hex_of(c: tuple[int, int, int]) -> str:
    return "#%02X%02X%02X" % c


def style_payloads(d: dict) -> dict[int, dict]:
    """section_id → ⑦ 결과(당시 현재 · 완료 시도만)."""
    tasks = {a["id"]: a for a in d["job_async_task"]}
    out: dict[int, dict] = {}
    for h in d["task_handoff"]:
        a = tasks.get(h["task_id"])
        p = h.get("payload") or {}
        if a and a["stage"] == "style" and a["is_current"] and h["outcome"] == "done" and p.get("style"):
            out[int(a["unit_id"])] = p["style"]
    return out


def region_rows(tb: dict, sb: dict) -> list[dict]:
    """⑦ 영역 측정값에 DB `source_lines`의 bbox · 텍스트를 붙인다(region_key로 대응, 순서는 ⑦ 결과 그대로)."""
    geo = {r["region_key"]: (li, ln["line_key"], r) for li, ln in enumerate(tb["source_lines"]) for r in ln["regions"]}
    rows = []
    for r in sb["regions"]:
        li, line_key, g = geo[r["region_key"]]
        assert line_key == r["line_key"], (tb["id"], r["region_key"])
        o = r.get("otsu")
        fg_ratio = None
        if o:  # 외곽에 덜 닿는 집단이 글자 쪽(otsu_border)
            fg_px = o["dark_px"] if o["border_dark"] < o["border_light"] else o["light_px"]
            fg_ratio = fg_px / (o["dark_px"] + o["light_px"])
        rows.append({"region_key": r["region_key"], "line_key": r["line_key"], "line_index": li, "status": r["status"],
                     "text": g["text"], "bbox": g["bbox"], "score": r["score"], "font_color": r["font_color"],
                     "bg_color": r["bg_color"], "est_font_px": r["est_font_px"], "color_error": r.get("color_error"),
                     "otsu": o, "fg_ratio": fg_ratio, "hangul": bool(HANGUL.search(g["text"])),
                     "chars": len(re.sub(r"\s", "", g["text"]))})
    return rows


def _line_rep(item: str, regs: list[dict]):
    vals = [r[_FIELD[item]] for r in regs if r[_FIELD[item]] is not None]
    if not vals:
        return None
    if item == "size":
        return float(median_fraction([Fraction(str(v)) for v in vals]))
    return hex_of(representative_color([rgb(v) for v in vals]))


_FIELD = {"size": "est_font_px", "font_color": "font_color", "bg_color": "bg_color"}


def _diff(item: str, a, b) -> float:
    if item == "size":
        return max(a, b) / min(a, b)
    return dist(a, b)


def item_metrics(item: str, regs: list[dict], block_rep) -> dict:
    """한 항목의 블록 안 차이. pairs = 값이 있는 영역 쌍(같은 줄 여부 표시), line_pairs = 줄 대표값 쌍."""
    f = _FIELD[item]
    have = [r for r in regs if r[f] is not None]
    out: dict = {"n": len(have), "null": len(regs) - len(have)}
    if len(have) < 2:
        return out | {"max": None}
    pairs = [(_diff(item, a[f], b[f]), a, b) for a, b in itertools.combinations(have, 2)]
    by_line: dict[str, list[dict]] = collections.defaultdict(list)
    for r in have:
        by_line[r["line_key"]].append(r)
    reps = {k: _line_rep(item, v) for k, v in by_line.items()}
    lpairs = [_diff(item, reps[a], reps[b]) for a, b in itertools.combinations(reps, 2)]
    out |= {"max": max(p[0] for p in pairs),
            "max_within": max((p[0] for p in pairs if p[1]["line_key"] == p[2]["line_key"]), default=None),
            "max_between": max(lpairs, default=None), "lines": len(reps), "_pairs": pairs}
    if item == "size":
        vals = [r[f] for r in have]
        out["rep_dev"] = max(abs(v - block_rep) for v in vals) / block_rep if block_rep else None
        out["min"], out["max_px"] = min(vals), max(vals)
    else:
        out["max_delta_e"] = max(delta_e(a[f], b[f]) for _, a, b in pairs)
    return out


def loc(m: dict, thr: float) -> str:
    if m.get("max") is None or m["max"] < thr:
        return "none"
    w = m["max_within"] is not None and m["max_within"] >= thr
    b = m["max_between"] is not None and m["max_between"] >= thr
    return "both" if w and b else "within" if w else "between" if b else "pair_only"


def combine(a: str, b: str) -> str:
    """두 항목의 위치를 합친다 — within · between이 하나라도 있으면 그것, 없고 pair_only만 있으면 pair_only."""
    s = {a, b}
    w = bool(s & {"within", "both"})
    t = bool(s & {"between", "both"})
    return "both" if w and t else "within" if w else "between" if t else "pair_only" if "pair_only" in s else "none"


def suspects(item: str, m: dict, thr: float) -> list[str]:
    """기준을 넘는 영역 쌍에 붙는 측정 오류 의심 표시(휴리스틱)."""
    flags: set[str] = set()
    for d, a, b in m.get("_pairs", []):
        if d < thr:
            continue
        if min(a["score"], b["score"]) < LOW_SCORE:
            flags.add("low_score")
        if item == "size":
            if a["hangul"] != b["hangul"]:
                flags.add("script_mix")
            if min(a["chars"], b["chars"]) <= SHORT_TEXT:
                flags.add("short_text")
        else:
            if all(r["font_color"] and r["bg_color"] for r in (a, b)):
                if dist(a["font_color"], b["bg_color"]) < SWAP_NEAR and dist(a["bg_color"], b["font_color"]) < SWAP_NEAR:
                    flags.add("polarity_swap")
                if min(dist(r["font_color"], r["bg_color"]) for r in (a, b)) < LOW_CONTRAST:
                    flags.add("low_contrast")
            if any(r["fg_ratio"] is not None and r["fg_ratio"] < MINOR_CLASS for r in (a, b)):
                flags.add("minor_class")
    return sorted(flags)


def pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def quantiles(vals: list[float]) -> dict:
    v = sorted(vals)
    if not v:
        return {}

    def q(p: float) -> float:
        k = (len(v) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 3)
    return {"n": len(v), "p50": q(0.5), "p75": q(0.75), "p90": q(0.9), "p95": q(0.95), "max": round(v[-1], 3)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--gs", nargs="+", default=["GS-01", "GS-02"])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--size-thr", nargs="+", type=float, default=[1.2, 1.5, 2.0])
    ap.add_argument("--color-thr", nargs="+", type=float, default=[30, 60, 100, 150])
    a = ap.parse_args(argv)

    blocks: list[dict] = []
    counts: dict = {"by_gs": {}}
    inputs = {}
    for gs in a.gs:
        path = a.run / "evidence" / f"{gs}.json"
        raw = path.read_bytes()
        inputs[gs] = {"path": path.as_posix(), "sha256": hashlib.sha256(raw).hexdigest()}
        d = json.loads(raw.decode("utf-8"))
        tb = {b["id"]: b for b in d["text_block"]}
        sec = {s["id"]: s for s in d["section"]}
        st = style_payloads(d)
        c = collections.Counter(sections_with_style=len(st), sections=len(sec), text_blocks=len(tb))
        cfgs = {json.dumps(p["config"], sort_keys=True) for p in st.values()}
        for sid, p in sorted(st.items()):
            for sb in p["blocks"]:
                c[f"style_{sb['status']}"] += 1
                if sb["status"] == "excluded":
                    continue
                b = tb[int(sb["block_key"].split("_")[1])]
                assert b["section_id"] == sid
                regs = region_rows(b, sb)
                measured = [r for r in regs if r["status"] == "measured"]
                c["regions_measured"] += len(measured)
                c["regions_blank_text"] += sum(r["status"] == "blank_text" for r in regs)
                c["regions_other_status"] += sum(r["status"] not in ("measured", "blank_text") for r in regs)
                for f in ("font_color", "bg_color", "est_font_px"):
                    c[f"measured_null_{f}"] += sum(r[f] is None for r in measured)
                c["blocks_measured_%s" % ("0" if not measured else "1" if len(measured) == 1 else "2plus")] += 1
                if len(measured) < 2:
                    continue
                lines = collections.Counter(r["line_key"] for r in measured)
                m = {it: item_metrics(it, measured, sb["est_font_px"]) for it in ITEMS}
                row = {"gs": gs, "id": b["id"], "section_id": sid, "role": b["role"], "is_excluded": b["is_excluded"],
                       "source_ko": b["source_ko"], "bbox": b["bbox"], "section_width": sec[sid]["bbox"]["w"],
                       "image_key": sec[sid]["image_key"],
                       "block_style": {k: sb[k] for k in ("font_color", "bg_color", "est_font_px", "align")},
                       "n_regions": len(measured), "n_lines": len(lines), "multi_region_lines": sum(v >= 2 for v in lines.values()),
                       "regions": measured, "metrics": {}, "loc": {}, "suspect": {}}
                for it in ITEMS:
                    thrs = a.size_thr if it == "size" else a.color_thr
                    row["metrics"][it] = {k: v for k, v in m[it].items() if not k.startswith("_")}
                    row["loc"][it] = {f"{t:g}": loc(m[it], t) for t in thrs}
                    row["suspect"][it] = {f"{t:g}": suspects(it, m[it], t) for t in thrs}
                blocks.append(row)
        counts["by_gs"][gs] = dict(sorted(c.items())) | {"style_config": sorted(cfgs)}

    n = len(blocks)
    summary: dict = {"inputs": inputs, "thresholds": {"size_ratio": a.size_thr, "color_srgb": a.color_thr},
                     "suspect_constants": {"LOW_SCORE": LOW_SCORE, "SHORT_TEXT": SHORT_TEXT, "LOW_CONTRAST": LOW_CONTRAST,
                                           "SWAP_NEAR": SWAP_NEAR, "MINOR_CLASS": MINOR_CLASS},
                     "counts": counts, "target_blocks": n,
                     "target_by_role": dict(collections.Counter(b["role"] for b in blocks)),
                     "target_by_gs": dict(collections.Counter(b["gs"] for b in blocks)),
                     "target_structure": {"multi_line": sum(b["n_lines"] >= 2 for b in blocks),
                                          "single_line": sum(b["n_lines"] == 1 for b in blocks),
                                          "has_multi_region_line": sum(b["multi_region_lines"] > 0 for b in blocks),
                                          "is_excluded": dict(collections.Counter(str(b["is_excluded"]) for b in blocks))}}

    # 분포(기준값 없이)
    summary["distribution"] = {
        "size_ratio": quantiles([b["metrics"]["size"]["max"] for b in blocks if b["metrics"]["size"]["max"] is not None]),
        "size_rep_dev": quantiles([b["metrics"]["size"]["rep_dev"] for b in blocks if b["metrics"]["size"].get("rep_dev") is not None]),
        **{f"{it}_srgb": quantiles([b["metrics"][it]["max"] for b in blocks if b["metrics"][it]["max"] is not None])
           for it in ("font_color", "bg_color")},
        **{f"{it}_delta_e": quantiles([b["metrics"][it]["max_delta_e"] for b in blocks if b["metrics"][it].get("max_delta_e") is not None])
           for it in ("font_color", "bg_color")}}

    def table(rows: list[dict], key) -> dict:
        c = collections.Counter(key(b) for b in rows)
        return {t: {"n": c.get(t, 0), "pct": pct(c.get(t, 0), len(rows))} for t in TYPES} | {"blocks": len(rows)}

    # 항목별 · 기준값별 위치 유형
    summary["by_item"] = {}
    for it in ITEMS:
        thrs = a.size_thr if it == "size" else a.color_thr
        summary["by_item"][it] = {}
        for t in thrs:
            k = f"{t:g}"
            diff = [b for b in blocks if b["loc"][it][k] != "none"]
            sus = collections.Counter(f for b in diff for f in b["suspect"][it][k])
            summary["by_item"][it][k] = table(blocks, lambda b: b["loc"][it][k]) | {
                "by_role": {r: table([b for b in blocks if b["role"] == r], lambda b: b["loc"][it][k])
                            for r in ROLES if any(b["role"] == r for b in blocks)},
                "by_gs": {g: table([b for b in blocks if b["gs"] == g], lambda b: b["loc"][it][k]) for g in a.gs},
                "diff_blocks": len(diff), "suspect_any": sum(bool(b["suspect"][it][k]) for b in diff),
                "suspect_any_pct_of_diff": pct(sum(bool(b["suspect"][it][k]) for b in diff), len(diff)),
                "suspect_flags": dict(sorted(sus.items()))}

    # 조판에 쓰이는 두 항목(크기 · 글자색)을 합친 유형 — 기준값 조합 격자
    summary["combined"] = {}
    for ts in a.size_thr:
        for tc in a.color_thr:
            ks, kc = f"{ts:g}", f"{tc:g}"

            def typ(b: dict) -> str:
                return combine(b["loc"]["size"][ks], b["loc"]["font_color"][kc])

            def sus_all(b: dict) -> bool:
                """차이를 만든 항목 모두에 의심 표시가 있다(의심 없는 항목이 하나라도 차이를 만들면 False)."""
                made = [(it, k) for it, k in (("size", ks), ("font_color", kc)) if b["loc"][it][k] != "none"]
                return bool(made) and all(b["suspect"][it][k] for it, k in made)
            diff = [b for b in blocks if typ(b) != "none"]
            clean = [b for b in diff if not sus_all(b)]
            summary["combined"][f"size{ks}_color{kc}"] = table(blocks, typ) | {
                "by_role": {r: table([b for b in blocks if b["role"] == r], typ) for r in ROLES if any(b["role"] == r for b in blocks)},
                "by_gs": {g: table([b for b in blocks if b["gs"] == g], typ) for g in a.gs},
                "diff_blocks": len(diff), "suspect_all_items": len(diff) - len(clean),
                "suspect_all_items_pct_of_diff": pct(len(diff) - len(clean), len(diff)),
                "without_suspect": {t: sum(typ(b) == t for b in clean) for t in TYPES if t != "none"}}

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "blocks.json").write_text(json.dumps(blocks, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("counts", "target_blocks", "target_by_role", "target_by_gs", "target_structure",
                                               "distribution")}, ensure_ascii=False, indent=1))
    for it in ITEMS:
        for k, v in summary["by_item"][it].items():
            print(it, k, {t: v[t]["n"] for t in TYPES}, "의심", v["suspect_any"], "/", v["diff_blocks"], v["suspect_flags"])
    for k, v in summary["combined"].items():
        print(k, {t: v[t]["n"] for t in TYPES}, "모든 항목 의심", v["suspect_all_items"], "/", v["diff_blocks"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
