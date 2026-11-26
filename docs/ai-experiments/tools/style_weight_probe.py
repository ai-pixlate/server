"""⑦ 글자 굵기 측정 가능성 실측(2026-11-26) — 개발용, 운영 경로 아님. 운영 코드 · config · 저장된 결과는 바꾸지 않는다.

    T=docs/ai-experiments/tools/style_weight_probe.py
    O=docs/ai-experiments/runs/2026-11-26_07-style_weight-measure
    R=docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01
    python $T measure --run $R --gs GS-01 GS-02 --out $O            # 줄별 측정 후보값 → $O/lines.json
    python $T page --lines $O/lines.json --run $R --out $O/label     # 사람 라벨 페이지(정적 HTML)
    python $T score --lines $O/lines.json --tsv <라벨 TSV> [<덮어쓸 라벨 TSV> …] --out $O/score_<이름>.json
    # 에이전트 라벨 중 사람 검수가 필요한 줄만 추려 따로 라벨 페이지를 만든다
    python $T select --lines $O/lines.json --tsv <에이전트 라벨 TSV> --cut 0.1458 --out $O/human_review_list.tsv
    python $T page --lines $O/lines.json --run $R --only $O/human_review_list.tsv --out $O/label_review

질문: 원본 글자의 굵기를 ⑦ 단계에서 따로 잴 수 있는가. 지금은 굵기를 재지 않고 역할별 설정값(title · price SemiBold, 나머지 Regular)으로만 그린다.

measure — 입력(읽기만): 베이스라인 회차의 `evidence/<GS>.json` · `objects/`(원본 섹션 이미지). ⑦ 처리 대상 블록의 측정 영역마다 bbox를 그대로
crop해 ⑦과 같은 방식으로 글자 · 배경을 나눈다(`pipeline.stages.style`의 `luma` · `otsu_threshold` · `border_mask` — 명도 Otsu, 외곽에 덜 닿는
집단이 글자). 새 실행 · 모델 호출 없음. 영역별 후보값:
- `stroke_med` 획 두께(px) = 글자 픽셀마다 min(가로 연속 길이, 세로 연속 길이)의 중앙값. 정수 px라 작은 글자에서 뭉개진다.
- `stroke_mean` 같은 값의 평균.
- `ap` = 2 × 글자 면적 ÷ 둘레(4-이웃에 배경이 있는 글자 픽셀 수). 획 모양이면 두께에 가깝고 소수로 나온다.
- `density` = 글자 픽셀 ÷ 영역 픽셀.
- `ink_h` 글자 높이(px) = 글자 픽셀이 있는 행 범위. 굵기 후보 = 위 두께 ÷ `ink_h`(A `stroke_med` · B `stroke_mean` · C `ap`) · D `density`.
줄 값 = 그 줄 측정 영역 값의 중앙값. 글자 픽셀이 20개 미만이거나 두 집단으로 나뉘지 않는 영역은 뺀다.

page — 라벨 표본: 글자 높이 구간(BANDS) × 그 구간 안 후보 C의 3분위마다 QUOTA개(구간 안 (GS, 블록, 줄)의 SHA-256 순), 그리고 블록 안 줄끼리
후보 C가 PAIR_RATIO배 이상 다른 블록 PAIR_BLOCKS개의 줄(블록당 최대 4줄). 카드 순서는 SHA-256으로 섞는다. 측정값은 페이지에 보이지 않는다.
질문 하나 — 빨간 상자 줄의 글자 굵기. 라벨 기준은 Pretendard 실제 굵기 다섯 가지(Thin 100 · Light 300 · Medium 500 · Bold 700 · Black 900 —
사용자 결정 2026-11-26)와 줄 안에 섞임 · 판단 불가이고, 머리에 그 다섯 굵기 견본을 둔다(--font-zip 공식 배포 zip에서 그린다).
--out이 이미 있으면 거부한다. 입력은 브라우저에만 저장되고 "TSV 내보내기"로 받는다.

score — 라벨 TSV(열: case_no · gs · block_line("블록id:줄번호") · weight · …)와 lines.json을 대조한다. 두 묶음 채점: 굵음 = bold · black,
보통 = thin · light · medium(기준을 바꾸기 전 첫 표본 TSV의 heavy는 굵음, regular는 보통으로 묶는다. mixed · unsure는 뺀다).
다섯 굵기 채점은 `five_level`(--font-zip을 주면 견본 굵기의 측정값과 대조). 후보별 AUC(굵은 줄이 보통 줄보다 값이 클 확률) · 균형 정확도가 가장 높은 경계값 · 글자 높이 구간별 · 한글 포함 여부별,
같은 블록 안 굵음 – 보통 줄 쌍의 순서 정답률, 같은 블록 안 같은 라벨 줄 쌍의 값 비율(흔들림). **경계값은 관찰값이지 확정값이 아니다.**
TSV를 여러 개 주면 뒤 파일의 라벨이 같은 줄의 앞 파일 라벨을 덮는다(에이전트 라벨 위에 사람 라벨을 얹는 용도).

select — 에이전트 라벨 가운데 사람이 다시 봐야 할 줄을 추린다(도구가 정한 기준 — 사유를 함께 적는다):
`low_conf` 에이전트 메모에 "확신 낮음" · `mixed` 줄 안에 굵기가 섞임 · `thin` 얇음(얇음 / 보통 경계는 주관적) ·
`disagree` 후보 B와 라벨이 어긋남(B ≥ --cut인데 보통, 또는 그 반대 — 글자 높이 25px 미만은 `disagree_small`) ·
`near_cut` 라벨과 맞지만 B가 --cut ± NEAR 안(글자 높이 25px 이상). "판단 불가"는 넣지 않는다.
page --only <그 목록>이면 목록의 줄만 카드로 만들고, 사례 번호는 전체 표본의 번호를 그대로 쓴다. 에이전트 라벨 · 사유는 "참고 보기"에만 보인다.

page --verify <에이전트 라벨 TSV> — 사람이 라벨을 새로 붙이지 않고 에이전트 라벨이 맞는지만 본다. 라벨이 붙은 줄을 모두 라벨별로 묶어 보여 주고,
선택지는 "맞음" · "틀림 → 실제 굵기"(여섯 라벨) · "틀림 → 실제는 모르겠음" · "모르겠음"이다. --only 목록을 함께 주면 그 줄에 "다시 볼 줄" 표시만 붙인다.
에이전트 라벨을 본 뒤의 판단이라 맹검이 아니다. score --verify <내보낸 TSV>는 맞음 · 모르겠음 · 미확인 줄은 에이전트 라벨 그대로,
실제 굵기를 고른 줄은 그 라벨로 바꾸고, "실제는 모르겠음"은 채점에서 뺀다.
    python $T page --lines $O/lines.json --run $R --verify <에이전트 라벨 TSV> --only $O/human_review_list.tsv --out $O/label_verify
    python $T score --lines $O/lines.json --tsv <에이전트 라벨 TSV> --verify <내보낸 TSV> --out $O/score_verified.json
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import itertools
import json
import statistics
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.stages.style import border_mask, luma, otsu_threshold  # noqa: E402
from style_intra_block_diff import region_rows, style_payloads  # noqa: E402

METRICS = {"A": "획 중앙값 ÷ 글자 높이", "B": "획 평균 ÷ 글자 높이", "C": "2·면적/둘레 ÷ 글자 높이", "D": "글자 픽셀 비율"}
BANDS = ((0, 15), (15, 25), (25, 40), (40, 70), (70, 10 ** 6))
QUOTA = {0: 2, 15: 7, 25: 8, 40: 8, 70: 5}  # 구간 시작값 → 3분위마다 뽑는 수
PAIR_RATIO, PAIR_BLOCKS, PAIR_LINES = 1.3, 12, 4
MIN_INK = 20
# 라벨 기준 = Pretendard 실제 굵기 다섯 가지(사용자 결정 2026-11-26). 줄을 가장 가까운 견본으로 라벨한다.
WEIGHTS = {"thin": 100, "light": 300, "medium": 500, "bold": 700, "black": 900}
FACE = {"thin": "Thin", "light": "Light", "medium": "Medium", "bold": "Bold", "black": "Black"}
ORDER = tuple(WEIGHTS)
VERDICTS = tuple((k, f"{FACE[k]} {w}") for k, w in WEIGHTS.items()) + (("mixed", "줄 안에 굵기가 섞임"), ("unsure", "판단 불가"))
# 두 묶음 채점용. regular · heavy는 기준을 바꾸기 전 라벨(얇음 · 보통 · 굵음 · 아주 굵음 — 에이전트가 정한 기준, 첫 표본 TSV)이다
BOLD, PLAIN = ("bold", "black", "heavy"), ("thin", "light", "medium", "regular")
LABEL_COLOR = {"thin": (0, 150, 190), "light": (30, 100, 220), "medium": (35, 150, 65), "bold": (235, 130, 0), "black": (215, 30, 30),
               "mixed": (150, 60, 190), "unsure": (120, 120, 120)}
LABEL_KO = dict(VERDICTS) | {"regular": "보통(이전 기준)", "heavy": "아주 굵음(이전 기준)"}
REF_TEXT, REF_PX = "촉촉한 마무리감으로", 40  # 견본 측정값(ref_values)을 재는 글 · 크기
# 확인 페이지(page --verify)의 선택지 — 맞음, 또는 틀리면 실제 굵기
VERIFY = (("ok", "맞음"),) + tuple((v, f"틀림 → {t}") for v, t in VERDICTS) + (("wrong", "틀림 → 실제는 모르겠음"), ("skip", "모르겠음"))
REASON_KO = {"low_conf": "에이전트 확신 낮음", "mixed": "줄 안에 섞임", "thin": "얇음 / 보통 경계", "disagree": "측정값과 어긋남",
             "disagree_small": "측정값과 어긋남(작은 글자)", "near_cut": "측정값이 경계 근처"}


def fg_mask(rgb: np.ndarray) -> np.ndarray | None:
    """⑦과 같은 분리 — 명도 Otsu 두 집단 중 외곽에 덜 닿는 쪽이 글자. 나눌 수 없거나 외곽 접촉이 같으면 None."""
    y = luma(rgb)
    t = otsu_threshold(y)
    if t is None:
        return None
    dark = y <= t
    bm = border_mask(*y.shape)
    bd, bl = int((dark & bm).sum()), int((~dark & bm).sum())
    if bd == bl:
        return None
    return dark if bd < bl else ~dark


def run_lengths(mask: np.ndarray) -> np.ndarray:
    """각 True 픽셀이 속한 가로 연속 구간의 길이."""
    out = np.zeros(mask.shape, dtype=np.int32)
    for i, row in enumerate(mask):
        edges = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        for s, e in zip(edges[::2], edges[1::2]):
            out[i, s:e] = e - s
    return out


def measure_mask(mask: np.ndarray | None) -> dict | None:
    if mask is None or int(mask.sum()) < MIN_INK:
        return None
    mask = np.ascontiguousarray(mask)
    local = np.minimum(run_lengths(mask), run_lengths(np.ascontiguousarray(mask.T)).T)[mask]
    rows = np.flatnonzero(mask.any(axis=1))
    p = np.pad(mask, 1)
    edge = mask & ~(p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:])
    return {"stroke_med": float(np.median(local)), "stroke_mean": float(local.mean()), "ink_h": int(rows[-1] - rows[0] + 1),
            "ap": 2 * float(mask.sum()) / int(edge.sum()), "density": float(mask.mean())}


def candidates(m: dict) -> dict:
    return {"A": m["stroke_med"] / m["ink_h"], "B": m["stroke_mean"] / m["ink_h"], "C": m["ap"] / m["ink_h"], "D": m["density"]}


def cmd_measure(a: argparse.Namespace) -> int:
    idx = json.loads((a.run / "object-index.json").read_text(encoding="utf-8"))
    ims: dict[str, np.ndarray] = {}
    lines, c = [], collections.Counter()
    for gs in a.gs:
        d = json.loads((a.run / "evidence" / f"{gs}.json").read_text(encoding="utf-8"))
        tb = {b["id"]: b for b in d["text_block"]}
        sec = {s["id"]: s for s in d["section"]}
        for sid, p in sorted(style_payloads(d).items()):
            key = sec[sid]["image_key"]
            if key not in ims:
                ims[key] = np.asarray(Image.open(a.run / "objects" / idx[key]["sha256"]).convert("RGB"))
            im = ims[key]
            for sb in p["blocks"]:
                if sb["status"] == "excluded":
                    continue
                b = tb[int(sb["block_key"].split("_")[1])]
                by: dict[int, list[dict]] = collections.defaultdict(list)
                for r in region_rows(b, sb):
                    if r["status"] != "measured":
                        continue
                    c["regions"] += 1
                    bb = r["bbox"]
                    m = measure_mask(fg_mask(im[bb["y"]:bb["y"] + bb["h"], bb["x"]:bb["x"] + bb["w"]]))
                    if m is None:
                        c["regions_unmeasured"] += 1
                        continue
                    by[r["line_index"]].append({"text": r["text"], "bbox": bb, "hangul": r["hangul"], "est_font_px": r["est_font_px"], **m, **candidates(m)})
                for li, regs in sorted(by.items()):
                    x0, y0 = min(r["bbox"]["x"] for r in regs), min(r["bbox"]["y"] for r in regs)
                    x1, y1 = max(r["bbox"]["x"] + r["bbox"]["w"] for r in regs), max(r["bbox"]["y"] + r["bbox"]["h"] for r in regs)
                    row = {"gs": gs, "block_id": b["id"], "section_id": sid, "role": b["role"], "line": li + 1, "n_lines": len(b["source_lines"]),
                           "text": " ".join(r["text"] for r in regs), "bbox": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
                           "block_bbox": b["bbox"], "image_key": key, "n_regions": len(regs), "hangul": any(r["hangul"] for r in regs)}
                    for k in ("ink_h", "stroke_med", "stroke_mean", "ap", "density", "est_font_px", "A", "B", "C", "D"):
                        row[k] = statistics.median(r[k] for r in regs)
                    lines.append(row)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "lines.json").write_text(json.dumps(lines, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    band = collections.Counter(next(lo for lo, hi in BANDS if lo <= ln["ink_h"] < hi) for ln in lines)
    print(f"줄 {len(lines)} · 블록 {len({(ln['gs'], ln['block_id']) for ln in lines})} · 영역 {c['regions']}(측정 못 함 {c['regions_unmeasured']}) · "
          f"글자 높이 구간별 줄 {dict(sorted(band.items()))}")
    return 0


# ---------------------------------------------------------------------------
# 라벨 페이지
# ---------------------------------------------------------------------------
def _h(ln: dict) -> str:
    return hashlib.sha256(f"{ln['gs']}:{ln['block_id']}:{ln['line']}".encode()).hexdigest()


def pick(lines: list[dict]) -> list[dict]:
    chosen: dict[tuple, dict] = {}

    def add(ln: dict, why: str) -> None:
        chosen.setdefault((ln["gs"], ln["block_id"], ln["line"]), dict(ln, sample=why))

    for lo, hi in BANDS:
        rows = sorted((ln for ln in lines if lo <= ln["ink_h"] < hi), key=lambda ln: ln["C"])
        for t in range(3):
            part = rows[len(rows) * t // 3: len(rows) * (t + 1) // 3]
            for ln in sorted(part, key=_h)[:QUOTA[lo]]:
                add(ln, f"band{lo}_t{t + 1}")
    by: dict[tuple, list[dict]] = collections.defaultdict(list)
    for ln in lines:
        if ln["ink_h"] >= 15:
            by[(ln["gs"], ln["block_id"])].append(ln)
    pairs = [v for v in by.values() if len(v) >= 2 and max(x["C"] for x in v) / min(x["C"] for x in v) >= PAIR_RATIO]
    for v in sorted(pairs, key=lambda v: _h(v[0]))[:PAIR_BLOCKS]:
        for ln in sorted(v, key=lambda x: x["line"])[:PAIR_LINES]:
            add(ln, "pair")
    return sorted(chosen.values(), key=_h)


def line_crops(im: Image.Image, ln: dict) -> tuple[Image.Image, Image.Image]:
    """줄을 키운 crop(상자 없음)과, 블록 주변에서 그 줄에 빨간 상자를 그린 crop."""
    b = ln["bbox"]
    pad = max(6, b["h"] // 4)
    z = im.crop((max(0, b["x"] - pad), max(0, b["y"] - pad), min(im.width, b["x"] + b["w"] + pad), min(im.height, b["y"] + b["h"] + pad)))
    s = min(6, max(1, round(64 / b["h"])), max(1, 1400 // z.width))
    if s > 1:
        z = z.resize((z.width * s, z.height * s), Image.LANCZOS)
    k = ln["block_bbox"]
    cp = max(40, k["h"] // 3)
    x0, y0 = max(0, k["x"] - cp), max(0, k["y"] - cp)
    ctx = im.crop((x0, y0, min(im.width, k["x"] + k["w"] + cp), min(im.height, k["y"] + k["h"] + cp))).copy()
    ImageDraw.Draw(ctx).rectangle((b["x"] - x0 - 2, b["y"] - y0 - 2, b["x"] - x0 + b["w"] + 1, b["y"] - y0 + b["h"] + 1), outline=(255, 0, 0), width=2)
    return z, ctx


def load_fonts(zip_path: Path | None, faces: dict | None = None) -> dict:
    """견본 굵기 다섯 가지의 OTF를 공식 배포 zip(Pretendard v1.3.9)에서 읽는다. zip의 SHA-256이 설정 자료(`style_defaults.json`)의 값과 달라야
    하면 거부한다. 저장소에는 Regular · SemiBold만 있어 견본을 그리려면 zip이 필요하다 — 글꼴 파일을 저장소에 추가하지 않는다.
    faces = {키: 굵기 이름}을 주면 그 굵기들을 읽는다(기본은 라벨 기준 다섯 굵기)."""
    import io
    import zipfile

    if zip_path is None:
        raise SystemExit("견본 굵기를 그리려면 --font-zip <공식 Pretendard-1.3.9.zip>이 필요하다")
    want = json.loads((ROOT / "pipeline" / "data" / "style_defaults.json").read_text(encoding="utf-8"))["fonts"]["package"]["zip_sha256"]
    raw = zip_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != want:
        raise SystemExit(f"zip SHA-256이 설정 자료의 값({want[:12]}…)과 다르다 — 공식 배포본이 아니다: {zip_path}")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        return {k: z.read(f"public/static/Pretendard-{face}.otf") for k, face in (faces or FACE).items()}


def _font(fonts: dict[str, bytes], key: str, px: int) -> ImageFont.FreeTypeFont:
    import io

    return ImageFont.truetype(io.BytesIO(fonts[key]), px)


def reference(fonts: dict[str, bytes], px: int = 38) -> Image.Image:
    """라벨 기준이 되는 Pretendard 다섯 굵기 견본. 이름표 색 = 확인 페이지의 상자 색."""
    im = Image.new("RGB", (900, len(ORDER) * (px + 26) + 8), (255, 255, 255))
    dr = ImageDraw.Draw(im)
    small = _font(fonts, "medium", 14)
    for i, k in enumerate(ORDER):
        y = 6 + i * (px + 26)
        dr.rectangle((8, y, 8 + int(dr.textlength(LABEL_KO[k], font=small)) + 12, y + 18), fill=LABEL_COLOR[k])
        dr.text((14, y + 1), LABEL_KO[k], font=small, fill=(255, 255, 255))
        dr.text((8, y + 19), "촉촉한 마무리감 Calming Sun 98%", font=_font(fonts, k, px), fill=(30, 30, 30))
    return im


def ref_values(fonts: dict[str, bytes]) -> dict[str, float]:
    """견본 다섯 굵기를 줄과 같은 방식으로 잰 후보 B(검정 글자 · 흰 바탕, 한글 REF_TEXT · REF_PX px)."""
    out = {}
    for k in ORDER:
        f = _font(fonts, k, REF_PX)
        l, t, r, b = f.getbbox(REF_TEXT)
        im = Image.new("RGB", (r - l + 8, b - t + 8), (255, 255, 255))
        ImageDraw.Draw(im).text((4 - l, 4 - t), REF_TEXT, font=f, fill=(20, 20, 20))
        out[k] = round(candidates(measure_mask(fg_mask(np.asarray(im))))["B"], 4)
    return out


def cmd_page(a: argparse.Namespace) -> int:
    from typeset_align_review_page import render_page
    from typeset_overflow_review_page import esc

    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    raw = a.lines.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    cases = list(enumerate(pick(json.loads(raw.decode("utf-8"))), 1))  # (전체 표본의 사례 번호, 줄)
    def cid_of(ln: dict) -> str:
        return f"{ln['gs']}|{ln['block_id']}:{ln['line']}"

    refs: dict[str, str] = {}
    flagged: dict[str, str] = {}
    agent: dict[str, dict] = {}
    if a.only:
        rows = {f"{r['gs']}|{r['block_line']}": r for r in read_tsv(a.only)}
        refs = {k: f"에이전트 라벨 {r['agent_label']} · 검수 사유 {r['reasons']}" + (f" · 메모 {r['agent_note']}" if r["agent_note"] else "")
                for k, r in rows.items()}
        flagged = {k: " · ".join(REASON_KO.get(x, x) for x in r["reasons"].split(",")) for k, r in rows.items()}
    if a.verify:  # 에이전트 라벨을 보여 주고 맞음 / 틀림만 받는다 — 라벨별로 묶어 보여 준다(--only는 "다시 볼 줄" 표시에만 쓴다)
        agent = {f"{r['gs']}|{r['block_line']}": r for r in read_tsv(a.verify) if r.get("weight")}
        order = [v for v, _ in VERDICTS]
        cases = sorted(((n, ln) for n, ln in cases if cid_of(ln) in agent), key=lambda c: (order.index(agent[cid_of(c[1])]["weight"]), c[0]))
        assert len(cases) == len(agent), "라벨의 줄이 표본에 없다"
    elif a.only:
        cases = [(n, ln) for n, ln in cases if cid_of(ln) in refs]
        assert len(cases) == len(refs), "목록의 줄이 표본에 없다"
    idx = json.loads((a.run / "object-index.json").read_text(encoding="utf-8"))
    opened: dict[str, Image.Image] = {}

    def obj(key: str) -> Image.Image:
        if key not in opened:
            opened[key] = Image.open(a.run / "objects" / idx[key]["sha256"]).convert("RGB")
        return opened[key]

    a.out.mkdir(parents=True)
    (a.out / "img").mkdir()
    reference(load_fonts(a.font_zip)).save(a.out / "img" / "reference.png")
    cards, ids = [], []
    for n, ln in cases:
        cid = f"{ln['gs']}|{ln['block_id']}:{ln['line']}"
        ids.append(cid)
        z, ctx = line_crops(obj(ln["image_key"]), ln)
        stem = f"{ln['gs']}_{ln['block_id']}_{ln['line']}"
        z.save(a.out / "img" / f"{stem}_line.png")
        ctx.save(a.out / "img" / f"{stem}_ctx.png")
        radios = "".join(f'<label><input type="radio" name="v:{esc(cid)}" value="{v}"> {esc(t)}</label>' for v, t in (VERIFY if a.verify else VERDICTS))
        ref = f'<div class="ref"><b>참고</b> {esc(refs[cid])}</div>' if cid in refs and not a.verify else ""
        badge = ""
        if a.verify:
            r = agent[cid]
            badge = (f'<p style="margin:4px 0 6px;font-size:15px"><b style="background:#1d1d1f;color:#fff;padding:2px 9px;border-radius:4px">'
                     f'에이전트 라벨: {esc(LABEL_KO[r["weight"]])}</b>'
                     + (f' <span class="sub">메모: {esc(r["note"])}</span>' if r.get("note") else "")
                     + (f' <span style="background:#ffe9a8;padding:2px 7px;border-radius:4px;font-size:12.5px;margin-left:6px">다시 볼 줄 — {esc(flagged[cid])}</span>'
                        if cid in flagged else "") + "</p>")
        cards.append(
            f'<section class="card" id="{esc(cid)}" data-id="{esc(cid)}">'
            f'<h3>사례 {n} <span class="sub">{esc(ln["gs"])} · 블록 {esc(ln["block_id"])} · 줄 {ln["line"]} / {ln["n_lines"]} · {esc(ln["role"])}</span></h3>'
            f'{badge}<div class="txt"><div><b>OCR 텍스트</b> {esc(ln["text"])}</div></div>'
            f'<div class="imgs"><figure><figcaption>이 줄(키운 그림)</figcaption><img src="img/{stem}_line.png" alt="줄" loading="lazy"></figure>'
            f'<figure><figcaption>블록 주변 — 빨간 상자가 이 줄</figcaption><img src="img/{stem}_ctx.png" alt="주변" loading="lazy"></figure></div>{ref}'
            f'<div class="form"><div class="row"><b>{"확인" if a.verify else "굵기"}</b>{radios}<button type="button" class="clear">지우기</button></div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="예: 앞 두 단어만 굵음 · 흐려서 판단 어려움"></div></div></section>')
    (a.out / "cases.json").write_text(json.dumps(
        [{"case_no": n, "gs": ln["gs"], "block_id": ln["block_id"], "line": ln["line"], "role": ln["role"], "sample": ln["sample"], "text": ln["text"]}
         for n, ln in cases], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if a.verify:
        key = f"style-weight-verify:{fp}"
        n_flag = sum(cid in flagged for cid in ids)
        intro = ('<b>확인</b> — 각 줄에 적힌 <b>에이전트 라벨</b>이 <b>빨간 상자 줄</b>의 글자 굵기와 맞나요? 맞으면 "맞음", 틀리면 실제 굵기를 고릅니다'
                 '(실제 굵기를 고르기 어려우면 "틀림 → 실제는 모르겠음"). 아래 Pretendard 다섯 굵기 견본이 기준입니다.<br>'
                 '<img src="img/reference.png" alt="견본" style="max-width:100%;border:1px solid #ddd;margin:6px 0"><br>'
                 '라벨 = 견본 가운데 가장 가까운 굵기, 한 줄 안에서 일부 단어만 굵기가 다르면 '
                 '줄 안에 섞임, 글자가 너무 작거나 흐리거나 글자가 아니면 판단 불가. 글꼴이 견본과 달라도 획의 두께만 봅니다. 이미지를 누르면 원본 픽셀 크기로 봅니다.<br>'
                 f'사례 {len(cases)}개 — 에이전트 라벨별로 묶여 있습니다(가는 굵기 → 굵은 굵기 → 섞임 → 판단 불가). '
                 f'노란 표시 "다시 볼 줄"({n_flag}개)은 에이전트가 확신이 낮았거나 측정값과 어긋나거나 경계에 가까운 줄입니다. 측정값은 이 페이지에 없습니다. '
                 '입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>'
                 f'<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 입력 지문 {fp} · 저장 키 {esc(key)}</span>')
        page = render_page("⑦ 글자 굵기 — 에이전트 라벨 확인", intro, cards, ids, key, fp, VERIFY,
                           "case_no\\tgs\\tblock_line\\tverdict\\tnote\\treview_fingerprint", f"style_weight_verify_{fp}.tsv")
        (a.out / "index.html").write_text(page, encoding="utf-8")
        print(f"확인 페이지: {a.out / 'index.html'} · 사례 {len(cases)}(다시 볼 줄 {n_flag}) · 저장 키 {key}")
        return 0
    key = f"style-weight-label{'-review' if a.only else ''}:{fp}"
    intro = ('<b>질문</b> — <b>빨간 상자 줄</b>의 글자 굵기는 어느 쪽인가요? 아래 Pretendard 다섯 굵기 견본을 기준으로 고릅니다.<br>'
             '<img src="img/reference.png" alt="견본" style="max-width:100%;border:1px solid #ddd;margin:6px 0"><br>'
             '견본 다섯 굵기 가운데 가장 가까운 것을 고릅니다. 한 줄 안에서 일부 단어만 굵기가 다르면 '
             '"줄 안에 굵기가 섞임"을 고르고 메모에 어느 부분인지 적어 주세요. 글자가 너무 작거나 흐려서 알 수 없으면 "판단 불가"입니다.<br>'
             '글꼴이 견본과 달라도 획의 두께만 봅니다. 글자 크기 · 색은 판단에 넣지 않습니다. 이미지를 누르면 원본 픽셀 크기로 봅니다.<br>'
             + (f'사례 {len(cases)}개 — 에이전트 라벨 가운데 사람이 다시 봐야 할 줄만 추렸습니다(사례 번호는 전체 표본의 번호). '
                '"참고 보기"는 에이전트 라벨과 추린 사유이며 사람 판단이 아닙니다 — 먼저 끈 채로 판단하기를 권합니다. 측정값은 이 페이지에 없습니다. '
                if a.only else
                f'사례 {len(cases)}개 — 글자 높이와 측정 후보값이 고르게 섞이도록 뽑았고 순서는 섞여 있습니다. 측정값은 이 페이지에 없습니다. ')
             + '입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>'
             f'<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 입력 지문 {fp} · 저장 키 {esc(key)}</span>')
    page = render_page("⑦ 글자 굵기 — 라벨", intro, cards, ids, key, fp, VERDICTS,
                       "case_no\\tgs\\tblock_line\\tweight\\tnote\\treview_fingerprint", f"style_weight_label_{fp}.tsv")
    (a.out / "index.html").write_text(page, encoding="utf-8")
    c = collections.Counter(ln["sample"].split("_")[0] for _, ln in cases)
    print(f"라벨 페이지: {a.out / 'index.html'} · 사례 {len(cases)} {dict(sorted(c.items()))} · 저장 키 {key}")
    return 0


# ---------------------------------------------------------------------------
# 채점
# ---------------------------------------------------------------------------
def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    return round(sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg)), 3)


def best_cut(pos: list[float], neg: list[float]) -> dict | None:
    """균형 정확도가 가장 높은 경계값(값 ≥ 경계 → 굵음). 관찰값이다."""
    if not pos or not neg:
        return None
    vals = sorted(set(pos + neg))
    best = None
    for lo, hi in zip(vals, vals[1:]):
        t = (lo + hi) / 2
        tpr, tnr = sum(p >= t for p in pos) / len(pos), sum(q < t for q in neg) / len(neg)
        if best is None or (tpr + tnr) / 2 > best["balanced_acc"]:
            best = {"cut": round(t, 4), "balanced_acc": round((tpr + tnr) / 2, 3), "bold_hit": f"{sum(p >= t for p in pos)}/{len(pos)}",
                    "plain_hit": f"{sum(q < t for q in neg)}/{len(neg)}"}
    return best


def read_tsv(path: Path) -> list[dict]:
    import csv

    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


NEAR = 0.015  # select: 경계값 근처로 보는 폭(후보 B)


def cmd_select(a: argparse.Namespace) -> int:
    """에이전트 라벨 중 사람 검수가 필요한 줄(머리말 select). 기준은 도구가 정한 것이다."""
    lines = {(ln["gs"], ln["block_id"], ln["line"]): ln for ln in json.loads(a.lines.read_text(encoding="utf-8"))}
    out, c = ["case_no\tgs\tblock_line\tagent_label\treasons\tagent_note"], collections.Counter()
    for r in read_tsv(a.tsv):
        bid, li = r["block_line"].split(":")
        ln, lab, why = lines[(r["gs"], int(bid), int(li))], r["weight"], []
        if "확신 낮음" in r["note"]:
            why.append("low_conf")
        if lab == "mixed":
            why.append("mixed")
        if lab == "thin":
            why.append("thin")
        if lab in BOLD + PLAIN:
            if (ln["B"] >= a.cut) != (lab in BOLD):
                why.append("disagree" if ln["ink_h"] >= 25 else "disagree_small")
            elif ln["ink_h"] >= 25 and abs(ln["B"] - a.cut) <= NEAR:
                why.append("near_cut")
        if why:
            c.update(why)
            out.append("\t".join([r["case_no"], r["gs"], r["block_line"], lab, ",".join(why), r["note"]]))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"사람 검수 대상 {len(out) - 1}줄 {dict(sorted(c.items()))} → {a.out}")
    return 0


def _spearman(x: list[float], y: list[float]) -> float | None:
    """순위 상관(동순위는 평균 순위)."""
    if len(x) < 3:
        return None

    def rank(v: list[float]) -> np.ndarray:
        a = np.asarray(v, dtype=float)
        order = a.argsort(kind="stable")
        r = np.empty(len(a))
        r[order] = np.arange(1, len(a) + 1)
        for val in np.unique(a):
            r[a == val] = r[a == val].mean()
        return r
    rx, ry = rank(x), rank(y)
    return None if rx.std() == 0 or ry.std() == 0 else round(float(np.corrcoef(rx, ry)[0, 1]), 3)


def five_level(rows: list[dict], fonts: dict[str, bytes] | None) -> dict:
    """다섯 굵기 라벨(ORDER) 채점 — 후보 B 기준. 라벨이 ORDER 밖인 줄(섞임 · 판단 불가 · 이전 기준 라벨)은 뺀다.
    - 라벨별 B 중앙값, 라벨 순위와 B의 순위 상관.
    - 같은 섹션(이미지) 안 라벨이 다른 줄 쌍에서 굵은 쪽의 B가 큰 비율(한 단계 차이 / 두 단계 이상 차이).
    - 글꼴 zip이 있으면: 견본 다섯 굵기의 B(`ref_values`)에 가장 가까운(로그 거리) 굵기를 측정 굵기로 보고 라벨과 대조 —
      일치 · 한 단계 이내 · 평균 단계 차이 · 혼동표. 같은 줄의 지금 조판 굵기(역할 설정 — title · price 600, 나머지 400)와 라벨의
      평균 굵기 차이(굵기 단위)도 함께 낸다. 견본 값은 검정 글자 · 흰 바탕 한글 렌더에서 잰 것이라 실제 이미지와 조건이 다르다."""
    import math

    if any(r["label"] in ("regular", "heavy") for r in rows):
        return {"skipped": "기준을 바꾸기 전 라벨(regular · heavy)이 있는 TSV다 — 다섯 굵기 채점을 하지 않는다"}
    rs = [r for r in rows if r["label"] in ORDER]
    out: dict = {"used": len(rs), "labels": {k: sum(r["label"] == k for r in rs) for k in ORDER}}
    if not rs:
        return out
    refs = ref_values(fonts) if fonts else None
    out["reference_B"] = refs
    subsets = {"all": rs, "ink_h>=25": [r for r in rs if r["ink_h"] >= 25], "hangul&ink_h>=25": [r for r in rs if r["hangul"] and r["ink_h"] >= 25]}
    for name, v in subsets.items():
        m: dict = {"n": len(v), "median_B_by_label": {k: round(statistics.median(r["B"] for r in v if r["label"] == k), 4)
                                                      for k in ORDER if any(r["label"] == k for r in v)},
                   "spearman": _spearman([ORDER.index(r["label"]) for r in v], [r["B"] for r in v])}
        by: dict[tuple, list[dict]] = collections.defaultdict(list)
        for r in v:
            by[(r["gs"], r["section_id"])].append(r)
        pairs = [(x, y, ORDER.index(x["label"]) - ORDER.index(y["label"])) for g in by.values() for x, y in itertools.permutations(g, 2)
                 if ORDER.index(x["label"]) > ORDER.index(y["label"])]
        for gap, key in ((1, "one_step"), (2, "two_plus_steps")):
            p = [(x, y) for x, y, d in pairs if (d == 1 if gap == 1 else d >= 2)]
            m[f"in_section_pairs_{key}"] = {"pairs": len(p), "order_right": sum(x["B"] > y["B"] for x, y in p)}
        if refs and v:
            pred = [min(ORDER, key=lambda k: abs(math.log(r["B"] / refs[k]))) for r in v]
            diff = [ORDER.index(p) - ORDER.index(r["label"]) for p, r in zip(pred, v)]
            role = [600 if r["role"] in ("title", "price") else 400 for r in v]
            m["measured_vs_label"] = {
                "exact": sum(d == 0 for d in diff), "within_one": sum(abs(d) <= 1 for d in diff), "measured_heavier": sum(d > 0 for d in diff),
                "measured_lighter": sum(d < 0 for d in diff), "mean_abs_steps": round(sum(abs(d) for d in diff) / len(v), 3),
                "mean_abs_weight": round(sum(abs(WEIGHTS[p] - WEIGHTS[r["label"]]) for p, r in zip(pred, v)) / len(v), 1),
                "confusion_label_to_measured": {k: {j: sum(p == j for p, r in zip(pred, v) if r["label"] == k) for j in ORDER}
                                                for k in ORDER if any(r["label"] == k for r in v)}}
            m["role_setting_vs_label"] = {"mean_abs_weight": round(sum(abs(w - WEIGHTS[r["label"]]) for w, r in zip(role, v)) / len(v), 1),
                                          "role_600": sum(w == 600 for w in role), "role_400": sum(w == 400 for w in role)}
        out[name] = m
    return out


def cmd_score(a: argparse.Namespace) -> int:
    lines = {(ln["gs"], ln["block_id"], ln["line"]): ln for ln in json.loads(a.lines.read_text(encoding="utf-8"))}
    labels: dict[tuple, str] = {}
    overridden = 0
    for i, path in enumerate(a.tsv):  # 뒤 파일이 같은 줄의 앞 라벨을 덮는다
        for r in read_tsv(path):
            if not r.get("weight"):
                continue
            bid, li = r["block_line"].split(":")
            k = (r["gs"], int(bid), int(li))
            overridden += i > 0 and k in labels and labels[k] != r["weight"]
            labels[k] = r["weight"]
    verify: dict = {}
    if a.verify:  # 확인 페이지 결과 — ok는 그대로, 실제 굵기를 골랐으면 바꾸고, wrong(실제 모름)은 뺀다. skip · 미확인은 그대로 둔다
        v = collections.Counter()
        for r in read_tsv(a.verify):
            if not r.get("verdict"):
                continue
            bid, li = r["block_line"].split(":")
            k = (r["gs"], int(bid), int(li))
            if r["verdict"] == "ok":
                v["confirmed"] += 1
            elif r["verdict"] == "skip":
                v["skipped"] += 1
            elif r["verdict"] == "wrong":
                v["wrong_dropped"] += 1
                labels.pop(k)
            elif r["verdict"] == labels[k]:
                v["confirmed"] += 1
            else:
                v["corrected"] += 1
                v[f"{labels[k]}→{r['verdict']}"] += 1
                labels[k] = r["verdict"]
        verify = dict(v) | {"not_reviewed": len(labels) + v["wrong_dropped"] - v["confirmed"] - v["skipped"] - v["corrected"] - v["wrong_dropped"]}
    rows = [dict(lines[k], label=v) for k, v in labels.items()]
    used = [r for r in rows if r["label"] in BOLD + PLAIN]
    out: dict = {"tsv": [p.as_posix() for p in a.tsv], "changed_by_later_tsv": overridden, "verify": verify, "labeled": len(rows),
                 "labels": dict(collections.Counter(r["label"] for r in rows)), "used": len(used), "metrics": {}}

    def groups(rs: list[dict], k: str) -> tuple[list[float], list[float]]:
        return [r[k] for r in rs if r["label"] in BOLD], [r[k] for r in rs if r["label"] in PLAIN]

    subsets = {"all": used, "ink_h<25": [r for r in used if r["ink_h"] < 25], "ink_h>=25": [r for r in used if r["ink_h"] >= 25],
               "hangul": [r for r in used if r["hangul"]], "no_hangul": [r for r in used if not r["hangul"]],
               "hangul&ink_h>=25": [r for r in used if r["hangul"] and r["ink_h"] >= 25]}
    by_block: dict[tuple, list[dict]] = collections.defaultdict(list)
    by_section: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in used:
        by_block[(r["gs"], r["block_id"])].append(r)
        by_section[(r["gs"], r["section_id"])].append(r)
    for k, name in METRICS.items():
        m: dict = {"name": name, "median_by_label": {lab: round(statistics.median(r[k] for r in rows if r["label"] == lab), 4)
                                                     for lab, _ in VERDICTS if any(r["label"] == lab for r in rows)}}
        for sname, rs in subsets.items():
            pos, neg = groups(rs, k)
            m[sname] = {"bold": len(pos), "plain": len(neg), "auc": auc(pos, neg), "best": best_cut(pos, neg)}
        diff = [(x, y) for v in by_block.values() for x, y in itertools.permutations(v, 2) if x["label"] in BOLD and y["label"] in PLAIN]
        same = [max(x[k], y[k]) / min(x[k], y[k]) for v in by_block.values() for x, y in itertools.combinations(v, 2)
                if (x["label"] in BOLD) == (y["label"] in BOLD)]
        m["in_block_pairs"] = {"bold_vs_plain": len(diff), "order_right": sum(x[k] > y[k] for x, y in diff),
                               "ratio_bold_over_plain_median": round(statistics.median(x[k] / y[k] for x, y in diff), 3) if diff else None,
                               "ratio_bold_over_plain_min": round(min(x[k] / y[k] for x, y in diff), 3) if diff else None,
                               "same_label_pairs": len(same), "same_label_ratio_median": round(statistics.median(same), 3) if same else None,
                               "same_label_ratio_max": round(max(same), 3) if same else None}
        # 같은 섹션(이미지) 안 굵음 – 보통 줄 쌍 — 전체와, 두 줄 모두 글자 높이 25px 이상인 쌍만
        sdiff = [(x, y) for v in by_section.values() for x, y in itertools.permutations(v, 2) if x["label"] in BOLD and y["label"] in PLAIN]
        big = [(x, y) for x, y in sdiff if x["ink_h"] >= 25 and y["ink_h"] >= 25]
        m["in_section_pairs"] = {"bold_vs_plain": len(sdiff), "order_right": sum(x[k] > y[k] for x, y in sdiff),
                                 "both_ink_h>=25": len(big), "order_right_both_ink_h>=25": sum(x[k] > y[k] for x, y in big),
                                 "sections_with_both": sum(1 for v in by_section.values() if {r["label"] in BOLD for r in v} == {True, False})}
        out["metrics"][k] = m
    out["five_level"] = five_level(rows, load_fonts(a.font_zip) if a.font_zip else None)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


# ---------------------------------------------------------------------------
# 섹션 이미지 단위 라벨 · 확인 — 한 이미지 안의 줄끼리 견주어 본다
# ---------------------------------------------------------------------------
SEC_MIN_H, SEC_LINES, SEC_MAX_PX, SEC_COUNT = 15, (3, 24), 2600, 20



def section_cases(lines: list[dict]) -> list[tuple[tuple, list[dict]]]:
    """라벨할 섹션 — 글자 높이 SEC_MIN_H 이상인 줄이 SEC_LINES개이고 그 줄들의 세로 범위가 SEC_MAX_PX 이하인 섹션을 SHA-256 순으로 SEC_COUNT개.
    줄 번호는 섹션 안에서 위 → 아래, 왼쪽 → 오른쪽."""
    by: dict[tuple, list[dict]] = collections.defaultdict(list)
    for ln in lines:
        if ln["ink_h"] >= SEC_MIN_H:
            by[(ln["gs"], ln["section_id"])].append(ln)
    out = []
    for k, v in by.items():
        v.sort(key=lambda x: (x["bbox"]["y"], x["bbox"]["x"]))
        span = max(x["bbox"]["y"] + x["bbox"]["h"] for x in v) - min(x["bbox"]["y"] for x in v)
        if SEC_LINES[0] <= len(v) <= SEC_LINES[1] and span <= SEC_MAX_PX:
            out.append((k, v))
    return sorted(out, key=lambda kv: hashlib.sha256(f"{kv[0][0]}:{kv[0][1]}".encode()).hexdigest())[:SEC_COUNT]


def section_image(im: Image.Image, v: list[dict], labels: dict[int, str] | None, boxes: bool = True) -> Image.Image:
    """섹션에서 줄들이 있는 세로 범위를 잘라 줄마다 번호와 상자를 그린다. labels가 있으면 상자 · 번호 색 = 라벨 색."""
    top = max(0, min(x["bbox"]["y"] for x in v) - 40)
    z = im.crop((0, top, im.width, min(im.height, max(x["bbox"]["y"] + x["bbox"]["h"] for x in v) + 40))).copy()
    if not boxes:
        return z
    d = ImageDraw.Draw(z)
    font = ImageFont.truetype(str(ROOT / "pipeline" / "data" / "fonts" / "pretendard" / "Pretendard-SemiBold.otf"), 15)
    for n, x in enumerate(v, 1):
        b = x["bbox"]
        c = LABEL_COLOR[labels[n]] if labels else (225, 0, 90)
        d.rectangle((b["x"] - 4, b["y"] - top - 4, b["x"] + b["w"] + 3, b["y"] - top + b["h"] + 3), outline=c, width=2)
        w = int(d.textlength(str(n), font=font)) + 8
        tx, ty = max(0, b["x"] - 4 - w), max(0, b["y"] - top - 4)
        d.rectangle((tx, ty, tx + w, ty + 19), fill=c)
        d.text((tx + 4, ty + 1), str(n), font=font, fill=(255, 255, 255))
    return z


def _open_images(run: Path):
    idx = json.loads((run / "object-index.json").read_text(encoding="utf-8"))
    opened: dict[str, Image.Image] = {}

    def obj(key: str) -> Image.Image:
        if key not in opened:
            opened[key] = Image.open(run / "objects" / idx[key]["sha256"]).convert("RGB")
        return opened[key]
    return obj


def cmd_sections(a: argparse.Namespace) -> int:
    """라벨용 시트 — 섹션마다 줄 번호를 그린 그림과 줄 목록(라벨 없음)."""
    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    cases = section_cases(json.loads(a.lines.read_text(encoding="utf-8")))
    obj = _open_images(a.run)
    a.out.mkdir(parents=True)
    listing = []
    for (gs, sid), v in cases:
        section_image(obj(v[0]["image_key"]), v, None).save(a.out / f"{gs}_{sid}.png")
        listing.append({"gs": gs, "section_id": sid, "lines": [{"n": n, "block_line": f"{x['block_id']}:{x['line']}", "role": x["role"], "text": x["text"]}
                                                               for n, x in enumerate(v, 1)]})
    (a.out / "sections.json").write_text(json.dumps(listing, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"섹션 {len(cases)} · 줄 {sum(len(v) for _, v in cases)} → {a.out}")
    return 0


def cmd_section_page(a: argparse.Namespace) -> int:
    """이미지 단위 확인 페이지 — 섹션 그림 위에 에이전트 라벨을 색으로 표시하고, 틀린 줄만 고친 뒤 이미지마다 "확인 완료"를 누른다."""
    from typeset_overflow_review_page import esc

    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    raw = a.lines.read_bytes()
    fp = hashlib.sha256(raw + a.labels.read_bytes()).hexdigest()[:16]
    agent = {(r["gs"], r["block_line"]): r for r in read_tsv(a.labels) if r.get("weight")}
    cases = section_cases(json.loads(raw.decode("utf-8")))
    assert all((gs, f"{x['block_id']}:{x['line']}") in agent for (gs, _), v in cases for x in v), "라벨이 없는 줄이 있다"
    weights = list(ORDER)

    def kinds(gs: str, v: list[dict]) -> int:
        return len({agent[(gs, f"{x['block_id']}:{x['line']}")]["weight"] for x in v} & set(weights))
    cases.sort(key=lambda kv: -min(kinds(kv[0][0], kv[1]), 2))  # 굵기를 다르게 본 이미지가 먼저(안정 정렬)
    obj = _open_images(a.run)
    a.out.mkdir(parents=True)
    (a.out / "img").mkdir()
    reference(load_fonts(a.font_zip)).save(a.out / "img" / "reference.png")
    cards, data = [], []
    for i, ((gs, sid), v) in enumerate(cases, 1):
        labs = {n: agent[(gs, f"{x['block_id']}:{x['line']}")] for n, x in enumerate(v, 1)}
        im = obj(v[0]["image_key"])
        section_image(im, v, {n: r["weight"] for n, r in labs.items()}).save(a.out / "img" / f"{gs}_{sid}_box.png")
        section_image(im, v, None, boxes=False).save(a.out / "img" / f"{gs}_{sid}_plain.png")
        cid = f"{gs}|{sid}"
        data.append({"id": cid, "gs": gs, "lines": [{"k": f"{x['block_id']}:{x['line']}", "n": n} for n, x in enumerate(v, 1)]})
        count = collections.Counter(r["weight"] for r in labs.values())
        summary = " · ".join(f'<span class="chip" style="background:rgb{LABEL_COLOR[w]}">{esc(LABEL_KO[w])} {count[w]}</span>' for w, _ in VERDICTS if count[w])
        opts = '<option value="">맞음</option>' + "".join(f'<option value="{w}">실제: {esc(t)}</option>' for w, t in VERDICTS) + \
            '<option value="wrong">틀림(실제는 모르겠음)</option>'
        rows = "".join(
            f'<tr><td><span class="chip" style="background:rgb{LABEL_COLOR[labs[n]["weight"]]}">{n}</span></td>'
            f'<td><b style="color:rgb{LABEL_COLOR[labs[n]["weight"]]}">{esc(LABEL_KO[labs[n]["weight"]])}</b></td>'
            f'<td>{esc(x["text"])}{" <i>— " + esc(labs[n]["note"]) + "</i>" if labs[n].get("note") else ""}</td>'
            f'<td><select data-k="{x["block_id"]}:{x["line"]}">{opts}</select></td></tr>' for n, x in enumerate(v, 1))
        differs = kinds(gs, v) >= 2
        cards.append(
            f'<section class="card" id="{esc(cid)}" data-id="{esc(cid)}"><h3>이미지 {i} <span class="sub">{esc(gs)} · 섹션 {sid} · 줄 {len(v)}개 · '
            f'{"굵기를 다르게 본 이미지" if differs else "굵기가 모두 같다고 본 이미지"}</span></h3><p class="meta">{summary}</p>'
            f'<div class="imgs"><img class="boxed" src="img/{gs}_{sid}_box.png" alt="라벨 표시" loading="lazy">'
            f'<img class="plain" src="img/{gs}_{sid}_plain.png" alt="원본" loading="lazy"></div>'
            f'<table class="ln"><tr><th>줄</th><th>에이전트 라벨</th><th>OCR 텍스트</th><th>틀리면 고르기</th></tr>{rows}</table>'
            f'<div class="row"><button type="button" class="done">확인 완료 — 고치지 않은 줄은 맞음</button>'
            f'<input type="text" class="note" placeholder="메모(선택)"></div></section>')
    legend = " ".join(f'<span class="chip" style="background:rgb{LABEL_COLOR[w]}">{esc(t)}</span>' for w, t in VERDICTS)
    key = f"style-weight-section-verify:{fp}"
    n_diff = sum(kinds(gs, v) >= 2 for (gs, _), v in cases)
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⑦ 글자 굵기 — 이미지 단위 확인</title><style>
body{{margin:0;background:#f6f6f4;color:#1d1d1f;font:14px/1.5 system-ui,-apple-system,"Malgun Gothic",sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid #ddd;padding:8px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
header h1{{font-size:16px;margin:0 8px 0 0}} header button,header select{{font:inherit;padding:4px 10px}}
main{{padding:12px 16px;max-width:1280px;margin:0 auto}} .intro{{background:#fff;border:1px solid #ddd;padding:10px 14px;margin-bottom:12px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:6px;padding:10px 14px;margin:0 0 16px}} .card.ok{{border-left:6px solid #3a9d5d}}
.card h3{{margin:0 0 4px;font-size:15px}} .sub{{font-weight:normal;color:#666;font-size:12.5px;margin-left:6px}} .meta{{margin:0 0 8px}}
.chip{{display:inline-block;color:#fff;border-radius:4px;padding:1px 8px;font-size:12.5px;font-weight:600}}
.imgs img{{display:block;max-width:100%;border:1px solid #ddd;cursor:zoom-in;background:#fff}} .imgs img.big{{max-width:none;cursor:zoom-out}}
.imgs img.plain{{display:none}} body.nobox .imgs img.plain{{display:block}} body.nobox .imgs img.boxed{{display:none}}
table.ln{{border-collapse:collapse;margin:8px 0;font-size:13px}} table.ln td,table.ln th{{border:1px solid #ddd;padding:2px 8px;text-align:left}}
table.ln th{{background:#f1f1ee}} table.ln select{{font:inherit}} table.ln tr.fix td{{background:#fff4d6}} table.ln i{{color:#777}}
.row{{display:flex;gap:10px;align-items:center;margin-top:6px}} .row button{{font:inherit;padding:5px 12px}} .note{{flex:1;padding:4px}}
</style></head><body>
<header><h1>⑦ 글자 굵기 — 이미지 단위 확인</h1><span id="cnt"></span>{legend}
<label><input type="checkbox" id="nobox"> 상자 숨기기</label>
<label>보기 <select id="flt"><option value="all">전체</option><option value="todo">미확인</option></select></label>
<button id="exp">TSV 내보내기</button></header>
<main><div class="intro"><b>확인</b> — 이미지마다 줄에 상자와 번호가 있고 <b>상자 색이 에이전트가 본 굵기</b>입니다. 한 이미지 안에서 줄끼리 견주어
색이 맞게 나뉘었는지 봐 주세요. 틀린 줄만 아래 표에서 실제 굵기를 고르고, 다 봤으면 <b>"확인 완료"</b>를 누릅니다(고치지 않은 줄은 맞음으로 기록).<br>
<img src="img/reference.png" alt="견본" style="max-width:100%;border:1px solid #ddd;margin:6px 0"><br>
라벨 = 위 Pretendard 다섯 굵기 견본 가운데 <b>가장 가까운 것</b>입니다(견본의 이름표 색 = 상자 색). 두 견본의 중간쯤이면 에이전트는 굵은 쪽으로 붙였습니다
(예: Regular 400쯤 → Medium, SemiBold 600쯤 → Bold). 한 줄 안에서 일부 단어만 굵기가 다르면 줄 안에 섞임, 너무 작거나 흐리거나 글자가 아니면 판단 불가.
글꼴이 달라도 획의 두께만 보고 크기 · 색은 판단에 넣지 않습니다. 글자 높이 {SEC_MIN_H}px 미만 줄은 상자가 없습니다.<br>
이미지 {len(cases)}개 · 줄 {sum(len(v) for _, v in cases)}개 — 굵기를 다르게 본 이미지 {n_diff}개가 먼저 나옵니다. 이미지를 누르면 원본 크기로 봅니다.
측정값은 이 페이지에 없습니다. 입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>
<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 입력 지문 {fp} · 저장 키 {esc(key)}</span></div>
{"".join(cards)}</main>
<script>
const K={json.dumps(key)},FP={json.dumps(fp)},CARDS={json.dumps(data)};
let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')||{{}}}}catch(e){{st={{}}}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}};paint()}}
function paint(){{let n=0;for(const c of CARDS){{const el=document.getElementById(c.id),s=st[c.id]||{{}};if(s.done)n++;el.classList.toggle('ok',!!s.done);
el.querySelector('.done').textContent=s.done?'확인함 — 다시 누르면 취소':'확인 완료 — 고치지 않은 줄은 맞음';
el.querySelectorAll('select').forEach(x=>x.closest('tr').classList.toggle('fix',!!x.value))}}
document.getElementById('cnt').textContent='확인 '+n+' / '+CARDS.length}}
for(const c of CARDS){{const el=document.getElementById(c.id),s=st[c.id]||{{}};
el.querySelectorAll('select').forEach(x=>{{x.value=(s.fix||{{}})[x.dataset.k]||'';x.onchange=()=>{{const t=st[c.id]=st[c.id]||{{}};t.fix=t.fix||{{}};
if(x.value)t.fix[x.dataset.k]=x.value;else delete t.fix[x.dataset.k];save()}}}});
el.querySelector('.done').onclick=()=>{{const t=st[c.id]=st[c.id]||{{}};t.done=!t.done;save()}};
const note=el.querySelector('.note');note.value=s.n||'';note.oninput=()=>{{(st[c.id]=st[c.id]||{{}}).n=note.value;save()}}}}
document.querySelectorAll('.imgs img').forEach(im=>im.onclick=()=>im.classList.toggle('big'));
document.getElementById('nobox').onchange=e=>document.body.classList.toggle('nobox',e.target.checked);
document.getElementById('flt').onchange=e=>{{for(const c of CARDS)document.getElementById(c.id).style.display=(e.target.value==='todo'&&(st[c.id]||{{}}).done)?'none':''}};
document.getElementById('exp').onclick=()=>{{let t='case_no\\tgs\\tblock_line\\tverdict\\tnote\\treview_fingerprint\\n',i=0;
for(const c of CARDS){{const s=st[c.id];if(!s||!s.done)continue;for(const l of c.lines){{i++;
t+=[i,c.gs,l.k,(s.fix||{{}})[l.k]||'ok',(s.n||'').replace(/[\\t\\n]/g,' '),FP].join('\\t')+'\\n'}}}}
const u=URL.createObjectURL(new Blob([t],{{type:'text/tab-separated-values'}}));const x=document.createElement('a');x.href=u;
x.download='style_weight_section_verify_'+FP+'.tsv';x.click()}};
paint();
</script></body></html>"""
    (a.out / "index.html").write_text(page, encoding="utf-8")
    print(f"이미지 단위 확인 페이지: {a.out / 'index.html'} · 이미지 {len(cases)}(굵기를 다르게 본 이미지 {n_diff}) · 줄 {sum(len(v) for _, v in cases)} · 저장 키 {key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--run", required=True, type=Path)
    m.add_argument("--gs", nargs="+", default=["GS-01", "GS-02"])
    m.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("page")
    p.add_argument("--lines", required=True, type=Path)
    p.add_argument("--run", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--only", type=Path, help="select가 만든 목록 — 그 줄만 카드로 만든다(--verify와 함께면 '다시 볼 줄' 표시만)")
    p.add_argument("--verify", type=Path, help="에이전트 라벨 TSV — 라벨을 보여 주고 맞음 / 틀림을 받는 확인 페이지를 만든다")
    p.add_argument("--font-zip", type=Path, help="공식 Pretendard-1.3.9.zip — 견본 굵기를 그린다")
    s = sub.add_parser("score")
    s.add_argument("--lines", required=True, type=Path)
    s.add_argument("--tsv", required=True, type=Path, nargs="+")
    s.add_argument("--verify", type=Path, help="확인 페이지에서 내보낸 TSV — 라벨에 얹는다")
    s.add_argument("--font-zip", type=Path, help="공식 Pretendard-1.3.9.zip — 견본 굵기의 측정값으로 다섯 단계 채점을 더한다")
    s.add_argument("--out", required=True, type=Path)
    e = sub.add_parser("select")
    e.add_argument("--lines", required=True, type=Path)
    e.add_argument("--tsv", required=True, type=Path)
    e.add_argument("--cut", required=True, type=float, help="후보 B의 관찰 경계값(확정값 아님)")
    e.add_argument("--out", required=True, type=Path)
    c = sub.add_parser("sections")
    c.add_argument("--lines", required=True, type=Path)
    c.add_argument("--run", required=True, type=Path)
    c.add_argument("--out", required=True, type=Path)
    g = sub.add_parser("section-page")
    g.add_argument("--lines", required=True, type=Path)
    g.add_argument("--run", required=True, type=Path)
    g.add_argument("--labels", required=True, type=Path, help="섹션 단위 에이전트 라벨 TSV(gs · block_line · weight · note)")
    g.add_argument("--out", required=True, type=Path)
    g.add_argument("--font-zip", type=Path, help="공식 Pretendard-1.3.9.zip — 견본 굵기를 그린다")
    a = ap.parse_args(argv)
    return {"measure": cmd_measure, "page": cmd_page, "score": cmd_score, "select": cmd_select, "sections": cmd_sections,
            "section-page": cmd_section_page}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
