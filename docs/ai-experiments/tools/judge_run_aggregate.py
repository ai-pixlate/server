"""③-1 · ③-1' 실측 출력의 관찰값 집계 + 오버레이 생성(2026-09-29). 판정 · 정답이 아니라 관찰값만 낸다.

    python docs/ai-experiments/tools/judge_run_aggregate.py --run pipeline/out/judge-run-v1_<date> --input pipeline/samples/local/blocks-input-v1 [--no-overlay]

출력: <run>/aggregate.json(항목별 status · evidence_source · 매칭×status 교차 · 권고 · 충돌 · 억제 · dict_coverage · 지연 · 실패 · 추가 분류 비교 · 제외 섹션 상세)
      <run>/<원본>/overlay/<key>.png(inspect --judge --policy)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import inspect as insp  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.types import JudgeResult, PolicyResult  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--no-overlay", action="store_true")
    a = ap.parse_args(argv)
    run, inp = Path(a.run), Path(a.input)
    rows = json.loads((run / "sections.json").read_text(encoding="utf-8"))
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))

    item_status: dict[str, Counter] = defaultdict(Counter)
    item_source: dict[str, Counter] = defaultdict(Counter)
    cross: Counter = Counter()  # (matched?, status)
    cross_by_item: dict[str, Counter] = defaultdict(Counter)
    present_without_match: list[dict] = []
    absent_with_match: list[dict] = []
    uncertain_list: list[dict] = []
    excluded: list[dict] = []
    conflicts_total = suppressed_total = 0
    coverage: Counter = Counter()
    durations: list[float] = []
    failures: list[dict] = []
    extra_cmp: dict[str, Counter] = defaultdict(Counter)
    extra_diff: list[dict] = []
    rg_findings: Counter = Counter()
    verdict_status: Counter = Counter()
    overlays = 0
    for r in rows:
        src, key = r["source"], r["section"]
        durations.append(r.get("duration_s") or 0)
        if r.get("judge_status") != "ok":
            failures.append({"source": src, "section": key, "judge_status": r.get("judge_status"), "error": r.get("error")})
            continue
        jr = jsonio.load_model(run / src / "judge" / f"{key}.json", JudgeResult)
        pr = jsonio.load_model(run / src / "policy" / f"{key}.json", PolicyResult)
        matched_refs = {m.dictionary_ref for m in jr.matches}
        for f in jr.content_findings.findings:
            if f.content_type.startswith("RG-"):
                rg_findings[f.content_type] += 1
                continue
            item_status[f.content_type][f.status] += 1
            item_source[f.content_type][f.evidence_source] += 1
            matched = f.content_type in matched_refs
            cross[(matched, f.status)] += 1
            cross_by_item[f.content_type][f"{'matched' if matched else 'unmatched'}:{f.status}"] += 1
            entry = {"source": src, "section": key, "item": f.content_type, "evidence_source": f.evidence_source,
                     "evidence": f.evidence_block_ids, "reason": f.reason}
            if f.status == "present" and not matched:
                present_without_match.append(entry)
            if f.status == "absent" and matched:
                absent_with_match.append(entry)
            if f.status == "uncertain":
                uncertain_list.append(entry)
        conflicts_total += len(pr.conflicts)
        suppressed_total += len(pr.suppressed)
        if pr.applied:
            coverage[pr.applied.dict_coverage] += 1
        for v in pr.verdicts:
            verdict_status[(v.dictionary_ref, v.verdict_status, v.finding_status)] += 1
        if pr.bucket_recommendation == "exclude":
            excluded.append({"source": src, "section": key, "class": r["regulatory_class"],
                             "verdicts": [{"ref": v.dictionary_ref, "status": v.verdict_status, "finding_status": v.finding_status,
                                           "problem_text": (v.problem_text or "")[:80], "conflict": v.conflict_group} for v in pr.verdicts]})
        for ec, info in (r.get("extra") or {}).items():
            extra_cmp[ec][(pr.bucket_recommendation, info["bucket"])] += 1
            if info["bucket"] != pr.bucket_recommendation or info["verdicts"] != len(pr.verdicts):
                extra_diff.append({"source": src, "section": key, "base": r["regulatory_class"], "base_bucket": pr.bucket_recommendation,
                                   "base_verdicts": len(pr.verdicts), "extra_class": ec, **info})
        if not a.no_overlay:
            merge = jsonio.load_merge(inp / src / "merge" / f"{key}.json")
            split = jsonio.load_split(inp / src / "split.json")
            sec = next(s for s in split.sections if s.section_key == key)
            insp.overlay_judge(sec.image_path, merge, jr, run / src / "overlay" / f"{key}.png", policy=pr)
            overlays += 1

    ds = sorted(d for d in durations if d)
    agg = {
        "run": str(run), "sections": len(rows), "failures": failures,
        "usage_total": manifest.get("usage_total") or json.loads((run / "summary.json").read_text(encoding="utf-8")).get("usage_total"),
        "duration_s": {"median": ds[len(ds) // 2] if ds else None, "p90": ds[int(len(ds) * 0.9)] if ds else None, "max": ds[-1] if ds else None,
                       "total": round(sum(ds), 1)},
        "item_status": {k: dict(v) for k, v in sorted(item_status.items())},
        "item_evidence_source": {k: dict(v) for k, v in sorted(item_source.items())},
        "cross_matched_status": {f"{'matched' if m else 'unmatched'}:{s}": n for (m, s), n in sorted(cross.items())},
        "cross_by_item": {k: dict(v) for k, v in sorted(cross_by_item.items())},
        "present_without_match": present_without_match, "absent_with_match": absent_with_match, "uncertain": uncertain_list,
        "rg_findings": dict(rg_findings), "verdicts": {f"{k[0]} {k[1]} ({k[2]})": n for k, n in sorted(verdict_status.items())},
        "excluded_sections": excluded, "conflicts_total": conflicts_total, "suppressed_total": suppressed_total,
        "dict_coverage": dict(coverage),
        "extra_classes": {ec: {f"base={b} extra={e}": n for (b, e), n in sorted(c.items())} for ec, c in extra_cmp.items()},
        "extra_class_diffs": extra_diff, "overlays": overlays,
        "note": "관찰값. 정답 라벨이 없으므로 present/absent 수는 정확도가 아니다. 분류는 실험용 가정",
    }
    (run / "aggregate.json").write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps({k: v for k, v in agg.items() if k not in ("present_without_match", "absent_with_match", "uncertain", "excluded_sections", "extra_class_diffs")}, ensure_ascii=False, indent=2))
    print("present_without_match", len(present_without_match), "absent_with_match", len(absent_with_match), "uncertain", len(uncertain_list),
          "excluded", len(excluded), "extra_diffs", len(extra_diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
