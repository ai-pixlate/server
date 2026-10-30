"""⑨ 넘침 판정 기준별 영향 비교(2026-10-30) — 개발용, 운영 경로 아님. 운영 조판 코드는 바꾸지 않는다.

    python docs/ai-experiments/tools/typeset_overflow_rule_compare.py \
        --blocks docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow/blocks.json \
        --tsv docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow/typeset_overflow_review_<지문>.tsv \
        --out docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow/rule_compare.json

입력: `typeset_overflow_probe.py`의 `blocks.json`과 사람 검수 TSV. 읽기만 한다. blocks.json은 검수 지문의 입력이라 다시 쓰지 않는다.
출력: `rule_compare.json`(블록별 판정은 넣지 않고 집계만).

기준(폭 · 높이는 Pillow 근사 — 운영 조판과 같은 함수 · 같은 위치):
- current : 현행. 한 단어 진행 폭 > 영역 폭, 또는 줄 수 × 행간 > 영역 높이
- a       : 폭은 현행, 높이는 (줄 수 − 1) × 행간 + 글자 크기 > 영역 높이
- glyph   : 글리프 잉크(`font.getbbox`)가 영역 밖이면 넘침, 허용 오차 없음 — 합의 기준(조판 협의 Q-01 · Q-06)의 근사
- glyph_1px : glyph에 1px 허용을 더한 것. Q-06과 맞지 않아 채택 대상이 아니다(비교 참고)

근사의 한계: 운영 Pillow에는 자간 · 커닝이 없고 `font.getbbox`는 Canvas TextMetrics가 아니며 줄바꿈도 브라우저 결과가 아니다.
이 수치는 변경 전 비교 자료이며 브라우저 렌더의 기대값이나 합격 기준으로 쓰지 않는다.

사람 판단 비교: inside · outside 사례만 센다(unsure 제외). 과잉 = 사람 inside인데 넘침, 누락 = 사람 outside인데 넘침 아님.
검수 표본은 leading을 전수 포함한 선별 표본이므로 일치 비율을 491개 전체로 넓히지 않는다.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import typeset as t  # noqa: E402
from typeset_overflow_probe import _layout  # noqa: E402

RULES = ("current", "a", "glyph", "glyph_1px")
EM_RATIOS = (0.9, 0.88, 0.85, 0.833)


def _outside(ink, box, tol):
    """글리프 잉크가 영역 밖인지 (폭, 높이). 소수를 유지하고, 경계와 같으면 안이다."""
    if not ink:
        return False, False
    return (ink[0] < box["x"] - tol or ink[2] > box["x"] + box["w"] + tol,
            ink[1] < box["y"] - tol or ink[3] > box["y"] + box["h"] + tol)


def judge(rows: list[dict]) -> list[dict]:
    defaults = t.load_role_defaults()
    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    out = []
    for r in rows:
        font = t._font(defaults, r["role"], r["font_px"])
        L = _layout(draw, r["trans_1"], font, r["box"], r["align"], r["font_px"])
        w, h = _outside(L["ink"], r["box"], 0)
        w1, h1 = _outside(L["ink"], r["box"], 1)
        out.append({"r": r, "current": L["overflow"],
                    "a": bool(L["too_wide"] or (L["lines"] - 1) * L["line_h"] + r["font_px"] > r["box"]["h"]),
                    "glyph": w or h, "glyph_1px": w1 or h1,
                    "kind": "both" if w and h else "width" if w else "height" if h else "none"})
    return out


def compare(rows: list[dict], verdict: dict) -> dict:
    C = collections.Counter
    one = [x for x in rows if x["r"]["src_lines"] == 1]
    decided = [x for x in rows if verdict.get((x["r"]["gs"], x["r"]["id"])) in ("inside", "outside")]
    table = {}
    for k in RULES:
        over = [x for x in decided if x[k] and verdict[(x["r"]["gs"], x["r"]["id"])] == "inside"]
        miss = [x for x in decided if not x[k] and verdict[(x["r"]["gs"], x["r"]["id"])] == "outside"]
        table[k] = {"overflow": sum(x[k] for x in rows), "source_1_line": sum(x[k] for x in one),
                    "tr_1_line": sum(x[k] for x in rows if x["r"]["tr_lines"] == 1),
                    "human_agree": len(decided) - len(over) - len(miss), "human_over": len(over), "human_miss": len(miss),
                    "over_blocks": [f'{x["r"]["gs"]}#{x["r"]["id"]}:{x["r"]["cls"]}' for x in over],
                    "miss_blocks": [f'{x["r"]["gs"]}#{x["r"]["id"]}:{x["r"]["cls"]}' for x in miss]}
    tr1 = [x for x in one if x["r"]["tr_lines"] == 1]
    trn = [x for x in one if x["r"]["tr_lines"] >= 2]
    return {
        "decided": len(decided), "rules": table,
        "glyph_source_1_line": {"tr_1_line": dict(C(x["kind"] for x in tr1)), "tr_multi_line": dict(C(x["kind"] for x in trn)),
                                "tr_1_line_same_text": dict(C(x["kind"] for x in tr1 if x["r"]["same_text"])),
                                "released": dict(C(x["r"]["cls"] for x in one if x["current"] and not x["glyph"]))},
        "glyph_tr_1_line_below": sorted(({"block": f'{x["r"]["gs"]}#{x["r"]["id"]}', "over": round(x["r"]["ink_bottom_over"], 3),
                                          "text": x["r"]["trans_1"]} for x in tr1 if x["r"]["ink_bottom_over"] > 0), key=lambda v: v["over"]),
        "a_released_outside": [{"block": f'{x["r"]["gs"]}#{x["r"]["id"]}', "cls": x["r"]["cls"], "over": round(x["r"]["ink_bottom_over"], 3)}
                               for x in rows if x["current"] and not x["a"] and x["r"]["cls"] in ("wrap", "edge")],
        "em_ratio_tr_1_line_height_over": {str(e): sum(x["r"]["one_line_h_ratio"] * e / 0.9 > 1 for x in rows if x["r"]["tr_lines"] == 1)
                                           for e in EM_RATIOS},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=Path, required=True)
    ap.add_argument("--tsv", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows = judge(json.loads(a.blocks.read_text(encoding="utf-8")))
    with a.tsv.open(encoding="utf-8", newline="") as f:
        verdict = {(r["gs"], int(r["block_id"])): r["verdict"] for r in csv.DictReader(f, delimiter="\t") if r["verdict"]}
    s = compare(rows, verdict)
    text = json.dumps(s, ensure_ascii=False, indent=1)
    a.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
