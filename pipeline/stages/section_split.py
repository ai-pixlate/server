"""① 섹션 분해 — `color_snap_vlm2` [pipeline.md 단계표 ①].

배경색 전환으로 자르고, 색이 안 바뀌는 긴 구간만 VLM이 경계를 고른다. 입도는 소제목 단위.
원본 1장 단위로 실행하며, 섹션 이미지를 out_dir에 잘라 저장하고 SplitResult를 돌려준다.

경계 결정 순서 (파라미터는 config `[section]`, 값은 open-questions #26 잠정):
1. 행 프로파일 — 행마다 배경색(픽셀 중앙값 RGB)과 색 표준편차를 구한다. 중앙값이라 글자가 있어도 배경이 남는다.
2. 색 전환 경계 — 행 색을 `color_window_px` 창으로 평활해 앞 창과 비교, RGB 거리 ≥ `color_delta`인 구간의
   최댓값 행을 후보로 잡는다. 확정 조건 둘: (a) 후보 앞뒤 `min_section_px` 폭의 행 색 **중앙값**이 `color_delta`
   이상 다르다(큰 제목 글자·사진 띠가 만드는 짧은 색 튐 제거) (b) 전환 행 앞뒤 중 한 행은 균일 행이다
   (표준편차 ≤ `blank_row_std`; 사진 내부의 색 변화는 배경 전환이 아니다). 경계 = 새 색의 첫 행.
3. 최소 높이 — 인접 경계 간격이 `min_section_px`보다 짧으면 뒤 경계를 버리고 앞 구간에 붙인다.
4. 긴 구간 VLM — 색 전환만으로 자른 구간이 `long_section_px`보다 길면 그 구간 이미지를 긴 변 `vlm_long_side_px`로
   줄이고 왼쪽에 y 눈금 띠를 붙여 VLM에 준다(프롬프트 `prompt_path`). 받은 y는 원본 좌표로 되돌린다.
5. 보정(snap) — VLM의 y를 반경 `snap_radius_px` 안에서 가장 가까운 균일 행(표준편차 ≤ `blank_row_std`, 여백)
   구간의 중앙으로 옮긴다. 여백 구간은 **현재 색 구간 안에서만** 찾는다(색 전환 경계 양쪽 여백이 합쳐지지 않게).
   반경 안에 여백이 없으면 후보를 버린다. 색 전환 경계와 합쳐 3을 다시 적용한다.

VLM 실패는 #25 미정 — `pipeline.vlm.VlmError`를 그대로 전파하고 대체 처리를 하지 않는다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from pipeline.errors import image_open_failed
from pipeline.types import Section, SourceImage, SplitResult, section_key
from pipeline.vlm import BoundaryPicker, GeminiBoundaryPicker

RULER_W = 48  # VLM 입력 왼쪽 눈금 띠 폭(px)
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


# ---- 2·3. 색 전환 경계 ------------------------------------------------------------
def color_boundaries(
    color: np.ndarray, std: np.ndarray, window: int, delta: float, min_section: int, blank_std: float
) -> list[int]:
    """행 색 프로파일에서 배경색 전환 행을 찾는다(오름차순, 0과 h 제외, 최소 높이 적용)."""
    h = len(color)
    if h < 2 * window or window < 1:
        return []
    cs = np.vstack([np.zeros((1, 3), dtype=np.float64), np.cumsum(color, axis=0, dtype=np.float64)])

    ys = np.arange(window, h - window + 1)  # 경계 y: 앞 창 [y-w, y) vs 뒤 창 [y, y+w)
    before = (cs[ys] - cs[ys - window]) / window
    after = (cs[ys + window] - cs[ys]) / window
    d = np.linalg.norm(after - before, axis=1)
    hot = d >= delta

    candidates: list[int] = []
    i = 0
    n = len(ys)
    while i < n:
        if not hot[i]:
            i += 1
            continue
        j = i
        while j < n and hot[j]:
            j += 1
        y = int(ys[i + int(np.argmax(d[i:j]))])
        a0, b1 = max(0, y - min_section), min(h, y + min_section)
        wide = np.linalg.norm(np.median(color[y:b1], axis=0) - np.median(color[a0:y], axis=0))
        uniform_edge = min(float(std[y - 1]), float(std[y])) <= blank_std
        if wide >= delta and uniform_edge:  # (a) 넓은 창 중앙값 (b) 전환 행 앞뒤 중 한 행은 균일
            candidates.append(y)
        i = j
    return enforce_min_section(candidates, h, min_section)


def enforce_min_section(candidates: list[int], height: int, min_section: int, fixed: list[int] = ()) -> list[int]:
    """`min_section`보다 짧은 구간을 만드는 경계를 버린다(뒤 경계를 버리고 앞 구간에 붙인다).

    `fixed`는 이미 확정된 경계(색 전환)로, 후보는 이들과도 간격을 지켜야 하며 결과에 함께 들어간다.
    """
    kept: list[int] = sorted(set(fixed))
    for y in sorted(set(candidates)):
        if y in kept:
            continue
        if any(abs(y - k) < min_section for k in kept):
            continue
        if y < min_section or height - y < min_section:
            continue
        kept.append(y)
        kept.sort()
    return kept


# ---- 5. 여백 행과 보정 ------------------------------------------------------------
def blank_runs(std: np.ndarray, std_max: float) -> list[tuple[int, int]]:
    """균일 행(여백)의 연속 구간 [start, end) 목록."""
    blank = std <= std_max
    runs: list[tuple[int, int]] = []
    i = 0
    n = len(blank)
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


# ---- 4. 긴 구간 VLM ---------------------------------------------------------------
def load_prompt(path: str | Path, width: int, height: int) -> str:
    text = Path(path).read_text(encoding="utf-8")
    return text.replace("{{WIDTH}}", str(width)).replace("{{HEIGHT}}", str(height))


def prepare_vlm_image(segment: Image.Image, long_side_px: int) -> tuple[Image.Image, float]:
    """긴 변을 long_side_px 이하로 줄이고 왼쪽에 y 눈금 띠를 붙인다. (이미지, scale=리사이즈/원본) 반환."""
    w, h = segment.size
    scale = min(1.0, long_side_px / max(w, h))
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


def vlm_boundaries(segment: Image.Image, sc: dict[str, Any], vlm: BoundaryPicker) -> list[int]:
    """긴 구간 하나에 VLM을 호출해 구간 로컬 원본 좌표의 경계 y 목록을 받는다."""
    img, scale = prepare_vlm_image(segment, int(sc["vlm_long_side_px"]))
    prompt = load_prompt(sc["prompt_path"], img.width - RULER_W, img.height)
    ys = vlm(img, prompt)
    h_resized = img.height
    return sorted({round(y / scale) for y in ys if 0 < y < h_resized})


def default_picker(sc: dict[str, Any]) -> BoundaryPicker:
    return GeminiBoundaryPicker(model=str(sc["vlm_model"]), temperature=float(sc["vlm_temperature"]))


# ---- 조립 -------------------------------------------------------------------------
def decide_boundaries(im: Image.Image, sc: dict[str, Any], vlm: BoundaryPicker | None = None) -> list[int]:
    """경계 목록(오름차순, 0과 height 제외)을 정한다. crop_sections()와 분리해 테스트한다."""
    window = int(sc["color_window_px"])
    delta = float(sc["color_delta"])
    min_section = int(sc["min_section_px"])
    long_px = int(sc["long_section_px"])
    radius = int(sc["snap_radius_px"])

    blank_std = float(sc["blank_row_std"])
    color, std = row_profile(im)
    fixed = color_boundaries(color, std, window, delta, min_section, blank_std)
    h = im.height
    edges = [0, *fixed, h]

    candidates: list[int] = []
    for top, bottom in zip(edges, edges[1:]):
        if bottom - top <= long_px:
            continue
        if vlm is None:
            vlm = default_picker(sc)
        # 여백 구간은 현재 색 구간 안에서만 찾는다. 원본 전체로 찾으면 색 전환 경계 양쪽 여백이 한 구간으로
        # 합쳐져 그 중앙(다른 색 구간)으로 보정된 뒤 폐기된다.
        runs = [(top + s, top + e) for s, e in blank_runs(std[top:bottom], blank_std)]
        for y in vlm_boundaries(im.crop((0, top, im.width, bottom)), sc, vlm):
            snapped = snap_to_blank(top + y, runs, radius)
            if snapped is not None and top < snapped < bottom:
                candidates.append(snapped)
    return enforce_min_section(candidates, h, min_section, fixed=fixed)


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


def run(src: SourceImage, cfg: dict[str, Any], out_dir: Path, vlm: BoundaryPicker | None = None) -> SplitResult:
    """① 실행. `vlm`을 주면 기본 Gemini 호출자 대신 쓴다(테스트·실험용). 긴 구간이 없으면 VLM을 부르지 않는다."""
    im = open_source(src)
    boundaries = decide_boundaries(im, cfg["section"], vlm)
    return crop_sections(src, im, boundaries, out_dir)
