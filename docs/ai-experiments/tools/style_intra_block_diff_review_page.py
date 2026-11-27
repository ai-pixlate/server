"""⑦ 블록 안 스타일 차이 — 사람 확인용 표본 페이지(2026-11-26). 개발용, 운영 경로 아님.

    python docs/ai-experiments/tools/style_intra_block_diff_review_page.py \
        --blocks docs/ai-experiments/runs/2026-11-26_07-style_intra-block-diff/blocks.json \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --out docs/ai-experiments/runs/2026-11-26_07-style_intra-block-diff/review

입력(읽기만): `style_intra_block_diff.py`의 `blocks.json`과 베이스라인 회차의 `objects/` · `object-index.json`(원본 섹션 이미지).
출력: `<out>/index.html` · `<out>/img/`(PNG — 원본 crop과 영역 번호를 그린 crop) · `<out>/cases.json`(고른 사례). --out이 있으면 거부한다.

표본: 유형별로 고른다. 유형은 **가장 느슨한 기준값**(blocks.json의 첫 크기 · 글자색 기준)에서 크기 · 글자색을 합친 위치 유형이다 —
그 기준에서도 "차이 없음"인 블록이 차이 없음 표본이고, 차이 표본에는 작은 차이부터 큰 차이까지 섞인다(기준값을 하나로 정하려는 것이 아니다).
유형 안에서는 차이 크기(max(크기 비율 / 크기 기준, 글자색 거리 / 색 기준)) 순위에서 같은 간격으로 뽑는다(무작위 아님, 결정적).
개수는 QUOTA — 유형의 블록이 그보다 적으면 전부. 카드 순서는 (GS, id)의 SHA-256으로 섞는다.

표시: 원본 crop · 영역 번호를 그린 crop(줄마다 색이 다르다) · 영역별 측정값 표(크기 · 글자색 · 배경색 견본 · Otsu 진단 · OCR 점수)는 항상 보인다.
도구가 붙인 유형 · 기준값별 위치 · 측정 오류 의심 표시는 "참고 보기"를 켤 때만 보인다(기본 꺼짐) — 도구 판단이지 사람 판단이 아니다.
질문 둘: (1) 원본에서 이 블록 글자의 스타일(크기 · 색 · 굵기)이 실제로 어디서 다른가 (2) 표의 측정값이 원본과 맞는가.
입력은 브라우저에만 저장(localStorage)되고 "TSV 내보내기"로 받는다. 입력 문자열은 모두 HTML 이스케이프한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from style_intra_block_diff import combine  # noqa: E402
from typeset_align_review_page import render_page  # noqa: E402
from typeset_overflow_review_page import esc  # noqa: E402

QUOTA = {"between": 12, "within": 12, "both": 6, "pair_only": 3, "none": 10}
TYPE_KO = {"none": "차이 없음", "between": "줄 사이", "within": "줄 안", "both": "둘 다", "pair_only": "영역 쌍만"}
V_STYLE = (("between", "줄 사이만 다름"), ("within", "줄 안에서만 다름"), ("both", "둘 다"), ("none", "차이 없음"), ("unsure", "판단 불가"))
V_MEASURE = (("ok", "측정값이 원본과 맞음"), ("false_diff", "가짜 차이(측정 오류)"), ("missed", "실제 차이를 못 잡음"), ("unsure", "판단 불가"))
LINE_COLORS = ((230, 30, 30), (20, 110, 230), (20, 150, 60), (200, 120, 0), (150, 40, 190), (0, 150, 160))
STYLE = ("<style>table.rg{border-collapse:collapse;font-size:12.5px;margin:6px 0}table.rg td,table.rg th{border:1px solid #ddd;padding:2px 7px;"
         "text-align:left;white-space:nowrap}table.rg th{background:#f1f1ee}.sw{display:inline-block;width:26px;height:13px;border:1px solid #888;"
         "vertical-align:-2px;margin-right:4px}.ln{display:inline-block;width:10px;height:10px;margin-right:4px}</style>")


def _h(b: dict) -> str:
    return hashlib.sha256(f"{b['gs']}:{b['id']}".encode()).hexdigest()


def keys(blocks: list[dict]) -> tuple[str, str]:
    """가장 느슨한 기준값(blocks.json에 기록된 첫 크기 · 글자색 기준)."""
    return next(iter(blocks[0]["loc"]["size"])), next(iter(blocks[0]["loc"]["font_color"]))


def score(b: dict, ks: str, kc: str) -> float:
    return max((b["metrics"]["size"]["max"] or 0) / float(ks), (b["metrics"]["font_color"]["max"] or 0) / float(kc))


def pick(blocks: list[dict]) -> list[dict]:
    ks, kc = keys(blocks)
    by: dict[str, list[dict]] = {}
    for b in blocks:
        by.setdefault(combine(b["loc"]["size"][ks], b["loc"]["font_color"][kc]), []).append(b)
    out = []
    for typ, q in QUOTA.items():
        rows = sorted(by.get(typ, []), key=lambda b: (score(b, ks, kc), _h(b)))
        if len(rows) <= q:
            idx = range(len(rows))
        else:
            idx = sorted({round(i * (len(rows) - 1) / (q - 1)) for i in range(q)})
        out += [dict(rows[i], sample_type=typ, sample_rank=f"{i + 1}/{len(rows)}") for i in idx]
    return sorted(out, key=_h)


def crops(im: Image.Image, b: dict) -> tuple[Image.Image, Image.Image, int]:
    """블록 주변 crop(원본)과 영역 상자 · 번호를 그린 crop. 작은 글자는 정수배로 키운다(최근접 — 색을 섞지 않는다)."""
    x, y, w, h = (b["bbox"][k] for k in "xywh")
    pad = max(40, h // 2)
    x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(im.width, x + w + pad), min(im.height, y + h + pad)
    min_h = min(r["bbox"]["h"] for r in b["regions"])
    s = min(4, max(1, math.ceil(40 / min_h)), max(1, 1600 // (x1 - x0)))
    z = im.crop((x0, y0, x1, y1))
    if s > 1:
        z = z.resize((z.width * s, z.height * s), Image.NEAREST)
    boxed = z.copy()
    d = ImageDraw.Draw(boxed)
    for n, r in enumerate(b["regions"], 1):
        c = LINE_COLORS[r["line_index"] % len(LINE_COLORS)]
        rb = r["bbox"]
        px0, py0 = (rb["x"] - x0) * s, (rb["y"] - y0) * s
        d.rectangle((px0 - 1, py0 - 1, px0 + rb["w"] * s, py0 + rb["h"] * s), outline=c, width=1)
        d.rectangle((px0 - 1, py0 - 13, px0 + 7 * len(str(n)) + 3, py0 - 1), fill=c)
        d.text((px0 + 1, py0 - 13), str(n), fill=(255, 255, 255))
    return z, boxed, s


def swatch(c: str | None) -> str:
    return f'<span class="sw" style="background:{esc(c)}"></span>{esc(c)}' if c else "null"


def region_table(b: dict) -> str:
    rows = []
    for n, r in enumerate(b["regions"], 1):
        o = r["otsu"] or {}
        c = "rgb(%d,%d,%d)" % LINE_COLORS[r["line_index"] % len(LINE_COLORS)]
        fg = f'{100 * r["fg_ratio"]:.0f}%' if r["fg_ratio"] is not None else "—"
        rows.append(f'<tr><td><span class="ln" style="background:{c}"></span>{n}</td><td>{r["line_index"] + 1}</td><td>{esc(r["text"])}</td>'
                    f'<td>{r["bbox"]["w"]}×{r["bbox"]["h"]}</td><td><b>{r["est_font_px"]:g}</b></td><td>{swatch(r["font_color"])}</td>'
                    f'<td>{swatch(r["bg_color"])}</td><td>{esc(o.get("threshold", "—"))} · {fg} · {esc(o.get("border_dark", "—"))}/{esc(o.get("border_light", "—"))}</td>'
                    f'<td>{r["score"]:.3f}</td></tr>')
    return ('<table class="rg"><tr><th>영역</th><th>줄</th><th>OCR 텍스트</th><th>bbox 폭×높이</th><th>크기 px</th><th>글자색</th><th>배경색</th>'
            '<th>Otsu 임계 · 글자 쪽 픽셀 비율 · 외곽 접촉(어두운/밝은)</th><th>OCR 점수</th></tr>' + "".join(rows) + "</table>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--blocks", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    raw = a.blocks.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    blocks = json.loads(raw.decode("utf-8"))
    ks, kc = keys(blocks)
    cases = pick(blocks)
    idx = json.loads((a.run / "object-index.json").read_text(encoding="utf-8"))
    opened: dict[str, Image.Image] = {}

    def obj(key: str) -> Image.Image:
        if key not in opened:
            opened[key] = Image.open(a.run / "objects" / idx[key]["sha256"]).convert("RGB")
        return opened[key]

    a.out.mkdir(parents=True)
    (a.out / "img").mkdir()
    cards, ids = [], []
    for n, b in enumerate(cases, 1):
        cid = f"{b['gs']}|{b['id']}"
        ids.append(cid)
        orig, boxed, s = crops(obj(b["image_key"]), b)
        names = {}
        for kind, im in (("orig", orig), ("box", boxed)):
            names[kind] = f"img/{b['gs']}_{b['id']}_{kind}.png"
            im.save(a.out / names[kind])
        m, st = b["metrics"], b["block_style"]
        sz, fc, bg = m["size"], m["font_color"], m["bg_color"]
        locs = " · ".join(f'{it} {" / ".join(f"{k}: {TYPE_KO[v]}" for k, v in b["loc"][it].items())}' for it in ("size", "font_color", "bg_color"))
        sus = " · ".join(f'{it} {k}: {", ".join(v)}' for it in ("size", "font_color", "bg_color") for k, v in b["suspect"][it].items() if v) or "없음"
        ref = (f'<div class="ref"><b>참고 · 도구 유형(크기 {esc(ks)} · 글자색 {esc(kc)} 기준)</b> {esc(TYPE_KO[b["sample_type"]])} '
               f'(유형 안 차이 순위 {esc(b["sample_rank"])})<br><b>기준값별 위치</b> {esc(locs)}<br><b>측정 오류 의심 표시(휴리스틱)</b> {esc(sus)}</div>')
        radios = {f: "".join(f'<label><input type="radio" name="{f}:{esc(cid)}" value="{v}"> {esc(t)}</label>' for v, t in vs)
                  for f, vs in (("v", V_STYLE), ("m", V_MEASURE))}
        cards.append(
            f'<section class="card" id="{esc(cid)}" data-id="{esc(cid)}">'
            f'<h3>사례 {n} <span class="sub">{esc(b["gs"])} · 블록 {esc(b["id"])} · 섹션 {esc(b["section_id"])} · {esc(b["role"])}</span></h3>'
            f'<p class="meta">줄 {b["n_lines"]} · 측정 영역 {b["n_regions"]} · 블록 {b["bbox"]["w"]}×{b["bbox"]["h"]}px · 그림 배율 ×{s}</p>'
            f'<div class="txt"><div><b>원문</b> {esc(b["source_ko"])}</div></div>'
            f'<div class="imgs"><figure><figcaption>원본</figcaption><img src="{names["orig"]}" alt="원본" loading="lazy"></figure>'
            f'<figure><figcaption>영역 번호(줄마다 색이 다름)</figcaption><img src="{names["box"]}" alt="영역" loading="lazy"></figure></div>'
            f'<p class="meta"><b>⑦ 블록 대표값(조판에 쓰인 값)</b> 크기 {st["est_font_px"]:g}px · 글자색 {swatch(st["font_color"])} · 배경색 {swatch(st["bg_color"])}<br>'
            f'<b>영역 간 차이</b> 크기 최대/최소 {sz["max"]:.2f}배({sz["min"]:g} ~ {sz["max_px"]:g}px, 대표값 대비 최대 편차 {100 * sz["rep_dev"]:.0f}%) · '
            f'글자색 최대 거리 {fc["max"]:.0f}(ΔE {fc["max_delta_e"]:.0f}) · 배경색 최대 거리 {bg["max"]:.0f}(ΔE {bg["max_delta_e"]:.0f})</p>'
            f'{region_table(b)}{ref}'
            f'<div class="form"><div class="row"><b>스타일</b>{radios["v"]}</div><div class="row"><b>측정</b>{radios["m"]}'
            f'<button type="button" class="clear">지우기</button></div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="예: 2번 영역만 굵고 주황색 · 3번 크기는 숫자라 작게 잡힘"></div></div></section>')
    (a.out / "cases.json").write_text(json.dumps(
        [{"case_no": n, "gs": b["gs"], "id": b["id"], "role": b["role"], "sample_type": b["sample_type"], "sample_rank": b["sample_rank"]}
         for n, b in enumerate(cases, 1)], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    key = f"style-intra-block-diff-review:{fp}"
    intro = (f'{STYLE}<b>질문 1 · 스타일</b> — 원본에서 이 블록(영역 번호가 붙은 글자들)의 <b>글자 스타일(크기 · 색 · 굵기)이 실제로 어디서 다른가요?</b> '
             f'줄마다 다르면 "줄 사이만", 같은 줄 안의 일부 단어만 다르면 "줄 안에서만", 둘 다면 "둘 다"입니다. 굵기만 달라도 다른 것으로 봅니다(메모에 적어 주세요).<br>'
             f'<b>질문 2 · 측정</b> — 아래 표의 영역별 크기 · 글자색이 원본과 맞나요? 원본은 같은데 표의 값만 다르면 "가짜 차이", '
             f'원본은 다른데 표의 값이 같거나 영역이 나뉘지 않아 드러나지 않으면 "실제 차이를 못 잡음"입니다.<br>'
             f'크기 px는 영역 bbox 높이 × 0.9라서 한글 · 영문 소문자 · 숫자 · 기호처럼 글자 종류가 다르면 같은 글자 크기도 다르게 잡힐 수 있습니다. '
             f'이미지를 누르면 원본 픽셀 크기로 봅니다.<br>'
             f'사례 {len(cases)}개 — 유형별로 고른 표본이고 순서는 섞여 있습니다. "참고 보기"는 도구가 붙인 유형 · 의심 표시이며 사람 판단이 아닙니다 — '
             f'먼저 끈 채로 판단하기를 권합니다. 입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>'
             f'<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 입력 지문 {fp} · 저장 키 {esc(key)}</span>')
    page = render_page("⑦ 블록 안 스타일 차이 — 표본 확인", intro, cards, ids, key, fp, V_STYLE,
                       "case_no\\tgs\\tblock_id\\tstyle_diff\\tmeasure\\tnote\\treview_fingerprint",
                       f"style_intra_block_diff_review_{fp}.tsv", fields=("v", "m"))
    (a.out / "index.html").write_text(page, encoding="utf-8")
    c: dict[str, int] = {}
    for b in cases:
        c[b["sample_type"]] = c.get(b["sample_type"], 0) + 1
    print(f"검수 페이지: {a.out / 'index.html'} · 사례 {len(cases)} {c} · 저장 키 {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
