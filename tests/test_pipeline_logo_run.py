"""⑤ 실행 계층 — CLI `logo` · 실측 드라이버 · execute_logo · 원자적 저장(실제 임시 디렉터리, 모델 호출 없음).

기대 동작은 open-questions #68 · pipeline.md 7.5절(사용자 결정 1~5 포함):
- 종료 코드 0 성공 · 2 입력 · 설정 오류(판정 결과 없음, run.json만) · 4 실행 · 저장 오류 · 사전 검사 내부 예외.
- 기존 출력 폴더 거부(덮어쓰지 않음). run.json 저장 자체가 실패하면 4이고 기록했다고 보고하지 않는다.
- 판정 중 예기치 않은 오류 → 그 섹션 failed 결과 · 기록 후 전체 중단, 완료 섹션 보존, 전체 failed · 미처리 건수.
- 드라이버: 최상위 SHA256SUMS를 뺀 파일 집합 = 해시 목록(누락 · 불일치 · 목록 밖 거부, 하위 SHA256SUMS는 일반 파일),
  연결 입력본 해시, products[].images ⊆ 입력본 원본, not_in_input 제외 원본 ∩ 개발 대상 = ∅, (원본, 섹션) 중복 거부.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from pipeline import jsonio
from pipeline import run as runmod
from pipeline.stages import label as label_stage
from pipeline.stages import logo
from pipeline.types import BBox, LabelChecked, LabelDecision, LabelResult, Line, LogoResult, MergeResult, OcrRegion, TextBlock

DRIVER = Path(__file__).resolve().parents[1] / "docs" / "ai-experiments" / "tools" / "logo_run_driver.py"


def _load_driver():
    spec = importlib.util.spec_from_file_location("logo_run_driver", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _block(key, text, order, section):
    bb = BBox(x=10, y=40 * order, w=200, h=30)
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text, score=0.9,
                    poly=[[10, 40 * order], [210, 40 * order], [210, 40 * order + 30], [10, 40 * order + 30]], bbox=bb)
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=bb, regions=[reg])], bbox=bb, role="body", ocr_confidence=0.9)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _write_sums(root: Path) -> None:
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.relative_to(root).as_posix() != "SHA256SUMS")
    (root / "SHA256SUMS").write_text("".join(f"{_sha(p)}  {p.relative_to(root).as_posix()}\n" for p in files), encoding="utf-8")


# 원본 2개가 같은 section_key(sec_1_01)를 쓴다 — 원본이 다르면 허용
SECTIONS = {
    "GS-01_001": {"sec_1_01": (["구달", "맑은 어성초", "GOODAL"], {"blk_003": True}),
                  "sec_1_02": (["goodal®", ""], {})},
    "GS-01_002": {"sec_1_01": (["goodal", "기타"], {})},
}


def make_input(root: Path, sections=SECTIONS, manifest_extra=None) -> Path:
    root.mkdir(parents=True)
    images = []
    for image_id, secs in sections.items():
        rows = []
        for key, (texts, labels) in secs.items():
            blocks = [_block(f"blk_{i + 1:03d}", t, i + 1, key) for i, t in enumerate(texts)]
            lab = LabelResult(section_key=key, status="ok",
                              labels=[LabelDecision(block_key=b.block_key, is_product_label=labels.get(b.block_key, False), basis="vlm") for b in blocks],
                              checked=LabelChecked(input_fingerprint=label_stage.input_fingerprint(blocks), llm_called=True))
            jsonio.write_model(root / image_id / "merge" / f"{key}.json", MergeResult(section_key=key, blocks=blocks))
            jsonio.write_model(root / image_id / "label" / f"{key}.json", lab)
            rows.append({"section_key": key})
        images.append({"image": image_id, "sections": rows})
    manifest = {"name": "logo-input-test", "images": images, **(manifest_extra or {})}
    (root / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (root / "upstream").mkdir()
    (root / "upstream" / "SHA256SUMS").write_text("nested list (일반 파일)\n", encoding="utf-8")
    _write_sums(root)
    return root


def make_brand(root: Path, inp: Path, **over) -> Path:
    root.mkdir(parents=True)
    meta = {
        "version": "brand-meta-test",
        "for_input": {"name": "logo-input-test", "path": "옛/경로/출처기록", "sha256sums_sha256": _sha(inp / "SHA256SUMS")},
        "products": [{"image_group": "GS-01", "product_name": "추적용", "name_ko": "구달", "name_ko_status": "provided",
                      "name_en": "goodal", "name_en_status": "provided", "images": ["GS-01_001", "GS-01_002"]}],
        "not_in_input": {"images": {"GS-01_009": "GS-01"}},
    }
    meta.update(over)
    (root / "brand_metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    _write_sums(root)
    return root


@pytest.fixture
def bundles(tmp_path):
    inp = make_input(tmp_path / "input")
    return inp, make_brand(tmp_path / "brand", inp)


def _run_json(out: Path) -> dict:
    return json.loads((out / "run.json").read_text(encoding="utf-8"))


def _result_files(out: Path) -> list[Path]:
    return sorted(out.rglob("logo/*.json")) if out.exists() else []


def _tree_hash(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# 드라이버
# ---------------------------------------------------------------------------
def test_driver_success_and_inputs_unchanged(bundles, tmp_path):
    inp, brand = bundles
    before = (_tree_hash(inp), _tree_hash(brand))
    out = tmp_path / "out"
    assert _load_driver().main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 0
    assert (_tree_hash(inp), _tree_hash(brand)) == before
    rj = _run_json(out)
    assert rj["status"] == "ok" and rj["error"] is None
    c = rj["counts"]
    assert (c["sections_total"], c["sections_ok"], c["sections_failed"], c["sections_not_run"]) == (3, 3, 0, 0)
    # 기대값은 규칙에서: 구달 · 맑은 어성초 · GOODAL(④ true) / goodal® · "" / goodal · 기타
    assert (c["blocks"], c["product_label"], c["exact_match"], c["no_match"], c["empty_text"]) == (7, 1, 2, 3, 1)
    assert rj["inputs"]["input_sha256sums_sha256"] == _sha(inp / "SHA256SUMS")
    assert rj["inputs"]["brand_sha256sums_sha256"] == _sha(brand / "SHA256SUMS")
    assert rj["inputs"]["brand_not_in_input"] == ["GS-01_009"]
    assert {"python", "unicode_version", "git_commit", "git_dirty", "started_at", "duration_s"} <= set(rj)
    r = jsonio.load_model(out / "GS-01_002" / "logo" / "sec_1_01.json", LogoResult)
    assert [(d.is_brand_logo, d.basis) for d in r.decisions] == [(True, "exact_match"), (False, "no_match")]
    assert (out / "GS-01_001" / "logo" / "sec_1_01.json").exists()  # 같은 section_key, 다른 원본 — 둘 다 저장
    assert not list(out.rglob("*.tmp"))


def test_driver_refuses_existing_out(bundles, tmp_path):
    inp, brand = bundles
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x", encoding="utf-8")
    assert _load_driver().main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 2
    assert [p.name for p in out.iterdir()] == ["keep.txt"]


def _driver_input_error(inp, brand, tmp_path) -> dict:
    out = tmp_path / "out_err"
    assert _load_driver().main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 2
    rj = _run_json(out)
    assert rj["status"] == "input_error" and rj["error"]
    assert _result_files(out) == []  # 판정 결과 없음
    return rj


def test_driver_hash_mismatch(bundles, tmp_path):
    inp, brand = bundles
    p = inp / "GS-01_001" / "merge" / "sec_1_02.json"
    p.write_text(p.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert "해시 불일치" in _driver_input_error(inp, brand, tmp_path)["error"]


def test_driver_missing_file(bundles, tmp_path):
    inp, brand = bundles
    (inp / "GS-01_002" / "label" / "sec_1_01.json").unlink()
    assert "없음" in _driver_input_error(inp, brand, tmp_path)["error"]


def test_driver_unlisted_file_rejected_not_deleted(bundles, tmp_path):
    inp, brand = bundles
    extra = inp / "GS-01_001" / "desktop.ini"
    extra.write_text("x", encoding="utf-8")
    assert "목록 밖" in _driver_input_error(inp, brand, tmp_path)["error"]
    assert extra.exists()


def test_driver_nested_sha256sums_is_ordinary_file(bundles, tmp_path):
    inp, brand = bundles
    (inp / "upstream" / "SHA256SUMS").write_text("changed\n", encoding="utf-8")
    assert "upstream/SHA256SUMS" in _driver_input_error(inp, brand, tmp_path)["error"]


def test_driver_brand_file_set_checked(bundles, tmp_path):
    inp, brand = bundles
    (brand / "note.txt").write_text("x", encoding="utf-8")
    assert "브랜드 자료" in _driver_input_error(inp, brand, tmp_path)["error"]


def test_driver_link_hash_mismatch(tmp_path):
    inp = make_input(tmp_path / "input")
    brand = make_brand(tmp_path / "brand", inp, for_input={"name": "logo-input-test", "sha256sums_sha256": "0" * 64})
    assert "연결 입력본" in _driver_input_error(inp, brand, tmp_path)["error"]


def test_driver_brand_image_outside_input(tmp_path):
    inp = make_input(tmp_path / "input")
    products = [{"image_group": "GS-01", "name_ko": "구달", "name_ko_status": "provided", "name_en": "goodal",
                 "name_en_status": "provided", "images": ["GS-01_001", "GS-01_002", "GS-01_005"]}]
    brand = make_brand(tmp_path / "brand", inp, products=products)
    assert "입력본 밖" in _driver_input_error(inp, brand, tmp_path)["error"]


def test_driver_not_in_input_overlapping_targets(tmp_path):
    inp = make_input(tmp_path / "input")
    brand = make_brand(tmp_path / "brand", inp, not_in_input={"images": {"GS-01_002": "GS-01"}})
    _driver_input_error(inp, brand, tmp_path)


def test_driver_image_without_brand_link(tmp_path):
    secs = {**SECTIONS, "GS-02_001": {"sec_1_01": (["셀리맥스"], {})}}
    inp = make_input(tmp_path / "input", secs)
    _driver_input_error(inp, make_brand(tmp_path / "brand", inp), tmp_path)


def test_driver_duplicate_manifest_section(tmp_path):
    inp = make_input(tmp_path / "input")
    m = json.loads((inp / "MANIFEST.json").read_text(encoding="utf-8"))
    m["images"][0]["sections"].append({"section_key": "sec_1_01"})
    (inp / "MANIFEST.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    _write_sums(inp)
    brand = make_brand(tmp_path / "brand", inp)
    assert "두 번 이상" in _driver_input_error(inp, brand, tmp_path)["error"]


def test_driver_label_failed_is_input_error(tmp_path):
    inp = make_input(tmp_path / "input")
    p = inp / "GS-01_002" / "label" / "sec_1_01.json"
    lab = jsonio.load_model(p, LabelResult)
    jsonio.write_model(p, LabelResult(section_key=lab.section_key, status="failed", labels=None, checked=lab.checked, error="504"))
    _write_sums(inp)
    _driver_input_error(inp, make_brand(tmp_path / "brand", inp), tmp_path)


@pytest.mark.parametrize("target", ["verify_file_set", "read_sums", "logo_run_base"])
def test_driver_internal_error_in_precheck_is_4(bundles, tmp_path, monkeypatch, target):
    drv = _load_driver()
    inp, brand = bundles

    def boom(*a, **k):
        raise RuntimeError("주입한 내부 오류")

    monkeypatch.setattr(drv, target, boom)
    out = tmp_path / "out"
    assert drv.main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 4
    rj = _run_json(out)
    assert rj["status"] == "failed" and "RuntimeError" in rj["error"] and _result_files(out) == []


def test_driver_internal_error_in_loading_is_4(bundles, tmp_path, monkeypatch):
    inp, brand = bundles

    def boom(*a, **k):
        raise RuntimeError("로딩 중 내부 오류")

    monkeypatch.setattr(jsonio, "load_merge", boom)
    out = tmp_path / "out"
    assert _load_driver().main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 4
    assert _run_json(out)["status"] == "failed"


# ---------------------------------------------------------------------------
# 중간 실패 · 저장 실패 — 완료 결과 보존, 전체 failed, 미처리 건수
# ---------------------------------------------------------------------------
def test_unexpected_error_mid_run(bundles, tmp_path, monkeypatch):
    inp, brand = bundles
    real = logo.run
    calls = []

    def flaky(image_id, merged, lab, meta, cfg):
        calls.append((image_id, merged.section_key))
        if len(calls) == 2:
            raise RuntimeError("판정 중 주입 오류")
        return real(image_id, merged, lab, meta, cfg)

    monkeypatch.setattr(logo, "run", flaky)
    out = tmp_path / "out"
    assert _load_driver().main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 4
    rj = _run_json(out)
    c = rj["counts"]
    assert rj["status"] == "failed" and (c["sections_ok"], c["sections_failed"], c["sections_not_run"]) == (1, 1, 1)
    assert [s["status"] for s in rj["sections"]] == ["ok", "failed"]
    first = jsonio.load_model(out / "GS-01_001" / "logo" / "sec_1_01.json", LogoResult)
    failed = jsonio.load_model(out / "GS-01_001" / "logo" / "sec_1_02.json", LogoResult)
    assert first.status == "ok" and failed.status == "failed" and failed.decisions is None and "RuntimeError" in failed.error
    assert json.loads((out / "GS-01_001" / "logo_debug" / "sec_1_02.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not (out / "GS-01_002").exists()  # 미처리 — 결과를 만들지 않는다


def test_save_error_mid_run(bundles, tmp_path, monkeypatch):
    inp, brand = bundles
    real = jsonio.write_text_atomic

    def failing(path, text):
        if Path(path).as_posix().endswith("GS-01_001/logo/sec_1_02.json"):
            raise OSError("디스크 가득 참(주입)")
        return real(path, text)

    monkeypatch.setattr(jsonio, "write_text_atomic", failing)
    out = tmp_path / "out"
    assert _load_driver().main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 4
    rj = _run_json(out)
    assert rj["status"] == "failed" and rj["sections"][-1]["status"] == "save_failed"
    assert rj["sections"][-1]["judge_status"] == "ok"  # 판정 결과 상태와 실행 상태를 구분한다
    assert rj["counts"]["sections_not_run"] == 1
    assert (out / "GS-01_001" / "logo" / "sec_1_01.json").exists()
    assert not (out / "GS-01_001" / "logo" / "sec_1_02.json").exists()


def test_run_json_save_failure_is_4_without_claim(bundles, tmp_path, monkeypatch, capsys):
    inp, brand = bundles
    real = jsonio.write_text_atomic

    def failing(path, text):
        if Path(path).name == "run.json":
            raise OSError("run.json 쓰기 실패(주입)")
        return real(path, text)

    monkeypatch.setattr(jsonio, "write_text_atomic", failing)
    out = tmp_path / "out"
    assert _load_driver().main(["--input", str(inp), "--brand-meta", str(brand), "--out", str(out)]) == 4
    assert not (out / "run.json").exists()
    err = capsys.readouterr().err
    assert "run.json 저장 실패" in err and "run.json에 기록" not in err


def test_execute_logo_rejects_duplicate_jobs(bundles, tmp_path):
    from pipeline import config as cfgmod

    inp, brand = bundles
    job = {"image_id": "GS-01_001", "merged": jsonio.load_merge(inp / "GS-01_001" / "merge" / "sec_1_01.json"),
           "label": jsonio.load_model(inp / "GS-01_001" / "label" / "sec_1_01.json", LabelResult)}
    out = tmp_path / "out"
    started = runmod.datetime.now(runmod.timezone.utc)
    code = runmod.execute_logo(out, [job, dict(job)], json.loads((brand / "brand_metadata.json").read_text(encoding="utf-8")),
                               cfgmod.load_config(), runmod.logo_run_base("test", None, started), started)
    assert code == 2 and _run_json(out)["status"] == "input_error" and _result_files(out) == []


# ---------------------------------------------------------------------------
# CLI `logo`
# ---------------------------------------------------------------------------
def _cli(inp, brand, out, image="GS-01_001", key="sec_1_01", extra=()):
    return runmod.main(["logo", "--merge", str(inp / image / "merge" / f"{key}.json"), "--label", str(inp / image / "label" / f"{key}.json"),
                        "--brand-meta", str(brand / "brand_metadata.json"), "--image-id", image, "--out", str(out), *extra])


def test_cli_success(bundles, tmp_path):
    inp, brand = bundles
    out = tmp_path / "out"
    assert _cli(inp, brand, out) == 0
    r = jsonio.load_model(out / "GS-01_001" / "logo" / "sec_1_01.json", LogoResult)
    assert [(d.is_brand_logo, d.basis) for d in r.decisions] == [(True, "exact_match"), (False, "no_match"), (None, "product_label")]
    rj = _run_json(out)
    assert rj["status"] == "ok" and rj["mode"] == "single" and set(rj["inputs"]["sha256"]) == {"merge", "label", "brand_meta"}


def test_cli_relative_paths_from_cwd(bundles, tmp_path, monkeypatch):
    inp, brand = bundles
    monkeypatch.chdir(tmp_path)
    assert runmod.main(["logo", "--merge", "input/GS-01_002/merge/sec_1_01.json", "--label", "input/GS-01_002/label/sec_1_01.json",
                        "--brand-meta", "brand/brand_metadata.json", "--image-id", "GS-01_002", "--out", "rel_out"]) == 0
    assert (tmp_path / "rel_out" / "GS-01_002" / "logo" / "sec_1_01.json").exists()


def test_cli_refuses_existing_out(bundles, tmp_path):
    inp, brand = bundles
    out = tmp_path / "out"
    out.mkdir()
    assert _cli(inp, brand, out) == 2 and list(out.iterdir()) == []


@pytest.mark.parametrize("extra, image", [
    ((), "GS-01_009"),                                    # 제외 원본 — 연결 그룹 없음
    (("--set", "logo.normalize=['nfkc', 'lower']"), "GS-01_001"),  # 승인 외 설정
    (("--set", "logo.unknown=1"), "GS-01_001"),           # 없는 config 키(ConfigKeyError)
])
def test_cli_input_or_config_error_is_2(bundles, tmp_path, extra, image):
    inp, brand = bundles
    out = tmp_path / "out"
    assert runmod.main(["logo", "--merge", str(inp / "GS-01_001" / "merge" / "sec_1_01.json"),
                        "--label", str(inp / "GS-01_001" / "label" / "sec_1_01.json"),
                        "--brand-meta", str(brand / "brand_metadata.json"), "--image-id", image, "--out", str(out), *extra]) == 2
    assert _run_json(out)["status"] == "input_error" and _result_files(out) == []


def test_cli_label_section_mismatch_is_2(bundles, tmp_path):
    inp, brand = bundles
    out = tmp_path / "out"
    assert runmod.main(["logo", "--merge", str(inp / "GS-01_001" / "merge" / "sec_1_01.json"),
                        "--label", str(inp / "GS-01_001" / "label" / "sec_1_02.json"),
                        "--brand-meta", str(brand / "brand_metadata.json"), "--image-id", "GS-01_001", "--out", str(out)]) == 2


def test_cli_missing_file_is_2(bundles, tmp_path):
    inp, brand = bundles
    out = tmp_path / "out"
    assert runmod.main(["logo", "--merge", str(tmp_path / "없음.json"), "--label", str(inp / "GS-01_001" / "label" / "sec_1_01.json"),
                        "--brand-meta", str(brand / "brand_metadata.json"), "--image-id", "GS-01_001", "--out", str(out)]) == 2


@pytest.mark.parametrize("target", ["load_merge", "sha256_file"])
def test_cli_internal_error_in_loading_is_4(bundles, tmp_path, monkeypatch, target):
    inp, brand = bundles

    def boom(*a, **k):
        raise RuntimeError("주입한 내부 오류")

    monkeypatch.setattr(jsonio, target, boom)
    out = tmp_path / "out"
    assert _cli(inp, brand, out) == 4
    rj = _run_json(out)
    assert rj["status"] == "failed" and "RuntimeError" in rj["error"] and _result_files(out) == []


def test_cli_internal_error_in_record_init_is_4(bundles, tmp_path, monkeypatch):
    inp, brand = bundles

    def boom(*a, **k):
        raise RuntimeError("기록 머리 생성 실패(주입)")

    monkeypatch.setattr(runmod, "logo_run_base", boom)
    out = tmp_path / "out"
    assert _cli(inp, brand, out) == 4
    rj = _run_json(out)
    assert rj["status"] == "failed" and rj["mode"] == "single"


# ---------------------------------------------------------------------------
# 원자적 저장
# ---------------------------------------------------------------------------
def test_atomic_write_keeps_old_file_and_no_temp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "d" / "r.json"
    jsonio.write_text_atomic(target, "old")

    def fail_replace(src, dst):
        raise OSError("교체 실패(주입)")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        jsonio.write_text_atomic(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in target.parent.iterdir()] == ["r.json"]


def test_atomic_write_failure_leaves_no_partial_file(tmp_path, monkeypatch):
    target = tmp_path / "d" / "new.json"
    real_fdopen = os.fdopen

    class Boom:
        def __init__(self, f):
            self.f = f

        def __enter__(self):
            return self

        def write(self, text):
            self.f.write(text[:3])
            raise OSError("쓰기 중단(주입)")

        def __exit__(self, *a):
            self.f.close()

    monkeypatch.setattr(os, "fdopen", lambda fd, *a, **k: Boom(real_fdopen(fd, *a, **k)))
    with pytest.raises(OSError):
        jsonio.write_text_atomic(target, "complete text")
    assert not target.exists() and list(target.parent.iterdir()) == []
