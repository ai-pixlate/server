"""⑨ 넘침 판정 육안 검수 결과 집계(2026-10-30) — 개발용, 운영 경로 아님.

    python docs/ai-experiments/tools/typeset_overflow_review_aggregate.py \
        --blocks docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow/blocks.json \
        --tsv <typeset_overflow_review_<지문>.tsv> [--out <summary.json>]

입력: `typeset_overflow_probe.py`의 `blocks.json`과 `typeset_overflow_review_page.py`에서 내보낸 TSV. 읽기만 한다.
TSV의 review_fingerprint가 blocks.json의 SHA-256 앞 16자리와 다르면 거부한다(다른 입력으로 만든 페이지의 검수).

집계: 검수 표본(검수 페이지와 같은 `pick` — 64개) 전체에 대해 도구 분류(leading · edge · wrap · width · fit) × 사람 판단
(inside · outside · unsure · 미검수) 교차표, 기록 overflow 기준 판정 일치,
원문 1줄 · 번역 1줄 블록의 같은 교차표, 메모가 있는 사례 목록.
- "판정 오류"는 기록 overflow와 사람 판단이 반대인 경우만 센다(overflow=true · inside, overflow=false · outside). unsure · 미검수는 따로 센다.
- 사람 판단은 검수 표본(전수 아님)에 대한 것이다. 표본 밖 블록으로 넓혀 말하지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from typeset_overflow_review_page import pick  # noqa: E402

CLS = ("leading", "edge", "wrap", "width", "fit")
VERDICTS = ("inside", "outside", "unsure", "unreviewed")


def table(rows: list[dict], verdict: dict) -> dict:
    t = {c: collections.Counter() for c in CLS}
    for r in rows:
        t[r["cls"]][verdict.get((r["gs"], r["id"]), "unreviewed")] += 1
    return {c: {v: t[c][v] for v in VERDICTS} for c in CLS if sum(t[c].values())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=Path, required=True)
    ap.add_argument("--tsv", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    raw = a.blocks.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    rows = json.loads(raw.decode("utf-8"))
    blocks = {(r["gs"], r["id"]): r for r in rows}
    sample = pick(rows)
    in_sample = {(r["gs"], r["id"]) for r in sample}
    verdict, notes, bad = {}, [], []
    with a.tsv.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter="\t"):
            if row["review_fingerprint"] != fp:
                bad.append(row)
                continue
            k = (row["gs"], int(row["block_id"]))
            if k not in in_sample:
                bad.append(row)
                continue
            if row["verdict"]:
                verdict[k] = row["verdict"]
            if row["note"].strip():
                notes.append({"case_no": int(row["case_no"]), "gs": k[0], "block_id": k[1], "cls": blocks[k]["cls"],
                              "verdict": row["verdict"] or None, "note": row["note"]})
    if bad:
        print(f"입력 지문 {fp}와 맞지 않거나 검수 표본에 없는 행 {len(bad)}개 — 집계하지 않는다", file=sys.stderr)
        return 1
    reviewed = [blocks[k] for k in verdict]
    wrong = [r for r in reviewed if (r["db_overflow"] and verdict[(r["gs"], r["id"])] == "inside")
             or (not r["db_overflow"] and verdict[(r["gs"], r["id"])] == "outside")]
    one = [r for r in sample if r["src_lines"] == 1 and r["tr_lines"] == 1]
    s = {
        "review_fingerprint": fp,
        "sample": len(sample),
        "reviewed": len(verdict),
        "verdict": dict(collections.Counter(verdict.values())),
        "cls_x_verdict": table(sample, verdict),
        "overflow_judgment": {
            "agree": sum(1 for r in reviewed if verdict[(r["gs"], r["id"])] == ("outside" if r["db_overflow"] else "inside")),
            "wrong": len(wrong),
            "unsure": sum(1 for r in reviewed if verdict[(r["gs"], r["id"])] == "unsure"),
            "wrong_blocks": [{"gs": r["gs"], "block_id": r["id"], "cls": r["cls"], "db_overflow": r["db_overflow"],
                              "src_lines": r["src_lines"], "tr_lines": r["tr_lines"], "same_text": r["same_text"]} for r in wrong],
        },
        "src_1_tr_1_cls_x_verdict": table(one, verdict),
        "notes": sorted(notes, key=lambda n: n["case_no"]),
    }
    text = json.dumps(s, ensure_ascii=False, indent=1)
    if a.out:
        a.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
