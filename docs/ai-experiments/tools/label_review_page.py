"""④ 실측 결과를 이미지와 대조해 검수하는 페이지 생성(사용자 검수용, 2026-09-29).

    python docs/ai-experiments/tools/label_review_page.py --run pipeline/out/label-run-v1_<date> --input pipeline/samples/local/blocks-input-v1 \
        [--ai-review docs/ai-experiments/runs/<...>/ai_review.tsv] --out pipeline/out/label-run-v1_<date>/review

출력: <out>/index.html · <out>/img/(오버레이 사본). 섹션 카드마다 오버레이(true 빨강 · false 초록 · 공백 규칙 회색 점선 · 실패 회색 테두리)와
블록 표(키 · 역할 · 원문 · 판정 · AI 검토 분류). 블록마다 사용자 검수 분류를 고르면 브라우저에만 저장되고(localStorage) "TSV 내보내기"로 받는다.
분류는 검수 명세(2026-09-29_04-label_review-spec-v1.md)와 같다. 카드 순서: 실행 실패 → AI 검토 분류가 ok가 아닌 블록이 있는 섹션 → 긴 섹션(보낸 이미지 폭 400px 미만) → 나머지.
이 페이지는 정답이 아니며 AI 검토 분류는 사람 판단이 아니다.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import jsonio  # noqa: E402
from pipeline.types import LabelResult  # noqa: E402

CLASSES = [
    ("ok", "맞음"),
    ("miss", "라벨 미탐(패키지 글자인데 false)"),
    ("overprotect", "일반 문구 과보호(페이지 글자인데 true)"),
    ("mixed", "혼합 블록 — 상류 과병합 영향(true로 보호)"),
    ("unsure", "판단 불가"),
]


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def load_ai(path: Path | None) -> dict[tuple[str, str, str], dict[str, str]]:
    if path is None or not path.exists():
        return {}
    out = {}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            out[(r["source"], r["section"], r["block"])] = r
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--ai-review", default=None)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    run, inp, out = Path(a.run), Path(a.input), Path(a.out)
    (out / "img").mkdir(parents=True, exist_ok=True)
    rows = json.loads((run / "sections.json").read_text(encoding="utf-8"))
    ai = load_ai(Path(a.ai_review) if a.ai_review else None)

    cards = []
    for r in rows:
        src, key = r["source"], r["section"]
        merged = jsonio.load_merge(inp / src / "merge" / f"{key}.json")
        lp = run / src / "label" / f"{key}.json"
        res = jsonio.load_model(lp, LabelResult) if lp.exists() else None
        dec = {d.block_key: d for d in (res.labels or [])} if res else {}
        ov = run / src / "overlay" / f"{key}.png"
        img_rel = None
        if ov.exists():
            img_rel = f"img/{src}__{key}.png"
            shutil.copyfile(ov, out / img_rel)
        disagree = sum(1 for b in merged.blocks if ai.get((src, key, b.block_key), {}).get("class") not in (None, "", "ok"))
        narrow = bool(r.get("sent_size")) and r["sent_size"][0] < 400
        prio = 0 if r.get("status") != "ok" else (1 if disagree else (2 if narrow else 3))
        trs = []
        for b in merged.blocks:
            d = dec.get(b.block_key)
            if d is None:
                verdict, color = ("미판정(실패)" if res is None or res.labels is None else "판정 없음"), "#777"
            elif d.basis == "blank_text":
                verdict, color = "false (공백 · 미전송)", "#999"
            else:
                verdict, color = ("true (라벨)", "#dc2828") if d.is_product_label else ("false", "#28a03c")
            airow = ai.get((src, key, b.block_key), {})
            ai_txt = f"{airow.get('class', '')} {airow.get('note', '')}".strip()
            bid = f"{src}|{key}|{b.block_key}"
            opts = "".join(f'<option value="{c}">{esc(t)}</option>' for c, t in [("", "—")] + CLASSES)
            trs.append(
                f'<tr><td>{esc(b.block_key)}</td><td>{esc(b.role)}</td><td class="t">{esc(b.source_ko)}</td>'
                f'<td style="color:{color};font-weight:600">{esc(verdict)}</td><td class="ai">{esc(ai_txt)}</td>'
                f'<td><select data-id="{esc(bid)}">{opts}</select> <input data-note="{esc(bid)}" placeholder="메모"></td></tr>'
            )
        meta = (f"상태 {esc(r.get('status'))} · 블록 {r['blocks']} (보냄 {r['sent']} · 공백 {r['blank']}) · true {esc(r.get('true'))} · "
                f"원본 {r['size'][0]}x{r['size'][1]} → 보낸 이미지 {esc(r.get('sent_size'))} · 호출 {esc(r.get('call_duration_s'))}s"
                + (f" · 오류 {esc(r.get('error'))}" if r.get("error") else ""))
        img_html = f'<a href="{img_rel}" target="_blank"><img src="{img_rel}" loading="lazy"></a>' if img_rel else "<p>오버레이 없음</p>"
        cards.append((prio, src, key, f'<section class="card p{prio}" id="{esc(src)}__{esc(key)}"><h2>{esc(src)} / {esc(key)}'
                      f'{" · AI 검토 ok 아님 " + str(disagree) if disagree else ""}{" · 긴 섹션" if narrow else ""}</h2><p class="meta">{meta}</p>'
                      f'<div class="row"><div class="img">{img_html}</div><table><thead><tr><th>블록</th><th>역할</th><th>원문</th><th>④ 판정</th>'
                      f'<th>AI 검토</th><th>사용자 검수</th></tr></thead><tbody>{"".join(trs)}</tbody></table></div></section>'))
    cards.sort(key=lambda c: (c[0], c[1], c[2]))
    legend = " · ".join(f"<b>{esc(c)}</b> {esc(t)}" for c, t in CLASSES)
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>④ 라벨 판정 검수</title><style>
:root{{--bg:#fff;--fg:#111;--mut:#666;--line:#ddd}}
@media (prefers-color-scheme: dark){{:root{{--bg:#16181c;--fg:#eee;--mut:#aaa;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 16px 40px}}
header{{position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:2}}
.card{{border:1px solid var(--line);border-radius:8px;margin:14px 0;padding:10px}}.card.p0{{border-color:#888}}.card.p1{{border-color:#f09614}}
.meta{{color:var(--mut);margin:2px 0 8px}}.row{{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}}
.img{{flex:0 0 420px;max-width:100%}}.img img{{width:100%;border:1px solid var(--line)}}
table{{flex:1 1 480px;border-collapse:collapse;font-size:13px}}td,th{{border-bottom:1px solid var(--line);padding:3px 5px;vertical-align:top;text-align:left}}
td.t{{white-space:pre-wrap;max-width:360px}}td.ai{{color:var(--mut);max-width:220px}}input{{width:110px}}
</style></head><body><header><b>④ 제품 라벨 판정 검수</b> — {len(rows)}섹션. 정답이 아니다. AI 검토 분류는 사람 판단이 아니다.
<button id="exp">TSV 내보내기</button> <span id="cnt"></span><br><small>분류: {legend}</small></header>
{"".join(c[3] for c in cards)}
<script>
const K='label-review-v1';let st={{}};try{{st=JSON.parse(localStorage.getItem(K)||'{{}}')}}catch(e){{}}
function save(){{try{{localStorage.setItem(K,JSON.stringify(st))}}catch(e){{}};document.getElementById('cnt').textContent='검수 '+Object.values(st).filter(v=>v.c).length+'개'}}
document.querySelectorAll('select[data-id]').forEach(s=>{{const id=s.dataset.id;if(st[id])s.value=st[id].c||'';s.onchange=()=>{{st[id]=Object.assign(st[id]||{{}},{{c:s.value}});save()}}}});
document.querySelectorAll('input[data-note]').forEach(i=>{{const id=i.dataset.note;if(st[id])i.value=st[id].n||'';i.oninput=()=>{{st[id]=Object.assign(st[id]||{{}},{{n:i.value}});save()}}}});
document.getElementById('exp').onclick=()=>{{let t='source\\tsection\\tblock\\tclass\\tnote\\n';for(const[k,v]of Object.entries(st)){{if(!v.c&&!v.n)continue;const[a,b,c]=k.split('|');t+=[a,b,c,v.c||'',(v.n||'').replace(/[\\t\\n]/g,' ')].join('\\t')+'\\n'}}
const u=URL.createObjectURL(new Blob([t],{{type:'text/tab-separated-values'}}));const e=document.createElement('a');e.href=u;e.download='label_user_review.tsv';e.click()}};save();
</script></body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    print(f"→ {out / 'index.html'} (섹션 {len(cards)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
