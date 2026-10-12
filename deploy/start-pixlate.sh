#!/usr/bin/env bash
# EC2 설치 위치: /opt/pixlate/start-pixlate.sh — pixlate.service ExecStart. 비밀 값 없음. set -x 금지.
# EC2 에는 API·cpu 워커·복구 프로세스와 Redis 만 둔다. ocr·gpu 큐는 학교 GPU pod 워커가 소비한다(gpu/README.md).
#
# 동작은 /etc/pixlate/deploy.mode 한 단어로 정한다. 파일이 없거나 값이 다르면 maintenance 로 동작한다(live 로 시작하지 않는다).
#   maintenance : API·cpu 워커·복구 프로세스를 멈추고 자동 재시작을 끈다(재부팅해도 유입·소비·복구 없음). Redis 는 그대로 둔다
#   validate    : 고정 digest API 를 127.0.0.1 에만 연다(외부·ALB 유입 없음, 자동 재시작 끔). 워커·복구는 멈춘 채
#   live        : API 외부 공개 → cpu 워커 → 복구 프로세스 순서로 기동(자동 재시작 켬)
# 모드는 /opt/pixlate/bin/pixlate-mode 로만 바꾼다(파일 기록과 적용을 한 번에 해서 restart 정책과 모드가 어긋나지 않게 한다).
#
# Redis: 기존 pixlate-redis 컨테이너·볼륨을 지우거나 다시 만들지 않는다. 멈춰 있으면 같은 컨테이너를 시작한다.
#        컨테이너가 없으면 빈 Redis 를 만들지 않고 나머지를 멈춘 뒤 중단한다(복구는 담당자 판단).
# 앱 이미지는 /etc/pixlate/release.conf 의 digest 로만 받는다. latest 태그는 쓰지 않는다.
# 컨테이너 사양(digest·명령·포트·마운트·env 파일 내용·래퍼 내용·restart)이 바뀌면 해당 컨테이너만 다시 만든다.
# 표준 출력·오류는 deploy/bin/redact_run.py 를 거쳐서만 Docker 로그에 도달한다.
set -euo pipefail
umask 077

REGION=ap-northeast-2
AWS=${AWS:-/snap/bin/aws}
DOCKER=${DOCKER:-docker}
CONF_DIR=${CONF_DIR:-/etc/pixlate}
ENVFILE=$CONF_DIR/pixlate.env
GEMINI_ENV=$CONF_DIR/worker-gemini.env      # GEMINI_API_KEY 한 줄만. cpu 워커에만 연결(API·복구에는 연결 안 함)
RELEASE=$CONF_DIR/release.conf              # RELEASE_SHA / API_DIGEST (+선택 REGISTRY). 비밀 아님
MODE_FILE=$CONF_DIR/deploy.mode
APPLIED=$CONF_DIR/deploy.mode.applied       # 마지막으로 적용을 끝낸 모드(상태 확인용)
WRAP=${WRAP:-/opt/pixlate/bin/redact_run.py}  # 로그 정제 래퍼(시그널 전달·종료 코드 보존)
NET=pixlate-net
APP_CONTAINERS=(pixlate-recovery pixlate-worker-cpu pixlate-api)
OFFLOADED=(pixlate-worker-gpu pixlate-worker-ocr)   # EC2 에서 돌리지 않는 큐의 옛 컨테이너(지우지 않고 멈춤)

log() { echo "[start-pixlate] $*"; }
die() { echo "[start-pixlate] 중단: $*" >&2; exit 1; }

mode=$( { tr -d ' \r\n' < "$MODE_FILE"; } 2>/dev/null || true)
case "$mode" in maintenance|validate|live) ;; *) log "모드 파일 없음/값 이상 → maintenance"; mode=maintenance ;; esac
log "mode=$mode"

conf() { grep -E "^$1=" "$RELEASE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r' || true; }
exists()  { $DOCKER container inspect "$1" >/dev/null 2>&1; }
running() { [ "$($DOCKER container inspect -f '{{.State.Running}}' "$1" 2>/dev/null || echo false)" = "true" ]; }

hold() {  # 멈추고 자동 재시작을 끈다. 지우지 않는다(웜 종료 대기 600초)
  for c in "$@"; do
    if exists "$c"; then
      $DOCKER update --restart=no "$c" >/dev/null
      if running "$c"; then $DOCKER stop -t 600 "$c" >/dev/null; log "$c 정지"; fi
    fi
  done
}

ensure_network() { $DOCKER network inspect "$NET" >/dev/null 2>&1 || $DOCKER network create "$NET" >/dev/null; }

ensure_redis() {
  if ! exists pixlate-redis; then
    hold "${APP_CONTAINERS[@]}"
    die "pixlate-redis 컨테이너가 없다. 빈 Redis 를 자동으로 만들지 않는다(앱 컨테이너는 멈춤)"
  fi
  if ! running pixlate-redis; then
    $DOCKER start pixlate-redis >/dev/null
    log "기존 Redis 컨테이너 시작(재생성 안 함)"
  fi
}

filehash() { sha256sum "$1" 2>/dev/null | cut -c1-64 || echo missing; }

# 같은 사양이면 재사용, 다르면 교체(pixlate-redis 는 절대 대상이 아님)
run_managed() {
  local name=$1 restart=$2; shift 2
  [ "$name" != pixlate-redis ] || die "Redis 는 run_managed 대상이 아니다"
  local parts=("$restart" "wrap=$(filehash "$WRAP")") prev=""
  for a in "$@"; do
    parts+=("$a")
    [ "$prev" = "--env-file" ] && parts+=("content=$(filehash "$a")")  # 값 대신 내용 해시만 사양에 넣는다
    prev=$a
  done
  local spec; spec=$(printf '%s\0' "${parts[@]}" | sha256sum | cut -c1-16)
  local cur; cur=$($DOCKER container inspect -f '{{index .Config.Labels "pixlate.spec"}}' "$name" 2>/dev/null || true)
  if [ "$cur" = "$spec" ]; then
    $DOCKER update --restart="$restart" "$name" >/dev/null
    running "$name" || $DOCKER start "$name" >/dev/null
    log "$name 재사용(restart=$restart)"
  else
    if exists "$name"; then
      running "$name" && $DOCKER stop -t 600 "$name" >/dev/null
      $DOCKER rm "$name" >/dev/null
    fi
    $DOCKER run -d --name "$name" --label "pixlate.spec=$spec" --label "pixlate.release=$RELEASE_SHA" \
      --network "$NET" --restart "$restart" --init -v "$WRAP:/opt/pixlate/bin/redact_run.py:ro" "$@" >/dev/null
    log "$name 생성(restart=$restart)"
  fi
}

ensure_network
ensure_redis
hold "${OFFLOADED[@]}"

if [ "$mode" = maintenance ]; then
  hold "${APP_CONTAINERS[@]}"
  echo maintenance > "$APPLIED"
  log "maintenance: Redis 만 유지, 유입·소비·복구 없음"
  exit 0
fi

RELEASE_SHA=$(conf RELEASE_SHA); API_DIGEST=$(conf API_DIGEST); REGISTRY=$(conf REGISTRY)
fail_closed() { hold "${APP_CONTAINERS[@]}"; echo maintenance > "$APPLIED"; die "$*"; }
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || fail_closed "release.conf RELEASE_SHA 형식(40자리)"
[[ "$API_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail_closed "release.conf API_DIGEST 형식"
[ -r "$WRAP" ] || fail_closed "로그 래퍼 없음"
[ -r "$ENVFILE" ] || fail_closed "공유 환경 파일 없음"
if grep -q '^GEMINI_API_KEY=' "$ENVFILE"; then fail_closed "공유 환경 파일에 GEMINI_API_KEY 가 있다(API·복구에 키가 들어간다)"; fi

if [ -z "$REGISTRY" ]; then
  ACCOUNT=$($AWS sts get-caller-identity --query Account --output text)
  REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
  $AWS ecr get-login-password --region "$REGION" | $DOCKER login --username AWS --password-stdin "$REGISTRY" >/dev/null 2>&1 \
    || fail_closed "ECR 로그인 실패"
fi
IMAGE=$REGISTRY/pixlate-api@$API_DIGEST
$DOCKER pull -q "$IMAGE" >/dev/null || fail_closed "API 이미지 pull 실패"

API_CMD=(python /opt/pixlate/bin/redact_run.py -- uvicorn app.main:app --host 0.0.0.0 --port 8000)

if [ "$mode" = validate ]; then
  hold pixlate-recovery pixlate-worker-cpu
  run_managed pixlate-api no -p 127.0.0.1:8000:8000 --env-file "$ENVFILE" "$IMAGE" "${API_CMD[@]}"
  echo validate > "$APPLIED"
  log "validate: API 는 127.0.0.1 에만 열림(외부 유입 없음), 워커·복구 정지. release=$RELEASE_SHA"
  exit 0
fi

# live
[ -s "$GEMINI_ENV" ] || fail_closed "워커 Gemini 환경 파일 없음"
[ "$(stat -c '%U:%G %a' "$GEMINI_ENV")" = "root:root 600" ] || fail_closed "워커 Gemini 환경 파일 소유자·권한은 root:root 600 이어야 한다"
[ "$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$GEMINI_ENV")" = 1 ] && grep -q '^GEMINI_API_KEY=.' "$GEMINI_ENV" \
  || fail_closed "워커 Gemini 환경 파일은 비어 있지 않은 GEMINI_API_KEY 한 줄이어야 한다"

run_managed pixlate-api unless-stopped -p 8000:8000 --env-file "$ENVFILE" "$IMAGE" "${API_CMD[@]}"
run_managed pixlate-worker-cpu unless-stopped --env-file "$ENVFILE" --env-file "$GEMINI_ENV" "$IMAGE" \
  python /opt/pixlate/bin/redact_run.py -- celery -A app.celery_app:celery_app worker -Q cpu --pool=solo -n "cpu@%h" --loglevel=info
run_managed pixlate-recovery unless-stopped --env-file "$ENVFILE" "$IMAGE" \
  python /opt/pixlate/bin/redact_run.py -- python -m app.recovery --interval 60
echo live > "$APPLIED"
log "live: release=$RELEASE_SHA"
