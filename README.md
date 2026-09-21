# Pixlate Server

AI 상세페이지 로컬라이제이션 서비스 **Pixlate**의 백엔드(BE)와 AI 파이프라인을 함께 관리하는 레포지터리입니다.
한국어 상세페이지 이미지를 받아 OCR → 번역 → 인페인팅 → 렌더를 거쳐 타겟 국가용 이미지를 만듭니다.

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
| N3 | 섹션 검수 (사용자) | API `SEC-01~04` | - | ✅ |
| N4 | 번역 | `run_translate` | `cpu` | 🚧 스텁 (LLM 예정) |
| N4 | 인페인팅 (원문 텍스트 제거) | `run_inpaint` | `gpu` | 🚧 스텁 (LaMa 예정) |
| N5 | 번역 검수 (사용자) | API `CFM-01~04` | - | ✅ |
| N6 | 렌더 · 규격 검증 · S3 업로드 | `run_render` | `cpu` | ✅ Pillow 축소판 |

### BE ↔ AI 경계

- **BE 담당**: API, Job 상태 전이(`job.status`, `current_step`), DB 스키마, S3 업로드, 큐 라우팅
- **AI 담당**: 각 태스크 안의 모델 추론(OCR · 번역 · 인페인팅)
- **약속**
  - 태스크 이름과 큐 라우팅은 [`app/celery_app.py`](app/celery_app.py)를 기준으로 합니다. 바꿀 때는 BE와 먼저 합의합니다.
  - 태스크는 결과를 DB 행(`section`, `text_block`, `job_async_task`)과 S3 오브젝트 키로 남깁니다. DB에는 URL이 아니라 **S3 키만** 저장합니다.
  - 스키마 변경은 Alembic 마이그레이션(`migrations/versions/`)으로만 합니다.

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
