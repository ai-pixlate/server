"""analyze() — 초기 분석: ① 섹션 분해 → ② 텍스트 추출 → ③ 병합·역할 분류 [계약 1.2].

수행하지 않음: 스타일 추출 · 섹션 판정 · 정책 적용 · 라벨 · 로고.
입력은 워커가 준비한 로컬 경로. 실패는 AnalyzeError로 던지고, 텍스트 0개는 경고로 남긴다 [계약 8장].
③ 설정(휴리스틱, use_llm이면 LLM 설정 · API 키까지)은 ① 전에 검증한다(잘못된 설정으로 섹션 분해·OCR을 돌리지 않게).
block_key는 ③이 섹션 안에서 매긴 번호를 최종 순서(upload_order → section_order → block_order)로 실행 전체에서 한 번
다시 매긴다(dev.md 3절, open-questions #38 H). use_llm이면 out_dir/merge_debug/에 섹션별 llm_assist 기록과
(section_key, 섹션 최종 블록 키) → 실행 최종 블록 키 대응(block_keys.json)을 남긴다(pipeline.md 7.2절).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.stages import merge, ocr, section_split
from pipeline.types import AnalyzeResult, AnalyzeWarning, SourceImage, SplitResult, block_key
from pipeline.vlm import BoundaryPicker, MergeAssistant, VlmError

SPLIT_FALLBACK_KIND = "section_split_whole_image"  # ① 분해 실패 → 원본 전체 한 섹션(D9-1 · #25). 인계 기록용 이름이며 API 코드가 아니다


def split_or_whole(
    src: SourceImage, cfg: dict[str, Any], out_dir: Path, *, vlm: BoundaryPicker | None = None,
    fallbacks: list[dict[str, Any]] | None = None,
) -> SplitResult:
    """① 실행. VLM 경계 선택이 실패하면(VlmError) 원본 전체를 한 섹션으로 대체한다 [통합 D9-1 · #25].

    원본을 열 수 없으면 대체 섹션도 만들 수 없으므로 IMAGE_OPEN_FAILED 그대로다. 대체는 성공으로 숨기지 않고 `fallbacks`에 원본 ·
    사유를 남긴다(호출자가 실행 기록 · 인계에 싣는다). 후속 OCR · 병합은 대체 섹션에도 그대로 수행한다."""
    try:
        return section_split.run(src, cfg, out_dir) if vlm is None else section_split.run(src, cfg, out_dir, vlm=vlm)
    except VlmError as e:
        im = section_split.open_source(src)
        result = section_split.crop_sections(src, im, [], out_dir)
        if fallbacks is not None:
            fallbacks.append({"kind": SPLIT_FALLBACK_KIND, "source_image_id": src.source_image_id,
                              "cause": f"{e.__class__.__name__}: {e}"})
        return result


def analyze(
    sources: list[SourceImage], cfg: dict[str, Any], out_dir: Path, *, use_llm: bool = True,
    llm: MergeAssistant | None = None, vlm: BoundaryPicker | None = None, fallbacks: list[dict[str, Any]] | None = None,
    ocr_engine: Any | None = None,
) -> AnalyzeResult:
    """use_llm은 ③ merge.run에 그대로 전달한다(CLI analyze --no-llm → False). llm · vlm · ocr_engine은 테스트·실험용 호출자 주입.
    fallbacks에 list를 주면 ① 대체(원본 전체 섹션) 기록을 받는다. AnalyzeResult 버전 1 형식은 바꾸지 않는다(D3)."""
    merge.validate_config(cfg)
    debug_dir = out_dir / "merge_debug"
    recorder = None
    if use_llm:
        merge.validate_llm_config(cfg, need_api_key=llm is None)
        llm = llm if llm is not None else merge.default_assistant(cfg)
        recorder = merge.json_recorder(debug_dir)
    sections = []
    blocks = []
    warnings: list[AnalyzeWarning] = []
    for src in sorted(sources, key=lambda s: s.upload_order):
        split = split_or_whole(src, cfg, out_dir / "sections", vlm=vlm, fallbacks=fallbacks)
        text_found = False
        for sec in split.sections:
            ocr_res = ocr.run(sec, cfg) if ocr_engine is None else ocr.run(sec, cfg, engine=ocr_engine)
            text_found = text_found or bool(ocr_res.regions)
            merged = merge.run(sec, ocr_res, cfg, use_llm=use_llm, llm=llm, recorder=recorder)
            sections.append(sec)
            blocks.extend(merged.blocks)
        if not text_found:
            warnings.append(
                AnalyzeWarning(
                    code="NO_TEXT_DETECTED",
                    source_image_id=src.source_image_id,
                    message="원본 전체에서 텍스트가 검출되지 않음",
                )
            )
    # 섹션 안 번호 → 실행 전체 번호. 이 시점까지 block_key를 참조하는 결과는 없다(③-1 이후 단계가 새 키를 받는다).
    renumbered = [b.model_copy(update={"block_key": block_key(i)}) for i, b in enumerate(blocks, start=1)]
    if use_llm:
        keymap = [
            {"section_key": b.section_key, "section_block_key": b.block_key, "run_block_key": r.block_key}
            for b, r in zip(blocks, renumbered)
        ]
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "block_keys.json").write_text(json.dumps(keymap, ensure_ascii=False, indent=2), encoding="utf-8")
    return AnalyzeResult(sections=sections, blocks=renumbered, warnings=warnings)
