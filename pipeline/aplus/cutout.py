"""A+ 이미지 칸 누끼 — 7칸 구성이 누끼를 요청한 사진의 배경을 지운다. 개발용 v1(사용자 결정 2026-11-24, 운영 계약 아님. open-questions.md #98).

순서(근거 시험: docs/ai-experiments/2026-11-23_aplus_cutout-trial.md):
1. 누끼할 사진은 7칸 구성이 정한다(`AplusImage.cutout`). 이 모듈은 그 판단을 바꾸지 않는다.
2. 글자를 지운 섹션 이미지(⑥ 결과)에서 자를 자리를 넓혀 자른다. 지운 이미지가 없으면 원본 섹션에서 자르고 확인 표시를 남긴다.
3. 전경 분리 모델로 알파를 얻는다. 경계 보정(alpha matting)은 쓰지 않는다.
4. 가장자리를 안쪽으로 줄이고, 떨어진 작은 조각을 지우고, 남은 대상에 맞춰 다시 자른 뒤 투명 여백을 두른다.
5. 실패(모델 호출 실패 · 남은 대상 없음)는 그 사진만 `failed`다 — 그 칸은 원본 크롭을 쓴다. 다른 사진은 계속한다.

하지 않는 것: 제품에 붙은 그래픽 · 소품 떼기(사람 확인), 슬롯 종횡비 패딩, 덩어리 고르기.
전경 분리 모델은 `Matting`으로 주입한다. 기본 어댑터 `RembgMatting`은 rembg를 함수 안에서만 불러오고 가중치를 내려받지 않는다 —
rembg가 없거나 가중치 파일이 없으면 `CutoutModelNotAvailable`이다(환경 문제라 예외로 올린다).
알파 정리(`shrink_edge` · `remove_fragments` · `subject_box`)는 numpy · Pillow만 쓴다(SciPy 없음) — 로컬 테스트 대상.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageFilter

from pipeline.aplus.label import Recorder, _emit
from pipeline.types import AplusCutout, AplusCutoutResult, AplusImage, AplusPlan, BBox, Section

TABLE = "aplus_cutout"
KEEP_DILATE_PX = 2  # 남긴 덩어리 둘레에서 이만큼까지의 반투명 가장자리는 남기고 그 밖의 알파는 지운다


class CutoutModelNotAvailable(RuntimeError):
    """전경 분리 모델을 쓸 수 없다 — rembg 미설치 · 모르는 모델 이름 · 가중치 파일 없음. 누끼 성공으로 표시하지 않고 예외로 올린다."""


class Matting(Protocol):
    """전경 분리 모델. RGB 이미지를 받아 같은 크기의 알파(L, 0 = 배경 · 255 = 대상)를 돌려준다."""

    config: dict[str, Any]

    def __call__(self, image: Image.Image) -> Image.Image: ...


class RembgMatting:
    """rembg 세션 어댑터(CPU). 경계 보정 없이 마스크만 받는다. 가중치는 rembg의 모델 폴더(`<REMBG_HOME 또는 ~/.rembg>/models/<모델>/`)에
    미리 있어야 한다 — 없으면 내려받지 않고 `CutoutModelNotAvailable`."""

    def __init__(self, model: str) -> None:
        try:
            import rembg  # noqa: PLC0415 — 누끼 환경에만 있다(pipeline/requirements-cutout.txt)
            from rembg.sessions import sessions_class  # noqa: PLC0415
        except ImportError as e:
            raise CutoutModelNotAvailable(f"rembg를 불러올 수 없다({e}) — pipeline/requirements-cutout.txt") from e
        cls = next((s for s in sessions_class if s.name() == model), None)
        if cls is None:
            raise CutoutModelNotAvailable(f"aplus_cutout.model={model!r} — rembg에 없는 모델 이름")
        weights = cls.resolve_existing(f"{model}.onnx")
        if weights is None:
            raise CutoutModelNotAvailable(f"가중치 {model}.onnx가 없다({cls.model_dir()}) — 이 어댑터는 내려받지 않는다(dev.md 6절)")
        self._remove = rembg.remove
        self._session = rembg.new_session(model)
        self.config = {"adapter": "rembg", "rembg_version": rembg.__version__, "model": model, "weights": str(weights),
                       "alpha_matting": False, "providers": list(self._session.inner_session.get_providers())}

    def __call__(self, image: Image.Image) -> Image.Image:
        return self._remove(image.convert("RGB"), session=self._session, only_mask=True).convert("L")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _integer(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """[aplus_cutout] 설정 검증. 통과하면 그 표를 돌려준다. 어기면 ValueError."""
    if TABLE not in cfg:
        raise ValueError(f"config에 [{TABLE}] 표가 없다")
    c = cfg[TABLE]
    keys = ("model", "margin_ratio", "alpha_threshold", "edge_shrink_px", "min_fragment_ratio", "pad_ratio", "edge_touch_ratio")
    missing = [k for k in keys if k not in c]
    if missing:
        raise ValueError(f"잘못된 [{TABLE}] 설정: 없는 키 " + ", ".join(f"{TABLE}.{k}" for k in missing))
    errors: list[str] = []
    if not isinstance(c["model"], str) or not c["model"].strip():
        errors.append(f"{TABLE}.model={c['model']!r} — 비어 있지 않은 문자열")
    for k, lo, hi in (("margin_ratio", 0, 1), ("min_fragment_ratio", 0, 1), ("pad_ratio", 0, 1), ("edge_touch_ratio", 0, 1)):
        if not _number(c[k]) or not lo <= c[k] <= hi:
            errors.append(f"{TABLE}.{k}={c[k]!r} — 수치(bool 제외) · {lo} 이상 {hi} 이하")
    if not _integer(c["alpha_threshold"]) or not 1 <= c["alpha_threshold"] <= 255:
        errors.append(f"{TABLE}.alpha_threshold={c['alpha_threshold']!r} — 정수 · 1 이상 255 이하")
    if not _integer(c["edge_shrink_px"]) or not 0 <= c["edge_shrink_px"] <= 16:
        errors.append(f"{TABLE}.edge_shrink_px={c['edge_shrink_px']!r} — 정수 · 0 이상 16 이하")
    if errors:
        raise ValueError(f"잘못된 [{TABLE}] 설정: " + "; ".join(errors))
    return c


# ---------------------------------------------------------------------------
# 자를 자리 · 알파 정리 (numpy · Pillow만)
# ---------------------------------------------------------------------------
def widen(b: BBox, margin_ratio: float, width: int, height: int) -> BBox:
    """자를 자리를 가로 · 세로 각각 양쪽으로 `margin_ratio`만큼 넓힌다. 섹션 밖으로는 나가지 않는다."""
    mx, my = round(b.w * margin_ratio), round(b.h * margin_ratio)
    x0, y0, x1, y1 = max(0, b.x - mx), max(0, b.y - my), min(width, b.x2 + mx), min(height, b.y2 + my)
    return BBox(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def shrink_edge(alpha: np.ndarray, px: int) -> np.ndarray:
    """알파를 `px`픽셀 안쪽으로 줄인다(둘레 (2·px+1)² 창의 최솟값). 조각 바깥은 가장자리 값이 이어진다고 본다 —
    자른 자리에서 잘린 대상을 그 변에서 더 깎지 않는다."""
    if px <= 0:
        return alpha
    padded = np.pad(alpha, px, mode="edge")
    out = np.asarray(Image.fromarray(padded).filter(ImageFilter.MinFilter(2 * px + 1)))
    return out[px:-px, px:-px].copy()


def remove_fragments(mask: np.ndarray, min_ratio: float) -> tuple[np.ndarray, int, int]:
    """떨어진 작은 조각 지우기. `mask`(bool)의 덩어리(8방향 연결) 가운데 넓이가 전체의 `min_ratio`보다 작은 것을 뺀다.
    가장 큰 덩어리는 크기와 관계없이 남긴다. 돌려주는 값: (남긴 마스크, 남긴 덩어리 수, 지운 덩어리 수).
    행마다 이어진 구간을 구해 위 행의 구간과 묶는다(SciPy 없이 — 구간 수만큼만 돈다)."""
    h, w = mask.shape
    edges = np.diff(np.pad(mask.astype(np.int8), ((0, 0), (1, 1))), axis=1)
    sr, sc = np.nonzero(edges == 1)   # 구간 시작(행, 열) — 행 순서, 같은 행 안에서는 열 순서
    _, ec = np.nonzero(edges == -1)   # 구간 끝(다음 열). 시작과 같은 순서 · 같은 개수
    n = len(sr)
    if n == 0:
        return np.zeros_like(mask, dtype=bool), 0, 0
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    bounds = np.searchsorted(sr, np.arange(h + 1))  # 행 r의 구간 = [bounds[r], bounds[r + 1])
    starts, ends = sc.tolist(), ec.tolist()
    for r in range(1, h):
        a, a_end, b, b_end = int(bounds[r - 1]), int(bounds[r]), int(bounds[r]), int(bounds[r + 1])
        while a < a_end and b < b_end:
            if starts[a] <= ends[b] and starts[b] <= ends[a]:  # 대각선으로 닿는 것까지 이어진 것으로 본다
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
            if ends[a] < ends[b]:
                a += 1
            else:
                b += 1
    roots = np.fromiter((find(i) for i in range(n)), dtype=np.int64, count=n)
    lengths = ec - sc
    area = np.bincount(roots, weights=lengths, minlength=n)
    comp = np.flatnonzero(area > 0)
    keep = {int(i) for i in comp if area[i] >= min_ratio * h * w}
    keep.add(int(comp[np.argmax(area[comp])]))
    out = np.zeros_like(mask, dtype=bool)
    rows = sr.tolist()
    for i in range(n):
        if int(roots[i]) in keep:
            out[rows[i], starts[i]:ends[i]] = True
    return out, len(keep), len(comp) - len(keep)


def clean_alpha(alpha: np.ndarray, *, threshold: int, edge_shrink_px: int, min_fragment_ratio: float) -> tuple[np.ndarray, dict[str, Any]]:
    """모델 알파(uint8) → 정리한 알파. 가장자리를 줄이고, 작은 조각을 지우고, 남긴 덩어리 둘레 밖의 알파를 0으로 한다."""
    a = shrink_edge(alpha, edge_shrink_px)
    kept, n_kept, n_removed = remove_fragments(a >= threshold, min_fragment_ratio)
    stats = {"components_kept": n_kept, "components_removed": n_removed, "foreground_ratio": round(float(kept.mean()), 4)}
    if n_kept == 0:
        return np.zeros_like(a), stats
    near = np.asarray(Image.fromarray(np.pad(kept, KEEP_DILATE_PX).astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(2 * KEEP_DILATE_PX + 1)))
    near = near[KEEP_DILATE_PX:-KEEP_DILATE_PX, KEEP_DILATE_PX:-KEEP_DILATE_PX] > 0
    return np.where(near, a, 0).astype(np.uint8), stats


def subject_box(alpha: np.ndarray) -> tuple[int, int, int, int] | None:
    """알파가 0보다 큰 곳을 감싸는 상자 (x0, y0, x1, y1). 없으면 None."""
    ys, xs = np.nonzero(alpha)
    if len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def touching_sides(alpha: np.ndarray, threshold: int, ratio: float) -> list[str]:
    """대상이 조각의 변을 `ratio` 넘게 차지하는 변 이름 — 자른 자리에서 대상이 잘렸을 수 있다는 표시에 쓴다."""
    fg = alpha >= threshold
    sides = (("위", fg[0]), ("아래", fg[-1]), ("왼쪽", fg[:, 0]), ("오른쪽", fg[:, -1]))
    return [name for name, line in sides if float(line.mean()) > ratio]


def cut(rgb: Image.Image, alpha: np.ndarray, pad_ratio: float) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
    """조각(RGB)과 정리한 알파 → 대상에 맞춰 다시 자르고 투명 여백을 두른 RGBA, 조각 안의 대상 상자. 대상이 없으면 None."""
    box = subject_box(alpha)
    if box is None:
        return None
    rgba = rgb.convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha))
    tight = rgba.crop(box)
    pad = round(max(tight.size) * pad_ratio)
    out = Image.new("RGBA", (tight.width + 2 * pad, tight.height + 2 * pad), (0, 0, 0, 0))
    out.paste(tight, (pad, pad))
    return out, box


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def targets(plan: AplusPlan) -> list[tuple[int, str, AplusImage]]:
    """누끼를 요청한 이미지 칸 — (칸 번호, 칸 이름, 이미지). 칸 순서 → 큰 사진 · 마크 · 항목 순."""
    out: list[tuple[int, str, AplusImage]] = []
    for s in plan.slots or []:
        m = s.module
        if m is None:
            continue
        cands = [("image", m.image), ("mark", m.mark)] + [(f"items[{i}].image", it.image) for i, it in enumerate(m.items)]
        out += [(s.slot, name, img) for name, img in cands if img is not None and img.cutout]
    return out


def _file_name(slot: int, field: str) -> str:
    return f"slot{slot}_{field.replace('[', '').replace('].image', '')}.png"  # slot5_items0.png


def run(plan: AplusPlan, sections: list[Section], erased_images: Mapping[str, str | Path], out_dir: str | Path, cfg: dict[str, Any], *,
        matting: Matting | None = None, recorder: Recorder | None = None) -> AplusCutoutResult:
    """상품 하나의 누끼. `plan`은 성공한 7칸 구성, `sections`는 그 섹션들, `erased_images`는 section_key → 글자를 지운 섹션 이미지 경로
    (없는 섹션은 원본에서 자른다). 결과 PNG는 `out_dir`에 쓴다(같은 이름이 이미 있으면 덮어쓰지 않고 입력 오류).
    누끼 요청이 하나도 없으면 모델을 만들지 않는다. 입력 오류 · 모델을 쓸 수 없는 환경은 예외, 사진별 실패는 그 사진의 `failed`다."""
    c = validate_config(cfg)
    if plan.status != "ok":
        raise ValueError(f"7칸 구성이 실패 상태다: {plan.error}")
    by_key = {s.section_key: s for s in sections}
    todo = targets(plan)
    missing = sorted({img.section_key for _, _, img in todo if img.section_key not in by_key})
    if missing:
        raise ValueError(f"누끼할 사진의 섹션이 sections에 없다: {missing}")
    out = Path(out_dir)
    clash = [n for n in (_file_name(slot, f) for slot, f, _ in todo) if (out / n).exists()]
    if clash:
        raise ValueError(f"출력 폴더에 같은 이름의 파일이 이미 있다: {clash}")
    rec: dict[str, Any] = {"stage": TABLE, "config": dict(c), "matting": None, "items": []}
    if not todo:
        _emit(recorder, rec, failed=False)
        return AplusCutoutResult()
    model = matting if matting is not None else RembgMatting(c["model"])
    rec["matting"] = dict(model.config)
    out.mkdir(parents=True, exist_ok=True)
    done: dict[tuple[str, int, int, int, int], AplusCutout] = {}  # 같은 자리를 두 칸이 쓰면(사진 다시 쓰기) 한 번만 누끼한다
    items: list[AplusCutout] = []
    for slot, field, img in todo:
        same = done.get((img.section_key, img.bbox.x, img.bbox.y, img.bbox.w, img.bbox.h))
        if same is not None:
            items.append(same.model_copy(update={"slot": slot, "field": field}))
            rec["items"].append({"slot": slot, "field": field, "reused": f"칸 {same.slot} {same.field}"})
            continue
        s = by_key[img.section_key]
        t0 = time.perf_counter()
        flags: list[str] = []
        erased = erased_images.get(img.section_key)
        source = "erased" if erased is not None else "original"
        if erased is None:
            flags.append("[확인] 글자를 지운 섹션 이미지가 없어 원본에서 자름 — 글자가 함께 남았을 수 있음")
        with Image.open(erased if erased is not None else s.image_path) as im:
            if im.size != (s.width, s.height):
                raise ValueError(f"{img.section_key}: {source} 이미지 크기 {im.size}가 섹션 크기 {(s.width, s.height)}와 다르다")
            if img.bbox.x < 0 or img.bbox.y < 0 or img.bbox.x2 > s.width or img.bbox.y2 > s.height:
                raise ValueError(f"{img.section_key}: 자를 자리 {img.bbox}가 섹션 밖이다")
            wide = widen(img.bbox, c["margin_ratio"], s.width, s.height)
            piece = im.convert("RGB").crop((wide.x, wide.y, wide.x2, wide.y2))
        info: dict[str, Any] = {"slot": slot, "field": field, "section_key": img.section_key, "source": source,
                                "crop_bbox": wide.model_dump(), "piece_size": list(piece.size)}
        result: AplusCutout
        try:
            raw = model(piece)
            if raw.size != piece.size:
                raise ValueError(f"모델이 돌려준 알파 크기 {raw.size}가 조각 크기 {piece.size}와 다르다")
            alpha, stats = clean_alpha(np.asarray(raw.convert("L")), threshold=c["alpha_threshold"], edge_shrink_px=c["edge_shrink_px"],
                                       min_fragment_ratio=c["min_fragment_ratio"])
            info.update(stats)
            made = cut(piece, alpha, c["pad_ratio"])
            if made is None:
                raise ValueError("남은 대상이 없다")
        except Exception as e:  # noqa: BLE001 — 사진 하나의 실패로 나머지를 멈추지 않는다
            info.update(status="failed", error=f"{e.__class__.__name__}: {e}", seconds=round(time.perf_counter() - t0, 3))
            result = AplusCutout(slot=slot, field=field, section_key=img.section_key, status="failed", source=source, crop_bbox=wide,
                                 flags=flags, error=info["error"])
        else:
            png, (x0, y0, x1, y1) = made
            sides = touching_sides(alpha, c["alpha_threshold"], c["edge_touch_ratio"])
            if sides:
                flags.append(f"[확인] 대상이 자른 자리의 {' · '.join(sides)} 변에 닿음 — 잘렸을 수 있음")
            path = out / _file_name(slot, field)
            png.save(path)
            info.update(status="ok", image=path.name, size=list(png.size), touching=sides, seconds=round(time.perf_counter() - t0, 3))
            result = AplusCutout(slot=slot, field=field, section_key=img.section_key, status="ok", source=source, crop_bbox=wide,
                                 subject_bbox=BBox(x=wide.x + x0, y=wide.y + y0, w=x1 - x0, h=y1 - y0), image_path=str(path),
                                 width=png.width, height=png.height, flags=flags)
        done[(img.section_key, img.bbox.x, img.bbox.y, img.bbox.w, img.bbox.h)] = result
        items.append(result)
        rec["items"].append(info)
    _emit(recorder, rec, failed=False)
    return AplusCutoutResult(items=items, matting_config=dict(model.config))
