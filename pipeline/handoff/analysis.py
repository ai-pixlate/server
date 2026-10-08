"""①②③ 초기 분석 인계 — `analyze()` → `AnalyzeResult` 버전 1 [통합 D3 · D9-1 · 5.31 · 5.32].

BE ocr 워커가 호출한다. 입력 원본은 워커가 준비한 절대 로컬 경로와 SHA-256으로 받고, 결과는 AnalyzeResult v1(payload)과
섹션 이미지 산출물 목록으로 돌려준다. 업로드 · DB 저장 · 임시 키→DB id 변환은 BE가 한다.

- ① 분해 실패는 원본 전체 한 섹션으로 대체하고 `split_fallbacks`에 남긴다(후속 OCR · 병합은 그대로 수행). AnalyzeResult 형식은 바꾸지 않는다.
- 그 밖의 실패는 failed 보고. 계약 8장의 AnalyzeError 코드(IMAGE_OPEN_FAILED · OCR_FAILED)는 payload.analyze_error에 그대로 싣는다.
- 섹션 이미지는 다시 열어 크기 · 형식 · 해시를 재고, 출력 폴더 밖 · 크기 불일치면 output_invalid — 부분 결과를 완료로 내지 않는다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from pipeline import config as cfgmod
from pipeline.analyze import analyze
from pipeline.errors import AnalyzeError
from pipeline.handoff.canonical import sha256_file
from pipeline.stages import merge
from pipeline.handoff.envelope import (
    CONTRACT_VERSION,
    HandoffInputError,
    RequestIdentity,
    StageReport,
    _Model,
    artifact_of,
    check_expected_manifest,
    failed_report,
    identity_fallback,
    manifest_of,
    parse_identity,
)
from pipeline.types import SCHEMA_VERSION, SourceImage
from pipeline.vlm import BoundaryPicker, MergeAssistant, VlmError

ANALYZE_ADAPTER_VERSION = "analyze-handoff@2026-10-08.1"
REPO_ROOT = Path(__file__).resolve().parents[2]


class SourceInput(_Model):
    source_image_id: int
    upload_order: int
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _abs(cls, v: str) -> str:
        if not Path(v).is_absolute():
            raise ValueError(f"원본 경로는 절대 경로여야 한다: {v!r}")
        return v


class AnalyzeRequest(RequestIdentity):
    stage: Literal["analyze"]
    sources: list[SourceInput] = Field(min_length=1)
    out_dir: str  # 이번 시도 전용 새 폴더(절대 경로). 있으면 비어 있어야 한다
    use_llm: bool = True  # ③ llm_assist. 운영 기본 True

    @field_validator("out_dir")
    @classmethod
    def _abs(cls, v: str) -> str:
        if not Path(v).is_absolute():
            raise ValueError(f"out_dir는 절대 경로여야 한다: {v!r}")
        return v


def _prompt_sha(path_value: str) -> str | None:
    p = Path(path_value)
    p = p if p.is_absolute() else REPO_ROOT / p
    return sha256_file(p) if p.is_file() else None


def _manifest(req: AnalyzeRequest, cfg: dict[str, Any]) -> dict[str, Any]:
    snap = cfgmod.snapshot(cfg)
    return {
        "stage": "analyze", "contract_version": CONTRACT_VERSION, "adapter": ANALYZE_ADAPTER_VERSION,
        "sources": [{"source_image_id": s.source_image_id, "upload_order": s.upload_order, "sha256": s.sha256}
                    for s in sorted(req.sources, key=lambda s: s.upload_order)],
        "use_llm": req.use_llm,
        "config": {k: snap[k] for k in ("section", "ocr", "merge")},
        "prompts": {"section": _prompt_sha(cfg["section"]["prompt_path"]),
                    "merge": _prompt_sha(cfg["merge"]["prompt_path"]) if req.use_llm else None},
    }


def _check_inputs(req: AnalyzeRequest) -> None:
    ids = [s.source_image_id for s in req.sources]
    orders = [s.upload_order for s in req.sources]
    if len(set(ids)) != len(ids) or len(set(orders)) != len(orders):
        raise HandoffInputError("source_image_id 또는 upload_order가 중복이다")
    for s in req.sources:
        p = Path(s.path)
        if not p.is_file():
            raise HandoffInputError(f"원본 파일이 없다: {s.path}")
        got = sha256_file(p)
        if got != s.sha256:
            raise HandoffInputError(f"원본 {s.source_image_id} SHA-256 불일치 — 선언 {s.sha256[:12]}… · 실제 {got[:12]}…")
    out = Path(req.out_dir)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise HandoffInputError(f"out_dir가 비어 있지 않다(이전 시도 파일과 섞이지 않게 새 폴더를 준다): {out}")


def run_analyze(raw: Any, cfg: dict[str, Any], *, llm: MergeAssistant | None = None, vlm: BoundaryPicker | None = None,
                ocr_engine: Any | None = None) -> StageReport:
    """BE 요청(JSON dict) → 보고. 예외를 밖으로 던지지 않고 실패도 보고로 돌려준다(BaseException은 제외)."""
    try:
        ident = parse_identity(raw, "analyze")
    except (ValidationError, HandoffInputError) as e:
        return failed_report(identity_fallback(raw, "analyze"), "input_invalid", f"요청 식별 오류: {e}")
    try:
        req = AnalyzeRequest.model_validate(raw)
    except ValidationError as e:
        return failed_report(ident, "input_invalid", f"요청 형식 오류: {e}")
    impl = {"adapter": ANALYZE_ADAPTER_VERSION, "analyze_result_schema": SCHEMA_VERSION,
            "models": {"section_vlm": cfg["section"]["vlm_model"], "ocr_det": cfg["ocr"]["det_model"], "ocr_rec": cfg["ocr"]["rec_model"],
                       "merge_llm": cfg["merge"]["llm_model"] if req.use_llm else None}}
    try:
        _check_inputs(req)
        manifest, msha = manifest_of(_manifest(req, cfg))
        check_expected_manifest(ident, msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)

    out = Path(req.out_dir)
    sources = [SourceImage(source_image_id=s.source_image_id, upload_order=s.upload_order, path=s.path) for s in req.sources]
    fallbacks: list[dict[str, Any]] = []

    def fail(kind: str, msg: str, *, retryable: bool | None = None, payload: dict | None = None) -> StageReport:
        return failed_report(ident, kind, msg, retryable=retryable, manifest=manifest, target_count=len(sources), payload=payload,
                             implementation=impl, diagnostics={"split_fallbacks": fallbacks})

    try:
        res = analyze(sources, cfg, out, use_llm=req.use_llm, llm=llm, vlm=vlm, fallbacks=fallbacks, ocr_engine=ocr_engine)
    except AnalyzeError as e:
        kind = "input_invalid" if e.code == "IMAGE_OPEN_FAILED" else "model_call_failed"
        return fail(kind, str(e), retryable=e.retryable, payload={"analyze_error": e.to_dict()})
    except NotImplementedError as e:  # ② 4,000px 초과 섹션(#31 · #32 미구현)
        return fail("unsupported_input", f"{e}")
    except VlmError as e:  # ③ llm_assist 호출 · 응답 검증 실패 — 휴리스틱 대체는 승인되지 않았다(#37)
        kind = "response_invalid" if isinstance(e, merge.LlmResponseError) else "model_call_failed"
        return fail(kind, f"③ 병합 보정 실패: {e}")
    except ValueError as e:  # 설정 · API 키 없음
        return fail("config_invalid", f"{e}")
    except Exception as e:  # noqa: BLE001
        return fail("internal", f"{e.__class__.__name__}: {e}")

    artifacts = []
    root = out.resolve()
    try:
        for sec in res.sections:
            p = Path(sec.image_path).resolve()
            if root not in p.parents:
                raise ValueError(f"{sec.section_key}: 섹션 이미지가 out_dir 밖이다 {p}")
            a = artifact_of("section_image", sec.section_key, p)
            if (a.width, a.height) != (sec.width, sec.height):
                raise ValueError(f"{sec.section_key}: 이미지 크기 {(a.width, a.height)} ≠ 섹션 ({sec.width}, {sec.height})")
            artifacts.append(a)
    except Exception as e:  # noqa: BLE001
        return fail("output_invalid", f"섹션 이미지 검증 실패: {e}")
    known = {s.section_key for s in res.sections}
    orphan = [b.block_key for b in res.blocks if b.section_key not in known]
    if orphan:
        return fail("output_invalid", f"섹션이 없는 블록 {orphan}")
    summary = {
        "sections": len(res.sections), "blocks": len(res.blocks),
        "empty_blocks": sum(1 for b in res.blocks if not b.source_ko.strip()),
        "sections_without_blocks": len(known - {b.section_key for b in res.blocks}),
        "warnings": [w.code for w in res.warnings], "split_fallbacks": len(fallbacks),
    }
    return StageReport(
        execution_id=ident.execution_id, attempt_id=ident.attempt_id, stage="analyze", input_snapshot_id=ident.input_snapshot_id,
        input_manifest=manifest, input_manifest_sha256=msha, outcome="completed", target_count=len(sources),
        payload={"analyze_result": res.model_dump(mode="json"), "split_fallbacks": fallbacks, "summary": summary},
        artifacts=artifacts, implementation=impl,
    )
