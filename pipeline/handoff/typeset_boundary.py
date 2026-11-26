"""⑨ 이미지 경계 이탈 · 글자 잘림 판정(Q-07) — 블록 배치 영역 넘침과 섹션 이미지 밖으로 나가 잘리는 글자를 구분한다.

자동 조정과 상관없이 혼자 부를 수 있다 — 입력은 지금 렌더 입력(renderInput 블록: 자동 조정 결과 · 셀러가 고친 배치 모두)과
그 입력의 FE 측정 결과뿐이다. 측정을 새로 하지 않는다(서버 HTML 렌더 결과의 `blocks[].measure`를 그대로 넣는다).

판정 세 가지는 서로 독립이다(`contract.md` 6.2 · 6.6, FE `evaluateTypesetBlock` — 블록 박스 넘침과 이미지 이탈을 합치지 않는다).
- 배치 영역 넘침(boxOverflow): FE 측정의 `overflow` 그대로 — 글자가 배치 박스를 넘었다. 그림은 잘리지 않을 수 있다.
- 섹션 잘림(sectionClip): 글자 경계가 섹션 이미지(0,0)–(width,height) 밖으로 나갔다. 섹션 PNG 는 섹션 크기로 촬영되므로
  밖으로 나간 부분은 실제로 그려지지 않는다(글자 손실). 경계와 같으면 정상, 허용 오차 없음.
- 최종 이미지 이탈(finalImage): 포함 섹션을 순서대로 쌓은 최종 이미지의 경계를 넘었다(FE `evaluateFinalImageBoundary`와 같은 좌표 규칙).
  섹션마다 따로 촬영해 이어 붙이므로 최종 이미지 이탈은 섹션 잘림의 일부(가로 · 첫 섹션 위 · 마지막 섹션 아래)다 — 기록용으로 함께 낸다.

status: fit(넘침 없음) · box_overflow(배치 영역만 넘침, 글자는 섹션 안) · section_clip(섹션 밖으로 잘림) · measure_failed.
확정 차단 · N6 오류 · 화면 표시는 BE · FE 연결 몫이다(`contract.md` 6.6, `open-questions.md` #88) — 이 모듈은 판정만 한다.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from pipeline.handoff import typeset_geometry as geo

RULE_VERSION = "typeset-boundary/1"


def _area(r: Mapping[str, float]) -> float:
    return max(0.0, r["w"]) * max(0.0, r["h"])


def _inside_fraction(ink: Mapping[str, float], width: float, height: float) -> float:
    """글자 경계 사각형 중 섹션 안에 남는 비율(0~1) — 잘린 정도를 가늠하는 근사(그린 픽셀 수가 아니다)."""
    r = geo.ink_rect(ink)
    a = _area(r)
    if a <= 0:
        return 1.0
    x0, y0 = max(0.0, r["x"]), max(0.0, r["y"])
    x1, y1 = min(float(width), r["x"] + r["w"]), min(float(height), r["y"] + r["h"])
    return max(0.0, x1 - x0) * max(0.0, y1 - y0) / a


def judge_block(m: geo.Measured, width: float, height: float) -> dict[str, Any]:
    """블록 하나 — 배치 영역 넘침과 섹션 잘림을 따로 낸다."""
    if not m.ok:
        return {"status": "measure_failed", "reason": m.reason, "box": m.box, "ink": None,
                "boxOverflow": None, "sectionClip": None}
    clip_ex = geo.excess(m.ink, geo.section_box(width, height))
    clip = geo.sides_of(clip_ex)
    status = "section_clip" if clip["any"] else "box_overflow" if m.overflow["any"] else "fit"
    out: dict[str, Any] = {
        "status": status, "box": m.box, "ink": m.ink, "lines": len(m.lines),
        "boxOverflow": {**m.overflow, "excessPx": m.excess},
        "sectionClip": {**clip, "excessPx": clip_ex},
    }
    if clip["any"] and m.ink:
        frac = _inside_fraction(m.ink, width, height)
        out["sectionClip"]["inkInsideFraction"] = round(frac, 4)
        out["sectionClip"]["fullyOutside"] = frac == 0.0
    return out


def judge_section(section: Mapping[str, Any], measured: Mapping[int, geo.Measured]) -> dict[int, dict[str, Any]]:
    """섹션 하나의 블록별 판정. section = {"width", "height"}, measured = blockId → Measured."""
    w, h = float(section["width"]), float(section["height"])
    return {bid: judge_block(m, w, h) for bid, m in measured.items()}


def judge_render(section: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]],
                 raw_measures: Mapping[int, Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    """렌더 결과를 그대로 넣는 입구 — blocks = 그 렌더에 넣은 renderInput 블록, raw_measures = blockId → FE 측정 결과 dict
    (서버 HTML 렌더 `blocks[].measure`). 자동 조정 없이 셀러 수정 뒤 배치를 다시 판정할 때 이것만 부른다."""
    measured = {int(b["blockId"]): geo.read_measure(raw_measures.get(int(b["blockId"])) or {}, b["box"]) for b in blocks}
    return judge_section(section, measured)


def judge_final(sections: Sequence[Mapping[str, Any]], width: float | None = None) -> dict[str, Any]:
    """최종 출력 이미지 기준 이탈 — sections = [{"id", "order", "height", "width", "include", "blocks": {blockId: judge_block 결과}}].
    포함 섹션만 순서대로 쌓는다(제외 섹션 · 원본 top_offset · N5 displayTop 을 쓰지 않는다, `contract.md` 6.6).
    width 를 주지 않으면 포함 섹션 폭의 최댓값(원본 규격 경로는 섹션 폭이 같다). 블록마다 finalImage 를 붙인 사본과 요약을 돌려준다."""
    offsets, total = geo.final_offsets([{"id": s["id"], "order": s["order"], "height": s["height"], "include": s["include"]}
                                        for s in sections])
    if width is None:
        width = max((float(s["width"]) for s in sections if s["include"]), default=0.0)
    image = {"x": 0.0, "y": 0.0, "w": float(width), "h": total}
    out_sections, violations, clips, overflows = [], 0, 0, 0
    for s in sorted(sections, key=lambda s: s["order"]):
        off = offsets.get(s["id"])
        blocks = {}
        for bid, j in s["blocks"].items():
            j = dict(j)
            if off is None:
                j["finalImage"] = {"state": "section-excluded"}
            elif j.get("ink"):
                ink = j["ink"]
                moved = {"left": ink["left"], "right": ink["right"], "top": ink["top"] + off, "bottom": ink["bottom"] + off}
                ex = geo.excess(moved, image)
                j["finalImage"] = {"state": "in-output", "offsetY": off, "inkInImage": moved, **geo.sides_of(ex), "excessPx": ex}
                violations += bool(j["finalImage"]["any"])
            else:
                j["finalImage"] = {"state": "in-output", "offsetY": off, "inkInImage": None, "any": False}
            if off is not None:
                clips += j.get("status") == "section_clip"
                overflows += j.get("status") == "box_overflow"
            blocks[bid] = j
        out_sections.append({"id": s["id"], "order": s["order"], "include": s["include"], "offsetY": off, "blocks": blocks})
    return {"rule": RULE_VERSION, "image": {"width": image["w"], "height": total}, "sections": out_sections,
            "summary": {"finalImageViolations": violations, "sectionClipBlocks": clips, "boxOverflowOnlyBlocks": overflows}}
