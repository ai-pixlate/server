"""A+ 슬롯 내용 육안 검수 갤러리 — 개발용 검수 도구(운영 렌더 아님).

`pipeline.types.AplusModule`을 참고 렌더러(이 폴더의 `renderer/` · `templates/`, seed-builder PR #3 사본) 입력으로 바꿔
970×300 PNG를 찍고, 모듈마다 원본 섹션(사진을 자른 자리 표시) · 문구의 출처 블록과 원문 · 글자 수 한도 · 템플릿 규칙 검사 ·
렌더러 경고를 한 화면에 모은다. 칸마다 판단(템플릿 · 문구 · 사진)과 메모를 적어 CSV로 내려받을 수 있다(브라우저에만 저장).

주의 — 이 이미지는 **매핑과 슬롯 채우기를 눈으로 보기 위한 것**이다. 최종 렌더는 FE가 한다. 글꼴은 조판 계약과 같은 Pretendard지만
글자 자동 축소(최소 80%)는 조판 계약(docs/ai/contract.md 6.4)과 달라서, 여기 보이는 글자 크기 · 넘침은 최종 결과의 근거가 아니다.

치수는 규격 데이터(pipeline/data/aplus/spec.json)의 `regions`를 렌더러 형식으로 내보내 쓴다 — 치수의 정본은 하나다.
실행에는 jinja2 · playwright(+ Chromium)가 필요하다(파이프라인 의존성 아님): `pip install jinja2 playwright` · `playwright install chromium`.

    python docs/ai-experiments/tools/aplus_review/gallery.py --plan aplus_plan.json --split split.json --out 새폴더 [--blocks 원문.json] [--cutout aplus_cutout.json]

`--cutout`(`pipeline.aplus.cutout.run`의 결과)을 주면 누끼에 성공한 이미지 칸은 배경을 지운 그림으로 그리고, 누끼 원천 · 확인 표시 · 실패를 메모로 붙인다.
"""
from __future__ import annotations

import base64
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
for p in (str(REPO_ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from pipeline.aplus import content as aplus_content  # noqa: E402
from pipeline.aplus.spec import AplusSpec, load_spec  # noqa: E402
from pipeline.types import AplusImage, AplusModule  # noqa: E402

NOTICE = ("검수용 렌더입니다. 최종 렌더는 FE가 하며, 글자 자동 축소가 조판 계약과 달라 이 이미지의 글자 크기 · 넘침은 최종 결과의 근거가 아닙니다. "
          "매핑(어느 섹션이 어느 칸 · 템플릿에 갔는지)과 슬롯 채우기(어느 블록이 어느 칸에 들어갔는지)를 봅니다.")
THUMB_W = 360
BOX_COLORS = [(230, 30, 60), (20, 110, 230), (20, 150, 80), (200, 120, 0), (140, 60, 200), (0, 150, 160)]


@dataclass
class Entry:
    """갤러리 한 장. `module`이 None이면 빈 칸으로 표시한다. `sections`는 글 재료로 쓴 섹션(원본 그림을 옆에 보여 준다).
    `cutouts`는 이미지 칸 이름(`image` · `mark` · `items[i].image`) → 배경을 지운 PNG 경로 — 있으면 원본 크롭 대신 그린다."""

    title: str
    module: AplusModule | None
    notes: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    cutouts: dict[str, str] = field(default_factory=dict)


def export_renderer_spec(spec: AplusSpec, path: Path) -> Path:
    """규격 데이터의 구역을 참고 렌더러의 치수 파일 형식({템플릿: [구역…], "_global": …})으로 쓴다."""
    data: dict[str, Any] = {tid: t["regions"] for tid, t in spec.raw["templates"].items()}
    data["_global"] = spec.raw.get("global_notes", {})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return path


def _crop(img: AplusImage, section_images: dict[str, str | Path], out: Path) -> str:
    src = section_images.get(img.section_key)
    if src is None:
        raise KeyError(f"섹션 이미지 경로가 없다: {img.section_key}")
    with Image.open(src) as im:
        im = im.convert("RGB")
        b = img.bbox
        if b.x < 0 or b.y < 0 or b.x2 > im.width or b.y2 > im.height:
            raise ValueError(f"자를 자리 {b}가 섹션 이미지 {im.width}x{im.height} 밖이다: {img.section_key}")
        out.parent.mkdir(parents=True, exist_ok=True)
        im.crop((b.x, b.y, b.x2, b.y2)).save(out)
    return str(out)


def module_images(m: AplusModule) -> list[tuple[str, AplusImage]]:
    """모듈의 이미지 칸 전부 — (이름, 자를 자리)."""
    out: list[tuple[str, AplusImage]] = []
    if m.image is not None:
        out.append(("큰 사진", m.image))
    if m.mark is not None:
        out.append(("마크", m.mark))
    out += [(f"항목{i + 1}", it.image) for i, it in enumerate(m.items) if it.image is not None]
    return out


def to_renderer_content(m: AplusModule, section_images: dict[str, str | Path], image_dir: Path, name: str,
                        cutouts: dict[str, str] | None = None) -> dict[str, Any]:
    """슬롯 내용 → 참고 렌더러 입력 JSON. 이미지 칸은 원본 섹션에서 잘라 `image_dir`에 저장한다.
    `cutouts`에 그 칸의 누끼 PNG가 있으면 그것을 `image_dir`에 복사해 쓴다(전체가 보이게)."""
    cutouts = cutouts or {}

    def picture(field_name: str, img: AplusImage, file: str) -> dict[str, Any]:
        done = cutouts.get(field_name)
        if done is None:
            return {"src": _crop(img, section_images, image_dir / f"{file}.png"), "fit": img.fit}
        image_dir.mkdir(parents=True, exist_ok=True)
        dst = image_dir / f"{file}_cutout.png"
        dst.write_bytes(Path(done).read_bytes())
        return {"src": str(dst), "fit": "contain"}

    c: dict[str, Any] = {"template": m.template, "mirror": m.mirror}
    if m.layout != "default":
        c["layout"] = m.layout
    if m.mode is not None:
        c["mode"] = m.mode
    for k in ("label", "headline", "subheadline", "body"):
        v = getattr(m, k)
        if v is not None:
            c[k] = v.text
    if m.image is not None:
        c["image"] = picture("image", m.image, f"{name}_main")
    if m.mark is not None:
        c["mark"] = picture("mark", m.mark, f"{name}_mark")
    if m.items:
        c["items"] = []
        for i, it in enumerate(m.items):
            d: dict[str, Any] = {}
            if it.title is not None:
                d["title"] = it.title.text
            if it.body is not None:
                d["body"] = it.body.text
            if it.color is not None:
                d["color"] = it.color
            if it.image is not None:
                d["image"] = picture(f"items[{i}].image", it.image, f"{name}_item{i + 1}")
            c["items"].append(d)
    return c


def _source_thumbs(en: Entry, section_images: dict[str, str | Path], out: Path, name: str) -> list[dict[str, Any]]:
    """글 재료 섹션과 사진을 가져온 섹션의 축소 그림. 사진을 자른 자리는 색 상자로 표시한다."""
    boxes: dict[str, list[tuple[str, AplusImage]]] = {}
    if en.module is not None:
        for what, img in module_images(en.module):
            boxes.setdefault(img.section_key, []).append((what, img))
    keys = list(dict.fromkeys(list(en.sections) + list(boxes)))
    thumbs = []
    for key in keys:
        src = section_images.get(key)
        if src is None:
            continue
        with Image.open(src) as im:
            im = im.convert("RGB")
            k = THUMB_W / im.width
            small = im.resize((THUMB_W, max(1, round(im.height * k))), Image.LANCZOS)
        dr = ImageDraw.Draw(small)
        for j, (_, img) in enumerate(boxes.get(key, [])):
            b = img.bbox
            dr.rectangle([b.x * k, b.y * k, b.x2 * k, b.y2 * k], outline=BOX_COLORS[j % len(BOX_COLORS)], width=3)
        fn = out / "sources" / f"{name}_{key}.jpg"
        fn.parent.mkdir(parents=True, exist_ok=True)
        small.save(fn, quality=82)
        thumbs.append({"section_key": key, "file": f"sources/{fn.name}", "is_text_source": key in en.sections,
                       "photos": [(what, BOX_COLORS[j % len(BOX_COLORS)]) for j, (what, _) in enumerate(boxes.get(key, []))]})
    return thumbs


def _limit_rows(m: AplusModule, spec: AplusSpec, block_sources: dict[str, str]) -> str:
    e = html.escape
    rows = []
    for c in aplus_content.limit_checks(m, spec):
        lim = "미정" if c.max_chars is None else str(c.max_chars)
        cls = ' class="over"' if c.over else ""
        if c.field.startswith("items["):
            idx = int(c.field[6:c.field.index("]")])
            text = getattr(m.items[idx], c.field.split(".")[1]).text
        else:
            text = getattr(m, c.field).text
        ko = " / ".join(" ".join(block_sources[k].split()) for k in c.origin_block_keys if k in block_sources)
        rows.append(f"<tr{cls}><th>{e(c.field)}</th><td>{e(text)}<div class='ko'>{e(ko)}</div></td><td class='n'>{c.chars} / {lim}"
                    f"{' (+' + str(c.over) + ')' if c.over else ''}</td><td class='k'>{e(', '.join(c.origin_block_keys)) or '—'}</td></tr>")
    return "".join(rows)


CSS = """
:root{--bg:#f4f5f7;--card:#fff;--ink:#1d2127;--sub:#5d6670;--line:#dfe3e8;--acc:#2f6fd6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 "Malgun Gothic",system-ui,sans-serif}
header{position:sticky;top:0;z-index:3;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0}button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
main{max-width:1480px;margin:0 auto;padding:12px 16px 80px}
.notice{background:#fff8e1;border:1px solid #f0d98a;border-radius:8px;padding:10px 14px;margin:10px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-top:16px}
.card h2{margin:0 0 8px;font-size:16px}.card h2 small{color:var(--sub);font-weight:400}
.grid{display:grid;grid-template-columns:392px 1fr;gap:16px;align-items:start}
@media (max-width:1100px){.grid{grid-template-columns:1fr}}
.src{display:flex;flex-direction:column;gap:10px;max-height:720px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px;background:#fafbfc}
.src figure{margin:0}.src img{width:100%;display:block;border:1px solid var(--line);margin-top:4px}.src figcaption{font-size:12.5px}
.sw{display:inline-block;width:10px;height:10px;margin:0 3px 0 6px;vertical-align:middle}
.render img{width:100%;max-width:970px;display:block;border:1px solid var(--line)}
table{border-collapse:collapse;width:100%;margin-top:10px}td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
th{width:120px;color:var(--sub);font-weight:600}td.n{width:105px;white-space:nowrap}td.k{width:150px;color:var(--sub);font-size:12px;word-break:break-all}
.ko{color:var(--sub);font-size:12.5px;margin-top:2px}
tr.over td.n{color:#c62828;font-weight:700}.err{color:#c62828}.warn{color:#b45309}.note{color:var(--sub)}.empty{color:#c62828;font-weight:700}
ul{margin:8px 0 0;padding-left:20px}
.rev{display:flex;flex-wrap:wrap;gap:8px 16px;margin-top:10px;align-items:center}.rev label{font-size:13px}
textarea{width:100%;min-height:42px;font:inherit;border:1px solid var(--line);border-radius:6px;padding:6px;margin-top:6px}
"""
JS = """
const KEY='aplus_review_'+document.body.dataset.key;let st={};try{st=JSON.parse(localStorage.getItem(KEY)||'{}')}catch(e){}
function save(){try{localStorage.setItem(KEY,JSON.stringify(st))}catch(e){}}
document.querySelectorAll('.card[data-name]').forEach(el=>{const id=el.dataset.name,v=st[id]||{};
 el.querySelectorAll('select').forEach(s=>{if(v[s.name])s.value=v[s.name];s.onchange=()=>{st[id]={...(st[id]||{}),[s.name]:s.value};save()}});
 const t=el.querySelector('textarea');if(t){t.value=v.m||'';t.oninput=()=>{st[id]={...(st[id]||{}),m:t.value};save()}}});
document.getElementById('csv').onclick=()=>{const rows=[['칸','템플릿','템플릿 판단','문구 판단','사진 판단','메모']];
 document.querySelectorAll('.card[data-name]').forEach(el=>{const v=st[el.dataset.name]||{};rows.push([el.dataset.title,el.dataset.tid,v.t||'',v.c||'',v.p||'',v.m||''])});
 const csv='\\ufeff'+rows.map(r=>r.map(x=>'"'+String(x).replace(/"/g,'""')+'"').join(',')).join('\\r\\n');
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='aplus_review.csv';a.click()};
"""


def _sel(name: str, opts: list[str]) -> str:
    return f'<select name="{name}"><option value="">-</option>' + "".join(f"<option>{o}</option>" for o in opts) + "</select>"


def _data_uri(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64," + base64.standard_b64encode(path.read_bytes()).decode("ascii")


def render_gallery(entries: list[Entry], section_images: dict[str, str | Path], out_dir: str | Path, *,
                   title: str = "A+ 슬롯 내용 검수", spec: AplusSpec | None = None, scale: int = 1,
                   block_sources: dict[str, str] | None = None, inline: bool = True) -> dict[str, Any]:
    """entries를 차례로 그려 `out_dir/index.html` · `renders/` · `content/` · `images/` · `sources/` · `report.json`을 만든다.
    템플릿 규칙을 어긴 모듈은 그리지 않고 어긴 항목을 보여 준다. `block_sources`(block_key → 원문)를 주면 문구 아래에 원문을 붙인다.
    `inline`이면 index.html에 그림을 넣어 파일 하나로 열 수 있게 한다. 돌려주는 값은 report.json 내용."""
    from renderer import ContentError, Renderer  # noqa: PLC0415 — jinja2 · playwright는 이 도구에서만 필요하다

    spec = spec or load_spec()
    block_sources = block_sources or {}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    spec_file = export_renderer_spec(spec, out / "_renderer_spec" / "templates_spec.json")
    e = html.escape

    def src_of(rel: str) -> str:
        return _data_uri(out / rel) if inline else e(rel)

    cards: list[str] = []
    report: list[dict[str, Any]] = []
    with Renderer(scale=scale, spec_path=spec_file) as r:
        for i, en in enumerate(entries, start=1):
            m = en.module
            name = f"{i:02d}_{m.template if m else 'empty'}"
            rec: dict[str, Any] = {"name": name, "title": en.title, "template": m.template if m else None,
                                   "errors": [], "warnings": [], "limit_excesses": [], "png": None}
            notes = "".join(f"<li class='note'>{e(n)}</li>" for n in en.notes)
            thumbs = _source_thumbs(en, section_images, out, name)
            figs = "".join(
                f"<figure><figcaption><b>{e(t['section_key'])}</b>{' · 글 재료' if t['is_text_source'] else ''}"
                + "".join(f"<span class='sw' style='background:rgb{c}'></span>{e(w)}" for w, c in t["photos"])
                + f"</figcaption><img src='{src_of(t['file'])}' loading='lazy' alt=''></figure>" for t in thumbs)
            src_html = f"<div class='src'><b>원본 섹션</b>{figs or '<span class=note>없음</span>'}</div>"
            review = (f"<div class='rev'><label>템플릿 {_sel('t', ['좋음', 'A/B 바꾸기', '다른 칸이 맞음', '비워야 함'])}</label>"
                      f"<label>문구 {_sel('c', ['좋음', '블록 선택 문제', '빠진 내용 있음'])}</label>"
                      f"<label>사진 {_sel('p', ['좋음', '자리 다시', '다른 사진', '누끼 문제', '누끼하면 안 됨', '누끼해야 함'])}</label></div>"
                      "<textarea placeholder='메모'></textarea>")
            if m is None:
                cards.append(f"<div class='card' data-name='{e(name)}' data-title='{e(en.title)}' data-tid=''><h2>{e(en.title)} — "
                             f"<span class='empty'>빈 칸</span></h2><div class='grid'>{src_html}<div><ul>{notes}</ul>{review}</div></div></div>")
                report.append(rec)
                continue
            rec["errors"] = aplus_content.validate_module(m, spec)
            img_html = ""
            if not rec["errors"]:
                try:
                    raw = to_renderer_content(m, section_images, out / "images", name, en.cutouts)
                    (out / "content").mkdir(exist_ok=True)
                    (out / "content" / f"{name}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
                    rep = r.render(raw, out / "renders" / f"{name}.png", base_dir=out)
                    rec["warnings"] = rep["warnings"]
                    rec["png"] = f"renders/{name}.png"
                    img_html = f"<div class='render'><img src='{src_of(rec['png'])}' alt=''></div>"
                except (ContentError, KeyError, ValueError, OSError) as x:
                    rec["errors"].append(f"그리기 실패: {x}")
            known = m.template in spec.template_ids
            rec["limit_excesses"] = [{"field": c.field, "chars": c.chars, "max_chars": c.max_chars, "over": c.over}
                                     for c in aplus_content.limit_excesses(m, spec)] if known else []
            t = spec.template(m.template) if known else {}
            sub = f"{m.template} · {t.get('frame', '')} · {t.get('amazon_module') or '자체 배치'}"
            variant = " · ".join(x for x in (f"layout {m.layout}" if m.layout != "default" else "", f"mode {m.mode}" if m.mode else "",
                                             "좌우 반전" if m.mirror else "") if x)
            msgs = "".join(f"<li class='err'>{e(x)}</li>" for x in rec["errors"]) + \
                   "".join(f"<li class='warn'>[검수 렌더] {e(x)}</li>" for x in rec["warnings"]) + notes
            rows = _limit_rows(m, spec, block_sources) if known else ""
            cards.append(f"<div class='card' data-name='{e(name)}' data-title='{e(en.title)}' data-tid='{e(m.template)}'>"
                         f"<h2>{e(en.title)} <small>{e(sub)}{' · ' + e(variant) if variant else ''}</small></h2>"
                         f"<div class='grid'>{src_html}<div>{img_html}{'<ul>' + msgs + '</ul>' if msgs else ''}"
                         f"<table><tr><th>칸</th><th>문구(아래 회색 = 원문)</th><th>글자 수 / 한도</th><th>출처 블록</th></tr>{rows}</table>"
                         f"{review}</div></div></div>")
            report.append(rec)
    page = (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body data-key='{e(title)}'>"
            f"<header><h1>{e(title)}</h1><button id='csv'>판단 결과 CSV 내려받기</button></header><main>"
            f"<div class='notice'>{e(NOTICE)}<br>규격 데이터 {e(spec.version)} · 빨간 숫자 = 글자 수 한도 초과(자르지 않고 표시만) · "
            f"한도 '미정' = 자료에 값이 없는 조합 · 원본 섹션의 색 상자 = 사진을 자른 자리 · 판단과 메모는 이 브라우저에만 저장됩니다</div>"
            f"{''.join(cards)}</main><script>{JS}</script></body></html>")
    (out / "index.html").write_text(page, encoding="utf-8", newline="\n")
    result = {"title": title, "spec_version": spec.version, "spec_sha256": spec.sha256, "entries": report}
    (out / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return result


def entries_from_plan(plan: Any, spec: AplusSpec, cutout: Any = None) -> list[Entry]:
    """`AplusPlan`의 7칸을 갤러리 항목으로. 고른 이유 · 글 재료 섹션 · 코드가 바로잡은 점 · 넣지 않은 블록을 메모로 붙인다.
    `cutout`(`AplusCutoutResult`)을 주면 누끼 결과를 칸에 연결하고 원천 · 확인 표시 · 실패를 메모로 붙인다."""
    what_ko = {"image": "큰 사진", "mark": "마크"}
    source_ko = {"erased": "글자 지운 섹션", "original": "원본 섹션"}
    by_slot: dict[int, list[Any]] = {}
    for x in (cutout.items if cutout is not None else []):
        by_slot.setdefault(x.slot, []).append(x)
    reason_ko = {"limit": "한도 직전에서 중단", "not_candidate": "그 칸 라벨의 섹션이 아님", "already_used": "다른 곳에서 이미 씀",
                 "unknown": "보낸 적 없는 블록 ID"}
    out: list[Entry] = []
    for s in plan.slots:
        notes = [f"고른 이유: {s.why}"] if s.why else []
        notes += s.flags
        notes += [f"넣지 않은 블록 — {d.field}: {', '.join(d.block_keys)} ({reason_ko[d.reason]})" for d in s.dropped]
        cuts: dict[str, str] = {}
        for x in by_slot.get(s.slot, []):
            what = what_ko.get(x.field) or f"항목{int(x.field[6:x.field.index(']')]) + 1} 사진"
            if x.status == "ok":
                cuts[x.field] = x.image_path
                notes.append(f"누끼 — {what}: {source_ko[x.source]}에서 자름, {x.width}×{x.height}px")
            else:
                notes.append(f"누끼 실패 — {what}: {x.error} (원본 크롭으로 그림)")
            notes += [f"누끼 — {what}: {f}" for f in x.flags]
        out.append(Entry(title=f"칸 {s.slot} {spec.raw['slots'][str(s.slot)]['name_ko']}", module=s.module, notes=notes, sections=list(s.sources),
                         cutouts=cuts))
    return out


def main() -> None:
    import argparse  # noqa: PLC0415

    from pipeline import jsonio  # noqa: PLC0415
    from pipeline.types import AplusCutoutResult, AplusPlan  # noqa: PLC0415

    ap = argparse.ArgumentParser(description="A+ 7칸 구성 결과(AplusPlan JSON)를 검수 화면으로 그린다")
    ap.add_argument("--plan", required=True, help="AplusPlan JSON(status=ok)")
    ap.add_argument("--split", required=True, help="섹션 이미지를 찾을 split.json")
    ap.add_argument("--out", required=True, help="새 출력 폴더")
    ap.add_argument("--blocks", help="블록 원문 JSON({block_key: 원문}). 주면 문구 아래에 원문을 붙인다")
    ap.add_argument("--cutout", help="누끼 결과 JSON(AplusCutoutResult). 주면 누끼에 성공한 이미지 칸을 배경을 지운 그림으로 그린다")
    ap.add_argument("--title", default="A+ 슬롯 내용 검수")
    ap.add_argument("--no-inline", action="store_true", help="index.html에 그림을 넣지 않고 파일 경로로 연결한다")
    a = ap.parse_args()
    out = Path(a.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"출력 폴더가 비어 있지 않다: {out}")
    plan = AplusPlan.model_validate_json(Path(a.plan).read_text(encoding="utf-8"))
    if plan.status != "ok":
        raise SystemExit(f"구성 결과가 실패 상태다: {plan.error}")
    spec = load_spec()
    images = {s.section_key: s.image_path for s in jsonio.load_split(a.split).sections}
    sources = json.loads(Path(a.blocks).read_text(encoding="utf-8")) if a.blocks else None
    cut = AplusCutoutResult.model_validate_json(Path(a.cutout).read_text(encoding="utf-8")) if a.cutout else None
    rep = render_gallery(entries_from_plan(plan, spec, cut), images, out, title=a.title, spec=spec, block_sources=sources, inline=not a.no_inline)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for x in rep["entries"]:
        print(f"{x['title']} | {x['template'] or '빈 칸'} | 오류 {len(x['errors'])} · 검수 렌더 경고 {len(x['warnings'])} · 한도 초과 {len(x['limit_excesses'])}")
    print(out / "index.html")


if __name__ == "__main__":
    main()
