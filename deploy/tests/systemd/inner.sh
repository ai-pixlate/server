#!/bin/bash
# deploy/tests/fe_systemd.sh 가 격리 컨테이너(systemd + Docker) 안에서 실행한다. /draft = 레포 deploy/(읽기 전용). 값은 모두 가짜.
#   main   : 설치 → 시나리오 1~9
#   reboot : 컨테이너 재시작(=재부팅) 뒤 확인
# 시험 속도를 위해 drop-in 으로 RestartSec·TimeoutStopSec·STOP_WAIT_S·HEALTH_WAIT_S 만 줄인다(나머지는 레포 단위 파일 그대로).
set -uo pipefail
PASS=0; FAIL=0
chk() { local n=$1; shift; if "$@"; then echo "  PASS $n"; PASS=$((PASS+1)); else echo "  FAIL $n"; FAIL=$((FAIL+1)); fi; }
waitfor() { local n=$1; shift; for _ in $(seq "$n"); do "$@" && return 0; sleep 1; done; return 1; }
fest()  { docker inspect -f '{{.State.Status}}' pixlate-fe 2>/dev/null || echo absent; }
fexit() { docker inspect -f '{{.State.ExitCode}}' pixlate-fe 2>/dev/null; }
ust()   { systemctl show pixlate-fe.service -p ActiveState --value; }
bst()   { systemctl show pixlate.service -p ActiveState --value; }
fe_up()   { [ "$(fest)" = running ] && [ "$(docker inspect -f '{{.State.Health.Status}}' pixlate-fe 2>/dev/null)" = healthy ] && [ "$(ust)" = active ]; }
fe_down() { [ "$(fest)" != running ] && [ "$(ust)" = inactive ]; }
fe_after_be() { [ "$(date -d "$(docker inspect -f '{{.State.StartedAt}}' pixlate-fe)" +%s%N)" -gt "$(cat /tmp/be_done_ns)" ]; }
nrestarts() { systemctl show pixlate-fe.service -p NRestarts --value; }
mode() { echo "$1" > /etc/pixlate/deploy.mode; }
dg() { docker inspect -f '{{index .RepoDigests 0}}' "localhost:5000/fake-fe:$1" | cut -d@ -f2; }
setimg() { printf 'FE_IMAGE=localhost:5000/fake-fe@%s\nFE_PORT=3000\nFE_MEM_LIMIT=64m\n' "$(dg "$1")" > /etc/pixlate/fe.env; }

case "${1:-main}" in
main)
  waitfor 90 systemctl is-active -q docker || { echo "docker.service 미기동"; exit 1; }
  mkdir -p /etc/pixlate /opt/pixlate/fe /etc/systemd/system/pixlate-fe.service.d
  install -m755 /draft/fe/pixlate-fe.sh /opt/pixlate/fe/; install -m644 /draft/fe/compose.yaml /opt/pixlate/fe/
  install -m755 /draft/tests/systemd/fake-start-pixlate.sh /opt/pixlate/start-pixlate.sh
  install -m644 /draft/pixlate.service /draft/fe/pixlate-fe.service /etc/systemd/system/
  printf '[Service]\nRestartSec=3\nTimeoutStopSec=20\nEnvironment=STOP_WAIT_S=6 HEALTH_WAIT_S=40\n' > /etc/systemd/system/pixlate-fe.service.d/test.conf
  docker run -d --name reg --restart unless-stopped -p 5000:5000 registry:2 >/dev/null; sleep 2
  for m in exit ignore; do
    docker build -q --build-arg TERM_MODE=$m -t localhost:5000/fake-fe:$m /draft/tests/fake-fe >/dev/null && docker push -q localhost:5000/fake-fe:$m >/dev/null
  done
  docker pull -q busybox:1.36 >/dev/null
  setimg exit; mode live; echo ok > /etc/pixlate/fake-be
  grep -qE '@sha256:[0-9a-f]{64}$' /etc/pixlate/fe.env || { echo "준비 실패: 가짜 FE 이미지 digest 없음(안쪽 Docker 확인)"; echo "RESULT pass=0 fail=1"; exit 1; }
  systemctl daemon-reload; systemctl enable -q pixlate.service pixlate-fe.service
  chk "단위 파일 검증(systemd-analyze verify)" systemd-analyze verify /etc/systemd/system/pixlate-fe.service
  chk "WantedBy=pixlate.service 링크" test -L /etc/systemd/system/pixlate.service.wants/pixlate-fe.service

  echo "== 1. BE 시작이 FE 를 함께 시작하고, FE 는 BE 완료 뒤에 뜬다"
  systemctl start pixlate.service
  chk "FE running/healthy·unit active" waitfor 60 fe_up
  chk "FE 시작 시각 > BE 완료 시각" fe_after_be

  echo "== 2. maintenance 전환(모드 기록 + pixlate.service 재시작 = pixlate-mode 와 같은 동작) → FE 정지"
  mode maintenance; systemctl restart pixlate.service
  chk "FE 정지·unit inactive" waitfor 30 fe_down
  chk "FE 종료 코드 0(TERM 정지, SIGKILL 137 아님)" test "$(fexit)" = 0
  chk "unit 이 failed 가 아님" test "$(systemctl show pixlate-fe.service -p Result --value)" = success

  echo "== 3. live 로 되돌리면 꺼져 있던 FE 도 다시 시작(PartOf 는 활성 유닛만 재시작 → Wants 로 시작)"
  mode live; systemctl restart pixlate.service
  chk "FE running/healthy·unit active" waitfor 60 fe_up

  echo "== 4. BE live 적용 실패(live-incomplete) — 네트워크·API 는 남아 있어도 FE 를 띄우지 않는다"
  echo fail > /etc/pixlate/fake-be; systemctl restart pixlate.service
  chk "pixlate.service failed" test "$(bst)" = failed
  chk "applied=live-incomplete, 네트워크·가짜 API 는 남아 있음" bash -c '[ "$(cat /etc/pixlate/deploy.mode.applied)" = live-incomplete ] && docker network inspect pixlate-net >/dev/null && [ "$(docker inspect -f {{.State.Status}} fake-api)" = running ]'
  chk "FE 정지·unit inactive" waitfor 30 fe_down
  chk "journal 에 'BE 가 live 로 적용되지 않음'" bash -c 'journalctl -u pixlate-fe --no-pager | grep -q "applied=live-incomplete"'

  echo "== 5. BE 복구 → FE 다시 기동"
  echo ok > /etc/pixlate/fake-be; systemctl restart pixlate.service
  chk "FE running/healthy·unit active" waitfor 60 fe_up

  echo "== 6. 실행 중 FE 종료 → systemd 가 BE 조건부터 다시 확인하고 다시 띄운다(Docker 재시작 정책 아님)"
  n0=$(nrestarts); docker exec pixlate-fe touch /tmp/crash
  chk "NRestarts 증가" waitfor 60 bash -c "[ \$(systemctl show pixlate-fe.service -p NRestarts --value) -gt $n0 ]"
  chk "FE running/healthy·unit active" waitfor 60 fe_up
  chk "컨테이너 재시작 정책 no" test "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' pixlate-fe)" = no

  echo "== 7. systemctl stop pixlate-fe → TERM 정지"
  systemctl stop pixlate-fe.service
  # systemctl show -p A,B --value 는 요청 순서가 아니라 내부 순서로 출력하므로 하나씩 조회한다
  chk "FE exited·종료 코드 0" test "$(docker inspect -f '{{.State.Status}}/{{.State.ExitCode}}' pixlate-fe)" = exited/0
  chk "unit inactive·Result success" test "$(ust)/$(systemctl show pixlate-fe.service -p Result --value)" = inactive/success
  systemctl start pixlate-fe.service
  chk "다시 start → FE running" waitfor 60 fe_up

  echo "== 8. Docker 데몬 재시작"
  systemctl restart docker
  chk "FE 가 다시 running/healthy·unit active(90초 안)" waitfor 90 fe_up
  chk "BE 도 active" test "$(bst)" = active
  echo "  (관찰) pixlate.service=$(bst) fe=$(fest)/$(ust) NRestarts=$(nrestarts)"

  echo "== 9. 정지 대기 초과(TERM 무시 FE) — 강제 종료하지 않는다"
  setimg ignore; systemctl restart pixlate-fe.service
  chk "TERM 무시 FE running" waitfor 60 fe_up
  systemctl stop pixlate-fe.service
  chk "STOP_WAIT_S 뒤에도 컨테이너 running(SIGKILL 없음)" bash -c 'sleep 3; [ "$(docker inspect -f {{.State.Status}} pixlate-fe)" = running ]'
  chk "journal 에 '끝나지 않음(running, 강제 종료하지 않음)'" bash -c 'journalctl -u pixlate-fe --no-pager | grep -q "강제 종료하지 않음"'
  echo "  (관찰) unit=$(ust)/$(systemctl show pixlate-fe.service -p Result --value)"
  docker rm -f pixlate-fe >/dev/null   # 시험 컨테이너 정리(격리 환경 안)
  setimg exit; systemctl reset-failed pixlate-fe.service 2>/dev/null; systemctl start pixlate-fe.service
  chk "재부팅 시험 준비: FE running" waitfor 60 fe_up
  ;;
reboot)
  waitfor 120 bash -c 'systemctl is-system-running 2>/dev/null | grep -qE "running|degraded"'
  echo "== 10. 재부팅(컨테이너 재시작) 뒤"
  chk "pixlate.service active(success)" waitfor 90 bash -c '[ "$(systemctl show pixlate.service -p ActiveState --value)" = active ]'
  chk "FE running/healthy·unit active" waitfor 90 fe_up
  chk "FE 시작 시각 > 이번 부팅의 BE 완료 시각(Docker 가 BE 보다 먼저 띄우지 않음)" fe_after_be
  chk "FE 재시작 횟수 0(이번 부팅에서 한 번에 기동)" test "$(nrestarts)" = 0
  ;;
esac
echo "RESULT pass=$PASS fail=$FAIL"
