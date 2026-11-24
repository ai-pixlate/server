"""⑨ 조판 자동 조정 — 원본 규격 경로, 1차 번역 뒤 섹션마다 한 번(A+ 텍스트 슬롯은 대상이 아니다 — 호출자가 거른다).

PM 결정(2026-11-24, 사용자 전달)을 따른다. PR #141 실험 후보(BWES90D)와 다르면 이 결정이 우선한다.
- 순서: 줄바꿈 위치 조정 → 정렬 방향으로 폭 확장 → 마지막으로 최대 −10% 축소. 높이 확장 · 줄 추가 · 자간 조정 · 문구 축약은 하지 않는다.
- 폭 확장 배율 상한은 없다. 섹션 안에서 다른 글과 겹치지 않는 여백까지(이웃과 GAP_PX 간격). 사진 · 그래픽 위 확장은 막지 않는다(이후 과제).
- 두 블록이 같은 빈자리를 차지하지 않는다 — 블록을 하나 조정할 때마다 그 블록이 실제로 차지하는 자리(박스 ∪ 그린 글자)를 장애물에 반영한다.
- 끝까지 넘침이 남으면 시도한 조정을 남기지 않는다 — 조정 전 상태 그대로 두고 넘침과 원인을 보고한다.

설계(구현 결정 — 정책이 정하지 않은 세부):
- 넘침 판정은 FE 측정 엔진의 `overflow.any`(허용 오차 없음). 서버에서는 서버 HTML 렌더(FE 내부 렌더 경로)로 잰 결과를 쓴다.
- 블록 처리 순서: 넘친 블록을 위에서 아래(박스 y), 왼쪽에서 오른쪽(x), blockId 순으로 하나씩. 결과가 입력 순서에 매이지 않고 늘 같다.
- 여백 점유: 다른 조판 블록은 지금 자리(조정했으면 조정 뒤 박스 ∪ 글자, 아니면 원래 박스 ∪ 글자), 조판 대상이 아닌 블록(제외 · 라벨 ·
  로고 · 번역 없음)은 원래 박스(`obstacles`). 원래부터 겹친 이웃도 장애물에서 빼지 않는다 — 다른 블록 자리(간격만큼 넓힌 사각형)와 겹치는
  넓이가 원래 자리(원래 박스 ∪ 원래 글자)보다 커지는 폭은 쓰지 않고(`new_overlaps`), 채택 직전에도 다시 확인한다.
- 줄바꿈: 번역문의 명시적 개행(앞뒤 일반 공백 · 탭 포함)만 공백 하나로 풀어 브라우저 자연 줄바꿈에 맡긴다. NBSP 등 다른 공백 문자 · 글자는
  그대로. 줄 수가 늘거나 넘침이 줄지 않으면 쓰지 않는다.
- 폭 확장: 정렬 방향(left → 오른쪽, right → 왼쪽, center → 양쪽 같은 만큼 — 좁은 쪽 여백까지). 최대 폭에서 다 들어가면 들어가는
  최소 폭(정밀도 WIDTH_PRECISION). 다 들어가지 않으면 — 확장 단독으로 나아지지 않았더라도 — 최대 폭을 쥔 채 축소로 넘기고,
  축소로 해소되면 그 배율에서 들어가는 최소 폭으로 다시 줄인다(원래 폭으로 돌아갈 수도 있다).
- 축소: 시스템 배율을 FLOOR(0.90)까지 내려 들어가는 가장 큰 배율(정밀도 SCALE_PRECISION). 시스템 배율은 셀러 배율과 따로 기록한다.

출력 시스템 조정 = {box, fontScale(시스템 배율), lineBreakApplied}. 저장 계약(`layout_adjust.system`)에는 박스 자리가 없다(`open-questions.md` #87) —
저장 · 셀러 조정과의 합성 · 재사용/무효화는 BE 연결 몫이고, 이 모듈은 계산 결과와 그 결과로 만든 렌더 입력(`apply_system`)만 낸다.
N5 수정 뒤에는 이 모듈을 다시 부르지 않는다 — 조정값을 유지한 렌더 입력을 `typeset_boundary.judge_render`로 다시 판정한다.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from pipeline.handoff import typeset_geometry as geo

RULE_VERSION = "typeset-adjust/2"
FLOOR = 0.90            # PM: 최대 −10%
GAP_PX = 2.0            # 이웃 글과의 간격 — 측정 오차 실측 근거는 docs/ai-experiments/2026-11-24_09-typeset_auto-adjust-impl.md
WIDTH_PRECISION = 0.5   # 폭 탐색 정밀도(px)
SCALE_PRECISION = 0.005  # 축소 탐색 정밀도(배율)
H_SIDES, V_SIDES = ("left", "right"), ("top", "bottom")
# 명시적 개행(앞뒤 일반 공백 · 탭 포함)만 공백 하나로 바꾼다. NBSP(U+00A0) · 좁은 NBSP(U+202F) 등 줄바꿈 의미가 있는 공백은 건드리지 않는다
_BREAK = re.compile(r"[ \t]*(?:\r\n|\r|\n| | )[ \t]*")


def collapse_breaks(text: str) -> str:
    """줄바꿈 위치 조정 — 번역문의 명시적 개행을 풀어 브라우저 자연 줄바꿈에 맡긴다(글자 · 다른 공백 문자는 그대로)."""
    return _BREAK.sub(" ", text)


def _inter_area(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    w = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    h = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    return w * h if w > 0 and h > 0 else 0.0


def new_overlaps(box: Mapping[str, float], original: Sequence[Mapping[str, float]], others: Sequence[Mapping[str, Any]],
                 gap: float) -> list[int]:
    """후보 박스가 다른 블록의 자리(간격 gap 만큼 넓힌 사각형)와 겹치는 넓이가 원래 자리(original = 원래 박스 ∪ 원래 글자)보다
    커지는 이웃 id. 원래부터 겹쳐 있던 만큼은 허용하고, 조정으로 겹침이 새로 생기거나 커지는 것만 막는다."""
    bad = set()
    for o in others:
        oi = {"x": o["x"] - gap, "y": o["y"] - gap, "w": o["w"] + 2 * gap, "h": o["h"] + 2 * gap}
        if _inter_area(box, oi) > max((_inter_area(r, oi) for r in original), default=0.0) + 1e-6:
            bad.add(int(o["blockId"]))
    return sorted(bad)


def apply_system(api_block: Mapping[str, Any], system: Mapping[str, Any] | None) -> dict[str, Any]:
    """시스템 조정을 renderInput 블록에 적용한 사본. system=None 이면 그대로. 글자 배율 = 입력 배율 × 시스템 배율."""
    b = copy.deepcopy(dict(api_block))
    if not system:
        return b
    if system.get("lineBreakApplied"):
        b["text"] = collapse_breaks(b["text"])
    if system.get("box"):
        b["box"] = {k: float(system["box"][k]) for k in ("x", "y", "w", "h")}
    if system.get("fontScale") not in (None, 1, 1.0):
        b["style"] = {**b["style"], "fontScale": float(b["style"]["fontScale"]) * float(system["fontScale"])}
    return b


class _Probe:
    """블록 하나의 후보 상태를 재는 도우미 — 측정 횟수를 센다."""

    def __init__(self, measure, section: Mapping[str, Any], base: Mapping[str, Any]):
        self.measure, self.section, self.base, self.count = measure, section, base, 0

    def __call__(self, st: Mapping[str, Any]) -> geo.Measured:
        self.count += 1
        blk = apply_system(self.base, {"lineBreakApplied": st["lb"], "box": st["box"], "fontScale": st["scale"]})
        return geo.measure_blocks(self.measure, self.section, [blk])[int(self.base["blockId"])]


def _h_excess(m: geo.Measured) -> float:
    return max(m.excess["left"], m.excess["right"]) if m.ok else float("inf")


def _widen_box(box: Mapping[str, float], lim: Mapping[str, float], align: str) -> tuple[float, Any]:
    x0, w0 = box["x"], box["w"]
    room_l, room_r = max(0.0, x0 - lim["left"]), max(0.0, lim["right"] - (x0 + w0))
    if align == "left":
        wmax = w0 + room_r
    elif align == "right":
        wmax = w0 + room_l
    else:
        wmax = w0 + 2 * min(room_l, room_r)

    def at(w: float) -> dict[str, float]:
        x = x0 if align == "left" else (x0 + w0 - w) if align == "right" else x0 - (w - w0) / 2
        return {**{k: float(box[k]) for k in ("x", "y", "w", "h")}, "x": x, "w": w}
    return wmax, at


def _min_width(probe: _Probe, st: dict[str, Any], at, lo: float, hi: float, good) -> tuple[float, geo.Measured]:
    """good(측정)을 만족하는 가장 작은 폭(hi 는 만족한다고 가정)."""
    best = probe({**st, "box": at(hi)})
    while hi - lo > WIDTH_PRECISION:
        mid = (lo + hi) / 2
        m = probe({**st, "box": at(mid)})
        if good(m):
            hi, best = mid, m
        else:
            lo = mid
    return hi, best


def _adjust_block(probe: _Probe, section: Mapping[str, Any], base: Mapping[str, Any], cur: geo.Measured,
                  obstacles: list[dict[str, Any]], obstacles_orig: list[dict[str, Any]], ignore: set[int], gap: float,
                  floor: float, original: list[dict[str, float]]) -> tuple[dict[str, Any], geo.Measured, list[dict[str, Any]], dict[str, Any]]:
    st = {"lb": False, "box": {k: float(base["box"][k]) for k in ("x", "y", "w", "h")}, "scale": 1.0}
    steps: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}
    w0 = st["box"]["w"]
    held = False  # 폭 확장만으로는 나아지지 않았지만 최대 폭을 쥔 채 축소를 해 본다
    # 1) 줄바꿈 위치
    if "\n" in base["text"]:
        m = probe({**st, "lb": True})
        ok = m.ok and len(m.lines) <= len(cur.lines) and (not m.over or m.excess["max"] < cur.excess["max"])
        steps.append({"op": "linebreak", "applied": ok, "linesBefore": len(cur.lines), "linesAfter": len(m.lines) if m.ok else None})
        if ok:
            st, cur = {**st, "lb": True}, m
    else:
        steps.append({"op": "linebreak", "applied": False, "why": "no_newline"})
    # 2) 정렬 방향 폭 확장
    full = False
    if cur.over:
        sw, sh = float(section["width"]), float(section["height"])
        lim = geo.free_limits(st["box"], obstacles, sw, sh, gap, ignore)
        lim0 = geo.free_limits(st["box"], obstacles_orig, sw, sh, gap, ignore)
        align = base["style"]["align"]
        wmax, at = _widen_box(st["box"], lim, align)
        wmax0, _ = _widen_box(st["box"], lim0, align)
        dirs = {"left": ("right",), "right": ("left",), "center": ("left", "right")}[align]
        competed = wmax < wmax0 - 1e-6
        info: dict[str, Any] = {"op": "widen", "applied": False, "align": align, "maxWidth": wmax, "maxWidthWithoutNeighborMoves": wmax0,
                                "limitedBy": {d: lim["limitedBy"][d] for d in dirs}, "competition": competed}
        if competed:  # 먼저 조정된 이웃이 이 블록이 쓸 수 있던 여백을 차지했다
            extra["competition"] = sorted({lim["limitedBy"][d] for d in dirs if lim[d] != lim0[d]} - {None})
            info["competitors"] = extra["competition"]
        # 여백 한계 안에서도 다른 블록 자리와의 겹침이 원래보다 커지지 않는 폭까지만(원래부터 겹친 이웃 포함)
        if new_overlaps(at(wmax), original, obstacles, gap):
            lo, hi = st["box"]["w"], wmax
            while hi - lo > WIDTH_PRECISION:
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if not new_overlaps(at(mid), original, obstacles, gap) else (lo, mid)
            info["limitedByOverlap"] = new_overlaps(at(wmax), original, obstacles, gap)
            wmax = lo
            info["maxWidth"] = wmax
        if wmax <= st["box"]["w"] + WIDTH_PRECISION:
            info["why"] = "no_room"
        else:
            m = probe({**st, "box": at(wmax)})
            improved = m.ok and (not m.over or len(m.lines) < len(cur.lines) or m.excess["max"] < cur.excess["max"])
            if not improved:
                info["why"] = "no_gain_alone"
                if m.ok:  # 확장 단독 효과가 없어도 확장 + 축소로 풀릴 수 있다
                    st, cur, held = {**st, "box": at(wmax)}, m, True
                    info["heldForShrink"] = True
            elif not m.over:
                w, m = _min_width(probe, st, at, st["box"]["w"], wmax, lambda x: x.ok and not x.over)
                st, cur, full = {**st, "box": at(w)}, m, True
                info.update(applied=True, width=[w0, w], fullFit=True)
            else:
                st, cur = {**st, "box": at(wmax)}, m
                info.update(applied=True, width=[w0, wmax], fullFit=False)
        steps.append(info)
    # 3) 축소(최대 −10%)
    if cur.over:
        m_floor = probe({**st, "scale": floor})
        if not m_floor.ok or m_floor.over:
            steps.append({"op": "shrink", "applied": False, "why": "need_below_floor", "floor": floor})
            extra["needBelowFloor"] = True
        else:
            lo, hi, best = floor, 1.0, m_floor  # lo: 들어감, hi: 넘침
            while hi - lo > SCALE_PRECISION:
                mid = round((lo + hi) / 2, 4)
                m = probe({**st, "scale": mid})
                if m.ok and not m.over:
                    lo, best = mid, m
                else:
                    hi = mid
            st, cur = {**st, "scale": lo}, best
            steps.append({"op": "shrink", "applied": True, "scale": lo})
            # 넓힌 폭을 쥔 채 축소했다면 그 배율에서 들어가는 최소 폭으로 다시 줄인다(원래 폭으로 돌아갈 수도 있다)
            widen = next((s for s in steps if s["op"] == "widen" and (s.get("applied") or s.get("heldForShrink"))), None)
            if widen and not full:
                align = base["style"]["align"]
                _, at = _widen_box({**st["box"], "x": base["box"]["x"], "w": w0}, {"left": -1e18, "right": 1e18}, align)
                w, m = _min_width(probe, st, at, w0, st["box"]["w"], lambda x: x.ok and not x.over)
                st, cur = {**st, "box": at(w)}, m
                widen.update(applied=w > w0 + 1e-9, width=[w0, w], tightenedAfterShrink=True)
    return st, cur, steps, extra


def _causes(cur: geo.Measured, steps: list[dict[str, Any]], box: Mapping[str, float]) -> list[str]:
    if not cur.ok:
        return [f"measure_failed:{cur.reason}"]
    out = []
    if any(s in cur.sides for s in H_SIDES):
        for ln in cur.raw.get("lines") or []:
            ink = ln.get("ink")
            if ink and " " not in str(ln.get("text", "")).strip() and ink["right"] - ink["left"] > box["w"]:
                out.append("long_word")
                break
    w = next((s for s in steps if s["op"] == "widen"), None)
    if w and (w.get("why") in ("no_room", "no_gain_alone") or (w.get("applied") and not w.get("fullFit"))):
        out.append("space_blocked")
    if w and w.get("competition"):
        out.append("space_taken_by_neighbor")
    if any(s["op"] == "shrink" and s.get("why") == "need_below_floor" for s in steps):
        out.append("shrink_floor")
    if any(s in cur.sides for s in V_SIDES):
        out.append("vertical_overflow")
    return out or ["no_applicable_means"]


def adjust_section(section: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]], measure: geo.SectionMeasurer, *,
                   obstacles: Sequence[Mapping[str, Any]] = (), gap: float = GAP_PX, floor: float = FLOOR) -> dict[str, Any]:
    """섹션 하나의 자동 조정.

    section = {"key"(기록용), "width", "height", ...측정기에 넘길 값}, blocks = 조판 대상 renderInput 블록(1차 번역 · 셀러 조정 없음),
    measure = SectionMeasurer, obstacles = 조판 대상이 아닌 블록 박스 [{"blockId", "x", "y", "w", "h"}].
    반환: blocks[blockId] = {status, system(해소된 블록만), steps, causes, competition, measures}, before · after(FE 측정 원본),
    renderBlocks(조정을 적용한 renderInput 블록 — 해소 못 한 블록은 원래 입력).
    status: not_needed · resolved · unresolved(원래 입력으로 복원) · measure_failed."""
    by_id = {int(b["blockId"]): b for b in blocks}
    before = geo.measure_blocks(measure, section, list(blocks))
    static = [{"blockId": int(o["blockId"]), **{k: float(o[k]) for k in ("x", "y", "w", "h")}} for o in obstacles]
    occ = {bid: geo.occupied(bid, by_id[bid]["box"], m.ink) for bid, m in before.items()}
    occ_orig = copy.deepcopy(occ)
    final_api = {bid: copy.deepcopy(dict(b)) for bid, b in by_id.items()}
    res: dict[int, dict[str, Any]] = {}
    for bid, m in before.items():
        res[bid] = {"status": "measure_failed" if not m.ok else "not_needed", "system": None, "steps": [], "measures": 0}
        if not m.ok:
            res[bid]["reason"] = m.reason
    order = sorted((bid for bid, m in before.items() if m.ok and m.over),
                   key=lambda b: (by_id[b]["box"]["y"], by_id[b]["box"]["x"], b))
    for bid in order:
        base = by_id[bid]
        others = [r for k, rs in occ.items() if k != bid for r in rs] + static
        others_orig = [r for k, rs in occ_orig.items() if k != bid for r in rs] + static
        # 원래부터 겹친 이웃도 장애물에서 빼지 않는다 — 겹침이 원래보다 커지는지는 new_overlaps 로 따로 막는다
        original = [{k: r[k] for k in ("x", "y", "w", "h")} for r in occ_orig[bid]]
        probe = _Probe(measure, section, base)
        st, cur, steps, extra = _adjust_block(probe, section, base, before[bid], others, others_orig, {bid}, gap, floor, original)
        row = res[bid]
        row.update(steps=steps, measures=probe.count, attempt={"box": st["box"], "fontScale": st["scale"], "lineBreakApplied": st["lb"]})
        if extra.get("competition"):
            row["competition"] = extra["competition"]
        clash = new_overlaps(st["box"], original, others, gap) if cur.ok and not cur.over else []
        if clash:  # 채택 직전 확인 — 넘침이 풀렸어도 이웃과 새로 겹치면 채택하지 않는다
            steps.append({"op": "accept", "applied": False, "why": "new_overlap", "neighbors": clash})
        if cur.ok and not cur.over and not clash:
            row["status"] = "resolved"
            row["system"] = {"box": st["box"], "fontScale": st["scale"], "lineBreakApplied": st["lb"]}
            final_api[bid] = apply_system(base, row["system"])
            occ[bid] = geo.occupied(bid, st["box"], cur.ink)
        else:
            row["status"] = "unresolved"  # 시도한 조정은 남기지 않는다(attempt 는 기록만)
            row["causes"] = ["new_overlap"] if clash else _causes(cur, steps, st["box"])
            row["remainingOverflow"] = {"sides": cur.sides, "excessPx": cur.excess} if cur.ok else None
    renders = [final_api[int(b["blockId"])] for b in blocks]
    after = geo.measure_blocks(measure, section, renders)
    dropped = False
    for bid, row in res.items():
        if row["status"] == "resolved" and after[bid].over:  # 같은 입력을 다시 쟀는데 넘치면 재현성 문제 — 조정을 버린다
            row.update(status="unresolved", system=None, causes=["not_reproducible"])
            final_api[bid] = copy.deepcopy(dict(by_id[bid]))
            dropped = True
    renders = [final_api[int(b["blockId"])] for b in blocks]
    if dropped:
        after = geo.measure_blocks(measure, section, renders)
    return {"rule": RULE_VERSION, "section": section.get("key"), "gapPx": gap, "floor": floor, "order": order,
            "blocks": res, "before": {bid: m.raw for bid, m in before.items()},
            "after": {bid: m.raw for bid, m in after.items()}, "renderBlocks": renders}
