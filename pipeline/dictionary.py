"""③-1 · ③-1' 사전 데이터 — 정규화 JSON의 스키마 · 로더 · 두 뷰.

근거: docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md 8절(D8), open-questions.md #3 · #60. 전달 자료(설명서 · 규제사전 ·
현지부적합사전, docs/ai/README.md 2.1)는 정본이 아니므로 여기의 필드는 **개발용 잠정 스키마**이며 BE 저장 계약이 아니다.

파일(정규화 JSON, 실제 값은 git 밖 `judge.dict_dir`, 합성 데이터는 `pipeline/data/dict/synthetic/`):
- regulation.json        규제사전(적재 대상 탭 16행 + 대표 근거)
- local_unsuitable.json  현지부적합사전(8행)
- policy_rules.json      ③-1' 매핑 · 규제 분류 변환 · 예외 쌍(설명서 §3 · §4 기반 잠정, D10 · D2)

버전 세 가지(D8, 2026-09-28 사용자 지시):
- schema_version       정규화 JSON 구조 버전(이 모듈의 DICT_SCHEMA_VERSION)
- dictionary_version   이번에 만든 데이터 묶음의 고유 식별자(예: regulation@2026-09-28.1). 같은 이름으로 내용이 바뀌지 않게
                       파일 SHA-256을 실행 기록에 함께 남긴다(fingerprint()).
- source               원본이 주장하는 버전과 확인 상태(claimed_version · version_status · version_note)

두 뷰(설계 1절): ③-1은 판정값 · 대체 표현 · 사유 같은 **정책 필드를 읽지 않는다.** judge_view()는 패턴 · 항목명 · 맥락 2열만
노출하고, policy_view()는 전체 항목과 규칙을 노출한다. 테스트가 이 경계를 고정한다.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DICT_SCHEMA_VERSION = "1"

REGULATION_FILE = "regulation.json"
LOCAL_FILE = "local_unsuitable.json"
RULES_FILE = "policy_rules.json"
DICT_FILES: tuple[str, ...] = (REGULATION_FILE, LOCAL_FILE, RULES_FILE)

RegulatoryClass = Literal["cosmetic", "otc", "common"]
RegulationVerdict = Literal["allowed", "conditional", "rewritable", "regulated"]
LocalVerdict = Literal["irrelevant", "needs_fix"]
VersionStatus = Literal["as_claimed", "mismatch_pending", "unknown"]
DictCoverage = Literal["selected_class_all_entries", "partial_class_combination", "unverified_class"]


class _Model(BaseModel):
    """모르는 키는 거부한다. 원본 열 이름(별칭)과 속성 이름 둘 다로 만들 수 있고, 저장은 별칭으로 한다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceInfo(_Model):
    """원본 자료 정보. claimed_version은 원본이 스스로 밝힌 버전, version_status는 그 확인 상태."""

    file: str
    sha256: str = Field(min_length=64, max_length=64)
    sheets: list[str]
    claimed_version: str | None = None
    version_status: VersionStatus = "unknown"
    version_note: str | None = None  # 불일치 · 확인 대기 사유(open-questions #50)
    extracted_at: str  # YYYY-MM-DD
    row_count: int = Field(ge=0)


# ---------------------------------------------------------------------------
# 규제사전
# ---------------------------------------------------------------------------
class RegulationEvidence(_Model):
    """대표 근거(근거 탭 is_primary=Y 행). 조문 전문은 화면에 싣지 않지만 데이터로는 보존한다."""

    evidence_id: str
    source_type: str  # Warning Letter · FDA 공식 문서 · OTC 모노그래프 · 시장 관행(설명서 §4)
    article: str | None = None
    url: str
    quote: str | None = None
    verified_at: str | None = None


class RegulationEntry(_Model):
    """규제사전 한 행. 열 이름은 원본(규제사전 탭)을 그대로 별칭으로 둔다. 시트 전용 열(confidence · source · version)은 없다."""

    id: str = Field(pattern=r"^RG-\d{3}$")
    dict_type: Literal["regulatory"] = "regulatory"
    target_country: str
    regulatory_class: RegulatoryClass
    source_expression: str = Field(min_length=1)
    variant_ko: list[str] = Field(alias="variant_expressions.ko", min_length=1)
    variant_en: list[str] = Field(alias="variant_expressions.en", default_factory=list)
    alternative_expression: list[str] = Field(default_factory=list)
    verdict_status: RegulationVerdict
    reason: str = Field(min_length=1)
    evidence: RegulationEvidence
    verified_at: str = Field(min_length=1)
    internal_category: str | None = None

    @model_validator(mode="after")
    def _alternative_rule(self) -> "RegulationEntry":
        # 설명서 §4: rewritable은 대체 표현 필수, allowed는 비움. regulated는 유무로 교체형/금지형이 갈린다(③-1'이 해석).
        if self.verdict_status == "rewritable" and not self.alternative_expression:
            raise ValueError(f"{self.id}: rewritable은 alternative_expression이 비어 있으면 안 된다(설명서 §4)")
        if self.verdict_status == "allowed" and self.alternative_expression:
            raise ValueError(f"{self.id}: allowed는 alternative_expression을 비운다(설명서 §4)")
        return self


class RegulationDict(_Model):
    schema_version: str = DICT_SCHEMA_VERSION
    dictionary_version: str = Field(pattern=r"^regulation@\d{4}-\d{2}-\d{2}\.\d+$")
    dict_type: Literal["regulatory"] = "regulatory"
    source: SourceInfo
    entries: list[RegulationEntry]

    @model_validator(mode="after")
    def _unique_ids(self) -> "RegulationDict":
        _check_unique([e.id for e in self.entries], "regulation")
        return self


# ---------------------------------------------------------------------------
# 현지부적합사전
# ---------------------------------------------------------------------------
class LocalEntry(_Model):
    """현지부적합사전 한 행. 별칭은 원본 열 이름(한국어). 판단 근거 · kr_freq · kr_corpus는 내부 기록용이라 넣지 않는다(설명서 §3)."""

    id: str = Field(pattern=r"^LC-\d{2}$")
    item: str = Field(alias="항목", min_length=1)
    patterns: list[str] = Field(alias="패턴", min_length=1)
    verdict_status: LocalVerdict = Field(alias="판정")
    exclusion_context: str = Field(alias="제외하는 맥락", min_length=1)
    keep_context: str = Field(alias="제외하지 않는 맥락", min_length=1)
    seller_message: str = Field(alias="셀러 문장", min_length=1)
    verified_at: str = Field(min_length=1)


class LocalDict(_Model):
    schema_version: str = DICT_SCHEMA_VERSION
    dictionary_version: str = Field(pattern=r"^local@\d{4}-\d{2}-\d{2}\.\d+$")
    dict_type: Literal["local"] = "local"
    target_country: str = "US"  # 설명서 §3 고정값(제안). BE 저장 필드 확정 아님
    source: SourceInfo
    entries: list[LocalEntry]

    @model_validator(mode="after")
    def _unique_ids(self) -> "LocalDict":
        _check_unique([e.id for e in self.entries], "local")
        return self


# ---------------------------------------------------------------------------
# 정책 규칙 — 설명서 §3 · §4 기반 잠정(D10), 예외 쌍(D2)은 근거 확인 전 빈 목록
# ---------------------------------------------------------------------------
class ClassRule(_Model):
    """상품 규제 분류 → 적용 사전 분류와 검사 범위 표시(D9). 검사 범위는 '현재 사전 항목 기준'이다."""

    applied_classes: list[RegulatoryClass] = Field(min_length=1)
    dict_coverage: DictCoverage


class VerdictRule(_Model):
    """finding(사전 판정값 × 대체 표현 유무) → 출력 판정값 · 버킷. emit_verdict_status=None은 판정 행을 만들지 않음(allowed)."""

    dict_type: Literal["regulatory", "local"]
    verdict_status: str
    alternative: Literal["any", "required", "present", "absent"] = "any"
    emit_verdict_status: str | None
    bucket: Literal["include", "exclude", "none"]


class OverrideRule(_Model):
    """예외 쌍(D2). keep 항목의 매칭 구간과 겹치는 drop 항목의 매칭을 억제한다. 실제 값은 데이터 담당 확인 후에만 채운다."""

    keep: str
    drop: list[str] = Field(min_length=1)
    basis: str = Field(min_length=1)  # 근거(사전 노트 · 결정기록 등). 비워 둘 수 없다


class PolicyRules(_Model):
    schema_version: str = DICT_SCHEMA_VERSION
    rules_version: str = Field(pattern=r"^policy@\d{4}-\d{2}-\d{2}\.\d+$")
    basis: str = Field(min_length=1)  # "설명서 §3 · §4 기반 잠정 …" — 계약 채택 전임을 파일 안에 명시
    regulatory_class_map: dict[str, ClassRule]
    verdict_map: list[VerdictRule] = Field(min_length=1)
    uncertain_bucket: Literal["include", "exclude"]
    overrides: list[OverrideRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _class_map_complete(self) -> "PolicyRules":
        # API의 job.regulatory_class enum(docs/openapi.yaml) 네 값이 모두 있어야 한다. 누락(None)은 표에 없고 입력 오류다(D9-c).
        missing = [k for k in ("cosmetic", "otc", "combination", "unknown") if k not in self.regulatory_class_map]
        if missing:
            raise ValueError("policy_rules.regulatory_class_map에 없는 분류: " + ", ".join(missing))
        return self


# ---------------------------------------------------------------------------
# 로더와 두 뷰
# ---------------------------------------------------------------------------
class DictionaryError(ValueError):
    pass


@dataclass(frozen=True)
class JudgeItem:
    """③-1이 보는 사전 항목 — 정책 필드 없음(설계 1절 · 8절 '두 뷰')."""

    id: str
    dict_type: Literal["regulatory", "local"]
    name: str
    patterns_ko: tuple[str, ...]
    exclusion_context: str | None  # local만
    keep_context: str | None  # local만


@dataclass(frozen=True)
class JudgeDictView:
    items: tuple[JudgeItem, ...]
    dictionary_version: dict[str, str]
    fingerprint: dict[str, str]

    def local_items(self) -> tuple[JudgeItem, ...]:
        return tuple(i for i in self.items if i.dict_type == "local")

    def regulatory_items(self) -> tuple[JudgeItem, ...]:
        return tuple(i for i in self.items if i.dict_type == "regulatory")

    def by_id(self, item_id: str) -> JudgeItem:
        for i in self.items:
            if i.id == item_id:
                return i
        raise KeyError(item_id)


@dataclass(frozen=True)
class PolicyDictView:
    regulation: dict[str, RegulationEntry]
    local: dict[str, LocalEntry]
    rules: PolicyRules
    dictionary_version: dict[str, str]
    fingerprint: dict[str, str]


@dataclass(frozen=True)
class Dictionaries:
    regulation: RegulationDict
    local: LocalDict
    rules: PolicyRules
    fingerprint: dict[str, str]  # 파일 이름 → SHA-256(재현성 기록용)
    dir: Path

    @property
    def dictionary_version(self) -> dict[str, str]:
        return {
            "regulation": self.regulation.dictionary_version,
            "local": self.local.dictionary_version,
            "rules": self.rules.rules_version,
        }

    def judge_view(self) -> JudgeDictView:
        items: list[JudgeItem] = []
        for e in self.local.entries:
            items.append(JudgeItem(e.id, "local", e.item, tuple(e.patterns), e.exclusion_context, e.keep_context))
        for e in self.regulation.entries:
            items.append(JudgeItem(e.id, "regulatory", e.source_expression, tuple(e.variant_ko), None, None))
        return JudgeDictView(tuple(items), self.dictionary_version, dict(self.fingerprint))

    def policy_view(self) -> PolicyDictView:
        return PolicyDictView(
            {e.id: e for e in self.regulation.entries},
            {e.id: e for e in self.local.entries},
            self.rules,
            self.dictionary_version,
            dict(self.fingerprint),
        )


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_versioned(path: Path, model: type[_Model]) -> Any:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DictionaryError(f"사전 파일이 없다: {path}") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise DictionaryError(f"사전 파일을 읽을 수 없다: {path} ({e.__class__.__name__})") from e
    if not isinstance(raw, dict) or raw.get("schema_version") != DICT_SCHEMA_VERSION:
        raise DictionaryError(
            f"{path.name}: schema_version이 {DICT_SCHEMA_VERSION!r}가 아니다: {raw.get('schema_version') if isinstance(raw, dict) else raw!r}"
        )
    try:
        return model.model_validate(raw)
    except ValueError as e:
        raise DictionaryError(f"{path.name}: {e}") from e


def load_dictionaries(dict_dir: str | Path) -> Dictionaries:
    """세 파일을 읽고 검증한다. 어느 하나라도 없거나 구조가 틀리면 DictionaryError."""
    d = Path(dict_dir)
    reg = _read_versioned(d / REGULATION_FILE, RegulationDict)
    loc = _read_versioned(d / LOCAL_FILE, LocalDict)
    rules = _read_versioned(d / RULES_FILE, PolicyRules)
    known = {e.id for e in reg.entries} | {e.id for e in loc.entries}
    for o in rules.overrides:
        unknown = [x for x in [o.keep, *o.drop] if x not in known]
        if unknown:
            raise DictionaryError(f"{RULES_FILE}: overrides가 사전에 없는 항목을 가리킨다: {unknown}")
    fp = {name: sha256_of(d / name) for name in DICT_FILES}
    return Dictionaries(reg, loc, rules, fp, d)


def dump_model(model: _Model) -> str:
    """정규화 JSON 저장 형식 — 원본 열 이름(별칭) · UTF-8 · 들여쓰기 2 · 줄 끝 개행."""
    return json.dumps(model.model_dump(by_alias=True, mode="json"), ensure_ascii=False, indent=2) + "\n"


def _check_unique(ids: list[str], label: str) -> None:
    seen: set[str] = set()
    dup = sorted({i for i in ids if i in seen or seen.add(i)})  # type: ignore[func-returns-value]
    if dup:
        raise ValueError(f"{label}: 중복 id {dup}")
