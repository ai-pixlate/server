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


# ── 박스 자동 맞춤 · 갱신 장애물 ──

def _ev(lines, ink, font_px=10.0):
    return {"ok": True, "over": False, "sides": [], "lines": len(lines),
            "pr": {"lines": [{"text": f"l{i}", "rect": r} for i, r in enumerate(lines)], "ink": ink, "fontPx": font_px}}


BOX = {"x": 100.0, "y": 50.0, "w": 200.0, "h": 60.0}
LINES = [{"left": 140.0, "right": 260.0, "top": 50.0, "bottom": 62.0}, {"left": 160.0, "right": 240.0, "top": 62.0, "bottom": 74.0}]
INK = {"left": 141.0, "right": 259.0, "top": 52.0, "bottom": 75.0}


@pytest.mark.parametrize("align, x, w", [("center", 140.0, 120.0), ("left", 100.0, 160.0), ("right", 140.0, 160.0)])
def test_fit_box_keeps_alignment_anchor(align, x, w):
    blk = {"style": {"align": align, "lineHeightRatio": 1.2}}
    nb = ae.fit_box(blk, {"box": dict(BOX)}, _ev(LINES, INK), "layout")
    assert nb["x"] == pytest.approx(x) and nb["w"] == pytest.approx(w)
    assert nb["y"] == 50.0 and nb["h"] == pytest.approx(24.0)  # 줄 2 × 10px × 1.2 — 위쪽 변 고정


def test_fit_box_ink_mode_covers_descender_and_never_grows():
    blk = {"style": {"align": "center", "lineHeightRatio": 1.2}}
    nb = ae.fit_box(blk, {"box": dict(BOX)}, _ev(LINES, INK), "ink")
    assert nb["h"] == pytest.approx(25.0)  # ink 아래 끝(75) − 위(50) > 줄 상자 24
    tall = {**INK, "bottom": 200.0}
    assert ae.fit_box(blk, {"box": dict(BOX)}, _ev(LINES, tall), "ink")["h"] == 60.0  # 원래 높이를 넘지 않는다
    full = [{"left": 100.0, "right": 300.0, "top": 50.0, "bottom": 110.0}]
    assert ae.fit_box(blk, {"box": dict(BOX)}, _ev(full, {**INK, "left": 100.0, "right": 300.0, "bottom": 110.0}), "ink") is None


def test_occupied_includes_spilled_ink():
    ev = _ev(LINES, {**INK, "bottom": 140.0})
    rects = ae.occupied(7, BOX, ev)
    assert len(rects) == 2 and rects[1]["h"] == pytest.approx(88.0) and all(r["blockId"] == 7 for r in rects)


def _row(bid, box, ink):
    return {"sectionKey": "GS-01/s1", "blockId": bid, "box": box, "primary": {"ok": True, "ink": ink}}


def test_new_overlaps_counts_only_new_and_depth():
    b1 = {"x": 0, "y": 0, "w": 100, "h": 20}
    b2 = {"x": 0, "y": 30, "w": 100, "h": 20}
    B = {"a": _row(1, b1, {"left": 0, "right": 90, "top": 2, "bottom": 18}), "b": _row(2, b2, {"left": 0, "right": 90, "top": 32, "bottom": 48})}
    A = {"a": _row(1, {**b1, "h": 40}, {"left": 0, "right": 90, "top": 2, "bottom": 35}), "b": B["b"]}  # a의 글자가 b 자리로 5px 들어감
    r = ae.new_overlaps(A, B, {}, ["a"])
    assert r["blocks"] == 1 and r["deeperThanQuantAllow"] == 1
    assert ae.new_overlaps(B, B, {}, ["a"])["blocks"] == 0


# ── 집계 전 연결 · 조건 검사 ──

import json  # noqa: E402

WIN = {"system": "Windows", "release": "11", "version": "10.0.26200", "machine": "AMD64"}
ENV = {"fe": {"modules": {"modules": {"typeset-rules": {"source_sha256": "x"}}}}, "dpr": 2.0, "platform": WIN,
       "metricsProbe": {"quantized": True, "rows": [{"text": "P", "ascent": 12}]}}


def _run(tmp_path, old_meta=False):
    """기준 입력 → 기준 측정 → 탐색(조정 입력 · 결정 · meta) → 재측정이 맞게 이어진 최소 회차."""
    base = {"variant": "baseline", "sections": [{"key": "GS-01/s1", "width": 400, "height": 300, "blocks": [
        {"blockId": 9, "text": "a", "box": {"x": 0, "y": 0, "w": 10, "h": 10}, "style": {"fontScale": 1.0}}]}]}
    (tmp_path / "input.json").write_text(json.dumps(base), encoding="utf-8")
    raw = (tmp_path / "input.json").read_bytes()
    meas = {"tool": "m/1", "fonts": {"faces": {"400": {"sha256": "f"}}}, "toolAssets": {"a": "1"}, "browser": {"version": "149"},
            "checkDpr": 1.0, **ENV}
    (tmp_path / "before").mkdir()
    (tmp_path / "before" / "measure.json").write_text(json.dumps({"meta": {**meas, "inputSha256": ae.tbm.input_sha256(base)}, "blocks": []}),
                                                      encoding="utf-8")
    out = tmp_path / "search"
    sdir = out / "quant_aware" / "S90"
    (sdir / "measure").mkdir(parents=True)
    (out / "obstacles.json").write_text("{}", encoding="utf-8")
    after = {**base, "variant": "adjust:quant_aware:S90", "derivedFrom": [{"variant": "baseline", "sha256": ae.tbm.sha256_bytes(ae.tbm.canon(base).encode()),
                                                                            "adjust": {"criterion": "quant_aware", "strategy": "S90"}}]}
    (sdir / "after-input.json").write_text(json.dumps(after), encoding="utf-8")
    (sdir / "decisions.jsonl").write_text("", encoding="utf-8")
    meta = {"tool": ae.SEARCH_TOOL, "criterion": "quant_aware", "strategy": "S90", "inputSha256": ae.tbm.sha256_bytes(raw),
            "browser": "149", **ENV}
    if not old_meta:
        meta.update(afterInputSha256=ae.tbm.input_sha256(after), decisionsSha256=ae.tbm.sha256_bytes(b""),
                    fonts=meas["fonts"], toolAssets=meas["toolAssets"])
    (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (sdir / "measure" / "measure.json").write_text(json.dumps({"meta": {**meas, "inputSha256": ae.tbm.input_sha256(after)}, "blocks": []}),
                                                   encoding="utf-8")
    return out, tmp_path / "before", sdir


def _edit(path, fn):
    d = json.loads(path.read_text(encoding="utf-8"))
    fn(d)
    path.write_text(json.dumps(d), encoding="utf-8")


def test_provenance_ok_and_old_meta_marked(tmp_path):
    out, before, _ = _run(tmp_path)
    assert ae.check_provenance(out, before, False)["strategies"] == {"quant_aware/S90": {"searchLink": "verified", "afterMeasure": "verified"}}
    (tmp_path / "old").mkdir()
    out2, before2, _ = _run(tmp_path / "old", old_meta=True)
    assert ae.check_provenance(out2, before2, False)["strategies"]["quant_aware/S90"]["searchLink"] == "unverified_old_meta"


@pytest.mark.parametrize("breaker", [
    # 탐색을 다시 돌려 조정 입력이 바뀌었는데 재측정을 빠뜨렸다
    lambda out, before, sdir: _edit(sdir / "after-input.json", lambda d: d["sections"][0]["blocks"][0]["style"].update(fontScale=0.9)),
    # 기준 입력이 바뀌었다(탐색 · 기준 측정은 예전 입력)
    lambda out, before, sdir: _edit(out.parent / "input.json", lambda d: d.update(variant="other")),
    # 결정 기록만 다른 실행의 것
    lambda out, before, sdir: (sdir / "decisions.jsonl").write_text('{"key": "x"}\n', encoding="utf-8"),
])
def test_provenance_refuses_broken_link_even_when_allowed(tmp_path, breaker):
    out, before, sdir = _run(tmp_path)
    breaker(out, before, sdir)
    with pytest.raises(SystemExit):
        ae.check_provenance(out, before, True)


@pytest.mark.parametrize("target", ["measure", "search"])
def test_provenance_refuses_condition_diff_unless_allowed(tmp_path, target):
    out, before, sdir = _run(tmp_path)
    linux = {"system": "Linux", "release": "6.8", "version": "#1", "machine": "x86_64"}
    if target == "measure":
        _edit(sdir / "measure" / "measure.json", lambda d: d["meta"].update(platform=linux))
    else:
        _edit(sdir / "meta.json", lambda d: d.update(platform=linux))
    with pytest.raises(SystemExit):
        ae.check_provenance(out, before, False)
    assert list(ae.check_provenance(out, before, True)["conditionDiffs"]) == [f"quant_aware/S90:{target}"]
