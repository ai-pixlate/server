"""⑨ 조판 자동 조정 육안 검수 도구(docs/ai-experiments/tools/typeset_adjust_review.py)의 순수 계산 — 브라우저 · 회차 자료 없이.
묶음 나누기 · TSV 검증 · 집계 분리만 본다. 그림 · 측정은 실제 회차로 확인한다."""
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


ar = _load("typeset_adjust_review")
SEC = "GS-01/s1"


def _row(bid, box, ink, text="a b", scale=1.0, lines=1, over=False, judgement="fit"):
    sides = {s: over and s == "bottom" for s in ("left", "right", "top", "bottom")}
    return {"key": f"{SEC}/b{bid}", "sectionKey": SEC, "blockId": bid, "text": text, "role": "body", "box": box,
            "sectionSize": {"width": 1000, "height": 800}, "style": {"fontScale": scale, "align": "left"}, "judgement": judgement,
            "primary": {"ok": True, "fontPx": 20.0 * scale, "ink": ink, "lines": [{"text": f"l{i}"} for i in range(lines)],
                        "overflow": {**sides, "any": over}, "excess": {"max": 30.0 if over else 0.0}}}


def _ink(b, dy=0.0):
    return {"left": b["x"] + 1, "right": b["x"] + b["w"] - 1, "top": b["y"] + 1, "bottom": b["y"] + b["h"] - 1 + dy}


B1, B2, B3, B4, B5 = ({"x": 10, "y": 10, "w": 100, "h": 20}, {"x": 10, "y": 60, "w": 100, "h": 20}, {"x": 300, "y": 10, "w": 50, "h": 20},
                      {"x": 300, "y": 200, "w": 200, "h": 40}, {"x": 600, "y": 200, "w": 200, "h": 40})


def _L():
    B = {r["key"]: r for r in [_row(1, B1, _ink(B1, 25), over=True, judgement="overflow_confirmed"),
                               _row(2, {**B2, "w": 80}, _ink({**B2, "w": 80}, 0), over=True, judgement="overflow_confirmed"),
                               _row(3, B3, _ink(B3, 40), over=True, judgement="overflow_confirmed"),
                               _row(4, B4, _ink(B4)), _row(5, B5, _ink(B5))]}
    k = lambda i: f"{SEC}/b{i}"  # noqa: E731
    # 기준 후보: b1 = 높이 확장 + 축소 + 줄바꿈(글자가 b2 자리까지 내려감), b2 = 폭 확장만, b3 = 미해결
    A = dict(B)
    A[k(1)] = _row(1, {**B1, "h": 45}, _ink(B1, 40), text="a\nb", scale=0.95, lines=2)
    A[k(2)] = _row(2, B2, _ink({**B2, "w": 80}))
    dec = {k(1): {"status": "resolved"}, k(2): {"status": "resolved"},
           k(3): {"status": "unresolved", "causes": ["space_blocked"], "final": {"scale": 0.9, "box": B3}}}
    # 맞춤: b4 적용, b5 기각
    Af = dict(B)
    Af[k(4)] = _row(4, {**B4, "w": 150}, _ink(B4))
    decf = {k(4): {"fit": {"applied": True, "areaRatio": 0.75}},
            k(5): {"fit": {"applied": False, "sameLayout": False, "areaRatio": 0.8, "box": {**B5, "w": 160}}}}
    return {"B": B, "obstacles": {}, "main": ar.MAIN, "V": {ar.MAIN: {"A": A, "dec": dec}, ar.FIT: {"A": Af, "dec": decf}}}


def _spill(_sec, _ink, box):
    return 0.2 if box == B1 else 0.01  # b1 = 무늬 위, b2 = 단색 여백


def test_pick_groups():
    cases = ar.pick(_L(), _spill)
    by = {}
    for c in cases:
        by.setdefault(c["group"], []).append(c)
    assert [c["key"] for c in by["spill"] if not c.get("control")] == [f"{SEC}/b1"]
    assert [c["key"] for c in by["spill"] if c.get("control")] == [f"{SEC}/b2"]
    assert [c["key"] for c in by["widen"]] == [f"{SEC}/b2"]
    assert [c["key"] for c in by["shrink"]] == [f"{SEC}/b1"] and [c["key"] for c in by["linebreak"]] == [f"{SEC}/b1"]
    assert by["linebreak"][0]["lines"] == [["l0"], ["l0", "l1"]]
    u = by["unresolved"][0]
    assert u["key"] == f"{SEC}/b3" and u["right"] is None and u["left"] == "before"
    fit = {c["key"]: c for c in by["fit"]}
    assert fit[f"{SEC}/b4"]["right"] == ar.FIT and not fit[f"{SEC}/b4"].get("rejected")
    assert fit[f"{SEC}/b5"]["rejected"] and fit[f"{SEC}/b5"]["right"] is None and any(r["kind"] == "prop" for r in fit[f"{SEC}/b5"]["rects"])
    ov = by["overlap"][0]
    ov = next(c for c in by["overlap"] if c["key"] == f"{SEC}/b1")
    assert sorted(c["key"] for c in by["overlap"]) == [f"{SEC}/b1", f"{SEC}/b2"] and (ov["left"], ov["right"]) == ("before", ar.MAIN)
    assert [r["kind"] for r in ov["rects"] if r["side"] == "R"].count("nb") >= 1  # 이웃(b2) 자리를 오른쪽 그림에 표시
    assert [c["group"] for c in cases] == sorted((c["group"] for c in cases), key=ar.ORDER.index)  # 묶음 순서


def _tsv(tmp_path, rows):
    p = tmp_path / "r.tsv"
    p.write_text("\t".join(ar.TSV_COLUMNS) + "\n" + "".join("\t".join(r) + "\n" for r in rows), encoding="utf-8")
    return p


IDS = {f"spill:{SEC}/b1", f"fit:{SEC}/b4"}


def test_read_tsv_accepts_valid(tmp_path):
    seen = ar.read_tsv(_tsv(tmp_path, [["1", "spill", f"{SEC}/b1", "bad", "사진 위", "fp"]]), "fp", IDS)
    assert seen[f"spill:{SEC}/b1"]["verdict"] == "bad"


@pytest.mark.parametrize("row", [
    ["1", "spill", f"{SEC}/b1", "ok", "", "other"],     # 다른 페이지의 지문
    ["1", "shrink", f"{SEC}/b1", "ok", "", "fp"],       # 그 묶음에 없는 사례
    ["1", "spill", f"{SEC}/b1", "inside", "", "fp"],    # 정해진 답이 아님
])
def test_read_tsv_refuses(tmp_path, row):
    with pytest.raises(SystemExit):
        ar.read_tsv(_tsv(tmp_path, [row]), "fp", IDS)


def test_read_tsv_refuses_duplicate(tmp_path):
    r = ["1", "spill", f"{SEC}/b1", "ok", "", "fp"]
    with pytest.raises(SystemExit):
        ar.read_tsv(_tsv(tmp_path, [r, r]), "fp", IDS)


def test_summarize_splits_control_and_rejected():
    cases = ar.pick(_L(), _spill)
    seen = {f"spill:{SEC}/b1": {"verdict": "bad", "note": "사진 위"}, f"spill:{SEC}/b2": {"verdict": "ok", "note": ""}}
    s = ar.summarize(cases, seen)["groups"]
    assert s["spill"]["verdicts"] == {"사진 · 무늬 위 어림": {"읽기 어렵거나 그림을 가림": 1}, "대조(단색 여백 어림)": {"문제없음": 1}}
    assert s["spill"]["reviewed"] == 2 and s["spill"]["memos"] == [{"key": f"{SEC}/b1", "verdict": "읽기 어렵거나 그림을 가림", "note": "사진 위"}]
    assert s["fit"]["verdicts"] == {"적용": {"미검토": 1}, "기각 제안": {"미검토": 1}}


# ── 검토 지문: FE 모듈 · 원본 섹션 그림 ──

def _fp_case(tmp_path):
    plain, clean = tmp_path / "s.plain.png", tmp_path / "s.clean.png"
    plain.write_bytes(b"plain-1")
    clean.write_bytes(b"clean-1")
    meta = {"tool": "m/1", "inputSha256": "i", "renderDpr": 1.0,
            "fe": {"modules": {"modules": {"typeset-rules": {"source_sha256": "a"}}}}}
    L = {"meta": {"before": dict(meta), ar.MAIN: dict(meta)}}
    c = {"group": "widen", "key": f"{SEC}/b1", "left": "before", "right": ar.MAIN, "rects": [], "info": "", "crop": {"x": 0},
         "src": {"L": plain, "R": plain}, "thumbSrc": clean}
    return L, [c], plain, clean


def test_fingerprint_changes_with_fe_module_and_source_images(tmp_path):
    L, cases, plain, clean = _fp_case(tmp_path)
    fp = ar.fingerprint(L, cases)
    assert ar.fingerprint(L, cases) == fp
    L["meta"][ar.MAIN]["fe"] = {"modules": {"modules": {"typeset-rules": {"source_sha256": "b"}}}}  # FE 엔진만 바꿔 재측정
    assert ar.fingerprint(L, cases) != fp
    L, cases, plain, clean = _fp_case(tmp_path)
    plain.write_bytes(b"plain-2")  # 재측정으로 섹션 합성 그림이 바뀜
    assert ar.fingerprint(L, cases) != fp
    L, cases, plain, clean = _fp_case(tmp_path)
    clean.write_bytes(b"clean-2")  # 섹션 전체 보기 그림도 대상
    assert ar.fingerprint(L, cases) != fp
