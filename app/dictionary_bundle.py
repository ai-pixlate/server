"""분석 실행의 고정 사전 묶음 — DB 적재본에서 한 번 만들고 실행 내내 바꾸지 않는다(D5, contract.md 9.1).

- 일관된 읽기: 규제·현지 행과 근거를 REPEATABLE READ 한 트랜잭션에서 읽는다.
- 원본 ID(external_id) ↔ DB PK 와 당시 내용을 묶음에 고정한다. AI 는 원본 ID 를 돌려주고 BE 는 이 묶음으로 PK·근거를 찾는다.
- 서비스 판정값(verdict_status)과 원값(source_verdict_status)을 함께 싣고, 원값 누락을 추정 복원하지 않는다.
- 대체 표현은 DB 문자열 그대로 싣는다(문자열→배열 변환 규칙 미확정, #71 — 나누지 않는다).
- 적용 분류(D8 F-LNG-03): cosmetic→cosmetic, otc→otc, combination→otc(한계 안내), unknown→cosmetic(분류 미확인 안내).
  미선택(NULL)은 분석 시작 전에 막는다.
- 규제 조회 실패(질의 오류·해당 행 없음·필수값 누락)는 BundleError — N2 실패(자동 재시도 → 오류)다(D9-1).
- 현지 조회 실패는 묶음의 local.status='unavailable' 로 남기고 분석은 계속한다(제외 없이 검사 불가 안내, D9-1).
- 이번 범위 밖 판정값(cultural, D9-2)은 공급하지 않고 skipped 로 기록한다.
- ③-1′ 정책 규칙은 DB에 없다. 저장소 운영 규칙(pipeline/data/policy_rules.json, AI 소유)을 묶음 생성 시 그대로 고정한다.
  환경변수 PIXLATE_POLICY_RULES 는 실험용 대체 경로다. BE는 규칙 내용을 만들지 않는다. 규칙 지문(policy_rules_sha256)도 보존한다.
  규제 행 조회 분류는 규칙의 applied_classes(common 포함)를 따르고, common 을 뺀 분류가 D8 대응과 같아야 한다.
- AI 인계 형식(pipeline.handoff.bundle.RuntimeBundle)은 to_ai_bundle()이 이 저장 묶음에서 결정적으로 만든다.
  대체 표현은 #71 결정 전까지 나누지 않은 원문 문자열 그대로 보낸다(PR #55 3217142 형식 — AI는 ⑧ 표현 지시로 쓰지 않는다).
  근거는 저장 묶음의 근거 배열 전체(PK 제외)를 보낸다. DB PK 는 AI에 보내지 않는다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app import db as app_db
from app.manifest import fingerprint

BUNDLE_SCHEMA = "3"  # 2: policy_rules 고정, 3: 규칙 기본값 저장소 파일·policy_rules_sha256
APPLIED_CLASSES = {"cosmetic": ["cosmetic"], "otc": ["otc"], "combination": ["otc"], "unknown": ["cosmetic"]}
CLASS_NOTICE = {"combination": "combination_otc_basis", "unknown": "unverified_class"}
REGULATORY_VERDICTS = ("regulated", "conditional", "allowed")
LOCAL_VERDICTS = ("irrelevant", "needs_fix")  # cultural 은 이번 범위 제외(D9-2)
LOCAL_CLASS = "common"
COMMON_CLASS = "common"  # 규제 행 중 모든 분류에 공통 적용되는 행(적재 분류값)
POLICY_RULES_ENV = "PIXLATE_POLICY_RULES"


class BundleError(RuntimeError):
    """규제 사전 조회·공통 묶음 생성 실패. retryable 은 일시적 DB 오류 여부."""

    def __init__(self, message: str, *, retryable: bool, code: str = "REGULATORY_DICT_LOOKUP_FAILED"):
        super().__init__(message)
        self.retryable = retryable
        self.code = code


def _date(v: Any) -> str | None:
    return v.isoformat() if v is not None else None


def _entry(r: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pk": int(r["id"]),
        "external_id": r["external_id"],
        "dict_type": r["dict_type"],
        "target_country": r["target_country"],
        "regulatory_class": r["regulatory_class"],
        "source_expression": r["source_expression"],
        "variant_ko": list(r["variant_ko"] or []),
        "forbidden_en": list(r["forbidden_en"]) if r["forbidden_en"] is not None else None,
        "alternative_expression": r["alternative_expression"],
        "verdict_status": r["verdict_status"],
        "source_verdict_status": r["source_verdict_status"],
        "reason": r["reason"],
        "confirmed_date": _date(r["confirmed_date"]),
        "exclusion_context": r["exclusion_context"],
        "keep_context": r["keep_context"],
        "evidence": [
            {
                "pk": int(e["id"]),
                "external_id": e["external_id"],
                "source_type": e["evidence_source_type"],
                "document": e["evidence_document"],
                "quote": e["evidence_quote"],
                "article": e["evidence_article"],
                "url": e["evidence_url"],
                "is_primary": bool(e["is_primary"]),
            }
            for e in evidence
        ],
    }


def _problems_regulatory(e: dict[str, Any]) -> list[str]:
    p = []
    if not e["source_expression"]:
        p.append("source_expression 비어 있음")
    if not e["variant_ko"] or not all(isinstance(x, str) and x for x in e["variant_ko"]):
        p.append("variant_ko 가 비어 있지 않은 문자열 배열이 아님")
    if e["verdict_status"] not in REGULATORY_VERDICTS:
        p.append(f"verdict_status {e['verdict_status']!r}")
    if not e["reason"]:
        p.append("reason 비어 있음")
    return p


def _problems_local(e: dict[str, Any]) -> list[str]:
    p = []
    if not e["source_expression"]:
        p.append("항목명(source_expression) 비어 있음")
    if not e["variant_ko"] or not all(isinstance(x, str) and x for x in e["variant_ko"]):
        p.append("패턴(variant_ko) 이 비어 있지 않은 문자열 배열이 아님")
    for k in ("exclusion_context", "keep_context", "reason"):
        if not e[k]:
            p.append(f"{k} 비어 있음")
    return p


def load_policy_rules(path: str | None = None) -> dict[str, Any]:
    """③-1′ 정책 규칙 JSON을 읽어 검증한다. 경로를 주지 않으면 PIXLATE_POLICY_RULES(실험용 대체), 그것도 없으면
    저장소 운영 규칙(pipeline.handoff.bundle.POLICY_RULES_PATH)을 쓴다.

    검증: pipeline 의 PolicyRules 구조, overrides 빈 목록(개발용 예외 쌍 금지, 5.21 R04),
    각 분류의 applied_classes 에서 common 을 뺀 값이 D8 대응(APPLIED_CLASSES)과 같을 것."""
    from pydantic import ValidationError

    from pipeline.dictionary import PolicyRules
    from pipeline.handoff.bundle import POLICY_RULES_PATH

    path = path or os.getenv(POLICY_RULES_ENV) or str(POLICY_RULES_PATH)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        rules = PolicyRules.model_validate(raw)
    except (OSError, ValueError, ValidationError) as e:
        raise BundleError(f"정책 규칙을 읽을 수 없다({POLICY_RULES_ENV}): {e.__class__.__name__}: {e}",
                          retryable=False, code="POLICY_RULES_INVALID") from e
    if rules.overrides:
        raise BundleError("정책 규칙 overrides(개발용 예외 쌍)는 운영에 쓰지 않는다", retryable=False, code="POLICY_RULES_INVALID")
    for cls, want in APPLIED_CLASSES.items():
        got = [c for c in rules.regulatory_class_map[cls].applied_classes if c != COMMON_CLASS]
        if sorted(got) != sorted(want):
            raise BundleError(f"정책 규칙의 {cls} 적용 분류 {got} ≠ D8 {want}", retryable=False, code="POLICY_RULES_INVALID")
    return raw


def build_bundle(target_country: str, regulatory_class: str) -> dict[str, Any]:
    """DB 적재본으로 고정 묶음을 만든다. 반환 dict 에 'sha256'(정규 직렬화 지문)을 넣는다."""
    if regulatory_class not in APPLIED_CLASSES:
        raise BundleError(f"규제 분류 {regulatory_class!r} — 미선택·미지원 분류로 규제 검사를 시작하지 않는다",
                          retryable=False, code="REGULATORY_CLASS_INVALID")
    rules = load_policy_rules()
    applied = list(rules["regulatory_class_map"][regulatory_class]["applied_classes"])
    eng = app_db.engine
    try:
        with eng.connect() as conn:
            conn = conn.execution_options(isolation_level="REPEATABLE READ")
            with conn.begin():
                reg_rows = [dict(r) for r in conn.execute(
                    text(
                        "SELECT * FROM expression_dictionary WHERE dict_type = 'regulatory' AND target_country = :c "
                        "AND regulatory_class = ANY(:cls) ORDER BY external_id"
                    ),
                    {"c": target_country, "cls": applied},
                ).mappings().all()]
                local_rows: list[dict[str, Any]] | None
                local_error: str | None = None
                try:
                    sp = conn.begin_nested()
                    local_rows = [dict(r) for r in conn.execute(
                        text(
                            "SELECT * FROM expression_dictionary WHERE dict_type = 'local' AND target_country = :c "
                            "AND regulatory_class = :lc ORDER BY external_id"
                        ),
                        {"c": target_country, "lc": LOCAL_CLASS},
                    ).mappings().all()]
                    sp.commit()
                except Exception as e:  # noqa: BLE001 — 현지 조회 실패는 분석을 막지 않는다(D9-1)
                    sp.rollback()
                    local_rows, local_error = None, f"현지 사전 조회 오류: {e.__class__.__name__}"
                ids = [r["id"] for r in reg_rows] + [r["id"] for r in (local_rows or [])]
                ev_rows = [dict(r) for r in conn.execute(
                    text("SELECT * FROM expression_dictionary_evidence WHERE dictionary_id = ANY(:ids) ORDER BY dictionary_id, id"),
                    {"ids": ids},
                ).mappings().all()] if ids else []
    except BundleError:
        raise
    except Exception as e:  # noqa: BLE001 — DB 접근 실패: 일시 오류로 보고 자동 재시도 대상
        raise BundleError(f"규제 사전 조회 실패: {e.__class__.__name__}: {e}", retryable=True) from e

    ev_by: dict[int, list[dict[str, Any]]] = {}
    for e in ev_rows:
        ev_by.setdefault(e["dictionary_id"], []).append(e)

    reg_entries = [_entry(r, ev_by.get(r["id"], [])) for r in reg_rows]
    if not reg_entries:
        raise BundleError(f"규제 사전 행 없음(국가 {target_country} · 분류 {applied})", retryable=True)
    bad = {e["external_id"]: _problems_regulatory(e) for e in reg_entries}
    bad = {k: v for k, v in bad.items() if v}
    if bad:
        raise BundleError(f"규제 사전 필수값 누락으로 공급할 수 없다: {bad}", retryable=False, code="REGULATORY_DICT_INVALID")

    local: dict[str, Any]
    if local_rows is None:
        local = {"status": "unavailable", "error": local_error, "entries": [], "skipped": []}
    else:
        entries = [_entry(r, ev_by.get(r["id"], [])) for r in local_rows]
        skipped = [e["external_id"] for e in entries if e["verdict_status"] not in LOCAL_VERDICTS]
        entries = [e for e in entries if e["verdict_status"] in LOCAL_VERDICTS]
        lbad = {e["external_id"]: _problems_local(e) for e in entries}
        lbad = {k: v for k, v in lbad.items() if v}
        from pipeline.handoff.bundle import load_type_map

        want = {t.external_id for t in load_type_map().local}
        got = {e["external_id"] for e in entries}
        if entries and got != want:
            # AI 판정은 지원 대응표의 8항목을 정확히 요구한다(D9-2). 다르면 현지 사전을 공급할 수 없는 것으로 보고 제외 없이 검사 불가(D9-1)
            lbad["__type_map__"] = [f"현지 사전 항목 {sorted(got)} ≠ 지원 대응표 {sorted(want)}"]
        if not entries:
            local = {"status": "unavailable", "error": "현지 사전 행 없음", "entries": [], "skipped": skipped}
        elif lbad:
            tm = lbad.pop("__type_map__", None)
            msgs = ([f"현지 사전 필수값 누락: {sorted(lbad)}"] if lbad else []) + (tm or [])
            local = {"status": "unavailable", "error": "; ".join(msgs), "entries": [], "skipped": skipped}
        else:
            local = {"status": "ok", "error": None, "entries": entries, "skipped": skipped}

    bundle = {
        "bundle_schema": BUNDLE_SCHEMA,
        "target_country": target_country,
        "regulatory_class": regulatory_class,
        "applied_classes": applied,
        "class_notice": CLASS_NOTICE.get(regulatory_class),
        "regulatory": {"status": "ok", "entries": reg_entries},
        "local": local,
        "policy_rules": rules,
        "policy_rules_sha256": fingerprint(rules),  # AI 보고의 bundle identity rules_sha256 과 같은 JCS 지문
    }
    bundle["sha256"] = fingerprint(bundle)
    return bundle


def entries_by_ref(bundle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {e["external_id"]: e for e in bundle["regulatory"]["entries"]}
    out.update({e["external_id"]: e for e in bundle["local"]["entries"]})
    return out


def primary_evidence(entry: dict[str, Any]) -> dict[str, Any] | None:
    for e in entry["evidence"]:
        if e["is_primary"]:
            return e
    return None


def verify_bundle(bundle: dict[str, Any]) -> None:
    """저장된 묶음이 고정 당시 지문과 같은지(손상·교체 검사). 다르면 ValueError."""
    body = {k: v for k, v in bundle.items() if k != "sha256"}
    if fingerprint(body) != bundle.get("sha256"):
        raise ValueError("고정 사전 묶음 지문 불일치")


# ---------------------------------------------------------------------------------------------------------
# AI 인계 형식 — pipeline.handoff.bundle.RuntimeBundle
# ---------------------------------------------------------------------------------------------------------
_AI_EVIDENCE_KEYS = ("external_id", "source_type", "document", "quote", "article", "url", "is_primary")


def _ai_evidence(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """근거 배열 전체(DB 1:N)를 PK 없이 보낸다. 표시용 조문·링크는 AI가 대표 근거(is_primary)에서 고른다(D6 복수 근거 보존)."""
    return [{k: e[k] for k in _AI_EVIDENCE_KEYS} for e in entry["evidence"]]


def _ai_alternatives(raw: str | None) -> str | list[str]:
    """#71 구분 규칙 확정 전: 나누지 않은 원문 문자열 그대로. 비었으면 빈 배열(대체 표현 없음)."""
    return raw if raw and raw.strip() else []


def to_ai_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    """저장 묶음 → AI 인계 묶음. 같은 저장 묶음이면 항상 같은 결과·지문이다.

    - 규제 행: 서비스 verdict_status(regulated·conditional·allowed)와 원값 source_verdict_status 를 그대로 보낸다.
      원래 rewritable 행은 regulated + 대체 표현 있음으로 들어가며 규칙의 해당 조합을 따른다.
    - 현지 행: 항목명←source_expression, 패턴←variant_ko, 셀러 문장←reason. 공급 불가(조회 실패·필수값·대응표 불일치,
      build_bundle 에서 판정)면 local=null 과 사유를 보낸다(D9-1: 제외 없이 검사 불가).
    - 정책 규칙이 없으면 BundleError(판정을 시작하지 않는다)."""
    from pipeline.handoff.bundle import bundle_content_sha256

    rules = bundle.get("policy_rules")
    if rules is None:  # 정책 규칙 고정 이전(묶음 스키마 1)의 저장 묶음
        raise BundleError("정책 규칙이 묶음에 없다(옛 묶음) — 새 분석 실행이 필요하다", retryable=False, code="POLICY_RULES_UNAVAILABLE")
    reg = [
        {"external_id": e["external_id"], "regulatory_class": e["regulatory_class"], "source_expression": e["source_expression"],
         "variant_ko": list(e["variant_ko"]), "variant_en": list(e["forbidden_en"] or []),
         "alternative_expression": _ai_alternatives(e["alternative_expression"]), "verdict_status": e["verdict_status"],
         "source_verdict_status": e["source_verdict_status"], "reason": e["reason"], "evidence": _ai_evidence(e),
         "confirmed_date": e["confirmed_date"]}
        for e in bundle["regulatory"]["entries"]
    ]
    local: dict[str, Any] | None = None
    local_unavailable: dict[str, Any] | None = None
    lb = bundle["local"]
    if lb["status"] != "ok":
        local_unavailable = {"reason": lb.get("error") or "현지 사전 조회 실패"}
    else:
        local = {"version": "db-sha256:" + fingerprint(lb["entries"])[:16], "entries": [
            {"external_id": e["external_id"], "item": e["source_expression"], "patterns": list(e["variant_ko"]),
             "verdict_status": e["verdict_status"], "exclusion_context": e["exclusion_context"],
             "keep_context": e["keep_context"], "seller_message": e["reason"], "confirmed_date": e["confirmed_date"]}
            for e in lb["entries"]]}
    out: dict[str, Any] = {
        "bundle_id": "be-" + bundle["sha256"][:16],
        "target_country": bundle["target_country"],
        "regulation": {"version": "db-sha256:" + fingerprint(bundle["regulatory"]["entries"])[:16], "entries": reg},
        "local": local,
        "local_unavailable": local_unavailable,
        "rules": rules,
    }
    out["bundle_sha256"] = bundle_content_sha256(out)
    return out
