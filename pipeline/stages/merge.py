"""③ 줄·문단 병합 + 역할 분류 — `heuristic_v2` → `llm_assist` [pipeline.md 단계표 ③].

- 휴리스틱으로 영역 → 줄 → 문단(블록)을 만들고, LLM은 추가 병합과 역할 재판정만 한다. 분할 금지.
- 블록 1건 = 문단 1개. role 5종. ocr_confidence = 구성 영역 score 최솟값(types.ocr_confidence_of).
- 원시 영역은 모두 정확히 한 블록에 한 번 들어가고 값을 바꾸지 않는다 [계약 2.5].
- 휴리스틱의 정본 규칙 요약은 pipeline.md 7.1절이다. 세부 정의(계산식 · 정렬 · 입력 분류 · 역할 패턴 · 설정 검증)와
  PoC 확인 동작 · 우리 잠정 설계(open-questions #38)의 구분은 docs/ai/archive/pipeline-v1-detail.md 7.1절 원문에 있고,
  현재 동작의 기준은 이 모듈과 손 계산 테스트다. 규칙 수준의 변경은 pipeline.md 7.1절을 같은 커밋에서 고친다.

- llm_assist v1 규칙 요약은 pipeline.md 7.2절, 세부 정의(입력 · 출력 · 응답 검증 · 로컬 조립 · 기록 · 실행 모드별 설정 검증)
  원문은 archive 7.2절(open-questions #41).
  v1은 블록 병합만 한다 — 같은 줄 조각의 줄 복구는 하지 않는다. use_llm=False(CLI --no-llm)면 휴리스틱만 돈다.

- OCR 후처리(open-questions #86, pipeline.md 7.1 · 7.2절) — 두 스위치 모두 기본 꺼짐이며, 끄면 위 동작과 같다.
  `merge.xycut`: 텍스트 영역을 여백(XY-cut)으로 문단 후보 영역으로 나누고, 영역마다 세로 중심으로 가로줄을 묶어
  줄 · 블록을 만든다. 블록 순서는 후보 영역 → 가로줄 순번 → x다. 휴리스틱 블록은 후보 영역을 넘지 않고, LLM 묶음은
  칸 묶음(y 절단으로만 이어진 후보 영역의 묶음)을 넘지 못한다 — 나란한 칸(x 절단)은 계속 경계다.
  `merge.fullwidth`: `Line.text` · `source_ko`에서 전각 영문 · 숫자 · 기호(U+FF01~FF5E)와 전각 공백(U+3000)만 반각으로 바꾼다.
  `merge.num_o`: `Line.text` · `source_ko`에서 숫자 덩어리 안의 영문 o/O를 0으로 바꾼다(전각 변환 뒤, 2026-10-26 채택).
  원시 영역(`regions[].text` 등)은 어느 경우에도 바꾸지 않는다.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from pipeline.types import (
    ROLES,
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
from pipeline.vlm import API_KEY_ENV, GeminiMergeAssistant, LlmReply, MergeAssistant, VlmError, sha256_text

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

# 설정 값 검증 대상 — pipeline.md 7.1절 "설정 값 검증". llm_model · llm_temperature · llm_timeout_s · prompt_path는
# validate_llm_config(7.2절)가 실행 모드별로 검사한다.
_INT_KEYS = ("gutter_min_px", "gutter_width_div", "title_max_lines", "caption_max_chars", "price_max_chars")
_NUM_KEYS = (
    "line_gap", "para_gap", "h_ratio", "overlap", "line_v_overlap", "line_gap_min", "para_gap_min",
    "left_align_tol", "title_pct", "caption_pct", "xycut_gap_ratio", "line_center_tol", "line_center_tol_min_px",
) + _INT_KEYS
_BOOL_KEYS = ("gutter", "xycut", "fullwidth", "num_o")  # 정확히 bool 타입만 받는다

# 전각 → 반각: 전각 영문 · 숫자 · 기호(U+FF01~FF5E)와 전각 공백(U+3000)만. NFKC는 쓰지 않는다(①·™·한글 자모는 그대로)
# [`open-questions.md` #86]. 코드 포인트 1:1 치환이라 같은 조립 문자열의 길이는 바뀌지 않는다.
FULLWIDTH = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
FULLWIDTH[0x3000] = 0x20

# 숫자 속 o/O → 0: 숫자로 시작하고 숫자 · o · O · 쉼표 · 점만 있는 덩어리 안의 o/O만 [`open-questions.md` #86].
# 참고 구현 seed-builder PR #1(fc72ecd) reorder_ocr_v2.py의 NUM_WITH_O에서 시작 조건만 좁혔다(2026-10-26 사용자 결정): 덩어리가
# 영문자 · 숫자 · 점 · 쉼표 바로 뒤에서 시작하면 바꾸지 않는다. 참고 구현은 영문자만 막아, 영문에 붙은 숫자열의 다음 숫자부터
# 다시 매칭했다(SPF50O → SPF500, B60O1 → B6001). 예: SPF 5O → 50, 1oo% → 100%, (2o17) → (2017), 13.O → 13.0.
# 1:1 치환이라 같은 조립 문자열의 길이는 바뀌지 않는다. SPF5O · SPF50O · v1.2O(영문에 붙은 숫자열) · 3o개(한글이 붙음)는 바꾸지 않는다.
NUM_WITH_O = re.compile(r"(?<![A-Za-z0-9.,])(\d[\doO]*(?:[.,][\doO]+)*)(?=%|\b)")


def validate_config(cfg: dict[str, Any]) -> None:
    """[merge] 휴리스틱 설정을 검사하고 어기면 ValueError. 병합 입력을 건드리기 전에 부른다.

    타입(수치 · bool 제외 · 유한값 · 정수 전용)을 먼저 보고, 모두 맞을 때만 범위를 본다.
    """
    m = cfg.get("merge")
    if not isinstance(m, dict):
        raise ValueError("config에 [merge] 표가 없다")
    missing = [k for k in _NUM_KEYS + _BOOL_KEYS + ("llm_split",) if k not in m]
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
    for k in _BOOL_KEYS:
        if not isinstance(m[k], bool):
            errors.append(f"merge.{k}={m[k]!r} — bool이어야 한다")
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
        *[(m[k] > 0, f"merge.{k} — 0 초과") for k in ("xycut_gap_ratio", "line_center_tol")],
        (m["line_center_tol_min_px"] >= 0, "merge.line_center_tol_min_px — 0 이상"),
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
    # xycut일 때만: (가로줄 순번, 첫 영역 x, 첫 영역 region_key). 줄 · 블록 순서와 LLM 재조립 정렬에 쓴다
    rank: tuple[int, int, str] | None = None

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


def to_halfwidth(text: str) -> str:
    """전각 영문 · 숫자 · 기호(U+FF01~FF5E) · 전각 공백(U+3000)만 반각으로. 그 밖의 문자는 그대로."""
    return text.translate(FULLWIDTH)


def fix_num_o(text: str) -> str:
    """숫자 덩어리(NUM_WITH_O) 안의 영문 o/O만 숫자 0으로. 그 밖의 문자는 그대로."""
    return NUM_WITH_O.sub(lambda mt: mt.group(1).replace("o", "0").replace("O", "0"), text)


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
# OCR 후처리 — 여백 분리(XY-cut) · 세로 중심 줄 정렬 [`open-questions.md` #86]
# ---------------------------------------------------------------------------
@dataclass
class _Cut:
    """분할 트리의 내부 노드. 좌표 < value면 low(왼쪽 · 위), 그 밖(같음 포함)은 high(오른쪽 · 아래)."""

    axis: str  # "x" | "y"
    value: float
    low: "_Cut | int | None" = None
    high: "_Cut | int | None" = None


def widest_gap(spans: list[tuple[int, int]]) -> tuple[float, float | None]:
    """[lo, hi) 구간들을 lo 순으로 훑어 어느 구간도 덮지 않는 가장 넓은 빈 구간의 (폭, 가운데 좌표). 없으면 (0, None).
    폭이 같으면 앞쪽(작은 좌표)을 고른다."""
    spans = sorted(spans)
    best, cut = 0.0, None
    reach = spans[0][1]
    for lo, hi in spans[1:]:
        if lo - reach > best:
            best, cut = float(lo - reach), (reach + lo) / 2
        reach = max(reach, hi)
    return best, cut


def _choose_cut(regions: list[OcrRegion], min_gap: float) -> tuple[str, float] | None:
    """더 넓은 여백을 먼저 자른다(같으면 x). 최소 여백 미만이면 None(후보 영역 하나)."""
    if len(regions) <= 1:
        return None
    gap_x, cut_x = widest_gap([(r.bbox.x, r.bbox.x2) for r in regions])
    gap_y, cut_y = widest_gap([(r.bbox.y, r.bbox.y2) for r in regions])
    if gap_x >= min_gap and gap_x >= gap_y and cut_x is not None:
        return "x", cut_x
    if gap_y >= min_gap and cut_y is not None:
        return "y", cut_y
    return None


def _coord(r: OcrRegion, axis: str) -> int:
    return r.bbox.x if axis == "x" else r.bbox.y


def build_cut_tree(regions: list[OcrRegion], min_gap: float) -> tuple[_Cut | int, list[list[OcrRegion]]]:
    """텍스트 영역의 분할 트리와 후보 영역(잎) 목록. 잎 번호 = 왼쪽 · 위 자식을 먼저 도는 깊이 우선 순서(0부터).

    영역은 좌상단 좌표가 절단값보다 작으면 low, 아니면 high로 간다. 재귀 대신 명시적 스택을 쓴다(긴 섹션의 많은 영역)."""
    leaves: list[list[OcrRegion]] = []
    holder = _Cut("x", 0.0)  # 루트를 받는 임시 부모(holder.low)
    stack: list[tuple[list[OcrRegion], _Cut, str]] = [(regions, holder, "low")]
    while stack:
        regs, parent, side = stack.pop()
        chosen = _choose_cut(regs, min_gap)
        node: _Cut | int
        if chosen is None:
            node = len(leaves)
            leaves.append(regs)
        else:
            axis, value = chosen
            node = _Cut(axis, value)
            stack.append(([r for r in regs if _coord(r, axis) >= value], node, "high"))
            stack.append(([r for r in regs if _coord(r, axis) < value], node, "low"))
        setattr(parent, side, node)
    root = holder.low
    assert root is not None
    return root, leaves


def locate(tree: _Cut | int, x: float, y: float) -> int:
    """좌표 (x, y)가 속하는 후보 영역 번호. 루트부터 선택한 자식의 절단만 따라간다. 절단값과 같으면 high."""
    node = tree
    while isinstance(node, _Cut):
        node = node.low if (x if node.axis == "x" else y) < node.value else node.high
    assert isinstance(node, int)
    return node


def column_groups(tree: _Cut | int) -> list[int]:
    """후보 영역(잎, 0부터) → 칸 묶음 번호(1부터). 깊이 우선 순서에서 이웃한 두 잎 사이 경로(공통 조상 포함)의 절단이
    모두 y면 같은 칸 묶음으로 잇는다. 칸 묶음은 읽기 순서에서 연달아 있는 잎들이다 — 경로가 모두 y여도 사이에 다른 칸
    묶음이 끼면(예: 위 제목과 아래 문단 사이의 다단 영역) 잇지 않는다.

    이웃한 두 잎의 공통 조상 N = 뒤 잎이 처음 들어선 high 가지의 부모다. 경로가 모두 y ⇔ N이 y 절단이고, N부터 두 잎까지
    x 절단이 없다(위쪽 x 절단 수가 N과 같다). 재귀 대신 명시적 스택을 쓴다."""
    info: dict[int, tuple[int, tuple[_Cut, int] | None]] = {}  # 잎 → (위쪽 x 절단 수, (공통 조상, 그 위쪽 x 절단 수))
    stack: list[tuple[_Cut | int | None, int, tuple[_Cut, int] | None]] = [(tree, 0, None)]
    while stack:
        node, nx, turn = stack.pop()
        if isinstance(node, _Cut):
            below = nx + (node.axis == "x")
            stack.append((node.high, below, (node, nx)))
            stack.append((node.low, below, turn))  # low(왼쪽 · 위)를 먼저 돈다 — 첫 잎은 들어온 turn을 이어받는다
        else:
            assert isinstance(node, int)
            info[node] = (nx, turn)
    groups: list[int] = []
    prev_nx = 0
    for leaf in sorted(info):
        nx, turn = info[leaf]
        joined = bool(groups) and turn is not None and turn[0].axis == "y" and nx == turn[1] == prev_nx
        groups.append(groups[-1] if joined else len(set(groups)) + 1)
        prev_nx = nx
    return groups


def _center_y(r: OcrRegion) -> float:
    return r.bbox.y + r.bbox.h / 2


def reading_lines(regions: list[OcrRegion], m: dict[str, Any]) -> list[tuple[float, list[OcrRegion]]]:
    """후보 영역 하나의 텍스트 영역 → 가로줄 [(기준 y, x순 영역)]. (세로 중심, x, region_key) 순으로 보며, 가로줄을 시작한
    상자의 세로 중심(기준 y)과의 차이가 허용치 이하면 같은 가로줄이다. 허용치 = max(line_center_tol_min_px,
    line_center_tol × 영역 높이 중앙값). 기준 y는 이후 다시 계산하지 않는다."""
    if not regions:
        return []
    tol = max(m["line_center_tol_min_px"], m["line_center_tol"] * statistics.median(r.bbox.h for r in regions))
    rows: list[tuple[float, list[OcrRegion]]] = []
    for r in sorted(regions, key=lambda r: (_center_y(r), r.bbox.x, r.region_key)):
        if rows and _center_y(r) - rows[-1][0] <= tol:
            rows[-1][1].append(r)
        else:
            rows.append((_center_y(r), [r]))
    return [(base, sorted(regs, key=lambda r: (r.bbox.x, r.bbox.y, r.region_key))) for base, regs in rows]


def _split_row(regs: list[OcrRegion], m: dict[str, Any], gutters: list[tuple[int, int]]) -> list[list[OcrRegion]]:
    """가로줄 하나(x순)를 Line 조각으로 나눈다. 이웃 영역 사이에 gutter가 있을 때만 끊는다.

    높이비(h_ratio)는 보지 않는다 — 위첨자 · 각주 기호와 작은 조각이 대응 줄에서 떨어지고, 큰 글자 사이에 낀 작은 영역이
    줄을 끊었기 때문이다(4단계 1차 사람 검수, 사용자 결정 2026-10-23 — open-questions #86). h_ratio는 블록 병합과
    xycut을 끈 기존 줄 병합에서는 그대로 쓴다."""
    pieces = [[regs[0]]]
    for b in regs[1:]:
        a = pieces[-1][-1]
        if m["gutter"] and any(a.bbox.x2 <= g0 and g1 <= b.bbox.x for g0, g1 in gutters):
            pieces.append([b])
        else:
            pieces[-1].append(b)
    return pieces


@dataclass
class _Layout:
    """xycut 배치 정보(섹션 블록 키 · 줄 키 기준). LLM 재조립이 칸 묶음 경계와 읽기 순서를 지키는 데 쓴다."""

    block_region: dict[str, int] = field(default_factory=dict)  # 블록 키 → 후보 영역 번호(1부터)
    block_group: dict[str, int] = field(default_factory=dict)  # 블록 키 → 칸 묶음 번호(1부터) — LLM 병합의 경계
    leaf_group: list[int] = field(default_factory=list)  # 후보 영역(0부터) → 칸 묶음 번호, 진단용
    block_rank: dict[str, tuple] = field(default_factory=dict)  # 블록 키 → 영역 안 정렬 키
    line_rank: dict[str, tuple[int, int, str]] = field(default_factory=dict)  # 줄 키 → (가로줄 순번, x, region_key)
    n_regions: int = 0
    cuts: list[dict[str, Any]] = field(default_factory=list)  # 진단용 분할 트리(전위 순회, path = 루트부터 L/H)
    row_bases: list[list[float]] = field(default_factory=list)  # 진단용 후보 영역별 가로줄 기준 y


def _cut_list(tree: _Cut | int) -> list[dict[str, Any]]:
    """분할 트리를 전위 순회 목록으로. path는 루트부터 low = L · high = H. 잎은 후보 영역 번호(1부터)."""
    out: list[dict[str, Any]] = []
    stack: list[tuple[_Cut | int | None, str]] = [(tree, "")]
    while stack:
        node, path = stack.pop()
        if isinstance(node, _Cut):
            out.append({"path": path, "axis": node.axis, "value": node.value})
            stack.append((node.high, path + "H"))
            stack.append((node.low, path + "L"))
        else:
            out.append({"path": path, "region": (node or 0) + 1})
    return out


def _xycut_groups(
    regions: list[OcrRegion], kinds: dict[str, str], text_regions: list[OcrRegion], m: dict[str, Any],
    gutters: list[tuple[int, int]],
) -> tuple[list[tuple[int, tuple, list[_Line]]], _Cut | int, list[list[float]]]:
    """([(후보 영역 번호, 영역 안 정렬 키, 줄 묶음)] 최종 블록 순서, 분할 트리, 영역별 가로줄 기준 y).

    텍스트 블록 키 = (시작 가로줄 순번, 0, 첫 영역 x, 첫 영역 region_key). 빈 텍스트 · 크기 0 단독 블록은 분할 트리를
    좌상단으로 따라가 후보 영역을 찾고, k = 기준 y ≤ 단독 영역 세로 중심인 가로줄 수로 키 (k, 1, 위쪽 y, x, region_key).
    블록 시작 가로줄 기준 배치라 여러 줄 문단의 중간에 끼어들지 않는다. 텍스트 영역이 없으면 영역 하나 · 가로줄 0개다."""
    if text_regions:
        min_gap = m["xycut_gap_ratio"] * statistics.median(r.bbox.h for r in text_regions)
        tree, leaves = build_cut_tree(text_regions, min_gap)
    else:
        tree, leaves = 0, [[]]
    items: list[tuple[int, tuple, list[_Line]]] = []
    bases: list[list[float]] = []
    for li, leaf in enumerate(leaves):
        rows = reading_lines(leaf, m)
        bases.append([base for base, _ in rows])
        lines = [
            _Line(piece, rank=(idx, piece[0].bbox.x, piece[0].region_key))
            for idx, (_, regs) in enumerate(rows, start=1)
            for piece in _split_row(regs, m, gutters)
        ]
        for g in group_blocks(lines, m):
            rank = g[0].rank
            assert rank is not None
            items.append((li, (rank[0], 0, rank[1], rank[2]), g))
    for r in regions:
        if kinds[r.region_key] == "text":
            continue
        li = locate(tree, r.bbox.x, r.bbox.y)
        k = sum(1 for b in bases[li] if b <= _center_y(r))
        items.append((li, (k, 1, r.bbox.y, r.bbox.x, r.region_key), [_Line([r])]))
    items.sort(key=lambda it: (it[0], it[1]))
    return [(li + 1, key, g) for li, key, g in items], tree, bases


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
    """heuristic_v2 — LLM 없이 블록과 역할을 만든다(pipeline.md 7.1절). 후처리 스위치는 모듈 설명 참고."""
    return _heuristic(section, ocr, cfg)[0]


def _section_stats(regions: list[OcrRegion], m: dict[str, Any]) -> tuple[float, float] | None:
    return height_stats([r.bbox.h for r in regions if region_kind(r) == "text"], m)


def _heuristic(
    section: Section, ocr: OcrResult, cfg: dict[str, Any]
) -> tuple[MergeResult, _Layout | None, dict[str, Any], dict[str, Any] | None]:
    """(결과, xycut 배치 정보 또는 None, 후처리 요약, 후처리 진단 또는 None).
    요약은 LLM 호출 기록(merge_debug)에, 진단은 LLM 사용 여부와 관계없이 merge_post_debug에 남는다(두 스위치가 모두 꺼지면 None)."""
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

    layout: _Layout | None = None
    placed: list[tuple[int, tuple, list[_Line]]] | None = None
    if m["xycut"]:
        placed, tree, bases = _xycut_groups(ocr.regions, kinds, text_regions, m, gutters)
        groups = [g for _, _, g in placed]
        layout = _Layout(n_regions=len(bases), cuts=_cut_list(tree), row_bases=bases, leaf_group=column_groups(tree))
    else:
        groups = group_blocks(group_lines(text_regions, m, gutters), m)
        groups += [[_Line([r])] for r in ocr.regions if kinds[r.region_key] != "text"]  # 빈 텍스트 · 크기 0 단독 블록
        groups.sort(key=_block_order)

    stats = height_stats([r.bbox.h for r in text_regions], m)
    blocks: list[TextBlock] = []
    n_line = 0
    changed: list[str] = []
    num_o_changed: list[str] = []
    for i, g in enumerate(groups, start=1):
        lines: list[Line] = []
        for ln in g:
            n_line += 1
            text = ln.text
            if m["fullwidth"]:
                half = to_halfwidth(text)
                if half != text:
                    changed.append(line_key(n_line))
                text = half
            if m["num_o"]:  # 전각 변환 뒤(참고 구현과 같은 순서)
                fixed = fix_num_o(text)
                if fixed != text:
                    num_o_changed.append(line_key(n_line))
                text = fixed
            lines.append(Line(line_key=line_key(n_line), text=text, bbox=ln.bbox, regions=list(ln.regions)))
            if layout is not None and ln.rank is not None:
                layout.line_rank[lines[-1].line_key] = ln.rank
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
        if layout is not None and placed is not None:
            layout.block_region[block_key(i)] = placed[i - 1][0]
            layout.block_group[block_key(i)] = layout.leaf_group[placed[i - 1][0] - 1]
            layout.block_rank[block_key(i)] = placed[i - 1][1]
    post: dict[str, Any] = {"xycut": m["xycut"], "fullwidth": m["fullwidth"]}
    if layout is not None:
        post["candidate_regions"] = layout.n_regions
    if m["fullwidth"]:
        post["fullwidth_changed_lines"] = len(changed)
    if m["num_o"]:  # 켰을 때만 기록 — 끄면 기존 기록 형식 그대로
        post["num_o"] = True
        post["num_o_changed_lines"] = len(num_o_changed)
    result = MergeResult(section_key=section.section_key, blocks=blocks)
    diag = (_post_diagnostics(section, result, layout, post, changed, m, num_o_changed)
            if m["xycut"] or m["fullwidth"] or m["num_o"] else None)
    return result, layout, post, diag


_DIAG_KEYS = ("xycut", "fullwidth", "xycut_gap_ratio", "line_center_tol", "line_center_tol_min_px", "h_ratio", "gutter")


def _post_diagnostics(
    section: Section, heur: MergeResult, layout: _Layout | None, post: dict[str, Any], changed: list[str], m: dict[str, Any],
    num_o_changed: list[str] | None = None,
) -> dict[str, Any]:
    """후처리 진단(개발 확인용, 계약 값 아님). 블록 키는 휴리스틱(LLM 전) 섹션 키다 — LLM을 쓰면 최종 키는 merge_debug의
    block_map으로 잇는다. `interleaving_pairs`는 같은 후보 영역에서 가로줄 범위가 교차하는 LLM 전송 블록 쌍으로, LLM이 합치면
    줄이 가로줄 순으로 다시 정렬돼 번갈아 섞일 수 있는 후보다(pipeline.md 7.2절)."""
    diag: dict[str, Any] = {
        "section_key": section.section_key,
        "source_image_id": section.source_image_id,
        "postprocess": dict(post),
        "config": {k: m[k] for k in _DIAG_KEYS},
        "fullwidth_changed_lines": changed,
    }
    if m["num_o"]:
        diag["config"]["num_o"] = True
        diag["num_o_changed_lines"] = num_o_changed or []
    if layout is None:
        return diag
    blocks = []
    for b in heur.blocks:
        rows = [layout.line_rank[ln.line_key][0] for ln in b.source_lines if ln.line_key in layout.line_rank]
        blocks.append({"block_key": b.block_key, "region": layout.block_region[b.block_key],
                       "group": layout.block_group[b.block_key], "sent_to_llm": _sendable(b),
                       "rows": [min(rows), max(rows)] if rows else None, "after_rows": None if rows else layout.block_rank[b.block_key][0],
                       "role": b.role, "text": b.source_ko})
    pairs = []
    sent = [x for x in blocks if x["sent_to_llm"]]
    for i, a in enumerate(sent):
        for b in sent[i + 1:]:
            if a["region"] == b["region"] and a["rows"][0] <= b["rows"][1] and b["rows"][0] <= a["rows"][1]:
                pairs.append([a["block_key"], b["block_key"]])
    diag.update({
        "cuts": layout.cuts,
        "regions": [{"region": n, "group": layout.leaf_group[n - 1], "rows": len(bases), "row_bases": bases,
                     "blocks": [x["block_key"] for x in blocks if x["region"] == n]}
                    for n, bases in enumerate(layout.row_bases, start=1)],
        "blocks": blocks,
        "interleaving_pairs": pairs,
    })
    return diag


# ---------------------------------------------------------------------------
# llm_assist v1 [pipeline.md 7.2절 · open-questions #41]
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[2]  # pipeline 패키지의 상위 폴더
REGION_PROMPT_PATH = "pipeline/prompts/merge_assist_region.md"  # xycut일 때 merge.prompt_path 뒤에 붙는 영역 규칙(#86)

Recorder = Callable[[dict[str, Any]], None]


class LlmResponseError(VlmError):
    """LLM 응답이 7.2절 응답 검증을 통과하지 못했다. reasons = 어긴 항목 목록."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("LLM 응답 검증 실패: " + "; ".join(reasons))


def resolve_prompt_path(p: str) -> Path:
    """상대 경로는 레포 루트(pipeline 패키지의 상위 폴더) 기준, 절대 경로는 그대로(7.2절)."""
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def _finite_number(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def validate_llm_config(cfg: dict[str, Any], *, need_api_key: bool) -> str:
    """LLM 실행·재생 모드의 설정 검증(7.2절). 어기면 ValueError. 통과하면 프롬프트 원문을 돌려준다.

    need_api_key: 실제 호출(기본 Gemini 호출자)일 때만 True. 재생 · 주입 호출자는 키가 필요 없다.
    """
    m = cfg["merge"]
    missing = [k for k in ("llm_model", "llm_temperature", "llm_timeout_s", "prompt_path") if k not in m]
    if missing:
        raise ValueError("잘못된 [merge] LLM 설정: 없는 키 " + ", ".join(f"merge.{k}" for k in missing))
    errors: list[str] = []
    if not isinstance(m["llm_model"], str) or not m["llm_model"].strip():
        errors.append(f"merge.llm_model={m['llm_model']!r} — 비어 있지 않은 문자열")
    t = m["llm_temperature"]
    if not _finite_number(t) or not 0 <= t <= 2:
        errors.append(f"merge.llm_temperature={t!r} — 수치(bool 제외) · 유한값 · 0 이상 2 이하")
    to = m["llm_timeout_s"]
    if not _finite_number(to) or not to > 0:
        errors.append(f"merge.llm_timeout_s={to!r} — 수치(bool 제외) · 유한값 · 0 초과")
    prompt = ""
    pp = m["prompt_path"]
    if not isinstance(pp, str) or not pp.strip():
        errors.append(f"merge.prompt_path={pp!r} — 비어 있지 않은 문자열")
    else:
        path = resolve_prompt_path(pp)
        try:
            prompt = path.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"merge.prompt_path — 프롬프트가 비어 있다: {path}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"merge.prompt_path — 파일을 UTF-8로 읽을 수 없다: {path} ({e.__class__.__name__})")
    if m.get("xycut") is True and prompt:
        # xycut이면 영역 규칙 보충 프롬프트를 뒤에 붙인다(키 없음 — 위치는 dev.md 2절). 끄면 프롬프트가 바뀌지 않는다
        path = resolve_prompt_path(REGION_PROMPT_PATH)
        try:
            extra = path.read_text(encoding="utf-8")
            if not extra.strip():
                errors.append(f"영역 규칙 보충 프롬프트가 비어 있다: {path}")
            prompt = prompt.rstrip("\n") + "\n\n" + extra
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"영역 규칙 보충 프롬프트를 UTF-8로 읽을 수 없다: {path} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다 — 실제 LLM 호출에 필요하다")
    if errors:
        raise ValueError("잘못된 [merge] LLM 설정: " + "; ".join(errors))
    return prompt


def llm_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """기록 · 재생 비교에 쓰는 모델 설정(모델 · 온도 · 시간 제한)."""
    m = cfg["merge"]
    return {"model": m["llm_model"], "temperature": m["llm_temperature"], "timeout_s": m["llm_timeout_s"]}


def default_assistant(cfg: dict[str, Any]) -> GeminiMergeAssistant:
    c = llm_config(cfg)
    return GeminiMergeAssistant(model=c["model"], temperature=c["temperature"], timeout_s=c["timeout_s"])


def _sendable(b: TextBlock) -> bool:
    """텍스트가 있고 크기 0이 아닌 휴리스틱 블록만 LLM에 보낸다(빈 텍스트 · 크기 0 단독 블록은 보존)."""
    return all(region_kind(r) == "text" for ln in b.source_lines for r in ln.regions)


def build_payload(
    section: Section, heur: MergeResult, layout: _Layout | None = None
) -> tuple[dict[str, Any], dict[str, TextBlock]]:
    """LLM 입력(7.2절)과 임시 ID → 휴리스틱 블록 대응. ID는 보낼 블록의 블록 순서대로 b1, b2, …
    xycut이면(layout) 블록마다 `region`을 더한다 — 값은 칸 묶음 번호(LLM이 넘지 못하는 경계, #86)다."""
    send = [b for b in heur.blocks if _sendable(b)]
    ids = {f"b{i}": b for i, b in enumerate(send, start=1)}
    blocks = []
    for bid, b in ids.items():
        item: dict[str, Any] = {
            "id": bid,
            "text": b.source_ko,
            "bbox": [b.bbox.x, b.bbox.y, b.bbox.w, b.bbox.h],
            "lines": len(b.source_lines),
            "font_h": font_h([r for ln in b.source_lines for r in ln.regions]),
        }
        if layout is not None:
            item["region"] = layout.block_group[b.block_key]
        blocks.append(item)
    payload = {"section": {"width": section.width, "height": section.height}, "blocks": blocks}
    return payload, ids


def payload_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_groups(text: str, ids: dict[str, TextBlock]) -> list[tuple[list[str], str]]:
    """응답 검증(7.2절). 형식 · 스키마 · 모든 ID가 정확히 한 번 · 미등록 ID · 빈 묶음 · 역할 5종. 어기면 LlmResponseError."""
    try:
        data = json.loads(text)
    except ValueError:
        raise LlmResponseError(["JSON 형식이 아니다"]) from None
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise LlmResponseError(["최상위가 {\"blocks\": [...]} 형식이 아니다"])
    reasons: list[str] = []
    groups: list[tuple[list[str], str]] = []
    for i, item in enumerate(data["blocks"]):
        if not isinstance(item, dict) or not isinstance(item.get("members"), list) or "role" not in item:
            reasons.append(f"blocks[{i}] — members 배열과 role이 있어야 한다")
            continue
        members = item["members"]
        if not members:
            reasons.append(f"blocks[{i}] — 빈 members")
        if not all(isinstance(x, str) for x in members):
            reasons.append(f"blocks[{i}] — members는 문자열 ID 배열이어야 한다")
            continue
        if item["role"] not in ROLES:
            reasons.append(f"blocks[{i}] — 모르는 role {item['role']!r}")
        groups.append((list(members), item["role"]))
    seen = Counter(x for members, _ in groups for x in members)
    unknown = sorted(x for x in seen if x not in ids)
    dup = sorted(x for x, n in seen.items() if n > 1 and x in ids)
    missing = sorted((x for x in ids if x not in seen), key=lambda s: int(s[1:]))
    if unknown:
        reasons.append("모르는 ID " + ", ".join(unknown))
    if dup:
        reasons.append("두 번 이상 나온 ID " + ", ".join(dup))
    if missing:
        reasons.append("빠진 ID " + ", ".join(missing))
    if reasons:
        raise LlmResponseError(reasons)
    return groups


def assemble(
    section: Section, heur: MergeResult, ids: dict[str, TextBlock], groups: list[tuple[list[str], str]],
    layout: _Layout | None = None, m: dict[str, Any] | None = None,
) -> tuple[MergeResult, dict[str, str], dict[str, Any]]:
    """로컬 조립(7.2절, v1 — 블록 병합만). 같은 줄 조각의 줄 복구는 하지 않는다. 보내지 않은 블록은 그대로.
    반환: (결과, 휴리스틱 블록 키 → 섹션 최종 블록 키, xycut 조립 기록).

    layout 없음(기본): 묶음의 멤버는 휴리스틱 블록 순서로 정렬해 줄을 그대로 잇고, 묶음은 bbox (y, x) 순.
    layout 있음(xycut, #86): 칸 묶음을 넘는 묶음은 칸 묶음별로 나누고 LLM 역할 대신 조각마다 classify를 다시 적용한다.
    읽기 순서 키는 (후보 영역, 가로줄 순번, x, region_key)다 — 가로줄 순번은 후보 영역마다 1부터 다시 시작하므로 영역을
    맨 앞에 둔다. 묶음 안의 모든 줄을 이 키로 다시 정렬하고(블록 단위로 잇지 않음), 묶음은 이 순서로 가장 앞선 멤버
    자리에 둔다. 같은 후보 영역에서 멤버들의 가로줄 범위가 교차하는 묶음은 기록에 남긴다."""
    notes: dict[str, Any] = {}
    sent = {b.block_key for b in ids.values()}
    if layout is None:
        out: list[tuple[list[TextBlock], list[Line], str]] = []
        for members, role in groups:
            bs = sorted((ids[x] for x in members), key=lambda b: b.block_order)
            out.append((bs, [ln for b in bs for ln in b.source_lines], role))
        out += [([b], list(b.source_lines), b.role) for b in heur.blocks if b.block_key not in sent]

        def order(g: tuple[list[TextBlock], list[Line], str]) -> tuple:
            bb = BBox.union([b.bbox for b in g[0]])
            return (bb.y, bb.x, g[0][0].source_lines[0].regions[0].region_key)

        out.sort(key=order)
    else:
        assert m is not None
        stats = _section_stats([r for b in heur.blocks for ln in b.source_lines for r in ln.regions], m)
        splits: list[dict[str, Any]] = []
        interleaved: list[list[str]] = []
        # (멤버, LLM 역할 또는 None, 분리 기록의 조각 항목). None = 칸 묶음을 넘은 묶음의 조각
        parts: list[tuple[list[TextBlock], str | None, dict[str, Any] | None]] = []
        for members, role in groups:
            by_group: dict[int, list[TextBlock]] = {}
            for x in members:
                by_group.setdefault(layout.block_group[ids[x].block_key], []).append(ids[x])
            if len(by_group) == 1:
                parts.append((next(iter(by_group.values())), role, None))
                continue
            split: dict[str, Any] = {"members": sorted(ids[x].block_key for x in members), "llm_role": role, "parts": []}
            for group in sorted(by_group):  # 기록 키 이름은 region 그대로 — 값은 칸 묶음 번호(페이로드 region과 같음)
                piece = {"region": group, "members": sorted(b.block_key for b in by_group[group]), "role": None}
                split["parts"].append(piece)
                parts.append((by_group[group], None, piece))
            splits.append(split)
        out = []
        for bs, role, piece in parts:
            bs = sorted(bs, key=lambda b: (layout.block_region[b.block_key], layout.block_rank[b.block_key]))
            leaf = {ln.line_key: layout.block_region[b.block_key] for b in bs for ln in b.source_lines}
            lines = sorted((ln for b in bs for ln in b.source_lines),
                           key=lambda ln: (leaf[ln.line_key], *layout.line_rank[ln.line_key]))
            if len(bs) > 1:
                spans = sorted(((layout.block_region[b.block_key], layout.line_rank[b.source_lines[0].line_key][0]),
                                (layout.block_region[b.block_key], layout.line_rank[b.source_lines[-1].line_key][0]))
                               for b in bs)
                if any(nxt[0] <= cur[1] for cur, nxt in zip(spans, spans[1:])):
                    interleaved.append([b.block_key for b in bs])
            if role is None:  # LLM 역할을 쓰지 않고 휴리스틱 역할 판정을 조각에 다시 적용한다
                text = "\n".join(ln.text for ln in lines)
                role = classify(text, len(lines), font_h([r for ln in lines for r in ln.regions]), stats, m)
                assert piece is not None
                piece["role"] = role
            out.append((bs, lines, role))
        out += [([b], list(b.source_lines), b.role) for b in heur.blocks if b.block_key not in sent]
        out.sort(key=lambda g: (layout.block_region[g[0][0].block_key], layout.block_rank[g[0][0].block_key]))
        notes = {"region_splits": splits, "interleaved": interleaved}
    blocks: list[TextBlock] = []
    mapping: dict[str, str] = {}
    n_line = 0
    for i, (members, src_lines, role) in enumerate(out, start=1):
        lines: list[Line] = []
        for ln in src_lines:
            n_line += 1
            lines.append(ln.model_copy(update={"line_key": line_key(n_line)}))
        for b in members:
            mapping[b.block_key] = block_key(i)
        regions = [r for ln in lines for r in ln.regions]
        blocks.append(
            TextBlock(
                block_key=block_key(i),
                section_key=section.section_key,
                block_order=i,
                source_ko="\n".join(ln.text for ln in lines),
                source_lines=lines,
                bbox=BBox.union([r.bbox for r in regions]),
                role=role,
                ocr_confidence=ocr_confidence_of(lines),
            )
        )
    return MergeResult(section_key=section.section_key, blocks=blocks), mapping, notes


def _emit(recorder: Recorder | None, rec: dict[str, Any], err: BaseException | None = None) -> None:
    """기록을 쓴다. 호출·검증이 실패한 중이면(err) 기록 저장 실패는 err의 note로만 붙이고 원래 오류를 살린다.
    성공한 중이면 기록 저장 실패를 그대로 올린다 — 기록 없는 결과를 남기지 않는다(7.2절)."""
    if recorder is None:
        return
    try:
        recorder(rec)
    except Exception as w:  # noqa: BLE001
        if err is None:
            raise
        err.add_note(f"merge_debug 기록 저장 실패: {w!r}")


def assist(
    section: Section, heur: MergeResult, prompt: str, cfg: dict[str, Any], llm: MergeAssistant,
    recorder: Recorder | None = None, layout: _Layout | None = None, post: dict[str, Any] | None = None,
) -> MergeResult:
    """llm_assist v1: 휴리스틱 결과에 LLM 추가 병합 · 역할 재판정을 적용한다. 실패는 VlmError로 전파(#37).
    layout · post는 후처리를 켰을 때만(_heuristic이 만든 값). 둘 다 없으면 기록 형식도 기존과 같다."""
    payload, ids = build_payload(section, heur, layout)
    ptext = payload_text(payload)
    started = time.monotonic()
    rec: dict[str, Any] = {
        "section_key": section.section_key,
        "source_image_id": section.source_image_id,
        "status": None,
        "model_config": llm_config(cfg),
        "prompt_path": str(cfg["merge"]["prompt_path"]),
        "prompt_sha256": sha256_text(prompt),
        "payload": payload,
        "payload_sha256": sha256_text(ptext),
        "ids": {bid: b.block_key for bid, b in ids.items()},
        "response_text": None,
        "error": None,
        "result": None,
        "usage": None,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": None,
    }
    if post is not None and (post.get("xycut") or post.get("fullwidth") or post.get("num_o")):
        rec["postprocess"] = dict(post)  # 후처리를 켰을 때만 — 계약 값이 아니라 개발 확인용

    def done(status: str) -> None:
        rec["status"] = status
        rec["duration_s"] = round(time.monotonic() - started, 3)

    if not ids:
        done("skipped")  # 보낼 블록 0개 — 호출하지 않는다
        rec["result"] = {"groups": [], "block_map": {b.block_key: b.block_key for b in heur.blocks}}
        _emit(recorder, rec)
        return heur
    try:
        reply: LlmReply = llm(prompt, ptext)
    except VlmError as e:
        done("call_failed")
        rec["error"] = str(e)
        _emit(recorder, rec, e)
        raise
    rec["response_text"] = reply.text
    rec["usage"] = reply.usage
    try:
        groups = parse_groups(reply.text, ids)
    except LlmResponseError as e:
        done("validation_failed")
        rec["error"] = e.reasons
        _emit(recorder, rec, e)
        raise
    result, mapping, notes = assemble(section, heur, ids, groups, layout, cfg["merge"])
    done("ok")
    rec["result"] = {
        "groups": [{"members": [ids[x].block_key for x in members], "role": role} for members, role in groups],
        "block_map": mapping,
        **notes,  # xycut이면 region_splits(칸 묶음을 넘은 묶음의 분리 · 역할 대체) · interleaved(가로줄 범위 교차 묶음)
    }
    _emit(recorder, rec)
    return result


def json_recorder(debug_dir: Path) -> Recorder:
    """`<debug_dir>/<section_key>.json`에 기록을 쓰는 recorder."""

    def write(rec: dict[str, Any]) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{rec['section_key']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")

    return write


def run(
    section: Section, ocr: OcrResult, cfg: dict[str, Any], *, use_llm: bool = True,
    llm: MergeAssistant | None = None, recorder: Recorder | None = None, post_recorder: Recorder | None = None,
) -> MergeResult:
    """③ 실행. use_llm=False면 heuristic_v2만(프롬프트 · API 키 불필요). llm을 주지 않으면 기본 Gemini 호출자(API 키 필요).

    post_recorder: 후처리 스위치(`merge.xycut` · `merge.fullwidth` · `merge.num_o`) 중 하나라도 켜져 있으면 LLM 사용 여부와 관계없이
    휴리스틱 직후 섹션마다 후처리 진단을 넘긴다(LLM 호출 전이라 LLM이 실패해도 남는다). 모두 꺼져 있으면 부르지 않는다."""
    validate_config(cfg)
    prompt = validate_llm_config(cfg, need_api_key=llm is None) if use_llm else ""
    heur, layout, post, diag = _heuristic(section, ocr, cfg)
    if diag is not None and post_recorder is not None:
        diag["use_llm"] = use_llm
        post_recorder(diag)
    if not use_llm:
        return heur
    return assist(section, heur, prompt, cfg, llm if llm is not None else default_assistant(cfg), recorder,
                  layout=layout, post=post)
