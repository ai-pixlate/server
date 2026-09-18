"""렌더 엔진 (축소판) — Pillow 기반 조판·캡처 + 규격 검증.

번역된 text_block 들을 캔버스에 조판해 결과 PNG(bytes)를 만들고,
채널 규격(module_spec) 대비 규격 검증 결과(JSONB용 dict)를 만든다.
렌더(그리기)와 검증(규칙)을 분리해, 프로덕션에선 render_section_png 만
Playwright(HTML/CSS 조판)로 교체하면 되도록 설계.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

CANVAS_WIDTH = 1000  # 상세 이미지 기본 폭(px)
MARGIN = 48
LINE_GAP = 14
BLOCK_GAP = 28

# role별 조판 규칙(축소판) — 글자 크기 / 색상
ROLE_SIZE = {"title": 44, "body": 28, "caption": 20, "price": 34, "caution": 22}
ROLE_COLOR = {
    "title": (20, 20, 20),
    "body": (40, 40, 40),
    "caption": (110, 110, 110),
    "price": (198, 40, 40),
    "caution": (176, 96, 0),
}
DEFAULT_SIZE = 28
DEFAULT_COLOR = (40, 40, 40)


def _font(size: int):
    """Pillow 10.1+ 내장 TrueType 을 크기 지정해 로드(별도 폰트 설치 불필요)."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # 구버전 Pillow 대비
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, s: str, font, max_width: int) -> list[str]:
    """단어 단위 줄바꿈(영문 기준). 캔버스 폭을 넘지 않게 줄을 나눈다."""
    words = s.split()
    if not words:
        return [""]
    lines, cur = [], words[0]
    for w in words[1:]:
        trial = f"{cur} {w}"
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines


def render_section_png(blocks: list[dict], width: int = CANVAS_WIDTH) -> tuple[bytes, int, int]:
    """섹션의 블록들을 조판해 PNG bytes 를 만든다.

    blocks: [{role, trans_1, source_ko, is_excluded}] 순서대로.
    반환: (png_bytes, width, height).
    is_excluded(제품 라벨/브랜드 로고) 블록은 번역 대상이 아니므로 건너뛴다.
    """
    max_text_w = width - MARGIN * 2

    # 1) 측정용 임시 draw 로 줄바꿈·높이 레이아웃 계산
    tmp = Image.new("RGB", (width, 10), "white")
    d = ImageDraw.Draw(tmp)

    layout: list[tuple[str, object, tuple, int] | None] = []  # (line, font, color, line_h) | None(블록 여백)
    for b in blocks:
        if b.get("is_excluded"):
            continue
        content = (b.get("trans_1") or b.get("source_ko") or "").strip()
        if not content:
            continue
        role = b.get("role") or "body"
        font = _font(ROLE_SIZE.get(role, DEFAULT_SIZE))
        color = ROLE_COLOR.get(role, DEFAULT_COLOR)
        for line in _wrap(d, content, font, max_text_w):
            bb = d.textbbox((0, 0), line or " ", font=font)
            layout.append((line, font, color, bb[3] - bb[1]))
        layout.append(None)  # 블록 사이 여백

    # 2) 전체 높이 산정
    y = MARGIN
    for item in layout:
        if item is None:
            y += BLOCK_GAP
        else:
            y += item[3] + LINE_GAP
    total_h = max(y + MARGIN, 240)

    # 3) 실제 조판
    img = Image.new("RGB", (width, total_h), "white")
    draw = ImageDraw.Draw(img)
    yy = MARGIN
    for item in layout:
        if item is None:
            yy += BLOCK_GAP
            continue
        line, font, color, line_h = item
        draw.text((MARGIN, yy), line, font=font, fill=color)
        yy += line_h + LINE_GAP

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), width, total_h


def validate_deliverable(blocks: list[dict], width: int, height: int, spec: dict) -> dict:
    """규격 검증(축소판): 이미지 폭 · 포맷 · 블록 글자수(char_limit).

    spec: {"maxWidth": int, "charLimit": int|None}
    반환(JSONB 저장용): {"passed": bool, "checks": [...]}.
    error 심각도 검사가 모두 통과해야 passed=True (warning 은 통과 여부에 미반영).
    """
    checks: list[dict] = []
    max_width = spec.get("maxWidth") or CANVAS_WIDTH

    checks.append({
        "key": "image_width", "scope": "image",
        "expected": f"<= {max_width}", "actual": width,
        "severity": "error", "passed": width <= max_width,
    })
    checks.append({
        "key": "format", "scope": "image",
        "expected": "png", "actual": "png",
        "severity": "error", "passed": True,
    })

    for b in blocks:
        if b.get("is_excluded"):
            continue
        limit = b.get("char_limit") or spec.get("charLimit")
        if not limit:
            continue
        n = len(b.get("trans_1") or "")
        checks.append({
            "key": "char_limit", "scope": "block", "blockId": b.get("id"),
            "expected": f"<= {limit}", "actual": n,
            "severity": "warning", "passed": n <= limit,
        })

    passed = all(c["passed"] for c in checks if c["severity"] == "error")
    return {"passed": passed, "checks": checks}
