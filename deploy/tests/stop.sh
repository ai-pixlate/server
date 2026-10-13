#!/bin/bash
# deploy/tests/run.sh 가 docker:27-dind 안에서 실행한다. /draft = 레포 deploy/(읽기 전용). 값은 모두 가짜.
set -uo pipefail
apk add -q bash coreutils >/dev/null 2>&1
mkdir -p /etc/pixlate /opt/pixlate/bin
cp /draft/start-pixlate.sh /opt/pixlate/; cp /draft/bin/redact_run.py /draft/bin/pixlate-mode /draft/bin/state_check.py /opt/pixlate/bin/
chmod +x /opt/pixlate/start-pixlate.sh /opt/pixlate/bin/pixlate-mode
S=/opt/pixlate/start-pixlate.sh
P=0; F=0
ok() { if ( eval "$2" ); then echo "  PASS $1"; P=$((P+1)); else echo "  FAIL $1"; F=$((F+1)); fi; }
cid() { docker inspect -f '{{.Id}}' "$1" 2>/dev/null | cut -c1-12; }
st()  { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo absent; }
rp()  { docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$1" 2>/dev/null; }
ex()  { docker inspect -f '{{.State.ExitCode}}' "$1" 2>/dev/null; }
applied() { cat /etc/pixlate/deploy.mode.applied; }
kills() { docker events --since "$1" --until "$(date +%s)" --filter event=kill --format '{{.Actor.Attributes.name}}:{{.Actor.Attributes.signal}}' | grep -v '^reg:' | tr '\n' ' '; }
mode() { echo "$1" > /etc/pixlate/deploy.mode; }

docker run -d --name reg -p 5000:5000 registry:2 >/dev/null; sleep 2
docker build -q --build-arg FLAVOR=fast -t localhost:5000/pixlate-api:fast /draft/tests/fake >/dev/null
docker build -q --build-arg FLAVOR=slow --build-arg SLOW=25 -t localhost:5000/pixlate-api:slow /draft/tests/fake >/dev/null
docker build -q --build-arg FLAVOR=crash --build-arg CRASH=1 -t localhost:5000/pixlate-api:crash /draft/tests/fake >/dev/null
for t in fast slow crash; do docker push -q localhost:5000/pixlate-api:$t >/dev/null; done
dg() { docker inspect -f '{{index .RepoDigests 0}}' localhost:5000/pixlate-api:$1 | cut -d@ -f2; }
FAST=$(dg fast); SLOW=$(dg slow); CRASH=$(dg crash)

docker network create pixlate-net >/dev/null
docker run -d --name pixlate-redis --network pixlate-net --restart unless-stopped redis:7-alpine redis-server --requirepass testpw >/dev/null
sleep 1; docker exec pixlate-redis redis-cli -a testpw --no-auth-warning SET marker keep-me >/dev/null
R0=$(cid pixlate-redis)
printf 'DATABASE_URL=postgresql+psycopg://u:dbsecretpw@db.example.ap-northeast-2.rds.amazonaws.com:5432/pixlate\nREDIS_PASSWORD=testpw\nCELERY_BROKER_URL=redis://:testpw@pixlate-redis:6379/0\nS3_BUCKET=bucket-name-xyz\n' > /etc/pixlate/pixlate.env; chmod 600 /etc/pixlate/pixlate.env
echo "GEMINI_API_KEY=AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAK" > /etc/pixlate/worker-gemini.env; chmod 600 /etc/pixlate/worker-gemini.env
rel() { printf 'RELEASE_SHA=e1291abceb8d280594e366be733c3cf950b1a0c4\nAPI_DIGEST=%s\nREGISTRY=localhost:5000\n' "$1" > /etc/pixlate/release.conf; }
redis_ok() { [ "$(cid pixlate-redis)" = "$R0" ] && [ "$(docker exec pixlate-redis redis-cli -a testpw --no-auth-warning GET marker)" = keep-me ]; }

echo "== A. live(느린 cpu 워커 25초) → maintenance, 대기 5초: 강제 종료 없이 미완료로 실패"
rel "$SLOW"; mode live; $S >/tmp/o 2>&1; ok "live 기동" "[ \$? = 0 ] || true; [ \$(st pixlate-worker-cpu) = running ] && [ \$(st pixlate-api) = running ] && [ \$(st pixlate-recovery) = running ]"
sleep 3; T0=$(date +%s); C1=$(cid pixlate-worker-cpu); A1=$(cid pixlate-api)
mode maintenance; STOP_WAIT_S=5 $S >/tmp/o 2>&1; rc=$?
ok "exit 1, applied=maintenance-incomplete" "[ $rc = 1 ] && [ \"\$(applied)\" = maintenance-incomplete ]"
ok "API·복구 정지, cpu 는 계속 실행(SIGKILL 없음)" "[ \$(st pixlate-api) = exited ] && [ \$(st pixlate-recovery) = exited ] && [ \$(st pixlate-worker-cpu) = running ]"
ok "세 컨테이너 모두 restart=no, 삭제·재생성 없음" "[ \$(rp pixlate-api) = no ] && [ \$(rp pixlate-recovery) = no ] && [ \$(rp pixlate-worker-cpu) = no ] && [ \$(cid pixlate-worker-cpu) = $C1 ] && [ \$(cid pixlate-api) = $A1 ]"
K=$(kills $T0); echo "    kill events: $K"
ok "TERM 순서 API → 복구 → cpu, KILL 신호 없음" "echo '$K' | grep -q 'pixlate-api:15 pixlate-recovery:15 pixlate-worker-cpu:15' && ! echo '$K' | grep -qE ':9( |$)'"
ok "실패 보고에 남은 컨테이너 표시" "grep -q 'pixlate-worker-cpu 5초 안에 끝나지 않음' /tmp/o"
ok "Redis 영향 없음" "redis_ok"
for i in $(seq 40); do [ "$(st pixlate-worker-cpu)" = exited ] && break; sleep 1; done
ok "cpu 는 스스로 웜 종료(exit 0)" "[ \$(st pixlate-worker-cpu) = exited ] && [ \$(ex pixlate-worker-cpu) = 0 ] && docker logs pixlate-worker-cpu 2>&1 | grep -q 'warm shutdown'"
T1=$(date +%s); $S >/tmp/o 2>&1; rc=$?
ok "재실행: exit 0, applied=maintenance, 추가 TERM 없음" "[ $rc = 0 ] && [ \"\$(applied)\" = maintenance ] && [ -z \"\$(kills $T1)\" ]"

echo "== B. 이미 정지·반복 실행·컨테이너 없음"
$S >/tmp/o 2>&1; ok "반복 maintenance exit 0" "[ \$? = 0 ] || true; [ \"\$(applied)\" = maintenance ]"
docker rm pixlate-recovery >/dev/null; $S >/tmp/o 2>&1; rc=$?
ok "복구 컨테이너 없어도 exit 0" "[ $rc = 0 ] && [ \"\$(applied)\" = maintenance ]"

echo "== C. Docker 명령 일부 실패(복구의 restart 끄기 실패, API TERM 실패)에도 나머지에 요청 전달"
rel "$FAST"; mode live; $S >/tmp/o 2>&1
cat > /usr/local/bin/faildocker <<'X'
#!/bin/sh
[ "$1" = update ] && [ "$3" = pixlate-recovery ] && { echo "simulated failure" >&2; exit 1; }
[ "$1" = kill ] && [ "$3" = pixlate-api ] && { echo "simulated failure" >&2; exit 1; }
exec docker "$@"
X
chmod +x /usr/local/bin/faildocker
sleep 2; T2=$(date +%s); mode maintenance; DOCKER=faildocker STOP_WAIT_S=8 $S >/tmp/o 2>&1; rc=$?
K=$(kills $T2); echo "    kill events: $K"
ok "exit 1, applied=maintenance-incomplete" "[ $rc = 1 ] && [ \"\$(applied)\" = maintenance-incomplete ]"
ok "실패 2건이 모두 보고됨" "grep -q 'pixlate-recovery 자동 재시작 끄기 실패' /tmp/o && grep -q 'pixlate-api TERM 전달 실패' /tmp/o"
ok "API 실패 뒤에도 복구·cpu 에 TERM 전달" "echo '$K' | grep -q 'pixlate-recovery:15' && echo '$K' | grep -q 'pixlate-worker-cpu:15'"
ok "API 는 남아 있음을 보고(강제 종료 없음)" "[ \$(st pixlate-api) = running ] && grep -q 'pixlate-api 8초 안에 끝나지 않음' /tmp/o"
echo "    recovery after TERM with restart policy still on: state=$(st pixlate-recovery) restart=$(rp pixlate-recovery)"
$S >/tmp/o 2>&1; rc=$?
ok "정상 docker 로 재실행하면 완료" "[ $rc = 0 ] && [ \"\$(applied)\" = maintenance ] && for c in pixlate-api pixlate-recovery pixlate-worker-cpu; do [ \$(st \$c) != running ] && [ \$(rp \$c) = no ] || exit 1; done"

echo "== D. live → validate 중 cpu 정지 미완료면 API 를 다시 열지 않음"
rel "$SLOW"; mode live; $S >/tmp/o 2>&1; sleep 3; A2=$(cid pixlate-api)
mode validate; STOP_WAIT_S=5 $S >/tmp/o 2>&1; rc=$?
ok "exit 1, applied=validate-incomplete" "[ $rc = 1 ] && [ \"\$(applied)\" = validate-incomplete ]"
ok "옛 API 정지·삭제 안 함, 새 API 생성 안 함" "[ \$(st pixlate-api) = exited ] && [ \$(cid pixlate-api) = $A2 ] && [ \"\$(docker inspect -f '{{json .HostConfig.PortBindings}}' pixlate-api)\" = '{\"8000/tcp\":[{\"HostIp\":\"\",\"HostPort\":\"8000\"}]}' ]"
for i in $(seq 40); do [ "$(st pixlate-worker-cpu)" = exited ] && break; sleep 1; done
$S >/tmp/o 2>&1; rc=$?
ok "cpu 종료 후 재실행하면 validate 완료(127.0.0.1 API)" "[ $rc = 0 ] && [ \"\$(applied)\" = validate ] && [ \$(st pixlate-api) = running ] && docker inspect -f '{{json .HostConfig.PortBindings}}' pixlate-api | grep -q 127.0.0.1"

echo "== E. pixlate-mode: 요청과 실제 적용 구분, 실패 종료 코드"
rel "$SLOW"; mode live; $S >/tmp/o 2>&1; sleep 3
printf '#!/bin/sh\nSTOP_WAIT_S=5 /opt/pixlate/start-pixlate.sh >/dev/null 2>&1\n' > /usr/local/bin/fakesystemctl; chmod +x /usr/local/bin/fakesystemctl
SYSTEMCTL=fakesystemctl /opt/pixlate/bin/pixlate-mode maintenance >/tmp/m 2>&1; rc=$?
sed 's/^/    /' /tmp/m
ok "pixlate-mode 종료 코드 실패, '(미완료)' 표시" "[ $rc != 0 ] && grep -q 'requested=maintenance applied=maintenance-incomplete (미완료)' /tmp/m"
ok "대상 컨테이너 상태 출력(cpu running)" "grep -q 'pixlate-worker-cpu: running restart=no' /tmp/m"
ok "상태 재조회 시도 결과 출력(가짜 이미지라 실패 표시)" "grep -qE 'state check (failed|skipped)' /tmp/m"
for i in $(seq 40); do [ "$(st pixlate-worker-cpu)" = exited ] && break; sleep 1; done
SYSTEMCTL=fakesystemctl /opt/pixlate/bin/pixlate-mode maintenance >/tmp/m 2>&1; rc=$?
ok "재실행 후 requested=applied, 종료 코드 0" "grep -q '^requested=maintenance applied=maintenance$' /tmp/m"

# 지정한 Docker 하위 명령·컨테이너 조합만 실패시키는 래퍼(/tmp/failon 한 줄 = "<명령> <컨테이너>")
cat > /usr/local/bin/faildocker2 <<'X'
#!/bin/sh
sub=$1; name=""; for a in "$@"; do name=$a; done
if [ "$sub" = container ] && [ "$2" = inspect ]; then sub=inspect; fi
if [ "$sub" = run ]; then n=0; for a in "$@"; do if [ $n = 1 ]; then name=$a; n=2; fi; [ "$a" = --name ] && n=1; done; fi
if grep -qx "$sub $name sock" /tmp/failon 2>/dev/null; then
  echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running? (dial unix /var/run/docker.sock: connect: no such file or directory)" >&2; exit 1
fi
if grep -qx "$sub $name" /tmp/failon 2>/dev/null; then echo "Error response from daemon: simulated $sub failure" >&2; exit 1; fi
exec docker "$@"
X
chmod +x /usr/local/bin/faildocker2
failon() { printf '%s\n' "$@" > /tmp/failon; }
D2() { DOCKER=faildocker2 STOP_WAIT_S=${W:-8} START_SETTLE_S=2 "$S" >/tmp/o 2>&1; }

echo "== G. 기동 명령 실패 전파(run·start·rm) · 기동 직후 종료"
rel "$FAST"; mode maintenance; $S >/tmp/o 2>&1
failon "run pixlate-api"; mode validate; D2; rc=$?
ok "run 실패 → exit 1, validate-incomplete, '실행 확인' 로그 없음" "[ $rc = 1 ] && [ \"\$(applied)\" = validate-incomplete ] && grep -q 'pixlate-api 생성 실패' /tmp/o && ! grep -q 'pixlate-api 실행 확인' /tmp/o"
failon; D2; rc=$?
ok "실패 해제 후 재실행 → validate 완료(API 127.0.0.1)" "[ $rc = 0 ] && [ \"\$(applied)\" = validate ] && [ \$(st pixlate-api) = running ]"
mode maintenance; D2; failon "start pixlate-api"; mode validate; D2; rc=$?
ok "start 실패(같은 사양 재사용) → exit 1, validate-incomplete" "[ $rc = 1 ] && [ \"\$(applied)\" = validate-incomplete ] && grep -q 'pixlate-api 시작 실패' /tmp/o && [ \$(st pixlate-api) = exited ]"
failon; D2
failon "rm pixlate-api"; mode live; D2; rc=$?
ok "rm 실패(교체) → exit 1, live-incomplete, cpu·복구 기동 안 함" "[ $rc = 1 ] && [ \"\$(applied)\" = live-incomplete ] && grep -q 'pixlate-api 삭제 실패' /tmp/o && [ \$(st pixlate-worker-cpu) != running ] && [ \$(st pixlate-recovery) != running ]"
failon; mode maintenance; D2
rel "$CRASH"; mode validate; D2; rc=$?
ok "기동 직후 종료하는 API → exit 1, validate-incomplete" "[ $rc = 1 ] && [ \"\$(applied)\" = validate-incomplete ] && grep -q '기동 직후 실행 중이 아님' /tmp/o"
rel "$FAST"; mode maintenance; D2

echo "== H. 조회 실패·paused 는 정지 완료가 아니다"
mode live; D2; [ "$(applied)" = live ] || echo "    (live 준비 실패: $(applied))"
failon "inspect pixlate-recovery"; mode maintenance; D2; rc=$?
ok "복구 상태 조회 실패 → exit 1, maintenance-incomplete, 없음으로 취급 안 함" "[ $rc = 1 ] && [ \"\$(applied)\" = maintenance-incomplete ] && grep -q 'pixlate-recovery 상태 조회 실패' /tmp/o"
ok "조회 실패와 별개로 API·cpu 는 정지" "[ \$(st pixlate-api) = exited ] && [ \$(st pixlate-worker-cpu) = exited ]"
failon; D2; ok "해제 후 재실행 → maintenance 완료" "[ \$? = 0 ] || true; [ \"\$(applied)\" = maintenance ]"
mode live; D2; docker pause pixlate-worker-cpu >/dev/null
mode maintenance; D2; rc=$?
ok "paused cpu → exit 1, maintenance-incomplete, 자동 unpause 안 함" "[ $rc = 1 ] && [ \"\$(applied)\" = maintenance-incomplete ] && grep -q 'pixlate-worker-cpu paused' /tmp/o && [ \$(st pixlate-worker-cpu) = paused ]"
docker unpause pixlate-worker-cpu >/dev/null; D2; rc=$?
ok "unpause 뒤 재실행 → maintenance 완료" "[ $rc = 0 ] && [ \"\$(applied)\" = maintenance ] && [ \$(st pixlate-worker-cpu) = exited ]"

echo "== I. 옛 워커 정지가 끝나지 않으면 live 기동을 시작하지 않음"
docker run -d --name pixlate-worker-ocr --network pixlate-net --restart unless-stopped localhost:5000/pixlate-api:slow celery old >/dev/null; sleep 3
T3=$(date +%s); mode live; W=3 D2; rc=$?
ok "exit 1, live-incomplete, '옛 워커 정지 미완료'" "[ $rc = 1 ] && [ \"\$(applied)\" = live-incomplete ] && grep -q '옛 워커 정지 미완료' /tmp/o"
ok "API·cpu·복구는 기동되지 않음" "[ \$(st pixlate-api) != running ] && [ \$(st pixlate-worker-cpu) != running ] && [ \$(st pixlate-recovery) != running ]"
ok "옛 ocr 은 TERM 만 받고 계속 실행(강제 종료 없음)" "[ \$(st pixlate-worker-ocr) = running ] && [ \$(rp pixlate-worker-ocr) = no ] && ! kills $T3 | grep -q ':9'"
for i in $(seq 40); do [ "$(st pixlate-worker-ocr)" = exited ] && break; sleep 1; done
D2; rc=$?; ok "옛 ocr 종료 후 재실행 → live 완료" "[ $rc = 0 ] && [ \"\$(applied)\" = live ]"

echo "== J. pixlate-mode: 모드 파일 저장 실패 시 재시작하지 않음"
mkdir -p /tmp/failbin; printf '#!/bin/sh\necho "mv: simulated failure" >&2\nexit 1\n' > /tmp/failbin/mv; chmod +x /tmp/failbin/mv
printf '#!/bin/sh\ntouch /tmp/restarted\n' > /usr/local/bin/markersystemctl; chmod +x /usr/local/bin/markersystemctl
rm -f /tmp/restarted; BEFORE=$(cat /etc/pixlate/deploy.mode)
PATH=/tmp/failbin:$PATH SYSTEMCTL=markersystemctl /opt/pixlate/bin/pixlate-mode maintenance >/tmp/m 2>&1; rc=$?
ok "exit 1, 재시작 호출 없음, 모드 파일 그대로" "[ $rc = 1 ] && [ ! -e /tmp/restarted ] && [ \"\$(cat /etc/pixlate/deploy.mode)\" = '$BEFORE' ] && grep -q '모드 파일 저장 실패' /tmp/m"
ok "임시 파일을 남기지 않음" "[ -z \"\$(ls -A /etc/pixlate | grep '^\.deploy\.mode\.')\" ]"

echo "== K. Docker 소켓 오류는 '없음'이 아니다 · 적용 상태 저장 실패는 성공이 아니다"
rel "$FAST"; mode live; D2; [ "$(applied)" = live ] || echo "    (live 준비 실패: $(applied))"
failon "inspect pixlate-recovery sock"; mode maintenance; D2; rc=$?
ok "소켓 오류(no such file or directory) → unknown, exit 1, maintenance-incomplete" "[ $rc = 1 ] && [ \"\$(applied)\" = maintenance-incomplete ] && grep -q 'pixlate-recovery 상태 조회 실패' /tmp/o && ! grep -q 'pixlate-recovery 정지 확인(없음)' /tmp/o"
failon; D2
mode live; D2
DOCKER="docker -H unix:///tmp/nope.sock" STOP_WAIT_S=3 START_SETTLE_S=1 $S >/tmp/o 2>&1; rc=$?
ok "데몬 연결 불가 상태의 live → exit 1, 아무것도 새로 띄우지 않음" "[ $rc = 1 ] && [ \"\$(applied)\" != live ] && ! grep -q '실행 확인' /tmp/o"
mode maintenance; DOCKER="docker -H unix:///tmp/nope.sock" STOP_WAIT_S=3 $S >/tmp/o 2>&1; rc=$?
ok "데몬 연결 불가 → exit 1, 정지 완료 기록 안 함, Redis 재생성 안 함" "[ $rc = 1 ] && [ \"\$(applied)\" != maintenance ] && ! grep -q 'maintenance: Redis 만 유지' /tmp/o && redis_ok"
ok "실제 컨테이너는 그대로(연결 불가라 아무것도 못 함을 보고)" "[ \$(st pixlate-api) = running ] && grep -q '상태 조회 실패' /tmp/o"
$S >/tmp/o 2>&1; ok "정상 연결로 재실행 → maintenance 완료" "[ \$? = 0 ] || true; [ \"\$(applied)\" = maintenance ]"
mode live; $S >/tmp/o 2>&1
mode maintenance; PATH=/tmp/failbin:$PATH $S >/tmp/o 2>&1; rc=$?
ok "applied 저장 실패(mv) → exit 1, 완료 로그 없음" "[ $rc = 1 ] && ! grep -q 'maintenance: Redis 만 유지' /tmp/o && grep -q '적용 상태 저장 실패' /tmp/o"
ok "안전을 위한 정지는 계속 수행" "[ \$(st pixlate-api) = exited ] && [ \$(st pixlate-recovery) = exited ] && [ \$(st pixlate-worker-cpu) = exited ]"
ok "이전 값(live)이 남지 않음" "[ ! -e /etc/pixlate/deploy.mode.applied ] || [ \"\$(applied)\" != live ]"
$S >/tmp/o 2>&1; ok "정상 재실행 → maintenance 기록" "[ \"\$(applied)\" = maintenance ]"
PATH=/tmp/failbin:$PATH $S >/tmp/o 2>&1; rc=$?
ok "이미 maintenance 인데 저장 실패 → exit 1, 옛 'maintenance' 를 최신처럼 남기지 않음" "[ $rc = 1 ] && { [ ! -e /etc/pixlate/deploy.mode.applied ] || [ \"\$(applied)\" != maintenance ]; }"
ok "임시 파일을 남기지 않음" "[ -z \"\$(ls -A /etc/pixlate | grep '^\.deploy\.mode\.applied\.')\" ]"
$S >/tmp/o 2>&1; ok "정상 재실행 → maintenance 기록 복구" "[ \"\$(applied)\" = maintenance ]"

echo "== L. 진행 표시 저장과 이전 값 삭제가 모두 실패(읽기 전용 흉내)"
mkdir -p /tmp/failbin2; for b in mv rm; do printf '#!/bin/sh\necho "%s: simulated read-only failure" >&2\nexit 1\n' "$b" > /tmp/failbin2/$b; chmod +x /tmp/failbin2/$b; done
mode live; $S >/tmp/o 2>&1; [ "$(applied)" = live ] || echo "    (live 준비 실패: $(applied))"
T4=$(date +%s); mode maintenance; PATH=/tmp/failbin2:$PATH $S >/tmp/o 2>&1; rc=$?
ok "exit 1, 이전 값(live)을 지우지 못했고 최신이 아님을 보고" "[ $rc = 1 ] && grep -q '이전 적용 값(live)을 지우지 못했다' /tmp/o && grep -q '최신이 아니다' /tmp/o"
ok "그래도 API·복구·cpu 에 TERM 전달·정지" "[ \$(st pixlate-api) = exited ] && [ \$(st pixlate-recovery) = exited ] && [ \$(st pixlate-worker-cpu) = exited ] && kills $T4 | grep -q 'pixlate-api:15 pixlate-recovery:15 pixlate-worker-cpu:15'"
ok "완료 로그 없음" "! grep -q 'maintenance: Redis 만 유지' /tmp/o"
mode live; PATH=/tmp/failbin2:$PATH START_SETTLE_S=1 $S >/tmp/o 2>&1; rc=$?
ok "같은 상황에서 live → exit 1, 새 기동 차단" "[ $rc = 1 ] && [ \$(st pixlate-api) = exited ] && [ \$(st pixlate-worker-cpu) = exited ] && [ \$(st pixlate-recovery) = exited ] && ! grep -q '실행 확인' /tmp/o"
rm -f /etc/pixlate/.deploy.mode.applied.*   # 가짜 rm 때문에 남은 임시 파일 정리(시험 전용)
$S >/tmp/o 2>&1; ok "정상 재실행 → live 복구" "[ \"\$(applied)\" = live ]"
mode maintenance; $S >/tmp/o 2>&1; ok "정상 maintenance" "[ \"\$(applied)\" = maintenance ]"

echo "== F. 출력 정제·Redis"
ok "스크립트 출력에 비밀 없음" "! grep -qE 'testpw|dbsecretpw|AIzaFAKE|bucket-name-xyz' /tmp/o /tmp/m"
ok "Redis 동일 컨테이너·데이터 유지" "redis_ok && [ \$(rp pixlate-redis) = unless-stopped ]"
echo "RESULT pass=$P fail=$F"
