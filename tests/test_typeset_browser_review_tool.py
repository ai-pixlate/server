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


def _blocks():
    js = ["overflow_quantized"] * 3 + ["overflow_subpixel"] + ["overflow_confirmed"] * 10 + ["fit"] * 10 + ["measure_failed"]
    return [{"key": f"GS-01/s1/b{i}", "judgement": j, "images": {"review": "x.png"} if j != "measure_failed" else {}}
            for i, j in enumerate(js)]


@pytest.fixture
def run(tmp_path):
    (tmp_path / "measure.json").write_text(json.dumps({"meta": {"inputSha256": "abc"}, "blocks": _blocks()}), encoding="utf-8")
    return tmp_path


def test_pick_takes_all_suspects_and_spaced_controls():
    cases = rv.pick(_blocks())
    js = [c["judgement"] for c in cases]
    assert js.count("overflow_quantized") == 3 and js.count("overflow_subpixel") == 1
    assert js.count("overflow_confirmed") == 6 and js.count("fit") == 6 and "measure_failed" not in js
    assert len({c["key"] for c in cases}) == len(cases)


def test_fingerprint_changes_with_judgement():
    cases = rv.pick(_blocks())
    a = rv.fingerprint({"inputSha256": "abc"}, cases)
    changed = [dict(c, judgement="fit") if i == 0 else c for i, c in enumerate(cases)]
    assert a != rv.fingerprint({"inputSha256": "abc"}, changed)


def _tsv(run, lines):
    p = run / "r.tsv"
    p.write_text(COLS + "".join(l + "\n" for l in lines), encoding="utf-8")
    return p


def _fp(run):
    m = json.loads((run / "measure.json").read_text(encoding="utf-8"))
    cases = rv.pick(m["blocks"])
    return cases, rv.fingerprint(m["meta"], cases)


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
