"""단계 간 입출력 타입 — 초기 분석(①~③, analyze()) 범위.

근거: contract.md 1.1(출력 단위) · 2장(블록 필드) · 2.5(source_lines) · 3.1(좌표계) · 8장(경고).
규칙:
- 좌표는 정수 픽셀. 섹션 안의 모든 좌표는 **섹션 로컬**이며 원본 기준 세로 = top_offset + y.
- section_key · block_key · line_key · region_key는 한 번의 실행 안에서만 유일한 임시 식별자다.
  DB id 변환은 워커가 한다(db-map.md 2절). 이 파일은 DB 컬럼을 모른다.
- 파일은 워커가 준비한 로컬 경로로만 주고받는다. 함수는 S3를 모른다(contract.md 1.1).
- 계약이 미정으로 둔 값(섹션 range #13, style #14 등)은 여기에 두지 않는다. 결정되면 필드를 추가한다.
- 후속 단계(③-1 판정 · ④ 라벨 · ⑤ 로고 · ⑥⑦⑧)의 타입은 그 단계에 착수할 때 이 파일에 덧붙인다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1"  # 공통 결과 버전(AnalyzeResult · OcrResult · MergeResult). 워커 인계 형식은 이 값을 따른다
SPLIT_SCHEMA_VERSION = "2"  # SplitResult(① 출력 타입이자 split.json 파일)만: 상대 image_path = split.json 폴더 기준. 읽기 허용 버전은 jsonio.READ_VERSIONS

Role = Literal["title", "body", "caption", "price", "caution"]
ROLES: tuple[str, ...] = ("title", "body", "caption", "price", "caution")


class _Model(BaseModel):
    """공통 설정: 모르는 키는 거부한다(계약 밖 키가 조용히 섞이지 않게)."""

    model_config = ConfigDict(extra="forbid")


class BBox(_Model):
    """축 정렬 사각형 {x, y, w, h}. 좌상단 원점, 픽셀 정수."""

    x: int
    y: int
    w: int = Field(ge=0)
    h: int = Field(ge=0)

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @classmethod
    def from_poly(cls, poly: list[tuple[int, int]]) -> "BBox":
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        return cls(x=min(xs), y=min(ys), w=max(xs) - min(xs), h=max(ys) - min(ys))

    @classmethod
    def union(cls, boxes: list["BBox"]) -> "BBox":
        if not boxes:
            raise ValueError("빈 목록의 union은 정의되지 않는다")
        x1 = min(b.x for b in boxes)
        y1 = min(b.y for b in boxes)
        x2 = max(b.x2 for b in boxes)
        y2 = max(b.y2 for b in boxes)
        return cls(x=x1, y=y1, w=x2 - x1, h=y2 - y1)


# ---------------------------------------------------------------------------
# ① 섹션 분해
# ---------------------------------------------------------------------------
class SourceImage(_Model):
    """①의 입력 한 장. 워커가 S3에서 내려받아 로컬 경로를 준다."""

    source_image_id: int
    upload_order: int  # 원본 간 순서 = 업로드 순서
    path: str  # 로컬 파일 경로


class Section(_Model):
    """①의 출력 단위. 원본 전체 폭을 쓰는 세로 구간 [계약 3.1]."""

    section_key: str
    source_image_id: int
    section_order: int  # 원본 내 순서 (1부터)
    top_offset: int = Field(ge=0)  # 원본 기준 세로 시작
    height: int = Field(gt=0)  # 원본 기준 높이
    width: int = Field(gt=0)  # = 원본 폭 (MVP)
    image_path: str  # 잘라낸 섹션 이미지 로컬 경로. ②·⑥·⑦·③-1·④가 그대로 읽는다


class SplitResult(_Model):
    """① 출력: 원본 한 장 → 섹션 목록(section_order 순). 파일 버전은 SPLIT_SCHEMA_VERSION(경로 규칙, dev.md 3절) — 워커 인계 형식(AnalyzeResult)에는 들어가지 않는다."""

    schema_version: str = SPLIT_SCHEMA_VERSION
    source_image_id: int
    source_width: int = Field(gt=0)
    source_height: int = Field(gt=0)
    sections: list[Section]


# ---------------------------------------------------------------------------
# ② 텍스트 추출 (OCR)
# ---------------------------------------------------------------------------
class OcrRegion(_Model):
    """원시 OCR 영역 하나 [계약 2.5]. 섹션 로컬 좌표. 임시 분할 타일 좌표는 여기 오기 전에 복원한다."""

    region_key: str
    text: str
    score: float = Field(ge=0.0, le=1.0)
    poly: list[tuple[int, int]] = Field(min_length=3)
    bbox: BBox

    @field_validator("poly", mode="before")
    @classmethod
    def _poly_points(cls, v):
        # JSON은 [[x,y],...]로 오간다. 튜플로 정규화.
        return [tuple(p) for p in v]


class OcrResult(_Model):
    """② 출력: 섹션 하나의 영역 목록(읽기 순서). 텍스트 0개면 regions=[] (오류 아님)."""

    schema_version: str = SCHEMA_VERSION
    section_key: str
    regions: list[OcrRegion]


# ---------------------------------------------------------------------------
# ③ 줄·문단 병합 + 역할 분류
# ---------------------------------------------------------------------------
class Line(_Model):
    """블록 안의 한 줄 [계약 2.5]. 영역을 잃지 않고 묶는다."""

    line_key: str
    text: str
    bbox: BBox
    regions: list[OcrRegion] = Field(min_length=1)


class TextBlock(_Model):
    """③ 출력 단위 = 문단 1개 [계약 2장]. 초기 분석 시 style·판정 플래그는 없다(NULL)."""

    block_key: str
    section_key: str
    block_order: int  # 섹션 내 순서 (1부터)
    source_ko: str
    source_lines: list[Line] = Field(min_length=1)
    bbox: BBox
    role: Role
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class MergeResult(_Model):
    """③ 출력: 섹션 하나의 블록 목록(block_order 순)."""

    schema_version: str = SCHEMA_VERSION
    section_key: str
    blocks: list[TextBlock]


# ---------------------------------------------------------------------------
# analyze() = ① → ② → ③
# ---------------------------------------------------------------------------
class AnalyzeWarning(_Model):
    """오류가 아닌 분석 경고 [계약 8장]. 예: 원본 전체에서 텍스트 0개."""

    code: Literal["NO_TEXT_DETECTED"]
    source_image_id: int
    message: str


class AnalyzeResult(_Model):
    """analyze() 출력. 워커가 section·text_block 행으로 저장한다."""

    schema_version: str = SCHEMA_VERSION
    sections: list[Section]  # upload_order → section_order 순
    blocks: list[TextBlock]  # 섹션 순 → block_order 순
    warnings: list[AnalyzeWarning] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 계산 도우미 (계약이 정한 계산 규칙만)
# ---------------------------------------------------------------------------
def ocr_confidence_of(lines: list[Line]) -> float | None:
    """블록 신뢰도 = 구성 영역 score의 최솟값, 영역이 없으면 None [계약 2장]."""
    scores = [r.score for ln in lines for r in ln.regions]
    return min(scores) if scores else None


def bbox_of_lines(lines: list[Line]) -> BBox:
    return BBox.union([ln.bbox for ln in lines])


# 임시 식별자 규칙 — 실행 안에서 유일하면 된다
def section_key(source_image_id: int, section_order: int) -> str:
    return f"sec_{source_image_id}_{section_order:02d}"


def region_key(n: int) -> str:
    return f"reg_{n:04d}"


def line_key(n: int) -> str:
    return f"line_{n:03d}"


def block_key(n: int) -> str:
    return f"blk_{n:03d}"
