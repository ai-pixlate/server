"""결과 JSON을 섹션 이미지 위에 그려 사람이 확인하는 도구.

- OcrResult  → 영역 poly(초록) + region_key·score
- MergeResult → 블록 bbox(역할별 색) + block_key·role, 줄 bbox(연한 선)
- SplitResult → 원본 위에 섹션 경계선 + section_key
- JudgeResult(+PolicyResult) → 매칭 후보 블록(파랑 얇은 선) · finding 근거 블록(상태별 색: present 빨강 · absent 초록 · uncertain 주황) ·
  실패는 회색 테두리 + FAILED. 이미지 전용 근거는 좌표가 없으므로 박스를 그리지 않고 상단 설명 줄에 적는다. 정책 권고 · verdict · 충돌은 상단 설명 줄
- LabelResult(④) → 블록 bbox: 라벨 true 빨강 굵은 선 · false 초록 얇은 선 · 공백 규칙 false(VLM 미전송) 회색 점선 · 판정 없는 블록(목록 밖) 보라.
  실패는 회색 테두리 + FAILED(판정 박스 없음). 요약은 이미지 위 흰 띠

라벨은 키·숫자만 그린다(한글 폰트가 없는 환경에서도 깨지지 않게). 원문은 JSON에서 본다.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pipeline.types import JudgeResult, LabelResult, MergeResult, OcrResult, PolicyResult, SplitResult

ROLE_COLORS = {
    "title": (220, 20, 60),  # crimson
    "body": (30, 100, 220),  # blue
    "caption": (120, 120, 120),  # gray
    "price": (200, 120, 0),  # orange
    "caution": (150, 0, 180),  # purple
}
REGION_COLOR = (0, 160, 60)
LINE_COLOR = (120, 200, 160)
SECTION_COLOR = (255, 0, 0)
STATUS_COLORS = {  # ③-1 finding 상태 — 서로 구분되는 색(absent · uncertain · 실패를 같은 색으로 두지 않는다)
    "present": (220, 40, 40),  # red
    "absent": (40, 160, 60),  # green
    "uncertain": (240, 150, 20),  # orange
}
FAILED_COLOR = (120, 120, 120)  # gray
CANDIDATE_COLOR = (30, 100, 220)  # blue — 규칙 매칭 후보(판정 아님)


def _font(size: int = 12):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 오래된 Pillow
        return ImageFont.load_default()


def _label(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, color, font) -> None:
    x, y = xy
    tb = draw.textbbox((x, y), text, font=font)
    draw.rectangle(tb, fill=(255, 255, 255))
    draw.text((x, y), text, fill=color, font=font)


def overlay_ocr(image_path: str | Path, res: OcrResult, out: str | Path) -> Path:
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = _font()
    for r in res.regions:
        draw.polygon([tuple(p) for p in r.poly], outline=REGION_COLOR, width=2)
        _label(draw, (r.bbox.x, max(0, r.bbox.y - 15)), f"{r.region_key} {r.score:.2f}", REGION_COLOR, font)
    return _save(im, out)


def overlay_merge(image_path: str | Path, res: MergeResult, out: str | Path) -> Path:
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = _font()
    for b in res.blocks:
        color = ROLE_COLORS[b.role]
        for ln in b.source_lines:
            draw.rectangle((ln.bbox.x, ln.bbox.y, ln.bbox.x2, ln.bbox.y2), outline=LINE_COLOR, width=1)
        draw.rectangle((b.bbox.x, b.bbox.y, b.bbox.x2, b.bbox.y2), outline=color, width=3)
        conf = "" if b.ocr_confidence is None else f" {b.ocr_confidence:.2f}"
        _label(draw, (b.bbox.x, max(0, b.bbox.y - 15)), f"{b.block_key} {b.role}{conf}", color, font)
    return _save(im, out)


def overlay_split(image_path: str | Path, res: SplitResult, out: str | Path) -> Path:
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = _font(14)
    for s in res.sections:
        draw.line((0, s.top_offset, im.width, s.top_offset), fill=SECTION_COLOR, width=3)
        _label(draw, (4, s.top_offset + 4), f"{s.section_key} top={s.top_offset} h={s.height}", SECTION_COLOR, font)
    return _save(im, out)


def overlay_judge(image_path: str | Path, merge: MergeResult, judge: JudgeResult, out: str | Path, policy: PolicyResult | None = None) -> Path:
    """③-1(+③-1') 결과 오버레이. 블록 좌표는 MergeResult에서 가져온다(JudgeResult에는 좌표가 없다).
    설명 줄(판정 요약 · 이미지 전용 근거 · 정책)은 콘텐츠를 덮지 않도록 이미지 **위에 흰 띠를 덧붙여** 그린다."""
    src = Image.open(image_path).convert("RGB")
    font = _font()
    boxes = {b.block_key: b.bbox for b in merge.blocks}
    header: list[tuple[str, tuple[int, int, int]]] = []
    code = {"present": "P", "absent": "A", "uncertain": "U"}
    failed = judge.status == "failed" or judge.content_findings is None
    if failed:
        header.append((f"FAILED {judge.status}: {(judge.error or '')[:70]}", FAILED_COLOR))
    else:
        header.append((f"judge {judge.status}: findings {len(judge.content_findings.findings)} matches {len(judge.matches)}", (0, 0, 0)))
        for f in judge.content_findings.findings:
            if not f.evidence_block_ids:
                header.append((f"{f.content_type} {code[f.status]} ({f.evidence_source}, no block)", STATUS_COLORS[f.status]))
            for bk in f.evidence_block_ids:
                if bk not in boxes:
                    header.append((f"{f.content_type} {code[f.status]} block {bk} not in merge", STATUS_COLORS[f.status]))
    if policy is not None:
        if policy.status != "ok":
            header.append((f"policy {policy.status}: {(policy.error or '')[:70]}", FAILED_COLOR))
        else:
            rec_color = STATUS_COLORS["present"] if policy.bucket_recommendation == "exclude" else STATUS_COLORS["absent"]
            cls = policy.applied.regulatory_class if policy.applied else "?"
            cov = policy.applied.dict_coverage if policy.applied else "?"
            header.append((f"policy {policy.bucket_recommendation} class={cls} coverage={cov}", rec_color))
            for v in policy.verdicts:
                tag = f" conflict:{v.conflict_group}" if v.conflict_group else ""
                unc = " (uncertain)" if v.finding_status == "uncertain" else ""
                header.append((f"  {v.dictionary_ref} {v.verdict_status}{unc}{tag}", rec_color))
            if policy.suppressed:
                header.append((f"  suppressed {len(policy.suppressed)}", CANDIDATE_COLOR))
    margin = 14 * len(header) + 8
    im = Image.new("RGB", (src.width, src.height + margin), (255, 255, 255))
    im.paste(src, (0, margin))
    draw = ImageDraw.Draw(im)
    y = 4
    for text, color in header:
        _label(draw, (4, y), text, color, font)
        y += 14
    draw.line((0, margin - 1, im.width, margin - 1), fill=(0, 0, 0), width=1)

    def box(bb, pad: int):
        return (bb.x - pad, bb.y - pad + margin, bb.x2 + pad, bb.y2 + pad + margin)

    # 1) 규칙 매칭 후보 — 얇은 파란 선 + 항목 ID(판정이 아니다)
    seen: set[tuple[str, str]] = set()
    for m in judge.matches:
        bb = boxes.get(m.block_key)
        if bb is None or (m.block_key, m.dictionary_ref) in seen:
            continue
        seen.add((m.block_key, m.dictionary_ref))
        draw.rectangle(box(bb, 2), outline=CANDIDATE_COLOR, width=1)
        _label(draw, (bb.x2 - 60, bb.y2 - 12 + margin), f"m {m.dictionary_ref}", CANDIDATE_COLOR, font)
    # 2) finding — 근거 블록에 상태별 색. 실패는 회색 테두리. 이미지 전용 근거는 박스 없음(설명 줄)
    if failed:
        draw.rectangle((0, margin, im.width - 1, im.height - 1), outline=FAILED_COLOR, width=8)
    else:
        offsets: dict[str, int] = {}
        for f in judge.content_findings.findings:
            color = STATUS_COLORS[f.status]
            for bk in f.evidence_block_ids:
                bb = boxes.get(bk)
                if bb is None:
                    continue
                n = offsets.get(bk, 0)
                offsets[bk] = n + 1
                draw.rectangle(box(bb, 3 * n), outline=color, width=3)
                _label(draw, (bb.x, max(margin, bb.y - 15 - 13 * n + margin)), f"{f.content_type} {code[f.status]} {f.evidence_source}", color, font)
    return _save(im, out)


LABEL_TRUE_COLOR = (220, 40, 40)  # red — 라벨(보호)
LABEL_FALSE_COLOR = (40, 160, 60)  # green — 라벨 아님
BLANK_COLOR = (150, 150, 150)  # gray — 공백 블록(VLM 미전송, false 기록)
MISSING_COLOR = (150, 0, 180)  # purple — 판정 목록에 없는 블록(입력 불일치 신호)


def _dashed_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color, dash: int = 6, width: int = 2) -> None:
    x1, y1, x2, y2 = box
    for x in range(x1, x2, dash * 2):
        draw.line((x, y1, min(x + dash, x2), y1), fill=color, width=width)
        draw.line((x, y2, min(x + dash, x2), y2), fill=color, width=width)
    for y in range(y1, y2, dash * 2):
        draw.line((x1, y, x1, min(y + dash, y2)), fill=color, width=width)
        draw.line((x2, y, x2, min(y + dash, y2)), fill=color, width=width)


def overlay_label(image_path: str | Path, merge: MergeResult, label: LabelResult, out: str | Path) -> Path:
    """④ 결과 오버레이. 블록 좌표는 MergeResult에서(LabelResult에는 좌표가 없다). 요약 줄은 콘텐츠를 덮지 않게 위에 흰 띠로 덧붙인다."""
    src = Image.open(image_path).convert("RGB")
    font = _font()
    decisions = {d.block_key: d for d in (label.labels or [])}
    failed = label.status == "failed" or label.labels is None
    header: list[tuple[str, tuple[int, int, int]]] = []
    if failed:
        header.append((f"FAILED label: {(label.error or '')[:80]}", FAILED_COLOR))
    else:
        n_true = sum(1 for d in label.labels if d.is_product_label)
        n_blank = sum(1 for d in label.labels if d.basis == "blank_text")
        header.append((f"label ok: blocks {len(label.labels)} true {n_true} false {len(label.labels) - n_true} (blank {n_blank}) "
                       f"llm_called={label.checked.llm_called}", (0, 0, 0)))
        missing = [b.block_key for b in merge.blocks if b.block_key not in decisions]
        if missing:
            header.append((f"no decision for {missing[:6]}", MISSING_COLOR))
    margin = 14 * len(header) + 8
    im = Image.new("RGB", (src.width, src.height + margin), (255, 255, 255))
    im.paste(src, (0, margin))
    draw = ImageDraw.Draw(im)
    y = 4
    for text, color in header:
        _label(draw, (4, y), text, color, font)
        y += 14
    draw.line((0, margin - 1, im.width, margin - 1), fill=(0, 0, 0), width=1)
    if failed:
        draw.rectangle((0, margin, im.width - 1, im.height - 1), outline=FAILED_COLOR, width=8)
        for b in merge.blocks:
            draw.rectangle((b.bbox.x, b.bbox.y + margin, b.bbox.x2, b.bbox.y2 + margin), outline=FAILED_COLOR, width=1)
        return _save(im, out)
    for b in merge.blocks:
        box = (b.bbox.x, b.bbox.y + margin, b.bbox.x2, b.bbox.y2 + margin)
        d = decisions.get(b.block_key)
        if d is None:
            draw.rectangle(box, outline=MISSING_COLOR, width=2)
            continue
        if d.basis == "blank_text":
            _dashed_rect(draw, box, BLANK_COLOR)
            continue
        color = LABEL_TRUE_COLOR if d.is_product_label else LABEL_FALSE_COLOR
        draw.rectangle(box, outline=color, width=4 if d.is_product_label else 2)
        _label(draw, (b.bbox.x, max(margin, b.bbox.y - 14 + margin)), f"{b.block_key} {'L' if d.is_product_label else '-'}", color, font)
    return _save(im, out)


def _save(im: Image.Image, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    return out
