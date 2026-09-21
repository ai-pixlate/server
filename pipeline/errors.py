"""분석 함수의 오류 반환 — contract.md 8장.

AnalyzeError(code, retryable, message, source_image_id)
- retryable은 워커가 소비한다. 재시도 여부는 retryable + 오류 코드별 정책 + 남은 허용 횟수로 워커가 판단한다.
- 텍스트 0개 검출은 오류가 아니다(빈 blocks + 경고).
"""
from __future__ import annotations

# 오류 코드 → 재시도 가능 여부 [계약 8장]
ERROR_POLICY: dict[str, bool] = {
    "IMAGE_OPEN_FAILED": False,  # 깨진 파일 · 미지원 포맷
    "OCR_FAILED": True,  # OCR 타임아웃 · 일시 오류 (허용 횟수 내)
}


class AnalyzeError(Exception):
    """초기 분석(①~③) 실패. 워커가 job_async_task.error_code/error_message에 기록한다."""

    def __init__(
        self,
        code: str,
        message: str,
        source_image_id: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        if retryable is None:
            if code not in ERROR_POLICY:
                raise ValueError(f"알 수 없는 오류 코드 {code!r}: retryable을 명시하거나 ERROR_POLICY에 추가한다")
            retryable = ERROR_POLICY[code]
        self.code = code
        self.retryable = retryable
        self.message = message
        self.source_image_id = source_image_id
        super().__init__(f"{code}: {message} (source_image_id={source_image_id}, retryable={retryable})")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "retryable": self.retryable,
            "message": self.message,
            "source_image_id": self.source_image_id,
        }


def image_open_failed(path: str, source_image_id: int | None, cause: BaseException | None = None) -> AnalyzeError:
    detail = f": {cause}" if cause else ""
    return AnalyzeError("IMAGE_OPEN_FAILED", f"이미지를 열 수 없음 {path}{detail}", source_image_id)


def ocr_failed(section_key: str, source_image_id: int | None, cause: BaseException | None = None) -> AnalyzeError:
    detail = f": {cause}" if cause else ""
    return AnalyzeError("OCR_FAILED", f"OCR 실패 section={section_key}{detail}", source_image_id)
