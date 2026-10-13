#!/usr/bin/env bash
# 실제 앱 이미지로 cpu 워커 실행 방식별 '작업 실행 시점의 pipeline 지연 import'를 시험한다.
# 운영 DB·Redis·모델 호출 없음: 외부로 나가지 않는 내부 네트워크와 임시 Redis 만 쓰고, 끝나면 모두 지운다.
#
#   bash deploy/tests/worker_import.sh <앱 이미지>      예: pixlate-api:e1291ab (배포 digest 와 같은 이미지)
#
# 기대: 기존 방식(celery 실행 파일)  → 실패 재현(ModuleNotFoundError: pipeline)
#       수정 방식(python -m celery)   → 성공
# 그리고 deploy/start-pixlate.sh 의 cpu 워커 명령이 수정 방식인지 확인한다.
# 두 방식 모두 같은 이미지·작업 디렉터리(/app)·로그 래퍼(deploy/bin/redact_run.py)를 쓰고, 탐침 태스크만 --include 로 더한다.
set -uo pipefail
IMG=${1:?usage: worker_import.sh <앱 이미지>}
here=$(cd "$(dirname "$0")" && pwd)
deploy=$(dirname "$here")
wrap=$deploy/bin/redact_run.py; probe=$here/probe/itest_probe.py
if command -v cygpath >/dev/null 2>&1; then wrap=$(cygpath -w "$wrap"); probe=$(cygpath -w "$probe"); export MSYS_NO_PATHCONV=1; fi
NET=pixlate-import-test; R=$NET-redis
pass=0; fail=0
ok() { if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }
cleanup() { docker rm -f "$R" "$NET-old" "$NET-new" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
docker network create --internal "$NET" >/dev/null || exit 1
docker run -d --name "$R" --network "$NET" redis:7-alpine >/dev/null || exit 1
ENV=(-e "CELERY_BROKER_URL=redis://$R:6379/0" -e "CELERY_RESULT_BACKEND=redis://$R:6379/1")
MNT=(-v "$wrap:/opt/pixlate/bin/redact_run.py:ro" -v "$probe:/app/itest_probe.py:ro")
WORKER=(-A app.celery_app:celery_app worker -Q cpu --pool=solo -n "cpu@%h" --loglevel=info --include itest_probe)

probe_once() {  # $1 컨테이너 이름, 나머지 = 실행 명령. 워커가 준비되면 탐침 1건을 보내고 결과 한 줄을 출력한다
  local name=$1; shift
  docker run -d --name "$name" --network "$NET" --init "${ENV[@]}" "${MNT[@]}" "$IMG" "$@" >/dev/null || { echo "start-failed"; return; }
  for _ in $(seq 60); do docker logs "$name" 2>&1 | grep -q "ready\." && break; sleep 1; done
  docker run --rm --network "$NET" "${ENV[@]}" -w /app "$IMG" python -c "
from app.celery_app import celery_app
r = celery_app.send_task('itest.lazy_pipeline', queue='cpu')
print('RESULT', r.get(timeout=60, propagate=False))" 2>&1 | grep '^RESULT' || echo "RESULT none"
  docker logs "$name" 2>&1 | grep -E "itest.lazy_pipeline\[.*\] (succeeded|raised)" | sed -E 's/\[[0-9a-f-]{36}\]/[id]/' | cut -c1-200 | sed 's/^/  log: /'
  docker rm -f "$name" >/dev/null
}

echo "=== 이미지: $(docker image inspect -f '{{.Id}}' "$IMG" 2>/dev/null | cut -c1-19)"
echo "=== 기존 방식: python redact_run.py -- celery ${WORKER[*]}"
OLD=$(probe_once "$NET-old" python /opt/pixlate/bin/redact_run.py -- celery "${WORKER[@]}"); echo "$OLD"
echo "=== 수정 방식: python redact_run.py -- python -m celery ${WORKER[*]}"
NEW=$(probe_once "$NET-new" python /opt/pixlate/bin/redact_run.py -- python -m celery "${WORKER[@]}"); echo "$NEW"

# 수정 방식도 래퍼를 거쳐 TERM 을 받으면 웜 종료하고 0 으로 끝나는지 — 작업 없는 유휴 워커의 TERM 전달·웜 종료 확인이다.
# 실행 중 작업을 끝까지 마친 뒤 종료하는지는 가짜 워커의 지연 종료 시험(stop.sh)이 따로 본다.
docker run -d --name "$NET-new" --network "$NET" --init "${ENV[@]}" "${MNT[@]}" "$IMG" \
  python /opt/pixlate/bin/redact_run.py -- python -m celery "${WORKER[@]}" >/dev/null
for _ in $(seq 60); do docker logs "$NET-new" 2>&1 | grep -q "ready\." && break; sleep 1; done
docker kill --signal=TERM "$NET-new" >/dev/null
for _ in $(seq 60); do [ "$(docker inspect -f '{{.State.Status}}' "$NET-new")" = exited ] && break; sleep 1; done
TERM_EXIT=$(docker inspect -f '{{.State.Status}}:{{.State.ExitCode}}' "$NET-new"); TERM_LOG=$(docker logs "$NET-new" 2>&1 | grep -ci "warm shutdown")
echo "=== 수정 방식 유휴 워커 TERM: $TERM_EXIT, warm shutdown 로그 ${TERM_LOG}줄"
docker rm -f "$NET-new" >/dev/null

ok "수정 방식: 유휴 워커 TERM → 웜 종료·종료 코드 0" '[ "$TERM_EXIT" = exited:0 ] && [ "$TERM_LOG" -ge 1 ]'
ok "기존 방식: 작업 실행 시점 pipeline import 실패 재현" 'printf "%s" "$OLD" | grep -q "^RESULT .*'"'"'ok'"'"': False.*ModuleNotFoundError.*pipeline"'
ok "수정 방식: 작업 실행 시점 pipeline import 성공" 'printf "%s" "$NEW" | grep -q "^RESULT .*'"'"'ok'"'"': True.*/app/pipeline/__init__.py"'
ok "start-pixlate.sh cpu 워커가 python -m celery 로 시작" 'grep -qE -- "redact_run.py -- python -m celery -A app.celery_app:celery_app worker -Q cpu" "$deploy/start-pixlate.sh"'
ok "start-pixlate.sh 에 celery 실행 파일로 워커를 띄우는 명령 없음" '! grep -qE -- "redact_run.py -- celery -A" "$deploy/start-pixlate.sh"'
echo "RESULT pass=$pass fail=$fail"
[ $fail = 0 ]
