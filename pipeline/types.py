"""단계 간 입출력 타입 — 초기 분석(①~③, analyze()) 범위.

근거: contract.md 1.1(출력 단위) · 2장(블록 필드) · 2.5(source_lines) · 3.1(좌표계) · 8장(경고).
규칙:
- 좌표는 정수 픽셀. 섹션 안의 모든 좌표는 **섹션 로컬**이며 원본 기준 세로 = top_offset + y.
- section_key · block_key · line_key · region_key는 임시 식별자다. section_key · block_key는 한 번의 실행 안에서,
  region_key · line_key는 섹션 안에서 유일하다(섹션 밖에서 참조하면 section_key와 쌍으로) — dev.md 3절.
  DB id 변환은 워커가 한다(db-map.md 2절). 이 파일은 DB 컬럼을 모른다.
- 파일은 워커가 준비한 로컬 경로로만 주고받는다. 함수는 S3를 모른다(contract.md 1.1).
- 계약이 미정으로 둔 값(섹션 range #13, style #14 등)은 여기에 두지 않는다. 결정되면 필드를 추가한다.
- 후속 단계(④ 라벨 · ⑤ 로고 · ⑥⑦⑧)의 타입은 그 단계에 착수할 때 이 파일에 덧붙인다.
- ③-1 · ③-1'(파일 끝 절)은 **개발용 잠정 타입**이다(2026-09-28 착수, docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md ·
  open-questions.md #60). `ContentFinding`의 6키만 계약(4.1)이고, `matches` · `finding_status` · `conflict_group` · `dict_coverage` ·
  `source_verdict_status` · `bucket_recommendation` · `applied` 등은 BE 합의 전 필드다. BE가 확정한 저장 · 인계 계약으로 읽지 않는다.
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


# 임시 식별자 규칙 — 유일 범위는 위 모듈 설명(dev.md 3절)
def section_key(source_image_id: int, section_order: int) -> str:
    return f"sec_{source_image_id}_{section_order:02d}"


def region_key(n: int) -> str:
    return f"reg_{n:04d}"


def line_key(n: int) -> str:
    return f"line_{n:03d}"


def block_key(n: int) -> str:
    return f"blk_{n:03d}"


# ---------------------------------------------------------------------------
# ③-1 AI 섹션 판정 · ③-1' 정책 적용 — 개발용 잠정 타입 (BE 인계 · 저장 계약 아님)
# 근거: docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md(설계 v1, 조건부 · 잠정 승인 2026-09-28) · open-questions.md #60.
# 계약 근거가 있는 것은 [계약 n]으로, 없는 것은 [잠정 · BE 합의]로 표시한다.
# ---------------------------------------------------------------------------
FindingStatus = Literal["present", "absent", "uncertain"]  # [계약 4.1]
EvidenceSource = Literal["text", "image", "image_and_text"]  # [계약 4.1] 실제 근거 출처(동봉한 입력 종류가 아님)
JudgeStatus = Literal["ok", "skipped", "failed"]  # [잠정] skipped = 호출 대상 아님(call_scope=matched), failed = 2.6절
PolicyStatus = Literal["ok", "incomplete", "input_error"]  # [잠정] 3.4절 — ok가 아니면 권고 · verdict 없음
Bucket = Literal["include", "exclude"]
RegulatoryClassInput = Literal["cosmetic", "otc", "combination", "unknown"]  # API job.regulatory_class enum(docs/openapi.yaml)
DictCoverage = Literal["selected_class_all_entries", "partial_class_combination", "unverified_class"]  # [잠정 · BE 합의] D9


class ContentFinding(_Model):
    """[계약 4.1] content_findings.findings[] 한 항목. content_type 값 목록은 미정(#4, D6 잠정 = 사전 항목 ID)."""

    finding_key: str
    content_type: str
    status: FindingStatus
    evidence_block_ids: list[str] = Field(default_factory=list)  # 임시 block_key. 저장 시 text_block.id(db-map.md 2절)
    evidence_source: EvidenceSource
    reason: str

    @field_validator("evidence_block_ids")
    @classmethod
    def _unique_blocks(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("evidence_block_ids에 중복이 있다")
        return v


class ContentFindings(_Model):
    """[계약 4.1] section.content_findings JSON. findings=[]는 검사 완료 · 해당 없음, None(NULL)은 판정 미완료(#45)."""

    schema_version: str = SCHEMA_VERSION
    findings: list[ContentFinding] = Field(default_factory=list)


class Span(_Model):
    """원문 source_ko 안의 문자 구간 [start, end). 정규화 전 좌표."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: int, info) -> int:
        if "start" in info.data and v <= info.data["start"]:
            raise ValueError("end는 start보다 커야 한다")
        return v


class Match(_Model):
    """[잠정 · BE 합의 D13] 규칙 매칭 근거 — finding과 별개로 보존하며 ③-1'의 정상 입력(problem_text · 겹침 처리)."""

    match_key: str
    finding_key: str
    dictionary_ref: str  # 사전 항목 ID(RG-xxx · LC-xx). DB id 변환은 워커
    pattern: str
    block_key: str
    raw_span: Span
    matched_text: str = Field(min_length=1)


class JudgeChecked(_Model):
    """[잠정] 검사 범위 기록 — 누락(검사 범위 밖)과 absent(검사해 해당 없음)를 구분한다(설계 2.5절). event_log.payload 재료."""

    dictionary_version: dict[str, str]
    dictionary_fingerprint: dict[str, str]  # 파일 이름 → SHA-256
    match_rules_version: str
    items: list[str]  # 검사한 사전 항목 ID
    llm_called: bool


class JudgeResult(_Model):
    """[잠정] ③-1 출력 = 계약 구조(content_findings) + 근거 객체(matches) + 검사 범위. failed면 content_findings=None(계약 4.1 NULL)."""

    schema_version: str = SCHEMA_VERSION
    section_key: str
    status: JudgeStatus
    content_findings: ContentFindings | None
    matches: list[Match] = Field(default_factory=list)
    checked: JudgeChecked
    error: str | None = None

    @field_validator("content_findings")
    @classmethod
    def _status_consistency(cls, v: ContentFindings | None, info) -> ContentFindings | None:
        status = info.data.get("status")
        if status == "failed" and v is not None:
            raise ValueError("failed면 content_findings는 None(판정 미완료)이어야 한다")
        if status in ("ok", "skipped") and v is None:
            raise ValueError(f"{status}면 content_findings가 있어야 한다(빈 목록 허용)")
        return v


class JudgeContext(_Model):
    """[잠정 · BE 합의 #47] ③-1 · ③-1' 공통 입력 중 직렬화 가능한 것. 사전 핸들(dictionary.Dictionaries)은 별도 인자."""

    regulatory_class: RegulatoryClassInput | None = None  # None = 누락(D9-c input_error). "unknown"과 구분
    prev_section_text: str | None = None  # 같은 원본 안 앞 섹션 블록 텍스트(참고용, 근거 지정 불가) D5
    next_section_text: str | None = None


class VerdictDraft(_Model):
    """[계약 4.2 + 잠정] section_verdict 후보. verdict_type · exclusion_reason은 BE 파생이라 없다(설계 3.2절)."""

    verdict_key: str
    finding_key: str
    dictionary_ref: str
    verdict_status: str  # 허용값 · 매핑은 정책 계약 소유(#7). 여기서는 문자열
    source_verdict_status: str  # [잠정 · BE 합의 D11] 사전 원값(rewritable 보존)
    finding_status: Literal["present", "uncertain"]  # [잠정 · BE 합의 D12] 확신 상태. reason에 섞지 않는다
    problem_text: str | None  # 규제 = 매칭 원문, 현지부적합 = 근거 블록 텍스트, 이미지 전용 근거 = None
    alternative_expression: list[str] = Field(default_factory=list)
    basis_article: str | None = None
    evidence_url: str | None = None
    reason: str  # 판정 시점 사전 사유의 스냅샷만(AI 서술 · 확신 표시 금지)
    conflict_group: str | None = None  # [잠정 · BE 합의 D2-b] 규칙 충돌에 따른 잠정 제외 표시(ConflictGroup.group_key)


class ConflictGroup(_Model):
    """[잠정 · BE 합의 D2-b] 미확정 겹침 집합 — 관련 사전 ID와 매칭 근거를 보존해 사용자 확인 화면까지 전달한다."""

    group_key: str
    dictionary_refs: list[str] = Field(min_length=2)
    match_keys: list[str] = Field(min_length=2)
    block_key: str


class SuppressedMatch(_Model):
    """[잠정] 예외 쌍(overrides)으로 억제된 매칭 하나. finding은 지우지 않는다."""

    match_key: str
    finding_key: str
    suppressed_by: str  # 억제한 매칭의 match_key


class PolicyApplied(_Model):
    """[잠정 · BE 합의 D9] 적용 정보 — 원래 선택값 · 실제 적용 분류 · 검사 범위(현재 사전 항목 기준). audit_log.detail · event_log.payload 재료."""

    regulatory_class: RegulatoryClassInput
    applied_classes: list[str]
    dict_coverage: DictCoverage
    dictionary_version: dict[str, str]
    dictionary_fingerprint: dict[str, str]
    rules_version: str


class PolicyResult(_Model):
    """[잠정] ③-1' 출력. status가 ok가 아니면 bucket_recommendation=None · verdicts=[](설계 3.4절 — 미완료 입력에서 포함 권고를 만들지 않는다)."""

    schema_version: str = SCHEMA_VERSION
    section_key: str
    status: PolicyStatus
    bucket_recommendation: Bucket | None
    verdicts: list[VerdictDraft] = Field(default_factory=list)
    suppressed: list[SuppressedMatch] = Field(default_factory=list)
    conflicts: list[ConflictGroup] = Field(default_factory=list)
    applied: PolicyApplied | None
    error: str | None = None

    @field_validator("bucket_recommendation")
    @classmethod
    def _no_recommendation_unless_ok(cls, v: Bucket | None, info) -> Bucket | None:
        if info.data.get("status") != "ok" and v is not None:
            raise ValueError("status가 ok가 아니면 bucket_recommendation은 None이어야 한다")
        if info.data.get("status") == "ok" and v is None:
            raise ValueError("status가 ok면 bucket_recommendation이 있어야 한다")
        return v

    @field_validator("verdicts")
    @classmethod
    def _no_verdicts_unless_ok(cls, v: list[VerdictDraft], info) -> list[VerdictDraft]:
        if info.data.get("status") != "ok" and v:
            raise ValueError("status가 ok가 아니면 verdicts는 비어 있어야 한다")
        return v


def finding_key(n: int) -> str:
    return f"f_{n:02d}"


def match_key(n: int) -> str:
    return f"m_{n:03d}"


def verdict_key(n: int) -> str:
    return f"v_{n:02d}"
