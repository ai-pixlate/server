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
- 섹션 단위 지문은 같은 섹션의 블록이 공유하므로 지문 일치만으로 블록을 가리지 못한다. 넘겨받은 결과의 block_key(LLM 실패는 scope의
  target_keys)가 집계 대상 블록과 다르면 AggregateError다.
- 활성 검사(`active_checks`): 이 실행에서 **돌리기로 한** 검사를 BE가 명시한다(예: 규칙 검사만 `("rule",)` · 두 검사
  `("rule", "llm")`. 어느 구성을 언제 쓸지는 BE 연동 때 정한다). run_state=none에서 활성 여부를 추정하지 않는다 — 활성인데 시도가 없으면(none) 「실행해야 하는데 누락된 검사」로
  미완료이고, 비활성 검사는 넘겨받은 결과를 **읽지 않는다**(축 값 `inactive`, 상태 null). 비활성 검사의 과거 결과를 현재 경고로 쓰지 않는다.
- checks_settled = 활성 검사 모두 run_state ∈ {done, skipped, failed} ∧ freshness=current. 활성 검사가 none · pending · running · cancelled
  이거나 stale · 비교 불가이면 false. 현재 입력의 최종 실패와 정상 생략(no_targets)은 종료로 본다(실패 확인 절차는 별도).
  **처리 종료 여부일 뿐** 문제없음 · 경고 등급 · 검수 확정 허용을 뜻하지 않는다. 경고 · 실패 확인과 확정 조건은 BE가 따로 판단한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

AGGREGATE_VERSION = "check-aggregate@2026-10-13.1"
RUN_STATES = ("none", "pending", "running", "done", "skipped", "failed", "cancelled")
SETTLED_STATES = frozenset({"done", "skipped", "failed"})
CHECK_NAMES = ("rule", "llm")


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


def _check_owner(block_key: str, c: CheckInput, name: str) -> None:
    """넘겨받은 결과가 집계 대상 블록의 것인지(상태 · 유효성과 무관하게 항상) 확인한다."""
    if c.block_result is not None and c.block_result.get("block_key") != block_key:
        raise AggregateError(f"{name} 결과의 block_key {c.block_result.get('block_key')!r} ≠ 집계 대상 {block_key!r}")
    if c.failed_scope is not None and block_key not in c.failed_scope.get("target_keys", []):
        raise AggregateError(f"{name} 실패 보고의 검사 대상에 {block_key!r}가 없다")


def _usable(c: CheckInput, fresh: str) -> bool:
    return c.run_state == "done" and fresh == "current" and c.block_result is not None


def _rule_axis(block_key: str, c: CheckInput, fresh: str, check_ids: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any] | None]:
    _check_owner(block_key, c, "규칙")
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


def _llm_axis(block_key: str, c: CheckInput, fresh: str, check_ids: list[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    state = {"run_state": c.run_state, "freshness": fresh, "scope": "unknown"}
    empty = {i: {"llm": "unavailable", "llm_undetermined_reason": None, "llm_location_keys": [], "llm_basis_summary": None}
             for i in check_ids}
    _check_owner(block_key, c, "LLM")
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


def _active(active_checks: Any) -> frozenset[str]:
    if isinstance(active_checks, str) or not isinstance(active_checks, (tuple, list, set, frozenset)):
        raise AggregateError(f"active_checks는 검사 이름의 모음이어야 한다(예: ('rule',)): {active_checks!r}")
    names = list(active_checks)
    if not names or len(set(names)) != len(names) or set(names) - set(CHECK_NAMES):
        raise AggregateError(f"active_checks는 {CHECK_NAMES}의 중복 없는 비어 있지 않은 부분집합이어야 한다: {active_checks!r}")
    return frozenset(names)


def _inactive(check_ids: list[str], axis: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    state = {"active": False, "run_state": None, "freshness": None, "scope": None}
    if axis == "rule":
        return state, {i: {"rule": "inactive", "rule_match_keys": [], "rule_suppressed_match_keys": []} for i in check_ids}
    return state, {i: {"llm": "inactive", "llm_undetermined_reason": None, "llm_location_keys": [], "llm_basis_summary": None}
                   for i in check_ids}


def aggregate_block_checks(*, block_key: str, check_item_ids: list[str], active_checks: Any,
                           rule: CheckInput | None, llm: CheckInput | None) -> dict[str, Any]:
    """블록 하나의 검사 상태와 검사 항목별 두 축. check_item_ids는 `review.check_item_ids(bundle, …)`(묶음 순서).
    active_checks: 이 실행에서 돌리기로 한 검사(('rule',) 또는 ('rule', 'llm')) — 기본값 없음. 활성 검사의 CheckInput은 필수(시도가 없으면
    run_state='none'), 비활성 검사는 None을 넘기며 넘겨도 읽지 않는다."""
    active = _active(active_checks)
    given = {"rule": rule, "llm": llm}
    missing = [n for n in CHECK_NAMES if n in active and given[n] is None]
    if missing:
        raise AggregateError(f"활성 검사 {missing}의 상태가 없다 — 시도가 없으면 run_state='none'으로 넘긴다(비활성으로 추정하지 않음)")
    if "rule" in active:
        assert rule is not None
        rf = _freshness(rule)
        rstate, raxis, rrefs = _rule_axis(block_key, rule, rf, check_item_ids)
        rstate = {"active": True, **rstate}
    else:
        (rstate, raxis), rrefs = _inactive(check_item_ids, "rule"), None
    if "llm" in active:
        assert llm is not None
        lf = _freshness(llm)
        lstate, laxis, lrefs = _llm_axis(block_key, llm, lf, check_item_ids)
        lstate = {"active": True, **lstate}
    else:
        (lstate, laxis), lrefs = _inactive(check_item_ids, "llm"), []
    items = [{"external_id": i, **raxis[i], **laxis[i]} for i in check_item_ids]
    return {
        "aggregate_version": AGGREGATE_VERSION, "block_key": block_key, "active_checks": [n for n in CHECK_NAMES if n in active],
        "checks": {"rule": rstate, "llm": lstate},
        "items": items,
        "references": {"rule_suppressions": rrefs["suppressions"] if rrefs else None, "llm_allowed_references": lrefs},
        "terms": rrefs["terms"] if rrefs else None,  # 강제 용어 결과는 그대로 전달(LLM 결과로 바꾸지 않음)
        "checks_settled": all(s["run_state"] in SETTLED_STATES and s["freshness"] == "current"
                              for s in (rstate, lstate) if s["active"]),
    }
