"""analyze() — 초기 분석: ① 섹션 분해 → ② 텍스트 추출 → ③ 병합·역할 분류 [계약 1.2].

수행하지 않음: 스타일 추출 · 섹션 판정 · 정책 적용 · 라벨 · 로고.
입력은 워커가 준비한 로컬 경로. 실패는 AnalyzeError로 던지고, 텍스트 0개는 경고로 남긴다 [계약 8장].
③ 설정(휴리스틱, use_llm이면 LLM 설정 · API 키까지)은 ① 전에 검증한다(잘못된 설정으로 섹션 분해·OCR을 돌리지 않게).
block_key는 ③이 섹션 안에서 매긴 번호를 최종 순서(upload_order → section_order → block_order)로 실행 전체에서 한 번
다시 매긴다(dev.md 3절, open-questions #38 H). use_llm이면 out_dir/merge_debug/에 섹션별 llm_assist 기록과
(section_key, 섹션 최종 블록 키) → 실행 최종 블록 키 대응(block_keys.json)을 남긴다(pipeline.md 7.2절).
② 분할 설정도 ① 전에 검증한다. 높이 > ocr.split_threshold_px 섹션(① 대체 섹션 포함)은 타일 OCR로 처리하고
out_dir/ocr_debug/<section_key>.json에 타일 진단을 남긴다 — 실패(OCR_FAILED)해도 남긴다(pipeline.md 7.9절). 짧은 섹션은
호출별 중복 제거 기록이 있을 때만 남긴다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pipeline.stages import merge, ocr, section_split
from pipeline.types import AnalyzeResult, AnalyzeWarning, SourceImage, SplitResult, block_key
from pipeline.vlm import BoundaryPicker, MergeAssistant, VlmError

SPLIT_FALLBACK_KIND = "section_split_whole_image"  # ① 분해 실패 → 원본 전체 한 섹션(D9-1 · #25). 인계 기록용 이름이며 API 코드가 아니다

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalyzeProgress:
    """analyze() 진행 이벤트. source_index · region_index는 1부터. 영역은 ① 섹션 단위이며 타일은 세지 않는다."""
    event: str  # split_start | split_done | ocr_start | merge_start | region_done
    source_index: int
    source_total: int
    source_image_id: int
    region_index: int | None
    region_count_in_source: int | None
    regions_done: int
    regions_known: int
    sources_split: int


ProgressCallback = Callable[[AnalyzeProgress], None]


def _emit(progress: ProgressCallback | None, **kw: Any) -> None:
    if progress is None:
        return
    try:
        progress(AnalyzeProgress(**kw))
    except Exception:
        _log.warning("analyze progress callback failed (event=%s)", kw.get("event"), exc_info=True)


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


def _ocr_section(sec, cfg: dict[str, Any], engine: Any | None, debug_dir: Path):
    """② 실행. 타일 섹션(또는 호출별 중복 제거 기록이 있는 짧은 섹션)이면 성공 · 실패와 관계없이 진단을
    debug_dir/<section_key>.json에 쓴다."""
    diag: dict[str, Any] = {}
    try:
        return ocr.run(sec, cfg, diag=diag) if engine is None else ocr.run(sec, cfg, engine=engine, diag=diag)
    finally:
        if diag:
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"{sec.section_key}.json").write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")


def analyze(
    sources: list[SourceImage], cfg: dict[str, Any], out_dir: Path, *, use_llm: bool = True,
    llm: MergeAssistant | None = None, vlm: BoundaryPicker | None = None, fallbacks: list[dict[str, Any]] | None = None,
    ocr_engine: Any | None = None, progress: ProgressCallback | None = None,
) -> AnalyzeResult:
    """use_llm은 ③ merge.run에 그대로 전달한다(CLI analyze --no-llm → False). llm · vlm · ocr_engine은 테스트·실험용 호출자 주입.
    fallbacks에 list를 주면 ① 대체(원본 전체 섹션) 기록을 받는다. AnalyzeResult 버전 1 형식은 바꾸지 않는다(D3).
    progress를 주면 원본 · 영역 단계마다 AnalyzeProgress를 넘긴다(콜백 예외는 경고로 남기고 무시한다)."""
    merge.validate_config(cfg)
    ocr.validate_config(cfg)
    debug_dir = out_dir / "merge_debug"
    recorder = None
    if use_llm:
        merge.validate_llm_config(cfg, need_api_key=llm is None)
        llm = llm if llm is not None else merge.default_assistant(cfg)
        recorder = merge.json_recorder(debug_dir)
    sections = []
    blocks = []
    warnings: list[AnalyzeWarning] = []
    source_total = len(sources)
    regions_done = 0
    regions_known = 0
    sources_split = 0
    for source_index, src in enumerate(sorted(sources, key=lambda s: s.upload_order), start=1):
        def emit(event: str, region_index: int | None, region_count: int | None) -> None:
            _emit(progress, event=event, source_index=source_index, source_total=source_total,
                  source_image_id=src.source_image_id, region_index=region_index, region_count_in_source=region_count,
                  regions_done=regions_done, regions_known=regions_known, sources_split=sources_split)

        emit("split_start", None, None)
        split = split_or_whole(src, cfg, out_dir / "sections", vlm=vlm, fallbacks=fallbacks)
        n = len(split.sections)
        sources_split += 1
        regions_known += n
        emit("split_done", None, n)
        text_found = False
        for region_index, sec in enumerate(split.sections, start=1):
            emit("ocr_start", region_index, n)
            ocr_res = _ocr_section(sec, cfg, ocr_engine, out_dir / "ocr_debug")
            text_found = text_found or bool(ocr_res.regions)
            emit("merge_start", region_index, n)
            merged = merge.run(sec, ocr_res, cfg, use_llm=use_llm, llm=llm, recorder=recorder)
            sections.append(sec)
            blocks.extend(merged.blocks)
            regions_done += 1
            emit("region_done", region_index, n)
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
