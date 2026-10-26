"""③ OCR 후처리 4단계 4차(y 절단 LLM 병합 허용안) 사람 검수 페이지 생성(2026-10-23).

    python docs/ai-experiments/tools/merge_post_step4_round4_review_page.py --run pipeline/out/merge-post-step4-round4_2026-10-23 \
        --round1 pipeline/out/merge-post-step4-round1_2026-10-23 --input pipeline/samples/local/blocks-input-v1 --out OUT_DIR

기존 LLM 실행 결과(`llm-run` B3 · B3_yonly)만 읽는다 — OCR · LLM 추가 호출 없음. B3_yonly에서 후보 영역(잎)을 넘어 새로 합쳐진
블록만 카드로 만든다(블록 8개 = 1차 사람 판정 S1 사례 9건, `reg_0023` · `reg_0024`가 한 블록). 카드 순서: 1차에서 "나뉘어야 함"인데
합쳐진 것 → "이어져야 함" → "둘 다 허용". 카드마다 표시 없는 crop · B3 블록 경계 · B3_yonly 블록 경계 그림, 접어 둔 섹션 전체 그림,
두 변형의 블록 텍스트 · 역할 · 블록 경계 번호, 사례 ID와 1차 판정, 글자 비교(공백 제외)를 둔다. 판정 선택 · 메모는 선택이다(브라우저에만
저장, TSV 내보내기 가능) — 의견은 채팅으로 줘도 된다. 실패한 섹션(B3_yonly 미실행)은 페이지 머리에 미검증으로 적는다.
이 페이지는 정답이 아니며 채택 승인이 아니다.
5차(2026-10-26 계획)에는 `--sections`(섹션 목록 파일) · `--round1` 생략(1차 판정 없는 새 섹션) · `--store-key`(브라우저 저장 키)를 쓴다.
`--run`을 여러 번 주면 실행 폴더마다 카드를 모으고(섹션은 각 폴더 B3_yonly/run.json의 계획, 카드 ID 앞에 폴더 이름),
`--exclude-known DIR`은 DIR/B3_yonly에 같은 원시 영역 구성으로 이미 있는 블록(예: 4차에서 검수한 8블록)을 뺀다.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import merge_post_step4 as step4  # noqa: E402
from merge_post_step4_review_page import Section  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import merge  # noqa: E402

FONT = ImageFont.load_default(size=15)
COLORS = [(220, 0, 0), (0, 90, 220), (0, 150, 60), (200, 110, 0), (150, 0, 170), (0, 150, 160)]
CHOICES = [("current_ok", "현행(B3)이 적절"), ("yonly_ok", "y 병합이 적절"), ("both_ok", "둘 다 허용"),
           ("both_bad", "둘 다 부적절"), ("undecidable", "판단 어려움")]
Q1_LABEL = {"joined_needed": "이어져야 함", "separate_ok": "나뉘어야 함", "either_ok": "둘 다 허용"}
GROUP_ORDER = {"separate_ok": 0, "joined_needed": 1, "either_ok": 2}
GROUP_TITLE = {0: "① 1차에서 '나뉘어야 함'인데 y 병합으로 합쳐진 것", 1: "② 1차에서 '이어져야 함' — 개선 후보",
               2: "③ 1차에서 '둘 다 허용'", 9: "1차 사람 판정이 없는 블록"}


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def rkeys(b) -> list[str]:
    return [r.region_key for ln in b.source_lines for r in ln.regions]


def draw(sec: Section, blocks: list, area: tuple[int, int, int, int], dest: Path, labels: list[str] | None,
         margin: int = 30, fit_w: int | None = None) -> str:
    with Image.open(sec.path) as im:
        src = im.convert("RGB")
    x0, y0, x1, y1 = area
    box = (max(0, x0 - margin), max(0, y0 - margin), min(src.width, x1 + margin), min(src.height, y1 + margin))
    crop = src.crop(box)
    k = fit_w / crop.width if fit_w else min(3, max(1, math.ceil(560 / max(1, crop.width))))
    if k != 1:
        crop = crop.resize((max(1, round(crop.width * k)), max(1, round(crop.height * k))), Image.LANCZOS)
    if labels is not None:
        d = ImageDraw.Draw(crop)
        for i, (b, text) in enumerate(zip(blocks, labels)):
            c = COLORS[i % len(COLORS)]
            bx0, by0, bx1, by1 = sec.union(rkeys(b))
            rect = [(bx0 - box[0]) * k, (by0 - box[1]) * k, (bx1 - box[0]) * k, (by1 - box[1]) * k]
            d.rectangle(rect, outline=c, width=3)
            l, t, r, bt = d.textbbox((rect[0] + 2, max(0, rect[1] - 19)), text, font=FONT)
            d.rectangle([l - 2, t - 1, r + 2, bt + 1], fill=(255, 255, 255), outline=c)
            d.text((rect[0] + 2, max(0, rect[1] - 19)), text, fill=c, font=FONT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest)
    return dest.name


def char_check(b3_texts: list[str], y_text: str) -> str:
    """공백 · 줄바꿈을 뺀 글자로 B3 블록들(블록 순서대로 이음)과 y 병합 블록을 비교한다."""
    a = "".join("".join(t.split()) for t in b3_texts)
    b = "".join(y_text.split())
    if a == b:
        return "공백 제외 글자 · 순서 모두 같음"
    ca, cb = Counter(a), Counter(b)
    if ca == cb:
        return "공백 제외 글자 구성은 같고 순서가 다름(줄 순서 차이)"
    plus, minus = cb - ca, ca - cb
    return (f"글자 차이 — y 병합에만 있음: {''.join(sorted(plus.elements())) or '없음'} · "
            f"B3에만 있음: {''.join(sorted(minus.elements())) or '없음'}")


def build(run: Path, round1: Path | None, inp: Path, out: Path, names: list[str], tag: str = "",
          known: set | None = None) -> tuple[list[dict], list[str]]:
    ck, tsv = {"cases": []}, {}
    if round1 is not None:
        ck = json.loads((round1 / "checkset.json").read_text(encoding="utf-8"))
        tsv = {r["case_ids"]: r for r in csv.DictReader(open(round1 / "review" / "merge_post_step4_review_2026-10-23.tsv",
                                                              encoding="utf-8"), delimiter="\t")}
    cfg = step4.base_cfg([])
    cards, failed = [], []
    for name in names:
        img, sec_meta, ocr = step4.section_inputs(inp, name)
        key = sec_meta.section_key
        py, pb = run / "B3_yonly" / img / "merge" / f"{key}.json", run / "B3" / img / "merge" / f"{key}.json"
        if not (py.is_file() and pb.is_file()):
            failed.append(f"{tag}:{name}" if tag else name)
            continue
        heur, layout, _, _ = merge._heuristic(sec_meta, ocr, cfg)  # B3 후보 영역(잎) 번호
        leaf = {r: layout.block_region[b.block_key] for b in heur.blocks for r in rkeys(b)}
        y, b3 = jsonio.load_merge(py), jsonio.load_merge(pb)
        sec = Section(inp, name)
        for yb in y.blocks:
            regs = set(rkeys(yb))
            if len({leaf[r] for r in regs}) < 2 or (known and (name, frozenset(regs)) in known):
                continue
            b3_blocks = sorted([b for b in b3.blocks if regs & set(rkeys(b))], key=lambda b: b.block_order)
            cases = [c for c in ck["cases"] if c["set"] == "S1" and c["section"] == name and regs & set(c["regions"])
                     and c["id"] in tsv and tsv[c["id"]]["q1"] != "excluded"]
            group = min(GROUP_ORDER[tsv[c["id"]]["q1"]] for c in cases) if cases else 9
            all_regs = sorted(regs | {r for b in b3_blocks for r in rkeys(b)})
            area = sec.union(all_regs)
            stem = f"{tag + '_' if tag else ''}{img}_{key}_y{yb.block_order}"
            imgs = {
                "plain": draw(sec, [], area, out / "img" / f"{stem}_plain.png", None),
                "b3": draw(sec, b3_blocks, area, out / "img" / f"{stem}_b3.png",
                           [f"B3 #{b.block_order} {b.role}" for b in b3_blocks]),
                "y": draw(sec, [yb], area, out / "img" / f"{stem}_y.png", [f"y #{yb.block_order} {yb.role}"]),
            }
            with Image.open(sec.path) as im:
                w, h = im.size
            imgs["section"] = draw(sec, [yb], (0, 0, w, h), out / "img" / f"{stem}_section.png", [""], margin=0, fit_w=520)
            cards.append({
                "id": f"{tag + ':' if tag else ''}{name}/y{yb.block_order}", "section": name, "group": group, "tag": tag,
                "cases": [{"id": c["id"], "q1": tsv[c["id"]]["q1"], "q2": tsv[c["id"]]["q2"]} for c in cases],
                "b3": [{"order": b.block_order, "role": b.role, "lines": [ln.text for ln in b.source_lines]} for b in b3_blocks],
                "y": {"order": yb.block_order, "role": yb.role, "lines": [ln.text for ln in yb.source_lines],
                      "leaves": sorted({leaf[r] for r in regs})},
                "chars": char_check([b.source_ko for b in b3_blocks], yb.source_ko),
                "imgs": imgs,
            })
    cards.sort(key=lambda c: (c["group"], c["tag"], c["section"], c["y"]["order"]))
    return cards, failed


def card_html(c: dict) -> str:
    cases = " · ".join(f"<code>{esc(x['id'].split(':', 1)[1])}</code> 1차: <b>{esc(Q1_LABEL.get(x['q1'], x['q1']))}</b>"
                       + (f" (1차에서 고른 적절한 변형: {esc(x['q2'])})" if x["q2"] else "") for x in c["cases"])
    b3_rows = "".join(f"<tr><td>B3 #{b['order']}</td><td>{esc(b['role'])}</td><td>{'<br>'.join(f'{i}. {esc(t)}' for i, t in enumerate(b['lines'], 1))}</td></tr>"
                      for b in c["b3"])
    y = c["y"]
    y_row = (f"<tr class='yrow'><td>y #{y['order']}</td><td>{esc(y['role'])}</td>"
             f"<td>{'<br>'.join(f'{i}. {esc(t)}' for i, t in enumerate(y['lines'], 1))}</td></tr>")
    radios = "".join(f'<label><input type="radio" name="j:{esc(c["id"])}" value="{v}"> {esc(t)}</label>' for v, t in CHOICES)
    return f"""
<section class="card" data-id="{esc(c['id'])}">
  <h3>{esc(c['tag'] + ' · ' if c['tag'] else '')}{esc(c['section'])} — y 병합 블록 #{y['order']} <span class="meta">(후보 영역 {', '.join(map(str, y['leaves']))}에 걸쳐 합쳐짐)</span></h3>
  <div class="meta">사례: {cases}</div>
  <div class="imgs">
    <figure><figcaption>표시 없음</figcaption><img src="img/{esc(c['imgs']['plain'])}" alt="표시 없는 crop"></figure>
    <figure><figcaption>현행 B3 블록 경계</figcaption><img src="img/{esc(c['imgs']['b3'])}" alt="B3"></figure>
    <figure><figcaption>B3_yonly(y 병합) 블록 경계</figcaption><img src="img/{esc(c['imgs']['y'])}" alt="y 병합"></figure>
  </div>
  <details><summary>섹션 전체 보기(빨간 틀 = y 병합 블록)</summary><img class="section" src="img/{esc(c['imgs']['section'])}" alt="섹션 전체"></details>
  <table><thead><tr><th>블록</th><th>역할</th><th>줄(위에서 아래)</th></tr></thead><tbody>{b3_rows}{y_row}</tbody></table>
  <div class="meta">글자 비교: {esc(c['chars'])}</div>
  <div class="form"><div><b>이 결과를 하나의 문단 · 편집 블록으로 묶어도 되는가?</b> (선택 · 채팅으로 줘도 됨)</div>
    <div class="q">{radios} <button type="button" class="clear">지우기</button></div>
    <input type="text" class="note" placeholder="메모(선택)"></div>
</section>"""


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>y 병합안 검수</title>
<style>
:root {{ --bg:#fff; --fg:#111; --muted:#666; --line:#ddd; --card:#fafafa; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.55 system-ui, sans-serif; }}
header {{ position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:8px 16px; z-index:1; }}
main {{ padding:8px 16px 40px; max-width:1500px; }}
h2 {{ margin:22px 0 4px; font-size:17px; }} h3 {{ margin:0 0 4px; font-size:15px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin:12px 0; }}
.meta {{ color:var(--muted); font-size:12.5px; margin:2px 0; }} .meta b {{ color:var(--fg); }}
.imgs {{ display:flex; flex-wrap:wrap; gap:10px; align-items:flex-start; margin-top:6px; }}
figure {{ margin:0; max-width:100%; }} figure img, img.section {{ max-width:100%; border:1px solid var(--line); background:#fff; }}
figcaption {{ font-weight:600; font-size:12px; }}
details {{ margin:6px 0; }} summary {{ cursor:pointer; color:var(--muted); font-size:12.5px; }}
table {{ border-collapse:collapse; width:100%; margin-top:6px; background:#fff; }}
td, th {{ border:1px solid var(--line); padding:3px 6px; vertical-align:top; text-align:left; font-size:13px; }}
tr.yrow td {{ background:#fff7e6; }}
.form {{ margin-top:8px; display:flex; flex-direction:column; gap:4px; }} .q label {{ margin-right:12px; white-space:nowrap; }}
.note {{ width:100%; max-width:800px; }}
</style></head>
<body>
<header>
  <strong>y 절단 LLM 병합 허용안 — 새로 합쳐진 블록 검수</strong>
  <button type="button" id="export">TSV 내보내기(선택)</button>
</header>
<main><p class="meta">{intro}</p>{body}</main>
<script id="cards" type="application/json">{cards_json}</script>
<script>
const CARDS = JSON.parse(document.getElementById('cards').textContent);
const KEY = {store_key} + (new URLSearchParams(location.search).get('sandbox') ? ':sandbox' : '');
let store = {{}};
try {{ store = JSON.parse(localStorage.getItem(KEY) || '{{}}') || {{}}; }} catch (e) {{ store = {{}}; }}
function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(store)); }} catch (e) {{}} }}
CARDS.forEach(c => {{
  const el = document.querySelector('.card[data-id="' + CSS.escape(c.id) + '"]'), s = store[c.id] || {{}};
  el.querySelectorAll('input[type=radio]').forEach(r => {{
    r.checked = s.judgment === r.value;
    r.addEventListener('change', () => {{ store[c.id] = Object.assign({{}}, store[c.id], {{judgment: r.value}}); save(); }});
  }});
  const note = el.querySelector('.note'); note.value = s.note || '';
  note.addEventListener('input', e => {{ store[c.id] = Object.assign({{}}, store[c.id], {{note: e.target.value}}); save(); }});
  el.querySelector('.clear').addEventListener('click', () => {{
    store[c.id] = Object.assign({{}}, store[c.id], {{judgment: ''}}); save();
    el.querySelectorAll('input[type=radio]').forEach(r => r.checked = false);
  }});
}});
document.getElementById('export').addEventListener('click', () => {{
  const clean = v => String(v == null ? '' : v).replace(/[\\t\\r\\n]+/g, ' ');
  const rows = ['card_id\\tsection\\tcases\\tjudgment\\tnote'].concat(CARDS.map(c => {{
    const s = store[c.id] || {{}};
    return [c.id, c.section, c.cases.join(','), s.judgment, s.note].map(clean).join('\\t');
  }}));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([rows.join('\\n') + '\\n'], {{type: 'text/tab-separated-values'}}));
  a.download = 'merge_post_round4_review_' + new Date().toISOString().slice(0, 10) + '.tsv'; a.click();
}});
</script>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", action="append", required=True, help="llm-run 출력 폴더(B3 · B3_yonly). 여러 번 줄 수 있다")
    ap.add_argument("--exclude-known", help="이 폴더의 B3_yonly에 같은 영역 구성으로 있는 블록은 카드에서 뺀다")
    ap.add_argument("--round1", help="1차 산출물 폴더(checkset.json · review/ TSV). 없으면 1차 판정을 붙이지 않는다")
    ap.add_argument("--sections", help="섹션 목록 파일(줄마다 원본/섹션). 없으면 4차 LLM_SECTIONS 18개")
    ap.add_argument("--store-key", default="merge-post-round4-review-v1", help="브라우저 저장 키(페이지마다 다르게)")
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    out = Path(a.out)
    step4.check_round_code("round4")  # B3 채택 뒤 코드에서 잎 번호를 다시 계산한다
    known = None
    if a.exclude_known:
        kd = Path(a.exclude_known) / "B3_yonly"
        known = {(f"{p.parent.parent.name}/{p.stem}", frozenset(rkeys(b)))
                 for p in kd.glob("*/merge/*.json") for b in jsonio.load_merge(p).blocks}
    cards, failed = [], []
    for run in map(Path, a.run):
        if len(a.run) == 1:
            names = ([x.strip() for x in Path(a.sections).read_text(encoding="utf-8").splitlines() if x.strip()]
                     if a.sections else step4.LLM_SECTIONS)
        else:
            names = [x["section"] for x in json.loads((run / "B3_yonly" / "run.json").read_text(encoding="utf-8"))["plan"]]
        c, f = build(run, Path(a.round1) if a.round1 else None, Path(a.input), out, names,
                     tag=run.name if len(a.run) > 1 else "", known=known)
        cards += c
        failed += f
    cards.sort(key=lambda c: (c["group"], c["tag"], c["section"], c["y"]["order"]))
    n_cases = sum(len(c["cases"]) for c in cards)
    intro = (f"새로 합쳐진 블록 {len(cards)}개(1차 사람 판정 사례 {n_cases}건). 질문: 이 결과를 하나의 문단 · 편집 블록으로 묶어도 되는가 — "
             "읽히는지만이 아니라 독립된 제목 · 제품 설명의 구분이 유지되는지 본다. 개선 후보는 글자 누락 · 추가 · 줄 순서 · 문맥도 본다. "
             "판정 선택은 필수가 아니다(채팅 의견으로 충분). 기존 LLM 실행 결과만 쓴다(추가 호출 없음). "
             f"미검증: {', '.join(failed) or '없음'}(호출 실패 — 재실행하지 않음). 이 페이지는 정답이 아니며 채택 승인이 아니다.")
    body, last = "", None
    for c in cards:
        if c["group"] != last:
            body += f"<h2>{esc(GROUP_TITLE.get(c['group'], '기타'))}</h2>"
            last = c["group"]
        body += card_html(c)
    data = [{"id": c["id"], "section": c["section"], "cases": [x["id"] for x in c["cases"]]} for c in cards]
    page = PAGE.format(intro=esc(intro), body=body, cards_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
                       store_key=json.dumps(a.store_key))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"{out / 'index.html'}: 블록 {len(cards)}개 · 사례 {n_cases}건 · 미검증 {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
