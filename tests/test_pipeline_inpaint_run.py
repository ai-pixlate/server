"""⑥ 인페인팅 실행 계층 — 개발용 v1 형식(pipeline.md 7.6절) · 저장 · 실패 · 덮어쓰기 방지 (합성 입력 · 가짜 모델, 실제 LaMa 없음)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from pipeline import config as cfgmod
from pipeline import jsonio
from pipeline import run as runmod
from pipeline.types import InpaintCounts, InpaintFiles, InpaintResult, Section, SplitResult
from tests.test_pipeline_inpaint import FakeModel, grid, image_of, logo_record_for, make_block, make_inputs, rect, region

W, H = 12, 10
TARGET = [make_block(1, [region("reg_0001", rect(2, 2, 4, 2))])]  # 손 계산 마스크: test_single_target_mask_hand_computed
WANT_FINAL = grid(
    "............", "..####......", ".######.....", ".######.....", "..####......",
    "............", "............", "............", "............", "............",
)
LOW_ONLY = [make_block(1, [region("reg_0001", rect(2, 2, 4, 2), score=0.1)])]  # 빈 마스크


@pytest.fixture
def cfg():
    return cfgmod.load_config()


def job(tmp: Path, image_id: str, blocks=TARGET, flags=None, image=None) -> dict:
    flags = flags or [(False, False)] * len(blocks)
    s, m, l, g, _ = make_inputs(blocks, flags, W, H)
    g = g.model_copy(update={"image_id": image_id})
    img = image if image is not None else image_of(W, H)
    p = tmp / f"{image_id}.png"
    Image.fromarray(img).save(p)
    return {"image_id": image_id, "section": s, "image_path": p, "merged": m, "label": l, "logo": g,
            "logo_record": logo_record_for(m, l, g), "_image": img}


def execute(out: Path, jobs, cfg, mode, **kw) -> tuple[int, dict]:
    started = datetime.now(timezone.utc)
    record = runmod.inpaint_run_base(mode, cfg, started)
    code = runmod.execute_inpaint(out, jobs, cfg, mode, record, started, **kw)
    return code, json.loads((out / "run.json").read_text(encoding="utf-8"))


def read_png(p: Path) -> np.ndarray:
    with Image.open(p) as im:
        return np.array(im)


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# 실행 함수
# ---------------------------------------------------------------------------
def test_mask_only_writes_masks_and_no_background(tmp_path, cfg):
    j = job(tmp_path, "IMG-01")
    code, rec = execute(tmp_path / "out", [j], cfg, "mask_only")
    assert code == 0 and rec["status"] == "ok" and rec["mode"] == "mask_only" and rec["model"] is None and rec["timeout_s"] is None
    res = jsonio.load_model(tmp_path / "out" / "IMG-01/inpaint/sec_1_01.json", InpaintResult)
    assert res.status == "masked" and res.model_called is False and res.files.background is None
    final = read_png(tmp_path / "out" / res.files.final_mask)
    assert final.dtype == np.uint8 and set(np.unique(final)) <= {0, 255}
    assert ((final == 255) == WANT_FINAL).all()
    assert not read_png(tmp_path / "out" / res.files.protect_mask).any()
    assert not (tmp_path / "out" / "IMG-01/inpaint_bg").exists()
    dbg = json.loads((tmp_path / "out" / "IMG-01/inpaint_debug/sec_1_01.json").read_text(encoding="utf-8"))
    assert dbg["regions"][0]["kind"] == "target" and dbg["regions"][0]["radius"] == 1 and dbg["counts"]["final_px"] == 20
    assert set(dbg["fingerprints"]) >= {"blocks", "label", "logo", "logo_record", "image_file_sha256", "image_pixels", "final_mask_pixels",
                                        "protect_mask_pixels"}
    assert rec["counts"]["sections_ok"] == 1 and rec["counts"]["by_result"]["masked"] == 1


def test_inpaint_with_fake_model_keeps_outside_pixels(tmp_path, cfg):
    j = job(tmp_path, "IMG-01")
    model = FakeModel(fill=7)
    code, rec = execute(tmp_path / "out", [j], cfg, "inpaint", model_factory=lambda c: model)
    assert code == 0 and rec["status"] == "ok" and rec["model"] == {"name": "fake"}
    assert len(model.calls) == 1
    res = jsonio.load_model(tmp_path / "out" / "IMG-01/inpaint/sec_1_01.json", InpaintResult)
    assert res.status == "inpainted" and res.model_called
    bg = read_png(tmp_path / "out" / res.files.background)
    assert bg.shape == (H, W, 3) and bg.dtype == np.uint8
    assert (bg[WANT_FINAL] == 7).all()
    assert (bg[~WANT_FINAL] == j["_image"][~WANT_FINAL]).all()


def test_empty_mask_unchanged_without_model_call(tmp_path, cfg):
    j = job(tmp_path, "IMG-01", blocks=LOW_ONLY)
    model = FakeModel()
    code, _ = execute(tmp_path / "out", [j], cfg, "inpaint", model_factory=lambda c: model)
    assert code == 0 and model.calls == []
    res = jsonio.load_model(tmp_path / "out" / "IMG-01/inpaint/sec_1_01.json", InpaintResult)
    assert res.status == "unchanged" and not res.model_called and res.counts.final_px == 0
    assert (read_png(tmp_path / "out" / res.files.background) == j["_image"]).all()


def test_default_factory_is_not_available_exit_3(tmp_path, cfg):
    j = job(tmp_path, "IMG-01")
    code, rec = execute(tmp_path / "out", [j], cfg, "inpaint")
    assert code == 3 and rec["status"] == "failed" and "어댑터 미구현" in rec["error"]
    assert [s["status"] for s in rec["sections"]] == ["not_run"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["run.json"]


def test_model_init_failure_no_results(tmp_path, cfg):
    def boom(c):
        raise RuntimeError("CUDA 없음")

    code, rec = execute(tmp_path / "out", [job(tmp_path, "IMG-01")], cfg, "inpaint", model_factory=boom)
    assert code == 4 and "모델 초기화 실패" in rec["error"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["run.json"]


def test_mid_run_model_failure_stops_and_keeps_completed(tmp_path, cfg):
    jobs = [job(tmp_path, f"IMG-0{i}") for i in (1, 2, 3)]

    class SecondFails(FakeModel):
        def inpaint(self, image, mask):
            if len(self.calls) == 1:
                self.calls.append((image, mask))
                raise MemoryError("oom")
            return super().inpaint(image, mask)

    model = SecondFails()
    code, rec = execute(tmp_path / "out", jobs, cfg, "inpaint", model_factory=lambda c: model)
    assert code == 4 and rec["status"] == "failed"
    assert [s["status"] for s in rec["sections"]] == ["ok", "failed", "not_run"]
    assert rec["counts"]["sections_ok"] == 1 and rec["counts"]["sections_failed"] == 1 and rec["counts"]["sections_not_run"] == 1
    out = tmp_path / "out"
    assert jsonio.load_model(out / "IMG-01/inpaint/sec_1_01.json", InpaintResult).status == "inpainted"
    failed = jsonio.load_model(out / "IMG-02/inpaint/sec_1_01.json", InpaintResult)
    assert failed.status == "failed" and failed.files.background is None and "MemoryError" in failed.error
    assert not (out / "IMG-02/inpaint_bg").exists()
    assert not (out / "IMG-03").exists()


@pytest.mark.parametrize("output", [np.zeros((H, W + 1, 3), dtype=np.uint8), np.zeros((H, W, 3), dtype=np.float64)])
def test_invalid_model_output_is_failed_section(tmp_path, cfg, output):
    code, rec = execute(tmp_path / "out", [job(tmp_path, "IMG-01")], cfg, "inpaint", model_factory=lambda c: FakeModel(output=output))
    assert code == 4 and rec["sections"][0]["status"] == "failed"
    assert jsonio.load_model(tmp_path / "out/IMG-01/inpaint/sec_1_01.json", InpaintResult).status == "failed"


def test_background_save_failure_is_not_success(tmp_path, cfg, monkeypatch):
    real = runmod.save_png_verified

    def fail_bg(path, arr):
        if "inpaint_bg" in Path(path).as_posix():
            raise OSError("디스크 가득 참")
        return real(path, arr)

    monkeypatch.setattr(runmod, "save_png_verified", fail_bg)
    code, rec = execute(tmp_path / "out", [job(tmp_path, "IMG-01"), job(tmp_path, "IMG-02")], cfg, "inpaint",
                        model_factory=lambda c: FakeModel())
    assert code == 4 and rec["status"] == "failed"
    assert [s["status"] for s in rec["sections"]] == ["save_failed", "not_run"]
    assert not (tmp_path / "out/IMG-01/inpaint").exists()  # 결과 파일(완료 표시)을 쓰지 않았다


def test_result_save_failure_is_not_success(tmp_path, cfg, monkeypatch):
    real = jsonio.write_text_atomic

    def fail_result(path, text):
        if "/inpaint/" in Path(path).as_posix():
            raise OSError("쓰기 금지")
        return real(path, text)

    monkeypatch.setattr(jsonio, "write_text_atomic", fail_result)
    code, rec = execute(tmp_path / "out", [job(tmp_path, "IMG-01")], cfg, "mask_only")
    assert code == 4 and rec["sections"][0]["status"] == "save_failed" and rec["sections"][0]["result_status"] == "masked"


def test_png_readback_mismatch_is_save_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(jsonio, "write_bytes_atomic", lambda path, data: Path(path).write_bytes(_png(np.zeros((2, 2), np.uint8))))
    with pytest.raises(OSError, match="다르다"):
        runmod.save_png_verified(tmp_path / "x.png", np.full((2, 2), 255, dtype=np.uint8))


def _png(arr) -> bytes:
    import io

    b = io.BytesIO()
    Image.fromarray(arr).save(b, format="PNG")
    return b.getvalue()


def test_precheck_failure_writes_no_results(tmp_path, cfg):
    good = job(tmp_path, "IMG-01")
    bad = job(tmp_path, "IMG-02")
    bad["logo_record"] = {**bad["logo_record"], "blocks_fingerprint": "0" * 64}
    code, rec = execute(tmp_path / "out", [good, bad], cfg, "mask_only")
    assert code == 2 and rec["status"] == "input_error" and "IMG-02" in rec["error"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["run.json"]
    assert rec["counts"]["sections_not_run"] == 2


def test_duplicate_jobs_rejected(tmp_path, cfg):
    j = job(tmp_path, "IMG-01")
    code, rec = execute(tmp_path / "out", [j, j], cfg, "mask_only")
    assert code == 2 and "두 번" in rec["error"]


def test_non_rgb_image_rejected_in_precheck(tmp_path, cfg):
    j = job(tmp_path, "IMG-01")
    Image.fromarray(np.zeros((H, W, 4), dtype=np.uint8)).save(j["image_path"])
    code, rec = execute(tmp_path / "out", [j], cfg, "mask_only")
    assert code == 2 and "RGBA" in rec["error"]


def test_image_size_mismatch_rejected_in_precheck(tmp_path, cfg):
    j = job(tmp_path, "IMG-01")
    Image.fromarray(np.zeros((H + 1, W, 3), dtype=np.uint8)).save(j["image_path"])
    code, rec = execute(tmp_path / "out", [j], cfg, "mask_only")
    assert code == 2 and "크기" in rec["error"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def write_bundle(root: Path, blocks=TARGET, flags=None, sums: bool = False) -> dict[str, Path]:
    flags = flags or [(False, False)] * len(blocks)
    s, m, l, g, r = make_inputs(blocks, flags, W, H)
    img_dir = root / "IMG-01"
    (img_dir / "sections").mkdir(parents=True)
    Image.fromarray(image_of(W, H)).save(img_dir / "sections" / "sec_1_01.png")
    section = Section(**{**s.model_dump(), "image_path": str(img_dir / "sections" / "sec_1_01.png")})
    jsonio.write_model(img_dir / "split.json", SplitResult(source_image_id=1, source_width=W, source_height=H, sections=[section]))
    g = g.model_copy(update={"image_id": "IMG-01"})
    paths = {"split": img_dir / "split.json", "merge": img_dir / "merge.json", "label": img_dir / "label.json", "logo": img_dir / "logo.json",
             "logo_debug": img_dir / "logo_debug.json"}
    paths["merge"].write_text(m.model_dump_json(), encoding="utf-8")
    paths["label"].write_text(l.model_dump_json(), encoding="utf-8")
    paths["logo"].write_text(g.model_dump_json(), encoding="utf-8")
    paths["logo_debug"].write_text(json.dumps(logo_record_for(m, l, g), ensure_ascii=False), encoding="utf-8")
    if sums:
        (root / "SHA256SUMS").write_text("", encoding="utf-8")
    return paths


def cli(paths: dict[str, Path], out: Path, mode: str = "mask-only") -> int:
    return runmod.main(["inpaint", "--mode", mode, "--split", str(paths["split"]), "--section", "sec_1_01", "--image-id", "IMG-01",
                        "--merge", str(paths["merge"]), "--label", str(paths["label"]), "--logo", str(paths["logo"]),
                        "--logo-debug", str(paths["logo_debug"]), "--out", str(out)])


def test_cli_mask_only_and_inputs_unchanged(tmp_path):
    paths = write_bundle(tmp_path / "in")
    before = tree_hashes(tmp_path / "in")
    assert cli(paths, tmp_path / "out") == 0
    assert tree_hashes(tmp_path / "in") == before
    rec = json.loads((tmp_path / "out/run.json").read_text(encoding="utf-8"))
    assert rec["status"] == "ok" and rec["mode"] == "mask_only" and set(rec["inputs"]["sha256"]) == {
        "split", "merge", "label", "logo", "logo_debug", "image"}
    res = jsonio.load_model(tmp_path / "out/IMG-01/inpaint/sec_1_01.json", InpaintResult)
    assert res.status == "masked"
    assert ((read_png(tmp_path / "out" / res.files.final_mask) == 255) == WANT_FINAL).all()


def test_cli_inpaint_mode_exits_3_without_results(tmp_path):
    paths = write_bundle(tmp_path / "in")
    assert cli(paths, tmp_path / "out", mode="inpaint") == 3
    rec = json.loads((tmp_path / "out/run.json").read_text(encoding="utf-8"))
    assert rec["status"] == "failed" and not (tmp_path / "out/IMG-01").exists()


def test_cli_mode_is_required(tmp_path):
    paths = write_bundle(tmp_path / "in")
    with pytest.raises(SystemExit):
        runmod.main(["inpaint", "--split", str(paths["split"]), "--section", "sec_1_01", "--image-id", "IMG-01", "--merge", str(paths["merge"]),
                     "--label", str(paths["label"]), "--logo", str(paths["logo"]), "--logo-debug", str(paths["logo_debug"]),
                     "--out", str(tmp_path / "out")])


def test_cli_refuses_existing_out(tmp_path):
    paths = write_bundle(tmp_path / "in")
    out = tmp_path / "out"
    out.mkdir()
    (out / "run.json").write_text("이전 결과", encoding="utf-8")
    assert cli(paths, out) == 2
    assert (out / "run.json").read_text(encoding="utf-8") == "이전 결과"


def test_cli_refuses_out_inside_fixed_bundle(tmp_path):
    paths = write_bundle(tmp_path / "in", sums=True)
    before = tree_hashes(tmp_path / "in")
    assert cli(paths, tmp_path / "in" / "new_out") == 2
    assert not (tmp_path / "in" / "new_out").exists()
    assert tree_hashes(tmp_path / "in") == before


def test_cli_missing_section_is_input_error(tmp_path):
    paths = write_bundle(tmp_path / "in")
    code = runmod.main(["inpaint", "--mode", "mask-only", "--split", str(paths["split"]), "--section", "sec_9_99", "--image-id", "IMG-01",
                        "--merge", str(paths["merge"]), "--label", str(paths["label"]), "--logo", str(paths["logo"]),
                        "--logo-debug", str(paths["logo_debug"]), "--out", str(tmp_path / "out")])
    assert code == 2
    assert json.loads((tmp_path / "out/run.json").read_text(encoding="utf-8"))["status"] == "input_error"


# ---------------------------------------------------------------------------
# 결과 타입 불변식
# ---------------------------------------------------------------------------
COUNTS = InpaintCounts(regions=1, target=1, zero_area=0, protected_region=0, in_protected_block=0, protected_blocks=0, delete_px=20,
                       protect_px=0, conflict_px=0, final_px=20)
FILES = InpaintFiles(final_mask="a.png", protect_mask="b.png", background=None)


@pytest.mark.parametrize("kw", [
    dict(mode="mask_only", status="masked", model_called=False, files=FILES.model_copy(update={"background": "c.png"})),
    dict(mode="inpaint", status="masked", model_called=False, files=FILES),
    dict(mode="inpaint", status="inpainted", model_called=False, files=FILES.model_copy(update={"background": "c.png"})),
    dict(mode="inpaint", status="unchanged", model_called=False, files=FILES.model_copy(update={"background": "c.png"})),  # final_px 20
    dict(mode="inpaint", status="inpainted", model_called=True, files=FILES),  # 배경 없음
    dict(mode="inpaint", status="failed", model_called=True, files=FILES, error=None),
    dict(mode="inpaint", status="failed", model_called=True, files=FILES.model_copy(update={"background": "c.png"}), error="x"),
    dict(mode="mask_only", status="masked", model_called=False, files=FILES, error="x"),
])
def test_inpaint_result_invariants(kw):
    with pytest.raises(ValidationError):
        InpaintResult(image_id="I", section_key="s", counts=COUNTS, **kw)


def test_inpaint_result_version_checked_on_load(tmp_path):
    r = InpaintResult(image_id="I", section_key="s", mode="mask_only", status="masked", model_called=False, files=FILES, counts=COUNTS)
    raw = json.loads(r.model_dump_json())
    raw["schema_version"] = "9"
    p = tmp_path / "r.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(jsonio.SchemaVersionError):
        jsonio.load_model(p, InpaintResult)


# ---------------------------------------------------------------------------
# 검토 반영(2026-09-30): 경로 식별자 · 중첩 입력본 · 공용 실행 함수 출력 보호 · 지연 모델 초기화
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("image_id", ["../escape", "a/b", r"a\b", "..", ".", "C:x", "", " IMG", "a\x08b", "a\nb"])
def test_unsafe_image_id_rejected_and_nothing_written_outside(tmp_path, cfg, image_id):
    j = job(tmp_path, "IMG-01")
    j["image_id"] = image_id
    j["logo"] = j["logo"].model_copy(update={"image_id": image_id})
    j["logo_record"] = logo_record_for(j["merged"], j["label"], j["logo"])
    before = sorted(p.name for p in tmp_path.iterdir())
    code, rec = execute(tmp_path / "out", [j], cfg, "mask_only")
    assert code == 2 and rec["status"] == "input_error" and "image_id" in rec["error"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(before + ["out"])
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["run.json"]


@pytest.mark.parametrize("key", ["../sec", "sec/1", r"sec\1", ".."])
def test_unsafe_section_key_rejected(tmp_path, cfg, key):
    j = job(tmp_path, "IMG-01")
    j["section"] = j["section"].model_copy(update={"section_key": key})
    code, rec = execute(tmp_path / "out", [j], cfg, "mask_only")
    assert code == 2 and "section_key" in rec["error"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["run.json"]


def test_cli_escape_image_id_writes_nothing_outside_out(tmp_path):
    paths = write_bundle(tmp_path / "in")
    code = runmod.main(["inpaint", "--mode", "mask-only", "--split", str(paths["split"]), "--section", "sec_1_01", "--image-id", "../escape",
                        "--merge", str(paths["merge"]), "--label", str(paths["label"]), "--logo", str(paths["logo"]),
                        "--logo-debug", str(paths["logo_debug"]), "--out", str(tmp_path / "out")])
    assert code == 2
    assert not (tmp_path / "escape").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in", "out"]


def test_nested_bundles_protect_outer_bundle(tmp_path):
    outer = tmp_path / "outer"
    paths = write_bundle(outer / "inner", sums=True)  # 안쪽 입력본 SHA256SUMS
    (outer / "SHA256SUMS").write_text("", encoding="utf-8")  # 바깥 입력본 SHA256SUMS
    before = tree_hashes(outer)
    assert runmod.fixed_bundle_roots(paths["merge"]) == [(outer / "inner").resolve(), outer.resolve()]
    assert cli(paths, outer / "new_out") == 2  # 안쪽 밖 · 바깥 안
    assert not (outer / "new_out").exists()
    assert tree_hashes(outer) == before


def test_execute_refuses_existing_out_and_keeps_results(tmp_path, cfg):
    j = job(tmp_path, "IMG-01")
    out = tmp_path / "out"
    assert execute(out, [j], cfg, "mask_only")[0] == 0
    before = tree_hashes(out)
    started = datetime.now(timezone.utc)
    code = runmod.execute_inpaint(out, [j], cfg, "mask_only", runmod.inpaint_run_base("mask_only", cfg, started), started)
    assert code == 2
    assert tree_hashes(out) == before


def test_execute_refuses_out_inside_bundle_of_input_paths(tmp_path, cfg):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "SHA256SUMS").write_text("", encoding="utf-8")
    (bundle / "merge.json").write_text("{}", encoding="utf-8")
    j = job(tmp_path, "IMG-01")
    j["input_paths"] = [bundle / "merge.json"]
    started = datetime.now(timezone.utc)
    code = runmod.execute_inpaint(bundle / "out", [j], cfg, "mask_only", runmod.inpaint_run_base("mask_only", cfg, started), started)
    assert code == 2 and not (bundle / "out").exists()
    # image_path가 입력본 안에 있어도 같다
    img_in_bundle = bundle / "IMG-02.png"
    Image.fromarray(image_of(W, H)).save(img_in_bundle)
    j2 = job(tmp_path, "IMG-02")
    j2["image_path"] = img_in_bundle
    code = runmod.execute_inpaint(bundle / "out2", [j2], cfg, "mask_only", runmod.inpaint_run_base("mask_only", cfg, started), started)
    assert code == 2 and not (bundle / "out2").exists()


def test_empty_masks_only_need_no_model(tmp_path, cfg):
    # 기본 팩토리(어댑터 미구현)라도 빈 마스크 섹션만 있으면 모델을 초기화하지 않고 unchanged로 끝난다
    j = job(tmp_path, "IMG-01", blocks=LOW_ONLY)
    code, rec = execute(tmp_path / "out", [j], cfg, "inpaint")
    assert code == 0 and rec["status"] == "ok" and rec["model"] is None
    res = jsonio.load_model(tmp_path / "out/IMG-01/inpaint/sec_1_01.json", InpaintResult)
    assert res.status == "unchanged" and not res.model_called


def test_lazy_init_failure_after_unchanged_section(tmp_path, cfg):
    jobs = [job(tmp_path, "IMG-01", blocks=LOW_ONLY), job(tmp_path, "IMG-02"), job(tmp_path, "IMG-03")]
    calls = []

    def factory(c):
        calls.append(1)
        raise RuntimeError("CUDA 없음")

    code, rec = execute(tmp_path / "out", jobs, cfg, "inpaint", model_factory=factory)
    assert code == 4 and "모델 초기화 실패" in rec["error"] and calls == [1]
    assert [s["status"] for s in rec["sections"]] == ["ok", "not_run", "not_run"]
    assert jsonio.load_model(tmp_path / "out/IMG-01/inpaint/sec_1_01.json", InpaintResult).status == "unchanged"
    assert not (tmp_path / "out/IMG-02").exists() and not (tmp_path / "out/IMG-03").exists()


def test_model_initialized_once_for_many_sections(tmp_path, cfg):
    jobs = [job(tmp_path, f"IMG-0{i}") for i in (1, 2, 3)]
    made = []

    def factory(c):
        made.append(FakeModel())
        return made[-1]

    code, _ = execute(tmp_path / "out", jobs, cfg, "inpaint", model_factory=factory)
    assert code == 0 and len(made) == 1 and len(made[0].calls) == 3
