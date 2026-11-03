"""⑨ 넘침 판정 육안 검수 결과 집계(2026-10-30) — 개발용, 운영 경로 아님.

    python docs/ai-experiments/tools/typeset_overflow_review_aggregate.py \
        --blocks docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow/blocks.json \
        --tsv <typeset_overflow_review_<지문>.tsv> [--out <summary.json>]

입력: `typeset_overflow_probe.py`의 `blocks.json`과 `typeset_overflow_review_page.py`에서 내보낸 TSV. 읽기만 한다.
TSV 검증은 `load_review`가 맡고 `typeset_overflow_rule_compare.py`도 같은 함수를 쓴다. 아래 중 하나라도 어기면 결과 파일을 만들지 않고 종료 코드 1:
- 필수 열(case_no · gs · block_id · verdict · note · review_fingerprint)이 있다
- 모든 행의 review_fingerprint가 blocks.json의 SHA-256 앞 16자리와 같다(다른 입력으로 만든 페이지의 검수를 섞지 않는다)
- (gs, block_id)가 검수 표본(`pick`)에 있다
- verdict가 inside · outside · unsure · 빈 값(미검수) 중 하나다
- 같은 (gs, block_id) 행이 두 번 나오지 않는다(덮어쓰기로 판단이 바뀌지 않게 한다)

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
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from typeset_overflow_review_page import pick  # noqa: E402

CLS = ("leading", "edge", "wrap", "width", "fit")
VERDICTS = ("inside", "outside", "unsure", "unreviewed")
TSV_COLUMNS = ("case_no", "gs", "block_id", "verdict", "note", "review_fingerprint")
TSV_VERDICTS = ("inside", "outside", "unsure", "")


class ReviewInputError(ValueError):
    """검수 TSV가 blocks.json · 검수 표본과 맞지 않는다. 집계하지 않는다."""


@dataclass(frozen=True)
class Review:
    fingerprint: str
    rows: list[dict]
    blocks: dict
    sample: list[dict]
    verdict: dict  # (gs, block_id) → inside | outside | unsure (미검수는 넣지 않음)
    notes: list[dict]


def load_review(blocks_path: Path, tsv_path: Path) -> Review:
    """blocks.json과 검수 TSV를 읽어 검증한다. 문제를 모두 모아 ReviewInputError 하나로 보고한다."""
    raw = blocks_path.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    rows = json.loads(raw.decode("utf-8"))
    blocks = {(r["gs"], r["id"]): r for r in rows}
    sample = pick(rows)
    in_sample = {(r["gs"], r["id"]) for r in sample}
    verdict, notes, problems, seen = {}, [], [], set()
    with tsv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        missing = [c for c in TSV_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ReviewInputError(f"TSV 필수 열 없음: {missing}")
        for n, row in enumerate(reader, 2):
            if row["review_fingerprint"] != fp:
                problems.append(f"{n}행: 지문 {row['review_fingerprint']!r} ≠ blocks.json {fp}")
                continue
            try:
                k = (row["gs"], int(row["block_id"]))
            except ValueError:
                problems.append(f"{n}행: block_id {row['block_id']!r}가 정수가 아님")
                continue
            if k not in in_sample:
                problems.append(f"{n}행: {k}는 검수 표본에 없음")
                continue
            if k in seen:
                problems.append(f"{n}행: {k} 중복")
                continue
            seen.add(k)
            if row["verdict"] not in TSV_VERDICTS:
                problems.append(f"{n}행: verdict {row['verdict']!r}는 허용값 {TSV_VERDICTS} 밖")
                continue
            if row["verdict"]:
                verdict[k] = row["verdict"]
            if row["note"].strip():
                notes.append({"case_no": int(row["case_no"]), "gs": k[0], "block_id": k[1], "cls": blocks[k]["cls"],
                              "verdict": row["verdict"] or None, "note": row["note"]})
    if problems:
        shown = "; ".join(problems[:5]) + (f" 외 {len(problems) - 5}건" if len(problems) > 5 else "")
        raise ReviewInputError(f"검수 TSV 문제 {len(problems)}건 — 집계하지 않는다: {shown}")
    return Review(fingerprint=fp, rows=rows, blocks=blocks, sample=sample, verdict=verdict, notes=notes)


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
    try:
        rv = load_review(a.blocks, a.tsv)
    except ReviewInputError as e:
        print(e, file=sys.stderr)
        return 1
    fp, blocks, sample, verdict, notes = rv.fingerprint, rv.blocks, rv.sample, rv.verdict, rv.notes
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
