"""⑦ 섹션 기준 정렬 사람 검수 — 원본 기준선 라벨 페이지 생성 · 채점(2026-11-05). 개발용, 운영 경로 아님.

    # 1) 검수 페이지(정적 HTML + 이미지 폴더, 로컬에서 연다)
    python docs/ai-experiments/tools/style_section_align_review_page.py page \
        --rows docs/ai-experiments/runs/2026-11-05_07-style_section-align/rows_0.005.json \
        --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 \
        --out docs/ai-experiments/runs/2026-11-05_07-style_section-align/review_v2
    # 2) 내보낸 TSV로 이전 · 수정 후 정렬 채점
    python docs/ai-experiments/tools/style_section_align_review_page.py score \
        --rows <위 rows> --tsv <내보낸 TSV> --out <summary json>

입력: `style_section_align_eval.py`의 `rows_<허용 오차>.json`(블록별 이전 · 새 적용 정렬)과 베이스라인 회차(`evidence/` · `objects/` ·
`object-index.json`). 모두 읽기만 한다. page 출력: `<out>/index.html` · `<out>/img/`(PNG). --out이 이미 있으면 거부한다(덮어쓰지 않는다).

검수 질문은 하나 — 원본에서 빨간 상자 글자의 정렬 기준선(왼쪽 끝 · 가운데 · 오른쪽 끝 · 판단 불가). 2026-11-03 라벨 검수와 같은 질문이다.
사람은 원본만 보고 고르고, 이전 · 수정 후가 맞았는지는 score가 계산한다(정렬이 바뀌지 않은 블록에도 같은 질문이 통한다).

렌더(참고): 섹션마다 인페인트 배경 위에 운영 조판 `app.typeset.render`로 두 번 그린다 — 이전(v1.0.1 적용 정렬)과 수정 후(새 적용 정렬).
두 렌더는 정렬만 다르고 글자색 · 크기 · 배치 영역 · 번역문은 같다(DB 보존본 값). 저장된 v1.0.1 렌더 이미지는 쓰지 않는다.

검수 대상: 정렬이 바뀐 블록 전부 + 바뀌지 않았지만 에이전트 라벨과 다른 블록 전부. 순서는 (GS, id)의 SHA-256으로 섞는다.
표시 원칙: 렌더 · 적용 정렬 · 에이전트 라벨 · 판정 근거 · 구분(바뀜 / 그대로)은 "참고 보기"를 켤 때만 보인다(기본 꺼짐). 미리 선택된 값은 없다.
입력은 브라우저에만 저장(localStorage)되고 "TSV 내보내기"로 받는다. 입력 문자열은 모두 HTML 이스케이프한다.
"""
from __future__ import annotations

import argparse
import collections
import copy
import csv
import hashlib
import io
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from typeset_align_review_page import crop, render_page  # noqa: E402
from typeset_overflow_review_page import esc  # noqa: E402

VERDICTS = (("left", "왼쪽 끝"), ("center", "가운데"), ("right", "오른쪽 끝"), ("unsure", "판단 불가"))
ALIGNS = ("left", "center", "right")


def _h(r: dict) -> str:
    return hashlib.sha256(f"{r['gs']}:{r['id']}".encode()).hexdigest()


def pick(rows: list[dict]) -> list[dict]:
    return sorted([r for r in rows if r["new_applied"] != r["old_applied"] or (r["evaluable"] and r["new_error"])], key=_h)


def page(a: argparse.Namespace) -> int:
    from app import typeset as t

    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    raw = a.rows.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    rows = json.loads(raw.decode("utf-8"))
    by_id = {(r["gs"], r["id"]): r for r in rows}
    cases = pick(rows)
    idx = json.loads((a.run / "object-index.json").read_text(encoding="utf-8"))
    ev = {g: json.loads((a.run / "evidence" / f"{g}.json").read_text(encoding="utf-8")) for g in sorted({r["gs"] for r in rows})}
    defaults = t.load_role_defaults()

    def obj(key: str) -> bytes:
        return (a.run / "objects" / idx[key]["sha256"]).read_bytes()

    def section_inputs(g: str, sid: int) -> tuple[dict, list[dict], str]:
        d = ev[g]
        sec = next(s for s in d["section"] if s["id"] == sid)
        stage = next((x["status"] for x in d["job_async_task"] if x["stage"] == "style" and x["is_current"] and x["unit_id"] == sid), None)
        blocks = [b for b in d["text_block"] if b["section_id"] == sid and b["is_excluded"] is False
                  and (b["source_ko"] or "").strip() and (b["trans_1"] or "").strip()]
        return sec, blocks, stage

    def render(g: str, sid: int, which: str) -> tuple[Image.Image, dict[int, dict]]:
        sec, blocks, stage = section_inputs(g, sid)
        rbs, boxes = [], {}
        for b in blocks:
            box = (b["auto_adjust"] or {}).get("layout_bbox") or b["bbox"]
            box = {k: int(box[k]) for k in ("x", "y", "w", "h")}
            boxes[b["id"]] = box
            style = copy.deepcopy(b["style"])
            r = by_id.get((g, b["id"]))
            if style is not None and r is not None:
                style["align"] = r[f"{which}_applied"]  # 정렬만 바꾼다(측정값 자리 — 기본값 해석을 거치지 않게 적용값을 그대로)
            rbs.append(t.RenderBlock(id=b["id"], role=b["role"], text=b["trans_1"], box=box, style=style, style_stage=stage,
                                     source_ko=b["source_ko"]))
        res = t.render(obj(sec["inpaint_image_url"]), rbs, defaults)
        return Image.open(io.BytesIO(res.png)).convert("RGB"), boxes

    a.out.mkdir(parents=True)
    (a.out / "img").mkdir()
    cache: dict[tuple, tuple[Image.Image, dict]] = {}
    origs: dict[tuple, Image.Image] = {}
    cards, ids = [], []
    for n, r in enumerate(cases, 1):
        g, sid, bid = r["gs"], r["section_id"], r["id"]
        cid = f"{g}|{bid}"
        ids.append(cid)
        if (g, sid) not in origs:
            sec, _, _ = section_inputs(g, sid)
            origs[(g, sid)] = Image.open(io.BytesIO(obj(sec["image_key"]))).convert("RGB")
            for which in ("old", "new"):
                cache[(g, sid, which)] = render(g, sid, which)
        tb = next(b for b in ev[g]["text_block"] if b["id"] == bid)
        names = {}
        for kind, im, box in (("orig", origs[(g, sid)], tb["bbox"]), ("old", *cache[(g, sid, "old")]), ("new", *cache[(g, sid, "new")])):
            name = f"img/{g}_{bid}_{kind}.png"
            crop(im, box if kind == "orig" else box[bid]).save(a.out / name)
            names[kind] = name
        radios = "".join(f'<label><input type="radio" name="v:{esc(cid)}" value="{v}"> {esc(x)}</label>' for v, x in VERDICTS)
        group = "정렬 바뀜" if r["new_applied"] != r["old_applied"] else "그대로 · 에이전트 라벨과 다름"
        ref = (f'<div class="ref"><b>참고 · 구분</b> {esc(group)} · <b>에이전트 라벨</b> {esc(r["label"])} — {esc(r["basis_note"])}<br>'
               f'<b>이전</b> {esc(r["old_applied"])}(⑦ 근거 {esc(r["old_basis"])}) → <b>수정 후</b> {esc(r["new_applied"])}'
               f'(근거 {esc(r["new_basis"])} · 지지 수 {esc(r["scores"])} · 섹션 중앙 {esc(r["section_center"])})'
               f'{" · <b>11/03 사람 판정</b> " + esc(r["human"]) if r.get("human") else ""}'
               f'<figure><figcaption>수정 후 렌더 — 정렬 {esc(r["new_applied"])}</figcaption><img src="{names["new"]}" alt="수정 후" loading="lazy"></figure>'
               f'<figure><figcaption>이전 렌더 — 정렬 {esc(r["old_applied"])}</figcaption><img src="{names["old"]}" alt="이전" loading="lazy"></figure></div>')
        cards.append(
            f'<section class="card" id="{esc(cid)}" data-id="{esc(cid)}">'
            f'<h3>사례 {n} <span class="sub">{esc(g)} · 블록 {esc(bid)} · 섹션 {esc(sid)} · {esc(r["role"])}</span></h3>'
            f'<p class="meta">원문 {r["src_lines"]}줄 · 영역 {tb["bbox"]["w"]}×{tb["bbox"]["h"]}px</p>'
            f'<div class="txt"><div><b>원문</b> {esc(tb["source_ko"])}</div></div>'
            f'<div class="imgs"><figure><figcaption>원본</figcaption><img src="{names["orig"]}" alt="원본" loading="lazy"></figure></div>{ref}'
            f'<div class="form"><div class="row"><b>판단</b>{radios}<button type="button" class="clear">지우기</button></div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="예: 원형 배지 중앙 · 위 제목과 같은 왼쪽 축"></div></div></section>')
    changed = sum(r["new_applied"] != r["old_applied"] for r in cases)
    key = f"style-section-align-review-v2:{fp}"
    intro = (f'<b>검수 질문</b> — <b>원본</b>에서 빨간 상자 안 글자는 <b>어느 기준선에 맞춰 놓였나요?</b> '
             f'번역문이 같은 상자에 들어간다면 왼쪽 끝 · 가운데 · 오른쪽 끝 중 어디에 맞춰야 원본 배치와 같아 보이는지로 판단해 주세요.<br>'
             f'한 줄 글자는 상자 자체보다 주변(배지 · 칸 · 버튼 · 위아래 문구 · 반복 항목)을 보고 판단합니다. 근거가 없거나 둘 이상이 똑같이 그럴듯하면 '
             f'"판단 불가"로 두고 메모에 이유를 적어 주세요. 이미지를 누르면 원본 픽셀 크기로 봅니다.<br>'
             f'사례 {len(cases)}개 — 정렬이 바뀐 블록 {changed} · 바뀌지 않았지만 에이전트 라벨과 다른 블록 {len(cases) - changed}(순서는 섞여 있음). '
             f'이전 · 수정 후 정렬이 맞았는지는 내보낸 TSV로 도구가 계산합니다. "참고 보기"는 두 렌더 · 적용 정렬 · 에이전트 판단이며 사람 판단이 아닙니다 — '
             f'먼저 끈 채로 판단하기를 권합니다. 입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>'
             f'<span class="sub">회차 baseline-v1-0-1-gs-v1-20261030-01 · 판정 코드 feature/style-section-align · 입력 지문 {fp} · 저장 키 {esc(key)}</span>')
    html = render_page("⑦ 섹션 기준 정렬 — 원본 기준선 검수", intro, cards, ids, key, fp, VERDICTS,
                       "case_no\\tgs\\tblock_id\\tverdict\\tnote\\treview_fingerprint", f"style_section_align_review_{fp}.tsv")
    (a.out / "index.html").write_text(html, encoding="utf-8")
    print(f"검수 페이지: {a.out / 'index.html'} · 사례 {len(cases)}(바뀜 {changed}) · 저장 키 {key}")
    return 0


def score(a: argparse.Namespace) -> int:
    raw = a.rows.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    rows = {(r["gs"], r["id"]): r for r in json.loads(raw.decode("utf-8"))}
    with a.tsv.open(encoding="utf-8", newline="") as f:
        marks = [m for m in csv.DictReader(f, delimiter="\t") if m["verdict"]]
    bad = sorted({m["review_fingerprint"] for m in marks} - {fp})
    if bad:
        print(f"TSV 입력 지문 {bad} ≠ rows 지문 {fp} — 다른 입력으로 만든 검수다", file=sys.stderr)
        return 1
    judged = [(rows[(m["gs"], int(m["block_id"]))], m["verdict"], m["note"]) for m in marks]
    ev = [(r, v) for r, v, _ in judged if v in ALIGNS]

    def tally(sub: list[tuple[dict, str]]) -> dict:
        return {"judged": len(sub), "old_errors": sum(r["old_applied"] != v for r, v in sub),
                "new_errors": sum(r["new_applied"] != v for r, v in sub),
                "fixed": sum(r["old_applied"] != v and r["new_applied"] == v for r, v in sub),
                "broken": sum(r["old_applied"] == v and r["new_applied"] != v for r, v in sub)}

    out = {"rows_fingerprint": fp, "marked": len(judged), "unsure": sum(v == "unsure" for _, v, _ in judged), "all": tally(ev),
           "changed": tally([(r, v) for r, v in ev if r["new_applied"] != r["old_applied"]]),
           "unchanged": tally([(r, v) for r, v in ev if r["new_applied"] == r["old_applied"]]),
           "single_line": tally([(r, v) for r, v in ev if r["src_lines"] == 1]),
           "multi_line": tally([(r, v) for r, v in ev if r["src_lines"] > 1]),
           "by_new_basis": {b: tally([(r, v) for r, v in ev if r["new_basis"] == b]) for b in sorted({r["new_basis"] for r, _ in ev})},
           "agent_agrees": sum(r["label"] == v for r, v in ev),
           "new_confusion": dict(sorted(collections.Counter(f"{v}→{r['new_applied']}" for r, v in ev if r["new_applied"] != v).items()))}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("page")
    p.add_argument("--rows", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("score")
    s.add_argument("--rows", type=Path, required=True)
    s.add_argument("--tsv", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    return page(a) if a.cmd == "page" else score(a)


if __name__ == "__main__":
    sys.exit(main())
