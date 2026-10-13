#!/usr/bin/env bash
# EC2 설치 위치: /opt/pixlate/start-pixlate.sh — pixlate.service ExecStart. 비밀 값 없음. set -x 금지.
# EC2 에는 API·cpu 워커·복구 프로세스와 Redis 만 둔다. ocr·gpu 큐는 학교 GPU pod 워커가 소비한다(gpu/README.md).
#
# 동작은 /etc/pixlate/deploy.mode 한 단어로 정한다. 파일이 없거나 값이 다르면 maintenance 로 동작한다(live 로 시작하지 않는다).
#   maintenance : API·복구·cpu 워커를 멈추고 자동 재시작을 끈다(재부팅해도 유입·소비·복구 없음). Redis 는 그대로 둔다
#   validate    : 고정 digest API 를 127.0.0.1 에만 연다(외부·ALB 유입 없음, 자동 재시작 끔). 워커·복구는 멈춘 채
#   live        : API 외부 공개 → cpu 워커 → 복구 프로세스 순서로 기동(자동 재시작 켬)
# 모드는 /opt/pixlate/bin/pixlate-mode 로만 바꾼다(파일 기록과 적용을 한 번에 해서 restart 정책과 모드가 어긋나지 않게 한다).
#
# 정지(graceful_stop): ① 대상 전부의 자동 재시작을 먼저 끈다 ② API(신규 유입 차단) → 복구 → cpu 순으로 TERM 만 보낸다
#   ③ 보낸 뒤 따로 기다린다(STOP_WAIT_S). 하나가 실패하거나 늦어도 나머지 대상에는 계속 요청한다.
#   대기가 끝나도 SIGKILL·삭제·재생성·unpause 를 하지 않는다. 정지 완료는 exited·created 만 인정한다
#   (paused·restarting·조회 실패는 미완료). 실패는 모아 보고하고 적용 상태를 `<모드>-incomplete` 로 남긴 뒤 실패로 끝낸다.
#   정지 중인 작업·요청이 후속 메시지를 만들 수 있으므로 정지 뒤 큐·DB 상태는 pixlate-mode 가 따로 다시 확인한다.
# 기동(run_managed): Docker 명령(update·start·rm·run) 실패를 하나하나 실패로 돌려주고, 기동 뒤 실행 중인지 확인한다.
#   적용 상태(applied)는 모든 대상이 확인된 뒤에만 기록한다.
#
# Redis: 기존 pixlate-redis 컨테이너·볼륨을 지우거나 다시 만들지 않는다. 멈춰 있으면 같은 컨테이너를 시작한다.
#        컨테이너가 없거나 조회할 수 없으면 빈 Redis 를 만들지 않고 나머지를 멈춘 뒤 중단한다(복구는 담당자 판단).
# 앱 이미지는 /etc/pixlate/release.conf 의 digest 로만 받는다. latest 태그는 쓰지 않는다.
# 컨테이너 사양(digest·명령·포트·마운트·env 파일 내용·래퍼 내용·restart)이 바뀌면 해당 컨테이너만 정지 확인 후 다시 만든다.
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
APPLIED=$CONF_DIR/deploy.mode.applied       # 마지막 적용 결과. 실패가 있으면 <모드>-incomplete
WRAP=${WRAP:-/opt/pixlate/bin/redact_run.py}  # 로그 정제 래퍼(시그널 전달·종료 코드 보존)
STOP_WAIT_S=${STOP_WAIT_S:-600}             # 정지 대기 상한(초). 넘어도 강제 종료하지 않고 실패로 보고
START_SETTLE_S=${START_SETTLE_S:-3}         # 기동 뒤 실행 상태를 확인하기까지 기다리는 시간(초)
NET=pixlate-net
STOP_ORDER=(pixlate-api pixlate-recovery pixlate-worker-cpu)   # 신규 유입 차단 → 재전달 중단 → 웜 종료
OFFLOADED=(pixlate-worker-gpu pixlate-worker-ocr)              # EC2 에서 돌리지 않는 큐의 옛 컨테이너(지우지 않고 멈춤)
FAILURES=()

log() { echo "[start-pixlate] $*"; }
die() { echo "[start-pixlate] 중단: $*" >&2; exit 1; }
note_fail() { FAILURES+=("$*"); echo "[start-pixlate] 실패: $*" >&2; }

mode=$( { tr -d ' \r\n' < "$MODE_FILE"; } 2>/dev/null || true)
case "$mode" in maintenance|validate|live) ;; *) log "모드 파일 없음/값 이상 → maintenance"; mode=maintenance ;; esac
log "mode=$mode"

conf() { grep -E "^$1=" "$RELEASE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r' || true; }

# 컨테이너 상태: Docker 상태 문자열, 실제로 없으면 absent, 조회 자체가 실패하면 unknown(없음으로 취급하지 않는다)
state() {
  local out
  if out=$($DOCKER container inspect -f '{{.State.Status}}' "$1" 2>&1); then echo "$out"
  elif printf '%s' "$out" | grep -qE 'No such (container|object)'; then echo absent  # 이 컨테이너가 없다는 명확한 응답만
  else echo unknown; fi  # 데몬·소켓 연결 오류(예: docker.sock "no such file or directory") 등은 없음이 아니다
}
running() { [ "$(state "$1")" = running ]; }

# 자동 재시작 끄기 → TERM 순차 전달 → 공동 대기. 실패는 모아 두고 끝까지 진행한다. SIGKILL·삭제·재생성·unpause 없음
graceful_stop() {
  local c st targets=()
  for c in "$@"; do
    st=$(state "$c")
    case "$st" in
      absent) ;;
      unknown) note_fail "$c 상태 조회 실패(없음으로 취급하지 않음)"; targets+=("$c") ;;
      *) targets+=("$c") ;;
    esac
  done
  [ ${#targets[@]} -gt 0 ] || return 0
  for c in "${targets[@]}"; do
    $DOCKER update --restart=no "$c" >/dev/null 2>&1 || note_fail "$c 자동 재시작 끄기 실패"
  done
  for c in "${targets[@]}"; do
    st=$(state "$c")
    case "$st" in
      running|restarting)
        if $DOCKER kill --signal=TERM "$c" >/dev/null 2>&1; then log "$c 정지 요청(TERM)"; else note_fail "$c TERM 전달 실패"; fi ;;
      paused) note_fail "$c paused 상태 — TERM 이 처리되지 않는다(자동 unpause 하지 않음)" ;;
    esac
  done
  local deadline=$(( $(date +%s) + STOP_WAIT_S ))
  for c in "${targets[@]}"; do
    while :; do
      st=$(state "$c")
      case "$st" in
        running|restarting|removing) [ "$(date +%s)" -lt "$deadline" ] || break; sleep 2 ;;
        *) break ;;
      esac
    done
    case "$st" in
      exited|created) log "$c 정지 확인($st, exit=$($DOCKER container inspect -f '{{.State.ExitCode}}' "$c" 2>/dev/null || echo '?'))" ;;
      absent) log "$c 정지 확인(없음)" ;;
      running|restarting|removing) note_fail "$c ${STOP_WAIT_S}초 안에 끝나지 않음($st, 강제 종료하지 않음)" ;;
      paused) ;;  # 위에서 이미 실패로 기록
      *) note_fail "$c 정지 확인 불가($st)" ;;
    esac
  done
}

# 적용 상태 파일은 임시 파일에 쓴 뒤 원자적으로 교체하고, 다시 읽어 확인한다. 하나라도 실패하면 실패를 돌려준다
write_applied() {
  local tmp
  tmp=$(mktemp "$CONF_DIR/.deploy.mode.applied.XXXXXX" 2>/dev/null) || return 1
  if printf '%s\n' "$1" > "$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$APPLIED" \
     && [ "$(cat "$APPLIED" 2>/dev/null)" = "$1" ]; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

# 시작할 때 <모드>-in-progress 로 표시해, 중간에 끝나거나 최종 기록이 실패해도 이전 완료 값이 최신처럼 남지 않게 한다.
# 표시에 실패하면 이전 값을 지우고 실패로 기록한다(정지는 계속하되 새로 띄우지는 않는다).
mark_in_progress() {
  write_applied "$1-in-progress" && return 0
  # 삭제 실패(읽기 전용 파일시스템 등)로 set -e 가 정지 전에 스크립트를 끝내지 않게 실패를 직접 처리한다
  rm -f "$APPLIED" 2>/dev/null || true
  if [ -e "$APPLIED" ]; then
    note_fail "적용 상태 기록 실패(진행 표시) — 이전 적용 값($(cat "$APPLIED" 2>/dev/null || echo '?'))을 지우지 못했다. 이 값은 최신이 아니다"
  else
    note_fail "적용 상태 기록 실패(진행 표시) — 이전 적용 값은 지웠다"
  fi
}

# 적용 결과를 남긴다. 실패가 하나라도 있으면 <모드>-incomplete 로 적고 실패. 기록 자체가 실패해도 실패
finish() {
  local label=$1 target
  if [ ${#FAILURES[@]} -eq 0 ]; then target=$label; else target=$label-incomplete; fi
  if ! write_applied "$target"; then
    note_fail "적용 상태 저장 실패($target) — 기록된 적용 상태는 최신이 아니다"
    return 1
  fi
  [ ${#FAILURES[@]} -eq 0 ] && return 0
  log "적용 미완료($label): 실패 ${#FAILURES[@]}건 — 남은 컨테이너는 그대로 두었다"
  return 1
}
no_failures() { [ ${#FAILURES[@]} -eq 0 ]; }

# 네트워크 확인 실패(데몬 연결 불가 등)도 실패로 기록하고 계속 진행해 나머지 대상의 상태를 보고한다(새로 띄우지는 않는다)
ensure_network() {
  $DOCKER network inspect "$NET" >/dev/null 2>&1 || $DOCKER network create "$NET" >/dev/null 2>&1     || note_fail "Docker 네트워크($NET) 확인·생성 실패(데몬 연결 불가 등)"
}

ensure_redis() {
  local st; st=$(state pixlate-redis)
  case "$st" in
    running) ;;
    absent|unknown)
      [ "$st" = unknown ] && note_fail "pixlate-redis 상태 조회 실패"
      graceful_stop "${STOP_ORDER[@]}" "${OFFLOADED[@]}"
      finish maintenance || true
      die "pixlate-redis 컨테이너가 없거나 조회할 수 없다($st). 빈 Redis 를 자동으로 만들지 않는다(앱 컨테이너는 정지 요청)" ;;
    *)
      if $DOCKER start pixlate-redis >/dev/null 2>&1 && running pixlate-redis; then
        log "기존 Redis 컨테이너 시작(재생성 안 함)"
      else
        note_fail "pixlate-redis 시작 실패($st)"
        graceful_stop "${STOP_ORDER[@]}" "${OFFLOADED[@]}"
        finish maintenance || true
        die "기존 Redis 컨테이너를 시작하지 못했다(재생성하지 않음)"
      fi ;;
  esac
}

filehash() { sha256sum "$1" 2>/dev/null | cut -c1-64 || echo missing; }

# 같은 사양이면 재사용, 다르면 정지 확인 뒤 교체(pixlate-redis 는 절대 대상이 아님).
# 조건문(&&·||) 안에서 불리므로 set -e 에 기대지 않고 Docker 명령마다 실패를 직접 돌려준다. 기동 뒤 실행 중인지 확인한다.
run_managed() {
  local name=$1 restart=$2; shift 2
  [ "$name" != pixlate-redis ] || die "Redis 는 run_managed 대상이 아니다"
  local parts=("$restart" "wrap=$(filehash "$WRAP")") prev="" a
  for a in "$@"; do
    parts+=("$a")
    [ "$prev" = "--env-file" ] && parts+=("content=$(filehash "$a")")  # 값 대신 내용 해시만 사양에 넣는다
    prev=$a
  done
  local spec; spec=$(printf '%s\0' "${parts[@]}" | sha256sum | cut -c1-16)
  local st; st=$(state "$name")
  if [ "$st" = unknown ]; then note_fail "$name 상태 조회 실패"; return 1; fi
  local cur=""
  if [ "$st" != absent ]; then
    if ! cur=$($DOCKER container inspect -f '{{index .Config.Labels "pixlate.spec"}}' "$name" 2>/dev/null); then
      note_fail "$name 사양 조회 실패"; return 1
    fi
  fi
  if [ "$st" != absent ] && [ "$cur" = "$spec" ]; then
    if ! $DOCKER update --restart="$restart" "$name" >/dev/null 2>&1; then note_fail "$name restart 정책 변경 실패"; return 1; fi
    if [ "$st" != running ]; then
      if ! $DOCKER start "$name" >/dev/null 2>&1; then note_fail "$name 시작 실패"; return 1; fi
    fi
  else
    if [ "$st" != absent ]; then
      local before=${#FAILURES[@]}
      graceful_stop "$name"
      [ ${#FAILURES[@]} -eq "$before" ] || return 1
      if ! $DOCKER rm "$name" >/dev/null 2>&1; then note_fail "$name 삭제 실패(교체 중단)"; return 1; fi
    fi
    if ! $DOCKER run -d --name "$name" --label "pixlate.spec=$spec" --label "pixlate.release=$RELEASE_SHA" \
        --network "$NET" --restart "$restart" --init -v "$WRAP:/opt/pixlate/bin/redact_run.py:ro" "$@" >/dev/null 2>&1; then
      note_fail "$name 생성 실패"; return 1
    fi
  fi
  sleep "$START_SETTLE_S"
  st=$(state "$name")
  if [ "$st" != running ]; then note_fail "$name 기동 직후 실행 중이 아님($st)"; return 1; fi
  log "$name 실행 확인(restart=$restart)"
}

mark_in_progress "$mode"
ensure_network
ensure_redis

if [ "$mode" = maintenance ]; then
  graceful_stop "${STOP_ORDER[@]}" "${OFFLOADED[@]}"
  finish maintenance || die "maintenance 정지 미완료(위 실패 참고)"
  log "maintenance: Redis 만 유지, 유입·소비·복구 정지 확인"
  exit 0
fi

RELEASE_SHA=$(conf RELEASE_SHA); API_DIGEST=$(conf API_DIGEST); REGISTRY=$(conf REGISTRY)
fail_closed() { graceful_stop "${STOP_ORDER[@]}" "${OFFLOADED[@]}"; finish maintenance || true; die "$*"; }
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
  # 외부 유입을 먼저 끊는다(API 포함 전부 정지 확인 후 127.0.0.1 전용 API 만 다시 연다)
  graceful_stop "${STOP_ORDER[@]}" "${OFFLOADED[@]}"
  no_failures || { finish validate || true; die "validate 전 정지 미완료 — API 를 다시 열지 않았다"; }
  run_managed pixlate-api no -p 127.0.0.1:8000:8000 --env-file "$ENVFILE" "$IMAGE" "${API_CMD[@]}" \
    || { finish validate || true; die "validate API 기동 실패"; }
  finish validate || die "validate 미완료"
  log "validate: API 는 127.0.0.1 에만 열림(외부 유입 없음), 워커·복구 정지. release=$RELEASE_SHA"
  exit 0
fi

# live
[ -s "$GEMINI_ENV" ] || fail_closed "워커 Gemini 환경 파일 없음"
[ "$(stat -c '%U:%G %a' "$GEMINI_ENV")" = "root:root 600" ] || fail_closed "워커 Gemini 환경 파일 소유자·권한은 root:root 600 이어야 한다"
[ "$(grep -cE '^[A-Za-z_][A-Za-z0-9_]*=' "$GEMINI_ENV")" = 1 ] && grep -q '^GEMINI_API_KEY=.' "$GEMINI_ENV" \
  || fail_closed "워커 Gemini 환경 파일은 비어 있지 않은 GEMINI_API_KEY 한 줄이어야 한다"

# 옛 워커(ocr·gpu) 정지가 확인되기 전에는 아무것도 새로 띄우지 않는다
graceful_stop "${OFFLOADED[@]}"
no_failures || { finish live || true; die "옛 워커 정지 미완료 — live 기동을 시작하지 않았다"; }
run_managed pixlate-api unless-stopped -p 8000:8000 --env-file "$ENVFILE" "$IMAGE" "${API_CMD[@]}" \
  && run_managed pixlate-worker-cpu unless-stopped --env-file "$ENVFILE" --env-file "$GEMINI_ENV" "$IMAGE" \
       python /opt/pixlate/bin/redact_run.py -- celery -A app.celery_app:celery_app worker -Q cpu --pool=solo -n "cpu@%h" --loglevel=info \
  && run_managed pixlate-recovery unless-stopped --env-file "$ENVFILE" "$IMAGE" \
       python /opt/pixlate/bin/redact_run.py -- python -m app.recovery --interval 60 \
  || { finish live || true; die "live 기동 실패(위 실패 참고) — 남은 컨테이너는 그대로 두었다"; }
finish live || die "live 미완료"
log "live: release=$RELEASE_SHA"
