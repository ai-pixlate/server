"""CLI·오류·오버레이 검증 — 모델 없이 도는 부분만."""
import json
from pathlib import Path

import pytest
from PIL import Image

from pipeline import run as cli
from pipeline.errors import AnalyzeError, image_open_failed
from pipeline.stages.section_split import open_source
from pipeline.types import SourceImage

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "pipeline" / "samples" / "synthetic_01"


def test_analyze_error_policy():
    e = image_open_failed("x.png", 7)
    assert (e.code, e.retryable, e.source_image_id) == ("IMAGE_OPEN_FAILED", False, 7)
    assert AnalyzeError("OCR_FAILED", "t").retryable is True
    with pytest.raises(ValueError):
        AnalyzeError("UNKNOWN", "t")
    assert AnalyzeError("UNKNOWN", "t", retryable=False).to_dict()["retryable"] is False


def test_open_source_broken_file_raises_image_open_failed(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    with pytest.raises(AnalyzeError) as ei:
        open_source(SourceImage(source_image_id=3, upload_order=1, path=str(bad)))
    assert ei.value.code == "IMAGE_OPEN_FAILED" and not ei.value.retryable


def test_cli_config_prints_effective_values(capsys):
    assert cli.main(["config", "--set", "merge.line_gap=0.9"]) == 0
    out = capsys.readouterr().out
    assert "merge.line_gap = 0.9" in out


def test_cli_unimplemented_stage_exits_3(tmp_path):
    exp = SAMPLE / "expected"
    rc = cli.main(["merge", "--split", str(exp / "split.json"), "--ocr", str(exp / "ocr/sec_1_01.json"), "--out", str(tmp_path)])
    assert rc == 3  # ③ 미구현


def test_cli_inspect_draws_overlays(tmp_path):
    exp = SAMPLE / "expected"
    sec_png = ROOT / "pipeline/samples/synthetic_01/expected/sections/sec_1_01.png"
    assert cli.main(["inspect", "--split", str(exp / "split.json"), "--image", str(SAMPLE / "source.png"), "--out", str(tmp_path / "split.png")]) == 0
    assert cli.main(["inspect", "--ocr", str(exp / "ocr/sec_1_01.json"), "--image", str(sec_png), "--out", str(tmp_path / "ocr.png")]) == 0
    assert cli.main(["inspect", "--merge", str(exp / "merge/sec_1_01.json"), "--image", str(sec_png), "--out", str(tmp_path / "merge.png")]) == 0
    for name in ("split.png", "ocr.png", "merge.png"):
        assert Image.open(tmp_path / name).size[0] == 600


def test_sample_generator_is_reproducible(tmp_path):
    from pipeline.samples.make_synthetic import main as make

    make(tmp_path / "synthetic_01")
    for rel in ("expected/split.json", "expected/ocr/sec_1_01.json", "expected/merge/sec_1_02.json"):
        a = json.loads((tmp_path / "synthetic_01" / rel).read_text(encoding="utf-8"))
        b = json.loads((SAMPLE / rel).read_text(encoding="utf-8"))
        for d in (a, b):  # 섹션 이미지 경로만 생성 위치에 따라 다르다
            for s in d.get("sections", []):
                s.pop("image_path")
        assert a == b
