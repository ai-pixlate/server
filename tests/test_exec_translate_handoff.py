"""⑧ AI 인계 연결 — HandoffTranslator(run_translate, PR #55) → TranslateOutcome → 채택, 실제 PostgreSQL 로 N3 → N4 → N5.

LLM 은 AI 계약 예제의 가짜 모델(pipeline.handoff.examples.FakeTranslate)이다. 실제 Gemini 호출·번역 품질 검증이 아니다.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app import ai_adapters
from pipeline.config import load_config
from pipeline.handoff.examples import FakeTranslate
from tests.exec_db import requires_db
from tests.exec_fakes import FakeAnalyzer
from tests.test_exec_bundle import _rules
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401 — 픽스처 재사용

pytestmark = requires_db


def _mutate(raw):
    raw["blocks"][1]["source_ko"] = "가짜완화 크림"  # 섹션 1 본문 — 대체 표현이 있는 금지형 매칭(포함 섹션, 표현 지시 대상)
    raw["blocks"][5]["source_ko"] = "판독불가 문구"  # 섹션 2 본문 — 가짜 모델의 명시적 실패(부분 실패)


@pytest.fixture
def handoff_env(env, tmp_path, monkeypatch):
    from app import db as app_db

    p = tmp_path / "policy_rules.json"
    p.write_text(json.dumps(_rules(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PIXLATE_POLICY_RULES", str(p))
    with app_db.engine.begin() as c:
        did = c.execute(text(
            "INSERT INTO expression_dictionary (external_id, source_expression, variant_ko, forbidden_en, target_country, regulatory_class, "
            "dict_type, verdict_status, source_verdict_status, alternative_expression, reason, confirmed_date) VALUES "
            "('RG-777', '가짜완화', CAST(:v AS jsonb), NULL, 'US', 'cosmetic', 'regulatory', 'regulated', 'rewritable', "
            "'the look of calm', '완화 표방 금지', DATE '2026-09-30') RETURNING id"), {"v": json.dumps(["가짜완화"], ensure_ascii=False)}
        ).scalar_one()
        c.execute(text("INSERT INTO expression_dictionary_evidence (dictionary_id, external_id, evidence_source_type, is_primary) "
                       "VALUES (:d, 'WL-777', 'Warning Letter', true)"), {"d": did})
        c.execute(text("DELETE FROM glossary"))
        c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement) "
                       "VALUES ('크림', 'cream', 'en', 'skincare', 'enforced')"))
    llm = FakeTranslate()
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(mutate=_mutate),
                             translator=ai_adapters.HandoffTranslator(load_config(), llm=llm))
    return env


def test_handoff_translator_partial_failure_reaches_n5_with_fixed_supply(handoff_env):
    ids = _to_n3(handoff_env)
    c = ids["c"]
    assert c.post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    handoff_env["q"].drain()
    assert _step(ids) == ("review", "N5")  # 번역 일부 실패는 성공분 보존·실패 칸 비움으로 N5(D9-3)
    rows = {r["source_ko"]: r for r in _q(
        "SELECT tb.source_ko, tb.trans_1, tb.revision FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j",
        j=ids["job"])}
    assert rows["가짜완화 크림"]["trans_1"] == "the look of fake calm cream" and rows["가짜완화 크림"]["revision"] == 1
    assert rows["판독불가 문구"]["trans_1"] is None
    atts = _q("SELECT a.status, a.error_code, a.input_manifest FROM job_async_task a WHERE a.job_id = :j AND a.stage = 'translate' "
              "ORDER BY a.unit_id", j=ids["job"])
    assert [(a["status"], a["error_code"]) for a in atts] == [("done", None), ("failed", "TRANSLATE_PARTIAL")]
    supply = atts[0]["input_manifest"]["supply"]  # 시도 생성 때 고정한 공급 지문
    # #71 확정 전에는 대체 표현이 원문 문자열이라 ⑧ 표현 지시를 보내지 않는다(PR #55 3217142)
    assert supply["instructions"] == []
    assert supply["bundle_sha256"] and supply["glossary_sha256"]


def test_handoff_translator_rejects_changed_supply(handoff_env):
    from app import db as app_db

    ids = _to_n3(handoff_env)
    c = ids["c"]
    assert c.post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    held = [m for m in handoff_env["q"].drain(skip={"app.tasks.run_translate"}) if m[0] == "app.tasks.run_translate"]
    assert held  # ⑧ 시도는 만들어졌고 아직 실행 전
    with app_db.engine.begin() as cn:  # 실행 전에 용어집이 바뀌면
        cn.execute(text("UPDATE glossary SET term_target = 'creme'"))
    handoff_env["q"].messages.extend(held)
    handoff_env["q"].drain()
    codes = {a["error_code"] for a in _q("SELECT error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate'", j=ids["job"])}
    assert "INPUT_CHANGED" in codes  # 고정 공급과 다르면 계산하지 않는다(5.32 입력 고정)
