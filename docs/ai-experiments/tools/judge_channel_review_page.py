"""③-1 채널 정책 맥락 판정 실측의 사람 확인용 페이지 — 섹션 이미지 · 블록 원문 · 채널 항목별 판정을 한 파일에 담는다(2026-11-27).

    python docs/ai-experiments/tools/judge_channel_review_page.py --run pipeline/out/judge-channel-run-v1_<date> \
        --input pipeline/samples/local/blocks-input-v1 [--out <파일>] [--image-width 460]

읽기 전용 화면이다 — 라벨을 입력하지 않는다. 모델이 낸 판정 · 사유 · 근거 블록 · 패턴 신호를 그대로 보여 준다.
이미지는 줄여서 파일 안에 넣는다(이 파일 하나로 열린다). 기본 출력은 `<run>/review.html`(git 제외 폴더).
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import jsonio  # noqa: E402

_ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
_ap.add_argument("--run", required=True)
_ap.add_argument("--input", required=True)
_ap.add_argument("--out", default=None)
_ap.add_argument("--image-width", type=int, default=460)
_args = _ap.parse_args()
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

RUN, INPUT = Path(_args.run), Path(_args.input)
OUT = Path(_args.out) if _args.out else RUN / "review.html"
STATUS_KO = {"present": "해당", "absent": "해당 아님", "uncertain": "불확실"}
SOURCE_KO = {"text": "글", "image": "이미지", "image_and_text": "이미지+글"}
IMG_W = _args.image_width


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def image_data(path: str) -> tuple[str, int, int]:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    if w > IMG_W:
        img = img.resize((IMG_W, max(1, round(h * IMG_W / w))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=68, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), w, h


manifest = json.loads((RUN / "manifest.json").read_text(encoding="utf-8"))
CODES = {v: k for k, v in manifest["experimental_codes"].items()}  # 유형 코드 → 사전 ID(실험용 이름)
items_meta = {i[0]: i[1] for i in manifest["channel_items"]}
cards, counts, n_img_bytes = [], {"all": 0, "present": 0, "failed": 0, "signal": 0}, 0
per_item = {c: {"present": 0, "absent": 0, "uncertain": 0} for c in CODES.values()}
sources = sorted(p for p in INPUT.iterdir() if p.is_dir() and (p / "merge").is_dir())
for src in sources:
    split = jsonio.load_split(src / "split.json")
    order = {s.section_key: s.section_order for s in split.sections}
    for mp in sorted((src / "merge").glob("*.json"), key=lambda p: order.get(p.stem, 0)):
        rf = RUN / src.name / f"{mp.stem}.json"
        if not rf.exists():
            continue
        d = json.loads(rf.read_text(encoding="utf-8"))
        r, call = d["result"], d["call"]
        merged = jsonio.load_merge(mp)
        sec = next(s for s in split.sections if s.section_key == merged.section_key)
        key2tmp = {b.block_key: f"b{i + 1}" for i, b in enumerate(sorted(merged.blocks, key=lambda b: b.block_order))}
        b64, w, h = image_data(sec.image_path)
        n_img_bytes += len(b64)
        name = f"{src.name}/{merged.section_key}"
        failed = r["channel_status"] != "completed"
        finds = {CODES[x["content_type"]]: x for x in r["content_findings"]["findings"] if x["content_type"] in CODES}
        payload_items = {it["id"]: it for it in call["payload"]["items"]}
        has_present = any(x["status"] in ("present", "uncertain") for x in finds.values())
        has_signal = any(it["matched_patterns"] for it in payload_items.values())
        ev_blocks = {key2tmp[k] for x in finds.values() if x["status"] in ("present", "uncertain") for k in x["evidence_block_ids"]}
        sig_blocks = {b for it in payload_items.values() for b in it["candidate_blocks"]}
        counts["all"] += 1
        counts["present"] += has_present
        counts["failed"] += failed
        counts["signal"] += has_signal

        rows = []
        for cid in CODES.values():
            it = payload_items[cid]
            sig = ("<span class='sig'>패턴 신호: " + esc(", ".join(it["matched_patterns"])) + " (" + esc(", ".join(it["candidate_blocks"])) + ")</span>"
                   if it["matched_patterns"] else "<span class='nosig'>패턴 신호 없음</span>")
            if failed:
                rows.append(f"<tr class='it'><td class='iid'>{cid}<div class='iname'>{esc(items_meta[cid])}</div></td>"
                            f"<td><span class='chip fail'>판정 없음</span></td><td>{sig}</td></tr>")
                continue
            x = finds[cid]
            per_item[cid][x["status"]] += 1
            ev = ", ".join(key2tmp[k] for k in x["evidence_block_ids"]) or "없음"
            rows.append(
                f"<tr class='it st-{x['status']}'><td class='iid'>{cid}<div class='iname'>{esc(items_meta[cid])}</div></td>"
                f"<td><span class='chip {x['status']}'>{STATUS_KO[x['status']]}</span></td>"
                f"<td><div class='reason'>{esc(x['reason'])}</div>"
                f"<div class='meta'>근거 출처 {SOURCE_KO[x['evidence_source']]} · 근거 블록 {esc(ev)} · {sig}</div></td></tr>")
        blocks = []
        for b in call["payload"]["blocks"]:
            cls = "blk" + (" ev" if b["id"] in ev_blocks else "") + (" sg" if b["id"] in sig_blocks else "")
            blocks.append(f"<li class='{cls}'><span class='bid'>{b['id']}</span><span class='role'>{esc(b['role'])}</span>"
                          f"<span class='btxt'>{esc(b['text'])}</span></li>")
        fail_box = ""
        if failed:
            f = r["channel_failure"] or {}
            fail_box = f"<div class='failbox'>채널 판정 실패 — {esc(f.get('kind'))}: {esc(str(f.get('message'))[:300])}</div>"
        usage = call.get("usage") or {}
        tags = " ".join(t for t, on in (("present", has_present), ("failed", failed), ("signal", has_signal)) if on)
        badge = ("<span class='chip fail'>판정 실패 · 제외 권고</span>" if failed else
                 "<span class='chip present'>제외 권고</span>" if r["bucket_recommendation"] == "exclude" else "<span class='chip absent'>포함</span>")
        cards.append(
            f"<section class='card' data-tags='{tags}' id='{esc(name.replace('/', '_'))}'>"
            f"<header><h2>{esc(name)}</h2>{badge}"
            f"<span class='small'>블록 {len(call['payload']['blocks'])}개 · 원본 {w}×{h}px · {call.get('duration_s', '-')}초 · "
            f"{usage.get('total_token_count', '-')} 토큰</span></header>"
            f"<div class='body'><div class='imgwrap'><img loading='lazy' alt='{esc(name)} 섹션 이미지' src='data:image/jpeg;base64,{b64}'></div>"
            f"<div class='right'>{fail_box}<table class='items'><tbody>{''.join(rows)}</tbody></table>"
            f"<details open><summary>블록 원문 {len(blocks)}개</summary><ul class='blocks'>{''.join(blocks)}</ul></details></div></div></section>")

item_rows = "".join(
    f"<tr><td>{c}</td><td>{esc(items_meta[c])}</td><td class='num'>{v['present']}</td><td class='num'>{v['uncertain']}</td><td class='num'>{v['absent']}</td></tr>"
    for c, v in per_item.items())
cfg = manifest["judge_config"]
page = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>채널 판정 실측 확인</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#1c2026; --sub:#5b6472; --line:#dfe3ea; --hit:#b42318; --hitbg:#fdeceb; --ok:#3d4a5c; --okbg:#eef1f5;
  --unc:#8a5a00; --uncbg:#fff3d6; --sig:#1d5fbf; --sigbg:#e8f0fd; --evbg:#fff1ef; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171c; --card:#1c2027; --ink:#e8ebf0; --sub:#9aa4b3; --line:#2e3440; --hit:#ff8a80; --hitbg:#3a1d1b;
  --ok:#b8c2d0; --okbg:#262c36; --unc:#ffcf70; --uncbg:#3a2e12; --sig:#8ab4ff; --sigbg:#1b2a44; --evbg:#33201e; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.55 "Pretendard","Malgun Gothic",system-ui,sans-serif; }}
.top {{ position:sticky; top:0; z-index:5; background:var(--card); border-bottom:1px solid var(--line); padding:10px 16px; }}
.top h1 {{ font-size:16px; margin:0 0 6px; }}
.filters {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center; }}
.filters button {{ font:inherit; padding:5px 11px; border:1px solid var(--line); border-radius:999px; background:var(--bg); color:var(--ink); cursor:pointer; }}
.filters button[aria-pressed="true"] {{ background:var(--ink); color:var(--card); border-color:var(--ink); }}
.filters .shown {{ color:var(--sub); margin-left:6px; }}
main {{ max-width:1180px; margin:0 auto; padding:14px 16px 60px; }}
.intro {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:14px; }}
.intro p {{ margin:4px 0; color:var(--sub); }}
.intro table {{ border-collapse:collapse; margin-top:8px; }}
.intro th,.intro td {{ border-bottom:1px solid var(--line); padding:4px 12px 4px 0; text-align:left; }}
.num {{ text-align:right !important; font-variant-numeric:tabular-nums; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; margin-bottom:14px; overflow:hidden; }}
.card header {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; padding:9px 14px; border-bottom:1px solid var(--line); }}
.card h2 {{ font-size:14px; margin:0; font-variant-numeric:tabular-nums; }}
.small {{ color:var(--sub); font-size:12px; margin-left:auto; }}
.body {{ display:grid; grid-template-columns:minmax(220px,380px) 1fr; gap:14px; padding:12px 14px; }}
@media (max-width:760px) {{ .body {{ grid-template-columns:1fr; }} }}
.imgwrap {{ max-height:640px; overflow:auto; border:1px solid var(--line); border-radius:6px; background:#fff; }}
.imgwrap img {{ display:block; width:100%; height:auto; }}
.items {{ width:100%; border-collapse:collapse; }}
.items td {{ vertical-align:top; padding:7px 8px 7px 0; border-bottom:1px solid var(--line); }}
.iid {{ white-space:nowrap; font-weight:600; width:150px; }}
.iname {{ font-weight:400; color:var(--sub); font-size:12px; white-space:normal; }}
.chip {{ display:inline-block; padding:1px 9px; border-radius:999px; font-size:12px; font-weight:600; white-space:nowrap; }}
.chip.present,.chip.fail {{ color:var(--hit); background:var(--hitbg); }}
.chip.absent {{ color:var(--ok); background:var(--okbg); }}
.chip.uncertain {{ color:var(--unc); background:var(--uncbg); }}
.it.st-absent .reason {{ color:var(--sub); }}
.meta {{ color:var(--sub); font-size:12px; margin-top:2px; }}
.sig {{ color:var(--sig); }}
.failbox {{ color:var(--hit); background:var(--hitbg); border-radius:6px; padding:8px 10px; margin-bottom:8px; overflow-wrap:anywhere; }}
details {{ margin-top:10px; }}
summary {{ cursor:pointer; color:var(--sub); }}
.blocks {{ list-style:none; margin:6px 0 0; padding:0; }}
.blk {{ display:grid; grid-template-columns:34px 58px 1fr; gap:6px; padding:3px 6px; border-radius:4px; }}
.blk.sg {{ background:var(--sigbg); }}
.blk.ev {{ background:var(--evbg); outline:1px solid var(--hit); }}
.bid {{ color:var(--sub); font-variant-numeric:tabular-nums; }}
.role {{ color:var(--sub); font-size:12px; }}
.btxt {{ white-space:pre-wrap; overflow-wrap:anywhere; }}
.legend span {{ display:inline-block; padding:0 6px; border-radius:4px; margin-right:6px; }}
</style></head><body>
<div class="top"><h1>채널 정책 판정 실측 확인 — {counts['all']}섹션</h1>
<div class="filters" role="group" aria-label="보기 선택">
<button data-f="all" aria-pressed="true">전체 {counts['all']}</button>
<button data-f="present" aria-pressed="false">해당 · 불확실 {counts['present']}</button>
<button data-f="failed" aria-pressed="false">판정 실패 {counts['failed']}</button>
<button data-f="signal" aria-pressed="false">패턴 신호 있음 {counts['signal']}</button>
<span class="shown" id="shown"></span></div></div>
<main>
<div class="intro">
<p><b>읽기 전용 확인 화면입니다.</b> 모델이 낸 판정을 그대로 보여 주며, 정답 라벨이 없으므로 숫자는 정확도가 아닙니다. 사람 검수 전입니다.</p>
<p>입력 blocks-input-v1 · 채널 사전 {esc(manifest.get('v9_file', '현지부적합 v9'))}의 적재 대상 {len(CODES)}행 · 모델 {esc(cfg['llm_model'])} · 섹션마다 1회 호출(재시도 없음) · 실행 {esc(manifest['started_at'][:10])} · 코드 {esc(str(manifest['git'].get('commit', manifest['git']))[:12])}</p>
<p>유형 코드는 실험용 제안 이름입니다. 가격 접미 예외: {esc(', '.join(manifest.get('price_suffix_exception') or []) or '끔')}. 현지(국가) 판정과 규제 검출은 이 실행에 포함하지 않았습니다.</p>
<table><thead><tr><th>ID</th><th>항목</th><th class="num">해당</th><th class="num">불확실</th><th class="num">해당 아님</th></tr></thead><tbody>{item_rows}</tbody></table>
<p class="legend" style="margin-top:8px">블록 표시: <span style="background:var(--evbg);outline:1px solid var(--hit)">해당 · 불확실 판정의 근거 블록</span><span style="background:var(--sigbg)">패턴 신호가 걸린 블록</span></p>
</div>
{''.join(cards)}
</main>
<script>
const btns=[...document.querySelectorAll('.filters button')],cards=[...document.querySelectorAll('.card')],shown=document.getElementById('shown');
function apply(f){{let n=0;cards.forEach(c=>{{const on=f==='all'||c.dataset.tags.split(' ').includes(f);c.style.display=on?'':'none';n+=on;}});
btns.forEach(b=>b.setAttribute('aria-pressed',b.dataset.f===f));shown.textContent=n+'개 표시';}}
btns.forEach(b=>b.addEventListener('click',()=>{{apply(b.dataset.f);window.scrollTo(0,0);}}));apply('all');
</script></body></html>"""
OUT.write_text(page, encoding="utf-8", newline="\n")
print("저장", OUT, f"{OUT.stat().st_size / 1e6:.1f}MB", "섹션", counts, "항목", per_item)
