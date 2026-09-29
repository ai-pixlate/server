"""VLM·LLM 호출 — ① 섹션 분해의 긴 구간 경계 선택 [pipeline.md 단계표 ①] · ③ `llm_assist` [pipeline.md 7.2절].

③ 호출자(`GeminiMergeAssistant` · `ReplayMergeAssistant`)는 파일 끝에 있다. 실패는 ①과 같은 `VlmError`로 전파한다
(open-questions #37). ③ 호출자는 시간 제한(`merge.llm_timeout_s`)을 SDK `HttpOptions.timeout`으로 넘기고, 재시도 옵션은
주지 않는다 — 설치된 google-genai 2.24.0은 `retry_options`가 없으면 1회만 시도한다.

아래는 ① 설명이다.

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
from dataclasses import dataclass
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


def image_sha256(image: Image.Image) -> str:
    """VLM에 실제로 넘기는 이미지(눈금 띠 포함)의 픽셀 해시. 같은 크기라도 내용이 다르면 다르다."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    return hashlib.sha256(rgb.tobytes()).hexdigest()[:16]


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
    구간·창 좌표(`expect()`로 미리 받음) · 입력 이미지 픽셀 해시 · 크기 · 프롬프트 해시와 맞는지 확인한다.
    색 전환 경계가 달라져 구간·창이 바뀌면 그 자리에서 멈추고, 기록이 다 쓰이지 않으면 `finish()`가 멈춘다
    (호출 구성이 달라진 비교를 성공으로 오인하지 않게).
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
        self._meta: dict[str, Any] | None = None

    @classmethod
    def from_file(cls, path: str | Path, expect_input: dict[str, Any], expect_config: dict[str, Any]) -> "ReplayBoundaryPicker":
        record = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(record, expect_input, expect_config)

    @property
    def remaining(self) -> int:
        return len(self._calls) - self._pos

    def expect(self, meta: dict[str, Any]) -> None:
        """다음 호출의 구간·창 좌표(원본 기준). `vlm_boundaries`가 호출 직전에 알려 준다."""
        self._meta = meta

    def finish(self) -> None:
        """모든 호출이 끝난 뒤. 기록이 남아 있으면 호출 구성이 달라진 것이므로 실패."""
        if self.remaining:
            raise VlmReplayMismatch(f"재생 기록 {self.remaining}회가 쓰이지 않았다 — 구간·창 구성이 기록과 다르다")

    def __call__(self, image: Image.Image, prompt: str) -> list[int]:
        if self._pos >= len(self._calls):
            raise VlmReplayMismatch(f"재생할 기록이 남아 있지 않다 (기록 {len(self._calls)}회, 현재 {self._pos + 1}번째 호출)")
        call = self._calls[self._pos]
        self._pos += 1
        expect = {
            "segment": (self._meta or {}).get("segment"),
            "abs_window": (self._meta or {}).get("abs_window"),
            "input_size": list(image.size),
            "image_sha256": image_sha256(image),
            "prompt_sha256": prompt_sha256(prompt),
        }
        self._meta = None
        got = {k: call.get(k) for k in expect}
        if got != expect:
            diff = {k: (got[k], expect[k]) for k in expect if got[k] != expect[k]}
            raise VlmReplayMismatch(f"{self._pos}번째 호출 불일치 (기록, 현재): {diff} — 구간·창·입력 이미지·프롬프트가 달라졌다")
        return list(call["raw"])


# ---------------------------------------------------------------------------
# ③ llm_assist 호출자 [pipeline.md 7.2절 · open-questions #41]
# ---------------------------------------------------------------------------
ROLES_FOR_SCHEMA = ["title", "body", "caption", "price", "caution"]

MERGE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "members": {"type": "array", "items": {"type": "string"}},
                    "role": {"type": "string", "enum": ROLES_FOR_SCHEMA},
                },
                "required": ["members", "role"],
            },
        }
    },
    "required": ["blocks"],
}


def sha256_text(text: str) -> str:
    """③ 기록용 전체 SHA-256(①의 `prompt_sha256`은 16자로 줄인 값이라 따로 둔다)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class LlmReply:
    """③ 호출 응답. text는 원응답 그대로(비어 있을 수 있다 — 검증에서 걸린다), usage는 토큰 사용량."""

    text: str
    usage: dict[str, Any] | None = None


class MergeAssistant(Protocol):
    config: dict[str, Any]

    def __call__(self, prompt: str, payload: str) -> LlmReply: ...


class GeminiMergeAssistant:
    """google-genai로 ③ 병합 묶음·역할 JSON을 받는 기본 호출자. 호출 1회 = 섹션 1개."""

    def __init__(self, model: str, temperature: float, timeout_s: float) -> None:
        self.config = {"model": model, "temperature": temperature, "timeout_s": timeout_s}
        self._client = None

    def _client_or_raise(self):
        if self._client is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise VlmError(f"환경변수 {API_KEY_ENV}가 없다 — llm_assist를 실행할 수 없다")
            try:
                from google import genai  # noqa: PLC0415
                from google.genai import types as gtypes  # noqa: PLC0415
            except ImportError as e:
                raise VlmError("google-genai가 설치되지 않았다 (pipeline/requirements.txt)", e) from e
            timeout_ms = int(round(self.config["timeout_s"] * 1000))
            self._client = genai.Client(api_key=key, http_options=gtypes.HttpOptions(timeout=timeout_ms))
        return self._client

    def __call__(self, prompt: str, payload: str) -> LlmReply:
        try:
            client = self._client_or_raise()
            from google.genai import types as gtypes  # noqa: PLC0415

            config = gtypes.GenerateContentConfig(
                temperature=self.config["temperature"],
                response_mime_type="application/json",
                response_schema=MERGE_RESPONSE_SCHEMA,
                automatic_function_calling=gtypes.AutomaticFunctionCallingConfig(disable=True),
            )
            resp = client.models.generate_content(model=self.config["model"], contents=[prompt, payload], config=config)
            text = resp.text or ""
            um = getattr(resp, "usage_metadata", None)
            usage = um.model_dump(mode="json", exclude_none=True) if um is not None and hasattr(um, "model_dump") else None
        except VlmError:
            raise
        except Exception as e:  # noqa: BLE001 — 실패 분류는 #37 결정 전에는 하지 않는다(시간 초과 포함)
            raise VlmError(f"LLM 호출 실패 model={self.config['model']}", e) from e
        return LlmReply(text=text, usage=usage)


class ReplayMergeAssistant:
    """이전 `merge_debug/<section_key>.json` 기록의 응답을 재생한다. API를 부르지 않는다.

    모델 설정 · 프롬프트 SHA-256 · 페이로드 SHA-256이 기록과 같아야 하고, 재생한 응답은 실제 호출과 같은 검증을 거친다
    (검증은 호출하는 쪽 — merge.assist). 호출 실패 기록은 같은 실패로 재생한다. 기록 1개 = 호출 1회.
    """

    def __init__(self, record: dict[str, Any], expect_config: dict[str, Any]) -> None:
        got = record.get("model_config")
        if got != expect_config:
            raise VlmReplayMismatch(f"모델 설정 불일치 (기록, 현재): {got}, {expect_config}")
        self.record = record
        self.config = expect_config
        self.used = False

    @classmethod
    def from_file(cls, path: str | Path, expect_config: dict[str, Any]) -> "ReplayMergeAssistant":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), expect_config)

    def __call__(self, prompt: str, payload: str) -> LlmReply:
        if self.used:
            raise VlmReplayMismatch("재생 기록은 호출 1회분이다")
        self.used = True
        expect = {"prompt_sha256": sha256_text(prompt), "payload_sha256": sha256_text(payload)}
        diff = {k: (self.record.get(k), v) for k, v in expect.items() if self.record.get(k) != v}
        if diff:
            raise VlmReplayMismatch(f"재생 입력 불일치 (기록, 현재): {diff} — 프롬프트나 휴리스틱 결과가 달라졌다")
        if self.record.get("status") == "call_failed":
            raise VlmError(f"기록된 호출 실패를 재생: {self.record.get('error')}")
        text = self.record.get("response_text")
        if text is None:
            raise VlmReplayMismatch(f"재생 기록에 응답이 없다 (status={self.record.get('status')!r})")
        return LlmReply(text=text, usage=self.record.get("usage"))

    def finish(self) -> None:
        """실행이 끝난 뒤. 호출이 있어야 하는 기록이 쓰이지 않았으면 입력이 달라진 것이므로 실패."""
        if not self.used and self.record.get("status") != "skipped":
            raise VlmReplayMismatch("재생 기록이 쓰이지 않았다 — 이번 실행은 LLM에 보낼 블록이 없었다")
