"""⑦ 스타일 추출 — 단독 개발 v1, 핵심 계산(2026-10-06). pipeline.md 단계표 ⑦, contract.md 1.3 · 2장.

정본(확정): `otsu_border` — 테두리에 많이 닿는 쪽을 배경으로. 크기 = bbox 높이 → em ×1.35. 정렬 = 블록 안 줄들의 좌 · 중앙 · 우 분산 비교,
허용 오차 블록 폭 12%. **원본 섹션 이미지** 사용(⑥ 출력 아님). 폰트 패밀리 · 굵기는 추출하지 않는다[개발계획 2.1, 4.1, 6장].
라벨 · 로고 블록은 스타일 추출에서 제외한다[계약 1.3].

단독 개발 v1 명세(사용자 승인 2026-10-06 — 운영 text_block.style 계약(#14) · BE 연동 계약이 아니다):
- 처리 대상 = ④ `is_product_label=false` 그리고 ⑤ `is_brand_logo=false`. ④ true → ⑤ product_label/null(정상 생략)이고 제외 사유
  product_label, ④ false · ⑤ true → brand_logo. 미판정 · 누락 · 실패를 false로 바꾸지 않는다(섹션 입력 오류).
- 공백만 있는 영역(`text.strip() == ""`)은 추출 생략(blank_text). 저신뢰지만 텍스트가 있는 영역은 추출한다 — ⑥ score_min을 쓰지 않는다.
- 영역 색: bbox를 여백 없이 crop → 명도 Y = (299R + 587G + 114B + 500) // 1000 → 256구간 Otsu(t = 0~254, A = Y ≤ t, B = Y > t,
  두 집단이 모두 비지 않은 t만, 집단 간 분산 ∝ (S_A·n_B − S_B·n_A)² / (n_A·n_B)를 Python 정수 교차곱으로 비교, 최댓값 동률이면 가장 작은 t)
  → crop 외곽 픽셀(중복 없이, 폭 또는 높이 1이면 전체)에 더 많이 닿는 집단이 배경 → 집단별 RGB 채널 중앙값(짝수면 가운데 두 값의 평균)을
  floor(m + 0.5)로 정수화해 "#RRGGBB". 두 집단으로 나눌 수 없으면 single_class, 외곽 접촉 수가 같으면 border_tie — 색만 None.
  저대비 · 그라데이션용 추가 임계값은 없다.
- 영역 크기: est_font_px = float(bbox.h × Fraction(str(em_ratio))), 정수 반올림 없음.
- 블록 대표: 크기 = 유효 영역 크기의 중앙값(짝수면 가운데 두 값의 평균). 글자색 · 배경색은 독립 집계 — 유효 영역 정수 RGB의 채널별
  중앙값(반올림하지 않음)에 sRGB 유클리드 제곱 거리가 가장 가까운 실제 영역 색, 동률은 source_lines의 영역 순서. 가중치 · Lab 없음.
- 정렬: 공백 영역만 있는 줄은 빼고, 나머지 줄은 비공백 영역 bbox 합집합의 왼쪽 x · 중앙 x + w/2 · 오른쪽 x + w. 모분산(ddof=0)과
  (블록 bbox.w × Fraction(str(align_tolerance)))²를 Fraction으로 정확히 비교. 유효 줄 0개 → None(no_text_lines), 1개 → left
  (default_single_line), 여러 줄 → 후보 중 분산 최소(estimated), 최솟값 동률 → left(default_tie, left가 후보 밖이어도), 후보 없음 → None
  (no_candidate). std 실수는 진단 기록일 뿐 판단에 쓰지 않는다.
- 섹션 입력 오류(설정 · 식별자(섹션 안 block_key · line_key · region_key 중복 포함) · 판정 · 지문 · 이미지 크기 · 경계 밖 bbox ·
  측정 대상의 면적 0 bbox · bbox ≠ poly 외접 사각형 · 설정 × 실제 높이 · 폭이 float 범위 초과)는
  결과 없이 StyleInputError — 문제를 모아 한 번에 보고한다. bbox를 자르거나 보정하지 않는다.

extract는 파일을 읽거나 쓰지 않는다. run은 섹션 이미지 읽기만 더한다(RGB 모드만, 변환 없음). 입력 객체는 바꾸지 않는다.
배치에서 섹션별로 계속 처리할지는 후속 실행 계층이 정한다.
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

import numpy as np

from pipeline.stages.label import input_fingerprint
from pipeline.stages.logo import sha256_json
from pipeline.types import (
    ALIGN_VALUES,
    BBox,
    BlockStyle,
    LabelResult,
    LogoResult,
    MergeResult,
    RegionStyle,
    Section,
    StyleAlignDiag,
    StyleConfigSnapshot,
    StyleCounts,
    StyleFingerprints,
    StyleOtsuDiag,
    StyleResult,
    TextBlock,
)

METHODS = ("otsu_border",)
LUMA_RULE = "Y = (299R + 587G + 114B + 500) // 1000"  # BT.601 정수식, 반올림


class StyleInputError(ValueError):
    """⑦ 섹션 입력 · 설정 오류. 결과를 만들지 않는다. 정상 입력에서의 색 추출 불가는 이 오류가 아니라 결과에 기록한다."""


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_config(cfg: dict[str, Any]) -> None:
    """[style] 설정 검증. 기본값은 config가 정본이며 여기서 채우지 않는다. 어기면 StyleInputError."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("style"), dict):
        raise StyleInputError("config에 [style] 표가 없다")
    st = cfg["style"]
    missing = [k for k in ("method", "em_ratio", "align_tolerance") if k not in st]
    if missing:
        raise StyleInputError("잘못된 [style] 설정: 없는 키 " + ", ".join(f"style.{k}" for k in missing))
    errors: list[str] = []
    if st["method"] not in METHODS:
        errors.append(f"style.method={st['method']!r} — {METHODS} 중 하나")
    if not (_is_number(st["em_ratio"]) and st["em_ratio"] > 0):
        errors.append(f"style.em_ratio={st['em_ratio']!r} — 0보다 큰 유한 실수(bool 불가)")
    if not (_is_number(st["align_tolerance"]) and st["align_tolerance"] >= 0):
        errors.append(f"style.align_tolerance={st['align_tolerance']!r} — 0 이상 유한 실수(bool 불가)")
    if errors:
        raise StyleInputError("잘못된 [style] 설정: " + "; ".join(errors))


def exact_ratio(value: float) -> Fraction:
    """설정 값의 10진 표기를 정확한 분수로(1.35 → 27/20). 부동소수 곱셈 오차(20 × 1.35 = 27.000000000000004)를 피한다."""
    return Fraction(str(value))


# ---------------------------------------------------------------------------
# 입력 검증
# ---------------------------------------------------------------------------
def _dups(keys: list[str]) -> list[str]:
    return sorted({k for k in keys if keys.count(k) > 1})


def _bbox_oob(b: BBox, width: int, height: int) -> bool:
    return b.x < 0 or b.y < 0 or b.x2 > width or b.y2 > height


def _is_blank(text: str) -> bool:
    return not text.strip()


def _float_representable(value: Fraction) -> bool:
    """정확한 값이 유한 float로 변환되는지. float(Fraction)은 범위를 넘으면 OverflowError를 낸다 — 이 변환 하나만 확인한다."""
    try:
        float(value)
    except OverflowError:
        return False
    return True


def validate_inputs(
    image_id: str, section: Section, image_size: tuple[int, int], merged: MergeResult, label: LabelResult, logo: LogoResult,
    logo_record: dict[str, Any], cfg: dict[str, Any],
) -> tuple[dict[str, str | None], StyleFingerprints]:
    """설정 · 식별자 · 판정 · 지문 · 기하를 검증한다. image_size = 실제 섹션 이미지 (W, H).
    반환: (block_key → 제외 사유(product_label · brand_logo) 또는 None, 입력 지문). 어기면 StyleInputError(문제를 모아 한 번에).
    앞 단계 검사가 실패하면 그 결과에 기대는 뒤 검사는 하지 않는다."""
    validate_config(cfg)
    if not isinstance(image_id, str) or not image_id:
        raise StyleInputError(f"원본 이미지 식별자가 비어 있다: {image_id!r}")
    key = section.section_key
    W, H = section.width, section.height
    where = f"{image_id}/{key}"
    if tuple(image_size) != (W, H):
        raise StyleInputError(f"{where}: 섹션 이미지 크기 {tuple(image_size)} ≠ 섹션 메타데이터 ({W}, {H})")

    problems: list[str] = []
    # 식별자 대응: 원본 식별자 + section_key + block_key (+ 섹션 안 line_key · region_key — 제외 블록 포함, dev.md 3절)
    for name, k in (("③", merged.section_key), ("④", label.section_key), ("⑤", logo.section_key)):
        if k != key:
            problems.append(f"{name} section_key {k} ≠ 섹션 {key}")
    if logo.image_id != image_id:
        problems.append(f"⑤ image_id {logo.image_id} ≠ {image_id}")
    other = [b.block_key for b in merged.blocks if b.section_key != key]
    if other:
        problems.append(f"③ 블록의 section_key가 섹션과 다르다 {other}")
    bkeys = [b.block_key for b in merged.blocks]
    if _dups(bkeys):
        problems.append(f"③ block_key 중복 {_dups(bkeys)}")
    lkeys = [ln.line_key for b in merged.blocks for ln in b.source_lines]
    if _dups(lkeys):
        problems.append(f"line_key 중복 {_dups(lkeys)}")
    rkeys =[r.region_key for b in merged.blocks for ln in b.source_lines for r in ln.regions]
    if _dups(rkeys):
        problems.append(f"원시 region_key 중복 {_dups(rkeys)}")
    if problems:
        raise StyleInputError(f"{where}: " + "; ".join(problems))

    # ④⑤ 판정: 둘 다 ok · 블록과 일대일. 미판정 · 누락 · 실패를 false로 바꾸지 않는다
    if label.status != "ok" or label.labels is None:
        raise StyleInputError(f"{where}: ④ 결과가 ok가 아니다(status={label.status}) — 미판정을 false로 바꾸지 않는다")
    if logo.status != "ok" or logo.decisions is None:
        raise StyleInputError(f"{where}: ⑤ 결과가 ok가 아니다(status={logo.status}) — 미판정을 false로 바꾸지 않는다")
    for name, keys in (("④", [d.block_key for d in label.labels]), ("⑤", [d.block_key for d in logo.decisions])):
        if _dups(keys):
            problems.append(f"{name} 판정 중복 {_dups(keys)}")
        if sorted(set(bkeys) - set(keys)):
            problems.append(f"{name} 판정 누락 {sorted(set(bkeys) - set(keys))}")
        if sorted(set(keys) - set(bkeys)):
            problems.append(f"③에 없는 블록의 {name} 판정 {sorted(set(keys) - set(bkeys))}")
    if problems:
        raise StyleInputError(f"{where}: " + "; ".join(problems))
    by_label = {d.block_key: d.is_product_label for d in label.labels}
    by_logo = {d.block_key: d for d in logo.decisions}
    excluded: dict[str, str | None] = {}
    for bk in bkeys:
        is_label, lg = by_label[bk], by_logo[bk]
        if type(is_label) is not bool:
            problems.append(f"{bk}: ④ 값이 boolean이 아니다({is_label!r})")
        elif is_label:
            if not (lg.is_brand_logo is None and lg.basis == "product_label"):
                problems.append(f"{bk}: ④ true인데 ⑤가 생략(product_label/null)이 아니다({lg.basis}/{lg.is_brand_logo})")
            excluded[bk] = "product_label"
        elif type(lg.is_brand_logo) is not bool:
            problems.append(f"{bk}: ④ false인데 ⑤ 값이 boolean이 아니다({lg.basis}/{lg.is_brand_logo}) — 미판정을 false로 바꾸지 않는다")
        else:
            excluded[bk] = "brand_logo" if lg.is_brand_logo else None
    if problems:
        raise StyleInputError(f"{where}: " + "; ".join(problems))

    # 지문: ④는 현재 ③ 블록으로, ⑤는 현재 ③ 블록 · ④ 결과로 판정했어야 한다(⑥과 같은 대조)
    fp_blocks = input_fingerprint(merged.blocks)
    fp_label = sha256_json(label.model_dump(mode="json"))
    fp_logo = sha256_json(logo.model_dump(mode="json"))
    if label.checked.input_fingerprint != fp_blocks:
        problems.append(f"④ 입력 지문 {label.checked.input_fingerprint[:12]}… ≠ 현재 ③ 블록 {fp_blocks[:12]}…")
    if not isinstance(logo_record, dict):
        raise StyleInputError(f"{where}: " + "; ".join(problems + ["⑤ 기록(logo_debug)이 객체가 아니다"]))
    if logo_record.get("status") != "ok" or logo_record.get("image_id") != image_id or logo_record.get("section_key") != key:
        problems.append(f"⑤ 기록의 상태 · 식별자 불일치(status={logo_record.get('status')!r} · "
                        f"{logo_record.get('image_id')!r}/{logo_record.get('section_key')!r})")
    if logo_record.get("blocks_fingerprint") != fp_blocks:
        problems.append(f"⑤ 기록의 블록 지문 ≠ 현재 ③ 블록 {fp_blocks[:12]}…")
    if logo_record.get("label_fingerprint") != fp_label:
        problems.append(f"⑤ 기록의 ④ 결과 지문 ≠ 현재 ④ 결과 {fp_label[:12]}…")
    rows = logo_record.get("decisions")
    want = [(d.block_key, d.is_brand_logo, d.basis) for d in logo.decisions]
    got = [(r.get("block_key"), r.get("is_brand_logo"), r.get("basis")) for r in rows] if isinstance(rows, list) and all(
        isinstance(r, dict) for r in rows) else None
    if got != want:
        problems.append("⑤ 기록의 판정 목록 ≠ ⑤ 결과")
    if problems:
        raise StyleInputError(f"{where}: " + "; ".join(problems))

    # 기하: 경계 밖은 모든 블록 · 줄 · 영역 bbox(제외 블록 포함). bbox = poly 외접 사각형은 모든 영역.
    # 면적 0은 측정 대상(처리 대상 블록의 비공백 영역)만 — 생략하는 공백 영역 · 제외 대상은 면적 0만으로 실패시키지 않는다
    for b in merged.blocks:
        if _bbox_oob(b.bbox, W, H):
            problems.append(f"{b.block_key}: 블록 bbox {b.bbox.model_dump()}가 영상 [0,{W}]×[0,{H}] 밖")
        for ln in b.source_lines:
            if _bbox_oob(ln.bbox, W, H):
                problems.append(f"{b.block_key}/{ln.line_key}: 줄 bbox {ln.bbox.model_dump()}가 영상 [0,{W}]×[0,{H}] 밖")
            for r in ln.regions:
                if _bbox_oob(r.bbox, W, H):
                    problems.append(f"{b.block_key}/{r.region_key}: 영역 bbox {r.bbox.model_dump()}가 영상 [0,{W}]×[0,{H}] 밖 — "
                                    "자르거나 보정하지 않는다")
                if r.bbox != BBox.from_poly(list(r.poly)):
                    problems.append(f"{b.block_key}/{r.region_key}: bbox {r.bbox.model_dump()} ≠ poly 외접 사각형 — 글자 높이의 근거가 모호하다")
                if excluded[b.block_key] is None and not _is_blank(r.text) and (r.bbox.w == 0 or r.bbox.h == 0):
                    problems.append(f"{b.block_key}/{r.region_key}: 측정 대상 영역 bbox의 면적이 0 {r.bbox.model_dump()}")
    if problems:
        raise StyleInputError(f"{where}: " + "; ".join(problems))

    # 설정 × 실제 크기가 결과의 float로 표현되는지(유한 설정값이라도 곱이 float 범위를 넘을 수 있다). 상한을 새로 두지 않고 표현 가능 여부만 본다.
    # 크기 = 측정 대상 영역 높이 × em_ratio(블록 중앙값은 영역 값 사이라 따로 보지 않는다), 허용 오차 = 비제외 블록 폭 × align_tolerance
    st = cfg["style"]
    em, tol = exact_ratio(st["em_ratio"]), exact_ratio(st["align_tolerance"])
    for b in merged.blocks:
        if excluded[b.block_key] is not None:
            continue
        if not _float_representable(b.bbox.w * tol):
            problems.append(f"{b.block_key}: 블록 폭 {b.bbox.w} × style.align_tolerance={st['align_tolerance']!r}가 float 범위를 넘는다")
        for ln in b.source_lines:
            for r in ln.regions:
                if not _is_blank(r.text) and not _float_representable(r.bbox.h * em):
                    problems.append(f"{b.block_key}/{r.region_key}: 영역 높이 {r.bbox.h} × style.em_ratio={st['em_ratio']!r}가 float 범위를 넘는다")
    if problems:
        raise StyleInputError(f"{where}: " + "; ".join(problems))
    fps =StyleFingerprints(blocks=fp_blocks, label=fp_label, logo=fp_logo, logo_record=sha256_json(logo_record))
    return excluded, fps


# ---------------------------------------------------------------------------
# 영역 색 · 크기
# ---------------------------------------------------------------------------
def luma(rgb: np.ndarray) -> np.ndarray:
    """(…, 3) uint8 RGB → 명도 정수 0~255. uint8 상태로 가중합하지 않고 int64로 바꿔 계산한다."""
    c = rgb.astype(np.int64)
    return (299 * c[..., 0] + 587 * c[..., 1] + 114 * c[..., 2] + 500) // 1000


def otsu_threshold(y: np.ndarray) -> int | None:
    """256구간 Otsu 임계값 t(0~254). A = Y ≤ t, B = Y > t. 두 집단이 모두 비지 않는 t만 후보이고, 후보가 없으면 None.
    집단 간 분산 ∝ (S_A·n_B − S_B·n_A)² / (n_A·n_B) — Python 정수 교차곱으로 비교, 최댓값 동률이면 가장 작은 t."""
    hist = np.bincount(y.ravel(), minlength=256)
    counts = [int(v) for v in hist]
    n_total = sum(counts)
    s_total = sum(i * c for i, c in enumerate(counts))
    best_t: int | None = None
    best_num, best_den = 0, 1
    n_a = s_a = 0
    for t in range(255):
        n_a += counts[t]
        s_a += t * counts[t]
        n_b, s_b = n_total - n_a, s_total - s_a
        if n_a == 0 or n_b == 0:
            continue
        num, den = (s_a * n_b - s_b * n_a) ** 2, n_a * n_b
        if best_t is None or num * best_den > best_num * den:  # 엄격히 클 때만 갱신 → 동률이면 먼저(작은 t)
            best_t, best_num, best_den = t, num, den
    return best_t


def border_mask(h: int, w: int) -> np.ndarray:
    """crop 외곽 픽셀(각 픽셀 한 번). 폭 또는 높이가 1이면 crop 전체."""
    m = np.zeros((h, w), dtype=bool)
    m[0, :] = m[-1, :] = True
    m[:, 0] = m[:, -1] = True
    return m


def median2(values: np.ndarray) -> int:
    """정수 값들의 중앙값 × 2(정확). 홀수 개면 가운데 값 × 2, 짝수 개면 가운데 두 값의 합."""
    v = np.sort(values.astype(np.int64))
    n = len(v)
    return int(v[n // 2]) * 2 if n % 2 else int(v[n // 2 - 1]) + int(v[n // 2])


def half_up(doubled: int) -> int:
    """중앙값 m = doubled / 2를 floor(m + 0.5)로 정수화 = floor((doubled + 1) / 2)."""
    return (doubled + 1) // 2


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def class_color(pixels: np.ndarray) -> tuple[int, int, int]:
    """(n, 3) 픽셀의 채널별 중앙값(짝수면 가운데 두 값의 평균)을 half-up으로 정수화한 RGB."""
    return tuple(half_up(median2(pixels[:, ch])) for ch in range(3))  # type: ignore[return-value]


def region_colors(crop: np.ndarray) -> tuple[tuple[int, int, int] | None, tuple[int, int, int] | None, str | None, StyleOtsuDiag | None]:
    """crop (h, w, 3) uint8 RGB → (글자색, 배경색, color_error, Otsu 진단). 실패하면 두 색 None."""
    h, w = crop.shape[:2]
    y = luma(crop)
    t = otsu_threshold(y)
    if t is None:
        return None, None, "single_class", None
    dark = y <= t
    border = border_mask(h, w)
    border_px = int(border.sum())
    border_dark = int((dark & border).sum())
    diag = StyleOtsuDiag(threshold=t, dark_px=int(dark.sum()), light_px=int((~dark).sum()), border_px=border_px,
                         border_dark=border_dark, border_light=border_px - border_dark)
    if diag.border_dark == diag.border_light:
        return None, None, "border_tie", diag
    bg_is_dark = diag.border_dark > diag.border_light
    fg_mask = ~dark if bg_is_dark else dark
    return class_color(crop[fg_mask]), class_color(crop[~fg_mask]), None, diag


def est_font_size(h: int, em_ratio: float) -> Fraction:
    """영역 글자 크기 = bbox 높이 × em_ratio(정확한 분수). 결과에는 float로 기록하며 정수로 반올림하지 않는다."""
    return h * exact_ratio(em_ratio)


# ---------------------------------------------------------------------------
# 블록 집계 · 정렬
# ---------------------------------------------------------------------------
def median_fraction(values: list[Fraction]) -> Fraction:
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def representative_color(colors: list[tuple[int, int, int]]) -> tuple[int, int, int] | None:
    """채널별 중앙값(반올림하지 않음)에 sRGB 유클리드 제곱 거리가 가장 가까운 실제 색. 2배 정수로 정확히 비교하고 동률이면 앞선 것.
    colors는 source_lines의 영역 순서. 비어 있으면 None."""
    if not colors:
        return None
    arr = np.array(colors, dtype=np.int64)
    med2 = [median2(arr[:, ch]) for ch in range(3)]
    best, best_d = None, None
    for c in colors:
        d = sum((2 * c[ch] - med2[ch]) ** 2 for ch in range(3))
        if best_d is None or d < best_d:
            best, best_d = c, d
    return best


def _population_variance(values: list[Fraction]) -> Fraction:
    n = len(values)
    mean = sum(values, Fraction(0)) / n
    return sum(((v - mean) ** 2 for v in values), Fraction(0)) / n


def estimate_align(block: TextBlock, align_tolerance: float) -> tuple[str | None, str | None, str | None, StyleAlignDiag]:
    """블록 정렬 → (align, align_basis, null 사유, 진단). 공백 영역만 있는 줄은 빼고 나머지 줄은 비공백 영역 bbox 합집합을 쓴다."""
    tol_px = block.bbox.w * exact_ratio(align_tolerance)
    limit = tol_px * tol_px
    coords: dict[str, list[Fraction]] = {a: [] for a in ALIGN_VALUES}
    for ln in block.source_lines:
        boxes = [r.bbox for r in ln.regions if not _is_blank(r.text)]
        if not boxes:
            continue
        x0 = min(b.x for b in boxes)
        x1 = max(b.x2 for b in boxes)
        coords["left"].append(Fraction(x0))
        coords["center"].append(Fraction(x0 + x1, 2))
        coords["right"].append(Fraction(x1))
    n_lines = len(coords["left"])
    if n_lines == 0:
        diag = StyleAlignDiag(std_left=None, std_center=None, std_right=None, tolerance_px=float(tol_px), candidates=[])
        return None, None, "no_text_lines", diag
    var = {a: _population_variance(coords[a]) for a in ALIGN_VALUES}
    candidates = [a for a in ALIGN_VALUES if var[a] <= limit]
    diag = StyleAlignDiag(std_left=math.sqrt(var["left"]), std_center=math.sqrt(var["center"]), std_right=math.sqrt(var["right"]),
                          tolerance_px=float(tol_px), candidates=candidates)
    if n_lines == 1:
        return "left", "default_single_line", None, diag
    if not candidates:
        return None, None, "no_candidate", diag
    best = min(var[a] for a in candidates)
    winners = [a for a in candidates if var[a] == best]
    if len(winners) > 1:
        return "left", "default_tie", None, diag
    return winners[0], "estimated", None, diag


def _excluded_block(block: TextBlock, reason: str) -> BlockStyle:
    n = sum(len(ln.regions) for ln in block.source_lines)
    return BlockStyle(
        block_key=block.block_key, status="excluded", excluded_reason=reason, font_color=None, bg_color=None, est_font_px=None,
        align=None, align_basis=None, align_diag=None, null_reasons={"font_color": "excluded", "bg_color": "excluded",
                                                                     "est_font_px": "excluded", "align": "excluded"},
        counts=StyleCounts(regions=n, measured=0, color_failed=0, blank_text=0), regions=[],
    )


def _block_style(block: TextBlock, image: np.ndarray, em_ratio: float, align_tolerance: float) -> BlockStyle:
    regions: list[RegionStyle] = []
    fonts: list[tuple[int, int, int]] = []
    bgs: list[tuple[int, int, int]] = []
    sizes: list[Fraction] = []
    for ln in block.source_lines:
        for r in ln.regions:
            if _is_blank(r.text):
                regions.append(RegionStyle(region_key=r.region_key, line_key=ln.line_key, score=r.score, status="blank_text",
                                           font_color=None, bg_color=None, est_font_px=None, color_error=None, otsu=None))
                continue
            b = r.bbox
            size = est_font_size(b.h, em_ratio)
            sizes.append(size)
            fg, bg, err, diag = region_colors(image[b.y:b.y2, b.x:b.x2])
            if err is None:
                fonts.append(fg)  # type: ignore[arg-type]
                bgs.append(bg)  # type: ignore[arg-type]
            regions.append(RegionStyle(
                region_key=r.region_key, line_key=ln.line_key, score=r.score, status="measured" if err is None else "color_failed",
                font_color=hex_color(fg) if fg else None, bg_color=hex_color(bg) if bg else None, est_font_px=float(size),
                color_error=err, otsu=diag,
            ))
    counts = StyleCounts(regions=len(regions), measured=sum(r.status == "measured" for r in regions),
                         color_failed=sum(r.status == "color_failed" for r in regions),
                         blank_text=sum(r.status == "blank_text" for r in regions))
    align, basis, align_reason, align_diag = estimate_align(block, align_tolerance)
    null_reasons: dict[str, str] = {}
    if not sizes:
        status = "no_text"
        null_reasons.update(font_color="no_text", bg_color="no_text", est_font_px="no_text")
    else:
        status = "partial" if counts.color_failed else "ok"
        if not fonts:
            null_reasons.update(font_color="no_valid_region", bg_color="no_valid_region")
    if align is None:
        null_reasons["align"] = align_reason  # type: ignore[assignment]
    font, bg = representative_color(fonts), representative_color(bgs)
    return BlockStyle(
        block_key=block.block_key, status=status, excluded_reason=None, font_color=hex_color(font) if font else None,
        bg_color=hex_color(bg) if bg else None, est_font_px=float(median_fraction(sizes)) if sizes else None, align=align,
        align_basis=basis, align_diag=align_diag, null_reasons=null_reasons, counts=counts, regions=regions,
    )


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------
def extract(
    image_id: str, section: Section, image: np.ndarray, merged: MergeResult, label: LabelResult, logo: LogoResult,
    logo_record: dict[str, Any], cfg: dict[str, Any],
) -> StyleResult:
    """⑦ 핵심 계산 — 파일 입출력 없음. image = 원본 섹션 이미지 (H, W, 3) uint8 RGB 배열(⑥ 출력 아님).
    섹션 입력 오류는 StyleInputError(결과 없음). 입력 객체 · 배열은 바꾸지 않는다."""
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        desc = f"{type(image).__name__} {getattr(image, 'dtype', None)} {getattr(image, 'shape', None)}"
        raise StyleInputError(f"{image_id}/{section.section_key}: 섹션 이미지는 (H, W, 3) uint8 RGB 배열이어야 한다 — {desc}")
    excluded, fps = validate_inputs(image_id, section, (image.shape[1], image.shape[0]), merged, label, logo, logo_record, cfg)
    st = cfg["style"]
    blocks: list[BlockStyle] = []
    for b in sorted(merged.blocks, key=lambda b: (b.block_order, b.block_key)):
        reason = excluded[b.block_key]
        blocks.append(_excluded_block(b, reason) if reason else _block_style(b, image, st["em_ratio"], st["align_tolerance"]))
    return StyleResult(
        image_id=image_id, section_key=section.section_key,
        config=StyleConfigSnapshot(method=st["method"], em_ratio=st["em_ratio"], align_tolerance=st["align_tolerance"]),
        input_fingerprints=fps, blocks=blocks,
    )


def run(
    image_id: str, section: Section, merged: MergeResult, label: LabelResult, logo: LogoResult, logo_record: dict[str, Any],
    cfg: dict[str, Any],
) -> StyleResult:
    """section.image_path(원본 섹션 이미지)를 읽어 extract를 부른다. RGB 모드가 아니면 변환하지 않고 StyleInputError."""
    from PIL import Image

    where = f"{image_id}/{section.section_key}"
    try:
        with Image.open(section.image_path) as im:
            if im.mode != "RGB":
                raise StyleInputError(f"{where}: 섹션 이미지 모드 {im.mode} — RGB만 지원(색 공간 변환 없음)")
            image = np.array(im)
    except OSError as e:  # 파일 없음 · 열 수 없는 이미지
        raise StyleInputError(f"{where}: 섹션 이미지를 열 수 없다 {section.image_path}: {e}") from e
    return extract(image_id, section, image, merged, label, logo, logo_record, cfg)
