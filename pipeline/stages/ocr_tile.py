"""② OCR 임시 분할 — 높이가 `ocr.split_threshold_px`를 넘는 섹션의 타일 OCR [계약 3.2] [pipeline.md 7.9절].

`ocr.run()`이 부르는 보조 모듈이다(단계 함수 아님). 타일은 OCR 처리 단위이며 의미 섹션으로 저장하지 않는다.

1. 타일 계획 — 행 색 표준편차 ≤ `tile_blank_row_std`인 행을 균일 행(여백)으로 본다. 커서 s에서 명목 끝 e = s + `tile_px`.
   절단선 c는 창 [e − overlap, e)의 균일 행 중 e에 가장 가까운 행(없으면 e). 다음 타일 시작 s'는 창
   [c − 2·overlap, c − overlap]의 균일 행 중 c − overlap에 가장 가까운 행(없으면 c − overlap). 균일 행은 글자에서
   min(여백 높이의 절반, overlap // 2)만큼 떨어진 범위에서 고른다(`pick_blank_row`).
   여백에서 잘라도 겹침(≥ overlap)을 유지한다. H − c < overlap이면 현재 타일을 H까지 늘린다.
2. 좌표 복원 — 타일마다 엔진을 부르고 영역을 타일 범위로 자른 뒤 섹션 로컬 y로 옮긴다. 정리 전에 모든 검출을 복원한다.
3. 대응 — 이웃 타일의 겹침 띠에 걸친 검출 사이에만 간선을 둔다(세로 겹침 비율 ≥ `tile_match_min_v_overlap`이고
   가로 교집합 > 0). 텍스트는 대응에 쓰지 않는다. 연결 성분 = 한 영역 대 여러 영역의 대응 묶음.
4. 분류 — 두 타일 성분은 읽기 순서 텍스트(NFKC · 공백 제거)가 같고 가로 덮음 위치 IoU ≥ `tile_equiv_min_x_iou`일 때만
   동등(덮음은 x 구간 합집합, 글자 높이 이하 간격은 메움 — 길이가 아니라 위치를 비교). 동등하면 한쪽 타일의 검출 묶음 전체를 대표로 고른다: 내부 절단선 `tile_edge_touch_px` 안 접촉이 없는 쪽 →
   절단선에서 더 먼 쪽 → 위 타일. OCR 신뢰도는 쓰지 않는다. 동등하지 않으면 충돌, 한쪽 타일에만 있으면 보존.
5. 보완 OCR — 충돌 · 잘림 의심(대표나 단독 검출이 내부 절단선에 접촉)이 있는 경계마다 최대 1회, 해당 성분을 감싸는
   crop(위아래 `tile_supplement_margin_px` 밖, 여백 우선)으로 다시 OCR한다. 보완 검출과 위치로 대응한 성분 묶음(군집)을
   판정한다(`_judge_supplement`): 보완 판독이 절단선 비접촉 타일 판독을 확인하면(같음, 또는 숫자에 붙지 않은
   문장부호만 다름) 그 타일 묶음 전체를, 깨끗한 타일 판독이 없으면 보완 검출을 남긴다. 한 묶음이 나머지 판독을 위치와
   텍스트(포함) 모두에서 포함하면(덜 검출) 그 묶음을 경고와 함께 남긴다. 군집의 나머지는 버린다(단순 추가 없음, 대응
   없는 보완 검출은 버리고 기록). 그 밖 — 잘림 없는 판독이 같은 자리에서 서로 다르게 읽힌 경우를 포함한 명백한 충돌 —
   은 OCR_FAILED.
6. 정렬 — 최종 영역을 PaddleX `SortQuadBoxes`와 같은 규칙(첫 점 (y, x) 정렬 후 |Δy| < 10 인접 교환)으로 정렬하고
   region_key를 다시 매긴다.

임계값 · 후보값은 실측은 수행했으나 운영 기준값으로 확정하지 않은 잠정값이다(`pipeline/config/default.toml` `[ocr]`,
open-questions #31 · #32).
진단(diag)은 계약 값이 아니라 개발 확인용이다(CLI `ocr_debug/<section_key>.json`).
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from pipeline.errors import AnalyzeError
from pipeline.types import BBox, OcrRegion, Section, region_key

_ROW_CHUNK = 2048  # 행 프로파일 계산 단위(최대 1000×37,665px 섹션의 메모리 제한)
_SORT_ROW_TOL = 10  # PaddleX SortQuadBoxes의 같은 줄 판정(|Δy| < 10)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")  # 숫자 값 토큰: 50 · 1.5 · 1,000 (구분 기호까지 값의 일부로 본다)


# ---- 설정 -------------------------------------------------------------------------------
@dataclass(frozen=True)
class TileParams:
    threshold_px: int
    tile_px: int
    overlap_px: int
    blank_row_std: float
    min_v_overlap: float
    min_x_iou: float
    edge_touch_px: int
    box_jitter_px: int
    supplement_margin_px: int

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def params_of(cfg: dict[str, Any]) -> TileParams:
    """cfg["ocr"]의 분할 키를 읽고 검증한다. 어기면 ValueError(설정 오류)."""
    oc = cfg["ocr"]
    p = TileParams(
        threshold_px=oc["split_threshold_px"], tile_px=oc["tile_px"], overlap_px=oc["tile_overlap_px"],
        blank_row_std=oc["tile_blank_row_std"], min_v_overlap=oc["tile_match_min_v_overlap"],
        min_x_iou=oc["tile_equiv_min_x_iou"], edge_touch_px=oc["tile_edge_touch_px"],
        box_jitter_px=oc["tile_box_jitter_px"], supplement_margin_px=oc["tile_supplement_margin_px"],
    )
    ints = ("threshold_px", "tile_px", "overlap_px", "edge_touch_px", "box_jitter_px", "supplement_margin_px")
    for k in ints:
        v = getattr(p, k)
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"ocr 분할 설정 {k}={v!r}는 정수여야 한다")
    for k in ("blank_row_std", "min_v_overlap", "min_x_iou"):
        v = getattr(p, k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"ocr 분할 설정 {k}={v!r}는 수여야 한다")
    if p.overlap_px <= 0:
        raise ValueError(f"ocr.tile_overlap_px={p.overlap_px} — 0보다 커야 한다(겹침 없이는 경계 중복 정리를 못 한다)")
    if p.tile_px <= 3 * p.overlap_px:
        # 다음 타일 시작은 c − 2·overlap까지 올라가므로 커서 전진량 ≥ tile_px − 3·overlap > 0이어야 한다
        raise ValueError(f"ocr.tile_px={p.tile_px}는 3 × tile_overlap_px={3 * p.overlap_px}보다 커야 한다(커서 정체 방지)")
    if p.tile_px + p.overlap_px > p.threshold_px:
        raise ValueError(f"ocr.tile_px + tile_overlap_px = {p.tile_px + p.overlap_px} > split_threshold_px={p.threshold_px} "
                         "— 최대 타일 높이가 분할 기준을 넘는다")
    if p.blank_row_std < 0 or p.edge_touch_px < 0 or p.box_jitter_px < 0 or p.supplement_margin_px < 0:
        raise ValueError("ocr.tile_blank_row_std · tile_edge_touch_px · tile_box_jitter_px · tile_supplement_margin_px는 "
                         "0 이상이어야 한다")
    for k in ("min_v_overlap", "min_x_iou"):
        if not 0 < getattr(p, k) <= 1:
            raise ValueError(f"ocr 분할 설정 {k}={getattr(p, k)}는 (0, 1] 범위여야 한다")
    return p


# ---- 1. 타일 계획 ---------------------------------------------------------------------------
def row_std(image: np.ndarray) -> np.ndarray:
    """행별 색 표준편차 (h,) — 채널별 행 표준편차의 평균(① `section_split.row_profile`과 같은 정의)."""
    h = image.shape[0]
    out = np.empty(h, dtype=np.float32)
    for y0 in range(0, h, _ROW_CHUNK):
        y1 = min(h, y0 + _ROW_CHUNK)
        out[y0:y1] = image[y0:y1].astype(np.float32).std(axis=1).mean(axis=1)
    return out


def blank_runs(std: np.ndarray, std_max: float) -> list[tuple[int, int]]:
    """균일 행의 연속 구간 [start, end) 목록."""
    blank = np.concatenate(([False], std <= std_max, [False]))
    d = np.diff(blank.astype(np.int8))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def pick_blank_row(runs: list[tuple[int, int]], lo: int, hi: int, target: int, clear_max: int) -> int | None:
    """창 [lo, hi) 안에서 target에 가장 가까운 균일 행. 없으면 None.

    각 균일 행 구간 [a, b)에서 글자와의 거리 g = min((b − a − 1) // 2, clear_max)를 두고 [a + g, b − 1 − g] 안에서
    target에 가장 가까운 행을 고른다(짧은 여백은 중앙, 긴 여백은 글자에서 clear_max 떨어진 범위). 그 행이 창 밖이면
    창 안쪽 끝으로 옮긴다. 거리가 같으면 더 긴 구간, 그래도 같으면 위쪽 구간."""
    best: tuple[int, int, int, int] | None = None  # (거리, −길이, 시작, 행)
    for a, b in runs:
        a2, b2 = max(a, lo), min(b, hi)
        if a2 >= b2:
            continue
        g = min((b - a - 1) // 2, clear_max)
        row = min(max(target, a + g), b - 1 - g)
        row = min(max(row, a2), b2 - 1)
        cand = (abs(row - target), -(b - a), a, row)
        if best is None or cand[:3] < best[:3]:
            best = cand
    return None if best is None else best[3]


@dataclass(frozen=True)
class Tile:
    index: int
    y0: int
    y1: int
    cut: str  # 아래 끝: blank(여백 절단) · fixed(여백 없음, 명목 끝) · end(섹션 끝) · extended_end(짧은 꼬리를 흡수)
    start: str  # 위 끝: origin(섹션 시작) · blank · fixed

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "y0": self.y0, "y1": self.y1, "height": self.y1 - self.y0, "cut": self.cut,
                "start": self.start}


def plan_tiles(std: np.ndarray, p: TileParams) -> list[Tile]:
    height = len(std)
    runs = blank_runs(std, p.blank_row_std)
    ov = p.overlap_px
    tiles: list[Tile] = []
    s, start = 0, "origin"
    while True:
        e = s + p.tile_px
        if e >= height:
            tiles.append(Tile(len(tiles), s, height, "end", start))
            return tiles
        c = pick_blank_row(runs, e - ov, e, e, ov // 2)
        cut = "blank" if c is not None else "fixed"
        c = e if c is None else c
        if height - c < ov:
            tiles.append(Tile(len(tiles), s, height, "extended_end", start))
            return tiles
        tiles.append(Tile(len(tiles), s, c, cut, start))
        s2 = pick_blank_row(runs, c - 2 * ov, c - ov + 1, c - ov, ov // 2)
        start = "blank" if s2 is not None else "fixed"
        s = c - ov if s2 is None else s2


# ---- 2. 검출 · 좌표 복원 ---------------------------------------------------------------------
@dataclass
class Det:
    id: int
    origin: str  # tile | supplement
    crop: int  # 타일 번호 또는 보완한 경계 번호
    order: int  # 그 crop의 엔진 출력 순서
    text: str
    score: float
    poly: list[tuple[int, int]]  # 섹션 로컬
    bbox: BBox
    touch_top: bool  # crop의 내부(섹션 경계가 아닌) 위 끝에 접촉
    touch_bottom: bool

    @property
    def touching(self) -> bool:
        return self.touch_top or self.touch_bottom

    def as_dict(self) -> dict[str, Any]:
        b = self.bbox
        return {"id": self.id, "origin": self.origin, "crop": self.crop, "order": self.order, "text": self.text,
                "score": self.score, "bbox": [b.x, b.y, b.w, b.h], "touch_top": self.touch_top,
                "touch_bottom": self.touch_bottom}


def restore(regions: list[OcrRegion], y0: int, y1: int, height: int, p: TileParams, origin: str, crop: int,
            start_id: int) -> list[Det]:
    """crop 로컬(이미 crop 범위로 자른) 영역 → 섹션 로컬 Det. 접촉은 섹션 경계가 아닌 crop 끝만 본다."""
    ch = y1 - y0
    out = []
    for i, r in enumerate(regions):
        poly = [(x, y + y0) for x, y in r.poly]
        out.append(Det(
            id=start_id + i, origin=origin, crop=crop, order=i, text=r.text, score=r.score, poly=poly,
            bbox=BBox.from_poly(poly),
            touch_top=y0 > 0 and r.bbox.y <= p.edge_touch_px,
            touch_bottom=y1 < height and r.bbox.y2 >= ch - p.edge_touch_px,
        ))
    return out


# ---- 3·4. 대응 · 분류 ----------------------------------------------------------------------
def v_overlap(a: BBox, b: BBox) -> float:
    inter = min(a.y2, b.y2) - max(a.y, b.y)
    return max(0, inter) / max(1, min(a.h, b.h))


def linked(a: BBox, b: BBox, p: TileParams) -> bool:
    return min(a.x2, b.x2) - max(a.x, b.x) > 0 and v_overlap(a, b) >= p.min_v_overlap


def normalize_text(text: str) -> str:
    """텍스트 비교용 — NFKC 후 공백 문자 제거. 대소문자 · 문장부호는 그대로 둔다(다르면 다른 판독)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if not ch.isspace())


def reading_rows(dets: list[Det], p: TileParams) -> list[list[Det]]:
    """읽기 순서의 줄 — 세로 겹침(≥ tile_match_min_v_overlap)으로 묶고 줄 안은 x 순."""
    rows: list[list[Det]] = []
    for d in sorted(dets, key=lambda d: (d.bbox.y + d.bbox.y2, d.bbox.x)):
        if rows:
            span = BBox.union([m.bbox for m in rows[-1]])
            if v_overlap(span, d.bbox) >= p.min_v_overlap:
                rows[-1].append(d)
                continue
        rows.append([d])
    return [sorted(row, key=lambda d: d.bbox.x) for row in rows]


def reading_text(dets: list[Det], p: TileParams) -> str:
    """읽기 순서 텍스트를 이어 정규화한다."""
    return "".join(normalize_text(d.text) for row in reading_rows(dets, p) for d in row)


def punctuation_only_diff(a: str, b: str) -> bool:
    """정규화한 두 판독의 차이가 문장부호(Unicode P*)뿐이고 그 문장부호가 숫자에 붙어 있지 않으면 True.

    보완 OCR이 타일 판독을 확인했는지 볼 때만 쓴다(예: "큰제목" vs "큰·제목"). "1.5"와 "15"처럼 숫자 옆 문장부호 차이는
    값이 달라지므로 확인으로 보지 않는다. 같은 문자열은 False(차이 없음은 따로 본다)."""
    if a == b:
        return False
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        chars = a[i1:i2] + b[j1:j2]
        if not chars or not all(unicodedata.category(ch).startswith("P") for ch in chars):
            return False
        near = [a[k] for k in (i1 - 1, i2) if 0 <= k < len(a)] + [b[k] for k in (j1 - 1, j2) if 0 <= k < len(b)]
        if any(ch.isdigit() for ch in near):
            return False
    return True


def _intervals(dets: list[Det], gap: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted((d.bbox.x, d.bbox.x2) for d in dets if d.bbox.w > 0):
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _measure(iv: list[tuple[int, int]]) -> int:
    return sum(y - x for x, y in iv)


def _intersect(ia: list[tuple[int, int]], ib: list[tuple[int, int]]) -> int:
    inter, i, j = 0, 0, 0
    while i < len(ia) and j < len(ib):
        inter += max(0, min(ia[i][1], ib[j][1]) - max(ia[i][0], ib[j][0]))
        if ia[i][1] < ib[j][1]:
            i += 1
        else:
            j += 1
    return inter


def _dilate(iv: list[tuple[int, int]], r: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in iv:
        a, b = a - r, b + r
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _covers(a: list[Det], b: list[Det]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    gap = max((d.bbox.h for d in a + b), default=0)  # 양쪽 검출 중 가장 큰 높이 이하의 x 간격은 메운다
    return _intervals(a, gap), _intervals(b, gap)


def x_cover_iou(a: list[Det], b: list[Det], jitter: int = 0) -> float:
    """가로 덮음 위치의 IoU — 양쪽 x 구간 합집합끼리의 교집합 길이 / 합집합 길이. 길이만 같은 경우를 걸러낸다.

    단어 사이 간격으로 한 줄이 여러 영역으로 나뉘는 경우(한 영역 대 여러 영역)를 같은 덮음으로 보도록, 양쪽 검출 중
    가장 큰 높이 이하의 간격은 메운다. 그보다 넓은 빈칸은 덮지 않은 위치로 남는다. jitter만큼 양쪽 구간을 넓혀
    작은 박스의 몇 px 흔들림이 비율을 크게 바꾸지 않게 한다."""
    ia, ib = _covers(a, b)
    ia, ib = _dilate(ia, jitter), _dilate(ib, jitter)
    inter = _intersect(ia, ib)
    union = _measure(ia) + _measure(ib) - inter
    return 1.0 if union == 0 else inter / union


def x_covered_by(a: list[Det], b: list[Det], jitter: int = 0) -> float:
    """a의 가로 덮음 중 (jitter만큼 넓힌) b가 덮는 비율. a가 비면 1."""
    ia, ib = _covers(a, b)
    la = _measure(ia)
    return 1.0 if la == 0 else _intersect(ia, _dilate(ib, jitter)) / la


class _UF:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb, key=str)] = min(ra, rb, key=str)


@dataclass
class Component:
    id: int
    members: list[Det]
    kind: str = "single"  # single · single_touching · equivalent · equivalent_touching · conflict
    boundary: int | None = None  # 관련 경계(위 타일 번호). single은 None
    keep: list[Det] = field(default_factory=list)  # 현재 남기는 검출
    rule: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    replaced_by: int | None = None  # 보완한 경계 번호

    @property
    def suspect(self) -> bool:
        return self.kind in ("single_touching", "equivalent_touching", "conflict")

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "boundary": self.boundary, "rule": self.rule,
                "members": [d.id for d in self.members], "keep": [d.id for d in self.keep],
                "replaced_by_supplement": self.replaced_by, **self.detail}


def _touched_boundary(d: Det) -> int:
    return d.crop - 1 if d.touch_top else d.crop


def build_components(dets: list[Det], tiles: list[Tile], p: TileParams) -> list[Component]:
    uf = _UF()
    by_tile: dict[int, list[Det]] = {}
    for d in dets:
        uf.find(d.id)
        by_tile.setdefault(d.crop, []).append(d)
    for b in range(len(tiles) - 1):
        lo, hi = tiles[b + 1].y0, tiles[b].y1  # 겹침 띠 [lo, hi)
        upper = [d for d in by_tile.get(b, []) if d.bbox.y2 > lo]
        lower = [d for d in by_tile.get(b + 1, []) if d.bbox.y < hi]
        for a in upper:
            for c in lower:
                if linked(a.bbox, c.bbox, p):
                    uf.union(a.id, c.id)
    groups: dict[int, list[Det]] = {}
    for d in dets:
        groups.setdefault(uf.find(d.id), []).append(d)
    comps = [Component(i, sorted(m, key=lambda d: d.id)) for i, m in enumerate(sorted(groups.values(), key=lambda m: min(d.id for d in m)))]
    for c in comps:
        classify(c, tiles, p)
    return comps


def classify(c: Component, tiles: list[Tile], p: TileParams) -> None:
    crops = sorted({d.crop for d in c.members})
    if len(crops) == 1:
        c.keep = list(c.members)
        touching = [d for d in c.members if d.touching]
        if touching:
            c.kind, c.rule = "single_touching", "one_side_only_touching_cut"
            c.boundary = min(_touched_boundary(d) for d in touching)
        else:
            c.kind, c.rule = "single", "one_side_only"
        return
    c.boundary = crops[0]
    if len(crops) > 2:
        c.kind, c.rule, c.keep = "conflict", "spans_more_than_two_tiles", list(c.members)
        return
    up = [d for d in c.members if d.crop == crops[0]]
    lo = [d for d in c.members if d.crop == crops[1]]
    t_up, t_lo = reading_text(up, p), reading_text(lo, p)
    iou = x_cover_iou(up, lo, p.box_jitter_px)
    c.detail = {"upper_tile": crops[0], "lower_tile": crops[1], "upper_text": t_up, "lower_text": t_lo,
                "x_cover_iou": round(iou, 4), "upper_count": len(up), "lower_count": len(lo)}
    if t_up != t_lo or iou < p.min_x_iou:
        c.kind = "conflict"
        c.rule = "text_differs" if t_up != t_lo else "x_cover_differs"
        c.keep = list(c.members)  # 해결 전에는 아무것도 버리지 않는다(실패 시 기록용)
        return
    touch_up, touch_lo = any(d.touching for d in up), any(d.touching for d in lo)
    if touch_up != touch_lo:
        side, c.rule = ("upper", "not_touching_cut") if not touch_up else ("lower", "not_touching_cut")
    else:
        d_up = tiles[crops[0]].y1 - max(d.bbox.y2 for d in up)  # 위 타일 절단선까지의 거리
        d_lo = min(d.bbox.y for d in lo) - tiles[crops[1]].y0  # 아래 타일 시작선까지의 거리
        side = "upper" if d_up >= d_lo else "lower"
        c.rule = "farther_from_cut" if d_up != d_lo else "tie_upper"
        c.detail.update({"upper_cut_distance": int(d_up), "lower_cut_distance": int(d_lo)})
    c.keep = up if side == "upper" else lo
    c.detail["chosen"] = side
    c.kind = "equivalent_touching" if touch_up and touch_lo else "equivalent"


# ---- 5. 보완 OCR -------------------------------------------------------------------------
Recognize = Callable[[int, int], list[OcrRegion]]  # (y0, y1) → crop 로컬 영역(엔진 호출 + 형식 검증)


def supplement_crop(comps: list[Component], runs: list[tuple[int, int]], height: int, p: TileParams) -> tuple[int, int]:
    top = min(d.bbox.y for c in comps for d in c.members)
    bottom = max(d.bbox.y2 for c in comps for d in c.members)
    m = p.supplement_margin_px
    if top - m <= 0:
        ra = 0
    else:
        r = pick_blank_row(runs, max(0, top - 2 * m), top - m + 1, top - m, m // 2)
        ra = top - m if r is None else r
    if bottom + m >= height:
        rb = height
    else:
        r = pick_blank_row(runs, bottom + m, min(height, bottom + 2 * m + 1), bottom + m, m // 2)
        rb = bottom + m if r is None else r
    return ra, rb


def resolve_boundary(b: int, comps: list[Component], all_comps: list[Component], dets_by_id: dict[int, Det],
                     comp_of: dict[int, Component], tiles: list[Tile], runs: list[tuple[int, int]], height: int,
                     p: TileParams, recognize: Recognize, next_id: int) -> tuple[dict[str, Any], list[Det]]:
    """경계 b의 의심 성분을 보완 OCR 1회로 해결한다. 반환: (진단, 새 보완 검출)."""
    ra, rb = supplement_crop(comps, runs, height, p)
    rec: dict[str, Any] = {"boundary": b, "crop": [ra, rb], "components": [c.id for c in comps], "clusters": [],
                           "discarded": []}
    if rb - ra > p.threshold_px:
        rec["status"] = "crop_too_tall"
        for c in comps:
            _unresolved(c, "supplement_crop_too_tall")
        return rec, []
    regions = recognize(ra, rb)
    rec["called"] = True
    sdets = restore(regions, ra, rb, height, p, "supplement", b, next_id)
    rec["detections"] = [d.as_dict() for d in sdets]  # 채택 여부와 관계없이 보완 검출 전체를 남긴다
    # 보완 검출 ↔ 기존 타일 검출(위치 대응) → 성분 단위 군집
    uf = _UF()
    for c in comps:
        uf.find(("c", c.id))
    links: dict[int, set[int]] = {}
    for s in sdets:
        uf.find(("s", s.id))
        hits = {comp_of[t.id].id for t in dets_by_id.values() if t.origin == "tile" and linked(s.bbox, t.bbox, p)}
        links[s.id] = hits
        for cid in hits:
            uf.union(("s", s.id), ("c", cid))
    by_id = {c.id: c for c in all_comps}
    clusters: dict[Any, dict[str, list]] = {}
    for s in sdets:
        clusters.setdefault(uf.find(("s", s.id)), {"s": [], "c": []})["s"].append(s)
    for cid in {cid for hs in links.values() for cid in hs} | {c.id for c in comps}:
        clusters.setdefault(uf.find(("c", cid)), {"s": [], "c": []})["c"].append(by_id[cid])
    target_ids = {c.id for c in comps}
    added: list[Det] = []
    for key in sorted(clusters, key=str):
        cl = clusters[key]
        cl_comps = sorted(cl["c"], key=lambda c: c.id)
        ss = sorted(cl["s"], key=lambda d: d.id)
        if not any(c.id in target_ids for c in cl_comps):
            for s in ss:
                rec["discarded"].append({"det": s.id, "reason": "unmatched" if not links[s.id] else "matches_settled_only",
                                         "text": s.text})
            continue
        crec: dict[str, Any] = {"components": [c.id for c in cl_comps], "supplement": [s.id for s in ss]}
        rec["clusters"].append(crec)
        foreign = [c.id for c in cl_comps if c.id not in target_ids and (c.suspect or c.replaced_by is not None)]
        if foreign:
            crec["status"] = "unresolved"
            crec["reason"] = f"overlaps_other_suspect_components {foreign}"
        else:
            crec.update(_judge_supplement(cl_comps, ss, tiles, p))
        if crec["status"] == "resolved":
            for c in cl_comps:
                c.keep, c.replaced_by = [], b
            if crec["use"] == "supplement":
                kept = list(ss)
                added.extend(ss)
            else:  # 보완 OCR이 확인한 타일 한쪽의 검출 묶음 전체
                kept = [d for c in cl_comps for d in c.members if d.crop == crec["use_tile"]]
            cl_comps[0].keep = kept  # 최종 목록에서는 성분 구분이 없다 — 군집의 첫 성분에 둔다
        else:
            for c in cl_comps:
                if c.id in target_ids:
                    _unresolved(c, crec["reason"])
    # 보완 검출과 대응이 하나도 없는 의심 성분
    seen = {cid for cr in rec["clusters"] for cid in cr["components"]}
    for c in comps:
        if c.id not in seen:  # 위 루프에서 항상 군집에 들어가므로 방어용
            _unresolved(c, "no_supplement_detection")
    statuses = {cr["status"] for cr in rec["clusters"]}
    rec["status"] = "resolved" if statuses == {"resolved"} else ("unresolved" if "resolved" not in statuses else "partial")
    return rec, added


def numbers_within(part: str, whole: str) -> bool:
    """part의 숫자 값 토큰이 whole에 같은 값으로 모두 있는가(개수 포함). '5'는 '50' 안에 문자열로 있어도 다른 값이다."""
    return not (Counter(_NUMBER.findall(part)) - Counter(_NUMBER.findall(whole)))


def text_consistent(part: str, whole: str) -> bool:
    """part 판독이 whole 판독과 모순되지 않는가 — (같음 · 숫자에 붙지 않은 문장부호만 다름 · whole 안에 그대로 포함(덜 검출))
    이면서 숫자 값이 바뀌지 않음. 'SPF5' ⊂ 'SPF50'은 문자열로 포함되지만 숫자 값이 달라 모순이다."""
    return (part == whole or punctuation_only_diff(part, whole) or part in whole) and numbers_within(part, whole)


def _judge_supplement(cl_comps: list[Component], ss: list[Det], tiles: list[Tile], p: TileParams) -> dict[str, Any]:
    """보완 OCR 뒤 군집을 판정한다. 남길 쪽은 언제나 한 crop의 검출 묶음 전체다(양쪽 박스를 섞지 않는다).

    '깨끗한' 판독 = 내부 절단선 · 보완 crop 끝에 접촉하지 않은 묶음. 보완 판독은 비어 있지 않고 깨끗할 때만 쓴다.
    1. 보완 판독이 깨끗한 타일 판독과 같거나 숫자에 붙지 않은 문장부호만 다르면(확인) 그 타일 묶음(같음 우선, 여럿이면
       타일 내부 끝에서 더 먼 쪽 → 위 타일). 보완 검출은 확인에만 쓰고 버린다.
    2. 깨끗한 타일 판독이 없고 보완이 깨끗하면 보완 검출로 대체한다.
    덜 검출(경고 `partial_detection`) — 한 묶음이 나머지 판독을 위치(가로 덮음)와 텍스트(포함) 모두에서 포함할 때만:
    3. 다른 깨끗한 판독(깨끗한 보완 포함) 모두를 포함하는 깨끗한 타일 묶음 중 하나.
    4. 깨끗한 타일 묶음 모두를 포함하는 깨끗한 보완 묶음.
    5. 그 밖 — 미해결(명백한 충돌). 잘림 없는 판독끼리 같은 자리에서 글자가 서로 다르면(판독 흔들림, 숫자 포함) 여기서
       실패한다 — 어느 판독이 맞는지 OCR만으로 정하지 않는다(사용자 결정 2026-10-13).
    숫자 값 확인 — 1~4로 고른 뒤에도, 버려지는 잘림 없는 판독(깨끗한 타일 · 깨끗한 보완)의 숫자 값 토큰이 고른 판독에 같은
    값으로 모두 있지 않으면 미해결(`numbers_conflict`)이다. 2:1 확인(다수결)으로도 서로 다른 숫자 값을 덮지 않는다.
    위치 포함 비교에는 tile_box_jitter_px 허용치를 둔다."""
    members = [d for c in cl_comps for d in c.members]
    clean: dict[int, list[Det]] = {}
    for t in sorted({d.crop for d in members}):
        side = [d for d in members if d.crop == t]
        if not any(d.touching for d in side):
            clean[t] = side
    s_ok = bool(ss) and not any(d.touching for d in ss)
    readings = {t: reading_text(side, p) for t, side in clean.items()}
    s_text = reading_text(ss, p) if ss else None
    out: dict[str, Any] = {"supplement_state": "clean" if s_ok else ("touching" if ss else "none"),
                           "supplement_text": s_text, "clean_tile_readings": {str(t): r for t, r in readings.items()}}
    jit, th = p.box_jitter_px, p.min_x_iou

    def clearance(t: int) -> int:  # 그 타일의 내부 끝(섹션 경계 제외)까지 가장 가까운 거리
        tile = tiles[t]
        gaps = [d.bbox.y - tile.y0 for d in clean[t] if tile.y0 > 0] + \
               [tile.y1 - d.bbox.y2 for d in clean[t] if tile.y1 < tiles[-1].y1]
        return min(gaps, default=1 << 30)

    def numbers_ok(chosen: str, dropped: list[str]) -> dict[str, Any]:
        bad = [r for r in dropped if not numbers_within(r, chosen)]
        if bad:
            for k in ("use", "use_tile", "warning"):  # 채택하지 않으므로 성공 표시를 남기지 않는다
                out.pop(k, None)
            out.update({"status": "unresolved", "reason": "numbers_conflict", "candidate_text": chosen,
                        "numbers_conflict_with": bad})
        return out

    def use_tile(cands: list[int], rule: str, warning: str | None = None, **kw: Any) -> dict[str, Any]:
        t = max(cands, key=lambda t: (clearance(t), -t))
        out.update({"status": "resolved", "use": "tile", "use_tile": t, "rule": rule, **kw})
        if warning:
            out["warning"] = warning
        return numbers_ok(readings[t], [r for u, r in readings.items() if u != t] + ([s_text] if s_ok else []))

    def use_supplement(rule: str, warning: str | None = None) -> dict[str, Any]:
        out.update({"status": "resolved", "use": "supplement", "rule": rule})
        if warning:
            out["warning"] = warning
        return numbers_ok(s_text, list(readings.values()))

    def contains(dets: list[Det], text: str, other: list[Det], other_text: str) -> bool:
        return x_covered_by(other, dets, jit) >= th and text_consistent(other_text, text)

    if s_ok:
        exact = [t for t, r in readings.items() if r == s_text]
        minor = [t for t, r in readings.items() if t not in exact and punctuation_only_diff(r, s_text)]
        if exact or minor:
            return use_tile(exact or minor,
                            "supplement_confirms_tile" if exact else "supplement_confirms_tile_punctuation_only",
                            confirmed_tiles=exact or minor)
        if not clean:
            return use_supplement("supplement_replaces_truncated")
    if clean:
        cands = [t for t in clean
                 if all(contains(clean[t], readings[t], clean[u], readings[u]) for u in clean if u != t)
                 and (not s_ok or contains(clean[t], readings[t], ss, s_text))]
        if cands:
            return use_tile(cands, "tile_contains_all_readings", "partial_detection")
        if s_ok and all(contains(ss, s_text, side, readings[t]) for t, side in clean.items()):
            return use_supplement("supplement_contains_all_readings", "partial_detection")
    out.update({"status": "unresolved",
                "reason": "no_untruncated_reading" if not clean and not s_ok else "readings_conflict"})
    return out


def _unresolved(c: Component, reason: str) -> None:
    c.detail["unresolved"] = reason


# ---- 6. 정렬 · 실행 -----------------------------------------------------------------------
def sort_like_paddle(dets: list[Det]) -> list[Det]:
    """PaddleX SortQuadBoxes와 같은 규칙: 첫 점 (y, x)로 정렬한 뒤 |Δy| < 10이고 x가 역순인 이웃을 교환한다."""
    out = sorted(dets, key=lambda d: (d.poly[0][1], d.poly[0][0], d.id))
    for i in range(len(out) - 1):
        for j in range(i, -1, -1):
            a, b = out[j], out[j + 1]
            if abs(b.poly[0][1] - a.poly[0][1]) < _SORT_ROW_TOL and b.poly[0][0] < a.poly[0][0]:
                out[j], out[j + 1] = b, a
            else:
                break
    return out


def run_tiled(section: Section, image: np.ndarray, p: TileParams, recognize: Recognize,
              diag: dict[str, Any] | None = None) -> list[OcrRegion]:
    """타일 OCR → 좌표 복원 → 중복 정리 → 보완 OCR → 정렬. 충돌이 남으면 OCR_FAILED.

    recognize(y0, y1)는 image[y0:y1]을 OCR해 crop 로컬로 자른 영역을 돌려준다(엔진 오류 · 형식 오류는 그쪽에서 던진다)."""
    d = diag if diag is not None else {}
    height = image.shape[0]
    std = row_std(image)
    runs = blank_runs(std, p.blank_row_std)
    tiles = plan_tiles(std, p)
    d.update({"tiled": True, "section_key": section.section_key, "height": height, "width": image.shape[1],
              "params": p.as_dict(), "tiles": [t.as_dict() for t in tiles],
              "engine_calls": {"tiles": 0, "supplements": 0, "supplement_limit": len(tiles) - 1}})
    dets: list[Det] = []
    for t in tiles:
        regions = recognize(t.y0, t.y1)
        d["engine_calls"]["tiles"] += 1
        dets.extend(restore(regions, t.y0, t.y1, height, p, "tile", t.index, len(dets)))
    comps = build_components(dets, tiles, p)
    comp_of = {m.id: c for c in comps for m in c.members}
    dets_by_id = {x.id: x for x in dets}
    supplements: list[dict[str, Any]] = []
    all_dets = list(dets)
    d["detections"] = [x.as_dict() for x in all_dets]
    for b in range(len(tiles) - 1):
        targets = [c for c in comps if c.suspect and c.boundary == b]
        if not targets:
            continue
        rec, added = resolve_boundary(b, targets, comps, dets_by_id, comp_of, tiles, runs, height, p, recognize,
                                      len(all_dets))
        if rec.get("called"):
            d["engine_calls"]["supplements"] += 1
        all_dets.extend(added)
        supplements.append(rec)
    d["detections"] = [x.as_dict() for x in all_dets]
    d["components"] = [c.as_dict() for c in comps if len(c.members) > 1 or c.kind != "single"]
    d["supplements"] = supplements
    failed = [c for c in comps if c.kind == "conflict" and "unresolved" in c.detail]
    d["warnings"] = [{"component": c.id, "kind": c.kind, "reason": c.detail["unresolved"]}
                     for c in comps if c.kind != "conflict" and "unresolved" in c.detail]
    d["warnings"] += [{"boundary": s["boundary"], "components": cl["components"], "kind": cl["warning"],
                       "supplement_text": cl["supplement_text"], "clean_tile_readings": cl["clean_tile_readings"],
                       "use": cl["use"], "use_tile": cl.get("use_tile")}  # 보완 검출을 쓴 군집은 use_tile이 없다
                      for s in supplements for cl in s["clusters"] if cl.get("warning")]
    kept = [x for c in comps for x in c.keep]
    dropped = sorted({x.id for x in dets} - {x.id for x in kept})
    d["dropped"] = dropped
    if failed:
        d["failed"] = [{"component": c.id, "boundary": c.boundary, "rule": c.rule, "reason": c.detail["unresolved"],
                        "y": [min(m.bbox.y for m in c.members), max(m.bbox.y2 for m in c.members)]} for c in failed]
        summary = ", ".join(f"경계 {f['boundary']} y{f['y'][0]}~{f['y'][1]} {f['reason']}" for f in d["failed"])
        raise AnalyzeError("OCR_FAILED", f"OCR 타일 경계 충돌 미해결 section={section.section_key} — {summary}",
                           section.source_image_id)
    ordered = sort_like_paddle(kept)
    d["regions"] = [{"region_key": region_key(i), "det": x.id} for i, x in enumerate(ordered, 1)]
    return [OcrRegion(region_key=region_key(i), text=x.text, score=x.score, poly=x.poly, bbox=x.bbox)
            for i, x in enumerate(ordered, 1)]
