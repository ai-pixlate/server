"""⑥ LaMa 어댑터 GPU 확인 · 실측 도구(실험용, 2026-09-30). 운영 코드가 아니다.

    python docs/ai-experiments/tools/inpaint_lama_probe.py synthetic --out RUN_DIR
    python docs/ai-experiments/tools/inpaint_lama_probe.py sections --input BUNDLE (--sections IMG/KEY ... | --all) --out RUN_DIR [--repeat N]
    python docs/ai-experiments/tools/inpaint_lama_probe.py timeout-check --input BUNDLE --sections IMG/KEY ... --out RUN_DIR

- synthetic: 크기가 8의 배수가 아닌 합성 RGB로 어댑터를 같은 프로세스에서 직접 호출해 크기 · dtype · 색 순서 · 패딩 제거 · 마스크 밖 동일성 ·
  반복 차이를 확인하고, 실행 계층(execute_inpaint)으로 한 번 더 돈다.
- sections: 고정 입력본의 MANIFEST 개발 대상 섹션을 execute_inpaint(inpaint 모드, 기본 팩토리 = 모델 전용 자식 프로세스 + 실제 LaMa)로 실행하고
  자동 검사(배경 크기 · 형식 · 최종 마스크 ∩ 보호 = 0 · 마스크 밖 원본 동일 · 빈 마스크 호출 0 · 실행 상태)와 측정(초기화 · 호출 번호별 추론 ·
  최대 메모리)을 남긴다. --repeat N이면 앞의 N섹션(모델 호출 섹션)을 **새 모델 프로세스**로 다시 추론해 결과 픽셀 차이를 잰다.
- timeout-check: config 시간 제한을 아주 짧게 덮어써(추론 · 초기화 각각) 강제 중단을 일으키고 자식 종료 · GPU 메모리 반환 · 실패 기록을 확인한다.
입력 불변: 실행 전후 입력본 `SHA256SUMS`에 적힌 **실제 파일 전부**의 SHA-256과 파일 집합을 대조한다(목록 파일만 비교하지 않는다).
종료 코드: 0 = 실행 성공 + 자동 검사 모두 통과 / 1 = 자동 검사 실패 / 2 = 입력 · 인자 오류(실행 전 입력본 불일치 포함) /
그 밖 = execute_inpaint 종료 코드(3 · 4). 출력은 새 폴더에만 쓴다. 입력본은 읽기만 한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline import run as runmod  # noqa: E402
from pipeline.stages import inpaint  # noqa: E402
from pipeline.types import InpaintResult, LabelResult, LogoResult  # noqa: E402


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nvidia_used_mib() -> int | None:
    """nvidia-smi 기준 GPU 사용 메모리(MiB) — 부모 프로세스에 CUDA를 올리지 않고 잰다."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             check=True, timeout=20).stdout
        return int(out.strip().splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def verify_bundle(bundle: Path) -> dict:
    """SHA256SUMS의 모든 항목을 실제 파일로 다시 계산해 대조하고, 목록 밖 · 누락 파일을 찾는다(SHA256SUMS 자신 제외)."""
    listed: dict[str, str] = {}
    for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if line.strip():
            h, _, name = line.partition("  ")
            listed[name.lstrip("*")] = h
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} - {"SHA256SUMS"}
    mismatched = [n for n, h in sorted(listed.items()) if n in actual and jsonio.sha256_file(bundle / n) != h]
    missing = sorted(set(listed) - actual)
    extra = sorted(actual - set(listed))
    return {"listed": len(listed), "files": len(actual), "sha256sums": jsonio.sha256_file(bundle / "SHA256SUMS"),
            "mismatched": mismatched, "missing": missing, "extra": extra, "ok": not (mismatched or missing or extra)}


# ---------------------------------------------------------------------------
def synthetic(out: Path) -> tuple[dict, int]:
    from pipeline.stages.inpaint_lama import LamaModel

    out.mkdir(parents=True, exist_ok=False)
    H, W = 61, 90
    left, right = np.array([200, 30, 60], np.uint8), np.array([40, 160, 220], np.uint8)
    img = np.empty((H, W, 3), np.uint8)
    img[:, : W // 2] = left
    img[:, W // 2:] = right
    img[20:24, 8:36] = 0
    img[28:40, 12:15] = 0
    mask = np.zeros((H, W), np.uint8)
    mask[17:43, 5:39] = 255
    rec: dict = {"started_at": now(), "shape": [H, W], "mask_px": int((mask > 0).sum())}
    t0 = time.perf_counter()
    model = LamaModel()
    rec["init_wall_s"] = round(time.perf_counter() - t0, 3)
    rec["describe"] = model.describe()
    outs = [model.inpaint(img.copy(), mask.copy()) for _ in range(4)]
    o = outs[0]
    final = mask > 0
    comp = inpaint.composite(img, o, final)
    fill = o[final].reshape(-1, 3).astype(float).mean(axis=0)
    checks = {
        "shape_ok": o.shape == img.shape and o.dtype == np.uint8,
        "outside_equal_after_composite": bool((comp[~final] == img[~final]).all()),
        "color_order_ok": bool(np.abs(fill - left).sum() < np.abs(fill - left[::-1]).sum()),
    }
    rec["checks"] = checks
    rec["observed"] = {"fill_mean_rgb": [round(x, 1) for x in fill], "left_bg": left.tolist(),
                       "repeat_max_abs_diff": [int(np.abs(outs[0].astype(int) - x.astype(int)).max()) for x in outs[1:]],
                       "raw_outside_mask_max_abs_diff": int(np.abs(o[~final].astype(int) - img[~final].astype(int)).max())}
    rec["calls"] = model.calls
    for name, arr in (("synthetic_input", img), ("synthetic_mask", mask), ("synthetic_model_out", o), ("synthetic_composite", comp)):
        Image.fromarray(arr).save(out / f"{name}.png")
    from tests.test_pipeline_inpaint import logo_record_for, make_block, make_inputs, rect, region

    s, m, lb, g, _ = make_inputs([make_block(1, [region("reg_0001", rect(2, 2, 4, 2))])], [(False, False)], 12, 10)
    g = g.model_copy(update={"image_id": "SYN"})
    p = out / "syn_section.png"
    Image.fromarray(np.full((10, 12, 3), (90, 120, 150), np.uint8)).save(p)
    job = {"image_id": "SYN", "section": s, "image_path": p, "merged": m, "label": lb, "logo": g, "logo_record": logo_record_for(m, lb, g)}
    cfg = cfgmod.load_config()
    started = datetime.now(timezone.utc)
    code = runmod.execute_inpaint(out / "execute", [job], cfg, "inpaint", runmod.inpaint_run_base("inpaint", cfg, started), started,
                                  model_factory=lambda c: model)
    res = jsonio.load_model(out / "execute/SYN/inpaint/sec_1_01.json", InpaintResult) if code == 0 else None
    checks["execute_ok"] = code == 0 and res is not None and res.status == "inpainted"
    rec["execute_code"] = code
    rec["ended_at"] = now()
    failed = [k for k, v in checks.items() if not v]
    rec["failed_checks"] = failed
    (out / "synthetic.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec, (1 if failed else 0)


# ---------------------------------------------------------------------------
def load_job(bundle: Path, image_id: str, key: str) -> dict:
    d = bundle / image_id
    split = jsonio.load_split(d / "split.json")
    section = next(s for s in split.sections if s.section_key == key)
    return {"image_id": image_id, "section": section, "image_path": Path(section.image_path),
            "merged": jsonio.load_merge(d / "merge" / f"{key}.json"), "label": jsonio.load_model(d / "label" / f"{key}.json", LabelResult),
            "logo": jsonio.load_model(d / "logo" / f"{key}.json", LogoResult),
            "logo_record": json.loads((d / "logo_debug" / f"{key}.json").read_text(encoding="utf-8")),
            "input_paths": [d / "split.json", d / "merge" / f"{key}.json", d / "label" / f"{key}.json", d / "logo" / f"{key}.json",
                            d / "logo_debug" / f"{key}.json"]}


def manifest_pairs(bundle: Path) -> list[tuple[str, str]]:
    manifest = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
    return [(im["image"], s["section_key"]) for im in manifest["images"] for s in im["sections"]]


def select(bundle: Path, targets: list[str] | None) -> list[tuple[str, str]]:
    allowed = manifest_pairs(bundle)
    if targets is None:
        return allowed
    pairs = [tuple(t.split("/", 1)) for t in targets]
    bad = [p for p in pairs if p not in set(allowed)]
    if bad:
        raise SystemExit(f"MANIFEST 개발 대상이 아닌 섹션: {bad}")
    return pairs


def section_rows(out: Path, pairs, jobs, run: dict) -> list[dict]:
    rows = []
    for (image_id, key), job in zip(pairs, jobs):
        entry = next(s for s in run["sections"] if s["image_id"] == image_id and s["section_key"] == key)
        row = {"section": f"{image_id}/{key}", "run_status": entry["status"], "result_status": entry.get("result_status")}
        rp = out / image_id / "inpaint" / f"{key}.json"
        if rp.is_file():
            res = jsonio.load_model(rp, InpaintResult)
            with Image.open(job["image_path"]) as im:
                orig = np.array(im)
            final = np.array(Image.open(out / res.files.final_mask)) == 255 if res.files.final_mask else None
            protect = np.array(Image.open(out / res.files.protect_mask)) == 255 if res.files.protect_mask else None
            row.update({"size": list(orig.shape[:2]), "model_called": res.model_called,
                        "final_px": res.counts.final_px if res.counts else None, "conflict_px": res.counts.conflict_px if res.counts else None})
            if final is not None and protect is not None:
                row["final_and_protect_px"] = int((final & protect).sum())
            if res.files.background:
                bg = np.array(Image.open(out / res.files.background))
                row["bg_shape_ok"] = bg.shape == orig.shape and bg.dtype == np.uint8
                row["outside_equal"] = bool((bg[~final] == orig[~final]).all())
                row["inside_changed_px"] = int((bg[final] != orig[final]).any(axis=-1).sum())
            dbg = json.loads((out / image_id / "inpaint_debug" / f"{key}.json").read_text(encoding="utf-8"))
            if dbg.get("model_call"):
                row["call"] = dbg["model_call"]
        rows.append(row)
    return rows


def row_failures(row: dict) -> list[str]:
    f = []
    if row["run_status"] != "ok":
        f.append(f"run_status={row['run_status']}")
    if row.get("final_and_protect_px", 0) != 0:
        f.append("final∩protect")
    if row.get("bg_shape_ok") is False:
        f.append("bg_shape")
    if row.get("outside_equal") is False:
        f.append("outside_changed")
    if row.get("final_px") == 0 and row.get("model_called"):
        f.append("empty_mask_model_called")
    if row["run_status"] == "ok" and "bg_shape_ok" not in row:
        f.append("no_background")
    return f


def sections(bundle: Path, targets: list[str] | None, out: Path, repeat: int) -> tuple[dict, int]:
    pre = verify_bundle(bundle)
    if not pre["ok"]:
        print(f"입력본 불일치 — 실행하지 않음: {json.dumps({k: pre[k] for k in ('mismatched', 'missing', 'extra')}, ensure_ascii=False)[:2000]}",
              file=sys.stderr)
        return {"bundle_before": pre}, 2
    pairs = select(bundle, targets)
    jobs = [load_job(bundle, i, k) for i, k in pairs]
    cfg = cfgmod.load_config()
    holder: dict = {}

    def factory(c):
        holder["model"] = inpaint.build_model(c)  # 모델 전용 자식 프로세스(spawn) + 실제 LaMa
        return holder["model"]

    gpu_before = nvidia_used_mib()
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    code = runmod.execute_inpaint(out, jobs, cfg, "inpaint", runmod.inpaint_run_base("inpaint", cfg, started), started, model_factory=factory)
    total_s = round(time.perf_counter() - t0, 3)
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    model = holder.get("model")
    rows = section_rows(out, pairs, jobs, run)
    rep = []
    if repeat and code == 0:
        m2 = inpaint.build_model(cfg)
        try:
            for (image_id, key), job in list(zip(pairs, jobs)):
                if len(rep) >= repeat:
                    break
                res = jsonio.load_model(out / image_id / "inpaint" / f"{key}.json", InpaintResult)
                if not res.model_called:
                    continue
                plan = inpaint.validate_inputs(image_id, job["section"], (job["section"].width, job["section"].height), job["merged"],
                                               job["label"], job["logo"], job["logo_record"], cfg)
                masks = inpaint.build_masks(plan)
                saved = np.array(Image.open(out / res.files.final_mask)) == 255
                with Image.open(job["image_path"]) as im:
                    orig = np.array(im)
                again = inpaint.apply(orig, plan, masks, m2).background
                first = np.array(Image.open(out / res.files.background))
                diff = np.abs(again.astype(int) - first.astype(int))
                rep.append({"section": f"{image_id}/{key}", "mask_equal": bool((masks.final == saved).all()), "max_abs_diff": int(diff.max()),
                            "diff_px": int((diff > 0).any(axis=-1).sum()), "call": m2.calls[-1]})
        finally:
            rep_term = m2.close()
    else:
        rep_term = None
    post = verify_bundle(bundle)
    failures = {r["section"]: row_failures(r) for r in rows if row_failures(r)}
    checks = {"run_ok": code == 0 and run["status"] == "ok", "sections_ok": not failures, "bundle_unchanged": post["ok"],
              "repeat_masks_equal": all(x["mask_equal"] for x in rep), "no_child_left": mp.active_children() == []}
    calls = [r["call"] for r in rows if r.get("call") and "total_s" in r["call"]]
    rec = {"started_at": started.isoformat(timespec="seconds"), "exit_code": code, "run_status": run["status"], "run_error": run["error"],
           "sections_total": len(pairs), "total_wall_s": total_s, "describe": model.describe() if model else None,
           "model_termination": run.get("model_termination"), "gpu_used_mib_before": gpu_before, "gpu_used_mib_after": nvidia_used_mib(),
           "timing": {"init_wall_s": getattr(model, "init_wall_s", None),
                      "first4_roundtrip_s": [c.get("roundtrip_s") for c in calls[:4]],
                      "after4_roundtrip_s_max": max((c.get("roundtrip_s") for c in calls[4:]), default=None),
                      "after4_roundtrip_s_median": float(np.median([c.get("roundtrip_s") for c in calls[4:]])) if len(calls) > 4 else None,
                      "roundtrip_s_max": max((c.get("roundtrip_s") for c in calls), default=None),
                      "peak_allocated_mib_max": max((c.get("peak_allocated_mib", 0) for c in calls), default=None),
                      "peak_reserved_mib_max": max((c.get("peak_reserved_mib", 0) for c in calls), default=None)},
           "sections": rows, "failures": failures, "repeat": rep, "repeat_termination": rep_term,
           "bundle_before": {k: pre[k] for k in ("listed", "files", "sha256sums", "ok")}, "bundle_after": post, "checks": checks}
    rec["failed_checks"] = [k for k, v in checks.items() if not v]
    (out.parent / f"{out.name}.probe.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec, (code if code != 0 else (1 if rec["failed_checks"] else 0))


# ---------------------------------------------------------------------------
def timeout_check(bundle: Path, targets: list[str], out: Path) -> tuple[dict, int]:
    """추론 시간 초과(첫 호출이 워밍업이라 0.05초 제한이면 반드시 넘는다)와 초기화 시간 초과(0.5초)를 일으켜 확인한다."""
    pre = verify_bundle(bundle)
    if not pre["ok"]:
        return {"bundle_before": pre}, 2
    pairs = select(bundle, targets)
    out.mkdir(parents=True, exist_ok=False)
    results = {}
    for name, overrides in (("infer_timeout", ["inpaint.infer_timeout_s=0.05"]), ("init_timeout", ["inpaint.init_timeout_s=0.5"])):
        cfg = cfgmod.load_config(overrides=overrides)
        jobs = [load_job(bundle, i, k) for i, k in pairs]
        holder: dict = {}

        def factory(c):
            holder["model"] = inpaint.build_model(c)
            return holder["model"]

        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        code = runmod.execute_inpaint(out / name, jobs, cfg, "inpaint", runmod.inpaint_run_base("inpaint", cfg, started), started,
                                      model_factory=factory)
        wall = round(time.perf_counter() - t0, 3)
        time.sleep(2)  # 드라이버가 종료된 프로세스의 메모리를 회수할 시간
        run = json.loads((out / name / "run.json").read_text(encoding="utf-8"))
        results[name] = {"exit_code": code, "run_status": run["status"], "run_error": run["error"], "wall_s": wall,
                         "sections": [s["status"] for s in run["sections"]], "model_termination": run.get("model_termination"),
                         "timeout_s": run["timeout_s"], "gpu_used_mib_after": nvidia_used_mib(), "children_after": len(mp.active_children())}
        if name == "infer_timeout":
            dbg_p = out / name / pairs[0][0] / "inpaint_debug" / f"{pairs[0][1]}.json"
            if dbg_p.is_file():
                dbg = json.loads(dbg_p.read_text(encoding="utf-8"))
                results[name]["first_section_debug"] = {"status": dbg["status"], "error": dbg["error"], "model_call": dbg.get("model_call"),
                                                        "model_termination": dbg.get("model_termination")}
    post = verify_bundle(bundle)
    it, nt = results["infer_timeout"], results["init_timeout"]
    checks = {
        "infer_timeout_exit4": it["exit_code"] == 4 and it["run_status"] == "failed",
        "infer_timeout_sections": it["sections"][:1] == ["failed"] and all(s == "not_run" for s in it["sections"][1:]),
        "init_timeout_exit4_no_results": nt["exit_code"] == 4 and all(s == "not_run" for s in nt["sections"]),
        "gpu_released": all((r["gpu_used_mib_after"] is not None and r["gpu_used_mib_after"] < 100) for r in results.values()),
        "no_child_left": all(r["children_after"] == 0 for r in results.values()),
        "bundle_unchanged": post["ok"],
    }
    rec = {"started_at": now(), "results": results, "checks": checks, "failed_checks": [k for k, v in checks.items() if not v],
           "bundle_after": {k: post[k] for k in ("listed", "files", "ok", "mismatched", "missing", "extra")}}
    (out / "timeout_check.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec, (1 if rec["failed_checks"] else 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("synthetic")
    a.add_argument("--out", required=True)
    b = sub.add_parser("sections")
    b.add_argument("--input", required=True)
    g = b.add_mutually_exclusive_group(required=True)
    g.add_argument("--sections", nargs="+")
    g.add_argument("--all", action="store_true", help="MANIFEST 개발 대상 전부")
    b.add_argument("--out", required=True)
    b.add_argument("--repeat", type=int, default=0)
    c = sub.add_parser("timeout-check")
    c.add_argument("--input", required=True)
    c.add_argument("--sections", nargs="+", required=True)
    c.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "synthetic":
        rec, code = synthetic(Path(args.out))
    elif args.cmd == "sections":
        rec, code = sections(Path(args.input), None if args.all else args.sections, Path(args.out), args.repeat)
    else:
        rec, code = timeout_check(Path(args.input), args.sections, Path(args.out))
    brief = {k: v for k, v in rec.items() if k not in ("describe", "sections", "calls", "bundle_after")}
    print(json.dumps(brief, ensure_ascii=False, indent=1)[:8000])
    print(f"종료 코드 {code}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
