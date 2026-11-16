"""⑨ 브라우저 넘침 측정 — 사람 확인 페이지 생성 · 결과 집계(2026-11-16). 개발용, 운영 경로 아님.

    # 검토 페이지(이미지를 data URI로 넣은 HTML 한 파일 — 그 파일만 열거나 전달하면 된다)
    python docs/ai-experiments/tools/typeset_browser_review.py page --run <measure 출력 폴더>
    # 내보낸 TSV 집계(검증에 실패하면 결과를 만들지 않고 종료 코드 1)
    python docs/ai-experiments/tools/typeset_browser_review.py aggregate --run <measure 출력 폴더> --tsv <내보낸 TSV>

입력: `typeset_browser_measure.py measure`의 출력 폴더(`measure.json` · `blocks/*.review.png` · `blocks/*.scene.png`). 읽기만 한다.
출력: `<run>/check/index.html`(검토 페이지) · `<run>/check/review_summary.json`(집계).

무엇을 사람에게 묻나 — 질문은 하나: "검정 글자가 빨간 선 안쪽(배치 영역)에 들어가나요?"(안에 들어감 · 밖으로 나감 · 판단 불가)
- 전수: 도구가 넘침이라 했지만 실제 픽셀로는 영역 안으로 보이는 사례 = 측정 오차 의심(overflow_quantized · overflow_subpixel ·
  overflow_pixel_inside · fit_pixel_outside). 사람 판단으로 "측정 오류"인지 "실제 넘침"인지 가린다.
- 대조: 도구가 실제 넘침(overflow_confirmed)으로 본 것 · 정상(fit)으로 본 것 일부(키 순으로 같은 간격 추출). 질문 · 그림이
  제대로 작동하는지와 도구가 한쪽으로 치우치지 않았는지 본다.
- 순서는 키의 SHA-256 순으로 섞는다. 도구 분류는 기본으로 숨기고, 사용자가 펼칠 때만 보인다. 미리 선택된 값은 없다.

그림:
- ① 판단용: 측정에 쓴 블록 요소를 글자색만 검정으로 바꿔 흰 바탕에 DPR 2로 찍은 그대로(줄바꿈 · 글자 위치는 실제와 같다).
  빨간 선은 영역 바깥쪽 1 디바이스 픽셀에 그려 영역 안 픽셀을 가리지 않는다. 확대(1 · 2 · 4배, 픽셀 그대로).
- ② 실제 화면: 같은 입력의 섹션 합성(배경 · 글자색 그대로, 섹션 밖도 보이게, 덧그림 없음) + 같은 빨간 선.
사람 판단은 표본에 대한 것이다(전수 아님). 판정 규칙(허용 오차 없음)을 바꾸는 근거로 쓰지 않는다 — 측정 오류와 실제 넘침을 가리는 근거다.
"""
from __future__ import annotations

import argparse
import base64
import collections
import csv
import hashlib
import html
import json
import sys
from pathlib import Path

FULL = ("overflow_quantized", "overflow_subpixel", "overflow_pixel_inside", "fit_pixel_outside")
CONTROL = {"overflow_confirmed": 6, "fit": 6}
VERDICTS = (("inside", "안에 들어감", "1"), ("outside", "밖으로 나감", "2"), ("unsure", "판단 불가", "3"))
TSV_COLUMNS = ("case_no", "key", "verdict", "note", "review_fingerprint")
SIDE_KO = {"left": "왼쪽", "right": "오른쪽", "top": "위", "bottom": "아래"}
TOOL_KO = {
    "overflow_quantized": "넘침(작은 초과) — 이 PC의 측정 반올림 범위라 실제로는 안일 수 있다고 본 사례",
    "overflow_subpixel": "넘침(0.5px 이하) — 그림으로는 구분하기 어려운 사례",
    "overflow_pixel_inside": "넘침 — 그림으로는 안으로 보여 설명되지 않는 사례",
    "fit_pixel_outside": "정상 — 그림으로는 밖으로 보여 설명되지 않는 사례",
    "overflow_confirmed": "넘침 — 그림으로도 밖으로 보인 사례(대조)",
    "fit": "정상 — 그림으로도 안으로 보인 사례(대조)",
}


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def pick(blocks: list[dict]) -> list[dict]:
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for r in sorted(blocks, key=lambda r: r["key"]):
        if r.get("images", {}).get("review"):
            by[r["judgement"]].append(r)
    out = [r for j in FULL for r in by.get(j, [])]
    for j, n in CONTROL.items():
        src = by.get(j, [])
        k = min(n, len(src))
        out += [src[int(i * len(src) / k)] for i in range(k)] if k else []
    return sorted(out, key=lambda r: hashlib.sha256(r["key"].encode()).hexdigest())


def fingerprint(meta: dict, cases: list[dict]) -> str:
    raw = json.dumps({"input": meta.get("inputSha256"), "cases": [[r["key"], r["judgement"]] for r in cases]},
                     ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def tool_note(r: dict) -> str:
    p = r["primary"]
    if not p.get("ok"):
        return "측정 실패"
    sides = [SIDE_KO[s] for s in SIDE_KO if p["overflow"][s]]
    ex = p["excess"]
    over = " · ".join(f"{SIDE_KO[s]} {ex[s]:.2f}px" for s in SIDE_KO if p["overflow"][s]) or "없음"
    pix = (r.get("pixel") or {}).get("excess") or {}
    pix_over = " · ".join(f"{SIDE_KO[s]} {pix[s]:.2f}px" for s in SIDE_KO if pix.get(s, 0) > 0) or "없음"
    return (f"{TOOL_KO.get(r['judgement'], r['judgement'])}<br>도구가 계산한 초과: {esc(over)}"
            f"{' (방향 ' + esc('·'.join(sides)) + ')' if sides else ''}<br>그림에서 잰 초과: {esc(pix_over)} · 줄 {len(p['lines'])}")


def build_page(run: Path) -> Path:
    m = json.loads((run / "measure.json").read_text(encoding="utf-8"))
    cases = pick(m["blocks"])
    if not cases:
        raise SystemExit("검토할 사례가 없다(measure 출력에 검수용 그림이 없으면 최신 도구로 measure를 다시 실행한다)")
    fp = fingerprint(m["meta"], cases)
    key = f"typeset-browser-review:{fp}"
    data = []
    for n, r in enumerate(cases, 1):
        img = r["images"]
        data.append({"id": r["key"], "n": n, "text": r["text"], "role": r["role"],
                     "review": data_uri(run / img["review"]), "scene": data_uri(run / img["scene"]) if img.get("scene") else None,
                     "tool": tool_note(r), "dpr": m["meta"]["dpr"]})
    counts = collections.Counter("full" if r["judgement"] in FULL else "control" for r in cases)
    page = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")) \
        .replace("__KEY__", json.dumps(key)).replace("__FP__", json.dumps(fp)) \
        .replace("__N__", str(len(cases))).replace("__FULL__", str(counts["full"])).replace("__CONTROL__", str(counts["control"])) \
        .replace("__META__", esc(f"입력 {m['meta'].get('inputVariant')} · Chromium {m['meta']['browser']['version']} · 지문 {fp}"))
    out = run / "check"
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    return out / "index.html"


def load_review(run: Path, tsv: Path) -> tuple[list[dict], dict[str, dict]]:
    """TSV를 검증해 읽는다 — 열 · 지문 · 표본 · 판단값 · 중복 중 하나라도 어기면 종료(다른 입력의 검토를 섞지 않는다)."""
    m = json.loads((run / "measure.json").read_text(encoding="utf-8"))
    cases = pick(m["blocks"])
    fp = fingerprint(m["meta"], cases)
    keys = {r["key"] for r in cases}
    with tsv.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    if not rows or set(TSV_COLUMNS) - set(rows[0]):
        raise SystemExit(f"TSV 열이 다르다 — 필요: {TSV_COLUMNS}")
    seen: dict[str, dict] = {}
    for x in rows:
        if x["review_fingerprint"] != fp:
            raise SystemExit(f"지문이 다르다({x['review_fingerprint']} ≠ {fp}) — 다른 측정 결과로 만든 페이지의 검토다")
        if x["key"] not in keys:
            raise SystemExit(f"검토 표본에 없는 사례: {x['key']}")
        if x["verdict"] not in ("inside", "outside", "unsure", ""):
            raise SystemExit(f"판단 값이 다르다: {x['verdict']}")
        if x["key"] in seen:
            raise SystemExit(f"같은 사례가 두 번 있다: {x['key']}")
        seen[x["key"]] = x
    return cases, seen


def aggregate(run: Path, tsv: Path) -> dict:
    cases, seen = load_review(run, tsv)
    C = collections.Counter
    table = C((r["judgement"], (seen.get(r["key"]) or {}).get("verdict") or "unreviewed") for r in cases)
    res = {"cases": len(cases), "reviewed": sum(1 for r in cases if (seen.get(r["key"]) or {}).get("verdict")),
           "note": "사람 판단은 표본에 대한 것이다(전수 아님). 판정 규칙(허용 오차 없음)을 바꾸는 근거가 아니다",
           "table": {f"{j} / {v}": n for (j, v), n in sorted(table.items())},
           "memos": [{"key": k, "verdict": x["verdict"], "note": x["note"]} for k, x in seen.items() if x["note"].strip()]}
    (run / "check").mkdir(exist_ok=True)
    (run / "check" / "review_summary.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>넘침 판정 사람 확인</title><style>
:root{--bg:#f4f5f2;--fg:#1f2326;--card:#fff;--line:#d9dcd6;--mut:#626a70;--in:#2f8f55;--out:#c8362f;--uns:#b7791f;--acc:#24567a}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Malgun Gothic",sans-serif}
header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center}
header h1{font-size:17px;margin:0} .bar{flex:1;min-width:160px;height:8px;background:#e6e8e3;border-radius:4px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--in);width:0} button{font:inherit;cursor:pointer;border:1px solid #b9beb6;background:#fff;border-radius:6px;padding:6px 12px}
button:hover{border-color:var(--acc)} button:focus-visible,input:focus-visible{outline:3px solid #8cb7d6;outline-offset:2px}
main{max-width:1100px;margin:0 auto;padding:14px 16px 40px} details.guide{background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px 16px;margin-bottom:14px}
details.guide summary{cursor:pointer;font-weight:600} .guide ol{margin:8px 0 4px;padding-left:20px} .guide li{margin:4px 0} .mut{color:var(--mut);font-size:13px}
.case{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px}
.top{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between} .top h2{margin:0;font-size:18px}
.q{font-size:18px;font-weight:600;margin:12px 0 4px} .text{white-space:pre-wrap;background:#f1f3ef;border-radius:6px;padding:6px 10px;margin:8px 0 12px;font-size:14px}
.figs{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:14px} figure{margin:0;min-width:0} figcaption{font-size:13px;color:var(--mut);margin-bottom:4px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.zoom button{padding:2px 8px;font-size:12px} .zoom button[aria-pressed=true]{background:var(--acc);color:#fff;border-color:var(--acc)}
.view{border:1px solid var(--line);border-radius:6px;overflow:auto;max-height:58vh;background:repeating-conic-gradient(#eef0ec 0% 25%,#fafbf9 0% 50%) 0/16px 16px}
.view img{display:block;image-rendering:pixelated;margin:0} .scene img{width:100%;image-rendering:auto}
.answers{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 8px} .answers button{flex:1;min-width:150px;padding:12px;font-size:16px;font-weight:600}
.answers button kbd{font:12px Consolas,monospace;border:1px solid currentColor;border-radius:4px;padding:0 5px;margin-left:6px;opacity:.7}
.answers .inside[aria-pressed=true]{background:var(--in);border-color:var(--in);color:#fff} .answers .outside[aria-pressed=true]{background:var(--out);border-color:var(--out);color:#fff}
.answers .unsure[aria-pressed=true]{background:var(--uns);border-color:var(--uns);color:#fff}
.memo{display:flex;gap:8px;align-items:center} .memo input{flex:1;font:inherit;padding:6px 8px;border:1px solid #b9beb6;border-radius:6px}
details.tool{margin-top:10px;font-size:13.5px} details.tool summary{cursor:pointer;color:var(--mut)} .toolbox{background:#f3f5fa;border:1px dashed #9fb0d0;border-radius:6px;padding:8px 12px;margin-top:6px}
.saved{color:var(--in);font-size:13px;min-width:60px} .nav{margin-top:16px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px 14px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px} .chips button{width:38px;padding:4px 0;font-size:13px}
.chips .inside{background:#dff1e5;border-color:var(--in)} .chips .outside{background:#f8dedc;border-color:var(--out)} .chips .unsure{background:#f6ead2;border-color:var(--uns)}
.chips .cur{outline:3px solid var(--acc)} .legend span{display:inline-block;margin-right:12px}
@media(max-width:760px){.figs{grid-template-columns:1fr} header h1{font-size:15px}}
</style></head><body>
<header><h1>넘침 판정 사람 확인</h1><span id="cnt">0 / __N__</span><div class="bar"><i id="bar"></i></div>
<button id="next-todo">다음 미검토 ⏭</button><button id="exp">결과 내려받기(TSV)</button></header>
<main>
<details class="guide" id="guide" open><summary>무엇을 하나요? (처음 한 번 읽어 주세요)</summary>
<ol>
<li><b>할 일</b> — 사례마다 질문 하나에 답합니다: <b>검정 글자가 빨간 선 안쪽에 들어가나요?</b> 빨간 선 안쪽이 번역문을 놓을 배치 영역입니다.</li>
<li><b>왜</b> — 측정 도구가 "넘침"이라고 했지만 그림으로는 영역 안처럼 보이는 경계 사례 __FULL__건이 있습니다. 측정 오차인지 실제 넘침인지는
사람 눈으로 가려야 합니다. 비교를 위해 도구가 확실히 넘침 · 정상으로 본 사례 __CONTROL__건을 섞었습니다(순서 무작위).</li>
<li><b>판단 기준</b> — 글자가 선에 <b>닿기만</b> 하면 "안에 들어감", 선을 넘어 <b>한 픽셀이라도 바깥</b>에 보이면 "밖으로 나감",
확대해도 모르겠으면 "판단 불가". 줄 간격 · 정렬 · 글자 크기의 적절함은 이번 질문이 아닙니다(필요하면 메모).</li>
<li><b>그림</b> — ① 판단용: 글자를 검정으로 바꿔 흰 바탕에 찍은 것(위치 · 줄바꿈은 실제와 같음). 배율은 사례마다 자동으로 맞춰집니다. 큰 그림은 "맞춤"(칸 폭에 맞춰 축소)으로 시작하니, 선 가까운 곳은 2 · 4 · 8배로 키워 확인하세요(픽셀 그대로 확대).
② 실제 화면: 배경 · 글자색 그대로(무엇인지 알아보는 용도).</li>
<li><b>조작</b> — 키보드 <b>1</b> 안 · <b>2</b> 밖 · <b>3</b> 판단 불가, <b>←/→</b> 이전/다음. 답하면 다음 미검토 사례로 넘어갑니다.
입력은 이 브라우저에만 저장됩니다 — 끝나면 <b>결과 내려받기(TSV)</b>를 눌러 파일을 전달해 주세요.</li>
</ol><p class="mut">"도구 판정 보기"는 답한 뒤에 펼치기를 권합니다(먼저 보면 판단이 쏠립니다). · __META__</p></details>
<section class="case" id="case" aria-live="polite"></section>
<nav class="nav"><b>사례 이동</b> <span class="legend mut"><span>■ 회색 미검토</span><span style="color:var(--in)">■ 안</span><span style="color:var(--out)">■ 밖</span><span style="color:var(--uns)">■ 판단 불가</span></span>
<div class="chips" id="chips"></div></nav>
</main>
<script>
const DATA=__DATA__, K=__KEY__, FP=__FP__, LABEL={inside:'안에 들어감',outside:'밖으로 나감',unsure:'판단 불가'};
let st={}; try{st=JSON.parse(localStorage.getItem(K)||'{}')||{}}catch(e){st={}}
let cur=0, zoom=2; const ZOOMS=['fit',1,2,4,8];
// 사례마다 그림이 판단하기 좋은 크기로 시작한다 — 칸보다 크면 맞춤, 아니면 칸(약 640px)에 들어가는 가장 큰 배율
function autoZoom(im){const w=im.naturalWidth/DATA[cur].dpr, box=im.parentElement.clientWidth-2; if(w>box)return 'fit'; return [1,2,4,8].filter(z=>z===1||w*z<=Math.min(640,box)).pop()}
const $=s=>document.querySelector(s), esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function save(){try{localStorage.setItem(K,JSON.stringify(st))}catch(e){}; chips(); progress()}
function done(id){return !!(st[id]&&st[id].v)}
function progress(){const n=DATA.filter(d=>done(d.id)).length; $('#cnt').textContent=n+' / '+DATA.length+' 검토'; $('#bar').style.width=(100*n/DATA.length)+'%'}
function chips(){$('#chips').innerHTML=DATA.map((d,i)=>{const v=(st[d.id]||{}).v||''; return `<button class="${v}${i===cur?' cur':''}" data-i="${i}" title="사례 ${i+1}${v?' — '+LABEL[v]:''}">${i+1}</button>`}).join('');
  document.querySelectorAll('#chips button').forEach(b=>b.onclick=()=>show(+b.dataset.i))}
function setZoom(z){zoom=z; const im=$('#rv'); if(im&&im.naturalWidth){im.style.width=z==='fit'?'100%':(im.naturalWidth/DATA[cur].dpr*z)+'px';
  im.style.imageRendering=z==='fit'?'auto':'pixelated'}
  document.querySelectorAll('.zoom button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.z===String(z)))}
function show(i){cur=(i+DATA.length)%DATA.length; const d=DATA[cur], v=st[d.id]||{};
  $('#case').innerHTML=`<div class="top"><h2>사례 ${d.n} / ${DATA.length}</h2><div><button id="prev">◀ 이전</button> <button id="next">다음 ▶</button></div></div>
  <p class="q">검정 글자가 빨간 선 안쪽에 들어가나요?</p><div class="text">${esc(d.text)}</div>
  <div class="figs"><figure><figcaption>① 판단용 — 검정 글자 · 흰 바탕 <span class="zoom">${ZOOMS.map(z=>`<button data-z="${z}">${z==='fit'?'맞춤':z+'배'}</button>`).join('')}</span></figcaption>
  <div class="view"><img id="rv" src="${d.review}" alt="판단용 그림"></div></figure>
  ${d.scene?`<figure class="scene"><figcaption>② 실제 화면 — 배경 · 글자색 그대로</figcaption><div class="view"><img src="${d.scene}" alt="실제 화면"></div></figure>`:''}</div>
  <div class="answers">${[['inside',1],['outside',2],['unsure',3]].map(([k,n])=>`<button class="${k}" data-v="${k}" aria-pressed="${v.v===k}">${LABEL[k]}<kbd>${n}</kbd></button>`).join('')}</div>
  <div class="memo"><input id="memo" placeholder="메모(선택) — 예: g 꼬리만 선을 1px 넘음 · 오른쪽 끝 글자가 선에 닿음" value="${esc(v.n||'')}"><span class="saved" id="saved"></span></div>
  <details class="tool"><summary>도구 판정 보기(답한 뒤 권장)</summary><div class="toolbox">${d.tool}</div></details>`;
  $('#prev').onclick=()=>show(cur-1); $('#next').onclick=()=>show(cur+1);
  document.querySelectorAll('.zoom button').forEach(b=>b.onclick=()=>setZoom(b.dataset.z==='fit'?'fit':+b.dataset.z));
  const im=$('#rv'); if(im.complete)setZoom(autoZoom(im)); else im.onload=()=>setZoom(autoZoom(im));
  document.querySelectorAll('.answers button').forEach(b=>b.onclick=()=>answer(b.dataset.v));
  $('#memo').oninput=e=>{(st[d.id]=st[d.id]||{}).n=e.target.value; save(); $('#saved').textContent='저장됨'};
  chips()}
function answer(val){const d=DATA[cur]; (st[d.id]=st[d.id]||{}).v=val; save();
  document.querySelectorAll('.answers button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.v===val)); $('#saved').textContent='저장됨';
  const nx=DATA.findIndex((x,i)=>i>cur&&!done(x.id)), first=DATA.findIndex(x=>!done(x.id));
  const to=nx>=0?nx:first; if(to>=0&&to!==cur)setTimeout(()=>show(to),350); else if(to<0)$('#saved').textContent='모두 검토함 — 결과를 내려받아 주세요'}
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT')return; if(e.key==='1')answer('inside'); else if(e.key==='2')answer('outside');
  else if(e.key==='3')answer('unsure'); else if(e.key==='ArrowLeft')show(cur-1); else if(e.key==='ArrowRight')show(cur+1)});
$('#next-todo').onclick=()=>{const i=DATA.findIndex((x,i)=>i>cur&&!done(x.id)); const j=i>=0?i:DATA.findIndex(x=>!done(x.id)); if(j>=0)show(j)};
$('#exp').onclick=()=>{let t='case_no\tkey\tverdict\tnote\treview_fingerprint\n'; DATA.forEach(d=>{const v=st[d.id]; if(!v||!(v.v||v.n))return;
  t+=[d.n,d.id,v.v||'',(v.n||'').replace(/[\t\n]/g,' '),FP].join('\t')+'\n'});
  const u=URL.createObjectURL(new Blob([t],{type:'text/tab-separated-values'})); const a=document.createElement('a'); a.href=u; a.download='typeset_browser_review_'+FP+'.tsv'; a.click()};
if(DATA.some(d=>done(d.id)))$('#guide').open=false;
const firstTodo=DATA.findIndex(x=>!done(x.id)); show(firstTodo>=0?firstTodo:0); progress();
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("page")
    a.add_argument("--run", type=Path, required=True)
    a = sub.add_parser("aggregate")
    a.add_argument("--run", type=Path, required=True)
    a.add_argument("--tsv", type=Path, required=True)
    args = ap.parse_args()
    if args.cmd == "page":
        print(f"검토 페이지: {build_page(args.run)}")
    else:
        print(json.dumps(aggregate(args.run, args.tsv), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
