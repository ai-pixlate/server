"""단계 간 입출력 타입 — 초기 분석(①~③, analyze()) 범위.

근거: contract.md 1.1(출력 단위) · 2장(블록 필드) · 2.5(source_lines) · 3.1(좌표계) · 8장(경고).
규칙:
- 좌표는 정수 픽셀. 섹션 안의 모든 좌표는 **섹션 로컬**이며 원본 기준 세로 = top_offset + y.
- section_key · block_key · line_key · region_key는 임시 식별자다. section_key · block_key는 한 번의 실행 안에서,
  region_key · line_key는 섹션 안에서 유일하다(섹션 밖에서 참조하면 section_key와 쌍으로) — dev.md 3절.
  DB id 변환은 워커가 한다(db-map.md 2절). 이 파일은 DB 컬럼을 모른다.
- 파일은 워커가 준비한 로컬 경로로만 주고받는다. 함수는 S3를 모른다(contract.md 1.1).
- 계약이 미정으로 둔 값(섹션 range #13, style #14 등)은 여기에 두지 않는다. 결정되면 필드를 추가한다.
- 후속 단계(⑥⑦⑧)의 타입은 그 단계에 착수할 때 이 파일에 덧붙인다. ④ 라벨(#66) · ⑤ 로고(#68, 파일 끝 절)도 개발용 잠정 타입이다.
- ③-1 · ③-1'(파일 끝 절)은 **개발용 잠정 타입**이다(2026-09-28 착수, docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md ·
  open-questions.md #60). `ContentFinding`의 6키만 계약(4.1)이고, `matches` · `finding_status` · `conflict_group` · `dict_coverage` ·
  `source_verdict_status` · `bucket_recommendation` · `applied` 등은 BE 합의 전 필드다. BE가 확정한 저장 · 인계 계약으로 읽지 않는다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

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
    """[잠정 · BE 합의 D13] 규칙 매칭 근거 — finding과 별개로 보존하며 ③-1'의 정상 입력(problem_text · 겹침 처리).

    raw_span은 원문 `source_ko`의 **문자(코드 포인트) 인덱스 반개구간 [start, end)** 이며 정규화 전 좌표다.
    matched_text = source_ko[start:end] (원문의 공백 · 줄바꿈이 그대로 들어 있을 수 있다).
    finding_key는 ③-1이 finding을 조립할 때 채운다. 검출 전용 결과(DetectionResult)에서는 None이다.
    """

    match_key: str
    finding_key: str | None = None
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
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")  # 판정 당시 블록(block_key · source_ko, 순서)의 SHA-256. ③-1'이 현재 블록과 대조한다


def blocks_fingerprint(blocks: list["TextBlock"]) -> str:
    """③-1 입력 블록의 지문 — block_order 순 (block_key, source_ko)의 JSON SHA-256. 텍스트가 바뀐 블록에 이전 판정을 적용하지 않기 위한 것."""
    import hashlib
    import json

    payload = [[b.block_key, b.source_ko] for b in sorted(blocks, key=lambda b: b.block_order)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


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

    @field_validator("matches")
    @classmethod
    def _matches_bound_to_findings(cls, v: list[Match], info) -> list[Match]:
        cf = info.data.get("content_findings")
        keys = {f.finding_key for f in cf.findings} if cf is not None else set()
        loose = [m.match_key for m in v if m.finding_key is None or m.finding_key not in keys]
        if loose:
            raise ValueError(f"JudgeResult의 매칭은 finding에 묶여야 한다(검출 전용 결과가 아니다): {loose}")
        return v


class DetectionResult(_Model):
    """[잠정] ③-1a **검출 전용** 출력 — 판정 결과가 아니다.

    `content_findings`가 없고 status는 `detect_only`뿐이다. `judge --no-llm`의 출력이며, "규칙만으로 최종 판정"이 아니라
    "후보 검출만 수행"을 뜻한다. `findings=[]` · `status=ok`로 판정 완료처럼 전달하지 않으며, ③-1'(`policy.run`)의 입력이 될 수 없다.
    원료 · 연구원 같은 매칭은 의도된 후보 생성이지 부적합 판정 성공이 아니다.
    """

    schema_version: str = SCHEMA_VERSION
    section_key: str
    status: Literal["detect_only"] = "detect_only"
    matches: list[Match] = Field(default_factory=list)
    candidates: dict[str, list[str]] = Field(default_factory=dict)  # 사전 항목 ID → match_key 목록(매칭된 항목만)
    checked: JudgeChecked  # llm_called는 항상 False

    @field_validator("matches")
    @classmethod
    def _no_finding_keys(cls, v: list[Match]) -> list[Match]:
        bound = [m.match_key for m in v if m.finding_key is not None]
        if bound:
            raise ValueError(f"검출 전용 결과의 매칭은 finding에 묶이지 않는다: {bound}")
        return v

    @field_validator("checked")
    @classmethod
    def _llm_not_called(cls, v: JudgeChecked) -> JudgeChecked:
        if v.llm_called:
            raise ValueError("검출 전용 결과는 LLM을 부르지 않는다")
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


# ---------------------------------------------------------------------------
# ④ 제품 라벨 판정 — 개발용 잠정 타입 (BE 인계 · 저장 계약 아님)
# 근거: 계약 2.2 · 2.4(is_product_label 3상태) · open-questions.md #66(④ 단독 개발 방침, 사용자 승인 2026-09-29).
# 운영 결과 형식 · 실패 계약 · DB 저장 대응은 전체 통합 때 정한다(#64 · #65). AnalyzeResult와 공통 버전은 바꾸지 않는다.
# ---------------------------------------------------------------------------
LabelStatus = Literal["ok", "failed"]  # [잠정] failed = 섹션 전체 판정 실패(labels=None, 미판정). 부분 성공은 없다
LabelBasis = Literal["vlm", "blank_text"]  # [잠정] vlm = VLM 응답 / blank_text = 공백 블록(VLM 대상 제외, false 기록)


class LabelDecision(_Model):
    """[계약 2.2 + 잠정] 블록 하나의 판정. is_product_label은 JSON boolean만 받는다(문자열 · 숫자를 묵시 변환하지 않음)."""

    block_key: str
    is_product_label: bool = Field(strict=True)
    basis: LabelBasis


class LabelChecked(_Model):
    """[잠정] 검사 범위 기록 — 어떤 블록을 VLM에 보냈고 어떤 블록을 공백 규칙으로 처리했는지. event_log.payload 재료."""

    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")  # 판정 당시 블록 전체(원문 · 좌표 · 역할 · 원시 OCR)의 SHA-256
    llm_called: bool
    sent_block_keys: list[str] = Field(default_factory=list)
    blank_block_keys: list[str] = Field(default_factory=list)


class LabelResult(_Model):
    """[잠정] ④ 출력 — 분석 결과(MergeResult)와 분리한 블록별 is_product_label. 원문 · 좌표 · 역할 · 블록 구성은 담지 않는다(바꾸지 않는다).

    ok면 labels가 섹션의 모든 블록(block_order 순)에 하나씩 있다(블록이 없으면 빈 목록). failed면 labels=None — 미판정이며
    false · 빈 성공 결과로 바꾸지 않는다.
    """

    schema_version: str = SCHEMA_VERSION
    section_key: str
    status: LabelStatus
    labels: list[LabelDecision] | None
    checked: LabelChecked
    error: str | None = None

    @field_validator("labels")
    @classmethod
    def _labels_by_status(cls, v: list[LabelDecision] | None, info) -> list[LabelDecision] | None:
        status = info.data.get("status")
        if status == "failed" and v is not None:
            raise ValueError("failed면 labels는 None(미판정)이어야 한다 — 부분 성공 · false 대체 없음")
        if status == "ok" and v is None:
            raise ValueError("ok면 labels가 있어야 한다(블록이 없으면 빈 목록)")
        if v is not None and len({d.block_key for d in v}) != len(v):
            raise ValueError("labels에 같은 block_key가 두 번 있다")
        return v

    @model_validator(mode="after")
    def _error_when_failed(self) -> "LabelResult":
        if self.status == "failed" and not self.error:
            raise ValueError("failed면 error가 있어야 한다")
        return self


# ---------------------------------------------------------------------------
# ⑤ 브랜드 로고 제외 — 개발용 잠정 타입 (BE 인계 · 저장 계약 아님)
# 근거: 계약 2.3(block_exact) · 2.4(is_brand_logo 3상태) · open-questions.md #68(⑤ 단독 개발 방침, 사용자 승인 2026-09-29).
# basis · error는 개발용 기록 값이며 운영 enum · 오류 코드가 아니다. is_excluded는 여기서 계산하지 않는다(DB 자동계산, #63).
# AnalyzeResult와 공통 SCHEMA_VERSION은 바꾸지 않는다 — 이 결과는 전용 버전 LOGO_SCHEMA_VERSION을 쓴다.
# ---------------------------------------------------------------------------
LOGO_SCHEMA_VERSION = "1"  # [잠정 #68] ⑤ 개발용 결과(LogoResult) 전용 버전
LogoStatus = Literal["ok", "failed"]  # [잠정 #68] failed = 판정 중 예기치 않은 오류로 섹션 판정 없음. 부분 성공은 없다
# [잠정 #68] product_label = ④ true라 비교 생략(null) / empty_text = 정규화 결과가 빈 문자열(false) /
# exact_match = 한글명 또는 영문명과 완전 일치(true) / no_match = 정상 불일치(false, 경고 아님)
LogoBasis = Literal["product_label", "empty_text", "exact_match", "no_match"]
_LOGO_VALUE_BY_BASIS: dict[str, bool | None] = {"product_label": None, "empty_text": False, "exact_match": True, "no_match": False}


class LogoDecision(_Model):
    """[계약 2.3 + 잠정 #68] 블록 하나의 로고 판정. is_brand_logo는 JSON boolean 또는 null(④ 라벨이라 비교 생략)만 받는다."""

    block_key: str
    is_brand_logo: StrictBool | None  # 필수 키(기본값 없음) — 누락을 null로 채우지 않는다
    basis: LogoBasis

    @model_validator(mode="after")
    def _value_matches_basis(self) -> "LogoDecision":
        expected = _LOGO_VALUE_BY_BASIS[self.basis]
        if self.is_brand_logo is not expected:
            raise ValueError(f"basis {self.basis}이면 is_brand_logo는 {expected}여야 한다(받은 값 {self.is_brand_logo!r})")
        return self


class LogoResult(_Model):
    """[잠정 #68] ⑤ 출력 — 섹션 하나의 블록별 is_brand_logo. 원문 · 좌표 · 역할 · 원시 OCR · ④ 결과는 담지 않는다(바꾸지 않는다).

    ok면 decisions가 섹션의 모든 블록에 정확히 하나씩(block_order 순, 동률은 block_key 순 — 순서와 누락 여부는 판정 단계가 보장),
    블록이 없으면 빈 목록이고 error는 None. failed면 decisions=None · error 필수 — 일부 블록만 성공으로 저장하지 않는다.
    """

    schema_version: str = LOGO_SCHEMA_VERSION
    image_id: str  # 원본 이미지 식별자(고정 입력본의 이미지 폴더 이름). 파일 간 식별 = image_id + section_key + block_key
    section_key: str
    status: LogoStatus
    decisions: list[LogoDecision] | None
    error: str | None = None

    @model_validator(mode="after")
    def _by_status(self) -> "LogoResult":
        if self.status == "ok":
            if self.decisions is None:
                raise ValueError("ok면 decisions가 있어야 한다(블록이 없으면 빈 목록)")
            if self.error is not None:
                raise ValueError("ok면 error는 None이어야 한다")
            keys = [d.block_key for d in self.decisions]
            if len(set(keys)) != len(keys):
                raise ValueError("decisions에 같은 block_key가 두 번 있다")
        else:
            if self.decisions is not None:
                raise ValueError("failed면 decisions는 None이어야 한다 — 부분 성공 · false 대체 없음")
            if not (self.error and self.error.strip()):
                raise ValueError("failed면 비어 있지 않은 error가 있어야 한다")
        return self


def finding_key(n: int) -> str:
    return f"f_{n:02d}"


def match_key(n: int) -> str:
    return f"m_{n:03d}"


def verdict_key(n: int) -> str:
    return f"v_{n:02d}"


# ---------------------------------------------------------------------------
# ⑥ 인페인팅 — 개발용 잠정 타입 (BE 인계 · 저장 계약 아님)
# 근거: 계약 5.2 · 2.5(원시 영역 score) · open-questions.md 2.3절(⑥ 단독 개발 계획 승인 2026-09-30) · #72 · #74 · #75,
# 개발용 v1 형식은 사용자 승인(2026-09-30, pipeline.md 7.6절). status · kind · 오류 문자열은 운영 enum · 오류 코드가 아니다.
# AnalyzeResult와 공통 SCHEMA_VERSION은 바꾸지 않는다 — 이 결과는 전용 버전 INPAINT_SCHEMA_VERSION을 쓴다.
# ---------------------------------------------------------------------------
INPAINT_SCHEMA_VERSION = "1"  # [잠정] ⑥ 개발용 결과(InpaintResult) 전용 버전
InpaintMode = Literal["mask_only", "inpaint"]  # [잠정] mask_only = 마스크 · 진단만(배경 없음) / inpaint = 모델 추론 경로
# [잠정] masked = 마스크만 만듦(인페인팅 아님) / inpainted = 모델 1회 호출 후 합성 / unchanged = 빈 마스크라 모델 호출 없이 원본 사본 /
# failed = 배경 없음 · error 필수
InpaintStatus = Literal["masked", "inpainted", "unchanged", "failed"]


class InpaintFiles(_Model):
    """[잠정] 산출 파일 — 실행 출력 폴더(--out) 기준 상대 경로('/' 구분). 만들지 않은 파일은 None."""

    final_mask: str | None
    protect_mask: str | None
    background: str | None


class InpaintCounts(_Model):
    """[잠정] 영역 분류별 개수와 마스크 픽셀 수. conflict_px = 삭제 합집합 ∩ 보호(보호가 이겨 지우지 않은 픽셀)."""

    regions: int = Field(ge=0)
    target: int = Field(ge=0)
    zero_area: int = Field(ge=0)
    protected_region: int = Field(ge=0)
    in_protected_block: int = Field(ge=0)
    protected_blocks: int = Field(ge=0)
    delete_px: int = Field(ge=0)
    protect_px: int = Field(ge=0)
    conflict_px: int = Field(ge=0)
    final_px: int = Field(ge=0)


class InpaintResult(_Model):
    """[잠정] ⑥ 섹션 하나의 결과. 원문 · 좌표 · 원시 OCR · ④⑤ 판정은 담지 않는다(바꾸지 않는다). 상세 영역 기록은 inpaint_debug.

    상태 불변식: masked(mask_only · 모델 호출 없음 · 배경 없음) / inpainted(inpaint · 호출 · 배경 · final_px > 0) /
    unchanged(inpaint · 호출 없음 · 배경 = 원본 사본 · final_px = 0) / failed(배경 없음 · error 필수). 성공 상태의 error는 None.
    """

    schema_version: str = INPAINT_SCHEMA_VERSION
    image_id: str  # 원본 이미지 식별자(고정 입력본의 이미지 폴더 이름). 파일 간 식별 = image_id + section_key
    section_key: str
    mode: InpaintMode
    status: InpaintStatus
    model_called: StrictBool
    files: InpaintFiles
    counts: InpaintCounts | None  # 마스크 계산 전 실패면 None
    error: str | None = None

    @model_validator(mode="after")
    def _by_status(self) -> "InpaintResult":
        s, f = self.status, self.files
        if s == "failed":
            if f.background is not None:
                raise ValueError("failed면 background는 None이어야 한다")
            if not (self.error and self.error.strip()):
                raise ValueError("failed면 비어 있지 않은 error가 있어야 한다")
            return self
        if self.error is not None:
            raise ValueError(f"{s}이면 error는 None이어야 한다")
        if self.counts is None or f.final_mask is None or f.protect_mask is None:
            raise ValueError(f"{s}이면 counts와 두 마스크 파일이 있어야 한다")
        if s == "masked":
            if self.mode != "mask_only" or self.model_called or f.background is not None:
                raise ValueError("masked는 mask_only 모드 · 모델 호출 없음 · 배경 없음이어야 한다")
        elif self.mode != "inpaint" or f.background is None:
            raise ValueError(f"{s}은 inpaint 모드 · 배경 파일이 있어야 한다")
        elif s == "inpainted" and not (self.model_called and self.counts.final_px > 0):
            raise ValueError("inpainted는 모델 호출 · final_px > 0이어야 한다")
        elif s == "unchanged" and (self.model_called or self.counts.final_px != 0):
            raise ValueError("unchanged는 모델 호출 없음 · final_px = 0이어야 한다")
        return self


# ---------------------------------------------------------------------------
# ⑦ 스타일 추출 — 개발용 잠정 타입 (BE 인계 · 저장 계약 아님)
# 근거: pipeline.md 단계표 ⑦(otsu_border · em ×1.35 · 정렬 허용 오차 블록 폭 12% · 원본 섹션 이미지) · 계약 1.3(라벨 · 로고 제외) ·
# 단독 개발 v1 명세(사용자 승인 2026-10-06). text_block.style JSON 계약(#14)이 아니다 — font_color · bg_color · est_font_px · align은
# 개발용 측정값 이름이고, status · 사유 값은 운영 enum · 오류 코드가 아니다. TextBlock과 공통 SCHEMA_VERSION은 바꾸지 않는다.
# ---------------------------------------------------------------------------
STYLE_SCHEMA_VERSION = "1"  # [잠정] ⑦ 개발용 결과(StyleResult) 전용 버전
AlignValue = Literal["left", "center", "right"]
ALIGN_VALUES: tuple[str, ...] = ("left", "center", "right")
# [잠정] 블록 status = 영역 측정 상태(정렬 성공 여부와 별개 — align · align_basis · null_reasons로 판단)
# excluded = 라벨 · 로고 제외 / no_text = 비공백 측정 대상 영역 없음 / ok = 측정 대상 영역의 색 · 크기를 모두 얻음 /
# partial = 일부 또는 전부 측정 대상 영역의 색 추출 실패
StyleBlockStatus = Literal["excluded", "no_text", "ok", "partial"]
# [잠정] measured = 색 · 크기 측정 / color_failed = 색만 None(크기는 있음) / blank_text = 공백만 있는 영역이라 추출 생략
StyleRegionStatus = Literal["measured", "color_failed", "blank_text"]
StyleExcludedReason = Literal["product_label", "brand_logo"]
# [잠정] single_class = crop 명도가 두 집단으로 나뉘지 않음(단색 포함) / border_tie = 두 집단의 외곽 접촉 수가 같음
StyleColorError = Literal["single_class", "border_tie"]
# [잠정] estimated = 허용 오차 안 후보 중 분산 최소가 하나 / default_single_line = 유효 줄 1개라 기본 좌정렬 /
# default_tie = 후보 중 분산 최솟값 동률이라 기본 좌정렬(좌가 후보가 아니어도). 기본값 적용은 정렬 추정 성공이 아니다
AlignBasis = Literal["estimated", "default_single_line", "default_tie"]
# [잠정] null 사유: excluded = 라벨 · 로고 제외 / no_text = 비공백 측정 대상 영역 없음 / no_valid_region = 유효한 영역 값 없음 /
# no_candidate = 허용 오차를 충족한 정렬 후보 없음 / no_text_lines = 비공백 영역이 있는 줄 없음
StyleNullReason = Literal["excluded", "no_text", "no_valid_region", "no_candidate", "no_text_lines"]
STYLE_VALUE_FIELDS: tuple[str, ...] = ("font_color", "bg_color", "est_font_px", "align")
HEX_COLOR_PATTERN = r"^#[0-9A-F]{6}$"  # sRGB "#RRGGBB"(대문자)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StyleOtsuDiag(_Model):
    """[잠정] 영역 crop의 Otsu 진단. A(dark) = 명도 Y ≤ threshold, B(light) = Y > threshold. 배경 = 외곽 접촉이 많은 집단."""

    threshold: int = Field(ge=0, le=254)
    dark_px: int = Field(gt=0)
    light_px: int = Field(gt=0)
    border_px: int = Field(gt=0)
    border_dark: int = Field(ge=0)
    border_light: int = Field(ge=0)

    @model_validator(mode="after")
    def _border_sum(self) -> "StyleOtsuDiag":
        if self.border_dark + self.border_light != self.border_px:
            raise ValueError("border_dark + border_light는 border_px와 같아야 한다")
        return self


class RegionStyle(_Model):
    """[잠정] 원시 OCR 영역 하나의 측정값. score는 OCR 점수 보존(⑦은 score로 거르지 않는다)."""

    region_key: str
    line_key: str
    score: float = Field(ge=0.0, le=1.0)
    status: StyleRegionStatus
    font_color: str | None = Field(pattern=HEX_COLOR_PATTERN)
    bg_color: str | None = Field(pattern=HEX_COLOR_PATTERN)
    est_font_px: float | None = Field(gt=0)
    color_error: StyleColorError | None
    otsu: StyleOtsuDiag | None

    @model_validator(mode="after")
    def _by_status(self) -> "RegionStyle":
        colors = (self.font_color, self.bg_color)
        if self.status == "measured":
            if None in colors or self.est_font_px is None or self.color_error is not None or self.otsu is None:
                raise ValueError("measured는 두 색 · 크기 · otsu가 있고 color_error가 None이어야 한다")
        elif self.status == "color_failed":
            if colors != (None, None) or self.est_font_px is None or self.color_error is None:
                raise ValueError("color_failed는 두 색이 None · 크기가 있고 color_error가 있어야 한다")
            if (self.color_error == "single_class") != (self.otsu is None):
                raise ValueError("otsu는 single_class일 때만 None이다")
        elif colors != (None, None) or self.est_font_px is not None or self.color_error is not None or self.otsu is not None:
            raise ValueError("blank_text는 측정값 · color_error · otsu가 모두 None이어야 한다")
        return self


class StyleAlignDiag(_Model):
    """[잠정] 블록 정렬 진단. std는 모표준편차(ddof=0)의 실수 기록이며 판단에는 쓰지 않는다(판단은 분산의 정확 비교).
    tolerance_px = 블록 bbox.w × style.align_tolerance. candidates = 허용 오차를 충족한 정렬(left → center → right 순)."""

    std_left: float | None = Field(ge=0)  # 유효 줄 0개면 None
    std_center: float | None = Field(ge=0)
    std_right: float | None = Field(ge=0)
    tolerance_px: float = Field(ge=0)
    candidates: list[AlignValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "StyleAlignDiag":
        stds = (self.std_left, self.std_center, self.std_right)
        if None in stds and stds != (None, None, None):
            raise ValueError("세 std는 모두 있거나 모두 None이어야 한다")
        if stds == (None, None, None) and self.candidates:
            raise ValueError("유효 줄이 없으면 candidates는 비어 있어야 한다")
        if list(self.candidates) != [a for a in ALIGN_VALUES if a in self.candidates]:
            raise ValueError("candidates는 중복 없이 left → center → right 순이어야 한다")
        return self


class StyleCounts(_Model):
    """[잠정] regions = 원본 블록의 전체 영역 수. 제외 블록은 나머지 셋이 0(합 ≠ regions), 비제외 블록은 셋의 합 = regions."""

    regions: int = Field(ge=0)
    measured: int = Field(ge=0)
    color_failed: int = Field(ge=0)
    blank_text: int = Field(ge=0)


class BlockStyle(_Model):
    """[잠정] 블록 하나의 대표값과 근거. regions는 source_lines의 줄 순서 → 줄 안 영역 순서(제외 블록은 빈 목록).
    null_reasons는 값이 None인 측정값 필드(STYLE_VALUE_FIELDS)마다 하나씩, None이 아닌 필드에는 없다."""

    block_key: str
    status: StyleBlockStatus
    excluded_reason: StyleExcludedReason | None
    font_color: str | None = Field(pattern=HEX_COLOR_PATTERN)
    bg_color: str | None = Field(pattern=HEX_COLOR_PATTERN)
    est_font_px: float | None = Field(gt=0)
    align: AlignValue | None
    align_basis: AlignBasis | None
    align_diag: StyleAlignDiag | None
    null_reasons: dict[str, StyleNullReason] = Field(default_factory=dict)
    counts: StyleCounts
    regions: list[RegionStyle] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "BlockStyle":
        values = {f: getattr(self, f) for f in STYLE_VALUE_FIELDS}
        if set(self.null_reasons) != {f for f, v in values.items() if v is None}:
            raise ValueError("null_reasons의 키는 값이 None인 측정값 필드와 같아야 한다")
        if (self.align is None) != (self.align_basis is None):
            raise ValueError("align이 None이면 align_basis도 None이어야 한다(그 역도)")
        c = self.counts
        if self.status == "excluded":
            if self.excluded_reason is None or self.regions or self.align_diag is not None:
                raise ValueError("excluded는 excluded_reason이 있고 regions가 비어 있으며 align_diag가 None이어야 한다")
            if any(v is not None for v in values.values()) or set(self.null_reasons.values()) != {"excluded"}:
                raise ValueError("excluded는 모든 측정값이 None이고 사유가 excluded여야 한다")
            if (c.measured, c.color_failed, c.blank_text) != (0, 0, 0):
                raise ValueError("excluded의 measured · color_failed · blank_text는 0이어야 한다")
            return self
        if self.excluded_reason is not None or self.align_diag is None:
            raise ValueError(f"{self.status}는 excluded_reason이 None이고 align_diag가 있어야 한다")
        if c.measured + c.color_failed + c.blank_text != c.regions or len(self.regions) != c.regions:
            raise ValueError("비제외 블록은 measured + color_failed + blank_text = regions = 영역 결과 수여야 한다")
        got = {s: sum(r.status == s for r in self.regions) for s in ("measured", "color_failed", "blank_text")}
        if got != {"measured": c.measured, "color_failed": c.color_failed, "blank_text": c.blank_text}:
            raise ValueError("counts가 영역 결과의 상태 수와 다르다")
        expected = "no_text" if c.measured + c.color_failed == 0 else ("partial" if c.color_failed else "ok")
        if self.status != expected:
            raise ValueError(f"status {self.status} ≠ 영역 상태로 정해지는 {expected}")
        return self


class StyleConfigSnapshot(_Model):
    """[잠정] 추출에 쓴 [style] 설정 값."""

    method: Literal["otsu_border"]
    em_ratio: float
    align_tolerance: float


class StyleFingerprints(_Model):
    """[잠정] 입력 지문(SHA-256) — blocks = 현재 ③ 블록(label.input_fingerprint), label · logo = 결과 JSON, logo_record = ⑤ 기록 자체.
    이미지 픽셀 · 설정은 들어 있지 않으므로 완전한 캐시 키가 아니다."""

    blocks: str = Field(pattern=_SHA256_PATTERN)
    label: str = Field(pattern=_SHA256_PATTERN)
    logo: str = Field(pattern=_SHA256_PATTERN)
    logo_record: str = Field(pattern=_SHA256_PATTERN)


class StyleResult(_Model):
    """[잠정] ⑦ 섹션 하나의 결과 — ③의 모든 블록을 block_order 순으로 담는다. 원문 · 좌표 · ④⑤ 판정은 담지 않는다(바꾸지 않는다).
    섹션 입력 오류는 결과 없이 StyleInputError(stages/style.py)이므로 섹션 status는 없다."""

    schema_version: str = STYLE_SCHEMA_VERSION
    image_id: str  # 원본 이미지 식별자(고정 입력본의 이미지 폴더 이름). 파일 간 식별 = image_id + section_key + block_key + region_key
    section_key: str
    config: StyleConfigSnapshot
    input_fingerprints: StyleFingerprints
    blocks: list[BlockStyle]

    @field_validator("blocks")
    @classmethod
    def _unique_blocks(cls, v: list[BlockStyle]) -> list[BlockStyle]:
        keys = [b.block_key for b in v]
        if len(set(keys)) != len(keys):
            raise ValueError("blocks에 같은 block_key가 두 번 있다")
        return v
