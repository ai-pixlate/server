"""현재 번역문 검사 — 규제 표현 영어 재대조 · 강제 용어 적용 [contract.md 5.1 · 7.1 · 7.2 · 통합 D8 · D9-2 · 5.23 · 5.25].

규칙 기반(모델 호출 없음). ⑧ 직후와 N5 사용자 수정 뒤 재검증에 같은 함수를 쓴다. 검사 대상은 한 블록의 **현재 revision 번역문**이다.
- 규제: 적용 분류 RG 행의 variant_en을 영어 규칙으로 매칭하고 감싸기 억제(5.23) 후 남은 비허용 매칭마다 compliance_flags 후보
  (type=regulatory · producer=translation_validation · detected_in=translation · detected_by=rule). 검출은 위반 확정이 아니다.
  영어 패턴이 없는 항목은 검사하지 못한 것으로 따로 기록한다(문제 없음으로 표시하지 않음, contract 7.2).
- 강제 용어: 원문에서 유효한 enforced 용어(`pipeline.glossary` 선별 규칙 — 공통 한국어 매칭 + 같은 블록 강제 용어끼리 위치별
  포함 억제, BE 공급과 같은 함수)마다 term_target이 번역문에 있어야 한다(영어 규칙 · 대소문자 무시 · 활용형 허용 없음).
  없으면 mandatory_term_unapplied 후보. 긴 강제 용어에 포함된 위치뿐인 짧은 강제 용어는 적용 대상이 아니다(통합 D10).
  term_target에 대안 구분 문자('; ' · '/')가 있으면 해석 규칙이 정해지지 않았으므로 unverifiable로 남긴다(적용/미적용을 추정하지 않음).
- 번역문 LLM 규제 판정은 이 모듈이 하지 않는다 — 독립된 검사 `stages/translation_review.py`(사용자 결정 2026-10-12)가 따로 하며,
  서로의 결과를 입력으로 받거나 지우지 않는다. `complete`는 검사 범위 완결성(영어 패턴 없는 항목 0건 ∧ unverifiable 용어 0건)이다.
  현지 판정을 번역문에 다시 적용하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline import glossary as gl
from pipeline.matching import MATCH_RULES_VERSION, PatternSpec, find_matches, resolve_overlaps

# 원문 적용 대상은 glossary-select 규칙 · 번역문 term_target 영어 규칙 · 대안 구분 문자는 unverifiable
TERM_RULES_VERSION = "term-check@2026-10-11.1"
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
    # 공급과 같은 선별(블록 하나) — 정규화 원문 충돌은 GlossaryDataError(호출자가 검사 실패로 기록)
    applicable = {e.glossary_id for e in gl.select([(block_key, source_ko)],
                                                   [gl.Entry(t.glossary_id, t.term_ko, t.term_target, t.enforcement) for t in terms])}
    for t in terms:
        if t.enforcement != "enforced" or t.glossary_id not in applicable:
            continue  # 원문에 없거나 긴 강제 용어에 포함된 위치뿐이면 적용 대상이 아니다
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
        "rules": {"match": MATCH_RULES_VERSION, "terms": TERM_RULES_VERSION, "glossary_select": gl.SELECT_RULES_VERSION},
    }
