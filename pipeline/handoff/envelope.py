"""AI ↔ BE 실행 인계 공통 봉투 [통합 5.24 · 5.31 · 5.32].

요청(BE → AI): 실행 · 시도 식별과 단계, 단계별 고정 입력. 키는 분석 때 고정한 임시 키를 유지하며 DB id 변환은 BE가 한다.
보고(AI → BE): 같은 식별 + `outcome`(completed · skipped · failed) + payload + 산출물 목록 + 실패 분류 + 입력 지문.

- `outcome`은 **단계 계산 결과**이며 API TaskStatus · 화면 전이가 아니다. completed는 렌더 · 검수 진입 보장이 아니다(5.24).
- skipped는 처리 대상 없음(정상 생략)에만 쓴다. 사유와 대상 수, 후속 단계가 필요한 참조를 함께 준다. 실패를 skipped로 바꾸지 않는다.
- failed는 실패 분류(`FailureKind`) · retryable 후보 · 메시지를 준다. 분류 문자열은 AI 인계용이며 job_async_task.error_code가 아니다 —
  BE가 오류 코드 · 재시도 한도로 매핑한다. ⑧의 구조 검증된 부분 결과는 failed + 성공 블록 payload다(5.32).
- 산출물은 실행 환경의 절대 경로 · 바이트 수 · SHA-256 · 형식 · 이미지 크기. BE는 업로더 주장값이 아니라 직접 측정해 검증한다(5.32).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pipeline.handoff.canonical import sha256_canonical, sha256_file

CONTRACT_VERSION = "handoff@2026-10-08.1"  # 이 봉투의 버전. AnalyzeResult 버전 1 · 개발용 결과 버전과 별개

Stage = Literal["analyze", "judge", "label", "logo", "inpaint", "style", "translate", "text_check"]
Outcome = Literal["completed", "skipped", "failed"]
# AI 인계용 실패 분류 — 운영 오류 코드가 아니다(BE가 매핑). retryable은 후보이며 실제 재시도는 BE의 누적 한도 · 정책으로 정한다
FailureKind = Literal[
    "input_invalid",  # 필수값 누락 · 형식 · 키/지문/해시 불일치 · 파일 없음 — 같은 입력으로 다시 해도 같다
    "unsupported_input",  # 정본이 지원하지 않는 입력(예: OCR 4,000px 초과 섹션, 미지원 사전 값)
    "config_invalid",  # 설정 · 프롬프트 · API 키 없음
    "bundle_invalid",  # 고정 사전 묶음의 구조 · 무결성 · 충돌(정규화 후 같은 구간의 금지/허용 등)
    "dependency_unavailable",  # 모델 런타임 · 가중치 · SDK 없음
    "model_call_failed",  # 외부 모델 · 자식 프로세스 호출 실패 · 시간 초과(세분은 #33 · #46 후속)
    "response_invalid",  # 모델 응답이 구조 검증을 통과하지 못함
    "output_invalid",  # 산출물 저장 · 재검증 실패
    "cancelled",  # 실행 권한 상실 · 취소 신호로 중단
    "internal",  # 예기치 않은 코드 오류
]
RETRYABLE_DEFAULT: dict[str, bool] = {
    "input_invalid": False, "unsupported_input": False, "config_invalid": False, "bundle_invalid": False,
    "dependency_unavailable": False, "model_call_failed": True, "response_invalid": True, "output_invalid": True,
    "cancelled": False, "internal": False,
}
ArtifactFormat = Literal["png", "json"]
SHA256 = r"^[0-9a-f]{64}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HandoffInputError(ValueError):
    """요청이 계약을 어긴다(필수값 · 형식 · 키 · 지문). 어댑터는 이것을 failed(input_invalid) 보고로 바꾼다 — 빈 성공으로 만들지 않는다."""

    def __init__(self, message: str, kind: str = "input_invalid") -> None:
        self.kind = kind
        super().__init__(message)


class RequestIdentity(_Model):
    """모든 요청의 공통 식별. 값은 BE의 실행 · 시도 식별자를 문자열로 받는다(형식은 BE 소유)."""

    contract_version: str
    execution_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    stage: Stage
    input_snapshot_id: str | None = None  # BE가 고정한 입력 스냅샷 식별(있으면 그대로 돌려준다)
    expected_input_manifest_sha256: str | None = Field(default=None, pattern=SHA256)  # BE가 고정한 지문. 주면 AI가 대조한다

    @model_validator(mode="after")
    def _version(self) -> "RequestIdentity":
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError(f"contract_version {self.contract_version!r} — 이 구현은 {CONTRACT_VERSION!r}만 받는다")
        return self


class Failure(_Model):
    kind: FailureKind
    retryable: bool
    message: str = Field(min_length=1)
    targets: list[str] = Field(default_factory=list)  # 실패 대상 키(⑧ 블록 · ③-1 섹션 등). 단계 전체 실패면 빈 목록 가능


class Artifact(_Model):
    """산출 파일 하나. path는 이 실행 환경의 절대 경로(다른 서버와 공유 주소가 아니다)."""

    kind: str = Field(min_length=1)  # section_image · background · delete_mask · protect_mask …
    part_key: str = Field(min_length=1)  # 같은 kind 안의 대상(섹션 키 등)
    path: str
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256)
    format: ArtifactFormat
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _absolute(self) -> "Artifact":
        if not Path(self.path).is_absolute():
            raise ValueError(f"산출물 경로는 절대 경로여야 한다: {self.path!r}")
        if self.format == "png" and (self.width is None or self.height is None):
            raise ValueError("png 산출물은 width · height가 있어야 한다")
        return self


class SourceRef(_Model):
    """파일을 새로 만들지 않고 검증된 기존 입력을 가리킨다(예: ⑥ 빈 마스크의 원본 섹션 배경, 5.31). BE가 소속 · 불변 식별을 확인한다."""

    kind: str = Field(min_length=1)
    part_key: str = Field(min_length=1)
    ref: Literal["section_image"]
    sha256: str = Field(pattern=SHA256)


class StageReport(_Model):
    contract_version: str = CONTRACT_VERSION
    execution_id: str
    attempt_id: str
    stage: Stage
    input_snapshot_id: str | None = None
    input_manifest: dict[str, Any] | None  # 이 단계가 실제로 소비한 입력의 요약(파일은 SHA-256). 요청 검증 전에 실패하면 None
    input_manifest_sha256: str | None = Field(pattern=SHA256)
    outcome: Outcome
    target_count: int = Field(ge=0)  # 이번 계산 대상 수(⑧ 재시도면 이번 실패 대상 수 — 전체 완료 분모가 아니다, 5.32)
    skip_reason: str | None = None
    payload: dict[str, Any] | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    failure: Failure | None = None
    implementation: dict[str, Any] = Field(default_factory=dict)  # 구현 · 모델 · 프롬프트 · 설정 식별(버전 + 내용 해시)
    diagnostics: dict[str, Any] = Field(default_factory=dict)  # 운영 저장 필수 아님. 원문 · 응답 전문은 넣지 않는다

    @model_validator(mode="after")
    def _by_outcome(self) -> "StageReport":
        if (self.input_manifest is None) != (self.input_manifest_sha256 is None):
            raise ValueError("input_manifest와 input_manifest_sha256은 함께 있거나 함께 없어야 한다")
        if self.input_manifest is not None and sha256_canonical(self.input_manifest) != self.input_manifest_sha256:
            raise ValueError("input_manifest_sha256이 input_manifest의 JCS SHA-256과 다르다")
        if self.outcome == "failed":
            if self.failure is None:
                raise ValueError("failed면 failure가 있어야 한다")
            if self.skip_reason is not None:
                raise ValueError("failed에는 skip_reason을 두지 않는다 — 실패를 정상 생략으로 바꾸지 않는다")
            return self
        if self.failure is not None:
            raise ValueError(f"{self.outcome}이면 failure는 None이어야 한다")
        if self.input_manifest is None or self.payload is None:
            raise ValueError(f"{self.outcome}이면 input_manifest와 payload가 있어야 한다(생략도 대상 없음의 증거와 후속 참조를 준다)")
        if self.outcome == "skipped":
            if not self.skip_reason or self.target_count != 0:
                raise ValueError("skipped는 skip_reason이 있고 target_count가 0이어야 한다")
        elif self.skip_reason is not None:
            raise ValueError("completed에는 skip_reason을 두지 않는다")
        keys = [(a.kind, a.part_key) for a in self.artifacts] + [(r.kind, r.part_key) for r in self.source_refs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"같은 (kind, part_key) 산출물이 두 번 있다: {sorted(k for k in set(keys) if keys.count(k) > 1)}")
        return self


# ---------------------------------------------------------------------------
# 보고 작성 도우미
# ---------------------------------------------------------------------------
def manifest_of(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    return data, sha256_canonical(data)


def check_expected_manifest(identity: RequestIdentity, manifest_sha: str) -> None:
    exp = identity.expected_input_manifest_sha256
    if exp is not None and exp != manifest_sha:
        raise HandoffInputError(f"입력 지문 불일치 — BE 고정 {exp[:12]}… · AI 계산 {manifest_sha[:12]}… (다른 입력으로 계산하지 않는다)")


def failed_report(identity: RequestIdentity | dict[str, Any], kind: str, message: str, *, retryable: bool | None = None,
                  targets: list[str] | None = None, manifest: dict[str, Any] | None = None, target_count: int = 0,
                  payload: dict[str, Any] | None = None, implementation: dict[str, Any] | None = None,
                  diagnostics: dict[str, Any] | None = None) -> StageReport:
    ident = identity.model_dump() if isinstance(identity, RequestIdentity) else identity
    return StageReport(
        execution_id=str(ident.get("execution_id") or ""), attempt_id=str(ident.get("attempt_id") or ""),
        stage=ident["stage"], input_snapshot_id=ident.get("input_snapshot_id"),
        input_manifest=manifest, input_manifest_sha256=sha256_canonical(manifest) if manifest is not None else None,
        outcome="failed", target_count=target_count, payload=payload,
        failure=Failure(kind=kind, retryable=RETRYABLE_DEFAULT[kind] if retryable is None else retryable,
                        message=message or kind, targets=targets or []),
        implementation=implementation or {}, diagnostics=diagnostics or {},
    )


def artifact_of(kind: str, part_key: str, path: Path) -> Artifact:
    """파일을 다시 열어 실제 바이트 · 해시 · 형식 · 크기를 잰다(쓴 값을 믿지 않는다)."""
    p = Path(path).resolve()
    data_len = p.stat().st_size
    fmt: ArtifactFormat
    width = height = None
    if p.suffix.lower() == ".png":
        from PIL import Image

        with Image.open(p) as im:
            if im.format != "PNG":
                raise ValueError(f"{p}: PNG가 아니다({im.format})")
            width, height = im.size
        fmt = "png"
    elif p.suffix.lower() == ".json":
        fmt = "json"
    else:
        raise ValueError(f"지원하지 않는 산출물 형식 {p.suffix}")
    return Artifact(kind=kind, part_key=part_key, path=str(p), bytes=data_len, sha256=sha256_file(p), format=fmt,
                    width=width, height=height)


def parse_identity(raw: Any, stage: str) -> RequestIdentity:
    """요청의 식별 부분만 먼저 검증한다(나머지 검증이 실패해도 식별을 담은 실패 보고를 만들기 위해)."""
    if not isinstance(raw, dict):
        raise HandoffInputError("요청은 JSON 객체여야 한다")
    keys = ("contract_version", "execution_id", "attempt_id", "stage", "input_snapshot_id", "expected_input_manifest_sha256")
    ident = RequestIdentity.model_validate({k: raw[k] for k in keys if k in raw})
    if ident.stage != stage:
        raise HandoffInputError(f"요청 stage {ident.stage!r} ≠ 이 어댑터 {stage!r}")
    return ident


def identity_fallback(raw: Any, stage: str) -> dict[str, Any]:
    """식별 검증조차 실패한 요청의 실패 보고용 식별(받은 값을 그대로 문자열화, 없으면 빈 값)."""
    r = raw if isinstance(raw, dict) else {}
    return {"execution_id": str(r.get("execution_id") or "-"), "attempt_id": str(r.get("attempt_id") or "-"), "stage": stage,
            "input_snapshot_id": r.get("input_snapshot_id") if isinstance(r.get("input_snapshot_id"), str) else None}
