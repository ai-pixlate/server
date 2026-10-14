#!/bin/bash
# deploy/tests/run.sh fe(또는 fe_mutation.sh)가 docker:27-dind 안에서 실행한다. 호스트 Docker 의 컨테이너는 건드리지 않는다.
# pixlate-fe.sh·compose.yaml 시험 — 가짜 FE(busybox, tests/fake-fe)·가짜 API·로컬 레지스트리·systemctl 흉내. 값은 모두 가짜.
#
#   FE_TESTS : 실행할 묶음(빈 값이면 전체) — compose gate start crash stop stubborn query paused down daemon output
#   FE_SRC   : 시험할 fe 폴더(기본 /draft/fe). 변형 시험이 결함을 넣은 사본을 넘긴다
# 공통 준비(cgroup 위임·레지스트리·가짜 이미지)는 dind 안에서 한 번만 하고(/tmp/fe-setup.done) 재사용한다.
# 묶음마다 상태를 처음부터 다시 만든다(FE·가짜 API·네트워크·모드 파일).
# 시험용 대기값: 정지 대기 3초(TERM 무시 FE 로 '대기 초과 → 미완료' 확인), healthy 20초, run 상한 30초. 운영 값은 바꾸지 않는다.
#
# 결과 줄: PASS <ID> <설명> / FAIL <ID> <설명> [사유]. 사유가 '시간 초과'뿐이면 결함 검출로 보지 않는다(fe_mutation.sh).
# 마지막 줄: RESULT pass= fail= timeout= env= (timeout = 사유가 시간 초과뿐인 FAIL 수, env = 환경 준비 실패 수)
set -uo pipefail
FE_SRC=${FE_SRC:-/draft/fe}
ALL="compose gate start crash stop stubborn query paused down daemon output"
FE_TESTS=${FE_TESTS:-$ALL}
export CONF_DIR=/etc/pixlate FE_DIR=/opt/pixlate/fe RUN_DIR=/tmp/fe-run HEALTH_WAIT_S=20 STOP_WAIT_S=3 SYSTEMCTL=/tmp/systemctl
RUN_LIMIT_S=30
S=/opt/pixlate/fe/pixlate-fe.sh
PASS=0; FAIL=0; TMO=0; ENVF=0

# ── 결과 기록 ─────────────────────────────────────────────
# T <ID> <설명> <검사 함수> : 검사 함수는 성공이면 0, 실패면 사유를 출력하고 1(하위 셸에서 돈다 — 상태를 바꾸는 동작은 밖에서)
T() {
  local id=$1 desc=$2 why
  if why=$("$3" 2>&1); then echo "  PASS $id $desc"; PASS=$((PASS+1))
  else why=${why:-검사 실패}; echo "  FAIL $id $desc [$why]"; FAIL=$((FAIL+1)); [ "$why" = "시간 초과" ] && TMO=$((TMO+1)); fi
}
env_fail() { echo "  ENV $*"; ENVF=$((ENVF+1)); }
finish() { echo "RESULT pass=$PASS fail=$FAIL timeout=$TMO env=$ENVF"; exit 0; }

# ── 공통 준비(한 번) ──────────────────────────────────────
setup_once() {
  [ -e /tmp/fe-setup.done ] && return 0
  apk add -q bash coreutils procps >/dev/null 2>&1
  # cgroup v2 dind: 메모리 상한(mem_limit) 컨테이너를 띄우려면 하위 cgroup 에 컨트롤러를 위임해야 한다
  # (docker:dind 기본 entrypoint 가 하는 일. run.sh 는 그것을 거치지 않으므로 여기서 하고 dockerd 를 다시 띄운다)
  if [ -f /sys/fs/cgroup/cgroup.controllers ] && [ ! -d /sys/fs/cgroup/init ]; then
    pkill dockerd; for _ in $(seq 30); do pgrep dockerd >/dev/null || break; sleep 1; done
    mkdir -p /sys/fs/cgroup/init
    xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || true
    sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers > /sys/fs/cgroup/cgroup.subtree_control
    (dockerd >/var/log/dockerd.log 2>&1 &)
  fi
  for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker info >/dev/null 2>&1 || { env_fail "dockerd 미기동"; return 1; }
  docker run -d --name reg --restart always -p 5000:5000 registry:2 >/dev/null 2>&1 || { env_fail "레지스트리 기동 실패"; return 1; }
  for _ in $(seq 20); do wget -q -O /dev/null http://localhost:5000/v2/ 2>/dev/null && break; sleep 1; done
  local m
  for m in exit ignore; do
    { docker build -q --build-arg TERM_MODE=$m -t localhost:5000/fake-fe:$m /draft/tests/fake-fe >/dev/null 2>&1 \
      && docker push -q localhost:5000/fake-fe:$m >/dev/null 2>&1; } || { env_fail "가짜 FE($m) 빌드·push 실패"; return 1; }
  done
  docker pull -q busybox:1.36 >/dev/null 2>&1 || { env_fail "busybox pull 실패"; return 1; }
  printf '#!/bin/sh\ncase "$*" in *ActiveState*) cat /tmp/be_active ;; *Result*) cat /tmp/be_result ;; esac\n' > /tmp/systemctl
  printf '#!/bin/sh\necho "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?" >&2; exit 1\n' > /tmp/dockerfail
  chmod +x /tmp/systemctl /tmp/dockerfail
  touch /tmp/fe-setup.done
}
setup_once || finish

dg() { docker inspect -f '{{index .RepoDigests 0}}' localhost:5000/fake-fe:$1 | cut -d@ -f2; }
FE_OK=localhost:5000/fake-fe@$(dg exit); FE_STUB=localhost:5000/fake-fe@$(dg ignore)
mkdir -p /etc/pixlate "$FE_DIR"
{ cp "$FE_SRC/compose.yaml" "$FE_SRC/pixlate-fe.sh" "$FE_DIR/" && chmod +x "$S"; } || { env_fail "시험 대상($FE_SRC) 복사 실패"; finish; }

# ── 상태 도구 ─────────────────────────────────────────────
st()       { docker inspect -f '{{.State.Status}}' pixlate-fe 2>/dev/null || echo absent; }
fe_count() { docker ps -aq -f name='^pixlate-fe$' | wc -l; }
setenv()   { printf 'FE_IMAGE=%s\nFE_PORT=3000\nFE_MEM_LIMIT=64m\n' "$1" > /etc/pixlate/fe.env; }
be()       { echo "$1" > /etc/pixlate/deploy.mode; echo "$2" > /etc/pixlate/deploy.mode.applied; echo "$3" > /tmp/be_active; echo "$4" > /tmp/be_result; }
api_up()   { docker run -d --name fake-api --network pixlate-net --network-alias pixlate-api busybox:1.36 sh -c 'mkdir -p /w && echo ok > /w/index.html && exec httpd -f -p 8000 -h /w' >/dev/null; }
RUN_PID=""
stop_bg()  { if [ -n "$RUN_PID" ]; then pkill -P "$RUN_PID" 2>/dev/null; kill "$RUN_PID" 2>/dev/null; wait "$RUN_PID" 2>/dev/null; fi; RUN_PID=""; }
# 묶음 시작 상태: BE live 적용 성공, 네트워크·가짜 API 있음, FE 없음
reset_state() {
  stop_bg
  docker rm -f pixlate-fe fake-api >/dev/null 2>&1; docker network rm pixlate-net >/dev/null 2>&1
  rm -rf "$RUN_DIR" /tmp/o /tmp/o2 /tmp/run.log; setenv "$FE_OK"; be live live active success
  docker network create pixlate-net >/dev/null && api_up
}
# run 을 동기로(상한 RUN_LIMIT_S). RRC = 종료 코드 또는 timeout(이때 run 과 그 하위 프로세스만 끝낸다 — 컨테이너는 그대로)
run_sync() {
  : > /tmp/o; "$@" bash $S run > /tmp/o 2>&1 & local p=$!
  for _ in $(seq $((RUN_LIMIT_S*2))); do kill -0 $p 2>/dev/null || break; sleep 0.5; done
  if kill -0 $p 2>/dev/null; then pkill -P $p 2>/dev/null; kill $p 2>/dev/null; wait $p 2>/dev/null; RRC=timeout; else wait $p; RRC=$?; fi
}
# run 을 백그라운드로 띄우고 '기동' 로그 또는 종료까지(상한 RUN_LIMIT_S). 본 셸에서 부른다
run_bg() {
  : > /tmp/run.log; bash $S run > /tmp/run.log 2>&1 & RUN_PID=$!
  for _ in $(seq $((RUN_LIMIT_S*2))); do grep -q '기동(' /tmp/run.log && return 0; kill -0 $RUN_PID 2>/dev/null || return 0; sleep 0.5; done
}
# 백그라운드 run 의 종료를 기다린다(상한 RUN_LIMIT_S). RC = 종료 코드 또는 timeout
run_wait() {
  for _ in $(seq $((RUN_LIMIT_S*2))); do kill -0 $RUN_PID 2>/dev/null || break; sleep 0.5; done
  if kill -0 $RUN_PID 2>/dev/null; then stop_bg; RC=timeout; else wait $RUN_PID; RC=$?; RUN_PID=""; fi
}
# 사유 조립: 결함을 드러내는 사유를 먼저, 시간 초과는 따로 붙인다
why()   { local r=() a; for a in "$@"; do [ -n "$a" ] && r+=("$a"); done; local IFS=';'; echo "${r[*]}"; }
rc_is() { [ "$RRC" = "$1" ] && return 0; [ "$RRC" = timeout ] || echo "종료 코드 $RRC"; return 1; }
tmo()   { [ "$RRC" = timeout ] && echo "시간 초과"; }
fail_if() { [ -z "$1" ] && return 0; echo "$1"; return 1; }

sel() { case " $FE_TESTS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# ── compose ───────────────────────────────────────────────
if sel compose; then echo "== compose 설정"
  c1() { docker compose -f $FE_DIR/compose.yaml --env-file /etc/pixlate/fe.env config 2>/dev/null | grep -qE 'restart: "?no"?' || { echo "restart 가 no 가 아님"; return 1; }; }
  c2() { ! docker compose -f $FE_DIR/compose.yaml --env-file /dev/null config -q 2>/dev/null || { echo "FE_IMAGE 없이 통과"; return 1; }; }
  reset_state; T C1 "compose config 유효, restart \"no\"(재시작 주체는 systemd)" c1; T C2 "FE_IMAGE 없으면 compose 가 거부" c2
fi

# ── gate: BE 가 live 로 적용되지 않으면 띄우지 않는다 ────────
gate_case() {  # be 인자 4개 → run → 컨테이너 없음·종료 코드 0
  reset_state; be "$@"; run_sync
  local a=""; [ "$(fe_count)" = 0 ] || a="FE 컨테이너가 만들어짐($(st))"
  fail_if "$(why "$a" "$(rc_is 0)" "$(tmo)")"
}
if sel gate; then echo "== BE 적용 결과 확인(요청·적용·이번 기동)"
  g1() { gate_case maintenance maintenance active success; }
  g2() { gate_case live live-incomplete failed exit-code || return 1; grep -q 'applied=live-incomplete' /tmp/o || { echo "로그에 applied=live-incomplete 없음"; return 1; }; }
  g3() { gate_case live live failed exit-code; }
  g4() { gate_case live live-in-progress active success; }
  T G1 "requested=maintenance → 0, 컨테이너 없음" g1
  T G2 "requested=live·applied=live-incomplete(네트워크·API 는 남아 있음) → 0, 컨테이너 없음" g2
  T G3 "applied=live 라도 pixlate.service failed → 0, 컨테이너 없음" g3
  T G4 "applied=live-in-progress → 0, 컨테이너 없음" g4
fi

# ── start: 기동 조건 ───────────────────────────────────────
if sel start; then echo "== 기동"
  no_fe_rc1() { local a=""; [ "$(fe_count)" = 0 ] || a="FE 컨테이너가 만들어짐($(st))"; fail_if "$(why "$a" "$(rc_is 1)" "$(tmo)")"; }
  s1() { reset_state; setenv "${FE_OK%@*}:exit"; run_sync; no_fe_rc1; }
  s2() { reset_state; docker rm -f fake-api >/dev/null; docker network rm pixlate-net >/dev/null; run_sync; no_fe_rc1; }
  s3() { reset_state; docker rm -f fake-api >/dev/null; run_sync
         local a="" b=""; [ "$(st)" = exited ] || a="FE 가 정지되지 않음($(st))"; grep -q 'pixlate-api:8000 에 닿지 않음' /tmp/o || b="API 미연결 보고 없음"
         fail_if "$(why "$a" "$b" "$(rc_is 1)" "$(tmo)")"; }
  s4() { local r=""; kill -0 "$RUN_PID" 2>/dev/null || r="run 이 끝남"
         [ "$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' pixlate-fe 2>/dev/null)" = "running healthy" ] || r=$(why "$r" "FE running/healthy 아님")
         fail_if "$r"; }
  s5() { [ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}} {{.HostConfig.Init}} {{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' pixlate-fe 2>/dev/null)" = 'no true pixlate-net' ] || { echo "재시작 정책·init·네트워크 불일치"; return 1; }; }
  T S1 "FE_IMAGE 가 태그 → 1, 컨테이너 없음" s1
  T S2 "BE 네트워크 없음 → 1, 컨테이너 없음" s2
  T S3 "API 에 닿지 않음(healthcheck 는 통과) → 1, FE 는 TERM 으로 정지" s3
  reset_state; run_bg
  T S4 "정상: run 이 감시 상태로 남고 FE running/healthy" s4
  T S5 "Docker 재시작 정책 no·init·pixlate-net 만" s5
fi

# ── crash: 실행 중 종료 감지 ───────────────────────────────
if sel crash; then echo "== 실행 중 종료 감지"
  reset_state; run_bg; docker exec pixlate-fe touch /tmp/crash; run_wait; sleep 3
  x1() { [ "$RC" = 1 ] && grep -q '실행 중 끝났다(exit=3)' /tmp/run.log && return 0; [ "$RC" = timeout ] && echo "시간 초과" || echo "run 종료 코드 $RC 또는 보고 없음"; return 1; }
  x2() { [ "$(st)" = exited ] || { echo "Docker 가 FE 를 다시 띄움($(st))"; return 1; }; }
  T X1 "FE 가 실행 중 끝나면 run 이 1 로 끝남(systemd 재시작 대상)" x1
  T X2 "Docker 가 스스로 다시 띄우지 않음" x2
fi

# ── stop: TERM 정지 ───────────────────────────────────────
if sel stop; then echo "== 정지(TERM 만)"
  reset_state; run_bg; bash $S stop > /tmp/o 2>&1; SRC=$?; run_wait
  p1() { [ "$SRC" = 0 ] && grep -q '정지 확인(exited)' /tmp/o && [ "$(st)" = exited ] || { echo "stop 종료 코드 $SRC·상태 $(st)"; return 1; }; }
  p2() { [ "$RC" = 0 ] && grep -q '정지 요청에 따른 종료' /tmp/run.log && return 0; [ "$RC" = timeout ] && echo "시간 초과" || echo "run 종료 코드 $RC(정지 요청인데 실패로 끝남 — systemd 가 재시작)"; return 1; }
  p3() { local c; c=$(docker inspect -f '{{.State.ExitCode}}' pixlate-fe); [ "$c" = 0 ] || { echo "종료 코드 $c"; return 1; }; }
  T P1 "stop: TERM 정지 확인 → 0" p1
  T P2 "정지 요청에 따른 종료는 run 이 0 으로 끝남(재시작 안 함)" p2
  T P3 "TERM 정지 종료 코드 0(SIGKILL 137 아님)" p3
fi

# ── stubborn: 정지 대기 초과 ───────────────────────────────
if sel stubborn; then echo "== 정지 대기 초과(TERM 무시 FE, 대기 ${STOP_WAIT_S}초)"
  reset_state; setenv "$FE_STUB"; run_bg; bash $S stop > /tmp/o 2>&1; SRC=$?; sleep 2
  k1() { [ "$SRC" = 1 ] && grep -q '끝나지 않음(running, 강제 종료하지 않음)' /tmp/o || { echo "stop 종료 코드 $SRC·미완료 보고 없음"; return 1; }; }
  k2() { [ "$(st)" = running ] || { echo "컨테이너가 강제로 끝남($(st), exit=$(docker inspect -f '{{.State.ExitCode}}' pixlate-fe 2>/dev/null))"; return 1; }; }
  T K1 "TERM 무시 FE: 대기 초과 → 미완료 보고·1" k1
  T K2 "강제 종료하지 않음: 여전히 running" k2
fi

# ── query: Docker 조회 실패 ───────────────────────────────
if sel query; then echo "== Docker 조회 실패는 '없음'이 아니다"
  reset_state; run_bg; DOCKER=/tmp/dockerfail bash $S stop > /tmp/o 2>&1; SRC=$?
  q1() { [ "$SRC" = 1 ] && grep -q '정지 여부 미확인' /tmp/o && ! grep -q '정지 확인' /tmp/o || { echo "stop 종료 코드 $SRC·'$(tail -1 /tmp/o)'"; return 1; }; }
  q2() { [ "$(st)" = running ] || { echo "FE 상태 $(st)"; return 1; }; }
  T Q1 "stop: 조회 실패 → 1, 정지 미확인 보고, '정지 확인' 없음" q1
  T Q2 "그 사이 FE 는 그대로 running" q2
  stop_bg
  q3() { run_sync env DOCKER=/tmp/dockerfail; local b=""; grep -q 'Docker 조회 실패' /tmp/o || b="조회 실패 보고 없음"; fail_if "$(why "$b" "$(rc_is 1)" "$(tmo)")"; }
  T Q3 "run: 조회 실패 → 1(기동하지 않음)" q3
fi

# ── paused ────────────────────────────────────────────────
if sel paused; then echo "== paused"
  reset_state; run_bg; docker pause pixlate-fe >/dev/null; bash $S stop > /tmp/o 2>&1; SRC=$?
  a1() { [ "$SRC" = 1 ] && grep -q 'paused' /tmp/o && [ "$(st)" = paused ] || { echo "stop 종료 코드 $SRC·상태 $(st)"; return 1; }; }
  T A1 "paused → 1(자동 unpause 하지 않음)" a1
  docker unpause pixlate-fe >/dev/null 2>&1
fi

# ── down: BE 가 live 에서 내려가면 떠 있던 FE 를 정지 ─────────
if sel down; then echo "== BE 가 live 에서 내려가면 떠 있던 FE 를 TERM 으로 정지"
  reset_state; run_bg; bash $S stop >/dev/null 2>&1; run_wait; docker start pixlate-fe >/dev/null; sleep 2
  be maintenance maintenance active success; run_sync
  d1() { local a=""; [ "$(st)" = exited ] || a="FE 가 정지되지 않음($(st))"; grep -q '정지 확인(exited)' /tmp/o || a=$(why "$a" "정지 확인 보고 없음"); fail_if "$(why "$a" "$(rc_is 0)" "$(tmo)")"; }
  T D1 "requested=maintenance 로 run → FE 정지 확인, 0" d1
fi

# ── daemon: Docker 데몬 재시작 ─────────────────────────────
if sel daemon; then echo "== Docker 데몬 재시작"
  reset_state; run_bg
  pkill -TERM dockerd; for _ in $(seq 30); do pgrep dockerd >/dev/null || break; sleep 1; done
  (dockerd >/var/log/dockerd2.log 2>&1 &); for _ in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker info >/dev/null 2>&1 || env_fail "dockerd 재기동 실패"
  run_wait; sleep 5
  r1() { [ "$(st)" = exited ] || { echo "Docker 가 FE 를 다시 띄움($(st))"; return 1; }; }
  r2() { [ "$RC" = 1 ] && return 0; [ "$RC" = timeout ] && echo "시간 초과" || echo "run 종료 코드 $RC"; return 1; }
  T R1 "데몬 재시작 뒤 FE 가 스스로 뜨지 않음(restart no)" r1
  T R2 "감시하던 run 은 1 로 끝남(systemd 가 조건부터 다시 확인)" r2
fi

# ── output ────────────────────────────────────────────────
if sel output; then echo "== 출력"
  reset_state; run_bg; bash $S stop > /tmp/o2 2>&1; run_wait
  o1() { local out; out=$(cat /tmp/run.log /tmp/o2; bash $S status); ! grep -qE 'localhost:5000|sha256:[0-9a-f]{12}' <<<"$out" || { echo "레지스트리 주소·digest 노출"; return 1; }; }
  T O1 "출력에 레지스트리 주소·digest 원문 없음" o1
fi

stop_bg
finish
