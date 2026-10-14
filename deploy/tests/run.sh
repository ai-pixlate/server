#!/usr/bin/env bash
# 배포 스크립트 시험 실행기 — 실제 Docker 엔진을 docker:27-dind(privileged) 컨테이너 안에 띄워 시험한다.
# 운영 서버·운영 Redis·DB 에 연결하지 않는다. 값은 모두 가짜다.
#
#   bash deploy/tests/run.sh            # modes(모드·Redis 보존·digest·env 교체) + stop(정지·기동 실패 경로) + fe(FE 배포 스크립트)
#   bash deploy/tests/run.sh stop       # 하나만
#   FE_TESTS="gate stop" bash deploy/tests/run.sh fe   # fe 묶음 중 일부만(fe.sh 머리말)
#
# 실행마다 dind 컨테이너 이름에 실행 ID 를 붙이고(pixlate-deploy-test-<묶음>-<실행 ID>), 같은 PC 에서 시험 실행기는 하나만 돈다(lib.sh 잠금).
# 끝나거나 중단되면 이 실행이 만든 컨테이너만 지운다. 이름이 이미 있으면 지우지 않고 중단한다.
# 시작 때 deploy/ 를 이 실행의 임시 폴더로 복사해 그 사본으로 시험하고, 끝에 원본 해시를 다시 비교한다(바뀌었으면 무효, 종료 코드 3).
# 결과: 각 시나리오 PASS/FAIL 과 RESULT 줄. FAIL·환경 오류가 하나라도 있으면 종료 코드 1.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
suites=("${@:-modes}"); [ $# -gt 0 ] || suites=(modes stop fe)
test_lock
freeze_source "$(dirname "$here")"; deploy=$SRC_SNAP   # 시험은 고정 사본으로
if command -v cygpath >/dev/null 2>&1; then deploy=$(cygpath -w "$deploy"); export MSYS_NO_PATHCONV=1; fi  # Windows Git Bash
rc=0
for s in "${suites[@]}"; do
  name=pixlate-deploy-test-$s-$RUN_ID
  require_absent container "$name"
  own_container "$name"
  echo "=== $s ($name)"
  if ! docker run -d --privileged --name "$name" --entrypoint sh -v "$deploy:/draft:ro" docker:27-dind -c 'sleep infinity' >/dev/null \
     || ! docker exec "$name" sh -c 'apk add -q bash coreutils >/dev/null 2>&1; (dockerd >/var/log/dockerd.log 2>&1 &); for i in $(seq 60); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'; then
    echo "ENV dind 준비 실패($s)"; rc=1; docker rm -f "$name" >/dev/null 2>&1; continue
  fi
  run_child docker exec -e FE_TESTS="${FE_TESTS:-}" "$name" bash "/draft/tests/$s.sh"
  out=$(grep -vE 'Pulling|Download|Verifying|Waiting|Pull complete|Digest:|Status:|Unable to find|Already exists|Extracting' "$CHILD_OUT")
  echo "$out"
  echo "$out" | grep -qE '^RESULT pass=[0-9]+ fail=0( timeout=0 env=0)?$' || rc=1
  docker rm -f "$name" >/dev/null
done
source_check || rc=3
exit $rc
