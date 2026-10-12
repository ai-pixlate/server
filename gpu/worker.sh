#!/usr/bin/env bash
# 학교 GPU pod 의 큐 워커 관리: bash /data/pixlate/worker.sh <gpu|ocr> <start|stop|status>
#
# - EC2 Redis 의 gpu 큐(인페인팅)·ocr 큐(분석)를 소비한다. 둘 다 --pool=solo --concurrency=1(prefork 금지).
# - 게이트 파일(/data/pixlate/<큐>.enabled)이 없으면 시작하지 않는다. pod 재시작·재접속만으로 소비가 시작되지 않게 하고,
#   검증이 끝난 뒤 담당자가 게이트를 만들어 시작한다.
# - /data/server 코드가 /data/pixlate/release.sha 와 같을 때만 시작한다.
# - 표준 출력·오류는 로그 정제 래퍼(redact_run.py)를 거쳐서만 로그 파일에 쓴다. 연결 주소·비밀 원문은 파일에 남지 않는다.
# - stop 은 TERM 으로 웜 종료(처리 중인 작업은 끝까지)하고 기다린다. 강제 종료(-9)는 하지 않는다.
# 비밀 값은 이 파일에 없다. set -x 를 쓰지 않는다.
set -euo pipefail
umask 077

Q=${1:-}; CMD=${2:-}
BASE=/data/pixlate
SRC=/data/server
ENV=/data/server.env                  # 브로커·결과 저장소·DB·S3 자격(승인된 방식) — root 600
GEMINI_ENV=$BASE/worker-gemini.env     # GEMINI_API_KEY 한 줄 — ocr 워커에만 연결 — root 600
WRAP=$BASE/bin/redact_run.py
RELEASE=$BASE/release.sha

case "$Q" in
  gpu) PY=/data/venv/bin/python;     LOG=/data/gpu-worker.log ;;   # torch 는 /opt/venv 를 .pth 로 공유
  ocr) PY=/data/venv-ocr/bin/python; LOG=/data/ocr-worker.log ;;   # PaddleOCR(CPU) 전용 venv. gpu venv 와 섞지 않는다
  *) echo "usage: $0 <gpu|ocr> <start|stop|status>" >&2; exit 2 ;;
esac
GATE=$BASE/$Q.enabled
PIDF=$BASE/$Q.pid

die() { echo "[worker:$Q] 중단: $*" >&2; exit 1; }
alive() { [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; }
has_key() { grep -qE "^(export )?$1=." "$2"; }

case "$CMD" in
  start)
    [ -f "$GATE" ] || die "게이트 파일 없음($GATE) — 소비 재개 결정 전에는 시작하지 않는다"
    alive && die "이미 실행 중"
    if pgrep -f "celery -A app.celery_app[: ].*worker -Q $Q( |$)" >/dev/null; then
      die "관리 밖의 $Q 워커가 떠 있다(기존 nohup 워커 등). 먼저 웜 종료한다"
    fi
    [ -r "$WRAP" ] || die "로그 래퍼 없음"
    [ -x "$PY" ] || die "venv 없음($PY)"
    want=$(tr -d ' \r\n' < "$RELEASE" 2>/dev/null || true); have=$(git -C "$SRC" rev-parse HEAD)
    [ -n "$want" ] && [ "$have" = "$want" ] || die "코드($SRC)가 배포 SHA 와 다르다"
    [ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] || die "코드에 커밋되지 않은 변경이 있다"
    for k in CELERY_BROKER_URL CELERY_RESULT_BACKEND DATABASE_URL; do has_key "$k" "$ENV" || die "$k 없음"; done
    if [ "$Q" = ocr ]; then
      for l in libgomp.so.1 libGL.so.1 libglib-2.0.so.0; do
        ldconfig -p | grep -q "$l" || die "OCR 런타임 라이브러리 $l 없음(이미지 재빌드 전 pod 재시작 시 사라진다)"
      done
      [ "$(stat -c '%U %a' "$GEMINI_ENV" 2>/dev/null)" = "root 600" ] || die "워커 Gemini 환경 파일 소유자·권한(root 600)"
      [ "$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$GEMINI_ENV")" = 1 ] && has_key GEMINI_API_KEY "$GEMINI_ENV" \
        || die "워커 Gemini 환경 파일은 비어 있지 않은 GEMINI_API_KEY 한 줄이어야 한다"
    fi
    (
      cd "$SRC"
      set -a; . "$ENV"; [ "$Q" = ocr ] && . "$GEMINI_ENV"; set +a
      setsid nohup "$PY" "$WRAP" -- "$PY" -m celery -A app.celery_app:celery_app worker -Q "$Q" \
        --pool=solo --concurrency=1 -n "$Q@%h" --loglevel=info >> "$LOG" 2>&1 < /dev/null &
      echo $! > "$PIDF"
    )
    echo "[worker:$Q] 시작(pid $(cat "$PIDF"), 로그 $LOG)"
    ;;
  stop)
    alive || { echo "[worker:$Q] 실행 중 아님"; rm -f "$PIDF"; exit 0; }
    kill -TERM "$(cat "$PIDF")"         # 래퍼 → celery 웜 종료
    for _ in $(seq 1 3600); do alive || break; sleep 1; done
    alive && die "3600초 안에 끝나지 않았다(강제 종료 여부는 담당자 판단)"
    rm -f "$PIDF"; echo "[worker:$Q] 정지"
    ;;
  status)
    if alive; then echo "running pid=$(cat "$PIDF")"; else echo stopped; fi
    echo "gate=$([ -f "$GATE" ] && echo on || echo off) release=$(cat "$RELEASE" 2>/dev/null | cut -c1-12) code=$(git -C "$SRC" rev-parse --short=12 HEAD)"
    ;;
  *) echo "usage: $0 <gpu|ocr> <start|stop|status>" >&2; exit 2 ;;
esac
