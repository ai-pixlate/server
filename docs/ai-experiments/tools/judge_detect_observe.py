"""③-1a 검출 전용 관찰값 집계 + 원본별 규제 분류 후보(2026-09-29). 판정 · 정답 · 오탐률이 아니라 관찰값만 낸다.

    python docs/ai-experiments/tools/judge_detect_observe.py --input pipeline/samples/local/blocks-input-v1 --out pipeline/out/judge-detect-obs_<date>

출력: <out>/sections.json(섹션별 텍스트 길이 · 후보 항목 수 · 매칭 수) · items.json(항목별 매칭 수 · 섹션 수 · 원문 예시) ·
regulatory_class_candidates.json(원본별 후보 분류 · 근거 문구 · 확인 필요) · summary.json(입력 묶음 · 사전 해시 · 매칭 규칙 버전).
규제 분류 후보는 텍스트 지표로 만든 **가정**이며 사람이 확인하기 전에는 확정이 아니다("대부분 cosmetic, 선크림은 otc"를 규칙으로 쓰지 않는다).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import judge  # noqa: E402

SUN_STRONG = [r"SPF\s*\d", r"PA\+{1,4}", r"자외선\s*차단", r"선크림", r"썬크림", r"선스틱", r"썬스틱", r"선쿠션", r"선세럼", r"선로션", r"sunscreen", r"UV\s*차단"]
SUN_WEAK = [r"자외선", r"UVA", r"UVB", r"백탁"]
OTC_OTHER = [r"여드름\s*치료", r"의약품", r"OTC", r"모노그래프"]


def _text(blocks) -> str:
    return "\n".join(b.source_ko for b in blocks)


def _find(patterns, text):
    hits = []
    for p in patterns:
        for m in re.finditer(p, text, flags=re.IGNORECASE):
            s = max(0, m.start() - 15)
            hits.append(text[s:m.end() + 15].replace("\n", " "))
            if len(hits) >= 3:
                return hits
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args(argv)
    cfg = cfgmod.load_config(overrides=a.set)
    dicts = judge.load_dicts(cfg)
    view = dicts.judge_view()
    inp, out = Path(a.input), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    sections = []
    item_matches: Counter = Counter()
    item_sections: Counter = Counter()
    examples: dict[str, list[dict]] = defaultdict(list)
    source_text: dict[str, list[str]] = defaultdict(list)
    for src_dir in sorted(p for p in inp.iterdir() if p.is_dir() and (p / "merge").is_dir()):
        for mp in sorted((src_dir / "merge").glob("*.json")):
            mr = jsonio.load_merge(mp)
            res = judge.detect_only(mr.section_key, mr.blocks, cfg, dicts=dicts)
            text = _text(mr.blocks)
            source_text[src_dir.name].append(text)
            refs = Counter(m.dictionary_ref for m in res.matches)
            for ref, n in refs.items():
                item_matches[ref] += n
                item_sections[ref] += 1
            for m in res.matches:
                if len(examples[m.dictionary_ref]) < 12:
                    blk = next(b for b in mr.blocks if b.block_key == m.block_key)
                    s, e = m.raw_span.start, m.raw_span.end
                    examples[m.dictionary_ref].append({
                        "source": src_dir.name, "section": mr.section_key, "block": m.block_key, "pattern": m.pattern,
                        "context": blk.source_ko[max(0, s - 12):s] + "【" + blk.source_ko[s:e] + "】" + blk.source_ko[e:e + 12],
                    })
            sections.append({
                "source": src_dir.name, "section": mr.section_key, "blocks": len(mr.blocks), "text_chars": len(text),
                "text_chars_normalized": len(judge.normalize_with_offsets(text)[0]),
                "matches": len(res.matches), "candidate_items": sorted(refs), "candidate_item_count": len(refs),
                "lc_candidate_count": sum(1 for r in refs if r.startswith("LC-")),
            })
    items = [{"item": i.id, "name": i.name, "dict_type": i.dict_type, "patterns": len(i.patterns_ko),
              "matches": item_matches.get(i.id, 0), "sections": item_sections.get(i.id, 0), "examples": examples.get(i.id, [])}
             for i in view.items]

    candidates = []
    for src, texts in sorted(source_text.items()):
        text = "\n".join(texts)
        strong, weak, other = _find(SUN_STRONG, text), _find(SUN_WEAK, text), _find(OTC_OTHER, text)
        if strong:
            cand, why, check = "otc", "선크림 지표(SPF · PA · 자외선 차단 · 선크림)", False
        elif weak or other:
            cand, why, check = "cosmetic", "약한 지표만(자외선 · UVA/UVB 언급 등) — 선크림인지 텍스트로 확정 불가", True
        else:
            cand, why, check = "cosmetic", "선크림 · OTC 지표 없음", False
        candidates.append({"source": src, "candidate": cand, "basis": why, "needs_review": check,
                           "evidence_strong": strong, "evidence_weak": weak, "evidence_other": other,
                           "note": "텍스트 지표로 만든 가정. 사람이 확인하기 전 결과는 '가정한 분류에 따른 정책 동작 실험'이다"})

    n_sec = len(sections)
    summary = {
        "input": str(inp), "sections": n_sec, "sources": len(source_text),
        "dictionary_version": dicts.dictionary_version, "dictionary_fingerprint": dicts.fingerprint,
        "match_rules_version": judge.MATCH_RULES_VERSION, "match_mode": cfg["judge"]["match_mode"], "call_scope": cfg["judge"]["call_scope"],
        "sections_with_any_match": sum(1 for s in sections if s["matches"]),
        "sections_with_lc_match": sum(1 for s in sections if s["lc_candidate_count"]),
        "sections_without_text": sum(1 for s in sections if s["text_chars"] == 0),
        "text_chars_total": sum(s["text_chars"] for s in sections),
        "text_chars_median": sorted(s["text_chars"] for s in sections)[n_sec // 2] if n_sec else 0,
        "note": "관찰값. 정답 라벨이 없으므로 매칭 수는 오탐 수 · 오탐률이 아니다. call_scope=all이면 매칭 없는 섹션도 LLM 호출 대상이다",
    }
    for name, obj in (("sections", sections), ("items", items), ("regulatory_class_candidates", candidates), ("summary", summary)):
        (out / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
