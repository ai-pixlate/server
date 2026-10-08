"""⑧ 번역 · 번역문 검사 — 운영 인계 [통합 D9-2 · D9-3 · 5.25 · 5.31 · 5.32 ⑧ 재시도].

번역(`run_translate`)
- 대상: BE가 지정한 블록(포함 섹션 · 라벨 false · 로고 false · 비공백 원문). 재시도면 이번 실패 대상만. `preserved`(이미 채택된 성공분)는
  문맥으로만 쓰고 결과를 돌려주지 않는다 — 기존 성공분 · 사용자 수정값을 덮어쓰지 않는다.
- 결과: 대상마다 completed(번역문 · 해시) 또는 failed(사유). 전부 성공 → completed, 일부라도 실패 → failed + 성공분 payload
  (부분 실패), 대상 0 → skipped. 호출 · 응답 구조 실패는 대상 전체 failed다(일부를 임의 성공으로 추출하지 않음).
- 표현 지시는 외부 ID로만 받고 대체 표현은 **고정 묶음에서** 꺼낸다(BE 문자열을 그대로 믿지 않음). 템플릿(자리표시자) 대체 표현 ·
  allowed · conditional 항목은 지시로 받지 않는다 — RG-021/022는 원문 수치를 유지한다(D9).
- 용어 공급 실패는 빈 용어 목록과 다르다: 번역하지 않고 failed(input_invalid).
번역문 검사(`run_text_check`): 블록별 현재 revision 문구의 영어 규제 재대조 · 강제 용어. 번역 계산과 별개 논리 작업이다(5.32).
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, ValidationError

from pipeline.handoff.bundle import BundleError, CheckedBundle, check_bundle, load_type_map
from pipeline.handoff.canonical import sha256_canonical, sha256_text
from pipeline.handoff.envelope import (
    CONTRACT_VERSION,
    HandoffInputError,
    RequestIdentity,
    StageReport,
    _Model,
    check_expected_manifest,
    failed_report,
    identity_fallback,
    manifest_of,
    parse_identity,
)
from pipeline.matching import MATCH_RULES_VERSION, MatchConflictError
from pipeline.stages import text_check
from pipeline.stages import translate as tr
from pipeline.types import TextBlock
from pipeline.vlm import JudgeAssistant, VlmError

TRANSLATE_ADAPTER_VERSION = "translate-handoff@2026-10-08.1"
_PLACEHOLDER = re.compile(r"\[[^\[\]\n]+\]")
SHA256 = r"^[0-9a-f]{64}$"


class TargetRef(_Model):
    block_key: str = Field(min_length=1)
    revision: int = Field(ge=0)


class PreservedRef(_Model):
    block_key: str = Field(min_length=1)
    revision: int = Field(ge=0)
    translation_sha256: str = Field(pattern=SHA256)
    source_attempt_id: str = Field(min_length=1)


class InstructionRef(_Model):
    block_key: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    matched_text: str = Field(min_length=1)


class TermIn(_Model):
    glossary_id: str = Field(min_length=1)
    term_ko: str = Field(min_length=1)
    term_target: str = Field(min_length=1)
    enforcement: Literal["enforced", "reference"]


class GlossarySupply(_Model):
    status: Literal["ok", "failed"]
    version: str | None = None
    reason: str | None = None
    terms: list[TermIn] = Field(default_factory=list)


class TranslateRequest(RequestIdentity):
    stage: Literal["translate"]
    target_country: str = Field(min_length=1)
    target_lang: str = Field(min_length=1)
    regulatory_class: Literal["cosmetic", "otc", "combination", "unknown"]
    section_key: str = Field(min_length=1)
    blocks: list[dict[str, Any]]  # 섹션의 모든 블록(문맥) — TextBlock
    targets: list[TargetRef]
    preserved: list[PreservedRef] = Field(default_factory=list)
    instructions: list[InstructionRef] = Field(default_factory=list)
    glossary: GlossarySupply
    bundle: dict[str, Any]


class CheckTarget(_Model):
    block_key: str = Field(min_length=1)
    revision: int = Field(ge=0)
    text: str  # 검사할 현재 번역문(자동 번역 또는 사용자 수정)


class TextCheckRequest(RequestIdentity):
    stage: Literal["text_check"]
    target_country: str = Field(min_length=1)
    regulatory_class: Literal["cosmetic", "otc", "combination", "unknown"]
    section_key: str = Field(min_length=1)
    blocks: list[dict[str, Any]]
    targets: list[CheckTarget]
    glossary: GlossarySupply
    bundle: dict[str, Any]


def template_tokens(cb: CheckedBundle) -> set[str]:
    """고정 묶음의 대체 표현에 든 자리표시자 토큰(예 '[value]'). 번역 지시 거부 · 번역문 누출 검사에 쓴다."""
    return {m.group(0) for r in cb.regulation.values() for a in r.alternative_expression for m in _PLACEHOLDER.finditer(a)}


def _blocks(raw_blocks: list[dict[str, Any]], section_key: str) -> list[TextBlock]:
    try:
        blocks = [TextBlock.model_validate(b) for b in raw_blocks]
    except ValidationError as e:
        raise HandoffInputError(f"블록 형식 오류: {e}") from e
    keys = [b.block_key for b in blocks]
    if len(set(keys)) != len(keys) or any(b.section_key != section_key for b in blocks):
        raise HandoffInputError("블록 키 중복 또는 다른 섹션 블록")
    return blocks


def _glossary(g: GlossarySupply) -> list[TermIn]:
    if g.status != "ok":
        raise HandoffInputError(f"용어 공급 실패 — 빈 용어 목록으로 처리하지 않는다: {g.reason or '사유 없음'}")
    ids = [t.glossary_id for t in g.terms]
    if len(set(ids)) != len(ids):
        raise HandoffInputError("glossary_id 중복")
    return g.terms


def run_translate(raw: Any, cfg: dict[str, Any], *, llm: JudgeAssistant | None = None) -> StageReport:
    try:
        ident = parse_identity(raw, "translate")
    except (ValidationError, HandoffInputError) as e:
        return failed_report(identity_fallback(raw, "translate"), "input_invalid", f"요청 식별 오류: {e}")
    try:
        req = TranslateRequest.model_validate(raw)
    except ValidationError as e:
        return failed_report(ident, "input_invalid", f"요청 형식 오류: {e}")
    impl = {"adapter": TRANSLATE_ADAPTER_VERSION, "stage": tr.TRANSLATE_IMPL_VERSION}
    try:
        prompt = tr.validate_config(cfg, need_api_key=llm is None)
        impl["model"] = tr.model_config(cfg)
    except ValueError as e:
        return failed_report(ident, "config_invalid", str(e), implementation=impl)
    try:
        cb = check_bundle(req.bundle, target_country=req.target_country, types=load_type_map())
    except BundleError as e:
        return failed_report(ident, "bundle_invalid", str(e), implementation=impl)
    try:
        blocks = _blocks(req.blocks, req.section_key)
        by_key = {b.block_key: b for b in blocks}
        tkeys = [t.block_key for t in req.targets]
        pkeys = [p.block_key for p in req.preserved]
        problems = []
        if len(set(tkeys)) != len(tkeys) or len(set(pkeys)) != len(pkeys):
            problems.append("targets 또는 preserved에 중복")
        if set(tkeys) & set(pkeys):
            problems.append(f"대상과 보존 성공분이 겹친다 {sorted(set(tkeys) & set(pkeys))}")
        unknown = sorted((set(tkeys) | set(pkeys)) - set(by_key))
        if unknown:
            problems.append(f"섹션에 없는 블록 {unknown}")
        blank = [k for k in tkeys if k in by_key and not by_key[k].source_ko.strip()]
        if blank:
            problems.append(f"공백 원문 블록은 번역 대상이 아니다 {blank}")
        if problems:
            raise HandoffInputError("; ".join(problems))
        terms = _glossary(req.glossary)
        tokens = template_tokens(cb)
        instructions = []
        for ins in req.instructions:
            row = cb.regulation.get(ins.external_id)
            if ins.block_key not in set(tkeys):
                raise HandoffInputError(f"지시 대상 {ins.block_key}가 이번 번역 대상이 아니다")
            if row is None:
                raise HandoffInputError(f"지시의 {ins.external_id}가 고정 묶음에 없다")
            if row.verdict_status not in ("rewritable", "regulated") or not row.alternative_expression:
                raise HandoffInputError(f"{ins.external_id}({row.verdict_status}): 대체 표현 지시 대상이 아니다")
            if any(_PLACEHOLDER.search(a) for a in row.alternative_expression):
                raise HandoffInputError(f"{ins.external_id}: 자리표시자 템플릿 대체 표현은 번역 지시로 쓰지 않는다(원문 수치 유지, D9)")
            if ins.matched_text not in by_key[ins.block_key].source_ko:
                raise HandoffInputError(f"{ins.external_id}: matched_text가 블록 원문에 없다")
            instructions.append(tr.Instruction(ins.block_key, ins.external_id, ins.matched_text, tuple(row.alternative_expression)))
        manifest, msha = manifest_of({
            "stage": "translate", "contract_version": CONTRACT_VERSION, "adapter": TRANSLATE_ADAPTER_VERSION,
            "target": {"country": req.target_country, "lang": req.target_lang}, "regulatory_class": req.regulatory_class,
            "section_key": req.section_key,
            "context": [{"block_key": b.block_key, "block_order": b.block_order, "role": b.role, "source_ko": b.source_ko}
                        for b in sorted(blocks, key=lambda b: b.block_order)],
            "targets": [t.model_dump() for t in req.targets], "preserved": [p.model_dump() for p in req.preserved],
            "instructions": [i.model_dump() for i in req.instructions],
            "glossary": {"version": req.glossary.version, "terms": [t.model_dump() for t in terms]},
            "bundle": cb.identity, "model": impl["model"], "prompt_sha256": sha256_text(prompt),
        })
        check_expected_manifest(ident, msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)
    rev = {t.block_key: t.revision for t in req.targets}
    if not req.targets:
        return StageReport(execution_id=ident.execution_id, attempt_id=ident.attempt_id, stage="translate",
                           input_snapshot_id=ident.input_snapshot_id, input_manifest=manifest, input_manifest_sha256=msha,
                           outcome="skipped", target_count=0, skip_reason="no_targets",
                           payload={"section_key": req.section_key, "blocks": [], "preserved": [p.model_dump() for p in req.preserved]},
                           implementation=impl)
    context = [tr.ContextBlock(b.block_key, b.block_order, b.role, b.source_ko) for b in blocks]
    terms_in = [tr.Term(t.glossary_id, t.term_ko, t.term_target, t.enforcement) for t in terms]
    ordered_targets = [b.block_key for b in sorted(blocks, key=lambda b: b.block_order) if b.block_key in rev]
    try:
        outs, diag = tr.translate_section(context, ordered_targets, terms_in, instructions, cfg, target_lang=req.target_lang,
                                          target_country=req.target_country, prompt=prompt,
                                          llm=llm if llm is not None else tr.default_assistant(cfg), placeholder_tokens=tokens)
    except tr.TranslateResponseError as e:
        return failed_report(ident, "response_invalid", "; ".join(e.reasons), manifest=manifest, target_count=len(ordered_targets),
                             targets=ordered_targets, implementation=impl,
                             payload={"section_key": req.section_key, "blocks": [_failed_block(k, rev[k], by_key, "응답 구조 실패") for k in ordered_targets]})
    except VlmError as e:
        return failed_report(ident, "model_call_failed", str(e), manifest=manifest, target_count=len(ordered_targets),
                             targets=ordered_targets, implementation=impl,
                             payload={"section_key": req.section_key, "blocks": [_failed_block(k, rev[k], by_key, "호출 실패") for k in ordered_targets]})
    result_blocks = []
    for o in outs:
        src = by_key[o.block_key].source_ko
        item = {"block_key": o.block_key, "input_revision": rev[o.block_key], "outcome": o.outcome,
                "source_sha256": sha256_text(src), "applied_instructions": o.applied_instructions,
                "trans_1": o.translation, "translation_sha256": sha256_text(o.translation) if o.translation is not None else None,
                "error": o.error}
        result_blocks.append(item)
    failed = [b["block_key"] for b in result_blocks if b["outcome"] == "failed"]
    payload = {"section_key": req.section_key, "blocks": result_blocks, "preserved": [p.model_dump() for p in req.preserved],
               "summary": {"targets": len(result_blocks), "completed": len(result_blocks) - len(failed), "failed": len(failed)}}
    if failed:
        # 블록별 실패(모델 명시 실패 · 빈 번역문 · 자리표시자) — 성공분은 payload에 보존, 시도는 failed(5.32)
        return failed_report(ident, "response_invalid",
                             f"번역 실패 블록 {len(failed)}/{len(result_blocks)}: {failed}", retryable=True, targets=failed,
                             manifest=manifest, target_count=len(result_blocks), payload=payload, implementation=impl,
                             diagnostics=diag)
    return StageReport(execution_id=ident.execution_id, attempt_id=ident.attempt_id, stage="translate",
                       input_snapshot_id=ident.input_snapshot_id, input_manifest=manifest, input_manifest_sha256=msha,
                       outcome="completed", target_count=len(result_blocks), payload=payload, implementation=impl, diagnostics=diag)


def _failed_block(key: str, revision: int, by_key: dict[str, TextBlock], why: str) -> dict[str, Any]:
    return {"block_key": key, "input_revision": revision, "outcome": "failed", "source_sha256": sha256_text(by_key[key].source_ko),
            "applied_instructions": [], "trans_1": None, "translation_sha256": None, "error": why}


def run_text_check(raw: Any, cfg: dict[str, Any] | None = None) -> StageReport:
    del cfg  # 규칙 기반 — 설정 없음
    try:
        ident = parse_identity(raw, "text_check")
    except (ValidationError, HandoffInputError) as e:
        return failed_report(identity_fallback(raw, "text_check"), "input_invalid", f"요청 식별 오류: {e}")
    try:
        req = TextCheckRequest.model_validate(raw)
    except ValidationError as e:
        return failed_report(ident, "input_invalid", f"요청 형식 오류: {e}")
    impl = {"adapter": TRANSLATE_ADAPTER_VERSION, "match_rules": MATCH_RULES_VERSION, "term_rules": text_check.TERM_RULES_VERSION}
    try:
        cb = check_bundle(req.bundle, target_country=req.target_country, types=load_type_map())
    except BundleError as e:
        return failed_report(ident, "bundle_invalid", str(e), implementation=impl)
    try:
        blocks = _blocks(req.blocks, req.section_key)
        by_key = {b.block_key: b for b in blocks}
        keys = [t.block_key for t in req.targets]
        if len(set(keys)) != len(keys) or set(keys) - set(by_key):
            raise HandoffInputError("검사 대상 블록 중복 또는 섹션에 없는 블록")
        terms = _glossary(req.glossary)
        applied = cb.applied_classes(req.regulatory_class)
        rows = [text_check.RegRow(r.external_id, r.verdict_status, tuple(r.variant_en)) for r in cb.regulation.values()
                if r.regulatory_class in applied]
        manifest, msha = manifest_of({
            "stage": "text_check", "contract_version": CONTRACT_VERSION, "adapter": TRANSLATE_ADAPTER_VERSION,
            "rules": impl, "bundle": cb.identity, "regulatory_class": req.regulatory_class, "applied_classes": applied,
            "section_key": req.section_key,
            "targets": [{"block_key": t.block_key, "revision": t.revision, "text_sha256": sha256_text(t.text),
                         "source_sha256": sha256_text(by_key[t.block_key].source_ko)} for t in req.targets],
            "glossary": {"version": req.glossary.version, "terms": [t.model_dump() for t in terms]},
        })
        check_expected_manifest(ident, msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)
    if not req.targets:
        return StageReport(execution_id=ident.execution_id, attempt_id=ident.attempt_id, stage="text_check",
                           input_snapshot_id=ident.input_snapshot_id, input_manifest=manifest, input_manifest_sha256=msha,
                           outcome="skipped", target_count=0, skip_reason="no_targets",
                           payload={"section_key": req.section_key, "blocks": []}, implementation=impl)
    gl = [text_check.GlossaryTerm(t.glossary_id, t.term_ko, t.term_target, t.enforcement) for t in terms]
    tokens = template_tokens(cb)
    results = []
    try:
        for t in req.targets:
            src = by_key[t.block_key].source_ko
            r = text_check.check_block(t.block_key, t.revision, t.text, src, rows, gl)
            r["text_sha256"] = sha256_text(t.text)
            r["placeholder_leaks"] = sorted(x for x in tokens if x in t.text and x not in src)
            results.append(r)
    except MatchConflictError as e:  # 같은 구간의 금지/허용 — 임의 해소하지 않음. 검사 실패(통과로 바꾸지 않음)
        return failed_report(ident, "bundle_invalid", f"영어 재대조 충돌: {e}", manifest=manifest, target_count=len(req.targets),
                             targets=[t.block_key for t in req.targets], implementation=impl)
    return StageReport(execution_id=ident.execution_id, attempt_id=ident.attempt_id, stage="text_check",
                       input_snapshot_id=ident.input_snapshot_id, input_manifest=manifest, input_manifest_sha256=msha,
                       outcome="completed", target_count=len(results),
                       payload={"section_key": req.section_key, "blocks": results,
                                "result_sha256": sha256_canonical(results)}, implementation=impl)
