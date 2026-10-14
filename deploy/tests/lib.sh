# 호스트에서 도는 시험 실행기(run.sh·fe_systemd.sh·fe_mutation.sh) 공용 — source 해서 쓴다.
#   - 실행 ID: 자원 이름에 붙여 실행마다 다르게 한다(이전 시험이 남아 있어도 서로 지우지 않는다)
#   - 잠금: 시험 실행기는 한 번에 하나만(같은 PC 의 Docker 를 나눠 쓰면 시간·자원이 엉킨다)
#   - 정리: 끝나거나 중단되면 이 실행의 하위 프로세스와 이 실행이 만든 자원만 지운다
# 운영 서버에는 연결하지 않는다.
RUN_ID=${RUN_ID:-$(date +%Y%m%d%H%M%S)-$$}
TEST_LOCK=${TEST_LOCK:-${TMPDIR:-/tmp}/pixlate-deploy-tests.lock}
_OWN_CONTAINERS=(); _OWN_VOLUMES=(); _OWN_IMAGES=(); _LOCKED=0

# 잠금: 디렉터리 생성(원자적). 잡고 있는 실행이 살아 있으면 시작하지 않는다. 죽은 실행의 잠금이면 알리고 넘겨받는다
test_lock() {
  if ! mkdir "$TEST_LOCK" 2>/dev/null; then
    local holder; holder=$(cat "$TEST_LOCK/pid" 2>/dev/null || true)
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
      echo "중단: 다른 시험 실행이 진행 중이다(pid $holder, 실행 $(cat "$TEST_LOCK/run" 2>/dev/null)). 끝난 뒤 다시 실행한다." >&2; exit 2
    fi
    echo "알림: 끝난 실행의 잠금(pid ${holder:-?})을 넘겨받는다" >&2
    rm -rf "$TEST_LOCK"; mkdir "$TEST_LOCK" || { echo "중단: 잠금을 만들지 못했다" >&2; exit 2; }
  fi
  echo $$ > "$TEST_LOCK/pid"; echo "$RUN_ID" > "$TEST_LOCK/run"; _LOCKED=1
}

# 오래 걸리는 명령은 이것으로 부른다: 백그라운드로 띄우고 wait 로 기다려야 중단 신호(TERM·INT)가 바로 처리된다
# (bash 는 앞에서 기다리는 명령이 끝날 때까지 trap 을 미룬다. $(…) 로 감싸도 마찬가지라 쓰지 않는다).
# 출력은 이 실행의 임시 파일($CHILD_OUT)에 남기고 종료 코드를 돌려준다. 파일은 정리 때 지운다
_OWN_FILES=(); CHILD_OUT=""
run_child() {
  CHILD_OUT=$(mktemp "${TMPDIR:-/tmp}/pixlate-test-$RUN_ID.XXXXXX"); _OWN_FILES+=("$CHILD_OUT")
  "$@" > "$CHILD_OUT" 2>&1 & local c=$!
  wait "$c"
}

# 소스 고정: 실행 시작 때 deploy/ 를 이 실행의 임시 폴더로 복사해 그 사본으로 시험한다(SRC_SNAP).
# 끝날 때 원본 해시를 다시 계산해, 실행 도중 원본이 바뀌었으면 '무효'를 출력한다 — 결과는 고정 사본 기준이지만 최종 근거로 쓰지 않는다.
# 해시 대상: deploy/ 의 모든 파일(itest/ 와 *.md 제외 — 시험이 읽지 않는다)
_OWN_DIRS=(); SRC_ORIG=""; SRC_SNAP=""; SRC_HASH=""
src_manifest() { (cd "$1" && find . -type f ! -path './itest/*' ! -name '*.md' | LC_ALL=C sort | xargs sha256sum) | sha256sum | cut -c1-16; }
freeze_source() {
  SRC_ORIG=$1
  SRC_SNAP=$(mktemp -d "${TMPDIR:-/tmp}/pixlate-test-src-$RUN_ID.XXXXXX") || { echo "환경 오류: 사본 폴더를 만들지 못했다" >&2; exit 1; }
  _OWN_DIRS+=("$SRC_SNAP")
  cp -r "$SRC_ORIG/." "$SRC_SNAP/" || { echo "환경 오류: 소스 복사 실패" >&2; exit 1; }
  SRC_HASH=$(src_manifest "$SRC_ORIG")
  [ "$(src_manifest "$SRC_SNAP")" = "$SRC_HASH" ] || { echo "환경 오류: 사본 해시가 원본과 다르다(복사 중 변경)" >&2; exit 1; }
  local ver; ver=$(git -C "$SRC_ORIG" rev-parse --short HEAD 2>/dev/null || echo '?')
  [ -n "$(git -C "$SRC_ORIG" status --porcelain -- . 2>/dev/null)" ] && ver="$ver + 미커밋 변경"
  echo "소스 고정: $ver, 해시 $SRC_HASH"
}
# 0 = 원본이 그대로, 3 = 실행 도중 바뀜(무효)
source_check() {
  local now; now=$(src_manifest "$SRC_ORIG")
  if [ "$now" = "$SRC_HASH" ]; then echo "소스 확인: 실행 전후 해시 동일($SRC_HASH)"; return 0; fi
  echo "무효: 실행 도중 원본 소스가 바뀌었다($SRC_HASH → $now). 결과는 고정 사본 기준이며 최종 근거로 쓰지 않는다"; return 3
}

own_container() { _OWN_CONTAINERS+=("$1"); }
own_volume()    { _OWN_VOLUMES+=("$1"); }
own_image()     { _OWN_IMAGES+=("$1"); }

# 이름이 이미 있으면(다른 실행의 자원) 지우지 않고 중단
require_absent() {
  local kind=$1 n=$2
  if docker "$kind" inspect "$n" >/dev/null 2>&1; then echo "중단: $kind $n 이 이미 있다(이 실행이 만든 것이 아니므로 지우지 않는다)" >&2; exit 2; fi
}

test_cleanup() {
  local rc=$? p
  trap - EXIT INT TERM
  for p in $(jobs -p 2>/dev/null); do kill "$p" 2>/dev/null; done
  # 컨테이너를 지우면 그 안으로 붙은 docker exec 클라이언트도 끝난다
  for p in "${_OWN_CONTAINERS[@]}"; do docker rm -f "$p" >/dev/null 2>&1; done
  for p in "${_OWN_VOLUMES[@]}"; do docker volume rm "$p" >/dev/null 2>&1; done
  for p in "${_OWN_IMAGES[@]}"; do docker image rm "$p" >/dev/null 2>&1; done
  for p in "${_OWN_FILES[@]}"; do rm -f "$p"; done
  for p in "${_OWN_DIRS[@]}"; do rm -rf "$p"; done
  [ "$_LOCKED" = 1 ] && [ "$(cat "$TEST_LOCK/pid" 2>/dev/null)" = "$$" ] && rm -rf "$TEST_LOCK"
  exit "$rc"
}
trap test_cleanup EXIT
trap 'echo "중단 요청 — 이 실행의 자원을 정리한다" >&2; exit 130' INT TERM
