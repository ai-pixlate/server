"""⑨ 넘침 판정 육안 검수 페이지 생성 — 정적 HTML, 로컬에서 연다(2026-10-30). 개발용, 운영 경로 아님.

    python docs/ai-experiments/tools/typeset_overflow_review_page.py \
        --blocks docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow/blocks.json \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --out docs/ai-experiments/runs/2026-10-30_09-typeset_single-line-overflow/review

입력: `typeset_overflow_probe.py`의 `blocks.json`(블록별 재계산 · 분류)과 베이스라인 회차(`evidence/` · `objects/` · `object-index.json`).
둘 다 읽기만 한다. 출력은 이미지를 data URI로 넣은 HTML 한 파일(`<out>/index.html`)이라 그 파일만 열거나 전달하면 된다.
--out이 이미 있으면 거부한다(덮어쓰지 않는다).

검수 대상: 도구 분류 leading(오판 후보) · edge(경계) 전부 + 대조용 wrap · width · fit 일부(분류별 (GS, 블록 id) 순으로 같은 간격 추출).
사례 순서는 (GS, 블록 id)의 SHA-256 순서로 섞어 분류끼리 모이지 않게 한다.

표시 원칙:
- 검수 질문은 하나 — 오른쪽 렌더에서 번역문 글자가 빨간 상자(배치 영역) 안에 들어가는가. 빨간 선은 영역 바깥 1px에 그려 영역 안 픽셀을 가리지 않는다.
- 이미지는 원본 섹션 이미지 · 미리보기 렌더(`section.render_image_key`)를 같은 범위로 자른 원본 픽셀 그대로의 PNG다(축소 · 보정 없음).
- 도구 분류 · 기록된 overflow는 "참고 보기"를 켤 때만 보인다(기본 꺼짐). 미리 선택된 값은 없다.
- 검수 입력은 브라우저에만 저장(localStorage)되고 "TSV 내보내기"로 받는다. 입력 문자열은 모두 HTML 이스케이프한다.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import html
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

FULL = ("leading", "edge")
CONTROL = {"wrap": 8, "width": 8, "fit": 4}
VERDICTS = (("inside", "안에 들어감"), ("outside", "밖으로 나감"), ("unsure", "판단 불가"))
CLS_TEXT = {"leading": "leading — 글리프는 영역 안, 줄 수 × 행간만 초과(오판 후보)", "edge": "edge — 글리프가 영역을 3% 이하로 넘음(경계)",
            "wrap": "wrap — 줄바꿈으로 글리프가 영역 아래로 나감", "width": "width — 한 단어가 영역 폭보다 넓음", "fit": "fit — 넘침 아님"}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def pick(rows: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (r["gs"], r["id"])):
        by.setdefault(r["cls"], []).append(r)
    out = [r for c in FULL for r in by.get(c, [])]
    for c, n in CONTROL.items():
        src = by.get(c, [])
        if src:
            step = len(src) / min(n, len(src))
            out += [src[int(i * step)] for i in range(min(n, len(src)))]
    return sorted(out, key=lambda r: hashlib.sha256(f"{r['gs']}:{r['id']}".encode()).hexdigest())


def crop_pair(org: Image.Image, ren: Image.Image, box: dict) -> tuple[Image.Image, Image.Image]:
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    pad_y = max(24, h)
    pad_x = max(pad_y, w // 4)
    c = (max(0, x - pad_x), max(0, y - pad_y), min(ren.width, x + w + pad_x), min(ren.height, y + h + int(pad_y * 2.5)))
    res = []
    for im in (org, ren):
        z = im.crop(c)
        ImageDraw.Draw(z).rectangle((x - c[0] - 1, y - c[1] - 1, x - c[0] + w, y - c[1] + h), outline=(255, 0, 0), width=1)
        res.append(z)
    return res[0], res[1]


def data_uri(im: Image.Image) -> str:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    raw = a.blocks.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    cases = pick(json.loads(raw.decode("utf-8")))
    idx = json.loads((a.run / "object-index.json").read_text(encoding="utf-8"))
    secs = {g: {s["id"]: s for s in json.loads((a.run / "evidence" / f"{g}.json").read_text(encoding="utf-8"))["section"]}
            for g in sorted({r["gs"] for r in cases})}
    a.out.mkdir(parents=True)
    opened: dict[str, Image.Image] = {}

    def obj(key: str) -> Image.Image:
        if key not in opened:
            opened[key] = Image.open(a.run / "objects" / idx[key]["sha256"]).convert("RGB")
        return opened[key]

    cards, ids = [], []
    for n, r in enumerate(cases, 1):
        cid = f"{r['gs']}|{r['id']}"
        ids.append(cid)
        sec = secs[r["gs"]][r["section_id"]]
        o, z = crop_pair(obj(sec["image_key"]), obj(sec["render_image_key"]), r["box"])
        src_o, src_z = data_uri(o), data_uri(z)
        b = r["box"]
        radios = "".join(f'<label><input type="radio" name="v:{esc(cid)}" value="{v}"> {esc(t)}</label>' for v, t in VERDICTS)
        ref = (f'<div class="ref"><b>참고 · 도구 분류</b> {esc(CLS_TEXT.get(r["cls"], r["cls"]))} · 기록 overflow={esc(r["db_overflow"])} · '
               f'1줄 높이/영역 높이 {r["one_line_h_ratio"]:.3f} · 잉크 폭/영역 폭 {esc(round(r["ink_w_ratio"], 3) if r["ink_w_ratio"] is not None else "—")} · '
               f'잉크 하단 초과 {esc(round(r["ink_bottom_over"], 3) if r["ink_bottom_over"] is not None else "—")}(영역 높이 대비)</div>')
        cards.append(
            f'<section class="card" id="{esc(cid)}" data-id="{esc(cid)}" data-cls="{esc(r["cls"])}">'
            f'<h3>사례 {n} <span class="sub">{esc(r["gs"])} · 블록 {esc(r["id"])} · 섹션 {esc(r["section_id"])} · {esc(r["role"])}</span></h3>'
            f'<p class="meta">영역 {b["w"]}×{b["h"]}px · 글자 크기 {r["font_px"]:g}px · 정렬 {esc(r["align"])} · 원문 {r["src_lines"]}줄 → 렌더 {r["tr_lines"]}줄'
            f'{" · 원문과 같은 문구" if r["same_text"] else ""}</p>'
            f'<div class="txt"><div><b>원문</b> {esc(r["source_ko"])}</div><div><b>번역</b> {esc(r["trans_1"])}</div></div>'
            f'<div class="imgs"><figure><figcaption>원본</figcaption><img src="{src_o}" alt="원본"></figure>'
            f'<figure><figcaption>렌더(미리보기)</figcaption><img src="{src_z}" alt="렌더"></figure></div>{ref}'
            f'<div class="form"><div class="row"><b>판단</b>{radios}<button type="button" class="clear">지우기</button></div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="예: 하강부(g·y)만 1px 걸림 · 오른쪽으로 넘침"></div></div></section>')
    key = f"typeset-overflow-review:{fp}"
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⑨ 넘침 판정 육안 검수</title><style>
:root{{--bg:#f6f6f4;--fg:#1d1d1f;--card:#fff;--line:#ddd;--mut:#666}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Malgun Gothic",sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}}
header h1{{font-size:16px;margin:0 8px 0 0}} header button,header select{{font:inherit;padding:4px 10px}}
main{{padding:12px 16px;max-width:1200px;margin:0 auto}} .intro{{background:#fff;border:1px solid var(--line);padding:10px 14px;margin-bottom:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 14px;margin:0 0 14px}}
.card.done{{border-left:5px solid #3a9d5d}} .card h3{{margin:0 0 4px;font-size:15px}} .sub{{font-weight:normal;color:var(--mut);font-size:12.5px;margin-left:6px}}
.meta{{margin:0 0 6px;color:var(--mut);font-size:12.5px}} .txt{{font-size:13px;margin-bottom:8px;white-space:pre-wrap}} .txt b{{color:var(--mut);margin-right:6px}}
.imgs{{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start}} figure{{margin:0;flex:0 1 auto}} figcaption{{font-size:12px;color:var(--mut)}}
.imgs img{{display:block;border:1px solid var(--line);max-width:min(560px,calc(100vw - 72px));cursor:zoom-in;background:#fff}}
.imgs img.big{{max-width:none;width:auto;cursor:zoom-out;image-rendering:pixelated}}
.form .row{{margin-top:8px;display:flex;flex-wrap:wrap;gap:4px 16px;align-items:center}} .form b{{width:36px}} .note{{flex:1;min-width:260px;padding:4px}}
.ref{{display:none;background:#f3f5fb;border:1px dashed #9fb0d9;border-radius:4px;padding:6px 10px;margin-top:8px;font-size:12.5px}} .ref b{{color:#34518f;margin-right:6px}}
body.showref .ref{{display:block}}
</style></head><body>
<header><h1>⑨ 넘침 판정 육안 검수</h1><span id="cnt"></span>
<label>보기 <select id="flt"><option value="all">전체</option><option value="todo">미검수</option><option value="done">검수함</option>
{"".join(f'<option value="v:{v}">판단: {esc(t)}</option>' for v, t in VERDICTS)}</select></label>
<label><input type="checkbox" id="showref"> 참고 보기(도구 분류)</label>
<button id="exp">TSV 내보내기</button></header>
<main><div class="intro"><b>검수 질문</b> — <b>오른쪽 렌더</b>에서 번역문 글자가 <b>빨간 상자(배치 영역) 안에 들어가나요?</b>
빨간 선은 영역 바로 바깥에 그렸습니다 — 글자가 선에 닿기만 하고 넘지 않으면 "안에 들어감"입니다. 왼쪽 원본은 원래 글자가 영역 안에 어떻게 놓였는지 비교용입니다.
이미지를 누르면 원본 픽셀 크기(작은 사례는 확대)로 보고 다시 누르면 돌아옵니다.
줄 간격 · 정렬 · 글자 크기의 적절함은 이번 질문이 아닙니다 — 필요하면 메모에 적어 주세요.<br>
사례는 {len(cases)}개입니다(시스템이 넘침으로 본 사례와 아닌 사례가 섞여 있고 순서는 무작위). "참고 보기"는 도구의 자동 분류이며 사람 판단이 아닙니다 —
먼저 끈 채로 판단하기를 권합니다. 미리 선택된 값은 없고 입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>
<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 입력 지문 {fp} · 저장 키 {esc(key)}</span></div>
{"".join(cards)}</main>
<script>
const K={json.dumps(key)},FP={json.dumps(fp)},IDS={json.dumps(ids)};
let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')||{{}}}}catch(e){{st={{}}}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}};count()}}
function done(v){{return !!(v&&v.v)}}
function count(){{let n=0;for(const id of IDS){{const c=document.getElementById(id),d=done(st[id]);if(d)n++;if(c)c.classList.toggle('done',d)}}
document.getElementById('cnt').textContent='검수 '+n+' / '+IDS.length}}
for(const card of document.querySelectorAll('.card[data-id]')){{const id=card.dataset.id,v=st[id]||{{}};
card.querySelectorAll('input[type=radio]').forEach(r=>{{if(v.v===r.value)r.checked=true;r.onchange=()=>{{(st[id]=st[id]||{{}}).v=r.value;save()}}}});
card.querySelector('.clear').onclick=()=>{{card.querySelectorAll('input[type=radio]').forEach(r=>r.checked=false);if(st[id])delete st[id].v;save()}};
const note=card.querySelector('.note');note.value=v.n||'';note.oninput=()=>{{(st[id]=st[id]||{{}}).n=note.value;save()}}}}
document.querySelectorAll('.imgs img').forEach(im=>im.onclick=()=>{{const big=!im.classList.contains('big');im.classList.toggle('big',big);
im.style.width=big&&im.naturalWidth<560?(im.naturalWidth*2)+'px':''}});
document.getElementById('showref').onchange=e=>document.body.classList.toggle('showref',e.target.checked);
document.getElementById('flt').onchange=e=>{{const f=e.target.value;for(const c of document.querySelectorAll('.card')){{const v=st[c.id]||{{}};let show=true;
if(f==='todo')show=!done(v);else if(f==='done')show=done(v);else if(f.startsWith('v:'))show=v.v===f.slice(2);c.style.display=show?'':'none'}}}};
document.getElementById('exp').onclick=()=>{{let t='case_no\\tgs\\tblock_id\\tverdict\\tnote\\treview_fingerprint\\n';IDS.forEach((id,i)=>{{const v=st[id];
if(!v||!(v.v||v.n))return;const[g,b]=id.split('|');t+=[i+1,g,b,v.v||'',(v.n||'').replace(/[\\t\\n]/g,' '),FP].join('\\t')+'\\n'}});
const u=URL.createObjectURL(new Blob([t],{{type:'text/tab-separated-values'}}));const x=document.createElement('a');x.href=u;
x.download='typeset_overflow_review_'+FP+'.tsv';x.click()}};
count();
</script></body></html>"""
    (a.out / "index.html").write_text(page, encoding="utf-8")
    print(f"검수 페이지: {a.out / 'index.html'} · 사례 {len(cases)} · 저장 키 {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
