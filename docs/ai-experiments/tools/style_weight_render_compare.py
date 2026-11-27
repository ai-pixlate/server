"""⑦ 글자 굵기 — 번역문 전후 비교 렌더(2026-11-26). 개발용, 운영 경로 아님. 운영 코드 · config · 저장된 결과는 바꾸지 않는다.

    python docs/ai-experiments/tools/style_weight_render_compare.py \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --lines docs/ai-experiments/runs/2026-11-26_07-style_weight-measure/lines.json \
        --labels docs/ai-experiments/2026-11-26_07-style_weight-measure_agent-labels-by-section.tsv \
        --font-zip <공식 Pretendard-1.3.9.zip> \
        --out docs/ai-experiments/runs/2026-11-26_07-style_weight-measure/render_compare

질문: 측정한 굵기로 번역문을 그리면 지금(역할 설정 굵기)보다 원본의 굵기 느낌에 가까워지는가. 눈으로 보는 자료를 만든다.

입력(읽기만): 베이스라인 회차의 `evidence/<GS>.json`(v1.0.1이 낸 번역문 `trans_1` · ⑦ 측정 스타일 · 배치 영역) · `objects/`(원본 섹션 이미지와 ⑥ 인페인트
배경), `style_weight_probe.py measure`의 `lines.json`(줄별 후보 B), 섹션 단위 에이전트 라벨 TSV, 공식 Pretendard zip(아홉 굵기 OTF — 저장소에 없다).
대상 섹션은 `style_weight_probe.section_cases`의 섹션(섹션 단위 라벨 표본과 같다).

섹션마다 인페인트 배경 위에 번역문을 세 번 그린다. 글자색 · 크기 · 정렬 · 배치 영역 · 번역문은 같고 **굵기만 다르다**:
- now(지금): 역할 설정 굵기 — title · price SemiBold 600, 나머지 Regular 400(`style_defaults.json`).
- measured(측정 굵기): 블록의 줄별 후보 B 중앙값에 가장 가까운(로그 거리) Pretendard 견본 굵기 — Thin 100 · Light 300 · Medium 500 · Bold 700 · Black 900
  (견본 값은 `style_weight_probe.ref_values`). 블록에 줄 측정값이 없으면 지금 굵기 그대로.
- label(라벨 굵기): 블록에 속한 줄의 에이전트 라벨 가운데 굵기 라벨의 가운데 값(짝수면 굵은 쪽). 굵기 라벨이 없으면(작은 글자 · 섞임 · 판단 불가)
  지금 굵기 그대로. **에이전트 라벨이며 사람 판정이 아니다.**
굵기는 블록당 하나다 — 번역 · 조판이 블록 단위이기 때문이다. 한 블록 안에서 줄마다 굵기가 다르거나 일부 단어만 굵은 경우는 드러나지 않는다.

그리기: 배치와 줄바꿈은 운영 Pillow 조판 `app.typeset.render`와 같은 규칙으로 한다(`resolve_style` · `_wrap` · `LINE_SPACING`을 그대로 부르고 그리기
루프만 이 도구에 옮겨 글꼴을 바꿨다). 배치 영역 · 셀러 배율은 `typeset_overflow_probe.applied_layout`. **운영 렌더러는 서버 HTML 렌더(2026-11-19~)라
이 그림은 운영 결과와 줄바꿈 · 자간이 다를 수 있다** — 굵기 차이를 보는 용도다. 굵기가 바뀌면 글자 폭이 달라져 줄바꿈 · 넘침도 달라진다.

출력: `<out>/index.html` · `<out>/img/`(섹션별 원본 · now · measured · label PNG, 같은 세로 범위로 자름) · `<out>/blocks.json`(블록별 굵기).
페이지는 이미지마다 두 그림을 나란히 보여 준다 — 처음에는 왼쪽 원본 · 오른쪽 측정 굵기이고, 머리에서 칸마다 보여 줄 그림을 고른다
(왼쪽 Q W E R · 오른쪽 숫자 키 1 2 3 4). --out이 있으면 거부한다. 입력은 브라우저에만 저장되고 TSV로 내보낸다.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import math
import statistics
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from style_weight_probe import LABEL_COLOR, ORDER, WEIGHTS, load_fonts, read_tsv, ref_values, section_cases  # noqa: E402

FACES9 = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium", 600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black"}
VIEWS = (("orig", "원본"), ("now", "지금(역할 굵기)"), ("measured", "측정 굵기"), ("label", "에이전트 라벨 굵기"))
Q_MEASURED = (("measured", "측정 굵기가 원본에 더 가깝다"), ("now", "지금이 더 가깝다"), ("same", "비슷하다"), ("neither", "둘 다 안 맞다"))
Q_LABEL = (("better", "지금보다 낫다"), ("same", "비슷하다"), ("worse", "지금보다 못하다"))


def main(argv: list[str] | None = None) -> int:
    from typeset_overflow_probe import applied_layout
    from typeset_overflow_review_page import esc

    from app import typeset as t
    from pipeline.handoff import style_defaults as sd

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, type=Path)
    ap.add_argument("--lines", required=True, type=Path)
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--font-zip", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1

    raw = a.lines.read_bytes()
    fp = hashlib.sha256(raw + a.labels.read_bytes()).hexdigest()[:16]
    lines = json.loads(raw.decode("utf-8"))
    cases = section_cases(lines)
    by_block: dict[tuple, list[dict]] = collections.defaultdict(list)
    for ln in lines:
        by_block[(ln["gs"], ln["block_id"])].append(ln)
    label_of = {(r["gs"], r["block_line"]): r["weight"] for r in read_tsv(a.labels)}
    fonts = load_fonts(a.font_zip, FACES9)
    refs = ref_values(load_fonts(a.font_zip))
    defaults = t.load_role_defaults()
    idx = json.loads((a.run / "object-index.json").read_text(encoding="utf-8"))
    ev = {g: json.loads((a.run / "evidence" / f"{g}.json").read_text(encoding="utf-8")) for g in sorted({k[0] for k, _ in cases})}

    def obj(key: str) -> bytes:
        return (a.run / "objects" / idx[key]["sha256"]).read_bytes()

    def weights(gs: str, b: dict) -> dict:
        """블록의 세 가지 굵기와 근거."""
        now = sd.role_spec(defaults.data, b["role"]).weight
        ls = by_block.get((gs, b["id"]), [])
        bval = statistics.median(x["B"] for x in ls) if ls else None
        measured = WEIGHTS[min(ORDER, key=lambda k: abs(math.log(bval / refs[k])))] if bval else now
        ranks = sorted(ORDER.index(v) for x in ls if (v := label_of.get((gs, f"{x['block_id']}:{x['line']}"))) in ORDER)
        label = WEIGHTS[ORDER[ranks[len(ranks) // 2]]] if ranks else now
        return {"now": now, "measured": measured, "label": label, "B": round(bval, 4) if bval else None, "has_measure": bool(ls),
                "has_label": bool(ranks), "ink_h": statistics.median(x["ink_h"] for x in ls) if ls else None}

    def draw(bg: bytes, blocks: list[tuple[dict, dict, dict, float]], which: str) -> Image.Image:
        """`app.typeset.render`와 같은 배치 · 줄바꿈으로 그리고 글꼴 굵기만 바꾼다."""
        img = Image.open(io.BytesIO(bg)).convert("RGB")
        d = ImageDraw.Draw(img)
        for b, w, box, scale in blocks:
            rb = t.RenderBlock(id=b["id"], role=b["role"], text=b["trans_1"], box=box, style=b["style"], style_stage=b["_stage"],
                               source_ko=b["source_ko"], font_scale=scale)
            resolved = t.resolve_style(rb, defaults, img.width)
            if resolved is None:
                continue
            applied = resolved[0]
            px = float(applied["est_font_px"]) * scale
            font = ImageFont.truetype(io.BytesIO(fonts[w[which]]), size=max(1.0, px))
            y = box["y"]
            for line in t._wrap(d, rb.text, font, max(1, box["w"]))[0]:
                lw = d.textlength(line, font=font)
                x = box["x"] + ((box["w"] - lw) / 2 if applied["align"] == "center" else (box["w"] - lw) if applied["align"] == "right" else 0)
                d.text((x, y), line, font=font, fill=t._rgb(applied["font_color"]))
                y += px * t.LINE_SPACING
        return img

    a.out.mkdir(parents=True)
    (a.out / "img").mkdir()
    cards, data, listing = [], [], []
    count = collections.Counter()
    for i, ((gs, sid), _) in enumerate(cases, 1):
        dd = ev[gs]
        sec = next(s for s in dd["section"] if s["id"] == sid)
        stage = next((x["status"] for x in dd["job_async_task"] if x["stage"] == "style" and x["is_current"] and x["unit_id"] == sid), None)
        rows = sorted((b for b in dd["text_block"] if b["section_id"] == sid and b["is_excluded"] is False and (b["source_ko"] or "").strip()
                       and (b["trans_1"] or "").strip()), key=lambda b: (b["bbox"]["y"], b["bbox"]["x"]))
        blocks = []
        for b in rows:
            box, scale = applied_layout(b)
            blocks.append((dict(b, _stage=stage), weights(gs, b), box, scale))
        if not blocks:
            continue
        top = max(0, int(min(min(b["bbox"]["y"], box["y"]) for b, _, box, _ in blocks)) - 40)
        orig = Image.open(io.BytesIO(obj(sec["image_key"]))).convert("RGB")
        bottom = min(orig.height, int(max(max(b["bbox"]["y"] + b["bbox"]["h"], box["y"] + box["h"]) for b, _, box, _ in blocks)) + 40)
        bg = obj(sec["inpaint_image_url"])
        for view, _ in VIEWS:
            im = orig if view == "orig" else draw(bg, blocks, view)
            im.crop((0, top, im.width, bottom)).save(a.out / "img" / f"{gs}_{sid}_{view}.png")
        cid = f"{gs}|{sid}"
        data.append(cid)
        trs = []
        for b, w, _, _ in blocks:
            count["blocks"] += 1
            count["measured_differs"] += w["measured"] != w["now"]
            count["label_differs"] += w["label"] != w["now"]
            count["no_label"] += not w["has_label"]
            count[f"now_{w['now']}"] += 1
            count[f"measured_{w['measured']}"] += 1
            listing.append({"gs": gs, "section_id": sid, "block_id": b["id"], "role": b["role"], **w})

            def cell(v: int, mark: bool = True) -> str:
                key = next((k for k, x in WEIGHTS.items() if x == v), None)
                color = f"rgb{LABEL_COLOR[key]}" if key else "#555"
                return f'<b style="color:{color}">{FACES9[v]} {v}</b>' + ("" if mark else " <i>(지금 굵기 그대로)</i>")
            height = "" if w["ink_h"] is None else f'{w["ink_h"]:g}px'
            text = (b["trans_1"] or "").replace("\n", " / ")[:70]
            trs.append(f'<tr><td>{esc(b["role"])}</td><td>{esc(text)}</td><td>{cell(w["now"])}</td>'
                       f'<td>{cell(w["measured"], w["has_measure"])}</td><td>{cell(w["label"], w["has_label"])}</td><td>{height}</td></tr>')
        pane = "".join(f'<figure class="v-{v}"><figcaption>{esc(t_)}</figcaption><img src="img/{gs}_{sid}_{v}.png" alt="{esc(t_)}" loading="lazy"></figure>'
                       for v, t_ in VIEWS)
        imgs = f'<div class="pane left">{pane}</div><div class="pane right">{pane}</div>'

        def radios(f: str, opts: tuple) -> str:
            return "".join(f'<label><input type="radio" name="{f}:{esc(cid)}" value="{v}"> {esc(x)}</label>' for v, x in opts)
        cards.append(
            f'<section class="card" id="{esc(cid)}"><h3>이미지 {i} <span class="sub">{esc(gs)} · 섹션 {sid} · 블록 {len(blocks)}개</span></h3>'
            f'<div class="imgs">{imgs}</div>'
            f'<table class="ln"><tr><th>역할</th><th>번역문</th><th>지금</th><th>측정 굵기</th><th>에이전트 라벨 굵기</th><th>원본 글자 높이</th></tr>{"".join(trs)}</table>'
            f'<div class="row"><b>측정 굵기</b>{radios("m", Q_MEASURED)}</div><div class="row"><b>라벨 굵기</b>{radios("l", Q_LABEL)}'
            f'<button type="button" class="clear">지우기</button></div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="예: 작은 주석이 너무 굵게 나옴 · 제목은 더 굵어야 함"></div></section>')
    (a.out / "blocks.json").write_text(json.dumps(listing, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    key = f"style-weight-render-compare:{fp}"
    def view_radio(side: str, default: str, keys: str) -> str:
        return "".join(f'<label><input type="radio" name="view-{side}" value="{v}"{" checked" if v == default else ""}> {k} {esc(x)}</label>'
                       for k, (v, x) in zip(keys, VIEWS))
    css_views = "".join(f"body.l-{v} .pane.left figure.v-{v},body.r-{v} .pane.right figure.v-{v}{{display:block}}" for v, _ in VIEWS)
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⑦ 글자 굵기 — 번역문 전후 비교</title><style>
body{{margin:0;background:#f6f6f4;color:#1d1d1f;font:14px/1.5 system-ui,-apple-system,"Malgun Gothic",sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid #ddd;padding:8px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}}
header h1{{font-size:16px;margin:0 8px 0 0}} header button{{font:inherit;padding:4px 10px}} header label{{background:#f1f1ee;border-radius:4px;padding:2px 8px}}
main{{padding:12px 16px;max-width:2100px;margin:0 auto}} .intro{{background:#fff;border:1px solid #ddd;padding:10px 14px;margin-bottom:12px}}
.card{{background:#fff;border:1px solid #ddd;border-radius:6px;padding:10px 14px;margin:0 0 16px}} .card.ok{{border-left:6px solid #3a9d5d}}
.card h3{{margin:0 0 6px;font-size:15px}} .sub{{font-weight:normal;color:#666;font-size:12.5px;margin-left:6px}}
.imgs{{display:flex;gap:10px;align-items:flex-start}} .pane{{flex:1 1 0;min-width:0}} .pane figure{{display:none;margin:0}}
.pane figcaption{{font-weight:600;font-size:13px;margin-bottom:3px}} .pane.left figcaption{{color:#34518f}} .pane.right figcaption{{color:#a0510a}}
.pane img{{display:block;width:100%;border:1px solid #ddd;background:#fff}} {css_views}
table.ln{{border-collapse:collapse;margin:8px 0;font-size:13px}} table.ln td,table.ln th{{border:1px solid #ddd;padding:2px 8px;text-align:left}}
table.ln th{{background:#f1f1ee}} table.ln i{{color:#888}}
.row{{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;margin-top:6px}} .row b{{min-width:64px}} .row button{{font:inherit;padding:3px 10px}}
.note{{flex:1;min-width:260px;padding:4px}}
</style></head><body class="l-orig r-measured">
<header><h1>⑦ 글자 굵기 — 번역문 전후 비교</h1><span id="cnt"></span><span><b>왼쪽</b> {view_radio("l", "orig", "QWER")}</span>
<span><b>오른쪽</b> {view_radio("r", "measured", "1234")}</span><button id="exp">TSV 내보내기</button></header>
<main><div class="intro"><b>보는 법</b> — 이미지마다 <b>두 그림이 나란히</b> 나옵니다. 처음에는 왼쪽이 원본, 오른쪽이 측정 굵기로 그린 번역문입니다.
머리의 "왼쪽" · "오른쪽"에서 칸마다 보여 줄 그림을 고릅니다(왼쪽은 Q W E R 키, 오른쪽은 숫자 키 1 2 3 4). 예: 왼쪽 지금 · 오른쪽 측정 굵기로 두면 전후 비교입니다.
<b>원본</b>과 견주어 번역문의 <b>굵기 느낌</b>이 어느 쪽이 더 가까운지 봐 주세요. 세 번역 그림은 글자색 · 크기 · 정렬 · 번역문이 같고 굵기만 다릅니다.<br>
<b>지금</b> = 역할 설정 굵기(title · price SemiBold 600, 나머지 Regular 400). <b>측정 굵기</b> = 원본에서 잰 획 두께에 가장 가까운 Pretendard 굵기
(Thin 100 · Light 300 · Medium 500 · Bold 700 · Black 900). <b>에이전트 라벨 굵기</b> = 에이전트가 눈으로 본 굵기(사람 판정 아님) —
라벨이 없는 블록(작은 글자 등)은 지금 굵기 그대로입니다.<br>
굵기는 블록당 하나입니다. 한 블록 안에서 줄마다 굵기가 다르거나 일부 단어만 굵은 원본은 한 굵기로 그려집니다. 이 그림은 로컬 Pillow 조판이라
운영 화면(서버 HTML 렌더)과 줄바꿈 · 자간이 다를 수 있고, 넘침 · 줄바꿈은 판단에 넣지 않아도 됩니다.<br>
이미지 {len(data)}개. 입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>
<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 입력 지문 {fp} · 저장 키 {esc(key)}</span></div>
{"".join(cards)}</main>
<script>
const K={json.dumps(key)},FP={json.dumps(fp)},IDS={json.dumps(data)},VIEWS={json.dumps([v for v, _ in VIEWS])};
let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')||{{}}}}catch(e){{st={{}}}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}};paint()}}
function paint(){{let n=0;for(const id of IDS){{const d=!!(st[id]&&st[id].m);if(d)n++;document.getElementById(id).classList.toggle('ok',d)}}
document.getElementById('cnt').textContent='판단 '+n+' / '+IDS.length}}
const cur={{l:'orig',r:'measured'}};
function view(side,v){{cur[side]=v;document.body.className='l-'+cur.l+' r-'+cur.r;
document.querySelectorAll('input[name=view-'+side+']').forEach(r=>r.checked=r.value===v)}}
for(const side of ['l','r'])document.querySelectorAll('input[name=view-'+side+']').forEach(r=>r.onchange=()=>view(side,r.value));
document.addEventListener('keydown',e=>{{if(e.target.tagName==='INPUT'&&e.target.type==='text')return;
const i='1234'.indexOf(e.key),j='qwer'.indexOf(e.key.toLowerCase());if(i>=0)view('r',VIEWS[i]);else if(j>=0)view('l',VIEWS[j])}});
for(const id of IDS){{const el=document.getElementById(id),s=st[id]||{{}};
el.querySelectorAll('input[type=radio]').forEach(r=>{{const f=r.name.split(':')[0];if(s[f]===r.value)r.checked=true;
r.onchange=()=>{{(st[id]=st[id]||{{}})[f]=r.value;save()}}}});
el.querySelector('.clear').onclick=()=>{{el.querySelectorAll('input[type=radio]').forEach(r=>r.checked=false);if(st[id]){{delete st[id].m;delete st[id].l}}save()}};
const note=el.querySelector('.note');note.value=s.n||'';note.oninput=()=>{{(st[id]=st[id]||{{}}).n=note.value;save()}}}}
document.getElementById('exp').onclick=()=>{{let t='case_no\\tgs\\tsection_id\\tmeasured_vs_now\\tlabel_vs_now\\tnote\\treview_fingerprint\\n';
IDS.forEach((id,i)=>{{const s=st[id];if(!s||!(s.m||s.l||s.n))return;const[g,sid]=id.split('|');
t+=[i+1,g,sid,s.m||'',s.l||'',(s.n||'').replace(/[\\t\\n]/g,' '),FP].join('\\t')+'\\n'}});
const u=URL.createObjectURL(new Blob([t],{{type:'text/tab-separated-values'}}));const x=document.createElement('a');x.href=u;
x.download='style_weight_render_compare_'+FP+'.tsv';x.click()}};
paint();
</script></body></html>"""
    (a.out / "index.html").write_text(page, encoding="utf-8")
    print(f"전후 비교 페이지: {a.out / 'index.html'} · 이미지 {len(data)} · 블록 {count['blocks']} · 측정 굵기가 지금과 다른 블록 {count['measured_differs']} · "
          f"라벨 굵기가 지금과 다른 블록 {count['label_differs']}(라벨 없는 블록 {count['no_label']}) · 저장 키 {key}")
    print({k: v for k, v in sorted(count.items()) if k.startswith(("now_", "measured_"))})
    return 0


if __name__ == "__main__":
    sys.exit(main())
