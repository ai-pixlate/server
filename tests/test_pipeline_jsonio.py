"""JSON 읽기·쓰기 — 버전 확인 · image_path 해석(JSON 폴더 기준) · 전환 · 고정 입력본 (dev.md 3·4·5절)."""
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from pipeline import jsonio
from pipeline import run as cli
from pipeline.types import AnalyzeResult, MergeResult, OcrResult, Section, SplitResult

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "pipeline" / "samples" / "synthetic_01"


def _split_v1(tmp_path: Path, heights=(300, 200), width=40) -> tuple[Path, Path]:
    """버전 1 형식 실행 출력: tmp/base/run/split.json, image_path는 base 기준 상대 경로."""
    base = tmp_path / "base"
    run = base / "run"
    (run / "sections").mkdir(parents=True)
    sections, top = [], 0
    for i, h in enumerate(heights, 1):
        key = f"sec_1_{i:02d}"
        Image.new("RGB", (width, h), (i * 40, 0, 0)).save(run / "sections" / f"{key}.png")
        sections.append({"section_key": key, "source_image_id": 1, "section_order": i, "top_offset": top,
                         "height": h, "width": width, "image_path": f"run/sections/{key}.png"})
        top += h
    raw = {"schema_version": "1", "source_image_id": 1, "source_width": width, "source_height": top, "sections": sections}
    (run / "split.json").write_text(json.dumps(raw), encoding="utf-8")
    (run / "split_debug.json").write_text("{}", encoding="utf-8")
    (run / "run.json").write_text('{"stage": "split"}', encoding="utf-8")
    return base, run / "split.json"


def _tree_hashes(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): jsonio.sha256_file(p) for p in sorted(d.rglob("*")) if p.is_file()}


# ---- 읽기: 버전 ---------------------------------------------------------------------
def test_sample_split_resolves_relative_to_json_folder():
    split = jsonio.load_split(SAMPLE / "expected" / "split.json")
    assert split.schema_version == "2"
    assert Path(split.sections[0].image_path) == (SAMPLE / "expected" / "sections" / "sec_1_01.png").resolve()


@pytest.mark.parametrize("raw_version, cls, match", [
    (None, SplitResult, "schema_version 없음"),
    ("3", SplitResult, "미지원"),
    ("1", SplitResult, "convert-split"),
    (None, OcrResult, "schema_version 없음"),
])
def test_missing_or_unsupported_version_is_rejected(tmp_path, raw_version, cls, match):
    src = SAMPLE / "expected" / ("split.json" if cls is SplitResult else "ocr/sec_1_01.json")
    raw = json.loads(src.read_text(encoding="utf-8"))
    if raw_version is None:
        raw.pop("schema_version")
    else:
        raw["schema_version"] = raw_version
    p = tmp_path / "x.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(jsonio.SchemaVersionError, match=match):
        jsonio.load_model(p, cls)


def _sec(image_path: str) -> Section:
    return Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=10, width=10,
                   image_path=image_path)


def test_default_output_versions_common_1_split_2():
    # 공통 결과 버전(워커 인계 형식 포함)은 "1", split.json 파일 버전만 "2"
    assert AnalyzeResult(sections=[], blocks=[]).schema_version == "1"
    assert OcrResult(section_key="s", regions=[]).schema_version == "1"
    assert MergeResult(section_key="s", blocks=[]).schema_version == "1"
    assert SplitResult(source_image_id=1, source_width=10, source_height=10, sections=[_sec("x.png")]).schema_version == "2"


def test_analyze_result_is_written_verbatim_without_path_rule(tmp_path):
    # AnalyzeResult(워커 인계 형식)는 PR 이전과 같이 버전 1, image_path를 손대지 않고 쓴다(JSON 폴더 기준 상대화 없음)
    given = "pipeline/out/run7/sections/sec_1_01.png"  # 실행 시점의 경로 문자열 그대로(작업 디렉터리 기준 표기)
    res = AnalyzeResult(sections=[_sec(given)], blocks=[])
    out = tmp_path / "deep" / "analyze.json"
    jsonio.write_model(out, res)
    raw = json.loads(out.read_text(encoding="utf-8"))
    assert raw["schema_version"] == "1" and raw["sections"][0]["image_path"] == given
    assert raw == json.loads(res.model_dump_json())  # 쓴 내용 = 모델 그대로


def test_analyze_result_is_not_a_loader_target(tmp_path):
    p = tmp_path / "analyze.json"
    p.write_text(json.dumps({"schema_version": "1", "sections": [], "blocks": [], "warnings": []}), encoding="utf-8")
    with pytest.raises(TypeError, match="로더의 대상이 아니다"):
        jsonio.load_model(p, AnalyzeResult)
    assert not hasattr(jsonio, "load_analyze")


def test_split_path_rule_does_not_propagate_through_shared_sections(tmp_path):
    # analyze()는 split.sections를 그대로 AnalyzeResult에 담는다. split.json을 쓰더라도 그 Section 객체가 바뀌면 안 된다.
    img = (tmp_path / "sections" / "sec_1_01.png").resolve()
    img.parent.mkdir()
    Image.new("RGB", (10, 10)).save(img)
    sec = _sec(str(img))
    split = SplitResult(source_image_id=1, source_width=10, source_height=10, sections=[sec])
    jsonio.write_model(tmp_path / "split.json", split)
    assert json.loads((tmp_path / "split.json").read_text(encoding="utf-8"))["sections"][0]["image_path"] == "sections/sec_1_01.png"
    assert sec.image_path == str(img) and split.sections[0] is sec  # 공용 Section은 그대로
    res = AnalyzeResult(sections=split.sections, blocks=[])
    jsonio.write_model(tmp_path / "analyze.json", res)
    assert json.loads((tmp_path / "analyze.json").read_text(encoding="utf-8"))["sections"][0]["image_path"] == str(img)


@pytest.mark.parametrize("version", ["1", "2"])  # "2"는 2026-09-23~25 사이 생성된 파일(구조 동일)
def test_ocr_and_merge_accept_version_1_and_2(tmp_path, version):
    for sub in ("ocr", "merge"):
        raw = json.loads((SAMPLE / "expected" / sub / "sec_1_01.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == "1"  # 레포 기대 파일은 기본 출력 버전
        raw["schema_version"] = version
        p = tmp_path / f"{sub}.json"
        p.write_text(json.dumps(raw), encoding="utf-8")
        assert jsonio.load_model(p, jsonio.OcrResult if sub == "ocr" else jsonio.MergeResult).section_key == "sec_1_01"


# ---- 읽기: 경로 ---------------------------------------------------------------------
def test_resolution_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    expected = jsonio.load_split(SAMPLE / "expected" / "split.json").sections[1].image_path
    monkeypatch.chdir(tmp_path)
    assert jsonio.load_split(SAMPLE / "expected" / "split.json").sections[1].image_path == expected


def test_moved_folder_still_loads_from_its_new_location(tmp_path):
    moved = tmp_path / "moved"
    shutil.copytree(SAMPLE / "expected", moved)
    split = jsonio.load_split(moved / "split.json")
    for s in split.sections:
        assert Path(s.image_path).is_relative_to(moved.resolve()) and Path(s.image_path).is_file()


def test_absolute_image_path_is_kept(tmp_path):
    raw = json.loads((SAMPLE / "expected" / "split.json").read_text(encoding="utf-8"))
    absolute = str((SAMPLE / "expected" / "sections" / "sec_1_01.png").resolve())
    raw["sections"][0]["image_path"] = absolute
    p = tmp_path / "split.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert jsonio.load_split(p).sections[0].image_path == absolute


def test_write_then_load_round_trips_paths(tmp_path):
    split = jsonio.load_split(SAMPLE / "expected" / "split.json")
    out = tmp_path / "copy" / "split.json"
    jsonio.write_model(out, split)
    stored = json.loads(out.read_text(encoding="utf-8"))["sections"][0]["image_path"]
    assert not Path(stored).is_absolute() and "\\" not in stored  # JSON 폴더 기준 상대 경로, / 구분
    assert jsonio.load_split(out).sections[0].image_path == split.sections[0].image_path


def test_cli_split_writes_version_2_with_folder_relative_paths(tmp_path):
    assert cli.main(["split", "--source", str(SAMPLE / "source.png"), "--out", str(tmp_path / "o"),
                     "--set", "section.long_section_px=999999"]) == 0
    raw = json.loads((tmp_path / "o" / "split.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == "2"
    assert [s["image_path"] for s in raw["sections"]] == ["sections/sec_1_01.png", "sections/sec_1_02.png"]
    record = json.loads((tmp_path / "o" / "run.json").read_text(encoding="utf-8"))
    assert "git_commit" in record and "git_dirty" in record


# ---- 일반 전환 ----------------------------------------------------------------------
def test_convert_split_keeps_image_targets(tmp_path):
    base, v1 = _split_v1(tmp_path)
    dst = tmp_path / "elsewhere" / "split.json"
    original = jsonio.convert_split(v1, dst, base)
    assert original["sec_1_01"] == "run/sections/sec_1_01.png"
    split = jsonio.load_split(dst)
    assert Path(split.sections[0].image_path) == (v1.parent / "sections" / "sec_1_01.png").resolve()
    with pytest.raises(FileExistsError):
        jsonio.convert_split(v1, dst, base)


def test_convert_split_fails_when_target_image_missing(tmp_path):
    base, v1 = _split_v1(tmp_path)
    with pytest.raises(jsonio.InputCheckError, match="이미지 없음"):
        jsonio.convert_split(v1, tmp_path / "x.json", tmp_path)  # 잘못된 base


# ---- 고정 입력본 --------------------------------------------------------------------
def test_freeze_input_copies_retargets_and_leaves_source_untouched(tmp_path):
    base, v1 = _split_v1(tmp_path)
    before = _tree_hashes(base)
    out = tmp_path / "v1" / "GS-00_000"
    prov = jsonio.freeze_input(v1, out, base=base, meta={"run": "r5/full", "commit": "6d09dfb"})
    assert _tree_hashes(base) == before
    assert prov["verification"]["ok"] and prov["meta"]["run"] == "r5/full" and prov["source_schema_version"] == "1"
    assert {"origin/split.json", "origin/split_debug.json", "origin/run.json"} == set(prov["origin_files"])
    split = jsonio.load_split(out / "split.json")
    for s in split.sections:
        assert Path(s.image_path).is_relative_to(out.resolve())
    # 원본 실행 출력을 지워도(여기서는 테스트용 사본) 입력본만으로 검증된다
    shutil.rmtree(base)
    assert jsonio.verify_input(out)["sections"] == 2


def test_freeze_input_accepts_version_2_source(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(SAMPLE / "expected", src)
    prov = jsonio.freeze_input(src / "split.json", tmp_path / "v1")
    assert prov["source_schema_version"] == "2" and prov["sections"][1]["original_image_path"] == "sections/sec_1_02.png"


def test_freeze_input_refuses_non_empty_output(tmp_path):
    base, v1 = _split_v1(tmp_path)
    out = tmp_path / "v1"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        jsonio.freeze_input(v1, out, base=base)


def test_verify_input_rejects_reference_outside_input(tmp_path):
    base, v1 = _split_v1(tmp_path)
    out = tmp_path / "v1"
    jsonio.freeze_input(v1, out, base=base)
    raw = json.loads((out / "split.json").read_text(encoding="utf-8"))
    raw["sections"][0]["image_path"] = str((v1.parent / "sections" / "sec_1_01.png").resolve())  # 원본 참조
    (out / "split.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(jsonio.InputCheckError, match="입력본 밖"):
        jsonio.verify_input(out)
    with pytest.raises(jsonio.InputCheckError, match="입력본 밖"):
        jsonio.verify_relocated(out)


def test_verify_input_detects_changed_image(tmp_path):
    base, v1 = _split_v1(tmp_path)
    out = tmp_path / "v1"
    jsonio.freeze_input(v1, out, base=base)
    Image.new("RGB", (40, 300), (1, 2, 3)).save(out / "sections" / "sec_1_01.png")
    with pytest.raises(jsonio.InputCheckError, match="SHA-256"):
        jsonio.verify_input(out)


@pytest.mark.parametrize("sections, match", [
    ([(0, 300), (301, 199)], "끝 300"),          # 틈
    ([(10, 300), (310, 190)], "첫 섹션 시작"),
    ([(0, 300), (300, 150)], "source_height"),  # 합이 모자람
])
def test_continuity_checks_offsets_not_just_sum(sections, match):
    secs = [Section(section_key=f"s{i}", source_image_id=1, section_order=i, top_offset=t, height=h, width=10,
                    image_path="x.png") for i, (t, h) in enumerate(sections, 1)]
    split = SplitResult(source_image_id=1, source_width=10, source_height=500, sections=secs)
    with pytest.raises(jsonio.InputCheckError, match=match):
        jsonio.check_continuity(split)


def test_cli_freeze_and_verify_input(tmp_path):
    base, v1 = _split_v1(tmp_path)
    out = tmp_path / "v1"
    assert cli.main(["freeze-input", "--split", str(v1), "--base", str(base), "--out", str(out),
                     "--meta", "round=run3", "--meta", "criterion=정답 2891 유지"]) == 0
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert prov["meta"] == {"round": "run3", "criterion": "정답 2891 유지"}
    assert cli.main(["verify-input", "--dir", str(out), "--relocated"]) == 0
