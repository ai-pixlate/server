"""고정 입력 명세(input_manifest)·인계 본문의 정규 직렬화와 지문.

근거: integration-decisions.md 5.32 "input_manifest의 직렬화는 RFC 8785(JCS) 정규 JSON 후 SHA-256".
JCS 구현은 AI 인계 계층과 같은 `pipeline.handoff.canonical` 하나만 쓴다(PR #55 BE 확인 2-h). 구현이 둘이면 실수·큰 정수에서
지문이 갈려 BE가 고정한 지문과 AI가 계산한 지문을 대조할 수 없다.

- 객체 키: UTF-16 코드 단위 순(JCS 3.2.3). NULL 과 키 누락을 구분하고 배열 순서를 보존한다.
- 문자열: 정규화(NFC 등)를 하지 않는다 — source_ko 의 LF·Unicode 를 그대로 둔다. 짝 없는 서로게이트는 여기서 먼저 거부한다.
- 숫자: ECMAScript Number::toString 표기(실수 포함). NaN·Infinity 는 거부한다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pipeline.handoff import canonical as _jcs


class ManifestError(ValueError):
    pass


def _check(value: Any, where: str) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise ManifestError(f"{where}: 짝 없는 서로게이트 문자는 직렬화하지 않는다")
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                _check(k, where)
            _check(v, f"{where}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check(v, f"{where}[{i}]")


def canonical_json(value: Any) -> str:
    _check(value, "$")
    try:
        return _jcs.canonical_json(value)
    except _jcs.CanonicalError as e:
        raise ManifestError(str(e)) from e


def fingerprint(value: Any) -> str:
    """정규 직렬화(UTF-8)의 SHA-256 16진수."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def stable_json(value: Any) -> str:
    """실수를 포함할 수 있는 인계 본문용 결정적 직렬화(키 정렬·공백 없음·NaN 거부). JCS 와 다르며 BE 내부 비교에만 쓴다."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()
