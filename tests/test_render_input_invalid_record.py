"""렌더 입력을 만들 수 없는 섹션(적용 박스를 읽을 수 없음 — RenderInputInvalid)의 실패 기록(README 7.2 ② · reviewPreview 「구현 상태」 ②).

이전: 미리보기 시도를 만들 때 예외가 그대로 올라가 N4 작업 단위 재시도 · 블록 저장 · 복원이 500 으로 끝나고(롤백) 같은 요청을 반복해도 같았다.
이제: 그 섹션은 입력 오류 상태를 적은 실패 시도(errorCode RENDER_INPUT_INVALID)로 남기고 큐로 보내지 않는다. 정상 입력 · 지문을 지어내지 않는다.
상태 전이는 다른 미리보기 실패와 같다(렌더 실패는 원본 표시로 진행 — D9-3 · test_exec_downstream.test_unreadable_style_defaults_render_fails_n5_opens_save_blocked).
같은 오류 상태로 반복해도 새 시도 · 전달 · 재시도 횟수 소모가 없고, 입력을 고치면 정상 미리보기로 돌아온다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app import execution
from app.flows import downstream
from tests.exec_db import requires_db
from tests.test_job_state_timers import _q, _state, _to_n5, dburl, env  # noqa: F401 — fixture 재사용
from tests.test_n4_job_recovery import _attempts, _job_row, _retry_job, _session, _stage_timeout, _timed_out
from tests.test_timeout_followups import _sections

pytestmark = requires_db


def _previews(job, sid=None):
    sql = ("SELECT id, unit_id, status, error_code, is_current, retry_count, target_count, supersedes_task_id, input_manifest "
           "FROM job_async_task WHERE job_id = :j AND stage = 'preview'")
    if sid is not None:
        return _q(sql + " AND unit_id = :s ORDER BY id", j=job, s=sid)
    return _q(sql + " ORDER BY id", j=job)


def _preview_msgs(env):
    return [m for m in env["q"].messages if m[0] == "app.tasks.run_preview"]


def _break_box(sid):
    """그 섹션의 그릴 블록 하나의 bbox 를 읽을 수 없게(NULL) 만든다. (블록 id, 원래 bbox)"""
    r = _q("SELECT id, bbox FROM text_block WHERE section_id = :s AND trans_1 IS NOT NULL AND is_excluded IS FALSE "
           "AND coalesce(trim(source_ko), '') <> '' ORDER BY id LIMIT 1", s=sid)[0]
    _q("UPDATE text_block SET bbox = NULL WHERE id = :b RETURNING id", b=r["id"])
    return r["id"], r["bbox"]


def _progress(job):
    """진행 집계를 한 번 부른다(일반 진행 경로 — 채택 뒤 핸들러가 부르는 것과 같은 함수). 새로 만든 · 전달할 시도 id."""
    db = _session()
    try:
        run = execution.active_run(db, job, "downstream")
        execution.lock_job(db, job)
        ids = downstream.progress(db, run["id"])
        db.commit()
    finally:
        db.close()
    execution.dispatch(ids)
    return ids


def _inpaint_exhausted_with_bad_box(env, monkeypatch):
    """N4: 인페인트 시간 초과(한도 0 = 소진 → 대체 진행) 섹션 하나의 블록 bbox 를 NULL 로. (ids, 나쁜 섹션, 나쁜 블록, 원래 bbox, 다른 섹션들)"""
    monkeypatch.setitem(downstream.MAX_RETRY, "inpaint", 0)
    ids = _stage_timeout(env, {"app.tasks.run_inpaint"})
    job = ids["job"]
    units = sorted({r["unit_id"] for r in _q("SELECT unit_id FROM job_async_task WHERE id = ANY(:i)", i=_timed_out(job, "inpaint"))})
    assert len(units) >= 2
    bad = units[0]
    blk, box = _break_box(bad)
    return ids, bad, blk, box, units[1:]


def test_job_retry_records_input_error_instead_of_500_and_other_sections_proceed(env, monkeypatch):
    """이전 반복 500 재현 사례: 이제 [다시 시도]가 성공하고, 나쁜 섹션은 실패 기록(전달 안 함), 다른 섹션은 정상 미리보기로 진행."""
    ids, bad, blk, box, others = _inpaint_exhausted_with_bad_box(env, monkeypatch)
    job = ids["job"]
    r = _retry_job(job)  # 이전: RenderInputInvalid → 롤백 → 500
    pv_bad = _previews(job, bad)
    assert len(pv_bad) == 1
    f = pv_bad[0]
    assert (f["status"], f["error_code"], f["is_current"], f["retry_count"], f["target_count"]) == (
        "failed", "RENDER_INPUT_INVALID", True, 0, 0)
    assert f["input_manifest"]["input_error"] == {"code": "RENDER_INPUT_INVALID", "block_ids": [blk]}
    assert not {"blocks", "background", "renderer"} & set(f["input_manifest"])  # 렌더 입력 · 배경 · 렌더러를 지어내지 않는다
    # 실패 기록은 전달 목록(createdTaskIds = 커밋 뒤 큐로 보내는 후속 시도)에 들어가지 않는다 — 렌더 실행 대상이 아니다
    assert f["id"] not in r["createdTaskIds"] and f["id"] not in r["retriedTaskIds"]
    sent = {m[1][0] for m in _preview_msgs(env)}
    assert sent == {p["id"] for s in others for p in _previews(job, s)}  # 큐로 간 미리보기 = 다른 섹션의 새 시도뿐
    for s in others:  # 다른 섹션은 막히지 않고 정상 미리보기 시도가 만들어져 전달됐다
        pv = _previews(job, s)
        assert len(pv) == 1 and pv[0]["status"] == "pending" and pv[0]["id"] in sent
    assert _state(ids) == ("processing", "N4", "translating")
    env["q"].drain()
    # 다른 미리보기 실패와 같은 규칙(렌더 실패는 원본 표시로 진행 — D9-3): 다른 섹션이 끝나면 N5
    assert _state(ids) == ("review", "N5", "reviewing")
    items = [i for i in ids["c"].get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskId"] == f["id"]]
    assert items and items[0]["status"] == "failed" and items[0]["errorCode"] == "RENDER_INPUT_INVALID" and items[0]["retryable"] is False
    sec = [s for s in ids["c"].get(f"/v1/jobs/{job}/preview").json()["sections"] if s["id"] == bad][0]
    assert sec["inputError"] == {"code": "RENDER_INPUT_INVALID", "blockIds": [blk]}


def test_input_error_repeated_progress_and_user_retry_create_nothing(env, monkeypatch):
    ids, bad, blk, box, others = _inpaint_exhausted_with_bad_box(env, monkeypatch)
    job = ids["job"]
    _retry_job(job)
    env["q"].drain()
    assert _state(ids)[:2] == ("review", "N5")
    before = _attempts(job)
    for _ in range(3):  # 같은 입력 오류 상태로 진행 집계 반복
        assert _progress(job) == []
    assert _attempts(job) == before and _preview_msgs(env) == []
    f = [p for p in _previews(job, bad) if p["is_current"]][0]
    r = ids["c"].post(f"/v1/jobs/{job}/tasks/{f['id']}/retry")  # 같은 입력으로는 다시 할 수 없다
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert _attempts(job) == before  # 재시도 횟수 · 시도 그대로


def test_input_fixed_recovers_to_normal_preview_without_using_retry(env, monkeypatch):
    ids, bad, blk, box, others = _inpaint_exhausted_with_bad_box(env, monkeypatch)
    job = ids["job"]
    _retry_job(job)
    env["q"].drain()
    f = [p for p in _previews(job, bad) if p["is_current"]][0]
    _q("UPDATE text_block SET bbox = CAST(:b AS jsonb) WHERE id = :i RETURNING id", b=__import__("json").dumps(box), i=blk)  # 입력을 고침
    new = _progress(job)
    assert len(new) == 1
    n = [p for p in _previews(job, bad) if p["is_current"]][0]
    assert n["id"] == new[0] and n["supersedes_task_id"] == f["id"] and n["status"] == "pending"
    assert n["retry_count"] == f["retry_count"]  # 입력 변경 재계산 — 재시도 한도를 쓰지 않는다
    assert "input_error" not in n["input_manifest"] and n["target_count"] > 0
    env["q"].drain()
    assert [p for p in _previews(job, bad) if p["is_current"]][0]["status"] == "done"
    sec = [s for s in ids["c"].get(f"/v1/jobs/{job}/preview").json()["sections"] if s["id"] == bad][0]
    assert sec["inputError"] is None


def test_block_save_and_restore_record_failure_instead_of_500(env):
    """같은 공통 함수를 쓰는 저장(블록 수정 뒤 재렌더) · 복원 경로: 저장은 성공하고 실패는 시도로 남는다(전달 안 함). 반복해도 한 건."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    sid = _q("SELECT DISTINCT tb.section_id AS s FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
             "AND tb.trans_1 IS NOT NULL ORDER BY 1 LIMIT 1", j=job)[0]["s"]
    rows = _q("SELECT id FROM text_block WHERE section_id = :s AND trans_1 IS NOT NULL AND is_excluded IS FALSE "
              "AND coalesce(trim(source_ko), '') <> '' ORDER BY id", s=sid)
    assert len(rows) >= 2
    bad_blk, _box = _break_box(sid)
    edit = [r["id"] for r in rows if r["id"] != bad_blk][0]
    env["q"].messages = []
    for i in range(2):  # 다른 블록 번역문 저장 두 번(그 블록 revision 이 바뀌어도 나쁜 블록의 오류 상태는 같다)
        blk = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == edit][0]
        r = c.patch(f"/v1/jobs/{job}/blocks/{edit}", json={"trans1": f"Edited {i}", "revision": blk["revision"]})
        assert r.status_code == 200, r.text  # 이전: 500
        assert r.json()["rerenderTaskId"] is None  # 전달할 재렌더 시도가 없다(실패는 시도로 기록)
    cur = [p for p in _previews(job, sid) if p["is_current"]]
    assert len(cur) == 1 and cur[0]["status"] == "failed" and cur[0]["error_code"] == "RENDER_INPUT_INVALID"
    assert len([p for p in _previews(job, sid) if p["error_code"] == "RENDER_INPUT_INVALID"]) == 1  # 반복 저장에도 한 건
    assert _preview_msgs(env) == []
    db = _session()
    try:  # 복원 경로(N5 섹션 되살림이 부르는 함수) — 같은 오류 상태라 새로 만들지 않고 전달할 것도 없다
        assert downstream.ensure_preview_for_restore(db, job, sid) is None
        db.commit()
    finally:
        db.close()
    assert len([p for p in _previews(job, sid) if p["error_code"] == "RENDER_INPUT_INVALID"]) == 1


def test_done_preview_becomes_input_error_once_and_bad_block_resave_adds_nothing(env):
    """완료된 미리보기 뒤 입력이 읽을 수 없게 바뀌면(진행 집계의 입력 변경 재계산) 실패 기록 하나로 대체.
    나쁜 블록 자체의 번역문을 다시 저장해도(revision 이 올라가도) 오류 상태는 같아 새 시도 · 전달이 없다."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    sid = _q("SELECT DISTINCT tb.section_id AS s FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
             "AND tb.trans_1 IS NOT NULL ORDER BY 1 LIMIT 1", j=job)[0]["s"]
    done = [p for p in _previews(job, sid) if p["is_current"]][0]
    assert done["status"] == "done"
    bad_blk, _box = _break_box(sid)
    env["q"].messages = []
    assert _progress(job) == []  # 전달할 것 없음
    cur = [p for p in _previews(job, sid) if p["is_current"]]
    assert len(cur) == 1 and cur[0]["status"] == "failed" and cur[0]["supersedes_task_id"] == done["id"]
    assert cur[0]["retry_count"] == done["retry_count"]
    for i in range(2):
        blk = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == bad_blk][0]
        r = c.patch(f"/v1/jobs/{job}/blocks/{bad_blk}", json={"trans1": f"Bad block edited {i}", "revision": blk["revision"]})
        assert r.status_code == 200, r.text
        assert r.json()["rerenderTaskId"] is None
    assert [p["id"] for p in _previews(job, sid) if p["error_code"] == "RENDER_INPUT_INVALID"] == [cur[0]["id"]]
    assert _preview_msgs(env) == []


def test_input_error_fixed_through_layout_adjust_api_recovers(env):
    """실패 기록 상태에서 셀러가 배치 조정(API)으로 그 블록의 적용 박스를 정하면 정상 재렌더 시도가 만들어져 전달 · 완료된다."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    sid = _q("SELECT DISTINCT tb.section_id AS s FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
             "AND tb.trans_1 IS NOT NULL ORDER BY 1 LIMIT 1", j=job)[0]["s"]
    bad_blk, box = _break_box(sid)
    _progress(job)
    failed = [p for p in _previews(job, sid) if p["is_current"]][0]
    assert failed["error_code"] == "RENDER_INPUT_INVALID"
    blk = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == bad_blk][0]
    env["q"].messages = []
    r = c.patch(f"/v1/jobs/{job}/blocks/{bad_blk}", json={
        "trans1": blk["trans1"], "revision": blk["revision"],
        "layoutAdjust": {"box": {k: box[k] for k in ("x", "y", "w", "h")}, "fontScale": 1.0}})
    assert r.status_code == 200, r.text
    tid = r.json()["rerenderTaskId"]
    assert tid is not None and [m[1][0] for m in _preview_msgs(env)] == [tid]
    n = [p for p in _previews(job, sid) if p["is_current"]][0]
    assert n["id"] == tid and n["supersedes_task_id"] == failed["id"] and "input_error" not in n["input_manifest"]
    assert n["retry_count"] == failed["retry_count"]
    env["q"].drain()
    assert [p for p in _previews(job, sid) if p["is_current"]][0]["status"] == "done"


def test_unexpected_error_in_preview_inputs_keeps_atomic_rollback(env, monkeypatch):
    """예상한 입력 오류만 기록한다. 그 밖의 장애는 잡지 않고 기존처럼 전체 롤백 · 전달 없음."""
    ids, bad, blk, box, others = _inpaint_exhausted_with_bad_box(env, monkeypatch)
    job = ids["job"]
    _q("UPDATE text_block SET bbox = CAST(:b AS jsonb) WHERE id = :i RETURNING id", b=__import__("json").dumps(box), i=blk)

    def boom(db, run_id, sid, **kw):
        raise RuntimeError("예상하지 못한 장애")

    monkeypatch.setattr(downstream, "_render_inputs", boom)
    before = (_attempts(job), _job_row(job), _sections(job))
    with pytest.raises(RuntimeError):
        _retry_job(job)
    assert (_attempts(job), _job_row(job), _sections(job)) == before and env["q"].messages == []


def test_input_error_record_is_not_user_retryable():
    """실패 기록 시도의 재시도 차단 사유(JOB-05 retryable=false · JOB-06 409 의 근거)."""
    att = {"stage": "preview", "input_manifest": {"stage": "preview", "section_id": 1,
                                                  "input_error": {"code": "RENDER_INPUT_INVALID", "block_ids": []}}}
    assert "RENDER_INPUT_INVALID" in execution.retry_blocked(att)
