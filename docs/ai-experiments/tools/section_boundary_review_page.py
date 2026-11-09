"""① 섹션 경계 재검사 사람 검수 페이지 생성 — 정적 HTML, 로컬에서 연다(2026-11-03). 개발용, 운영 경로 아님.

    python docs/ai-experiments/tools/section_boundary_review_page.py \
        --boundaries docs/ai-experiments/runs/2026-11-03_01-split_boundary-recheck/boundaries.json \
        --review docs/ai-experiments/2026-11-03_01-split_boundary-recheck_review.tsv \
        --images pipeline/samples/evaluation/gs-v1 \
        --out docs/ai-experiments/runs/2026-11-03_01-split_boundary-recheck/review

입력: `section_boundary_probe.py`의 `boundaries.json`(경계별 신호) · 에이전트 판정표 TSV · 원본 상세페이지. 모두 읽기만 한다.
출력은 이미지를 data URI로 넣은 HTML 한 파일(`<out>/index.html`). --out이 이미 있으면 거부한다(덮어쓰지 않는다).

검수 대상: 고유 경계(원본, y) 전부. 순서는 (원본, y)의 SHA-256으로 섞는다.

표시 원칙:
- 검수 질문은 둘 — (1) 경계가 글자 획을 자르는가 (2) 경계가 한 문단 · 여러 줄 제목을 가르는가. 각각 예 · 아니오 · 판단 불가.
- 원본을 전체 폭 · 경계 ±CROP_PAD로 자른 원본 픽셀 그대로의 PNG다(축소 · 보정 없음). 경계 위치는 이미지 양옆의 빨간 표시와,
  이미지 위에 겹치는 점선(머리줄 "경계선 표시"로 끄고 켬)으로 보인다 — 점선을 끄면 경계 행의 픽셀을 가리지 않는다.
- 에이전트 판정 · 신호(행 표준편차 · OCR 접촉 · 가까운 줄)는 "참고 보기"를 켤 때만 보인다(기본 꺼짐). 미리 선택된 값은 없다.
- 입력은 브라우저에만 저장(localStorage)되고 "TSV 내보내기"로 받는다. 입력 문자열은 모두 HTML 이스케이프한다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from typeset_align_review_page import render_page  # noqa: E402
from typeset_overflow_review_page import data_uri, esc  # noqa: E402

VERDICTS = (("yes", "예"), ("no", "아니오"), ("unsure", "판단 불가"))
CROP_PAD = 120
MARK = 10  # 양옆 경계 표시 띠 폭(px)


def crop(im: Image.Image, y: int) -> tuple[Image.Image, int]:
    """원본 ±CROP_PAD를 자르고 양옆에 표시 띠를 붙인다. 원본 픽셀은 건드리지 않는다. (그림, 그림 안 경계 y)"""
    top, bot = max(0, y - CROP_PAD), min(im.height, y + CROP_PAD)
    c = im.crop((0, top, im.width, bot))
    out = Image.new("RGB", (c.width + 2 * MARK, c.height), (235, 235, 235))
    out.paste(c, (MARK, 0))
    d = ImageDraw.Draw(out)
    by = y - top
    for x0 in (0, c.width + MARK):
        d.polygon([(x0, by - 6), (x0 + MARK - 1, by), (x0, by + 6)] if x0 == 0 else
                  [(x0 + MARK - 1, by - 6), (x0, by), (x0 + MARK - 1, by + 6)], fill=(230, 0, 0))
    return out, by


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--boundaries", type=Path, required=True)
    ap.add_argument("--review", type=Path, required=True)
    ap.add_argument("--images", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    if a.out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {a.out}", file=sys.stderr)
        return 1
    raw = a.boundaries.read_bytes()
    fp = hashlib.sha256(raw).hexdigest()[:16]
    rows = json.loads(raw.decode("utf-8"))
    with a.review.open(encoding="utf-8", newline="") as fh:
        agent = {(r["image"], int(r["y"])): r for r in csv.DictReader(fh, delimiter="\t")}
    uniq = sorted({(r["image"], r["y"]) for r in rows}, key=lambda k: hashlib.sha256(f"{k[0]}:{k[1]}".encode()).hexdigest())
    missing = [k for k in uniq if k not in agent]
    if missing:
        print(f"판정표에 없는 경계 {len(missing)}: {missing[:5]}", file=sys.stderr)
        return 1
    a.out.mkdir(parents=True)
    opened: dict[str, Image.Image] = {}
    cards, ids = [], []
    for n, (img, y) in enumerate(uniq, 1):
        cid = f"{img}|{y}"
        ids.append(cid)
        if img not in opened:
            opened[img] = Image.open(a.images / img[:5] / f"{img}.jpg").convert("RGB")
        pic, by = crop(opened[img], y)
        recs = [r for r in rows if (r["image"], r["y"]) == (img, y)]
        sig = " / ".join(
            f'{esc(r["dataset"])}: 행 표준편차 {r["std_prev"]}·{r["std_at"]} · OCR 접촉 위 {esc(r.get("touch_above", "—"))} 아래 {esc(r.get("touch_below", "—"))}'
            f' · 가까운 줄 {esc(r.get("near_text", "—"))} 간격/줄 높이 {esc(r.get("gap_ratio", "—"))}' for r in recs)
        ag = agent[(img, y)]
        ref = (f'<div class="ref"><b>참고 · 에이전트 판정</b> 글자 획 절단 {esc(ag["line_cut"])} · 문단 분할 {esc(ag["paragraph_split"])} — '
               f'{esc(ag["note"])}<br><b>신호</b> {sig}</div>')
        radios = "".join(
            f'<div class="row"><b>{q}</b>' + "".join(
                f'<label><input type="radio" name="{f}:{esc(cid)}" value="{v}"> {esc(t)}</label>' for v, t in VERDICTS) + "</div>"
            for f, q in (("v", "(1) 글자 획을 자르나요?"), ("p", "(2) 한 문단 · 여러 줄 제목을 가르나요?")))
        cards.append(
            f'<section class="card" id="{esc(cid)}" data-id="{esc(cid)}">'
            f'<h3>사례 {n} <span class="sub">{esc(img)} · 경계 y={y} · 자료 {esc(", ".join(sorted({r["dataset"] for r in recs})))}</span></h3>'
            f'<div class="imgs"><figure><figcaption>원본(경계 위아래 {CROP_PAD}px, 양옆 빨간 표시 = 경계)</figcaption>'
            f'<div class="wrap"><img src="{data_uri(pic)}" alt="원본"><div class="bline" style="top:{by / pic.height * 100:.4f}%"></div></div>'
            f'</figure></div>{ref}'
            f'<div class="form">{radios}<div class="row"><button type="button" class="clear">지우기</button></div>'
            f'<div class="row"><b>메모</b><input type="text" class="note" placeholder="예: 두 줄 제목 사이 · 사진 위 · 하강부만 걸림"></div></div></section>')
    key = f"section-boundary-review:{fp}"
    intro = (f'<b>검수 질문</b> — 섹션 경계(빨간 표시)가 <b>(1) 글자 획을 자르는지</b>, <b>(2) 한 문단이나 여러 줄 제목을 둘로 나누는지</b> 판단해 주세요. '
             f'경계 위는 앞 섹션, 아래는 뒤 섹션입니다. 경계가 글자 바로 위 · 아래 행에 닿기만 하고 획을 지나지 않으면 (1)은 "아니오"입니다.<br>'
             f'점선은 이미지 위에 겹쳐 그린 것이라 경계 행 픽셀을 가립니다 — 머리줄 "경계선 표시"를 끄면 원본 픽셀만 보입니다. 이미지를 누르면 원본 픽셀 크기로 봅니다.<br>'
             f'사례는 고유 경계 {len(uniq)}개 전부입니다(순서는 무작위). "참고 보기"는 에이전트 판정 · 자동 신호이며 사람 판단이 아닙니다 — 먼저 끈 채로 판단하기를 '
             f'권합니다. 두 질문 모두 답해야 검수함으로 셉니다. 입력은 이 브라우저에만 저장됩니다. 끝나면 TSV로 내보내세요.<br>'
             f'<span class="sub">입력 지문 {fp} · 저장 키 {esc(key)}</span>')
    page = render_page("① 섹션 경계 재검사 검수", intro, cards, ids, key, fp, VERDICTS,
                       "case_no\\timage\\ty\\tline_cut\\tparagraph_split\\tnote\\treview_fingerprint",
                       f"section_boundary_review_{fp}.tsv", fields=("v", "p"),
                       controls='<label><input type="checkbox" id="showline" checked> 경계선 표시</label>')
    (a.out / "index.html").write_text(page, encoding="utf-8")
    print(f"검수 페이지: {a.out / 'index.html'} · 사례 {len(uniq)} · 저장 키 {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
