"""③ OCR 후처리 4단계 1차 실험 — 점검 세트 고정 · 변형 실행 · 집계(설계: 2026-10-23_03-merge_post-step4-design-v1.md).

LLM 호출 없이 blocks-input-v1의 휴리스틱만 비교한다. 파이프라인 코드는 바꾸지 않고, 실험 도구 안에서만 함수를 바꿔 끼운다.
변형은 하나씩 별도 프로세스로 실행한다(`variant` 하위 명령을 변형마다 따로 부른다 — `all`이 그렇게 한다).

    python docs/ai-experiments/tools/merge_post_step4.py all --input pipeline/samples/local/blocks-input-v1 \
        --label-run pipeline/out/label-run-v1_2026-09-29 --out RUN_DIR

하위 명령:
- checkset: A0(기본 config + merge.xycut=true) 기준으로 점검 세트 S1 · S2 · S3 · S4를 뽑아 RUN_DIR/checkset.json에 고정한다.
  사례는 원시 영역 집합으로 정의한다(변형마다 블록 번호가 달라도 같은 사례). ④ 라벨 1회 실측이 true로 본 블록에 속하면
  label_hint를 붙인다(사람 판단 아님).
- variant --name N [--set 표.키=값 ...] [--patch b3|gapcond]: 한 변형을 돌려 RUN_DIR/variants/N.json에 쓴다.
  b3 = 가로줄의 Line 나누기에서 높이비 조건만 끔, gapcond = 같은 곳에 기존 가로 간격 조건(line_gap_min · line_gap)을 더함.
- report: 변형 결과를 A0와 비교해 RUN_DIR/summary.json · RUN_DIR/report.md · RUN_DIR/crops/를 만든다.
- all [--round round1|round2] [--checkset PATH]: checkset(또는 고정본 복사) → 그 차수의 변형(각각 새 프로세스) → report.
  2차(round2)는 B3 채택 뒤의 기준(B3)과 B3 + C_tol0.7을 1차에서 고정한 점검 세트로 비교한다.
구조 지표(블록 수 · 걸친 영역 수 등)는 원인 분석용이며 품질 판정이 아니다. 어떤 결과도 정답이 아니다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import merge  # noqa: E402
from pipeline.types import OcrResult  # noqa: E402

FONT = ImageFont.load_default(size=14)

# 1차 변형(설계 3절). A0 = 기준, OFF = 참고, D = 전각 변환 목록
ROUND1 = [
    ("A0", [], None),
    ("OFF", ["merge.xycut=false"], None),
    ("A1_gap1.0", ["merge.xycut_gap_ratio=1.0"], None),
    ("A1_gap1.2", ["merge.xycut_gap_ratio=1.2"], None),
    ("A1_gap1.5", ["merge.xycut_gap_ratio=1.5"], None),
    ("B3_no_hratio", [], "b3"),
    ("C_tol0.35", ["merge.line_center_tol=0.35"], None),
    ("C_tol0.7", ["merge.line_center_tol=0.7"], None),
    ("C_min12", ["merge.line_center_tol_min_px=12"], None),
    ("C_gapcond", [], "gapcond"),
    ("D_fullwidth", ["merge.fullwidth=true"], None),
]
# 2차(2026-10-23): B3(가로줄 Line 나누기의 높이비 해제)를 파이프라인에 채택한 뒤의 기준과 C_tol0.7 조합.
# 1차 A0(높이비 있음)는 채택 전 코드(4ec58bb)에서만 재현된다. b3 patch는 채택 뒤 코드에서는 아무것도 바꾸지 않는다.
ROUND2 = [
    ("B3", [], None),
    ("B3_C_tol0.7", ["merge.line_center_tol=0.7"], None),
]
ROUNDS = {"round1": ROUND1, "round2": ROUND2}


def rkeys(b) -> list[str]:
    return [r.region_key for ln in b.source_lines for r in ln.regions]


def sections(inp: Path):
    for img in sorted(p for p in inp.iterdir() if p.is_dir() and (p / "split.json").is_file()):
        split = jsonio.load_split(img / "split.json")
        for ocr_path in sorted((img / "ocr").glob("*.json")):
            ocr_res = jsonio.load_ocr(ocr_path)
            sec = next(s for s in split.sections if s.section_key == ocr_res.section_key)
            yield img, sec, ocr_res, img / "merge" / ocr_path.name


def base_cfg(sets: list[str]) -> dict:
    cfg = cfgmod.load_config(None, ["merge.xycut=true", *sets])
    merge.validate_config(cfg)
    return cfg


def provenance(inp: Path, sets: list[str], patch: str | None) -> dict:
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    sums = inp / "SHA256SUMS"
    return {"commit": git("rev-parse", "HEAD"), "dirty_paths": git("status", "--porcelain").splitlines(),
            "input": str(inp), "input_sha256sums_sha256": hashlib.sha256(sums.read_bytes()).hexdigest() if sums.is_file() else None,
            "set": ["merge.xycut=true", *sets], "patch": patch}


# ---------------------------------------------------------------------------
# 점검 세트
# ---------------------------------------------------------------------------
def label_regions(label_run: Path | None, img: Path, sec_key: str, final) -> set[str]:
    if label_run is None:
        return set()
    p = label_run / img.name / "label" / f"{sec_key}.json"
    if not p.is_file():
        return set()
    d = json.loads(p.read_text(encoding="utf-8"))
    true_keys = {x["block_key"] for x in d.get("labels", []) if x.get("is_product_label") is True}
    return {rk for b in final.blocks if b.block_key in true_keys for rk in rkeys(b)}


def cmd_checkset(a) -> int:
    from merge_post_compare import components, inversions, kind_of
    from merge_post_gutter_check import check_section

    inp, out = Path(a.input), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg_off = cfgmod.load_config()
    cfg_a0 = base_cfg([])
    label_run = Path(a.label_run) if a.label_run else None
    cases: list[dict] = []
    s3: list[dict] = []
    for img, sec, ocr_res, final_path in sections(inp):
        name = f"{img.name}/{sec.section_key}"
        final = jsonio.load_merge(final_path)
        labels = label_regions(label_run, img, sec.section_key, final)
        off = merge.heuristic(sec, ocr_res, cfg_off)
        a0, layout, _, _ = merge._heuristic(sec, ocr_res, cfg_a0)
        same_pairs = []

        def add(set_name: str, regs: list[str], basis: str) -> None:
            cases.append({"set": set_name, "section": name, "regions": sorted(regs), "basis": basis,
                          "label_hint": bool(labels & set(regs))})

        for offs, ons in components(off, a0):
            kind = kind_of(offs, ons, layout)
            if kind == "same":
                same_pairs.append((offs[0], ons[0]))
            elif kind == "split_cut":
                add("S1", rkeys(offs[0]), "끔 휴리스틱 블록이 A0에서 다른 후보 영역으로 나뉨")
            elif kind == "split_inner":
                add("S2", rkeys(offs[0]), "끔 휴리스틱 블록이 A0에서 같은 후보 영역 안 여러 블록으로 나뉨")
            elif kind == "merge":
                add("S4", rkeys(ons[0]), "끔 휴리스틱 블록 여러 개가 A0에서 한 블록으로 합쳐짐")
        a0_region = {rk: layout.block_region[b.block_key] for b in a0.blocks for rk in rkeys(b)}
        for fb in final.blocks:
            regs = rkeys(fb)
            if len({a0_region[rk] for rk in regs}) > 1:
                add("S1", regs, "끈 상태 LLM 최종 블록이 A0에서 여러 후보 영역에 걸침")
        _, gcases = check_section(sec, ocr_res, final, cfg_a0)
        for c in gcases:
            if c["exact_cover"]:
                add("S4", c["off_regions"], "교차 블록 병합 점검의 범위 같은 대리 사례")
        cross, inner, _ = inversions(same_pairs, layout)
        if cross:
            s3.append({"section": name, "cross_region_inversions": cross, "same_region_inversions": inner})
    # 같은 세트 안 중복 제거(같은 영역 집합), 세트 간 중복 표시
    seen: dict[tuple, dict] = {}
    for c in cases:
        k = (c["set"], c["section"], tuple(c["regions"]))
        if k in seen:
            seen[k]["basis"] = sorted(set(seen[k]["basis"].split(" + ")) | {c["basis"]})
            seen[k]["basis"] = " + ".join(seen[k]["basis"])
            continue
        seen[k] = c
    uniq = list(seen.values())
    by_sig: dict[tuple, set[str]] = {}
    for c in uniq:
        by_sig.setdefault((c["section"], tuple(c["regions"])), set()).add(c["set"])
    uniq.sort(key=lambda c: (c["set"], c["section"], c["regions"][0], len(c["regions"]), c["regions"]))
    used: Counter = Counter()
    for c in uniq:
        base_id = f"{c['set']}:{c['section']}:{c['regions'][0]}"
        used[base_id] += 1  # 첫 영역 키가 같은 다른 영역 집합은 #2, #3 … 으로 구분한다(정렬 순서로 고정)
        c["id"] = base_id if used[base_id] == 1 else f"{base_id}#{used[base_id]}"
        c["also_in"] = sorted(by_sig[(c["section"], tuple(c["regions"]))] - {c["set"]})
    s3.sort(key=lambda s: -s["cross_region_inversions"])
    reps = {s["section"] for s in s3[:5]} | {"GS-01_001/sec_1_01", "GS-03_014/sec_1_01"}
    for s in s3:
        s["representative"] = s["section"] in reps
    doc = {"provenance": provenance(inp, [], None), "label_run": str(label_run) if label_run else None,
           "cases": uniq, "S3": s3}
    (out / "checkset.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    cnt = Counter(c["set"] for c in uniq)
    print(json.dumps({"cases": dict(cnt), "label_hint": dict(Counter(c["set"] for c in uniq if c["label_hint"])),
                      "cross_set": sum(1 for c in uniq if c["also_in"]), "S3_sections": len(s3),
                      "S3_representative": sorted(reps & {s["section"] for s in s3})}, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# 변형 실행(별도 프로세스)
# ---------------------------------------------------------------------------
def patch(name: str | None) -> None:
    if name is None:
        return
    if name == "b3":
        def split_row(regs, m, gutters):
            pieces = [[regs[0]]]
            for b in regs[1:]:
                a = pieces[-1][-1]
                if m["gutter"] and any(a.bbox.x2 <= g0 and g1 <= b.bbox.x for g0, g1 in gutters):
                    pieces.append([b])
                else:
                    pieces[-1].append(b)
            return pieces
    elif name == "gapcond":
        orig = merge._split_row

        def split_row(regs, m, gutters):
            out = []
            for piece in orig(regs, m, gutters):
                cur = [piece[0]]
                for b in piece[1:]:
                    a = cur[-1]
                    base = min(a.bbox.h, b.bbox.h)
                    if m["line_gap_min"] * base <= b.bbox.x - a.bbox.x2 <= m["line_gap"] * base:
                        cur.append(b)
                    else:
                        out.append(cur)
                        cur = [b]
                out.append(cur)
            return out
    else:
        raise SystemExit(f"모르는 patch {name}")
    merge._split_row = split_row


def check_invariants(sec, ocr_res, res, cfg) -> list[str]:
    problems = []
    out = [r for b in res.blocks for ln in b.source_lines for r in ln.regions]
    if Counter(r.region_key for r in out) != Counter(r.region_key for r in ocr_res.regions):
        problems.append("영역이 정확히 한 번이 아니다")
    by_key = {r.region_key: r for r in ocr_res.regions}
    if any(r.model_dump() != by_key[r.region_key].model_dump() for r in out):
        problems.append("원시 영역 값이 바뀌었다")
    if [b.block_order for b in res.blocks] != list(range(1, len(res.blocks) + 1)):
        problems.append("block_order가 연속이 아니다")
    shuffled = list(ocr_res.regions)
    random.Random(7).shuffle(shuffled)
    if merge.heuristic(sec, OcrResult(section_key=ocr_res.section_key, regions=shuffled), cfg) != res:
        problems.append("입력 순서에 따라 결과가 다르다")
    return problems


def proxy_text(sec, res, layout, regs: list[str], cfg) -> str | None:
    """대리 재조립: 사례 영역을 정확히 덮는 블록들을 한 묶음으로 assemble한 텍스트. 정확히 덮지 못하면 None.
    묶음이 후보 영역을 넘으면 assemble이 영역별로 나누므로 나뉜 블록 텍스트를 ` ‖ `로 잇는다."""
    want = set(regs)
    members = [b for b in res.blocks if set(rkeys(b)) & want]
    if {rk for b in members for rk in rkeys(b)} != want or not all(merge._sendable(b) for b in members):
        return None
    if len(members) == 1:
        return members[0].source_ko
    ids = {f"b{i}": b for i, b in enumerate(members, start=1)}
    out, _, _ = merge.assemble(sec, res, ids, [(list(ids), "body")], layout, cfg["merge"])
    return " ‖ ".join(b.source_ko for b in out.blocks if set(rkeys(b)) & want)


def cmd_variant(a) -> int:
    inp, out = Path(a.input), Path(a.out)
    patch(a.patch)
    cfg = base_cfg(a.set or [])
    checkset = json.loads((out / "checkset.json").read_text(encoding="utf-8"))
    cases_by_sec: dict[str, list[dict]] = {}
    for c in checkset["cases"]:
        cases_by_sec.setdefault(c["section"], []).append(c)
    secs: dict[str, dict] = {}
    problems: dict[str, list[str]] = {}
    case_out: dict[str, dict] = {}
    fullwidth: list[dict] = []
    for img, sec, ocr_res, _ in sections(inp):
        name = f"{img.name}/{sec.section_key}"
        if cfg["merge"]["xycut"]:
            res, layout, _, _ = merge._heuristic(sec, ocr_res, cfg)
            region_of = {b.block_key: layout.block_region[b.block_key] for b in res.blocks}
        else:
            res, layout = merge.heuristic(sec, ocr_res, cfg), None
            region_of = {b.block_key: 1 for b in res.blocks}
        p = check_invariants(sec, ocr_res, res, cfg)
        if p:
            problems[name] = p
        secs[name] = {"regions": (layout.n_regions if layout else None),
                      "blocks": [{"regions": rkeys(b), "role": b.role, "text": b.source_ko, "lines": len(b.source_lines),
                                  "region": region_of[b.block_key]} for b in res.blocks]}
        owner = {rk: b for b in res.blocks for rk in rkeys(b)}
        for c in cases_by_sec.get(name, []):
            touched = list({owner[rk].block_key: owner[rk] for rk in c["regions"]}.values())
            case_out[c["id"]] = {
                "blocks": len(touched), "candidate_regions": len({region_of[b.block_key] for b in touched}),
                "exact": {rk for b in touched for rk in rkeys(b)} == set(c["regions"]),
                "lines": sum(len(b.source_lines) for b in touched),
                "texts": [b.source_ko for b in sorted(touched, key=lambda b: b.block_order)],
                "roles": [b.role for b in sorted(touched, key=lambda b: b.block_order)],
                "proxy_text": proxy_text(sec, res, layout, c["regions"], cfg) if layout else None,
            }
        if cfg["merge"]["fullwidth"]:
            for b in res.blocks:
                for ln in b.source_lines:
                    raw = " ".join(r.text.strip() for r in ln.regions)
                    if raw != ln.text:
                        fullwidth.append({"section": name, "raw": raw, "converted": ln.text})
    doc = {"name": a.name, "provenance": provenance(inp, a.set or [], a.patch), "invariant_problems": problems,
           "sections": secs, "cases": case_out, "fullwidth_changes": fullwidth}
    (out / "variants").mkdir(parents=True, exist_ok=True)
    (out / "variants" / f"{a.name}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{a.name}: 섹션 {len(secs)} · 불변식 위반 {len(problems)} · 블록 {sum(len(s['blocks']) for s in secs.values())}")
    return 0


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------
def order_inversions(base: list[dict], other: list[dict]) -> int:
    """두 결과에 같은 영역 집합으로 있는 블록끼리 순서가 뒤바뀐 쌍 수."""
    pos = {tuple(b["regions"]): i for i, b in enumerate(other)}
    seq = [pos[tuple(b["regions"])] for b in base if tuple(b["regions"]) in pos]
    return sum(1 for i in range(len(seq)) for j in range(i + 1, len(seq)) if seq[i] > seq[j])


def crop_case(inp: Path, case: dict, dest: Path, margin: int = 30) -> str | None:
    img_name, sec_key = case["section"].split("/")
    split = jsonio.load_split(inp / img_name / "split.json")
    sec = next(s for s in split.sections if s.section_key == sec_key)
    ocr_res = jsonio.load_ocr(inp / img_name / "ocr" / f"{sec_key}.json")
    regs = [r for r in ocr_res.regions if r.region_key in set(case["regions"])]
    if not regs:
        return None
    x0, y0 = min(r.bbox.x for r in regs), min(r.bbox.y for r in regs)
    x1, y1 = max(r.bbox.x2 for r in regs), max(r.bbox.y2 for r in regs)
    with Image.open(inp / img_name / sec.image_path) as im:
        src = im.convert("RGB")
    box = (max(0, x0 - margin), max(0, y0 - margin), min(src.width, x1 + margin), min(src.height, y1 + margin))
    crop = src.crop(box)
    k = 2 if crop.width < 400 else 1
    if k > 1:
        crop = crop.resize((crop.width * k, crop.height * k), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    for r in regs:
        d.rectangle([(r.bbox.x - box[0]) * k, (r.bbox.y - box[1]) * k, (r.bbox.x2 - box[0]) * k, (r.bbox.y2 - box[1]) * k],
                    outline=(220, 0, 0), width=1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest)
    return str(dest)


def cmd_report(a) -> int:
    inp, out = Path(a.input), Path(a.out)
    checkset = json.loads((out / "checkset.json").read_text(encoding="utf-8"))
    names = [n for n, _, _ in ROUNDS[a.round] if (out / "variants" / f"{n}.json").is_file()]
    v = {n: json.loads((out / "variants" / f"{n}.json").read_text(encoding="utf-8")) for n in names}
    base = v[names[0]]  # 그 차수의 첫 변형이 기준(1차 A0, 2차 B3)
    s3_reps = {s["section"] for s in checkset["S3"] if s["representative"]}
    s3_all = {s["section"] for s in checkset["S3"]}
    table = {}
    for n in names:
        d = v[n]
        blocks = [b for s in d["sections"].values() for b in s["blocks"]]
        changed = [k for k in d["sections"] if d["sections"][k]["blocks"] != base["sections"][k]["blocks"]]
        inv_s3 = {k: order_inversions(base["sections"][k]["blocks"], d["sections"][k]["blocks"]) for k in s3_all}
        cs = d["cases"]
        row = {
            "invariant_problems": len(d["invariant_problems"]),
            "blocks": len(blocks), "roles": dict(Counter(b["role"] for b in blocks)),
            "candidate_regions": sum(s["regions"] or 0 for s in d["sections"].values()),
            "sections_changed_vs_base": len(changed),
            "S1_single_region": sum(1 for c in checkset["cases"] if c["set"] == "S1" and cs[c["id"]]["candidate_regions"] == 1),
            "S1_single_block": sum(1 for c in checkset["cases"] if c["set"] == "S1" and cs[c["id"]]["blocks"] == 1),
            "S2_single_block": sum(1 for c in checkset["cases"] if c["set"] == "S2" and cs[c["id"]]["blocks"] == 1),
            "S3_sections_order_changed_vs_base": sum(1 for k in s3_all if inv_s3[k]),
            "S3_reps_order_changed_vs_base": sorted(k for k in s3_reps if inv_s3[k]),
            "S4_changed_vs_base": sum(1 for c in checkset["cases"] if c["set"] == "S4"
                                    and (cs[c["id"]]["texts"] != base["cases"][c["id"]]["texts"]
                                         or cs[c["id"]]["proxy_text"] != base["cases"][c["id"]]["proxy_text"])),
            "fullwidth_changes": len(d["fullwidth_changes"]),
        }
        table[n] = row
    (out / "summary.json").write_text(json.dumps({"checkset_provenance": checkset["provenance"],
                                                  "variants": {n: v[n]["provenance"] for n in names}, "table": table},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 4단계 1차 실험 — 변형별 점검 세트 상태(LLM 없음)", "",
             "빨간 상자 = 사례의 원시 영역. 텍스트 칸: 사례 영역을 덮는 블록들의 텍스트(` ‖ `로 구분). 어느 결과도 정답이 아니다.", ""]
    for set_name in ("S1", "S2", "S4"):
        sel = [c for c in checkset["cases"] if c["set"] == set_name]
        lines += [f"## {set_name} ({len(sel)})", ""]
        for c in sel:
            diff = [n for n in names if v[n]["cases"][c["id"]]["texts"] != base["cases"][c["id"]]["texts"]
                    or v[n]["cases"][c["id"]]["proxy_text"] != base["cases"][c["id"]]["proxy_text"]]
            if set_name == "S4" and not diff:
                continue  # S4는 A0와 달라진 사례만 보여 준다(회귀 후보)
            crop = crop_case(inp, c, out / "crops" / f"{c['id'].replace(':', '_').replace('/', '_')}.png")
            tag = (" · 라벨 판정기 힌트" if c["label_hint"] else "") + (f" · 다른 세트: {','.join(c['also_in'])}" if c["also_in"] else "")
            lines += [f"### {c['id']}{tag}", "", f"근거: {c['basis']}", "", f"crop: `{crop}`", "",
                      "| 변형 | 블록 | 후보 영역 | 텍스트 |", "|---|---|---|---|"]
            for n in names:
                r = v[n]["cases"][c["id"]]
                text = " ‖ ".join(t.replace("\n", " / ") for t in r["texts"])
                if set_name == "S4" and r["proxy_text"] is not None:
                    text += f"  〔대리 재조립: {r['proxy_text'].replace(chr(10), ' / ')}〕"
                lines.append(f"| {n} | {r['blocks']} | {r['candidate_regions']} | {text.replace('|', '¦')} |")
            lines.append("")
    lines += ["## D 전각 변환 목록", ""]
    for x in v.get("D_fullwidth", {}).get("fullwidth_changes", []):
        lines.append(f"- {x['section']}: `{x['raw']}` → `{x['converted']}`")
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(table, ensure_ascii=False, indent=1))
    return 0


def cmd_all(a) -> int:
    common = ["--input", a.input, "--out", a.out]
    me = [sys.executable, str(Path(__file__).resolve())]
    if a.checkset:  # 고정한 점검 세트를 다시 쓴다(새로 뽑지 않음)
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "checkset.json").write_bytes(Path(a.checkset).read_bytes())
    else:
        subprocess.run([*me, "checkset", *common, *(["--label-run", a.label_run] if a.label_run else [])], check=True)
    for name, sets, p in ROUNDS[a.round]:
        argv = [*me, "variant", *common, "--name", name, *[x for s in sets for x in ("--set", s)], *(["--patch", p] if p else [])]
        subprocess.run(argv, check=True)  # 변형마다 새 프로세스 — 바꿔 끼운 함수가 다음 변형에 남지 않는다
    subprocess.run([*me, "report", *common, "--round", a.round], check=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("checkset", "variant", "report", "all"):
        sp = sub.add_parser(c)
        sp.add_argument("--input", required=True)
        sp.add_argument("--out", required=True)
        if c in ("checkset", "all"):
            sp.add_argument("--label-run")
        if c in ("report", "all"):
            sp.add_argument("--round", choices=sorted(ROUNDS), default="round1")
        if c == "all":
            sp.add_argument("--checkset", help="이미 고정한 checkset.json(주면 새로 뽑지 않는다)")
        if c == "variant":
            sp.add_argument("--name", required=True)
            sp.add_argument("--set", action="append")
            sp.add_argument("--patch", choices=["b3", "gapcond"])
    a = ap.parse_args(argv)
    return {"checkset": cmd_checkset, "variant": cmd_variant, "report": cmd_report, "all": cmd_all}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
