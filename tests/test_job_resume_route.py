"""JOB-08 `POST /jobs/{jobId}/resume` · JOB-05 `jobRetry` — N4 작업 단위 재시도의 공개 경로(PM 11-27 · FE 11-27 동의 · 11-28 응답 세부 합의).

검증: 소유권(404) · 202 뒤 JOB-05 재조회 · 대체 진행만이면 202 에서 바로 N5 · 실제 차단 시 변경 없음(409 RETRY_NOT_ALLOWED +
details.blocking) · 중복 요청 409 INVALID_STATE · JOB-05 조회 뒤 상태가 바뀌면 POST 가 다시 판정 · failed · N4 밖에서 jobRetry 생략 ·
정본(openapi)의 응답 필드 · enum 과 서버 값 일치.
판정 규칙 자체는 test_n4_job_recovery 가 다룬다. 대역 · 준비는 같은 파일들과 같다.
"""
from __future__ import annotations

from tests.exec_db import requires_db
from tests.test_job_state_timers import _age_state, _fresh, _new_job, _q, _state, _to_n3, dburl, env  # noqa: F401 — fixture 재사용
from tests.test_n4_job_recovery import _attempts, _job_row, _stage_timeout, _timed_out
from tests.test_timeout_followups import _inpaint_timeout, _sections

from app.flows import downstream

pytestmark = requires_db


def _tasks(ids):
    r = ids["c"].get(f"/v1/jobs/{ids['job']}/tasks")
    assert r.status_code == 200, r.text
    return r.json()


def _resume(ids, client=None):
    return (client or ids["c"]).post(f"/v1/jobs/{ids['job']}/resume")


def test_resume_other_sellers_job_is_404_and_changes_nothing(env):
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    other = _new_job(env)  # 다른 셀러
    before = (_attempts(job), _job_row(job))
    r = _resume(ids, other["c"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND" and r.json()["error"]["details"]["reason"] == "job_not_found"
    assert (_attempts(job), _job_row(job)) == before and env["q"].messages == []
    assert _resume({"c": ids["c"], "job": 999999999}).status_code == 404  # 없는 작업


def test_resume_202_then_job05_and_duplicate_is_invalid_state(env):
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    t = _tasks(ids)
    assert t["jobStatus"] == "failed" and t["currentStep"] == "N4"
    assert t["jobRetry"] == {"available": True, "blocking": []}
    old = {a["id"] for a in _attempts(job)}
    r = _resume(ids)
    assert r.status_code == 202, r.text
    body = r.json()
    assert {k: body[k] for k in ("jobId", "status", "currentStep")} == {"jobId": job, "status": "processing", "currentStep": "N4"}
    # retriedTaskIds = 이번 요청이 새로 만든 재시도 시도(실패 시도를 대체)만, fallbackSectionIds = 대체 진행 섹션(여기선 없음)
    new = {a["id"]: a for a in _attempts(job) if a["id"] not in old}
    assert sorted(body["retriedTaskIds"]) == sorted(new) and all(a["supersedes_task_id"] in set(tr) | set(inp) for a in new.values())
    assert len(body["retriedTaskIds"]) == len(tr) + len(inp) and body["fallbackSectionIds"] == []
    assert _state(ids) == ("processing", "N4", "translating") and _fresh(job) is True  # 30분 새로 셈
    assert "jobRetry" not in _tasks(ids)  # failed · N4 밖에서는 속성 생략(null 아님)
    created = _attempts(job)
    d = _resume(ids)  # 이미 처리 중 — 중복 요청
    assert d.status_code == 409
    err = d.json()["error"]
    assert err["code"] == "INVALID_STATE" and not (err.get("details") or {}).get("blocking")  # 재시도 불가(RETRY_NOT_ALLOWED)와 구분
    assert _attempts(job) == created  # 새 시도 한 벌만
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")


def test_resume_outside_failed_n4_is_invalid_state_without_job_retry(env):
    ids = _to_n3(env)
    assert "jobRetry" not in _tasks(ids)
    r = _resume(ids)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert _state(ids)[:2] == ("review", "N3")


def test_failed_n4_without_open_downstream_run_omits_job_retry_and_resume_is_invalid_state(env):
    """생략 조건의 두 번째 갈래: failed · N4 이지만 진행 중인 하류 실행이 없음 → jobRetry 속성 생략(null 아님), JOB-08 은 409 INVALID_STATE.
    인위 상태(정상 흐름에서는 하류 실행 없이 failed · N4 가 되지 않는다 — 판정 갈래만 본다)."""
    ids = _to_n3(env)
    job = ids["job"]
    assert not _q("SELECT id FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND run_kind = 'downstream'", j=job)
    _q("UPDATE job SET status = 'failed', current_step = 'N4', user_facing_status = 'failed' WHERE id = :j RETURNING id", j=job)
    t = _tasks(ids)
    assert (t["jobStatus"], t["currentStep"]) == ("failed", "N4") and "jobRetry" not in t
    before = (_attempts(job), _job_row(job))
    r = _resume(ids)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert (_attempts(job), _job_row(job)) == before


def test_resume_all_fallback_returns_202_already_in_n5(env, monkeypatch):
    """다시 실행할 것 없이 모두 대체 진행이면 같은 요청에서 바로 N5 — FE 는 진행 화면 없이 N5 로."""
    monkeypatch.setitem(downstream.MAX_RETRY, "preview", 0)  # 미리보기 시간 초과 = 소진 → 원본 표시로 대체
    ids = _stage_timeout(env, {"app.tasks.run_preview"})
    job = ids["job"]
    pv = _timed_out(job, "preview")
    assert pv
    units = sorted({r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=pv)})
    assert _tasks(ids)["jobRetry"] == {"available": True, "blocking": []}  # 대체 진행은 차단이 아니다
    r = _resume(ids)
    assert r.status_code == 202, r.text
    assert r.json() == {"jobId": job, "status": "review", "currentStep": "N5", "retriedTaskIds": [], "fallbackSectionIds": units}
    assert _state(ids) == ("review", "N5", "reviewing") and env["q"].messages == []


def test_resume_blocked_label_changes_nothing_and_matches_job05(env, monkeypatch):
    monkeypatch.setitem(downstream.MAX_RETRY, "label", 0)  # 라벨 소진 = 임시 동작으로 막힘(확정 정책 구현은 후순위)
    ids = _stage_timeout(env, {"app.tasks.run_label"})
    job = ids["job"]
    labels = _timed_out(job, "label")
    units = sorted({r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=labels)})
    want = [{"sectionId": u, "stage": "label", "reason": "retry_limit_exceeded"} for u in units]  # 차단 전용 단계 값
    t = _tasks(ids)
    assert t["jobRetry"]["available"] is False and sorted(t["jobRetry"]["blocking"], key=lambda b: b["sectionId"]) == want
    before = (_attempts(job), _job_row(job), _sections(job))
    r = _resume(ids)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "RETRY_NOT_ALLOWED" and sorted(err["details"]["blocking"], key=lambda b: b["sectionId"]) == want
    assert (_attempts(job), _job_row(job), _sections(job)) == before and env["q"].messages == []  # 아무것도 바꾸지 않음
    assert ids["c"].delete(f"/v1/jobs/{job}").status_code == 200


def test_resume_job_wide_translation_zero_is_one_item_without_section(env, monkeypatch):
    """작업 전체 번역 성공 0 은 특정 섹션 하나의 문제가 아니라 sectionId = null 항목 하나(FE 11-28 합의)."""
    monkeypatch.setitem(downstream.MAX_RETRY, "translate", 0)
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    want = [{"sectionId": None, "stage": "translate", "reason": "job_translation_zero"}]
    assert _tasks(ids)["jobRetry"] == {"available": False, "blocking": want}
    before = (_attempts(job), _job_row(job))
    r = _resume(ids)
    assert r.status_code == 409 and r.json()["error"]["details"]["blocking"] == want
    assert (_attempts(job), _job_row(job)) == before
    assert ids["c"].delete(f"/v1/jobs/{job}").status_code == 200


def test_public_blocking_reason_table_covers_every_internal_block_reason():
    """차단 사유 누락 검증: _classify · job_retry_plan 이 낼 수 있는 BLOCK 사유는 BLOCK_REASONS 에 모두 있고, 각 사유는
    공개 대응(의도한 통합 포함)이 있거나 「공개 대응 없음(지금 정책에서 생기지 않음)」으로 따로 적혀 있다.
    대응 없는 사유 · 단계는 다른 값으로 바꾸지 않고 오류로 드러난다."""
    import inspect
    import re

    import pytest

    src = inspect.getsource(downstream._classify) + inspect.getsource(downstream.job_retry_plan)
    literal = set(re.findall(r'BLOCK, "(\w+)"', src)) | set(re.findall(r'BLOCK, "(\w+)" if', src))
    literal |= set(re.findall(r'"reason": "(\w+)"', inspect.getsource(downstream.job_retry_plan)))
    literal |= set(re.findall(r'"(\w+)" if _exhausted', inspect.getsource(downstream._no_retry_reason)))
    literal |= set(re.findall(r'else "(\w+)"', inspect.getsource(downstream._no_retry_reason)))
    assert literal and literal <= downstream.BLOCK_REASONS, literal - downstream.BLOCK_REASONS
    assert set(downstream._PUBLIC_BLOCK_REASON) | downstream._UNPUBLISHED_BLOCK_REASONS == downstream.BLOCK_REASONS
    assert not set(downstream._PUBLIC_BLOCK_REASON) & downstream._UNPUBLISHED_BLOCK_REASONS
    for r in downstream._UNPUBLISHED_BLOCK_REASONS:
        with pytest.raises(downstream.UnmappedBlockReason):
            downstream.public_blocking([{"sectionId": 1, "stage": "label", "reason": r}])
    with pytest.raises(downstream.UnmappedBlockReason):  # 차단이 생길 수 없는 단계
        downstream.public_blocking([{"sectionId": 1, "stage": "inpaint", "reason": "retry_limit"}])
    # 같은 섹션 · 단계 · 사유는 한 항목, 작업 전체 번역 성공 0 은 섹션별 항목을 합쳐 sectionId = null 하나
    assert downstream.public_blocking([
        {"sectionId": 1, "stage": "label", "reason": "retry_limit"}, {"sectionId": 1, "stage": "logo", "reason": "retry_limit"},
        {"sectionId": 2, "stage": "translate", "reason": "job_translation_zero"},
        {"sectionId": 3, "stage": "translate", "reason": "job_translation_zero"},
    ]) == [{"sectionId": 1, "stage": "label", "reason": "retry_limit_exceeded"},
           {"sectionId": 1, "stage": "logo", "reason": "retry_limit_exceeded"},
           {"sectionId": None, "stage": "translate", "reason": "job_translation_zero"}]


def _client_500(ids):
    """서버 예외를 시험으로 올리지 않고 500 응답으로 받는 클라이언트(같은 로그인)."""
    from fastapi.testclient import TestClient

    from app.main import app

    cl = TestClient(app, raise_server_exceptions=False)
    cl.headers["Authorization"] = ids["c"].headers["Authorization"]
    return cl


def test_resume_with_unreadable_box_records_input_error_then_n5(env, monkeypatch):
    """이전 반복 500 재현 사례(대체 진행 뒤 미리보기 입력을 만들 때 적용 박스를 읽을 수 없는 블록) — 7.2 ② 실패 기록(2e33c33) 뒤:
    /resume 은 202, 그 섹션은 실패 시도로 남고(전달 안 함) 다른 섹션은 진행해 N5. 같은 요청 반복은 INVALID_STATE(이미 처리 중)."""
    monkeypatch.setitem(downstream.MAX_RETRY, "inpaint", 0)  # 인페인트 시간 초과 = 소진 → 대체 진행 → 그 자리에서 미리보기 입력을 만든다
    ids = _stage_timeout(env, {"app.tasks.run_inpaint"})
    job = ids["job"]
    inp = _timed_out(job, "inpaint")
    assert inp
    sid = _q("SELECT unit_id FROM job_async_task WHERE id = :t", t=inp[0])[0]["unit_id"]
    bad = _q("SELECT id FROM text_block WHERE section_id = :s AND trans_1 IS NOT NULL AND is_excluded IS FALSE ORDER BY id LIMIT 1",
             s=sid)[0]["id"]
    _q("UPDATE text_block SET bbox = NULL WHERE id = :b RETURNING id", b=bad)
    assert _tasks(ids)["jobRetry"] == {"available": True, "blocking": []}  # 조회와 POST 가 이제 어긋나지 않는다
    cl = _client_500(ids)
    r = cl.post(f"/v1/jobs/{job}/resume")
    assert r.status_code == 202, r.text
    rec = _q("SELECT id, status, error_code, is_current FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND unit_id = :s",
             j=job, s=sid)
    assert len(rec) == 1 and (rec[0]["status"], rec[0]["error_code"], rec[0]["is_current"]) == ("failed", "RENDER_INPUT_INVALID", True)
    assert rec[0]["id"] not in {m[1][0] for m in env["q"].messages if m[0] == "app.tasks.run_preview"}
    assert sid in r.json()["fallbackSectionIds"] and rec[0]["id"] not in r.json()["retriedTaskIds"]
    assert r.json()["retriedTaskIds"] == []  # 대체 진행 뒤 새로 생긴 미리보기 · 입력 오류 기록은 재시도 목록에 넣지 않는다
    d = cl.post(f"/v1/jobs/{job}/resume")  # 같은 요청 반복 — 이미 처리 중
    assert d.status_code == 409 and d.json()["error"]["code"] == "INVALID_STATE"
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")  # 다른 미리보기 실패와 같은 규칙(D9-3)
    assert len(_q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND unit_id = :s", j=job, s=sid)) == 1


def _concurrent_resume(ids, dburl, monkeypatch):
    """독립 DB 세션 두 개(요청 두 개)가 같은 작업의 /resume 을 동시에 보낸다. 작업 행을 다른 연결이 먼저 잡아 두 요청이 모두
    잠금에서 기다리게 한 뒤 놓는다. 반환: (요청별 (상태 코드, error.code), 잠금을 얻은 뒤 판정 때 본 state 목록(판정 순서))."""
    import threading
    import time

    from sqlalchemy import create_engine
    from sqlalchemy import text as sql

    seen: list = []
    real = downstream.job_retry_plan

    def recording(db, job_id, **kw):  # 잠금을 얻은 뒤 판정할 때 본 상태를 남긴다
        p = real(db, job_id, **kw)
        seen.append(p["state"])  # 요청 처리 스레드는 시험 스레드와 달라 순서로 본다(잠금 아래 판정은 한 번에 하나)
        return p

    monkeypatch.setattr(downstream, "job_retry_plan", recording)
    out: dict = {}

    def call(name):
        cl = _client_500(ids)
        r = cl.post(f"/v1/jobs/{ids['job']}/resume")
        out[name] = (r.status_code, r.json().get("error", {}).get("code") if r.status_code >= 400 else None)

    eng = create_engine(dburl)
    try:
        with eng.connect() as holder:
            holder.execute(sql("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": ids["job"]})
            ts = [threading.Thread(target=call, args=(n,), name=n) for n in ("a", "b")]
            [t.start() for t in ts]
            waiting = 0
            for _ in range(200):
                with eng.connect() as probe:
                    waiting = probe.execute(sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                                "AND datname = current_database()")).scalar()
                if waiting >= 2:
                    break
                time.sleep(0.05)
            assert waiting >= 2, waiting  # 두 요청 모두 작업 행 잠금에서 기다림
            assert seen == []  # 잠금을 얻기 전에는 판정하지 않는다
            holder.rollback()
            [t.join(timeout=60) for t in ts]
    finally:
        eng.dispose()
    return out, seen


def test_resume_concurrent_requests_make_one_set_and_second_rejudges_after_lock(env, dburl, monkeypatch):
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    out, seen = _concurrent_resume(ids, dburl, monkeypatch)
    assert sorted(out.values(), key=str) == sorted([(202, None), (409, "INVALID_STATE")], key=str), out
    assert seen == ["ok", "invalid_state"]  # 뒤 요청은 앞 요청 커밋 뒤 processing 을 보고 다시 판정
    trs = [a for a in _attempts(job) if a["stage"] == "translate"]
    assert len(trs) == 2 * len(tr)  # 옛 시도 + 새 시도 한 벌
    assert _state(ids) == ("processing", "N4", "translating")
    env["q"].drain()
    assert _state(ids) == ("review", "N5", "reviewing")


def test_resume_concurrent_when_first_goes_straight_to_n5(env, dburl, monkeypatch):
    monkeypatch.setitem(downstream.MAX_RETRY, "preview", 0)  # 모두 대체 진행 → 앞 요청이 바로 N5
    ids = _stage_timeout(env, {"app.tasks.run_preview"})
    job = ids["job"]
    before = _attempts(job)
    out, seen = _concurrent_resume(ids, dburl, monkeypatch)
    assert sorted(out.values(), key=str) == sorted([(202, None), (409, "INVALID_STATE")], key=str), out
    assert seen == ["ok", "invalid_state"]  # 뒤 요청은 review · N5 를 보고 거절
    assert _attempts(job) == before  # 새 시도 없음(대체 진행만)
    assert _state(ids) == ("review", "N5", "reviewing") and env["q"].messages == []


def test_resume_revalidates_after_state_changed_since_job05(env):
    """JOB-05 가 available=true 였어도 그 뒤 상태가 바뀌면 POST 가 잠근 뒤 다시 판정한다 — 성공 보장 아님."""
    ids, tr, inp = _inpaint_timeout(env)
    job = ids["job"]
    assert _tasks(ids)["jobRetry"]["available"] is True
    assert ids["c"].post(f"/v1/jobs/{job}/abort").status_code == 200  # 조회 뒤 [중단] → N3
    before = _attempts(job)
    r = _resume(ids)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert _attempts(job) == before and _state(ids)[:2] == ("review", "N3")
    assert "jobRetry" not in _tasks(ids)  # 차단 · 판정 정보는 N4 응답 시점 값 — N3 복귀 뒤에는 다시 조회되지 않는다


def test_openapi_resume_contract_matches_server_values():
    """정본(docs/openapi.yaml)의 JOB-08 202 필드 · 차단 항목 enum 과 서버가 내는 값이 같다(한쪽만 바뀌는 것을 막는다)."""
    import pathlib

    import yaml

    spec = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "docs" / "openapi.yaml").read_text(encoding="utf-8"))
    sch = spec["components"]["schemas"]
    acc = sch["JobResumeAccepted"]
    assert set(acc["required"]) == set(acc["properties"]) == {"jobId", "status", "currentStep", "retriedTaskIds", "fallbackSectionIds"}
    item = sch["JobRetryBlockingItem"]["properties"]
    assert set(sch["JobRetryBlockingItem"]["required"]) == set(item) == {"sectionId", "stage", "reason"}
    assert set(item["stage"]["enum"]) == downstream._PUBLIC_BLOCK_STAGES
    assert set(item["reason"]["enum"]) == set(downstream._PUBLIC_BLOCK_REASON.values())
    assert "null" in item["sectionId"]["type"]
    assert set(sch["JobRetry"]["required"]) == set(sch["JobRetry"]["properties"]) == {"available", "blocking"}
    assert sch["JobRetry"]["properties"]["blocking"]["items"]["$ref"].endswith("/JobRetryBlockingItem")
    status_props = [s["properties"]["jobRetry"] for s in sch.values() if isinstance(s, dict) and "jobRetry" in s.get("properties", {})]
    assert len(status_props) == 1 and status_props[0]["allOf"][0]["$ref"].endswith("/JobRetry")
    assert "jobRetry" not in [r for s in sch.values() if isinstance(s, dict) for r in s.get("required", [])]  # 선택 속성(생략 규칙)
    op = spec["paths"]["/jobs/{jobId}/resume"]["post"]
    assert op["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("/JobResumeAccepted")
