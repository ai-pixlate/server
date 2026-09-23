"""① 섹션 분해 — `color_snap_vlm2` [pipeline.md 단계표 ①].

배경색 전환으로 자르고, 색이 안 바뀌는 긴 구간만 VLM이 경계를 고른다. 입도는 소제목 단위.
원본 1장 단위로 실행하며, 섹션 이미지를 out_dir에 잘라 저장하고 SplitResult를 돌려준다.

경계 결정 순서 (파라미터는 config `[section]`, 값은 open-questions #26 잠정):
1. 행 프로파일 — 행마다 배경색(픽셀 중앙값 RGB)과 색 표준편차를 구한다. 중앙값이라 글자가 있어도 배경이 남는다.
   표준편차 ≤ `blank_row_std`인 행을 **균일 행**(여백·빈 배경)이라 부른다.
2. 색 전환 후보 — 행 색을 `color_window_px` 창으로 평활해 앞 창과 비교, RGB 거리 ≥ `color_delta`인 구간의
   최댓값 행을 후보로 잡는다. 경계 = 새 색의 첫 행. 그 행과 바로 위 행이 모두 균일 행이 아니면(글자 위에 놓임)
   반경 `snap_radius_px` 안 **위쪽 여백의 끝 행**(글자 바로 위)으로 옮기고, 위쪽에 없으면 아래쪽 여백의 시작 행으로
   옮긴다. 반경 안에 여백이 없으면 기각(`no_blank_row`). 전환 행의 글자는 새 배경 위에 있어 아래 섹션에 속하므로
   위쪽을 우선한다. 글자를 가르는 절단은 ② OCR을 망친다.
3. 후보 확정 — 후보 양쪽 구간(이웃 후보까지, 최대 `min_section_px`. 창의 4배보다 가까운 이웃은 얇은 바·구분선의
   반대편 모서리로 보고 건너뛴다)에 대해
   (a) 두 구간의 행 색 **중앙값** 거리 ≥ `color_delta` (큰 제목 글자가 만드는 짧은 색 튐 제거) 이고
   (b) 두 구간 **모두** 균일 행 비율 ≥ `bg_row_ratio` (배경다움) 일 때만 확정한다.
   사진·표·일러스트 띠는 균일 행이 거의 없어 그 위아래 경계가 모두 기각되고 제목·그림·각주가 한 섹션에 남는다.
   확정 후보의 강도 = (a)의 거리.
4. 최소 높이 — 인접 경계 간격이 `min_section_px`보다 짧으면 **강도가 약한** 경계를 버린다(같으면 위쪽을 남긴다).
5. 빈 구간 병합 — 모든 행이 균일한 구간(여백뿐인 띠)은 앞 구간에 붙인다(첫 구간이면 뒤 구간에).
6. 긴 구간 VLM — 위 결과 구간이 `long_section_px`보다 길면 VLM에 준다. 입력은 폭을 `vlm_width_px` 이하로 줄이고
   (긴 변 기준이 아니다 — 긴 띠를 긴 변 기준으로 줄이면 글자가 판독 불가), 높이가 `vlm_window_px`(원본 px)를 넘으면
   `vlm_window_overlap_px` 겹침으로 창을 나눠 여러 번 호출한 뒤 합친다. 왼쪽에 y 눈금 띠를 붙인다(프롬프트 `prompt_path`).
7. 보정(snap) — VLM의 y에서 반경 `snap_radius_px` 안에 균일 행 구간(여백)이 있으면 그 구간의 **중앙**으로 옮긴다.
   반경은 여백을 찾는 거리이고 목적지는 여백 중앙이므로, 넓은 여백에서는 반경보다 멀리 움직인다(같은 여백을 가리킨
   여러 응답이 한 점으로 모이게 하는 설계). 여백 구간은 **현재 구간 안에서만** 찾는다. 반경 안에 여백이 없으면 후보를
   버리고 진단에 남긴다. 색 전환 경계와 합쳐 4를 다시 적용한 뒤, 5를 한 번 더 적용한다(VLM 경계가 여백 안에 놓여
   색 경계·구간 끝과의 사이가 여백뿐이면 VLM 경계를 지운다).

진단: `decide_boundaries(..., diag={})`에 dict를 주면 후보·기각 사유·빈 구간 병합·VLM 호출·보정 결과를 채운다.
CLI는 이를 `split_debug.json`으로 남긴다(dev.md 4절). 계약 값이 아니라 개발 확인용이다.
VLM 실패는 #25 미정 — `pipeline.vlm.VlmError`를 그대로 전파하고 대체 처리를 하지 않는다.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pipeline.errors import image_open_failed
from pipeline.types import Section, SourceImage, SplitResult, section_key
from pipeline.vlm import BoundaryPicker, GeminiBoundaryPicker, image_sha256, prompt_sha256

RULER_W = 48  # VLM 입력 왼쪽 눈금 띠 폭(px)
_SIDE_MIN_FACTOR = 4  # 후보 확정 창을 자르는 이웃 후보의 최소 거리 = color_window_px × 이 값. 더 가까운 후보(얇은 바·선)는 무시
RULER_STEP = 100  # 눈금 간격(리사이즈 후 px)
_PROFILE_CHUNK = 2048  # 행 프로파일 계산 단위 — 최대 1000×37,665px 원본에서 메모리를 제한한다


def open_source(src: SourceImage) -> Image.Image:
    """원본을 연다. 깨진 파일·미지원 포맷은 IMAGE_OPEN_FAILED(재시도 불가)."""
    try:
        im = Image.open(src.path)
        im.load()
        return im
    except Exception as e:  # noqa: BLE001 — Pillow 예외 종류가 다양하다
        raise image_open_failed(src.path, src.source_image_id, e) from e


# ---- 1. 행 프로파일 -------------------------------------------------------------
def row_profile(im: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """행별 배경색 (h,3)과 행 색 표준편차 (h,). 배경색은 행 픽셀의 채널별 중앙값."""
    rgb = im if im.mode == "RGB" else im.convert("RGB")
    w, h = rgb.size
    color = np.empty((h, 3), dtype=np.float32)
    std = np.empty(h, dtype=np.float32)
    for y0 in range(0, h, _PROFILE_CHUNK):
        y1 = min(h, y0 + _PROFILE_CHUNK)
        arr = np.asarray(rgb.crop((0, y0, w, y1)), dtype=np.float32)  # (n, w, 3)
        color[y0:y1] = np.median(arr, axis=1)
        std[y0:y1] = arr.std(axis=1).mean(axis=1)
    return color, std


def uniform_ratio(std: np.ndarray, blank_std: float) -> float:
    """구간에서 균일 행이 차지하는 비율. 빈 구간은 1.0."""
    return float((std <= blank_std).mean()) if len(std) else 1.0


# ---- 2·3·4. 색 전환 경계 -------------------------------------------------------------
def color_candidates(color: np.ndarray, window: int, delta: float) -> list[tuple[int, float]]:
    """평활 창 비교로 전환 후보 (y, 창 거리) 목록을 얻는다. 확정 전 단계."""
    h = len(color)
    if h < 2 * window or window < 1:
        return []
    cs = np.vstack([np.zeros((1, 3), dtype=np.float64), np.cumsum(color, axis=0, dtype=np.float64)])
    ys = np.arange(window, h - window + 1)  # 경계 y: 앞 창 [y-w, y) vs 뒤 창 [y, y+w)
    before = (cs[ys] - cs[ys - window]) / window
    after = (cs[ys + window] - cs[ys]) / window
    d = np.linalg.norm(after - before, axis=1)
    hot = d >= delta

    out: list[tuple[int, float]] = []
    i, n = 0, len(ys)
    while i < n:
        if not hot[i]:
            i += 1
            continue
        j = i
        while j < n and hot[j]:
            j += 1
        k = i + int(np.argmax(d[i:j]))
        # 창 평균의 최댓값은 실제 전환 행에서 몇 행 어긋난다. 전환 구간 안에서 행 간 색 변화가 가장 큰 행을 경계로 잡는다.
        lo, hi = int(ys[i]), int(ys[j - 1])
        step = np.linalg.norm(color[lo : hi + 1] - color[lo - 1 : hi], axis=1)
        y = lo + int(np.argmax(step))
        out.append((y, float(d[k])))
        i = j
    return out


def _neighbor_bounds(ys: list[int], idx: int, h: int, min_gap: int, y: int | None = None) -> tuple[int, int]:
    """후보 idx의 앞뒤 이웃 후보 y. `min_gap`보다 가까운 이웃(얇은 바·구분선의 반대편 모서리)은 건너뛴다.

    `y`를 주면(여백 보정 뒤 좌표) 그 위치를 기준으로 거리를 잰다.
    """
    y = ys[idx] if y is None else y
    prev_y = 0
    for k in range(idx - 1, -1, -1):
        if y - ys[k] >= min_gap:
            prev_y = ys[k]
            break
    next_y = h
    for k in range(idx + 1, len(ys)):
        if ys[k] - y >= min_gap:
            next_y = ys[k]
            break
    return prev_y, next_y


def snap_color_boundary(y: int, runs: list[tuple[int, int]], radius: int) -> int | None:
    """글자 위에 놓인 색 경계를 여백으로 옮긴다. VLM 보정(여백 중앙)과 달리 **여백의 끝 행**을 쓴다.

    전환 행의 글자는 새 배경 위에 있으므로 아래 섹션에 속한다. 그래서 반경 안 **위쪽** 여백 구간의 끝(글자 바로 위 행)을
    우선하고, 위쪽에 없으면 아래쪽 여백 구간의 시작(글자 바로 아래 행)을 쓴다. 둘 다 없으면 None(후보 기각).
    """
    above = [e for s, e in runs if e <= y and y - e <= radius]
    if above:
        return max(above)
    below = [s for s, e in runs if s >= y and s - y <= radius]
    if below:
        return min(below)
    return None


def color_boundaries(
    color: np.ndarray,
    std: np.ndarray,
    window: int,
    delta: float,
    min_section: int,
    blank_std: float,
    bg_ratio: float,
    diag: dict[str, Any] | None = None,
    snap_radius: int = 0,
) -> tuple[list[int], dict[int, float]]:
    """배경색 전환 경계(오름차순, 0과 h 제외, 최소 높이 적용)와 경계별 강도를 돌려준다.

    `snap_radius` > 0 이면 글자 위에 놓인 경계 행을 반경 안 여백으로 옮기고(`snap_color_boundary`), 없으면 기각한다.
    """
    h = len(color)
    cands = color_candidates(color, window, delta)
    ys = [y for y, _ in cands]
    accepted: dict[int, float] = {}
    records: list[dict[str, Any]] = []
    min_gap = _SIDE_MIN_FACTOR * window
    runs = blank_runs(std, blank_std)
    for idx, (y_raw, narrow) in enumerate(cands):
        # 경계 행이 글자 위(y와 y-1 모두 균일 행 아님)면 여백으로 옮긴다. 옮길 여백이 반경 안에 없으면 기각.
        y = y_raw
        snapped = False
        if not (std[y] <= blank_std or std[y - 1] <= blank_std):
            moved = snap_color_boundary(y, runs, snap_radius)
            if moved is None or moved <= 0 or moved >= h:
                records.append({"y": y_raw, "y_raw": y_raw, "narrow": round(narrow, 1), "wide": None, "bg_before": None,
                                "bg_after": None, "accepted": False, "reason": "no_blank_row"})
                continue
            y, snapped = moved, True
        prev_y, next_y = _neighbor_bounds(ys, idx, h, min_gap, y=y)
        a0, b1 = max(prev_y, y - min_section), min(next_y, y + min_section)
        if a0 >= y or b1 <= y:
            records.append({"y": y, "y_raw": y_raw, "narrow": round(narrow, 1), "wide": None, "bg_before": None,
                            "bg_after": None, "accepted": False, "reason": "no_window"})
            continue
        wide = float(np.linalg.norm(np.median(color[y:b1], axis=0) - np.median(color[a0:y], axis=0)))
        r_before = uniform_ratio(std[a0:y], blank_std)
        r_after = uniform_ratio(std[y:b1], blank_std)
        if wide < delta:
            reason = "median_delta"
        elif min(r_before, r_after) < bg_ratio:
            reason = "bg_ratio"
        else:
            reason = "ok"
            accepted[y] = max(wide, accepted.get(y, 0.0))  # 두 후보가 같은 여백 행으로 모이면 하나로
        records.append(
            {"y": y, "y_raw": y_raw, "snapped": snapped, "narrow": round(narrow, 1), "wide": round(wide, 1),
             "bg_before": round(r_before, 2), "bg_after": round(r_after, 2), "accepted": reason == "ok", "reason": reason}
        )
    kept = enforce_min_section(list(accepted), h, min_section, strengths=accepted)
    for r in records:
        if r["accepted"] and r["y"] not in kept:
            r["accepted"], r["reason"] = False, "min_section"
    if diag is not None:
        diag["color_candidates"] = records
    return kept, {y: accepted[y] for y in kept}


def enforce_min_section(
    candidates: list[int],
    height: int,
    min_section: int,
    fixed: list[int] = (),
    strengths: dict[int, float] | None = None,
) -> list[int]:
    """`min_section`보다 짧은 구간을 만드는 경계를 버린다. 강한 경계부터 받아들이고(같으면 위쪽), 약한 쪽이 밀린다.

    `fixed`는 이미 확정된 경계로 먼저 자리를 잡는다. 후보는 이들과도 간격을 지켜야 하며 결과에 함께 들어간다.
    `strengths`가 없으면 후보는 모두 같은 강도(y 오름차순)다.
    """
    kept: list[int] = sorted(set(fixed))
    s = strengths or {}
    for y in sorted(set(candidates), key=lambda v: (-s.get(v, 0.0), v)):
        if y in kept:
            continue
        if y < min_section or height - y < min_section:
            continue
        if any(abs(y - k) < min_section for k in kept):
            continue
        kept.append(y)
    return sorted(kept)


# ---- 5. 빈 구간 병합 ------------------------------------------------------------------
def merge_empty_segments(
    boundaries: list[int],
    std: np.ndarray,
    blank_std: float,
    diag: dict[str, Any] | None = None,
    diag_key: str = "empty_merged",
    keep: set[int] | frozenset[int] = frozenset(),
) -> list[int]:
    """모든 행이 균일한 구간(여백뿐인 띠)을 앞 구간에 붙인다. 첫 구간이면 뒤 구간에 붙인다.

    `keep`에 든 경계(색 전환 경계)는 지키고, 빈 구간의 위 경계가 그것이면 아래 경계(VLM 보정 경계)를 대신 지운다.
    경계를 지우기만 하므로 구간은 길어질 뿐이며 최소 높이 조건이 새로 깨지지 않는다.
    """
    h = len(std)
    edges = [0, *boundaries, h]
    merged: list[dict[str, int]] = []
    changed = True
    while changed and len(edges) > 2:
        changed = False
        for i in range(len(edges) - 1):
            top, bottom = edges[i], edges[i + 1]
            if uniform_ratio(std[top:bottom], blank_std) < 1.0:
                continue
            if i == 0 or (top in keep and bottom not in keep and bottom != h):
                drop = bottom  # 첫 구간이거나 위 경계를 지켜야 하면 아래 경계를 지운다(뒤 구간에 붙인다)
            else:
                drop = top  # 앞 구간에 붙인다
            merged.append({"top": top, "bottom": bottom, "dropped": drop})
            edges.remove(drop)
            changed = True
            break
    if diag is not None:
        diag[diag_key] = merged
    return edges[1:-1]


# ---- 7. 여백 행과 보정 ----------------------------------------------------------------
def blank_runs(std: np.ndarray, std_max: float) -> list[tuple[int, int]]:
    """균일 행(여백)의 연속 구간 [start, end) 목록."""
    blank = std <= std_max
    runs: list[tuple[int, int]] = []
    i, n = 0, len(blank)
    while i < n:
        if not blank[i]:
            i += 1
            continue
        j = i
        while j < n and blank[j]:
            j += 1
        runs.append((i, j))
        i = j
    return runs


def snap_to_blank(y: int, runs: list[tuple[int, int]], radius: int) -> int | None:
    """y에서 반경 안 가장 가까운 여백 구간의 중앙. 없으면 None(후보 폐기)."""
    best: tuple[int, int] | None = None
    for s, e in runs:
        dist = 0 if s <= y < e else min(abs(y - s), abs(y - (e - 1)))
        if dist <= radius and (best is None or dist < best[0]):
            best = (dist, (s + e) // 2)
    return None if best is None else best[1]


# ---- 6. 긴 구간 VLM ---------------------------------------------------------------------
def load_prompt(path: str | Path, width: int, height: int) -> str:
    text = Path(path).read_text(encoding="utf-8")
    return text.replace("{{WIDTH}}", str(width)).replace("{{HEIGHT}}", str(height))


def prepare_vlm_image(segment: Image.Image, width_px: int) -> tuple[Image.Image, float]:
    """폭을 width_px 이하로 줄이고(비율 유지) 왼쪽에 y 눈금 띠를 붙인다. (이미지, scale=리사이즈/원본) 반환."""
    w, h = segment.size
    scale = min(1.0, width_px / w)
    rw, rh = max(1, round(w * scale)), max(1, round(h * scale))
    body = segment.convert("RGB").resize((rw, rh), Image.LANCZOS) if scale < 1.0 else segment.convert("RGB")
    out = Image.new("RGB", (rw + RULER_W, rh), (255, 255, 255))
    out.paste(body, (RULER_W, 0))
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default()
    draw.line((RULER_W - 1, 0, RULER_W - 1, rh), fill=(255, 0, 0))
    for y in range(0, rh, RULER_STEP):
        draw.line((RULER_W - 8, y, RULER_W - 1, y), fill=(255, 0, 0))
        draw.text((2, max(0, min(rh - 12, y - 5))), str(y), fill=(255, 0, 0), font=font)
    return out, scale


def vlm_windows(height: int, window: int, overlap: int) -> list[tuple[int, int]]:
    """구간 높이를 `window`(원본 px) 창으로 나눈다. 창은 `overlap`만큼 겹친다. 한 창에 들어가면 그대로."""
    if height <= window:
        return [(0, height)]
    step = max(1, window - overlap)
    n = math.ceil((height - overlap) / step)
    wins = []
    for i in range(n):
        top = i * step
        bottom = min(height, top + window)
        wins.append((top, bottom))
        if bottom >= height:
            break
    return wins


def vlm_boundaries(
    segment: Image.Image,
    sc: dict[str, Any],
    vlm: BoundaryPicker,
    diag_entry: dict[str, Any] | None = None,
    segment_top: int = 0,
) -> list[int]:
    """긴 구간 하나에 VLM을 (창마다) 호출해 구간 로컬 원본 좌표의 경계 y 목록을 받는다.

    `segment_top`은 구간의 원본 기준 시작 행. 기록·재생 검증용 절대 좌표에 쓴다.
    """
    wins = vlm_windows(segment.height, int(sc["vlm_window_px"]), int(sc["vlm_window_overlap_px"]))
    found: set[int] = set()
    calls: list[dict[str, Any]] = []
    seg_abs = [segment_top, segment_top + segment.height]
    expect_hook = getattr(vlm, "expect", None)  # ReplayBoundaryPicker만 가진다
    for top, bottom in wins:
        part = segment if (top, bottom) == (0, segment.height) else segment.crop((0, top, segment.width, bottom))
        img, scale = prepare_vlm_image(part, int(sc["vlm_width_px"]))
        prompt = load_prompt(sc["prompt_path"], img.width - RULER_W, img.height)
        abs_window = [segment_top + top, segment_top + bottom]
        if expect_hook is not None:
            expect_hook({"segment": seg_abs, "abs_window": abs_window})
        ys = vlm(img, prompt)
        local = sorted({top + round(y / scale) for y in ys if 0 < y < img.height})
        found.update(local)
        calls.append(
            {"segment": seg_abs, "window": [top, bottom], "abs_window": abs_window, "input_size": list(img.size),
             "scale": round(scale, 6), "image_sha256": image_sha256(img), "prompt_sha256": prompt_sha256(prompt),
             "raw": list(ys), "mapped": local}
        )
    if diag_entry is not None:
        diag_entry["calls"] = calls
    return sorted(found)


def vlm_seed(sc: dict[str, Any]) -> int | None:
    """`vlm_seed` 키. 음수면 보내지 않는다(기본)."""
    seed = int(sc["vlm_seed"])
    return None if seed < 0 else seed


def default_picker(sc: dict[str, Any]) -> BoundaryPicker:
    return GeminiBoundaryPicker(model=str(sc["vlm_model"]), temperature=float(sc["vlm_temperature"]), seed=vlm_seed(sc))


def vlm_context(sc: dict[str, Any]) -> dict[str, Any]:
    """VLM 응답에 영향을 주는 설정. 진단 기록과 재생 검증에 쓴다."""
    prompt_file = Path(sc["prompt_path"])
    return {
        "model": str(sc["vlm_model"]),
        "temperature": float(sc["vlm_temperature"]),
        "seed": vlm_seed(sc),
        "width_px": int(sc["vlm_width_px"]),
        "window_px": int(sc["vlm_window_px"]),
        "window_overlap_px": int(sc["vlm_window_overlap_px"]),
        "prompt_path": str(sc["prompt_path"]),
        "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest()[:16] if prompt_file.is_file() else None,
        "ruler_w": RULER_W,
        "ruler_step": RULER_STEP,
    }


def source_fingerprint(path: str | Path) -> dict[str, Any]:
    """원본 지문(파일 해시 · 크기). 진단 기록과 재생 검증에 쓴다."""
    p = Path(path)
    with Image.open(p) as im:
        w, h = im.size
    return {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()[:16], "width": w, "height": h}


# ---- 조립 ---------------------------------------------------------------------------------
def decide_boundaries(
    im: Image.Image,
    sc: dict[str, Any],
    vlm: BoundaryPicker | None = None,
    diag: dict[str, Any] | None = None,
) -> list[int]:
    """경계 목록(오름차순, 0과 height 제외)을 정한다. crop_sections()와 분리해 테스트한다."""
    window = int(sc["color_window_px"])
    delta = float(sc["color_delta"])
    min_section = int(sc["min_section_px"])
    long_px = int(sc["long_section_px"])
    radius = int(sc["snap_radius_px"])
    blank_std = float(sc["blank_row_std"])
    bg_ratio = float(sc["bg_row_ratio"])

    color, std = row_profile(im)
    fixed, strengths = color_boundaries(color, std, window, delta, min_section, blank_std, bg_ratio, diag, snap_radius=radius)
    fixed = merge_empty_segments(fixed, std, blank_std, diag)
    h = im.height
    edges = [0, *fixed, h]

    candidates: list[int] = []
    vlm_records: list[dict[str, Any]] = []
    for top, bottom in zip(edges, edges[1:]):
        if bottom - top <= long_px:
            continue
        if vlm is None:
            vlm = default_picker(sc)
        rec: dict[str, Any] = {"segment": [top, bottom], "snapped": [], "dropped": []}
        # 여백 구간은 현재 구간 안에서만 찾는다. 원본 전체로 찾으면 색 전환 경계 양쪽 여백이 한 구간으로
        # 합쳐져 그 중앙(다른 색 구간)으로 보정된 뒤 폐기된다.
        runs = [(top + s, top + e) for s, e in blank_runs(std[top:bottom], blank_std)]
        for y in vlm_boundaries(im.crop((0, top, im.width, bottom)), sc, vlm, rec, segment_top=top):
            snapped = snap_to_blank(top + y, runs, radius)
            if snapped is not None and top < snapped < bottom:
                candidates.append(snapped)
                rec["snapped"].append({"from": top + y, "to": snapped})
            else:
                rec["dropped"].append(top + y)
        vlm_records.append(rec)
    finish_hook = getattr(vlm, "finish", None)  # ReplayBoundaryPicker: 기록이 남으면 실패
    if finish_hook is not None:
        finish_hook()
    result = enforce_min_section(candidates, h, min_section, fixed=fixed)
    # VLM 보정 경계가 여백 구간 안에 놓이면 색 경계·구간 끝과 그 사이가 여백뿐인 구간이 될 수 있다. 한 번 더 병합한다.
    # 색 경계는 지키고 VLM 경계를 지운다. 경계를 지우기만 하므로 최소 높이는 다시 적용하지 않는다.
    result = merge_empty_segments(result, std, blank_std, diag, diag_key="empty_merged_after_vlm", keep=set(fixed))
    if diag is not None:
        diag["vlm"] = vlm_records
        diag["boundaries"] = result
    return result


def crop_sections(src: SourceImage, im: Image.Image, boundaries: list[int], out_dir: Path) -> SplitResult:
    """세로 경계 목록(오름차순, 0과 height 제외)으로 섹션 이미지를 잘라 저장한다. 경계 결정 알고리즘과 분리된 공통 부분."""
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = im.size
    edges = [0, *boundaries, h]
    sections: list[Section] = []
    for i in range(len(edges) - 1):
        top, bottom = edges[i], edges[i + 1]
        if bottom <= top:
            raise ValueError(f"경계가 오름차순이 아니다: {edges}")
        key = section_key(src.source_image_id, i + 1)
        path = out_dir / f"{key}.png"
        im.crop((0, top, w, bottom)).save(path)
        sections.append(
            Section(
                section_key=key,
                source_image_id=src.source_image_id,
                section_order=i + 1,
                top_offset=top,
                height=bottom - top,
                width=w,
                image_path=str(path),
            )
        )
    return SplitResult(source_image_id=src.source_image_id, source_width=w, source_height=h, sections=sections)


def run(
    src: SourceImage,
    cfg: dict[str, Any],
    out_dir: Path,
    vlm: BoundaryPicker | None = None,
    diag: dict[str, Any] | None = None,
) -> SplitResult:
    """① 실행. `vlm`을 주면 기본 Gemini 호출자 대신 쓴다(테스트·실험용). 긴 구간이 없으면 VLM을 부르지 않는다.
    `diag`에 dict를 주면 경계 결정 진단을 채운다."""
    im = open_source(src)
    if diag is not None:
        diag["input"] = source_fingerprint(src.path)
        diag["vlm_config"] = vlm_context(cfg["section"])
    boundaries = decide_boundaries(im, cfg["section"], vlm, diag)
    return crop_sections(src, im, boundaries, out_dir)
