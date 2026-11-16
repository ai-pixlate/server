"""⑨ 브라우저 넘침 측정 — 사람 확인 도구(docs/ai-experiments/tools/typeset_browser_review.py).
합성 measure.json만 쓴다(브라우저 · 그림 불필요). 표본 선택 · 지문 · TSV 검증 · 집계를 본다."""
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


rv = _load("typeset_browser_review")
COLS = "case_no\tkey\tverdict\tnote\treview_fingerprint\n"


META = {"inputSha256": "abc", "browser": {"version": "149"}, "platform": {"system": "Windows"}, "dpr": 2.0}


def _blocks():
    js = ["overflow_quantized"] * 3 + ["overflow_subpixel"] + ["overflow_confirmed"] * 10 + ["fit"] * 10 + ["measure_failed"]
    return [{"key": f"GS-01/s1/b{i}", "judgement": j, "primary": {"ink": {"left": 1.0, "right": 9.0}, "overflow": {"any": False}},
             "pixel": {"ink": {"left": 1.5}}, "images": {"review": f"blocks/{i}.review.png"} if j != "measure_failed" else {}}
            for i, j in enumerate(js)]


def _write(run, meta=META, blocks=None):
    blocks = blocks or _blocks()
    (run / "blocks").mkdir(exist_ok=True)
    for b in blocks:
        if b["images"].get("review") and not (run / b["images"]["review"]).exists():
            (run / b["images"]["review"]).write_bytes(b"png:" + b["key"].encode())
    (run / "measure.json").write_text(json.dumps({"meta": meta, "blocks": blocks}), encoding="utf-8")


@pytest.fixture
def run(tmp_path):
    _write(tmp_path)
    return tmp_path


def test_pick_takes_all_suspects_and_spaced_controls():
    cases = rv.pick(_blocks())
    js = [c["judgement"] for c in cases]
    assert js.count("overflow_quantized") == 3 and js.count("overflow_subpixel") == 1
    assert js.count("overflow_confirmed") == 6 and js.count("fit") == 6 and "measure_failed" not in js
    assert len({c["key"] for c in cases}) == len(cases)


def test_fingerprint_changes_with_result_environment_and_images(run):
    cases = rv.pick(_blocks())
    base = rv.fingerprint(META, cases, run)
    assert base == rv.fingerprint(META, rv.pick(_blocks()), run)  # 같은 결과면 같은 지문
    changed_judgement = [dict(c, judgement="fit") if i == 0 else c for i, c in enumerate(cases)]
    changed_ink = [dict(c, primary={**c["primary"], "ink": {"left": 0.0, "right": 9.0}}) if i == 0 else c for i, c in enumerate(cases)]
    assert base != rv.fingerprint(META, changed_judgement, run)
    assert base != rv.fingerprint(META, changed_ink, run)  # 분류가 같아도 측정값이 다르면 다르다
    assert base != rv.fingerprint({**META, "browser": {"version": "157"}}, cases, run)
    assert base != rv.fingerprint({**META, "platform": {"system": "Linux"}}, cases, run)
    (run / cases[0]["images"]["review"]).write_bytes(b"other image")
    assert base != rv.fingerprint(META, cases, run)  # 검수 그림이 바뀌면 다르다


def test_old_review_rejected_after_remeasure(run):
    cases, fp = _fp(run)
    tsv = _tsv(run, [f"1\t{cases[0]['key']}\tinside\t\t{fp}"])
    assert rv.aggregate(run, tsv)["reviewed"] == 1
    # 다시 측정해 브라우저 · 측정값이 바뀌었는데 분류는 같은 결과 — 이전 검토를 받지 않는다
    blocks = [dict(b, primary={**b["primary"], "ink": {"left": 0.5, "right": 9.0}}) for b in _blocks()]
    _write(run, {**META, "browser": {"version": "157"}}, blocks)
    with pytest.raises(SystemExit):
        rv.aggregate(run, tsv)


def _tsv(run, lines):
    p = run / "r.tsv"
    p.write_text(COLS + "".join(l + "\n" for l in lines), encoding="utf-8")
    return p


def _fp(run):
    m = json.loads((run / "measure.json").read_text(encoding="utf-8"))
    cases = rv.pick(m["blocks"])
    return cases, rv.fingerprint(m["meta"], cases, run)


def test_aggregate_counts_reviewed(run):
    cases, fp = _fp(run)
    res = rv.aggregate(run, _tsv(run, [f"1\t{cases[0]['key']}\toutside\t메모\t{fp}", f"2\t{cases[1]['key']}\t\t\t{fp}"]))
    assert res["cases"] == len(cases) and res["reviewed"] == 1
    assert res["memos"] == [{"key": cases[0]["key"], "verdict": "outside", "note": "메모"}]
    assert (run / "check" / "review_summary.json").exists()


@pytest.mark.parametrize("bad", ["fingerprint", "unknown_key", "verdict", "duplicate"])
def test_aggregate_rejects(run, bad):
    cases, fp = _fp(run)
    k = cases[0]["key"]
    lines = {"fingerprint": [f"1\t{k}\tinside\t\t0000000000000000"],
             "unknown_key": [f"1\tGS-09/s1/b1\tinside\t\t{fp}"],
             "verdict": [f"1\t{k}\tmaybe\t\t{fp}"],
             "duplicate": [f"1\t{k}\tinside\t\t{fp}", f"1\t{k}\toutside\t\t{fp}"]}[bad]
    with pytest.raises(SystemExit):
        rv.aggregate(run, _tsv(run, lines))
