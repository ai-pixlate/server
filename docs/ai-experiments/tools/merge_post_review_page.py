"""③ OCR 후처리 교차 블록 병합 점검의 사람 검수 페이지 생성(2026-10-23).

    python docs/ai-experiments/tools/merge_post_review_page.py --run pipeline/out/merge-post-gutter-check_2026-10-23 \
        --input pipeline/samples/local/blocks-input-v1 [--out RUN_DIR/review]

입력: merge_post_gutter_check.py의 RUN_DIR/cases.json과 고정 입력본(섹션 이미지). 출력: <out>/index.html · <out>/img/.
사례 카드마다 번호 없는 crop · 번호 crop(빨강 = xycut 재조립 순번, 파랑 = 끈 상태 순번) · 섹션 축소 이미지(빨간 틀 = 사례 위치) ·
끈 상태 원문과 xycut 재조립 텍스트 · 판정 선택지가 있다. 분류와 묶음(A 순서 비교 · B 순서 같음 · C 범위 다름)은 검수 명세
(2026-10-23_03-merge_post-review-spec-v1.md)와 같다. 선택은 브라우저에만 저장되고(localStorage) 중간에도 TSV로 내보내거나
내보낸 TSV를 다시 불러올 수 있다. 미검수는 판정이 비어 있는 상태이며 `undecidable`과 다르다. 이 페이지는 정답이 아니다.
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

ORDER_CHOICES = ["xycut_better", "off_better", "both_ok", "both_wrong", "undecidable"]
SCOPE_CHOICES = ["scope_xycut", "scope_off", "scope_both_ok", "scope_both_wrong", "undecidable"]
TAGS = ["superscript", "fragment", "package", "document", "duplicate", "typo", "other"]
GROUPS = {"A": "A 순서 비교(범위 같음 · 순서 다름)", "B": "B 순서 같음(범위 같음)", "C": "C 범위 다름"}
MIN_CROP_W = 640  # 작은 crop은 이 폭 이상이 되도록 정수배로 키운다(최대 4배)
THUMB_W = 280


def group_of(c: dict) -> str:
    if not c["exact_cover"]:
        return "C"
    return "B" if c["same_text_order"] else "A"


def case_id(c: dict) -> str:
    return f"{c['section']}/{c['final_block']}"


def file_stem(cid: str) -> str:
    return cid.replace("/", "_")


def section_image(inp: Path, section: str) -> Path:
    img, sec_key = section.split("/")
    split = jsonio.load_split(inp / img / "split.json")
    sec = next(s for s in split.sections if s.section_key == sec_key)
    p = Path(sec.image_path)
    return p if p.is_absolute() else inp / img / p


FONT = ImageFont.load_default(size=16)


def label(d: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, color: tuple[int, int, int]) -> None:
    x, y = xy
    l, t, r, b = d.textbbox((x, y), text, font=FONT)
    d.rectangle([l - 2, t - 1, r + 2, b + 1], fill=(255, 255, 255), outline=color)
    d.text((x, y), text, fill=color, font=FONT)


def render_case(src: Path, c: dict, out_img: Path, stem: str, margin: int = 40) -> dict[str, str]:
    x, y, w, h = c["bbox"]
    with Image.open(src) as im:
        sec = im.convert("RGB")
    box = (max(0, x - margin), max(0, y - margin), min(sec.width, x + w + margin), min(sec.height, y + h + margin))
    crop = sec.crop(box)
    k = min(4, max(1, math.ceil(MIN_CROP_W / max(1, crop.width))))
    if k > 1:
        crop = crop.resize((crop.width * k, crop.height * k), Image.LANCZOS)
    plain = crop.copy()
    d = ImageDraw.Draw(crop)
    off_pos = {key: i for i, key in enumerate(c["off_regions"], start=1)}
    for i, (key, rx, ry, rw, rh) in enumerate(c["regions"], start=1):
        x0, y0 = (rx - box[0]) * k, (ry - box[1]) * k
        d.rectangle([x0, y0, x0 + rw * k, y0 + rh * k], outline=(220, 0, 0), width=2)
        label(d, (x0 + 2, max(0, y0 - 20)), f"x{i}", (200, 0, 0))
        if key in off_pos:
            label(d, (x0 + rw * k - 34, max(0, y0 - 20)), f"o{off_pos[key]}", (0, 0, 200))
    # 섹션 축소 이미지: 긴 섹션은 사례 위아래 일부만 잘라 축소한다
    top, bottom = max(0, y - 2 * sec.width), min(sec.height, y + h + 2 * sec.width)
    ctx = sec.crop((0, top, sec.width, bottom))
    s = THUMB_W / ctx.width
    thumb = ctx.resize((THUMB_W, max(1, round(ctx.height * s))), Image.LANCZOS)
    ImageDraw.Draw(thumb).rectangle([x * s, (y - top) * s, (x + w) * s, (y + h - top) * s], outline=(220, 0, 0), width=2)
    out_img.mkdir(parents=True, exist_ok=True)
    names = {"plain": f"{stem}_plain.png", "marked": f"{stem}_marked.png", "section": f"{stem}_section.png"}
    plain.save(out_img / names["plain"])
    crop.save(out_img / names["marked"])
    thumb.save(out_img / names["section"])
    return names


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def card(c: dict, names: dict[str, str]) -> str:
    cid, g = c["id"], c["group"]
    choices = SCOPE_CHOICES if g == "C" else ORDER_CHOICES
    radios = "".join(
        f'<label><input type="radio" name="j:{esc(cid)}" value="{v}"> {v}</label>' for v in choices
    )
    tags = "".join(f'<label><input type="checkbox" data-tag="{t}"> {t}</label>' for t in TAGS)
    causes = ", ".join(c["causes"])
    return f"""
<section class="card" data-id="{esc(cid)}" data-group="{g}">
  <h3>{esc(cid)} <span class="grp">{esc(GROUPS[g])}</span> <span class="cause">점검 분류: {esc(causes)}</span></h3>
  <div class="imgs">
    <figure><img src="img/{names['plain']}" alt="번호 없는 crop"><figcaption>번호 없음</figcaption></figure>
    <figure class="marked"><img src="img/{names['marked']}" alt="번호 crop"><figcaption>빨강 x = xycut 순번 · 파랑 o = 끈 상태 순번</figcaption></figure>
    <figure><img src="img/{names['section']}" alt="섹션 위치"><figcaption>섹션 안 위치(빨간 틀)</figcaption></figure>
  </div>
  <div class="texts">
    <div><h4>끈 상태(블록 순서대로)</h4><pre>{esc(c['off_text'])}</pre></div>
    <div><h4>xycut 재조립(대리 병합)</h4><pre>{esc(c['xycut_merged_text'])}</pre></div>
  </div>
  <div class="form">
    <div class="radios">{radios} <button type="button" class="clear">판정 지우기</button></div>
    <div class="tags">{tags}</div>
    <input type="text" class="note" placeholder="메모(근거)">
    <span class="state"></span>
  </div>
</section>"""


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>후처리 교차 병합 검수</title>
<style>
:root {{ --bg:#fff; --fg:#111; --muted:#666; --line:#ddd; --card:#fafafa; --ok:#0a7d2c; --todo:#b45309; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, sans-serif; }}
header {{ position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:8px 16px; z-index:1; }}
header .row {{ display:flex; flex-wrap:wrap; gap:12px; align-items:center; }}
main {{ padding:8px 16px 40px; max-width:1400px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px; margin:12px 0; }}
.card.done {{ border-left:6px solid var(--ok); }} .card.todo {{ border-left:6px solid var(--todo); }}
h3 {{ margin:0 0 6px; font-size:15px; }} .grp, .cause {{ font-weight:normal; color:var(--muted); font-size:12px; margin-left:8px; }}
.imgs {{ display:flex; flex-wrap:wrap; gap:10px; align-items:flex-start; }}
figure {{ margin:0; max-width:100%; }} figure img {{ max-width:100%; border:1px solid var(--line); background:#fff; }}
figcaption {{ color:var(--muted); font-size:12px; }}
body.hide-marked .marked {{ display:none; }}
.texts {{ display:flex; flex-wrap:wrap; gap:12px; }} .texts > div {{ flex:1 1 320px; min-width:0; }}
h4 {{ margin:6px 0 2px; font-size:13px; }} pre {{ white-space:pre-wrap; word-break:break-all; background:#fff; border:1px solid var(--line); padding:6px; margin:0; }}
.form {{ margin-top:8px; display:flex; flex-direction:column; gap:4px; }} .radios label, .tags label {{ margin-right:10px; white-space:nowrap; }}
.note {{ width:100%; max-width:800px; }} .state {{ font-size:12px; color:var(--muted); }}
</style></head>
<body>
<header><div class="row">
  <strong>③ OCR 후처리 교차 병합 검수</strong>
  <span id="counts"></span>
  <label><input type="checkbox" id="hideMarked"> 번호 crop 숨기기</label>
  <button type="button" id="export">TSV 내보내기</button>
  <label>TSV 불러오기 <input type="file" id="import" accept=".tsv,.txt"></label>
</div>
<div style="color:var(--muted);font-size:12px">명세: docs/ai-experiments/2026-10-23_03-merge_post-review-spec-v1.md · 선택은 이 브라우저에만 저장된다(중간에도 TSV로 내보낸다). 판정이 비어 있으면 미검수다. 이 페이지는 정답이 아니다.</div>
</header>
<main>{cards}</main>
<script id="cases" type="application/json">{cases_json}</script>
<script>
const CASES = JSON.parse(document.getElementById('cases').textContent);
const KEY = {store_key};
let store = {{}};
try {{ store = JSON.parse(localStorage.getItem(KEY) || '{{}}') || {{}}; }} catch (e) {{ store = {{}}; }}
function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(store)); }} catch (e) {{}} }}
function cardOf(id) {{ return document.querySelector('.card[data-id="' + CSS.escape(id) + '"]'); }}
function render(id) {{
  const el = cardOf(id), s = store[id] || {{}};
  el.querySelectorAll('input[type=radio]').forEach(r => r.checked = r.value === s.judgment);
  el.querySelectorAll('input[data-tag]').forEach(c => c.checked = (s.tags || []).includes(c.dataset.tag));
  el.querySelector('.note').value = s.note || '';
  const done = !!s.judgment;
  el.classList.toggle('done', done); el.classList.toggle('todo', !done);
  el.querySelector('.state').textContent = done ? '검수함: ' + s.judgment : '미검수';
}}
function counts() {{
  const parts = ['A', 'B', 'C'].map(g => {{
    const ids = CASES.filter(c => c.group === g).map(c => c.id);
    const done = ids.filter(id => (store[id] || {{}}).judgment).length;
    return g + ' ' + done + '/' + ids.length;
  }});
  document.getElementById('counts').textContent = '검수 ' + parts.join(' · ');
}}
function update(id, patch) {{ store[id] = Object.assign({{}}, store[id] || {{}}, patch); save(); render(id); counts(); }}
CASES.forEach(c => {{
  const el = cardOf(c.id);
  el.querySelectorAll('input[type=radio]').forEach(r => r.addEventListener('change', () => update(c.id, {{judgment: r.value}})));
  el.querySelectorAll('input[data-tag]').forEach(cb => cb.addEventListener('change', () => update(c.id, {{
    tags: [...el.querySelectorAll('input[data-tag]:checked')].map(x => x.dataset.tag)}})));
  el.querySelector('.note').addEventListener('input', e => update(c.id, {{note: e.target.value}}));
  el.querySelector('.clear').addEventListener('click', () => update(c.id, {{judgment: ''}}));
  render(c.id);
}});
counts();
document.getElementById('hideMarked').addEventListener('change', e => document.body.classList.toggle('hide-marked', e.target.checked));
const COLS = ['case_id', 'group', 'section', 'final_block', 'judgment', 'tags', 'note', 'reviewed'];
const clean = v => String(v || '').replace(/[\\t\\r\\n]+/g, ' ');
document.getElementById('export').addEventListener('click', () => {{
  const rows = [COLS.join('\\t')].concat(CASES.map(c => {{
    const s = store[c.id] || {{}};
    return [c.id, c.group, c.section, c.final_block, s.judgment, (s.tags || []).join(','), s.note, s.judgment ? '1' : '0'].map(clean).join('\\t');
  }}));
  const blob = new Blob([rows.join('\\n') + '\\n'], {{type: 'text/tab-separated-values'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'merge_post_review_' + new Date().toISOString().slice(0, 10) + '.tsv';
  a.click(); URL.revokeObjectURL(a.href);
}});
document.getElementById('import').addEventListener('change', async e => {{
  const f = e.target.files[0]; if (!f) return;
  const lines = (await f.text()).split(/\\r?\\n/).filter(Boolean);
  const head = lines.shift().split('\\t');
  const ix = k => head.indexOf(k);
  const known = new Set(CASES.map(c => c.id));
  let n = 0;
  lines.forEach(line => {{
    const v = line.split('\\t'), id = v[ix('case_id')];
    if (!known.has(id)) return;
    store[id] = {{judgment: v[ix('judgment')] || '', tags: (v[ix('tags')] || '').split(',').filter(Boolean), note: v[ix('note')] || ''}};
    n++;
  }});
  save(); CASES.forEach(c => render(c.id)); counts();
  alert(n + '건을 불러왔다');
}});
</script>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, help="merge_post_gutter_check.py 출력 폴더(cases.json)")
    ap.add_argument("--input", required=True, help="blocks-input-v1 폴더")
    ap.add_argument("--out", help="기본 RUN_DIR/review")
    a = ap.parse_args(argv)
    run, inp = Path(a.run), Path(a.input)
    out = Path(a.out) if a.out else run / "review"
    cases = json.loads((run / "cases.json").read_text(encoding="utf-8"))
    for c in cases:
        c["id"], c["group"] = case_id(c), group_of(c)
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise SystemExit("사례 ID가 중복된다")
    cases.sort(key=lambda c: (c["group"], c["id"]))
    cards = []
    for c in cases:
        names = render_case(section_image(inp, c["section"]), c, out / "img", file_stem(c["id"]))
        cards.append(card(c, names))
    data = [{k: c[k] for k in ("id", "group", "section", "final_block")} for c in cases]
    page = PAGE.format(
        cards="".join(cards),
        cases_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
        store_key=json.dumps(f"merge-post-review-v1/{run.name}"),
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page, encoding="utf-8")
    by_group = {g: sum(c["group"] == g for c in cases) for g in GROUPS}
    print(f"{out / 'index.html'}: 사례 {len(cases)}건 {by_group}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
