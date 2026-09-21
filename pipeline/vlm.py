"""VLM 호출 — ① 섹션 분해의 긴 구간 경계 선택 [pipeline.md 단계표 ①].

- 모델·온도는 config `[section]`(`vlm_model` · `vlm_temperature`)에서 받는다. API 키는 환경변수
  `GEMINI_API_KEY` [dev.md 6절]. 키는 커밋하지 않는다.
- 응답은 JSON `{"boundaries": [y, ...]}`로 강제한다. y는 전달한 이미지의 픽셀 세로 좌표다.
- 실패 처리는 open-questions #25 미정이다. 오류 코드·재시도 정책이 정해지기 전까지 이 모듈은
  실패를 `VlmError`로 감싸 그대로 전파하고, 대체 처리(색 전환 경계만으로 진행)를 하지 않는다.
  `AnalyzeError`로 변환하지도 않는다 — 코드가 계약 8장에 없다.
- `google-genai`는 호출 시점에만 import한다. 키 없이 도는 로컬 테스트는 가짜 호출자를 주입한다
  (`section_split.run(..., vlm=...)`).
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol

from PIL import Image

API_KEY_ENV = "GEMINI_API_KEY"

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"boundaries": {"type": "array", "items": {"type": "integer"}}},
    "required": ["boundaries"],
}


class VlmError(Exception):
    """VLM 호출·응답 실패. #25 미정 — 코드·재시도 정책 결정 전까지 AnalyzeError로 바꾸지 않는다."""

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(message if cause is None else f"{message}: {cause}")


class BoundaryPicker(Protocol):
    def __call__(self, image: Image.Image, prompt: str) -> list[int]: ...


def parse_boundaries(text: str) -> list[int]:
    """응답 본문(JSON)에서 정수 y 목록을 꺼낸다. 형식이 다르면 VlmError."""
    try:
        data = json.loads(text)
        ys = data["boundaries"]
        if not isinstance(ys, list) or not all(isinstance(y, int) and not isinstance(y, bool) for y in ys):
            raise TypeError("boundaries는 정수 배열이어야 한다")
    except (ValueError, KeyError, TypeError) as e:
        raise VlmError(f"VLM 응답 형식 오류: {text[:200]!r}", e) from e
    return sorted(set(ys))


class GeminiBoundaryPicker:
    """google-genai로 경계 y 목록을 받는 기본 호출자."""

    def __init__(self, model: str, temperature: float) -> None:
        self.model = model
        self.temperature = temperature
        self._client = None

    def _client_or_raise(self):
        if self._client is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise VlmError(f"환경변수 {API_KEY_ENV}가 없다 — VLM 경계 선택을 실행할 수 없다")
            try:
                from google import genai  # noqa: PLC0415 — 로컬 기본 환경에서는 필요 없을 수 있다
            except ImportError as e:
                raise VlmError("google-genai가 설치되지 않았다 (pipeline/requirements.txt)", e) from e
            self._client = genai.Client(api_key=key)
        return self._client

    def __call__(self, image: Image.Image, prompt: str) -> list[int]:
        try:
            client = self._client_or_raise()  # 키 확인 · SDK import · Client 생성(프록시 설정 오류 등)까지 감싼다
            from google.genai import types as gtypes  # noqa: PLC0415

            resp = client.models.generate_content(
                model=self.model,
                contents=[prompt, image],
                config=gtypes.GenerateContentConfig(
                    temperature=self.temperature,
                    response_mime_type="application/json",
                    response_schema=_RESPONSE_SCHEMA,
                ),
            )
            text = resp.text
        except VlmError:
            raise
        except Exception as e:  # noqa: BLE001 — SDK 예외 종류를 #25 결정 전에는 구분하지 않는다
            raise VlmError(f"VLM 호출 실패 model={self.model}", e) from e
        if not text:
            raise VlmError("VLM 응답이 비어 있다")
        return parse_boundaries(text)
