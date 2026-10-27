"""② 호출별 명백한 중복 제거(#40)와 겹친 검출 재인식(#32)의 보조 함수 [pipeline.md 7.9절].

`ocr.run()`(짧은 섹션)과 `ocr_tile.py`(타일 · 보완 OCR · 재인식 연결)가 부른다. 단계 함수가 아니다.

- 중복 제거 — 엔진 호출 하나의 검출 목록 안에서만 한다. 텍스트가 문자 그대로 같고 실제 polygon IoU ≥ `dedup_min_poly_iou`이며
  윗변 방향 차 ≤ `dedup_max_angle_diff_deg`일 때만 뒤 검출을 지운다(bbox 겹침만으로 판정하지 않는다). 대표는 엔진 순서상 앞선
  검출이고 값을 바꾸지 않는다. 각 제거 대상은 대표와 직접 비교한다. 대표 후보가 여럿이거나, 텍스트가 다른 검출이 같은 자리에
  얽혀 있거나, polygon이 볼록이 아니면 지우지 않는다. 빈 문자열 · 신뢰도는 판정에 쓰지 않는다.
- geometry — 볼록 다각형 교집합(Sutherland–Hodgman)과 최소 회전 사각형(회전 캘리퍼스). 볼록이 아니면 None(판정하지 않음).
- crop — 원본 섹션 이미지에서 회전 사각형 주변 ROI만 잘라 PaddleX `CropByPolys.get_rotate_crop_image`(paddlex 3.7.2)와 같은
  방식(원근 변환 · INTER_CUBIC · BORDER_REPLICATE · 세로로 긴 crop은 90° 회전)으로 방향을 보정한다. 긴 섹션 전체를 변환하지
  않고 고정 픽셀을 잘라내지 않는다. cv2는 이 함수에서만 import한다(OCR 의존성, requirements-ocr.txt).
- 판독 변화 설명 — 재인식 판독이 원래 묶음과 다를 때, 실제 polygon이 겹친 이웃 경계 한쪽 끝의 숫자 몇 개를 지운 것만으로
  설명되는지 본다. 문자별 좌표는 만들지 않고 묶음 안 검출 원문의 경계만 위치 근거로 쓴다.

임계값은 `pipeline/config/default.toml` `[ocr]`의 잠정 구현값이다(실측 전, open-questions #32 · #40).
"""
from __future__ import annotations

import difflib
import itertools
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import numpy as np

from pipeline.types import OcrRegion

NUMBER = re.compile(r"\d+(?:[.,]\d+)*")  # 숫자 값 토큰: 50 · 1.5 · 1,000 (구분 기호까지 값의 일부로 본다)
_EPS = 1e-9
_ROTATE_RATIO = 1.5  # PaddleX get_rotate_crop_image: 높이 / 너비 ≥ 1.5면 90° 회전
_MAX_COMBOS = 4096  # 판독 변화 설명에서 볼 삭제 조합 수 상한(넘으면 설명하지 않음 — 설정값을 크게 바꿨을 때의 계산량 방어)


# ---- 설정 -------------------------------------------------------------------------------
@dataclass(frozen=True)
class OverlapParams:
    dedup_min_poly_iou: float
    dedup_max_angle_diff_deg: float
    rec_max_calls: int
    rec_max_group: int
    rec_max_x_overlap: float
    rec_max_angle_deg: float
    rec_max_foreign: float
    rec_max_fragment_digits: int

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


_KEYS = {  # 필드 → config 키
    "dedup_min_poly_iou": "dedup_min_poly_iou", "dedup_max_angle_diff_deg": "dedup_max_angle_diff_deg",
    "rec_max_calls": "overlap_rec_max_calls", "rec_max_group": "overlap_rec_max_group",
    "rec_max_x_overlap": "overlap_rec_max_x_overlap", "rec_max_angle_deg": "overlap_rec_max_angle_deg",
    "rec_max_foreign": "overlap_rec_max_foreign", "rec_max_fragment_digits": "overlap_rec_max_fragment_digits",
}


def params_of(cfg: dict[str, Any]) -> OverlapParams:
    """cfg["ocr"]의 중복 제거 · 재인식 키를 읽고 검증한다. 키가 없으면 KeyError, 값이 어긋나면 ValueError(설정 오류)."""
    oc = cfg["ocr"]
    p = OverlapParams(**{f: oc[k] for f, k in _KEYS.items()})
    for f in ("rec_max_calls", "rec_max_group", "rec_max_fragment_digits"):
        v = getattr(p, f)
        if not isinstance(v, int) or isinstance(v, bool):
            raise ValueError(f"ocr.{_KEYS[f]}={v!r}는 정수여야 한다")
    for f in ("dedup_min_poly_iou", "dedup_max_angle_diff_deg", "rec_max_x_overlap", "rec_max_angle_deg", "rec_max_foreign"):
        v = getattr(p, f)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            raise ValueError(f"ocr.{_KEYS[f]}={v!r}는 유한한 수여야 한다")
    checks = [
        ("dedup_min_poly_iou", 0 < p.dedup_min_poly_iou <= 1, "(0, 1]"),
        ("dedup_max_angle_diff_deg", 0 <= p.dedup_max_angle_diff_deg < 90, "[0, 90)"),
        ("rec_max_calls", p.rec_max_calls >= 0, "0 이상"),
        ("rec_max_group", p.rec_max_group >= 2, "2 이상"),
        ("rec_max_x_overlap", 0 < p.rec_max_x_overlap <= 1, "(0, 1]"),
        ("rec_max_angle_deg", 0 <= p.rec_max_angle_deg < 45, "[0, 45)"),
        ("rec_max_foreign", 0 <= p.rec_max_foreign <= 1, "[0, 1]"),
        ("rec_max_fragment_digits", p.rec_max_fragment_digits >= 0, "0 이상"),
    ]
    for f, ok, rng in checks:
        if not ok:
            raise ValueError(f"ocr.{_KEYS[f]}={getattr(p, f)}는 {rng}이어야 한다")
    return p


# ---- 텍스트 비교 -------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """텍스트 비교용 — NFKC 후 공백 문자 제거. 대소문자 · 문장부호는 그대로 둔다(다르면 다른 판독)."""
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if not ch.isspace())


def numbers_of(pieces: list[str]) -> tuple[str, ...]:
    """검출 원문마다 NFKC에서 공백을 살려 뽑은 숫자 값 토큰을 순서대로 잇는다(집합이 아니다). 검출 경계를 넘어 숫자를
    붙이지 않으므로 '50 100' · '50100', '50 50' · '50'이 서로 다르다."""
    return tuple(t for s in pieces for t in NUMBER.findall(unicodedata.normalize("NFKC", s)))


def comparable(pieces: list[str]) -> tuple[str, tuple[str, ...]]:
    """판독 비교 키 — (공백을 지운 텍스트, 순서 있는 숫자 값 토큰)."""
    return "".join(normalize_text(s) for s in pieces), numbers_of(pieces)


# ---- geometry ---------------------------------------------------------------------------
def _pts(poly: Any) -> np.ndarray:
    return np.asarray(poly, dtype=np.float64).reshape(-1, 2)


def signed_area(pts: np.ndarray) -> float:
    x, y = pts[:, 0], pts[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def convex_ccw(poly: Any) -> np.ndarray | None:
    """볼록 다각형이면 양의 넓이 방향으로 맞춘 점 배열, 아니면(넓이 0 · 오목 · 자기 교차) None."""
    pts = _pts(poly)
    keep = [i for i in range(len(pts)) if not np.allclose(pts[i], pts[i - 1])]
    pts = pts[keep]
    if len(pts) < 3:
        return None
    a = signed_area(pts)
    if abs(a) < _EPS:
        return None
    if a < 0:
        pts = pts[::-1]
    e = np.roll(pts, -1, axis=0) - pts
    e2 = np.roll(e, -1, axis=0)
    cross = e[:, 0] * e2[:, 1] - e[:, 1] * e2[:, 0]
    if (cross < -_EPS).any():
        return None
    # 한 바퀴만 도는지(자기 교차 없음): 볼록이면 외각 합이 2π
    ang = np.arctan2(e[:, 1], e[:, 0])
    turn = np.mod(np.diff(np.concatenate([ang, ang[:1]])) + np.pi, 2 * np.pi) - np.pi
    if abs(turn.sum() - 2 * np.pi) > 1e-6:
        return None
    return pts


def hull(points: Any) -> np.ndarray:
    """볼록 껍질(monotone chain), 양의 넓이 방향. 점이 3개 미만이거나 한 직선 위면 그 점들을 그대로 돌려준다."""
    pts = sorted({(float(x), float(y)) for x, y in _pts(points)})
    if len(pts) < 3:
        return np.asarray(pts, dtype=np.float64).reshape(-1, 2)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for q in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper: list[tuple[float, float]] = []
    for q in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return np.asarray(lower[:-1] + upper[:-1], dtype=np.float64)


def _cross_point(s: np.ndarray, e: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d1, d2 = e - s, b - a
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < _EPS:
        return e
    t = ((a[0] - s[0]) * d2[1] - (a[1] - s[1]) * d2[0]) / den
    return s + t * d1


def _clip(subject: np.ndarray, clipper: np.ndarray) -> np.ndarray:
    """Sutherland–Hodgman — 두 다각형 모두 볼록 · 양의 넓이 방향."""
    out = [p for p in subject]
    n = len(clipper)
    for i in range(n):
        a, b = clipper[i], clipper[(i + 1) % n]
        inp, out = out, []
        if not inp:
            break

        def inside(q: np.ndarray) -> bool:
            return (b[0] - a[0]) * (q[1] - a[1]) - (b[1] - a[1]) * (q[0] - a[0]) >= -_EPS

        s = inp[-1]
        for e in inp:
            if inside(e):
                if not inside(s):
                    out.append(_cross_point(s, e, a, b))
                out.append(e)
            elif inside(s):
                out.append(_cross_point(s, e, a, b))
            s = e
    return np.asarray(out, dtype=np.float64).reshape(-1, 2)


def _convex_inter(a: np.ndarray, b: np.ndarray) -> float:
    c = _clip(a, b)
    return 0.0 if len(c) < 3 else max(0.0, signed_area(c))


def intersection_area(poly_a: Any, poly_b: Any) -> float | None:
    """두 실제 polygon의 교집합 넓이. 어느 한쪽이라도 볼록이 아니면 None."""
    a, b = convex_ccw(poly_a), convex_ccw(poly_b)
    if a is None or b is None:
        return None
    return _convex_inter(a, b)


def poly_iou(poly_a: Any, poly_b: Any) -> float | None:
    a, b = convex_ccw(poly_a), convex_ccw(poly_b)
    if a is None or b is None:
        return None
    inter = _convex_inter(a, b)
    union = signed_area(a) + signed_area(b) - inter
    return 0.0 if union <= _EPS else inter / union


def inside_fraction(poly: Any, box: Any) -> float:
    """poly(볼록 껍질로 본다) 넓이 중 볼록 사각형 box 안에 든 비율. poly 넓이가 0이면 0."""
    h = hull(poly)
    if len(h) < 3 or signed_area(h) <= _EPS:
        return 0.0
    b = convex_ccw(box)
    if b is None:
        return 0.0
    return _convex_inter(h, b) / signed_area(h)


def top_edge_angle(poly: Any) -> float | None:
    """윗변(첫 점 → 둘째 점) 방향(도). 두 점이 같으면 None. PaddleOCR 사각형은 좌상단부터 시계 방향(화면 기준)이다."""
    pts = _pts(poly)
    if len(pts) < 2:
        return None
    d = pts[1] - pts[0]
    if abs(d[0]) < _EPS and abs(d[1]) < _EPS:
        return None
    return math.degrees(math.atan2(d[1], d[0]))


def angle_diff(a: float, b: float) -> float:
    """두 방향의 차(0~90도, 180도 주기)."""
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


@dataclass(frozen=True)
class Rect:
    box: np.ndarray  # (4, 2) 섹션 로컬 실수 좌표 — 좌상 · 우상 · 우하 · 좌하(PaddleX get_minarea_rect_crop 순서)
    width: float
    height: float
    angle: float  # 윗변 기울기(도)


def min_area_rect(points: Any) -> Rect | None:
    """점들을 모두 감싸는 최소 넓이 회전 사각형(회전 캘리퍼스). 점이 한 직선 위면 None.

    꼭짓점 순서는 PaddleX `get_minarea_rect_crop`과 같다: x(같으면 y)로 정렬해 왼쪽 두 점 중 위가 좌상 · 아래가 좌하, 오른쪽 두
    점 중 위가 우상 · 아래가 우하."""
    h = hull(points)
    if len(h) < 3 or signed_area(h) <= _EPS:
        return None
    best: tuple[float, np.ndarray, np.ndarray, float, float, float, float] | None = None
    for i in range(len(h)):
        edge = h[(i + 1) % len(h)] - h[i]
        length = float(np.hypot(edge[0], edge[1]))
        if length < _EPS:
            continue
        u = edge / length
        v = np.array([-u[1], u[0]])
        pu, pv = h @ u, h @ v
        area = float((pu.max() - pu.min()) * (pv.max() - pv.min()))
        if best is None or area < best[0] - _EPS:
            best = (area, u, v, float(pu.min()), float(pu.max()), float(pv.min()), float(pv.max()))
    if best is None:
        return None
    _, u, v, u0, u1, v0, v1 = best
    corners = [u * a + v * b for a, b in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]
    pts = sorted(corners, key=lambda q: (q[0], q[1]))
    tl, bl = (pts[0], pts[1]) if pts[0][1] <= pts[1][1] else (pts[1], pts[0])
    tr, br = (pts[2], pts[3]) if pts[2][1] <= pts[3][1] else (pts[3], pts[2])
    box = np.asarray([tl, tr, br, bl], dtype=np.float64)
    width = max(float(np.linalg.norm(tl - tr)), float(np.linalg.norm(bl - br)))
    height = max(float(np.linalg.norm(tl - bl)), float(np.linalg.norm(tr - br)))
    angle = math.degrees(math.atan2(tr[1] - tl[1], tr[0] - tl[0]))
    return Rect(box=box, width=width, height=height, angle=angle)


# ---- 1. 호출별 명백한 중복 제거 ------------------------------------------------------------------
def _bbox_meet(a: OcrRegion, b: OcrRegion) -> bool:
    return min(a.bbox.x2, b.bbox.x2) > max(a.bbox.x, b.bbox.x) and min(a.bbox.y2, b.bbox.y2) > max(a.bbox.y, b.bbox.y)


def _poly_list(r: OcrRegion) -> list[list[int]]:
    return [[int(x), int(y)] for x, y in r.poly]


def dedup(regions: list[OcrRegion], p: OverlapParams) -> tuple[list[int], list[dict[str, Any]]]:
    """엔진 호출 하나의 영역 목록(엔진 출력 순서)에서 명백한 중복을 지운다.

    반환: (남긴 영역의 원래 위치 목록 — 순서 유지, 기록). 기록은 제거한 검출(`action: removed`, 원시 값 · 대표 위치 · IoU ·
    방향 차)과, 같은 텍스트 · 같은 자리였지만 판정을 보류해 남긴 검출(`action: kept`, 사유)이다. 위치(order)는 엔진 출력 순서다."""
    th_iou, th_ang = p.dedup_min_poly_iou, p.dedup_max_angle_diff_deg
    angles = [top_edge_angle(r.poly) for r in regions]
    removed: set[int] = set()
    reps: list[int] = []
    records: list[dict[str, Any]] = []

    def measure(i: int, j: int) -> tuple[float, float] | None:
        """(polygon IoU, 윗변 방향 차). 판정할 수 없으면(볼록 아님 · 방향 없음) None."""
        if not _bbox_meet(regions[i], regions[j]):
            return 0.0, 0.0
        iou = poly_iou(regions[i].poly, regions[j].poly)
        if iou is None or angles[i] is None or angles[j] is None:
            return None
        return iou, angle_diff(angles[i], angles[j])

    def base(i: int) -> dict[str, Any]:
        r = regions[i]
        return {"order": i, "text": r.text, "score": r.score, "poly": _poly_list(r),
                "bbox": [r.bbox.x, r.bbox.y, r.bbox.w, r.bbox.h]}

    for i, r in enumerate(regions):
        if r.text == "":
            continue  # 빈 문자열은 비교 · 제거하지 않는다
        hits: list[tuple[int, float, float]] = []
        undecidable: list[int] = []
        for j in reps:
            if regions[j].text != r.text:
                continue
            m = measure(i, j)
            if m is None:
                if _bbox_meet(regions[i], regions[j]):
                    undecidable.append(j)
                continue
            if m[0] >= th_iou and m[1] <= th_ang:
                hits.append((j, m[0], m[1]))
        if not hits:
            if undecidable:
                records.append({**base(i), "action": "kept", "reason": "polygon_not_comparable", "candidates": undecidable})
            reps.append(i)
            continue
        if len(hits) > 1 or undecidable:
            records.append({**base(i), "action": "kept", "reason": "multiple_representatives",
                            "candidates": [j for j, _, _ in hits] + undecidable})
            reps.append(i)
            continue
        j, iou, ad = hits[0]
        tangled = []
        for k, o in enumerate(regions):
            if k in (i, j) or k in removed or o.text == r.text:
                continue
            for q in (i, j):
                if not _bbox_meet(o, regions[q]):
                    continue
                v = poly_iou(o.poly, regions[q].poly)
                if v is None or v >= th_iou:  # 볼록이 아닌 얽힘도 보수적으로 얽힘으로 본다
                    tangled.append(k)
                    break
        if tangled:
            records.append({**base(i), "action": "kept", "reason": "tangled_with_other_text", "representative": j,
                            "tangled": tangled})
            reps.append(i)
            continue
        removed.add(i)
        records.append({**base(i), "action": "removed", "reason": "same_text_same_polygon", "representative": j,
                        "poly_iou": round(iou, 4), "angle_diff_deg": round(ad, 3)})
    return [i for i in range(len(regions)) if i not in removed], records


def removed_count(records: list[dict[str, Any]]) -> int:
    return sum(1 for r in records if r.get("action") == "removed")


# ---- 2. 방향 보정 crop --------------------------------------------------------------------
def crop_key(box: np.ndarray) -> tuple[int, ...]:
    """같은 입력 crop 판별 키 — 회전 사각형 꼭짓점의 정수 반올림(섹션 로컬)."""
    return tuple(int(v) for v in np.rint(np.asarray(box, dtype=np.float64)).astype(np.int64).ravel())


def crop_size(box: np.ndarray) -> tuple[int, int]:
    """방향 보정 뒤 crop 크기 (너비, 높이) — PaddleX get_rotate_crop_image와 같은 계산(소수점 버림)."""
    b = np.asarray(box, dtype=np.float64)
    w = int(max(np.linalg.norm(b[0] - b[1]), np.linalg.norm(b[2] - b[3])))
    h = int(max(np.linalg.norm(b[0] - b[3]), np.linalg.norm(b[1] - b[2])))
    return w, h


def build_crop(image: np.ndarray, box: np.ndarray) -> tuple[np.ndarray, dict[str, Any]]:
    """섹션 이미지(BGR) → 회전 사각형 box의 방향 보정 crop과 좌표 기록.

    ROI = box 꼭짓점을 감싸는 정수 축 정렬 범위(이미지 안으로 자름). box를 ROI 로컬로 옮겨 원근 변환한다 — 섹션 로컬 좌표 =
    ROI 로컬 + (roi_x0, roi_y0). ROI 밖으로 나간 꼭짓점 부분은 BORDER_REPLICATE로 채운다(PaddleX도 이미지 밖을 같은 방식으로
    채운다). crop 크기가 0이면 ValueError."""
    import cv2  # OCR 의존성(paddlex가 opencv-contrib-python을 요구) — 재인식할 때만 필요

    hgt, wid = image.shape[:2]
    b = np.asarray(box, dtype=np.float64)
    fx0, fy0 = float(b[:, 0].min()), float(b[:, 1].min())
    fx1, fy1 = float(b[:, 0].max()), float(b[:, 1].max())
    x0, y0 = max(0, math.floor(fx0)), max(0, math.floor(fy0))
    x1, y1 = min(wid, math.ceil(fx1) + 1), min(hgt, math.ceil(fy1) + 1)
    cw, ch = crop_size(b)
    if x1 <= x0 or y1 <= y0 or cw <= 0 or ch <= 0:
        raise ValueError(f"crop 크기 0: roi {[x0, y0, x1, y1]} size {[cw, ch]}")
    roi = image[y0:y1, x0:x1]
    local = (b - np.array([x0, y0], dtype=np.float64)).astype(np.float32)
    std = np.float32([[0, 0], [cw, 0], [cw, ch], [0, ch]])
    m = cv2.getPerspectiveTransform(local, std)
    dst = cv2.warpPerspective(roi, m, (cw, ch), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    rotated = ch / cw >= _ROTATE_RATIO
    if rotated:
        dst = np.rot90(dst)
    info = {
        "box": [[round(float(x), 2), round(float(y), 2)] for x, y in b],
        "roi": [x0, y0, x1, y1],
        "roi_clamped": bool(fx0 < 0 or fy0 < 0 or fx1 > wid or fy1 > hgt),
        "size": [cw, ch],
        "rotated_90": bool(rotated),
    }
    return np.ascontiguousarray(dst), info


# ---- 3. 재인식 판독 변화 설명 ----------------------------------------------------------------
def _edge_digits(s: str, end: bool) -> int:
    t = s.rstrip() if end else s.lstrip()
    n = 0
    for ch in (reversed(t) if end else t):
        if not ch.isdigit():
            break
        n += 1
    return n


def _cut(s: str, n: int, end: bool) -> tuple[str, str]:
    if end:
        t = s.rstrip()
        return t[:-n] + s[len(t):], t[-n:]
    t = s.lstrip()
    return s[:len(s) - len(t)] + t[n:], t[:n]


def _char_kind(ch: str) -> str:
    c = unicodedata.category(ch)
    return "digit" if ch.isdigit() else ("symbol" if c[0] in "PS" else "text")


def describe_diff(pieces: list[str], reread: str) -> dict[str, Any]:
    """설명되지 않은 변화의 진단 요약 — 거절 사유 분류와 글자 차이."""
    a, b = comparable(pieces), comparable([reread])
    ops = []
    kinds: set[tuple[str, str]] = set()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a[0], b[0], autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        old, new = a[0][i1:i2], b[0][j1:j2]
        ops.append({"op": tag, "old": old, "new": new})
        kinds |= {("lost", _char_kind(ch)) for ch in old} | {("new", _char_kind(ch)) for ch in new}
    if not ops:
        reason = "numbers_split_or_merged"  # 글자는 같고 숫자 토큰 경계(공백)만 다르다 — 예: '50 100' → '50100'
    elif ("new", "digit") in kinds or ("new", "symbol") in kinds:
        reason = "new_digit_or_symbol"
    elif ("lost", "symbol") in kinds:
        reason = "lost_symbol_or_unit"
    elif ("lost", "text") in kinds or ("new", "text") in kinds:
        reason = "text_changed"
    else:
        reason = "digit_change_not_at_contact"
    return {"reason": reason, "ops": ops, "numbers_before": list(a[1]), "numbers_after": list(b[1])}


def explain_change(pieces: list[str], contacts: list[bool], reread: str, max_digits: int) -> dict[str, Any]:
    """재인식 판독 reread가 원래 묶음(검출 원문을 x 순서로, pieces)과 어떻게 다른지 설명한다.

    - `unchanged`: 비교 키(공백을 지운 텍스트 · 순서 있는 숫자 토큰)가 같다.
    - `fragment_digits`: polygon이 실제로 겹친 이웃 경계(contacts[j] — pieces[j]와 pieces[j+1] 사이)마다 왼쪽 검출 끝 · 오른쪽
      검출 앞에서 숫자를 max_digits개 이하 지운 결과가 reread와 같다. 삭제 수가 적은 조합을 먼저 본다. 검출 하나가 통째로
      비면 설명으로 보지 않는다. 그 밖의 글자 변경 · 새 글자 · 숫자 붙음은 설명하지 않는다.
    - `unexplained`: 그 밖 — `diff`에 거절 사유 분류(describe_diff)."""
    target = comparable([reread])
    if comparable(pieces) == target:
        return {"result": "unchanged"}
    options: list[list[tuple[int, int]]] = []
    for j, c in enumerate(contacts):
        if not c or max_digits <= 0:
            options.append([(0, 0)])
            continue
        left = min(_edge_digits(pieces[j], end=True), max_digits)
        right = min(_edge_digits(pieces[j + 1], end=False), max_digits)
        options.append([(x, y) for x in range(left + 1) for y in range(right + 1)])
    if math.prod(len(o) for o in options) > _MAX_COMBOS:
        return {"result": "unexplained", "diff": {**describe_diff(pieces, reread), "reason": "too_many_combinations"}}
    combos = sorted(itertools.product(*options), key=lambda cb: sum(x + y for x, y in cb))
    for combo in combos:
        if all(x == 0 and y == 0 for x, y in combo):
            continue
        cut = list(pieces)
        removed = []
        for j, (x, y) in enumerate(combo):
            if x:
                cut[j], chars = _cut(cut[j], x, end=True)
                removed.append({"junction": j, "where": "end_of_left", "chars": chars})
            if y:
                cut[j + 1], chars = _cut(cut[j + 1], y, end=False)
                removed.append({"junction": j, "where": "start_of_right", "chars": chars})
        if any(not normalize_text(s) for s in cut):
            continue
        if comparable(cut) == target:
            return {"result": "fragment_digits", "removed": removed, "pieces_after": cut}
    return {"result": "unexplained", "diff": describe_diff(pieces, reread)}
