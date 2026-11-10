"""규칙 검사(text_check)의 실제 공급자 — A1 inputKey · M3/A3 용어집 공급. 기본 꺼짐(PIXLATE_ACTIVE_CHECKS=rule 일 때만 등록).

근거: `plans/check-connect-2026-11-08/ai-confirm.md` 1절(M3 공동 권고안) · `ai-a1-a3-proposal.md` 1~4-1절(AI A3 조건부 동의).
- A1: `pipeline.handoff.translation.text_check_input_keys(request)` 를 그대로 쓴다. BE 는 키 규칙을 만들지 않는다.
- M3: 검사 등록 때 현재 용어집(대상 언어 · 적용 분류 = 작업 분류 ∪ common · `retired_at IS NULL`)을 AI 공통 선별 규칙
  (`pipeline.glossary.select_by_block`)으로 고르고, 그 결과를 검사 요청에 고정한다. 공급 기록은 삭제 가능한 고정 요청 객체의
  `supply_record` 에 두고 불변 명세에는 해시만 남는다(text_check._fixed).
  - 같은 입력 · 같은 사전 상태면 호출 목적(origin = query · confirm · adopt · translate · user · backfill)과 무관하게 같은 공급을 낸다.
    origin 은 로그에만 쓴다.
  - (1) 전체 번역 후보 선별(강제 + 참고, term_kind 포함) — 기록 전용. 실패해도 (2)를 막지 않는다(사유만 기록).
  - (2) 검사 공급(강제 용어만) — 요청 `glossary.terms`. 실패(강제 용어 자체 충돌 · 데이터 오류 · 합집합 충돌)는 `status=failed` · `reason`
    으로 넘긴다(빈 성공으로 바꾸지 않는다). 검사 대상 블록 **전부**에 대해 계산한다(재시도 대상 일부만이 아니다).
  - DB 조회 실패는 예외 그대로 올린다(빈 공급 성공으로 바꾸지 않는다).
- term_kind: DB `glossary.term_kind` 값을 그대로 넘긴다. NULL 은 None(충돌 예외 미적용) — ID 접두어로 추정하지 않는다.
- 적용 범위: 활성화 뒤 새로 만드는 하류 실행(run_scope.active_checks 고정). 기존 N5 일괄 등록 · 과거 작업 새 경고 · 용어집 변경 시
  자동 일괄 재검사는 하지 않는다.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.flows import text_check
from app.flows.common import block_key, json_value
from app.manifest import fingerprint

log = logging.getLogger(__name__)

# 고정 요청 객체 안 내부 기록 형식(DB 스키마 · 공개 API 아님). @2(11-10 AI A3 조건부 수용): 후보 내용 · 블록 순서 · 실패 단계 · 번역 선별 성격
SUPPLY_RECORD_FORMAT = "text-check-supply-record@2"


class AiInputKeys:
    """A1 — 기존 AI 함수 그대로."""

    name = "ai:text_check_input_keys"

    def block_keys(self, request: dict[str, Any]) -> dict[str, str]:
        from pipeline.handoff.translation import text_check_input_keys

        try:
            return text_check_input_keys(request)
        except ValueError as e:  # 요청 · 묶음 형식 오류 — 키를 정할 수 없다(채택 · 등록 거절 사유)
            raise text_check.InputKeysUnavailable(f"text_check_input_keys 계산 불가: {e}") from e


def _selection_dict(sel: Any) -> dict[str, Any]:
    """Selection 직렬화 — 필드명 · 순서 그대로(이름 변경 없음, ai-a1-a3-proposal 4절)."""
    return {
        "blocks": [{"block_key": b.block_key, "glossary_ids": list(b.glossary_ids)} for b in sel.blocks],
        "entries": [{"glossary_id": e.glossary_id, "term_ko": e.term_ko, "term_target": e.term_target, "enforcement": e.enforcement,
                     "term_kind": e.term_kind} for e in sel.entries],
        "exclusions": [{"block_key": x.block_key, "reference_id": x.reference_id, "enforced_ids": list(x.enforced_ids),
                        "start": x.start, "end": x.end, "rule": x.rule} for x in sel.exclusions],
        "union_conflicts": [list(c) for c in sel.union_conflicts],
    }


class GlossarySelectionSupply:
    """M3 · A3 — 검사 등록 때 현재 용어집을 AI 공통 선별 규칙으로 조회해 고정한다."""

    name = "m3:select_by_block@current-glossary"

    def candidate_rows(self, db: Session, lang: str, cats: list[str]) -> list[dict[str, Any]]:
        """후보 행(번역 공급과 같은 조회 조건 + term_kind). DB 오류는 그대로 올린다."""
        return [dict(r) for r in db.execute(
            text("SELECT id, term_ko, term_target, enforcement, internal_category, version, term_kind FROM glossary "
                 "WHERE target_lang = :l AND internal_category = ANY(:c) AND retired_at IS NULL ORDER BY id"),
            {"l": lang, "c": cats},
        ).mappings().all()]

    def glossary(self, db: Session, run: dict[str, Any], section_id: int, targets: list[dict[str, Any]], *, origin: str
                 ) -> dict[str, Any]:
        from app.flows import downstream
        from pipeline import glossary as gl

        lang = (json_value(run["run_scope"]) or {}).get("target_lang")
        if not lang:
            raise text_check.SupplyBasisUnresolved("실행에 대상 언어가 없다")
        cats = downstream._glossary_categories(db, section_id)
        rows = self.candidate_rows(db, lang, cats)
        blocks = [(block_key(t["id"]), t["source_ko"] or "") for t in targets]  # 그 섹션 검사 대상 전부
        entries = [gl.Entry(str(r["id"]), r["term_ko"], r["term_target"], r["enforcement"], r["term_kind"]) for r in rows]
        by_id = {e.glossary_id: e for e in entries}
        # 기록은 후보 조회가 성공한 뒤에만 만든다 — DB 조회 실패는 위에서 예외로 올라가 기록 · 공급이 없다(빈 공급 성공으로 바꾸지 않음).
        # 그래서 candidates.rows == [] 는 「정상 조회 결과 0건」만 뜻한다.
        record: dict[str, Any] = {
            "format": SUPPLY_RECORD_FORMAT,
            "select_rules_version": gl.SELECT_RULES_VERSION,
            "candidates": {
                "target_lang": lang, "categories": cats, "excludes_retired": True,
                # 실제 선별 함수에 넘긴 후보 입력값(Entry 필드 그대로, 조회 순서 = 선별 입력 순서) + 행 분류 · 버전.
                # 선별 전 실패(Prepared 데이터 검증)도 이 값으로 재현한다. 검사 선별 입력 = 이 중 enforcement == "enforced"
                "rows": [{"glossary_id": str(r["id"]), "term_ko": r["term_ko"], "term_target": r["term_target"],
                          "enforcement": r["enforcement"], "term_kind": r["term_kind"], "internal_category": r["internal_category"],
                          "version": r["version"]} for r in rows],
                # 선별 입력 블록 순서. 원문은 같은 고정 요청 객체의 request.blocks(source_ko)에 있다
                "block_keys": [k for k, _ in blocks],
            },
            # translation_selection 은 검사 등록 때 같은 후보로 다시 계산한 값이다 — 그 번역 실행이 실제로 받은 공급 기록이 아니다
            "translation_selection_basis": "check_registration_recompute",
            "failures": [],  # [{stage, reason}] — stage: translation_selection · check_selection · check_union
        }
        # (1) 전체 번역 후보 선별 — 기록 전용. 실패해도 (2)를 막지 않는다
        try:
            full = gl.select_by_block(blocks, entries)
            record["translation_selection"] = _selection_dict(full)
            excluded = sorted({x.reference_id for x in full.exclusions})
            record["excluded_entries"] = [{"glossary_id": i, "term_ko": by_id[i].term_ko, "term_target": by_id[i].term_target,
                                           "enforcement": by_id[i].enforcement, "term_kind": by_id[i].term_kind} for i in excluded]
        except gl.GlossaryDataError as e:
            record["translation_selection"] = {"error": str(e), "rules_version": gl.SELECT_RULES_VERSION}
            record["excluded_entries"] = None  # 선별 실패 — 제외 판단 자체가 없다
            record["failures"].append({"stage": "translation_selection", "reason": str(e)})
        # (2) 검사 공급 — 강제 용어만
        enforced = [e for e in entries if e.enforcement == "enforced"]
        selection_input = {"rules": gl.SELECT_RULES_VERSION, "target_lang": lang, "categories": cats, "kind": "text_check:enforced"}
        try:
            chk = gl.select_by_block(blocks, enforced)
        except gl.GlossaryDataError as e:
            record["check_selection"] = {"error": str(e), "rules_version": gl.SELECT_RULES_VERSION}
            record["failures"].append({"stage": "check_selection", "reason": str(e)})
            return {"status": "failed", "version": None, "reason": f"검사 용어 선별 실패: {e}", "terms": [], "supply_record": record}
        record["check_selection"] = _selection_dict(chk)
        if chk.union_conflicts:  # 한 목록으로 블록별 관계를 지킬 수 없다 — 공급 실패(BE 안 · ai-a1-a3-proposal 5절)
            reason = f"검사 용어 합집합 충돌(블록별 공급 필요) {[list(c) for c in chk.union_conflicts]}"
            record["failures"].append({"stage": "check_union", "reason": reason})
            return {"status": "failed", "version": None, "reason": reason, "terms": [], "supply_record": record}
        rows_by_id = {str(r["id"]): r for r in rows}
        terms = [{"glossary_id": e.glossary_id, "term_ko": e.term_ko, "term_target": e.term_target, "enforcement": "enforced"}
                 for e in chk.entries]
        content = [{**t, "internal_category": rows_by_id[t["glossary_id"]]["internal_category"],
                    "row_version": rows_by_id[t["glossary_id"]]["version"]} for t in terms]
        log.debug("검사 공급 section=%s origin=%s terms=%d", section_id, origin, len(terms))
        return {"status": "ok", "version": "db-sha256:" + fingerprint({"selection": selection_input, "terms": content})[:16],
                "reason": None, "terms": terms, "supply_record": record}


def install_from_env() -> bool:
    """API · 워커 · CLI 시작 때 부른다. `PIXLATE_ACTIVE_CHECKS` 에 rule 이 있을 때만 실제 공급자를 등록한다(기본 꺼짐 — 아무것도 하지 않음).
    등록 여부를 돌려준다. 값 검증(지원 검사 · 공급자 준비)은 text_check.configured_active_checks 가 실행 생성 때 한다."""
    import os

    raw = os.getenv(text_check.ACTIVE_ENV, "")
    if "rule" not in {v.strip() for v in raw.split(",")}:
        return False
    text_check.set_providers(supply=GlossarySelectionSupply(), keys=AiInputKeys())
    log.info("규칙 검사 공급자 등록: supply=%s keys=%s", GlossarySelectionSupply.name, AiInputKeys.name)
    return True
