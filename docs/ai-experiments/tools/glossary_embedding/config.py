"""임베딩 테스트 설정값 — 여기 값만 바꾸면 전체 스크립트/노트북에 반영된다.

환경변수로도 덮어쓸 수 있다 (예: PowerShell  $env:QDRANT_MODE="memory").
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── 임베딩 모델 (고정) ─────────────────────────────────────────
MODEL_NAME = "BAAI/bge-m3"
EMBED_DIM = 1024                 # BGE-M3 dense 차원
NORMALIZE = True                 # normalize_embeddings=True → 내적 = 코사인
BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "16"))
MAX_SEQ_LENGTH = 512             # 용어·OCR 블록은 짧다. 8192까지 가능하지만 느려짐
DEVICE = os.getenv("EMBED_DEVICE") or None   # None이면 cuda → mps → cpu 자동 선택

# ── Qdrant 저장 방식 (이 값 하나로 전환) ──────────────────────
#   "local"  : QdrantClient(path=QDRANT_PATH) — 디스크에 저장, 재실행해도 인덱스 유지
#   "memory" : QdrantClient(":memory:")      — 프로세스가 끝나면 사라짐, 빠른 실험용
QDRANT_MODE = os.getenv("QDRANT_MODE", "local")
# 인덱스는 홈 폴더 아래에 둔다(작업 폴더가 동기화 폴더여도 잠금 충돌이 없게)
# QDRANT_PATH = 2026-09-28 이전 실험 인덱스(glossary_bge_m3) 폴더 — 보존용
QDRANT_PATH = Path(os.getenv("QDRANT_PATH", Path.home() / ".pixate" / "qdrant_storage"))
# 새 컬렉션은 컬렉션마다 폴더를 따로 둔다. 로컬 모드는 폴더를 열 때 그 안의 모든 컬렉션을 RAM에 올리므로
# (2만 건 컬렉션 2개 = 약 4만 점) 한 폴더에 여러 개를 두면 RAM 8GB PC에서 MemoryError 가 났다(2026-09-30).
QDRANT_ROOT = Path(os.getenv("QDRANT_ROOT", Path.home() / ".pixate" / "qdrant"))


def qdrant_path(name: str | None = None) -> Path:
    name = name or collection()
    return QDRANT_PATH if name == LEGACY_COLLECTION else QDRANT_ROOT / name

# 2026-09-28 이전 실험 인덱스(전달본 pre-0928 · ko). 보존만 하고 새 실험은 collection()을 쓴다.
LEGACY_COLLECTION = "glossary_bge_m3"
COLLECTION = LEGACY_COLLECTION  # sample 소스·구 스크립트 호환용
# 인덱스 매니페스트(전달본 파일 해시 · 모델 설정 · 행별 내용 해시). 오래된 벡터 재사용을 막는다.
MANIFEST_PATH = Path(os.getenv("PIXATE_MANIFEST", Path.home() / ".pixate" / "index_manifest.json"))
# 임베딩 캐시 — (모델 · 정규화 · 최대 길이 · 텍스트)가 같으면 벡터를 다시 계산하지 않는다
EMBED_CACHE_PATH = Path(os.getenv("PIXATE_EMBED_CACHE", Path.home() / ".pixate" / "embed_cache.sqlite"))

# ── 무엇을 임베딩할지 ─────────────────────────────────────────
#   "ko"    : 한국어 용어만 (term_ko)
#   "ko_en" : "한국어 용어 | 영어 용어" — 영어 쪽 의미까지 벡터에 섞음
EMBED_TEXT_MODE = os.getenv("EMBED_TEXT_MODE", "ko")


def collection(delivery: str | None = None, text_mode: str | None = None) -> str:
    """전달본(또는 DB 스냅샷) · 임베딩 방식마다 컬렉션을 따로 둔다 (서로 덮어쓰지 않게)."""
    if delivery is None and GLOSSARY_SOURCE == "db_snapshot":
        delivery = DB_SNAPSHOT.replace("glossary_", "")  # 예: db_20261001
    return f"glossary__{delivery or GLOSSARY_DELIVERY}__{text_mode or EMBED_TEXT_MODE}"

# ── 검색 기본값 ───────────────────────────────────────────────
TOP_K = 5
TARGET_LANG = "en"               # MVP 도착어 = 영어 단일
# 이 점수 미만이면 "용어집에 해당 없음"으로 본다. 잠정값 — 05_experiments.py eval 의 임계값표(음성 27문항)로 확정하지 않는다.
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "0.55"))

# ── 데이터 ────────────────────────────────────────────────────
#   "team"   : data/deliveries/<전달본>/ 의 데이터팀 용어집 3종 (적재 규칙: 문구 적재여부=Y · 성분·인증 구역=A) 20,653행
#   "sample" : data/glossary_sample.csv 58행 (빠른 실험용 — 전체 용어집 성능으로 인용하지 않는다)
#   "db_snapshot" : 운영 RDS glossary 를 EC2에서 내보낸 JSONL(내부 PK id 포함) — data/db_snapshot/
GLOSSARY_SOURCE = os.getenv("GLOSSARY_SOURCE", "db_snapshot")  # 2026-10-06: 운영 DB 스냅샷을 기본으로
# DB 스냅샷 파일 접두어(glossary_db_<날짜>.jsonl + .meta.json). 2026-10-01 내보냄 — plan 변경 없음 20653 · PK 지문 일치
DB_SNAPSHOT = os.getenv("DB_SNAPSHOT", "glossary_db_20261001")
DB_SNAPSHOT_DIR = Path(__file__).resolve().parent / "data" / "db_snapshot"
# 전달본 폴더. 2026-09-28 = 서버 로더 테스트(test_real_delivery_2026_09_28)가 기준으로 삼는 확정본.
# pre-0928 = 그 이전 실험에 쓴 파일(문구 3행 구분자·version만 다름). SHA256SUMS 가 폴더마다 있다.
GLOSSARY_DELIVERY = os.getenv("GLOSSARY_DELIVERY", "2026-09-28")
DELIVERIES_DIR = ROOT / "data" / "deliveries"
TEAM_GLOSSARY_FILES = {
    "certification": "glossary_certification.xlsx",
    "phrase": "glossary_phrase.xlsx",
    "ingredient": "glossary_ingredient.xlsx",
}
GLOSSARY_CSV = ROOT / "data" / "glossary_sample.csv"       # sample 소스용 로컬 파일(저장소에 없음)

# 내부 카테고리 7종 + 공용 폴백 — ERD·DB문서의 **문서상 허용 목록**(PRD F-SRC-04b).
# 서버 마이그레이션 0005는 enforcement CHECK만 추가했고 카테고리 CHECK는 DB에 없다(deploy/glossary-ingestion.md 4절).
INTERNAL_CATEGORIES = [
    "Body", "Eyes", "Face", "Lip Care", "Maternity",
    "Sets, Kits", "Sunscreens & Tanning Products", "common",
]
