"""첫 다운로드 = 완료(F-FIN-03 · PM 11-25 D1~D4 · README 7.8 ⑧) — 실제 PostgreSQL + 메모리 저장소.

POST /jobs/{jobId}/exports/{artifactId}/download: 저장 대기면 완료 기록 + 링크, 완료면 링크만. 산출물 검증 · 상태 판정 ·
커밋 뒤에만 링크 · 반복 · 동시 · 커밋 결과 불명확 재요청 · 취소 경합 · 자동 취소 제외 · 소급 없음.
saved_at 쓰기 횟수는 이 일회용 시험 DB 에만 거는 기록 트리거로 센다(UPDATE OF saved_at — 같은 값이어도 SET 에 있으면 센다).
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app import recovery
from tests.test_job_state_timers import _age_activity, _age_state, _q, _state, _to_n5, dburl, env  # noqa: F401


@pytest.fixture
def saved_log(env):
    _q("CREATE TABLE IF NOT EXISTS t_saved_log (job_id bigint, saved_at timestamptz)")
    _q("CREATE OR REPLACE FUNCTION t_saved_fn() RETURNS trigger AS $$ BEGIN "
       "INSERT INTO t_saved_log VALUES (NEW.id, NEW.saved_at); RETURN NEW; END $$ LANGUAGE plpgsql")
    _q("DROP TRIGGER IF EXISTS t_saved_trg ON job")
    _q("CREATE TRIGGER t_saved_trg AFTER UPDATE OF saved_at ON job FOR EACH ROW EXECUTE FUNCTION t_saved_fn()")
    yield lambda job: _q("SELECT count(*) AS n FROM t_saved_log WHERE job_id = :j", j=job)[0]["n"]
    _q("DROP TRIGGER IF EXISTS t_saved_trg ON job")


@pytest.fixture
def up(env, monkeypatch):
    from app import s3

    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: env["store"].put(key, f.read()) or key)


def _client(ids, *, raise_errors=True):
    from app.main import app

    c = TestClient(app, raise_server_exceptions=raise_errors)
    c.headers["Authorization"] = ids["c"].headers["Authorization"]
    return c


def _awaiting(env):
    ids = _to_n5(env)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert _state(ids) == ("review", "N6", "awaiting_save")
    return ids


def _export(ids, comps=("content.csv",)):
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/export", json={"components": list(comps)})
    assert r.status_code == 201, r.text
    return r.json()["artifactId"]


def _url(ids, art):
    return f"/v1/jobs/{ids['job']}/exports/{art}/download"


def _saved(ids):
    return _q("SELECT is_saved, saved_at FROM job WHERE id = :j", j=ids["job"])[0]


def _cards(ids):
    return [c for c in ids["c"].get("/v1/library").json() if c["jobId"] == ids["job"]]


def _err(r, status, code):
    assert r.status_code == status, r.text
    e = r.json()["error"]
    assert e["code"] == code and "url" not in r.json()
    return e


# ---- 완료 대상 · 이미 완료 ----------------------------------------------------------------------------------------
def test_awaiting_save_first_download_completes_once_and_repeat_keeps_saved_at(env, up, saved_log):
    ids = _awaiting(env)
    art = _export(ids)
    assert _saved(ids)["is_saved"] is False and _cards(ids) == []
    r = ids["c"].post(_url(ids, art))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"].startswith("https://presigned.test/export/") and body["fileName"] and body["expiresAt"]
    assert _state(ids) == ("done", "N6", "done")
    first = _saved(ids)
    assert first["is_saved"] is True and first["saved_at"] is not None
    assert len(_cards(ids)) == 1 and saved_log(ids["job"]) == 1
    # 반복 다운로드 · 다른 묶음(행별) — 링크만, saved_at 그대로(D3)
    art2 = _export(ids, ("images",))
    for a in (art, art2, art):
        r = ids["c"].post(_url(ids, a))
        assert r.status_code == 200 and r.json()["url"]
    assert _saved(ids)["saved_at"] == first["saved_at"] and saved_log(ids["job"]) == 1
    assert len(_cards(ids)) == 1 and _state(ids) == ("done", "N6", "done")


def test_saved_job_download_does_not_rewrite_saved_at(env, up, saved_log):
    ids = _awaiting(env)
    art = _export(ids)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/save").status_code == 200
    before = _saved(ids)["saved_at"]
    n = saved_log(ids["job"])
    assert ids["c"].post(_url(ids, art)).status_code == 200
    assert _saved(ids)["saved_at"] == before and saved_log(ids["job"]) == n


def test_old_bundle_from_before_rerender_still_completes(env, up):
    """D4 허용 — 재렌더 전에 만든 묶음(렌더 오류 뒤 [다시 시도]로 새 결과가 채택됨)으로도 완료된다."""
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    old = _export(ids)  # N5 에서 만든 묶음(FIN-04 는 상태 제한 없음)
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert _state(ids) == ("review", "N6", "awaiting_save")
    assert c.post(_url(ids, old)).status_code == 200
    assert _state(ids) == ("done", "N6", "done")


# ---- 산출물 공통 검증(두 상태 모두) ------------------------------------------------------------------------------
def _bad_artifacts(env, ids):
    other = _awaiting(env)
    other_art = _export(other)
    mine = _export(ids)
    nodist = _export(ids)
    _q("UPDATE export_artifact SET is_distributable = false WHERE id = :a", a=nodist)
    nogen = _export(ids)
    _q("UPDATE export_artifact SET is_generated = false WHERE id = :a", a=nogen)
    nokey = _export(ids)
    _q("UPDATE export_artifact SET file_url = NULL WHERE id = :a", a=nokey)
    return {"other_job": other_art, "missing": mine + 10_000, "not_distributable": nodist, "not_generated": nogen,
            "no_file_key": nokey}, other


@pytest.mark.parametrize("saved_first", [False, True], ids=["awaiting_save", "done"])
def test_artifact_checks_in_both_states_404_without_state_change(env, up, saved_log, saved_first):
    ids = _awaiting(env)
    if saved_first:
        assert ids["c"].post(f"/v1/jobs/{ids['job']}/save").status_code == 200
    bad, other = _bad_artifacts(env, ids)
    before, n, st = _saved(ids), saved_log(ids["job"]), _state(ids)
    for why, art in bad.items():
        r = ids["c"].post(_url(ids, art))
        _err(r, 404, "NOT_FOUND")
        assert _state(ids) == st and _saved(ids) == before and saved_log(ids["job"]) == n, why
    # 남의 작업 — 소유 확인 404, 그 작업 상태도 그대로
    ok = _export(ids)
    r = other["c"].post(f"/v1/jobs/{ids['job']}/exports/{ok}/download")
    _err(r, 404, "NOT_FOUND")
    assert _state(ids) == st and _state(other) == ("review", "N6", "awaiting_save")


# ---- 상태 판정 --------------------------------------------------------------------------------------------------
def test_not_completable_states_409(env, up, saved_log):
    ids = _to_n5(env)
    c, job = ids["c"], ids["job"]
    art = _export(ids)
    e = _err(c.post(_url(ids, art)), 409, "INVALID_STATE")  # N5
    assert e["details"] is None
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    assert _state(ids) == ("processing", "N6", "rendering")
    _err(c.post(_url(ids, art)), 409, "INVALID_STATE")  # 최종 렌더 중
    env["q"].messages = []
    _age_state(job, 1801)
    assert recovery.sweep()["timed_out"] == 1
    assert _state(ids) == ("failed", "N6", "failed")
    _err(c.post(_url(ids, art)), 409, "INVALID_STATE")  # 렌더 오류
    assert _saved(ids)["is_saved"] is False and saved_log(job) == 0 and _cards(ids) == []


def test_cancelled_job_409_without_reason(env, up):
    ids = _awaiting(env)
    art = _export(ids)
    assert ids["c"].delete(f"/v1/jobs/{ids['job']}").status_code == 200
    e = _err(ids["c"].post(_url(ids, art)), 409, "INVALID_STATE")
    assert e["details"] is None
    assert _state(ids)[0] == "cancelled" and _saved(ids)["is_saved"] is False


# ---- 동시 · 취소 경합 -------------------------------------------------------------------------------------------
def _wait_lock_waiters(dburl, n, timeout=10.0):
    eng = create_engine(dburl)
    try:
        end = time.time() + timeout
        while time.time() < end:
            with eng.connect() as c:
                got = c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                     "AND query ILIKE '%FOR UPDATE%'")).scalar()
            if got >= n:
                return True
            time.sleep(0.05)
        return False
    finally:
        eng.dispose()


def test_concurrent_bundle_and_row_downloads_complete_once(env, up, saved_log, dburl):
    """묶음 · 행별 다운로드 거의 동시 2건 — 작업 행을 다른 연결이 잡은 동안 두 요청이 모두 잠금을 기다리게 한 뒤 놓는다.
    둘 다 200 링크, 완료 전이 · saved_at 쓰기 1번, 카드 1개."""
    ids = _awaiting(env)
    zip_art = _export(ids, ("content.csv", "images"))
    row_art = _export(ids, ("images",))
    eng = create_engine(dburl)
    holder = eng.connect()
    tx = holder.begin()
    holder.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": ids["job"]})
    res: dict = {}
    ts = [threading.Thread(target=lambda k=k, a=a: res.update({k: _client(ids).post(_url(ids, a))}))
          for k, a in (("zip", zip_art), ("row", row_art))]
    try:
        for t in ts:
            t.start()
        assert _wait_lock_waiters(dburl, 2), "두 요청이 작업 행 잠금을 기다리지 않는다"
    finally:
        tx.rollback()
        holder.close()
        for t in ts:
            t.join(30)
        eng.dispose()
    assert res["zip"].status_code == 200 and res["row"].status_code == 200, (res["zip"].text, res["row"].text)
    assert _state(ids) == ("done", "N6", "done") and saved_log(ids["job"]) == 1 and len(_cards(ids)) == 1


def test_cancel_takes_lock_first_then_download_409(env, up, dburl):
    """취소가 먼저 작업 행을 잡고 커밋 → 기다리던 다운로드는 잠금 뒤 cancelled 를 보고 409, 링크 · 완료 없음."""
    ids = _awaiting(env)
    art = _export(ids)
    eng = create_engine(dburl)
    holder = eng.connect()
    tx = holder.begin()
    holder.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": ids["job"]})
    res: dict = {}
    t = threading.Thread(target=lambda: res.update(r=_client(ids).post(_url(ids, art))))
    try:
        t.start()
        assert _wait_lock_waiters(dburl, 1)
        holder.execute(text("UPDATE job SET status = 'cancelled', user_facing_status = 'cancelled' WHERE id = :j"),
                       {"j": ids["job"]})
        tx.commit()
    finally:
        if tx.is_active:
            tx.rollback()
        holder.close()
        t.join(30)
        eng.dispose()
    _err(res["r"], 409, "INVALID_STATE")
    assert _saved(ids)["is_saved"] is False


# ---- 커밋 전 실패 · 커밋 결과 불명확 · 링크 서명 실패 --------------------------------------------------------------
def _fail_next_commit(monkeypatch, *, after_commit: bool):
    """다음 Session.commit 한 번을 실패시킨다. after_commit=True 면 실제로 커밋한 뒤 오류(서버가 성공을 확인 못 한 경우 흉내)."""
    real = Session.commit
    armed = {"on": True}

    def commit(self):
        if not armed["on"]:
            return real(self)
        armed["on"] = False
        if after_commit:
            real(self)
            raise RuntimeError("커밋 응답 유실 흉내")
        raise RuntimeError("커밋 직전 실패 흉내")

    monkeypatch.setattr(Session, "commit", commit)


def test_failure_before_commit_rolls_back_then_retry_completes(env, up, saved_log, monkeypatch):
    ids = _awaiting(env)
    art = _export(ids)
    _fail_next_commit(monkeypatch, after_commit=False)
    r = _client(ids, raise_errors=False).post(_url(ids, art))
    assert r.status_code == 500 and "url" not in r.json()
    assert _state(ids) == ("review", "N6", "awaiting_save") and _saved(ids)["is_saved"] is False
    assert saved_log(ids["job"]) == 0
    r = ids["c"].post(_url(ids, art))  # (ㄴ) 실제로 안 된 채 오류 → 재요청이 완료 재시도
    assert r.status_code == 200 and _state(ids) == ("done", "N6", "done") and saved_log(ids["job"]) == 1


def test_commit_landed_but_error_then_retry_keeps_single_completion(env, up, saved_log, monkeypatch):
    ids = _awaiting(env)
    art = _export(ids)
    _fail_next_commit(monkeypatch, after_commit=True)
    r = _client(ids, raise_errors=False).post(_url(ids, art))
    assert r.status_code == 500 and "url" not in r.json()  # 서버가 확인 못 했으면 링크를 내지 않는다
    assert _state(ids) == ("done", "N6", "done")  # (ㄱ) 실제로는 커밋됨
    first = _saved(ids)["saved_at"]
    r = ids["c"].post(_url(ids, art))  # 재요청 — 잠금 뒤 done → 링크만
    assert r.status_code == 200 and r.json()["url"]
    assert _saved(ids)["saved_at"] == first and saved_log(ids["job"]) == 1 and len(_cards(ids)) == 1


def test_commit_unclear_then_cancel_then_retry_409(env, up, monkeypatch):
    ids = _awaiting(env)
    art = _export(ids)
    _fail_next_commit(monkeypatch, after_commit=False)
    assert _client(ids, raise_errors=False).post(_url(ids, art)).status_code == 500
    assert ids["c"].delete(f"/v1/jobs/{ids['job']}").status_code == 200  # (ㄷ) 두 요청 사이 취소
    _err(ids["c"].post(_url(ids, art)), 409, "INVALID_STATE")


def test_link_signing_failure_no_completion(env, up, saved_log, monkeypatch):
    from app import s3

    ids = _awaiting(env)
    art = _export(ids)

    def boom(*a, **k):
        raise RuntimeError("서명 실패 흉내")

    monkeypatch.setattr(s3, "presigned_get", boom)
    r = _client(ids, raise_errors=False).post(_url(ids, art))
    assert r.status_code == 500 and "url" not in r.json()
    assert _state(ids) == ("review", "N6", "awaiting_save") and saved_log(ids["job"]) == 0


# ---- 자동 취소 · 소급 없음 · 마지막 움직임 ------------------------------------------------------------------------
def test_downloaded_job_is_not_auto_cancelled_but_awaiting_one_is(env, up):
    done = _awaiting(env)
    art = _export(done)
    assert done["c"].post(_url(done, art)).status_code == 200
    waiting = _awaiting(env)
    _export(waiting)
    _age_activity(done["job"], 25)
    _age_activity(waiting["job"], 25)
    recovery.sweep()
    assert _state(done) == ("done", "N6", "done")
    assert _state(waiting)[0] == "cancelled"


def test_get_download_does_not_complete_no_backfill(env, up):
    """기존 GET 은 그대로 상태를 바꾸지 않는다(D2 제한은 후속) — 내려받기만 한 옛 작업을 완료로 소급하지 않는다."""
    ids = _awaiting(env)
    art = _export(ids)
    assert ids["c"].get(_url(ids, art)).status_code == 200
    assert ids["c"].get(f"/v1/jobs/{ids['job']}/export/download", params={"artifactType": "csv"}).status_code == 200
    assert _state(ids) == ("review", "N6", "awaiting_save") and _saved(ids)["is_saved"] is False and _cards(ids) == []


def test_post_download_counts_as_activity(env, up):
    ids = _awaiting(env)
    art = _export(ids)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/save").status_code == 200
    _age_activity(ids["job"], 25)
    assert ids["c"].post(_url(ids, art)).status_code == 200
    assert not _q("SELECT last_activity_at < now() - interval '24 hours' AS old FROM job WHERE id = :j", j=ids["job"])[0]["old"]
