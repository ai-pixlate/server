"""A+ 섹션 내용 라벨 육안 확인 화면 — 섹션마다 그림 · 읽은 글 · 모델이 붙인 라벨을 나란히 놓는다(검수 도구, 운영 코드 아님).

판정을 기록하지 않는다. 보기만 하는 화면이다.

    python docs/ai-experiments/tools/aplus_review/label_review.py --out <새 폴더> [--title 제목] \
        --product <이름>=<aplus_label.json>=<sections.json>[=<aplus_label_debug.json>] [--product ...]

- `aplus_label.json`: `pipeline.aplus.label.run`의 결과(`AplusLabelResult`, `status=ok`).
- `sections.json`: 그 실행에 쓴 섹션 목록(`Section`을 JSON 배열로 — `section_key` · `width` · `height` · `image_path`).
- `aplus_label_debug.json`(선택): 그 실행의 기록. 주면 모델에 보낸 섹션의 글을 함께 보여 준다.
- 경로에 `=`가 있으면 안 된다. Windows에서는 `C:/…` 꼴로 준다.

출력: `index.html`(화면) · `images/`(섹션 그림 JPEG). 라벨 이름 · 칸 표는 규격 데이터(`pipeline/data/aplus/spec.json`), 라벨 정의는
프롬프트(`pipeline/prompts/aplus_label.md`)의 표에서 읽는다.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

from PIL import Image

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
from pipeline.aplus.spec import load_spec  # noqa: E402

Image.MAX_IMAGE_PIXELS = None
TALL_PX = 2000  # 이보다 높은 섹션을 "큰 섹션"으로 걸러 본다(화면용 기준)


def label_guide() -> dict[str, dict[str, str]]:
    """프롬프트의 라벨 표 → {코드: {정의, 헷갈릴 때}}."""
    out: dict[str, dict[str, str]] = {}
    for ln in (REPO / "pipeline/prompts/aplus_label.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0].isupper() and cells[0].isalpha():
            out[cells[0]] = {"definition": cells[2], "confusable": cells[3]}
    return out


def load_product(arg: str, out: Path, spec: Any) -> dict[str, Any]:
    parts = arg.split("=")
    if len(parts) not in (3, 4):
        raise SystemExit(f"--product는 이름=라벨.json=섹션.json[=기록.json] 꼴이다: {arg}")
    name, label_path, sections_path = parts[:3]
    result = json.loads(Path(label_path).read_text(encoding="utf-8"))
    if result.get("status") != "ok":
        raise SystemExit(f"{name}: 라벨 결과가 ok가 아니다({result.get('status')})")
    sections = {s["section_key"]: s for s in json.loads(Path(sections_path).read_text(encoding="utf-8"))}
    texts: dict[str, str] = {}
    if len(parts) == 4:
        rec = json.loads(Path(parts[3]).read_text(encoding="utf-8"))
        by_id = {s["id"]: s["text"] for s in rec["payload"]["sections"]}
        texts = {key: by_id.get(sid, "") for sid, key in rec["sections"].items()}
    missing = [x["section_key"] for x in result["labels"] if x["section_key"] not in sections]
    if missing:
        raise SystemExit(f"{name}: 섹션 목록에 없는 섹션 {missing[:3]} …")
    img_dir = out / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, x in enumerate(result["labels"], 1):
        s = sections[x["section_key"]]
        file = f"{name}_{i:03d}.jpg"
        Image.open(s["image_path"]).convert("RGB").save(img_dir / file, quality=82)
        slot = spec.raw["labels"][x["label"]].get("slot")
        rows.append({"product": name, "order": i, "key": x["section_key"], "w": s["width"], "h": s["height"], "img": f"images/{file}",
                     "label": x["label"], "sub": x["sub"], "confidence": x["confidence"], "reason": x["reason"], "slot": slot,
                     "text": texts.get(x["section_key"])})
    return {"name": name, "rows": rows}


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1b1b1b;--sub:#666;--line:#ddd;--chip:#eef0f3;--warn:#b54708;--out:#8a8a8a}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--card:#202020;--ink:#eaeaea;--sub:#a0a0a0;--line:#383838;--chip:#2c2f34;--warn:#f0a35e;--out:#8a8a8a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 "Pretendard","Malgun Gothic",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px}
h1{font-size:17px;margin:0 0 8px}.bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
button,select{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 10px;cursor:pointer}
button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}.count{margin-left:auto;color:var(--sub)}
main{max-width:1180px;margin:0 auto;padding:14px 16px 80px}
details.guide{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-bottom:14px}
details.guide table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}details.guide td,details.guide th{border-top:1px solid var(--line);padding:5px 6px;text-align:left;vertical-align:top}
.dist{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}.dist span{background:var(--chip);border-radius:6px;padding:2px 8px;font-size:13px}
.card{display:grid;grid-template-columns:minmax(0,440px) minmax(0,1fr);gap:16px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
@media (max-width:820px){.card{grid-template-columns:1fr}}
.pic{max-height:760px;overflow:auto;border:1px solid var(--line);border-radius:6px;background:#fff}.pic img{display:block;width:100%;cursor:zoom-in}
.meta{color:var(--sub);font-size:13px;margin-bottom:8px;word-break:break-all}
.lab{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px}
.code{font-weight:700;font-size:18px}.name{font-size:16px}.chip{background:var(--chip);border-radius:6px;padding:2px 8px;font-size:13px}
.slot{font-weight:600}.slot.none{color:var(--out)}.low{color:var(--warn);font-weight:600}
.row{margin:6px 0}.k{display:inline-block;min-width:74px;color:var(--sub)}
pre{white-space:pre-wrap;word-break:break-word;font:13px/1.5 inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;max-height:340px;overflow:auto;margin:4px 0 0}
#zoom{position:fixed;inset:0;background:rgba(0,0,0,.82);overflow:auto;display:none;z-index:20;cursor:zoom-out;text-align:center;padding:20px}#zoom img{max-width:none}
</style></head><body>
<header><h1>__TITLE__</h1>
<div class="bar" id="products"></div>
<div class="bar" style="margin-top:6px"><span id="filters"></span><select id="bylabel"></select><span class="count" id="count"></span></div>
</header>
<main>
<details class="guide"><summary>라벨 19개 — 정의와 가는 칸</summary><table id="guide"></table></details>
<div class="dist" id="dist"></div>
<div id="cards"></div>
</main>
<div id="zoom"><img alt=""></div>
<script>
const DATA=__DATA__;
const F={product:DATA.products[0].name,sub:false,low:false,tall:false,out:false,label:""};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const nm=c=>DATA.labels[c].name_ko, slotText=n=>n?`칸 ${n} ${DATA.slots[n]}`:"7칸 밖";
function rows(){return DATA.products.find(p=>p.name===F.product).rows.filter(r=>(!F.sub||r.sub.length)&&(!F.low||r.confidence==="low")&&(!F.tall||r.h>DATA.tall)&&(!F.out||!r.slot)&&(!F.label||r.label===F.label));}
function draw(){
  document.getElementById("products").innerHTML=DATA.products.map(p=>`<button class="${p.name===F.product?"on":""}" data-p="${esc(p.name)}">${esc(p.name)} (${p.rows.length})</button>`).join("");
  const chips=[["sub","보조 라벨 있음"],["low","확신 낮음"],["tall",`${DATA.tall.toLocaleString()}px 넘음`],["out","7칸 밖"]];
  document.getElementById("filters").innerHTML=chips.map(([k,t])=>`<button class="${F[k]?"on":""}" data-f="${k}">${t}</button>`).join(" ");
  const all=DATA.products.find(p=>p.name===F.product).rows, dist={};
  all.forEach(r=>dist[r.label]=(dist[r.label]||0)+1);
  document.getElementById("bylabel").innerHTML=`<option value="">라벨 전체</option>`+Object.keys(dist).sort((a,b)=>dist[b]-dist[a]).map(c=>`<option value="${c}" ${F.label===c?"selected":""}>${c} ${esc(nm(c))} (${dist[c]})</option>`).join("");
  document.getElementById("dist").innerHTML=Object.keys(dist).sort((a,b)=>dist[b]-dist[a]).map(c=>`<span>${c} ${esc(nm(c))} ${dist[c]}</span>`).join("");
  const rs=rows();
  document.getElementById("count").textContent=`${rs.length} / ${all.length}개`;
  document.getElementById("cards").innerHTML=rs.map(r=>`<section class="card">
    <div class="pic"><img loading="lazy" src="${r.img}" alt=""></div>
    <div><div class="meta">${esc(r.product)} · ${r.order}/${all.length} · ${esc(r.key)} · ${r.w}×${r.h}px</div>
      <div class="lab"><span class="code">${r.label}</span><span class="name">${esc(nm(r.label))}</span>${r.confidence==="low"?'<span class="low">확신 낮음</span>':""}</div>
      <div class="row"><span class="k">가는 칸</span><span class="slot ${r.slot?"":"none"}">${esc(slotText(r.slot))}</span></div>
      <div class="row"><span class="k">보조 라벨</span>${r.sub.length?r.sub.map(c=>`<span class="chip">${c} ${esc(nm(c))} · ${esc(slotText(DATA.labels[c].slot))}</span>`).join(" "):"없음"}</div>
      <div class="row"><span class="k">이유</span>${esc(r.reason)}</div>
      ${r.text===null?"":`<div class="row"><span class="k">읽은 글</span>${r.text?"":"(글 없음)"}</div>${r.text?`<pre>${esc(r.text)}</pre>`:""}`}
    </div></section>`).join("")||"<p>해당하는 섹션이 없다.</p>";
}
document.getElementById("guide").innerHTML="<tr><th>코드</th><th>이름</th><th>가는 칸</th><th>정의</th><th>헷갈릴 때</th></tr>"+Object.entries(DATA.labels).map(([c,v])=>`<tr><td><b>${c}</b></td><td>${esc(v.name_ko)}</td><td>${esc(slotText(v.slot))}</td><td>${esc(v.definition)}</td><td>${esc(v.confusable)}</td></tr>`).join("");
document.addEventListener("click",e=>{
  const t=e.target, z=document.getElementById("zoom");
  if(t.dataset.p){F.product=t.dataset.p;F.label="";draw();scrollTo(0,0);}
  else if(t.dataset.f){F[t.dataset.f]=!F[t.dataset.f];draw();}
  else if(t.closest(".pic")&&t.tagName==="IMG"){z.firstElementChild.src=t.src;z.style.display="block";z.scrollTop=0;}
  else if(t.closest("#zoom")){z.style.display="none";}
});
document.getElementById("bylabel").addEventListener("change",e=>{F.label=e.target.value;draw();});
document.addEventListener("keydown",e=>{
  if(e.key==="Escape"){document.getElementById("zoom").style.display="none";return;}
  if(e.key!=="j"&&e.key!=="k")return;
  const cs=[...document.querySelectorAll(".card")], top=document.querySelector("header").offsetHeight+8;
  const i=cs.findIndex(c=>c.getBoundingClientRect().top>top+2);
  const cur=i===-1?cs.length-1:i-1, next=cs[Math.max(0,Math.min(cs.length-1,e.key==="j"?cur+1:cur-1))];
  if(next)scrollTo({top:scrollY+next.getBoundingClientRect().top-top});
});
draw();
</script></body></html>
"""


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="A+ 섹션 내용 라벨 육안 확인 화면")
    ap.add_argument("--out", required=True, help="출력 폴더(새 폴더)")
    ap.add_argument("--title", default="A+ 섹션 내용 라벨 확인")
    ap.add_argument("--product", action="append", required=True, help="이름=aplus_label.json=sections.json[=aplus_label_debug.json]")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    spec = load_spec()
    guide = label_guide()
    labels = {c: {"name_ko": v["name_ko"], "slot": v.get("slot"), **guide.get(c, {"definition": "", "confusable": ""})} for c, v in spec.raw["labels"].items()}
    slots = {int(n): s["name_ko"] for n, s in spec.raw["slots"].items()}
    products = [load_product(p, out, spec) for p in a.product]
    data = {"products": products, "labels": labels, "slots": slots, "tall": TALL_PX, "spec_version": spec.version}
    page = PAGE.replace("__TITLE__", html.escape(a.title)).replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (out / "index.html").write_text(page, encoding="utf-8")
    for p in products:
        rs = p["rows"]
        print(f"{p['name']}: 섹션 {len(rs)} · 보조 라벨 {sum(1 for r in rs if r['sub'])} · 확신 낮음 {sum(1 for r in rs if r['confidence'] == 'low')}"
              f" · {TALL_PX}px 넘음 {sum(1 for r in rs if r['h'] > TALL_PX)} · 7칸 밖 {sum(1 for r in rs if not r['slot'])}")
    print("화면:", out / "index.html")


if __name__ == "__main__":
    main()
