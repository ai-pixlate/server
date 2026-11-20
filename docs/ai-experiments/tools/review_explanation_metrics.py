"""번역문 LLM 규제 판정 판단 이유 — 프롬프트별 자동 점검 집계(보조 지표, 2026-11-19).

    python docs/ai-experiments/tools/review_explanation_metrics.py --reg <regulation.json> --run <runs 폴더> [--variants v1,v2,v3] [--examples]

화면 경고 후보(위험 · 판단 불가) 설명만 센다. 패턴 검사이며 설명의 정확성을 판정하지 않는다.
- 영어 노출: 그 블록 번역문에 있는 영어 단어(3자 이상, 규격 표기 제외)가 설명에 들어갔는지 · 따옴표 인용이 있는지
- 문장 틀: 위험 → "읽힐 수 있어 문제가 될 수 있습니다" / 조건 확인 불가 → "확인되지 않아 판단할 수 없" + "쓸 수 있습니다"
- 사전 밖 분류어: 사전 사유에 없는 "의약품" · "질병" · "질환" · "치료"
- 단정 표현 · 내부 ID · 세 문장 이상 · 조문 표시(근거에 조문이 있는데 설명에 없는지 · 설명이나 확인할 조건에 조문이 남았는지)
- 판정 일치: 같은 (블록 × 항목)에서 v1과 판정이 다른 쌍
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_explanation_probe as probe  # noqa: E402

ALLOW_EN = {"spf", "uva", "uvb", "pfa", "otc", "m020"}
WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
QUOTE = re.compile(r"['‘’\"“”][A-Za-z][^'‘’\"“”]{1,}['‘’\"“”]")
ID_RE = re.compile(r"(?<![A-Za-z0-9])(?:[tra]\d+(?:\.e\d+)?|RG-\d+)(?![A-Za-z0-9])")
ASSERT_RE = re.compile(r"위반|불법|준수하지 않|해당합니다|금지됩니다|위험합니다")
DRIFT = ("의약품", "질병", "질환", "치료")
SENT = re.compile(r"[.!?。](?:\s|$)")


def checks(j: dict, text: str, item: dict) -> dict[str, bool]:
    r = j["reason"]
    en_words = {w.lower() for w in WORD.findall(text)} - ALLOW_EN
    leaked = [w for w in WORD.findall(r) if w.lower() in en_words]
    cond = j["undetermined_reason"] == "condition_unverifiable"
    frame = ("확인되지 않아 판단할 수 없" in r and "쓸 수 있습니다" in r) if cond else ("읽힐 수 있어 문제가 될 수 있습니다" in r)
    articles = [e["article"] for e in item.get("evidence", []) if e.get("article")]
    articles += re.findall(r"§\s*M?\d[\w.]*(?:\([\w]+\))*", item.get("reason", ""))  # 사전 사유 속 조문 표기도 공급된 조문이다
    return {
        "english": bool(leaked), "quote": bool(QUOTE.search(r)), "frame_miss": not frame,
        "drift": any(w in r and w not in item.get("reason", "") for w in DRIFT),
        "assert": bool(ASSERT_RE.search(r)), "ids": bool(ID_RE.search(r)),
        "over2": len([s for s in SENT.split(r.strip()) if s.strip()]) > 2,
        "no_article": bool(articles) and not any(a.replace(" ", "") in r.replace(" ", "") for a in articles),
        # 설명 · 확인할 조건에 조문 표기(§ · 공급 조문 · M020 같은 모노그래프 번호)가 남았는지 — 조문을 빼는 프롬프트(v4)용
        "has_article": any("§" in x or "M020" in x or any(a.replace(" ", "") in x.replace(" ", "") for a in articles)
                           for x in [r, *j["unverified_conditions"]]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reg", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--variants", default="v1,v2,v3,v4")
    ap.add_argument("--examples", action="store_true", help="걸린 설명을 함께 출력한다")
    a = ap.parse_args()
    variants = a.variants.split(",")
    bundle = probe.to_bundle(json.loads(a.reg.read_text(encoding="utf-8")))
    reqs: dict[tuple[str, str], dict] = {}
    stat = {v: Counter() for v in variants}
    judg: dict[tuple, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    examples = defaultdict(list)
    for f in sorted(a.run.glob("*.report.json")):
        v, s, c, _ = f.name[: -len(".report.json")].split("__")
        if v not in variants:
            continue
        if (s, c) not in reqs:
            reqs.update({(s, k): r for k, r in probe.requests_of(s, a.run, bundle).items()})
        d = json.loads(f.read_text(encoding="utf-8"))
        stat[v]["calls"] += 1
        if d["outcome"] != "completed":
            stat[v]["failed:" + d["failure"]["kind"]] += 1
            continue
        texts = {t["block_key"]: t["text"] for t in reqs[(s, c)]["targets"]}
        items = {i["external_id"]: i for i in d["payload"]["supplied_items"]["check"]}
        for b in d["payload"]["blocks"]:
            for j in b["judgments"]:
                judg[(s, c, b["block_key"], j["external_id"])][v].add(j["judgment"])
                if j["judgment"] == "no_risk":
                    continue
                stat[v]["shown"] += 1
                stat[v]["chars"] += len(j["reason"])
                for k, hit in checks(j, texts[b["block_key"]], items[j["external_id"]]).items():
                    if hit:
                        stat[v][k] += 1
                        examples[(v, k)].append(j["reason"])
    for v in variants:
        if v != "v1":
            both = [x for x in judg.values() if "v1" in x and v in x]
            stat[v]["pairs_vs_v1"] = len(both)
            stat[v]["differ_vs_v1"] = sum(1 for x in both if x["v1"] != x[v])
        stat[v]["unstable"] = sum(1 for x in judg.values() if len(x.get(v, ())) > 1)
    keys = ["calls", "shown", "chars", "english", "quote", "frame_miss", "drift", "assert", "ids", "over2", "no_article", "has_article",
            "unstable", "pairs_vs_v1", "differ_vs_v1"]
    fails = sorted({k for v in variants for k in stat[v] if k.startswith("failed:")})
    print("\t".join(["항목"] + variants))
    for k in keys + fails:
        print("\t".join([k] + [str(round(stat[v][k] / stat[v]["shown"]) if k == "chars" and stat[v]["shown"] else stat[v][k])
                               for v in variants]))
    if a.examples:
        for (v, k), xs in sorted(examples.items()):
            print(f"\n## {v} {k} ({len(xs)})")
            for x in xs[:6]:
                print(" -", x)


if __name__ == "__main__":
    main()
