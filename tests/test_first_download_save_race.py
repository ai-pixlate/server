"""첫 다운로드(FIN-05 POST) · [저장](FIN-06) 동시 — 작업 행 잠금 순서를 실제 2연결로 고정해 본다(PM 11-25 D3).

PM D3 규칙: 「반복 다운로드는 저장 시각 유지, 명시적 [저장]은 최근 저장 시각 갱신」 — saved_at 을 쓰는 것은 완료 전이(첫 다운로드
또는 첫 [저장])와 매 [저장]뿐이고, 이미 완료된 작업의 다운로드는 쓰지 않는다. 두 순서의 단언이 이 규칙의 두 갈래에 대응한다.

먼저 온 요청이 lock_job(FOR UPDATE)을 잡은 채 멈추고, 뒤 요청이 그 행 잠금을 기다리는 것(pg_stat_activity)을 확인한 뒤 놓는다.
① 다운로드 먼저 → [저장] 대기: 다운로드가 완료 전이(saved_at 1번), 뒤 [저장]은 done · N6 에서도 명시적 [저장]이라
   saved_at 을 다시 쓴다 → saved_log == 2, 응답 savedAt == DB 의 최종 saved_at(= [저장] 시각) — 「명시적 [저장]은 갱신」.
② [저장] 먼저 → 다운로드 대기: [저장]이 완료 전이(saved_at 1번), 뒤 다운로드는 잠금 뒤 done 을 보고 링크만 준다
   → saved_log == 1, DB saved_at == [저장] 응답 savedAt 그대로 — 「반복(완료 뒤) 다운로드는 유지」.
두 순서 모두 완료 전이 1번(done_log) · 카드 1장 · 500 없음.
"""
from __future__ import annotations

import sys
import threading
import time

import pytest
from sqlalchemy import text

from app import execution
from tests.test_first_download_complete import (  # noqa: F401
    _awaiting, _cards, _client, _export, _saved, _url, _wait_lock_waiters, dburl, env, saved_log, up,
)
from tests.test_job_state_timers import _q, _state


def _completions(job):
    """완료 전이 횟수 — status 가 done 으로 바뀐 UPDATE 를 이 일회용 시험 DB 의 기록 트리거로 센다."""
    return _q("SELECT count(*) AS n FROM t_done_log WHERE job_id = :j", j=job)[0]["n"]


@pytest.fixture
def done_log(env):
    _q("CREATE TABLE IF NOT EXISTS t_done_log (job_id bigint)")
    _q("CREATE OR REPLACE FUNCTION t_done_fn() RETURNS trigger AS $$ BEGIN "
       "IF NEW.status = 'done' AND OLD.status IS DISTINCT FROM 'done' THEN INSERT INTO t_done_log VALUES (NEW.id); END IF; "
       "RETURN NEW; END $$ LANGUAGE plpgsql")
    _q("DROP TRIGGER IF EXISTS t_done_trg ON job")
    _q("CREATE TRIGGER t_done_trg AFTER UPDATE ON job FOR EACH ROW EXECUTE FUNCTION t_done_fn()")
    yield _completions
    _q("DROP TRIGGER IF EXISTS t_done_trg ON job")


def _run_ordered(ids, art, dburl, monkeypatch, first: str):
    """first('download_and_complete' | 'save')가 잠금을 먼저 잡고 멈춘 동안 다른 쪽이 FOR UPDATE 에서 기다리게 한 뒤 놓는다."""
    real = execution.lock_job
    order: list[str] = []
    seen: dict[str, dict] = {}
    got_first = threading.Event()
    release = threading.Event()

    def gated(db, job_id):
        who = sys._getframe(1).f_code.co_name
        row = real(db, job_id)
        order.append(who)
        seen[who] = {"status": row["status"], "step": row["current_step"]}
        if who == first:
            got_first.set()
            assert release.wait(30), "놓아 주지 않았다"
        return row

    monkeypatch.setattr(execution, "lock_job", gated)
    job = ids["job"]
    calls = {
        "download_and_complete": lambda: _client(ids, raise_errors=False).post(_url(ids, art)),
        "save": lambda: _client(ids, raise_errors=False).post(f"/v1/jobs/{job}/save"),
    }
    second = "save" if first == "download_and_complete" else "download_and_complete"
    res: dict = {}
    ta = threading.Thread(target=lambda: res.update({first: calls[first]()}))
    tb = threading.Thread(target=lambda: res.update({second: calls[second]()}))
    try:
        ta.start()
        assert got_first.wait(15), "먼저 보낸 요청이 잠금을 잡지 못했다"
        tb.start()
        assert _wait_lock_waiters(dburl, 1), "뒤 요청이 작업 행 잠금을 기다리지 않는다"
        waiting_before_release = True
    finally:
        release.set()
        ta.join(30)
        tb.join(30)
    assert waiting_before_release
    return res, order, seen


@pytest.mark.parametrize("first", ["download_and_complete", "save"], ids=["download_first", "save_first"])
def test_download_and_save_concurrent(env, up, saved_log, done_log, dburl, monkeypatch, first):
    ids = _awaiting(env)
    art = _export(ids)
    job = ids["job"]
    assert _saved(ids)["is_saved"] is False and saved_log(job) == 0 and _cards(ids) == []

    res, order, seen = _run_ordered(ids, art, dburl, monkeypatch, first)
    second = "save" if first == "download_and_complete" else "download_and_complete"
    print(f"\n[{first} 먼저] 잠금 순서={order} 잠금 뒤 본 상태={seen} "
          f"응답={ {k: v.status_code for k, v in res.items()} } state={_state(ids)} saved={_saved(ids)} "
          f"saved_at 쓰기={saved_log(job)} 완료 전이={done_log(job)} 카드={len(_cards(ids))}")

    assert order == [first, second]
    assert seen[first] == {"status": "review", "step": "N6"}
    assert seen[second] == {"status": "done", "step": "N6"}  # 뒤 요청은 잠금 뒤 먼저 커밋된 완료를 본다
    for k, r in res.items():
        assert r.status_code == 200, (k, r.status_code, r.text)
    assert res["download_and_complete"].json()["url"]
    assert res["save"].json()["status"] == "done" and res["save"].json()["isSaved"] is True
    assert _state(ids) == ("done", "N6", "done") and _saved(ids)["is_saved"] is True
    assert done_log(job) == 1 and len(_cards(ids)) == 1
    if first == "download_and_complete":
        # 다운로드가 완료 전이로 1번, 뒤 [저장]은 FIN-06 현행대로 done 에서 다시 쓴다(D3 「매 [저장]」) → 2번
        assert saved_log(job) == 2
        assert res["save"].json()["savedAt"] == _saved(ids)["saved_at"].isoformat()
    else:
        # [저장]이 1번, 뒤 다운로드는 done 을 보고 링크만 — saved_at 을 다시 쓰지 않는다(D3)
        assert saved_log(job) == 1
        assert res["save"].json()["savedAt"] == _saved(ids)["saved_at"].isoformat()
