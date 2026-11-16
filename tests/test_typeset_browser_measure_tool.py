"""⑨ 브라우저 넘침 측정 실험 도구(docs/ai-experiments/tools/typeset_browser_measure.py)의 순수 계산 — 브라우저 · 회차 자료 없이.
판정(넘침 여부)은 FE 엔진 결과를 그대로 쓰고, 여기서는 해석 분류 · 초과량 · 변형 입력 검사 · 비교 조건 확인만 본다."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "docs" / "ai-experiments" / "tools"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tbm = _load("typeset_browser_measure")
BOX = {"x": 10.0, "y": 20.0, "w": 100.0, "h": 30.0}


def _ink(left=12.0, top=22.0, right=100.0, bottom=45.0):
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _primary(ink):
    ex = tbm.excess(ink, BOX)
    sides = {s: ex[s] > 0 for s in tbm.SIDES}
    return {"ok": True, "ink": ink, "excess": ex, "overflow": {**sides, "any": any(sides.values())}}


def _pixel(ink):
    return {"ink": ink, "excess": tbm.excess(ink, BOX)}


def test_excess_boundary_equal_is_zero_and_fraction_kept():
    assert tbm.excess(_ink(left=10, top=20, right=110, bottom=50), BOX)["max"] == 0.0
    ex = tbm.excess(_ink(right=110.115), BOX)
    assert ex["right"] == pytest.approx(0.115) and ex["max"] == pytest.approx(0.115)


@pytest.mark.parametrize("engine, pixel, quantized, expect", [
    (_ink(), _ink(), False, "fit"),
    (_ink(), _ink(right=111.0), False, "fit_pixel_outside"),
    (_ink(right=110.115), _ink(right=110.0), False, "overflow_subpixel"),  # 합의상 넘침, 픽셀 구분 불가
    (_ink(right=125.0), _ink(right=124.0), False, "overflow_confirmed"),
    (_ink(left=8.5), _ink(left=10.2), True, "overflow_quantized"),  # 정수화 환경 · 초과 1.5 ≤ QUANT_ALLOW · 픽셀 안
    (_ink(left=8.5), _ink(left=10.2), False, "overflow_pixel_inside"),  # 정수화 환경이 아니면 설명되지 않는 차
    (_ink(left=5.0), _ink(left=10.2), True, "overflow_pixel_inside"),  # 초과 5 > QUANT_ALLOW
])
def test_judge(engine, pixel, quantized, expect):
    assert tbm.judge(_primary(engine), _pixel(pixel), quantized) == expect


def test_judge_failure_and_unchecked():
    assert tbm.judge({"ok": False}, None, True) == "measure_failed"
    assert tbm.judge(_primary(_ink(right=125.0)), None, True) == "overflow_unchecked"


def test_judge_keeps_engine_verdict_when_other_side_confirms():
    # 왼쪽은 정수화 범위, 오른쪽은 픽셀도 밖 — 실제 넘침
    assert tbm.judge(_primary(_ink(left=8.5, right=125.0)), _pixel(_ink(left=10.2, right=124.0)), True) == "overflow_confirmed"


def _inp():
    return {"schema": tbm.INPUT_SCHEMA, "variant": "baseline", "sections": [{"key": "GS-01/s3", "width": 200, "height": 100, "blocks": [
        {"blockId": 40, "role": "body", "text": "Hello", "box": dict(BOX), "style": {"fontScale": 1.0}}]}]}


def test_derive_changes_only_named_fields_and_records_origin():
    out = tbm.derive(_inp(), [("GS-01/s3/b40", "fontScale=0.9"), ("GS-01/s3/b40", "box.h=40")], "after")
    b = out["sections"][0]["blocks"][0]
    assert b["style"]["fontScale"] == 0.9 and b["box"]["h"] == 40 and b["text"] == "Hello"
    assert out["variant"] == "after" and out["derivedFrom"][0]["variant"] == "baseline"


@pytest.mark.parametrize("assign", ["fontScale=0", "fontScale=nan", "box.w=500", "box.x=-1", "text=Hi"])
def test_derive_rejects_invalid(assign):
    with pytest.raises(SystemExit):
        tbm.derive(_inp(), [("GS-01/s3/b40", assign)], "after")


def _measure_dir(tmp_path, name, meta_overrides=None, box=None, over=False, ink=None):
    d = tmp_path / name
    d.mkdir()
    meta = {"tool": tbm.TOOL_VERSION, "inputVariant": name, "inputSha256": name, "dpr": 2.0, "checkDpr": 1.0, "toolAssets": {"a": "1"},
            "fe": {"modules": {"modules": {"typeset-rules": {"source_sha256": "x"}}}}, "fonts": {"faces": {"400": {"sha256": "f"}}},
            "browser": {"version": "149"}}
    meta.update(meta_overrides or {})
    blk = {"key": "GS-01/s3/b40", "text": "Hello", "role": "body", "box": box or dict(BOX), "style": {"fontScale": 1.0},
           "primary": {"ok": True, "fontPx": 16, "lines": [{}], "overflow": {"any": over}, "excess": {"max": 1.0 if over else 0.0},
                       "ink": ink or _ink()},
           "judgement": "overflow_confirmed" if over else "fit", "flags": [], "images": {}}
    (d / "measure.json").write_text(json.dumps({"meta": meta, "blocks": [blk]}), encoding="utf-8")
    return d


def test_compare_refuses_condition_diff(tmp_path):
    a = _measure_dir(tmp_path, "a")
    b = _measure_dir(tmp_path, "b", {"browser": {"version": "157"}})
    with pytest.raises(SystemExit):
        tbm.compare(a, b, tmp_path / "c", allow_condition_diff=False)


def test_compare_marks_changed_input_and_reproducibility(tmp_path):
    a = _measure_dir(tmp_path, "a", over=True)
    b = _measure_dir(tmp_path, "b", box={**BOX, "h": 40.0}, over=False)
    r = tbm.compare(a, b, tmp_path / "c", allow_condition_diff=False)
    assert r["summary"]["status"] == {"changed_input": 1}
    assert r["summary"]["overflow_changed"] == {"True -> False": 1}
    # 입력이 같은데 결과가 다르면 조정 효과가 아니라 재현성 문제로 따로 둔다
    c = _measure_dir(tmp_path, "c2", over=False, ink=_ink(right=101.0))
    r2 = tbm.compare(a, c, tmp_path / "d", allow_condition_diff=False)
    assert r2["summary"]["status"] == {"same_input_result_differs": 1}
