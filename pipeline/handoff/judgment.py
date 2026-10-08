"""③-1 AI 섹션 판정 · ③-1′ 정책 적용 — 운영 인계 [통합 D4~D9 · 5.22 · 5.23 · 5.11].

입력: 초기 분석 결과(AnalyzeResult v1) + BE 고정 사전 묶음 + 사용자 regulatory_class + 섹션 이미지(절대 경로 · SHA-256).
처리(섹션마다):
1. 규제 표현 검출 — 적용 분류의 RG 행 variant_ko를 5.23 한국어 규칙으로 매칭하고 감싸기 억제를 적용한다(의미 추론 없음).
   검출이 있으면 `regulatory_expression_match` finding 1건(present), 없으면 finding 없이 검사 범위에 기록한다(absent finding 없음).
2. 현지 8항목 맥락 판정 — 모든 섹션에서 8항목 전부 LLM 1회(A안, 패턴은 참고 신호). 섹션 · 항목당 finding 1건, 근거 블록 복수.
   호출 · 응답 실패는 그 섹션의 local=failed(uncertain · absent로 바꾸지 않음) → 정책은 섹션 제외 권고(판정 실패).
   현지 사전 조회 실패(묶음 local=null)는 local=not_checked — 제외하지 않고 검사 불가로 남긴다.
3. 정책 — 남은 판정 모두 유지, 제외형이 하나라도 있으면 제외 권고. allowed는 판정 행을 만들지 않는다. conflict_group · 예외 쌍 없음.
실패: 묶음 손상 · 충돌 · 규제 검출 불가는 보고 전체 failed(N2 실패, D9-1). 현지 실패는 섹션 결과로만 남기고 보고는 completed다.
content_type은 검사 대상 코드, external_id는 사용한 사전 ID로 분리한다(D7). 판정 · 근거 · 억제 관계는 audit 재료로 모두 보존한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError

from pipeline.handoff.bundle import BundleError, CheckedBundle, check_bundle, load_type_map
from pipeline.handoff.canonical import sha256_file, sha256_text
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
from pipeline.matching import MATCH_RULES_VERSION, MatchConflictError, PatternSpec, RawMatch, find_matches, resolve_overlaps
from pipeline.stages import judge as judge_stage
from pipeline.stages.judge import JudgeResponseError
from pipeline.types import AnalyzeResult, Section, TextBlock
from pipeline.vlm import JudgeAssistant, VlmError

JUDGE_ADAPTER_VERSION = "judge-handoff@2026-10-08.1"
POLICY_IMPL_VERSION = "policy-runtime@2026-10-08.1"  # 5.23 억제 · D8 집계(예외 쌍 · conflict_group 없음)
FINDINGS_SCHEMA_VERSION = "1"  # contract 4.1 content_findings.schema_version


class SectionImage(_Model):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class JudgeRequest(RequestIdentity):
    stage: Literal["judge"]
    target_country: str = Field(min_length=1)
    regulatory_class: Literal["cosmetic", "otc", "combination", "unknown"]  # 미선택은 BE가 차단한다(D8). null은 형식 오류
    analyze_result: dict[str, Any]  # 분석 실행 전체(앞뒤 문맥용). 판정 대상은 target_section_keys
    target_section_keys: list[str] | None = None  # 이번 판정 대상(섹션별 시도 · 재시도). None이면 전체. 결과 순서는 분석 순서
    section_images: dict[str, SectionImage]  # 판정 대상 section_key → 이 실행 환경의 섹션 이미지
    bundle: dict[str, Any]


def _texts(blocks: list[TextBlock]) -> str:
    return "\n".join(b.source_ko for b in blocks)


def _context(sections: list[Section], blocks_of: dict[str, list[TextBlock]], sec: Section, n: int) -> tuple[str | None, str | None]:
    if n <= 0:
        return None, None
    same = [s for s in sections if s.source_image_id == sec.source_image_id]
    same.sort(key=lambda s: s.section_order)
    i = [s.section_key for s in same].index(sec.section_key)

    def text_of(ss: list[Section]) -> str | None:
        parts = [_texts(blocks_of[s.section_key]) for s in ss]
        return "\n\n".join(parts) if parts else None

    return text_of(same[max(0, i - n):i]), text_of(same[i + 1:i + 1 + n])


def _local_payload(sec: Section, blocks: list[TextBlock], cb: CheckedBundle, candidates: list[RawMatch],
                   prev: str | None, nxt: str | None, context_sections: int) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """③-1 맥락 판정 페이로드(judge_context.md 규약). 8항목 전부. 판정값 · 셀러 문장 · 규제 항목은 보내지 않는다."""
    ordered = sorted(blocks, key=lambda b: b.block_order)
    block_ids = {f"b{i + 1}": b.block_key for i, b in enumerate(ordered)}
    tmp = {v: k for k, v in block_ids.items()}
    view = cb.local_view()
    items = []
    for it in view.items:
        ms = [m for m in candidates if m.external_id == it.id]
        items.append({
            "id": it.id, "name": it.name, "exclude_when": it.exclusion_context, "keep_when": it.keep_context,
            "candidate_blocks": sorted({tmp[m.block_key] for m in ms}, key=lambda s: int(s[1:])),
            "matched_patterns": sorted({p for m in ms for p in m.patterns}),
        })
    payload: dict[str, Any] = {"section": sec.section_key,
                               "blocks": [{"id": tmp[b.block_key], "role": b.role, "text": b.source_ko} for b in ordered],
                               "items": items}
    if context_sections > 0:
        payload["context"] = {"note": "앞뒤 섹션 텍스트는 해석 참고용이다. 현재 섹션의 근거로 삼지 않는다", "prev": prev, "next": nxt}
    return payload, block_ids, [it.id for it in view.items]


def _keyed(matches: list[RawMatch], prefix: str, start: int) -> int:
    n = start
    for m in matches:
        n += 1
        m.match_key = f"{prefix}{n:03d}"
    return n


def _match_json(m: RawMatch, field_name: str = "source_ko") -> dict[str, Any]:
    return {"match_key": m.match_key, "external_id": m.external_id, "block_key": m.block_key, "language": m.language,
            "text_field": field_name, "start": m.start, "end": m.end, "matched_text": m.matched_text, "patterns": list(m.patterns)}


def _rule_for(cb: CheckedBundle, dict_type: str, verdict_status: str, has_alt: bool):
    for r in cb.rules.verdict_map:
        if r.dict_type != dict_type or r.verdict_status != verdict_status:
            continue
        if r.alternative == "any" or (r.alternative in ("required", "present") and has_alt) or (r.alternative == "absent" and not has_alt):
            return r
    raise BundleError(f"verdict_map에 맞는 규칙이 없다 ({dict_type} · {verdict_status} · 대체 표현 {'있음' if has_alt else '없음'})")


def judge_section(sec: Section, blocks: list[TextBlock], cb: CheckedBundle, regulatory_class: str, cfg: dict[str, Any], *,
                  prev: str | None, nxt: str | None, llm: JudgeAssistant | None, prompt: str | None) -> dict[str, Any]:
    """섹션 하나. 규제 검출 오류(MatchConflictError · BundleError)는 그대로 올린다 — 호출자가 보고 전체를 실패로 만든다."""
    ordered = sorted(blocks, key=lambda b: (b.block_order, b.block_key))
    by_key = {b.block_key: b for b in ordered}
    applied = cb.applied_classes(regulatory_class)
    rg_rows = [r for r in cb.regulation.values() if r.regulatory_class in applied]
    rg_specs = [PatternSpec(r.external_id, p) for r in rg_rows for p in r.variant_ko]
    allowed_ids = {r.external_id for r in rg_rows if r.verdict_status == "allowed"}
    rg_raw: list[RawMatch] = []
    for b in ordered:
        rg_raw.extend(find_matches(b.block_key, b.source_ko, rg_specs, "ko"))
    _keyed(rg_raw, "rm_", 0)
    rg_live, rg_sup = resolve_overlaps(rg_raw, allowed_ids)

    findings: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    basis: list[str] = []
    limit = int(cfg["policy"]["problem_text_max_chars"])
    diag: dict[str, Any] = {}

    # --- 현지 8항목
    local_status: str
    local_failure: dict[str, Any] | None = None
    lc_raw: list[RawMatch] = []
    if cb.local is None:
        local_status = "not_checked"
        local_failure = {"kind": "dictionary_unavailable", "message": cb.raw.local_unavailable.reason}  # type: ignore[union-attr]
    else:
        price = {t.external_id: t.price_suffix_exception for t in cb.types.local}
        lc_specs = [PatternSpec(r.external_id, p, price_suffix=price[r.external_id]) for r in cb.local.values() for p in r.patterns]
        for b in ordered:
            lc_raw.extend(find_matches(b.block_key, b.source_ko, lc_specs, "ko"))
        _keyed(lc_raw, "lm_", 0)
        payload, block_ids, sent = _local_payload(sec, ordered, cb, lc_raw, prev, nxt, int(cfg["judge"]["context_sections"]))
        ptext = judge_stage.payload_text(payload)
        image, image_meta = judge_stage.prepare_image(sec, int(cfg["judge"]["image_width_px"]))  # OSError는 입력 오류로 올린다
        assistant = llm  # run_judgment가 실행당 한 번 만든다
        diag.update({"payload_sha256": sha256_text(ptext), "image": image_meta, "items_sent": sent})
        try:
            reply = assistant(prompt or "", ptext, image)
            diag["response_sha256"] = sha256_text(reply.text)
            diag["usage"] = reply.usage
            judged = judge_stage.parse_items(reply.text, sent, block_ids)
            local_status = "completed"
        except JudgeResponseError as e:
            local_status, local_failure = "failed", {"kind": "response_invalid", "message": "; ".join(e.reasons)}
        except VlmError as e:
            local_status, local_failure = "failed", {"kind": "model_call_failed", "message": str(e)}
        if local_status == "completed":
            for t in cb.types.local:
                j = judged[t.external_id]
                fk = f"f_{len(findings) + 1:02d}"
                findings.append({"finding_key": fk, "content_type": t.content_type, "status": j["status"],
                                 "evidence_block_ids": j["evidence"], "evidence_source": j["evidence_source"], "reason": j["reason"]})
                links.append({"finding_key": fk, "external_ids": [t.external_id],
                              "match_keys": [m.match_key for m in lc_raw if m.external_id == t.external_id], "verdict_keys": []})

    # --- 규제 검출 finding(섹션당 1건)
    reg_fk = None
    if rg_raw:
        reg_fk = f"f_{len(findings) + 1:02d}"
        ev = []
        for m in rg_raw:
            if m.block_key not in ev:
                ev.append(m.block_key)
        ids = sorted({m.external_id for m in rg_raw})
        findings.append({"finding_key": reg_fk, "content_type": cb.types.regulatory_content_type, "status": "present",
                         "evidence_block_ids": ev, "evidence_source": "text",
                         "reason": f"사전 표현 검출 {len(rg_raw)}건({', '.join(ids)}) — 표현 검출 사실이며 위반 확정이 아니다"})
        links.append({"finding_key": reg_fk, "external_ids": ids, "match_keys": [m.match_key for m in rg_raw], "verdict_keys": []})

    def add_verdict(fk: str, ext: str, dict_type: str, row, finding_status: str, problem: str | None, match_keys: list[str]) -> None:
        has_alt = row.has_alternative if dict_type == "regulatory" else False
        rule = _rule_for(cb, dict_type, row.verdict_status, has_alt)
        if rule.emit_verdict_status is None:
            return
        vk = f"v_{len(verdicts) + 1:02d}"
        bucket = cb.rules.uncertain_bucket if finding_status == "uncertain" else rule.bucket
        ev = row.primary_evidence if dict_type == "regulatory" else None  # 표시용 조문 · 링크는 대표 근거. 전체 근거는 사전 스냅샷에 보존
        verdicts.append({
            "verdict_key": vk, "finding_key": fk, "external_id": ext, "dict_type": dict_type,
            "verdict_status": rule.emit_verdict_status, "dictionary_verdict_status": row.verdict_status,
            "source_verdict_status": getattr(row, "source_verdict_status", None), "finding_status": finding_status,
            "problem_text": problem, "match_keys": match_keys,
            "alternative_expression": row.alternative_expression if dict_type == "regulatory" else [],  # 배열 또는 나누지 않은 원문 그대로
            "basis_article": ev.article if ev else None, "evidence_url": ev.url if ev else None,
            "reason": row.reason if dict_type == "regulatory" else row.seller_message, "bucket": bucket,
        })
        for ln in links:
            if ln["finding_key"] == fk:
                ln["verdict_keys"].append(vk)
        if bucket == "exclude":
            basis.append(vk)

    # 규제 판정: 억제되지 않은 비허용 매칭의 사전 항목마다 1행
    if reg_fk is not None:
        seen: list[str] = []
        for m in rg_live:
            if m.external_id not in seen:
                seen.append(m.external_id)
        for ext in seen:
            ms = [m for m in rg_live if m.external_id == ext]
            add_verdict(reg_fk, ext, "regulatory", cb.regulation[ext], "present", ms[0].matched_text, [m.match_key for m in ms])
    # 현지 판정
    if local_status == "completed":
        for f, t in zip(findings, cb.types.local):
            if f["status"] not in ("present", "uncertain"):
                continue
            text = "\n".join(by_key[k].source_ko for k in f["evidence_block_ids"]) if f["evidence_block_ids"] else None
            if text is not None and len(text) > limit:
                text = text[: max(1, limit - 1)] + "…"
            add_verdict(f["finding_key"], t.external_id, "local", cb.local[t.external_id], f["status"], text, [])  # type: ignore[index]
    elif local_status == "failed":
        basis.append("local_judgment_failed")
    recommendation = "exclude" if basis else "include"

    used_ids = sorted({ext for ln in links for ext in ln["external_ids"]})
    snapshots = []
    for ext in used_ids:
        if ext in cb.regulation:
            snapshots.append({"external_id": ext, "dict_type": "regulatory", "snapshot": cb.regulation[ext].model_dump(mode="json")})
        else:
            snapshots.append({"external_id": ext, "dict_type": "local", "snapshot": cb.local[ext].model_dump(mode="json")})  # type: ignore[index]
    matched_blocks = sorted({m.block_key for m in rg_raw + lc_raw}, key=lambda k: by_key[k].block_order)
    audit = {
        "inspection": {
            "regulatory": {"status": "completed", "regulatory_class": regulatory_class, "applied_classes": applied,
                           "dict_coverage": cb.rules.regulatory_class_map[regulatory_class].dict_coverage,
                           "entries_checked": sorted(r.external_id for r in rg_rows), "raw_matches": len(rg_raw),
                           "live_matches": len(rg_live), "finding": reg_fk is not None},
            "local": {"status": local_status, "items_checked": [t.external_id for t in cb.types.local] if local_status == "completed" else [],
                      "failure": local_failure},
        },
        "dictionary_snapshots": snapshots,
        "matches": [_match_json(m) for m in rg_raw + lc_raw],
        "suppressed": [{"match_key": s.match_key, "rule": s.rule, "by": list(s.by)} for s in rg_sup],
        "links": links,
        "block_texts": {k: by_key[k].source_ko for k in matched_blocks},  # 당시 원문(구간 재해석 금지, D6)
    }
    return {
        "section_key": sec.section_key,
        "regulatory_status": "completed",
        "local_status": local_status,
        "local_failure": local_failure,
        "content_findings": {"schema_version": FINDINGS_SCHEMA_VERSION, "findings": findings},
        "verdicts": verdicts,
        "bucket_recommendation": recommendation,
        "recommendation_basis": basis,
        "audit": audit,
        "_diagnostics": diag,
    }


def run_judgment(raw: Any, cfg: dict[str, Any], *, llm: JudgeAssistant | None = None) -> StageReport:
    try:
        ident = parse_identity(raw, "judge")
    except (ValidationError, HandoffInputError) as e:
        return failed_report(identity_fallback(raw, "judge"), "input_invalid", f"요청 식별 오류: {e}")
    try:
        req = JudgeRequest.model_validate(raw)
    except ValidationError as e:
        return failed_report(ident, "input_invalid", f"요청 형식 오류: {e}")
    impl = {"adapter": JUDGE_ADAPTER_VERSION, "policy": POLICY_IMPL_VERSION, "match_rules": MATCH_RULES_VERSION,
            "judge_model": {k: cfg["judge"].get(k) for k in ("llm_model", "llm_temperature", "llm_timeout_s")}}
    try:
        judge_stage.validate_config(cfg)
        if cfg["judge"]["call_scope"] != "all":
            raise ValueError(f"judge.call_scope={cfg['judge']['call_scope']!r} — 운영은 모든 섹션 8항목 판정(all)만 허용한다(D9-2 A안)")
        if "policy" not in cfg or not isinstance(cfg["policy"].get("problem_text_max_chars"), int):
            raise ValueError("[policy] problem_text_max_chars가 없다")
    except ValueError as e:
        return failed_report(ident, "config_invalid", str(e), implementation=impl)
    try:
        cb = check_bundle(req.bundle, target_country=req.target_country, types=load_type_map())
    except BundleError as e:
        return failed_report(ident, "bundle_invalid", str(e), implementation=impl)
    try:
        ar = AnalyzeResult.model_validate(req.analyze_result)
    except ValidationError as e:
        return failed_report(ident, "input_invalid", f"analyze_result 형식 오류: {e}", implementation=impl)
    keys = [s.section_key for s in ar.sections]
    problems = []
    if len(set(keys)) != len(keys):
        problems.append("섹션 키 중복")
    targets = keys if req.target_section_keys is None else req.target_section_keys
    if not targets or len(set(targets)) != len(targets) or set(targets) - set(keys):
        problems.append(f"target_section_keys {targets} — 비어 있지 않고 중복 없이 분석 섹션 안이어야 한다")
    if set(req.section_images) != set(targets):
        problems.append(f"section_images 키 {sorted(req.section_images)} ≠ 판정 대상 {sorted(set(targets))}")
    bkeys = [b.block_key for b in ar.blocks]
    if len(set(bkeys)) != len(bkeys):
        problems.append("블록 키 중복")
    orphan = [b.block_key for b in ar.blocks if b.section_key not in set(keys)]
    if orphan:
        problems.append(f"섹션이 없는 블록 {orphan}")
    if problems:
        return failed_report(ident, "input_invalid", "; ".join(problems), implementation=impl)
    all_sections = list(ar.sections)
    sections = []
    for s in ar.sections:
        if s.section_key not in set(targets):
            continue
        img = req.section_images[s.section_key]
        p = Path(img.path)
        if not p.is_absolute() or not p.is_file():
            return failed_report(ident, "input_invalid", f"{s.section_key}: 섹션 이미지가 없다 {img.path}", implementation=impl)
        if sha256_file(p) != img.sha256:
            return failed_report(ident, "input_invalid", f"{s.section_key}: 섹션 이미지 SHA-256 불일치", implementation=impl)
        sections.append(s.model_copy(update={"image_path": str(p)}))
    blocks_of = {s.section_key: [b for b in ar.blocks if b.section_key == s.section_key] for s in all_sections}
    prompt = None
    if cb.local is not None:
        try:
            prompt = judge_stage.validate_llm_config(cfg, need_api_key=llm is None)
        except ValueError as e:
            return failed_report(ident, "config_invalid", str(e), implementation=impl)
        llm = llm if llm is not None else judge_stage.default_assistant(cfg)
    manifest, msha = manifest_of({
        "stage": "judge", "contract_version": CONTRACT_VERSION, "adapter": JUDGE_ADAPTER_VERSION, "policy": POLICY_IMPL_VERSION,
        "match_rules": MATCH_RULES_VERSION, "bundle": cb.identity, "target_country": req.target_country,
        "regulatory_class": req.regulatory_class, "target_section_keys": [s.section_key for s in sections],
        "context": {"context_sections": int(cfg["judge"]["context_sections"]),
                    "blocks": {s.section_key: [[b.block_key, b.source_ko] for b in blocks_of[s.section_key]] for s in all_sections}},
        "sections": [{"section_key": s.section_key, "image_sha256": req.section_images[s.section_key].sha256,
                      "blocks": [b.model_dump(mode="json") for b in blocks_of[s.section_key]]} for s in sections],
        "config": {"judge": {k: cfg["judge"][k] for k in ("call_scope", "image_width_px", "context_sections", "llm_model",
                                                           "llm_temperature", "llm_timeout_s") if k in cfg["judge"]},
                   "policy": dict(cfg["policy"])},
        "prompt_sha256": sha256_text(prompt) if prompt is not None else None,
    })
    try:
        check_expected_manifest(ident, msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)
    results = []
    diags = {}
    try:
        for s in sections:
            prev, nxt = _context(all_sections, blocks_of, s, int(cfg["judge"]["context_sections"]))
            r = judge_section(s, blocks_of[s.section_key], cb, req.regulatory_class, cfg, prev=prev, nxt=nxt, llm=llm, prompt=prompt)
            diags[s.section_key] = r.pop("_diagnostics")
            results.append(r)
    except (MatchConflictError, BundleError) as e:  # 규제 검출 불가 = N2 실패(현지 실패 예외로 넓히지 않음)
        return failed_report(ident, "bundle_invalid", f"규제 표현 검출 실패: {e}", manifest=manifest, target_count=len(sections),
                             implementation=impl)
    except OSError as e:
        return failed_report(ident, "input_invalid", f"섹션 이미지를 열 수 없다: {e}", manifest=manifest, target_count=len(sections),
                             implementation=impl)
    except Exception as e:  # noqa: BLE001
        return failed_report(ident, "internal", f"{e.__class__.__name__}: {e}", manifest=manifest, target_count=len(sections),
                             implementation=impl)
    summary = {
        "sections": len(results),
        "local_failed": [r["section_key"] for r in results if r["local_status"] == "failed"],
        "local_not_checked": [r["section_key"] for r in results if r["local_status"] == "not_checked"],
        "exclude_recommended": [r["section_key"] for r in results if r["bucket_recommendation"] == "exclude"],
        "verdicts": sum(len(r["verdicts"]) for r in results),
    }
    return StageReport(
        execution_id=ident.execution_id, attempt_id=ident.attempt_id, stage="judge", input_snapshot_id=ident.input_snapshot_id,
        input_manifest=manifest, input_manifest_sha256=msha, outcome="completed", target_count=len(results),
        payload={"bundle": cb.identity, "regulatory_class": req.regulatory_class, "sections": results, "summary": summary},
        implementation=impl, diagnostics={"sections": diags},
    )
