#!/usr/bin/env bash
# /opt/pixlate/fe/pixlate-fe.sh run|stop|status — pixlate-fe.service 가 부른다(run = ExecStart, stop = ExecStop).
#
# 재시작 주체는 systemd 하나다(compose 는 restart: "no"). run 은 기동 조건을 확인해 FE 를 띄운 뒤 컨테이너가 끝날 때까지
# 기다린다(docker wait). 실행 중 FE 가 끝나면 run 이 실패로 끝나고 systemd(Restart=on-failure)가 조건부터 다시 확인한다.
#
# 기동 조건 — 모두 맞아야 띄운다:
#   요청 모드(deploy.mode) = live, 적용 결과(deploy.mode.applied) = live, 이번 BE 기동 성공(pixlate.service active/success)
#   하나라도 아니면 FE 를 정지 상태로 두고 0 으로 끝낸다(재시도하지 않는다. BE 가 다시 적용되면 pixlate.service 를 따라 다시 실행된다).
# 기동 실패 — 1 로 끝내 systemd 가 다시 시도한다:
#   Docker 조회 실패, BE 네트워크 없음, 이미지가 digest 아님, compose up 실패, healthy 아님, FE 컨테이너에서 pixlate-api:8000 에 닿지 않음.
#
# 정지는 TERM 만 보낸다(docker kill --signal=TERM). STOP_WAIT_S 안에 끝나지 않으면 미완료로 보고하고 1 로 끝낸다.
# SIGKILL·docker stop·삭제는 하지 않는다. Docker 조회가 실패하면 '없음'으로 보지 않고 정지 미확인으로 보고한다.
# 비밀 값은 이 파일·fe.env 에 없다. set -x 를 쓰지 않는다.
set -uo pipefail
CONF_DIR=${CONF_DIR:-/etc/pixlate}
FE_DIR=${FE_DIR:-/opt/pixlate/fe}
RUN_DIR=${RUN_DIR:-/run/pixlate-fe}
DOCKER=${DOCKER:-docker}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
BE_UNIT=${BE_UNIT:-pixlate.service}
HEALTH_WAIT_S=${HEALTH_WAIT_S:-120}
STOP_WAIT_S=${STOP_WAIT_S:-120}     # pixlate-fe.service 의 TimeoutStopSec 보다 작아야 한다
ENV_FILE=$CONF_DIR/fe.env
NET=pixlate-net
C=pixlate-fe
STOPPING=$RUN_DIR/stopping          # stop 이 남기는 표시. run 이 컨테이너 종료를 '정지 요청'으로 구분한다

log() { echo "[pixlate-fe] $*"; }
err() { echo "[pixlate-fe] 실패: $*" >&2; }
die() { err "$*"; exit 1; }
dc()  { $DOCKER compose -f "$FE_DIR/compose.yaml" --env-file "$ENV_FILE" "$@"; }
readf() { [ -r "$1" ] && tr -d ' \r\n' < "$1"; }

# start-pixlate.sh 의 state() 와 같다: Docker 상태 / 실제로 없으면 absent / 조회 자체가 실패하면 unknown(없음으로 보지 않는다)
state() {
  local out
  if out=$($DOCKER container inspect -f '{{.State.Status}}' "$C" 2>&1); then echo "$out"
  elif printf '%s' "$out" | grep -qE 'No such (container|object)'; then echo absent
  else echo unknown; fi
}
health() { $DOCKER container inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$C" 2>/dev/null || echo unknown; }

# TERM 전달 → STOP_WAIT_S 까지 확인. 0 = 정지 확인(또는 없음), 1 = 미완료·미확인
stop_fe() {
  local st; st=$(state)
  case "$st" in
    absent) log "정지 확인(컨테이너 없음)"; return 0 ;;
    exited|created|dead) log "정지 확인($st)"; return 0 ;;
    unknown) err "Docker 조회 실패 — FE 정지 여부 미확인"; return 1 ;;
    paused) err "paused 상태 — TERM 이 처리되지 않는다(자동 unpause 하지 않음)"; return 1 ;;
  esac
  $DOCKER kill --signal=TERM "$C" >/dev/null 2>&1 || { err "TERM 전달 실패($st)"; return 1; }
  log "정지 요청(TERM)"
  local deadline=$(( $(date +%s) + STOP_WAIT_S ))
  while :; do
    st=$(state)
    case "$st" in
      running|restarting|removing) [ "$(date +%s)" -lt "$deadline" ] || break; sleep 1 ;;
      *) break ;;
    esac
  done
  case "$st" in
    exited|absent) log "정지 확인($st)"; return 0 ;;
    running|restarting|removing) err "${STOP_WAIT_S}초 안에 끝나지 않음($st, 강제 종료하지 않음)"; return 1 ;;
    *) err "정지 확인 불가($st)"; return 1 ;;
  esac
}

# BE 가 이번에 live 로 적용됐는지. 아니면 이유를 남기고 1
be_live() {
  local req app act res
  req=$(readf "$CONF_DIR/deploy.mode"); app=$(readf "$CONF_DIR/deploy.mode.applied")
  act=$($SYSTEMCTL show "$BE_UNIT" -p ActiveState --value 2>/dev/null); res=$($SYSTEMCTL show "$BE_UNIT" -p Result --value 2>/dev/null)
  if [ "$req" = live ] && [ "$app" = live ] && [ "$act" = active ] && [ "$res" = success ]; then return 0; fi
  log "BE 가 live 로 적용되지 않음(requested=${req:-없음} applied=${app:-없음} ${BE_UNIT}=${act:-?}/${res:-?}) → FE 를 띄우지 않는다"
  return 1
}

case "${1:-}" in
  run)
    mkdir -p "$RUN_DIR" && rm -f "$STOPPING"
    if ! be_live; then stop_fe || exit 1; exit 0; fi
    [ -r "$ENV_FILE" ] || die "$ENV_FILE 없음"
    img=$(grep -E '^FE_IMAGE=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d ' \r')
    [[ "$img" =~ @sha256:[0-9a-f]{64}$ ]] || die "FE_IMAGE 는 digest(@sha256:)로 지정해야 한다"
    [ "$(state)" != unknown ] || die "Docker 조회 실패"
    $DOCKER network inspect "$NET" >/dev/null 2>&1 || die "BE 네트워크 $NET 없음"
    dc up -d --pull missing --no-build >/dev/null 2>&1 || die "compose up 실패(이미지 pull·포트 등 — docker compose logs 확인)"
    for _ in $(seq "$HEALTH_WAIT_S"); do
      [ "$(health)" = healthy ] && break
      [ "$(state)" = running ] || break
      sleep 1
    done
    if [ "$(health)" != healthy ]; then h="$(state)/$(health)"; stop_fe; die "healthy 아님($h)"; fi
    if ! $DOCKER exec "$C" wget -q -T 5 -O /dev/null http://pixlate-api:8000/ >/dev/null 2>&1; then
      stop_fe; die "FE 컨테이너에서 pixlate-api:8000 에 닿지 않음"
    fi
    log "기동(running/healthy, API 연결 확인) — 종료를 감시한다"
    code=$($DOCKER wait "$C" 2>/dev/null) || code=unknown
    if [ -e "$STOPPING" ]; then log "정지 요청에 따른 종료(exit=$code)"; exit 0; fi
    die "FE 가 실행 중 끝났다(exit=$code)"
    ;;
  stop)
    mkdir -p "$RUN_DIR" && : > "$STOPPING"
    stop_fe
    ;;
  status)
    echo "requested=$(readf "$CONF_DIR/deploy.mode" || echo none) applied=$(readf "$CONF_DIR/deploy.mode.applied" || echo none) fe=$(state)/$(health)"
    ;;
  *) echo "usage: $0 run|stop|status" >&2; exit 2 ;;
esac
