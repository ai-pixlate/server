"""text_block.layout_adjust 읽기·쓰기 — 셀러 배치 조정(계약 blockPatch 「블록 배치」)과 자동 조정 값을 나눠 담는다.

저장 모양(열 NULL = 아무 조정 없음):
    {"schema_version": 2,
     "user":   {"layout_bbox": {x,y,w,h} | null, "font_scale": 양수 | null} | null,   # 셀러 조정
     "system": {"font_scale": 양수, "line_break_applied": bool} | null,               # 자동 조정 — 2차에는 쓰는 코드가 없다
     "legacy": 출처를 알 수 없는 이전 값,                                              # 있을 때만
     "legacy_released": true}                                                         # 셀러가 박스를 명시적으로 해제한 뒤에만

- 응답: `layoutAdjust` = user, `autoAdjust` = system. legacy 는 응답에 내지 않는다.
- 렌더: 적용 박스 = user 박스, 없으면 기본 박스(원본 bbox). system 은 렌더에 적용하지 않는다(자동 조정 적용은 3차).
- legacy 는 출처를 정하지 않고 이전 서버의 적용 규칙만 유지한다: 그때 그려지던 layout_bbox 는 기본 박스로 계속 그리고, 그때 입력 오류이던 값은
  계속 입력 오류다(호출자가 RENDER_INPUT_INVALID 로 알린다). 셀러 조정이 아니므로 `layoutAdjust`·content.csv 에는 나오지 않는다.
- 셀러가 박스를 명시적으로 해제하면(`layoutAdjust: null`·`box: null`·원본 bbox 전송) 계약대로 적용 박스는 원본 bbox 다. legacy 값은 지우지 않고
  legacy_released 로 표시만 한다. 해제와 무관한 저장(번역문만·fontScale 만)은 legacy 적용을 그대로 둔다.
- 이전 `auto_adjust` 값의 변환 규칙은 그 마이그레이션(rename auto_adjust → layout_adjust) 머리말.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

SCHEMA_VERSION = 2
BOX_KEYS = ("x", "y", "w", "h")
_TOP_KEYS = {"schema_version", "user", "system", "legacy", "legacy_released"}
_USER_KEYS = {"layout_bbox", "font_scale", "updated_at"}
_SYSTEM_KEYS = {"font_scale", "line_break_applied"}


def is_number(v: Any) -> bool:
    """유한한 JSON 수인가(불리언·문자열 제외)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return not isinstance(v, float) or math.isfinite(v)


def _box_ok(v: Any) -> bool:
    return isinstance(v, dict) and set(v) == set(BOX_KEYS) and all(is_number(v[k]) for k in BOX_KEYS)


def _scale_ok(v: Any) -> bool:
    return is_number(v) and v > 0


def _base_box(legacy: Any, bbox: Any) -> Any:
    """셀러 박스가 없을 때의 적용 박스. legacy 는 이전 서버의 규칙 그대로 읽는다 — 객체가 아니면 없음(None), layout_bbox 가 있으면 그 값,
    없으면 원본 bbox. 좌표 형식 검사는 호출자가 원본 bbox 와 같은 기준으로 한다. 형식이 깨진 v2 는 이전 규칙이 없는 값이라 없음(None)."""
    if legacy is None:
        return bbox
    if not isinstance(legacy, dict):
        return None
    if "layout_bbox" not in legacy and legacy.get("schema_version") == SCHEMA_VERSION:
        return None
    box = legacy.get("layout_bbox")
    if box is not None and not isinstance(box, dict):
        return None
    return box or bbox


@dataclass(frozen=True)
class LayoutState:
    user_box: dict[str, Any] | None = None
    user_scale: float | None = None
    system: dict[str, Any] | None = None
    legacy: Any = None
    legacy_released: bool = False

    @property
    def applied_scale(self) -> float:
        return self.user_scale if self.user_scale is not None else 1.0

    def placement_source(self, bbox: Any) -> Any:
        """적용 박스의 원천. 정할 수 없으면 None."""
        if self.user_box is not None:
            return self.user_box
        return bbox if self.legacy_released else _base_box(self.legacy, bbox)


EMPTY = LayoutState()


def parse(raw: Any) -> LayoutState:
    """저장값 → 상태. 지금 형식이 아니면 값 전체를 legacy 로 본다(지우거나 셀러 값으로 해석하지 않는다).
    드라이버가 JSONB 문자열 값을 파이썬 문자열로 주므로, 문자열은 JSON 문서로 읽어 보고 안 되면 값 그대로 둔다."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            pass
    if raw is None:
        return EMPTY
    if not (isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION and set(raw) <= _TOP_KEYS):
        return LayoutState(legacy=raw)
    released = raw.get("legacy_released", False)
    if not isinstance(released, bool) or (released and raw.get("legacy") is None):
        return LayoutState(legacy=raw)
    user, system = raw.get("user"), raw.get("system")
    box, scale = None, None
    if user is not None:
        if not isinstance(user, dict) or not set(user) <= _USER_KEYS:
            return LayoutState(legacy=raw)
        box, scale = user.get("layout_bbox"), user.get("font_scale")
        if (box is not None and not _box_ok(box)) or (scale is not None and not _scale_ok(scale)):
            return LayoutState(legacy=raw)
    if system is not None:
        if not isinstance(system, dict) or not set(system) <= _SYSTEM_KEYS:
            return LayoutState(legacy=raw)
        if ("font_scale" in system and not _scale_ok(system["font_scale"])) or (
                "line_break_applied" in system and not isinstance(system["line_break_applied"], bool)):
            return LayoutState(legacy=raw)
    return LayoutState(user_box=box, user_scale=scale, system=system or None, legacy=raw.get("legacy"), legacy_released=released)


def dump(state: LayoutState) -> dict[str, Any] | None:
    """상태 → 저장값. 셀러 조정을 지워도 system·legacy 는 지우지 않는다. 아무것도 없으면 None(열 NULL)."""
    has_user = state.user_box is not None or state.user_scale is not None
    if not has_user and state.system is None and state.legacy is None:
        return None
    out: dict[str, Any] = {"schema_version": SCHEMA_VERSION,
                           "user": {"layout_bbox": state.user_box, "font_scale": state.user_scale} if has_user else None,
                           "system": state.system}
    if state.legacy is not None:
        out["legacy"] = state.legacy
    if state.legacy_released:
        out["legacy_released"] = True
    return out


def layout_view(state: LayoutState) -> dict[str, Any] | None:
    """계약 `TextBlock.layoutAdjust` — 저장된 셀러 조정. 없으면 null."""
    if state.user_box is None and state.user_scale is None:
        return None
    return {"box": state.user_box, "fontScale": state.user_scale}


def auto_view(state: LayoutState) -> dict[str, Any] | None:
    """계약 `TextBlock.autoAdjust` — 자동 조정 결과(저장 키 → 응답 키 매핑). 없으면 null."""
    if state.system is None:
        return None
    out: dict[str, Any] = {}
    if "font_scale" in state.system:
        out["fontScale"] = state.system["font_scale"]
    if "line_break_applied" in state.system:
        out["lineBreakApplied"] = state.system["line_break_applied"]
    return out or None


def apply_patch(state: LayoutState, patch: dict[str, Any] | None, bbox: Any) -> LayoutState:
    """요청 `layoutAdjust` 를 적용하고 2차 정규화한 새 상태. patch None = 셀러 조정 전부 해제, 안쪽 키 생략 = 유지,
    안쪽 null = 그 값만 해제. 배율 1.0 → null, 원본 bbox 와 같은 box → null(필드별로 따로 판단).
    박스를 해제하면(null·원본 bbox 전송) 적용 박스는 원본 bbox 다 — legacy 박스가 다시 살아나지 않게 표시한다.
    legacy 가 적용 박스에 영향을 주지 않으면 표시하지 않는다(적용값이 같으므로 no-op 이어야 한다)."""
    box_sent = patch is None or "box" in patch
    box = None if patch is None else (patch["box"] if box_sent else state.user_box)
    scale = None if patch is None else (patch["fontScale"] if "fontScale" in patch else state.user_scale)
    if scale == 1:
        scale = None
    if box is not None and isinstance(bbox, dict) and all(box[k] == bbox.get(k) for k in BOX_KEYS):
        box = None
    released = state.legacy_released
    if box_sent and box is None and state.legacy is not None and _base_box(state.legacy, bbox) != bbox:
        released = True
    return LayoutState(box, scale, state.system, state.legacy, released)


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def box_range_errors(box: dict[str, Any], width: Any, height: Any) -> list[str]:
    """범위 위반 설명(허용 오차 없이 십진수로 비교). 계약: 0 ≤ x, 0 ≤ y, w > 0, h > 0, x + w ≤ 섹션 폭, y + h ≤ 섹션 높이."""
    x, y, w, h = (_dec(box[k]) for k in BOX_KEYS)
    errs = []
    if x < 0:
        errs.append(f"x = {x} is negative")
    if y < 0:
        errs.append(f"y = {y} is negative")
    if w <= 0:
        errs.append(f"w = {w} must be positive")
    if h <= 0:
        errs.append(f"h = {h} must be positive")
    if x + w > _dec(width):
        errs.append(f"x + w = {x + w} exceeds section width {width}")
    if y + h > _dec(height):
        errs.append(f"y + h = {y + h} exceeds section height {height}")
    return errs


def csv_values(state: LayoutState) -> list[Any]:
    """content.csv 배치 5열(layout_x·layout_y·layout_w·layout_h·font_scale) — 셀러 조정값. 조정하지 않은 항목은 None."""
    box = state.user_box or {}
    return [box.get(k) for k in BOX_KEYS] + [state.user_scale]


def canonical_number(v: Any) -> Any:
    """같은 요청 판별용 — 10 과 10.0 은 같은 수다. 안쪽 값까지 정수 값은 int 로 맞춘다."""
    if isinstance(v, dict):
        return {k: canonical_number(x) for k, x in v.items()}
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v
