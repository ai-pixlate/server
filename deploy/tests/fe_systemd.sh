#!/usr/bin/env bash
# FE 배포 systemd 연동 시험 — 격리 컨테이너(Ubuntu 24.04 + systemd + Docker) 안에서 레포의 pixlate.service·pixlate-fe.service 를
# 실제 systemd 로 돌린다(BE 는 가짜 start-pixlate.sh). 운영 서버·호스트의 다른 컨테이너는 건드리지 않는다.
#
#   bash deploy/tests/fe_systemd.sh
#
# 자원 이름에 실행 ID 를 붙이고(pixlate-fe-systemd-<실행 ID>), 같은 PC 에서 시험 실행기는 하나만 돈다(lib.sh 잠금).
# 끝나거나 중단되면 이 실행이 만든 컨테이너·볼륨·이미지 태그만 지운다. 결과: PASS/FAIL 과 RESULT 줄. FAIL 이 있으면 종료 코드 1.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
test_lock
freeze_source "$(dirname "$here")"; deploy=$SRC_SNAP; sysdir=$SRC_SNAP/tests/systemd   # 시험은 고정 사본으로(끝에 원본과 해시 비교)
if command -v cygpath >/dev/null 2>&1; then deploy=$(cygpath -w "$deploy"); sysdir=$(cygpath -w "$sysdir"); export MSYS_NO_PATHCONV=1; fi  # Windows Git Bash
NAME=pixlate-fe-systemd-$RUN_ID; IMG=pixlate-fe-systemd:$RUN_ID
VOL=$NAME-docker; VOL2=$NAME-containerd   # 안쪽 Docker·containerd 저장소는 볼륨에(overlay 위 overlay 불가)
require_absent container "$NAME"; require_absent volume "$VOL"; require_absent volume "$VOL2"; require_absent image "$IMG"
own_container "$NAME"; own_volume "$VOL"; own_volume "$VOL2"; own_image "$IMG"
echo "실행 $RUN_ID ($NAME)"
run_child docker build -q -t "$IMG" "$sysdir" || { echo "환경 오류: 시험 이미지 빌드 실패"; exit 1; }
docker run -d --privileged --name "$NAME" -v "$VOL:/var/lib/docker" -v "$VOL2:/var/lib/containerd" -v "$deploy:/draft:ro" \
  --tmpfs /run --tmpfs /run/lock "$IMG" >/dev/null || { echo "환경 오류: 시험 컨테이너 기동 실패"; exit 1; }
for _ in $(seq 90); do docker exec "$NAME" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break; sleep 1; done

filter() { grep -vE 'Pulling|Download|Verifying|Waiting|Pull complete|Digest:|Status:|Unable to find|Already exists|Extracting'; }
run_child timeout 900 docker exec "$NAME" bash /draft/tests/systemd/inner.sh main; out1=$(filter < "$CHILD_OUT"); echo "$out1"
run_child docker restart -t 120 "$NAME"   # 재부팅: systemd 가 유닛을 순서대로 멈춘 뒤 다시 부팅한다
run_child timeout 600 docker exec "$NAME" bash /draft/tests/systemd/inner.sh reboot; out2=$(filter < "$CHILD_OUT"); echo "$out2"
p=$(printf '%s\n%s\n' "$out1" "$out2" | grep -c '^  PASS'); f=$(printf '%s\n%s\n' "$out1" "$out2" | grep -c '^  FAIL')
r=$(printf '%s\n%s\n' "$out1" "$out2" | grep -c '^RESULT')
[ "$r" = 2 ] || echo "환경 오류: 결과 줄 $r/2(시간 초과 또는 준비 실패)"
echo "RESULT pass=$p fail=$f"
source_check || exit 3
[ "$f" = 0 ] && [ "$r" = 2 ]
