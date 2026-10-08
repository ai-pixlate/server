"""현재 번역문 검사 — 규제 표현 영어 재대조 · 강제 용어 적용 [contract.md 5.1 · 7.1 · 7.2 · 통합 D8 · D9-2 · 5.23 · 5.25].

규칙 기반(모델 호출 없음). ⑧ 직후와 N5 사용자 수정 뒤 재검증에 같은 함수를 쓴다. 검사 대상은 한 블록의 **현재 revision 번역문**이다.
- 규제: 적용 분류 RG 행의 variant_en을 영어 규칙으로 매칭하고 감싸기 억제(5.23) 후 남은 비허용 매칭마다 compliance_flags 후보
  (type=regulatory · producer=translation_validation · detected_in=translation · detected_by=rule). 검출은 위반 확정이 아니다.
  영어 패턴이 없는 항목은 검사하지 못한 것으로 따로 기록한다(문제 없음으로 표시하지 않음, contract 7.2).
- 강제 용어: enforced 용어의 term_ko가 원문에 있으면(한국어 규칙) term_target이 번역문에 있어야 한다(영어 규칙 · 대소문자 무시 ·
  활용형 허용 없음). 없으면 mandatory_term_unapplied 후보. term_target에 대안 구분 문자('; ' · '/')가 있으면 해석 규칙이 정해지지
  않았으므로 unverifiable로 남긴다(적용/미적용을 추정하지 않음).
- 번역문 LLM 규제 판정(F-TRN-03)은 하지 않는다(D9-2). 현지 판정을 번역문에 다시 적용하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.matching import MATCH_RULES_VERSION, PatternSpec, find_matches, resolve_overlaps

TERM_RULES_VERSION = "term-check@2026-10-08.1"  # 원문 term_ko 한국어 규칙 · 번역문 term_target 영어 규칙 · 대안 구분 문자는 unverifiable
_ALTERNATIVE_MARKERS = ("; ", "/")


@dataclass(frozen=True)
class RegRow:
    external_id: str
    verdict_status: str
    variant_en: tuple[str, ...]


@dataclass(frozen=True)
class GlossaryTerm:
    glossary_id: str
    term_ko: str
    term_target: str
    enforcement: str


def check_block(block_key: str, revision: int, text: str, source_ko: str, rows: list[RegRow], terms: list[GlossaryTerm]) -> dict[str, Any]:
    """한 블록 검사 결과. flags는 contract 7.1의 compliance_flags 후보(근거 ID는 원본 external_id · glossary id — DB id 변환은 BE)."""
    specs = [PatternSpec(r.external_id, p) for r in rows for p in r.variant_en]
    raw = find_matches(block_key, text, specs, "en")
    for i, m in enumerate(raw, start=1):
        m.match_key = f"em_{i:03d}"
    allowed = {r.external_id for r in rows if r.verdict_status == "allowed"}
    live, sup = resolve_overlaps(raw, allowed)  # MatchConflictError는 호출자가 검사 실패로 기록한다
    verdict_of = {r.external_id: r.verdict_status for r in rows}
    flags: list[dict[str, Any]] = []
    for m in live:
        flags.append({"type": "regulatory", "producer": "translation_validation", "revision": revision, "external_id": m.external_id,
                      "dictionary_verdict_status": verdict_of[m.external_id], "problem_text": m.matched_text,
                      "start": m.start, "end": m.end, "detected_in": "translation", "detected_by": "rule"})
    term_results = []
    for t in terms:
        if t.enforcement != "enforced":
            continue
        if not find_matches(block_key, source_ko, [PatternSpec(t.glossary_id, t.term_ko)], "ko"):
            continue  # 원문에 용어가 없으면 적용 대상이 아니다
        if any(mk in t.term_target for mk in _ALTERNATIVE_MARKERS):
            term_results.append({"glossary_id": t.glossary_id, "status": "unverifiable",
                                 "reason": "term_target에 대안 구분 문자가 있어 적용 판단 규칙이 정해지지 않았다"})
            continue
        if find_matches(block_key, text, [PatternSpec(t.glossary_id, t.term_target)], "en"):
            term_results.append({"glossary_id": t.glossary_id, "status": "applied"})
        else:
            term_results.append({"glossary_id": t.glossary_id, "status": "unapplied"})
            flags.append({"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": revision,
                          "glossary_id": t.glossary_id})
    return {
        "block_key": block_key, "revision": revision,
        "regulatory": {"checked_ids": sorted(r.external_id for r in rows if r.variant_en),
                       "unchecked_ids": sorted(r.external_id for r in rows if not r.variant_en),
                       "matches": [{"match_key": m.match_key, "external_id": m.external_id, "start": m.start, "end": m.end,
                                    "matched_text": m.matched_text, "patterns": list(m.patterns)} for m in raw],
                       "suppressed": [{"match_key": s.match_key, "rule": s.rule, "by": list(s.by)} for s in sup]},
        "terms": term_results,
        "flags": flags,
        "complete": not any(r["status"] == "unverifiable" for r in term_results) and all(r.variant_en for r in rows),
        "rules": {"match": MATCH_RULES_VERSION, "terms": TERM_RULES_VERSION},
    }
