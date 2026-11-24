"""⑨ 정렬 원본 라벨 사람 검수 페이지 생성 — 정적 HTML, 로컬에서 연다(2026-11-03). 개발용, 운영 경로 아님.

    python docs/ai-experiments/tools/typeset_align_review_page.py \
        --rows docs/ai-experiments/runs/2026-11-03_09-typeset_align-frequency/align_rows.json \
        --features docs/ai-experiments/runs/2026-11-03_09-typeset_align-frequency/features.json \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --out docs/ai-experiments/runs/2026-11-03_09-typeset_align-frequency/review

입력: `typeset_align_probe.py aggregate`의 `align_rows.json`(에이전트 라벨 · 적용 정렬 · 오류) · `features.json`(영역)과 베이스라인 회차.
모두 읽기만 한다. 출력은 이미지를 data URI로 넣은 HTML 한 파일(`<out>/index.html`). --out이 이미 있으면 거부한다(덮어쓰지 않는다).

검수 대상(에이전트 라벨 확인용 — 전수가 아니다): 원문 여러 줄 오류 전부 · 약한 근거(`section_center_only`) 전부 + 한 줄 오류 30 ·
오류 아님 20 · 판단 불가 10(각각 (GS, id)의 SHA-256 순 앞에서부터, 결정적). 순서는 (GS, id)의 SHA-256으로 섞는다.

표시 원칙:
- 검수 질문은 하나 — 왼쪽 원본에서 빨간 상자 글자의 정렬 기준(왼쪽 끝 · 가운데 · 오른쪽 끝 · 판단 불가). 빨간 선은 상자 바깥 1px.
- 원본 섹션 이미지를 섹션 전체 폭으로 잘라 맥락(배지 · 칸 · 위아래 문구)을 함께 보인다. 축소 · 보정 없음.
- 에이전트 라벨 · 근거 · 적용 정렬 · 렌더는 "참고 보기"를 켤 때만 보인다(기본 꺼짐). 미리 선택된 값은 없다.
- 입력은 브라우저에만 저장(localStorage)되고 "TSV 내보내기"로 받는다. 입력 문자열은 모두 HTML 이스케이프한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from typeset_overflow_review_page import data_uri, esc  # noqa: E402

VERDICTS = (("left", "왼쪽 끝"), ("center", "가운데"), ("right", "오른쪽 끝"), ("unsure", "판단 불가"))
QUOTA = {"single_error": 30, "ok": 20, "unknown": 10}


def _h(r: dict) -> str:
    return hashlib.sha256(f"{r['gs']}:{r['id']}".encode()).hexdigest()


def pick(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=_h)
    chosen = {(r["gs"], r["id"]): r for r in rows
              if (r["error"] and r["src_lines"] >= 2) or r["basis_note"].startswith("section_center_only")}
    groups = {"single_error": [r for r in rows if r["error"] and r["src_lines"] == 1],
              "ok": [r for r in rows if r["evaluable"] and not r["error"]],
              "unknown": [r for r in rows if not r["evaluable"]]}
    for g, n in QUOTA.items():
        for r in [r for r in groups[g] if (r["gs"], r["id"]) not in chosen][:n]:
            chosen[(r["gs"], r["id"])] = r
    return sorted(chosen.values(), key=_h)


def crop(im: Image.Image, box: dict) -> Image.Image:
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    pad = max(80, h)
    z = im.crop((0, max(0, y - pad), im.width, min(im.height, y + h + pad)))
    top = max(0, y - pad)
    ImageDraw.Draw(z).rectangle((x - 1, y - top - 1, x + w, y - top + h), outline=(255, 0, 0), width=1)
    return z


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=Path, required=True)
    ap.add_argument("--features", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    raw = a.rows.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    cases = pick(json.loads(raw.decode("utf-8")))
    feats = {(f["gs"], f["id"]): f for f in json.loads(a.features.read_text(encoding="utf-8"))}
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
        f = feats[(r["gs"], r["id"])]
        sec = secs[r["gs"]][r["section_id"]]
        src_o, src_z = data_uri(crop(obj(sec["image_key"]), f["orig_bbox"])), data_uri(crop(obj(sec["render_image_key"]), f["box"]))
        radios = "".join(f'<label><input type="radio" name="v:{esc(cid)}" value="{v}"> {esc(t)}</label>' for v, t in VERDICTS)
        ref = (f'<div class="ref"><b>참고 · 에이전트 라벨</b> {esc(r["label"])} — {esc(r["basis_note"])}<br>'
               f'<b>적용 정렬</b> {esc(r["applied_align"])}(⑦ 근거 {esc(r["align_basis"] or r["align_null_reason"])}) · '
               f'오류 판정 {"예" if r["error"] else "아니오" if r["evaluable"] else "판단 불가"}'
               f'<figure><figcaption>렌더(미리보기)</figcaption><img src="{src_z}" alt="렌더"></figure></div>')
        cards.append(
            f'<section class="card" id="{esc(cid)}" data-id="{esc(cid)}">'
            f'<h3>사례 {n} <span class="sub">{esc(r["gs"])} · 블록 {esc(r["id"])} · 섹션 {esc(r["section_id"])} · {esc(r["role"])}</span></h3>'
            f'<p class="meta">원문 {r["src_lines"]}줄 · 영역 {f["orig_bbox"]["w"]}×{f["orig_bbox"]["h"]}px · 섹션 폭 {f["section_w"]}px</p>'
            f'<div class="txt"><div><b>원문</b> {esc(r["source_ko"])}</div></div>'
            f'<div class="imgs"><figure><figcaption>원본</figcaption><img src="{src_o}" alt="원본"></figure></div>{ref}'
            f'<div class="form"><div class="row"><b>판단</b>{radios}<button type="button" class="clear">지우기</button></div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="예: 원형 배지 중앙 · 위 제목과 같은 왼쪽 축"></div></div></section>')
    key = f"typeset-align-review:{fp}"
    intro = (f'<b>검수 질문</b> — <b>왼쪽 원본</b>에서 빨간 상자 안 글자는 <b>어느 기준선에 맞춰 놓였나요?</b> '
             f'번역문이 같은 상자에 들어간다면 왼쪽 끝 · 가운데 · 오른쪽 끝 중 어디에 맞춰야 원본 배치와 같아 보이는지로 판단해 주세요.<br>'
             f'한 줄 글자는 상자 자체보다 주변(배지 · 칸 · 버튼 · 위아래 문구 · 반복 항목)을 보고 판단합니다. 근거가 없거나 둘 이상이 똑같이 그럴듯하면 '
             f'"판단 불가"로 두고 메모에 이유를 적어 주세요. 이미지를 누르면 원본 픽셀 크기로 봅니다.<br>'
             f'사례는 {len(cases)}개입니다(에이전트가 오류로 본 사례와 아닌 사례가 섞여 있고 순서는 무작위). "참고 보기"는 에이전트 판단 · 시스템 적용값이며 '
             f'사람 판단이 아닙니다 — 먼저 끈 채로 판단하기를 권합니다. 입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>'
             f'<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 입력 지문 {fp} · 저장 키 {esc(key)}</span>')
    page = render_page("⑨ 정렬 원본 라벨 검수", intro, cards, ids, key, fp, VERDICTS,
                       "case_no\\tgs\\tblock_id\\tverdict\\tnote\\treview_fingerprint", f"typeset_align_review_{fp}.tsv")
    (a.out / "index.html").write_text(page, encoding="utf-8")
    print(f"검수 페이지: {a.out / 'index.html'} · 사례 {len(cases)} · 저장 키 {key}")
    return 0


def render_page(title: str, intro: str, cards: list[str], ids: list[str], key: str, fp: str,
                verdicts: tuple, header: str, filename: str, fields: tuple[str, ...] = ("v",), controls: str = "") -> str:
    """검수 페이지 HTML. fields = 카드 안 라디오 그룹 이름 접두(v · p …). TSV 열은 case_no · id 두 칸 · fields · note · 지문.
    controls = 머리줄에 더할 HTML(섹션 경계 검수의 경계선 표시 토글 등)."""
    flt = "".join(f'<option value="{f}:{v}">판단: {esc(t)}</option>' for f in fields[:1] for v, t in verdicts)
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>
:root{{--bg:#f6f6f4;--fg:#1d1d1f;--card:#fff;--line:#ddd;--mut:#666}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Malgun Gothic",sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}}
header h1{{font-size:16px;margin:0 8px 0 0}} header button,header select{{font:inherit;padding:4px 10px}}
main{{padding:12px 16px;max-width:1200px;margin:0 auto}} .intro{{background:#fff;border:1px solid var(--line);padding:10px 14px;margin-bottom:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 14px;margin:0 0 14px}}
.card.done{{border-left:5px solid #3a9d5d}} .card h3{{margin:0 0 4px;font-size:15px}} .sub{{font-weight:normal;color:var(--mut);font-size:12.5px;margin-left:6px}}
.meta{{margin:0 0 6px;color:var(--mut);font-size:12.5px}} .txt{{font-size:13px;margin-bottom:8px;white-space:pre-wrap}} .txt b{{color:var(--mut);margin-right:6px}}
.imgs{{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start}} figure{{margin:0;flex:0 1 auto}} figcaption{{font-size:12px;color:var(--mut)}}
.imgs img,.ref img{{display:block;border:1px solid var(--line);max-width:min(1000px,calc(100vw - 72px));cursor:zoom-in;background:#fff}}
img.big{{max-width:none!important;width:auto;cursor:zoom-out;image-rendering:pixelated}}
.wrap{{position:relative;display:inline-block}} .bline{{position:absolute;left:0;right:0;height:0;border-top:1px dashed rgba(255,0,0,.85);pointer-events:none}}
body.noline .bline{{display:none}}
.form .row{{margin-top:8px;display:flex;flex-wrap:wrap;gap:4px 16px;align-items:center}} .form b{{min-width:36px}} .note{{flex:1;min-width:260px;padding:4px}}
.ref{{display:none;background:#f3f5fb;border:1px dashed #9fb0d9;border-radius:4px;padding:6px 10px;margin-top:8px;font-size:12.5px}} .ref b{{color:#34518f;margin-right:6px}}
body.showref .ref{{display:block}}
</style></head><body>
<header><h1>{esc(title)}</h1><span id="cnt"></span>
<label>보기 <select id="flt"><option value="all">전체</option><option value="todo">미검수</option><option value="done">검수함</option>{flt}</select></label>
<label><input type="checkbox" id="showref"> 참고 보기</label>{controls}
<button id="exp">TSV 내보내기</button></header>
<main><div class="intro">{intro}</div>
{"".join(cards)}</main>
<script>
const K={json.dumps(key)},FP={json.dumps(fp)},IDS={json.dumps(ids)},F={json.dumps(list(fields))};
let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')||{{}}}}catch(e){{st={{}}}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}};count()}}
function done(v){{return !!v&&F.every(f=>v[f])}}
function count(){{let n=0;for(const id of IDS){{const c=document.getElementById(id),d=done(st[id]);if(d)n++;if(c)c.classList.toggle('done',d)}}
document.getElementById('cnt').textContent='검수 '+n+' / '+IDS.length}}
for(const card of document.querySelectorAll('.card[data-id]')){{const id=card.dataset.id,v=st[id]||{{}};
card.querySelectorAll('input[type=radio]').forEach(r=>{{const f=r.name.split(':')[0];if(v[f]===r.value)r.checked=true;
r.onchange=()=>{{(st[id]=st[id]||{{}})[f]=r.value;save()}}}});
card.querySelector('.clear').onclick=()=>{{card.querySelectorAll('input[type=radio]').forEach(r=>r.checked=false);if(st[id])F.forEach(f=>delete st[id][f]);save()}};
const note=card.querySelector('.note');note.value=v.n||'';note.oninput=()=>{{(st[id]=st[id]||{{}}).n=note.value;save()}}}}
document.querySelectorAll('.imgs img,.ref img').forEach(im=>im.onclick=()=>im.classList.toggle('big'));
document.getElementById('showref').onchange=e=>document.body.classList.toggle('showref',e.target.checked);
const nl=document.getElementById('showline');if(nl)nl.onchange=e=>document.body.classList.toggle('noline',!e.target.checked);
document.getElementById('flt').onchange=e=>{{const f=e.target.value;for(const c of document.querySelectorAll('.card')){{const v=st[c.id]||{{}};let show=true;
if(f==='todo')show=!done(v);else if(f==='done')show=done(v);else if(f.includes(':')){{const[k,x]=f.split(':');show=v[k]===x}}c.style.display=show?'':'none'}}}};
document.getElementById('exp').onclick=()=>{{let t={json.dumps(header.replace(chr(92) + 't', chr(9)))}+'\\n';IDS.forEach((id,i)=>{{const v=st[id];
if(!v||!(F.some(f=>v[f])||v.n))return;const[g,b]=id.split('|');t+=[i+1,g,b,...F.map(f=>v[f]||''),(v.n||'').replace(/[\\t\\n]/g,' '),FP].join('\\t')+'\\n'}});
const u=URL.createObjectURL(new Blob([t],{{type:'text/tab-separated-values'}}));const x=document.createElement('a');x.href=u;
x.download={json.dumps(filename)};x.click()}};
count();
</script></body></html>"""


if __name__ == "__main__":
    sys.exit(main())
