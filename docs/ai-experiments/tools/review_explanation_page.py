"""번역문 LLM 규제 판정 판단 이유 — 사람 검수 페이지 생성(2026-11-19).

    python docs/ai-experiments/tools/review_explanation_page.py --reg <regulation.json> --run <runs 폴더> [--variants v1,v2]

입력: review_explanation_probe.py가 저장한 `<variant>__<set>__<case>__<n>.report.json` · real_translations.json.
출력: <run>/review/index.html 한 파일(외부 자원 없음). 평가 입력은 브라우저 저장소에만 남고 TSV로 내려받는다.
페이지는 판정 · 설명을 보여 줄 뿐 정답을 정하지 않는다. 요약 수치의 패턴 검사(내부 ID · 단정 표현)는 보조 지표다.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_explanation_probe as probe  # noqa: E402

ID_RE = re.compile(r"(?<![A-Za-z0-9])(?:[tra]\d+(?:\.e\d+)?|RG-\d+)(?![A-Za-z0-9])")
ASSERT_RE = re.compile(r"위반|불법|준수하지 않")
JUDG_KO = {"risk": "위험", "no_risk": "해당 없음", "undetermined": "판단 불가"}
UND_KO = {"condition_unverifiable": "상품 조건 확인 불가", "insufficient_evidence": "근거 부족", "insufficient_context": "문맥 부족"}
BASIS_KO = {"dictionary_only": "사전 기록만", "external_evidence": "외부 근거", "both": "사전 + 외부 근거",
            "allowed_reference_only": "허용 표현 기록", "none": "근거 없음"}
SET_KO = {"R": "실제 섹션(골든셋 원문 → 운영 번역)", "1": "합성 1", "2": "합성 2"}


def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def highlight(text: str, spans: list[tuple[int, int]]) -> str:
    out, pos = [], 0
    for s, e in sorted(set(spans)):
        if s < pos:
            continue
        out += [esc(text[pos:s]), f"<mark>{esc(text[s:e])}</mark>"]
        pos = e
    out.append(esc(text[pos:]))
    return "".join(out).replace("\n", "<br>")


def load(run: Path, reg: Path, variants: list[str]):
    bundle = probe.to_bundle(json.loads(reg.read_text(encoding="utf-8")))
    reqs: dict[tuple[str, str], dict] = {}
    reports: dict[tuple[str, str], dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for f in sorted(run.glob("*.report.json"), key=lambda p: (p.name.rsplit("__", 1)[0], int(p.name.split("__")[3].split(".")[0]))):
        variant, set_name, case, _ = f.name[: -len(".report.json")].split("__")
        if variant not in variants:
            continue
        if (set_name, case) not in reqs:
            for c, r in probe.requests_of(set_name, run, bundle).items():
                reqs[(set_name, c)] = r
        reports[(set_name, case)][variant].append(json.loads(f.read_text(encoding="utf-8")))
    return reqs, reports


def summarize(reports, variants):
    s = {v: defaultdict(int) for v in variants}
    unstable = {v: 0 for v in variants}
    for by_v in reports.values():
        for v, reps in by_v.items():
            pair_j: dict[tuple[str, str], set] = defaultdict(set)
            for d in reps:
                s[v]["calls"] += 1
                if d["outcome"] != "completed":
                    s[v]["failed"] += 1
                    continue
                for b in d["payload"]["blocks"]:
                    s[v]["flags"] += len(b["flags"])
                    for j in b["judgments"]:
                        s[v][j["judgment"]] += 1
                        pair_j[(b["block_key"], j["external_id"])].add(j["judgment"])
                        if j["judgment"] != "no_risk" or j["mention"] == "present":
                            s[v]["shown"] += 1
                            s[v]["len"] += len(j["reason"])
                            s[v]["ids"] += bool(ID_RE.search(j["reason"]))
                            s[v]["assert"] += bool(ASSERT_RE.search(j["reason"]))
            unstable[v] += sum(1 for x in pair_j.values() if len(x) > 1)
    return s, unstable


def render(reqs, reports, variants, title_note: str) -> str:
    summ, unstable = summarize(reports, variants)
    rows = []
    for v in variants:
        x = summ[v]
        avg = round(x["len"] / x["shown"]) if x["shown"] else "-"
        rows.append(f"<tr><td><b>{esc(v)}</b></td><td>{x['calls']}</td><td>{x['failed']}</td><td>{x['flags']}</td><td>{x['risk']}</td>"
                    f"<td>{x['undetermined']}</td><td>{x['shown']}</td><td>{avg}</td><td>{x['ids']}</td><td>{x['assert']}</td>"
                    f"<td>{unstable[v]}</td></tr>")
    cards, toc, n_items = [], [], 0
    for (set_name, case), by_v in sorted(reports.items(), key=lambda kv: ({"R": 0, "1": 1, "2": 2}[kv[0][0]], kv[0][1])):
        req = reqs[(set_name, case)]
        texts = {t["block_key"]: t["text"] for t in req["targets"]}
        src = {b["block_key"]: b["source_ko"] for b in req["blocks"]}
        order = [b["block_key"] for b in sorted(req["blocks"], key=lambda b: b["block_order"])]
        supplied: dict[str, dict] = {}
        pairs: dict[tuple[str, str], dict[str, list]] = defaultdict(lambda: defaultdict(list))
        fails = []
        for v, reps in by_v.items():
            for i, d in enumerate(reps, 1):
                if d["outcome"] != "completed":
                    fails.append(f"{v} #{i}: {esc((d.get('failure') or {}).get('kind'))} — {esc((d.get('failure') or {}).get('message', ''))[:160]}")
                    continue
                for it in d["payload"]["supplied_items"]["check"] + d["payload"]["supplied_items"]["reference"]:
                    supplied[it["external_id"]] = it
                for b in d["payload"]["blocks"]:
                    for j in b["judgments"]:
                        pairs[(b["block_key"], j["external_id"])][v].append((i, j))
        shown = [(k, pv) for k, pv in pairs.items()
                 if any(j["judgment"] != "no_risk" or j["mention"] == "present" for runs in pv.values() for _, j in runs)]
        shown.sort(key=lambda kv: (order.index(kv[0][0]), kv[0][1]))
        sec_id = f"{set_name}-{case}".replace(".", "-")
        toc.append(f'<a href="#{sec_id}">{esc(case if set_name == "R" else SET_KO[set_name] + " " + case)}</a> <span class="muted">({len(shown)})</span>')
        ctx = "".join(f"<tr><td class='k'>{esc(k)}</td><td>{esc(src[k]).replace(chr(10), '<br>')}</td>"
                      f"<td>{esc(texts.get(k, '— (대상 아님)')).replace(chr(10), '<br>')}</td></tr>" for k in order)
        items = []
        for (bk, eid), pv in shown:
            n_items += 1
            it = supplied.get(eid, {})
            ev = "".join(f"<li>{esc(e.get('source_type'))} · {esc(e.get('article'))}"
                         f"{(' · “' + esc(e['quote']) + '”') if e.get('quote') else ''}"
                         f"{(' · <a href=\"' + esc(e['url']) + '\" target=\"_blank\" rel=\"noopener\">링크</a>') if e.get('url') else ''}</li>"
                         for e in it.get("evidence", []))
            spans = [(loc["start"], loc["end"]) for runs in pv.values() for _, j in runs for loc in j["locations"]]
            runs_html = []
            for v in variants:
                for i, j in pv.get(v, []):
                    locs = ", ".join(f"<code>{esc(loc['quote'])}</code>" for loc in j["locations"]) or "—"
                    cond = ("<div class='cond'>확인할 조건: " + " / ".join(esc(c) for c in j["unverified_conditions"]) + "</div>"
                            if j["unverified_conditions"] else "")
                    tag = JUDG_KO[j["judgment"]] + (f" · {UND_KO.get(j['undetermined_reason'], '')}" if j["undetermined_reason"] else "")
                    flagged = j["judgment"] != "no_risk"
                    runs_html.append(f"<tr class='{'flag' if flagged else 'nf'}'><td><b>{esc(v)}</b> #{i}</td>"
                                     f"<td><span class='j j-{j['judgment']}'>{esc(tag)}</span>"
                                     f"<div class='muted'>{'화면 경고 후보' if flagged else '경고 아님(내부 기록)'}</div></td>"
                                     f"<td>{locs}<div class='muted'>{esc(BASIS_KO.get(j['basis_summary'], j['basis_summary']))}</div></td>"
                                     f"<td class='reason'>{esc(j['reason'])}{cond}</td></tr>")
            key = f"{set_name}|{case}|{bk}|{eid}"
            rate = "".join(
                f"<div class='rate' data-key='{esc(key)}' data-v='{esc(v)}'><b>{esc(v)}</b>"
                f"<label>판정 <select data-f='judgment'><option></option><option>맞음</option><option>틀림</option><option>애매</option></select></label>"
                f"<label>설명 <select data-f='explain'><option></option><option>좋음</option><option>보통</option><option>나쁨</option></select></label>"
                f"<label>근거 없는 내용 <select data-f='unfounded'><option></option><option>없음</option><option>있음</option></select></label>"
                f"<input data-f='memo' placeholder='메모'></div>" for v in variants if pv.get(v))
            pref = (f"<div class='rate' data-key='{esc(key)}' data-v='*'><b>비교</b><label>더 나은 설명 <select data-f='better'><option></option>"
                    + "".join(f"<option>{esc(v)}</option>" for v in variants if pv.get(v)) + "<option>같음</option></select></label></div>")
            items.append(
                f"<div class='item' id='{esc(key)}'><div class='itemhead'><span class='k'>{esc(bk)}</span> × <b>{esc(eid)}</b> "
                f"<span class='muted'>{esc(it.get('source_expression'))} · 서비스 판정 {esc(it.get('verdict_status'))}"
                f"{(' (원판정 ' + esc(it['source_verdict_status']) + ')') if it.get('source_verdict_status') else ''}</span></div>"
                f"<div class='grid'><div><div class='lbl'>원문</div><div class='box'>{esc(src.get(bk)).replace(chr(10), '<br>')}</div>"
                f"<div class='lbl'>번역문(검사 대상)</div><div class='box en'>{highlight(texts.get(bk, ''), spans)}</div></div>"
                f"<div><div class='lbl'>사전 사유(공급 근거)</div><div class='box'>{esc(it.get('reason'))}</div>"
                f"<div class='lbl'>근거</div><ul class='ev'>{ev or '<li>없음</li>'}</ul></div></div>"
                f"<table class='runs'><thead><tr><th>실행</th><th>판정</th><th>문제 표현 · 근거 종류</th><th>판단 이유(설명)</th></tr></thead>"
                f"<tbody>{''.join(runs_html)}</tbody></table><div class='rates'>{rate}{pref}</div></div>")
        fail_html = f"<div class='fail'>실패한 실행: {'<br>'.join(fails)}</div>" if fails else ""
        cards.append(f"<section id='{sec_id}'><h2>{esc(case)} <span class='muted'>· {esc(SET_KO[set_name])} · 분류 {esc(req['regulatory_class'])}"
                     f"</span></h2>{fail_html}<details><summary>섹션 전체 문맥({len(order)}블록)</summary>"
                     f"<table class='ctx'><thead><tr><th>블록</th><th>원문</th><th>번역문</th></tr></thead><tbody>{ctx}</tbody></table></details>"
                     f"{''.join(items) or '<p class=muted>경고 후보 · 관련 표현 없음(모든 실행에서 모든 항목이 해당 없음)</p>'}</section>")
    return PAGE.replace("{{NOTE}}", title_note).replace("{{SUMMARY}}", "".join(rows)).replace("{{TOC}}", " · ".join(toc)) \
        .replace("{{CARDS}}", "".join(cards)).replace("{{TOTAL}}", str(n_items))


PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>규제 판정 설명 검수</title><style>
:root{--bg:#fafafa;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e3e3e8;--mark:#ffe08a;--risk:#c62828;--und:#b26a00;--ok:#2e7d32;--acc:#2d5bd7}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--muted:#a0a0a8;--card:#1d1d21;--line:#33333a;--mark:#7a5c00;--risk:#ef6b6b;--und:#e0a040;--ok:#6cc070;--acc:#7b9cff}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,"Segoe UI","Malgun Gothic",sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:28px 0 8px}
.muted{color:var(--muted);font-weight:normal}.note{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px}
table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px}.summary td,.summary th{text-align:right}.summary td:first-child,.summary th:first-child{text-align:left}
.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin:10px 0}.itemhead{margin-bottom:8px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.lbl{font-size:12px;color:var(--muted);margin-top:4px}.box{border:1px solid var(--line);border-radius:6px;padding:6px 8px;margin:2px 0 6px}
.en{font-family:ui-monospace,Consolas,monospace;font-size:13px}mark{background:var(--mark);color:inherit;border-radius:3px;padding:0 1px}
.ev{margin:2px 0;padding-left:18px;font-size:13px}.k{font-family:ui-monospace,Consolas,monospace;color:var(--muted)}
.j{font-weight:600}.j-risk{color:var(--risk)}.j-undetermined{color:var(--und)}.j-no_risk{color:var(--ok)}
tr.nf td{opacity:.75}.reason{font-size:14px}.cond{margin-top:4px;color:var(--und);font-size:13px}
.rates{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}
.rate{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.rate label{font-size:12px;color:var(--muted)}
select,input,button{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:2px 6px}
input{min-width:160px}button{cursor:pointer;padding:4px 10px}.fail{color:var(--risk);font-size:13px;margin:4px 0}
details{margin:4px 0 8px}summary{cursor:pointer;color:var(--acc)}.ctx td{font-size:13px}.bar{position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:2;border-bottom:1px solid var(--line)}
.toc a{color:var(--acc);text-decoration:none}code{font-size:12.5px}
</style></head><body><main>
<h1>번역문 규제 판정 — 판단 이유(설명) 검수</h1>
<div class="note">{{NOTE}}</div>
<h2>요약 <span class="muted">(보조 지표 — 패턴 검사는 판정 정확도를 뜻하지 않는다)</span></h2>
<table class="summary"><thead><tr><th>프롬프트</th><th>호출</th><th>실패</th><th>경고 후보(flag)</th><th>위험</th><th>판단 불가</th>
<th>표시 대상 설명</th><th>평균 글자</th><th>내부 ID 포함</th><th>단정 표현 포함</th><th>실행마다 판정이 달라진 쌍</th></tr></thead><tbody>{{SUMMARY}}</tbody></table>
<div class="bar"><span id="progress"></span> · <button id="exp">평가 TSV 내려받기</button> <button id="clr">평가 지우기</button>
<div class="toc">{{TOC}}</div></div>
{{CARDS}}
</main><script>
const KEY="review-explanation-2026-11-19", TOTAL={{TOTAL}};
let st={};try{st=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){st={}}
function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
const rates=[...document.querySelectorAll(".rate")];
function prog(){const done=new Set(Object.keys(st).filter(k=>Object.values(st[k]).some(x=>x)).map(k=>k.split("§")[0]));
document.getElementById("progress").textContent=`평가한 항목 ${done.size} / ${TOTAL}`}
rates.forEach(r=>{const id=r.dataset.key+"§"+r.dataset.v;r.querySelectorAll("[data-f]").forEach(el=>{const f=el.dataset.f;
if(st[id]&&st[id][f])el.value=st[id][f];el.addEventListener("change",()=>{(st[id]=st[id]||{})[f]=el.value;save();prog()});
if(el.tagName==="INPUT")el.addEventListener("input",()=>{(st[id]=st[id]||{})[f]=el.value;save()})})});
prog();
document.getElementById("exp").onclick=()=>{const head=["set","case","block","item","prompt","judgment","explain","unfounded","better","memo"];
const lines=[head.join("\\t")];Object.keys(st).sort().forEach(id=>{const [k,v]=id.split("§");const [s,c,b,i]=k.split("|");const x=st[id];
lines.push([s,c,b,i,v,x.judgment||"",x.explain||"",x.unfounded||"",x.better||"",(x.memo||"").replace(/[\\t\\n]/g," ")].join("\\t"))});
const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([lines.join("\\n")],{type:"text/tab-separated-values"}));
a.download="review_explanation_human.tsv";a.click()};
document.getElementById("clr").onclick=()=>{if(confirm("이 브라우저에 저장한 평가를 모두 지울까요?")){st={};save();location.reload()}};
</script></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reg", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--variants", default="v1,v2")
    a = ap.parse_args()
    variants = a.variants.split(",")
    reqs, reports = load(a.run, a.reg, variants)
    note = ("<b>무엇을 보나</b> — 같은 번역문을 기존 프롬프트(<b>v1</b>, <code>pipeline/prompts/translation_review.md</code>)와 판단 이유 작성 지침만 바꾼 변형"
            "(<b>v2</b>, <code>docs/ai-experiments/prompts/2026-11-19_translation_review_v2-seller-reason.md</code>)으로 각각 반복 판정했다. "
            "판정 규칙 · 모델(gemini-3.8-flash, 온도 0) · 시간 제한(180초) · 규제 행(로컬 정규화본 v9 계열)은 같다. "
            "항목은 어느 실행에서든 위험 · 판단 불가이거나 관련 표현이 있다고 본 (블록 × 사전 항목) 쌍이다. "
            "<b>화면 경고 후보</b>만 사용자에게 보일 수 있는 설명이고, '해당 없음'의 이유는 내부 기록이다. "
            "실제 섹션은 운영 번역 함수로 번역했으며 표현 지시는 넣지 않았고(운영 미활성), 라벨 · 로고 값은 모두 아님으로 가정했다. "
            "평가는 이 브라우저에만 저장된다 — 끝나면 TSV로 내려받는다.")
    out = a.run / "review"
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(reqs, reports, variants, note), encoding="utf-8")
    print(out / "index.html")


if __name__ == "__main__":
    main()
