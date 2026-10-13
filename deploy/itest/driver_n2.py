"""통합 검증 2차 — 기존 작업 9 재사용, N3 도달까지만(proceed·수동 재시도·추가 작업·정리 없음).

API 이미지 컨테이너 안에서 pixlate-net 의 pixlate-api:8000 을 부른다. 증거는 /ev 에만 쓴다(root 700, 실행마다 새 폴더).
토큰·URL·키·프롬프트·응답 본문·이미지는 기록하지 않는다.

모드:
  precheck  읽기 전용. 스키마 열 확인 → 큐·진행 0 → 작업 9 상태(N1 draft·원본 1장·진행 중 실행 없음) → 사전 공급 →
            감시·보고 쿼리를 없는 작업 id 와 작업 9 의 실제 취소 이력(run 16)으로 실행 → 판정·정착 함수를 그 행에 적용 →
            API 읽기(로그인: 기존 판매자라 DB 쓰기 없음, GET /jobs/9·/tasks·/sections)
  run       precheck 통과 시 POST /jobs/9/analyze 1회 → 새 실행(run)만 감시 → N3 또는 중단
  check     읽기 전용 정리 확인 한 줄(CHECK {json}) — 로컬 감시기가 driver 종료 뒤에도 감시를 이어 간다
  abort     /abort 단발(이미 보냈으면 보내지 않음) — driver 가 중단 없이 죽었을 때 로컬 감시기가 쓴다
/abort 를 보내기 직전에 /ev/ABORT_SENT 를 남긴다(누가 보냈든 한 번만).

판정(decide)은 순수 함수다. 감시 루프(Monitor)는 조회·중단·시계를 주입받아 모의 시험(test_driver.py)에서 그대로 돌린다.
  S1 이 실행의 시도 failed / 재시도(attempt_no>1 또는 retry_origin) / 예상 밖 단계 / 실행 failed·cancelled / 작업 failed
  S2 running 인데 lease_expires_at IS NULL 또는 <= DB now() (복구 코드와 같은 판정) / 단계 제한 시간 / pending 미시작 5분 / N2 15분
  S4 로컬 감시기가 /ev/STOP 기록(프로세스·컨테이너·메모리)
  S3 다른 작업 유입 → 이번 검증 중단·판정 보류(다른 사용자의 작업은 건드리지 않는다)
조회 실패는 '작업 0건'으로 해석하지 않는다: 그 틱은 판정하지 않고, 연속 3회면 감시 불능으로 중단한다.
중단 요청(/abort)은 한 번만 보낸다. 실패해도 다시 보내지 않고 중단 후 감시를 계속한다(새 시도가 생기면 uncontrolled).
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())  # 이미지 작업 폴더(app·pipeline 패키지)

EV = os.environ.get("ITEST_EV", "/ev")
API = "http://pixlate-api:8000/v1"
EMAIL = "itest-20261013@example.com"
JOB_ID = 9
PREV_RUN_ID = 16                         # 1차(감시기 오류로 중단된) 실행 — 이력 유지, 감시 대상 아님
SRC_SHA_PREFIX = "4f46d564ddc74dd8"
TICK = 10
N2_LIMIT_S = 15 * 60
STAGE_LIMIT_S = {"analyze": 10 * 60, "judge": 5 * 60}
PENDING_LIMIT_S = 5 * 60
N2_STAGES = {"analyze", "judge"}
QUERY_FAIL_LIMIT = 3
POST_ABORT_WATCH_S = 10 * 60

# 이 스크립트가 읽는 열 — precheck 에서 information_schema 와 대조한다(추측 금지)
COLUMNS = {
    "job": ["id", "seller_id", "status", "current_step", "user_facing_status", "target_country", "regulatory_class",
            "current_analysis_task_id"],
    "job_async_task": ["id", "job_id", "parent_task_id", "run_kind", "stage", "unit_id", "status", "attempt_no", "is_current",
                       "retry_count", "max_retry", "retry_origin", "error_code", "dispatch_count", "last_dispatched_at",
                       "started_at", "finished_at", "heartbeat_at", "lease_expires_at", "lease_owner", "cancelled_at"],
    "task_handoff": ["id", "task_id", "job_id", "lease_epoch", "outcome", "skip_reason", "target_count", "state", "late",
                     "received_at", "adopted_at", "reject_reason"],
    "task_lease_grant": ["id", "task_id", "generation_epoch", "issued_at", "closed_at", "close_reason"],
    "section": ["id", "job_id", "section_order", "top_offset", "height", "bbox", "bucket", "exclusion_reason",
                "excluded_stage", "category", "original_verdict", "content_findings", "analysis_task_id"],
    "text_block": ["id", "section_id", "block_order", "role", "is_excluded", "bbox", "ocr_confidence", "source_ko"],
    "source_image": ["id", "job_id", "width", "height", "sha256"],
    "seller": ["id", "email"],
}
ATT_COLS = ("id, parent_task_id, run_kind, stage, unit_id, status, attempt_no, is_current, retry_count, max_retry, "
            "retry_origin, error_code, dispatch_count, last_dispatched_at, started_at, finished_at, heartbeat_at, "
            "lease_expires_at, lease_owner IS NOT NULL AS leased, cancelled_at")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 증거 기록
def append(name, line):
    with open(f"{EV}/{name}", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def jl(name, obj):
    append(name, json.dumps(obj, ensure_ascii=False, default=str))


def out(msg):
    print(f"[itest {now_iso()}] {msg}", flush=True)
    append("progress.log", f"{now_iso()} {msg}")


def put_state(**kw):
    tmp = f"{EV}/.state.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"at": now_iso(), **kw}, f, ensure_ascii=False, default=str)
    os.replace(tmp, f"{EV}/state.json")


# ---------------------------------------------------------------- 순수 판정
def decide(rec, run_id, elapsed_s, stop_text=None):
    """한 스냅샷에 대한 중단 사유 목록. rec['attempts'] 는 이 작업의 모든 행(대표 행 포함). run_id 의 자식만 판정한다."""
    reasons, db_now = [], rec["db_now"]
    for a in rec["attempts"]:
        if a["parent_task_id"] != run_id:
            continue
        tag = f"#{a['id']}({a['stage']}/{a['unit_id']})"
        if a["stage"] not in N2_STAGES:
            reasons.append(f"S1 예상 밖 단계 {tag}")
        if a["status"] == "failed":
            reasons.append(f"S1 실패 {tag} {a['error_code']}")
        if (a["attempt_no"] or 1) > 1 or a["retry_origin"]:
            reasons.append(f"S1 재시도 {tag} attempt_no={a['attempt_no']} origin={a['retry_origin']}")
        if a["status"] == "running":
            exp = a["lease_expires_at"]
            if exp is None or exp <= db_now:
                reasons.append(f"S2 lease 만료 {tag} lease_expires_at={exp} db_now={db_now} heartbeat={a['heartbeat_at']}")
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
    if elapsed_s > N2_LIMIT_S:
        reasons.append(f"S2 N2 전체 제한 시간 {N2_LIMIT_S}s 초과")
    if rec["job"]["status"] == "failed":
        reasons.append(f"S1 작업 failed {rec['job']['current_step']}")
    if stop_text:
        reasons.append("S4 로컬 감시기 요청: " + stop_text[:200])
    return reasons


def reached_n3(rec, run_id):
    if rec["job"]["current_step"] != "N3":
        return None
    cur = [a for a in rec["attempts"] if a["parent_task_id"] == run_id and a["is_current"]]
    return {"current_statuses": sorted({a["status"] for a in cur}), "all_done": all(a["status"] == "done" for a in cur)}


def settled(rec, run_id):
    """중단 후 정착: 이 실행의 pending·running 0 이고 큐·unacked 0. 조회 실패 틱에는 부르지 않는다."""
    active = [a["id"] for a in rec["attempts"] if (a["parent_task_id"] == run_id or a["id"] == run_id)
              and a["status"] in ("pending", "running")]
    return not active and not any(rec["queues"].values())


def other_activity(rec, job_id):
    return [g for g in rec["global_active"] if g["job_id"] != job_id]


# ---------------------------------------------------------------- 감시 루프(주입형)
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
        except Exception as e:  # noqa: BLE001 — 원문 대신 유형만
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
            others = other_activity(rec, job_id)
            if others:
                return self.stop(job_id, run_id, [f"S3 다른 작업 유입 {others} — 이번 검증 중단·판정 보류(다른 작업은 건드리지 않음)"],
                                 rec=rec)
            n3 = reached_n3(rec, run_id)
            if n3 is not None:
                self.log(f"N3 도달: 현재 시도 상태={n3['current_statuses']} 모두 done={n3['all_done']}")
                self.state(phase="N3", job_id=job_id, run_id=run_id, all_done=n3["all_done"])
                return "N3"
            reasons = decide(rec, run_id, self.clock() - t0, self.stop_text())
            if reasons:
                return self.stop(job_id, run_id, reasons, rec=rec)
            self.sleep(TICK)

    def stop(self, job_id, run_id, reasons, rec):
        self.log("중단 판정: " + " | ".join(reasons))
        if rec is None:
            rec, err = self._fetch("pre-abort")        # 증거 먼저. 실패해도 중단 요청은 진행
            if err:
                self.log(f"중단 전 증거 조회 실패({err}) — 중단 요청은 계속")
        self.abort_calls += 1                           # 단 한 번
        res = self.abort_fn()
        self.log(f"POST /abort → {res} (이미 시작된 모델 요청·비용은 취소 보장 없음)")
        known = {a["id"] for a in rec["attempts"]} if rec else None
        if res == 409:
            chk, err = self._fetch("abort-409")
            if not err and reached_n3(chk, run_id) is not None:
                self.log("중단 요청 시점에 이미 N3 — 중단되지 않음, N3 로 처리")
                self.state(phase="N3", job_id=job_id, run_id=run_id, note="abort 409 at N3")
                return "N3"
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
                new = [a["id"] for a in chk["attempts"] if a["id"] not in known]
                if new:
                    self.log(f"중단 뒤 새 시도 {new} — 통제 불가(uncontrolled)")
                    self.state(phase="uncontrolled", job_id=job_id, run_id=run_id, new_attempts=new)
                    return "uncontrolled"
                if settled(chk, run_id):
                    self.log("중단 뒤 이 실행 pending·running 0, 큐 0 (워커 실제 종료는 로컬 감시기에서 확인)")
                    self.state(phase=f"{phase}-settled", job_id=job_id, run_id=run_id)
                    return f"{phase}-settled"
            self.sleep(TICK)
        self.log("중단 뒤 제한 시간 동안 정착 확인 못 함 — 보고 필요")
        self.state(phase=f"{phase}-unsettled", job_id=job_id, run_id=run_id)
        return f"{phase}-unsettled"


# ---------------------------------------------------------------- DB·API(운영에서만)
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

    def snapshot(self, job_id, label):
        job = self.q("SELECT status, current_step, user_facing_status, current_analysis_task_id FROM job WHERE id = :j", j=job_id)
        if not job:
            raise LookupError(f"job {job_id} 없음")
        rec = {"at": now_iso(), "label": label, "db_now": self.q("SELECT now() t")[0]["t"], "job": job[0],
               "attempts": self.q(f"SELECT {ATT_COLS} FROM job_async_task WHERE job_id = :j ORDER BY id", j=job_id),
               "handoffs": self.q("SELECT id, task_id, lease_epoch, outcome, skip_reason, target_count, state, late, received_at, "
                                  "adopted_at, left(reject_reason, 200) reject_reason FROM task_handoff WHERE job_id = :j ORDER BY id",
                                  j=job_id),
               "grants": self.q("SELECT g.id, g.task_id, g.generation_epoch, g.issued_at, g.closed_at, g.close_reason "
                                "FROM task_lease_grant g JOIN job_async_task a ON a.id = g.task_id WHERE a.job_id = :j ORDER BY g.id",
                                j=job_id),
               "queues": self.queues(),
               "global_active": self.q("SELECT job_id, stage, status, count(*) n FROM job_async_task WHERE status IN "
                                       "('pending','running') AND parent_task_id IS NOT NULL GROUP BY 1,2,3 ORDER BY 1,2,3")}
        jl("watch.jsonl", rec)
        return rec

    def n3_rows(self, job_id, run_id):
        return {
            "sections": self.q("SELECT id, section_order, top_offset, height, bbox, bucket, exclusion_reason, excluded_stage, category, "
                               "original_verdict IS NOT NULL AS has_verdict, content_findings IS NOT NULL AS has_findings "
                               "FROM section WHERE job_id = :j AND analysis_task_id = :r ORDER BY section_order", j=job_id, r=run_id),
            "blocks": self.q("SELECT b.section_id, b.id, b.block_order, b.role, b.is_excluded, b.bbox IS NOT NULL AS has_bbox, "
                             "b.ocr_confidence, left(b.source_ko, 40) txt FROM text_block b JOIN section s ON s.id = b.section_id "
                             "WHERE s.job_id = :j AND s.analysis_task_id = :r ORDER BY b.section_id, b.block_order", j=job_id, r=run_id),
            "attempts": self.q("SELECT stage, status, count(*) n, max(attempt_no) max_attempt FROM job_async_task "
                               "WHERE parent_task_id = :r GROUP BY 1,2 ORDER BY 1,2", r=run_id),
        }


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


def scrub(o):
    """API 응답에서 URL·키 값을 지우고 구조만 남긴다."""
    if isinstance(o, dict):
        return {k: ("<생략>" if any(s in k.lower() for s in ("url", "key", "token")) else scrub(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o


def report_n3(db, api, job_id, run_id, label="n3_report"):
    rows = db.n3_rows(job_id, run_id)
    r = api.call("GET", f"/jobs/{job_id}/sections")
    rows["api_sections"] = scrub(r.json()) if r.status_code == 200 else {"status": r.status_code}
    t = api.call("GET", f"/jobs/{job_id}/tasks")
    rows["api_tasks"] = scrub(t.json()) if t.status_code == 200 else {"status": t.status_code}
    jl(f"{label}.jsonl", {"at": now_iso(), "job_id": job_id, "run_id": run_id, **rows})
    out(f"{label}: run={run_id} 섹션 {len(rows['sections'])}개, 블록 {len(rows['blocks'])}개, 시도 {rows['attempts']}, "
        f"API sections={r.status_code} tasks={t.status_code}")
    return rows


def stop_text():
    try:
        with open(f"{EV}/STOP", encoding="utf-8") as f:
            return f.read().strip() or "STOP"
    except FileNotFoundError:
        return None


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
    rev = db.q("SELECT version_num v FROM alembic_version")[0]["v"]
    out(f"precheck queues={qs} active_attempts={act} open_handoffs={ho} db={rev}")
    if any(qs.values()) or act or ho or rev != "0008":
        fail("큐·진행 중 시도·열린 인계 0 / DB 0008 아님")
    seller = db.q("SELECT id FROM seller WHERE email = :e", e=EMAIL)
    job = db.q("SELECT id, seller_id, status, current_step, target_country, regulatory_class, current_analysis_task_id "
               "FROM job WHERE id = :j", j=JOB_ID)
    src = db.q("SELECT id, width, height, sha256 FROM source_image WHERE job_id = :j ORDER BY id", j=JOB_ID)
    out(f"precheck 작업 {JOB_ID}: {job} 원본 {[(s['id'], s['width'], s['height'], (s['sha256'] or '')[:16]) for s in src]}")
    if not seller or not job or job[0]["seller_id"] != seller[0]["id"]:
        fail("테스트 판매자·작업 소유 불일치")
    elif (job[0]["status"], job[0]["current_step"], job[0]["target_country"], job[0]["regulatory_class"]) != ("draft", "N1", "US", "cosmetic"):
        fail("작업 상태가 draft/N1/US/cosmetic 아님")
    if len(src) != 1 or (src[0]["sha256"] or "")[:16] != SRC_SHA_PREFIX or src[0]["width"] > 860:
        fail("원본이 1장·예상 해시·폭 860 이하가 아님")
    try:
        from app.dictionary_bundle import build_bundle  # SELECT 만 수행
        b = build_bundle("US", "cosmetic")
        out(f"precheck 규제 사전 US/cosmetic {len(b['regulatory']['entries'])}행 | 현지 status={b['local']['status']} "
            f"{len(b['local']['entries'])}행")
    except Exception as e:  # noqa: BLE001
        fail(f"규제 사전 공급 불가 {type(e).__name__} code={getattr(e, 'code', None)} module={getattr(e, 'name', None)}")
    # 감시·보고 쿼리 실제 실행: 없는 작업 id(열 이름·빈 결과 처리) → 작업 9 실제 취소 이력(행 처리·판정·정착)
    try:
        db.snapshot(-1, "precheck-missing")
        fail("없는 작업 id 조회가 예외 없이 끝남(없음을 감지하지 못함)")
    except LookupError:
        out("precheck 없는 작업 id: '작업 없음'으로 감지(0건으로 처리하지 않음)")
    rec = db.snapshot(JOB_ID, "precheck-history")
    prev = [a for a in rec["attempts"] if a["id"] == PREV_RUN_ID or a["parent_task_id"] == PREV_RUN_ID]
    out(f"precheck 작업 {JOB_ID} 이력: 시도 {[(a['id'], a['stage'], a['status']) for a in prev]}, 인계 {len(rec['handoffs'])}, "
        f"권한 {len(rec['grants'])}")
    d = decide(rec, PREV_RUN_ID, 0, None)
    out(f"precheck 판정 함수(이전 실행 #{PREV_RUN_ID}): {d}")
    if d != [f"S1 실행 #{PREV_RUN_ID} cancelled"]:
        fail("이전 취소 실행에 대한 판정 결과가 예상('실행 cancelled')과 다름")
    out(f"precheck 정착 함수(이전 실행): {settled(rec, PREV_RUN_ID)} / N3 판정: {reached_n3(rec, PREV_RUN_ID)}")
    if not settled(rec, PREV_RUN_ID) or reached_n3(rec, PREV_RUN_ID) is not None:
        fail("이전 실행 정착·N3 판정이 예상과 다름")
    if [a for a in rec["attempts"] if a["status"] in ("pending", "running")]:
        fail(f"작업 {JOB_ID} 에 진행 중 시도 있음")
    seller_id = api.login()  # 기존 판매자 — SELECT 만(신규 생성 없음)
    out(f"precheck 로그인: seller={seller_id} (기존)")
    g = api.call("GET", f"/jobs/{JOB_ID}")
    out(f"precheck GET /jobs/{JOB_ID} {g.status_code} step={g.json().get('currentStep') if g.status_code == 200 else None}")
    report_n3(db, api, JOB_ID, PREV_RUN_ID, label="precheck_report")
    out(f"precheck 결과: {'충족' if ok else '불충족'}")
    return ok


# ---------------------------------------------------------------- 실행
def run(db, api):
    if not precheck(db, api):
        put_state(phase="precheck-failed")
        return 3
    put_state(phase="waiting-watcher", job_id=JOB_ID)
    out("감시기 준비 대기(WATCHER_READY) — 준비 확인 전에는 analyze 를 보내지 않는다")
    w = wait_watcher()
    if w != "ready":
        out(f"감시기 준비 안 됨({w}) — analyze 를 보내지 않고 종료")
        put_state(phase=f"not-started-{w}", job_id=JOB_ID)
        return 3
    act = db.q("SELECT count(*) n FROM job_async_task WHERE status IN ('pending','running')")[0]["n"]
    if act or any(db.queues().values()):
        out(f"발행 직전 재확인 실패(진행 중 시도 {act}, 큐 {db.queues()}) — analyze 를 보내지 않음")
        put_state(phase="not-started-busy", job_id=JOB_ID)
        return 3
    out("감시기 준비 확인 + 발행 직전 큐·진행 0 → analyze 1회")
    r = api.call("POST", f"/jobs/{JOB_ID}/analyze")
    body = r.json() if r.status_code == 202 else {}
    out(f"analyze {r.status_code} existing={body.get('existing')}")
    if r.status_code != 202 or body.get("existing"):
        put_state(phase="analyze-rejected", job_id=JOB_ID)
        return 5
    try:  # 여기서 예외가 나도 감시 없이 진행되지 않게 아래 중단 경로로 보낸다
        att = db.q("SELECT parent_task_id FROM job_async_task WHERE id = :a", a=body["taskId"])
        run_id = att[0]["parent_task_id"] if att else None
    except Exception as e:  # noqa: BLE001
        out(f"새 실행 id 조회 실패 {type(e).__name__}")
        run_id = None
    out(f"새 실행: run={run_id} analyze attempt={body['taskId']} (이전 실행 #{PREV_RUN_ID} 과 구분)")
    put_state(phase="N2", job_id=JOB_ID, run_id=run_id)
    if run_id is None:
        out("새 실행 id 확인 실패 — 감시할 수 없어 중단")
        res = abort_once(api, "driver(run id 없음)")
        out(f"POST /abort → {res}")
        put_state(phase="aborted-unknown-run", job_id=JOB_ID)
        return 2
    mon = Monitor(fetch=lambda label: db.snapshot(JOB_ID, label),
                  abort=lambda: abort_once(api, "driver"),
                  stop_text=stop_text, log=out, state=put_state)
    outcome = mon.run(JOB_ID, run_id)
    if outcome == "N3":
        report_n3(db, api, JOB_ID, run_id)
        db.snapshot(JOB_ID, "final")
        return 0
    return {"uncontrolled": 4}.get(outcome, 2)


def abort_once(api, who):
    """/abort 단발(driver·감시기 공통). ABORT_SENT 는 O_CREAT|O_EXCL 로 원자적으로 만든다 — 먼저 만든 쪽만 요청한다.
    이 파일은 '중단 성공'이 아니라 '요청 시도권을 사용함'을 뜻한다. 실제 응답·실패·시간 초과는 ABORT_RESULT 에 따로 남긴다."""
    try:
        fd = os.open(f"{EV}/ABORT_SENT", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return "already-sent"
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"{now_iso()} 요청 시도권 사용: {who}" + chr(10))
    res = with_timeout(lambda: api.call("POST", f"/jobs/{JOB_ID}/abort", timeout=10).status_code, 12)
    append("ABORT_RESULT", f"{now_iso()} {who} result={res}")   # 200 / 409 / error:<유형> / timeout:12s
    return res


WATCHER_WAIT_S = 5 * 60


def wait_watcher(clock=time.monotonic, sleep=time.sleep):
    """감시기 준비 표시(WATCHER_READY)를 확인한 뒤에만 analyze 를 보낸다. 없거나 STOP 이 먼저 오면 보내지 않는다."""
    t0 = clock()
    while clock() - t0 < WATCHER_WAIT_S:
        if stop_text():
            return "stop"
        if os.path.exists(f"{EV}/WATCHER_READY"):
            return "ready"
        sleep(2)
    return "timeout"


def check(db):
    """읽기 전용 정리 확인. driver 종료·오류는 작업 종료의 증거가 아니므로 DB·큐로 직접 본다."""
    try:
        with open(f"{EV}/state.json", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:  # noqa: BLE001
        st = {}
    job = db.q("SELECT status, current_step, current_analysis_task_id FROM job WHERE id = :j", j=JOB_ID)[0]
    run_id = st.get("run_id") or job["current_analysis_task_id"]
    rows = db.q("SELECT id, parent_task_id, stage, status, attempt_no, retry_origin FROM job_async_task "
                "WHERE job_id = :j AND (id = :r OR parent_task_id = :r) ORDER BY id", j=JOB_ID, r=run_id or -1)
    res = {"run_id": run_id, "job": f"{job['status']}/{job['current_step']}",
           "attempt_ids": [r["id"] for r in rows],
           "active": [r["id"] for r in rows if r["status"] in ("pending", "running")],
           "retries": [r["id"] for r in rows if (r["attempt_no"] or 1) > 1 or r["retry_origin"]],
           "open_handoffs": db.q("SELECT count(*) n FROM task_handoff WHERE job_id = :j AND state IN "
                                 "('received','verifying','verified')", j=JOB_ID)[0]["n"],
           "queues": db.queues(),
           "other_active": db.q("SELECT count(*) n FROM job_async_task WHERE job_id <> :j AND status IN ('pending','running')",
                                j=JOB_ID)[0]["n"],
           "abort_sent": os.path.exists(f"{EV}/ABORT_SENT")}
    jl("check.jsonl", {"at": now_iso(), **res})
    print("CHECK " + json.dumps(res, default=str), flush=True)
    return 0


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


if __name__ == "__main__":
    os.umask(0o077)
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("precheck", "run", "check", "abort"):
        sys.exit("usage: driver.py precheck|run|check|abort")
    try:
        d, a = Db(), Api()
        if mode == "check":
            sys.exit(check(d))
        if mode == "abort":
            a.login()
            r = abort_once(a, "local-watcher")
            out(f"감시기 요청 /abort → {r}")
            print(f"ABORT {r}", flush=True)
            sys.exit(0)
        sys.exit((0 if precheck(d, a) else 3) if mode == "precheck" else run(d, a))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — 원문(주소 포함 가능) 대신 유형만. 감시 루프 밖의 예외
        out(f"driver 예외 {type(e).__name__}")
        put_state(phase="driver-error", error=type(e).__name__)
        sys.exit(6)
