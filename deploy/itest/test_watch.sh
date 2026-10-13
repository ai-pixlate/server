#!/usr/bin/env bash
# watch.sh 상태별 모의 시험 — ec2()/gpu() 를 가짜로 바꿔 운영 접속 없이 돌린다. bash test_watch.sh
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
W=$(mktemp -d)
pass=0; fail=0

cat > "$W/fake.sh" <<'EOF'
# 시나리오 폴더($SCN)의 ec2.N / gpu.N / chk.N / rc.N 을 호출 순서대로 돌려준다(없으면 직전 번호 반복). 동작은 $SCN/actions
_next() { local k=$1 n; n=$(cat "$SCN/$k.i" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$SCN/$k.i"
  while [ $n -gt 0 ] && [ ! -f "$SCN/$k.$n" ]; do n=$((n-1)); done; [ $n -gt 0 ] && cat "$SCN/$k.$n"; }
ec2() { case "$*" in
  *"{{.Name}}"*) _next ec2 ;;
  *"tee "*STOP*) echo "STOP" >> "$SCN/actions" ;;
  *WATCHER_READY*) echo "READY" >> "$SCN/actions"; echo ready ;;
  *"itest.sh"*" check "*) _next chk ;;
  *"itest.sh"*" abort "*) echo "ABORT" >> "$SCN/actions"; echo "ABORT 200" ;;
  *systemd-run*) echo "MAINT" >> "$SCN/actions" ;;
  *maint.rc*) _next rc ;;
  *) echo "UNKNOWN_EC2 $*" >> "$SCN/actions" ;; esac; }
gpu() { case "$*" in
  *rss_kb*) _next gpu ;;
  *"kill -TERM"*) echo "TERM" >> "$SCN/actions"; echo "ocr TERM 보냄(pid 11 확인됨)" ;;
  *) echo "UNKNOWN_GPU" >> "$SCN/actions" ;; esac; }
EOF

C="/pixlate-api aaa 2026-10-12T07:54:50Z running
/pixlate-worker-cpu bbb 2026-10-12T07:54:50Z running
/pixlate-recovery ccc 2026-10-12T07:54:51Z running
/pixlate-redis ddd 2026-09-28T04:31:31Z running"
CX=$(printf '%s\n' "$C" | sed 's/bbb/zzz/')
e() { printf '%s\nitest=%s\nphase=%s\nabort_sent=%s\nprobe=ok\n' "${4:-$C}" "$1" "$2" "${3:-0}"; }
g() { printf 'ocr pid=11 verified=%s rss_kb=%s\ngpu pid=12 verified=yes rss_kb=60000\ncg_kb=3000000\nprobe=ok\n' "${1:-yes}" "${2:-100000}"; }
# chk <진행 중 id 목록(json)> <전체 id 목록(json)> <abort_sent true|false> [job] [queue합]
chk() { printf 'noise\nCHECK {"run_id": 100, "job": "%s", "attempt_ids": %s, "active": %s, "retries": [], "open_handoffs": 0, "queues": {"ocr": %s, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0}, "other_active": 0, "abort_sent": %s}\n' \
  "${4:-draft/N1}" "$2" "$1" "${5:-0}" "$3"; }

scenario() {  # scenario <이름> <STOP> <ABORT> <TERM> <MAINT> <기대 종료코드> <기대 로그 문구> [둘째 문구] [READY 수]
  local name=$1 es=$2 ea=$3 et=$4 em=$5 ex=$6 want=$7 want2=${8:-$7} er=${9:-1} s a t m r rc
  SCN=$W/$name TICK=0 MAX_S=${MAXS:-15} LIMIT_KB=20971520 WATCH_FAKE=$W/fake.sh \
    timeout 90 bash "$HERE/watch.sh" cfg /ev "$W/$name/log" pixlate-itest-r2 /code > /dev/null 2>&1; rc=$?
  s=$(grep -c '^STOP' "$W/$name/actions"); a=$(grep -c '^ABORT' "$W/$name/actions")
  t=$(grep -c '^TERM' "$W/$name/actions"); m=$(grep -c '^MAINT' "$W/$name/actions"); r=$(grep -c '^READY' "$W/$name/actions")
  if [ "$s/$a/$t/$m/$rc/$r" = "$es/$ea/$et/$em/$ex/$er" ] && grep -q -- "$want" "$W/$name/log" && grep -q -- "$want2" "$W/$name/log"      && ! grep -q UNKNOWN "$W/$name/actions"; then
    echo "ok   $name"; pass=$((pass+1))
  else
    echo "FAIL $name: STOP/ABORT/TERM/MAINT/exit/READY=$s/$a/$t/$m/$rc/$r 기대 $es/$ea/$et/$em/$ex/$er 문구=$(grep -c -- "$want" "$W/$name/log")/$(grep -c -- "$want2" "$W/$name/log")"
    tail -5 "$W/$name/log" | sed 's/^/     /'; fail=$((fail+1))
  fi
}
mk() { mkdir -p "$W/$1"; touch "$W/$1/actions"; g > "$W/$1/gpu.1"; chk '[]' '[100,101]' true > "$W/$1/chk.1"; }

# 1 정상: N2 → N3, driver 종료 → DB 정리 확인 후 종료(①)
mk normal; e running:0 N2 > $W/normal/ec2.1; e running:0 N2 > $W/normal/ec2.2; e exited:0 N3 > $W/normal/ec2.3
chk '[]' '[100,101,102]' false review/N3 > $W/normal/chk.1
scenario normal 0 0 0 0 0 "종료: N3 도달 + 이번 실행 잔여 작업 정리 확인"

# 2 컨테이너 교체(여러 틱 지속) → STOP 한 번만(driver 가 /abort)
mk replaced; e running:0 N2 > $W/replaced/ec2.1; e running:0 N2 > $W/replaced/ec2.2; e running:0 N2 0 "$CX" > $W/replaced/ec2.3
e running:0 aborted 1 "$CX" > $W/replaced/ec2.4; e exited:2 aborted-settled 1 "$CX" > $W/replaced/ec2.5
scenario replaced 1 0 0 0 0 "S4: EC2 컨테이너 교체"

# 3 N2 중 메모리 초과 → STOP 한 번 + TERM 한 번
mk memory; e running:0 N2 > $W/memory/ec2.1; e running:0 N2 > $W/memory/ec2.2; e running:0 aborted 1 > $W/memory/ec2.4; e exited:2 aborted-settled 1 > $W/memory/ec2.5
g yes 21000000 > $W/memory/gpu.2
scenario memory 1 0 1 0 0 "메모리 ≥ 20GiB"

# 4 /abort 이후 메모리 초과 → 중복 /abort 없이 TERM 은 보낸다
mk abort_then_mem; e running:0 aborted 1 > $W/abort_then_mem/ec2.1; e running:0 aborted 1 > $W/abort_then_mem/ec2.2
e exited:2 aborted-settled 1 > $W/abort_then_mem/ec2.4; g yes 21000000 > $W/abort_then_mem/gpu.2
scenario abort_then_mem 0 0 1 0 0 "/abort 반복하지 않음"

# 5 PID 확인 불가(N2) → STOP 한 번, TERM 없음
mk pidbad; e running:0 N2 > $W/pidbad/ec2.1; e running:0 N2 > $W/pidbad/ec2.2; e running:0 aborted 1 > $W/pidbad/ec2.4; e exited:2 aborted-settled 1 > $W/pidbad/ec2.5
g no > $W/pidbad/gpu.2
scenario pidbad 1 0 0 0 0 "PID 확인 불가"

# 6 driver 종료(abort 실패·미정착) 뒤에도 running → 감시 계속, 정리 확인 뒤 종료(②). /abort 는 이미 나가서 반복 안 함
mk drv_exit_running; e running:0 N2 > $W/drv_exit_running/ec2.1; e exited:2 abort-failed-unsettled 1 > $W/drv_exit_running/ec2.2
chk '[101]' '[100,101]' true processing/N2 > $W/drv_exit_running/chk.1; chk '[101]' '[100,101]' true processing/N2 > $W/drv_exit_running/chk.2
chk '[]' '[100,101]' true draft/N1 > $W/drv_exit_running/chk.3
scenario drv_exit_running 0 0 0 0 0 "종료: 중단 후 이번 실행 진행 작업·큐·열린 인계 정리 확인"

# 7 driver 가 /abort 없이 죽음(N2 진행 중) → 단발 /abort 한 번, 정리 확인 후 종료
mk drv_dead; e running:0 N2 > $W/drv_dead/ec2.1; e running:0 N2 > $W/drv_dead/ec2.2; e exited:137 N2 0 > $W/drv_dead/ec2.3
chk '[101]' '[100,101]' false processing/N2 > $W/drv_dead/chk.1; chk '[101]' '[100,101]' true processing/N2 > $W/drv_dead/chk.2
chk '[]' '[100,101]' true draft/N1 > $W/drv_dead/chk.3
scenario drv_dead 0 1 0 0 0 "감시 불능으로 중단"

# 8 driver 종료 뒤 새 시도 발생 → 비상 중지(TERM·maintenance 각 한 번), maintenance 결과 수집 후 정리 확인
mk post_new; e running:0 N2 > $W/post_new/ec2.1; e exited:2 aborted-unsettled 1 > $W/post_new/ec2.2
chk '[101]' '[100,101]' true processing/N2 > $W/post_new/chk.1; chk '[103]' '[100,101,103]' true processing/N2 > $W/post_new/chk.2
chk '[]' '[100,101,103]' true draft/N1 > $W/post_new/chk.3
printf 'applied=maintenance-in-progress\n' > $W/post_new/rc.1; printf '0\napplied=maintenance\n' > $W/post_new/rc.2
scenario post_new 0 0 1 1 0 "maintenance 종료 코드 0, applied=maintenance" "종료: 중단 후 이번 실행 진행 작업·큐·열린 인계 정리 확인"

# 8b 새 시도 → 비상 중지 결과 수집 뒤에도 정리 안 됨 → maintenance 완료만으로 끝내지 않고 인계(종료 2)
mk post_new_unsettled; e running:0 N2 > $W/post_new_unsettled/ec2.1; e exited:2 aborted-unsettled 1 > $W/post_new_unsettled/ec2.2
chk '[101]' '[100,101]' true processing/N2 > $W/post_new_unsettled/chk.1; chk '[103]' '[100,101,103]' true processing/N2 > $W/post_new_unsettled/chk.2
printf '0
applied=maintenance
' > $W/post_new_unsettled/rc.1
MAXS=4 scenario post_new_unsettled 0 0 1 1 2 "maintenance 종료 코드 0" "사용자에게 감시 인계"

# 8c 감시 준비 실패(학교 기준 조회 실패) → 준비 표시 없음, 시작 안 함(종료 3)
mk not_ready; e running:0 waiting-watcher > $W/not_ready/ec2.1; echo "ssh: timeout" > $W/not_ready/gpu.1
scenario not_ready 0 0 0 0 3 "준비 표시 안 함" "준비 표시 안 함" 0

# 8d analyze 전(waiting-watcher) 이상 → STOP 기록(driver 는 analyze 를 보내지 않음), driver 미발행 종료 후 정리 확인
mk prestart_s4; e running:0 waiting-watcher > $W/prestart_s4/ec2.1; e running:0 waiting-watcher 0 "$CX" > $W/prestart_s4/ec2.2
e exited:3 not-started-stop 0 "$CX" > $W/prestart_s4/ec2.3; chk '[]' '[]' false draft/N1 > $W/prestart_s4/chk.1
scenario prestart_s4 1 0 0 0 0 "STOP 기록" "종료: analyze 미발행(not-started-stop)"

# 9 driver 가 uncontrolled 표시 → 비상 중지 한 번씩
mk uncontrolled; e running:0 N2 > $W/uncontrolled/ec2.1; e exited:4 uncontrolled 1 > $W/uncontrolled/ec2.2
chk '[]' '[100,101,104]' true draft/N1 > $W/uncontrolled/chk.1
printf 'applied=maintenance-in-progress\n' > $W/uncontrolled/rc.1; printf '0\napplied=maintenance\n' > $W/uncontrolled/rc.2
scenario uncontrolled 0 0 1 1 0 "비상 중지"

# 10 끝까지 정리 확인 불가(running 지속) → 사용자에게 감시 인계(종료 2)
mk never; e running:0 N2 > $W/never/ec2.1; e exited:2 abort-failed-unsettled 1 > $W/never/ec2.2
chk '[101]' '[100,101]' true processing/N2 > $W/never/chk.1
MAXS=3 scenario never 0 0 0 0 2 "사용자에게 감시 인계"

# 11 정리 확인 조회 실패는 정리로 보지 않음 → 인계(종료 2)
mk chkfail; e running:0 N2 > $W/chkfail/ec2.1; e exited:0 N3 > $W/chkfail/ec2.2; echo "docker: error" > $W/chkfail/chk.1
MAXS=3 scenario chkfail 0 0 0 0 2 "정리된 것으로 보지 않음"

# 12 조회 1회 실패는 넘어가고, 3회 연속 실패는 STOP 한 번
mk probe1; e running:0 N2 > $W/probe1/ec2.1; echo "ssh: timeout" > $W/probe1/ec2.2; e running:0 N2 > $W/probe1/ec2.3; e exited:0 N3 > $W/probe1/ec2.4
chk '[]' '[100,101]' false review/N3 > $W/probe1/chk.1
scenario probe1 0 0 0 0 0 "EC2 조회 실패 — 정상으로 보지 않음"
mk probe3; e running:0 N2 > $W/probe3/ec2.1; e running:0 N2 > $W/probe3/ec2.2; for i in 3 4 5; do echo "ssh: timeout" > $W/probe3/ec2.$i; done
e exited:2 aborted-settled 1 > $W/probe3/ec2.6
scenario probe3 1 0 0 0 0 "S4: 감시 조회 연속 실패"

# 13 중단 후 단계에서 PID 불일치 → 기록만(중복 /abort 없음)
mk late; e running:0 aborted 1 > $W/late/ec2.1; e running:0 aborted 1 > $W/late/ec2.2; e exited:2 aborted-settled 1 > $W/late/ec2.3
g no > $W/late/gpu.2
scenario late 0 0 0 0 0 "/abort 반복하지 않음"

# ===== N4 (driver_n4 check: after_abort·unexpected 포함) =====
# chk4 <active> <ids> <abort_sent> <job> <after_abort>
chk4() { printf 'CHECK {"run_id": 300, "job": "%s", "attempt_ids": %s, "active": %s, "retries": [], "unexpected": [], "after_abort": %s, "open_handoffs": 0, "queues": {"ocr": 0, "cpu": 0, "gpu": 0, "celery": 0, "unacked": 0}, "other_active": 0, "abort_sent": %s}\n' "$4" "$2" "$1" "$5" "$3"; }

# 14 N4 정상 → N5(대표 행 running 은 check 가 N5 에서만 제외) → 종료
mk n4_normal; e running:0 N4 > $W/n4_normal/ec2.1; e running:0 N4 > $W/n4_normal/ec2.2; e exited:0 N5-settled > $W/n4_normal/ec2.3
chk4 '[]' '[300,400,401,402]' false review/N5 '[]' > $W/n4_normal/chk.1
scenario n4_normal 0 0 0 0 0 "종료: N5 도달 + 이번 실행 잔여 작업 정리 확인" "검증 판정은 n5_verify 로 별도"

# 15 N4 중 driver 사망 → 정상 후속 시도(새 id)가 생겨도 비상 중지 아님, 단발 /abort 1회 → 정리 후 종료
mk n4_dead_followups; e running:0 N4 > $W/n4_dead_followups/ec2.1; e exited:137 N4 0 > $W/n4_dead_followups/ec2.2
chk4 '[300,400]' '[300,400]' false processing/N4 '[]' > $W/n4_dead_followups/chk.1
chk4 '[300,401]' '[300,400,401]' true processing/N4 '[]' > $W/n4_dead_followups/chk.2
chk4 '[]' '[300,400,401]' true review/N3 '[]' > $W/n4_dead_followups/chk.3
scenario n4_dead_followups 0 1 0 0 0 "감시 불능으로 중단" "종료: 중단 후 이번 실행 진행 작업"

# 16 중단 이후 생성된 시도 → 비상 중지(TERM·maintenance 각 1회), 결과 수집 후 정리 확인
mk n4_after_abort; e running:0 N4 > $W/n4_after_abort/ec2.1; e exited:2 aborted-unsettled 1 > $W/n4_after_abort/ec2.2
chk4 '[410]' '[300,400,410]' true processing/N4 '[410]' > $W/n4_after_abort/chk.1
chk4 '[]' '[300,400,410]' true review/N3 '[410]' > $W/n4_after_abort/chk.2
printf '0\napplied=maintenance\n' > $W/n4_after_abort/rc.1
scenario n4_after_abort 0 0 1 1 0 "중단 요청 이후 관측된 새 시도 1건" "maintenance 종료 코드 0"

# 17 N4 에서 대표 행만 running(하위 0) → 정리 아님(N5 아님) → 인계(종료 2)
mk n4_runrow; e running:0 N4 > $W/n4_runrow/ec2.1; e exited:2 abort-failed-unsettled 1 > $W/n4_runrow/ec2.2
chk4 '[300]' '[300,400]' true processing/N4 '[]' > $W/n4_runrow/chk.1
MAXS=3 scenario n4_runrow 0 0 0 0 2 "사용자에게 감시 인계"

echo "$pass/$((pass+fail)) 통과"
rm -rf "$W"
[ $fail = 0 ]
