"""④ 실측 드라이버(docs/ai-experiments/tools/label_run_driver.py) — 성공 · 판정 실패 · 입력 오류가 섞인 실행도 끝까지 집계한다(가짜 호출자, 실제 API 호출 없음)."""
import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image

from pipeline.stages import label
from pipeline.types import BBox, Line, MergeResult, OcrRegion, Section, SplitResult, TextBlock
from pipeline.vlm import LlmReply, VlmError

DRIVER = Path(__file__).resolve().parents[1] / "docs" / "ai-experiments" / "tools" / "label_run_driver.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("label_run_driver", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _block(key: str, text: str, order: int, section: str) -> TextBlock:
    bb = BBox(x=10, y=10 * order, w=100, h=20)
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text, score=0.9, poly=[[10, 10 * order], [110, 10 * order], [110, 10 * order + 20], [10, 10 * order + 20]], bbox=bb)
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=bb, regions=[reg])], bbox=bb, role="body", ocr_confidence=0.9)


def _make_input(root: Path) -> Path:
    """원본 1개 · 섹션 3개: sec_1_01 정상 · sec_1_02 이미지 크기 불일치(입력 오류) · sec_1_03 호출 실패."""
    src = root / "GS-99_001"
    (src / "sections").mkdir(parents=True)
    (src / "merge").mkdir()
    sections = []
    for i, (h, real_h) in enumerate([(200, 200), (200, 180), (200, 200)], start=1):
        key = f"sec_1_{i:02d}"
        img = src / "sections" / f"{key}.png"
        Image.new("RGB", (300, real_h), (255, 255, 255)).save(img)
        sections.append(Section(section_key=key, source_image_id=1, section_order=i, top_offset=200 * (i - 1), height=h, width=300, image_path=str(img)))
        (src / "merge" / f"{key}.json").write_text(MergeResult(section_key=key, blocks=[_block("blk_001", f"문구 {i}", 1, key)]).model_dump_json(), encoding="utf-8")
    split = SplitResult(source_image_id=1, source_width=300, source_height=600, sections=sections)
    (src / "split.json").write_text(split.model_dump_json(), encoding="utf-8")
    return root


class _Fake:
    config = {"model": "gemini-3.8-flash", "temperature": 0, "timeout_s": 60}

    def __call__(self, prompt, payload, image):
        if '"sec_1_03"' in payload:
            raise VlmError("504 DEADLINE_EXCEEDED")
        return LlmReply(text=json.dumps({"labels": [{"id": "b1", "is_product_label": True}]}), usage={"total_token_count": 10})


def test_driver_summarizes_mixed_success_failure_and_input_error(tmp_path, monkeypatch):
    drv = _load_driver()
    inp = _make_input(tmp_path / "input")
    monkeypatch.setenv(label.API_KEY_ENV, "dummy")
    monkeypatch.setattr(label, "default_assistant", lambda cfg: _Fake())
    out = tmp_path / "out"
    assert drv.main(["--input", str(inp), "--out", str(out), "--no-overlay"]) == 0
    rows = json.loads((out / "sections.json").read_text(encoding="utf-8"))
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    by = {r["section"]: r for r in rows}
    assert by["sec_1_01"]["status"] == "ok" and by["sec_1_01"]["true"] == 1
    assert by["sec_1_02"]["status"] == "driver_error" and "메타데이터" in by["sec_1_02"]["error"] and by["sec_1_02"]["true"] is None
    assert by["sec_1_03"]["status"] == "failed" and by["sec_1_03"]["record_status"] == "call_failed" and by["sec_1_03"]["true"] is None
    assert set(rows[0]) == set(rows[1]) == set(rows[2])  # 모든 행이 같은 키
    assert summary["run_status"] == "partial" and summary["status"] == {"ok": 1, "driver_error": 1, "failed": 1}
    assert summary["failed_sections"] == ["GS-99_001/sec_1_03"] and summary["driver_error_sections"] == ["GS-99_001/sec_1_02"]
    assert summary["true"] == 1 and summary["llm_calls"] == 2
    assert not (out / "GS-99_001" / "label" / "sec_1_02.json").exists()  # 입력 오류는 판정 결과를 만들지 않는다


def test_driver_refuses_existing_output(tmp_path, monkeypatch):
    drv = _load_driver()
    inp = _make_input(tmp_path / "input")
    out = tmp_path / "out"
    out.mkdir()
    assert drv.main(["--input", str(inp), "--out", str(out), "--dry-run"]) == 1


@pytest.mark.parametrize("sections, code", [("GS-99_001/sec_1_01", 0), ("GS-99_001/sec_9_99", 1)])
def test_driver_dry_run_and_section_filter(tmp_path, sections, code):
    drv = _load_driver()
    inp = _make_input(tmp_path / "input")
    out = tmp_path / "out"
    assert drv.main(["--input", str(inp), "--out", str(out), "--dry-run", "--sections", sections]) == code
    if code == 0:
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        assert summary["calls"] == 1 and summary["sent_blocks"] == 1
