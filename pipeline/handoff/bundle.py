"""BE가 공급하는 고정 사전 실행 묶음과 content_type 대응표 [통합 D5 · D7 · D9-2 · 5.10 · 5.22].

- BE가 분석 실행 시작 시 DB 적재본으로 만든 묶음 하나를 실행 입력으로 받는다. AI는 실행 중 최신 DB나 다른 묶음을 읽지 않는다.
- AI는 원본 사전 ID(external_id)로 참조를 반환한다. DB PK 대응 · 당시 내용 고정은 BE 몫이다.
- 묶음 무결성: `bundle_sha256` = `bundle_sha256` 키를 뺀 묶음 객체의 JCS SHA-256(5.32와 같은 직렬화). 다르면 손상 — bundle_invalid.
- 개발용 필수값을 DB에서 얻지 못하면 AI가 만들지 않는다. 원래 없는 값(조문 · URL · 원본 판정값 · 확인일)은 null로 받는다.
- 대체 표현은 BE가 묶음 생성 시 배열로 변환해 준다. AI 실행 중에는 문자열을 나누지 않는다.
- 현지 사전 조회 실패는 `local=null` + `local_unavailable.reason`으로 받는다(D9-1: 검사 불가 안내 후 N3). 규제 묶음 실패는 BE의 N2 실패다.
- 개발용 예외 쌍(`overrides`)은 운영 규칙이 아니다(5.21 R04). 비어 있지 않으면 거부한다 — 조용히 무시하지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pipeline.dictionary import JudgeDictView, JudgeItem, PolicyRules
from pipeline.handoff.canonical import sha256_canonical, sha256_file
from pipeline.matching import PatternError, normalize_pattern

CONTENT_TYPES_PATH = Path(__file__).resolve().parents[1] / "data" / "content_types.json"
SHA256 = r"^[0-9a-f]{64}$"


class BundleError(ValueError):
    """고정 묶음 · 대응표가 구조 · 무결성 · 지원 범위를 어긴다(bundle_invalid)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSnapshot(_Model):
    external_id: str | None = None
    source_type: str | None = None
    article: str | None = None
    url: str | None = None
    quote: str | None = None


class RegulationRow(_Model):
    external_id: str = Field(min_length=1)
    regulatory_class: Literal["cosmetic", "otc", "common"]
    source_expression: str = Field(min_length=1)
    variant_ko: list[str] = Field(min_length=1)
    variant_en: list[str] = Field(default_factory=list)  # DB forbidden_en(영어 재대조 패턴). 없으면 빈 목록 — NULL을 추정 변환하지 않도록 BE가 결정해 준다
    alternative_expression: list[str] = Field(default_factory=list)
    verdict_status: Literal["allowed", "conditional", "rewritable", "regulated"]
    source_verdict_status: str | None = None  # 원본 판정값. 없으면 null(서비스 값으로 역추정하지 않음)
    reason: str = Field(min_length=1)
    evidence: EvidenceSnapshot | None = None
    confirmed_date: str | None = None

    @model_validator(mode="after")
    def _alternative_rule(self) -> "RegulationRow":
        if self.verdict_status == "allowed" and self.alternative_expression:
            raise ValueError(f"{self.external_id}: allowed는 대체 표현이 없어야 한다")
        if self.verdict_status == "rewritable" and not self.alternative_expression:
            raise ValueError(f"{self.external_id}: rewritable은 대체 표현이 있어야 한다")
        return self


class LocalRow(_Model):
    external_id: str = Field(min_length=1)
    item: str = Field(min_length=1)  # 항목명(content_type 코드가 아니다)
    patterns: list[str] = Field(min_length=1)
    verdict_status: Literal["irrelevant", "needs_fix"]
    exclusion_context: str = Field(min_length=1)
    keep_context: str = Field(min_length=1)
    seller_message: str = Field(min_length=1)
    confirmed_date: str | None = None


class RegulationPart(_Model):
    version: str = Field(min_length=1)
    entries: list[RegulationRow]


class LocalPart(_Model):
    version: str = Field(min_length=1)
    entries: list[LocalRow]


class LocalUnavailable(_Model):
    reason: str = Field(min_length=1)


class RuntimeBundle(_Model):
    bundle_id: str = Field(min_length=1)
    bundle_sha256: str = Field(pattern=SHA256)
    target_country: str = Field(min_length=1)
    regulation: RegulationPart
    local: LocalPart | None
    local_unavailable: LocalUnavailable | None = None
    rules: dict[str, Any]  # PolicyRules 구조(rules_version · regulatory_class_map · verdict_map · uncertain_bucket · overrides)

    @model_validator(mode="after")
    def _local_xor(self) -> "RuntimeBundle":
        if (self.local is None) == (self.local_unavailable is None):
            raise ValueError("local과 local_unavailable 중 정확히 하나만 있어야 한다(현지 조회 실패면 local=null + 사유)")
        return self


@dataclass(frozen=True)
class LocalType:
    content_type: str
    external_id: str
    price_suffix_exception: bool


@dataclass(frozen=True)
class TypeMap:
    version: str
    sha256: str
    local: tuple[LocalType, ...]
    regulatory_content_type: str

    def local_by_id(self) -> dict[str, LocalType]:
        return {t.external_id: t for t in self.local}


def load_type_map(path: Path = CONTENT_TYPES_PATH) -> TypeMap:
    raw = json.loads(path.read_text(encoding="utf-8"))
    local = tuple(LocalType(x["content_type"], x["external_id"], bool(x.get("price_suffix_exception", False))) for x in raw["local"])
    types = [t.content_type for t in local] + [raw["regulatory"]["content_type"]]
    if len(set(types)) != len(types) or len({t.external_id for t in local}) != len(local):
        raise BundleError(f"{path.name}: content_type 또는 external_id가 중복이다")
    return TypeMap(raw["version"], sha256_file(path), local, raw["regulatory"]["content_type"])


@dataclass(frozen=True)
class CheckedBundle:
    raw: RuntimeBundle
    rules: PolicyRules
    regulation: dict[str, RegulationRow]
    local: dict[str, LocalRow] | None
    types: TypeMap

    @property
    def identity(self) -> dict[str, Any]:
        return {"bundle_id": self.raw.bundle_id, "bundle_sha256": self.raw.bundle_sha256,
                "regulation_version": self.raw.regulation.version,
                "local_version": self.raw.local.version if self.raw.local else None,
                "rules_version": self.rules.rules_version, "type_map_version": self.types.version, "type_map_sha256": self.types.sha256}

    def applied_classes(self, regulatory_class: str) -> list[str]:
        return list(self.rules.regulatory_class_map[regulatory_class].applied_classes)

    def local_view(self) -> JudgeDictView:
        """③-1 맥락 판정용 뷰 — 정책 필드 없음(판정값 · 셀러 문장 · 대체 표현을 모델에 보내지 않는다). 대응표 순서."""
        assert self.local is not None
        items = tuple(JudgeItem(t.external_id, "local", self.local[t.external_id].item, tuple(self.local[t.external_id].patterns),
                                self.local[t.external_id].exclusion_context, self.local[t.external_id].keep_context)
                      for t in self.types.local)
        return JudgeDictView(items, {"local": self.raw.local.version}, {"bundle_sha256": self.raw.bundle_sha256})


def bundle_content_sha256(raw: dict[str, Any]) -> str:
    return sha256_canonical({k: v for k, v in raw.items() if k != "bundle_sha256"})


def check_bundle(raw: Any, *, target_country: str, types: TypeMap | None = None) -> CheckedBundle:
    """구조 · 무결성 · 지원 범위를 검사한다. 어기면 BundleError. 최신 DB와 대조하지 않는다(D5)."""
    if not isinstance(raw, dict):
        raise BundleError("사전 묶음은 JSON 객체여야 한다")
    try:
        b = RuntimeBundle.model_validate(raw)
    except ValidationError as e:
        raise BundleError(f"사전 묶음 구조 오류: {e}") from e
    actual = bundle_content_sha256(raw)
    if actual != b.bundle_sha256:
        raise BundleError(f"사전 묶음 지문 불일치(손상 · 변경): 선언 {b.bundle_sha256[:12]}… · 계산 {actual[:12]}…")
    if b.target_country != target_country:
        raise BundleError(f"사전 묶음 국가 {b.target_country} ≠ 실행 국가 {target_country}")
    try:
        rules = PolicyRules.model_validate(b.rules)
    except ValidationError as e:
        raise BundleError(f"정책 규칙 구조 오류: {e}") from e
    if rules.overrides:
        raise BundleError("정책 규칙의 overrides(개발용 예외 쌍)는 운영 규칙이 아니다 — 5.23 감싸기 규칙으로 처리한다")
    types = types or load_type_map()
    problems: list[str] = []
    reg_ids = [e.external_id for e in b.regulation.entries]
    loc_ids = [e.external_id for e in b.local.entries] if b.local else []
    dup = sorted({i for i in reg_ids + loc_ids if (reg_ids + loc_ids).count(i) > 1})
    if dup:
        problems.append(f"중복 external_id {dup}")
    if b.local is not None:
        want = {t.external_id for t in types.local}
        if set(loc_ids) != want:
            problems.append(f"현지 항목 {sorted(set(loc_ids))} ≠ 대응표 {sorted(want)} (미지원 항목은 공급 전에 거부)")
    # 정규화 후 빈 패턴 · 같은 문자열의 금지/허용 적재(D8: 적재 거부 대상)
    for lang, rows in (("ko", [(e, e.variant_ko) for e in b.regulation.entries]), ("en", [(e, e.variant_en) for e in b.regulation.entries])):
        seen: dict[str, tuple[str, bool]] = {}
        for e, pats in rows:
            for p in pats:
                try:
                    n = normalize_pattern(p, lang)  # type: ignore[arg-type]
                except PatternError as pe:
                    problems.append(f"{e.external_id} {lang}: {pe}")
                    continue
                allowed = e.verdict_status == "allowed"
                prev = seen.get(n)
                if prev is not None and prev[1] != allowed:
                    problems.append(f"{lang} 정규화 결과가 같은 금지/허용 패턴: {prev[0]} · {e.external_id} ({p!r})")
                seen.setdefault(n, (e.external_id, allowed))
    if b.local is not None:
        for e in b.local.entries:
            for p in e.patterns:
                try:
                    normalize_pattern(p, "ko")
                except PatternError as pe:
                    problems.append(f"{e.external_id}: {pe}")
    if problems:
        raise BundleError("사전 묶음 검증 실패: " + "; ".join(problems))
    return CheckedBundle(b, rules, {e.external_id: e for e in b.regulation.entries},
                         {e.external_id: e for e in b.local.entries} if b.local else None, types)
