"""고정 사전 묶음 — 정책 규칙 주입·검증과 AI 인계 형식(pipeline.handoff.bundle.RuntimeBundle) 변환. DB 불필요."""
from __future__ import annotations

import copy
import json

import pytest

from app.dictionary_bundle import BundleError, load_policy_rules, to_ai_bundle
from app.manifest import fingerprint
from pipeline.handoff.bundle import check_bundle
from pipeline.handoff.bundle import load_policy_rules as repo_rules


def _rules(**over):
    r = copy.deepcopy(repo_rules())  # 저장소 운영 규칙(AI 소유, policy@2026-10-08.1)
    r.update(over)
    return r


def _entry(ext, dt, cls, src, vko, vs, *, alt=None, fen=None, reason="사유", primary=True):
    return {"pk": 1, "external_id": ext, "dict_type": dt, "target_country": "US", "regulatory_class": cls, "source_expression": src,
            "variant_ko": vko, "forbidden_en": fen, "alternative_expression": alt, "verdict_status": vs, "source_verdict_status": vs,
            "reason": reason, "confirmed_date": "2026-09-30", "exclusion_context": "제외 맥락" if dt == "local" else None,
            "keep_context": "유지 맥락" if dt == "local" else None,
            "evidence": [{"pk": 9, "external_id": "WL-1", "source_type": "Warning Letter", "document": "문서", "quote": "인용",
                          "article": "21 CFR 700", "url": "https://example.org/wl", "is_primary": primary}] if dt == "regulatory" else []}


def _be_bundle(*, local_ok=True, rules=None):
    b = {
        "bundle_schema": "2", "target_country": "US", "regulatory_class": "cosmetic", "applied_classes": ["cosmetic", "common"],
        "class_notice": None,
        "regulatory": {"status": "ok", "entries": [
            _entry("RG-901", "regulatory", "cosmetic", "치료", ["치료"], "regulated", fen=["cure"]),
            # 시트 rewritable → 적재 regulated + 원값 보존. 대체 표현은 나누지 않는다(#71)
            {**_entry("RG-902", "regulatory", "cosmetic", "진정", ["진정"], "regulated", alt="Soothing; Calming"),
             "source_verdict_status": "rewritable"},
            _entry("RG-903", "regulatory", "common", "보습", ["보습"], "allowed"),
        ]},
        "local": ({"status": "ok", "error": None, "skipped": [], "entries": [
            _entry(f"LC-0{i}", "local", "common", f"항목 {i}", [f"패턴{i}"], "needs_fix") for i in range(1, 9)]}
                  if local_ok else {"status": "unavailable", "error": "현지 사전 조회 오류", "entries": [], "skipped": []}),
        "policy_rules": rules if rules is not None else _rules(),
    }
    b["sha256"] = fingerprint(b)
    return b


# ---------------------------------------------------------------------------------------------------------
# 묶음 변환 · 정책 규칙 — DB 불필요
# ---------------------------------------------------------------------------------------------------------
def test_to_ai_bundle_passes_ai_bundle_check_and_is_deterministic():
    be = _be_bundle()
    ai = to_ai_bundle(be)
    cb = check_bundle(ai, target_country="US")  # AI 쪽 구조·무결성·대응표 검사 통과
    assert ai == to_ai_bundle(copy.deepcopy(be))
    rg = cb.regulation
    # #71 확정 전: 나누지 않은 원문 문자열 — "대체 표현 있음"·스냅샷에만 쓰이고 ⑧ 지시로 쓰이지 않는다(PR #55 3217142)
    assert rg["RG-902"].alternative_expression == "Soothing; Calming" and rg["RG-902"].alternatives_split is None
    assert rg["RG-902"].has_alternative and not rg["RG-901"].has_alternative
    assert rg["RG-902"].verdict_status == "regulated" and rg["RG-902"].source_verdict_status == "rewritable"
    assert rg["RG-901"].variant_en == ["cure"] and rg["RG-903"].variant_en == []  # NULL → 영어 패턴 없음
    ev = rg["RG-901"].evidence  # 근거 배열 전체(PK 제외), 표시용은 대표 근거
    assert [e.document for e in ev] == ["문서"] and rg["RG-901"].primary_evidence.article == "21 CFR 700"
    assert cb.local["LC-01"].seller_message == "사유" and cb.local["LC-01"].item == "항목 1"
    assert "pk" not in json.dumps(ai)  # DB PK 는 AI 에 보내지 않는다
    assert cb.identity["rules_sha256"] == fingerprint(be["policy_rules"])  # BE 저장 지문과 AI identity 대조 가능


def test_to_ai_bundle_local_unavailable_and_missing_rules():
    ai = to_ai_bundle(_be_bundle(local_ok=False))
    assert ai["local"] is None and ai["local_unavailable"] == {"reason": "현지 사전 조회 오류"}
    check_bundle(ai, target_country="US")
    be = _be_bundle()
    be["policy_rules"] = None
    with pytest.raises(BundleError) as e:
        to_ai_bundle(be)
    assert e.value.code == "POLICY_RULES_UNAVAILABLE"


def test_load_policy_rules_rejects_overrides_and_d8_mismatch(tmp_path):
    assert load_policy_rules("")["rules_version"] == "policy@2026-10-08.1"  # 경로 미지정 = 저장소 운영 규칙
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_rules()), encoding="utf-8")
    assert load_policy_rules(str(p))["rules_version"] == "policy@2026-10-08.1"
    bad = _rules()
    bad["regulatory_class_map"]["combination"]["applied_classes"] = ["cosmetic", "common"]  # D8: combination → otc
    p.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(BundleError, match="combination"):
        load_policy_rules(str(p))
    ov = _rules(overrides=[{"keep": "RG-901", "drop": "RG-902"}])
    p.write_text(json.dumps(ov), encoding="utf-8")
    with pytest.raises(BundleError):
        load_policy_rules(str(p))
