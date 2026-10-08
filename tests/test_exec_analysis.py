"""N2 초기 분석 → N3 — 실제 PostgreSQL + 메모리 저장소 + AI 대역(FakeAnalyzer·FakeJudge).

대역 성공은 BE 저장·전이 검증이며 실제 AI 통합 성공이 아니다.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import ai_adapters, execution
from app.manifest import sha256_bytes
from app.storage import MemoryStore, set_store
from pipeline.errors import AnalyzeError
from tests.exec_db import bound_app_db, fresh_database, requires_db, seed_job
from tests.exec_fakes import FakeAnalyzer, FakeJudge, QueueDispatcher, sample_source_png, seed_dictionary

pytestmark = requires_db


@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def env(dburl):
    from app import db as app_db

    store = MemoryStore()
    set_store(store)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    ai_adapters.reset_adapters()
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(), judge=FakeJudge())
    with app_db.engine.begin() as c:
        c.execute(text("DELETE FROM expression_dictionary"))
    yield {"store": store, "q": q}
    execution.set_dispatcher(None)
    set_store(None)
    ai_adapters.reset_adapters()


def _job(env, *, dictionary=True, local=True, regulatory_class="cosmetic", sources=1):
    from app import db as app_db

    with app_db.engine.begin() as c:
        if dictionary:
            seed_dictionary(c, with_local=local)
        ids = seed_job(c, regulatory_class=regulatory_class)
        data = sample_source_png()
        for i in range(sources):
            key = f"source/{ids['job']}/src{i}.png"
            env["store"].put(key, data)
            c.execute(
                text("INSERT INTO source_image (job_id, upload_order, file_url, width, height, sha256) "
                     "VALUES (:j, :o, :k, 600, 1000, :h)"),
                {"j": ids["job"], "o": i + 1, "k": key, "h": sha256_bytes(data)},
            )
    return ids


def _client(ids):
    from app.main import app

    c = TestClient(app)
    r = c.post("/v1/auth/login", json={"email": ids["email"], "password": "x"})
    c.headers["Authorization"] = f"Bearer {r.json()['accessToken']}"
    return c


def _q(sql, **p):
    from app import db as app_db

    with app_db.engine.begin() as c:
        res = c.execute(text(sql), p)
        return [dict(r) for r in res.mappings().all()] if res.returns_rows else None


def _mutate_regulatory(raw):
    raw["blocks"][1]["source_ko"] = "피부 치료 효과와 주름 개선"  # 규제 2개 매칭(치료=금지형, 주름 개선=조건부)


def test_happy_path_analysis_to_n3(env):
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(mutate=_mutate_regulatory))
    ids = _job(env)
    c = _client(ids)
    r = c.post(f"/v1/jobs/{ids['job']}/analyze")
    assert r.status_code == 202, r.text
    assert c.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N2"
    # 분석 시도만 큐에. 아직 섹션은 보이지 않는다
    assert [m[0] for m in env["q"].messages] == ["app.tasks.run_analyze"]
    env["q"].drain(limit=1)
    assert c.get(f"/v1/jobs/{ids['job']}/sections").json() == {"include": [], "exclude": []}  # 숨김(D3)
    env["q"].drain()
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert (job["status"], job["currentStep"], job["userFacingStatus"]) == ("review", "N3", "section_review")
    secs = c.get(f"/v1/jobs/{ids['job']}/sections").json()
    assert len(secs["exclude"]) == 1 and len(secs["include"]) == 1
    ex = secs["exclude"][0]
    assert ex["exclusionReason"] == "auto_regulatory" and ex["excludedStage"] == "N3"
    det = c.get(f"/v1/jobs/{ids['job']}/sections/{ex['id']}").json()
    findings = det["contentFindings"]["findings"]
    assert len(findings) == 1 and findings[0]["status"] == "present"
    blk = _q("SELECT id FROM text_block WHERE section_id = :s AND source_ko LIKE '%치료%'", s=ex["id"])[0]["id"]
    assert findings[0]["evidence_block_ids"] == [blk]  # 임시 키 → 이번 실행 DB id
    vs = {v["verdict_status"]: v for v in det["verdicts"]}
    assert vs["regulated"]["verdict_type"] == "regulatory" and vs["conditional"]["verdict_type"] == "regulatory_conditional"
    assert vs["regulated"]["basis_article"] == "21 CFR 700"
    audit = _q("SELECT detail FROM audit_log WHERE action_type = 'analysis_result_adopted' AND target_id = :s", s=ex["id"])[0]["detail"]
    assert {m["matched_text"] for m in audit["matches"]} == {"치료", "주름 개선"}
    assert all(m["source_ko"][m["start"]:m["end"]] == m["matched_text"] for m in audit["matches"])
    assert {s["external_id"] for s in audit["dictionary_snapshots"]} == {"RG-901", "RG-902"}
    run = _q("SELECT * FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL", j=ids["job"])[0]
    assert run["status"] == "done" and run["run_kind"] == "analysis" and run["task_type"] == "ocr"
    assert _q("SELECT current_analysis_task_id FROM job WHERE id = :j", j=ids["job"])[0]["current_analysis_task_id"] == run["id"]
    # 섹션 이미지는 검증 키에 고정, DB 에는 S3 키만
    keys = [s["image_key"] for s in _q("SELECT image_key FROM section WHERE job_id = :j", j=ids["job"])]
    assert all(k.startswith(f"verified/{ids['job']}/") for k in keys)
    assert all(env["store"].exists(k) for k in keys)
    tasks = c.get(f"/v1/jobs/{ids['job']}/tasks").json()
    assert tasks["currentStep"] == "N3"


def test_local_ai_failure_excludes_section_and_opens_n3(env):
    judge = FakeJudge()
    ai_adapters.set_adapters(judge=judge)
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain(limit=1)
    sec_ids = sorted(r["id"] for r in _q("SELECT id FROM section WHERE job_id = :j", j=ids["job"]))
    judge.local_fail = {f"sec_{sec_ids[0]}"}
    env["q"].drain()
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert job["currentStep"] == "N3"
    s0 = _q("SELECT bucket, exclusion_reason, content_findings, original_verdict FROM section WHERE id = :s", s=sec_ids[0])[0]
    assert (s0["bucket"], s0["exclusion_reason"]) == ("exclude", "auto_local_failed")
    assert s0["content_findings"] is not None and s0["original_verdict"]["local_status"] == "failed"
    att = _q("SELECT status, error_code FROM job_async_task WHERE stage = 'judge' AND unit_id = :s", s=sec_ids[0])
    assert att == [{"status": "failed", "error_code": "LOCAL_JUDGE_FAILED"}]  # 실패는 실패로 남는다
    s1 = _q("SELECT bucket FROM section WHERE id = :s", s=sec_ids[1])[0]
    assert s1["bucket"] == "include"


def test_local_dictionary_unavailable_is_not_exclusion(env):
    ids = _job(env, local=False)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain()
    assert c.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N3"
    assert {r["bucket"] for r in _q("SELECT bucket FROM section WHERE job_id = :j", j=ids["job"])} == {"include"}
    run = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL", j=ids["job"])[0]
    assert run == {"status": "done", "error_code": "LOCAL_DICT_UNAVAILABLE"}
    audit = _q("SELECT detail FROM audit_log WHERE action_type = 'analysis_result_adopted' AND detail->'execution'->>'job_id' = :j",
               j=str(ids["job"]))
    assert audit and all(a["detail"]["inspection"]["local"]["status"] == "not_inspected" for a in audit)


def test_regulatory_dictionary_missing_retries_twice_then_n2_error_and_retry_button(env):
    ids = _job(env, dictionary=False)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain()
    atts = _q("SELECT attempt_no, status, error_code, retry_origin, is_current FROM job_async_task "
              "WHERE job_id = :j AND stage = 'analyze' ORDER BY attempt_no", j=ids["job"])
    assert [(a["attempt_no"], a["status"], a["retry_origin"]) for a in atts] == [(1, "failed", None), (2, "failed", "auto"), (3, "failed", "auto")]
    assert all(a["error_code"] == "REGULATORY_DICT_LOOKUP_FAILED" for a in atts)
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert (job["status"], job["currentStep"], job["userFacingStatus"]) == ("failed", "N2", "failed")
    # [다시 시도] = 새 분석 실행
    from app import db as app_db

    with app_db.engine.begin() as conn:
        seed_dictionary(conn)
    r = c.post(f"/v1/jobs/{ids['job']}/analyze")
    assert r.status_code == 202
    env["q"].drain()
    assert c.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N3"
    runs = _q("SELECT status FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL ORDER BY id", j=ids["job"])
    assert [r["status"] for r in runs] == ["failed", "done"]


def test_judge_regulatory_failure_retries_and_recovers(env):
    judge = FakeJudge()
    ai_adapters.set_adapters(judge=judge)
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain(limit=1)
    sid = min(r["id"] for r in _q("SELECT id FROM section WHERE job_id = :j", j=ids["job"]))
    judge.reg_fail = {f"sec_{sid}": 1}
    env["q"].drain()
    assert c.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N3"
    atts = _q("SELECT attempt_no, status, is_current FROM job_async_task WHERE stage = 'judge' AND unit_id = :s ORDER BY attempt_no", s=sid)
    assert atts == [{"attempt_no": 1, "status": "failed", "is_current": False}, {"attempt_no": 2, "status": "done", "is_current": True}]


def test_judge_regulatory_failure_exhausted_fails_run_keeps_nothing_visible(env):
    judge = FakeJudge()
    ai_adapters.set_adapters(judge=judge)
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain(limit=1)
    sid = min(r["id"] for r in _q("SELECT id FROM section WHERE job_id = :j", j=ids["job"]))
    judge.reg_fail = {f"sec_{sid}": 99}
    env["q"].drain()
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert (job["status"], job["currentStep"]) == ("failed", "N2")
    assert _q("SELECT count(*) AS n FROM section WHERE job_id = :j", j=ids["job"])[0]["n"] == 0  # 숨은 섹션 정리


def test_judge_unknown_block_key_is_rejected_not_saved(env):
    def bad(out):
        out.findings.append(ai_adapters.FindingOut(finding_key="f_x", content_type="x", status="absent",
                                                   evidence_block_keys=["blk_999999"], evidence_source="text", reason="r"))
        return out

    ai_adapters.set_adapters(judge=FakeJudge(mutate=bad))
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain()
    # 모르는 키 = 저장 실패. 재시도해도 같은 결과라 한도 후 N2 오류
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert (job["status"], job["currentStep"]) == ("failed", "N2")
    h = _q("SELECT state, reject_reason FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
           "WHERE a.job_id = :j AND a.stage = 'judge'", j=ids["job"])
    assert h and all(x["state"] == "rejected" and "모르는 블록 키" in x["reject_reason"] for x in h)
    assert _q("SELECT count(*) AS n FROM section_verdict sv JOIN section s ON s.id = sv.section_id WHERE s.job_id = :j",
              j=ids["job"])[0]["n"] == 0


def test_analyzer_non_retryable_error_fails_immediately(env):
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(fail=AnalyzeError("IMAGE_OPEN_FAILED", "깨진 파일", 1)))
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain()
    atts = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'analyze'", j=ids["job"])
    assert atts == [{"status": "failed", "error_code": "IMAGE_OPEN_FAILED"}]
    assert c.get(f"/v1/jobs/{ids['job']}").json()["status"] == "failed"


def test_analyzer_retryable_error_recovers(env):
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(fail=AnalyzeError("OCR_FAILED", "timeout", 1), fails=1))
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain()
    assert c.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N3"


def test_section_image_missing_is_rejected(env):
    def drop_image(raw):
        import os

        os.remove(raw["sections"][0]["image_path"])

    ai_adapters.set_adapters(analyzer=FakeAnalyzer(mutate=drop_image))
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain()
    assert c.get(f"/v1/jobs/{ids['job']}").json()["status"] == "failed"
    h = _q("SELECT reject_reason FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j", j=ids["job"])
    assert h and all("섹션 이미지 없음" in x["reject_reason"] for x in h)
    assert _q("SELECT count(*) AS n FROM section WHERE job_id = :j", j=ids["job"])[0]["n"] == 0  # 부분 저장 없음


def test_duplicate_delivery_runs_once(env):
    analyzer = FakeAnalyzer()
    ai_adapters.set_adapters(analyzer=analyzer)
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    msg = env["q"].messages[0]
    env["q"].messages.append(msg)  # 같은 시도 메시지 중복
    env["q"].drain()
    assert analyzer.calls == 1
    assert c.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N3"


def test_analyze_twice_returns_existing_run(env):
    ids = _job(env)
    c = _client(ids)
    a = c.post(f"/v1/jobs/{ids['job']}/analyze").json()
    b = c.post(f"/v1/jobs/{ids['job']}/analyze").json()
    assert a["taskId"] == b["taskId"]
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL", j=ids["job"])[0]["n"] == 1


def test_analyze_rejected_without_regulatory_class(env):
    ids = _job(env, regulatory_class=None)
    c = _client(ids)
    r = c.post(f"/v1/jobs/{ids['job']}/analyze")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"


def test_source_changed_after_start(env):
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    key = _q("SELECT file_url FROM source_image WHERE job_id = :j", j=ids["job"])[0]["file_url"]
    env["store"].put(key, b"tampered")
    env["q"].drain()
    a = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'analyze'", j=ids["job"])
    assert a == [{"status": "failed", "error_code": "SOURCE_CHANGED"}]


def test_late_judge_result_after_abort_is_not_saved(env):
    ids = _job(env)
    c = _client(ids)
    c.post(f"/v1/jobs/{ids['job']}/analyze")
    env["q"].drain(limit=1)  # analyze 채택, judge 시도 대기
    judge_ids = [m[1][0] for m in env["q"].messages]
    leases = [execution.acquire(t, "slow-worker") for t in judge_ids]  # 판정 진행 중
    r = c.post(f"/v1/jobs/{ids['job']}/abort")
    assert r.json()["returnTo"] == "N1"
    # 늦은 결과: 권한을 잃어 등록 거절
    with pytest.raises(execution.LeaseLost):
        execution.register_handoff(leases[0], execution.Envelope(outcome="failed", target_count=1, payload={},
                                                                 input_fingerprint=leases[0].fingerprint))
    env["q"].drain()
    assert _q("SELECT count(*) AS n FROM section WHERE job_id = :j", j=ids["job"])[0]["n"] == 0
    job = c.get(f"/v1/jobs/{ids['job']}").json()
    assert (job["status"], job["currentStep"]) == ("draft", "N1")
    assert _q("SELECT count(*) AS n FROM source_image WHERE job_id = :j", j=ids["job"])[0]["n"] == 1  # 입력 유지


def test_split_fallback_record_kept_in_adoption(env):
    # ① 분해 실패 → 원본 전체 섹션 대체 기록은 AnalyzeResult 밖 인계 payload 로 오고 채택 기록에 남는다(D9-1, PR #55 BE 확인 1)
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(split_fallback=True))
    ids = _job(env)
    c = _client(ids)
    assert c.post(f"/v1/jobs/{ids['job']}/analyze").status_code == 202
    env["q"].drain()
    assert c.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N3"
    vr = _q("SELECT h.verify_result FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
            "WHERE a.job_id = :j AND a.stage = 'analyze' AND h.state = 'adopted'", j=ids["job"])[0]["verify_result"]
    src = _q("SELECT id FROM source_image WHERE job_id = :j", j=ids["job"])[0]["id"]
    assert [(f["kind"], f["source_image_id"]) for f in vr["split_fallbacks"]] == [("section_split_whole_image", src)]
