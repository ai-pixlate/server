"""⑨ 조판 — 자동 조정과 경계 이탈 판정이 함께 쓰는 측정 해석 · 좌표 계산(순수 계산, 브라우저 없음).

측정은 FE 공통 측정 엔진(`openTypesetMeasurer().measure`, 서버는 FE 내부 렌더 경로 `/internal/typeset-render`)의 결과를 그대로 쓴다.
이 모듈은 그 결과를 읽어 좌표를 비교할 뿐 측정 알고리즘을 새로 만들지 않는다(`contract.md` 6.7 — 서버에서 별도 측정 알고리즘 금지).

- 좌표: 섹션 로컬 원본 px. 박스 `{x, y, w, h}`(renderInput 모양), 글자 경계 `{left, top, right, bottom}`(FE `InkBounds`). 소수를 유지한다.
- 넘침 비교: 경계와 같으면 정상, 넘으면 넘침, 허용 오차 없음(`contract.md` 6.2 · 조판 D-01 · D-02, FE `inkOverflowSides`와 같은 비교).
- 블록 넘침 판정값은 FE 측정 결과의 `overflow`를 그대로 쓴다(`excess`는 양을 보고하기 위한 계산이며 판정을 대신하지 않는다).
- 최종 출력 위치: 포함 섹션만 순서대로 쌓은 누적 높이(FE `buildFinalSectionOffsets`와 같은 규칙, `contract.md` 6.6).

measure(측정 결과 하나)는 FE `TypesetMeasureSuccess | TypesetMeasureFailure` 그대로의 dict 다. 실패(`ok: false`)를 정상으로 바꾸지 않는다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

SIDES = ("left", "right", "top", "bottom")
Box = Mapping[str, float]  # {x, y, w, h}
Ink = Mapping[str, float]  # {left, top, right, bottom}


class SectionMeasurer(Protocol):
    """섹션 하나의 블록들을 FE 공통 측정 엔진으로 잰다.

    section = {"width", "height", "background"(호출자 소유 — 운영은 섹션 배경 PNG 바이트)},
    blocks = renderInput 블록(`app.render_input.RenderBlockInput.api` 모양: blockId · revision · text · box{x,y,w,h} · style 8키).
    반환 = blockId → FE 측정 결과 dict. 측정은 블록마다 독립이다(다른 블록이 결과에 영향을 주지 않는다).
    운영 구현은 BE 몫(서버 HTML 렌더 세션), 검수 도구 구현은 docs/ai-experiments/tools/typeset_auto_adjust_run.py.
    """

    def __call__(self, section: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]: ...


def excess(ink: Ink | None, box: Box) -> dict[str, float]:
    """글자 경계가 박스 밖으로 나간 양(px, 방향별, 0 이상). 경계와 같으면 0. max = 네 방향 최댓값."""
    if not ink:
        return {**{s: 0.0 for s in SIDES}, "max": 0.0}
    ex = {"left": max(0.0, box["x"] - ink["left"]), "right": max(0.0, ink["right"] - (box["x"] + box["w"])),
          "top": max(0.0, box["y"] - ink["top"]), "bottom": max(0.0, ink["bottom"] - (box["y"] + box["h"]))}
    ex["max"] = max(ex.values())
    return ex


def sides_of(ex: Mapping[str, float]) -> dict[str, bool]:
    """excess → 방향별 넘침(0보다 크면 넘침). any 포함."""
    s = {k: ex[k] > 0 for k in SIDES}
    return {**s, "any": any(s.values())}


def section_box(width: float, height: float) -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "w": float(width), "h": float(height)}


def ink_rect(ink: Ink) -> dict[str, float]:
    return {"x": ink["left"], "y": ink["top"], "w": ink["right"] - ink["left"], "h": ink["bottom"] - ink["top"]}


def intersects(a: Box, b: Box) -> bool:
    """두 사각형이 넓이를 갖고 겹치는가(맞닿음은 겹침이 아니다)."""
    return a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]


def union(a: Box, b: Box) -> dict[str, float]:
    x0, y0 = min(a["x"], b["x"]), min(a["y"], b["y"])
    x1, y1 = max(a["x"] + a["w"], b["x"] + b["w"]), max(a["y"] + a["h"], b["y"] + b["h"])
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


@dataclass(frozen=True)
class Measured:
    """FE 측정 결과 하나를 읽은 값. raw 는 원본 dict(기록 · 검수용으로 그대로 둔다)."""
    ok: bool
    box: dict[str, float]
    ink: dict[str, float] | None
    overflow: dict[str, bool]      # FE 판정 그대로(any 포함)
    excess: dict[str, float]       # 양 — 판정은 overflow 를 쓴다
    lines: list[str]
    font_px: float | None
    reason: str | None
    raw: dict[str, Any]

    @property
    def over(self) -> bool:
        """넘침 — 측정 실패는 넘침으로 다룬다(조정 성공으로 채택하지 않는다)."""
        return not self.ok or bool(self.overflow.get("any"))

    @property
    def sides(self) -> list[str]:
        return [s for s in SIDES if self.overflow.get(s)] if self.ok else []


def read_measure(raw: Mapping[str, Any], box: Box) -> Measured:
    """FE 측정 결과 dict → Measured. box 는 이 측정에 넣은 박스(측정 결과의 bbox 와 같아야 한다)."""
    b = {k: float(box[k]) for k in ("x", "y", "w", "h")}
    if not raw or not raw.get("ok"):
        reason = (raw or {}).get("reason") or "no-result"
        return Measured(False, b, None, {**{s: False for s in SIDES}, "any": False}, excess(None, b), [], None, str(reason), dict(raw or {}))
    ink = raw.get("ink")
    ink = {k: float(ink[k]) for k in ("left", "top", "right", "bottom")} if ink else None
    ov = raw.get("overflow") or {}
    overflow = {**{s: bool(ov.get(s)) for s in SIDES}, "any": bool(ov.get("any"))}
    lines = [str(ln.get("text", "")) for ln in raw.get("lines") or []]
    return Measured(True, b, ink, overflow, excess(ink, b), lines, raw.get("fontPx"), None, dict(raw))


def occupied(block_id: int, box: Box, ink: Ink | None) -> list[dict[str, Any]]:
    """블록이 섹션에서 차지하는 자리 = 배치 박스 ∪ 그린 글자(박스 밖으로 넘친 글자도 자리를 차지한다)."""
    out = [{"blockId": block_id, **{k: float(box[k]) for k in ("x", "y", "w", "h")}}]
    if ink:
        out.append({"blockId": block_id, **ink_rect(ink)})
    return out


def free_limits(box: Box, obstacles: Sequence[Mapping[str, Any]], sec_w: float, sec_h: float, gap: float,
                ignore: set[int]) -> dict[str, float]:
    """박스를 각 방향으로 넓힐 수 있는 한계 좌표 — 장애물(다른 블록이 차지한 자리)에서 gap 만큼 떨어진 곳 또는 섹션 경계.
    가로는 지금 박스의 세로 범위와 겹치는 장애물, 세로는 가로 범위와 겹치는 장애물만 본다. 한계는 지금 박스보다 안쪽으로 오지 않는다
    (이미 gap 보다 가까운 이웃이 있으면 그 방향으로는 넓히지 않는다는 뜻)."""
    x0, x1, y0, y1 = box["x"], box["x"] + box["w"], box["y"], box["y"] + box["h"]
    lim = {"left": 0.0, "right": float(sec_w), "top": 0.0, "bottom": float(sec_h), "limitedBy": {s: None for s in SIDES}}
    for o in obstacles:
        if o["blockId"] in ignore:
            continue
        ox0, ox1, oy0, oy1 = o["x"], o["x"] + o["w"], o["y"], o["y"] + o["h"]
        if y0 < oy1 and oy0 < y1:
            if ox0 >= x1 and ox0 - gap < lim["right"]:
                lim["right"], lim["limitedBy"]["right"] = ox0 - gap, o["blockId"]
            if ox1 <= x0 and ox1 + gap > lim["left"]:
                lim["left"], lim["limitedBy"]["left"] = ox1 + gap, o["blockId"]
        if x0 < ox1 and ox0 < x1:
            if oy0 >= y1 and oy0 - gap < lim["bottom"]:
                lim["bottom"], lim["limitedBy"]["bottom"] = oy0 - gap, o["blockId"]
            if oy1 <= y0 and oy1 + gap > lim["top"]:
                lim["top"], lim["limitedBy"]["top"] = oy1 + gap, o["blockId"]
    lim["left"], lim["right"] = min(lim["left"], x0), max(lim["right"], x1)
    lim["top"], lim["bottom"] = min(lim["top"], y0), max(lim["bottom"], y1)
    return lim


def final_offsets(sections: Sequence[Mapping[str, Any]]) -> tuple[dict[Any, float], float]:
    """최종 출력 이미지에서 각 포함 섹션 원점의 세로 위치와 전체 높이 — FE `buildFinalSectionOffsets`와 같은 규칙.
    sections = [{"id", "order", "height", "include"}]. 순서는 order, 제외 섹션은 offset 이 없다. 잘못된 높이 · 중복은 추측하지 않고 실패."""
    ids, orders = set(), set()
    for s in sections:
        h = s["height"]
        if not isinstance(h, (int, float)) or not math.isfinite(h) or h < 0:
            raise ValueError(f"섹션 {s['id']}의 높이가 잘못됐다: {h}")
        if s["id"] in ids or s["order"] in orders:
            raise ValueError(f"섹션 id · 순서 중복: {s['id']} · {s['order']}")
        ids.add(s["id"])
        orders.add(s["order"])
    offsets: dict[Any, float] = {}
    cursor = 0.0
    for s in sorted(sections, key=lambda s: s["order"]):
        if not s["include"]:
            continue
        offsets[s["id"]] = cursor
        cursor += float(s["height"])
    return offsets, cursor


def measure_blocks(measure: Callable[..., dict[int, dict[str, Any]]], section: Mapping[str, Any],
                   blocks: Sequence[Mapping[str, Any]]) -> dict[int, Measured]:
    """SectionMeasurer 로 재고 Measured 로 읽는다. 결과가 없는 블록은 측정 실패."""
    raw = measure(section, blocks)
    return {int(b["blockId"]): read_measure(raw.get(int(b["blockId"])) or {}, b["box"]) for b in blocks}
