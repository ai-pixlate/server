"""VLM 호출 — ① 섹션 분해의 긴 구간 경계 선택 [pipeline.md 단계표 ①].

- 모델·온도는 config `[section]`(`vlm_model` · `vlm_temperature`)에서 받는다. API 키는 환경변수
  `GEMINI_API_KEY` [dev.md 6절]. 키는 커밋하지 않는다.
- 응답은 JSON `{"boundaries": [y, ...]}`로 강제한다. y는 전달한 이미지의 픽셀 세로 좌표다.
- 실패 처리는 open-questions #25 미정이다. 오류 코드·재시도 정책이 정해지기 전까지 이 모듈은
  실패를 `VlmError`로 감싸 그대로 전파하고, 대체 처리(색 전환 경계만으로 진행)를 하지 않는다.
  `AnalyzeError`로 변환하지도 않는다 — 코드가 계약 8장에 없다.
- `google-genai`는 호출 시점에만 import한다. 키 없이 도는 로컬 테스트는 가짜 호출자를 주입한다
  (`section_split.run(..., vlm=...)`).
- `seed`는 실험용이다. 기본(None)은 보내지 않는다. 고정해도 동일 응답이 보장되지는 않는다(SDK 문서).
- `ReplayBoundaryPicker`는 이전 실행의 `split_debug.json`에 기록된 응답을 순서대로 재생한다(실험용). 원본·VLM 설정·
  창별 입력 크기·프롬프트가 기록과 다르면 `VlmReplayMismatch`로 멈춘다. seed 효과를 재는 도구가 아니다.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
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


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


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

    def __init__(self, model: str, temperature: float, seed: int | None = None) -> None:
        self.model = model
        self.temperature = temperature
        self.seed = seed
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

            config = gtypes.GenerateContentConfig(
                temperature=self.temperature,
                response_mime_type="application/json",
                response_schema=_RESPONSE_SCHEMA,
                # 도구 호출을 쓰지 않는데 SDK가 매 호출 "AFC ... not recommended" 경고를 stderr에 찍는다. 끈다.
                automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(disable=True),
            )
            if self.seed is not None:
                config.seed = self.seed  # 실험용. 기본은 보내지 않는다
            resp = client.models.generate_content(model=self.model, contents=[prompt, image], config=config)
            text = resp.text
        except VlmError:
            raise
        except Exception as e:  # noqa: BLE001 — SDK 예외 종류를 #25 결정 전에는 구분하지 않는다
            raise VlmError(f"VLM 호출 실패 model={self.model}", e) from e
        if not text:
            raise VlmError("VLM 응답이 비어 있다")
        return parse_boundaries(text)


class VlmReplayMismatch(VlmError):
    """재생 기록과 현재 실행의 입력·설정·호출 순서가 다르다. 실험 도구 오류이며 #25와 무관하다."""


class ReplayBoundaryPicker:
    """이전 `split_debug.json`의 VLM 응답을 순서대로 재생한다. API를 부르지 않는다.

    생성 시 원본 지문(`input`)과 VLM 설정(`vlm_config`)이 기록과 같은지 확인하고, 호출마다 다음 기록의
    입력 크기·프롬프트 해시와 맞는지 확인한다. 색 전환 경계가 달라져 구간·창이 바뀌면 그 자리에서 멈춘다.
    """

    def __init__(self, record: dict[str, Any], expect_input: dict[str, Any], expect_config: dict[str, Any]) -> None:
        for key, expect in (("input", expect_input), ("vlm_config", expect_config)):
            got = record.get(key)
            if got is None:
                raise VlmReplayMismatch(f"재생 기록에 {key!r}가 없다 — 이 기록은 재생을 지원하지 않는 버전에서 만들어졌다")
            diff = {k: (got.get(k), expect.get(k)) for k in set(got) | set(expect) if got.get(k) != expect.get(k)}
            if diff:
                raise VlmReplayMismatch(f"{key} 불일치 (기록, 현재): {diff}")
        self._calls: list[dict[str, Any]] = [c for seg in record.get("vlm", []) for c in seg.get("calls", [])]
        self._pos = 0

    @classmethod
    def from_file(cls, path: str | Path, expect_input: dict[str, Any], expect_config: dict[str, Any]) -> "ReplayBoundaryPicker":
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(record, expect_input, expect_config)

    @property
    def remaining(self) -> int:
        return len(self._calls) - self._pos

    def __call__(self, image: Image.Image, prompt: str) -> list[int]:
        if self._pos >= len(self._calls):
            raise VlmReplayMismatch(f"재생할 기록이 남아 있지 않다 (기록 {len(self._calls)}회, 현재 {self._pos + 1}번째 호출)")
        call = self._calls[self._pos]
        self._pos += 1
        expect = {"input_size": list(image.size), "prompt_sha256": prompt_sha256(prompt)}
        got = {k: call.get(k) for k in expect}
        if got != expect:
            raise VlmReplayMismatch(f"{self._pos}번째 호출 불일치 (기록, 현재): {got} vs {expect} — 구간·창·프롬프트가 달라졌다")
        return list(call["raw"])
