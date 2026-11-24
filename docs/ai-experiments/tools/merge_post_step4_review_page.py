"""③ OCR 후처리 4단계 1차 실험의 사람 검수 페이지 생성(2026-10-23).

    python docs/ai-experiments/tools/merge_post_step4_review_page.py --run pipeline/out/merge-post-step4-round1_2026-10-23 \
        --input pipeline/samples/local/blocks-input-v1 [--out RUN_DIR/review]

검수 요청 범위(실험 기록 `2026-10-23_03-merge_post-step4-round1.md` 5절)만 한 페이지에 모은다. 라벨 판정기 힌트가 붙은 사례는 넣지 않는다.
- S1: 라벨 힌트 없는 과분할 후보 — A0 / A1_gap1.2 / A1_gap1.5
- B3: S2 · S4 중 라벨 힌트 없고 B3가 A0와 다른 사례(같은 영역 집합은 카드 하나) — A0 / B3_no_hratio
- C: GS-01_002/sec_1_08의 위첨자 사례 — A0 / C_tol0.7
- R: GS-03_018/sec_1_08의 A1_gap1.5 칸 합침 회귀 — 섹션 전체 A0 / A1_gap1.5
카드마다 변형별 그림(사례 둘레, 블록마다 다른 색 상자 + 변형 안 블록 순번)과 블록 텍스트(` ‖ ` = 블록 경계, ` / ` = 줄 경계)를 둔다.
S4 대리 재조립 텍스트(〔 〕)는 과거 LLM 문단을 기준으로 블록을 한 묶음으로 조립한 결과이며 실제 LLM 출력이 아니다.
선택은 브라우저에만 저장되고(localStorage) 중간에도 TSV로 내보내거나 다시 불러올 수 있다(복수 선택은 쉼표로 잇는다). `?sandbox=1`이면
별도 저장소를 쓴다(동작 확인용). 의견은 채팅으로 줘도 된다(TSV 선택).
미검수는 판정이 비어 있는 상태이며 `undecidable` · `excluded`와 다르다. 이 페이지는 정답이 아니다.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from pipeline import jsonio  # noqa: E402

FONT = ImageFont.load_default(size=15)
COLORS = [(220, 0, 0), (0, 90, 220), (0, 150, 60), (200, 110, 0), (150, 0, 170), (0, 150, 160), (120, 120, 0)]
MIN_W = 520

GROUPS = {
    "S1": ("S1 과분할 후보 — 실제로 이어져야 할 문단이 잘렸는가", ["A0", "A1_gap1.2", "A1_gap1.5"]),
    "B3": ("B3 높이비 해제 — 같은 줄 연결 · 위첨자 · 조각", ["A0", "B3_no_hratio"]),
    "C": ("C 줄 중심 허용치 0.7 — 위첨자 위치", ["A0", "C_tol0.7"]),
    "R": ("A1_gap1.5 칸 합침 회귀 확인", ["A0", "A1_gap1.5"]),
}
# (키, 제목, 선택지, 복수 선택 여부). 복수 선택에서 EXCLUSIVE 값은 다른 선택과 함께 고를 수 없다
QUESTIONS = {
    "S1": [("q1", "이어져야 할 문단인가", ["joined_needed", "separate_ok", "either_ok", "undecidable", "excluded"], False),
           ("q2", "적절한 결과(복수 선택)", ["A0", "A1_gap1.2", "A1_gap1.5", "none_ok", "undecidable"], True)],
    "B3": [("q1", "판정", ["b3_better", "a0_better", "both_ok", "both_wrong", "undecidable", "excluded"], False)],
    "C": [("q1", "판정", ["c07_better", "a0_better", "both_ok", "both_wrong", "undecidable", "excluded"], False)],
    "R": [("q1", "판정", ["regression", "not_regression", "undecidable"], False)],
}
EXCLUSIVE = ["none_ok", "undecidable"]
LABELS = {
    "joined_needed": "예 — 이어져야 함", "separate_ok": "아니오 — 나뉘는 게 맞음",
    "either_ok": "둘 다 허용 — 이어져도 나뉘어도 문제없음", "undecidable": "판단 불가",
    "excluded": "제외(라벨 등)", "none_ok": "모두 부적절", "b3_better": "B3가 나음",
    "a0_better": "A0가 나음", "c07_better": "C_tol0.7이 나음", "both_ok": "둘 다 허용", "both_wrong": "둘 다 틀림",
    "regression": "회귀 맞음", "not_regression": "회귀 아님",
}


class Section:
    """섹션 이미지 · 원시 영역 bbox 조회."""

    def __init__(self, inp: Path, section: str):
        img, key = section.split("/")
        split = jsonio.load_split(inp / img / "split.json")
        sec = next(s for s in split.sections if s.section_key == key)
        p = Path(sec.image_path)
        self.path = p if p.is_absolute() else inp / img / p
        self.bbox = {r.region_key: r.bbox for r in jsonio.load_ocr(inp / img / "ocr" / f"{key}.json").regions}

    def union(self, regs: list[str]) -> tuple[int, int, int, int]:
        bs = [self.bbox[r] for r in regs]
        return min(b.x for b in bs), min(b.y for b in bs), max(b.x2 for b in bs), max(b.y2 for b in bs)


def label(d, xy, text, color):
    l, t, r, b = d.textbbox(xy, text, font=FONT)
    d.rectangle([l - 2, t - 1, r + 2, b + 1], fill=(255, 255, 255), outline=color)
    d.text(xy, text, fill=color, font=FONT)


def draw_blocks(sec: Section, blocks: list[dict], area: tuple[int, int, int, int], dest: Path, number_from: dict | None = None,
                margin: int = 30, fit_w: int | None = None) -> str:
    """area 둘레를 잘라 blocks를 블록마다 다른 색 상자로 그린다. 번호 = 변형 안 블록 순번(number_from)."""
    with Image.open(sec.path) as im:
        src = im.convert("RGB")
    x0, y0, x1, y1 = area
    box = (max(0, x0 - margin), max(0, y0 - margin), min(src.width, x1 + margin), min(src.height, y1 + margin))
    crop = src.crop(box)
    if fit_w:
        k = fit_w / crop.width
    else:
        k = min(4, max(1, math.ceil(MIN_W / max(1, crop.width))))
    if k != 1:
        crop = crop.resize((max(1, round(crop.width * k)), max(1, round(crop.height * k))), Image.LANCZOS)
    d = ImageDraw.Draw(crop)
    for i, b in enumerate(blocks):
        c = COLORS[i % len(COLORS)]
        bx0, by0, bx1, by1 = sec.union(b["regions"])
        rect = [(bx0 - box[0]) * k, (by0 - box[1]) * k, (bx1 - box[0]) * k, (by1 - box[1]) * k]
        d.rectangle(rect, outline=c, width=2)
        n = number_from.get(id(b), i + 1) if number_from else i + 1
        label(d, (rect[0] + 2, max(0, rect[1] - 18)), f"{n}", c)
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest)
    return dest.name


def touched(variant: dict, section: str, regs: list[str]) -> list[dict]:
    want = set(regs)
    return [b for b in variant["sections"][section]["blocks"] if want & set(b["regions"])]


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def fmt(texts: list[str]) -> str:
    return " ‖ ".join(t.replace("\n", " / ") for t in texts)


def build_cards(run: Path, inp: Path, out_img: Path) -> list[dict]:
    ck = json.loads((run / "checkset.json").read_text(encoding="utf-8"))
    names = {n for g in GROUPS.values() for n in g[1]}
    v = {n: json.loads((run / "variants" / f"{n}.json").read_text(encoding="utf-8")) for n in names}
    secs: dict[str, Section] = {}
    cards: list[dict] = []

    def sec_of(name: str) -> Section:
        if name not in secs:
            secs[name] = Section(inp, name)
        return secs[name]

    def case_card(group: str, ids: list[str], c: dict, proxy: bool) -> dict:
        variants = GROUPS[group][1]
        s = sec_of(c["section"])
        per = {n: touched(v[n], c["section"], c["regions"]) for n in variants}
        all_regs = {r for bs in per.values() for b in bs for r in b["regions"]} | set(c["regions"])
        area = s.union(sorted(all_regs))
        rows, imgs = [], []
        for n in variants:
            blocks = sorted(per[n], key=lambda b: v[n]["sections"][c["section"]]["blocks"].index(b))
            order = {id(b): v[n]["sections"][c["section"]]["blocks"].index(b) + 1 for b in blocks}
            stem = f"{group}_{ids[0].replace(':', '_').replace('/', '_').replace('#', '-')}_{n}"
            imgs.append((n, draw_blocks(s, blocks, area, out_img / f"{stem}.png", order)))
            r = v[n]["cases"][ids[0]]
            text = fmt([b["text"] for b in blocks])
            if proxy and r["proxy_text"] is not None:
                text += f"  〔대리 재조립: {r['proxy_text'].replace(chr(10), ' / ')}〕"
            rows.append((n, len(blocks), len({b["region"] for b in blocks}), text))
        return {"id": f"{group}/{ids[0]}", "group": group, "section": c["section"], "case_ids": ids,
                "basis": c["basis"], "images": imgs, "rows": rows}

    for c in ck["cases"]:
        if c["set"] == "S1" and not c["label_hint"]:
            cards.append(case_card("S1", [c["id"]], c, proxy=False))
    seen: dict[tuple, list[str]] = {}
    first: dict[tuple, dict] = {}
    for c in ck["cases"]:
        if c["set"] not in ("S2", "S4") or c["label_hint"]:
            continue
        a, b = v["A0"]["cases"][c["id"]], v["B3_no_hratio"]["cases"][c["id"]]
        if a["texts"] == b["texts"] and a["proxy_text"] == b["proxy_text"]:
            continue
        k = (c["section"], tuple(c["regions"]))
        seen.setdefault(k, []).append(c["id"])
        first.setdefault(k, c)
    for k, ids in seen.items():
        cards.append(case_card("B3", ids, first[k], proxy=True))
    c_case = next(c for c in ck["cases"] if c["id"] == "S4:GS-01_002/sec_1_08:reg_0006")
    cards.append(case_card("C", [c_case["id"]], c_case, proxy=True))
    # R: 섹션 전체
    name = "GS-03_018/sec_1_08"
    s = sec_of(name)
    rows, imgs = [], []
    for n in GROUPS["R"][1]:
        blocks = v[n]["sections"][name]["blocks"]
        with Image.open(s.path) as im:
            w, h = im.size
        imgs.append((n, draw_blocks(s, blocks, (0, 0, w, h), out_img / f"R_{name.replace('/', '_')}_{n}.png", margin=0, fit_w=560)))
        focus = [b for b in blocks if any(k in b["text"] for k in ("클리어", "착색", "동시 사용"))]
        rows.append((n, len(blocks), v[n]["sections"][name]["regions"],
                     "제품명 · 하단 문구 블록: " + fmt([b["text"] for b in focus])))
    cards.append({"id": f"R:{name}", "group": "R", "section": name, "case_ids": [], "basis": "A1_gap1.5에서 칸 사이 x 절단이 사라짐",
                  "images": imgs, "rows": rows})
    return cards


def card_html(c: dict) -> str:
    qs = QUESTIONS[c["group"]]
    q_html = ""
    for qk, title, opts, multi in qs:
        kind = "checkbox" if multi else "radio"
        radios = "".join(f'<label><input type="{kind}" name="{qk}:{esc(c["id"])}" data-q="{qk}" value="{o}"> {esc(LABELS.get(o, o))}</label>'
                         for o in opts)
        q_html += f'<div class="q"><b>{esc(title)}</b> {radios}</div>'
    imgs = "".join(f'<figure><figcaption>{esc(n)}</figcaption><img src="img/{esc(p)}" alt="{esc(n)}"></figure>' for n, p in c["images"])
    rows = "".join(f"<tr><td>{esc(n)}</td><td>{nb}</td><td>{nr}</td><td>{esc(t)}</td></tr>" for n, nb, nr, t in c["rows"])
    ids = " · ".join(c["case_ids"]) if c["case_ids"] else ""
    return f"""
<section class="card" data-id="{esc(c['id'])}" data-group="{c['group']}">
  <h3>{esc(c['id'])}</h3>
  <div class="meta">근거: {esc(c['basis'])}{(' · 사례 ID: ' + esc(ids)) if ids else ''}</div>
  <div class="imgs">{imgs}</div>
  <table><thead><tr><th>변형</th><th>블록</th><th>후보 영역</th><th>텍스트(‖ 블록 · / 줄)</th></tr></thead><tbody>{rows}</tbody></table>
  <div class="form">{q_html}<input type="text" class="note" placeholder="메모(근거)"> <span class="state"></span>
  <button type="button" class="clear">판정 지우기</button></div>
</section>"""


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>4단계 1차 검수</title>
<style>
:root {{ --bg:#fff; --fg:#111; --muted:#666; --line:#ddd; --card:#fafafa; --ok:#0a7d2c; --todo:#b45309; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, sans-serif; }}
header {{ position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:8px 16px; z-index:1; }}
header .row {{ display:flex; flex-wrap:wrap; gap:12px; align-items:center; }}
nav a {{ margin-right:10px; }}
main {{ padding:8px 16px 40px; max-width:1500px; }}
h2 {{ margin:20px 0 4px; font-size:17px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin:12px 0; }}
.card.done {{ border-left:6px solid var(--ok); }} .card.todo {{ border-left:6px solid var(--todo); }}
h3 {{ margin:0; font-size:15px; }} .meta {{ color:var(--muted); font-size:12px; margin-bottom:6px; }}
.imgs {{ display:flex; flex-wrap:wrap; gap:10px; align-items:flex-start; }}
figure {{ margin:0; max-width:100%; }} figure img {{ max-width:100%; border:1px solid var(--line); background:#fff; }}
figcaption {{ font-weight:600; font-size:12px; }}
table {{ border-collapse:collapse; width:100%; margin-top:6px; background:#fff; }}
td, th {{ border:1px solid var(--line); padding:3px 6px; vertical-align:top; text-align:left; font-size:13px; }}
td:last-child {{ word-break:break-all; }}
.form {{ margin-top:8px; display:flex; flex-direction:column; gap:4px; }} .q label {{ margin-right:10px; white-space:nowrap; }}
.note {{ width:100%; max-width:800px; }} .state {{ font-size:12px; color:var(--muted); }}
</style></head>
<body>
<header><div class="row">
  <strong>③ OCR 후처리 4단계 1차 검수</strong> <span id="counts"></span>
  <button type="button" id="export">TSV 내보내기</button>
  <label>TSV 불러오기 <input type="file" id="import" accept=".tsv,.txt"></label>
</div>
<nav>{nav}</nav>
<div style="color:var(--muted);font-size:12px">그림: 블록마다 다른 색 상자, 숫자 = 그 변형 안 블록 순번. 라벨 판정기 힌트 사례는 넣지 않았다. 판정이 비어 있으면 미검수다(판단 불가 · 제외와 다름). 의견은 채팅으로 줘도 된다. 이 페이지는 정답이 아니다.</div>
</header>
<main>{body}</main>
<script id="cards" type="application/json">{cards_json}</script>
<script>
const CARDS = JSON.parse(document.getElementById('cards').textContent);
// ?sandbox=1이면 별도 저장소를 쓴다(동작 확인용 — 실제 검수 값과 섞이지 않게)
const KEY = {store_key} + (new URLSearchParams(location.search).get('sandbox') ? ':sandbox' : '');
const EXCLUSIVE = {exclusive};
let store = {{}};
try {{ store = JSON.parse(localStorage.getItem(KEY) || '{{}}') || {{}}; }} catch (e) {{ store = {{}}; }}
// 이전 형식(복수 선택 질문을 문자열 하나로 저장)을 배열로 바꾼다. 'same_all'은 세 변형 모두 선택으로 본다
CARDS.forEach(c => {{
  const s = store[c.id]; if (!s) return;
  c.multi.forEach(q => {{
    if (typeof s[q] === 'string') s[q] = s[q] === 'same_all' ? ['A0', 'A1_gap1.2', 'A1_gap1.5'] : (s[q] ? [s[q]] : []);
  }});
}});
function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(store)); }} catch (e) {{}} }}
function cardOf(id) {{ return document.querySelector('.card[data-id="' + CSS.escape(id) + '"]'); }}
const filled = v => Array.isArray(v) ? v.length > 0 : !!v;
function done(c) {{ const s = store[c.id] || {{}}; return c.questions.every(q => filled(s[q])); }}
function render(c) {{
  const el = cardOf(c.id), s = store[c.id] || {{}};
  el.querySelectorAll('input[type=radio]').forEach(r => r.checked = s[r.dataset.q] === r.value);
  el.querySelectorAll('input[type=checkbox][data-q]').forEach(r => r.checked = (s[r.dataset.q] || []).includes(r.value));
  el.querySelector('.note').value = s.note || '';
  const ok = done(c);
  el.classList.toggle('done', ok); el.classList.toggle('todo', !ok);
  el.querySelector('.state').textContent = ok ? '검수함' : (c.questions.some(q => filled(s[q])) ? '일부 선택' : '미검수');
}}
function counts() {{
  const g = {{}};
  CARDS.forEach(c => {{ g[c.group] = g[c.group] || [0, 0]; g[c.group][1]++; if (done(c)) g[c.group][0]++; }});
  document.getElementById('counts').textContent = '검수 ' + Object.entries(g).map(([k, v]) => k + ' ' + v[0] + '/' + v[1]).join(' · ');
}}
function update(c, patch) {{ store[c.id] = Object.assign({{}}, store[c.id] || {{}}, patch); save(); render(c); counts(); }}
CARDS.forEach(c => {{
  const el = cardOf(c.id);
  el.querySelectorAll('input[type=radio]').forEach(r => r.addEventListener('change', () => update(c, {{[r.dataset.q]: r.value}})));
  el.querySelectorAll('input[type=checkbox][data-q]').forEach(r => r.addEventListener('change', () => {{
    const q = r.dataset.q;
    let v = [...el.querySelectorAll('input[type=checkbox][data-q="' + q + '"]:checked')].map(x => x.value);
    // 모두 부적절 · 판단 불가는 다른 선택과 함께 고를 수 없다 — 방금 누른 쪽을 남긴다
    if (r.checked) v = EXCLUSIVE.includes(r.value) ? [r.value] : v.filter(x => !EXCLUSIVE.includes(x));
    update(c, {{[q]: v}});
  }}));
  el.querySelector('.note').addEventListener('input', e => update(c, {{note: e.target.value}}));
  el.querySelector('.clear').addEventListener('click', () => {{
    const p = {{}}; c.questions.forEach(q => p[q] = c.multi.includes(q) ? [] : ''); update(c, p);
  }});
  render(c);
}});
counts();
const COLS = ['card_id', 'group', 'section', 'case_ids', 'q1', 'q2', 'note', 'reviewed'];
const clean = v => String(v == null ? '' : v).replace(/[\\t\\r\\n]+/g, ' ');
document.getElementById('export').addEventListener('click', () => {{
  const rows = [COLS.join('\\t')].concat(CARDS.map(c => {{
    const s = store[c.id] || {{}};
    const val = q => Array.isArray(s[q]) ? s[q].join(',') : s[q];  // 복수 선택은 쉼표로 잇는다
    return [c.id, c.group, c.section, c.case_ids.join(','), val('q1'), val('q2'), s.note, done(c) ? '1' : '0'].map(clean).join('\\t');
  }}));
  const blob = new Blob([rows.join('\\n') + '\\n'], {{type: 'text/tab-separated-values'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'merge_post_step4_review_' + new Date().toISOString().slice(0, 10) + '.tsv';
  a.click(); URL.revokeObjectURL(a.href);
}});
document.getElementById('import').addEventListener('change', async e => {{
  const f = e.target.files[0]; if (!f) return;
  const lines = (await f.text()).split(/\\r?\\n/).filter(Boolean);
  const head = lines.shift().split('\\t'), ix = k => head.indexOf(k);
  const known = new Map(CARDS.map(c => [c.id, c]));
  let n = 0;
  lines.forEach(line => {{
    const v = line.split('\\t'), c = known.get(v[ix('card_id')]);
    if (!c) return;
    const s = {{note: v[ix('note')] || ''}};
    ['q1', 'q2'].forEach(q => {{
      const raw = v[ix(q)] || '';
      s[q] = c.multi.includes(q) ? (raw === 'same_all' ? ['A0', 'A1_gap1.2', 'A1_gap1.5'] : raw.split(',').filter(Boolean)) : raw;
    }});
    store[c.id] = s; n++;
  }});
  save(); CARDS.forEach(render); counts(); alert(n + '건을 불러왔다');
}});
</script>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    run, inp = Path(a.run), Path(a.input)
    out = Path(a.out) if a.out else run / "review"
    cards = build_cards(run, inp, out / "img")
    ids = [c["id"] for c in cards]
    if len(set(ids)) != len(ids):
        raise SystemExit("카드 ID가 중복된다")
    body, nav = "", ""
    for g, (title, _) in GROUPS.items():
        sel = [c for c in cards if c["group"] == g]
        nav += f'<a href="#g-{g}">{esc(g)} ({len(sel)})</a>'
        body += f'<h2 id="g-{g}">{esc(title)} — {len(sel)}건</h2>' + "".join(card_html(c) for c in sel)
    data = [{"id": c["id"], "group": c["group"], "section": c["section"], "case_ids": c["case_ids"],
             "questions": [q for q, _, _, _ in QUESTIONS[c["group"]]],
             "multi": [q for q, _, _, m in QUESTIONS[c["group"]] if m]} for c in cards]
    page = PAGE.format(nav=nav, body=body, cards_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
                       store_key=json.dumps(f"merge-post-step4-review-v1/{run.name}"), exclusive=json.dumps(EXCLUSIVE))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"{out / 'index.html'}: 카드 {len(cards)}건 " + str({g: sum(c['group'] == g for c in cards) for g in GROUPS}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
