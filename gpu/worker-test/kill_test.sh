#!/usr/bin/env bash
# 작업 처리 중에 워커가 강제 종료(kill -9)되면 작업이 어떻게 되는지 확인한다.
# 학교 서버 점검·컨테이너 재시작 상황을 흉내 낸다. 학교 서버 컨테이너 안에서 실행한다.
#
# 사용법: bash kill_test.sh [default|acks_late]
#   default   : app/celery_app.py 와 같은 기본 설정
#   acks_late : 작업을 끝낸 뒤 완료 처리 + 완료되지 않은 작업은 20초 뒤 다시 큐로
set -u
cd "$(dirname "$0")"

MODE=${1:-default}
PY=/data/venv/bin/python
CELERY=/data/venv/bin/celery
# 평소 테스트용 워커(db 0)와 섞이지 않도록 별도 Redis DB 사용
export CELERY_BROKER_URL=redis://localhost:6379/3
export CELERY_RESULT_BACKEND=redis://localhost:6379/4
if [ "$MODE" = acks_late ]; then
    export ACKS_LATE=1 VISIBILITY_TIMEOUT=20
fi
redis-cli -n 3 flushdb > /dev/null
redis-cli -n 4 flushdb > /dev/null

start_worker() {
    local log="kill_test_${MODE}_$1.log"
    : > "$log"
    setsid nohup "$CELERY" -A worker_test worker -Q gpu -n "killtest@%h" --concurrency=1 --loglevel=info \
        > "$log" 2>&1 < /dev/null &
    for _ in $(seq 1 30); do grep -q "ready\." "$log" && return; sleep 1; done
}
stop_worker() { pkill -9 -f "killtest@"; sleep 1; }
state() { "$PY" -c "from worker_test import app; print(app.AsyncResult('$1').state)"; }

echo "== 모드: $MODE"
start_worker 1
ID=$("$PY" -c "from worker_test import fake_inpaint; print(fake_inpaint.delay(1, 15).id)")
echo "15초짜리 작업 전송"
sleep 5
echo "5초 뒤 작업 상태: $(state "$ID")"

stop_worker
echo "워커 강제 종료 → 대기열 gpu: $(redis-cli -n 3 llen gpu)건, 완료 처리 안 된 작업: $(redis-cli -n 3 hlen unacked)건"

start_worker 2
echo "워커 재시작"
start=$(date +%s)
for _ in $(seq 1 90); do
    [ "$(state "$ID")" = SUCCESS ] && break
    sleep 2
done
echo "재시작 후 $(( $(date +%s) - start ))초 경과 → 작업 상태: $(state "$ID")"
echo "재시작한 워커가 받은 작업: $(grep -c "received" "kill_test_${MODE}_2.log")건"
stop_worker
