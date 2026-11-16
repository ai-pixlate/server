"""⑨ 조판 자동 조정 실험 도구(docs/ai-experiments/tools/typeset_adjust_experiment.py)의 순수 계산 — 브라우저 없이.
여백 한계 · 겹침 · 수단 단계(가짜 측정기)를 본다. 측정 · 판정 자체는 typeset_browser_measure(FE 엔진)의 몫이라 여기서 다루지 않는다."""
import importlib.util
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "docs" / "ai-experiments" / "tools"
sys.path.insert(0, str(TOOLS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ae = _load("typeset_adjust_experiment")
SEC = {"key": "GS-01/s1", "width": 400, "height": 300}


def _o(i, x, y, w, h):
    return {"blockId": i, "x": x, "y": y, "w": w, "h": h}


def test_free_limits_stop_at_obstacles_and_section():
    box = {"x": 100, "y": 100, "w": 50, "h": 20}
    obs = [_o(1, 200, 105, 30, 10),   # 오른쪽, 세로로 겹침
           _o(2, 10, 90, 40, 40),     # 왼쪽, 세로로 겹침
           _o(3, 110, 160, 20, 20),   # 아래, 가로로 겹침
           _o(4, 300, 10, 20, 20)]    # 어느 방향과도 겹치지 않음
    lim = ae.free_limits(box, obs, 400, 300, ignore=set())
    assert lim == {"left": 50, "right": 200, "top": 0.0, "bottom": 160}
    assert ae.free_limits(box, obs, 400, 300, ignore={1, 3})["right"] == 400.0


def test_intersects_is_strict():
    a = {"x": 0, "y": 0, "w": 10, "h": 10}
    assert ae.intersects(a, {"x": 9.9, "y": 9.9, "w": 5, "h": 5})
    assert not ae.intersects(a, {"x": 10, "y": 0, "w": 5, "h": 5})  # 맞닿음은 겹침이 아니다


class FakeEval:
    """배율 s에서 오른쪽 초과 = 100·s − 영역 폭. 줄 수 1. 폭이 넓으면 들어간다."""

    def __init__(self):
        self.measures = 0

    def __call__(self, sec, blk, st):
        self.measures += 1
        need = 100 * st["scale"]
        over = need > st["box"]["w"]
        ex = {"left": 0.0, "right": max(0.0, need - st["box"]["w"]), "top": 0.0, "bottom": 0.0}
        ex["max"] = ex["right"]
        ink = {"left": st["box"]["x"], "right": st["box"]["x"] + need, "top": st["box"]["y"], "bottom": st["box"]["y"] + 10}
        return {"ok": True, "over": over, "sides": ["right"] if over else [], "lines": 1, "excess": ex,
                "pr": {"ink": ink, "lines": [{"text": "word", "ink": ink}]}}


def _blk(align="left"):
    return {"blockId": 9, "text": "a\nb", "box": {"x": 10, "y": 10, "w": 92, "h": 20}, "style": {"align": align, "fontScale": 1.0}}


def test_shrink_finds_largest_fitting_scale_within_floor():
    E, blk = FakeEval(), _blk()
    st = {"text": blk["text"], "box": dict(blk["box"]), "scale": 1.0}
    st2, ev, info = ae.step_shrink(E, SEC, blk, st, E(SEC, blk, st), {"floor": 0.90}, None)
    assert not ev["over"] and 0.915 <= st2["scale"] <= 0.92 and info["need_below_floor"] is False


def test_shrink_reports_floor_when_not_enough():
    E, blk = FakeEval(), _blk()
    blk["box"]["w"] = 80
    st = {"text": blk["text"], "box": dict(blk["box"]), "scale": 1.0}
    _, ev, info = ae.step_shrink(E, SEC, blk, st, E(SEC, blk, st), {"floor": 0.90}, None)
    assert ev["over"] and info["need_below_floor"] is True


@pytest.mark.parametrize("align, expect_x", [("left", 10), ("right", 2), ("center", 6)])
def test_expand_width_by_alignment_and_minimal(align, expect_x):
    E, blk = FakeEval(), _blk(align)
    st = {"text": blk["text"], "box": dict(blk["box"]), "scale": 1.0}
    ctx = {"obstacles": [], "ignore": {9}}
    st2, ev, info = ae.step_expand(E, SEC, blk, st, E(SEC, blk, st), None, ctx)
    assert not ev["over"] and info["applied"]
    assert 100 <= st2["box"]["w"] <= 100.5  # 필요한 만큼만(정밀도 0.5px)
    assert st2["box"]["x"] == pytest.approx(expect_x, abs=0.5)


def test_expand_blocked_by_obstacle_is_limited():
    E, blk = FakeEval(), _blk("left")
    st = {"text": blk["text"], "box": dict(blk["box"]), "scale": 1.0}
    ctx = {"obstacles": [_o(1, 105, 12, 10, 10)], "ignore": {9}}
    st2, ev, info = ae.step_expand(E, SEC, blk, st, E(SEC, blk, st), None, ctx)
    assert ev["over"] and info["widthLimited"] and st2["box"]["x"] + st2["box"]["w"] <= 105


def test_linebreak_rejected_when_no_gain():
    E, blk = FakeEval(), _blk()
    st = {"text": blk["text"], "box": dict(blk["box"]), "scale": 1.0}
    st2, _, info = ae.step_linebreak(E, SEC, blk, st, E(SEC, blk, st), None, None)
    assert st2["text"] == blk["text"] and info["applied"] is False  # 줄바꿈만으로 폭 초과가 줄지 않으면 채택하지 않는다


def test_unresolved_causes_long_word_and_floor():
    blk = _blk()
    st = {"text": "word", "box": {"x": 10, "y": 10, "w": 50, "h": 20}, "scale": 0.9}
    ev = FakeEval()(SEC, blk, st)
    causes = ae.unresolved_causes(SEC, blk, st, ev, [("shrink", {"need_below_floor": True})])
    assert causes == ["long_word", "shrink_floor"]
