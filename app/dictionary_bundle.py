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
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app import db as app_db
from app.manifest import fingerprint

BUNDLE_SCHEMA = "1"
APPLIED_CLASSES = {"cosmetic": ["cosmetic"], "otc": ["otc"], "combination": ["otc"], "unknown": ["cosmetic"]}
CLASS_NOTICE = {"combination": "combination_otc_basis", "unknown": "unverified_class"}
REGULATORY_VERDICTS = ("regulated", "conditional", "allowed")
LOCAL_VERDICTS = ("irrelevant", "needs_fix")  # cultural 은 이번 범위 제외(D9-2)
LOCAL_CLASS = "common"


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


def build_bundle(target_country: str, regulatory_class: str) -> dict[str, Any]:
    """DB 적재본으로 고정 묶음을 만든다. 반환 dict 에 'sha256'(정규 직렬화 지문)을 넣는다."""
    if regulatory_class not in APPLIED_CLASSES:
        raise BundleError(f"규제 분류 {regulatory_class!r} — 미선택·미지원 분류로 규제 검사를 시작하지 않는다",
                          retryable=False, code="REGULATORY_CLASS_INVALID")
    applied = APPLIED_CLASSES[regulatory_class]
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
        if not entries:
            local = {"status": "unavailable", "error": "현지 사전 행 없음", "entries": [], "skipped": skipped}
        elif lbad:
            local = {"status": "unavailable", "error": f"현지 사전 필수값 누락: {sorted(lbad)}", "entries": [], "skipped": skipped}
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
