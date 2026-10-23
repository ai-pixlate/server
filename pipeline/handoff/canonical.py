"""입력 지문 — RFC 8785(JCS) 정규 JSON + SHA-256 [통합 5.32 `input_manifest` 직렬화].

- 키는 UTF-16 코드 단위 순으로 정렬하고 공백 없이 쓴다. 문자열은 정규화하지 않는다(source_ko의 LF · Unicode 그대로, 5.31).
- 숫자는 ECMAScript Number::toString 규칙. NaN · Infinity는 거부한다(JSON에 없는 값).
- NULL과 키 누락을 구분하고 배열 순서를 보존한다. 파일은 경로가 아니라 내용 SHA-256으로 넣는다(호출자 책임).
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


class CanonicalError(ValueError):
    """JCS로 표현할 수 없는 값(NaN · Infinity · 모르는 타입 · 문자열이 아닌 키)."""


def _number(v: float) -> str:
    if not math.isfinite(v):
        raise CanonicalError(f"JSON에 없는 수치 {v!r}")
    if v == 0:
        return "0"
    if v.is_integer() and abs(v) < 2**53:  # 정확히 표현되는 정수만. 더 큰 값은 ES처럼 짧은 자릿수 + 0으로 쓴다
        return str(int(v))
    sign = "-" if v < 0 else ""
    # repr는 왕복 가능한 가장 짧은 자릿수 — ECMAScript와 같은 자릿수 집합이다. 표기만 ES 규칙으로 바꾼다
    mantissa, _, exp = repr(abs(v)).partition("e")
    int_part, _, frac = mantissa.partition(".")
    digits = (int_part + frac).lstrip("0") or "0"
    lead_zeros = len(int_part + frac) - len((int_part + frac).lstrip("0"))
    point = len(int_part) + (int(exp) if exp else 0) - lead_zeros  # 소수점 위치 n (값 = 0.digits × 10^n)
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= point <= 21:
        return sign + digits + "0" * (point - k)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * (-point) + digits
    e = point - 1
    mant = digits[0] + ("." + digits[1:] if k > 1 else "")
    return f"{sign}{mant}e{'+' if e >= 0 else '-'}{abs(e)}"


def _encode(v: Any, out: list[str]) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, int):
        out.append(str(v))
    elif isinstance(v, float):
        out.append(_number(v))
    elif isinstance(v, str):
        out.append(json.dumps(v, ensure_ascii=False))
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, x in enumerate(v):
            if i:
                out.append(",")
            _encode(x, out)
        out.append("]")
    elif isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            raise CanonicalError(f"객체 키는 문자열이어야 한다: {[k for k in v if not isinstance(k, str)]!r}")
        out.append("{")
        for i, k in enumerate(sorted(v, key=lambda s: s.encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(json.dumps(k, ensure_ascii=False))
            out.append(":")
            _encode(v[k], out)
        out.append("}")
    else:
        raise CanonicalError(f"JCS로 직렬화할 수 없는 타입 {type(v).__name__}")


def canonical_json(value: Any) -> str:
    out: list[str] = []
    _encode(value, out)
    return "".join(out)


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
