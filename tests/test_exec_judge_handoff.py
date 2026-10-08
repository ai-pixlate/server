"""③-1·③-1′ AI 인계 연결 — HandoffJudge(run_judgment, PR #55) → JudgeOutcome → 저장, 실제 PostgreSQL 로 N2 → N3.

LLM 은 AI 계약 예제의 가짜 모델(pipeline.handoff.examples)이다. 실제 Gemini 호출·판정 품질 검증이 아니다.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app import ai_adapters, execution
from app.storage import MemoryStore, set_store
from pipeline.config import load_config
from pipeline.handoff.examples import FailingLlm
from pipeline.handoff.examples import FakeJudge as FakeJudgeLlm
from tests.exec_db import bound_app_db, fresh_database, requires_db
from tests.exec_fakes import FakeAnalyzer, QueueDispatcher
from tests.test_exec_bundle import _rules


@pytest.fixture
def rules_file(tmp_path, monkeypatch):
    p = tmp_path / "policy_rules.json"
    p.write_text(json.dumps(_rules(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PIXLATE_POLICY_RULES", str(p))
    return p


# ---------------------------------------------------------------------------------------------------------
# N2 → N3 통합 — 실제 PostgreSQL + HandoffJudge(run_judgment) + 가짜 LLM
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def env(dburl, rules_file):
    from app import db as app_db

    store = MemoryStore()
    set_store(store)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    ai_adapters.reset_adapters()
    with app_db.engine.begin() as c:
        c.execute(text("DELETE FROM expression_dictionary"))
    yield {"store": store, "q": q}
    execution.set_dispatcher(None)
    set_store(None)
    ai_adapters.reset_adapters()


def _mutate_regulatory(raw):
    raw["blocks"][1]["source_ko"] = "피부 치료 효과와 주름 개선"


@requires_db
@pytest.mark.parametrize("llm,local_status", [(FakeJudgeLlm(), "ok"), (FailingLlm(), "failed")])
def test_handoff_judge_runs_n2_to_n3(env, llm, local_status):
    from tests.test_exec_analysis import _client, _job, _q

    cfg = load_config()
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(mutate=_mutate_regulatory), judge=ai_adapters.HandoffJudge(cfg, llm=llm))
    ids = _job(env)
    c = _client(ids)
    assert c.post(f"/v1/jobs/{ids['job']}/analyze").status_code == 202
    env["q"].drain()
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert (job["status"], job["currentStep"]) == ("review", "N3")  # 현지 판정 실패도 N3 진행(D9-1)
    secs = _q("SELECT id, bucket, exclusion_reason, content_findings, original_verdict FROM section WHERE job_id = :j ORDER BY id",
              j=ids["job"])
    assert all(s["content_findings"] is not None for s in secs)  # 현지 실패 섹션도 NULL 이 아니다(BE 확인 4)
    reg = next(s for s in secs if any(f["content_type"] == "regulatory_expression_match" for f in s["content_findings"]["findings"]))
    assert reg["bucket"] == "exclude" and reg["exclusion_reason"] == "auto_regulatory"
    assert {s["original_verdict"]["local_status"] for s in secs} == {local_status}
    if local_status == "failed":
        other = [s for s in secs if s["id"] != reg["id"]]
        assert all(s["exclusion_reason"] == "auto_local_failed" for s in other)
        assert all(not any(f["content_type"].startswith("local_") for f in s["content_findings"]["findings"]) for s in secs)
    else:
        assert all(sum(f["content_type"].startswith("local_") for f in s["content_findings"]["findings"]) == 8 for s in secs)
    audit = _q("SELECT detail FROM audit_log WHERE action_type = 'analysis_result_adopted' AND target_id = :s", s=reg["id"])[0]["detail"]
    assert audit["inputs"]["policy"]["rules_version"] == "policy@2026-10-08.1"
    assert {m["snapshot_key"] for m in audit["matches"]} >= {"RG-901", "RG-902"}


class _RecordingLlm(FakeJudgeLlm):
    def __init__(self):
        self.payloads = []

    def __call__(self, prompt, payload, image):
        self.payloads.append(json.loads(payload))
        return super().__call__(prompt, payload, image)


@requires_db
def test_section_attempt_gets_neighbor_context(env):
    # PR #56 AI 요청 4: 섹션별 시도도 분석 전체를 문맥으로 주고 target_section_keys 로 대상만 판정한다(D5 앞뒤 섹션 문맥)
    from tests.test_exec_analysis import _client, _job

    llm = _RecordingLlm()
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(), judge=ai_adapters.HandoffJudge(load_config(), llm=llm))
    ids = _job(env)
    assert _client(ids).post(f"/v1/jobs/{ids['job']}/analyze").status_code == 202
    env["q"].drain()
    assert len(llm.payloads) == 2  # 섹션 2개 — 섹션마다 한 번, 대상 섹션만 판정
    first, second = sorted(llm.payloads, key=lambda p: p["section"])
    assert first["context"]["prev"] is None and first["context"]["next"]  # 첫 섹션의 다음 섹션 원문
    assert second["context"]["prev"] and second["context"]["next"] is None
