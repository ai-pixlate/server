"""⑨ 넘침 검수 도구(docs/ai-experiments/tools/typeset_overflow_*) — 검수 TSV가 blocks.json · 검수 표본과 맞지 않으면 집계하지 않는다.
합성 blocks.json만 쓴다(폰트 · 회차 자료 불필요)."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "docs" / "ai-experiments" / "tools"
COLUMNS = "case_no\tgs\tblock_id\tverdict\tnote\treview_fingerprint"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass가 모듈을 찾을 수 있게 등록한다
    spec.loader.exec_module(mod)
    return mod


agg = _load("typeset_overflow_review_aggregate")


def _block(i, cls):
    return {"gs": "GS-01", "id": i, "cls": cls, "db_overflow": cls != "fit", "src_lines": 1, "tr_lines": 1, "same_text": False}


@pytest.fixture
def blocks(tmp_path):
    # leading 1 · 2, fit 3, wrap 10~18 — wrap은 표본 상한(8)을 넘어 하나(18)가 표본 밖이다
    rows = [_block(1, "leading"), _block(2, "leading"), _block(3, "fit")] + [_block(i, "wrap") for i in range(10, 19)]
    p = tmp_path / "blocks.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p, hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _tsv(tmp_path, lines, name="review.tsv"):
    p = tmp_path / name
    p.write_text("\n".join([COLUMNS, *lines]) + "\n", encoding="utf-8")
    return p


def test_valid_review_loads(blocks, tmp_path):
    path, fp = blocks
    rv = agg.load_review(path, _tsv(tmp_path, [f"1\tGS-01\t1\tinside\t메모\t{fp}", f"2\tGS-01\t10\toutside\t\t{fp}",
                                               f"3\tGS-01\t2\t\t\t{fp}"]))
    assert rv.verdict == {("GS-01", 1): "inside", ("GS-01", 10): "outside"}  # 빈 판정은 미검수
    assert [n["block_id"] for n in rv.notes] == [1]
    assert ("GS-01", 18) not in {(r["gs"], r["id"]) for r in rv.sample}


def test_other_fingerprint_with_same_ids_is_rejected(blocks, tmp_path):
    path, fp = blocks
    other = "0" * 16
    assert other != fp
    with pytest.raises(agg.ReviewInputError, match="지문"):
        agg.load_review(path, _tsv(tmp_path, [f"1\tGS-01\t1\tinside\t\t{other}"]))


def test_mixed_fingerprints_are_rejected(blocks, tmp_path):
    path, fp = blocks
    with pytest.raises(agg.ReviewInputError, match="지문"):
        agg.load_review(path, _tsv(tmp_path, [f"1\tGS-01\t1\tinside\t\t{fp}", f"2\tGS-01\t2\toutside\t\t{'f' * 16}"]))


@pytest.mark.parametrize("block_id", ["18", "999"])
def test_block_outside_review_sample_is_rejected(blocks, tmp_path, block_id):
    path, fp = blocks
    with pytest.raises(agg.ReviewInputError, match="표본"):
        agg.load_review(path, _tsv(tmp_path, [f"1\tGS-01\t{block_id}\tinside\t\t{fp}"]))


def test_unknown_verdict_is_rejected(blocks, tmp_path):
    path, fp = blocks
    with pytest.raises(agg.ReviewInputError, match="verdict"):
        agg.load_review(path, _tsv(tmp_path, [f"1\tGS-01\t1\tyes\t\t{fp}"]))


@pytest.mark.parametrize("second", ["inside", "outside"])
def test_duplicate_block_is_rejected_even_with_same_verdict(blocks, tmp_path, second):
    path, fp = blocks
    with pytest.raises(agg.ReviewInputError, match="중복"):
        agg.load_review(path, _tsv(tmp_path, [f"1\tGS-01\t1\tinside\t\t{fp}", f"2\tGS-01\t1\t{second}\t\t{fp}"]))


def test_missing_column_is_rejected(blocks, tmp_path):
    path, fp = blocks
    p = tmp_path / "review.tsv"
    p.write_text(f"case_no\tgs\tblock_id\tverdict\tnote\n1\tGS-01\t1\tinside\t\n", encoding="utf-8")
    with pytest.raises(agg.ReviewInputError, match="필수 열"):
        agg.load_review(path, p)


@pytest.mark.parametrize("tool", ["typeset_overflow_review_aggregate", "typeset_overflow_rule_compare"])
def test_cli_writes_nothing_on_bad_review(blocks, tmp_path, monkeypatch, tool):
    path, fp = blocks
    tsv = _tsv(tmp_path, [f"1\tGS-01\t1\tinside\t\t{'0' * 16}"])
    out = tmp_path / "out.json"
    mod = _load(tool)
    monkeypatch.setattr(sys, "argv", [tool, "--blocks", str(path), "--tsv", str(tsv), "--out", str(out)])
    assert mod.main() == 1
    assert not out.exists()
