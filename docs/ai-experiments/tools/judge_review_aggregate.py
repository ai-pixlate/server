"""③-1 실측 육안 검수 판정표(agent_*.json, 명세 v1) 집계(2026-09-29). 판정 성공률이 아니라 대조 결과와 원인별 건수를 낸다.

    python docs/ai-experiments/tools/judge_review_aggregate.py --review pipeline/out/judge-run-v1_<date>/review [--expect-sections 100]

출력: <review>/aggregate.json + stdout 요약. 검증: 섹션 누락 · 중복, 항목 8개 누락, 값 어휘, agree와 (human, llm) 대응의 일관성.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HUMAN = {"해당", "비해당", "판단불가"}
LLM = {"present", "absent", "uncertain"}
PAIR = {("해당", "present"), ("비해당", "absent"), ("판단불가", "uncertain")}
ITEMS = [f"LC-0{i}" for i in range(1, 9)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", required=True)
    ap.add_argument("--expect-sections", type=int, default=100)
    a = ap.parse_args(argv)
    rd = Path(a.review)
    files = sorted(rd.glob("agent_*.json"))
    problems: list[str] = []
    seen: Counter = Counter()
    item_total: Counter = Counter()
    item_agree: Counter = Counter()
    item_human: dict[str, Counter] = defaultdict(Counter)
    disagree: list[dict] = []
    cause_by_item: dict[str, Counter] = defaultdict(Counter)
    cause_total: Counter = Counter()
    undecidable: list[dict] = []
    mixed = 0
    rg_valid: Counter = Counter()
    rg_invalid: list[dict] = []
    verdict_h: Counter = Counter()
    verdict_dis: list[dict] = []
    notes: list[dict] = []
    per_reviewer: dict[str, Counter] = defaultdict(Counter)
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        rv = data.get("reviewer", f.stem)
        for s in data.get("sections", []):
            key = s.get("section")
            seen[key] += 1
            per_reviewer[rv]["sections"] += 1
            if s.get("mixed"):
                mixed += 1
            items = s.get("items", {})
            missing = [i for i in ITEMS if i not in items]
            if missing:
                problems.append(f"{rv} {key}: 항목 누락 {missing}")
            for iid, it in items.items():
                h, l, ag = it.get("human"), it.get("llm"), it.get("agree")
                if h not in HUMAN or l not in LLM:
                    problems.append(f"{rv} {key} {iid}: 값 어휘 오류 human={h!r} llm={l!r}")
                    continue
                expected = (h, l) in PAIR
                if ag != expected:
                    problems.append(f"{rv} {key} {iid}: agree={ag}가 (human={h}, llm={l})와 맞지 않는다 → {expected}로 집계")
                    ag = expected
                item_total[iid] += 1
                item_human[iid][h] += 1
                per_reviewer[rv]["items"] += 1
                if h == "판단불가":
                    undecidable.append({"section": key, "item": iid, "llm": l, "memo": it.get("memo", "")})
                if ag:
                    item_agree[iid] += 1
                else:
                    c = it.get("cause")
                    if c not in (1, 2, 3, 4, 5, 6):
                        problems.append(f"{rv} {key} {iid}: 불일치인데 원인 코드 없음/오류 {c!r} → 6으로 집계")
                        c = 6
                    cause_by_item[iid][c] += 1
                    cause_total[c] += 1
                    per_reviewer[rv]["disagree"] += 1
                    disagree.append({"section": key, "item": iid, "human": h, "llm": l, "cause": c, "memo": it.get("memo", ""), "reviewer": rv})
            for r in s.get("rg", []) or []:
                rg_valid[(r.get("ref"), r.get("valid"))] += 1
                if r.get("valid") != "예":
                    rg_invalid.append({"section": key, **r})
            v = s.get("verdict") or {}
            verdict_h[(v.get("llm_bucket"), v.get("human"))] += 1
            if v.get("human") in ("비동의", "보류"):
                verdict_dis.append({"section": key, **v})
            if s.get("notes"):
                notes.append({"section": key, "reviewer": rv, "notes": s["notes"]})
    dup = [k for k, n in seen.items() if n > 1]
    if dup:
        problems.append(f"중복 섹션 {dup}")
    if len(seen) != a.expect_sections:
        problems.append(f"섹션 수 {len(seen)} ≠ {a.expect_sections}")
    out = {
        "files": [f.name for f in files], "sections": len(seen), "problems": problems,
        "per_reviewer": {k: dict(v) for k, v in per_reviewer.items()},
        "items_total": sum(item_total.values()), "items_agree": sum(item_agree.values()),
        "item_agree": {i: f"{item_agree[i]}/{item_total[i]}" for i in ITEMS},
        "item_human": {i: dict(item_human[i]) for i in ITEMS},
        "cause_total": {str(k): v for k, v in sorted(cause_total.items())},
        "cause_by_item": {i: {str(k): v for k, v in sorted(cause_by_item[i].items())} for i in ITEMS if cause_by_item[i]},
        "disagreements": disagree, "undecidable": undecidable, "mixed_sections": mixed,
        "rg_valid": {f"{k[0]} {k[1]}": n for k, n in sorted(rg_valid.items(), key=lambda x: str(x[0]))}, "rg_not_valid": rg_invalid,
        "verdict": {f"{k[0]} → {k[1]}": n for k, n in sorted(verdict_h.items(), key=lambda x: str(x[0]))}, "verdict_disagree_or_hold": verdict_dis,
        "notes": notes,
    }
    (rd / "aggregate.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    brief = {k: v for k, v in out.items() if k not in ("disagreements", "undecidable", "notes", "rg_not_valid", "verdict_disagree_or_hold")}
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    print("disagreements", len(disagree), "undecidable", len(undecidable), "rg_not_valid", len(rg_invalid), "verdict_dis", len(verdict_dis), "notes", len(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
