"""② 타일 OCR 실측 점검 — 실제 긴 이미지에서 경계 누락 · 중복 잔존 · 오삭제 · 좌표 복원을 본다(open-questions #31 · #32).

사용(실제 PaddleOCR 환경):
    python docs/ai-experiments/tools/ocr_tile_check.py --out RUN_DIR IMG [IMG ...] [--set ocr.tile_px=1700 ...]
    python docs/ai-experiments/tools/ocr_tile_check.py --out RUN_DIR --analyze-fallback IMG   # ① 실패 → 원본 전체 섹션 경로

점검 1(경계 기준 crop): 타일 절단선 c마다 [c − R, c + R) crop을 따로 OCR하고, 그 crop 가장자리에서 떨어진 중앙
[c − R/2, c + R/2)에 들어간 기준 영역과 최종 영역을 위치(IoU ≥ 0.5)로 맞춘다. 기준에만 있는 영역 = 누락 후보,
최종에만 있는 영역 = 중복 · 과검출 후보, 짝인데 텍스트가 다름 = 판독 차이. 기준 crop도 OCR 결과라 정답이 아니다 —
후보는 overlays/의 그림으로 사람이 확인한다.
점검 2(같은 자리 중복): 최종 영역 쌍의 bbox 교집합이 작은 쪽 넓이의 50% 이상이면 중복 잔존 후보.
점검 3(좌표 복원): 모든 poly가 섹션 범위 안이고 bbox = poly 외접 사각형인지, 버린 검출마다 같은 성분에 남은 검출이 있는지.
점검 4(다른 타일링 대조): --compare-set으로 준 설정의 두 번째 실행과 위치 · 텍스트로 대조한다.
출력: RUN_DIR/<이름>/{ocr.json, ocr_debug.json, check.json, overlays/*.png}, RUN_DIR/summary.json. 원본 이미지는 복사하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from pipeline import config as cfgmod  # noqa: E402
from pipeline.stages import ocr, ocr_tile  # noqa: E402
from pipeline.types import BBox, Section, SourceImage  # noqa: E402

Image.MAX_IMAGE_PIXELS = None
R = 700  # 기준 crop 반높이


def iou(a: BBox, b: BBox) -> float:
    iw = max(0, min(a.x2, b.x2) - max(a.x, b.x))
    ih = max(0, min(a.y2, b.y2) - max(a.y, b.y))
    inter = iw * ih
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union else 0.0


def overlap_small(a: BBox, b: BBox) -> float:
    iw = max(0, min(a.x2, b.x2) - max(a.x, b.x))
    ih = max(0, min(a.y2, b.y2) - max(a.y, b.y))
    small = min(a.w * a.h, b.w * b.h)
    return iw * ih / small if small else 0.0


def match(ref: list[dict], fin: list[dict]) -> tuple[list, list, list]:
    """위치 탐욕 대응(IoU 큰 순). 반환: (짝, 기준만, 최종만)."""
    pairs = sorted(((iou(r["bbox"], f["bbox"]), i, j) for i, r in enumerate(ref) for j, f in enumerate(fin)), reverse=True)
    used_r, used_f, out = set(), set(), []
    for v, i, j in pairs:
        if v < 0.5:
            break
        if i in used_r or j in used_f:
            continue
        used_r.add(i)
        used_f.add(j)
        out.append((ref[i], fin[j], v))
    return out, [r for i, r in enumerate(ref) if i not in used_r], [f for j, f in enumerate(fin) if j not in used_f]


def as_items(regions) -> list[dict]:
    return [{"text": r.text, "norm": ocr_tile.normalize_text(r.text), "bbox": r.bbox, "score": r.score} for r in regions]


def bb(b: BBox) -> list[int]:
    return [b.x, b.y, b.w, b.h]


def draw_zone(img: np.ndarray, y0: int, y1: int, fin: list[dict], ref: list[dict], cuts: list[int], path: Path) -> None:
    im = Image.fromarray(np.ascontiguousarray(img[y0:y1, :, ::-1]))
    dr = ImageDraw.Draw(im)
    for c in cuts:
        if y0 <= c < y1:
            dr.line([(0, c - y0), (im.width, c - y0)], fill=(255, 0, 255), width=2)
    for r in ref:
        b = r["bbox"]
        dr.rectangle([b.x - 2, b.y - y0 - 2, b.x2 + 2, b.y2 - y0 + 2], outline=(0, 160, 255), width=2)
    for f in fin:
        b = f["bbox"]
        dr.rectangle([b.x, b.y - y0, b.x2, b.y2 - y0], outline=(255, 40, 40), width=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def run_one(img_path: Path, out: Path, cfg: dict, engine, compare_cfg: dict | None) -> dict:
    name = img_path.stem
    d_out = out / name
    d_out.mkdir(parents=True, exist_ok=True)
    with Image.open(img_path) as im:
        w, h = im.size
    sec = Section(section_key=f"sec_{name}", source_image_id=1, section_order=1, top_offset=0, height=h, width=w,
                  image_path=str(img_path))
    img = ocr.load_section_image(sec)
    diag: dict = {}
    t0 = time.perf_counter()
    status, err = "ok", None
    try:
        res = ocr.run(sec, cfg, engine=engine, diag=diag)
    except Exception as e:  # noqa: BLE001 — 실패도 기록한다
        status, err, res = "failed", f"{type(e).__name__}: {e}", None
    elapsed = time.perf_counter() - t0
    (d_out / "ocr_debug.json").write_text(json.dumps(diag, ensure_ascii=False, indent=1), encoding="utf-8")
    rec: dict = {"image": img_path.name, "sha256": hashlib.sha256(img_path.read_bytes()).hexdigest(), "width": w, "height": h,
                 "status": status, "error": err, "seconds": round(elapsed, 2), "tiles": diag.get("tiles"),
                 "engine_calls": diag.get("engine_calls"), "dropped": len(diag.get("dropped", [])),
                 "warnings": diag.get("warnings"), "supplements": [
                     {k: s.get(k) for k in ("boundary", "crop", "status", "clusters")} for s in diag.get("supplements", [])]}
    kinds: dict[str, int] = {}
    for c in diag.get("components", []):
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    rec["component_kinds"] = kinds
    if res is None:
        return rec
    (d_out / "ocr.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")
    fin = as_items(res.regions)
    # 점검 3: 좌표 · 버린 검출의 대응
    rec["coords_ok"] = all(0 <= x <= w and 0 <= y <= h for r in res.regions for x, y in r.poly) and all(
        r.bbox == BBox.from_poly(r.poly) for r in res.regions)
    kept_ids = {x["det"] for x in diag["regions"]}
    comp_members = {m: c for c in diag.get("components", []) for m in c["members"]}
    orphan_drops = [i for i in diag["dropped"] if not (i in comp_members and (set(comp_members[i]["members"]) & kept_ids or
                                                                               comp_members[i].get("replaced_by_supplement") is not None))]
    rec["dropped_without_kept_counterpart"] = orphan_drops
    # 점검 2: 같은 자리 중복
    dups = []
    for i, a in enumerate(fin):
        for b in fin[i + 1:]:
            if overlap_small(a["bbox"], b["bbox"]) >= 0.5:
                dups.append({"a": a["text"], "b": b["text"], "a_bbox": bb(a["bbox"]), "b_bbox": bb(b["bbox"])})
    rec["same_place_pairs"] = dups
    # 점검 1: 경계 기준 crop
    cuts = [t["y1"] for t in diag["tiles"][:-1]]
    starts = [t["y0"] for t in diag["tiles"][1:]]
    boundary_recs = []
    for b, (c, s0) in enumerate(zip(cuts, starts)):
        y0, y1 = max(0, c - R), min(h, c + R)
        raw = engine(img[y0:y1])
        ref_regions = ocr.to_result(sec, raw, w, y1 - y0).regions
        ref = [{"text": r.text, "norm": ocr_tile.normalize_text(r.text), "score": r.score,
                "bbox": BBox(x=r.bbox.x, y=r.bbox.y + y0, w=r.bbox.w, h=r.bbox.h)} for r in ref_regions]
        z0, z1 = max(0, c - R // 2), min(h, c + R // 2)
        inside = lambda it: it["bbox"].y >= z0 and it["bbox"].y2 <= z1  # noqa: E731
        ref_z = [r for r in ref if inside(r)]
        fin_z = [f for f in fin if inside(f)]
        pairs, ref_only, fin_only = match(ref_z, fin_z)
        diff = [{"ref": r["text"], "final": f["text"], "bbox": bb(f["bbox"])} for r, f, _ in pairs if r["norm"] != f["norm"]]
        brec = {"boundary": b, "cut": c, "next_start": s0, "zone": [z0, z1], "ref": len(ref_z), "final": len(fin_z),
                "matched": len(pairs), "text_diff": diff,
                "ref_only": [{"text": r["text"], "bbox": bb(r["bbox"])} for r in ref_only],
                "final_only": [{"text": f["text"], "bbox": bb(f["bbox"])} for f in fin_only]}
        boundary_recs.append(brec)
        draw_zone(img, z0, z1, fin_z, ref_z, [c, s0], d_out / "overlays" / f"boundary_{b:02d}_{c}.png")
    rec["boundaries"] = boundary_recs
    rec["boundary_totals"] = {k: sum(len(x[k]) if isinstance(x[k], list) else x[k] for x in boundary_recs)
                              for k in ("ref", "final", "matched", "text_diff", "ref_only", "final_only")}
    # 점검 4: 다른 타일링 대조
    if compare_cfg is not None:
        d2: dict = {}
        try:
            res2 = ocr.run(sec, compare_cfg, engine=engine, diag=d2)
            other = as_items(res2.regions)
            pairs, a_only, b_only = match(fin, other)
            rec["compare"] = {"tiles": [(t["y0"], t["y1"]) for t in d2["tiles"]], "regions": [len(fin), len(other)],
                              "matched": len(pairs), "text_diff": [{"a": a["text"], "b": b["text"], "bbox": bb(a["bbox"])}
                                                                   for a, b, _ in pairs if a["norm"] != b["norm"]],
                              "only_default": [{"text": x["text"], "bbox": bb(x["bbox"])} for x in a_only],
                              "only_compare": [{"text": x["text"], "bbox": bb(x["bbox"])} for x in b_only],
                              "supplements": d2["engine_calls"]["supplements"]}
        except Exception as e:  # noqa: BLE001
            rec["compare"] = {"error": f"{type(e).__name__}: {e}"}
    (d_out / "check.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return rec


class FailingVlm:
    def __call__(self, image, prompt):
        from pipeline.vlm import VlmError
        raise VlmError("실측 점검: ① 실패 경로 강제")


def analyze_fallback(img_path: Path, out: Path, cfg: dict, engine) -> dict:
    from pipeline.analyze import analyze
    d_out = out / f"analyze_fallback_{img_path.stem}"
    fallbacks: list = []
    t0 = time.perf_counter()
    res = analyze([SourceImage(source_image_id=1, upload_order=1, path=str(img_path))], cfg, d_out, use_llm=False,
                  vlm=FailingVlm(), fallbacks=fallbacks, ocr_engine=engine)
    elapsed = time.perf_counter() - t0
    (d_out / "analyze.json").write_text(res.model_dump_json(indent=1), encoding="utf-8")
    sec = res.sections[0]
    dbg = json.loads((d_out / "ocr_debug" / f"{sec.section_key}.json").read_text(encoding="utf-8"))
    return {"image": img_path.name, "fallbacks": fallbacks, "sections": [(s.section_key, s.top_offset, s.height, s.width) for s in res.sections],
            "blocks": len(res.blocks), "regions": len(dbg["regions"]), "tiles": len(dbg["tiles"]),
            "engine_calls": dbg["engine_calls"], "warnings": [w.code for w in res.warnings], "seconds": round(elapsed, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--set", action="append", default=[], help="기본 실행 override")
    ap.add_argument("--compare-set", action="append", default=None, help="대조 실행 override(예: ocr.tile_px=1700)")
    ap.add_argument("--analyze-fallback", action="store_true")
    a = ap.parse_args()
    cfg = cfgmod.load_config(overrides=a.set)
    engine = ocr.build_engine(cfg)
    a.out.mkdir(parents=True, exist_ok=True)
    summary: dict = {"engine": engine.info, "config_ocr": cfg["ocr"], "compare_set": a.compare_set, "images": []}
    for p in a.images:
        if a.analyze_fallback:
            r = analyze_fallback(p, a.out, cfg, engine)
        else:
            cmp_cfg = cfgmod.load_config(overrides=a.set + a.compare_set) if a.compare_set else None
            r = run_one(p, a.out, cfg, engine, cmp_cfg)
        summary["images"].append(r)
        print(json.dumps({k: r.get(k) for k in ("image", "status", "seconds", "engine_calls", "boundary_totals",
                                                 "same_place_pairs", "dropped_without_kept_counterpart", "fallbacks",
                                                 "blocks", "regions")}, ensure_ascii=False, default=str))
    name = "summary_fallback.json" if a.analyze_fallback else "summary.json"
    (a.out / name).write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
