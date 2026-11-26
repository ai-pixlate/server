"""A+ 압축 뒤 규칙 검사 — 압축이 바꾼 문구에 원본 규격 유지 경로의 번역문 규칙 검사를 그대로 돌린다. 개발용 v1(2026-11-26, 운영 계약 아님. open-questions.md #96).

규제 재검증(aplus.md 3절) 가운데 **규칙 검사**만 한다(사용자 결정 2026-11-26). 번역문 LLM 규제 판정은 하지 않는다.
- 자리: 압축 다음의 독립 단계다. 받는 것은 압축 결과(`AplusCompressResult`) · 출처 블록의 한국어 원문 · 적용 분류의 규제 행 · 용어 목록이다.
- 결과는 **기록만** 한다. 문구를 바꾸지 않고, 압축의 채택을 되돌리지 않는다(원본 경로의 원칙 — 검사 결과로 번역문을 바꾸지 않는다).
- 대상: 압축이 문구를 바꾼 칸(`outcome=compressed`)뿐이다. 바뀌지 않은 칸의 문구는 블록의 1차 번역 그대로이므로 여기서 다시 검사하지 않는다.
  여러 블록을 이어 붙인 칸도 다시 검사하지 않는다(원본 규칙은 블록을 넘어 매칭하지 않는다).
- 검사: `stages.text_check.check_block` 그대로 — 영어 규제 표현 재대조와 강제 용어 적용 여부. 새 규칙을 만들지 않는다.
  규제 행은 호출자가 적용 분류로 고른 것을 준다. 용어 목록을 받지 못하면(`terms=None`) 강제 용어를 검사하지 않고 그 사실을 남긴다
  (`terms_checked=False` · 칸마다 `complete=False`) — 빈 용어 목록(적용할 강제 용어 없음)과 다르다.
- 칸 기록의 `flags`에는 `revision`이 없다. 압축 문구는 블록의 어느 리비전도 아니다(칸 단위 식별은 BE 확인 필요 — #96).

사전 · 용어 데이터 오류(같은 구간의 금지/허용 충돌 · 빈 패턴 · 강제 용어 충돌)는 예외가 아니라 `status=failed` 결과다 — 통과로 바꾸지 않고,
압축 결과의 문구는 그대로다. 입력 오류(실패한 압축 결과 · 출처 블록이 1개가 아닌 바뀐 칸 · 원문이 없는 블록 · 칸 문구와 기록 불일치)는 예외다.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from pipeline import glossary as gl
from pipeline.aplus import compress
from pipeline.aplus.label import Recorder, _emit
from pipeline.matching import MATCH_RULES_VERSION, MatchConflictError, PatternError
from pipeline.stages import text_check
from pipeline.types import AplusCompressedField, AplusCompressResult, AplusRecheckedField, AplusRecheckResult

STAGE = "aplus_recheck"
RULES = {"match": MATCH_RULES_VERSION, "terms": text_check.TERM_RULES_VERSION, "glossary_select": gl.SELECT_RULES_VERSION}


def find_targets(result: AplusCompressResult) -> list[AplusCompressedField]:
    """검사 대상 — 압축이 문구를 바꾼 칸(칸 순서 그대로). 실패한 압축 결과면 ValueError."""
    if result.status != "ok" or result.slots is None:
        raise ValueError("실패한 압축 결과는 검사할 수 없다")
    modules = {s.slot: s.module for s in result.slots if s.module is not None}
    targets = [f for f in result.fields if f.outcome == "compressed"]
    for f in targets:
        where = f"칸 {f.slot} {f.field}"
        if len(f.origin_block_keys) != 1:
            raise ValueError(f"{where}: 출처 블록이 1개가 아닌 바뀐 칸은 검사 규칙이 없다 {f.origin_block_keys}")
        if f.slot not in modules or compress.get_text(modules[f.slot], f.field).text != f.answer_text:
            raise ValueError(f"{where}: 칸의 문구와 압축 기록의 문구가 다르다")
    return targets


def run(result: AplusCompressResult, sources: Mapping[str, str], rows: Sequence[text_check.RegRow],
        terms: Sequence[text_check.GlossaryTerm] | None, *, recorder: Recorder | None = None) -> AplusRecheckResult:
    """상품 하나의 압축 뒤 규칙 검사. `sources` = 블록 키 → 한국어 원문, `rows` = 적용 분류의 규제 행, `terms` = 용어 목록(None = 받지 못함).
    바뀐 칸이 없으면 검사 없이 빈 결과를 돌려준다."""
    targets = find_targets(result)
    missing = [f.origin_block_keys[0] for f in targets if f.origin_block_keys[0] not in sources]
    if missing:
        raise ValueError(f"한국어 원문이 없는 출처 블록 {missing}")
    terms_checked = terms is not None
    rec: dict[str, Any] = {
        "stage": STAGE, "status": None, "rules": dict(RULES), "terms_checked": terms_checked, "regulation_rows": len(rows),
        "terms": len(terms) if terms is not None else None,
        "targets": [{"slot": f.slot, "field": f.field, "block_key": f.origin_block_keys[0]} for f in targets], "error": None,
    }
    fields: list[AplusRecheckedField] = []
    try:
        for f in targets:
            key = f.origin_block_keys[0]
            r = text_check.check_block(key, 0, f.answer_text, sources[key], list(rows), list(terms or []))
            fields.append(AplusRecheckedField(
                slot=f.slot, field=f.field, block_key=key, text=f.answer_text, regulatory=r["regulatory"], terms=r["terms"],
                flags=[{k: v for k, v in flag.items() if k != "revision"} for flag in r["flags"]],
                complete=bool(r["complete"]) and terms_checked))
    except (MatchConflictError, PatternError, gl.GlossaryDataError) as e:  # 사전 · 용어 데이터 오류 — 통과로 바꾸지 않는다
        rec.update(status="failed", error=f"{e.__class__.__name__}: {e}")
        _emit(recorder, rec, failed=True)
        return AplusRecheckResult(status="failed", terms_checked=terms_checked, rules=dict(RULES), error=rec["error"])
    rec.update(status="ok", result=[x.model_dump(mode="json") for x in fields])
    _emit(recorder, rec, failed=False)
    return AplusRecheckResult(status="ok", fields=fields, terms_checked=terms_checked, rules=dict(RULES))
