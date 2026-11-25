"""자동 취소 · 처리 시간 상한의 경합 결함 AC-01~03(독립 검토 2026-11-25) — 실제 PostgreSQL + 메모리 저장소.

- AC-01: 시간 초과로 닫은 옛 시도는 다른 시도 재시도로 작업이 다시 처리 중이 돼도 등록 · 권한 인수 · 채택 모두에서 제외된다.
- AC-02: 작업을 바꾸는 요청의 변경과 job.last_activity_at 이 같은 트랜잭션으로 커밋된다(app.db._touch_activity).
- AC-03: 내보내기는 작업 행을 잠근 채 산출물을 만든다 — 취소와 어느 순서로 만나도 취소 뒤 산출물이 남지 않는다.

「DB 경합」 표시 시험은 실제 행 잠금 대기를 스레드로 만든다(대기 순서는 시작 간격으로 정한다). 나머지는 같은 DB에서 순서를 고정한다.
모델 · GPU · S3 · FE 렌더는 모두 대역(tests/exec_fakes · conftest)이다. 시각은 DB 열을 과거로 돌려 흉내 낸다.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import execution, recovery
from tests.test_job_state_timers import (_age_activity, _age_state, _new_job, _q, _state, _to_n3, _to_n5,  # noqa: F401
                                         dburl, env)


def _client_like(ids):
    """스레드마다 따로 쓰는 클라이언트(같은 로그인 토큰)."""
    from app.main import app

    c = TestClient(app)
    c.headers["Authorization"] = ids["c"].headers["Authorization"]
    return c


def _wait_blocked(dburl, pid_query_hint: str, timeout=5.0) -> bool:
    """다른 연결이 행 잠금을 기다리고 있는지(pg_stat_activity wait_event_type = 'Lock')."""
    eng = create_engine(dburl)
    try:
        end = time.time() + timeout
        while time.time() < end:
            with eng.connect() as c:
                n = c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query ILIKE :q"),
                              {"q": f"%{pid_query_hint}%"}).scalar()
            if n:
                return True
            time.sleep(0.05)
        return False
    finally:
        eng.dispose()


# ---- AC-01 -----------------------------------------------------------------------------------------------------
def test_ac01_timed_out_inpaint_late_success_not_adopted_after_other_retry(env):
    """N4: 인페인트(원격) · 번역이 끝나지 않은 채 30분 → 오류. 셀러가 번역 시도를 [다시 시도] → 작업이 다시 처리 중(N4).
    그 뒤 옛 인페인트 시도의 원격 성공 결과가 늦게 등록돼도 거절(processing_timeout)되고 채택 · 후속 · 단계 전이가 없다."""
    from app.flows import gpu_worker

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint", "app.tasks.run_translate"})  # GPU · 번역 메시지를 처리하지 않는다
    inp = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current AND status = 'pending' ORDER BY id",
             j=job)
    tr = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current AND status = 'pending' ORDER BY id",
            j=job)
    assert inp and tr
    late_tid = inp[0]["id"]
    late_unit = _q("SELECT unit_id FROM job_async_task WHERE id = :t", t=late_tid)[0]["unit_id"]
    sec_before = _q("SELECT inpaint_status, inpaint_image_url FROM section WHERE id = :s", s=late_unit)[0]
    seen: dict = {}

    class LateClient(gpu_worker.InProcessControlClient):
        def register_report(self, attempt_id, epoch, token, envelope):
            # 원격 워커가 계산 · 업로드를 마친 뒤 등록하기 직전에 시간 상한이 지나고, 셀러가 번역을 다시 시도했다
            _age_state(job, 1801)
            seen["timed_out"] = recovery.sweep()["timed_out"]
            seen["after_timeout"] = _state(ids)
            r = c.post(f"/v1/jobs/{job}/tasks/{tr[0]['id']}/retry")
            seen["retry"] = r.status_code
            seen["after_retry"] = _state(ids)
            return super().register_report(attempt_id, epoch, token, envelope)

    gpu_worker.set_client_factory(lambda: LateClient("gpu-late"))
    try:
        out = gpu_worker.execute_inpaint_remote(late_tid)
    finally:
        gpu_worker.set_client_factory(None)
    assert seen["timed_out"] == 1 and seen["after_timeout"] == ("failed", "N4", "failed")
    assert seen["retry"] == 200 and seen["after_retry"] == ("processing", "N4", "translating")
    assert out["ran"] is True
    env["q"].drain()  # 번역 재시도 · (있다면) 채택 메시지
    recovery.sweep()  # 인계 재전달(_stuck_handoffs) · 집계 복구도 다시 채택하지 않는다
    env["q"].drain()
    hs = _q("SELECT state, reject_reason, late FROM task_handoff WHERE task_id = :t", t=late_tid)
    assert hs and all(h["state"] == "rejected" and h["reject_reason"] == execution.TIMED_OUT_REJECT for h in hs), hs
    att = _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=late_tid)[0]
    assert att == {"status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    assert _q("SELECT count(*) AS n FROM task_artifact a JOIN task_handoff h ON h.id = a.handoff_id WHERE h.task_id = :t "
              "AND a.state = 'adopted'", t=late_tid)[0]["n"] == 0
    # 늦은 결과가 섹션 배경 · 미리보기 입력에 쓰이지 않고, 인페인트 재시도(대체 시도) · N5 전이도 만들지 않는다.
    # (번역 재시도가 끝나 만든 미리보기는 기존 규칙 — 인페인트가 끝나지 않은 섹션은 원본 배경 — 이며 늦은 결과와 무관하다)
    sec = _q("SELECT inpaint_status, inpaint_image_url FROM section WHERE id = :s", s=late_unit)[0]
    assert sec == sec_before
    late_keys = [r["staging_key"] for r in _q("SELECT a.staging_key FROM task_artifact a JOIN task_handoff h ON h.id = a.handoff_id "
                                              "WHERE h.task_id = :t", t=late_tid) if r["staging_key"]]
    assert late_keys  # 늦은 결과는 실제로 업로드 · 등록됐다
    for p in _q("SELECT input_manifest::text AS m FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=job):
        assert not any(k in (p["m"] or "") for k in late_keys)
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'inpaint'", j=job)[0]["n"] == len(inp)
    assert _state(ids)[1] == "N4"
    # 권한 인수를 다시 시도해도(재전달 · 반복 통지) 거절 상태 그대로 — 멱등
    for h in _q("SELECT id FROM task_handoff WHERE task_id = :t", t=late_tid):
        assert execution.claim_adoption(h["id"], "be-adopter-x") is None
    assert all(h["state"] == "rejected" for h in _q("SELECT state FROM task_handoff WHERE task_id = :t", t=late_tid))


def test_ac01_claimed_then_timed_out_then_adopt_is_rejected(env):
    """순서 반대: 늦은 인계를 BE 채택자가 먼저 인수(claim) → 채택 전에 시간 상한 → adopt 는 거절로 끝나고 다시 인수되지 않는다."""
    from app.flows import gpu_worker

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    tid = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current AND status = 'pending' ORDER BY id",
             j=job)[0]["id"]
    env["q"].drop = True  # 채택 메시지를 보내지 않고 직접 부른다
    try:
        gpu_worker.execute_inpaint_remote(tid)
    finally:
        env["q"].drop = False
    h = _q("SELECT id, state FROM task_handoff WHERE task_id = :t", t=tid)[0]
    assert h["state"] == "received"
    lease = execution.claim_adoption(h["id"], "be-adopter-1")
    assert lease is not None
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert execution.adopt(h["id"], lease) is None
    hh = _q("SELECT state, reject_reason FROM task_handoff WHERE id = :h", h=h["id"])[0]
    assert hh == {"state": "rejected", "reject_reason": execution.TIMED_OUT_REJECT}
    assert execution.claim_adoption(h["id"], "be-adopter-2") is None
    assert _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=tid)[0] == {
        "status": "failed", "error_code": "PROCESSING_TIMEOUT"}
    assert _state(ids) == ("failed", "N4", "failed")


def test_ac01_lease_expiry_recovery_still_adopts(env):
    """정상 권한 만료 복구(5.32 · claim_adoption 의 failed 분기)는 그대로: 권한 만료로 재시도 한도까지 실패한 현재 시도의 늦은 인계는
    다른 섹션이 아직 처리 중(processing · N4)이면 저장 복구로 채택한다. 같은 상황에서 시간 초과만 거절한다(위 시험)."""
    from app.flows.gpu_worker import InProcessControlClient

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    first = _q("SELECT id, unit_id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND is_current AND status = 'pending' "
               "ORDER BY id", j=job)
    assert len(first) >= 2  # 한 섹션만 만료시키고 다른 섹션은 대기로 둔다(작업이 처리 중으로 남게)
    unit = first[0]["unit_id"]
    cl = InProcessControlClient("gpu-slow")
    leases = {}
    for _round in range(5):  # 첫 시도 + 복구 재시도 — 한도까지 모두 계산 중 권한 만료
        rows = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' AND unit_id = :u AND is_current "
                  "AND status = 'pending'", j=job, u=unit)
        if not rows:
            break
        for r in rows:
            leases[r["id"]] = cl.acquire(r["id"])
            _q("UPDATE job_async_task SET lease_expires_at = now() - interval '1 second' WHERE id = :t", t=r["id"])
        recovery.sweep()
        env["q"].messages = [m for m in env["q"].messages if m[0] != "app.tasks.run_inpaint"]
        env["q"].drain()
    last = max(leases)
    att = _q("SELECT status, error_code, is_current FROM job_async_task WHERE id = :t", t=last)[0]
    assert att["status"] == "failed" and att["is_current"] and att["error_code"] != "PROCESSING_TIMEOUT", att
    assert _state(ids)[:2] == ("processing", "N4")
    got = leases[last]
    body = {"outcome": "failed", "target_count": 1, "input_fingerprint": got["fingerprint"],
            "payload": {"error_code": "INPAINT_FAILED", "message": "late", "retryable": False}, "artifacts": []}
    reg = cl.register(last, got["epoch"], got["token"], body)
    assert reg["late"] is True and reg["state"] == "received"
    env["q"].drain()
    h = _q("SELECT state, adopted_by FROM task_handoff WHERE task_id = :t", t=last)[0]
    assert h["state"] == "adopted" and h["adopted_by"]


# ---- AC-02 -----------------------------------------------------------------------------------------------------
def test_ac02_sweep_right_after_commit_sees_fresh_activity(env, dburl):
    """검토 시나리오 그대로: 24시간이 지난 N1 → PATCH 커밋 직후(응답 전) 자동 취소 점검이 돈다 → 활동 시각이 같은 커밋에 있어 취소하지 않는다."""
    ids = _new_job(env)
    c, job = ids["c"], ids["job"]
    _age_activity(job, 25)
    probe = create_engine(dburl)
    fired: dict = {}

    def after_commit(session):
        if fired:
            return
        with probe.connect() as cn:
            name = cn.execute(text("SELECT product_name FROM job WHERE id = :j"), {"j": job}).scalar()
        if name == "race-after-commit":  # 이 PATCH 의 커밋 직후
            fired["stats"] = recovery.sweep()

    event.listen(Session, "after_commit", after_commit)
    try:
        r = c.patch(f"/v1/jobs/{job}", json={"productName": "race-after-commit"})
    finally:
        event.remove(Session, "after_commit", after_commit)
        probe.dispose()
    assert fired, "PATCH 커밋 직후 점검이 돌지 않았다"
    assert fired["stats"]["auto_cancelled"] == 0
    assert r.status_code == 200
    assert _state(ids)[0] == "draft"
    assert _q("SELECT product_name FROM job WHERE id = :j", j=job)[0]["product_name"] == "race-after-commit"


def test_ac02_db_race_request_holding_lock_vs_auto_cancel(env, dburl):
    """DB 경합: 24시간이 지난 N1 의 PATCH 가 작업 행 잠금을 기다리는(=곧 잡을) 동안 점검이 돈다 → 점검은 잠긴 작업을 기다리지 않고
    건너뛴다(skipped_locked) → PATCH 200 · 활동 시각 갱신 → 다음 점검도 취소하지 않는다. 「성공 응답 + 취소」 조합이 없다."""
    ids = _new_job(env)
    job = ids["job"]
    _age_activity(job, 25)
    holder = create_engine(dburl)
    hc = holder.connect()
    tx = hc.begin()
    hc.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": job})
    res: dict = {}
    t = threading.Thread(target=lambda: res.update(patch=_client_like(ids).patch(f"/v1/jobs/{job}", json={"productName": "race-db"})))
    try:
        t.start()
        assert _wait_blocked(dburl, "FOR UPDATE"), "잠금 대기를 만들지 못했다"
        t0 = time.time()
        st = recovery.sweep()  # 잠금이 잡혀 있는 동안
        assert time.time() - t0 < 5, "점검이 잠긴 작업을 기다렸다"
        assert st["auto_cancelled"] == 0 and st["skipped_locked"] >= 1
    finally:
        tx.rollback()
        hc.close()
        holder.dispose()
        t.join(20)
    assert res["patch"].status_code == 200, res["patch"].text
    assert recovery.sweep()["auto_cancelled"] == 0 and _state(ids)[0] == "draft"


def test_ac02_sweep_first_then_request_409(env):
    """순서 고정: 점검이 먼저 잠가 취소 · 커밋 → 그 뒤 PATCH 는 409 이고 내용이 생기지 않는다."""
    ids = _new_job(env)
    job = ids["job"]
    _age_activity(job, 25)
    assert recovery.sweep()["auto_cancelled"] == 1
    r = ids["c"].patch(f"/v1/jobs/{job}", json={"productName": "late"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert _q("SELECT product_name FROM job WHERE id = :j", j=job)[0]["product_name"] is None


def test_ac02_sweep_skips_locked_processing_job_then_times_out_next_cycle(env, dburl):
    """시간 상한도 같다: 처리 중 작업 행이 잠겨 있으면 그 주기에는 건너뛰고(기다리지 않음), 풀린 뒤 다음 주기에 오류로 보낸다."""
    ids = _new_job(env)
    job = ids["job"]
    assert ids["c"].post(f"/v1/jobs/{job}/analyze").status_code == 202
    env["q"].messages = []
    _age_state(job, 1801)
    holder = create_engine(dburl)
    hc = holder.connect()
    tx = hc.begin()
    hc.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": job})
    try:
        t0 = time.time()
        st = recovery.sweep()
        assert time.time() - t0 < 5 and st["timed_out"] == 0 and st["skipped_locked"] >= 1
    finally:
        tx.rollback()
        hc.close()
        holder.dispose()
    assert recovery.sweep()["timed_out"] == 1
    assert _state(ids) == ("failed", "N2", "failed")


def test_ac02_touch_refuses_commit_on_cancelled_job(env):
    """활동 기록이 붙은 세션이 커밋하려는 때 작업이 이미 취소돼 있으면 커밋하지 않는다(변경 없음 · 409 로 바뀌는 예외)."""
    from app import db as app_db

    ids = _to_n3(env)
    job = ids["job"]
    assert ids["c"].delete(f"/v1/jobs/{job}").status_code == 200
    s = app_db.SessionLocal()
    try:
        s.info[app_db._ACTIVITY_KEY] = job
        s.execute(text("UPDATE job SET product_name = 'zombie' WHERE id = :j"), {"j": job})
        with pytest.raises(app_db.ActivityOnCancelledJob):
            s.commit()
        s.rollback()
    finally:
        s.close()
    assert _q("SELECT product_name FROM job WHERE id = :j", j=job)[0]["product_name"] is None


def test_ac02_activity_endpoint_matrix(env, monkeypatch):
    """세는 것: 변경 POST · PATCH · DELETE(원본 삭제)의 성공. 세지 않는 것: GET · 커밋 전 4xx · 롤백(revision 충돌)."""
    from app import s3
    from tests.exec_fakes import sample_source_png

    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: env["store"].put(key, f.read()) or key)
    monkeypatch.setattr(s3, "delete_object", lambda key: env["store"].objects.pop(key, None) and None)

    def old(job):
        return _q("SELECT last_activity_at < now() - interval '24 hours' AS old FROM job WHERE id = :j", j=job)[0]["old"]

    ids = _new_job(env)
    c, job = ids["c"], ids["job"]
    up = c.post(f"/v1/jobs/{job}/source-images", files={"files": ("b.png", sample_source_png(), "image/png")})
    assert up.status_code == 200
    img = up.json()["results"][0]["sourceImageId"]
    _age_activity(job, 25)
    assert c.get(f"/v1/jobs/{job}/source-images").status_code == 200 and old(job)
    assert c.delete(f"/v1/jobs/{job}/source-images/{img}").status_code == 204
    assert not old(job)  # 원본 삭제(DELETE)도 작업을 바꾸는 요청
    _age_activity(job, 25)
    assert c.post(f"/v1/jobs/{job}/source-images", files={"files": ("c.png", sample_source_png(), "image/png")}).status_code == 200
    assert not old(job)

    ids5 = _to_n5(env)
    c5, j5 = ids5["c"], ids5["job"]
    blk = _q("SELECT tb.id, tb.revision FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
             "AND tb.trans_1 IS NOT NULL ORDER BY tb.id LIMIT 1", j=j5)[0]
    _age_activity(j5, 25)
    r = c5.patch(f"/v1/jobs/{j5}/blocks/{blk['id']}", json={"trans1": "x", "revision": blk["revision"] + 99})
    assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    assert old(j5)  # 롤백한 요청은 세지 않는다
    r = c5.patch(f"/v1/jobs/{j5}/blocks/{blk['id']}", json={"trans1": "edited", "revision": blk["revision"]})
    assert r.status_code == 200, r.text
    assert not old(j5)


# ---- AC-03 -----------------------------------------------------------------------------------------------------
def _to_done(env):
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert c.post(f"/v1/jobs/{job}/save").status_code == 200
    return ids


def _no_export_left(env, job):
    assert _q("SELECT count(*) AS n FROM export_artifact WHERE job_id = :j", j=job)[0]["n"] == 0
    assert not [k for k in env["store"].objects if k.startswith(f"export/{job}/")]


def test_ac03_cancel_first_then_export_409_and_nothing_created(env, monkeypatch):
    from app import s3

    ids = _to_done(env)
    c, job = ids["c"], ids["job"]
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: env["store"].put(key, f.read()) or key)
    assert c.delete(f"/v1/jobs/{job}").status_code == 200
    r = c.post(f"/v1/jobs/{job}/export", json={"components": ["content.csv", "images"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    _no_export_left(env, job)


def test_ac03_db_race_export_in_progress_then_cancel(env, monkeypatch, dburl):
    """DB 경합: 내보내기가 S3 업로드 중(작업 행 잠금 보유)일 때 취소가 들어온다 → 취소는 내보내기 커밋까지 기다렸다가 산출물 행 · 파일까지
    지운다. 그동안 다른 작업의 복구 점검(시간 상한)은 기다리지 않는다."""
    from app import s3

    ids = _to_done(env)
    job = ids["job"]
    other = _new_job(env)  # 다른 작업 — 처리 중으로 30분 지남
    assert other["c"].post(f"/v1/jobs/{other['job']}/analyze").status_code == 202
    env["q"].messages = []
    _age_state(other["job"], 1801)

    uploading, go = threading.Event(), threading.Event()

    def slow_upload(f, key, content_type=None):
        data = f.read()
        uploading.set()
        go.wait(20)
        env["store"].put(key, data)
        return key

    monkeypatch.setattr(s3, "upload_fileobj", slow_upload)
    res: dict = {}
    te = threading.Thread(target=lambda: res.update(export=_client_like(ids).post(
        f"/v1/jobs/{job}/export", json={"components": ["content.csv"]})))
    tc = threading.Thread(target=lambda: res.update(cancel=_client_like(ids).delete(f"/v1/jobs/{job}")))
    te.start()
    try:
        assert uploading.wait(20)
        tc.start()
        assert _wait_blocked(dburl, "FOR UPDATE"), "취소가 내보내기 잠금을 기다리지 않는다"
        t0 = time.time()
        st = recovery.sweep()  # 다른 작업 점검 — 이 내보내기 잠금과 무관
        assert time.time() - t0 < 5 and st["timed_out"] >= 1
        assert tc.is_alive()
    finally:
        go.set()
        te.join(30)
        tc.join(30)
    assert res["export"].status_code == 201, res["export"].text
    assert res["cancel"].status_code == 200 and _state(ids)[0] == "cancelled"
    _no_export_left(env, job)
    art = res["export"].json()["artifactId"]
    assert ids["c"].get(f"/v1/jobs/{job}/exports/{art}/download").status_code in (404, 409)
    assert _state(other) == ("failed", "N2", "failed")


def test_ac03_review_job_export_holding_lock_does_not_stall_sweep(env, monkeypatch, dburl):
    """독립 재검토 1번: 자동 취소 대상(저장 대기 = review · N6, 24시간 지남)인 작업의 내보내기가 작업 행을 잡은 채 업로드 중일 때
    점검이 돈다 → 그 작업은 기다리지 않고 건너뛰고(skipped_locked), 뒤 작업(24시간 지난 N3)은 같은 주기에 취소된다.
    내보내기가 끝나 커밋하면 활동 시각이 새로 찍혀 다음 주기에도 취소되지 않는다."""
    from app import s3

    ids = _to_n5(env)  # 앞 작업(id 가 작다 — 점검이 먼저 본다)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert _state(ids) == ("review", "N6", "awaiting_save")
    behind = _to_n3(env)  # 뒤 작업
    _age_activity(job, 25)
    _age_activity(behind["job"], 25)

    uploading, go = threading.Event(), threading.Event()

    def slow_upload(f, key, content_type=None):
        data = f.read()
        uploading.set()
        go.wait(20)
        env["store"].put(key, data)
        return key

    monkeypatch.setattr(s3, "upload_fileobj", slow_upload)
    res: dict = {}
    te = threading.Thread(target=lambda: res.update(export=_client_like(ids).post(
        f"/v1/jobs/{job}/export", json={"components": ["content.csv"]})))
    te.start()
    try:
        assert uploading.wait(20)
        t0 = time.time()
        st = recovery.sweep()
        took = time.time() - t0
        assert took < 5, f"점검이 내보내기 잠금을 기다렸다({took:.1f}초)"
        assert st["skipped_locked"] >= 1 and st["auto_cancelled"] == 1
        assert _state(behind)[0] == "cancelled" and _state(ids)[:2] == ("review", "N6")
    finally:
        go.set()
        te.join(30)
    assert res["export"].status_code == 201, res["export"].text
    assert _q("SELECT last_activity_at > now() - interval '1 minute' AS fresh FROM job WHERE id = :j", j=job)[0]["fresh"] is True
    assert recovery.sweep()["auto_cancelled"] == 0
    assert _state(ids) == ("review", "N6", "awaiting_save")


def test_stuck_runs_candidate_export_lock_does_not_stall_sweep(env, monkeypatch, dburl):
    """독립 재검토 2차 1번: 집계 복구(_stuck_runs)는 처리 중 작업으로 제한되지 않는다 — N5 검수(review · N5)도 하류 실행이 running 으로
    남아 대상이다. 그 작업의 내보내기가 작업 행을 잡은 채 업로드 중일 때 전체 sweep() → 그 작업은 기다리지 않고 건너뛰고, 뒤 작업(24시간 지난
    N3)은 같은 주기에 자동 취소된다. 내보내기가 끝난 뒤 다음 주기에는 집계 복구가 그 작업을 다시 본다(기능은 그대로)."""
    from app import s3

    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    run = _q("SELECT r.id FROM job_async_task r WHERE r.job_id = :j AND r.parent_task_id IS NULL AND r.run_kind = 'downstream' "
             "AND r.status = 'running' AND NOT EXISTS (SELECT 1 FROM job_async_task a WHERE a.parent_task_id = r.id AND a.is_current "
             "AND a.status IN ('pending', 'running'))", j=job)
    assert run, "N5 작업의 하류 실행이 집계 복구 대상이 아니다(시나리오 전제)"
    behind = _to_n3(env)
    _age_activity(behind["job"], 25)

    uploading, go = threading.Event(), threading.Event()

    def slow_upload(f, key, content_type=None):
        data = f.read()
        uploading.set()
        go.wait(20)
        env["store"].put(key, data)
        return key

    monkeypatch.setattr(s3, "upload_fileobj", slow_upload)
    res: dict = {}
    te = threading.Thread(target=lambda: res.update(export=_client_like(ids).post(
        f"/v1/jobs/{job}/export", json={"components": ["content.csv"]})))
    te.start()
    try:
        assert uploading.wait(20)
        t0 = time.time()
        st = recovery.sweep()
        took = time.time() - t0
        assert took < 5, f"점검이 집계 복구 대상 작업의 잠금을 기다렸다({took:.1f}초)"
        assert st["skipped_locked"] >= 1 and st["auto_cancelled"] == 1
        assert _state(behind)[0] == "cancelled"
    finally:
        go.set()
        te.join(30)
    assert res["export"].status_code == 201, res["export"].text
    st2 = recovery.sweep()  # 다음 주기: 잠금이 풀려 집계 복구가 이 실행을 다시 본다
    assert st2["skipped_locked"] == 0 and st2["progressed"] >= 1
    assert _state(ids) == ("review", "N5", "reviewing")
