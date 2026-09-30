"""⑤ 브랜드 로고 제외 실측 결과를 원본 이미지와 대조하는 사용자 검수 페이지 생성(2026-09-30).

    python docs/ai-experiments/tools/logo_review_page.py --run pipeline/out/logo-run-v1_<date> \
        --input pipeline/samples/local/logo-input-v1 --brand-meta pipeline/samples/local/brand-meta-v1 \
        [--ai-review docs/ai-experiments/runs/<...>/ai_review.tsv] --out pipeline/out/logo-run-v1_<date>/review

출력: <out>/index.html · <out>/img/(블록 확대 · 섹션 문맥 · 섹션 오버레이). 실측 산출물(logo/ · logo_debug/)은 읽기만 한다.
- 위쪽 "우선 검수": 로고 true 전체 → 자동 후보(아래) → AI 사전 검토에 올린 블록. 블록마다 확대 이미지 · 섹션 문맥 · 원문 · 정규화 값 ·
  브랜드 비교값 · ④ · ⑤ 판정과 근거 · 자동 후보 이유 · AI 의견.
- 아래 "전체 블록": 원본 · 섹션별로 접어 둔 948블록 표와 섹션 오버레이(⑤ true 빨강 · false 초록 · 빈 텍스트 회색 · ④ 라벨 생략 파랑 점선).
- 사용자 검수(분류 · 기대 is_brand_logo · 메모)는 브라우저에만 저장(localStorage)되고 "TSV 내보내기"로 받는다. **미리 선택된 값은 없다**(AI 의견을 기본값으로 채우지 않음).
자동 후보는 검수 대상 선정용이다(부분 문자열 · 편집 거리 · 기호 제거 비교) — ⑤ 판정 규칙(완전 일치)을 바꾸지 않는다.
이 페이지는 정답이 아니며 AI 의견은 사람 판단이 아니다.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import jsonio  # noqa: E402
from pipeline.stages.logo import normalize  # noqa: E402
from pipeline.types import LabelResult, LogoResult  # noqa: E402

CLASSES = [
    ("ok", "맞음"),
    ("logo_miss", "로고 미탐(로고인데 false)"),
    ("logo_false_positive", "로고 오탐(로고가 아닌데 true)"),
    ("upstream", "상류 영향(OCR · 병합 · ④)"),
    ("brand_data", "브랜드 자료 확인 필요"),
    ("unsure", "판단 불가"),
]
EXPECTED = [("true", "true"), ("false", "false"), ("null", "null(④ 라벨 · 비교 생략)"), ("unknown", "모름")]
COLORS = {"exact_match": (220, 40, 40), "no_match": (40, 160, 60), "empty_text": (150, 150, 150), "product_label": (40, 90, 220)}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def candidate_reasons(norm: str, own: dict[str, str], others: set[str]) -> list[str]:
    """검수 대상 선정용 신호(판정 규칙 아님). own = {정규화 브랜드값: 원문}."""
    out = []
    if not norm:
        return out
    for x, raw in own.items():
        if x in norm and norm != x:
            out.append(f"브랜드명 포함({raw})")
        nosym = "".join(c for c in norm if not unicodedata.category(c).startswith("S"))
        if nosym != norm and nosym == x:
            out.append(f"S* 기호 때문에 불일치({raw})")
        if norm != x and len(norm) >= 3 and lev(norm, x) <= max(1, len(x) // 3):
            out.append(f"표기 변형 — 편집 거리 {lev(norm, x)}({raw})")
    cross = sorted(o for o in others if o and o in norm)
    if cross:
        out.append("다른 상품 브랜드명 포함 " + ",".join(cross))
    return out


def load_ai(path: Path | None) -> dict[tuple[str, str, str], dict[str, str]]:
    if path is None or not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return {(r["source"], r["section"], r["block"]): r for r in csv.DictReader(f, delimiter="\t")}


def crop_images(img: Image.Image, bbox, color, out_crop: Path, out_ctx: Path) -> None:
    """블록 확대(여백 포함, 작으면 최대 3배 확대)와 섹션 문맥(폭 480, 블록 굵은 테두리)."""
    W, H = img.size
    pad = max(24, bbox.h)
    x0, y0, x1, y1 = max(0, bbox.x - pad), max(0, bbox.y - pad), min(W, bbox.x2 + pad), min(H, bbox.y2 + pad)
    crop = img.crop((x0, y0, x1, y1)).copy()
    ImageDraw.Draw(crop).rectangle([bbox.x - x0, bbox.y - y0, bbox.x2 - x0, bbox.y2 - y0], outline=color, width=3)
    scale = min(3.0, max(1.0, 420 / max(1, crop.width)))
    if scale > 1:
        crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
    crop.save(out_crop)
    s = 480 / W
    ctx = img.resize((480, max(1, round(H * s))), Image.LANCZOS)
    ImageDraw.Draw(ctx).rectangle([bbox.x * s - 3, bbox.y * s - 3, bbox.x2 * s + 3, bbox.y2 * s + 3], outline=color, width=4)
    ctx.save(out_ctx)


def section_overlay(img: Image.Image, rows: list[dict], blocks: dict, out: Path) -> None:
    W, H = img.size
    s = 420 / W
    ov = img.resize((420, max(1, round(H * s))), Image.LANCZOS).convert("RGB")
    dr = ImageDraw.Draw(ov)
    for r in rows:
        b = blocks[r["block_key"]].bbox
        box = [b.x * s, b.y * s, b.x2 * s, b.y2 * s]
        dr.rectangle(box, outline=COLORS[r["basis"]], width=2)
        dr.text((box[0] + 2, box[1] + 1), str(r["block_order"]), fill=COLORS[r["basis"]])
    ov.save(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--brand-meta", required=True)
    ap.add_argument("--ai-review", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    run, inp, bdir, out = Path(a.run), Path(a.input), Path(a.brand_meta), Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    brand = json.loads((bdir / "brand_metadata.json").read_text(encoding="utf-8"))
    group_of = {i: p for p in brand["products"] for i in p["images"]}
    all_norm = {normalize(p[k]) for p in brand["products"] for k in ("name_ko", "name_en")}
    manifest = json.loads((inp / "MANIFEST.json").read_text(encoding="utf-8"))
    ai = load_ai(Path(a.ai_review) if a.ai_review else None)

    prio_cards: list[tuple[int, str, str]] = []  # (순위, 정렬 키, html)
    full: list[str] = []
    counts = {"blocks": 0, "true": 0, "auto": 0, "ai": 0}
    for img_entry in manifest["images"]:
        iid = img_entry["image"]
        p = group_of[iid]
        own = {normalize(p["name_ko"]): p["name_ko"], normalize(p["name_en"]): p["name_en"]}
        others = all_norm - set(own)
        split = {s.section_key: s for s in jsonio.load_split(inp / iid / "split.json").sections}
        sec_html = []
        for s in img_entry["sections"]:
            key = s["section_key"]
            merged = jsonio.load_merge(inp / iid / "merge" / f"{key}.json")
            blocks = {b.block_key: b for b in merged.blocks}
            lab = {d.block_key: d.is_product_label for d in jsonio.load_model(inp / iid / "label" / f"{key}.json", LabelResult).labels}
            res = jsonio.load_model(run / iid / "logo" / f"{key}.json", LogoResult)
            dbg = json.loads((run / iid / "logo_debug" / f"{key}.json").read_text(encoding="utf-8"))
            rows = dbg["decisions"]
            assert [r["block_key"] for r in rows] == [d.block_key for d in res.decisions]
            img = Image.open(split[key].image_path).convert("RGB")
            ov_rel = f"img/{iid}__{key}__overlay.png"
            section_overlay(img, rows, blocks, out / ov_rel)
            trs = []
            for r in rows:
                counts["blocks"] += 1
                b = blocks[r["block_key"]]
                norm = normalize(b.source_ko)
                reasons = candidate_reasons(norm, own, others)
                ai_row = ai.get((iid, key, b.block_key), {})
                bid = f"{iid}|{key}|{b.block_key}"
                verdict = {"exact_match": "true", "no_match": "false", "empty_text": "false", "product_label": "null"}[r["basis"]]
                opts = "".join(f'<option value="{c}">{esc(t)}</option>' for c, t in [("", "—")] + CLASSES)
                exp = "".join(f'<option value="{c}">{esc(t)}</option>' for c, t in [("", "—")] + EXPECTED)
                review = (f'<select data-id="{esc(bid)}" data-f="c">{opts}</select> 기대 <select data-id="{esc(bid)}" data-f="e">{exp}</select> '
                          f'<input data-id="{esc(bid)}" data-f="n" placeholder="메모">')
                trs.append(f'<tr><td>{r["block_order"]}</td><td>{esc(b.block_key)}</td><td>{esc(b.role)}</td><td class="t">{esc(b.source_ko)}</td>'
                           f'<td class="t">{esc(r["normalized"])}</td><td>{"true" if lab[b.block_key] else "false"}</td>'
                           f'<td class="v {r["basis"]}">{verdict} · {r["basis"]}</td><td class="ai">{esc("; ".join(reasons))}'
                           f'{"<br>" if reasons and ai_row else ""}{esc(ai_row.get("ai_note", ""))}</td><td>{review.replace("data-id", "data-full")}</td></tr>')
                rank = 0 if r["basis"] == "exact_match" else (1 if reasons and not lab[b.block_key] else (2 if ai_row else (3 if reasons else None)))
                if r["basis"] == "exact_match":
                    counts["true"] += 1
                if reasons:
                    counts["auto"] += 1
                if ai_row:
                    counts["ai"] += 1
                if rank is None:
                    continue
                crop_rel, ctx_rel = f"img/{iid}__{key}__{b.block_key}.png", f"img/{iid}__{key}__{b.block_key}__ctx.png"
                crop_images(img, b.bbox, COLORS[r["basis"]], out / crop_rel, out / ctx_rel)
                ai_html = (f'<p><b>AI 사전 검토</b> [{esc(ai_row.get("category"))}] {esc(ai_row.get("ai_note"))}</p>' if ai_row
                           else "<p class='mut'>AI 사전 검토 없음</p>")
                prio_cards.append((rank, f"{iid}/{key}/{r['block_order']:04d}", f"""
<section class="card r{rank}" id="{esc(bid)}"><h3>{esc(iid)} / {esc(key)} / {esc(b.block_key)} — ⑤ <span class="v {r['basis']}">{verdict} · {r['basis']}</span></h3>
<div class="row"><div class="crop"><a href="{crop_rel}" target="_blank"><img src="{crop_rel}"></a></div>
<div class="ctx"><a href="{ctx_rel}" target="_blank"><img src="{ctx_rel}" loading="lazy"></a></div>
<div class="info"><p>상품 {esc(p.get('product_name'))} · 그룹 {esc(p['image_group'])}<br>브랜드 비교값: <code>{esc(p['name_ko'])}</code> → <code>{esc(normalize(p['name_ko']))}</code> ·
<code>{esc(p['name_en'])}</code> → <code>{esc(normalize(p['name_en']))}</code></p>
<p>원문(역할 {esc(b.role)}): <span class="t">{esc(b.source_ko)}</span><br>정규화: <code>{esc(r['normalized'])}</code>
{'(④ 라벨이라 비교 생략 — 참고용 정규화: <code>' + esc(norm) + '</code>)' if r['normalized'] is None else ''}</p>
<p>④ is_product_label = <b>{"true" if lab[b.block_key] else "false"}</b> · ⑤ is_brand_logo = <b>{verdict}</b>(근거 {r['basis']}{', 일치 ' + ','.join(r['matched_names']) if r['matched_names'] else ''})</p>
<p>자동 후보 이유: {esc('; '.join(reasons) or '없음(로고 true)')}</p>{ai_html}
<p>{review}</p></div></div></section>"""))
            sec_html.append(f'<details><summary>{esc(key)} — 블록 {len(rows)} · true {sum(1 for r in rows if r["basis"] == "exact_match")}</summary>'
                            f'<div class="row"><div class="ovl"><a href="{ov_rel}" target="_blank"><img src="{ov_rel}" loading="lazy"></a></div>'
                            f'<table><thead><tr><th>#</th><th>블록</th><th>역할</th><th>원문</th><th>정규화</th><th>④</th><th>⑤</th><th>후보 이유 · AI</th>'
                            f'<th>사용자 검수</th></tr></thead><tbody>{"".join(trs)}</tbody></table></div></details>')
        full.append(f'<h3>{esc(iid)} — {esc(p["image_group"])} {esc(p["name_ko"])} / {esc(p["name_en"])}</h3>{"".join(sec_html)}')
    prio_cards.sort()
    rank_titles = {0: "로고 true 전체", 1: "자동 후보 — ④ false인데 브랜드명 포함 · 표기 변형 · 기호 차이", 2: "AI 사전 검토에 올린 블록(자동 후보 밖)",
                   3: "참고 — ④ 라벨이라 비교 생략된 브랜드 표기"}
    prio_html, last = [], None
    for rank, _, h in prio_cards:
        if rank != last:
            prio_html.append(f"<h2>{rank_titles[rank]} ({sum(1 for c in prio_cards if c[0] == rank)})</h2>")
            last = rank
        prio_html.append(h)
    brands = " · ".join(f"{esc(p['image_group'])} {esc(p['name_ko'])}/{esc(p['name_en'])}(원본 {len(p['images'])})" for p in brand["products"])
    legend = " · ".join(f"<b>{esc(c)}</b> {esc(t)}" for c, t in CLASSES)
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⑤ 로고 판정 검수</title><style>
:root{{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd;--code:#f3f3f3}}
@media (prefers-color-scheme: dark){{:root{{--bg:#16181c;--fg:#eee;--mut:#aaa;--line:#333;--code:#23262b}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0 16px 40px}}
header{{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:2}}
.card{{border:1px solid var(--line);border-radius:8px;margin:12px 0;padding:10px}}.card.r0{{border-color:#dc2828}}.card.r1{{border-color:#f09614}}
.row{{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}}.crop img{{max-width:min(560px,100%);border:1px solid var(--line)}}
.ctx img,.ovl img{{width:240px;border:1px solid var(--line)}}.info{{flex:1 1 320px;min-width:0}}
code{{background:var(--code);padding:0 3px;border-radius:3px;word-break:break-all}}.t{{white-space:pre-wrap;word-break:break-all}}
.mut,td.ai{{color:var(--mut)}}.v.exact_match{{color:#dc2828;font-weight:600}}.v.no_match{{color:#28a03c}}.v.empty_text{{color:#999}}.v.product_label{{color:#3c64dc}}
table{{flex:1 1 600px;border-collapse:collapse;font-size:12px}}td,th{{border-bottom:1px solid var(--line);padding:3px 4px;vertical-align:top;text-align:left}}
td.t{{max-width:280px}}td.ai{{max-width:200px}}input{{width:120px}}details{{margin:4px 0 4px 8px}}summary{{cursor:pointer}}
</style></head><body><header><b>⑤ 브랜드 로고 제외 검수</b> — {esc(run.name)} · 블록 {counts['blocks']} · 로고 true {counts['true']} · 자동 후보 {counts['auto']} · AI 검토 {counts['ai']}
<button id="exp">TSV 내보내기</button> <span id="cnt"></span><br><small>브랜드: {brands}. 판정 규칙: 정규화(NFKC → 소문자 → 공백 · 문장부호 제거, 기호 유지) 후 한글명 · 영문명과 <b>완전 일치</b>.
정답 · 사용자 승인이 아니다. 자동 후보 이유는 검수 대상 선정용이고 AI 의견은 사람 판단이 아니다. 분류: {legend}</small></header>
<h1>우선 검수</h1>{"".join(prio_html)}<h1>전체 블록</h1>{"".join(full)}
<script>
const K='logo-review-v1';let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}};document.getElementById('cnt').textContent='검수 '+Object.values(st).filter(v=>v.c||v.e).length+'개'}}
function sync(id,f,v){{document.querySelectorAll('[data-id="'+id+'"][data-f="'+f+'"],[data-full="'+id+'"][data-f="'+f+'"]').forEach(x=>{{if(x.value!==v)x.value=v}})}}
document.querySelectorAll('[data-id],[data-full]').forEach(x=>{{const id=x.dataset.id||x.dataset.full,f=x.dataset.f;if(st[id]&&st[id][f])x.value=st[id][f];
x.addEventListener(x.tagName==='INPUT'?'input':'change',()=>{{st[id]=Object.assign(st[id]||{{}},{{[f]:x.value}});sync(id,f,x.value);save()}})}});
document.getElementById('exp').onclick=()=>{{let t='source\\tsection\\tblock\\tclass\\texpected_is_brand_logo\\tnote\\n';for(const[k,v]of Object.entries(st)){{if(!v.c&&!v.e&&!v.n)continue;const[a,b,c]=k.split('|');t+=[a,b,c,v.c||'',v.e||'',(v.n||'').replace(/[\\t\\n]/g,' ')].join('\\t')+'\\n'}}
const u=URL.createObjectURL(new Blob([t],{{type:'text/tab-separated-values'}}));const e=document.createElement('a');e.href=u;e.download='logo_user_review.tsv';e.click()}};save();
</script></body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    print(f"-> {out / 'index.html'} (블록 {counts['blocks']} · 우선 {len(prio_cards)} · 로고 true {counts['true']} · 자동 후보 {counts['auto']} · AI {counts['ai']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
