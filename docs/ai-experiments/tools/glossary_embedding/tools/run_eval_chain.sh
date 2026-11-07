#!/usr/bin/env bash
# 2026-10-06 실험 순서 재현: ko_en 임베딩 → ko_en 인덱스 → ko 평가 → ko_en 평가 → (메모리 되면) 지연시간 측정.
# 단계마다 프로세스를 새로 띄워 메모리를 비운다(RAM 8GB PC 기준). 로그는 logs/chain_*.log
# 전제: ko 인덱스(05_experiments.py build --text-mode ko)와 평가셋(make_eval_team.py)이 이미 있음.
#   PY=/path/to/venv/python bash tools/run_eval_chain.sh
cd "$(dirname "$0")/.." || exit 1
export PYTHONUTF8=1 EMBED_BATCH_SIZE=${EMBED_BATCH_SIZE:-8}
PY=${PY:-python}
mkdir -p logs
log() { echo "[chain $(date '+%H:%M:%S')] $*"; }

step() {  # step <이름> <명령...>
  local name=$1; shift
  log "시작: $name"
  if "$@" > "logs/chain_${name}.log" 2>&1; then log "완료: $name"; else log "실패: $name (logs/chain_${name}.log)"; return 1; fi
}

# 1,000행씩 캐시에 저장하므로 중간에 멈추면 같은 명령으로 이어서 한다
step embed_ko_en $PY tools/embed_corpus.py --text-mode ko_en --chunk 500 --min-free-gb 0.5 || exit 1
step build_ko_en $PY 05_experiments.py build --text-mode ko_en || exit 1
step eval_ko     $PY 05_experiments.py eval  --text-mode ko
step eval_ko_en  $PY 05_experiments.py eval  --text-mode ko_en

free=$($PY -c "import psutil;print(round(psutil.virtual_memory().available/1e9,2))")
if $PY -c "import sys;sys.exit(0 if $free >= 1.2 else 1)"; then
  step latency_ko $PY 05_experiments.py latency --text-mode ko
else
  log "건너뜀: 지연시간 측정 — 여유 RAM ${free}GB < 1.2GB (모델+인덱스 동시 적재 위험)"
fi
log "전체 끝"
