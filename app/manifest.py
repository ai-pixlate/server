"""고정 입력 명세(input_manifest)·인계 본문의 정규 직렬화와 지문.

근거: integration-decisions.md 5.32 "input_manifest의 직렬화는 RFC 8785(JCS) 정규 JSON 후 SHA-256".
이 모듈은 BE가 만드는 명세에 필요한 JCS 부분집합만 지원한다.

- 객체 키: UTF-16 코드 단위 순으로 정렬(JCS 3.2.3). 중복 키 없음(파이썬 dict).
- 문자열: JSON.stringify 와 같은 이스케이프(", \\, \\b \\f \\n \\r \\t, 그 밖의 제어문자는 \\u00xx). 비ASCII는 그대로.
  정규화(NFC 등)를 하지 않는다 — source_ko 의 LF·Unicode 를 그대로 둔다. 짝 없는 서로게이트는 거부한다.
- 숫자: 정수만. 실수는 JCS의 ES6 숫자 표기를 맞추기 어려워 받지 않는다(명세에는 해시·ID·버전·개수만 넣는다).
- NULL 과 키 누락을 구분한다(값이 None 이면 null 로 남는다).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class ManifestError(ValueError):
    pass


def _utf16_key(s: str) -> bytes:
    return s.encode("utf-16-be", "surrogatepass")


def _check_str(s: str, where: str) -> None:
    for ch in s:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise ManifestError(f"{where}: 짝 없는 서로게이트 문자는 직렬화하지 않는다")


def _encode(value: Any, where: str, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        out.append(str(value))
    elif isinstance(value, float):
        raise ManifestError(f"{where}: 실수는 명세에 넣지 않는다(정수·문자열로 표현) — {value!r}")
    elif isinstance(value, str):
        _check_str(value, where)
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, v in enumerate(value):
            if i:
                out.append(",")
            _encode(v, f"{where}[{i}]", out)
        out.append("]")
    elif isinstance(value, dict):
        for k in value:
            if not isinstance(k, str):
                raise ManifestError(f"{where}: 객체 키는 문자열만 — {k!r}")
            _check_str(k, where)
        out.append("{")
        for i, k in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(json.dumps(k, ensure_ascii=False))
            out.append(":")
            _encode(value[k], f"{where}.{k}", out)
        out.append("}")
    else:
        raise ManifestError(f"{where}: 직렬화할 수 없는 값 {type(value).__name__}")


def canonical_json(value: Any) -> str:
    out: list[str] = []
    _encode(value, "$", out)
    return "".join(out)


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
