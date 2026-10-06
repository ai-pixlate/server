# Pixlate Server

AI 상세페이지 로컬라이제이션 서비스 **Pixlate**의 백엔드(BE)와 AI 파이프라인을 함께 관리하는 레포지터리입니다.
한국어 상세페이지 이미지를 분석하고, 섹션 확인과 라벨·로고 판정 뒤 인페인팅·스타일 추출·번역을 거쳐 타겟 국가용 이미지를 만듭니다. BE 통합의 목표 흐름이며, 현재 워커의 스텁·미연결 상태는 아래 표와 구분합니다.

- API 문서 사이트: https://ai-pixlate.github.io/server/
- 기여 규칙(브랜치·커밋·PR): [AGENTS.md](AGENTS.md)

## 아키텍처

```mermaid
flowchart LR
    FE[Frontend] -->|REST /v1| API[FastAPI<br/>app/]
    API --> DB[(PostgreSQL)]
    API --> S3[(S3)]
    API -->|.delay| R[(Redis<br/>Celery broker)]
    R --> W_OCR[워커 · ocr 큐<br/>분석/OCR]
    R --> W_CPU[워커 · cpu 큐<br/>번역 · 렌더]
    R --> W_GPU[워커 · gpu 큐<br/>인페인팅]
    W_OCR & W_CPU & W_GPU --> DB
    W_CPU & W_GPU --> S3
```

### 작업(Job) 흐름

| 단계 | 내용 | 처리 위치 | 큐 | 상태 |
| --- | --- | --- | --- | --- |
| N2 | 분석 · OCR, 섹션 분리 | `run_analyze` | `ocr` | 🚧 스텁 (PaddleOCR 예정) |
| N3 | 섹션 확인 (사용자) | API `SEC-01~04` | - | ✅ 기존 API · 통합 완료 조건 연결 예정 |
| N4 | 번역 | `run_translate` | `cpu` | 🚧 스텁 (LLM 예정) |
| N4 | 인페인팅 (원문 텍스트 제거) | `run_inpaint` | `gpu` | 🚧 스텁 (LaMa 예정) |
| N5 | 번역 검수 (사용자) | API `CFM-01~04` | - | ✅ |
| N6 | 렌더 · 규격 검증 · S3 업로드 | `run_render` | `cpu` | ✅ Pillow 축소판 |

### BE ↔ AI 경계

- **BE 담당**: API, Job 상태 전이(`job.status`, `current_step`), DB 스키마, S3 업로드, 큐 라우팅
- **AI 담당**: 초기 분석(①②③), 섹션 판정·정책 적용, 라벨·로고, 인페인팅·스타일·번역의 단계 로직. 기존 단독 구현과 BE 워커 연결은 별개입니다.
- **약속**
  - 태스크 이름과 큐 라우팅은 [`app/celery_app.py`](app/celery_app.py)를 기준으로 합니다. 바꿀 때는 BE와 먼저 합의합니다.
  - 태스크는 결과를 DB 행(`section`, `text_block`, `job_async_task`)과 S3 오브젝트 키로 남깁니다. DB에는 URL이 아니라 **S3 키만** 저장합니다.
  - 스키마 변경은 Alembic 마이그레이션(`migrations/versions/`)으로만 합니다.

### BE 통합 합의 — 2026-10-06, 원칙 확정·미구현

출처는 [통합 결정 기록](docs/ai/integration-decisions.md) D1~D5입니다. 이번 문서 갱신은 코드·마이그레이션·OpenAPI 변경이나 운영 적용 완료를 뜻하지 않습니다.

- **N3 준비**: 현재 유효한 `analyze()`(①②③)와 대응하는 섹션 판정·정책 결과가 준비된 뒤 섹션 확인을 엽니다. 초기 분석 실행은 proceed의 N4 실행과 구분합니다.
- **N4 진입**: 사용자 경계는 기존 proceed 한 번입니다. 포함 섹션이 없으면 거절하며, 해당 job 행 잠금 아래 중복 확인·대표 행/최초 작업 생성·N4 전환을 원자적으로 처리합니다. 새 화면 단계·엔드포인트를 추가하기로 합의한 것은 아닙니다.
- **N4 실행**: ④→⑤ 완료 후 유효한 대상에 ⑥⑦⑧을 함께 생성합니다. proceed 전 인페인트는 금지입니다. 현재 `run_analyze` 끝의 조기 큐 등록은 수정 예정입니다.
- **N5 진입**: 현재 실행의 필수 인페인트·스타일·번역이 모두 성공 또는 정상 생략 완료여야 합니다. 실패 시 N4·진행 중 상태를 유지하고 개별 실패를 표시합니다. 현재 번역 스텁 단독 완료 전이와 구분합니다.
- **BE 인계 책임**: 절대 로컬 경로 준비, 결과 검증·S3 업로드·DB 저장, 임시 키→DB ID, DB 적재본의 고정 사전 묶음 공급과 원본 사전 ID→DB PK 연결을 맡습니다. 로컬 경로는 다른 실행 환경의 공유 주소가 아니며 DB 이미지 참조에는 S3 키를 남깁니다.
- **실행 유효성**: 대표 실행·현재 시도로 취소와 오래된 결과를 차단하고 재시도는 새 개별 행으로 기록합니다. revision은 번역문·배치용으로 유지합니다. task_type·연결 컬럼·생략 표현·복구 정책은 구현 전 명세입니다.
- **미결**: PM 보류 정책, 실제 판정 저장·API 매핑, 하류 산출·렌더·신호·상품별 검증 정보는 [미결 목록](docs/ai/open-questions.md)에 남습니다. N5 검수와 N6 렌더를 연결할 상세 계약은 후속 안건입니다.

## 디렉터리

| 경로 | 담당 | 내용 |
| --- | --- | --- |
| [`app/`](app/) | BE | FastAPI 서버, 라우터, Celery 태스크, 렌더 엔진, S3 유틸 |
| [`migrations/`](migrations/) | BE | Alembic 마이그레이션 (스키마의 정본) |
| [`db/`](db/) | BE | 스키마 참고 스냅샷(`schema.sql`), 마스터 시드(`seed.sql`) |
| [`docs/`](docs/) | BE | OpenAPI 명세, Swagger UI — [docs/README.md](docs/README.md) |
| [`docs/ai/`](docs/ai/) | AI | AI 파이프라인 정본 문서 — [docs/ai/README.md](docs/ai/README.md) |
| [`gpu/`](gpu/) | AI | 학교 GPU 서버(KubeSphere · V100) 컨테이너, k8s 매니페스트 — [gpu/README.md](gpu/README.md) |
| [`pipeline/`](pipeline/) | AI | AI 파이프라인 코드 — 단계 간 타입, 단계별 실행 CLI, 샘플. 구조·실행법은 [docs/ai/dev.md](docs/ai/dev.md), 현황은 [docs/ai/status.md](docs/ai/status.md) |

## 빠른 시작 (BE 로컬)

Python 3.12, PostgreSQL, Redis가 필요합니다.

```bash
pip install -r requirements.txt

# 환경변수 (아래 표 참고)
export DATABASE_URL=postgresql+psycopg://pixlate:pixlate_dev_pw@localhost:5432/pixlate
export CELERY_BROKER_URL=redis://localhost:6379/0
export CELERY_RESULT_BACKEND=redis://localhost:6379/1

# DB 스키마 + 시드
alembic upgrade head
psql "postgresql://pixlate:pixlate_dev_pw@localhost:5432/pixlate" -f db/seed.sql

# API 서버 → http://localhost:8000/docs
uvicorn app.main:app --reload

# 워커 (큐별로 따로 실행)
celery -A app.celery_app worker -Q ocr -n ocr@%h
celery -A app.celery_app worker -Q cpu -n cpu@%h
celery -A app.celery_app worker -Q gpu -n gpu@%h
```

### 환경변수

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+psycopg://pixlate:...@pixlate-db:5432/pixlate` | PostgreSQL 접속 정보 |
| `CELERY_BROKER_URL` | `redis://pixlate-redis:6379/0` | Celery 브로커 |
| `CELERY_RESULT_BACKEND` | `redis://pixlate-redis:6379/1` | Celery 결과 저장소 |
| `S3_BUCKET` | `pixlate-storage-2026` | 이미지·산출물 버킷 |
| `AWS_REGION` | `ap-northeast-2` | S3 리전 |
| `S3_PRESIGN_TTL` | `300` | presigned URL 유효 시간(초) |
| `FRONTEND_ORIGINS` | `*` | CORS 허용 오리진 (콤마 구분) |

> S3 인증은 EC2 IAM 역할을 사용합니다. 로컬에서는 AWS 자격 증명이 따로 필요합니다. `.env`와 키는 커밋하지 않습니다.

## 빠른 시작 (AI)

```bash
pip install -r pipeline/requirements.txt
python -m pytest tests/test_pipeline_*.py
python -m pipeline.run --help
```

단계별 실행·샘플·환경 구분은 [docs/ai/dev.md](docs/ai/dev.md), GPU 컨테이너 빌드·배포·SSH 접속은 [gpu/README.md](gpu/README.md)를 참고하세요.

## 현재 상태

- [x] API 46개 (9월 MVP) — 대부분 실제 DB 연결 완료
- [x] Redis + Celery 비동기 뼈대, 큐 3분할(`ocr` / `cpu` / `gpu`)
- [x] S3 업로드 (원본 이미지 · 브랜드 로고 · 렌더 결과)
- [x] 렌더 엔진 축소판 (Pillow)
- [ ] OCR 모델 연결
- [ ] 번역(LLM) 연결
- [ ] 인페인팅(LaMa) 모델을 GPU 서버에 연결
- [ ] 렌더 엔진을 Playwright(HTML/CSS 조판)로 교체

## 참고 문서

- 요구사항정의서 · API 설계 Inventory v3.4.2 · ERD — TODO: 노션 링크
