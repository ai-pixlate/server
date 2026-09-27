"""③ 줄·문단 병합 + 역할 분류 — `heuristic_v2` → `llm_assist` [pipeline.md 단계표 ③].

- 휴리스틱으로 영역 → 줄 → 문단(블록)을 만들고, LLM은 추가 병합과 역할 재판정만 한다. 분할 금지.
- 블록 1건 = 문단 1개. role 5종. ocr_confidence = 구성 영역 score 최솟값(types.ocr_confidence_of).
- 원시 영역은 모두 정확히 한 블록에 한 번 들어가고 값을 바꾸지 않는다 [계약 2.5].
- 휴리스틱 정의(계산식 · 정렬 · 입력 분류 · 역할 패턴 · 설정 검증)는 pipeline.md 7.1절이 정본이다.
  PoC 확인 동작과 우리 잠정 설계(open-questions #38)의 구분도 그 절에 있다. 규칙을 바꾸면 문서와 같은 커밋에서.

llm_assist는 미구현(프롬프트 pipeline/prompts/merge_assist.md 미작성). use_llm=False(CLI --no-llm)로 휴리스틱만 돈다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from pipeline.types import (
    BBox,
    Line,
    MergeResult,
    OcrRegion,
    OcrResult,
    Role,
    Section,
    TextBlock,
    block_key,
    line_key,
    ocr_confidence_of,
)

# 역할 탐지 패턴 — pipeline.md 7.1절 "역할 판정" 표의 "현재" 열. 주의문구의 의미 범위는 open-questions #9.
PRICE_RE = re.compile(r"₩|\d[\d,]*\s*원")
CAUTION_RE = re.compile(
    "|".join(
        [
            "주의", "경고", r"유의\s*사항", r"유의하(?!게)", "금지", "삼가", "반드시", "사용을 중지",
            "보관", "직사광선", "어린이", "알레르기", r"이상\s*(?:이|증상|반응)", "증상", "전문의",
            "상담", r"(?<!전)문의", "※",
        ]
    )
)

# 설정 값 검증 대상 — pipeline.md 7.1절 "설정 값 검증". llm_model · llm_temperature · prompt_path는 llm_assist 착수 때.
_INT_KEYS = ("gutter_min_px", "gutter_width_div", "title_max_lines", "caption_max_chars", "price_max_chars")
_NUM_KEYS = (
    "line_gap", "para_gap", "h_ratio", "overlap", "line_v_overlap", "line_gap_min", "para_gap_min",
    "left_align_tol", "title_pct", "caption_pct",
) + _INT_KEYS


def validate_config(cfg: dict[str, Any]) -> None:
    """[merge] 휴리스틱 설정을 검사하고 어기면 ValueError. 병합 입력을 건드리기 전에 부른다.

    타입(수치 · bool 제외 · 유한값 · 정수 전용)을 먼저 보고, 모두 맞을 때만 범위를 본다.
    """
    m = cfg.get("merge")
    if not isinstance(m, dict):
        raise ValueError("config에 [merge] 표가 없다")
    missing = [k for k in _NUM_KEYS + ("gutter", "llm_split") if k not in m]
    if missing:
        raise ValueError("잘못된 [merge] 설정: 없는 키 " + ", ".join(f"merge.{k}" for k in missing))

    errors: list[str] = []
    for k in _NUM_KEYS:
        v = m[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"merge.{k}={v!r} — 수치여야 한다(bool 제외)")
        elif not math.isfinite(v):
            errors.append(f"merge.{k}={v!r} — 유한값이어야 한다")
        elif k in _INT_KEYS and not isinstance(v, int):
            errors.append(f"merge.{k}={v!r} — 정수여야 한다")
    if not isinstance(m["gutter"], bool):
        errors.append(f"merge.gutter={m['gutter']!r} — bool이어야 한다")
    if m["llm_split"] is not False:
        errors.append(f"merge.llm_split={m['llm_split']!r} — false만 허용(분할 금지 고정)")
    if errors:
        raise ValueError("잘못된 [merge] 설정: " + "; ".join(errors))

    rules = [
        (0 <= m["line_v_overlap"] <= 1, "merge.line_v_overlap — 0 이상 1 이하"),
        (0 <= m["overlap"] <= 1, "merge.overlap — 0 이상 1 이하"),
        (m["line_gap_min"] <= m["line_gap"], "merge.line_gap_min ≤ merge.line_gap"),
        (m["para_gap_min"] <= m["para_gap"], "merge.para_gap_min ≤ merge.para_gap"),
        (m["h_ratio"] >= 1, "merge.h_ratio — 1 이상"),
        *[(m[k] >= 0, f"merge.{k} — 0 이상") for k in ("left_align_tol", "gutter_min_px", "caption_max_chars", "price_max_chars")],
        *[(m[k] >= 1, f"merge.{k} — 1 이상") for k in ("gutter_width_div", "title_max_lines")],
        *[(0 <= m[k] <= 100, f"merge.{k} — 0 이상 100 이하") for k in ("title_pct", "caption_pct")],
        (m["caption_pct"] <= m["title_pct"], "merge.caption_pct ≤ merge.title_pct"),
    ]
    errors = [msg for ok, msg in rules if not ok]
    if errors:
        raise ValueError("잘못된 [merge] 설정: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# 입력 분류 · 정렬
# ---------------------------------------------------------------------------
def region_kind(r: OcrRegion) -> str:
    """'zero'(폭 또는 높이 0) · 'empty'(text.strip() == "") · 'text'. 둘 다면 크기 0이 우선."""
    if r.bbox.w == 0 or r.bbox.h == 0:
        return "zero"
    if r.text.strip() == "":
        return "empty"
    return "text"


def _region_order(r: OcrRegion) -> tuple[int, int, str]:
    return (r.bbox.x, r.bbox.y, r.region_key)


@dataclass
class _Line:
    regions: list[OcrRegion] = field(default_factory=list)

    @property
    def bbox(self) -> BBox:
        return BBox.union([r.bbox for r in self.regions])

    @property
    def height(self) -> int:
        """판단용 줄 높이 = 영역 높이의 최댓값(줄 bbox 높이와 다르다)."""
        return max(r.bbox.h for r in self.regions)

    @property
    def order(self) -> tuple[int, int, str]:
        first = self.regions[0]
        return (first.bbox.y, first.bbox.x, first.region_key)

    @property
    def text(self) -> str:
        return " ".join(r.text.strip() for r in self.regions)


# ---------------------------------------------------------------------------
# gutter · 줄 병합 · 블록 병합
# ---------------------------------------------------------------------------
def find_gutters(regions: list[OcrRegion], width: int, m: dict[str, Any]) -> list[tuple[int, int]]:
    """영역이 차지하지 않는 열 [g0, g1) 중 최소 폭 이상이고 x=0 · x=width에 붙지 않은 구간."""
    min_w = max(m["gutter_min_px"], width // m["gutter_width_div"])
    covered = bytearray(width)
    for r in regions:
        x0, x1 = max(0, r.bbox.x), min(width, r.bbox.x2)
        if x1 > x0:
            covered[x0:x1] = b"\x01" * (x1 - x0)
    gutters: list[tuple[int, int]] = []
    x = 0
    while x < width:
        if covered[x]:
            x += 1
            continue
        start = x
        while x < width and not covered[x]:
            x += 1
        if start > 0 and x < width and x - start >= min_w:
            gutters.append((start, x))
    return gutters


def _joins_line(a: OcrRegion, b: OcrRegion, m: dict[str, Any], gutters: list[tuple[int, int]]) -> bool:
    ah, bh = a.bbox.h, b.bbox.h
    base = min(ah, bh)
    inter = max(0, min(a.bbox.y2, b.bbox.y2) - max(a.bbox.y, b.bbox.y))
    if inter / max(1, base) < m["line_v_overlap"]:
        return False
    gap = b.bbox.x - a.bbox.x2
    if not (m["line_gap_min"] * base <= gap <= m["line_gap"] * base):
        return False
    if max(ah, bh) / base > m["h_ratio"]:
        return False
    if m["gutter"] and any(a.bbox.x2 <= g0 and g1 <= b.bbox.x for g0, g1 in gutters):
        return False
    return True


def group_lines(regions: list[OcrRegion], m: dict[str, Any], gutters: list[tuple[int, int]]) -> list[_Line]:
    """병합 참여 영역 → 줄. 줄은 만든 순서로 보고 마지막 영역과 비교해 처음 맞는 줄에 붙인다. 결과는 줄 순서."""
    lines: list[_Line] = []
    for b in sorted(regions, key=_region_order):
        for ln in lines:
            if _joins_line(ln.regions[-1], b, m, gutters):
                ln.regions.append(b)
                break
        else:
            lines.append(_Line([b]))
    return sorted(lines, key=lambda ln: ln.order)


def _joins_block(p: _Line, l: _Line, m: dict[str, Any]) -> bool:
    ph, lh = p.height, l.height
    base = min(ph, lh)
    pb, lb = p.bbox, l.bbox
    gap = lb.y - pb.y2
    if not (m["para_gap_min"] * base <= gap <= m["para_gap"] * base):
        return False
    if max(ph, lh) / base > m["h_ratio"]:
        return False
    ov = max(0, min(pb.x2, lb.x2) - max(pb.x, lb.x)) / min(pb.w, lb.w)
    if ov < m["overlap"] and abs(pb.x - lb.x) > m["left_align_tol"] * base:
        return False
    return True


def _block_order(g: list[_Line]) -> tuple[int, int, str]:
    bb = BBox.union([ln.bbox for ln in g])
    return (bb.y, bb.x, g[0].regions[0].region_key)


def group_blocks(lines: list[_Line], m: dict[str, Any]) -> list[list[_Line]]:
    """줄 순서의 줄 → 그룹. 그룹은 만든 순서로 보고 마지막 줄과 비교해 처음 맞는 그룹에 붙인다."""
    groups: list[list[_Line]] = []
    for ln in lines:
        for g in groups:
            if _joins_block(g[-1], ln, m):
                g.append(ln)
                break
        else:
            groups.append([ln])
    return groups


# ---------------------------------------------------------------------------
# 역할 판정
# ---------------------------------------------------------------------------
def font_h(regions: list[OcrRegion]) -> int:
    """블록 영역 높이를 오름차순 정렬한 배열의 0부터 센 위치 n // 2의 값(짝수면 가운데 두 값 중 큰 값)."""
    hs = sorted(r.bbox.h for r in regions)
    return hs[len(hs) // 2]


def height_stats(heights: list[int], m: dict[str, Any]) -> tuple[float, float] | None:
    """(h_big, h_small) 선형 보간 백분위. 대상이 없거나 h_big == h_small이면 None(크기 규칙 생략)."""
    if not heights:
        return None
    h_big = float(np.percentile(heights, m["title_pct"], method="linear"))
    h_small = float(np.percentile(heights, m["caption_pct"], method="linear"))
    if h_big == h_small:
        return None
    return h_big, h_small


def classify(text: str, n_lines: int, fh: int, stats: tuple[float, float] | None, m: dict[str, Any]) -> Role:
    if PRICE_RE.search(text) and len(text) <= m["price_max_chars"]:
        return "price"
    if CAUTION_RE.search(text):
        return "caution"
    if stats is not None:
        h_big, h_small = stats
        if fh >= h_big and n_lines <= m["title_max_lines"]:
            return "title"
        if fh <= h_small and len(text) <= m["caption_max_chars"]:
            return "caption"
    return "body"


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def heuristic(section: Section, ocr: OcrResult, cfg: dict[str, Any]) -> MergeResult:
    """heuristic_v2 — LLM 없이 블록과 역할을 만든다(pipeline.md 7.1절)."""
    m = cfg["merge"]
    if ocr.section_key != section.section_key:
        raise ValueError(f"OCR 결과({ocr.section_key})와 섹션({section.section_key})이 다르다")
    keys = [r.region_key for r in ocr.regions]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{section.section_key}: 섹션 안에서 region_key가 중복된다")

    kinds = {r.region_key: region_kind(r) for r in ocr.regions}
    text_regions = [r for r in ocr.regions if kinds[r.region_key] == "text"]
    gutter_regions = [r for r in ocr.regions if kinds[r.region_key] in ("text", "empty")]
    gutters = find_gutters(gutter_regions, section.width, m) if m["gutter"] else []

    groups = group_blocks(group_lines(text_regions, m, gutters), m)
    groups += [[_Line([r])] for r in ocr.regions if kinds[r.region_key] != "text"]  # 빈 텍스트 · 크기 0 단독 블록
    groups.sort(key=_block_order)

    stats = height_stats([r.bbox.h for r in text_regions], m)
    blocks: list[TextBlock] = []
    n_line = 0
    for i, g in enumerate(groups, start=1):
        lines: list[Line] = []
        for ln in g:
            n_line += 1
            lines.append(Line(line_key=line_key(n_line), text=ln.text, bbox=ln.bbox, regions=list(ln.regions)))
        text = "\n".join(ln.text for ln in lines)
        regions = [r for ln in g for r in ln.regions]
        blocks.append(
            TextBlock(
                block_key=block_key(i),
                section_key=section.section_key,
                block_order=i,
                source_ko=text,
                source_lines=lines,
                bbox=BBox.union([r.bbox for r in regions]),
                role=classify(text, len(lines), font_h(regions), stats, m),
                ocr_confidence=ocr_confidence_of(lines),
            )
        )
    return MergeResult(section_key=section.section_key, blocks=blocks)


def run(section: Section, ocr: OcrResult, cfg: dict[str, Any], *, use_llm: bool = True) -> MergeResult:
    validate_config(cfg)
    if use_llm:
        raise NotImplementedError("③ llm_assist 미구현 — 휴리스틱만은 use_llm=False(CLI --no-llm) · docs/ai/status.md")
    return heuristic(section, ocr, cfg)
