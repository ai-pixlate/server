"""⑦ 결과 시험 도우미 — 스키마 2(현재) 결과로 같은 계산의 스키마 1(2026-11-05 이전) 결과를 만든다.

스키마 2의 `align_diag.lines`는 이전 규칙(블록 안 줄 분산)의 계산 결과를 그대로 담는다. 그 값을 스키마 1의 정렬 · 근거 · 진단 자리에
옮기면 이전 코드가 같은 입력에서 냈을 결과가 된다(색 · 크기 · 영역 · 지문은 두 형식이 같다). 배포 전에 계산된 결과를 흉내 내는 데만 쓴다.
"""
from __future__ import annotations

import copy
from typing import Any


def to_schema_v1(result: dict[str, Any]) -> dict[str, Any]:
    """스키마 2 결과 dict(model_dump(mode="json")) → 스키마 1 dict. 입력은 바꾸지 않는다."""
    assert result["schema_version"] == "2", result.get("schema_version")
    out = copy.deepcopy(result)
    out["schema_version"] = "1"
    out["config"] = {k: out["config"][k] for k in ("method", "em_ratio", "align_tolerance")}
    for b in out["blocks"]:
        d = b.pop("align_diag")
        if d is None:  # 제외 블록
            b["align_diag"] = None
            continue
        ln = d["lines"]
        b["align"], b["align_basis"] = ln["align"], ln["basis"]
        reasons = {k: v for k, v in b["null_reasons"].items() if k != "align"}
        if ln["null_reason"] is not None:
            reasons["align"] = ln["null_reason"]
        b["null_reasons"] = reasons
        b["align_diag"] = {k: ln[k] for k in ("std_left", "std_center", "std_right", "tolerance_px", "candidates")}
    return out
