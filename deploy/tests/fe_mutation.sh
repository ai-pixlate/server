#!/usr/bin/env bash
# FE 배포 변형 시험 — fe/ 사본에 결함을 하나씩 넣고, 그 결함을 잡아야 하는 시험 묶음만 돌려 기대한 시험이
# '의도한 사유로' FAIL 하는지 본다. 정상 코드는 전체 묶음을 먼저 한 번 돌린다.
#
#   bash deploy/tests/fe_mutation.sh
#
# dind 컨테이너 하나(pixlate-fe-mut-<실행 ID>)에서 공통 준비(레지스트리·가짜 이미지)를 한 번만 하고 재사용한다. 묶음마다 상태는 fe.sh 가 새로 만든다.
# 판정: 검출 = 기대 FAIL 이 모두 결함 사유로 실패 / 부분 검출 / 미검출(시간 초과만) = 사유가 시간 초과뿐 / 미검출 / 환경 오류.
# 시간 초과·환경 오류는 검출로 세지 않는다. 같은 PC 에서 시험 실행기는 하나만 돈다(lib.sh 잠금). 운영 서버에는 연결하지 않는다.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
LIMIT_S=${LIMIT_S:-600}   # 한 번의 fe.sh 실행 상한(넘으면 환경 오류로 보고 남은 변형을 멈춘다)

# ID | 결함 | 대상 파일 | sed 식 | 돌릴 묶음 | 기대 FAIL(모두 결함 사유여야 검출)
MUTS=(
  'M1|요청 모드만 확인(적용 결과·BE 성공 무시)|pixlate-fe.sh|s/if \[ "\$req" = live \] && \[ "\$app" = live \] && \[ "\$act" = active \] && \[ "\$res" = success \]; then return 0; fi/if [ "$req" = live ]; then return 0; fi/|gate|G2 G3 G4'
  'M2|Docker 재시작 정책 unless-stopped|compose.yaml|s/restart: "no"/restart: unless-stopped/|compose crash daemon|C1 X2 R1'
  'M3|docker stop 으로 정지(유예 뒤 SIGKILL)|pixlate-fe.sh|s/\$DOCKER kill --signal=TERM "\$C"/$DOCKER stop -t "$STOP_WAIT_S" "$C"/|stubborn|K1 K2'
  'M4|조회 실패를 없음으로 처리|pixlate-fe.sh|s/else echo unknown; fi/else echo absent; fi/|query|Q1 Q3'
  'M5|정지 요청 표시 무시|pixlate-fe.sh|s/if \[ -e "\$STOPPING" \]; then/if false; then/|stop|P2'
  'M6|FE 컨테이너의 API 연결 확인 생략|pixlate-fe.sh|s/if ! \$DOCKER exec "\$C" wget -q -T 5 -O \/dev\/null http:\/\/pixlate-api:8000\/ >\/dev\/null 2>&1; then/if false; then/|start|S3'
)

test_lock
freeze_source "$(dirname "$here")"; deploy=$SRC_SNAP   # 시험은 고정 사본으로(끝에 원본과 해시 비교)
if command -v cygpath >/dev/null 2>&1; then deploy=$(cygpath -w "$deploy"); export MSYS_NO_PATHCONV=1; fi  # Windows Git Bash
NAME=pixlate-fe-mut-$RUN_ID
require_absent container "$NAME"; own_container "$NAME"
echo "실행 $RUN_ID ($NAME)"
filter() { grep -vE 'Pulling|Download|Verifying|Waiting|Pull complete|Digest:|Status:|Unable to find|Already exists|Extracting'; }
if ! docker run -d --privileged --name "$NAME" --entrypoint sh -v "$deploy:/draft:ro" docker:27-dind -c 'sleep infinity' >/dev/null \
   || ! docker exec "$NAME" sh -c 'apk add -q bash coreutils >/dev/null 2>&1; (dockerd >/var/log/dockerd.log 2>&1 &); for i in $(seq 60); do docker info >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1'; then
  echo "환경 오류: dind 준비 실패"; exit 1
fi
# fe.sh 한 번 실행. OUT 에 출력, 상한 초과면 1
fe_run() {  # <FE_SRC> <FE_TESTS>
  run_child timeout "$LIMIT_S" docker exec -e FE_SRC="$1" -e FE_TESTS="$2" "$NAME" bash /draft/tests/fe.sh; local rc=$?
  OUT=$(filter < "$CHILD_OUT")
  [ "$rc" != 124 ]
}

s0=$(date +%s)
echo "== 정상 코드(전체 묶음)"
fe_run /draft/fe "" || { echo "$OUT"; echo "환경 오류: 정상 코드 실행이 ${LIMIT_S}초를 넘었다"; exit 1; }
echo "$OUT" | grep -E '^  (FAIL|ENV)|^RESULT'
base=$(echo "$OUT" | grep -E '^RESULT')
echo "  소요 $(( $(date +%s)-s0 ))초"
[ "$base" = "RESULT pass=$(echo "$OUT" | grep -c '^  PASS') fail=0 timeout=0 env=0" ] || { echo "정상 코드가 통과하지 않아 변형 시험을 하지 않는다"; exit 1; }

summary=(); undetected=0
for m in "${MUTS[@]}"; do
  IFS='|' read -r id desc file expr groups expect <<<"$m"
  s=$(date +%s)
  if ! docker exec -e EXPR="$expr" "$NAME" sh -c "rm -rf /work/$id && mkdir -p /work/$id && cp -r /draft/fe /work/$id/ && sed -i \"\$EXPR\" /work/$id/fe/$file && ! cmp -s /draft/fe/$file /work/$id/fe/$file"; then
    summary+=("$id 환경 오류 — 결함 주입 실패($file 이 바뀌지 않음)"); undetected=$((undetected+1)); continue
  fi
  if ! fe_run "/work/$id/fe" "$groups"; then
    summary+=("$id 환경 오류 — ${LIMIT_S}초 초과(남은 변형 중단)"); undetected=$((undetected+1)); break
  fi
  res=$(echo "$OUT" | grep -E '^RESULT')
  if [ -z "$res" ] || ! echo "$res" | grep -q 'env=0$'; then summary+=("$id 환경 오류 — ${res:-RESULT 없음}"); undetected=$((undetected+1)); continue; fi
  det=(); tmo=(); miss=()
  for e in $expect; do
    line=$(echo "$OUT" | grep -E "^  FAIL $e " | head -1)
    if [ -z "$line" ]; then miss+=("$e")
    elif [[ "$line" =~ \[시간\ 초과\]$ ]]; then tmo+=("$e")
    else det+=("$e"); fi
  done
  other=$(echo "$OUT" | grep -E '^  FAIL ' | awk '{print $2}' | grep -vxF -f <(tr ' ' '\n' <<<"$expect") | tr '\n' ' ')
  if [ ${#det[@]} -eq $(wc -w <<<"$expect") ]; then v="검출"
  elif [ ${#det[@]} -gt 0 ]; then v="부분 검출"; undetected=$((undetected+1))
  elif [ ${#tmo[@]} -gt 0 ]; then v="미검출(시간 초과만)"; undetected=$((undetected+1))
  else v="미검출"; undetected=$((undetected+1)); fi
  summary+=("$id $v — $desc | 묶음: $groups | 결함 사유 FAIL: ${det[*]:-없음}${tmo:+ | 시간 초과만: ${tmo[*]}}${miss:+ | FAIL 안 남: ${miss[*]}}${other:+ | 그 밖의 FAIL: $other}| $(( $(date +%s)-s ))초")
  echo "== $id ($desc)"; echo "$OUT" | grep -E '^  FAIL|^RESULT'
done

echo "== 요약(전체 $(( $(date +%s)-s0 ))초)"
printf '  %s\n' "${summary[@]}"
echo "RESULT mutants=${#MUTS[@]} undetected=$undetected"
source_check || exit 3
[ "$undetected" = 0 ]
