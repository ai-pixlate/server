"""⑦ 섹션 기준 정렬 판정의 정답률 실측(2026-11-05) — 개발용, 운영 경로 아님. 운영 코드 · config는 바꾸지 않는다.

    python docs/ai-experiments/tools/style_section_align_eval.py \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --rows <2026-11-03 정렬 조사 out>/align_rows.json \
        --labels docs/ai-experiments/2026-11-03_09-typeset_align-frequency_labels.tsv \
        --human docs/ai-experiments/2026-11-03_09-typeset_align-frequency_human-review.tsv \
        --out docs/ai-experiments/runs/2026-11-05_07-style_section-align --tol 0.005 0.01 0.02 0.03 0.05

입력(읽기만): 베이스라인 회차의 `evidence/<GS>.json`(DB 행 보존본 — `section.bbox.w` · `text_block.source_lines` · ⑦ 인계 payload),
2026-11-03 정렬 조사의 `align_rows.json`(조판 대상 491블록 · 당시 적용 정렬 · 에이전트 원본 정렬 라벨 · 오류 여부)과 라벨 · 사람 검수 TSV.

재계산: 섹션마다 당시 ⑦ 결과에서 제외(라벨 · 로고)가 아닌 블록을 처리 대상으로 두고, 현재 `pipeline.stages.style`의
`block_span` · `section_align`을 그대로 부른다(허용 오차만 바꿔 가며). 새 적용 정렬 = 판정값, NULL이면 역할별 기본 정렬
(`pipeline/data/style_defaults.json`). ⑦ 결과가 없는 섹션(⑦ 실패)은 역할별 기본 정렬. ③ 블록 구성은 v1.0.1 결과 그대로다.

오류 정의는 2026-11-03 조사와 같다 — 평가 가능(라벨 left · center · right) 블록에서 적용 정렬 ≠ 라벨.
출력: `<out>/summary.json`(허용 오차별 집계) · `<out>/rows_<tol>.json`(블록별).
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline.stages import style  # noqa: E402

ALIGNS = ("left", "center", "right")


def _blocks(row: dict) -> NS:
    lines = []
    for ln in row.get("source_lines") or []:
        regs = [NS(text=r["text"], bbox=NS(x=r["bbox"]["x"], x2=r["bbox"]["x"] + r["bbox"]["w"])) for r in ln["regions"]]
        lines.append(NS(regions=regs))
    return NS(source_lines=lines)


def _style_results(d: dict) -> dict[int, dict[int, str]]:
    """section_id → {block id → ⑦ status}(당시 현재 · 완료 시도만)."""
    tasks = {a["id"]: a for a in d["job_async_task"]}
    out: dict[int, dict[int, str]] = {}
    for h in d["task_handoff"]:
        a = tasks.get(h["task_id"])
        p = h.get("payload") or {}
        if a and a["stage"] == "style" and a["is_current"] and h["outcome"] == "done" and p.get("style"):
            out[int(a["unit_id"])] = {int(b["block_key"].split("_")[1]): b["status"] for b in p["style"]["blocks"]}
    return out


def _tsv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--rows", required=True, type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--human", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tol", nargs="+", type=float, required=True)
    a = ap.parse_args(argv)

    rows = json.loads(a.rows.read_text(encoding="utf-8"))
    labels = {(r["gs"], int(r["id"])): r["label"] for r in _tsv(a.labels)}
    assert all(labels[(r["gs"], r["id"])] == r["label"] for r in rows), "align_rows 라벨과 라벨 TSV가 다르다"
    human = {(r["gs"], int(r["block_id"])): r["verdict"] for r in _tsv(a.human) if r["verdict"]}
    defaults = json.loads((ROOT / "pipeline" / "data" / "style_defaults.json").read_text(encoding="utf-8"))["pipeline_mapping"]["align"]

    ev = {gs: json.loads((a.run / "evidence" / f"{gs}.json").read_text(encoding="utf-8")) for gs in sorted({r["gs"] for r in rows})}
    width = {(gs, s["id"]): s["bbox"]["w"] for gs, d in ev.items() for s in d["section"]}
    tb = {(gs, b["id"]): b for gs, d in ev.items() for b in d["text_block"]}
    by_sec: dict[tuple[str, int], list[dict]] = collections.defaultdict(list)
    for (gs, _), b in sorted(tb.items(), key=lambda kv: (kv[0][0], kv[1]["section_id"], kv[1]["block_order"], kv[1]["id"])):
        by_sec[(gs, b["section_id"])].append(b)
    styled = {(gs, sid): st for gs, d in ev.items() for sid, st in _style_results(d).items()}

    a.out.mkdir(parents=True, exist_ok=True)
    summary = {"inputs": {"run": str(a.run), "rows": str(a.rows), "blocks": len(rows),
                          "sections": len({(r["gs"], r["section_id"]) for r in rows}),
                          "no_style_result_blocks": sum((r["gs"], r["section_id"]) not in styled for r in rows)},
               "defaults": defaults, "old": None, "by_tol": {}}
    for tol in a.tol:
        judged: dict[tuple[str, int], tuple] = {}
        for key, blocks in by_sec.items():
            st = styled.get(key)
            if st is None:
                continue
            spans = {f"blk_{b['id']}": style.block_span(_blocks(b)) for b in blocks if st.get(b["id"]) not in (None, "excluded")}
            for bkey, v in style.section_align(spans, width[key], tol).items():
                judged[(key[0], int(bkey.split("_")[1]))] = v
        out_rows = []
        for r in rows:
            k = (r["gs"], r["id"])
            align, basis, reason, diag = judged.get(k, (None, None, "no_style_result", None))
            applied = align or defaults[r["role"]]
            ok_eval = r["label"] in ALIGNS
            out_rows.append({
                "gs": r["gs"], "id": r["id"], "section_id": r["section_id"], "role": r["role"], "src_lines": r["src_lines"],
                "label": r["label"], "human": human.get(k), "old_applied": r["applied_align"], "old_basis": r["align_basis"],
                "old_error": r["error"], "new_align": align, "new_basis": basis or f"null:{reason}", "new_applied": applied,
                "new_error": ok_eval and applied != r["label"], "evaluable": ok_eval,
                "scores": diag["scores"].model_dump() if diag else None, "section_center": diag["section_center"] if diag else None,
                "basis_note": r.get("basis_note"),
            })
        (a.out / f"rows_{tol:g}.json").write_text(json.dumps(out_rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        summary["by_tol"][f"{tol:g}"] = _summarize(out_rows)
        if summary["old"] is None:
            summary["old"] = _summarize(out_rows, key="old")
    (a.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def _rate(rows: list[dict], err: str) -> dict:
    e = [r for r in rows if r["evaluable"]]
    n = sum(r[err] for r in e)
    return {"evaluable": len(e), "errors": n, "rate": round(n / len(e), 4) if e else None}


def _summarize(rows: list[dict], key: str = "new") -> dict:
    err = f"{key}_error"
    s = {"all": _rate(rows, err),
         "single_line": _rate([r for r in rows if r["src_lines"] == 1], err),
         "multi_line": _rate([r for r in rows if r["src_lines"] > 1], err),
         "by_role": {role: _rate([r for r in rows if r["role"] == role], err) for role in sorted({r["role"] for r in rows})},
         "by_label": {lab: _rate([r for r in rows if r["label"] == lab], err) for lab in ALIGNS}}
    h = [r for r in rows if r["human"] in ALIGNS]
    s["human_sample"] = {"judged": len(h), "errors": sum(r[f"{key}_applied"] != r["human"] for r in h)}
    if key == "new":
        basis = collections.Counter(r["new_basis"] for r in rows)
        s["basis"] = dict(sorted(basis.items()))
        s["by_basis"] = {b: _rate([r for r in rows if r["new_basis"] == b], err) for b in sorted(basis)}
        s["fixed_vs_old"] = sum(r["evaluable"] and r["old_error"] and not r["new_error"] for r in rows)
        s["broken_vs_old"] = sum(r["evaluable"] and not r["old_error"] and r["new_error"] for r in rows)
        s["confusion"] = dict(sorted(collections.Counter(f"{r['label']}→{r['new_applied']}" for r in rows
                                                         if r["evaluable"] and r["new_error"]).items()))
    return s


if __name__ == "__main__":
    raise SystemExit(main())
