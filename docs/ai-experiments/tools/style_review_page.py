"""⑦ 스타일 추출 결과의 육안 검수 페이지 생성 — 정적 HTML, 로컬에서 연다(2026-10-06).

    python docs/ai-experiments/tools/style_review_page.py --run pipeline/out/style-<이름> \
        --input pipeline/samples/local/downstream-input-v1 --out pipeline/out/style-<이름>/review

입력: style_run_driver.py 출력(run.json · <원본>/style/<key>.json)과 고정 입력본(split.json · 섹션 이미지 · merge). 둘 다 읽기만 한다.
원문 · 좌표는 StyleResult에 넣지 않고 입력본 merge에서 원본 + section_key + block_key + region_key로 연결한다(대응이 어긋나면 그 섹션은
오류로 표시하고 그림을 만들지 않는다).
출력: <out>/index.html · <out>/img/(섹션 원본 사본 · 블록 crop · 영역 crop, 모두 원본 픽셀 그대로 자른 PNG). --out이 이미 있거나 입력본 안이면 거부.

표시 원칙(사용자 지시 2026-10-06): 블록 status는 영역 측정 상태이며 품질 합격이 아니다 · excluded · no_text는 오류가 아니다 · null은 값으로
바꾸지 않고 "없음 + 사유"로 쓴다 · 정렬 추정(estimated)과 기본값(default_single_line · default_tie)을 다르게 표시한다 · 수치는 원본 이미지 px다 ·
원문을 임의 폰트로 다시 그리지 않는다(색은 견본만) · 품질 점수 · 자동 판정 · 검수 입력 저장은 없다. 입력 문자열은 모두 HTML 이스케이프한다.
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import jsonio  # noqa: E402
from pipeline.run import _safe_console, fixed_bundle_roots, safe_path_component  # noqa: E402
from pipeline.types import STYLE_SCHEMA_VERSION, BlockStyle, StyleResult, TextBlock  # noqa: E402

STATUS_TEXT = {"ok": "ok — 측정값 모두 얻음(품질 판정 아님)", "partial": "partial — 일부 영역 색 추출 불가",
               "no_text": "no_text — 측정할 글자 영역 없음(오류 아님)", "excluded": "excluded — 라벨 · 로고 제외(오류 아님)"}
BASIS_TEXT = {"estimated": "추정(estimated)", "default_single_line": "기본값 · 한 줄(default_single_line)",
              "default_tie": "기본값 · 분산 동률(default_tie)"}


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def num(v: float | None, nd: int = 3) -> str:
    return "—" if v is None else (repr(v) if nd is None else f"{v:.{nd}f}".rstrip("0").rstrip("."))


def out_guard(out: Path, inp: Path) -> str | None:
    if out.exists():
        return f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {out}"
    ro = out.resolve()
    for root in [inp.resolve(), *fixed_bundle_roots(inp / "MANIFEST.json")]:
        if ro == root or root in ro.parents:
            return f"출력 폴더 {out}가 입력본 {root} 안에 있다 — 입력본은 읽기 전용이다"
    return None


def img_src(name: str) -> str:
    """img/ 아래 파일의 src 값(퍼센트 인코딩 후 HTML 이스케이프)."""
    return esc("img/" + quote(name + ".png"))


def null_text(field: str, reasons: dict[str, str]) -> str:
    return f'<span class="null">없음(null) · 사유 <code>{esc(reasons.get(field, "?"))}</code></span>'


def color_cell(value: str | None, field: str, reasons: dict[str, str]) -> str:
    if value is None:
        return null_text(field, reasons)
    return f'<span class="sw" style="background:{esc(value)}"></span><code>{esc(value)}</code>'


def size_cell(value: float | None, field: str, reasons: dict[str, str]) -> str:
    return null_text(field, reasons) if value is None else f"<b>{esc(repr(value))}</b> px <span class=mut>(원본 이미지 기준)</span>"


def align_cell(b: BlockStyle) -> str:
    if b.align is None:
        return null_text("align", b.null_reasons)
    cls = "est" if b.align_basis == "estimated" else "dflt"
    return f'<b>{esc(b.align)}</b> <span class="basis {cls}">{esc(BASIS_TEXT.get(b.align_basis, b.align_basis))}</span>'


def save_crop(img: np.ndarray, box, path: Path) -> bool:
    if box.w <= 0 or box.h <= 0:
        return False
    Image.fromarray(img[box.y:box.y2, box.x:box.x2]).save(path)
    return True


def load_style_result(path: Path) -> StyleResult:
    """StyleResult 파일 읽기 — 버전 필드가 실제로 있고 STYLE_SCHEMA_VERSION과 같아야 한다(기본값이 조용히 채우지 않게).
    jsonio.load_model은 등록된 공통 타입만 읽으므로 여기서 직접 검증한다."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != STYLE_SCHEMA_VERSION:
        raise ValueError(f"StyleResult 버전 {raw.get('schema_version') if isinstance(raw, dict) else None!r} ≠ {STYLE_SCHEMA_VERSION!r}: {path}")
    return StyleResult.model_validate(raw)


def section_card(run: Path, inp: Path, out: Path, entry: dict[str, Any]) -> tuple[str, dict[str, int]]:
    image_id, key, status = entry.get("image_id"), entry.get("section_key"), entry.get("status")
    sid = f"{image_id}/{key}"
    head = f'<h2>{esc(image_id)} / {esc(key)}</h2>'
    if status != "ok":
        return (f'<section class="card fail" data-image="{esc(image_id)}" data-section="{esc(sid)}">{head}'
                f'<p class="bad">실행 상태 <b>{esc(status)}</b> — 결과 없음. {esc(entry.get("error"))}</p></section>', {"run_fail": 1})
    if not (safe_path_component(image_id) and safe_path_component(key)):
        return f'<section class="card fail">{head}<p class="bad">경로로 쓸 수 없는 식별자 — 표시하지 않음</p></section>', {"fail": 1}
    res = load_style_result(run / entry["result"])
    split = jsonio.load_split(inp / image_id / "split.json")
    section = next(s for s in split.sections if s.section_key == key)
    merged = jsonio.load_merge(inp / image_id / "merge" / f"{key}.json")
    problems = []
    if (res.image_id, res.section_key) != (image_id, key):
        problems.append(f"결과 식별자 {res.image_id}/{res.section_key} ≠ 실행 기록 {sid}")
    by_key: dict[str, TextBlock] = {b.block_key: b for b in merged.blocks}
    if [b.block_key for b in res.blocks] != [b.block_key for b in sorted(merged.blocks, key=lambda b: (b.block_order, b.block_key))]:
        problems.append("결과 블록 목록 ≠ 입력 merge 블록(block_order 순)")
    for b in res.blocks:
        if b.status == "excluded" or b.block_key not in by_key:
            continue
        want = [(ln.line_key, r.region_key) for ln in by_key[b.block_key].source_lines for r in ln.regions]
        if [(r.line_key, r.region_key) for r in b.regions] != want:
            problems.append(f"{b.block_key}: 결과 영역 (줄, 영역) ≠ 입력 source_lines 순서")
    unsafe = [b.block_key for b in res.blocks if not safe_path_component(b.block_key)]
    unsafe += [r.region_key for b in res.blocks for r in b.regions if not safe_path_component(r.region_key)]
    if unsafe:
        problems.append(f"경로로 쓸 수 없는 식별자 {unsafe[:5]}")
    if problems:
        return (f'<section class="card fail" data-image="{esc(image_id)}" data-section="{esc(sid)}">{head}'
                f'<p class="bad">입력 · 결과 대응 불일치 — 그림을 만들지 않음: {esc("; ".join(problems))}</p></section>', {"fail": 1})

    with Image.open(section.image_path) as im:
        if im.mode != "RGB":
            return f'<section class="card fail">{head}<p class="bad">섹션 이미지 모드 {esc(im.mode)}</p></section>', {"fail": 1}
        img = np.array(im)
    H, W = img.shape[:2]
    base = f"{image_id}__{key}"
    shutil.copyfile(section.image_path, out / "img" / f"{base}.png")
    boxes, rows = [], []
    stats = {"blocks": 0, "crops": 0}
    for b in res.blocks:
        src = by_key[b.block_key]
        stats["blocks"] += 1
        bb = src.bbox
        anchor = f"{base}__{b.block_key}"
        crop_ok = save_crop(img, bb, out / "img" / f"{anchor}.png")
        stats["crops"] += crop_ok
        basis = b.align_basis or "null"
        boxes.append(f'<a class="box s-{b.status}" href="#{esc(quote(anchor))}" title="{esc(b.block_key)} · {esc(b.status)}" '
                     f'style="left:{bb.x / W * 100:.4f}%;top:{bb.y / H * 100:.4f}%;width:{bb.w / W * 100:.4f}%;height:{bb.h / H * 100:.4f}%">'
                     f'<span>{esc(b.block_key[-3:])}</span></a>')
        crop_html = (f'<img class="crop" src="{img_src(anchor)}" alt="{esc(b.block_key)} 원본 crop" data-w="{bb.w}">'
                     f'<div class="mut">원본 crop {bb.w}×{bb.h}px · bbox ({bb.x}, {bb.y})</div>') if crop_ok else '<div class="null">crop 없음(면적 0)</div>'
        excl = f'<span class="chip ex">제외 사유 {esc(b.excluded_reason)}</span>' if b.excluded_reason else ""
        c = b.counts
        detail = ""
        if b.status != "excluded":
            rrows = []
            region_src = {r.region_key: (ln.line_key, r) for ln in src.source_lines for r in ln.regions}
            for r in b.regions:
                _, o = region_src[r.region_key]
                rname = f"{anchor}__{r.region_key}"
                rc = save_crop(img, o.bbox, out / "img" / f"{rname}.png")
                stats["crops"] += rc
                ot = r.otsu.model_dump() if r.otsu else None
                rrows.append(
                    f'<tr class="r-{r.status}"><td><code>{esc(r.line_key)}</code><br><code>{esc(r.region_key)}</code></td>'
                    f'<td>{f'<img class="rcrop" src="{img_src(rname)}" alt="영역 crop">' if rc else '<span class="null">crop 없음(면적 0)</span>'}'
                    f'<div class=mut>{o.bbox.w}×{o.bbox.h}px ({o.bbox.x}, {o.bbox.y})</div></td>'
                    f'<td class="txt">{esc(o.text)}</td><td>{esc(r.score)}</td><td>{esc(r.status)}</td>'
                    f'<td>{color_cell(r.font_color, "font_color", {"font_color": r.color_error or r.status})}</td>'
                    f'<td>{color_cell(r.bg_color, "bg_color", {"bg_color": r.color_error or r.status})}</td>'
                    f'<td>{"—(생략)" if r.est_font_px is None else esc(repr(r.est_font_px)) + " px"}</td>'
                    f'<td>{esc(r.color_error) if r.color_error else "—"}</td>'
                    f'<td class=mut>{esc(json.dumps(ot, ensure_ascii=False)) if ot else "—"}</td></tr>')
            d = b.align_diag
            diag = ("std 좌 · 중앙 · 우 = " + " · ".join(num(v) for v in (d.std_left, d.std_center, d.std_right))
                    + f" px · 허용 오차 {num(d.tolerance_px)} px(블록 bbox 폭 {bb.w} × align_tolerance) · 후보 "
                    + (", ".join(d.candidates) if d.candidates else "없음")) if d else "—"
            detail = (f'<details><summary>영역 {c.regions}개 · 정렬 진단 펼치기</summary>'
                      f'<p class="mut">정렬 진단(std는 기록용 실수, 판단은 분산의 정확 비교): {esc(diag)}</p>'
                      f'<div class="scroll"><table class="rt"><thead><tr><th>줄 · 영역</th><th>원본 crop</th><th>OCR 원문</th><th>OCR 점수</th><th>상태</th>'
                      f'<th>글자색</th><th>배경색</th><th>크기</th><th>색 실패 사유</th><th>Otsu 진단</th></tr></thead>'
                      f'<tbody>{"".join(rrows)}</tbody></table></div></details>')
        rows.append(
            f'<article class="blk s-{b.status}" id="{esc(anchor)}" data-image="{esc(image_id)}" data-section="{esc(sid)}" '
            f'data-status="{esc(b.status)}" data-basis="{esc(basis)}">'
            f'<div class="bh"><b>{esc(b.block_key)}</b> <span class="mut">role {esc(src.role)}</span> '
            f'<span class="chip st s-{b.status}">{esc(STATUS_TEXT.get(b.status, b.status))}</span>{excl}</div>'
            f'<div class="bgrid"><div>{crop_html}</div><div>'
            f'<pre class="txt">{esc(src.source_ko)}</pre>'
            f'<table class="kv"><tr><th>글자색</th><td>{color_cell(b.font_color, "font_color", b.null_reasons)}</td></tr>'
            f'<tr><th>배경색</th><td>{color_cell(b.bg_color, "bg_color", b.null_reasons)}</td></tr>'
            f'<tr><th>추정 크기</th><td>{size_cell(b.est_font_px, "est_font_px", b.null_reasons)}</td></tr>'
            f'<tr><th>정렬</th><td>{align_cell(b)}</td></tr>'
            f'<tr><th>영역 수</th><td>전체 {c.regions} · measured {c.measured} · color_failed {c.color_failed} · blank_text {c.blank_text}'
            f'{" <span class=mut>(제외 블록은 추출하지 않아 상태 수 0)</span>" if b.status == "excluded" else ""}</td></tr></table>'
            f'</div></div>{detail}</article>')
    counts = {s: sum(b.status == s for b in res.blocks) for s in ("ok", "partial", "no_text", "excluded")}
    summary = " · ".join(f"{k} {v}" for k, v in counts.items())
    card = (f'<section class="card" data-image="{esc(image_id)}" data-section="{esc(sid)}">{head}'
            f'<p class="mut">섹션 {W}×{H}px · 블록 {len(res.blocks)} ({esc(summary)}) · 결과 <code>{esc(entry["result"])}</code></p>'
            f'<div class="grid"><div class="left"><div class="pic"><img src="{img_src(base)}" alt="{esc(sid)} 원본 섹션">'
            f'{"".join(boxes)}</div><div class="mut">원본 섹션 이미지(⑥ 결과 아님) · 상자 = 블록 bbox, 누르면 해당 블록으로</div></div>'
            f'<div class="right">{"".join(rows)}</div></div></section>')
    return card, stats


PAGE_CSS = """
:root{--bg:#f6f6f4;--fg:#1d1d1f;--card:#fff;--line:#ddd;--mut:#666}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Malgun Gothic",sans-serif}
header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);padding:8px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}
header h1{font-size:16px;margin:0 10px 0 0} header select{font:inherit;padding:3px 6px}
main{padding:12px 16px;max-width:1500px;margin:0 auto} .intro{background:#fff;border:1px solid var(--line);padding:10px 14px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 14px;margin:0 0 16px}
.card h2{font-size:15px;margin:0 0 4px} .card.fail{border-left:5px solid #b00020} .bad{color:#b00020}
.grid{display:grid;grid-template-columns:minmax(220px,380px) 1fr;gap:14px;align-items:start}
.left{position:sticky;top:56px;max-height:calc(100vh - 70px);overflow:auto} .pic{position:relative;line-height:0}
.pic img{width:100%;display:block;border:1px solid var(--line)}
.box{position:absolute;border:2px solid #2a62d9;box-sizing:border-box;text-decoration:none}
.box span{position:absolute;top:-1px;left:-1px;font:10px/1.2 monospace;background:#2a62d9;color:#fff;padding:0 2px}
.box.s-partial{border-color:#d68910}.box.s-partial span{background:#d68910}
.box.s-no_text{border-color:#8e44ad;border-style:dotted}.box.s-no_text span{background:#8e44ad}
.box.s-excluded{border-color:#7f8c8d;border-style:dashed}.box.s-excluded span{background:#7f8c8d}
.blk{border:1px solid var(--line);border-radius:5px;padding:8px 10px;margin:0 0 10px}
.blk:target{outline:3px solid #2a62d9} .bh{margin-bottom:6px}
.bgrid{display:grid;grid-template-columns:minmax(120px,45%) 1fr;gap:10px}
.crop{max-width:100%;border:1px solid var(--line);background:repeating-conic-gradient(#eee 0 25%,#fff 0 50%) 0 0/12px 12px}
.rcrop{max-width:180px;border:1px solid var(--line)}
.txt{white-space:pre-wrap;margin:0 0 6px;font:13px/1.4 system-ui,"Malgun Gothic",sans-serif}
.kv th{text-align:left;color:var(--mut);font-weight:normal;padding-right:10px;white-space:nowrap} .kv td{padding:1px 0}
.sw{display:inline-block;width:22px;height:16px;border:1px solid #333;vertical-align:middle;margin-right:6px}
.null{color:#8a5a00;background:#fff6e0;border:1px dashed #d9a441;padding:0 5px;border-radius:3px}
.chip{display:inline-block;border-radius:10px;padding:0 8px;font-size:12px;margin-left:6px;border:1px solid #bbb;background:#f4f4f4}
.chip.st.s-ok{border-color:#2a62d9;color:#1b3f8f} .chip.st.s-partial{border-color:#d68910;color:#8a5a00}
.chip.st.s-no_text{border-color:#8e44ad;color:#5b2c6f} .chip.st.s-excluded,.chip.ex{border-color:#7f8c8d;color:#444}
.basis{display:inline-block;border-radius:3px;padding:0 6px;font-size:12px;margin-left:6px}
.basis.est{background:#1b3f8f;color:#fff} .basis.dflt{background:#fff;color:#8a5a00;border:2px dashed #d68910}
.mut{color:var(--mut);font-size:12px} details{margin-top:6px} summary{cursor:pointer;color:#1b3f8f}
.scroll{overflow-x:auto} .rt{border-collapse:collapse;font-size:12px;margin-top:4px;width:100%;min-width:900px} .rt td.txt{min-width:140px} .rt th,.rt td{border:1px solid #e3e3e3;padding:3px 5px;vertical-align:top}
.rt tr.r-blank_text{background:#fafafa} .rt tr.r-color_failed{background:#fff8ec}
code{font-size:12px} .legend span{margin-right:12px}
"""

PAGE_JS = """
const sel=id=>document.getElementById(id);
function apply(){const im=sel('fi').value,sc=sel('fs').value,st=sel('fst').value,ba=sel('fb').value;
for(const c of document.querySelectorAll('.card')){const okC=(im==='all'||c.dataset.image===im)&&(sc==='all'||c.dataset.section===sc);
let any=false;for(const b of c.querySelectorAll('.blk')){const ok=(st==='all'||b.dataset.status===st)&&(ba==='all'||b.dataset.basis===ba);
b.style.display=ok?'':'none';any=any||ok}
c.style.display=okC&&(any||!c.querySelector('.blk')&&st==='all'&&ba==='all')?'':'none'}}
for(const id of ['fi','fs','fst','fb'])sel(id).onchange=apply;
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="style_run_driver.py 출력 폴더(run.json)")
    ap.add_argument("--input", required=True, help="고정 입력본(downstream-input-v1)")
    ap.add_argument("--out", required=True, help="새 폴더(이미 있거나 입력본 안이면 거부)")
    a = ap.parse_args(argv)
    _safe_console()
    run, inp, out = Path(a.run), Path(a.input), Path(a.out)
    reason = out_guard(out, inp)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    rec = json.loads((run / "run.json").read_text(encoding="utf-8"))
    if rec.get("stage") != "style":
        print(f"⑦ 실행 기록이 아니다(stage={rec.get('stage')!r}): {run / 'run.json'}", file=sys.stderr)
        return 2
    (out / "img").mkdir(parents=True)
    cards, totals = [], {"fail": 0, "run_fail": 0, "blocks": 0, "crops": 0}
    images, sections = [], []
    for entry in rec.get("sections") or []:
        try:
            card, st = section_card(run, inp, out, entry)
        except Exception as e:  # noqa: BLE001 — 결과 · 입력을 읽지 못한 섹션은 오류 카드로 드러낸다(빈 카드 · 성공으로 바꾸지 않음)
            card = (f'<section class="card fail" data-image="{esc(entry.get("image_id"))}" '
                    f'data-section="{esc(entry.get("image_id"))}/{esc(entry.get("section_key"))}">'
                    f'<h2>{esc(entry.get("image_id"))} / {esc(entry.get("section_key"))}</h2>'
                    f'<p class="bad">검수 페이지 생성 실패 — {esc(e.__class__.__name__)}: {esc(e)}</p></section>')
            st = {"fail": 1}
        cards.append(card)
        for k, v in st.items():
            totals[k] = totals.get(k, 0) + v
        images.append(entry.get("image_id"))
        sections.append(f'{entry.get("image_id")}/{entry.get("section_key")}')
    opt = lambda vals: "".join(f'<option value="{esc(v)}">{esc(v)}</option>' for v in dict.fromkeys(vals))  # noqa: E731
    cfg = (rec.get("config") or {})
    style_cfg = {k: v for k, v in cfg.items() if k.startswith("style.")} or cfg.get("style")
    run_bad = rec.get("status") != "ok"
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>⑦ 스타일 추출 검수</title><style>{PAGE_CSS}</style></head><body>
<header><h1>⑦ 스타일 추출 검수</h1>
<label>원본 <select id="fi"><option value="all">전체</option>{opt(images)}</select></label>
<label>섹션 <select id="fs"><option value="all">전체</option>{opt(sections)}</select></label>
<label>상태 <select id="fst"><option value="all">전체</option>{opt(["ok", "partial", "no_text", "excluded"])}</select></label>
<label>정렬 근거 <select id="fb"><option value="all">전체</option><option value="estimated">estimated(추정)</option>
<option value="default_single_line">default_single_line(기본값)</option><option value="default_tie">default_tie(기본값)</option>
<option value="null">null(정렬 없음)</option></select></label></header>
<main><div class="intro">
<p><b>이 페이지는 사람이 원본과 추출값을 비교하기 위한 개발용 화면입니다.</b> 품질 점수 · 자동 합격 판정은 없습니다.
블록 <b>상태</b>는 영역 측정 상태입니다 — <code>ok</code>는 측정값을 모두 얻었다는 뜻이지 품질 합격이 아니며, <code>excluded</code>(라벨 · 로고 제외)와
<code>no_text</code>(공백 영역만)는 오류가 아닙니다. 정렬은 <span class="basis est">추정(estimated)</span>과
<span class="basis dflt">기본값(default_*)</span>을 구분해 보세요 — 기본 좌정렬은 추정 성공이 아닙니다. 값이 없으면 <span class="null">없음(null) · 사유</span>로 표시하며
검정 · 0px · 좌정렬로 바꾸지 않습니다. 크기 · 좌표는 모두 <b>원본 이미지 px</b>이고, 화면의 확대 · 축소와 무관합니다.
색은 견본과 #RRGGBB만 보이며 원문을 다른 폰트로 다시 그리지 않습니다(재조판 · 렌더 아님).</p>
<p class="legend mut"><span>상자 테두리: 파랑 ok · 주황 partial · 보라 점선 no_text · 회색 파선 excluded</span></p>
<p class="mut">실행 {esc(rec.get("started_at"))} · 상태 <b class="{"bad" if run_bad else ""}">{esc(rec.get("status"))}</b>{" — " + esc(rec.get("error")) if rec.get("error") else ""} ·
커밋 {esc((rec.get("git_commit") or "")[:7])}(dirty={esc(rec.get("git_dirty"))}) · 설정 {esc(json.dumps(style_cfg, ensure_ascii=False))} ·
선택 {esc(json.dumps(rec.get("selection"), ensure_ascii=False))}</p></div>
{"".join(cards)}</main><script>{PAGE_JS}</script></body></html>"""
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"검수 페이지: {out / 'index.html'} · 섹션 {len(cards)}(실행 실패 {totals['run_fail']} · 페이지 표시 불가 {totals['fail']}) · "
          f"블록 {totals['blocks']} · crop {totals['crops']}")
    return 0 if not totals["fail"] else 4  # 실행 실패 섹션은 실패 카드로 보이며 페이지 오류가 아니다


if __name__ == "__main__":
    sys.exit(main())
