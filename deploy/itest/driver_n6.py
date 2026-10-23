"""통합 검증 N6 — 작업 9(N5)에서 POST /v1/jobs/9/confirm 1회 → 최종 렌더 감시 → 처리 결과 보고. 렌더·저장·내보내기·재시도 요청 없음.

모드: precheck(읽기 전용) | run | check(읽기 전용 정리 확인 한 줄)
- confirm 은 CONFIRM_SENT(O_EXCL) 로 한 번만 보낸다. 응답이 유실·시간 초과돼도 다시 보내지 않고, 작업 단계와 새 최종 렌더 실행을
  DB 로 조회해 실제 반영 여부를 판정한다. confirm 자체가 렌더를 발행하므로 별도 렌더 요청을 하지 않는다.
- N6 에는 /abort 가 없다. 이상 사유가 나와도 추가 요청 없이 관찰·정리 확인만 하고 상태를 보존한 채 보고한다
  (운영 이상 S4 는 로컬 감시기의 비상 절차).
- 재시도: 최종 렌더는 '재시도 가능한 오류에 한해 자동 재시도 최대 1회'(app/flows/final.py after_failure). 복구 프로세스의
  lease 만료 재시도(retry_origin=recovery)는 따로 기록한다.
- 종료 상태:
    N6-done                    run 37 done + 최종 렌더 대표 실행 done + 현재 시도 done + 인계 adopted (+ 정리 확인)
    N6-render-failed-preserved 현재 시도 failed, 하위 진행·큐·열린 인계 0, 대표 실행만 running(수동 재시도 대기 상태로 보존)
    N6-unsettled               제한 시간 안에 위 둘 중 어느 것도 확인 못 함
  처리 성공과 시각 품질은 별개다(n6_verify.py).
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

EV = os.environ.get("ITEST_EV", "/ev")
API = "http://pixlate-api:8000/v1"
EMAIL = "itest-20261013@example.com"
JOB_ID = 9
DOWNSTREAM_RUN = 37
TICK = 10
STAGES = {"final_render"}
UNITS = {JOB_ID}
STAGE_LIMIT_S = {"final_render": 300}
PENDING_LIMIT_S = 300
TOTAL_LIMIT_S = 15 * 60
QUERY_FAIL_LIMIT = 3
OBSERVE_S = 10 * 60
OPEN_HANDOFF = ("received", "verifying", "verified")
ADOPT_WAIT_S = 300
ATT_COLS = ("id, parent_task_id, run_kind, stage, unit_id, status, attempt_no, is_current, retry_count, max_retry, "
            "retry_origin, error_code, dispatch_count, last_dispatched_at, started_at, finished_at, heartbeat_at, "
            "lease_expires_at, lease_owner IS NOT NULL AS leased, cancelled_at")
COLUMNS = {
    "job": ["id", "status", "current_step", "user_facing_status"],
    "job_async_task": ["id", "job_id", "parent_task_id", "run_kind", "stage", "unit_id", "status", "attempt_no", "is_current",
                       "retry_count", "max_retry", "retry_origin", "error_code", "dispatch_count", "last_dispatched_at",
                       "started_at", "finished_at", "heartbeat_at", "lease_expires_at", "lease_owner", "cancelled_at"],
    "task_handoff": ["id", "task_id", "job_id", "state", "received_at", "adopted_at", "reject_reason", "payload", "verify_result"],
    "task_artifact": ["id", "handoff_id", "kind", "part_key", "verified_key", "sha256", "declared_sha256", "byte_size", "width",
                      "height", "state"],
    "deliverable": ["id", "job_id", "source_image_id", "image_url", "format", "file_size", "render_status", "validation_result"],
    "deliverable_section": ["deliverable_id", "section_id", "stack_offset", "order_no"],
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(name, line):
    with open(f"{EV}/{name}", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def jl(name, obj):
    append(name, json.dumps(obj, ensure_ascii=False, default=str))


def out(msg):
    print(f"[n6 {now_iso()}] {msg}", flush=True)
    append("progress.log", f"{now_iso()} {msg}")


def put_state(**kw):
    tmp = f"{EV}/.state.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": now_iso(), **kw}, f, ensure_ascii=False, default=str)
    os.replace(tmp, f"{EV}/state.json")


# ---------------------------------------------------------------- 순수 판정
def lease_state(a, handoffs, db_now):
    """N4 driver 와 같은 판정: 수신·검증 인계가 있으면 채택 단계(권한 유효 여부 무관), 없으면 valid / expired."""
    open_h = [h for h in handoffs if h["task_id"] == a["id"] and h["state"] in OPEN_HANDOFF]
    if open_h:
        h = max(open_h, key=lambda x: x["id"])
        wait = (db_now - h["received_at"]).total_seconds() if h.get("received_at") else 0
        exp = a["lease_expires_at"]
        return ("adoption_delayed" if wait > ADOPT_WAIT_S else "adoption_wait"), {
            "handoff": h["id"], "state": h["state"], "wait_s": wait, "adopter_lease_valid": exp is not None and exp > db_now}
    exp = a["lease_expires_at"]
    if exp is not None and exp > db_now:
        return "valid", None
    return "expired", [(h["id"], h["state"]) for h in handoffs if h["task_id"] == a["id"]]


def kids(rec, run_id):
    return [a for a in rec["attempts"] if a["parent_task_id"] == run_id]


def decide(rec, run_id, elapsed_s, stop_text=None):
    """N6 이상 사유(중단 API 가 없으므로 '관찰 전환' 사유). 실패·재시도는 종료 상태 판정과 별도로 기록한다."""
    reasons, db_now = [], rec["db_now"]
    for a in kids(rec, run_id):
        tag = f"#{a['id']}({a['stage']}/{a['unit_id']})"
        if a["stage"] not in STAGES:
            reasons.append(f"S1 예상 밖 단계 {tag}")
        if a["unit_id"] not in UNITS:
            reasons.append(f"S1 예상 밖 단위 {tag}")
        if a["status"] == "failed":
            reasons.append(f"S1 렌더 실패 {tag} {a['error_code']}")
        if (a["attempt_no"] or 1) > 1 or a["retry_origin"]:
            reasons.append(f"S1 재시도 {tag} attempt_no={a['attempt_no']} origin={a['retry_origin']}"
                           + (" (복구 프로세스 lease 만료 재시도)" if a["retry_origin"] == "recovery" else ""))
        if a["status"] == "running":
            ls, info = lease_state(a, rec["handoffs"], db_now)
            if ls == "expired":
                reasons.append(f"S2 계산 권한 만료(수신·검증 인계 없음) {tag} lease_expires_at={a['lease_expires_at']} 다른 인계={info}")
            elif ls == "adoption_delayed":
                reasons.append(f"S2 인계 채택 지연 {tag} 인계 #{info['handoff']}({info['state']}) 수신 후 {info['wait_s']:.0f}s > {ADOPT_WAIT_S}s")
            if ls in ("valid", "expired"):
                lim = STAGE_LIMIT_S.get(a["stage"])
                if lim and a["started_at"] and (db_now - a["started_at"]).total_seconds() > lim:
                    reasons.append(f"S2 단계 제한 시간 {tag} {lim}s")
        if a["status"] == "pending" and a["started_at"] is None and a["last_dispatched_at"] is not None:
            if (db_now - a["last_dispatched_at"]).total_seconds() > PENDING_LIMIT_S:
                reasons.append(f"S2 pending 미시작 {tag} >{PENDING_LIMIT_S}s")
    if not any(a["id"] == run_id for a in rec["attempts"]):
        reasons.append(f"S1 최종 렌더 실행 #{run_id} 없음")
    if elapsed_s > TOTAL_LIMIT_S:
        reasons.append(f"S2 N6 전체 제한 시간 {TOTAL_LIMIT_S}s 초과")
    if rec["global_active"]:
        reasons.append(f"S3 다른 작업 유입 {rec['global_active']} — 이번 검증 판정 보류(다른 작업은 건드리지 않음)")
    if stop_text:
        reasons.append("S4 로컬 감시기 요청: " + stop_text[:200])
    return reasons


def final_state(rec, run_id):
    """종료 상태 판정. 반환: 'done' | 'failed-preserved' | None(아직 진행·불명)"""
    run = next((a for a in rec["attempts"] if a["id"] == run_id), None)
    ds = next((a for a in rec["attempts"] if a["id"] == DOWNSTREAM_RUN), None)
    cur = [a for a in kids(rec, run_id) if a["is_current"]]
    active = [a for a in kids(rec, run_id) if a["status"] in ("pending", "running")]
    open_h = [h for h in rec["handoffs"] if h["state"] in OPEN_HANDOFF]
    quiet = not active and not open_h and not any(rec["queues"].values())
    if run is None or not cur:
        return None
    adopted = all(any(h["task_id"] == a["id"] and h["state"] == "adopted" for h in rec["handoffs"]) for a in cur)
    if (run["status"] == "done" and ds and ds["status"] == "done" and all(a["status"] == "done" for a in cur) and adopted
            and rec["job"]["current_step"] == "N6" and quiet):
        return "done"
    if run["status"] == "running" and quiet and any(a["status"] == "failed" for a in cur):
        return "failed-preserved"
    return None


def settled(rec, run_id):
    active = [a for a in kids(rec, run_id) if a["status"] in ("pending", "running")]
    open_h = [h for h in rec["handoffs"] if h["state"] in OPEN_HANDOFF]
    return not active and not open_h and not any(rec["queues"].values())


# ---------------------------------------------------------------- 감시 루프(주입형)
class Monitor:
    def __init__(self, fetch, stop_text, clock=time.monotonic, sleep=time.sleep, log=None, state=None):
        self.fetch, self.stop_text = fetch, stop_text
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda m: None)
        self.state = state or (lambda **kw: None)
        self.requests = 0   # N6 감시 중 API 요청 수(항상 0 이어야 한다)

    def _fetch(self, label):
        try:
            return self.fetch(label), None
        except Exception as e:  # noqa: BLE001
            return None, type(e).__name__

    def run(self, run_id):
        t0, fails, observing, noted = self.clock(), 0, False, set()
        while True:
            rec, err = self._fetch("tick")
            if err:
                fails += 1
                self.log(f"조회 실패 {fails}/{QUERY_FAIL_LIMIT} ({err}) — 판정하지 않음(0건으로 보지 않음)")
                if fails >= QUERY_FAIL_LIMIT and not observing:
                    observing = True
                    self.log("감시 불능 — N6 은 중단 API 가 없으므로 추가 요청 없이 관찰 전환")
                if self.clock() - t0 > TOTAL_LIMIT_S + OBSERVE_S:
                    return self.finish(run_id, "N6-unsettled", reasons=["조회 실패 지속"])
                self.sleep(TICK)
                continue
            fails = 0
            fs = final_state(rec, run_id)
            if fs == "done":
                return self.finish(run_id, "N6-done", reasons=sorted(noted))
            if fs == "failed-preserved":
                return self.finish(run_id, "N6-render-failed-preserved", reasons=sorted(noted))
            reasons = decide(rec, run_id, self.clock() - t0, self.stop_text())
            new = [r for r in reasons if r not in noted]
            if new:
                noted.update(new)
                self.log("이상 사유(N6 — 중단 API 없음, 추가 요청 없이 관찰·보존): " + " | ".join(new))
                self.state(phase="N6-observing", run_id=run_id, reasons=sorted(noted))
                observing = True
            if self.clock() - t0 > TOTAL_LIMIT_S + (OBSERVE_S if observing else 0):
                return self.finish(run_id, "N6-unsettled", reasons=sorted(noted))
            self.sleep(TICK)

    def finish(self, run_id, phase, reasons):
        self.log(f"감시 종료: {phase} (이상 사유 {len(reasons)}건) — 처리 결과 판정과 시각 품질은 n6_verify.py 로 별도")
        self.state(phase=phase, job_id=JOB_ID, run_id=run_id, reasons=reasons)
        return phase


# ---------------------------------------------------------------- confirm 1회(응답 유실 시 재전송 없음)
def send_confirm(api, db_lookup, max_id_before):
    """반환 (결과, final_run_id). 결과: ok | rejected:<status> | reflected-after-unknown | not-reflected-after-unknown | already-sent
    db_lookup(max_id_before) → (job_step, final_run_id 또는 None)"""
    try:
        fd = os.open(f"{EV}/CONFIRM_SENT", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return "already-sent", None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{now_iso()} confirm 요청 시도권 사용 max_attempt_id_before={max_id_before}" + chr(10))
    res = with_timeout(lambda: api.call("POST", f"/jobs/{JOB_ID}/confirm", timeout=20,
                                        json={"acknowledgedWarnings": []}).status_code, 25)
    append("CONFIRM_RESULT", f"{now_iso()} result={res}")
    if res == 200:
        step, rid = db_lookup(max_id_before)
        return ("ok" if rid else "not-reflected-after-unknown"), rid
    if isinstance(res, int):
        return f"rejected:{res}", None
    step, rid = db_lookup(max_id_before)      # 시간 초과·오류: 다시 보내지 않고 반영 여부만 조회
    if step == "N6" and rid:
        return "reflected-after-unknown", rid
    return "not-reflected-after-unknown", None


# ---------------------------------------------------------------- DB·API
class Db:
    def __init__(self):
        import redis
        from sqlalchemy import create_engine, text
        self.text = text
        self.eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
        self.rds = redis.Redis.from_url(os.environ["CELERY_BROKER_URL"], socket_connect_timeout=10, socket_timeout=10)

    def q(self, sql, **p):
        with self.eng.connect() as c:
            return [dict(r) for r in c.execute(self.text(sql), p).mappings().all()]

    def queues(self):
        return {k: self.rds.llen(k) for k in ("ocr", "cpu", "gpu", "celery")} | {"unacked": self.rds.hlen("unacked")}

    def consistent(self, fn):
        with self.eng.connect() as c:
            c = c.execution_options(isolation_level="REPEATABLE READ")
            with c.begin():
                c.execute(self.text("SET TRANSACTION READ ONLY"))
                return fn(lambda sql, **p: [dict(r) for r in c.execute(self.text(sql), p).mappings().all()])

    def schema_problems(self):
        bad = []
        for t, cols in COLUMNS.items():
            have = {r["column_name"] for r in self.q(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t", t=t)}
            miss = [c for c in cols if c not in have]
            if miss:
                bad.append(f"{t}: {miss}")
        return bad

    def snapshot(self, label):
        def read(q):
            job = q("SELECT status, current_step, user_facing_status FROM job WHERE id = :j", j=JOB_ID)
            if not job:
                raise LookupError("job 없음")
            return {"at": now_iso(), "label": label, "db_now": q("SELECT now() t")[0]["t"], "job": job[0],
                    "attempts": q(f"SELECT {ATT_COLS} FROM job_async_task WHERE job_id = :j ORDER BY id", j=JOB_ID),
                    "handoffs": q("SELECT h.id, h.task_id, h.state, h.outcome, h.received_at, h.adopted_at, left(h.reject_reason, 200) "
                                  "reject_reason FROM task_handoff h WHERE h.job_id = :j ORDER BY h.id", j=JOB_ID),
                    "global_active": q("SELECT job_id, coalesce(stage, run_kind) stage, status, count(*) n FROM job_async_task "
                                       "WHERE job_id <> :j AND status IN ('pending','running') GROUP BY 1,2,3", j=JOB_ID)}
        rec = self.consistent(read)
        rec["queues"] = self.queues()
        jl("watch.jsonl", rec)
        return rec

    def lookup_confirm(self, max_id_before):
        r = self.q("SELECT (SELECT current_step FROM job WHERE id = :j) step, (SELECT max(id) FROM job_async_task WHERE job_id = :j "
                   "AND parent_task_id IS NULL AND run_kind = 'final_render' AND id > :m) rid", j=JOB_ID, m=max_id_before)[0]
        return r["step"], r["rid"]


class Api:
    def __init__(self):
        import httpx
        self.c = httpx.Client(base_url=API, timeout=30)
        self.h = {}

    def call(self, method, path, timeout=30, **kw):
        r = self.c.request(method, path, headers=self.h, timeout=timeout, **kw)
        jl("api.jsonl", {"at": now_iso(), "method": method, "path": path, "status": r.status_code})
        return r

    def login(self):
        r = self.call("POST", "/auth/login", json={"email": EMAIL, "password": ""})
        r.raise_for_status()
        self.h = {"Authorization": "Bearer " + r.json()["accessToken"]}
        return r.json()["seller"]["id"]


def stop_text():
    try:
        with open(f"{EV}/STOP", encoding="utf-8") as f:
            return f.read().strip() or "STOP"
    except FileNotFoundError:
        return None


def with_timeout(fn, sec):
    res = {}

    def go():
        try:
            res["v"] = fn()
        except Exception as e:  # noqa: BLE001
            res["v"] = f"error:{type(e).__name__}"
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(sec)
    return res.get("v", f"timeout:{sec}s")


WATCHER_WAIT_S = 5 * 60


def wait_watcher(clock=time.monotonic, sleep=time.sleep):
    t0 = clock()
    while clock() - t0 < WATCHER_WAIT_S:
        if stop_text():
            return "stop"
        if os.path.exists(f"{EV}/WATCHER_READY"):
            return "ready"
        sleep(2)
    return "timeout"


def check(db):
    """읽기 전용 정리 확인(감시기용, driver 와 같은 판정). N6: 최종 렌더 대표 행은 하위 진행이 없으면 제외(완료 또는 실패 보존)."""
    try:
        with open(f"{EV}/state.json", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        st = {}
    run_id = st.get("run_id")
    rec = db.snapshot("check") if hasattr(db, "snapshot") else None
    ks = kids(rec, run_id) if run_id else []
    act = [a["id"] for a in ks if a["status"] in ("pending", "running")]
    fs = final_state(rec, run_id) if run_id else None
    ds = next((a["status"] for a in rec["attempts"] if a["id"] == DOWNSTREAM_RUN), None)
    res = {"run_id": run_id, "job": f"{rec['job']['status']}/{rec['job']['current_step']}",
           "attempt_ids": [a["id"] for a in rec["attempts"] if run_id and (a["id"] == run_id or a["parent_task_id"] == run_id)],
           "active": act,
           "retries": [a["id"] for a in ks if (a["attempt_no"] or 1) > 1 or a["retry_origin"]],
           "recovery_retries": [a["id"] for a in ks if a["retry_origin"] == "recovery"],
           "unexpected": [a["id"] for a in ks if a["stage"] not in STAGES or a["unit_id"] not in UNITS],
           "after_abort": [], "lease_states": {str(a["id"]): lease_state(a, rec["handoffs"], rec["db_now"])[0] for a in ks if a["status"] == "running"},
           "final_state": fs, "downstream_run": ds,
           "open_handoffs": sum(1 for h in rec["handoffs"] if h["state"] in OPEN_HANDOFF),
           "queues": rec["queues"], "other_active": sum(g["n"] for g in rec["global_active"]),
           "abort_sent": False}
    jl("check.jsonl", {"at": now_iso(), **res})
    print("CHECK " + json.dumps(res, default=str), flush=True)
    return 0


# ---------------------------------------------------------------- 사전 확인(읽기 전용)
def precheck(db, api):
    ok = True

    def fail(m):
        nonlocal ok
        ok = False
        out("precheck FAIL: " + m)

    bad = db.schema_problems()
    out(f"precheck 스키마 열: {'모두 있음' if not bad else bad}")
    if bad:
        fail("스키마 열 불일치")
        return False
    rec = db.snapshot("precheck")
    out(f"precheck 작업: {rec['job']} 큐 {rec['queues']} 다른 작업 {rec['global_active']}")
    if (rec["job"]["status"], rec["job"]["current_step"]) != ("review", "N5"):
        fail("작업이 review/N5 아님")
    ds = next((a for a in rec["attempts"] if a["id"] == DOWNSTREAM_RUN), None)
    ds_kids = kids(rec, DOWNSTREAM_RUN)
    out(f"precheck 하류 실행 #{DOWNSTREAM_RUN}: {ds and ds['status']} 하위 {len(ds_kids)}건 상태 {sorted({a['status'] for a in ds_kids})}")
    if not ds or ds["status"] != "running" or any(a["status"] != "done" for a in ds_kids if a["is_current"]):
        fail("하류 실행이 running·하위 현재 시도 모두 done 이 아님")
    other_live = [a["id"] for a in rec["attempts"] if a["status"] in ("pending", "running") and a["id"] != DOWNSTREAM_RUN]
    if other_live or rec["global_active"] or any(rec["queues"].values()):
        fail(f"하류 대표 행 외 진행 중 행 {other_live}·다른 작업·큐 0 아님")
    if [h for h in rec["handoffs"] if h["state"] in OPEN_HANDOFF]:
        fail("열린 인계 있음")
    fr = [a for a in rec["attempts"] if a["run_kind"] == "final_render"]
    dl = db.q("SELECT count(*) n FROM deliverable WHERE job_id = :j", j=JOB_ID)[0]["n"]
    out(f"precheck 기존 최종 렌더 실행 {len(fr)}건, 산출물 {dl}건")
    if fr or dl:
        fail("이미 최종 렌더 실행 또는 산출물이 있음")
    d = decide(rec, -1, 0, None)
    out(f"precheck 판정 함수(실제 행, run=-1): {d} / 종료 상태 {final_state(rec, -1)} / 정리 {settled(rec, -1)}")
    if d != ["S1 최종 렌더 실행 #-1 없음"] or final_state(rec, -1) is not None or not settled(rec, -1):
        fail("판정 함수가 실제 행에서 예상과 다름")
    seller = api.login()
    out(f"precheck 로그인 seller={seller} (기존)")
    r = api.call("GET", f"/jobs/{JOB_ID}/blocks")
    sig = []
    if r.status_code == 200:
        body = r.json()
        items = body if isinstance(body, list) else (body.get("blocks") or body.get("items") or [])
        sig = [s.get("code") for b in items for s in (b.get("signals") or [])]
    out(f"precheck GET blocks {r.status_code}, 번역하지 못한 칸 경고 {sig.count('translation_failed')}건")
    if r.status_code != 200 or "translation_failed" in sig:
        fail("미해결 경고가 있거나 블록 조회 실패(acknowledgedWarnings=[] 로 확정할 수 없음)")
    for p in (f"/jobs/{JOB_ID}/deliverables", f"/jobs/{JOB_ID}/tasks"):
        out(f"precheck GET {p} {api.call('GET', p).status_code}")
    out(f"precheck 결과: {'충족' if ok else '불충족'}")
    return ok


# ---------------------------------------------------------------- 실행
def run(db, api):
    if not precheck(db, api):
        put_state(phase="precheck-failed")
        return 3
    put_state(phase="waiting-watcher", job_id=JOB_ID)
    out("감시기 준비 대기(WATCHER_READY) — 확인 전에는 confirm 을 보내지 않는다")
    w = wait_watcher()
    if w != "ready":
        out(f"감시기 준비 안 됨({w}) — confirm 을 보내지 않고 종료")
        put_state(phase=f"not-started-{w}", job_id=JOB_ID)
        return 3
    rec = db.snapshot("pre-confirm")
    other_live = [a["id"] for a in rec["attempts"] if a["status"] in ("pending", "running") and a["id"] != DOWNSTREAM_RUN]
    if other_live or rec["global_active"] or any(rec["queues"].values()) or rec["job"]["current_step"] != "N5":
        out(f"발행 직전 재확인 실패({other_live}, {rec['global_active']}, {rec['queues']}, {rec['job']}) — confirm 을 보내지 않음")
        put_state(phase="not-started-busy", job_id=JOB_ID)
        return 3
    max_before = max(a["id"] for a in rec["attempts"])
    out(f"감시기 준비 확인 + 발행 직전 큐·진행 0 → POST /v1/jobs/{JOB_ID}/confirm 1회(acknowledgedWarnings=[])")
    result, rid = send_confirm(api, db.lookup_confirm, max_before)
    out(f"confirm 결과: {result} → 최종 렌더 실행 {rid}")
    if not rid:
        put_state(phase=f"confirm-{result}", job_id=JOB_ID)
        return 5
    put_state(phase="N6", job_id=JOB_ID, run_id=rid, confirm=result)
    mon = Monitor(fetch=lambda label: db.snapshot(label), stop_text=stop_text, log=out, state=put_state)
    phase = mon.run(rid)
    if phase == "N6-done":
        r = api.call("GET", f"/jobs/{JOB_ID}/deliverables")
        n = len((r.json() or {}).get("deliverables") or []) if r.status_code == 200 else None
        out(f"사후 GET deliverables {r.status_code}, 산출물 {n}건(URL 은 기록하지 않음)")
    return {"N6-done": 0, "N6-render-failed-preserved": 2}.get(phase, 2)


if __name__ == "__main__":
    os.umask(0o077)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("precheck", "run", "check", "abort"):
        sys.exit("usage: driver_n6.py precheck|run|check|abort")
    if mode == "abort":   # N6 에는 중단 API 가 없다 — 요청을 보내지 않고 그 사실만 알린다(감시기 호환)
        out("감시기 중단 요청 수신 — N6 은 /abort 불가, 요청 보내지 않음(상태 보존)")
        print("ABORT unavailable-N6", flush=True)
        sys.exit(0)
    try:
        d, a = Db(), Api()
        if mode == "check":
            sys.exit(check(d))
        sys.exit((0 if precheck(d, a) else 3) if mode == "precheck" else run(d, a))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        out(f"driver 예외 {type(e).__name__}")
        put_state(phase="driver-error", error=type(e).__name__)
        sys.exit(6)
