"""analyze() — 초기 분석: ① 섹션 분해 → ② 텍스트 추출 → ③ 병합·역할 분류 [계약 1.2].

수행하지 않음: 스타일 추출 · 섹션 판정 · 정책 적용 · 라벨 · 로고.
입력은 워커가 준비한 로컬 경로. 실패는 AnalyzeError로 던지고, 텍스트 0개는 경고로 남긴다 [계약 8장].
③ 설정은 ① 전에 검증한다(잘못된 설정으로 섹션 분해·OCR을 돌리지 않게). block_key는 ③이 섹션 안에서 매긴 번호를
최종 순서(upload_order → section_order → block_order)로 실행 전체에서 한 번 다시 매긴다(dev.md 3절, open-questions #38 H).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.stages import merge, ocr, section_split
from pipeline.types import AnalyzeResult, AnalyzeWarning, SourceImage, block_key


def analyze(sources: list[SourceImage], cfg: dict[str, Any], out_dir: Path) -> AnalyzeResult:
    merge.validate_config(cfg)
    sections = []
    blocks = []
    warnings: list[AnalyzeWarning] = []
    for src in sorted(sources, key=lambda s: s.upload_order):
        split = section_split.run(src, cfg, out_dir / "sections")
        text_found = False
        for sec in split.sections:
            ocr_res = ocr.run(sec, cfg)
            text_found = text_found or bool(ocr_res.regions)
            merged = merge.run(sec, ocr_res, cfg)
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
    blocks = [b.model_copy(update={"block_key": block_key(i)}) for i, b in enumerate(blocks, start=1)]
    return AnalyzeResult(sections=sections, blocks=blocks, warnings=warnings)
