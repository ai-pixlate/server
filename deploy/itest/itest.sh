#!/usr/bin/env bash
# EC2: sudo bash itest.sh <증거 폴더> precheck|run|check|abort <컨테이너 이름>
# driver.py 를 배포 digest API 이미지 컨테이너에서 실행한다. 출력은 로그 래퍼를 거친다. 비밀 값은 출력하지 않는다.
# 실행 식별·중복 방지: 실행마다 새 증거 폴더와 새 컨테이너 이름을 쓴다. 이전 실행의 폴더·컨테이너(pixlate-itest)는 지우지 않는다.
#   run 은 ① 같은 이름 컨테이너가 있거나 ② pixlate-itest* 컨테이너가 하나라도 실행 중이거나 ③ 이 증거 폴더에 이미 실행 기록
#   (state.json)이 있으면 시작하지 않는다.
set -uo pipefail
umask 077
EV=$1; MODE=$2; NAME=${3:?컨테이너 이름}
SRC_DIR=$(cd "$(dirname "$0")" && pwd)
DG=$(grep -E '^API_DIGEST=' /etc/pixlate/release.conf | cut -d= -f2- | tr -d ' \r')
IMG=$(docker images --no-trunc --digests --format '{{.ID}} {{.Digest}}' | awk -v d="$DG" '$1==d || $2==d {print $1; exit}')
[ -n "$IMG" ] || { echo "배포 이미지 없음"; exit 1; }
install -d -m 700 -o root -g root "$EV"
ARGS=(--network pixlate-net --env-file /etc/pixlate/pixlate.env --init
      -v /opt/pixlate/bin/redact_run.py:/opt/pixlate/bin/redact_run.py:ro
      -v "$SRC_DIR/driver.py:/itest/driver.py:ro" -v "$EV:/ev")
CMD=(python /opt/pixlate/bin/redact_run.py -- python /itest/driver.py "$MODE")
case "$MODE" in
  precheck) docker run --rm "${ARGS[@]}" "$IMG" "${CMD[@]}"; echo "precheck exit=$?" ;;
  check|abort) docker run --rm "${ARGS[@]}" "$IMG" "${CMD[@]}" ;;   # 읽기 전용 정리 확인 / 단발 중단(ABORT_SENT 있으면 안 보냄)
  run)
    docker container inspect "$NAME" >/dev/null 2>&1 && { echo "$NAME 이미 있음 — 실행 안 함"; exit 1; }
    busy=$(docker ps --filter name=pixlate-itest --format '{{.Names}}')
    [ -z "$busy" ] || { echo "실행 중인 검증 컨테이너 있음($busy) — 실행 안 함"; exit 1; }
    [ ! -e "$EV/state.json" ] || { echo "이 증거 폴더에 이미 실행 기록이 있다 — 새 폴더를 쓴다"; exit 1; }
    docker run -d --name "$NAME" "${ARGS[@]}" "$IMG" "${CMD[@]}" >/dev/null && echo "$NAME 시작" ;;
  *) echo "usage: itest.sh <ev> precheck|run|check|abort <name>"; exit 2 ;;
esac
