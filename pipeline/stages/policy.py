"""③-1' 정책 적용 — finding × 사전 정책 필드 × 상품 규제 분류 × 규칙 → section_verdict 후보와 섹션 버킷 권고 (2026-09-29).

pipeline.md 단계표 ③-1' · 7.3절, 설계 docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md 3절(잠정, open-questions.md #60).
구현 소유자 AI 서버(open-questions.md 5절 #6). 결정적 로직이며 AI를 부르지 않는다(계약 1.3 · 9장).

순서(설계 3.2절, 승인된 결정만):
1. 적용 행 선택 — `status ∈ {present, uncertain}`인 finding 중 규제 항목은 `regulatory_class` → `policy_rules.regulatory_class_map`의 적용 분류에
   속한 행만, 현지부적합 항목은 전부. 원래 선택값 · 적용 분류 · 검사 범위(`dict_coverage`, "현재 사전 항목 기준")를 결과에 보존한다(D9).
   `regulatory_class` 누락(None)은 사용자가 고른 `unknown`과 구분해 `input_error`(정상 권고 없음 · 자동 재시도 없음, D9-c).
2. 중복 · 허용 예외 — **개별 매칭 단위**. 같은 블록에서 원문 구간이 겹치는 서로 다른 항목의 매칭에 대해 `overrides`(명시적 예외 쌍)만 적용해
   drop 쪽 매칭을 억제한다. 억제되지 않은 매칭이 하나라도 남은 finding은 verdict를 유지하고, 매칭이 있었는데 전부 억제된 finding만 verdict가
   없다(D2). 예외 목록에 없는 겹침은 둘 다 **보수적으로 계산에 넣고** `conflict_group`(관련 항목 · 매칭 근거)을 붙여 "규칙 충돌에 따른 잠정
   제외"로 표시한다(D2-b, 실험용 잠정 — 규제 해석 확정 아님). `allowed` 행은 verdict를 만들지 않는다.
3. 매핑 — `policy_rules.verdict_map`(설명서 §3 · §4 기반 잠정, D10). `rewritable`은 출력 `regulated` + 원값 보존(D11).
   `uncertain` finding은 판정값 그대로 + `finding_status=uncertain` + 버킷은 `uncertain_bucket`(D12). `reason`은 사전 사유 스냅샷만.
4. 집계 — status=ok에서만 "제외형 verdict가 하나라도 있으면 exclude, 아니면 include". `JudgeResult.status`가 ok · skipped가 아니면
   `incomplete`(verdict · 권고 없음, D7). `problem_text`는 규제 = 억제되지 않은 첫 매칭 원문, 현지부적합 = 근거 블록 텍스트(정상 입력 blocks,
   D13), 이미지 전용 근거 = None.
"""
from __future__ import annotations

from typing import Any

from pipeline.dictionary import Dictionaries, LocalEntry, PolicyDictView, RegulationEntry, VerdictRule
from pipeline.types import (
    ConflictGroup,
    ContentFinding,
    JudgeContext,
    JudgeResult,
    Match,
    PolicyApplied,
    PolicyResult,
    SuppressedMatch,
    TextBlock,
    VerdictDraft,
    blocks_fingerprint,
    verdict_key,
)

RULES_IMPL_VERSION = "policy-impl@2026-09-29.1"  # 이 모듈의 처리 순서 · 집계 규칙 버전(데이터 rules_version과 별개)


class PolicyInputError(ValueError):
    """③-1' 입력이 판정 결과가 아니거나(검출 전용 결과 등) 사전 · 블록과 어긋난다. 포함 권고를 만들 수 없다."""


def validate_config(cfg: dict[str, Any]) -> None:
    """[policy] 설정 검증. 어기면 ValueError."""
    if "policy" not in cfg:
        raise ValueError("config에 [policy] 표가 없다")
    p = cfg["policy"]
    if "problem_text_max_chars" not in p:
        raise ValueError("잘못된 [policy] 설정: 없는 키 policy.problem_text_max_chars")
    n = p["problem_text_max_chars"]
    if not (isinstance(n, int) and not isinstance(n, bool)) or n < 1:
        raise ValueError(f"잘못된 [policy] 설정: policy.problem_text_max_chars={n!r} — 정수 · 1 이상")


# ---------------------------------------------------------------------------
# 1 · 2 — 선택 · 예외 · 충돌
# ---------------------------------------------------------------------------
def _entry_of(ref: str, pv: PolicyDictView) -> RegulationEntry | LocalEntry:
    if ref in pv.regulation:
        return pv.regulation[ref]
    if ref in pv.local:
        return pv.local[ref]
    raise PolicyInputError(f"finding이 사전에 없는 항목을 가리킨다: {ref}")


def _overlaps(a: Match, b: Match) -> bool:
    return a.block_key == b.block_key and a.raw_span.start < b.raw_span.end and b.raw_span.start < a.raw_span.end


def apply_overrides(matches: list[Match], pv: PolicyDictView) -> list[SuppressedMatch]:
    """예외 쌍(keep → drop)을 개별 매칭 단위로 적용한다. 억제된 매칭 목록을 돌려준다(원본은 지우지 않는다)."""
    suppressed: dict[str, SuppressedMatch] = {}
    for o in pv.rules.overrides:
        keeps = [m for m in matches if m.dictionary_ref == o.keep]
        for m in matches:
            if m.dictionary_ref not in o.drop or m.match_key in suppressed:
                continue
            for k in keeps:
                if _overlaps(k, m):
                    suppressed[m.match_key] = SuppressedMatch(match_key=m.match_key, finding_key=m.finding_key or "", suppressed_by=k.match_key)
                    break
    return [suppressed[m.match_key] for m in matches if m.match_key in suppressed]


def conflict_groups(matches: list[Match]) -> list[ConflictGroup]:
    """억제되지 않은 매칭 중 같은 블록에서 서로 다른 항목끼리 겹치는 집합(연결 성분). 항목이 2개 이상인 집합만(D2-b)."""
    parent = {m.match_key: m.match_key for m in matches}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(matches):
        for b in matches[i + 1:]:
            if a.dictionary_ref != b.dictionary_ref and _overlaps(a, b):
                parent[find(a.match_key)] = find(b.match_key)
    comps: dict[str, list[Match]] = {}
    for m in matches:
        comps.setdefault(find(m.match_key), []).append(m)
    groups: list[ConflictGroup] = []
    for members in comps.values():
        refs = sorted({m.dictionary_ref for m in members})
        if len(refs) < 2:
            continue
        groups.append(ConflictGroup(group_key=f"c_{len(groups) + 1:02d}", dictionary_refs=refs,
                                    match_keys=[m.match_key for m in members], block_key=members[0].block_key))
    return groups


# ---------------------------------------------------------------------------
# 3 — 매핑
# ---------------------------------------------------------------------------
def _rule_for(entry: RegulationEntry | LocalEntry, pv: PolicyDictView) -> VerdictRule:
    dict_type = "regulatory" if isinstance(entry, RegulationEntry) else "local"
    has_alt = bool(getattr(entry, "alternative_expression", []))
    for r in pv.rules.verdict_map:
        if r.dict_type != dict_type or r.verdict_status != entry.verdict_status:
            continue
        if r.alternative == "any" or (r.alternative in ("required", "present") and has_alt) or (r.alternative == "absent" and not has_alt):
            return r
    raise PolicyInputError(f"{entry.id}: verdict_map에 맞는 규칙이 없다 ({dict_type} · {entry.verdict_status} · 대체 표현 {'있음' if has_alt else '없음'})")


def _problem_text(finding: ContentFinding, entry: RegulationEntry | LocalEntry, live: list[Match], blocks: dict[str, TextBlock], limit: int) -> str | None:
    if isinstance(entry, RegulationEntry):
        if not live:
            raise PolicyInputError(f"{finding.finding_key}({entry.id}): 규제 finding에 매칭이 없다")
        return live[0].matched_text
    if not finding.evidence_block_ids:
        return None  # 이미지 전용 근거 — AI 서술은 finding.reason(저장됨)에 있다
    text = "\n".join(blocks[k].source_ko for k in finding.evidence_block_ids)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def run(judge_result: JudgeResult, blocks: list[TextBlock], ctx: JudgeContext, cfg: dict[str, Any], *, dicts: Dictionaries) -> PolicyResult:
    """③-1' 실행. 입력은 반드시 `JudgeResult`(검출 전용 `DetectionResult`는 거부). 블록은 정상 입력(3.5절)이다."""
    validate_config(cfg)
    if not isinstance(judge_result, JudgeResult):
        raise PolicyInputError(
            f"③-1' 입력은 JudgeResult여야 한다. 받은 것: {type(judge_result).__name__}. "
            "검출 전용 결과(DetectionResult, judge --no-llm)는 판정 결과가 아니며 정책 입력이 될 수 없다"
        )
    pv = dicts.policy_view()
    key = judge_result.section_key
    limit = int(cfg["policy"]["problem_text_max_chars"])

    # 미완료 · 누락 입력 — 정상 권고를 만들지 않는다(3.4절)
    if judge_result.status not in ("ok", "skipped") or judge_result.content_findings is None:
        return PolicyResult(section_key=key, status="incomplete", bucket_recommendation=None, applied=None,
                            error=f"③-1 미완료(status={judge_result.status}): {judge_result.error}")
    if ctx.regulatory_class is None:
        return PolicyResult(section_key=key, status="input_error", bucket_recommendation=None, applied=None,
                            error="regulatory_class 누락 — 사용자가 고른 unknown과 다르다. 입력을 보완하기 전 자동 재시도하지 않는다(D9-c)")
    class_rule = pv.rules.regulatory_class_map[ctx.regulatory_class]
    applied = PolicyApplied(regulatory_class=ctx.regulatory_class, applied_classes=list(class_rule.applied_classes),
                            dict_coverage=class_rule.dict_coverage, dictionary_version=dict(pv.dictionary_version),
                            dictionary_fingerprint=dict(pv.fingerprint), rules_version=pv.rules.rules_version)
    block_map = {b.block_key for b in blocks}
    bad_blocks = [b.block_key for b in blocks if b.section_key != key]
    if bad_blocks:
        raise PolicyInputError(f"블록의 section_key가 섹션과 다르다: {bad_blocks}")
    current_fp = blocks_fingerprint(blocks)
    if current_fp != judge_result.checked.input_fingerprint:
        raise PolicyInputError(
            "현재 블록이 판정 당시 블록과 다르다(텍스트 · 순서 · 구성 변경). 이전 finding으로 정책을 계산하지 않는다 — ③-1 재실행 대상"
            f" (판정 {judge_result.checked.input_fingerprint[:12]}… · 현재 {current_fp[:12]}…)"
        )
    blocks_by_key = {b.block_key: b for b in blocks}

    # 1. 적용 행 선택
    selected: list[tuple[ContentFinding, RegulationEntry | LocalEntry]] = []
    for f in judge_result.content_findings.findings:
        if f.status not in ("present", "uncertain"):
            continue
        entry = _entry_of(f.content_type, pv)
        if isinstance(entry, RegulationEntry) and entry.regulatory_class not in class_rule.applied_classes:
            continue
        missing = [k for k in f.evidence_block_ids if k not in block_map]
        if missing:
            raise PolicyInputError(f"{f.finding_key}: 현재 섹션에 없는 근거 블록 {missing}. 블록이 바뀌었으면 ③-1 재실행 대상이다")
        selected.append((f, entry))
    selected_keys = {f.finding_key for f, _ in selected}
    matches = [m for m in judge_result.matches if m.finding_key in selected_keys]

    # 2. 예외 · 충돌(개별 매칭 단위)
    suppressed = apply_overrides(matches, pv)
    suppressed_keys = {s.match_key for s in suppressed}
    live_all = [m for m in matches if m.match_key not in suppressed_keys]
    conflicts = conflict_groups(live_all)
    conflict_of: dict[str, str] = {}
    for g in conflicts:
        for mk in g.match_keys:
            conflict_of[mk] = g.group_key

    # 3. 매핑 · 4. 집계
    verdicts: list[VerdictDraft] = []
    buckets: list[str] = []
    for f, entry in selected:
        had = [m for m in matches if m.finding_key == f.finding_key]
        live = [m for m in had if m.match_key not in suppressed_keys]
        if had and not live:
            continue  # 매칭이 있었는데 전부 억제됨 → verdict 없음(D2)
        rule = _rule_for(entry, pv)
        if rule.emit_verdict_status is None:
            continue  # allowed — 판정 행을 만들지 않는다
        bucket = pv.rules.uncertain_bucket if f.status == "uncertain" else rule.bucket
        groups = sorted({conflict_of[m.match_key] for m in live if m.match_key in conflict_of})
        if isinstance(entry, RegulationEntry):
            alt, article, url, reason = list(entry.alternative_expression), entry.evidence.article, entry.evidence.url, entry.reason
        else:
            alt, article, url, reason = [], None, None, entry.seller_message
        verdicts.append(
            VerdictDraft(
                verdict_key=verdict_key(len(verdicts) + 1), finding_key=f.finding_key, dictionary_ref=entry.id,
                verdict_status=rule.emit_verdict_status, source_verdict_status=entry.verdict_status,
                finding_status="uncertain" if f.status == "uncertain" else "present",
                problem_text=_problem_text(f, entry, live, blocks_by_key, limit),
                alternative_expression=alt, basis_article=article, evidence_url=url, reason=reason,
                conflict_group=groups[0] if groups else None,
            )
        )
        buckets.append(bucket)
    recommendation = "exclude" if any(b == "exclude" for b in buckets) else "include"
    return PolicyResult(section_key=key, status="ok", bucket_recommendation=recommendation, verdicts=verdicts,
                        suppressed=suppressed, conflicts=conflicts, applied=applied)
