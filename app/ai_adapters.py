"""AI 단계 호출 경계 — BE 워커가 부르는 함수와 그 입력·출력 조건.

AI 내부(모델·알고리즘·프롬프트)는 AI 담당이다. BE는 절대 로컬 경로와 고정 입력을 준비하고, 반환값을 검증·저장한다.
AI 함수는 DB·S3에 접근하지 않는다(contract.md 1.1, D3).

| 단계 | 인터페이스 | 기본 구현 |
|---|---|---|
| ①②③ | Analyzer.analyze | PipelineAnalyzer → pipeline.analyze.analyze (실제 호출) |
| ③-1·③-1′ | Judge.judge → JudgeOutcome | HandoffJudge → pipeline.handoff.judgment.run_judgment(PR #55)를 섹션 단위로 불러 변환. 정책 규칙(PIXLATE_POLICY_RULES) 없으면 Unavailable |
| ④ | Labeler.label | PipelineLabeler → pipeline.stages.label.run (실제 호출) |
| ⑤ | LogoJudge.logo | PipelineLogo → pipeline.stages.logo.run (실제 계산) |
| ⑥ | Inpainter(설정·모델 팩토리 제공) | GPU 실행은 pipeline.handoff.gpu.GpuInpaintRunner(app.flows.gpu_worker). PipelineInpainter 는 cfg·model_factory 공급원 |
| ⑦ | Styler.style | PipelineStyler → pipeline.stages.style.run (실제 계산) |
| ⑧ | Translator.translate → TranslateOutcome | HandoffTranslator → pipeline.handoff.translation.run_translate(PR #55). 입력은 context['handoff'](BE 고정 공급) |

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


class AdapterFailed(RuntimeError):
    """AI 인계 보고가 단계 실패(입력·설정·묶음·내부 등 블록별 결과가 없는 실패) 또는 BE 검증 실패. 성공·생략으로 바꾸지 않는다.
    code 는 task error_code(자유 텍스트) 후보이며 API enum 이 아니다."""

    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
        return self.analyze_with_fallbacks(sources, out_dir)[0]

    def analyze_with_fallbacks(self, sources: list[Any], out_dir: Path) -> tuple[Any, list[dict[str, Any]]]:
        """① 분해 실패 시 원본 전체 한 섹션 대체(D9-1)의 기록(원본·사유)을 함께 돌려준다. AnalyzeResult v1 은 바꾸지 않는다(BE 확인 1)."""
        from pipeline.analyze import analyze

        fallbacks: list[dict[str, Any]] = []
        return analyze(sources, self.cfg, out_dir, use_llm=self.use_llm, fallbacks=fallbacks), fallbacks


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
    # AI 인계 요청(AnalyzeResult 섹션·실행 식별)에 필요한 값. BE 워커가 DB에서 채운다
    source_image_id: int | None = None
    section_order: int | None = None
    top_offset: int | None = None
    execution_id: str | None = None
    attempt_id: str | None = None


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


class HandoffJudge:
    """③-1·③-1′ 운영 어댑터 — AI 인계 계층(pipeline.handoff.judgment.run_judgment)을 섹션 하나로 부르고 JudgeOutcome 으로 옮긴다.

    - 사전 묶음은 BE 저장 묶음에서 to_ai_bundle()로 만든 AI 형식이다. 정책 규칙이 없으면 AdapterUnavailable.
    - 보고는 pipeline.handoff.validate.validate_report 로 먼저 검사한다. 어기면 규제 판정 실패(재시도 후보)로 돌린다.
    - 섹션 단위로 부르므로 앞뒤 섹션 문맥은 아직 전달되지 않는다(대상 섹션 지정 입력을 PR #55에 요청, BE 확인 4).
    - AI 보고 failed(묶음·입력·설정·내부) = 규제 판정 실패. 현지 실패·미검사는 섹션 결과로만 남는다(D9-1)."""

    def __init__(self, cfg: dict[str, Any] | None = None, llm: Any | None = None):
        from pipeline.handoff.judgment import JUDGE_ADAPTER_VERSION

        self.cfg = cfg if cfg is not None else _load_cfg()
        self.llm = llm
        self.impl_version = f"{JUDGE_ADAPTER_VERSION}@cfg:{_cfg_hash(self.cfg, 'judge', 'policy')[:16]}"

    def request(self, section: JudgeSection, ai_bundle: dict[str, Any], regulatory_class: str, target_country: str) -> dict[str, Any]:
        from pipeline.handoff.canonical import sha256_file
        from pipeline.handoff.envelope import CONTRACT_VERSION

        missing = [k for k in ("source_image_id", "section_order", "top_offset") if getattr(section, k) is None]
        if missing:
            raise ValueError(f"JudgeSection 에 AI 요청 필드가 없다: {missing}")
        sec = {"section_key": section.section_key, "source_image_id": section.source_image_id, "section_order": section.section_order,
               "top_offset": section.top_offset, "height": section.height, "width": section.width, "image_path": section.image_path}
        blocks = [{"block_key": b.key, "section_key": section.section_key, "block_order": b.block_order, "source_ko": b.source_ko,
                   "source_lines": b.source_lines, "bbox": b.bbox, "role": b.role} for b in section.blocks]
        return {
            "contract_version": CONTRACT_VERSION, "execution_id": section.execution_id or "-", "attempt_id": section.attempt_id or "-",
            "stage": "judge", "target_country": target_country, "regulatory_class": regulatory_class,
            "analyze_result": {"schema_version": "1", "sections": [sec], "blocks": blocks, "warnings": []},
            "section_images": {section.section_key: {"path": section.image_path, "sha256": sha256_file(section.image_path)}},
            "bundle": ai_bundle,
        }

    def judge(self, section: JudgeSection, bundle: dict[str, Any], *, regulatory_class: str, target_country: str) -> JudgeOutcome:
        from app.dictionary_bundle import BundleError, to_ai_bundle
        from pipeline.handoff.judgment import run_judgment
        from pipeline.handoff.validate import validate_report

        try:
            ai_bundle = to_ai_bundle(bundle)
        except BundleError as e:
            raise AdapterUnavailable(f"③-1 판정을 시작할 수 없다: {e}") from e
        req = self.request(section, ai_bundle, regulatory_class, target_country)
        report = run_judgment(req, self.cfg, llm=self.llm)
        problems = validate_report(report, req)
        if problems:
            return _judge_failed(section.section_key, "response_invalid", "AI 보고 검증 실패: " + "; ".join(problems[:10]), True)
        return judge_outcome_from_report(report, section.section_key, ai_bundle["rules"])


def _judge_failed(section_key: str, kind: str, message: str, retryable: bool) -> JudgeOutcome:
    return JudgeOutcome(section_key=section_key,
                        regulatory=ScopeStatus(status="failed", error=f"{kind}: {message}"[:2000], retryable=retryable),
                        local=ScopeStatus(status="not_inspected", error="규제 판정 실패로 현지 판정 결과를 쓰지 않는다"),
                        inspection={"failure": {"kind": kind, "message": message[:2000]}})


def judge_outcome_from_report(report: Any, section_key: str, rules: dict[str, Any]) -> JudgeOutcome:
    """AI StageReport(stage=judge, 섹션 1개) → BE JudgeOutcome. 키는 BE가 준 임시 키(sec_·blk_) 그대로다.

    사전 연결은 audit.links 에서, 판정에 연결되지 않은 원시 후보와 억제 관계는 overlaps 에 보존한다(위반 배지로 쓰지 않음)."""
    from app.manifest import fingerprint

    if report.outcome == "failed":
        f = report.failure
        return _judge_failed(section_key, f.kind, f.message, bool(f.retryable))
    sections = report.payload["sections"]
    if len(sections) != 1 or sections[0]["section_key"] != section_key:
        return _judge_failed(section_key, "response_invalid", f"섹션 결과가 요청과 다르다: {[s['section_key'] for s in sections]}", True)
    r = sections[0]
    audit = r["audit"]
    links = {ln["finding_key"]: ln for ln in audit["links"]}
    m2f = {mk: fk for fk, ln in links.items() for mk in ln["match_keys"]}
    findings = [FindingOut(finding_key=f["finding_key"], content_type=f["content_type"], status=f["status"],
                           evidence_block_keys=list(f["evidence_block_ids"]), evidence_source=f["evidence_source"], reason=f["reason"],
                           dictionary_refs=list(links.get(f["finding_key"], {}).get("external_ids", [])))
                for f in r["content_findings"]["findings"]]
    matches, unlinked = [], []
    for m in audit["matches"]:
        if m["match_key"] in m2f:
            matches.append(MatchOut(match_key=m["match_key"], finding_key=m2f[m["match_key"]], dictionary_ref=m["external_id"],
                                    block_key=m["block_key"], start=m["start"], end=m["end"], matched_text=m["matched_text"]))
        else:
            unlinked.append({"kind": "unlinked_candidate", **m})
    verdicts = [VerdictOut(verdict_key=v["verdict_key"], finding_key=v["finding_key"], dictionary_ref=v["external_id"],
                           verdict_status=v["verdict_status"], bucket=v["bucket"], finding_status=v["finding_status"],
                           problem_text=v["problem_text"])
                for v in r["verdicts"]]
    overlaps = [{"kind": "suppressed", **s} for s in audit["suppressed"]] + unlinked
    local_map = {"completed": "ok", "failed": "failed", "not_checked": "not_inspected"}
    lf = r.get("local_failure") or {}
    local = ScopeStatus(status=local_map[r["local_status"]], error=lf.get("message") if r["local_status"] != "completed" else None)
    impl = dict(report.implementation)
    return JudgeOutcome(
        section_key=section_key, regulatory=ScopeStatus(status="ok"), local=local, findings=findings, matches=matches,
        verdicts=verdicts, overlaps=overlaps,
        inspection={**audit["inspection"], "bucket_recommendation": r["bucket_recommendation"],
                    "recommendation_basis": r["recommendation_basis"],
                    "ai_report": {"contract_version": report.contract_version, "input_manifest_sha256": report.input_manifest_sha256}},
        policy=PolicyInfo(rules_version=rules["rules_version"], rules_sha256=fingerprint(rules), impl_version=str(impl.get("policy"))),
        impl=impl,
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


class HandoffTranslator:
    """⑧ 운영 어댑터 — AI 인계 계층(pipeline.handoff.translation.run_translate)을 부르고 TranslateOutcome 으로 옮긴다.

    BE 워커가 context['handoff'] 에 고정 공급을 싣는다: 분석 실행의 저장 사전 묶음, 섹션 전체 블록(문맥), 대상·revision,
    보존 성공분 참조, 표현 지시, 용어집 공급, 실행 식별. 묶음은 to_ai_bundle()로 AI 형식으로 바꾼다.
    블록별 결과가 있는 실패(부분 실패·응답 구조 실패·호출 실패)는 items 로, 블록별 결과가 없는 실패는 AdapterFailed 로 돌려준다."""

    def __init__(self, cfg: dict[str, Any] | None = None, llm: Any | None = None):
        from pipeline.handoff.translation import TRANSLATE_ADAPTER_VERSION

        self.cfg = cfg if cfg is not None else _load_cfg()
        self.llm = llm
        self.impl_version = f"{TRANSLATE_ADAPTER_VERSION}@cfg:{_cfg_hash(self.cfg, 'translate')[:16]}"

    def request(self, section_key: str, target_lang: str, h: dict[str, Any], ai_bundle: dict[str, Any]) -> dict[str, Any]:
        from pipeline.handoff.envelope import CONTRACT_VERSION

        return {
            "contract_version": CONTRACT_VERSION, "execution_id": h["execution_id"], "attempt_id": h["attempt_id"], "stage": "translate",
            "target_country": h["target_country"], "target_lang": target_lang, "regulatory_class": h["regulatory_class"],
            "section_key": section_key, "blocks": h["blocks"], "targets": h["targets"], "preserved": h["preserved"],
            "instructions": h["instructions"], "glossary": h["glossary"], "bundle": ai_bundle,
        }

    def translate(self, section_key: str, blocks: list[TranslateBlock], *, target_lang: str, context: dict[str, Any]) -> TranslateOutcome:
        from app.dictionary_bundle import BundleError, to_ai_bundle
        from pipeline.handoff.translation import run_translate
        from pipeline.handoff.validate import validate_report

        h = context.get("handoff")
        if h is None:
            raise AdapterUnavailable("⑧ 번역 인계 공급(context['handoff'])이 없다")
        try:
            ai_bundle = to_ai_bundle(h["bundle"])
        except BundleError as e:
            raise AdapterUnavailable(f"⑧ 번역을 시작할 수 없다: {e}") from e
        req = self.request(section_key, target_lang, h, ai_bundle)
        report = run_translate(req, self.cfg, llm=self.llm)
        problems = validate_report(report, req)
        if problems:
            raise AdapterFailed("TRANSLATE_RESULT_INVALID", "AI 보고 검증 실패: " + "; ".join(problems[:10]), retryable=True)
        return translate_outcome_from_report(report, section_key)


def translate_outcome_from_report(report: Any, section_key: str) -> TranslateOutcome:
    """AI StageReport(stage=translate) → TranslateOutcome. 블록별 결과가 있으면(성공·부분 실패·응답/호출 실패) items 로 옮긴다."""
    blocks = (report.payload or {}).get("blocks") or []
    if not blocks:
        if report.outcome == "failed":
            f = report.failure
            raise AdapterFailed(f"TRANSLATE_{f.kind.upper()}", f.message, retryable=bool(f.retryable))
        return TranslateOutcome(section_key=section_key, items=[], impl=dict(report.implementation))
    items = [TranslateItem(key=b["block_key"], status="ok" if b["outcome"] == "completed" else "failed",
                           text=b["trans_1"] if b["outcome"] == "completed" else None,
                           error=None if b["outcome"] == "completed" else (b.get("error") or "번역 실패"))
             for b in blocks]
    impl = {**report.implementation, "input_manifest_sha256": report.input_manifest_sha256,
            "failure": report.failure.model_dump() if report.failure else None,
            "applied_instructions": {b["block_key"]: b.get("applied_instructions") for b in blocks if b.get("applied_instructions")}}
    return TranslateOutcome(section_key=section_key, items=items, impl=impl)


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
        _adapters.judge = HandoffJudge()
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
        _adapters.translator = HandoffTranslator()
    return _adapters.translator
