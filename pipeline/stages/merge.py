"""③ 줄·문단 병합 + 역할 분류 — `heuristic_v2` → `llm_assist` [pipeline.md 단계표 ③].

- 휴리스틱으로 영역 → 줄 → 문단(블록)을 만들고, LLM은 추가 병합과 역할 재판정만 한다. 분할 금지.
- 블록 1건 = 문단 1개. role 5종. ocr_confidence = 구성 영역 score 최솟값(types.ocr_confidence_of).
- 원시 영역의 식별자·좌표·score를 source_lines에 그대로 보존한다 [계약 2.5].

미구현. LLM 프롬프트는 pipeline/prompts/merge_assist.md(미작성).
"""
from __future__ import annotations

from typing import Any

from pipeline.types import MergeResult, OcrResult, Section


def run(section: Section, ocr: OcrResult, cfg: dict[str, Any]) -> MergeResult:
    raise NotImplementedError("③ 병합·역할 분류(heuristic_v2 + llm_assist) 미구현 — docs/ai/status.md")
