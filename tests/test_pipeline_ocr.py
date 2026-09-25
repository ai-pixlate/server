"""② 텍스트 추출 — 가짜 엔진으로 계약·변환·실패 처리·CLI 실행 구조를 검증한다.

실제 PaddleOCR 통합은 paddleocr가 설치된 환경에서만 돈다(맨 아래). 가짜 엔진 테스트는
어댑터가 같은 인덱스의 값을 한 영역으로 옮기는지만 보장하며, 실제 엔진 배열의 의미상 정렬은
설치 버전 프로브로 확인한다(pipeline.md 7절).
"""
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pipeline import config as cfgmod
from pipeline import jsonio
from pipeline import run as cli
from pipeline.errors import AnalyzeError
from pipeline.stages import ocr
from pipeline.types import BBox, Section, SplitResult

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SPLIT = ROOT / "pipeline" / "samples" / "synthetic_01" / "expected" / "split.json"


@pytest.fixture
def cfg():
    return cfgmod.load_config()


def _section(tmp_path: Path, h=200, w=100, key="sec_1_01", top=0, name=None) -> Section:
    path = tmp_path / (name or f"{key}.png")
    Image.new("RGB", (w, h), (255, 255, 255)).save(path)
    return Section(section_key=key, source_image_id=7, section_order=1, top_offset=top, height=h, width=w,
                   image_path=str(path))


class FakeEngine:
    def __init__(self, raw=None, exc=None):
        self.raw = raw if raw is not None else {"rec_polys": [], "rec_texts": [], "rec_scores": []}
        self.exc = exc
        self.calls = []
        self.info = {"engine": "fake"}

    def __call__(self, image):
        self.calls.append(image)
        if self.exc:
            raise self.exc
        return self.raw


def _raw(*regions):
    return {"rec_polys": [r[0] for r in regions], "rec_texts": [r[1] for r in regions],
            "rec_scores": [r[2] for r in regions], "dt_polys": [r[0] for r in regions]}


# ---- 변환 ---------------------------------------------------------------------------
def test_regions_keep_index_correspondence_and_order(cfg, tmp_path):
    sec = _section(tmp_path)
    raw = _raw(([[10, 20], [60, 20], [60, 34], [10, 34]], "첫째", 0.9),
               ([[5, 50], [90, 50], [90, 70], [5, 70]], "둘째", 0.5))
    res = ocr.run(sec, cfg, engine=FakeEngine(raw))
    assert [r.region_key for r in res.regions] == ["reg_0001", "reg_0002"]
    assert [(r.text, r.score) for r in res.regions] == [("첫째", 0.9), ("둘째", 0.5)]
    assert res.regions[1].poly == [(5, 50), (90, 50), (90, 70), (5, 70)]
    assert res.section_key == "sec_1_01" and res.schema_version == "2"


def test_empty_text_and_zero_score_are_kept(cfg, tmp_path):
    sec = _section(tmp_path)
    raw = _raw(([[1, 1], [9, 1], [9, 9], [1, 9]], "", 0.0), ([[1, 20], [9, 20], [9, 29], [1, 29]], "x", 0.01))
    res = ocr.run(sec, cfg, engine=FakeEngine(raw))
    assert [(r.text, r.score) for r in res.regions] == [("", 0.0), ("x", 0.01)]


def test_no_text_is_not_an_error(cfg, tmp_path):
    res = ocr.run(_section(tmp_path), cfg, engine=FakeEngine())
    assert res.regions == []


def test_poly_is_rounded_and_clipped_to_section_and_bbox_matches(cfg, tmp_path):
    sec = _section(tmp_path, h=200, w=100)
    tilted = [[-12.6, 30.2], [130.4, 60.0], [110.0, 230.7], [-3.0, 190.4]]  # 섹션 밖으로 나간 기울어진 사각형
    res = ocr.run(sec, cfg, engine=FakeEngine(_raw((tilted, "3", 0.388))))
    r = res.regions[0]
    assert r.poly == [(0, 30), (100, 60), (100, 200), (0, 190)]
    assert r.bbox == BBox.from_poly(r.poly) == BBox(x=0, y=30, w=100, h=170)
    assert all(0 <= x <= sec.width and 0 <= y <= sec.height for x, y in r.poly)


def test_coordinates_are_section_local_regardless_of_top_offset(cfg, tmp_path):
    sec = _section(tmp_path, top=5000)
    res = ocr.run(sec, cfg, engine=FakeEngine(_raw(([[10, 10], [20, 10], [20, 20], [10, 20]], "a", 1.0))))
    assert res.regions[0].bbox.y == 10  # top_offset을 더하지 않는다


def test_engine_receives_bgr_array_of_section_image(cfg, tmp_path):
    sec = _section(tmp_path, h=30, w=40)
    Image.new("RGB", (40, 30), (255, 0, 0)).save(sec.image_path)  # 빨강
    eng = FakeEngine()
    ocr.run(sec, cfg, engine=eng)
    img = eng.calls[0]
    assert img.shape == (30, 40, 3) and img.dtype == np.uint8 and tuple(img[0, 0]) == (0, 0, 255)


def test_non_ascii_image_path_is_readable(cfg, tmp_path):
    sec = _section(tmp_path, name="섹션_한글.png")
    assert ocr.run(sec, cfg, engine=FakeEngine()).regions == []


# ---- 실패 ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw, match", [
    ({"rec_polys": [[[0, 0], [1, 0], [1, 1]]], "rec_texts": [], "rec_scores": [0.9]}, "배열 길이 불일치"),
    ({"rec_texts": [], "rec_scores": []}, "키 없음"),
    ({"rec_polys": [[[0, 0], [1, 0], [1, 1]]], "rec_texts": ["a"], "rec_scores": [1.5]}, "형식 오류"),
])
def test_malformed_engine_output_is_ocr_failed(cfg, tmp_path, raw, match):
    with pytest.raises(AnalyzeError, match=match) as ei:
        ocr.run(_section(tmp_path), cfg, engine=FakeEngine(raw))
    assert ei.value.code == "OCR_FAILED" and ei.value.source_image_id == 7


@pytest.mark.parametrize("raw", [None, [], "text", {"rec_polys": None, "rec_texts": [], "rec_scores": []},
                                 {"rec_polys": [], "rec_texts": 3, "rec_scores": []}])
def test_non_dict_or_non_array_output_is_ocr_failed(cfg, tmp_path, raw):
    with pytest.raises(AnalyzeError) as ei:
        ocr.run(_section(tmp_path), cfg, engine=FakeEngine(raw) if raw is not None else lambda img: None)
    assert ei.value.code == "OCR_FAILED"


def test_image_size_must_match_section_metadata(cfg, tmp_path):
    sec = _section(tmp_path, h=200, w=100)
    Image.new("RGB", (100, 4500), (255, 255, 255)).save(sec.image_path)  # 메타데이터 200, 실제 4500
    eng = FakeEngine(_raw(([[1, 4100], [9, 4100], [9, 4110], [1, 4110]], "a", 1.0)))
    with pytest.raises(AnalyzeError, match="크기 불일치") as ei:
        ocr.run(sec, cfg, engine=eng)
    assert (ei.value.code, ei.value.retryable) == ("IMAGE_OPEN_FAILED", False) and not eng.calls


def test_inference_exception_is_ocr_failed(cfg, tmp_path):
    with pytest.raises(AnalyzeError, match="추론 실패") as ei:
        ocr.run(_section(tmp_path), cfg, engine=FakeEngine(exc=RuntimeError("boom")))
    assert ei.value.code == "OCR_FAILED"


@pytest.mark.parametrize("content", [None, b"", b"not an image"])
def test_unreadable_section_image_is_image_open_failed(cfg, tmp_path, content):
    sec = _section(tmp_path)
    p = Path(sec.image_path)
    if content is None:
        p.unlink()
    else:
        p.write_bytes(content)
    eng = FakeEngine()
    with pytest.raises(AnalyzeError) as ei:
        ocr.run(sec, cfg, engine=eng)
    assert (ei.value.code, ei.value.retryable) == ("IMAGE_OPEN_FAILED", False) and not eng.calls


def test_engine_init_failure_inside_run_is_ocr_failed(cfg, tmp_path, monkeypatch):
    def boom(_cfg):
        raise ocr.OcrEngineInitError("no model")
    monkeypatch.setattr(ocr, "build_engine", boom)
    with pytest.raises(AnalyzeError, match="엔진 초기화 실패"):
        ocr.run(_section(tmp_path), cfg)


def test_section_over_split_threshold_is_rejected_not_passed(cfg, tmp_path):
    eng = FakeEngine()
    ocr.run(_section(tmp_path, h=4000), cfg, engine=eng)  # 4000은 분할 대상 아님
    with pytest.raises(NotImplementedError, match="임시 분할 미구현"):
        ocr.run(_section(tmp_path, h=4001, key="sec_1_02"), cfg, engine=eng)
    assert len(eng.calls) == 1


def test_unsupported_preprocess_is_rejected(cfg):
    cfg["ocr"]["preprocess"] = "gray"
    with pytest.raises(ValueError, match="preprocess"):
        ocr.PaddleOcrEngine(cfg["ocr"])


# ---- CLI 실행 구조 (dev.md 4절) -------------------------------------------------------
def _split_dir(tmp_path: Path, heights=(100, 120, 90), bad: int | None = None) -> Path:
    d = tmp_path / "input"
    (d / "sections").mkdir(parents=True)
    secs, top = [], 0
    for i, h in enumerate(heights, 1):
        key = f"sec_1_{i:02d}"
        p = d / "sections" / f"{key}.png"
        if i == bad:
            p.write_bytes(b"broken")
        else:
            Image.new("RGB", (50, h), (255, 255, 255)).save(p)
        secs.append(Section(section_key=key, source_image_id=1, section_order=i, top_offset=top, height=h, width=50,
                            image_path=str(p)))
        top += h
    jsonio.write_model(d / "split.json", SplitResult(source_image_id=1, source_width=50, source_height=top, sections=secs))
    return d / "split.json"


@pytest.fixture
def fake_engine(monkeypatch):
    eng = FakeEngine(_raw(([[1, 1], [20, 1], [20, 9], [1, 9]], "글자", 0.95)))
    monkeypatch.setattr(ocr, "build_engine", lambda cfg: eng)
    return eng


def _record(out: Path) -> dict:
    return json.loads((out / "run.json").read_text(encoding="utf-8"))


def test_cli_ocr_all_ok(tmp_path, fake_engine):
    split = _split_dir(tmp_path)
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(split), "--out", str(out)]) == 0
    rec = _record(out)
    assert rec["status"] == "ok" and rec["run_error"] is None and rec["engine"] == {"engine": "fake"}
    assert {k: v["status"] for k, v in rec["sections"].items()} == dict.fromkeys(("sec_1_01", "sec_1_02", "sec_1_03"), "ok")
    assert "git_commit" in rec
    assert jsonio.load_ocr(out / "ocr" / "sec_1_02.json").regions[0].text == "글자"


def test_cli_ocr_section_failure_continues_and_marks_partial(tmp_path, fake_engine, capsys):
    split = _split_dir(tmp_path, bad=2)
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(split), "--out", str(out)]) == 2
    rec = _record(out)
    assert rec["status"] == "partial"
    s = rec["sections"]
    assert s["sec_1_01"]["status"] == s["sec_1_03"]["status"] == "ok"
    assert s["sec_1_02"]["status"] == "failed" and s["sec_1_02"]["code"] == "IMAGE_OPEN_FAILED" and s["sec_1_02"]["retryable"] is False
    assert not (out / "ocr" / "sec_1_02.json").exists()  # 실패를 빈 결과로 바꿔치지 않는다
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1])["code"] == "IMAGE_OPEN_FAILED"


def test_cli_ocr_malformed_output_keeps_going_and_writes_record(tmp_path, monkeypatch):
    calls = iter([None, _raw(([[1, 1], [9, 1], [9, 9], [1, 9]], "a", 0.9)), {"rec_polys": None, "rec_texts": [], "rec_scores": []}])
    monkeypatch.setattr(ocr, "build_engine", lambda cfg: FakeEngineFn(lambda img: next(calls)))
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(_split_dir(tmp_path)), "--out", str(out)]) == 2
    rec = _record(out)
    assert rec["status"] == "partial"
    assert [v["status"] for v in rec["sections"].values()] == ["failed", "ok", "failed"]
    assert {v.get("code") for v in rec["sections"].values() if v["status"] == "failed"} == {"OCR_FAILED"}


class FakeEngineFn:
    def __init__(self, fn):
        self.fn = fn
        self.info = {"engine": "fake"}

    def __call__(self, image):
        return self.fn(image)


def test_cli_ocr_all_failed(tmp_path, fake_engine):
    fake_engine.exc = RuntimeError("boom")
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(_split_dir(tmp_path)), "--out", str(out)]) == 2
    rec = _record(out)
    assert rec["status"] == "failed" and all(v["code"] == "OCR_FAILED" for v in rec["sections"].values())


def test_cli_ocr_engine_init_failure_stops_run(tmp_path, monkeypatch, capsys):
    def boom(cfg):
        raise ocr.OcrEngineInitError("ValueError: no model")
    monkeypatch.setattr(ocr, "build_engine", boom)
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(_split_dir(tmp_path)), "--out", str(out)]) == 2
    rec = _record(out)
    assert rec["status"] == "failed" and rec["run_error"]["code"] == "OCR_FAILED"
    assert rec["run_error"]["exception"] == "OcrEngineInitError"
    assert set(v["status"] for v in rec["sections"].values()) == {"not_run"}
    assert not (out / "ocr").exists()
    assert json.loads(capsys.readouterr().err.strip())["code"] == "OCR_FAILED"


def test_cli_ocr_oversized_section_fails_with_exit_3(tmp_path, fake_engine):
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(_split_dir(tmp_path, heights=(100, 4001))), "--out", str(out)]) == 3
    rec = _record(out)
    assert rec["status"] == "partial"
    assert rec["sections"]["sec_1_02"]["reason"] == "임시 분할 미구현"


def test_cli_ocr_refuses_reused_output_folder(tmp_path, fake_engine):
    split = _split_dir(tmp_path)
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(split), "--out", str(out)]) == 0
    with pytest.raises(FileExistsError):
        cli.main(["ocr", "--split", str(split), "--out", str(out)])


def test_cli_ocr_single_section(tmp_path, fake_engine):
    out = tmp_path / "out"
    assert cli.main(["ocr", "--split", str(_split_dir(tmp_path)), "--section", "sec_1_02", "--out", str(out)]) == 0
    assert list(_record(out)["sections"]) == ["sec_1_02"]


def test_cli_split_image_open_failure_exits_2_with_json(tmp_path, capsys):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert cli.main(["split", "--source", str(bad), "--out", str(tmp_path / "o")]) == 2
    assert json.loads(capsys.readouterr().err.strip())["code"] == "IMAGE_OPEN_FAILED"


# ---- 실제 PaddleOCR (설치된 환경에서만) ------------------------------------------------
def test_real_paddleocr_on_synthetic_section(cfg):
    pytest.importorskip("paddleocr")
    sec = jsonio.load_split(SAMPLE_SPLIT).sections[0]
    res = ocr.run(sec, cfg)
    assert res.regions, "합성 섹션(영문 제목·본문)에서 영역이 하나도 나오지 않음"
    for r in res.regions:
        assert 0.0 <= r.score <= 1.0 and r.bbox == BBox.from_poly(r.poly)
        assert all(0 <= x <= sec.width and 0 <= y <= sec.height for x, y in r.poly)
    info = ocr.build_engine(cfg).info
    assert info["settings"]["text_rec_score_thresh"] == 0.0
    assert info["device"] in ("cpu", "gpu:0")
    if info["device"] != "cpu":  # oneDNN 우회는 Windows CPU에만
        assert info["enable_mkldnn"] == "library default"
