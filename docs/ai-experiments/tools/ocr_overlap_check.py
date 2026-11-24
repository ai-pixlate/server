"""② 겹친 검출 재인식 · 호출별 중복 제거 실측 점검(#32 · #40) — 긴 원본을 한 섹션으로 OCR하고 교체 · 거절 · 제거 기록과
후속 영향(교체 영역 좌표 정합 · ③ 휴리스틱 블록의 원문 · ⑥ 마스크 입력 범위)을 요약한다.

    python docs/ai-experiments/tools/ocr_overlap_check.py IMG [IMG ...] --out DIR [--set ocr.tile_px=1700]

- 실제 PaddleOCR 엔진(`ocr.build_engine`)을 쓴다. 장치는 설치된 paddle 빌드가 정한다 — `--device cpu`를 주면 엔진을 만들기 전에
  `paddle.set_device("cpu")`로 CPU를 강제하고, 만든 뒤 실제 predictor 장치를 기록한다.
- 설정 실험은 `--set`(override)으로만 한다. 운영 기본값을 바꾸지 않는다.
- ③은 `heuristic_v2`만(LLM 없음), ⑥은 모델 없이 `pipeline.stages.inpaint`의 채우기 · 팽창 함수로 영역별 마스크 픽셀만 센다
  (라벨 · 로고 보호 · score_min 분류는 반영하지 않는다 — 교체 전후 영역 자체의 마스크 범위 비교용).
- 산출물(진단 JSON · OCR JSON · 요약 JSON)은 --out에 쓴다. docs/ai-experiments/runs/는 git 제외.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from pipeline import config as cfgmod  # noqa: E402
from pipeline.errors import AnalyzeError  # noqa: E402
from pipeline.stages import inpaint, merge, ocr  # noqa: E402
from pipeline.types import BBox, Section  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def region_mask_px(polys: list[list[tuple[int, int]]], heights: list[int], ratio: float) -> int:
    """영역마다 poly를 채우고 ceil(bbox.h × ratio) 원형 커널로 따로 팽창한 합집합의 픽셀 수(⑥ 규칙의 대상 영역 부분)."""
    pts = [p for poly in polys for p in poly]
    rmax = max(inpaint.dilate_radius(h, ratio) for h in heights)
    x0 = min(p[0] for p in pts) - rmax - 2
    y0 = min(p[1] for p in pts) - rmax - 2
    w = max(p[0] for p in pts) + rmax + 3 - x0
    h = max(p[1] for p in pts) + rmax + 3 - y0
    acc = np.zeros((h, w), dtype=bool)
    for poly, bh in zip(polys, heights):
        acc |= inpaint.dilate(inpaint.fill_polygon(poly, x0, y0, w, h), inpaint.dilate_radius(bh, ratio))
    return int(acc.sum())


def inside(pt: tuple[int, int], poly: list[tuple[int, int]], tol: float) -> bool:
    """볼록 다각형 poly 안(경계에서 tol 픽셀 밖까지 허용)."""
    n = len(poly)
    area = sum(poly[i][0] * poly[(i + 1) % n][1] - poly[(i + 1) % n][0] * poly[i][1] for i in range(n))
    sign = 1 if area > 0 else -1
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        cross = (bx - ax) * (pt[1] - ay) - (by - ay) * (pt[0] - ax)
        length = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 or 1
        if sign * cross / length < -tol:
            return False
    return True


def check_image(img: Path, cfg: dict, engine, out: Path) -> dict:
    with Image.open(img) as im:
        w, h = im.size
    sec = Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=h, width=w,
                  image_path=str(img))
    diag: dict = {}
    t = time.perf_counter()
    res, err = None, None
    try:
        res = ocr.run(sec, cfg, engine=engine, diag=diag)
    except AnalyzeError as e:
        err = {"code": e.code, "retryable": e.retryable, "reason": e.reason, "message": str(e)}
    dt = round(time.perf_counter() - t, 2)
    stem = img.stem
    (out / f"{stem}.diag.json").write_text(json.dumps(diag, ensure_ascii=False, indent=1), encoding="utf-8")
    if res is not None:
        (out / f"{stem}.ocr.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")
    rr = diag.get("overlap_rec", {})
    summary = {
        "image": stem, "size": [w, h], "seconds": dt, "status": "failed" if err else "ok", "error": err,
        "tiles": len(diag.get("tiles", [])), "supplements": diag.get("engine_calls", {}).get("supplements"),
        "overlap_rec_calls": rr.get("calls"), "cache_hits": rr.get("cache_hits"), "limit_reached": rr.get("limit_reached"),
        "unresolved_conflicts": len(diag.get("failed", [])),
        "dedup_removed": sum(c.get("removed", 0) for c in diag.get("dedup", []))
        + sum(s.get("dedup", {}).get("removed", 0) for s in diag.get("supplements", [])),
        "units": [{"stage": u["stage"], "decision": u.get("decision"), "reason": u.get("reason")} for u in rr.get("units", [])],
        "replacements": [],
    }
    final = {r["det"]: r["region_key"] for r in diag.get("regions", [])}
    merged = None
    if res is not None and any(s.get("decision") == "replaced" for u in rr.get("units", []) for s in u["sides"]):
        merged = merge.run(sec, res, cfg, use_llm=False)
    regs = {r.region_key: r for r in res.regions} if res is not None else {}
    ratio = cfg["inpaint"]["dilate_ratio"]
    for u in rr.get("units", []):
        for s in u["sides"]:
            if s.get("decision") != "replaced":
                continue
            orig = s["original"]
            rk = final.get(s["new_det"])
            rec = {"stage": u["stage"], "call": s["call"], "original": [o["text"] for o in orig],
                   "original_scores": [o["score"] for o in orig], "reread": s["reread"]["text"],
                   "reread_score": s["reread"]["score"], "removed": s["explain"]["removed"],
                   "crop_box": s["reread"]["box"], "final_region": rk}
            if rk is not None:
                r = regs[rk]
                opolys = [[tuple(q) for q in o["poly"]] for o in orig]
                rec["poly"] = [list(q) for q in r.poly]
                rec["bbox_matches_poly"] = r.bbox == BBox.from_poly(r.poly)
                rec["poly_in_section"] = all(0 <= x <= w and 0 <= y <= h for x, y in r.poly)
                rec["covers_original_polys"] = all(inside(q, r.poly, 1.0) for p in opolys for q in p)
                ob = BBox.union([BBox.from_poly(p) for p in opolys])
                rec["bbox_vs_original_union"] = {"new": [r.bbox.x, r.bbox.y, r.bbox.w, r.bbox.h],
                                                 "original_union": [ob.x, ob.y, ob.w, ob.h]}
                old_px = region_mask_px(opolys, [BBox.from_poly(p).h for p in opolys], ratio)
                new_px = region_mask_px([list(r.poly)], [r.bbox.h], ratio)
                rec["mask_px"] = {"original_regions": old_px, "replaced_region": new_px,
                                  "ratio": round(new_px / old_px, 4) if old_px else None}
                if merged is not None:
                    blk = next(b for b in merged.blocks for ln in b.source_lines for x in ln.regions if x.region_key == rk)
                    rec["block"] = {"block_key": blk.block_key, "source_ko": blk.source_ko,
                                    "region_keys": [x.region_key for ln in blk.source_lines for x in ln.regions],
                                    "reread_occurrences": blk.source_ko.replace(" ", "").count(r.text.replace(" ", "")),
                                    "original_text_left": [t for t in (o["text"] for o in orig)
                                                           if t.replace(" ", "") not in r.text.replace(" ", "")
                                                           and t.replace(" ", "") in blk.source_ko.replace(" ", "")]}
            summary["replacements"].append(rec)
    if merged is not None:
        keys = [x.region_key for b in merged.blocks for ln in b.source_lines for x in ln.regions]
        summary["merge_region_check"] = {"regions": len(res.regions), "in_blocks": len(keys), "unique": len(set(keys)),
                                         "same_set": set(keys) == set(regs)}
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--device", choices=["auto", "cpu"], default="auto")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=False)
    cfg = cfgmod.load_config(overrides=a.set)
    if a.device == "cpu":
        import paddle
        paddle.set_device("cpu")
    engine = ocr.build_engine(cfg)
    rec_model = engine._ocr.paddlex_pipeline.text_rec_model
    run = {"branch": git("branch", "--show-current"), "commit": git("rev-parse", "HEAD"),
           "dirty_files": git("status", "--short").splitlines(), "overrides": a.set, "engine": engine.info,
           "rec_predictor_device": getattr(rec_model, "device", None), "config_ocr": cfg["ocr"]}
    rows = [check_image(p, cfg, engine, a.out) for p in a.images]
    (a.out / "summary.json").write_text(json.dumps({"run": run, "images": rows}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    for r in rows:
        print(r["image"], r["status"], "conflicts", r["unresolved_conflicts"], "rerec", r["overlap_rec_calls"],
              "replaced", len(r["replacements"]), "dedup", r["dedup_removed"], f"{r['seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
