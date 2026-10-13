"""통합 검증 N4 — 작업 9(N3)에서 POST /v1/jobs/9/sections/proceed 1회 → N4 감시 → N5 에서 멈춤.
confirm·최종 렌더·수동 재시도·추가 작업·정리 없음. 스타일 기본값·앱 코드는 바꾸지 않는다.

모드: precheck(읽기 전용) | run | check(읽기 전용 정리 확인 한 줄) | abort(단발, ABORT_SENT 있으면 안 보냄)

감시 대상은 이번 하류 실행(run)뿐이다. 대표 행 제외는 '이번 run 의 대표 행'에만 적용한다(N5 에서도 running 으로 남는 것이 정상).
다른 작업의 행은 대표 행이든 시도 행이든 모두 '다른 작업 유입'으로 본다.
정상 후속 시도(label → logo → inpaint·style·translate → preview, attempt_no 1, 재시도 출처 없음, 포함 섹션)는 허용하고,
재시도·예상 밖 단계·예상 밖 단위만 이상으로 본다.

N5 도달·감시 종료와 검증 성공은 분리한다. preview 실패(STYLE_DEFAULTS_UNAPPROVED 등)여도 N5 로 넘어가므로 감시는 끝나지만
판정은 n5_verify.py 가 단계별로 따로 내린다. /abort 가 409 이고 작업이 N5 면 '더 중단하지 않고 잔여 작업을 확인'한다.
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
ANALYSIS_RUN = 22
SECTIONS = {10, 11}
BLOCKS = 7
TICK = 10
STAGES = {"label", "logo", "inpaint", "style", "translate", "preview"}
STAGE_LIMIT_S = {"label": 300, "logo": 120, "style": 180, "translate": 300, "preview": 300, "inpaint": 600}
PENDING_LIMIT_S = 300
TOTAL_LIMIT_S = 30 * 60
QUERY_FAIL_LIMIT = 3
POST_ABORT_WATCH_S = 10 * 60
ATT_COLS = ("id, parent_task_id, run_kind, stage, unit_id, status, attempt_no, is_current, retry_count, max_retry, "
            "retry_origin, error_code, dispatch_count, last_dispatched_at, started_at, finished_at, heartbeat_at, "
            "lease_expires_at, lease_owner IS NOT NULL AS leased, cancelled_at")
COLUMNS = {
    "job": ["id", "seller_id", "status", "current_step", "user_facing_status", "current_analysis_task_id"],
    "job_async_task": ["id", "job_id", "parent_task_id", "run_kind", "stage", "unit_id", "status", "attempt_no", "is_current",
                       "retry_count", "max_retry", "retry_origin", "error_code", "dispatch_count", "last_dispatched_at",
                       "started_at", "finished_at", "heartbeat_at", "lease_expires_at", "lease_owner", "cancelled_at"],
    "task_handoff": ["id", "task_id", "job_id", "lease_epoch", "outcome", "skip_reason", "target_count", "state", "late",
                     "received_at", "adopted_at", "reject_reason", "payload"],
    "task_lease_grant": ["id", "task_id", "generation_epoch", "issued_at", "closed_at", "close_reason"],
    "task_artifact": ["id", "handoff_id", "job_id", "kind", "part_key", "verified_key", "source_ref", "sha256",
                      "declared_sha256", "byte_size", "width", "height", "state"],
    "section": ["id", "job_id", "bucket", "bbox", "height", "image_key", "inpaint_status", "inpaint_image_url",
                "mask_image_url", "warning_badge", "render_image_key", "analysis_task_id"],
    "text_block": ["id", "section_id", "block_order", "role", "is_product_label", "is_brand_logo", "is_excluded", "style",
                   "trans_1", "block_status", "overflow", "source_ko"],
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(name, line):
    with open(f"{EV}/{name}", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def jl(name, obj):
    append(name, json.dumps(obj, ensure_ascii=False, default=str))


def out(msg):
    print(f"[n4 {now_iso()}] {msg}", flush=True)
    append("progress.log", f"{now_iso()} {msg}")


def put_state(**kw):
    tmp = f"{EV}/.state.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": now_iso(), **kw}, f, ensure_ascii=False, default=str)
    os.replace(tmp, f"{EV}/state.json")


# ---------------------------------------------------------------- 순수 판정
def mine(rec, run_id):
    return [a for a in rec["attempts"] if a["parent_task_id"] == run_id]


OPEN_HANDOFF = ("received", "verifying", "verified")
ADOPT_WAIT_S = 300   # 감시 기준(제안값): 인계 수신 시각부터 채택까지. GPU 계산 시작 시각과 구분한다


def lease_state(a, handoffs, db_now):
    """running 시도의 권한 상태 — driver(decide)와 check 가 같은 판정을 쓴다. 복구 코드(app/recovery.py)와 같은 기준.
    수신·검증 인계(received·verifying·verified)가 있으면 권한 유효 여부와 상관없이 '채택 단계'다:
      원격 인계 직후에는 권한이 만료(now 로 설정)돼 있고, cpu 채택자가 인수하면(claim_adoption) 새 유효 권한을 받고 verifying 이 된다.
      어느 쪽이든 계산 단계로 되돌아간 것이 아니므로 계산 시간 제한을 적용하지 않고, 채택 지연은 인계 수신 시각부터 잰다.
      adoption_wait     수신·검증 인계 있음, 수신 후 ADOPT_WAIT_S 이내(권한 만료 상태든 채택자 유효 권한이든)
      adoption_delayed  위와 같고 수신 후 ADOPT_WAIT_S 초과
    인계가 없을 때(계산 단계):
      valid             계산 권한 유효(lease_expires_at > now) → 계산 단계 제한 시간 적용
      expired           권한 만료(NULL 포함) → 계산 권한 만료(rejected·adopted 인계는 대기 상태로 치지 않음)
    반환: (상태, 정보)"""
    open_h = [h for h in handoffs if h["task_id"] == a["id"] and h["state"] in OPEN_HANDOFF]
    if open_h:
        h = max(open_h, key=lambda x: x["id"])
        wait = (db_now - h["received_at"]).total_seconds() if h.get("received_at") else 0
        exp = a["lease_expires_at"]
        info = {"handoff": h["id"], "state": h["state"], "wait_s": wait,
                "adopter_lease_valid": exp is not None and exp > db_now}
        return ("adoption_delayed" if wait > ADOPT_WAIT_S else "adoption_wait"), info
    exp = a["lease_expires_at"]
    if exp is not None and exp > db_now:
        return "valid", None
    return "expired", [(h["id"], h["state"]) for h in handoffs if h["task_id"] == a["id"]]


def decide(rec, run_id, elapsed_s, stop_text=None):
    reasons, db_now = [], rec["db_now"]
    for a in mine(rec, run_id):
        tag = f"#{a['id']}({a['stage']}/{a['unit_id']})"
        if a["stage"] not in STAGES:
            reasons.append(f"S1 예상 밖 단계 {tag}")
        if a["unit_id"] not in SECTIONS:
            reasons.append(f"S1 예상 밖 단위 {tag}")
        if a["status"] == "failed":
            reasons.append(f"S1 실패 {tag} {a['error_code']}")
        if (a["attempt_no"] or 1) > 1 or a["retry_origin"]:
            reasons.append(f"S1 재시도 {tag} attempt_no={a['attempt_no']} origin={a['retry_origin']}")
        if a["status"] == "running":
            ls, info = lease_state(a, rec["handoffs"], db_now)
            if ls == "expired":
                reasons.append(f"S2 계산 권한 만료(수신·검증 인계 없음) {tag} lease_expires_at={a['lease_expires_at']} db_now={db_now} "
                               f"heartbeat={a['heartbeat_at']} 다른 인계={info}")
            elif ls == "adoption_delayed":
                reasons.append(f"S2 인계 채택 지연 {tag} 인계 #{info['handoff']}({info['state']}) 수신 후 {info['wait_s']:.0f}s > {ADOPT_WAIT_S}s")
            if ls in ("valid", "expired"):   # 계산 단계 제한은 인계 전까지만(인계 뒤는 채택 대기 기준으로 본다)
                lim = STAGE_LIMIT_S.get(a["stage"])
                if lim and a["started_at"] and (db_now - a["started_at"]).total_seconds() > lim:
                    reasons.append(f"S2 단계 제한 시간 {tag} {lim}s")
        if a["status"] == "pending" and a["started_at"] is None and a["last_dispatched_at"] is not None:
            if (db_now - a["last_dispatched_at"]).total_seconds() > PENDING_LIMIT_S:
                reasons.append(f"S2 pending 미시작 {tag} >{PENDING_LIMIT_S}s dispatch_count={a['dispatch_count']}")
    run = next((a for a in rec["attempts"] if a["id"] == run_id), None)
    if run is None:
        reasons.append(f"S1 감시 대상 실행 #{run_id} 없음")
    elif run["status"] in ("failed", "cancelled"):
        reasons.append(f"S1 실행 #{run_id} {run['status']}")
    if elapsed_s > TOTAL_LIMIT_S:
        reasons.append(f"S2 N4 전체 제한 시간 {TOTAL_LIMIT_S}s 초과")
    if rec["job"]["status"] == "failed":
        reasons.append(f"S1 작업 failed {rec['job']['current_step']}")
    if rec["global_active"]:
        reasons.append(f"S3 다른 작업 유입 {rec['global_active']} — 이번 검증 중단·판정 보류(다른 작업은 건드리지 않음)")
    if stop_text:
        reasons.append("S4 로컬 감시기 요청: " + stop_text[:200])
    return reasons


def reached_n5(rec, run_id):
    if rec["job"]["current_step"] != "N5":
        return None
    cur = [a for a in mine(rec, run_id) if a["is_current"]]
    return {"statuses": sorted({a["status"] for a in cur}), "all_done": all(a["status"] == "done" for a in cur),
            "failed": [(a["id"], a["stage"], a["unit_id"], a["error_code"]) for a in cur if a["status"] == "failed"]}


def settled(rec, run_id):
    """이번 run 의 하위 시도 pending·running 0 + 큐·unacked 0 + 이 작업 열린 인계 0.
    대표 행은 작업이 N5 일 때만 제외한다(N5 에서 running 이 정상). N4·중단 실패 상태에서 대표 행이 running 이면 진행 중으로 본다."""
    active = [a["id"] for a in mine(rec, run_id) if a["status"] in ("pending", "running")]
    if rec["job"]["current_step"] != "N5":
        active += [a["id"] for a in rec["attempts"] if a["id"] == run_id and a["status"] in ("pending", "running")]
    open_h = [h["id"] for h in rec["handoffs"] if h["state"] in ("received", "verifying", "verified")]
    return not active and not open_h and not any(rec["queues"].values())


# ---------------------------------------------------------------- 감시 루프
class Monitor:
    def __init__(self, fetch, abort, stop_text, clock=time.monotonic, sleep=time.sleep, log=None, state=None):
        self.fetch, self.abort_fn, self.stop_text = fetch, abort, stop_text
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda m: None)
        self.state = state or (lambda **kw: None)
        self.abort_calls = 0

    def _fetch(self, label):
        try:
            return self.fetch(label), None
        except Exception as e:  # noqa: BLE001
            return None, type(e).__name__

    def run(self, job_id, run_id):
        t0, fails = self.clock(), 0
        while True:
            rec, err = self._fetch("tick")
            if err:
                fails += 1
                self.log(f"조회 실패 {fails}/{QUERY_FAIL_LIMIT} ({err}) — 이 틱은 판정하지 않음(0건으로 보지 않음)")
                if fails >= QUERY_FAIL_LIMIT:
                    return self.stop(job_id, run_id, [f"감시 불능: 조회 {QUERY_FAIL_LIMIT}회 연속 실패({err})"], rec=None)
                self.sleep(TICK)
                continue
            fails = 0
            n5 = reached_n5(rec, run_id)
            if n5 is not None:
                return self.at_n5(job_id, run_id, n5, note="N5 도달")
            reasons = decide(rec, run_id, self.clock() - t0, self.stop_text())
            if reasons:
                # 중단 직전 재확인: 새 스냅샷(시도·인계 한 트랜잭션)으로 다시 판정한다. 정상 전환(예: 인계 막 등록)이면 중단하지 않는다
                rec2, err2 = self._fetch("pre-abort-recheck")
                if err2:
                    # 보수적 정책: 재확인하지 못하면 안전 중단. 사유는 '처음 판정 확정'이 아니라 '재확인 실패'로 기록한다
                    self.log(f"중단 직전 재확인 조회 실패({err2})")
                    return self.stop(job_id, run_id, [f"중단 직전 재확인 실패—안전 중단(재확인 오류 {err2}; 처음 판정, 미확정: "
                                                      f"{' | '.join(reasons)})"], rec=rec)
                n5 = reached_n5(rec2, run_id)
                if n5 is not None:
                    return self.at_n5(job_id, run_id, n5, note="재확인 시점에 N5 도달")
                reasons2 = decide(rec2, run_id, self.clock() - t0, self.stop_text())
                if not reasons2:
                    self.log(f"중단 직전 재확인에서 사유 해소 — 중단하지 않음(처음 사유: {' | '.join(reasons)})")
                    self.sleep(TICK)
                    continue
                return self.stop(job_id, run_id, reasons2, rec=rec2)
            self.sleep(TICK)

    def at_n5(self, job_id, run_id, n5, note):
        """N5: 더 중단하지 않는다. 잔여 작업 정리를 확인하고 끝낸다(판정은 n5_verify.py)."""
        self.log(f"{note}: 현재 시도 상태={n5['statuses']} 모두 done={n5['all_done']} 실패={n5['failed']} — 감시 종료와 검증 판정은 별개")
        self.state(phase="N5", job_id=job_id, run_id=run_id, all_done=n5["all_done"], failed=n5["failed"], note=note)
        t0 = self.clock()
        while self.clock() - t0 < POST_ABORT_WATCH_S:
            chk, err = self._fetch("n5-settle")
            if err:
                self.log(f"N5 정리 확인 조회 실패({err}) — 정리로 보지 않음")
            elif settled(chk, run_id):
                self.log("N5 잔여 작업 정리 확인(이번 run 하위 진행 0·큐 0·열린 인계 0)")
                self.state(phase="N5-settled", job_id=job_id, run_id=run_id, all_done=n5["all_done"], failed=n5["failed"])
                return "N5-settled"
            self.sleep(TICK)
        self.state(phase="N5-unsettled", job_id=job_id, run_id=run_id)
        return "N5-unsettled"

    def stop(self, job_id, run_id, reasons, rec):
        self.log("중단 판정: " + " | ".join(reasons))
        if rec is None:
            rec, err = self._fetch("pre-abort")
            if err:
                self.log(f"중단 전 증거 조회 실패({err}) — 중단 요청은 계속")
        max_id = max((a["id"] for a in rec["attempts"]), default=0) if rec else None
        self.abort_calls += 1
        got = self.abort_fn(max_id)
        res, resp_max = got if isinstance(got, tuple) else (got, None)
        self.log(f"POST /abort → {res}, 응답 시점 최대 시도 id={resp_max} (이미 시작된 모델 요청·비용은 취소 보장 없음)")
        known = {a["id"] for a in rec["attempts"]} if rec else None
        if res == 409:
            chk, err = self._fetch("abort-409")
            if not err:
                n5 = reached_n5(chk, run_id)
                if n5 is not None:
                    return self.at_n5(job_id, run_id, n5, note="중단 요청 시점에 이미 N5(409) — 더 중단하지 않고 잔여 작업 확인")
        phase = "aborted" if res == 200 else "abort-failed"
        self.state(phase=phase, job_id=job_id, run_id=run_id, reasons=reasons, abort=res)
        t0 = self.clock()
        while self.clock() - t0 < POST_ABORT_WATCH_S:
            chk, err = self._fetch("post-abort")
            if err:
                self.log(f"중단 후 조회 실패({err}) — 정착으로 보지 않음")
            else:
                if known is None:
                    known = {a["id"] for a in chk["attempts"]}
                if resp_max is None:
                    resp_max = max((a["id"] for a in chk["attempts"]), default=0)   # 응답 직후 첫 조회로 대신
                new = [a for a in chk["attempts"] if a["id"] not in known]
                window = [a["id"] for a in new if a["id"] <= resp_max]              # 요청~응답 사이(정상 후속일 수 있음)
                after_resp = [a["id"] for a in new if a["id"] > resp_max]            # 응답 이후 생성
                live = [a["id"] for a in new if a["status"] in ("pending", "running")]
                if window and not getattr(self, "_win_logged", False):
                    self._win_logged = True
                    self.log(f"중단 요청 이후 관측된 새 시도(요청~응답 사이) {window} — 상태 "
                             f"{[(a['id'], a['status']) for a in new if a['id'] in window]}")
                if after_resp or (res != 200 and new) or (res == 200 and live):
                    if after_resp:
                        why = f"응답 이후 생성 {after_resp}"
                    elif res != 200:
                        why = f"중단 요청 실패({res}) 뒤 새 시도 {[a['id'] for a in new]}"
                    else:
                        why = f"중단 응답 뒤에도 진행 중인 새 시도 {live}"
                    self.log(f"중단 요청 이후 관측된 새 시도 — {why} — 통제 불가(보수적 정지, 취소 로직 결함 판정은 별도)")
                    self.state(phase="uncontrolled", job_id=job_id, run_id=run_id, new_attempts=[a["id"] for a in new],
                               window=window, after_response=after_resp, live=live)
                    return "uncontrolled"
                if settled(chk, run_id):
                    self.log("중단 뒤 이번 run(대표 행 포함) 진행 0, 큐 0, 열린 인계 0")
                    self.state(phase=f"{phase}-settled", job_id=job_id, run_id=run_id)
                    return f"{phase}-settled"
            self.sleep(TICK)
        self.log("중단 뒤 제한 시간 동안 정착 확인 못 함 — 보고 필요")
        self.state(phase=f"{phase}-unsettled", job_id=job_id, run_id=run_id)
        return f"{phase}-unsettled"


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

    def schema_problems(self):
        bad = []
        for t, cols in COLUMNS.items():
            have = {r["column_name"] for r in self.q(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t", t=t)}
            miss = [c for c in cols if c not in have]
            if miss:
                bad.append(f"{t}: {miss}")
        return bad

    def consistent(self, fn):
        """한 REPEATABLE READ 읽기 전용 트랜잭션 안에서 fn(q) 를 실행한다 — 시도·인계·now() 가 같은 스냅샷이다."""
        with self.eng.connect() as c:
            c = c.execution_options(isolation_level="REPEATABLE READ")
            with c.begin():
                c.execute(self.text("SET TRANSACTION READ ONLY"))
                return fn(lambda sql, **p: [dict(r) for r in c.execute(self.text(sql), p).mappings().all()])

    def snapshot(self, job_id, label):
        def read(q):
            job = q("SELECT status, current_step, user_facing_status FROM job WHERE id = :j", j=job_id)
            if not job:
                raise LookupError(f"job {job_id} 없음")
            return {"at": now_iso(), "label": label, "db_now": q("SELECT now() t")[0]["t"], "job": job[0],
                    "attempts": q(f"SELECT {ATT_COLS} FROM job_async_task WHERE job_id = :j ORDER BY id", j=job_id),
                    "handoffs": q("SELECT h.id, h.task_id, h.lease_epoch, h.outcome, h.skip_reason, h.target_count, h.state, h.late, "
                                  "h.received_at, h.adopted_at, left(h.reject_reason, 200) reject_reason FROM task_handoff h "
                                  "WHERE h.job_id = :j ORDER BY h.id", j=job_id),
                    "grants": q("SELECT g.id, g.task_id, g.generation_epoch, g.issued_at, g.closed_at, g.close_reason "
                                "FROM task_lease_grant g JOIN job_async_task a ON a.id = g.task_id WHERE a.job_id = :j ORDER BY g.id",
                                j=job_id),
                    # 다른 작업: 대표 행·시도 행 구분 없이 pending·running 전부
                    "global_active": q("SELECT job_id, coalesce(stage, run_kind) stage, status, count(*) n FROM job_async_task "
                                       "WHERE job_id <> :j AND status IN ('pending','running') GROUP BY 1,2,3 ORDER BY 1,2,3", j=job_id)}
        rec = self.consistent(read)
        rec["queues"] = self.queues()   # Redis 는 DB 스냅샷 밖(큐 판정은 정리 확인에만 쓴다)
        jl("watch.jsonl", rec)
        return rec


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


def abort_once(api, who, max_id, after=None):
    """/abort 단발. ABORT_SENT 는 O_EXCL — '요청 시도권 사용' 표시(성공 아님). 요청 시각·요청 시점 최대 시도 id 를 남긴다.
    응답 뒤에는 응답 시각·결과·응답 시점 최대 시도 id·대표 실행 상태를 ABORT_RESULT 에 남긴다(after() → (max_id, run_status)).
    요청~응답 사이에 생긴 시도는 정상 후속일 수 있다. '중단 요청 이후 관측된 새 시도'와 '응답 이후 생성'을 구분하는 근거다.
    반환: (결과, 응답 시점 최대 id). 이미 보냈으면 ("already-sent", None)."""
    try:
        fd = os.open(f"{EV}/ABORT_SENT", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return "already-sent", None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{now_iso()} 요청 시도권 사용: {who} max_attempt_id={max_id}" + chr(10))
    res = with_timeout(lambda: api.call("POST", f"/jobs/{JOB_ID}/abort", timeout=10).status_code, 12)
    resp_max, run_status = None, None
    if after:
        try:
            resp_max, run_status = after()
        except Exception as e:  # noqa: BLE001
            run_status = f"error:{type(e).__name__}"
    append("ABORT_RESULT", f"{now_iso()} {who} result={res} max_attempt_id_after_response={resp_max} run_status={run_status}")
    return res, resp_max


def _read_int(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().split(key + "=")[1].split()[0]
        return None if v == "None" else int(v)
    except (FileNotFoundError, IndexError, ValueError):
        return None


def abort_max_id():
    return _read_int(f"{EV}/ABORT_SENT", "max_attempt_id")


def abort_resp_max_id():
    return _read_int(f"{EV}/ABORT_RESULT", "max_attempt_id_after_response")


def check(db):
    """읽기 전용 정리 확인(감시기용). 이번 run 대표 행만 제외, 다른 작업 행은 모두 센다."""
    try:
        with open(f"{EV}/state.json", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        st = {}
    run_id = st.get("run_id")

    def read(q):   # 시도·인계·now()·다른 작업을 한 스냅샷에서 읽는다(driver 와 같은 판정 lease_state 사용)
        return {"job": q("SELECT status, current_step FROM job WHERE id = :j", j=JOB_ID)[0],
                "now": q("SELECT now() t")[0]["t"],
                "rows": q("SELECT id, parent_task_id, stage, unit_id, status, attempt_no, retry_origin, lease_expires_at "
                          "FROM job_async_task WHERE job_id = :j AND (id = :r OR parent_task_id = :r) ORDER BY id", j=JOB_ID, r=run_id or -1),
                "handoffs": q("SELECT id, task_id, state, received_at FROM task_handoff WHERE job_id = :j ORDER BY id", j=JOB_ID),
                "other": q("SELECT count(*) n FROM job_async_task WHERE job_id <> :j AND status IN ('pending','running')", j=JOB_ID)[0]["n"]}
    s = db.consistent(read)
    job, rows = s["job"], s["rows"]
    kids = [r for r in rows if r["parent_task_id"] == run_id]
    lease = {r["id"]: lease_state(r, s["handoffs"], s["now"])[0] for r in kids if r["status"] == "running"}
    mx, rmx = abort_max_id(), abort_resp_max_id()
    res = {"run_id": run_id, "job": f"{job['status']}/{job['current_step']}",
           "attempt_ids": [r["id"] for r in rows],
           # 대표 행은 작업이 N5 일 때만 제외(N4·중단 실패 상태의 running 대표 행은 진행 중)
           "active": [r["id"] for r in rows if r["status"] in ("pending", "running")
                      and (r["parent_task_id"] == run_id or job["current_step"] != "N5")],
           "retries": [r["id"] for r in kids if (r["attempt_no"] or 1) > 1 or r["retry_origin"]],
           "unexpected": [r["id"] for r in kids if r["stage"] not in STAGES or r["unit_id"] not in SECTIONS],
           "after_request": [r["id"] for r in kids if mx is not None and r["id"] > mx],
           "after_response": [r["id"] for r in kids if rmx is not None and r["id"] > rmx],
           "after_request_live": [r["id"] for r in kids if mx is not None and r["id"] > mx and r["status"] in ("pending", "running")],
           # 감시기 비상 기준: 응답 이후 생성 ∪ 요청 이후 생겼는데 아직 진행 중(요청~응답 사이 생겨 취소된 정상 후속은 제외)
           "after_abort": sorted({r["id"] for r in kids if (rmx is not None and r["id"] > rmx)
                                  or (mx is not None and r["id"] > mx and r["status"] in ("pending", "running"))}),
           "open_handoffs": sum(1 for h in s["handoffs"] if h["state"] in OPEN_HANDOFF),
           # running 시도별 권한 상태(driver 와 같은 판정): valid / adoption_wait / adoption_delayed / expired
           "lease_states": {str(k): v for k, v in lease.items()},
           "expired": [k for k, v in lease.items() if v == "expired"],
           "adoption_delayed": [k for k, v in lease.items() if v == "adoption_delayed"],
           "queues": db.queues(),
           "other_active": s["other"],
           "abort_sent": os.path.exists(f"{EV}/ABORT_SENT")}
    jl("check.jsonl", {"at": now_iso(), **res})
    print("CHECK " + json.dumps(res, default=str), flush=True)
    return 0


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
    qs = db.queues()
    act = db.q("SELECT count(*) n FROM job_async_task WHERE status IN ('pending','running')")[0]["n"]
    ho = db.q("SELECT count(*) n FROM task_handoff WHERE state IN ('received','verifying','verified')")[0]["n"]
    out(f"precheck queues={qs} 전체 pending·running 행(대표 행 포함)={act} open_handoffs={ho}")
    if any(qs.values()) or act or ho:
        fail("큐·진행 중 행·열린 인계 0 아님")
    job = db.q("SELECT status, current_step, current_analysis_task_id FROM job WHERE id = :j", j=JOB_ID)
    out(f"precheck 작업 {JOB_ID}: {job}")
    if not job or (job[0]["status"], job[0]["current_step"], job[0]["current_analysis_task_id"]) != ("review", "N3", ANALYSIS_RUN):
        fail(f"작업 상태가 review/N3·분석 run {ANALYSIS_RUN} 아님")
    ds = db.q("SELECT id, status FROM job_async_task WHERE job_id = :j AND run_kind = 'downstream' ORDER BY id", j=JOB_ID)
    out(f"precheck 기존 하류 실행: {ds}")
    if [d for d in ds if d["status"] in ("pending", "running")]:
        fail("진행 중 하류 실행 있음")
    secs = db.q("SELECT id, bucket, image_key IS NOT NULL has_key FROM section WHERE job_id = :j AND analysis_task_id = :r ORDER BY id",
                j=JOB_ID, r=ANALYSIS_RUN)
    nb = db.q("SELECT count(*) n FROM text_block b JOIN section s ON s.id = b.section_id WHERE s.job_id = :j AND s.analysis_task_id = :r",
              j=JOB_ID, r=ANALYSIS_RUN)[0]["n"]
    simg = db.q("SELECT kind, state, count(*) n FROM task_artifact WHERE job_id = :j AND kind = 'section_image' AND state = 'adopted' "
                "AND handoff_id IN (SELECT h.id FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.parent_task_id = :r) "
                "GROUP BY 1,2", j=JOB_ID, r=ANALYSIS_RUN)
    out(f"precheck 섹션 {[(s['id'], s['bucket'], s['has_key']) for s in secs]} 블록 {nb} 섹션 이미지 채택 {simg}")
    if {s["id"] for s in secs} != SECTIONS or any(s["bucket"] != "include" or not s["has_key"] for s in secs) or nb != BLOCKS:
        fail("섹션 10·11 include·이미지 키·블록 7개 조건 불일치")
    if not simg or simg[0]["n"] != len(SECTIONS):
        fail("섹션 이미지 채택 기록이 섹션 수와 다름(proceed 500 위험)")
    brand = db.q("SELECT b.name_ko, b.name_en FROM brand b JOIN job j ON j.brand_id = b.id WHERE j.id = :j", j=JOB_ID)
    out(f"precheck 브랜드명 비어 있지 않음: {bool(brand and (brand[0]['name_ko'] or '').strip() and (brand[0]['name_en'] or '').strip())}")
    # 감시·판정 함수를 실제 행에 적용(하류 실행이 없으므로 run=-1 → '실행 없음' 판정이어야 한다)
    try:
        db.snapshot(-1, "precheck-missing")
        fail("없는 작업 id 조회가 예외 없이 끝남")
    except LookupError:
        out("precheck 없는 작업 id: '작업 없음'으로 감지")
    rec = db.snapshot(JOB_ID, "precheck-real")
    d = decide(rec, -1, 0, None)
    out(f"precheck 판정 함수(작업 9 실제 행, 하류 run 없음): {d}")
    if d != ["S1 감시 대상 실행 #-1 없음"]:
        fail("판정 함수가 실제 행에서 예상과 다름(다른 작업 유입 또는 이상 행 가능)")
    out(f"precheck N5 판정: {reached_n5(rec, -1)} / 정리 판정(run 없음): {settled(rec, -1)}")
    if reached_n5(rec, -1) is not None or not settled(rec, -1):
        fail("N5·정리 판정이 예상과 다름")
    seller_id = api.login()
    out(f"precheck 로그인: seller={seller_id} (기존, DB 쓰기 없음)")
    for p in (f"/jobs/{JOB_ID}", f"/jobs/{JOB_ID}/tasks", f"/jobs/{JOB_ID}/sections", f"/jobs/{JOB_ID}/blocks", f"/jobs/{JOB_ID}/preview"):
        r = api.call("GET", p)
        out(f"precheck GET {p} {r.status_code}")
    out(f"precheck 결과: {'충족' if ok else '불충족'}")
    return ok


# ---------------------------------------------------------------- 실행
def run(db, api):
    if not precheck(db, api):
        put_state(phase="precheck-failed")
        return 3
    put_state(phase="waiting-watcher", job_id=JOB_ID)
    out("감시기 준비 대기(WATCHER_READY) — 확인 전에는 proceed 를 보내지 않는다")
    w = wait_watcher()
    if w != "ready":
        out(f"감시기 준비 안 됨({w}) — proceed 를 보내지 않고 종료")
        put_state(phase=f"not-started-{w}", job_id=JOB_ID)
        return 3
    act = db.q("SELECT count(*) n FROM job_async_task WHERE status IN ('pending','running')")[0]["n"]
    if act or any(db.queues().values()):
        out(f"발행 직전 재확인 실패(진행 중 행 {act}, 큐 {db.queues()}) — proceed 를 보내지 않음")
        put_state(phase="not-started-busy", job_id=JOB_ID)
        return 3
    out(f"감시기 준비 확인 + 발행 직전 큐·진행 0 → POST /v1/jobs/{JOB_ID}/sections/proceed 1회")
    r = api.call("POST", f"/jobs/{JOB_ID}/sections/proceed")
    body = r.json() if r.status_code == 202 else {}
    out(f"proceed {r.status_code}")
    if r.status_code != 202:
        put_state(phase="proceed-rejected", job_id=JOB_ID)
        return 5
    run_id = body.get("taskId")
    try:
        rk = db.q("SELECT run_kind, status FROM job_async_task WHERE id = :r AND job_id = :j AND parent_task_id IS NULL",
                  r=run_id, j=JOB_ID)
    except Exception as e:  # noqa: BLE001
        out(f"하류 실행 확인 조회 실패 {type(e).__name__}")
        rk = []
    if not rk or rk[0]["run_kind"] != "downstream":
        out(f"하류 실행 id 확인 실패(taskId={run_id}, {rk}) — 감시할 수 없어 중단")
        res, _ = abort_once(api, "driver(run 확인 실패)", None)
        out(f"POST /abort → {res}")
        put_state(phase="aborted-unknown-run", job_id=JOB_ID)
        return 2
    out(f"새 하류 실행: run={run_id} (분석 run {ANALYSIS_RUN} 과 구분)")
    put_state(phase="N4", job_id=JOB_ID, run_id=run_id)
    def after():
        r = db.q("SELECT (SELECT max(id) FROM job_async_task WHERE job_id = :j) m, (SELECT status FROM job_async_task WHERE id = :r) s",
                 j=JOB_ID, r=run_id)[0]
        return r["m"], r["s"]
    mon = Monitor(fetch=lambda label: db.snapshot(JOB_ID, label), abort=lambda mx: abort_once(api, "driver", mx, after),
                  stop_text=stop_text, log=out, state=put_state)
    outcome = mon.run(JOB_ID, run_id)
    out(f"감시 결과: {outcome} (검증 판정은 n5_verify.py)")
    return {"N5-settled": 0, "uncontrolled": 4}.get(outcome, 2)


if __name__ == "__main__":
    os.umask(0o077)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("precheck", "run", "check", "abort"):
        sys.exit("usage: driver_n4.py precheck|run|check|abort")
    try:
        d, a = Db(), Api()
        if mode == "check":
            sys.exit(check(d))
        if mode == "abort":
            a.login()
            mx = d.q("SELECT max(id) m FROM job_async_task WHERE job_id = :j", j=JOB_ID)[0]["m"]
            try:
                with open(f"{EV}/state.json", encoding="utf-8") as f:
                    rid = json.load(f).get("run_id")
            except Exception:  # noqa: BLE001
                rid = None

            def after():
                r0 = d.q("SELECT (SELECT max(id) FROM job_async_task WHERE job_id = :j) m, "
                         "(SELECT status FROM job_async_task WHERE id = :r) s", j=JOB_ID, r=rid or -1)[0]
                return r0["m"], r0["s"]
            r, rm = abort_once(a, "local-watcher", mx, after)
            out(f"감시기 요청 /abort → {r} (응답 시점 최대 시도 id {rm})")
            print(f"ABORT {r}", flush=True)
            sys.exit(0)
        sys.exit((0 if precheck(d, a) else 3) if mode == "precheck" else run(d, a))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        out(f"driver 예외 {type(e).__name__}")
        put_state(phase="driver-error", error=type(e).__name__)
        sys.exit(6)
