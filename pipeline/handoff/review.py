"""⑧ 번역문 LLM 규제 판정 — 운영 인계 `run_translation_review` [통합 5.24 · 5.31 · 5.32 · contract.md 7.1 · 7.2 · 사용자 결정 2026-10-12].

규칙 검사 `run_text_check`와 독립된 검사 단계(stage `translation_review`, producer `translation_llm_review`)다. 같은 섹션 단위 시도 ·
블록별 결과를 쓰며, 자동 번역 채택 직후와 N5 수정 뒤에 같은 요청 형식으로 부른다. 번역문 · 사용자 수정문 · 규칙/용어 결과를 바꾸지 않는다.

요청(BE → AI) — 라벨 · 로고 값은 **BE 공급 계약 후보**다. 누락값을 false로 추정하지 않는다(키가 없으면 input_invalid)
- targets[]: {block_key, revision, text, is_product_label, is_brand_logo} — 검사 대상(현재 번역문). 라벨 · 로고는 false · false여야 한다.
- context[]: {block_key, revision, is_product_label, is_brand_logo} — 대상 밖 섹션 블록(번역문 없음). 라벨 · 로고는 contract 2.4 완료 조합.
- blocks의 모든 키가 targets ∪ context에 정확히 한 번. 이웃 섹션은 넣지 않는다(context_scope=section).
보고(AI → BE)
- completed: 모든 (대상 × 검사 항목) 쌍에 유효한 판정. 개별 undetermined를 포함할 수 있다(실패가 아니다). 검사 항목 0개면 호출 없이
  completed + pair_count 0 — 범위가 0개라는 뜻이지 안전 확인이 아니다.
- skipped(no_targets): 대상 0개.
- failed: 실패 종류별 payload(`_failed_payload`). 분할 하나라도 실패하면 시도 전체 실패, 성공 분할은 `non_adoptable_diagnostics`
  (진단 · 이력용, 현재 유효한 판정 아님). 출력 잘림은 response_invalid + retryable=false(같은 설정 재시도로 풀리지 않음) — 일시적인
  호출 장애(model_call_failed, 재시도 후보)와 구분한다.
- 결과는 문제없음 · 경고 등급 · 확정 허용을 뜻하지 않는다. 신호 코드 · 표시 · 확인 정책은 BE · FE가 정한다.
유효성 — `review_manifest(request, cfg)`는 실행 없이 입력 지문을 계산한다. BE는 등록 때 이 값을 expected_input_manifest_sha256으로
고정하고, 조회 때 현재 DB와 그 분석 실행의 고정 묶음으로 다시 만든 요청의 지문과 비교한다(최신 DB 사전으로 바꾸지 않는다).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, StrictBool, ValidationError

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
from pipeline.stages import translation_review as rv
from pipeline.types import TextBlock
from pipeline.vlm import JudgeAssistant

REVIEW_ADAPTER_VERSION = "translation-review-handoff@2026-10-12.1"
PRODUCER = "translation_llm_review"
CHECK_VERDICTS = ("regulated", "conditional")


class ReviewTarget(_Model):
    block_key: str = Field(min_length=1)
    revision: int = Field(ge=0)
    text: str
    is_product_label: StrictBool | None  # 기본값 없음 — 누락은 형식 오류
    is_brand_logo: StrictBool | None


class ReviewContext(_Model):
    block_key: str = Field(min_length=1)
    revision: int = Field(ge=0)
    is_product_label: StrictBool | None
    is_brand_logo: StrictBool | None


class TranslationReviewRequest(RequestIdentity):
    stage: Literal["translation_review"]
    target_country: str = Field(min_length=1)
    target_lang: str = Field(min_length=1)
    regulatory_class: Literal["cosmetic", "otc", "combination", "unknown"]
    section_key: str = Field(min_length=1)
    blocks: list[dict[str, Any]]  # 섹션 전체 TextBlock(원문 · 순서 · 역할)
    targets: list[ReviewTarget]
    context: list[ReviewContext]
    bundle: dict[str, Any]


# contract 2.4 통합 완료 조합(라벨, 로고)
_COMPLETE_FLAGS = {(True, None), (False, True), (False, False)}


# ---------------------------------------------------------------------------
# 검사 항목 · 문맥 지문
# ---------------------------------------------------------------------------
def _evidence(row: Any) -> tuple[rv.Evidence, ...]:
    return tuple(rv.Evidence(i, e.external_id, e.source_type, e.document, e.article, e.quote, e.url) for i, e in enumerate(row.evidence))


def select_items(cb: CheckedBundle, regulatory_class: str) -> tuple[list[rv.Item], list[rv.Item], list[str]]:
    """(검사 항목, 참고 항목, 적용 분류 밖 ID). 묶음 순서. 문자열 매칭과 무관하게 적용 분류 전체."""
    applied = set(cb.applied_classes(regulatory_class))
    checks: list[rv.Item] = []
    refs: list[rv.Item] = []
    out: list[str] = []
    for r in cb.regulation.values():
        if r.regulatory_class not in applied:
            out.append(r.external_id)
            continue
        role = "check" if r.verdict_status in CHECK_VERDICTS else "reference"
        ref = f"r{len(checks) + 1}" if role == "check" else f"a{len(refs) + 1}"
        it = rv.Item(ref, role, r.external_id, r.regulatory_class, r.verdict_status, r.source_verdict_status, r.source_expression,
                     tuple(r.variant_en), r.reason, _evidence(r))
        (checks if role == "check" else refs).append(it)
    return checks, refs, out


def check_item_ids(bundle: Any, regulatory_class: str, target_country: str) -> list[str]:
    """집계 기준 검사 항목 ID(묶음 순서). BE가 집계(`check_aggregate`)에 넘길 때 쓴다."""
    cb = check_bundle(bundle, target_country=target_country, types=load_type_map())
    return [i.external_id for i in select_items(cb, regulatory_class)[0]]


def item_snapshot(it: rv.Item) -> dict[str, Any]:
    """공급 항목 스냅샷(근거 인용문 · URL 원문 그대로). 근거 이력 · audit 보존용."""
    return {"external_id": it.external_id, "role": it.role, "regulatory_class": it.regulatory_class,
            "verdict_status": it.verdict_status, "source_verdict_status": it.source_verdict_status,
            "source_expression": it.source_expression, "variant_en": list(it.variant_en), "reason": it.reason,
            "evidence": [{"index": e.index, "external_id": e.external_id, "source_type": e.source_type, "document": e.document,
                          "article": e.article, "quote": e.quote, "url": e.url} for e in it.evidence]}


def review_context_sha256(blocks: list[dict[str, Any]], targets: list[dict[str, Any]], context: list[dict[str, Any]]) -> str:
    """LLM이 읽은 섹션 문맥의 지문(대상 번역문 포함). 다른 블록의 문맥이 바뀌어도 값이 바뀐다 — BE의 stale 판단 기준.
    blocks: TextBlock dict(block_key · block_order · role · source_ko), targets/context: 요청 항목 dict."""
    t = {x["block_key"]: x for x in targets}
    c = {x["block_key"]: x for x in context}
    rows = []
    for b in sorted(blocks, key=lambda b: (b["block_order"], b["block_key"])):
        k = b["block_key"]
        e = t.get(k) or c.get(k) or {}
        rows.append({"block_key": k, "block_order": b["block_order"], "role": b["role"], "source_sha256": sha256_text(b["source_ko"]),
                     "revision": e.get("revision"), "is_target": k in t,
                     "is_product_label": e.get("is_product_label"), "is_brand_logo": e.get("is_brand_logo"),
                     "translation_sha256": sha256_text(t[k]["text"]) if k in t else None})
    return sha256_canonical({"context_scope": "section", "blocks": rows})


# ---------------------------------------------------------------------------
# 요청 준비 — 실행 · review_manifest 공용
# ---------------------------------------------------------------------------
class _Fail(Exception):
    def __init__(self, kind: str, message: str) -> None:
        self.kind = kind
        super().__init__(message)


@dataclass
class _Prepared:
    req: TranslationReviewRequest
    cb: CheckedBundle
    prompt: str
    impl: dict[str, Any]
    blocks: list[TextBlock]
    section: list[rv.SectionBlock]
    checks: list[rv.Item]
    refs: list[rv.Item]
    out_of_scope: list[str]
    class_basis: dict[str, Any]
    context_sha: str
    manifest: dict[str, Any]
    msha: str


def _blocks(raw_blocks: list[dict[str, Any]], section_key: str) -> list[TextBlock]:
    try:
        blocks = [TextBlock.model_validate(b) for b in raw_blocks]
    except ValidationError as e:
        raise HandoffInputError(f"블록 형식 오류: {e}") from e
    keys = [b.block_key for b in blocks]
    if len(set(keys)) != len(keys) or any(b.section_key != section_key for b in blocks):
        raise HandoffInputError("블록 키 중복 또는 다른 섹션 블록")
    return blocks


def _check_inputs(req: TranslationReviewRequest, blocks: list[TextBlock]) -> None:
    by_key = {b.block_key: b for b in blocks}
    tk = [t.block_key for t in req.targets]
    ck = [c.block_key for c in req.context]
    problems = []
    allk = tk + ck
    if len(set(allk)) != len(allk):
        problems.append("targets · context에 같은 블록이 두 번 이상 있다")
    if set(allk) != set(by_key):
        problems.append(f"targets ∪ context가 섹션 블록과 다르다(누락 {sorted(set(by_key) - set(allk))} · 섹션 밖 {sorted(set(allk) - set(by_key))})")
    for t in req.targets:
        if (t.is_product_label, t.is_brand_logo) != (False, False):
            problems.append(f"{t.block_key}: 검사 대상은 라벨 · 로고가 false · false여야 한다")
        if not t.text.strip():
            problems.append(f"{t.block_key}: 검사 대상 번역문이 공백이다")
        if t.block_key in by_key and not by_key[t.block_key].source_ko.strip():
            problems.append(f"{t.block_key}: 공백 원문 블록은 검사 대상이 아니다")
    for c in req.context:
        if (c.is_product_label, c.is_brand_logo) not in _COMPLETE_FLAGS:
            problems.append(f"{c.block_key}: 라벨 · 로고 값이 완료 조합이 아니다({c.is_product_label}, {c.is_brand_logo})")
    if problems:
        raise HandoffInputError("; ".join(problems))


def _prepare(raw: Any, cfg: dict[str, Any], *, need_api_key: bool) -> tuple[TranslationReviewRequest, _Prepared]:
    try:
        req = TranslationReviewRequest.model_validate(raw)
    except ValidationError as e:
        raise _Fail("input_invalid", f"요청 형식 오류: {e}") from e
    impl: dict[str, Any] = {"adapter": REVIEW_ADAPTER_VERSION, "stage": rv.REVIEW_IMPL_VERSION}
    try:
        prompt = rv.validate_config(cfg, need_api_key=need_api_key)
        impl["model"] = rv.model_config(cfg)
        impl["prompt_sha256"] = sha256_text(prompt)
        limits = rv.limits_config(cfg)
    except ValueError as e:
        raise _Fail("config_invalid", str(e)) from e
    try:
        cb = check_bundle(req.bundle, target_country=req.target_country, types=load_type_map())
    except BundleError as e:
        raise _Fail("bundle_invalid", str(e)) from e
    try:
        blocks = _blocks(req.blocks, req.section_key)
        _check_inputs(req, blocks)
    except HandoffInputError as e:
        raise _Fail(e.kind, str(e)) from e
    checks, refs, out = select_items(cb, req.regulatory_class)
    rule = cb.rules.regulatory_class_map[req.regulatory_class]
    class_basis = {"selected": req.regulatory_class, "applied_classes": list(rule.applied_classes), "dict_coverage": rule.dict_coverage}
    tmap = {t.block_key: t for t in req.targets}
    cmap = {c.block_key: c for c in req.context}
    tids = {k: f"t{i + 1}" for i, k in enumerate(b.block_key for b in sorted(blocks, key=lambda b: (b.block_order, b.block_key)) if b.block_key in tmap)}
    section = []
    for b in blocks:
        e = tmap.get(b.block_key) or cmap[b.block_key]
        section.append(rv.SectionBlock(b.block_key, b.block_order, b.role, b.source_ko, e.is_product_label, e.is_brand_logo,
                                       tids.get(b.block_key), tmap[b.block_key].text if b.block_key in tmap else None))
    blocks_raw = [b.model_dump() for b in blocks]
    context_sha = review_context_sha256(blocks_raw, [t.model_dump() for t in req.targets], [c.model_dump() for c in req.context])
    manifest, msha = manifest_of({
        "stage": "translation_review", "contract_version": CONTRACT_VERSION, "adapter": REVIEW_ADAPTER_VERSION,
        "review_rules": rv.REVIEW_IMPL_VERSION, "target": {"country": req.target_country, "lang": req.target_lang},
        "class_basis": class_basis, "section_key": req.section_key, "bundle": cb.identity,
        "items": {"check": [{"external_id": i.external_id, "item_sha256": sha256_canonical(item_snapshot(i))} for i in checks],
                  "reference": [{"external_id": i.external_id, "item_sha256": sha256_canonical(item_snapshot(i))} for i in refs],
                  "out_of_scope": out},
        "targets": [{"block_key": t.block_key, "revision": t.revision, "text_sha256": sha256_text(t.text),
                     "source_sha256": sha256_text(next(b.source_ko for b in blocks if b.block_key == t.block_key))} for t in req.targets],
        "context_sha256": context_sha, "model": impl["model"], "prompt_sha256": impl["prompt_sha256"], "limits": limits,
    })
    return req, _Prepared(req, cb, prompt, impl, blocks, section, checks, refs, out, class_basis, context_sha, manifest, msha)


def review_manifest(raw: Any, cfg: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """실행 없이 입력 지문을 계산한다(API 키 불필요). 요청 · 설정 · 묶음이 잘못되면 ValueError. expected 지문은 대조하지 않는다."""
    try:
        _, p = _prepare(raw, cfg, need_api_key=False)
    except _Fail as e:
        raise ValueError(f"{e.kind}: {e}") from e
    return p.manifest, p.msha


# ---------------------------------------------------------------------------
# 보고 조립
# ---------------------------------------------------------------------------
def _scope(p: _Prepared) -> dict[str, Any]:
    return {"check_item_ids": [i.external_id for i in p.checks], "reference_item_ids": [i.external_id for i in p.refs],
            "out_of_scope_item_ids": list(p.out_of_scope), "class_basis": p.class_basis,
            "target_keys": [t.block_key for t in p.req.targets], "pair_count": len(p.req.targets) * len(p.checks),
            "context_scope": "section"}


def _supplied(p: _Prepared) -> dict[str, Any]:
    return {"check": [item_snapshot(i) for i in p.checks], "reference": [item_snapshot(i) for i in p.refs]}


def _basis_out(refs: list[str], item: rv.Item, allowed: dict[str, rv.Item]) -> list[dict[str, Any]]:
    out = []
    for ref in refs:
        if ref == item.ref:
            out.append({"kind": "dictionary_entry", "external_id": item.external_id})
            continue
        ev = next((e for e in item.evidence if item.evidence_ref(e) == ref), None)
        if ev is not None:
            out.append({"kind": "evidence", "external_id": item.external_id, "evidence_index": ev.index,
                        "evidence_external_id": ev.external_id})
            continue
        for a in allowed.values():
            if ref == a.ref:
                out.append({"kind": "allowed_reference", "external_id": a.external_id, "evidence_index": None})
                break
            ev = next((e for e in a.evidence if a.evidence_ref(e) == ref), None)
            if ev is not None:
                out.append({"kind": "allowed_reference", "external_id": a.external_id, "evidence_index": ev.index})
                break
    return out


def judgment_out(j: rv.Judgment, item: rv.Item, allowed: dict[str, rv.Item]) -> dict[str, Any]:
    basis = _basis_out(j.basis, item, allowed)
    return {"external_id": item.external_id, "judgment": j.judgment, "mention": j.mention, "undetermined_reason": j.undetermined_reason,
            "locations": [{"quote": x.quote, "anchor": x.anchor, "start": x.start, "end": x.end} for x in j.locations],
            "basis": basis, "basis_summary": rv.basis_summary([b["kind"] for b in basis]),
            "unverified_conditions": list(j.unverified_conditions), "reason": j.reason, "chunk_index": j.chunk_index}


def flags_of(revision: int, judgments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """compliance_flags 후보(contract 7.1 확장). risk · undetermined만. 위치마다 1건, 위치 없는 undetermined는 위치 네 필드 null 1건."""
    out = []
    for j in judgments:
        if j["judgment"] == "no_risk":
            continue
        base = {"type": "regulatory", "producer": PRODUCER, "revision": revision, "external_id": j["external_id"],
                "judgment": j["judgment"], "undetermined_reason": j["undetermined_reason"], "detected_in": "translation",
                "detected_by": "llm"}
        if not j["locations"]:
            out.append({**base, "problem_text": None, "start": None, "end": None, "location_key": None})
        for loc in j["locations"]:
            out.append({**base, "problem_text": loc["quote"], "start": loc["start"], "end": loc["end"],
                        "location_key": loc["location_key"]})
    return out


def assemble_blocks(req_targets: list[dict[str, Any]], context_sha: str, judgments: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """블록별 결과. 판정은 검사 항목 순서, location_key는 블록 결과 안 순번(loc_001 …)."""
    out = []
    for t in req_targets:
        js = judgments.get(t["block_key"], [])
        n = 0
        for j in js:
            for loc in j["locations"]:
                n += 1
                loc["location_key"] = f"loc_{n:03d}"
        out.append({"block_key": t["block_key"], "revision": t["revision"], "text_sha256": sha256_text(t["text"]),
                    "context_sha256": context_sha, "judgments": js, "flags": flags_of(t["revision"], js),
                    "counts": {"pairs": len(js), **{k: sum(1 for j in js if j["judgment"] == k) for k in rv.JUDGMENTS}}})
    return out


def _chunk_out(r: rv.ChunkRecord, keys: dict[str, str], ext: dict[str, str]) -> dict[str, Any]:
    return {"index": r.index, "target_keys": [keys[t] for t in r.target_ids], "item_ids": [ext[i] for i in r.item_refs],
            "status": r.status, "input_code_points": r.input_code_points, "payload_sha256": r.payload_sha256,
            "response_sha256": r.response_sha256, "finish_reason": r.finish_reason, "error": r.error}


def _judgments_by_block(records: list[rv.ChunkRecord], keys: dict[str, str], items: dict[str, rv.Item],
                        allowed: dict[str, rv.Item], order: list[str]) -> dict[str, list[dict[str, Any]]]:
    pos = {r: i for i, r in enumerate(order)}
    by: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for rec in records:
        if rec.status != "validated":
            continue
        for j in rec.judgments:
            by.setdefault(keys[j.target_id], []).append((pos[j.item_ref], judgment_out(j, items[j.item_ref], allowed)))
    return {k: [x for _, x in sorted(v, key=lambda p: p[0])] for k, v in by.items()}


def run_translation_review(raw: Any, cfg: dict[str, Any], *, llm: JudgeAssistant | None = None) -> StageReport:
    try:
        ident = parse_identity(raw, "translation_review")
    except (ValidationError, HandoffInputError) as e:
        return failed_report(identity_fallback(raw, "translation_review"), "input_invalid", f"요청 식별 오류: {e}")
    try:
        req, p = _prepare(raw, cfg, need_api_key=llm is None)
    except _Fail as e:
        return failed_report(ident, e.kind, str(e), implementation={"adapter": REVIEW_ADAPTER_VERSION, "stage": rv.REVIEW_IMPL_VERSION})
    try:
        check_expected_manifest(ident, p.msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=p.impl)
    base = {"execution_id": ident.execution_id, "attempt_id": ident.attempt_id, "stage": "translation_review",
            "input_snapshot_id": ident.input_snapshot_id, "input_manifest": p.manifest, "input_manifest_sha256": p.msha,
            "implementation": p.impl}
    common = {"section_key": req.section_key, "scope": _scope(p), "context_sha256": p.context_sha, "supplied_items": _supplied(p)}
    if not req.targets:
        return StageReport(**base, outcome="skipped", target_count=0, skip_reason="no_targets",
                           payload={**common, "chunks": [], "blocks": []})
    target_keys = [t.block_key for t in req.targets]
    tkeys = {s.target_id: s.block_key for s in p.section if s.target_id is not None}
    ordered_tids = [s.target_id for s in sorted(p.section, key=lambda s: (s.block_order, s.block_key)) if s.target_id is not None]
    items = {i.ref: i for i in p.checks}
    allowed = {i.ref: i for i in p.refs}
    ext = {i.ref: i.external_id for i in p.checks}
    req_targets = [t.model_dump() for t in req.targets]
    if not p.checks:  # 범위 0개 — 호출하지 않는다. 안전 확인이 아니다
        payload = {**common, "chunks": [], "blocks": assemble_blocks(req_targets, p.context_sha, {})}
        payload["result_sha256"] = sha256_canonical(payload["blocks"])
        return StageReport(**base, outcome="completed", target_count=len(target_keys), payload=payload)

    def size_of(ts: list[str], rs: list[str]) -> int:
        c = rv.Chunk(0, tuple(ts), tuple(rs))
        return len(p.prompt) + len(rv.serialize_payload(rv.build_payload(p.section, c, items, p.refs, p.class_basis,
                                                                         req.target_lang, req.target_country)))

    lim = p.manifest["limits"]
    try:
        chunks = rv.plan_chunks(ordered_tids, [i.ref for i in p.checks], size_of, max_input_chars=lim["max_input_chars"],
                                max_pairs_per_call=lim["max_pairs_per_call"])
    except rv.ReviewInputTooLarge as e:
        return failed_report(ident, "unsupported_input", str(e), manifest=p.manifest, target_count=len(target_keys),
                             targets=target_keys, implementation=p.impl,
                             payload={**common, "chunks": [],
                                      "size": {"measured": e.measured, "limit": e.limit, "unit": "code_points"}})
    try:
        records = rv.review_section(p.section, chunks, items, p.refs, p.class_basis, target_lang=req.target_lang,
                                    target_country=req.target_country, prompt=p.prompt,
                                    llm=llm if llm is not None else rv.default_assistant(cfg))
    except rv.ReviewRunFailed as e:
        diag_by = _judgments_by_block(e.records, tkeys, items, allowed, [i.ref for i in p.checks])
        payload = {**common, "chunks": [_chunk_out(r, tkeys, ext) for r in e.records],
                   "non_adoptable_diagnostics": [{"block_key": k, "judgments": v} for k, v in diag_by.items()]}
        return failed_report(ident, e.kind, str(e), retryable=False if e.truncated else None, manifest=p.manifest,
                             target_count=len(target_keys), targets=target_keys, payload=payload, implementation=p.impl,
                             diagnostics={"usage": [r.usage for r in e.records if r.usage is not None]})
    judged = _judgments_by_block(records, tkeys, items, allowed, [i.ref for i in p.checks])
    payload = {**common, "chunks": [_chunk_out(r, tkeys, ext) for r in records],
               "blocks": assemble_blocks(req_targets, p.context_sha, judged)}
    payload["result_sha256"] = sha256_canonical(payload["blocks"])
    return StageReport(**base, outcome="completed", target_count=len(target_keys), payload=payload,
                       diagnostics={"usage": [r.usage for r in records if r.usage is not None]})
