#!/usr/bin/env bash
# 로컬 감시기 — EC2 컨테이너(교체·정지)·학교 워커(PID 확인·생존·메모리)를 TICK 초마다 보고, driver 가 끝난 뒤에는
# DB·큐 정리 확인(itest.sh check, 읽기 전용)으로 감시를 이어 간다. driver 종료·오류는 작업 종료의 증거가 아니다.
#
# 원칙: 이미 수행한 조치는 반복하지 않고, 새로운 위험에는 합의한 대응을 한다.
#   S4 컨테이너 교체·정지 / 워커 종료·PID 불일치 / 조회 연속 실패 →
#        /abort 가 아직 안 나갔으면: driver 실행 중이면 STOP 기록(driver 가 /abort), driver 가 없으면 단발 /abort(itest.sh abort)
#        이미 나갔으면 기록만(중복 /abort 금지)
#   메모리 20GiB 이상 → 위 중단 + 학교 워커 TERM(확인된 PID 에만, 한 번). /abort 이후에 생겨도 TERM 은 보낸다
#   새 시도 발생(driver 의 uncontrolled 또는 driver 종료 뒤 check 에서 새 시도 id) → 비상 중지: 학교 TERM(아직 안 보냈으면)과
#        EC2 maintenance 요청을 먼저 모두 보내고 대기는 분리, maintenance 종료 코드·적용 상태 수집
# 종료(셋 중 하나):
#   ① N3 + 이번 실행 잔여 작업 정리 확인  ② 중단 후 이번 실행 진행 작업·관련 큐·열린 인계 정리 확인
#   ③ 최대 시간까지 확인 불가 → "확인 불가 — 사용자에게 감시 인계"(종료 코드 2)
# ping·inspect 무응답은 판단에 쓰지 않는다. 조회 실패는 정상으로 보지 않는다. SIGKILL·재시도·삭제 없음.
# 사용: bash watch.sh <ssh_config> <EC2 증거 폴더> <로컬 로그> <driver 컨테이너 이름> <EC2 코드 폴더>
# 시험: WATCH_FAKE=<파일> 이면 ec2()/gpu() 를 그 파일의 함수로 바꾼다(운영 접속 없음).
set -uo pipefail
CFG=$1; EV=$2; LOG=$3; NAME=$4; CODE=$5
LIMIT_KB=${LIMIT_KB:-$((20 * 1024 * 1024))}
TICK=${TICK:-10}; MAX_S=${MAX_S:-$((45 * 60))}
PY=${PY:-python}
ec2() { ssh -F "$CFG" -o ConnectTimeout=8 ec2 "$@"; }
gpu() { ssh -F "$CFG" -o ConnectTimeout=8 gpu "$@"; }
[ -n "${WATCH_FAKE:-}" ] && . "$WATCH_FAKE"
log() { echo "$(date -u +%H:%M:%S) $*" | tee -a "$LOG"; }

ec2_probe() {
  ec2 "sudo docker inspect -f '{{.Name}} {{.Id}} {{.State.StartedAt}} {{.State.Status}}' pixlate-api pixlate-worker-cpu pixlate-recovery pixlate-redis 2>&1 | cut -c1-200
       echo \"itest=\$(sudo docker inspect -f '{{.State.Status}}:{{.State.ExitCode}}' $NAME 2>/dev/null | grep . || echo absent)\"
       echo \"phase=\$(sudo python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(\"phase\"))' $EV/state.json 2>/dev/null || echo none)\"
       echo \"abort_sent=\$(sudo test -e $EV/ABORT_SENT && echo 1 || echo 0)\"
       echo probe=ok"
}
SCHOOL_PROBE='for q in ocr gpu; do
  pid=$(cat /data/pixlate/$q.pid 2>/dev/null); cmd=""
  [ -n "$pid" ] && [ -r /proc/$pid/cmdline ] && cmd=$(tr "\0" " " < /proc/$pid/cmdline)
  v=no; printf "%s" "$cmd" | grep -q celery && printf "%s" "$cmd" | grep -qE -- "-Q $q( |$)" && v=yes
  rss=$(ps -e -o pid=,ppid=,rss= | awk -v r="$pid" "{p[\$1]=\$2; m[\$1]=\$3} END{t=0; if(r==\"\"){print 0; exit} for(x in p){y=x; n=0; while(y!=\"\" && y!=0 && y!=r && n<64){y=p[y]; n++} if(y==r) t+=m[x]} print t}")
  echo "$q pid=${pid:-none} verified=$v rss_kb=$rss"
done
echo "cg_kb=$(( $(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0) / 1024 ))"
echo probe=ok'
# 확인된 PID 에만 TERM(오래된 PID 파일이 다른 프로세스를 가리킬 수 있다)
SCHOOL_TERM='for q in ocr gpu; do
  pid=$(cat /data/pixlate/$q.pid 2>/dev/null); cmd=""
  [ -n "$pid" ] && [ -r /proc/$pid/cmdline ] && cmd=$(tr "\0" " " < /proc/$pid/cmdline)
  if printf "%s" "$cmd" | grep -q celery && printf "%s" "$cmd" | grep -qE -- "-Q $q( |$)"; then
    kill -TERM "$pid" && echo "$q TERM 보냄(pid $pid 확인됨)" || echo "$q TERM 실패"
  else echo "$q PID 확인 불가(${pid:-none}) — TERM 안 보냄"; fi
done'

stop_request() { ec2 "echo '$1' | sudo tee $EV/STOP >/dev/null" && log "STOP 기록(driver 가 /abort): $1" || log "STOP 기록 실패: $1"; }
abort_direct() { log "단발 /abort 요청(driver 없음): $(ec2 "sudo bash $CODE/itest.sh $EV abort $NAME" 2>&1 | grep -E '^ABORT ' | head -1)"; }
school_term() { log "학교 워커 TERM: $(gpu "$SCHOOL_TERM" 2>&1 | tr '\n' ';')"; }
ec2_maint() {
  local u="pixlate-emergency-$(date -u +%H%M%S)"
  ec2 "sudo systemd-run --unit=$u --collect /bin/bash -c '/opt/pixlate/bin/pixlate-mode maintenance > $EV/maint.out 2>&1; echo \$? > $EV/maint.rc'" >/dev/null 2>&1 \
    && log "EC2 maintenance 백그라운드 요청($u) — 종료 코드·적용 상태는 따로 수집" || log "EC2 maintenance 요청 실패"
}
# check 출력(CHECK {json}) → 셸 변수. 값은 숫자·id 목록·작업 상태 문자만 통과시킨다
ck_parse() {
  printf '%s\n' "$1" | sed -n 's/^.*CHECK //p' | head -1 | $PY -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    q = sum(int(v) for v in d["queues"].values())
    ids = ",".join(str(int(i)) for i in d["attempt_ids"]) or "-"
    job = "".join(c for c in str(d["job"]) if c.isalnum() or c in "/_-")
    aft = len(d["after_abort"]) if "after_abort" in d else -1   # N4 driver: 중단 이후 생성 시도 수, N2 driver: 없음(-1)
    print("ck_ok=1 ck_active=%d ck_ids=%s ck_retries=%d ck_ho=%d ck_q=%d ck_other=%d ck_abort=%d ck_job=%s ck_after=%d ck_unexp=%d"
          % (len(d["active"]), ids, len(d["retries"]), int(d["open_handoffs"]), q, int(d["other_active"]),
             1 if d["abort_sent"] else 0, job, aft, len(d.get("unexpected", []))))
except Exception:
    print("ck_ok=0")'
}

B0=$(ec2_probe); BASE=$(printf '%s\n' "$B0" | grep '^/pixlate')
[ "$(printf '%s\n' "$BASE" | grep -c .)" = 4 ] || { log "기준 컨테이너 조회 실패 — 감시 시작 안 함"; exit 3; }
log "기준 컨테이너: $(printf '%s\n' "$BASE" | awk '{print $1, substr($2,1,12), $3, $4}' | tr '\n' ';')"
t0=$(date +%s); halted=0; termed=0; emerg=0; maint_done=0; seen_running=0; probe_fail=0; known_ids=""; seen_reasons=""
printf '%s\n' "$B0" | grep -q '^itest=running' && seen_running=1   # 시작 시점에 driver 실행을 봤으면 종료 직후부터 정리 확인
# 감시 준비: 기준 컨테이너 + 학교 워커 두 개 PID 확인이 끝나야 준비 표시를 남긴다. driver 는 이 표시 전에는 analyze 를 보내지 않는다
S0=$(gpu "$SCHOOL_PROBE" 2>&1)
if printf '%s\n' "$S0" | grep -q '^probe=ok' && [ "$(printf '%s\n' "$S0" | grep -cE '^(ocr|gpu) pid=[0-9]+ verified=yes')" = 2 ]; then
  ec2 "sudo touch $EV/WATCHER_READY && sudo test -e $EV/WATCHER_READY && echo ready" 2>/dev/null | grep -q '^ready' \
    && log "감시 준비 완료 표시(WATCHER_READY) — 이후에만 analyze 발행" || { log "준비 표시 실패 — driver 는 analyze 를 보내지 않는다"; exit 3; }
else
  log "학교 워커 기준 조회 실패 — 준비 표시 안 함(driver 는 analyze 를 보내지 않는다)"; exit 3
fi
while :; do
  E=$(ec2_probe 2>&1); S=$(gpu "$SCHOOL_PROBE" 2>&1)
  reasons=(); ok_e=0; ok_s=0; abort_sent=unknown; itest=unknown; phase=unknown
  if printf '%s\n' "$E" | grep -q '^probe=ok'; then
    ok_e=1
    phase=$(printf '%s\n' "$E" | sed -n 's/^phase=//p'); itest=$(printf '%s\n' "$E" | sed -n 's/^itest=//p')
    abort_sent=$(printf '%s\n' "$E" | sed -n 's/^abort_sent=//p')
    [ "${itest%%:*}" = running ] && seen_running=1
    cur=$(printf '%s\n' "$E" | grep '^/pixlate')
    [ "$cur" = "$BASE" ] || reasons+=("EC2 컨테이너 교체·정지")
  else
    log "EC2 조회 실패 — 정상으로 보지 않음"
  fi
  memsum=0
  if printf '%s\n' "$S" | grep -q '^probe=ok'; then
    ok_s=1
    for q in ocr gpu; do
      printf '%s\n' "$S" | grep -qE "^$q pid=[0-9]+ verified=yes" || reasons+=("학교 $q 워커 종료 또는 PID 확인 불가")
    done
    rss_ocr=$(printf '%s\n' "$S" | awk '/^ocr /{sub("rss_kb=","",$4); print $4+0}')
    rss_gpu=$(printf '%s\n' "$S" | awk '/^gpu /{sub("rss_kb=","",$4); print $4+0}')
    memsum=$(( ${rss_ocr:-0} + ${rss_gpu:-0} ))
    [ "$memsum" -lt "$LIMIT_KB" ] || reasons+=("학교 워커 메모리 ≥ 20GiB")
  else
    log "학교 조회 실패 — 정상으로 보지 않음"
  fi
  if [ $ok_e = 1 ] && [ $ok_s = 1 ]; then probe_fail=0; else probe_fail=$((probe_fail+1)); fi
  [ $probe_fail -lt 3 ] || reasons+=("감시 조회 연속 실패")
  driver_alive=0; [ "${itest%%:*}" = running ] && driver_alive=1
  log "phase=$phase itest=$itest abort_sent=$abort_sent | $(printf '%s\n' "$S" | grep -E '^(ocr|gpu) ' | awk '{print $1, $3, int(substr($4,8)/1024)"MiB"}' | tr '\n' ' ')| memsum=$((memsum/1024))MiB"

  # driver 가 끝났으면(또는 상태를 모르면) DB·큐로 직접 확인
  ck_ok=0; settled_now=0; new_ids=""
  if [ $driver_alive = 0 ] && [ "$itest" != unknown ] && { [ $seen_running = 1 ] || [ $(( $(date +%s) - t0 )) -gt 30 ]; }; then
    C=$(ec2 "sudo bash $CODE/itest.sh $EV check $NAME" 2>&1)
    eval "$(ck_parse "$C")"
    if [ "$ck_ok" = 1 ]; then
      abort_sent=$ck_abort
      if [ -z "$known_ids" ]; then known_ids=$ck_ids
      else new_ids=$(comm -13 <(tr ',' '\n' <<< "$known_ids" | sort) <(tr ',' '\n' <<< "$ck_ids" | sort) | tr '\n' ' '); fi
      [ $ck_active = 0 ] && [ $ck_ho = 0 ] && [ $ck_q = 0 ] && settled_now=1
      log "정리 확인: job=$ck_job 진행 중 ${ck_active} 재시도 ${ck_retries} 열린 인계 ${ck_ho} 큐 ${ck_q} 다른 작업 ${ck_other} abort_sent=${ck_abort}${new_ids:+ 새 시도 $new_ids}"
    else
      log "정리 확인 조회 실패 — 정리된 것으로 보지 않음"
      probe_fail=$((probe_fail+1))
    fi
  fi

  # S4: 새 위험만 대응(같은 사유는 한 번만 기록), 중단은 /abort 가 아직 안 나갔을 때만
  for r in "${reasons[@]}"; do
    case "|$seen_reasons|" in *"|$r|"*) continue ;; esac
    seen_reasons="$seen_reasons|$r"; log "S4: $r"
    if [ $halted = 0 ] && [ "$abort_sent" != 1 ]; then
      if [ $driver_alive = 1 ] || [ "$itest" = unknown ]; then stop_request "$r"; halted=1   # waiting-watcher 단계면 driver 가 analyze 를 보내지 않음
      elif [ "$ck_ok" = 1 ] && [ $ck_active -gt 0 ]; then abort_direct; halted=1
      else log "중단할 진행 작업 없음(기록만)"; fi
    else
      log "중단은 이미 요청됨 — /abort 반복하지 않음(기록만)"
    fi
  done
  # driver 가 없는데 이번 실행이 아직 진행 중이고 /abort 가 안 나갔으면 → 감시 불능으로 단발 /abort
  if [ "$ck_ok" = 1 ] && [ $ck_active -gt 0 ] && [ "$ck_abort" = 0 ] && [ $halted = 0 ]; then
    log "driver 종료 뒤에도 이번 실행 진행 중(${ck_active}) — 감시 불능으로 중단"; abort_direct; halted=1
  fi
  if [ "$memsum" -ge "$LIMIT_KB" ] && [ $termed = 0 ]; then
    school_term; termed=1
    log "참고: 웜 종료는 현재 추론을 끝까지 하므로 메모리가 즉시 줄지 않는다. 증가가 계속되면 보고(SIGKILL 은 담당자 결정)"
  fi
  # 통제 불가: driver 표시, 또는 중단 이후 생성된 시도(N4 check). N2 check(ck_after=-1)는 이전처럼 id 비교.
  # 정상 후속 시도(label → logo → …)는 중단 전이면 새 id 여도 통제 불가가 아니다.
  unc=""; [ "$ck_ok" = 1 ] && [ "${ck_after:--1}" -gt 0 ] && unc="중단 요청 이후 관측된 새 시도 ${ck_after}건(응답 이후 생성 또는 아직 진행 중)"
  [ "$ck_ok" = 1 ] && [ "${ck_after:--1}" = -1 ] && [ -n "$new_ids" ] && unc="새 시도 $new_ids"
  if { [ "$phase" = uncontrolled ] || [ -n "$unc" ]; } && [ $emerg = 0 ]; then
    log "통제 불가(${unc:+$unc }phase=$phase) — 비상 중지: 학교 TERM 과 EC2 maintenance 를 먼저 모두 요청"
    if [ $termed = 0 ]; then school_term & fi
    ec2_maint & wait; emerg=1; termed=1
  fi
  if [ $emerg = 1 ] && [ $maint_done = 0 ]; then
    m=$(ec2 "cat $EV/maint.rc 2>/dev/null; echo applied=\$(cat /etc/pixlate/deploy.mode.applied 2>/dev/null)")
    rc=$(printf '%s\n' "$m" | head -1 | grep -E '^[0-9]+$' || true)
    if [ -n "$rc" ]; then log "maintenance 종료 코드 $rc, $(printf '%s\n' "$m" | grep applied=) — 이 값으로 판단(경과 시간 아님)"; maint_done=1
    else log "maintenance 진행 중 ($(printf '%s\n' "$m" | grep applied=))"; fi
  fi
  # 종료 ①② — DB 로 정리를 확인했고, 비상 maintenance 를 보냈다면 그 결과까지 모았을 때
  if [ $settled_now = 1 ] && { [ $emerg = 0 ] || [ $maint_done = 1 ]; }; then
    case "$phase" in
      N3) log "종료: N3 도달 + 이번 실행 잔여 작업 정리 확인(job=$ck_job)" ;;
      N5*) log "종료: N5 도달 + 이번 실행 잔여 작업 정리 확인(job=$ck_job) — 감시 종료이며 검증 판정은 n5_verify 로 별도" ;;
      N6-done) log "종료: N6 최종 렌더 처리 완료 + 잔여 작업 정리 확인(job=$ck_job) — 처리·품질 판정은 n6_verify 로 별도" ;;
      N6-render-failed-preserved) log "종료: N6 렌더 실패, 수동 재시도 대기 상태로 보존 + 잔여 작업 정리 확인(job=$ck_job)" ;;
      N6*) log "종료: N6 감시 종료(phase=$phase) + 잔여 작업 정리 확인(job=$ck_job)" ;;
      not-started-*|precheck-failed) log "종료: analyze 미발행($phase) + 진행 작업 없음 확인(job=$ck_job)" ;;
      *)  log "종료: 중단 후 이번 실행 진행 작업·큐·열린 인계 정리 확인(phase=$phase job=$ck_job)" ;;
    esac
    exit 0
  fi
  if [ $(( $(date +%s) - t0 )) -ge $MAX_S ]; then
    log "확인 불가 — 최대 감시 시간 도달(phase=$phase itest=$itest 정리확인=${ck_ok}/${settled_now}). 사용자에게 감시 인계"
    exit 2
  fi
  sleep "$TICK"
done
