"""번역문 규제 판정 설명 — 팀 공유용 읽기 전용 요약 페이지 생성(2026-11-19).

    python docs/ai-experiments/tools/review_explanation_share.py --reg <regulation.json> --run <runs 폴더> --out <html>

경고 1건(어느 실행에서든 위험 · 조건 확인 필요로 본 블록 × 사전 항목)마다 원문 · 번역문 · 판정 · AI 설명(현재 프롬프트 · 개선안) ·
사전 사유 · 근거만 싣는다. 내부 필드 이름은 쓰지 않는다. 입력은 review_explanation_probe.py의 저장 보고.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_explanation_probe as probe  # noqa: E402

LABELS = {
    "GS-02_011.sec_1_02": "주름 세럼 · 임상 결과", "GS-02_014.sec_1_06": "레티놀 세트 · 함께 사용 효과",
    "GS-03_002.sec_1_03": "미백 바디로션 · 고객 고민", "GS-03_018.sec_1_10": "미백 바디로션 · 제품 추천",
    "GS-01_002.sec_1_20": "톤업 선크림 · 제품 소개", "GS-01_002.sec_1_03": "선크림 · SPF 표기",
    "GS-01_002.sec_1_12": "선크림 · 피부 테스트", "1/cosmetic": "합성 문장 · 화장품", "1/otc": "합성 문장 · 선크림",
    "2/cosmetic": "합성 문장 · 화장품 2",
}
CLASS_KO = {"cosmetic": "화장품", "otc": "일반의약품(선크림)"}


def collect(run: Path, reg: Path) -> list[dict]:
    bundle = probe.to_bundle(json.loads(reg.read_text(encoding="utf-8")))
    reports: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for f in sorted(run.glob("v[12]__*.report.json"), key=lambda p: (p.name.rsplit("__", 1)[0], int(p.name.rsplit("__", 1)[1].split(".")[0]))):
        v, s, c, _ = f.name[: -len(".report.json")].split("__")
        reports[(s, c)][v].append(json.loads(f.read_text(encoding="utf-8")))
    out = []
    order_sets = {"R": 0, "1": 1, "2": 2}
    for (s, c), by_v in sorted(reports.items(), key=lambda kv: (order_sets[kv[0][0]], list(LABELS).index(kv[0][1] if kv[0][0] == "R" else f"{kv[0][0]}/{kv[0][1]}"))):
        req = probe.requests_of(s, run, bundle)[c]
        texts = {t["block_key"]: t["text"] for t in req["targets"]}
        src = {b["block_key"]: b["source_ko"] for b in req["blocks"]}
        order = [b["block_key"] for b in sorted(req["blocks"], key=lambda b: b["block_order"])]
        supplied, pairs, runs = {}, defaultdict(lambda: defaultdict(list)), {}
        for v, reps in by_v.items():
            runs[v] = {"total": len(reps), "failed": sum(d["outcome"] != "completed" for d in reps)}
            for d in reps:
                if d["outcome"] != "completed":
                    continue
                for it in d["payload"]["supplied_items"]["check"] + d["payload"]["supplied_items"]["reference"]:
                    supplied[it["external_id"]] = it
                for b in d["payload"]["blocks"]:
                    for j in b["judgments"]:
                        pairs[(b["block_key"], j["external_id"])][v].append(j)
        items = []
        for (bk, eid), pv in sorted(pairs.items(), key=lambda kv: (order.index(kv[0][0]), kv[0][1])):
            if not any(j["judgment"] != "no_risk" for js in pv.values() for j in js):
                continue
            it = supplied[eid]
            spans = sorted({(loc["start"], loc["end"]) for js in pv.values() for j in js for loc in j["locations"]})
            per = {}
            for v in ("v1", "v2"):
                js = pv.get(v, [])
                flagged = [j for j in js if j["judgment"] != "no_risk"]
                pick = flagged[0] if flagged else (js[0] if js else None)
                per[v] = {"judgments": [j["judgment"] + ("/cond" if j["undetermined_reason"] == "condition_unverifiable" else "") for j in js],
                          "reason": pick["reason"] if pick else None, "conditions": pick["unverified_conditions"] if pick else [],
                          "quotes": [loc["quote"] for loc in pick["locations"]] if pick else []}
            items.append({"block": bk, "source": "" if s != "R" else src[bk], "translation": texts[bk], "spans": spans,
                          "expression": it["source_expression"], "verdict": it["verdict_status"], "dict_reason": it["reason"],
                          "evidence": [{"type": e["source_type"], "article": e["article"], "quote": e["quote"], "url": e["url"]}
                                       for e in it["evidence"]], "per": per})
        out.append({"id": (c if s == "R" else f"{s}-{c}").replace(".", "-"), "label": LABELS[c if s == "R" else f"{s}/{c}"],
                    "real": s == "R", "class": CLASS_KO.get(req["regulatory_class"], req["regulatory_class"]),
                    "context": [{"block": k, "source": src[k] if s == "R" else "", "translation": texts.get(k)} for k in order],
                    "runs": runs, "items": items})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reg", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    data = collect(a.run, a.reg)
    tpl = (Path(__file__).resolve().parent / "review_explanation_share.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(tpl.replace("/*__DATA__*/[]", payload), encoding="utf-8")
    print(a.out, sum(len(s["items"]) for s in data), "warnings")


if __name__ == "__main__":
    main()
