#!/usr/bin/env bash
# GPU 이미지 로컬 점검(GPU 없이). 다시 빌드한 이미지를 Docker Hub 에 푸시하기 전에 돌린다.
#   A. 이미지 내용이 고정 파일·운영 기준선과 같은지  B. entrypoint PaddleX 캐시 링크 시나리오  C. 운영 freeze 로 워커 venv 를 재현해 import
# CUDA·LaMa 가중치·모델 로드는 확인하지 않는다(pod 교체 뒤 실제 워커 Python 으로 확인).
# 사용: bash gpu/tests/image_check.sh <이미지>   (Docker 필요. Windows 는 Git Bash). 마지막 줄 RESULT ... fail=0 이어야 한다
set -uo pipefail
IMG=${1:?usage: $0 <image>}
GPU_DIR=$(cd "$(dirname "$0")/.." && pwd); REPO=$(cd "$GPU_DIR/.." && pwd)
PASS=0; FAIL=0; RAN=0
ok()  { RAN=$((RAN+1)); PASS=$((PASS+1)); echo "PASS $*"; }
bad() { RAN=$((RAN+1)); FAIL=$((FAIL+1)); echo "FAIL $*"; }
t()   { local name=$1; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
hp()  { cygpath -w "$1" 2>/dev/null || echo "$1"; }   # docker -v 용 호스트 경로(Git Bash 면 Windows 경로)
inimg() { MSYS_NO_PATHCONV=1 docker run --rm --entrypoint bash "$IMG" -c "$1"; }
W=$(mktemp -d); trap 'rm -rf "$W"; docker rm -f $(docker ps -aq --filter label=pixlate-gpu-test) >/dev/null 2>&1' EXIT

# 기준선: /opt/venv 는 고정 파일(주석·pip 줄 제외), 워커 venv 는 gpu/baseline/ 의 운영 freeze
grep -v -E '^#|^pip==' "$GPU_DIR/opt-venv.constraints.txt" | tr -d '\r' > "$W/opt.expected"
grep -v '^#' "$GPU_DIR/baseline/venv-ocr.freeze.txt" | tr -d '\r' > "$W/venv-ocr.freeze"
sed 's/==.*//' "$W/opt.expected" | tr 'A-Z_' 'a-z-' > "$W/opt.names"
grep -v '^#' "$GPU_DIR/baseline/venv-gpu.freeze.txt" | tr -d '\r' \
  | awk -F'==' 'NR==FNR{o[$1];next} {n=tolower($1); gsub("_","-",n); if(!(n in o)) print}' "$W/opt.names" - > "$W/data-venv.only.txt"

echo "== A. 이미지 내용"
t "A1 OCR 런타임 라이브러리(worker.sh 와 같은 ldconfig 확인)" inimg 'for l in libgomp.so.1 libGL.so.1 libglib-2.0.so.0; do ldconfig -p | grep -q "$l" || exit 1; done'
t "A2 /usr/bin/python3.12 = 3.12.3(두 venv 의 home)" inimg '[ "$(/usr/bin/python3.12 -c "import platform;print(platform.python_version())")" = 3.12.3 ]'
inimg '/opt/venv/bin/pip freeze' | tr -d '\r' > "$W/opt.freeze"
t "A3 /opt/venv freeze = opt-venv.constraints.txt($(wc -l < "$W/opt.expected" | tr -d ' ')개)" diff "$W/opt.expected" "$W/opt.freeze"
t "A4 pip 26.2.1" inimg '[ "$(/opt/venv/bin/pip --version | cut -d" " -f2)" = 26.2.1 ]'
t "A5 시스템 dist-packages 비어 있음(기준선과 같음)" inimg '[ -z "$(ls -A /usr/lib/python3/dist-packages 2>/dev/null)" ]'
t "A6 torch·torchvision import, CUDA 12.6 빌드" inimg '/opt/venv/bin/python -c "import torch,torchvision; assert torch.__version__==\"2.14.0+cu126\" and torch.version.cuda==\"12.6\", torch.__version__; print(\"torch\", torch.__version__, \"cuda\", torch.version.cuda, \"avail\", torch.cuda.is_available())"'
t "A7 sshd 설정·/etc/environment" inimg '[ -f /etc/ssh/sshd_config.d/pixlate.conf ] && grep -q "^VIRTUAL_ENV=\"/opt/venv\"" /etc/environment && grep -q "^TORCH_HOME=\"/data/.cache/torch\"" /etc/environment'
t "A8 호스트키가 이미지에 없음" inimg '! ls /etc/ssh/ssh_host_* >/dev/null 2>&1'

echo "== B. entrypoint PaddleX 캐시 연결"
# $1 이름, $2 /data 준비(호스트 경로에서), $3 컨테이너 안 사전 명령(""=없음), $4 확인 명령(컨테이너 안), $5 로그에 기대하는 문자열(""=경고 없어야 함)
scen() {
  local name=$1 prep=$2 pre=$3 check=$4 want=$5 d="$W/$1" c="pxt-$1"
  mkdir -p "$d"; (cd "$d" && eval "$prep")
  local mnt=(-v "$(hp "$d"):/data"); [ "$name" = nomount ] && mnt=()
  if [ -n "$pre" ]; then
    MSYS_NO_PATHCONV=1 docker run -d --name "$c" --label pixlate-gpu-test "${mnt[@]}" --entrypoint bash "$IMG" -c "$pre; exec /usr/local/bin/entrypoint.sh" >/dev/null
  else
    MSYS_NO_PATHCONV=1 docker run -d --name "$c" --label pixlate-gpu-test "${mnt[@]}" "$IMG" >/dev/null
  fi
  sleep 3
  local logs; logs=$(docker logs "$c" 2>&1)
  t "B-$name sshd 가 PID 1 로 실행" bash -c "[ \"\$(MSYS_NO_PATHCONV=1 docker exec $c cat /proc/1/comm)\" = sshd ]"
  t "B-$name 결과 확인" bash -c "MSYS_NO_PATHCONV=1 docker exec $c bash -c '$check'"
  if [ -n "$want" ]; then t "B-$name 경고 기록" grep -q "$want" <<<"$logs"
  else t "B-$name PaddleX 경고 없음" bash -c "! grep -q 'paddlex\|PaddleX' <<<\"\$1\"" _ "$logs"; fi
  if [ "$name" = empty ]; then
    docker restart "$c" >/dev/null; sleep 3
    t "B-$name 재시작 후에도 같은 결과(반복 실행)" bash -c "MSYS_NO_PATHCONV=1 docker exec $c bash -c '$check'"
    t "B-$name 재시작 후 경고 없음" bash -c "! docker logs $c 2>&1 | grep -q 'PaddleX\|paddlex'"
  fi
  docker rm -f "$c" >/dev/null
}
LINKOK='[ -L /root/.paddlex ] && [ "$(readlink /root/.paddlex)" = /data/.cache/paddlex ] && [ -d /data/.cache/paddlex ]'
scen empty    ':' '' "$LINKOK" ''
scen existing 'mkdir -p .cache/paddlex/official_models/m && printf model > .cache/paddlex/official_models/m/w.bin' '' \
  "$LINKOK && [ \"\$(cat /data/.cache/paddlex/official_models/m/w.bin)\" = model ] && [ \"\$(cat /root/.paddlex/official_models/m/w.bin)\" = model ]" ''
scen targetfile 'mkdir -p .cache && printf keep > .cache/paddlex' '' \
  '[ ! -e /root/.paddlex ] && [ -f /data/.cache/paddlex ] && [ "$(cat /data/.cache/paddlex)" = keep ]' '디렉터리가 아니다'
scen cachefile 'printf keep > .cache' '' \
  '[ ! -e /root/.paddlex ] && [ -f /data/.cache ] && [ "$(cat /data/.cache)" = keep ]' '생성 실패'
scen linkisdir ':' 'mkdir -p /root/.paddlex && printf mine > /root/.paddlex/f' \
  '[ -d /root/.paddlex ] && [ ! -L /root/.paddlex ] && [ "$(cat /root/.paddlex/f)" = mine ]' '링크 아님'
scen linkother ':' 'mkdir -p /opt/other && ln -s /opt/other /root/.paddlex' \
  '[ "$(readlink /root/.paddlex)" = /opt/other ]' '다른 곳을 가리킨다'
scen nomount ':' '' "$LINKOK" ''

echo "== C. 워커 Python 재현(운영 freeze 그대로 설치, /data 대신 /tmp, GPU·가중치 없음)"
MSYS_NO_PATHCONV=1 docker run --rm --entrypoint bash -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$(hp "$W")":/w -v "$(hp "$REPO")":/src:ro "$IMG" -c '
set -u
r() { local n=$1; shift; if "$@" > /tmp/out 2>&1; then echo "PASS $n"; else echo "FAIL $n"; tail -3 /tmp/out | sed "s/^/    /"; fi; }
# ocr venv: 운영과 같이 시스템 Python 으로 만들고 운영 freeze 그대로 설치
/usr/bin/python3.12 -m venv /tmp/venv-ocr
r "C1 ocr venv 운영 freeze 설치" /tmp/venv-ocr/bin/pip install -q --no-deps -r <(tr -d "\r" < /w/venv-ocr.freeze)
r "C2 ocr venv pip check" /tmp/venv-ocr/bin/pip check
r "C3 ocr venv import paddle·paddleocr·cv2(런타임 라이브러리 로드)" /tmp/venv-ocr/bin/python -c "import paddle, cv2, paddleocr; print(paddle.__version__, cv2.__version__)"
cd /src
r "C4 ocr venv 레포 코드 import(app.celery_app·pipeline.stages.ocr·ocr_tile)" /tmp/venv-ocr/bin/python -c "import app.celery_app, pipeline.stages.ocr, pipeline.stages.ocr_tile"
cd /
# gpu venv: 운영과 같이 --system-site-packages + /opt/venv .pth 공유, /opt/venv 에 없는 패키지만 운영 freeze 대로 설치
/usr/bin/python3.12 -m venv --system-site-packages /tmp/venv
echo /opt/venv/lib/python3.12/site-packages > /tmp/venv/lib/python3.12/site-packages/opt-venv.pth
r "C5 gpu venv 운영 freeze(/opt/venv 에 없는 것만) 설치" /tmp/venv/bin/pip install -q --no-deps -r <(tr -d "\r" < /w/data-venv.only.txt)
r "C6 gpu venv pip check" /tmp/venv/bin/pip check
r "C7 gpu venv 의 torch 가 /opt/venv 것" /tmp/venv/bin/python -c "import torch; assert torch.__file__.startswith(\"/opt/venv/\"), torch.__file__; print(torch.__file__)"
cd /src
r "C8 gpu venv 레포 코드 import(app.celery_app·pipeline.stages.inpaint·inpaint_lama)" /tmp/venv/bin/python -c "import app.celery_app; from pipeline.stages import inpaint, inpaint_lama"
/tmp/venv/bin/python /src/gpu/lama_check.py > /tmp/lama.out 2>&1; echo "INFO lama_check(로컬, GPU·가중치 없음 — cuda·weights·model_load 실패가 정상):"; sed "s/^/    /" /tmp/lama.out | head -8
' > "$W/c.out" 2>&1
while read -r st rest; do case $st in PASS) ok "$rest";; FAIL) bad "$rest";; esac; done < <(grep -E '^(PASS|FAIL) ' "$W/c.out")
grep -v -E '^(PASS|FAIL) ' "$W/c.out"
grep -q -E '^(PASS|FAIL) C8' "$W/c.out" || bad "C 단계 결과 누락(C8 없음)"

echo "RESULT ran=$RAN pass=$PASS fail=$FAIL"
