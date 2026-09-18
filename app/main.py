"""Pixlate API — FastAPI mock 서버.

규격서(pixlate_db_docs v3.4.2)의 🟢9월 MVP 46개 오퍼레이션을 mock 응답으로 구현.
Swagger UI: /docs · ReDoc: /redoc · OpenAPI JSON: /openapi.json
경로 접두어는 servers(/v1)에만 붙인다(규격서 03_API_Inventory).
"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import (
    auth,
    brands,
    cfm,
    finalize,
    jobs,
    library,
    master,
    sections,
    source_images,
)

API_PREFIX = "/v1"

tags_metadata = [
    {"name": "Auth", "description": "인증/셀러 — 로그인·토큰·동의 (AUTH-01~05)"},
    {"name": "Brands", "description": "브랜드/로고 (BRD-01~07)"},
    {"name": "Master", "description": "마스터 데이터 — 국가·언어·규제분류·카테고리·규격 (MST-01~07)"},
    {"name": "Jobs", "description": "작업 생성·조회·취소·큐·재시도·중단·분석 (JOB-02~07, ANL-01)"},
    {"name": "SourceImages", "description": "원본 이미지 업로드·목록·재정렬·삭제 (SRC-01~04)"},
    {"name": "Sections", "description": "섹션 목록·상세·제외·진행 (SEC-01~04)"},
    {"name": "Inpaint", "description": "인페인팅 결과 조회 (INP-01)"},
    {"name": "Review", "description": "검수 — 텍스트 블록·수정·프리뷰·확정 (CFM-01~04)"},
    {"name": "Finalize", "description": "최종 산출물 — 렌더·검증·묶음·다운로드·저장 (FIN-01~06)"},
    {"name": "Library", "description": "보관함 카드 목록 (LIB-01)"},
]

app = FastAPI(
    title="Pixlate API (mock)",
    version="3.4.2-mock",
    description=(
        "AI 상세페이지 로컬라이제이션 서비스 Pixlate의 백엔드 API — **mock 서버**.\n\n"
        "규격서 v3.4.2의 🟢9월 MVP 46개 엔드포인트를 예시 응답으로 제공한다. "
        "FE 연동 배선 테스트용이며 실제 DB·비즈니스 로직은 없다."
    ),
    openapi_tags=tags_metadata,
)

# CORS — FE(다른 오리진)에서 호출 허용.
# 환경변수 FRONTEND_ORIGINS(콤마 구분)로 제한 가능. 기본은 전체 허용(테스트용).
_origins = os.getenv("FRONTEND_ORIGINS", "*").split(",")
_origins = [o.strip() for o in _origins if o.strip()]
_allow_credentials = _origins != ["*"]  # 쿠키(refresh) 사용 시엔 특정 오리진 지정 필요
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", tags=["Health"], summary="루트 — 서버 동작 확인")
def root():
    return {"service": "pixlate-api", "status": "running", "docs": "/docs", "apiPrefix": API_PREFIX}


@app.get("/health", tags=["Health"], summary="헬스 체크")
def health():
    return {"status": "ok"}


# 도메인별 라우터 등록 (모두 /v1 접두어)
for module in (auth, brands, master, jobs, source_images, sections, cfm, finalize, library):
    app.include_router(module.router, prefix=API_PREFIX)
