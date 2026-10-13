#!/bin/bash
# deploy/tests/run.sh 가 docker:27-dind 안에서 실행한다. /draft = 레포 deploy/(읽기 전용). 값은 모두 가짜.
set -uo pipefail
apk add -q bash coreutils >/dev/null 2>&1
mkdir -p /etc/pixlate /opt/pixlate/bin
cp /draft/start-pixlate.sh /opt/pixlate/; cp /draft/bin/redact_run.py /draft/bin/pixlate-mode /opt/pixlate/bin/
chmod +x /opt/pixlate/start-pixlate.sh /opt/pixlate/bin/pixlate-mode
S=/opt/pixlate/start-pixlate.sh
PASS=0; FAIL=0
ok() { if ( eval "$2" ); then echo "  PASS $1"; PASS=$((PASS+1)); else echo "  FAIL $1"; FAIL=$((FAIL+1)); fi; }
cid() { docker inspect -f '{{.Id}}' "$1" 2>/dev/null | cut -c1-12; }
rp()  { docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$1" 2>/dev/null; }
st()  { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo absent; }
envkeys() { docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" | cut -d= -f1 | tr '\n' ' '; }
rkey() { docker exec pixlate-redis redis-cli -a testpw --no-auth-warning GET marker; }
img() { docker inspect -f '{{.Config.Image}}' "$1"; }
mounts() { docker inspect -f '{{range .Mounts}}{{.Name}}:{{.Destination}} {{end}}' "$1"; }
bind() { docker inspect -f '{{json .HostConfig.PortBindings}}' "$1"; }
cmdof() { docker inspect -f '{{join .Config.Cmd " "}}' "$1"; }
SECRETS='testpw|dbsecretpw|rds\.amazonaws|bucket-name-xyz|AIzaFAKE'

# 레지스트리 + 가짜 이미지(api/ocr/api2)
docker run -d --name reg -p 5000:5000 registry:2 >/dev/null; sleep 2
for f in api ocr api2; do
  docker build -q --build-arg FLAVOR=$f -t localhost:5000/pixlate-api:$f /draft/tests/fake >/dev/null
  docker push -q localhost:5000/pixlate-api:$f >/dev/null
done
dg() { docker inspect -f '{{index .RepoDigests 0}}' localhost:5000/pixlate-api:$1 | cut -d@ -f2; }
API=$(dg api); OCR=$(dg ocr); API2=$(dg api2)

# 기존 운영 흉내: 익명 볼륨 Redis(비밀번호 인자) + 옛 api/워커(unless-stopped)
docker network create pixlate-net >/dev/null
docker run -d --name pixlate-redis --network pixlate-net -p 6379:6379 --restart unless-stopped redis:7-alpine redis-server --requirepass testpw >/dev/null
sleep 1; docker exec pixlate-redis redis-cli -a testpw --no-auth-warning SET marker keep-me >/dev/null
printf 'DATABASE_URL=postgresql+psycopg://u:dbsecretpw@db.example.ap-northeast-2.rds.amazonaws.com:5432/pixlate\nREDIS_PASSWORD=testpw\nCELERY_BROKER_URL=redis://:testpw@pixlate-redis:6379/0\nS3_BUCKET=bucket-name-xyz\n' > /etc/pixlate/pixlate.env
chmod 600 /etc/pixlate/pixlate.env
for n in pixlate-api pixlate-worker-cpu pixlate-worker-ocr; do
  docker run -d --name $n --network pixlate-net --restart unless-stopped --env-file /etc/pixlate/pixlate.env localhost:5000/pixlate-api:api celery old >/dev/null
done
R0=$(cid pixlate-redis); V0=$(mounts pixlate-redis)

echo "== 1. 모드 파일 없음 → maintenance"
$S >/tmp/o 2>&1; rc=$?
ok "exit 0" "[ $rc = 0 ]"
ok "옛 api·워커 정지+restart=no(삭제 아님)" '[ $(st pixlate-api) = exited ] && [ $(rp pixlate-api) = no ] && [ $(st pixlate-worker-cpu) = exited ] && [ $(rp pixlate-worker-ocr) = no ]'
ok "Redis 동일 컨테이너·볼륨·데이터" '[ $(cid pixlate-redis) = $R0 ] && [ "$(mounts pixlate-redis)" = "$V0" ] && [ "$(rkey)" = keep-me ]'
ok "applied=maintenance" 'grep -qx maintenance /etc/pixlate/deploy.mode.applied'

echo "== 2. 잘못된 모드 값(Live) → maintenance"
echo Live > /etc/pixlate/deploy.mode; $S >/tmp/o 2>&1; rc=$?
ok "exit 0 + mode=maintenance" "[ $rc = 0 ] && grep -q 'mode=maintenance' /tmp/o"

echo "== 3. validate 인데 release.conf 없음 → 실패·전부 정지"
echo validate > /etc/pixlate/deploy.mode; $S >/tmp/o 2>&1; rc=$?
ok "exit 1, 앱 정지" "[ $rc = 1 ] && [ \$(st pixlate-api) = exited ]"

printf 'RELEASE_SHA=e1291abceb8d280594e366be733c3cf950b1a0c4\nAPI_DIGEST=%s\nREGISTRY=localhost:5000\n' "$API" > /etc/pixlate/release.conf
echo "== 4. validate"
$S >/tmp/o 2>&1; rc=$?
ok "exit 0" "[ $rc = 0 ]"
ok "API 127.0.0.1 바인딩·restart=no·실행·digest" '[ "$(bind pixlate-api)" = "{\"8000/tcp\":[{\"HostIp\":\"127.0.0.1\",\"HostPort\":\"8000\"}]}" ] && [ $(rp pixlate-api) = no ] && [ $(st pixlate-api) = running ] && img pixlate-api | grep -q "@$API"'
ok "워커 정지 유지·복구 없음" '[ $(st pixlate-worker-cpu) = exited ] && [ $(st pixlate-worker-ocr) = exited ] && [ $(st pixlate-recovery) = absent ]'
ok "API 에 GEMINI 없음" '! envkeys pixlate-api | grep -q GEMINI'
sleep 2
ok "API 로그 정제(비밀 원문 0, 가림 표시 있음)" '! docker logs pixlate-api 2>&1 | grep -qE "$SECRETS" && docker logs pixlate-api 2>&1 | grep -q redacted'

echo "== 5. 데몬 재시작(재부팅 흉내) in validate"
A1=$(cid pixlate-api)
kill -TERM "$(cat /var/run/docker.pid)"; while [ -e /var/run/docker.pid ] && kill -0 "$(cat /var/run/docker.pid)" 2>/dev/null; do sleep 1; done
(dockerd >/var/log/dockerd2.log 2>&1 &)
for i in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done; sleep 3
ok "재시작 직후 API·워커 정지" '[ $(st pixlate-api) = exited ] && [ $(st pixlate-worker-cpu) = exited ]'
ok "Redis 자동 재기동·동일 컨테이너·데이터" '[ $(st pixlate-redis) = running ] && [ $(cid pixlate-redis) = $R0 ] && [ "$(rkey)" = keep-me ]'
docker start reg >/dev/null 2>&1 || true
$S >/tmp/o 2>&1
ok "서비스 재실행: API 재사용·워커 정지" '[ $(cid pixlate-api) = $A1 ] && [ $(st pixlate-api) = running ] && [ $(st pixlate-worker-ocr) = exited ]'

echo "== 6. live 차단 조건"
echo live > /etc/pixlate/deploy.mode; $S >/tmp/o 2>&1; rc=$?
ok "Gemini 파일 없음 → exit 1, 앱 정지, Redis 유지" "[ $rc = 1 ] && [ \$(st pixlate-api) = exited ] && [ \$(st pixlate-redis) = running ]"
echo "GEMINI_API_KEY=AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAK" > /etc/pixlate/worker-gemini.env; chmod 644 /etc/pixlate/worker-gemini.env
$S >/tmp/o 2>&1; rc=$?
ok "권한 644 → exit 1" "[ $rc = 1 ]"
chmod 600 /etc/pixlate/worker-gemini.env
echo "GEMINI_API_KEY=x" >> /etc/pixlate/pixlate.env; $S >/tmp/o 2>&1; rc=$?
ok "공유 env 에 GEMINI → exit 1" "[ $rc = 1 ] && grep -q '공유 환경 파일에 GEMINI' /tmp/o"
sed -i '/^GEMINI_API_KEY=/d' /etc/pixlate/pixlate.env

echo "== 7. live"
$S >/tmp/o 2>&1; rc=$?
ok "exit 0" "[ $rc = 0 ]"
ok "api·cpu·복구 running·unless-stopped" 'for c in pixlate-api pixlate-worker-cpu pixlate-recovery; do [ $(st $c) = running ] && [ $(rp $c) = unless-stopped ] || exit 1; done'
ok "EC2 ocr 워커는 정지·restart=no 유지(학교 GPU 로 이전)" '[ $(st pixlate-worker-ocr) = exited ] && [ $(rp pixlate-worker-ocr) = no ]'
ok "API 외부 바인딩" '[ "$(bind pixlate-api)" = "{\"8000/tcp\":[{\"HostIp\":\"\",\"HostPort\":\"8000\"}]}" ]'
ok "GEMINI 는 cpu 에만" 'envkeys pixlate-worker-cpu | grep -q GEMINI_API_KEY && ! envkeys pixlate-api | grep -q GEMINI && ! envkeys pixlate-recovery | grep -q GEMINI'
ok "복구 명령(SHA 와 동일)" '[ "$(cmdof pixlate-recovery)" = "python /opt/pixlate/bin/redact_run.py -- python -m app.recovery --interval 60" ]'
ok "워커 명령(큐·solo·노드명)" '[ "$(cmdof pixlate-worker-cpu)" = "python /opt/pixlate/bin/redact_run.py -- celery -A app.celery_app:celery_app worker -Q cpu --pool=solo -n cpu@%h --loglevel=info" ]'
sleep 2
ok "워커·복구 로그 정제" 'for c in pixlate-worker-cpu pixlate-recovery; do ! docker logs $c 2>&1 | grep -qE "$SECRETS" || exit 1; done'
ok "Redis 동일·데이터" '[ $(cid pixlate-redis) = $R0 ] && [ "$(rkey)" = keep-me ]'
C1=$(cid pixlate-worker-cpu); O1=$(cid pixlate-worker-ocr); A1=$(cid pixlate-api); V1=$(cid pixlate-recovery)

echo "== 8. 반복 실행"
$S >/tmp/o 2>&1
ok "ID 불변" '[ $(cid pixlate-worker-cpu) = $C1 ] && [ $(cid pixlate-worker-ocr) = $O1 ] && [ $(cid pixlate-api) = $A1 ] && [ $(cid pixlate-recovery) = $V1 ]'

echo "== 9. API digest 변경"
sed -i "s|^API_DIGEST=.*|API_DIGEST=$API2|" /etc/pixlate/release.conf; $S >/tmp/o 2>&1
ok "api·cpu·복구만 교체, ocr 유지" '[ $(cid pixlate-worker-cpu) != $C1 ] && [ $(cid pixlate-api) != $A1 ] && [ $(cid pixlate-recovery) != $V1 ] && [ $(cid pixlate-worker-ocr) = $O1 ] && img pixlate-api | grep -q "@$API2"'
C2=$(cid pixlate-worker-cpu); O2=$(cid pixlate-worker-ocr); A2=$(cid pixlate-api)

echo "== 10. Gemini 파일 내용 변경"
echo "GEMINI_API_KEY=AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFA2" > /etc/pixlate/worker-gemini.env; $S >/tmp/o 2>&1
ok "cpu 만 교체(옛 ocr 컨테이너 불변)" '[ $(cid pixlate-worker-cpu) != $C2 ] && [ $(cid pixlate-worker-ocr) = $O2 ] && [ $(cid pixlate-api) = $A2 ]'

echo "== 11. 종료 처리"
sleep 3; docker stop -t 30 pixlate-worker-cpu >/dev/null
ok "docker stop → TERM 전달·웜 종료·exit 0" '[ $(docker inspect -f "{{.State.ExitCode}}" pixlate-worker-cpu) = 0 ] && docker logs pixlate-worker-cpu 2>&1 | grep -q "warm shutdown"'
$S >/tmp/o 2>&1
ok "live 재실행 시 멈춘 워커 재시작" '[ $(st pixlate-worker-cpu) = running ]'

echo "== 12. Redis 멈춤"
docker stop pixlate-redis >/dev/null; $S >/tmp/o 2>&1
ok "같은 컨테이너 시작·데이터" '[ $(st pixlate-redis) = running ] && [ $(cid pixlate-redis) = $R0 ] && [ "$(rkey)" = keep-me ]'

echo "== 13. live → maintenance (pixlate-mode)"
CONF_DIR=/etc/pixlate SYSTEMCTL=true /opt/pixlate/bin/pixlate-mode maintenance >/dev/null; $S >/tmp/o 2>&1
ok "전부 정지·restart=no·Redis 유지" 'for c in pixlate-api pixlate-worker-cpu pixlate-recovery; do [ $(st $c) = exited ] && [ $(rp $c) = no ] || exit 1; done; [ $(st pixlate-redis) = running ]'
ok "모드 파일 권한 644·값" '[ "$(stat -c %a /etc/pixlate/deploy.mode)" = 644 ] && grep -qx maintenance /etc/pixlate/deploy.mode'

echo "== 14. Redis 부재"
docker rename pixlate-redis pixlate-redis-away
echo live > /etc/pixlate/deploy.mode; $S >/tmp/o 2>&1; rc=$?
ok "exit 1, 빈 Redis 생성 안 함, 앱 정지" "[ $rc = 1 ] && [ \$(st pixlate-redis) = absent ] && [ \$(st pixlate-api) = exited ] && [ \$(st pixlate-worker-cpu) = exited ]"
docker rename pixlate-redis-away pixlate-redis

echo "== 15. 스크립트 출력"
ok "start-pixlate 출력에 비밀 없음" '! grep -qE "$SECRETS" /tmp/o'
echo "RESULT pass=$PASS fail=$FAIL"
