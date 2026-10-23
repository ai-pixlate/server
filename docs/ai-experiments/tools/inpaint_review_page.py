"""⑥ 인페인팅 실측 결과의 사용자 검수 페이지 생성(2026-09-30).

    python docs/ai-experiments/tools/inpaint_review_page.py --run pipeline/out/inpaint-run100-v1_2026-09-30 \
        --input pipeline/samples/local/downstream-input-v1 --out pipeline/out/inpaint-run100-v1_2026-09-30/review         [--auto docs/ai-experiments/runs/<...>/auto_screen_v1.json] [--ai docs/ai-experiments/runs/<...>/ai_first_pass_v1.json]

출력: <out>/index.html · <out>/img/. 실측 산출물(inpaint/ · inpaint_bg/ · inpaint_mask/ · inpaint_debug/)과 입력본은 읽기만 한다.
- 섹션 카드(MANIFEST 순서): 원본 · 마스크 겹침(빨강 = 최종 삭제, 파랑 = 보호) · 결과 썸네일과 수치(크기 · 상태 · 최종 마스크 · 보호 충돌).
  썸네일을 누르면 원본 해상도 비교 창 — 1 원본 · 2 결과 · 3 겹침 · 4 대비 강조(결과를 섹션 중앙값 기준 ×4, 옅은 색조 차이 확인용),
  Space로 원본 ↔ 결과 전환, ← → 로 이전 · 다음 섹션, 배율 버튼.
- 참고 태그는 문서에 기록된 사례 번호(#39 · #67 · #69)와 빈 마스크 표시일 뿐 판정이 아니다.
- 사용자 검수(결론 · 관찰 유형 · 메모)는 브라우저에만 저장(localStorage)되고 "TSV 내보내기"로 받는다. **미리 선택된 값은 없다.**
  저장 키는 실행 기록 · 입력본 · 결과 파일 지문별(`inpaint-review:<지문 16자>`)이라 다른 실행의 검수값을 자동으로 불러오지 않는다.
- 참고 칸(선택): 자동 선별 지표 요약(`inpaint_auto_screen.py`)과 AI 1차 분류(우선순위 · 관찰 유형 · 메모 · 근거). 사용자 입력칸과 분리해
  글로만 보이고 입력값을 채우지 않는다. AI 분류는 사람 판단이 아니며, 후보가 없던 섹션은 "AI 육안 미확인"으로 표시한다.
이 페이지는 정답이 아니다. 자동 검사(크기 · 보호 교집합 · 마스크 밖 동일)는 실측 도구가 이미 했고, 여기서는 사람이 품질을 본다.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import jsonio  # noqa: E402
from pipeline.types import InpaintResult  # noqa: E402

VERDICTS = [("usable", "사용 가능"), ("minor", "경미한 흔적(사용 가능)"), ("fix", "수정 필요"), ("unusable", "사용 불가"), ("unsure", "판단 불가")]
TAGS = [
    ("residual_text", "잔여 글자(지워야 할 글자가 남음)"),
    ("smudge", "얼룩 · 번짐 · 색조 차이"),
    ("design_damage", "배경 · 디자인 요소 훼손(상자 · 형광펜 · 선 · 사진)"),
    ("protect_fragment", "보호로 남은 조각 · 과보호"),
    ("label_logo_erased", "라벨 · 로고가 지워짐(상류 #67 · #69 등)"),
    ("doc_39", "문서 삽입 글자(#39) 처리 문제"),
    ("other", "기타(메모)"),
]
KNOWN = {  # 문서에 기록된 사례(판정 아님): open-questions #39 · #67 · #69, 2026-09-30_06-inpaint_lama-gpu-probe.md
    ("GS-02_008", "sec_1_04"): ["#39 논문 캡처"], ("GS-03_014", "sec_1_01"): ["#39 보고서 캡처"],
    ("GS-03_018", "sec_1_08"): ["#67 반사상", "#69 비클리닉스 병합"], ("GS-02_001", "sec_1_02"): ["#67 기프트카드"],
    ("GS-01_002", "sec_1_23"): ["#69 goodal 병합"],
}
THUMB_W = 300
TAG_NAMES = {"residual_text": "잔여 글자", "smudge": "얼룩 · 색조", "design_damage": "디자인 훼손", "protect_fragment": "보호 조각",
             "label_logo_erased": "라벨 · 로고 지워짐", "doc_39": "#39 문서", "other": "기타"}
PRI_NAMES = {"high": "AI 우선 높음", "medium": "AI 우선 보통", "low": "AI 우선 낮음", "none": "AI: 후보 확인 · 문제 못 봄",
             "unreviewed": "AI 육안 미확인"}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def overlay(orig: np.ndarray, final: np.ndarray, protect: np.ndarray) -> np.ndarray:
    o = orig.astype(np.float32)
    out = o.copy()
    red, blue = np.array([255, 0, 0], np.float32), np.array([0, 90, 255], np.float32)
    out[final] = o[final] * 0.45 + red * 0.55
    out[protect] = o[protect] * 0.45 + blue * 0.55
    return out.astype(np.uint8)


def enhance(img: np.ndarray) -> np.ndarray:
    a = img.astype(np.float32)
    med = np.median(a.reshape(-1, 3), axis=0)
    return np.clip((a - med) * 4 + 128, 0, 255).astype(np.uint8)


def thumb(img: Image.Image, path: Path) -> None:
    t = img.convert("RGB")
    t = t.resize((THUMB_W, max(1, round(t.height * THUMB_W / t.width))), Image.LANCZOS)
    t.save(path, quality=85)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="execute_inpaint 출력 폴더(run.json · <image>/inpaint*)")
    ap.add_argument("--input", required=True, help="고정 입력본(MANIFEST.json · <image>/split.json · sections/)")
    ap.add_argument("--out", required=True, help="새 폴더(이미 있으면 거부)")
    ap.add_argument("--auto", help="자동 선별 결과 JSON(inpaint_auto_screen.py) — 참고 칸")
    ap.add_argument("--ai", help="AI 1차 분류 JSON — 참고 칸")
    a = ap.parse_args(argv)
    auto = json.loads(Path(a.auto).read_text(encoding="utf-8"))["sections"] if a.auto else {}
    ai_doc = json.loads(Path(a.ai).read_text(encoding="utf-8")) if a.ai else {}
    ai = ai_doc.get("sections", {})
    run, inp, out = Path(a.run), Path(a.input), Path(a.out)
    if out.exists():
        print(f"출력 폴더가 이미 있다: {out}", file=sys.stderr)
        return 2
    rec = json.loads((run / "run.json").read_text(encoding="utf-8"))
    manifest = json.loads((inp / "MANIFEST.json").read_text(encoding="utf-8"))
    order = [(im["image"], s["section_key"]) for im in manifest["images"] for s in im["sections"]]
    by_key = {(s["image_id"], s["section_key"]): s for s in rec["sections"]}
    (out / "img").mkdir(parents=True)
    fp_src = hashlib.sha256()
    fp_src.update(sha(run / "run.json").encode())
    fp_src.update(sha(inp / "SHA256SUMS").encode())
    cards, ids = [], []
    for image_id, key in order:
        entry = by_key.get((image_id, key))
        sid = f"{image_id}|{key}"
        ids.append(sid)
        if entry is None or entry.get("status") != "ok":
            cards.append(f'<section class="card" id="{esc(sid)}"><h3>{esc(image_id)} / {esc(key)}</h3>'
                         f'<p class="bad">실행 결과 없음 또는 실패(status={esc(entry and entry.get("status"))})</p></section>')
            continue
        res = jsonio.load_model(run / entry["result"], InpaintResult)
        fp_src.update(sha(run / entry["result"]).encode())
        split = jsonio.load_split(inp / image_id / "split.json")
        section = next(s for s in split.sections if s.section_key == key)
        with Image.open(section.image_path) as im:
            orig = np.array(im.convert("RGB"))
        final = np.array(Image.open(run / res.files.final_mask)) == 255
        protect = np.array(Image.open(run / res.files.protect_mask)) == 255
        bg_path = run / res.files.background
        base = f"{image_id}__{key}"
        shutil.copyfile(section.image_path, out / "img" / f"{base}.orig.png")
        shutil.copyfile(bg_path, out / "img" / f"{base}.res.png")
        Image.fromarray(overlay(orig, final, protect)).save(out / "img" / f"{base}.ovl.png")
        with Image.open(bg_path) as b:
            bg = np.array(b.convert("RGB"))
        Image.fromarray(enhance(bg)).save(out / "img" / f"{base}.enh.jpg", quality=90)
        for kind, arr in (("orig", orig), ("ovl", None), ("res", bg)):
            src = Image.fromarray(arr) if arr is not None else Image.open(out / "img" / f"{base}.ovl.png")
            thumb(src, out / "img" / f"{base}.{kind}.t.jpg")
        c = res.counts
        tags = KNOWN.get((image_id, key), []) + (["빈 마스크(모델 호출 없음)"] if res.status == "unchanged" else [])
        h, w = orig.shape[:2]
        call = json.loads((run / entry["debug"]).read_text(encoding="utf-8")).get("model_call") or {}
        meta = (f"{w}×{h} · {esc(res.status)} · 최종 마스크 {c.final_px:,}px({c.final_px / (w * h):.0%}) · 보호 충돌 {c.conflict_px:,}px · "
                f"대상 영역 {c.target} · 보호 영역 {c.protected_region} · 보호 블록 {c.protected_blocks}"
                + (f" · 추론 호출 #{call.get('n')} {call.get('roundtrip_s')}s" if call else ""))
        verdicts = "".join(f'<label><input type="radio" name="v_{esc(sid)}" value="{v}">{esc(t)}</label>' for v, t in VERDICTS)
        tagboxes = "".join(f'<label><input type="checkbox" data-tag="{t}">{esc(n)}</label>' for t, n in TAGS)
        imgs = "".join(
            f'<figure><figcaption>{cap}</figcaption><img loading="lazy" src="img/{base}.{k}.t.jpg" data-sid="{esc(sid)}" data-view="{k}" alt="{cap}"></figure>'
            for k, cap in (("orig", "원본"), ("ovl", "겹침(빨강 삭제 · 파랑 보호)"), ("res", "결과")))
        ref, ai_pri, ai_tags = "", "", []
        if a.auto or a.ai:
            au = auto.get(sid) or {}
            cc = au.get("candidate_counts") or {}
            auto_txt = (" · ".join(f"{TAG_NAMES.get(k, k)} {v}" for k, v in cc.items() if v) or "후보 없음") if au else "지표 없음"
            e = ai.get(sid)
            ai_pri = e["priority"] if e else "unreviewed"
            ai_tags = e["tags"] if e else []
            ai_txt = (f'<span class="pri {ai_pri}">{esc(PRI_NAMES[ai_pri])}</span> '
                      + " ".join(f'<span class="chip">{esc(TAG_NAMES.get(t, t))}</span>' for t in ai_tags)
                      + (f' — {esc(e["note"])} <span class="basis">({esc(e["basis"])})</span>' if e else ' — 자동 후보 없음 · AI 육안 미확인'))
            ref = (f'<div class="ref"><div><b>참고 · 자동 후보</b> {esc(auto_txt)}</div>'
                   f'<div><b>참고 · AI 1차 분류</b> {ai_txt}</div></div>')
        cards.append(
            f'<section class="card" id="{esc(sid)}" data-sid="{esc(sid)}" data-base="{base}" data-w="{w}" data-h="{h}" '
            f'data-aipri="{ai_pri}" data-aitags="{" ".join(ai_tags)}">'
            f'<h3>{esc(image_id)} / {esc(key)} {"".join(f"<span class=tag>{esc(t)}</span>" for t in tags)}</h3>'
            f'<p class="meta">{meta}</p><div class="imgs">{imgs}</div>{ref}'
            f'<div class="form"><div class="row"><b>결론</b>{verdicts}</div><div class="row"><b>관찰</b>{tagboxes}</div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="위치 · 내용(예: 제목 오른쪽 옅은 네모)"></div></div></section>')
    fp = fp_src.hexdigest()[:16]
    key = f"inpaint-review:{fp}"
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⑥ 인페인팅 검수 · 100섹션 v1</title><style>
:root{{--bg:#f6f6f4;--fg:#1d1d1f;--card:#fff;--line:#ddd;--mut:#666;--acc:#2a62d9}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Malgun Gothic",sans-serif}}
header{{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
header h1{{font-size:16px;margin:0 12px 0 0}} header button,header select{{font:inherit;padding:4px 10px}}
main{{padding:12px 16px;max-width:1200px;margin:0 auto}} .intro{{background:#fff;border:1px solid var(--line);padding:10px 14px;margin-bottom:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 14px;margin:0 0 14px}}
.card.done{{border-left:5px solid #3a9d5d}} .card h3{{margin:0 0 4px;font-size:15px}} .meta{{margin:0 0 8px;color:var(--mut);font-size:12.5px}}
.tag{{display:inline-block;background:#fff3cd;border:1px solid #e5c76b;border-radius:10px;padding:0 8px;margin-left:6px;font-size:12px;font-weight:normal}}
.imgs{{display:flex;gap:10px;overflow-x:auto}} figure{{margin:0;flex:0 0 auto}} figcaption{{font-size:12px;color:var(--mut)}}
.imgs img{{width:{THUMB_W}px;max-height:520px;object-fit:cover;object-position:top;border:1px solid var(--line);cursor:zoom-in;display:block}}
.form .row{{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center}} .form b{{width:36px}} .note{{flex:1;min-width:260px;padding:4px}}
.bad{{color:#b00020}} .ref{{background:#f3f5fb;border:1px dashed #9fb0d9;border-radius:4px;padding:6px 10px;margin-top:8px;font-size:12.5px}}
.ref b{{color:#34518f;margin-right:6px}} .chip{{display:inline-block;border:1px solid #b9c4e0;border-radius:10px;padding:0 7px;margin-right:3px;background:#fff}}
.pri{{display:inline-block;border-radius:3px;padding:0 6px;color:#fff;background:#888}} .pri.high{{background:#c0392b}} .pri.medium{{background:#d68910}}
.pri.low{{background:#7f8c8d}} .pri.none{{background:#27ae60}} .pri.unreviewed{{background:#bbb;color:#333}} .basis{{color:var(--mut)}} #modal{{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;z-index:10;flex-direction:column}}
#modal.on{{display:flex}} #mbar{{display:flex;gap:8px;align-items:center;padding:8px 12px;background:#222;color:#eee;flex-wrap:wrap}}
#mbar button{{font:inherit;padding:3px 10px}} #mbar button.on{{background:var(--acc);color:#fff}} #mwrap{{flex:1;overflow:auto;text-align:center}}
#mimg{{display:inline-block;image-rendering:auto}} .help{{color:#aaa;font-size:12px}}
</style></head><body>
<header><h1>⑥ 인페인팅 검수 · 100섹션 v1</h1><span id="cnt"></span>
<label>보기 <select id="flt"><option value="all">전체</option><option value="todo">미검수</option><option value="done">검수함</option>
{"".join(f'<option value="v:{v}">결론: {esc(t)}</option>' for v, t in VERDICTS)}{"".join(f'<option value="t:{t}">관찰: {esc(n)}</option>' for t, n in TAGS)}
<option value="known">참고 태그 있음</option>
{"".join(f'<option value="p:{k}">{esc(v)}</option>' for k, v in PRI_NAMES.items()) if (a.auto or a.ai) else ""}
{"".join(f'<option value="a:{k}">AI 관찰: {esc(v)}</option>' for k, v in TAG_NAMES.items()) if (a.auto or a.ai) else ""}</select></label>
<button id="exp">TSV 내보내기</button><span class="help">저장 키 {esc(key)}</span></header>
<main><div class="intro"><b>검수 방법</b> — 썸네일을 누르면 원본 해상도로 봅니다. <b>1</b> 원본 · <b>2</b> 결과 · <b>3</b> 겹침 · <b>4</b> 대비 강조(옅은 색조 차이),
<b>Space</b> 원본↔결과 전환, <b>← →</b> 이전·다음 섹션, <b>Esc</b> 닫기. 결론 1개 + 관찰 유형(여러 개) + 메모를 남기고 끝나면 TSV로 내보내세요.
참고 태그는 문서에 기록된 사례 번호일 뿐 판정이 아닙니다. 미리 선택된 값은 없습니다.
{('<br><b>참고 칸</b>(점선 상자) — 자동 후보 수와 AI 1차 분류입니다. <b>AI 분류는 사람 판단이 아니며</b> 입력칸을 채우지 않습니다. '
  'AI는 자동 후보 영역의 확대 이미지와 앞선 13섹션 검토만 봤고, 후보가 없던 섹션은 "AI 육안 미확인"입니다. '
  '"보기"에서 AI 우선순위 · 관찰 유형으로 거를 수 있습니다.') if (a.auto or a.ai) else ''} 입력은 이 브라우저에만 저장됩니다.
실행: {esc(rec.get("started_at"))} · 상태 {esc(rec.get("status"))} · 커밋 {esc((rec.get("git_commit") or "")[:7])}(dirty={esc(rec.get("git_dirty"))}) · 지문 {fp}</div>
{"".join(cards)}</main>
<div id="modal"><div id="mbar"><b id="mtitle"></b>
<button data-v="orig">1 원본</button><button data-v="res">2 결과</button><button data-v="ovl">3 겹침</button><button data-v="enh">4 대비 강조</button>
<span>배율</span><button data-z="0.5">50%</button><button data-z="1">100%</button><button data-z="2">200%</button><button data-z="fit">폭 맞춤</button>
<button id="prev">← 이전</button><button id="next">다음 →</button><button id="close">닫기(Esc)</button><span class="help" id="mhint"></span></div>
<div id="mwrap"><img id="mimg" alt=""></div></div>
<script>
const K={json.dumps(key)},FP={json.dumps(fp)},IDS={json.dumps(ids)},TAGS={json.dumps([t for t, _ in TAGS])};
let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}};count()}}
function done(v){{return v&&(v.v||(v.t&&v.t.length)||v.n)}}
function count(){{let n=0;for(const id of IDS){{const c=document.getElementById(id);const d=done(st[id]);if(d)n++;if(c)c.classList.toggle('done',!!(st[id]&&st[id].v))}}
document.getElementById('cnt').textContent='검수 '+n+' / '+IDS.length}}
for(const card of document.querySelectorAll('.card[data-sid]')){{const id=card.dataset.sid,v=st[id]||{{}};
card.querySelectorAll('input[type=radio]').forEach(r=>{{if(v.v===r.value)r.checked=true;r.onchange=()=>{{(st[id]=st[id]||{{}}).v=r.value;save()}}}});
card.querySelectorAll('input[data-tag]').forEach(c=>{{if((v.t||[]).includes(c.dataset.tag))c.checked=true;c.onchange=()=>{{const s=(st[id]=st[id]||{{}});
s.t=[...card.querySelectorAll('input[data-tag]:checked')].map(x=>x.dataset.tag);save()}}}});
const note=card.querySelector('.note');note.value=v.n||'';note.oninput=()=>{{(st[id]=st[id]||{{}}).n=note.value;save()}}}}
document.getElementById('flt').onchange=e=>{{const f=e.target.value;for(const c of document.querySelectorAll('.card')){{const v=st[c.id]||{{}};let show=true;
if(f==='todo')show=!done(v);else if(f==='done')show=!!done(v);else if(f.startsWith('v:'))show=v.v===f.slice(2);else if(f.startsWith('t:'))show=(v.t||[]).includes(f.slice(2));
else if(f==='known')show=!!c.querySelector('.tag');else if(f.startsWith('p:'))show=c.dataset.aipri===f.slice(2);
else if(f.startsWith('a:'))show=(c.dataset.aitags||'').split(' ').includes(f.slice(2));c.style.display=show?'':'none'}}}};
document.getElementById('exp').onclick=()=>{{let t='source\\tsection\\tverdict\\ttags\\tnote\\treview_fingerprint\\n';for(const id of IDS){{const v=st[id];if(!done(v))continue;
const[a,b]=id.split('|');t+=[a,b,v.v||'',(v.t||[]).join(','),(v.n||'').replace(/[\\t\\n]/g,' '),FP].join('\\t')+'\\n'}}
const u=URL.createObjectURL(new Blob([t],{{type:'text/tab-separated-values'}}));const x=document.createElement('a');x.href=u;x.download='inpaint_review_'+FP+'.tsv';x.click()}};
const M=document.getElementById('modal'),MI=document.getElementById('mimg'),cards=[...document.querySelectorAll('.card[data-base]')];let ci=0,view='res',zoom='fit',alt='orig';
function show(){{const c=cards[ci];MI.src='img/'+c.dataset.base+'.'+view+(view==='enh'?'.jpg':'.png');document.getElementById('mtitle').textContent=c.dataset.sid.replace('|',' / ')+' ('+(ci+1)+'/'+cards.length+')';
document.querySelectorAll('#mbar [data-v]').forEach(b=>b.classList.toggle('on',b.dataset.v===view));document.querySelectorAll('#mbar [data-z]').forEach(b=>b.classList.toggle('on',b.dataset.z===String(zoom)));
MI.style.width=zoom==='fit'?'min(100%,'+c.dataset.w*2+'px)':(c.dataset.w*zoom)+'px';document.getElementById('mhint').textContent=c.dataset.w+'×'+c.dataset.h}}
document.querySelectorAll('.imgs img').forEach(im=>im.onclick=()=>{{ci=cards.findIndex(c=>c.dataset.sid===im.dataset.sid);view=im.dataset.view;M.classList.add('on');show()}});
document.querySelectorAll('#mbar [data-v]').forEach(b=>b.onclick=()=>{{view=b.dataset.v;show()}});document.querySelectorAll('#mbar [data-z]').forEach(b=>b.onclick=()=>{{zoom=b.dataset.z==='fit'?'fit':+b.dataset.z;show()}});
document.getElementById('prev').onclick=()=>{{ci=(ci+cards.length-1)%cards.length;show()}};document.getElementById('next').onclick=()=>{{ci=(ci+1)%cards.length;show()}};
document.getElementById('close').onclick=()=>M.classList.remove('on');
document.addEventListener('keydown',e=>{{if(!M.classList.contains('on')||e.target.tagName==='INPUT')return;const m={{'1':'orig','2':'res','3':'ovl','4':'enh'}};
if(m[e.key]){{view=m[e.key];show()}}else if(e.key===' '){{e.preventDefault();view=view==='orig'?'res':'orig';show()}}else if(e.key==='ArrowRight'){{ci=(ci+1)%cards.length;show()}}
else if(e.key==='ArrowLeft'){{ci=(ci+cards.length-1)%cards.length;show()}}else if(e.key==='Escape')M.classList.remove('on')}});
count();
</script></body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"검수 페이지: {out / 'index.html'} · 섹션 {len(ids)} · 저장 키 {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
