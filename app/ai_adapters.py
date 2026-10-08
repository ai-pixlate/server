"""AI 단계 호출 경계 — BE 워커가 부르는 함수와 그 입력·출력 조건.

AI 내부(모델·알고리즘·프롬프트)는 AI 담당이다. BE는 절대 로컬 경로와 고정 입력을 준비하고, 반환값을 검증·저장한다.
AI 함수는 DB·S3에 접근하지 않는다(contract.md 1.1, D3).

| 단계 | 인터페이스 | 기본 구현 |
|---|---|---|
| ①②③ | Analyzer.analyze | PipelineAnalyzer → pipeline.analyze.analyze (실제 호출) |
| ③-1·③-1′ | Judge.judge → JudgeOutcome | UnavailableJudge — A안(전 섹션 현지 8항목·항목당 1건)과 DB 고정 묶음 입력형이 AI에서 아직 인계되지 않음 |
| ④ | Labeler.label | PipelineLabeler → pipeline.stages.label.run (실제 호출) |
| ⑤ | LogoJudge.logo | PipelineLogo → pipeline.stages.logo.run (실제 계산) |
| ⑥ | Inpainter.inpaint | PipelineInpainter → pipeline.stages.inpaint(+LaMa, GPU) |
| ⑦ | Styler.style | PipelineStyler → pipeline.stages.style.run (실제 계산) |
| ⑧ | Translator.translate → TranslateOutcome | UnavailableTranslator — AI ⑧ 미착수 |

테스트·로컬 검증은 set_adapters(...)로 대역을 끼운다. 대역 성공을 실제 통합 성공으로 보고하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class AdapterUnavailable(RuntimeError):
    """AI 구현이 아직 인계되지 않은 단계. 실패로 기록하며 성공·생략으로 바꾸지 않는다."""

    code = "AI_ADAPTER_UNAVAILABLE"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _cfg_hash(cfg: dict[str, Any], *tables: str) -> str:
    part = {t: cfg.get(t) for t in tables} if tables else cfg
    return hashlib.sha256(json.dumps(part, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _load_cfg() -> dict[str, Any]:
    from pipeline.config import load_config

    path = os.getenv("PIXLATE_PIPELINE_CONFIG")
    return load_config(path) if path else load_config()


# ---------------------------------------------------------------------------------------------------------
# ①②③ 초기 분석
# ---------------------------------------------------------------------------------------------------------
class Analyzer(Protocol):
    impl_version: str

    def analyze(self, sources: list[Any], out_dir: Path) -> Any:
        """sources: pipeline.types.SourceImage(절대 로컬 경로). 반환: pipeline.types.AnalyzeResult(버전 1).
        섹션 이미지는 out_dir 아래 로컬 파일. 실패는 pipeline.errors.AnalyzeError(code, retryable)."""
        ...


class PipelineAnalyzer:
    def __init__(self, cfg: dict[str, Any] | None = None, use_llm: bool | None = None):
        self.cfg = cfg if cfg is not None else _load_cfg()
        self.use_llm = use_llm if use_llm is not None else os.getenv("PIXLATE_ANALYZE_USE_LLM", "1") != "0"
        self.impl_version = f"pipeline.analyze@cfg:{_cfg_hash(self.cfg, 'section', 'ocr', 'merge')[:16]}:llm={int(self.use_llm)}"

    def analyze(self, sources: list[Any], out_dir: Path) -> Any:
        from pipeline.analyze import analyze

        return analyze(sources, self.cfg, out_dir, use_llm=self.use_llm)


# ---------------------------------------------------------------------------------------------------------
# ③-1 AI 섹션 판정 + ③-1′ 정책 적용 — BE가 요구하는 출력(JudgeOutcome)
# ---------------------------------------------------------------------------------------------------------
class JudgeBlock(_Strict):
    key: str
    block_order: int
    source_ko: str
    role: str
    bbox: dict[str, int]
    source_lines: list[dict[str, Any]]


class JudgeSection(_Strict):
    section_key: str
    image_path: str  # 절대 로컬 경로(섹션 이미지)
    width: int
    height: int
    blocks: list[JudgeBlock]
    prev_section_text: str | None = None  # 같은 원본 앞 섹션 텍스트(참고 문맥)
    next_section_text: str | None = None


class FindingOut(_Strict):
    """content_findings.findings[] 6필드 + 사용한 사전 원본 ID(0개 이상). evidence_block_keys 는 이번 입력의 키."""

    finding_key: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    status: Literal["present", "absent", "uncertain"]
    evidence_block_keys: list[str] = Field(default_factory=list)
    evidence_source: Literal["text", "image", "image_and_text"]
    reason: str = Field(min_length=1)
    dictionary_refs: list[str] = Field(default_factory=list)


class MatchOut(_Strict):
    """문자 구간은 source_ko 의 정규화 전 Unicode 코드 포인트 [start, end), matched_text == source_ko[start:end]."""

    match_key: str = Field(min_length=1)
    finding_key: str
    dictionary_ref: str
    block_key: str
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    matched_text: str = Field(min_length=1)


class VerdictOut(_Strict):
    """정책 판정 한 행. 사유·조문·링크·대체 표현은 BE가 고정 묶음 스냅샷에서 채운다(결과는 묶음의 ID·근거로 저장, D5)."""

    verdict_key: str = Field(min_length=1)
    finding_key: str
    dictionary_ref: str
    verdict_status: Literal["regulated", "conditional", "irrelevant", "needs_fix", "policy"]
    bucket: Literal["include", "exclude"]
    finding_status: Literal["present", "uncertain"]
    problem_text: str | None = None


class ScopeStatus(_Strict):
    status: Literal["ok", "failed", "not_inspected"]
    error: str | None = None
    retryable: StrictBool = False


class PolicyInfo(_Strict):
    rules_version: str = Field(min_length=1)
    rules_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    impl_version: str = Field(min_length=1)


class JudgeOutcome(_Strict):
    """③-1·③-1′ 한 섹션의 결과. regulatory 실패 = 이 섹션 판정 실패(N2 재시도·오류 대상),
    local failed = 현지 AI 판정 실패(D9-1: 섹션 제외·판정 실패 표시, 규제 결과는 보존),
    local not_inspected = 현지 사전 조회 실패로 검사하지 않음(제외 없이 NULL·안내)."""

    schema_version: Literal["1"] = "1"
    section_key: str
    regulatory: ScopeStatus
    local: ScopeStatus
    findings: list[FindingOut] = Field(default_factory=list)
    matches: list[MatchOut] = Field(default_factory=list)
    verdicts: list[VerdictOut] = Field(default_factory=list)
    overlaps: list[dict[str, Any]] = Field(default_factory=list)  # 억제·겹침 근거(원래 매칭 보존). 위반 배지로 쓰지 않는다
    inspection: dict[str, Any]  # 계획한 검사 · 실제 완료 범위 · 미검사 범위와 이유
    policy: PolicyInfo | None = None  # regulatory ok 이면 필수
    impl: dict[str, Any] = Field(default_factory=dict)  # 모델·프롬프트·설정 식별


class Judge(Protocol):
    impl_version: str

    def judge(self, section: JudgeSection, bundle: dict[str, Any], *, regulatory_class: str, target_country: str) -> JudgeOutcome:
        ...


class UnavailableJudge:
    impl_version = "unavailable"

    def judge(self, section: JudgeSection, bundle: dict[str, Any], *, regulatory_class: str, target_country: str) -> JudgeOutcome:
        raise AdapterUnavailable(
            "③-1·③-1′ 운영 어댑터가 아직 없다: A안(전 섹션 현지 8항목·섹션·항목당 1건) 출력과 DB 고정 묶음 입력형을 AI가 인계해야 한다"
        )


# ---------------------------------------------------------------------------------------------------------
# ④ 라벨 · ⑤ 로고 · ⑦ 스타일 · ⑥ 인페인팅 — 기존 단독 구현 연결
# ---------------------------------------------------------------------------------------------------------
class Labeler(Protocol):
    impl_version: str

    def label(self, section: Any, blocks: list[Any]) -> Any: ...


class PipelineLabeler:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg if cfg is not None else _load_cfg()
        self.impl_version = f"pipeline.label@cfg:{_cfg_hash(self.cfg, 'label')[:16]}"

    def label(self, section: Any, blocks: list[Any]) -> Any:
        from pipeline.stages import label

        return label.run(section, blocks, self.cfg)


class LogoJudge(Protocol):
    impl_version: str

    def logo(self, image_id: str, merged: Any, label_result: Any, brand_meta: dict[str, Any]) -> tuple[Any, dict[str, Any]]: ...


class PipelineLogo:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg if cfg is not None else _load_cfg()
        self.impl_version = f"pipeline.logo@cfg:{_cfg_hash(self.cfg, 'logo')[:16]}"

    def logo(self, image_id: str, merged: Any, label_result: Any, brand_meta: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        from pipeline.stages import logo

        return logo.run(image_id, merged, label_result, brand_meta, self.cfg)


class Styler(Protocol):
    impl_version: str

    def style(self, image_id: str, section: Any, merged: Any, label_result: Any, logo_result: Any,
              logo_record: dict[str, Any]) -> Any: ...


class PipelineStyler:
    def __init__(self, cfg: dict[str, Any] | None = None):
        self.cfg = cfg if cfg is not None else _load_cfg()
        self.impl_version = f"pipeline.style@cfg:{_cfg_hash(self.cfg, 'style')[:16]}"

    def style(self, image_id, section, merged, label_result, logo_result, logo_record):
        from pipeline.stages import style

        return style.run(image_id, section, merged, label_result, logo_result, logo_record, self.cfg)


@dataclass
class InpaintOut:
    """⑥ 한 섹션. status: inpainted / unchanged / failed. 배경·마스크는 PNG 바이트(로컬에서 만든 것).
    unchanged 는 최종 마스크 0 — 배경은 원본(source_ref)으로 대신한다."""

    status: str
    final_mask_png: bytes | None
    protect_mask_png: bytes | None
    background_png: bytes | None
    counts: dict[str, Any] | None
    regions: list[dict[str, Any]] | None
    protected_blocks: list[dict[str, Any]] | None
    model: dict[str, Any] | None
    error: str | None = None


class Inpainter(Protocol):
    impl_version: str

    def inpaint(self, image_id: str, section: Any, merged: Any, label_result: Any, logo_result: Any,
                logo_record: dict[str, Any]) -> InpaintOut: ...


class PipelineInpainter:
    """pipeline.stages.inpaint 의 검증·마스크·적용을 그대로 쓴다. 모델은 처음 비어 있지 않은 마스크에서 만든다(GPU 워커)."""

    def __init__(self, cfg: dict[str, Any] | None = None, model_factory=None):
        self.cfg = cfg if cfg is not None else _load_cfg()
        self.model_factory = model_factory
        self._model = None
        self.impl_version = f"pipeline.inpaint@cfg:{_cfg_hash(self.cfg, 'inpaint')[:16]}"

    def _png(self, arr) -> bytes:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return buf.getvalue()

    def inpaint(self, image_id, section, merged, label_result, logo_result, logo_record) -> InpaintOut:
        import numpy as np
        from PIL import Image

        from pipeline.stages import inpaint

        with Image.open(section.image_path) as im:
            if im.mode != "RGB":
                raise inpaint.InpaintInputError(f"섹션 이미지 모드 {im.mode} — RGB만 지원")
            image = np.array(im)
        plan = inpaint.validate_inputs(image_id, section, (image.shape[1], image.shape[0]), merged, label_result, logo_result,
                                       logo_record, self.cfg)
        masks = inpaint.build_masks(plan)
        final_u8 = masks.final.astype(np.uint8) * 255
        protect_u8 = masks.protect.astype(np.uint8) * 255
        regions = inpaint.region_diagnostics(plan, masks)
        protected = [{"block_key": p.block_key, "reason": p.reason, "bbox": p.bbox.model_dump()} for p in plan.protected_blocks]
        if not masks.final.any():
            return InpaintOut("unchanged", self._png(final_u8), self._png(protect_u8), None, masks.counts, regions, protected, None)
        if self._model is None:
            self._model = (self.model_factory or inpaint.build_model)(self.cfg)
        try:
            applied = inpaint.apply(image, plan, masks, self._model)
        except inpaint.InpaintModelError as e:
            return InpaintOut("failed", self._png(final_u8), self._png(protect_u8), None, masks.counts, regions, protected,
                              getattr(self._model, "describe", lambda: None)(), error=str(e))
        return InpaintOut("inpainted", self._png(final_u8), self._png(protect_u8), self._png(applied.background), masks.counts,
                          regions, protected, getattr(self._model, "describe", lambda: None)())

    def close(self) -> None:
        if self._model is not None and hasattr(self._model, "close"):
            self._model.close()


# ---------------------------------------------------------------------------------------------------------
# ⑧ 번역 — BE가 요구하는 출력(TranslateOutcome)
# ---------------------------------------------------------------------------------------------------------
class TranslateBlock(_Strict):
    key: str
    block_order: int
    role: str
    source_ko: str
    is_target: bool  # 이번 계산 대상(실패 대상 재시도면 실패 블록만 true). 문맥은 전체 블록


class TranslateItem(_Strict):
    """대상 키마다 하나. ok 면 text 필수, failed 면 error 필수. 키 누락·중복·미등록은 구조 실패(채택 거절)."""

    key: str
    status: Literal["ok", "failed"]
    text: str | None = None
    error: str | None = None
    glossary_ids: list[int] = Field(default_factory=list)


class TranslateOutcome(_Strict):
    schema_version: Literal["1"] = "1"
    section_key: str
    items: list[TranslateItem]
    impl: dict[str, Any] = Field(default_factory=dict)


class Translator(Protocol):
    impl_version: str

    def translate(self, section_key: str, blocks: list[TranslateBlock], *, target_lang: str, context: dict[str, Any]) -> TranslateOutcome:
        ...


class UnavailableTranslator:
    impl_version = "unavailable"

    def translate(self, section_key: str, blocks: list[TranslateBlock], *, target_lang: str, context: dict[str, Any]) -> TranslateOutcome:
        raise AdapterUnavailable("⑧ 번역 운영 어댑터가 아직 없다(AI ⑧ 미착수) — 입력/출력 계약은 TranslateOutcome")


# ---------------------------------------------------------------------------------------------------------
# 등록
# ---------------------------------------------------------------------------------------------------------
@dataclass
class Adapters:
    analyzer: Analyzer | None = None
    judge: Judge | None = None
    labeler: Labeler | None = None
    logo: LogoJudge | None = None
    inpainter: Inpainter | None = None
    styler: Styler | None = None
    translator: Translator | None = None
    extras: dict[str, Any] = field(default_factory=dict)


_adapters = Adapters()


def set_adapters(**kw: Any) -> None:
    for k, v in kw.items():
        if not hasattr(_adapters, k):
            raise AttributeError(k)
        setattr(_adapters, k, v)


def reset_adapters() -> None:
    global _adapters
    _adapters = Adapters()


def analyzer() -> Analyzer:
    if _adapters.analyzer is None:
        _adapters.analyzer = PipelineAnalyzer()
    return _adapters.analyzer


def judge() -> Judge:
    if _adapters.judge is None:
        _adapters.judge = UnavailableJudge()
    return _adapters.judge


def labeler() -> Labeler:
    if _adapters.labeler is None:
        _adapters.labeler = PipelineLabeler()
    return _adapters.labeler


def logo() -> LogoJudge:
    if _adapters.logo is None:
        _adapters.logo = PipelineLogo()
    return _adapters.logo


def inpainter() -> Inpainter:
    if _adapters.inpainter is None:
        _adapters.inpainter = PipelineInpainter()
    return _adapters.inpainter


def styler() -> Styler:
    if _adapters.styler is None:
        _adapters.styler = PipelineStyler()
    return _adapters.styler


def translator() -> Translator:
    if _adapters.translator is None:
        _adapters.translator = UnavailableTranslator()
    return _adapters.translator
