# glossary_embedding — ⑧ 용어집 RAG 검색 실험 도구 (BGE-M3 × Qdrant 로컬 모드)

> 실험 기록: `docs/ai-experiments/2026-10-06_08-glossary-embedding_bge-m3-eval.md` (결과 파일은 `results/`에 생기며 git에 올리지 않는다)
> **실험 도구다.** `pipeline/`(⑧ 번역)에 통합하지 않았고, 서버 `requirements.txt`·config·정본 문서를 바꾸지 않는다.

| 고정 | 값 |
|---|---|
| 임베딩 | `BAAI/bge-m3` dense 1024 · `normalize_embeddings=True` · 코사인 · `max_seq_length=512` |
| 벡터DB | `qdrant-client>=1.10` 로컬 모드(`QdrantClient(path=…)` / `":memory:"`) — 서버 · Docker 없음 |
| 용어집 | 운영 RDS `glossary` 스냅샷(`db_snapshot`, 기본) 또는 데이터팀 전달본 xlsx(`team`) |

## 1. 설치

```bash
python -m venv C:\venvs\pixate-emb
C:\venvs\pixate-emb\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
C:\venvs\pixate-emb\Scripts\pip install -r requirements.txt
```

Windows에서 torch 가 `c10.dll` 오류로 안 뜨면 Microsoft Visual C++ 재배포 패키지가 필요하다. 첫 실행 때 모델(약 2.3GB)을 받는다.

## 2. 데이터 준비 (git 제외 — `data/README.md`)

**전달본 xlsx** → `data/deliveries/<전달본>/` (기본 `2026-09-28`)

**운영 DB 스냅샷** → `data/db_snapshot/` — EC2에서 읽기 전용으로 내보낸다(쓰기 없음).

```bash
mkdir -p ~/glossary-export
sudo docker run --rm -i --network pixlate-net --env-file /etc/pixlate/pixlate.env -v ~/glossary-export:/out pixlate-api:glossary-d2a4ea6 python - <<'PY'
import datetime, hashlib, json
from sqlalchemy import text
from app.db import engine
COLS = ["id","external_id","term_ko","term_target","target_lang","internal_category","enforcement",
        "example_sentence","zone","term_kind","frequency","corpus_size","corpus_version","verified_at",
        "source","note","version","load_flag","label","representative"]
with engine.connect() as c:
    c.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    ver = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    rows = c.execute(text(f"SELECT {', '.join(COLS)} FROM glossary ORDER BY external_id NULLS LAST, id")).mappings().all()
    c.rollback()
day = datetime.date.today().strftime("%Y%m%d")
with open(f"/out/glossary_db_{day}.jsonl", "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(dict(r), ensure_ascii=False, default=str) + "\n")
pairs = sorted((r["external_id"], r["id"]) for r in rows if r["external_id"])
meta = {"exported_at": datetime.datetime.now().isoformat(timespec="seconds"), "alembic": ver,
        "rows": len(rows), "rows_with_external_id": len(pairs), "columns": COLS,
        "pk_fingerprint_md5": hashlib.md5(",".join(f"{e}:{i}" for e, i in pairs).encode("utf-8")).hexdigest()}
with open(f"/out/glossary_db_{day}.meta.json", "w", encoding="utf-8") as f:
    json.dump(meta, f, ensure_ascii=False, indent=2)
print(json.dumps(meta, ensure_ascii=False, indent=2))
PY
```

`pk_fingerprint_md5`는 `python -m app.glossary_ingest plan`의 PK 지문과 같은 계산이다(같아야 정상). S3 경유로 PC에 받고
`config.DB_SNAPSHOT`(기본 `glossary_db_20261001`)을 파일 이름에 맞춘다.

## 3. 실행 순서

| # | 명령 | 하는 일 | 모델 |
|---|---|---|---|
| 1 | `python 01_sanity_check.py` | 1024차원 · 정규화 · 도메인 쌍 유사도 · 처리량 | 필요 |
| 2 | `python tools/compare_snapshot_vs_files.py` | DB 스냅샷 ↔ 전달본 파일 한 칸씩 비교 · 내부 PK 범위 | 불필요 |
| 3 | `python tools/embed_corpus.py --text-mode ko` (·`ko_en`) | 용어집 전체 임베딩을 캐시에 저장(500행마다 저장 · 여유 RAM 감시) | 필요 |
| 4 | `python 05_experiments.py build --text-mode ko` | 캐시 벡터로 컬렉션 적재 · 매니페스트 기록 | 캐시 미스만 |
| 5 | `python make_eval_team.py` | 평가셋 v2(`data/eval_team_v2.csv`, 892문항) 생성 | 불필요 |
| 6 | `python 05_experiments.py eval --text-mode ko [--tag r2-review]` | 평가셋 v2 × 5개 조건 — 쿼리 임베딩(캐시) → 모델 해제 → 검색. 요청마다 30개를 받아 같은 최종 후보 수 K(1·3·5·10·20·30)로 채점, 전성분은 보조 지표 `@5n`(n = 입력 조각 수). 조건별 요청 수 · 검색 시간 기록 | 첫 실행만 |
| 7 | `python 05_experiments.py ids · filters · tokens · ocrfix · stale` | ID 연결 · 필터 · 토큰 잘림 · 치환 손상 · 오래된 벡터 재사용 검증 | 불필요(tokens는 토크나이저만) |
| 8 | `python 05_experiments.py latency` | 쿼리 임베딩 · 검색 지연 · 메모리 | 필요 |
| — | `python 03_search.py -q "문장" [-c Face] [--kind ingredient --ocr-fix]` | 대화형 검색(결과에 원본 ID · 내부 PK) | 필요 |
| — | `bash tools/run_eval_chain.sh` | 3(ko_en)→4→6(ko·ko_en)→8 을 단계별 새 프로세스로 실행 | — |
| — | `python -m pytest tests -q` | 평가 지표 회귀 테스트(가짜 Qdrant 클라이언트 — 모델 · 인덱스 · 실제 데이터 불필요): 고정 K · `@5n` 조각 수 · 16위 이후 정답 보존 · 조각 합치기 · 카테고리 차단 문항 분리 | 불필요 |

결과는 `results/<날짜>/<항목>.json`(+ 조건별 문항 CSV), `eval`은 `results/<날짜>/<tag>/`. 로그는 `logs/`.

## 4. 파일

| 경로 | 내용 |
|---|---|
| `config.py` | 모델 · `QDRANT_MODE`(local/memory) · 경로 · `GLOSSARY_SOURCE`(db_snapshot/team/sample) · `EMBED_TEXT_MODE`(ko/ko_en) · 임계값 · 전달본/스냅샷 이름 |
| `pixemb/embedder.py` | BGE-M3 래퍼(장치 자동 · GPU면 fp16) |
| `pixemb/glossary.py` | 전달본 xlsx 로더(서버 로더와 같은 적재 규칙 · 값 원문 보존) · DB 스냅샷 로더(행 수 · PK 지문 검증) |
| `pixemb/cache.py` | 임베딩 캐시(SQLite, 키 = 모델 · 정규화 · 최대 길이 · 텍스트) · 캐시 검증 |
| `pixemb/store.py` | Qdrant 적재 · **증분 동기화(삽입/벡터 갱신/payload 갱신/유지/index_only)** · 매니페스트 · 검색 필터(카테고리 · common 폴백 · 종류 포함/제외) |
| `pixemb/textnorm.py` | 데이터팀 성분 OCR 치환 규칙(전성분 문단 전용) |
| `01_sanity_check.py` · `02_build_index.py` · `03_search.py` | 점검 · 단독 적재 · 대화형 검색 |
| `05_experiments.py` · `experiments_eval.py` · `experiments_util.py` | 검증 실험 모음 · 평가 v2 본체(must/any 지표 · 고정 K · 임계값표 · 카테고리 차단 점검) · 결과 저장 |
| `make_eval_team.py` | 전달본 파일의 정답으로 평가셋 v2 생성(출처 · 독립성 · 금지 ID `forbidden_ids` 열 포함) |
| `tests/test_glossary_embedding_eval.py` · `conftest.py` | 평가 지표 회귀 테스트 · 도구 폴더 import 경로 |
| `tools/embed_corpus.py` · `tools/compare_snapshot_vs_files.py` · `tools/run_eval_chain.sh` | 전체 임베딩 · 스냅샷 대조 · 실험 순서 재현 |

## 5. 저장 위치와 주의

- 인덱스: `~/.pixate/qdrant/<컬렉션>/` — 컬렉션마다 폴더를 따로 둔다. 로컬 모드는 폴더를 열 때 안의 **모든** 컬렉션을 RAM에 올린다.
- 컬렉션 이름: `glossary__<전달본 또는 db_YYYYMMDD>__<ko|ko_en>` — 서로 덮어쓰지 않는다.
- 매니페스트 `~/.pixate/index_manifest.json`: 파일 해시 · 모델 설정 · 행 수. 다르면 `open_index`가 재사용을 거부하고 차이(계획)를 보여 준다.
  차이만 반영하려면 `sync=True`(05 `build`가 사용). 파일에 없는 기존 점은 지우지 않고 센다(서버 로더와 같은 원칙).
- 임베딩 캐시 `~/.pixate/embed_cache.sqlite`: 두 번째 실행부터 모델 없이 적재 · 평가.
- 로컬 모드는 한 폴더를 한 프로세스만 연다. 노트북과 스크립트를 동시에 열지 않는다.
- RAM 8GB PC에서는 모델과 2만 점 인덱스를 한 프로세스에 함께 올리면 메모리가 모자랄 수 있다. 평가 단계는 모델을 내린 뒤 인덱스를 연다.
