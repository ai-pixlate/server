"""병렬화 B — run_adopt 를 전용 adopt 큐로 분리한 뒤의 라우팅 · 동시 소비 시험.

- 라우팅: run_adopt 만 adopt 큐, 나머지 task 는 그대로(기본 큐 cpu).
- 소비자 3개(gpu · cpu · adopt 스레드)가 동시에 N4 를 처리해도 섹션마다 미리보기 1번 · N5 전이 1번 · 인계 채택 1번.
- 같은 인계의 채택 메시지 2개(옛 라우팅 cpu 워커 + 새 라우팅 adopt 워커, 또는 복구 재전달)가 동시에 와도 한쪽만 채택.

실제 PostgreSQL(일회용) + 메모리 저장소 + 대역 모델(tests/test_exec_downstream 의 env). 스레드는 워커 프로세스 흉내다
(같은 파이썬 프로세스 — 실제 Redis 전달 · acks_late · 재전달 · 프로세스 분리의 근거가 아니다).

이 파일에 넣지 않은 것: 권한 인수 패자의 인계 거절(reject_and_fail 보완 — fix v2) · 채택 ↔ 처리 시간 상한 점검 동시(#157 의존 — R3).
"""
from __future__ import annotations

import random
import threading
import time

import pytest

from app import execution, recovery
from app.celery_app import celery_app
from app.flows import downstream
from tests.exec_db import requires_db
from tests.test_exec_downstream import _q, _to_n3, dburl, env  # noqa: F401 — fixture 재사용

ADOPT = execution.ADOPT_TASK


def _queue_of(name: str) -> str:
    r = celery_app.amqp.router.route({}, name, args=(1,), kwargs={})
    q = r.get("queue")
    return getattr(q, "name", q)


def _state(ids):
    j = ids["c"].get(f"/v1/jobs/{ids['job']}").json()
    return j["status"], j["currentStep"], j["userFacingStatus"]


# ---- 라우팅 ---------------------------------------------------------------------------------------------------
def test_routing_only_run_adopt_moves_to_adopt_queue():
    expect = {"app.tasks.run_analyze": "ocr", "app.tasks.run_inpaint": "gpu", ADOPT: "adopt"}
    names = sorted(set(execution.TASK_BY_STAGE.values()) | {ADOPT})
    got = {n: _queue_of(n) for n in names}
    assert got == {n: expect.get(n, "cpu") for n in names}
    assert {q.name for q in celery_app.conf.task_queues} == {"ocr", "cpu", "gpu", "adopt"}
    assert celery_app.conf.task_default_queue == "cpu"
    # 라우팅 표에 적힌 task 가 전부 위 이름 집합 안에 있다(표에만 있고 시험이 모르는 task 없음)
    assert set(celery_app.conf.task_routes) == set(names)


# ---- 큐별 소비자 흉내 ---------------------------------------------------------------------------------------------
class Routed:
    """execution 의 dispatcher 자리에 끼운다. celery 라우팅 표대로 큐별 목록에 넣는다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.q: dict[str, list] = {"ocr": [], "cpu": [], "gpu": [], "adopt": []}
        self.sent: list[tuple[str, str]] = []
        self.busy: set[str] = set()

    def __call__(self, name, args):
        qn = _queue_of(name)
        with self.lock:
            self.q[qn].append((name, list(args)))
            self.sent.append((qn, name))

    def take(self, qn):
        with self.lock:
            if self.q[qn]:
                self.busy.add(qn)
                return self.q[qn].pop(0)
            return None

    def done(self, qn):
        with self.lock:
            self.busy.discard(qn)

    def idle(self):
        with self.lock:
            return not self.busy and not any(self.q.values())


def _run_consumers(rq: Routed, queues, seed: int, timeout=180.0):
    import app.tasks as tasks

    errors: list = []
    spans: list = []
    stop = threading.Event()

    def worker(qn, rnd):
        while not stop.is_set():
            m = rq.take(qn)
            if m is None:
                time.sleep(0.003)
                continue
            try:
                time.sleep(rnd.random() * 0.02)  # 처리 순서를 흔든다
                t0 = time.monotonic()
                getattr(tasks, m[0].rsplit(".", 1)[-1])(*m[1])
                spans.append((qn, m[0], t0, time.monotonic()))
            except Exception as e:  # noqa: BLE001 — celery 라면 로그 후 ack. 여기서는 모아서 검사한다
                errors.append((qn, m, repr(e)))
            finally:
                rq.done(qn)

    ths = [threading.Thread(target=worker, args=(qn, random.Random(seed * 31 + i)), name=f"w-{qn}", daemon=True)
           for i, qn in enumerate(queues)]
    for t in ths:
        t.start()
    end = time.monotonic() + timeout
    stable = 0
    while time.monotonic() < end:
        stable = stable + 1 if rq.idle() else 0
        if stable >= 20:
            break
        time.sleep(0.01)
    stop.set()
    for t in ths:
        t.join(10)
    assert rq.idle(), "시간 안에 끝나지 않음"
    return errors, spans


def _spy_n5(monkeypatch):
    calls: list = []
    lk = threading.Lock()
    real = downstream.set_job_state

    def spy(db, job_id, **kw):
        with lk:
            calls.append((kw.get("status"), kw.get("step")))
        return real(db, job_id, **kw)

    monkeypatch.setattr(downstream, "set_job_state", spy)
    return calls


def _inpaint_handoffs(job):
    return _q("SELECT h.id, h.state, h.reject_reason, h.adopted_by, a.id AS tid, a.unit_id, a.status, a.error_code "
              "FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
              "WHERE a.job_id = :j AND a.stage = 'inpaint' ORDER BY h.id", j=job)


@requires_db
@pytest.mark.parametrize("seed", range(6))
def test_three_consumers_concurrent_n4_single_preview_and_n5(env, monkeypatch, seed):
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    rq = Routed()
    execution.set_dispatcher(rq)
    n5 = _spy_n5(monkeypatch)
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    errors, spans = _run_consumers(rq, ("gpu", "cpu", "adopt"), seed)
    assert errors == []
    assert _state(ids) == ("review", "N5", "reviewing")
    assert n5.count(("review", "N5")) == 1
    sections = [r["unit_id"] for r in _q("SELECT DISTINCT unit_id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint'", j=job)]
    assert len(sections) >= 2
    pv = _q("SELECT unit_id, count(*) AS n, bool_and(status = 'done') AS ok FROM job_async_task WHERE job_id = :j AND stage = 'preview' "
            "GROUP BY unit_id ORDER BY unit_id", j=job)
    assert [(r["unit_id"], r["n"], r["ok"]) for r in pv] == [(s, 1, True) for s in sorted(sections)]
    hs = _inpaint_handoffs(job)
    assert len(hs) == len(sections) and all(h["state"] == "adopted" for h in hs)
    # 채택 메시지는 전부 adopt 큐로, adopt 소비자만 run_adopt 를 처리했다
    assert {(q, n) for q, n in rq.sent if n == ADOPT} == {("adopt", ADOPT)}
    assert not any(q == "cpu" and n == ADOPT for q, n in rq.sent)
    assert {q for q, n, _s, _e in spans if n == ADOPT} == {"adopt"}
    assert not any(q == "adopt" and n != ADOPT for q, n, _s, _e in spans)


def _to_received_handoffs(env):
    """N4 를 끝까지 돌리되 채택 메시지만 처리하지 않는다 → 인페인트 인계가 received 로 남고 작업은 처리 중."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    env["q"].drain(skip={ADOPT})
    hs = [h for h in _inpaint_handoffs(job) if h["state"] == "received"]
    assert len(hs) >= 2
    assert _state(ids)[:2] == ("processing", "N4")
    return ids, [h["id"] for h in hs]


@requires_db
@pytest.mark.parametrize("distinct", [True, False], ids=["two-hosts", "same-identity"])
@pytest.mark.parametrize("rnd", range(3))
def test_duplicate_adopt_messages_old_and_new_route_adopt_once(env, monkeypatch, distinct, rnd):
    """옛 판 발행자(→ cpu 워커)와 새 판 발행자(→ adopt 워커), 또는 복구 재전달로 같은 인계 메시지가 두 워커에 동시에 간다."""
    from app import tasks

    monkeypatch.setenv("PIXLATE_DISPATCH_STALE_S", "0")  # 같은 식별자 경우의 복구 재전달을 기다리지 않게
    ids, hids = _to_received_handoffs(env)
    job = ids["job"]
    if distinct:
        monkeypatch.setattr(execution, "worker_identity", lambda: f"{threading.current_thread().name}:1")
    else:
        monkeypatch.setattr(execution, "worker_identity", lambda: "same-host:1")
    results: dict = {}
    errors: list = []

    def call(h, who, bar, jitter):
        bar.wait()
        time.sleep(jitter)
        try:
            results.setdefault(h, []).append((who, tasks.run_adopt(h)))
        except Exception as e:  # noqa: BLE001
            errors.append((h, who, repr(e)))

    r = random.Random(rnd)
    for h in hids:
        bar = threading.Barrier(2)
        ths = [threading.Thread(target=call, args=(h, who, bar, r.random() * 0.01), name=f"adopter-{who}") for who in ("cpu", "adopt")]
        for t in ths:
            t.start()
        for t in ths:
            t.join(60)
    env["q"].drain()
    hs = {h["id"]: h for h in _inpaint_handoffs(job)}
    if not distinct and not all(hs[h]["state"] == "adopted" for h in hids):
        # 관찰(실제 배포에서는 컨테이너 hostname 이 달라 같은 식별자 두 실행자는 생기지 않음): 나중 인수자가 세대를 올리고,
        # 먼저 인수한 쪽이 LEASE_LOST(retryable) 로 reject_and_fail → 인계를 rejected 로 닫는다 → 나중 인수자 adopt 는 None.
        # 결과: 인계 결과를 버리고 권한 만료 뒤 복구가 인페인트를 다시 계산한다(정합성은 유지, GPU 재계산 · 지연).
        # 이 동작의 보완은 reject_and_fail 보완(fix v2) 범위다.
        assert all(hs[h]["state"] in ("adopted", "rejected") for h in hids)
        _q("UPDATE job_async_task SET lease_expires_at = now() - interval '1 second' WHERE id = ANY(:t)",
           t=[hs[h]["tid"] for h in hids if hs[h]["state"] != "adopted"])
        recovery.sweep()  # 권한 만료 → LEASE_EXPIRED + 복구 재시도(인페인트 재계산)
        env["q"].drain()
        per = _q("SELECT a.unit_id, count(*) FILTER (WHERE h.state = 'adopted') AS adopted FROM task_handoff h "
                 "JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j AND a.stage = 'inpaint' GROUP BY a.unit_id", j=job)
        assert all(p["adopted"] == 1 for p in per)
        assert _state(ids) == ("review", "N5", "reviewing")
        return
    assert all(hs[h]["state"] == "adopted" for h in hids)
    wins = {h: sum(1 for _, out in results.get(h, []) if out.get("adopted")) for h in hids}
    assert all(n == 1 for n in wins.values()), (wins, errors)
    if distinct:
        assert errors == []
    arts = _q("SELECT handoff_id, count(*) AS n, bool_and(state = 'adopted') AS ok FROM task_artifact WHERE handoff_id = ANY(:h) "
              "GROUP BY handoff_id", h=hids)
    assert all(a["ok"] for a in arts)
    pv = _q("SELECT unit_id, count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview' GROUP BY unit_id", j=job)
    assert pv and all(p["n"] == 1 for p in pv)
    assert _state(ids) == ("review", "N5", "reviewing")
