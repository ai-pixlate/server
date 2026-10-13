"""분석 함수의 오류 반환 — contract.md 8장.

AnalyzeError(code, retryable, message, source_image_id)
- retryable은 워커가 소비한다. 재시도 여부는 retryable + 오류 코드별 정책 + 남은 허용 횟수로 워커가 판단한다.
- 텍스트 0개 검출은 오류가 아니다(빈 blocks + 경고).

retryable 분류(#33, ② 타일 OCR 통합 범위 — 2026-10-13 사용자 위임 결정) — 오류 코드는 그대로 두고 원인별로 정한다.
같은 입력 · 같은 설정 · 같은 엔진으로 다시 실행해 결과가 바뀔 근거가 있으면 True, 없으면 False다.

| 원인(reason) | 코드 | retryable | 근거 |
|---|---|---|---|
| 이미지 열기 · 크기 불일치 | IMAGE_OPEN_FAILED | False | 입력 자체의 문제 [계약 8장] |
| 엔진 초기화 실패 | OCR_FAILED | True | 모델 내려받기 · 장치 초기화 등 실행 환경의 일시 오류일 수 있다 |
| 추론 예외(타일 · 보완 crop 포함) | OCR_FAILED | True | 시간 초과 · 장치 메모리 등 일시 오류 [계약 8장] |
| 엔진 출력 형식 오류 | OCR_FAILED | True(기존 값 유지) | 타일 범위 밖 — 분류는 #33 후속 |
| 타일 경계 충돌 미해결 | OCR_FAILED | **False** | 보완 OCR까지 거친 판독 충돌이다. OCR은 같은 입력에 같은 결과를 내므로(실측: 같은 이미지 재실행 결과 동일) 자동 재시도로 해소될 근거가 없다 |

reason은 진단용 내부 분류이며 to_dict() · 인계 형식에는 넣지 않는다(외부 오류 코드가 아니다).
"""
from __future__ import annotations

# 오류 코드 → 재시도 가능 여부 기본값 [계약 8장]. 원인별 예외는 위 표 — 생성 함수가 retryable을 명시한다
ERROR_POLICY: dict[str, bool] = {
    "IMAGE_OPEN_FAILED": False,  # 깨진 파일 · 미지원 포맷
    "OCR_FAILED": True,  # OCR 타임아웃 · 일시 오류 (허용 횟수 내)
}
OCR_TILE_CONFLICT = "ocr_tile_conflict"  # 위 표의 '타일 경계 충돌 미해결' reason
OCR_TILE_CONFLICT_RETRYABLE = False


class AnalyzeError(Exception):
    """초기 분석(①~③) 실패. 워커가 job_async_task.error_code/error_message에 기록한다."""

    def __init__(
        self,
        code: str,
        message: str,
        source_image_id: int | None = None,
        retryable: bool | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        if retryable is None:
            if code not in ERROR_POLICY:
                raise ValueError(f"알 수 없는 오류 코드 {code!r}: retryable을 명시하거나 ERROR_POLICY에 추가한다")
            retryable = ERROR_POLICY[code]
        self.code = code
        self.retryable = retryable
        self.message = message
        self.source_image_id = source_image_id
        self.reason = reason
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


def ocr_tile_conflict(section_key: str, source_image_id: int | None, summary: str) -> AnalyzeError:
    """② 타일 OCR에서 보완 OCR 후에도 판독 · 숫자 충돌이 남은 실패. 재시도 불가(위 표)."""
    return AnalyzeError("OCR_FAILED", f"OCR 타일 경계 충돌 미해결 section={section_key} — {summary}", source_image_id,
                        retryable=OCR_TILE_CONFLICT_RETRYABLE, reason=OCR_TILE_CONFLICT)
