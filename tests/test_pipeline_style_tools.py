"""⑦ 개발용 실행 도구(style_run_driver.py) · 검수 페이지(style_review_page.py) — 작은 합성 입력본(tmp_path)으로 확인한다.

도구 동작 검증이며 실제 스타일 품질 검증이 아니다. 기대 동작: MANIFEST images[].sections만 실행(split.json 전체 아님) · 선택 실행 ·
기존 출력 · 입력본 안 출력 거부(아무것도 쓰지 않음) · 섹션 실패는 기록하고 계속(전체 partial/failed · 종료 코드 4) · 저장 실패 드러냄 ·
결과는 StyleResult 그대로 · 페이지는 입력 문자열을 이스케이프하고 null을 값으로 바꾸지 않는다.
"""
from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pipeline import config as cfgmod
from pipeline import jsonio
from pipeline.stages import style
from pipeline.stages.label import input_fingerprint
from pipeline.stages.logo import sha256_json
from pipeline.types import (
    BBox, LabelChecked, LabelDecision, LabelResult, Line, LogoDecision, LogoResult, MergeResult, OcrRegion, Section, SplitResult,
    StyleResult, TextBlock,
)

TOOLS = Path(__file__).resolve().parents[1] / "docs" / "ai-experiments" / "tools"
IMAGE = "IMG-A"
EVIL = '<script>alert("x")</script> & "따옴표"'


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def driver():
    return _load("style_run_driver")


@pytest.fixture(scope="module")
def page():
    return _load("style_review_page")


def R(key, x, y, w, h, text="가", score=0.9):
    return OcrRegion(region_key=key, text=text, score=score, poly=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)], bbox=BBox(x=x, y=y, w=w, h=h))


def B(key, order, section_key, *lines):
    return TextBlock(block_key=key, section_key=section_key, block_order=order, source_ko="\n".join(ln.text for ln in lines),
                     source_lines=list(lines), bbox=BBox.union([ln.bbox for ln in lines]), role="body")


def L(key, *regs):
    return Line(line_key=key, text=" ".join(r.text for r in regs), bbox=BBox.union([r.bbox for r in regs]), regions=list(regs))


def write_section(root: Path, key: str, blocks, excl: dict[str, str]):
    merged = MergeResult(section_key=key, blocks=blocks)
    label = LabelResult(section_key=key, status="ok",
                        labels=[LabelDecision(block_key=b.block_key, is_product_label=excl.get(b.block_key) == "label", basis="vlm") for b in blocks],
                        checked=LabelChecked(input_fingerprint=input_fingerprint(blocks), llm_called=True))
    decs = [LogoDecision(block_key=b.block_key, is_brand_logo=None, basis="product_label") if excl.get(b.block_key) == "label" else
            LogoDecision(block_key=b.block_key, is_brand_logo=excl.get(b.block_key) == "logo",
                         basis="exact_match" if excl.get(b.block_key) == "logo" else "no_match") for b in blocks]
    logo = LogoResult(image_id=IMAGE, section_key=key, status="ok", decisions=decs)
    record = {"status": "ok", "image_id": IMAGE, "section_key": key, "blocks_fingerprint": input_fingerprint(blocks),
              "label_fingerprint": sha256_json(label.model_dump(mode="json")),
              "decisions": [{"block_key": d.block_key, "is_brand_logo": d.is_brand_logo, "basis": d.basis} for d in decs]}
    d = root / IMAGE
    for sub, model in (("merge", merged), ("label", label), ("logo", logo)):
        jsonio.write_model(d / sub / f"{key}.json", model)
    (d / "logo_debug").mkdir(parents=True, exist_ok=True)
    (d / "logo_debug" / f"{key}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def write_sums(root: Path):
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}"
             for p in sorted(root.rglob("*")) if p.is_file() and p.name != "SHA256SUMS"]
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_bundle(tmp: Path) -> Path:
    """원본 1장 · split 섹션 3개(MANIFEST 대상은 sec_1_01 · sec_1_02만). sec_1_01: 측정 ok(원문에 HTML 특수문자) · 단색(partial · 색 null) ·
    공백(no_text) · 라벨 · 로고 블록. sec_1_02: 블록 1개."""
    root = tmp / "bundle"
    (root / IMAGE / "sections").mkdir(parents=True)
    secs = []
    for n in (1, 2, 3):
        key = f"sec_1_{n:02d}"
        img = np.full((48, 64, 3), 255, np.uint8)
        img[1, 1] = (20, 40, 60)  # sec_1_01의 blk_001 영역(0,0,3,3) 가운데 글자
        img[11, 1] = 0
        p = root / IMAGE / "sections" / f"{key}.png"
        Image.fromarray(img).save(p)
        secs.append(Section(section_key=key, source_image_id=1, section_order=n, top_offset=48 * (n - 1), height=48, width=64,
                            image_path=str(p)))
    jsonio.write_model(root / IMAGE / "split.json", SplitResult(source_image_id=1, source_width=64, source_height=144, sections=secs))
    s1 = [B("blk_001", 1, "sec_1_01", L("line_001", R("reg_0001", 0, 0, 3, 3, text=EVIL, score=0.0))),
          B("blk_002", 2, "sec_1_01", L("line_002", R("reg_0002", 20, 0, 3, 3))),  # 흰색뿐 → single_class
          B("blk_003", 3, "sec_1_01", L("line_003", R("reg_0003", 30, 0, 3, 3, text=" "))),
          B("blk_004", 4, "sec_1_01", L("line_004", R("reg_0004", 40, 0, 3, 3))),
          B("blk_005", 5, "sec_1_01", L("line_005", R("reg_0005", 50, 0, 3, 3)))]
    write_section(root, "sec_1_01", s1, {"blk_004": "label", "blk_005": "logo"})
    write_section(root, "sec_1_02", [B("blk_001", 1, "sec_1_02", L("line_001", R("reg_0001", 0, 10, 3, 3)))], {})
    (root / "MANIFEST.json").write_text(json.dumps({"name": "style-tool-test", "images": [
        {"image": IMAGE, "sections": [{"section_key": "sec_1_01"}, {"section_key": "sec_1_02"}]}]}), encoding="utf-8")
    write_sums(root)
    return root


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def run_json(out: Path) -> dict:
    return json.loads((out / "run.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 실행 도구
# ---------------------------------------------------------------------------
def test_driver_explicit_selection_saves_style_result(driver, tmp_path):
    root = make_bundle(tmp_path)
    before = tree_hashes(root)
    out = tmp_path / "run1"
    assert driver.main(["--input", str(root), "--out", str(out), "--section", f"{IMAGE}/sec_1_01"]) == 0
    rec = run_json(out)
    assert (rec["stage"], rec["status"], rec["error"]) == ("style", "ok", None)
    assert rec["selection"] == {"mode": "explicit", "sections": [f"{IMAGE}/sec_1_01"]}
    assert rec["counts"] == {"selected": 1, "ok": 1, "input_error": 0, "failed": 0, "save_failed": 0}
    assert {"git_commit", "git_dirty", "started_at", "ran_at", "duration_s", "config"} <= set(rec)
    assert rec["config"]["style"] == {"method": "otsu_border", "em_ratio": 1.35, "align_tolerance": 0.12}
    [entry] = rec["sections"]
    assert (entry["image_id"], entry["section_key"], entry["status"], entry["result"]) == (IMAGE, "sec_1_01", "ok", f"{IMAGE}/style/sec_1_01.json")
    assert entry["inputs"]["section_image"]["path"] == f"{IMAGE}/sections/sec_1_01.png"  # 원본 섹션 이미지(인페인팅 결과 아님)
    assert entry["blocks"] == {"total": 5, "ok": 1, "partial": 1, "no_text": 1, "excluded": 2}
    raw = json.loads((out / entry["result"]).read_text(encoding="utf-8"))
    assert raw["schema_version"] == "1"
    res = StyleResult.model_validate(raw)  # StyleResult로 다시 읽힌다
    # 계산은 style.run 그대로 — 같은 입력으로 직접 부른 결과와 같다
    split = jsonio.load_split(root / IMAGE / "split.json")
    sec = next(s for s in split.sections if s.section_key == "sec_1_01")
    direct = style.run(IMAGE, sec, jsonio.load_merge(root / IMAGE / "merge/sec_1_01.json"),
                       jsonio.load_model(root / IMAGE / "label/sec_1_01.json", LabelResult),
                       jsonio.load_model(root / IMAGE / "logo/sec_1_01.json", LogoResult),
                       json.loads((root / IMAGE / "logo_debug/sec_1_01.json").read_text(encoding="utf-8")), cfgmod.load_config())
    assert res == direct
    assert (res.image_id, res.section_key) == (IMAGE, "sec_1_01")
    assert not (out / IMAGE / "style" / "sec_1_02.json").exists()
    assert tree_hashes(root) == before  # 입력본 파일 · 해시 불변, 추가 파일 없음


def test_driver_all_runs_manifest_targets_only(driver, tmp_path):
    root = make_bundle(tmp_path)
    out = tmp_path / "run_all"
    assert driver.main(["--input", str(root), "--out", str(out), "--all"]) == 0
    rec = run_json(out)
    assert [(s["section_key"], s["status"]) for s in rec["sections"]] == [("sec_1_01", "ok"), ("sec_1_02", "ok")]
    assert not (out / IMAGE / "style" / "sec_1_03.json").exists()  # split.json에는 있지만 MANIFEST 대상이 아님


@pytest.mark.parametrize("args, needle", [
    (["--section", f"{IMAGE}/sec_1_03"], "MANIFEST images[].sections에 없는 대상"),
    (["--section", f"{IMAGE}/sec_1_01", "--section", f"{IMAGE}/sec_1_01"], "같은 원본 · 섹션을 두 번 선택했다"),
    (["--section", "sec_1_01"], "--section 형식은 원본/섹션이다"),
])
def test_driver_selection_errors(driver, tmp_path, args, needle):
    root = make_bundle(tmp_path)
    out = tmp_path / "bad"
    assert driver.main(["--input", str(root), "--out", str(out), *args]) == 2
    rec = run_json(out)
    assert rec["status"] == "input_error" and needle in rec["error"]
    assert rec["sections"] == [] and not (out / IMAGE).exists()


def test_driver_refuses_existing_and_inside_bundle(driver, tmp_path):
    root = make_bundle(tmp_path)
    before = tree_hashes(root)
    existing = tmp_path / "exists"
    existing.mkdir()
    (existing / "keep.txt").write_text("x", encoding="utf-8")
    assert driver.main(["--input", str(root), "--out", str(existing), "--section", f"{IMAGE}/sec_1_01"]) == 2
    assert [p.name for p in existing.iterdir()] == ["keep.txt"]
    for inner in (root / "runs" / "x", root / IMAGE / "style_out"):
        assert driver.main(["--input", str(root), "--out", str(inner), "--section", f"{IMAGE}/sec_1_01"]) == 2
        assert not inner.exists()
    assert tree_hashes(root) == before


def test_driver_rejects_tampered_bundle(driver, tmp_path):
    root = make_bundle(tmp_path)
    (root / IMAGE / "stray.txt").write_text("x", encoding="utf-8")
    out = tmp_path / "tampered"
    assert driver.main(["--input", str(root), "--out", str(out), "--section", f"{IMAGE}/sec_1_01"]) == 2
    rec = run_json(out)
    assert rec["status"] == "input_error" and "목록 밖 파일 IMG-A/stray.txt" in rec["error"]


def test_driver_records_section_failure_and_continues(driver, tmp_path):
    root = make_bundle(tmp_path)
    p = root / IMAGE / "logo_debug" / "sec_1_01.json"
    rec0 = json.loads(p.read_text(encoding="utf-8"))
    rec0["blocks_fingerprint"] = "0" * 64  # sec_1_01만 ⑤ 기록 지문 불일치
    p.write_text(json.dumps(rec0), encoding="utf-8")
    write_sums(root)
    out = tmp_path / "partial"
    code = driver.main(["--input", str(root), "--out", str(out), "--all"])
    rec = run_json(out)
    assert code == 4 and rec["status"] == "partial" and "섹션 1/2개가 ok가 아니다" in rec["error"]
    first, second = rec["sections"]
    assert first["status"] == "input_error" and "⑤ 기록의 블록 지문" in first["error"] and first["result"] is None
    assert second["status"] == "ok"  # 실패 뒤에도 계속
    assert not (out / IMAGE / "style" / "sec_1_01.json").exists() and (out / IMAGE / "style" / "sec_1_02.json").exists()
    assert rec["counts"] == {"selected": 2, "ok": 1, "input_error": 1, "failed": 0, "save_failed": 0}


def test_driver_all_failed_and_unexpected_error(driver, tmp_path, monkeypatch):
    root = make_bundle(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("예상하지 못한 오류")

    monkeypatch.setattr(driver.style, "run", boom)
    out = tmp_path / "failed"
    assert driver.main(["--input", str(root), "--out", str(out), "--all"]) == 4
    rec = run_json(out)
    assert rec["status"] == "failed" and [s["status"] for s in rec["sections"]] == ["failed", "failed"]
    assert "RuntimeError: 예상하지 못한 오류" in rec["sections"][0]["error"]


def test_driver_save_failure_is_reported(driver, tmp_path, monkeypatch):
    root = make_bundle(tmp_path)

    def broken(out, rel, result):
        raise OSError("디스크 가득 참(모의)")

    monkeypatch.setattr(driver, "save_result", broken)
    out = tmp_path / "savefail"
    assert driver.main(["--input", str(root), "--out", str(out), "--section", f"{IMAGE}/sec_1_02"]) == 4
    [entry] = run_json(out)["sections"]
    assert entry["status"] == "save_failed" and "디스크 가득 참" in entry["error"] and entry["result"] is None


# ---------------------------------------------------------------------------
# 검수 페이지
# ---------------------------------------------------------------------------
def _built(driver, page, tmp_path, *sel):
    root = make_bundle(tmp_path)
    out = tmp_path / "run"
    driver.main(["--input", str(root), "--out", str(out), *sel])
    review = out / "review"
    code = page.main(["--run", str(out), "--input", str(root), "--out", str(review)])
    return root, out, review, code, (review / "index.html").read_text(encoding="utf-8")


def test_review_page_content_and_assets(driver, page, tmp_path):
    root, out, review, code, doc = _built(driver, page, tmp_path, "--all")
    assert code == 0
    # 입력 문자열 이스케이프 — 원문이 태그로 들어가지 않는다
    assert html.escape(EVIL, quote=True) in doc and EVIL not in doc and "<script>alert" not in doc
    # 모든 img src가 실제 파일
    srcs = re.findall(r'src="(img/[^"]+)"', doc)
    assert srcs and all((review / html.unescape(s)).is_file() for s in srcs)
    # 섹션 원본은 입력본 섹션 이미지의 사본(바이트 동일)
    assert (review / "img" / f"{IMAGE}__sec_1_01.png").read_bytes() == (root / IMAGE / "sections" / "sec_1_01.png").read_bytes()
    # 블록 crop은 bbox 그대로 자른 원본 픽셀
    with Image.open(review / "img" / f"{IMAGE}__sec_1_01__blk_001.png") as im, \
            Image.open(root / IMAGE / "sections" / "sec_1_01.png") as src:
        assert np.array_equal(np.array(im), np.array(src)[0:3, 0:3])
    # 측정값 · null · 정렬 근거 · 상태 표기
    assert "#14283C" in doc and "#FFFFFF" in doc
    assert "없음(null) · 사유 <code>no_valid_region</code>" in doc  # blk_002 색 null(검정 · 흰색으로 바꾸지 않음)
    assert "없음(null) · 사유 <code>no_text</code>" in doc and "없음(null) · 사유 <code>no_text_lines</code>" in doc
    assert "없음(null) · 사유 <code>excluded</code>" in doc and "제외 사유 product_label" in doc and "제외 사유 brand_logo" in doc
    assert "기본값 · 한 줄(default_single_line)" in doc and 'class="basis dflt"' in doc
    assert "4.05</b> px <span class=mut>(원본 이미지 기준)" in doc
    assert "ok — 측정값 모두 얻음(품질 판정 아님)" in doc and "excluded — 라벨 · 로고 제외(오류 아님)" in doc
    assert "합격" not in doc.replace("자동 합격 판정은 없습니다", "").replace("품질 합격이 아니며", "")
    assert "single_class" in doc  # 영역 색 실패 사유
    assert f'data-section="{IMAGE}/sec_1_02"' in doc


def test_review_page_shows_failed_section_and_mismatch(driver, page, tmp_path):
    root = make_bundle(tmp_path)
    p = root / IMAGE / "logo_debug" / "sec_1_01.json"
    r = json.loads(p.read_text(encoding="utf-8"))
    r["status"] = "failed"
    p.write_text(json.dumps(r), encoding="utf-8")
    write_sums(root)
    out = tmp_path / "run"
    assert driver.main(["--input", str(root), "--out", str(out), "--all"]) == 4
    # 결과 파일을 일부러 입력과 어긋나게(블록 하나 제거) — 페이지가 대응 불일치로 드러내야 한다
    res_path = out / IMAGE / "style" / "sec_1_02.json"
    res = json.loads(res_path.read_text(encoding="utf-8"))
    res["blocks"] = []
    res_path.write_text(json.dumps(res), encoding="utf-8")
    review = out / "review"
    assert page.main(["--run", str(out), "--input", str(root), "--out", str(review)]) == 4
    doc = (review / "index.html").read_text(encoding="utf-8")
    assert "실행 상태 <b>input_error</b> — 결과 없음" in doc and "⑤ 기록의 상태" in doc
    assert "입력 · 결과 대응 불일치" in doc
    assert 'class="bad">partial' in doc  # 실행 전체 상태를 숨기지 않는다


def test_review_page_refuses_existing_and_inside_bundle(driver, page, tmp_path):
    root = make_bundle(tmp_path)
    out = tmp_path / "run"
    driver.main(["--input", str(root), "--out", str(out), "--section", f"{IMAGE}/sec_1_02"])
    before = tree_hashes(root)
    assert page.main(["--run", str(out), "--input", str(root), "--out", str(out)]) == 2  # 이미 있음
    assert page.main(["--run", str(out), "--input", str(root), "--out", str(root / "review")]) == 2
    assert not (root / "review").exists() and tree_hashes(root) == before
