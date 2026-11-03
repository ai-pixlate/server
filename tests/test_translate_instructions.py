"""⑧ 표현 지시 공급(#71) — DB 없이 지시 생성 조건 · 구분 규칙 위반 묶음 · 활성화 전 시도의 공급 대조를 확인한다(합성 값)."""
from __future__ import annotations

import copy

from app.dictionary_bundle import to_ai_bundle
from app.flows import downstream as ds
from app.manifest import fingerprint
from pipeline.handoff.bundle import check_bundle
from tests.test_exec_bundle import _be_bundle, _entry

TARGETS = {10, 11}


def _bundle():
    """규제 행: 정상(RG-1) · conditional(RG-2) · 대체 표현 없음(RG-3) · 템플릿(RG-4) · 대상 밖 블록(RG-5) · 구분 규칙 위반(RG-6)."""
    b = _be_bundle()
    b["regulatory"]["entries"] = [
        _entry("RG-1", "regulatory", "cosmetic", "진정", ["진정"], "regulated", alt="calm; soothing"),
        _entry("RG-2", "regulatory", "cosmetic", "주름", ["주름"], "conditional", alt="fine lines"),
        _entry("RG-3", "regulatory", "cosmetic", "치료", ["치료"], "regulated"),
        _entry("RG-4", "regulatory", "cosmetic", "차단", ["차단"], "regulated", alt="SPF [value]"),
        _entry("RG-5", "regulatory", "cosmetic", "완화", ["완화"], "regulated", alt="eases"),
        _entry("RG-6", "regulatory", "cosmetic", "개선", ["개선"], "regulated", alt="improves;betters"),
        _entry("RG-7", "regulatory", "common", "보습", ["보습"], "allowed"),
    ]
    return b


def _detail():
    def v(ref, status, fk):
        return {"dictionary_ref": ref, "verdict_status": status, "finding_key": fk}

    def m(ref, fk, block, text):
        return {"snapshot_key": ref, "finding_key": fk, "block_id": block, "matched_text": text}

    return {
        "verdicts": [v("RG-1", "regulated", "f1"), v("RG-2", "conditional", "f1"), v("RG-3", "regulated", "f1"),
                     v("RG-4", "regulated", "f1"), v("RG-5", "regulated", "f1"), v("RG-6", "regulated", "f1"),
                     v("RG-7", "allowed", "f1"), v("LC-01", "needs_fix", "f2")],
        "matches": [m("RG-1", "f1", 10, "진정"), m("RG-1", "f1", 10, "진정"),  # 같은 매칭 중복 → 지시 하나
                    m("RG-1", "f1", 11, "진정"),  # 다른 대상 블록 → 따로
                    m("RG-2", "f1", 10, "주름"), m("RG-3", "f1", 10, "치료"), m("RG-4", "f1", 10, "차단"),
                    m("RG-5", "f1", 99, "완화"),  # 이번 대상이 아닌 블록
                    m("RG-6", "f1", 11, "개선"), m("RG-7", "f1", 11, "보습"), m("LC-01", "f2", 11, "패턴1")],
    }


class _Db:
    def __init__(self, detail):
        self.detail = detail

    def execute(self, *a, **k):
        return self

    def scalar(self):
        return self.detail


def test_instructions_only_for_regulated_with_split_alternative_on_target_blocks():
    out = ds._translate_instructions(_Db(_detail()), 1, TARGETS, _bundle())
    assert out == [{"block_key": "blk_10", "external_id": "RG-1", "matched_text": "진정"},
                   {"block_key": "blk_11", "external_id": "RG-1", "matched_text": "진정"}]


def test_split_rule_violation_in_stored_bundle_skips_instruction_without_error():
    b = _bundle()
    out = ds._translate_instructions(_Db(_detail()), 1, TARGETS, b)  # 예외 없이 RG-6 만 빠진다
    assert all(i["external_id"] != "RG-6" for i in out)
    rg = check_bundle(to_ai_bundle(b), target_country="US").regulation  # AI 묶음: 원문 문자열 — 대체 표현 있음 · 지시 불가
    assert rg["RG-6"].alternative_expression == "improves;betters" and rg["RG-6"].alternatives_split is None
    assert rg["RG-6"].has_alternative and rg["RG-1"].alternatives_split == ["calm", "soothing"]


def test_no_adopted_analysis_detail_gives_no_instructions():
    assert ds._translate_instructions(_Db(None), 1, TARGETS, _bundle()) == []


def _patch_supply(monkeypatch, bundle):
    glossary = {"status": "ok", "version": "db-sha256:0", "reason": None, "terms": []}
    monkeypatch.setattr(ds, "_run_scope", lambda db, run_id: {"analysis_task_id": 5, "target_lang": "en"})
    monkeypatch.setattr(ds, "_analysis_bundle", lambda db, aid: (7, bundle))
    monkeypatch.setattr(ds, "_glossary_supply", lambda db, lang, sid, targets: (glossary, {"rules": "r", "target_lang": "en"}))
    return glossary


def test_attempt_made_before_activation_keeps_old_supply(monkeypatch):
    bundle, db = _bundle(), _Db(_detail())
    glossary = _patch_supply(monkeypatch, bundle)
    targets = [{"id": 10}, {"id": 11}]
    # 활성화 전 코드가 시도 명세에 고정한 공급 지문(instructions_rule 없음 · 지시 없음)
    old = {"bundle_handoff_id": 7, "bundle_sha256": bundle["sha256"], "glossary_sha256": fingerprint(glossary),
           "glossary_selection": {"rules": "r", "target_lang": "en"}, "instructions": []}
    assert not ds._instructions_enabled(old)
    legacy = ds._translate_supply(db, 1, 3, targets, instructions=ds._instructions_enabled(old))
    assert ds._supply_ref(legacy) == old and legacy["instructions"] == []  # INPUT_CHANGED 없이 이전 규칙으로 실행
    # 새로 만드는 시도(최초 · 자동/수동 재시도)는 지시와 규칙 표시를 고정한다
    new = ds._supply_ref(ds._translate_supply(db, 1, 3, targets))
    assert new != old and new["instructions_rule"] == ds.INSTRUCTIONS_RULE and len(new["instructions"]) == 2
    assert ds._instructions_enabled(new) and ds._instructions_enabled(None)
    again = ds._supply_ref(ds._translate_supply(db, 1, 3, targets, instructions=ds._instructions_enabled(new)))
    assert again == new  # 새 시도의 실행 때 대조도 같다


def test_supply_ref_is_deterministic_for_same_inputs(monkeypatch):
    bundle = _bundle()
    _patch_supply(monkeypatch, bundle)
    a = ds._supply_ref(ds._translate_supply(_Db(_detail()), 1, 3, [{"id": 10}]))
    b = ds._supply_ref(ds._translate_supply(_Db(copy.deepcopy(_detail())), 1, 3, [{"id": 10}]))
    assert a == b and a["instructions"] == [{"block_key": "blk_10", "external_id": "RG-1", "matched_text": "진정"}]
