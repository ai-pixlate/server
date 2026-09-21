"""② 텍스트 추출 — PaddleOCR PP-OCRv5 [pipeline.md 단계표 ②].

- 전처리 없음. 4,000px 초과 섹션은 OCR 전 임시 분할(여백 우선 · 없으면 2,000px 띠 300px 겹침).
- 타일 좌표는 섹션 로컬로 복원하고 겹침 중복을 정리한 뒤 OcrResult로 돌려준다 [계약 3.2].
- 임시 분할은 처리 단위일 뿐 섹션으로 저장하지 않는다.
- 텍스트 0개는 오류가 아니다(regions=[]). 타임아웃·일시 오류는 OCR_FAILED(재시도 가능).

미구현. 의존성은 pipeline/requirements-ocr.txt (CPU로 로컬 실행 가능).
"""
from __future__ import annotations

from typing import Any

from pipeline.types import OcrResult, Section


def run(section: Section, cfg: dict[str, Any]) -> OcrResult:
    raise NotImplementedError("② 텍스트 추출(PaddleOCR) 미구현 — docs/ai/status.md")
