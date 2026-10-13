#!/usr/bin/env bash
# 배포 스크립트 시험 실행기 — 실제 Docker 엔진을 docker:27-dind(privileged) 컨테이너 안에 띄워 시험한다.
# 운영 서버·운영 Redis·DB 에 연결하지 않는다. 값은 모두 가짜다. 끝나면 시험 컨테이너를 지운다.
#
#   bash deploy/tests/run.sh            # modes(모드·Redis 보존·digest·env 교체) + stop(정지·기동 실패 경로)
#   bash deploy/tests/run.sh stop       # 하나만
#
# 결과: 각 시나리오 PASS/FAIL 과 RESULT 줄. FAIL 이 하나라도 있으면 종료 코드 1.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
deploy=$(dirname "$here")
if command -v cygpath >/dev/null 2>&1; then deploy=$(cygpath -w "$deploy"); export MSYS_NO_PATHCONV=1; fi  # Windows Git Bash
suites=("${@:-modes}"); [ $# -gt 0 ] || suites=(modes stop)
rc=0
for s in "${suites[@]}"; do
  name=pixlate-deploy-test-$s
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --privileged --name "$name" --entrypoint sh -v "$deploy:/draft:ro" docker:27-dind -c 'sleep infinity' >/dev/null || exit 1
  docker exec "$name" sh -c 'apk add -q bash coreutils >/dev/null 2>&1; (dockerd >/var/log/dockerd.log 2>&1 &); for i in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 1; done'
  echo "=== $s"
  out=$(docker exec "$name" bash "/draft/tests/$s.sh" 2>&1 | grep -vE 'Pulling|Download|Verifying|Waiting|Pull complete|Digest:|Status:|Unable to find|Already exists|Extracting')
  echo "$out"
  echo "$out" | grep -qE '^RESULT pass=[0-9]+ fail=0$' || rc=1
  docker rm -f "$name" >/dev/null
done
exit $rc
