"""번역문 검사 집계 — 규칙 검사(`text_check`)와 LLM 규제 판정(`translation_review`)의 블록별 두 축 [contract.md 7.1 · 7.2 · 사용자 결정 2026-10-12].

저장하지 않는 파생 함수다. 두 검사의 결과를 읽기만 하며 서로의 결과를 지우거나 바꾸지 않는다.
- 두 축(rule × llm) 비교는 **검사 항목(check_items: 적용 분류의 regulated · conditional)**에만 적용한다. allowed 참고 항목과 강제 용어 결과는
  별도 구역(`references` · `terms`)에 그대로 둔다. category를 만들지 않는다 — 화면용 묶음은 BE · FE가 두 축에서 파생한다.
- rule 축: detected(겹침 억제 이후 남은 유효 검출) · not_detected(억제된 매칭은 `rule_suppressed_match_keys`) · not_checked(영어 패턴 없음)
  · unavailable(규칙 결과가 현재 입력의 채택 결과가 아님). llm 축: risk · no_risk · undetermined · unavailable.
- 실행 상태 `run_state`는 BE 작업 상태를 그대로 받는다: none(현재 시도 없음) · pending · running(현재 시도의 job_async_task.status)
  · done(outcome completed 채택) · skipped(done + skip_reason=no_targets) · failed(BE가 더 재시도하지 않기로 정한 최종 실패) · cancelled.
  자동 재시도는 새 현재 시도의 pending으로 들어온다.
- freshness: BE가 시도에 고정한 입력 지문과, 현재 입력으로 다시 계산한 지문(`review_manifest` · `text_check_manifest`)을 비교한다.
  current · stale · not_comparable(시도 없음 · 지문 없음). 실패도 지문이 다르면 stale이다.
- checks_settled = 두 검사 모두 run_state ∈ {done, skipped, failed} ∧ freshness=current. **처리 종료 여부일 뿐** 문제없음 · 경고 등급 ·
  검수 확정 허용을 뜻하지 않는다. 경고 · 실패 확인과 확정 조건은 BE가 따로 판단한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AGGREGATE_VERSION = "check-aggregate@2026-10-12.1"
RUN_STATES = ("none", "pending", "running", "done", "skipped", "failed", "cancelled")
SETTLED_STATES = frozenset({"done", "skipped", "failed"})


class AggregateError(ValueError):
    """두 결과의 검사 항목이 서로 · 기준과 맞지 않는다(다른 묶음 등). 임의로 맞추지 않는다."""


@dataclass(frozen=True)
class CheckInput:
    """검사 하나(rule 또는 llm)의 현재 상태. block_result는 채택된 보고 payload의 해당 블록 항목(없으면 None).
    failed_scope는 LLM 실패 보고 payload의 scope(있으면) — 범위만 알려 주며 판정은 아니다."""

    run_state: str
    attempt_manifest_sha256: str | None = None  # BE가 시도에 고정한 지문
    current_manifest_sha256: str | None = None  # 현재 입력으로 다시 계산한 지문
    block_result: dict[str, Any] | None = None
    failed_scope: dict[str, Any] | None = None


def _freshness(c: CheckInput) -> str:
    if c.run_state not in RUN_STATES:
        raise AggregateError(f"모르는 run_state {c.run_state!r}")
    if c.run_state == "none" or c.attempt_manifest_sha256 is None or c.current_manifest_sha256 is None:
        return "not_comparable"
    return "current" if c.attempt_manifest_sha256 == c.current_manifest_sha256 else "stale"


def _usable(c: CheckInput, fresh: str) -> bool:
    return c.run_state == "done" and fresh == "current" and c.block_result is not None


def _rule_axis(c: CheckInput, fresh: str, check_ids: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any] | None]:
    state = {"run_state": c.run_state, "freshness": fresh, "scope": "unknown"}
    if not _usable(c, fresh):
        return state, {i: {"rule": "unavailable", "rule_match_keys": [], "rule_suppressed_match_keys": []} for i in check_ids}, None
    r = c.block_result
    assert r is not None
    reg = r["regulatory"]
    state["scope"] = "full" if r["complete"] else "partial"
    known = set(reg["checked_ids"]) | set(reg["unchecked_ids"])
    if set(check_ids) - known:
        raise AggregateError(f"규칙 검사에 없는 검사 항목 {sorted(set(check_ids) - known)} — 다른 묶음의 결과다")
    suppressed = {s["match_key"] for s in reg["suppressed"]}
    span_key = {(m["external_id"], m["start"], m["end"]): m["match_key"] for m in reg["matches"]}
    live: dict[str, list[str]] = {}
    for f in r["flags"]:
        if f.get("type") == "regulatory" and f.get("producer") == "translation_validation":
            mk = span_key.get((f["external_id"], f["start"], f["end"]))
            if mk is None or mk in suppressed:
                raise AggregateError(f"규칙 flag {f['external_id']} [{f['start']}, {f['end']})가 유효 매칭과 대응하지 않는다")
            live.setdefault(f["external_id"], []).append(mk)
    out = {}
    for i in check_ids:
        sup = sorted(m["match_key"] for m in reg["matches"] if m["external_id"] == i and m["match_key"] in suppressed)
        if i in reg["unchecked_ids"]:
            out[i] = {"rule": "not_checked", "rule_match_keys": [], "rule_suppressed_match_keys": sup}
        elif i in live:
            out[i] = {"rule": "detected", "rule_match_keys": sorted(live[i]), "rule_suppressed_match_keys": sup}
        else:
            out[i] = {"rule": "not_detected", "rule_match_keys": [], "rule_suppressed_match_keys": sup}
    refs = {"suppressions": reg["suppressed"], "terms": r["terms"]}
    return state, out, refs


def _llm_axis(c: CheckInput, fresh: str, check_ids: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    state = {"run_state": c.run_state, "freshness": fresh, "scope": "unknown"}
    empty = {i: {"llm": "unavailable", "llm_undetermined_reason": None, "llm_location_keys": [], "llm_basis_summary": None}
             for i in check_ids}
    if c.run_state == "failed" and fresh == "current" and c.failed_scope is not None:
        state["scope"] = "items" if c.failed_scope.get("check_item_ids") else "empty"
    if not _usable(c, fresh):
        return state, empty, []
    r = c.block_result
    assert r is not None
    js = {j["external_id"]: j for j in r["judgments"]}
    if r["judgments"] and set(js) != set(check_ids):
        raise AggregateError("LLM 판정 항목이 검사 항목과 다르다 — 다른 묶음 · 분류의 결과다")
    state["scope"] = "items" if r["judgments"] else "empty"
    if not r["judgments"]:
        if check_ids:
            raise AggregateError("검사 항목이 있는데 LLM 판정이 비었다")
        return state, empty, []
    out = {}
    allowed_refs = []
    for i in check_ids:
        j = js[i]
        out[i] = {"llm": j["judgment"], "llm_undetermined_reason": j["undetermined_reason"],
                  "llm_location_keys": [x["location_key"] for x in j["locations"]], "llm_basis_summary": j["basis_summary"]}
        cited = [b for b in j["basis"] if b["kind"] == "allowed_reference"]
        if cited:
            allowed_refs.append({"external_id": i, "allowed_references": cited})
    return state, out, allowed_refs


def aggregate_block_checks(*, block_key: str, check_item_ids: list[str], rule: CheckInput, llm: CheckInput) -> dict[str, Any]:
    """블록 하나의 두 검사 상태와 검사 항목별 두 축. check_item_ids는 `review.check_item_ids(bundle, …)`(묶음 순서)."""
    rf, lf = _freshness(rule), _freshness(llm)
    rstate, raxis, rrefs = _rule_axis(rule, rf, check_item_ids)
    lstate, laxis, lrefs = _llm_axis(llm, lf, check_item_ids)
    items = [{"external_id": i, **raxis[i], **laxis[i]} for i in check_item_ids]
    return {
        "aggregate_version": AGGREGATE_VERSION, "block_key": block_key,
        "checks": {"rule": rstate, "llm": lstate},
        "items": items,
        "references": {"rule_suppressions": rrefs["suppressions"] if rrefs else None, "llm_allowed_references": lrefs},
        "terms": rrefs["terms"] if rrefs else None,  # 강제 용어 결과는 그대로 전달(LLM 결과로 바꾸지 않음)
        "checks_settled": all(s["run_state"] in SETTLED_STATES and s["freshness"] == "current" for s in (rstate, lstate)),
    }
