"""⑦ 결과 시험 도우미 — 현재(스키마 3) 결과로 같은 계산의 이전 형식 결과를 만든다.

- 스키마 2(2026-11-05 ~ 2026-11-26, 굵기 없음): 스키마 3에서 굵기 필드(블록 font_weight · weight_diag · 사유, 영역 glyph_h · stroke_ratio,
  설정 weight_*)만 뺀 것이다 — 색 · 크기 · 정렬 · 영역 · 지문은 두 형식이 같다.
- 스키마 1(2026-11-05 이전, 블록 안 줄 정렬): 스키마 2의 `align_diag.lines`는 이전 규칙(블록 안 줄 분산)의 계산 결과를 그대로 담는다.
  그 값을 스키마 1의 정렬 · 근거 · 진단 자리에 옮기면 이전 코드가 같은 입력에서 냈을 결과가 된다.
배포 전에 계산된 결과를 흉내 내는 데만 쓴다.
"""
from __future__ import annotations

import copy
from typing import Any


def to_schema_v2(result: dict[str, Any]) -> dict[str, Any]:
    """스키마 3 결과 dict(model_dump(mode="json")) → 스키마 2 dict. 입력은 바꾸지 않는다."""
    assert result["schema_version"] == "3", result.get("schema_version")
    out = copy.deepcopy(result)
    out["schema_version"] = "2"
    out["config"] = {k: v for k, v in out["config"].items() if not k.startswith("weight_")}
    for b in out["blocks"]:
        del b["font_weight"], b["weight_diag"]
        b["null_reasons"] = {k: v for k, v in b["null_reasons"].items() if k != "font_weight"}
        for r in b["regions"]:
            del r["glyph_h"], r["stroke_ratio"]
    return out


def to_schema_v1(result: dict[str, Any]) -> dict[str, Any]:
    """현재(스키마 3) 또는 스키마 2 결과 dict → 스키마 1 dict. 입력은 바꾸지 않는다."""
    out = to_schema_v2(result) if result.get("schema_version") == "3" else copy.deepcopy(result)
    assert out["schema_version"] == "2", out.get("schema_version")
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
