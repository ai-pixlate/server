"""⑥ 인페인팅 결과 자동 선별 지표(실험용, 2026-09-30) — 사람 검수의 **우선순위 후보**를 고르는 도구이며 판정이 아니다.

    python docs/ai-experiments/tools/inpaint_auto_screen.py --run pipeline/out/inpaint-run100-v1_2026-09-30 \
        --input pipeline/samples/local/downstream-input-v1 --out docs/ai-experiments/runs/<...>/auto_screen.json [--crops DIR]

삭제 대상 영역(진단의 `target`)마다 창 = 영역 bbox + 팽창 반경 + 여백에서 계산한다(결과 · 원본 · 최종/보호 마스크, 회색조 = RGB 평균):
- edge_res / edge_orig: 최종 마스크 안(경계 2px 안쪽)에서 밝기 경사 |dx|+|dy| > 40인 픽셀 비율 — 결과 · 원본. 결과에 글자 획이 남으면 edge_res가 높다.
- edge_ring: 마스크 바깥 띠(1~6px, 보호 제외)의 결과 경사 비율 — 배경 자체의 질감 기준.
- patch_diff: 마스크 안 결과 평균색과 바깥 띠 결과 평균색의 차(RGB 평균 절대값), ring_std: 띠의 밝기 표준편차(균일한 배경일수록 작다).
- box_*: 마스크 안 원본의 평평한 픽셀(경사 ≤ 8) 중 최빈색(채널별 16단계 양자화)을 글자 뒤 바탕색으로 보고, 그 색이 바깥 띠 원본 평균과
  다르고(box_vs_ring) 마스크의 상당 부분을 차지하며(box_share) 결과 마스크 안 평균이 바탕색보다 띠 색에 가까우면(box_kept 낮음) 상자 · 형광펜이
  지워진 후보다. 굵은 글자 속(평평한 글자색)이 최빈색이 되면 오탐이 난다 — 육안 확인 대상.
- protect_adjacent: 이 영역의 팽창 창에 닿는 저신뢰 · 빈 텍스트 보호 영역 수.
후보 규칙(잠정, 13섹션 관찰로 정한 값 — 검증된 기준 아님)은 CANDIDATE_RULES. 결과는 섹션별 후보 영역 목록과 지표 원값이다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import jsonio  # noqa: E402
from pipeline.stages.inpaint import dilate  # noqa: E402
from pipeline.types import InpaintResult  # noqa: E402

GRAD_T = 40
CANDIDATE_RULES = {
    "residual_text": "edge_res >= 0.06 그리고 edge_res >= 2 × edge_ring 그리고 edge_res >= 0.25 × edge_orig",
    "smudge": "ring_std <= 6(균일 배경) 그리고 patch_diff >= 3",
    "design_damage": "box_vs_ring >= 25 그리고 box_share >= 0.3 그리고 box_kept <= 0.5",
    "protect_fragment": "protect_adjacent >= 1",
}


def flags_of(m: dict) -> list[str]:
    f = []
    if m["edge_res"] is not None and m["edge_res"] >= 0.06 and m["edge_res"] >= 2 * max(m["edge_ring"] or 0, 1e-6) and m["edge_res"] >= 0.25 * (m["edge_orig"] or 0):
        f.append("residual_text")
    if m["ring_std"] is not None and m["ring_std"] <= 6 and (m["patch_diff"] or 0) >= 3:
        f.append("smudge")
    if (m["box_vs_ring"] or 0) >= 25 and (m["box_share"] or 0) >= 0.3 and m["box_kept"] is not None and m["box_kept"] <= 0.5:
        f.append("design_damage")
    if m["protect_adjacent"] >= 1:
        f.append("protect_fragment")
    return f


def grad(gray: np.ndarray) -> np.ndarray:
    g = np.zeros_like(gray)
    g[:, 1:] += np.abs(np.diff(gray, axis=1))
    g[1:, :] += np.abs(np.diff(gray, axis=0))
    return g


def bbox_hit(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def region_metrics(orig, res, final, protect, bbox, r, prot_boxes):
    H, W = final.shape
    pad = r + 8
    x0, y0 = max(bbox["x"] - pad, 0), max(bbox["y"] - pad, 0)
    x1, y1 = min(bbox["x"] + bbox["w"] + pad, W), min(bbox["y"] + bbox["h"] + pad, H)
    m = final[y0:y1, x0:x1]
    # 이 영역 창의 최종 마스크 중 영역 bbox + 반경 안쪽만(이웃 영역 섞임을 줄인다)
    own = np.zeros_like(m)
    ox0, oy0 = max(bbox["x"] - r - x0, 0), max(bbox["y"] - r - y0, 0)
    own[oy0:bbox["y"] + bbox["h"] + r - y0, ox0:bbox["x"] + bbox["w"] + r - x0] = True
    m = m & own
    p = protect[y0:y1, x0:x1]
    o = orig[y0:y1, x0:x1].astype(np.float32)
    s = res[y0:y1, x0:x1].astype(np.float32)
    inner = m & ~dilate(~m, 2)
    ring = dilate(m, 6) & ~dilate(m, 1) & ~p & ~final[y0:y1, x0:x1]
    go, gs = grad(o.mean(axis=2)), grad(s.mean(axis=2))
    out = {"window": [int(x0), int(y0), int(x1), int(y1)], "mask_px": int(m.sum()), "edge_res": None, "edge_orig": None, "edge_ring": None,
           "patch_diff": None, "ring_std": None, "bg_change": None}
    if inner.sum() >= 20:
        out["edge_res"] = round(float((gs[inner] > GRAD_T).mean()), 4)
        out["edge_orig"] = round(float((go[inner] > GRAD_T).mean()), 4)
    if ring.sum() >= 20:
        out["edge_ring"] = round(float((gs[ring] > GRAD_T).mean()), 4)
        out["ring_std"] = round(float(s.mean(axis=2)[ring].std()), 2)
        if m.sum() >= 20:
            out["patch_diff"] = round(float(np.abs(s[m].mean(axis=0) - s[ring].mean(axis=0)).mean()), 2)
    out.update({"box_vs_ring": None, "box_share": None, "box_kept": None})
    flat = m & (go <= 8)
    if flat.sum() >= 30 and ring.sum() >= 20:
        q = (o[flat] // 16).astype(np.int32)
        codes = q[:, 0] * 256 + q[:, 1] * 16 + q[:, 2]
        vals, cnt = np.unique(codes, return_counts=True)
        mode = vals[cnt.argmax()]
        sel = codes == mode
        box = o[flat][sel].mean(axis=0)
        ring_o = o[ring].mean(axis=0)
        res_in = s[m].mean(axis=0)
        d_box_ring = float(np.abs(box - ring_o).mean())
        out["box_vs_ring"] = round(d_box_ring, 2)
        out["box_share"] = round(float(sel.sum() / max(m.sum(), 1)), 3)
        # 결과 평균을 (띠 색 → 상자색) 축에 투영: 0 = 띠 색(상자 지워짐) · 1 = 상자색 유지
        axis = box - ring_o
        out["box_kept"] = round(float(np.clip(np.dot(res_in - ring_o, axis) / (float(np.dot(axis, axis)) or 1.0), -1, 2)), 3)
    win = (bbox["x"] - r, bbox["y"] - r, bbox["x"] + bbox["w"] + r, bbox["y"] + bbox["h"] + r)
    out["protect_adjacent"] = sum(1 for pb in prot_boxes if bbox_hit(win, pb))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--crops", help="후보 영역 확대 이미지(원본 | 결과 | 대비 강조)를 쓸 폴더(선택)")
    a = ap.parse_args(argv)
    run, inp, out = Path(a.run), Path(a.input), Path(a.out)
    rec = json.loads((run / "run.json").read_text(encoding="utf-8"))
    crops = Path(a.crops) if a.crops else None
    if crops:
        crops.mkdir(parents=True, exist_ok=True)
    result = {"rules": CANDIDATE_RULES, "grad_threshold": GRAD_T, "run_started_at": rec.get("started_at"), "sections": {}}
    for entry in rec["sections"]:
        image_id, key = entry["image_id"], entry["section_key"]
        res_m = jsonio.load_model(run / entry["result"], InpaintResult)
        dbg = json.loads((run / entry["debug"]).read_text(encoding="utf-8"))
        split = jsonio.load_split(inp / image_id / "split.json")
        section = next(s for s in split.sections if s.section_key == key)
        with Image.open(section.image_path) as im:
            orig = np.array(im.convert("RGB"))
        final = np.array(Image.open(run / res_m.files.final_mask)) == 255
        protect = np.array(Image.open(run / res_m.files.protect_mask)) == 255
        with Image.open(run / res_m.files.background) as b:
            res = np.array(b.convert("RGB"))
        prot_boxes = [(r["bbox"]["x"], r["bbox"]["y"], r["bbox"]["x"] + r["bbox"]["w"], r["bbox"]["y"] + r["bbox"]["h"])
                      for r in dbg["regions"] if r["kind"] == "protected_region"]
        regs = []
        for rg in dbg["regions"]:
            if rg["kind"] != "target":
                continue
            mt = region_metrics(orig, res, final, protect, rg["bbox"], rg["radius"], prot_boxes)
            mt["flags"] = flags_of(mt)
            regs.append({"region_key": rg["region_key"], "block_key": rg["block_key"], "bbox": rg["bbox"], **mt})
        counts = {f: sum(1 for r in regs if f in r["flags"]) for f in CANDIDATE_RULES}
        cand = [r for r in regs if r["flags"]]
        result["sections"][f"{image_id}|{key}"] = {"targets": len(regs), "candidate_counts": counts, "candidates": cand,
                                                   "conflict_px": res_m.counts.conflict_px if res_m.counts else 0}
        if crops:
            med = np.median(res.reshape(-1, 3).astype(np.float32), axis=0)
            for r in sorted(cand, key=lambda r: -len(r["flags"]))[:4]:
                x0, y0, x1, y1 = r["window"]
                tiles = [orig[y0:y1, x0:x1], res[y0:y1, x0:x1],
                         np.clip((res[y0:y1, x0:x1].astype(np.float32) - med) * 4 + 128, 0, 255).astype(np.uint8)]
                sheet = np.concatenate([np.pad(t, ((0, 0), (0, 6), (0, 0)), constant_values=255) for t in tiles], axis=1)
                Image.fromarray(sheet).save(crops / f"{image_id}__{key}__{r['region_key']}__{'-'.join(r['flags'])}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    tot = {f: sum(s["candidate_counts"][f] for s in result["sections"].values()) for f in CANDIDATE_RULES}
    secs = {f: sum(1 for s in result["sections"].values() if s["candidate_counts"][f]) for f in CANDIDATE_RULES}
    print(json.dumps({"regions_flagged": tot, "sections_flagged": secs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
