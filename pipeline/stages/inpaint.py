"""⑥ 인페인팅 — 단독 개발 v1, 로컬 구현(2026-09-30). pipeline.md 단계표 ⑥ · 7.6절, contract.md 5.2, open-questions.md #72 · #74 · #75.

정본(확정): LaMa(iopaint) + `erase_s50` — 신뢰도 0.5 이상 · 텍스트 있는 **원시 영역**만 마스크, 마스크 = poly 채움 + 글자 높이 15% 팽창,
④⑤ 보호 영역은 마스크에서 빼고 팽창이 침범하지 않게 구성, 확대 재시도 없음[개발계획 2.1, 6장][계약 5.2]. 판정은 블록
`ocr_confidence`가 아니라 원시 영역별 `score`[계약 2.5]. 저신뢰 · 빈 텍스트 영역은 원문 보존[개발계획 6장].

단독 개발 계획(사용자 승인 2026-09-30, open-questions.md 2.3절 — 운영 계약 아님):
- 삭제 후보 블록 = ④ `is_product_label=false` 그리고 ⑤ `is_brand_logo=false`. 후보 블록의 원시 영역마다 `score >= inpaint.score_min`과
  (`inpaint.require_text`이면) `text.strip() != ""`를 판단한다. 블록 `ocr_confidence` · `role` · 블록 원문으로 대체하지 않는다.
- 영역별로 poly를 채우고 `bbox.h`를 글자 높이로, 반경 `ceil(bbox.h × inpaint.dilate_ratio)`의 원형 커널로 **영역마다 따로** 팽창한 뒤
  합집합. 합쳐진 마스크를 다시 팽창하지 않는다(중복 OCR 영역 #40이 있어도 누적 재팽창 없음).
- 보호 = 라벨 · 로고 블록의 `bbox` 전체 + 후보 블록의 저신뢰 · 빈 텍스트 원시 영역 poly. 삭제 합집합에서 보호를 **마지막에** 차감(보호 우선).
- 경계 밖 좌표는 오류(자르거나 보정하지 않음). 면적 0 삭제 대상은 삭제하지 않고 진단. 보호 대상의 기하가 유효하지 않으면 그 섹션 중단.
- 빈 마스크는 모델을 부르지 않고 원본을 그대로 돌려준다. 최종 마스크 안의 픽셀만 모델 결과에서 가져오고 나머지는 원본 복사.

잠정 구현 정의(이 모듈이 소유, 2026-09-30 — PoC에서 검증된 동작이 아니다):
- 좌표 평면: 정수 좌표는 픽셀 **경계**다. 픽셀 (x, y)는 [x, x+1) × [y, y+1)을 차지하고 섹션 이미지는 [0, W] × [0, H]다.
  꼭짓점 x = W · y = H는 영상 오른쪽 · 아래 가장자리에 닿은 것(허용)이고, x < 0 · x > W · y < 0 · y > H는 경계 밖(오류)이다.
  (입력본 실측: paddlex 출력 poly의 최댓값이 W · H와 같은 영역이 있고 W-1 · H-1에서 멈춘 영역은 없다 — 이 해석과 맞는다.)
- poly 채움: 픽셀 중심 (x+0.5, y+0.5)가 다각형 내부(0이 아닌 감김수)이거나 **경계 위**면 포함한다. 축 정렬 사각형 poly는
  열 x..x+w-1 · 행 y..y+h-1, 즉 bbox와 같은 w×h 픽셀이 된다. 계산은 좌표를 2배 한 정수로 해 부동소수 오차가 없다.
- bbox 사각형 보호: 같은 규칙으로 [x, x+w) × [y, y+h) = 열 x..x+w-1 · 행 y..y+h-1.
- 원형 커널: 반경 r의 오프셋 (dx, dy) 중 dx² + dy² ≤ r²인 것. r = 0이면 팽창 없음. 팽창이 영상 밖으로 나가는 부분은 버린다
  (입력 좌표를 자르는 것이 아니라 커널이 영상 밖에 닿는 경우다).
- 반경 계산: `dilate_ratio`를 설정 값의 10진 표기(`str(float)`)로 읽은 정확한 분수로 계산한다. 기본값 0.15에서는 높이 0~200,000
  전 범위에서 부동소수 올림과 차이가 없지만(확인 2026-09-30), `--set`으로 바꾼 값에서는 달라질 수 있다 — 예: 부동소수
  `100 × 0.07` = 7.000000000000001 → ceil 8, 정확한 분수로는 7.
- 면적 0: 꼭짓점 정수 신발끈 면적이 0(모든 꼭짓점이 한 직선 위)이면 면적 0 영역이다.
- 보호 기하가 유효하지 않음: 라벨 · 로고 블록 bbox의 w 또는 h가 0 · 경계 밖, 보호할 저신뢰 · 빈 텍스트 영역이 면적 0 · 경계 밖.
- 영역 bbox가 poly의 외접 사각형(`BBox.from_poly`)과 다르면 글자 높이의 근거가 모호하므로 입력 오류다.

이 모듈은 파일을 읽거나 쓰지 않는다(실행 계층 몫). 실제 LaMa 어댑터는 없다 — `InpaintModel` 인터페이스만 두고, 모델은 호출자가 주입한다.
원시 영역을 지우거나 다시 묶지 않고, ④⑤ 판정을 보정하지 않는다(#36 · #40 · #67 · #69). #34: 영역 score는 현재 인식 신뢰도 구현값이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Protocol

import numpy as np

from pipeline.stages.label import input_fingerprint
from pipeline.stages.logo import sha256_json
from pipeline.types import BBox, LabelResult, LogoResult, MergeResult, OcrRegion, Section

MODELS = ("lama",)
TIMEOUT_KEYS = ("init_timeout_s", "infer_timeout_s", "kill_grace_s")  # 모델 자식 프로세스 시간 제한(실측용 잠정값, #75)
LAMA_FACTORY = "pipeline.stages.inpaint_lama:LamaModel"  # 정본에 정의된 인페인팅 모델 값은 lama뿐이다[개발계획 6장]
RASTER_RULE = ("poly: 정수 좌표 = 픽셀 경계, 픽셀 중심 (x+0.5, y+0.5)가 내부(nonzero) 또는 경계 위면 포함 · "
               "bbox: 열 x..x+w-1 · 행 y..y+h-1 · 커널: dx²+dy² ≤ r² · r = ceil(bbox.h × Fraction(str(dilate_ratio))) · "
               "영역별 팽창 후 합집합 → 보호 차감")

# 영역 분류(진단용 잠정 값, 운영 enum 아님)
TARGET = "target"  # 삭제 대상 — 필터 통과 · 면적 > 0
ZERO_AREA = "zero_area"  # 필터 통과지만 면적 0 — 삭제하지 않음
PROTECTED_REGION = "protected_region"  # 후보 블록의 저신뢰 · 빈 텍스트 — poly 보호
IN_PROTECTED_BLOCK = "in_protected_block"  # 라벨 · 로고 블록 소속 — 블록 bbox로 보호


class InpaintInputError(ValueError):
    """⑥ 입력 · 설정 오류(보호를 보장할 수 없는 기하 포함). 마스크 · 모델 호출을 시작하지 않는다. false · 빈 마스크로 대체하지 않는다."""


class InpaintModelError(RuntimeError):
    """모델 호출 중 예외 또는 잘못된 모델 출력. 섹션 실패이며 실행 계층이 기록 후 실행을 멈춘다. 자동 재시도 · 축소 · CPU 대체 없음."""


class ModelNotAvailable(NotImplementedError):
    """인페인팅 모델을 쓸 수 없다 — 어댑터 없음 · torch 미설치 · 가중치 파일 없음(#72). 실행 계층은 그 섹션부터 결과 없이 종료 코드 3으로
    끝낸다 — 인페인팅 성공으로 표시하지 않는다. CUDA 없음 · 가중치 불일치 · 로드 실패는 초기화 실패(그 밖의 예외, 종료 코드 4)다."""


def build_model(cfg: dict[str, Any]) -> "InpaintModel":
    """기본 모델 팩토리 — `inpaint.model=lama`면 LaMa 어댑터(`inpaint_lama.LamaModel`, iopaint 1.6.0 재현 · GPU · FP32)를
    **모델 전용 자식 프로세스**(`inpaint_proc.SubprocessInpaintModel`, spawn)에서 초기화한다. 시간 제한은 `[inpaint]` `*_timeout_s` ·
    `kill_grace_s`. 가중치는 iopaint 기본 캐시 위치에서 읽고 내려받지 않는다. 원본을 그대로 돌려주는 대체 모델은 두지 않는다."""
    ip = cfg["inpaint"]
    if ip["model"] != "lama":
        raise ModelNotAvailable(f"inpaint.model={ip['model']!r} 어댑터 없음 — 마스크만 만들려면 --mode mask-only")
    from pipeline.stages.inpaint_proc import SubprocessInpaintModel

    return SubprocessInpaintModel(LAMA_FACTORY, **{k: ip[k] for k in TIMEOUT_KEYS})


def pixel_sha256(arr: np.ndarray) -> str:
    """배열 모양 · 자료형 · 원시 바이트의 SHA-256(파일 인코딩과 무관한 픽셀 지문)."""
    import hashlib

    h = hashlib.sha256(f"{arr.shape}|{arr.dtype}|".encode("ascii"))
    h.update(np.ascontiguousarray(arr).tobytes())
    return h.hexdigest()


class InpaintModel(Protocol):
    """인페인팅 모델 호출 인터페이스(개발용). 실제 LaMa 어댑터는 아직 없다(#72).

    - `describe()` — 실행 기록용 모델 식별(이름 · 버전 · 가중치 지문 등). 값은 구현이 정한다.
    - `inpaint(image, mask)` — image: (H, W, 3) uint8 RGB, mask: (H, W) uint8(0 = 보존 · 255 = 지움). 둘 다 호출마다 새 복사본을 넘긴다.
      반환은 (H, W, 3) uint8 RGB. 크기 조정 · 분할 · 장치 선택은 어댑터 몫이며 결과는 원본 크기로 돌려줘야 한다.
    """

    def describe(self) -> dict[str, Any]: ...

    def inpaint(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray: ...


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def validate_config(cfg: dict[str, Any]) -> None:
    """[inpaint] 설정 검증. 기본값은 config가 정본이며 여기서 채우지 않는다. 어기면 InpaintInputError."""
    if "inpaint" not in cfg:
        raise InpaintInputError("config에 [inpaint] 표가 없다")
    ip = cfg["inpaint"]
    missing = [k for k in ("model", "score_min", "require_text", "dilate_ratio", "dilate_retry", *TIMEOUT_KEYS) if k not in ip]
    if missing:
        raise InpaintInputError("잘못된 [inpaint] 설정: 없는 키 " + ", ".join(f"inpaint.{k}" for k in missing))
    errors: list[str] = []
    if ip["model"] not in MODELS:
        errors.append(f"inpaint.model={ip['model']!r} — {MODELS} 중 하나")
    sm = ip["score_min"]
    if not (isinstance(sm, (int, float)) and not isinstance(sm, bool) and math.isfinite(sm) and 0.0 <= sm <= 1.0):
        errors.append(f"inpaint.score_min={sm!r} — 0~1 실수")
    if not isinstance(ip["require_text"], bool):
        errors.append(f"inpaint.require_text={ip['require_text']!r} — boolean")
    dr = ip["dilate_ratio"]
    if not (isinstance(dr, (int, float)) and not isinstance(dr, bool) and math.isfinite(dr) and dr >= 0):
        errors.append(f"inpaint.dilate_ratio={dr!r} — 0 이상 실수")
    if ip["dilate_retry"] is not False:
        errors.append(f"inpaint.dilate_retry={ip['dilate_retry']!r} — 확대 재시도는 금지(false만)[개발계획 6장]")
    for k in TIMEOUT_KEYS:
        v = ip[k]
        if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0):
            errors.append(f"inpaint.{k}={v!r} — 0보다 큰 초")
    if errors:
        raise InpaintInputError("잘못된 [inpaint] 설정: " + "; ".join(errors))


def exact_ratio(value: float) -> Fraction:
    """설정 값의 10진 표기를 정확한 분수로(0.15 → 3/20). 부동소수 곱셈의 올림 오차를 피한다."""
    return Fraction(str(value))


def dilate_radius(height: int, ratio: float) -> int:
    """팽창 반경 = ceil(bbox.h × dilate_ratio) 픽셀(정확한 분수 계산)."""
    return math.ceil(height * exact_ratio(ratio))


# ---------------------------------------------------------------------------
# 기하 · 래스터화
# ---------------------------------------------------------------------------
def poly_area2(poly: list[tuple[int, int]]) -> int:
    """정수 신발끈 공식의 2배 면적(부호 포함). 0이면 면적 0."""
    s = 0
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return s


def out_of_bounds(points: list[tuple[int, int]], width: int, height: int) -> list[tuple[int, int]]:
    """[0, W] × [0, H] 밖의 꼭짓점."""
    return [(x, y) for x, y in points if x < 0 or x > width or y < 0 or y > height]


def bbox_out_of_bounds(b: BBox, width: int, height: int) -> bool:
    return b.x < 0 or b.y < 0 or b.x2 > width or b.y2 > height


def fill_polygon(poly: list[tuple[int, int]], x0: int, y0: int, w: int, h: int) -> np.ndarray:
    """창 [x0, x0+w) × [y0, y0+h) 픽셀의 poly 포함 여부(bool, 행 = y). 픽셀 중심이 내부(0이 아닌 감김수)이거나 경계 위면 True.
    좌표를 2배 한 정수(중심 = 홀수, 꼭짓점 = 짝수)로 계산해 정확하다."""
    if w <= 0 or h <= 0:
        return np.zeros((max(h, 0), max(w, 0)), dtype=bool)
    X = (2 * np.arange(x0, x0 + w, dtype=np.int64) + 1)[None, :]
    Y = (2 * np.arange(y0, y0 + h, dtype=np.int64) + 1)[:, None]
    pts = [(2 * int(px), 2 * int(py)) for px, py in poly]
    wn = np.zeros((h, w), dtype=np.int64)
    on = np.zeros((h, w), dtype=bool)
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        cross = (bx - ax) * (Y - ay) - (X - ax) * (by - ay)  # >0: 점이 변 a→b의 왼쪽
        on |= (cross == 0) & (X >= min(ax, bx)) & (X <= max(ax, bx)) & (Y >= min(ay, by)) & (Y <= max(ay, by))
        # 중심의 y(홀수)는 꼭짓점 y(짝수)와 같을 수 없어 수평 변 · 꼭짓점 통과의 모호함이 없다
        wn += ((ay <= Y) & (by > Y) & (cross > 0)).astype(np.int64)
        wn -= ((ay > Y) & (by <= Y) & (cross < 0)).astype(np.int64)
    return (wn != 0) | on


def disk_offsets(radius: int) -> list[tuple[int, int]]:
    """원형 커널을 행별 (dy, 반폭 k)로 — dx² + dy² ≤ r²인 |dx| ≤ k."""
    return [(dy, math.isqrt(radius * radius - dy * dy)) for dy in range(-radius, radius + 1)]


def dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """원형 커널(dx² + dy² ≤ r²) 이진 팽창. 배열 밖으로 나가는 부분은 버린다. 입력은 바꾸지 않는다."""
    if radius < 0:
        raise ValueError("반경은 0 이상")
    if radius == 0 or not mask.any():
        return mask.copy()
    h, w = mask.shape
    cs = np.zeros((h, w + 1), dtype=np.int64)
    np.cumsum(mask, axis=1, out=cs[:, 1:])
    cols = np.arange(w)
    rows: dict[int, np.ndarray] = {}
    out = np.zeros_like(mask, dtype=bool)
    for dy, k in disk_offsets(radius):
        if abs(dy) >= h:  # 반경이 배열 높이 이상이면 이 커널 행은 배열과 겹치지 않는다
            continue
        if k not in rows:  # 가로 반폭 k 팽창 = 구간 [x-k, x+k]에 True가 있는지
            lo = np.clip(cols - k, 0, w)
            hi = np.clip(cols + k + 1, 0, w)
            rows[k] = (cs[:, hi] - cs[:, lo]) > 0
        r = rows[k]
        if dy >= 0:
            out[dy:, :] |= r[: h - dy, :]
        else:
            out[: h + dy, :] |= r[-dy:, :]
    return out


def rect_mask(b: BBox, width: int, height: int) -> np.ndarray:
    m = np.zeros((height, width), dtype=bool)
    m[b.y:b.y2, b.x:b.x2] = True
    return m


# ---------------------------------------------------------------------------
# 입력 검증 · 영역 분류
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RegionPlan:
    """원시 영역 하나의 처리 계획(진단 재료). 원시 영역 자체는 바꾸지 않는다."""

    region_key: str
    line_key: str
    block_key: str
    kind: str  # TARGET · ZERO_AREA · PROTECTED_REGION · IN_PROTECTED_BLOCK
    reasons: tuple[str, ...]  # low_score · blank_text · product_label · brand_logo(해당하는 것 모두)
    score: float
    text_blank: bool
    poly: tuple[tuple[int, int], ...]
    bbox: BBox
    area2: int
    radius: int | None  # TARGET만


@dataclass(frozen=True)
class BlockProtection:
    block_key: str
    reason: str  # product_label · brand_logo
    bbox: BBox


@dataclass(frozen=True)
class SectionPlan:
    """검증을 통과한 섹션의 마스크 계획. build_masks의 입력."""

    image_id: str
    section_key: str
    width: int
    height: int
    regions: tuple[RegionPlan, ...]
    protected_blocks: tuple[BlockProtection, ...]
    fingerprints: dict[str, str]
    config: dict[str, Any] = field(default_factory=dict)


def _dups(keys: list[str]) -> list[str]:
    return sorted({k for k in keys if keys.count(k) > 1})


def validate_inputs(
    image_id: str, section: Section, image_size: tuple[int, int], merged: MergeResult, label: LabelResult, logo: LogoResult,
    logo_record: dict[str, Any], cfg: dict[str, Any],
) -> SectionPlan:
    """판정 · 식별자 · 지문 · 기하를 검증하고 영역을 분류한다(래스터화 없음). 어기면 InpaintInputError(문제를 모아 한 번에).
    image_size = 실제 섹션 이미지 (W, H). 입력은 바꾸지 않는다."""
    validate_config(cfg)
    ip = cfg["inpaint"]
    if not isinstance(image_id, str) or not image_id:
        raise InpaintInputError(f"원본 이미지 식별자가 비어 있다: {image_id!r}")
    key = section.section_key
    W, H = section.width, section.height
    where = f"{image_id}/{key}"
    if tuple(image_size) != (W, H):
        raise InpaintInputError(f"{where}: 섹션 이미지 크기 {tuple(image_size)} ≠ 섹션 메타데이터 ({W}, {H})")

    problems: list[str] = []
    # 식별자 대응: 원본 식별자 + section_key + block_key
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
    rkeys = [r.region_key for b in merged.blocks for ln in b.source_lines for r in ln.regions]
    if _dups(rkeys):
        problems.append(f"원시 region_key 중복 {_dups(rkeys)}")
    if problems:
        raise InpaintInputError(f"{where}: " + "; ".join(problems))

    # ④ 판정: ok · 블록마다 정확히 하나. 미판정 · 누락 · 실패를 false로 바꾸지 않는다
    if label.status != "ok" or label.labels is None:
        raise InpaintInputError(f"{where}: ④ 결과가 ok가 아니다(status={label.status}) — 미판정을 false로 바꾸지 않는다")
    if logo.status != "ok" or logo.decisions is None:
        raise InpaintInputError(f"{where}: ⑤ 결과가 ok가 아니다(status={logo.status}) — 미판정을 false로 바꾸지 않는다")
    for name, keys in (("④", [d.block_key for d in label.labels]), ("⑤", [d.block_key for d in logo.decisions])):
        if _dups(keys):
            problems.append(f"{name} 판정 중복 {_dups(keys)}")
        if sorted(set(bkeys) - set(keys)):
            problems.append(f"{name} 판정 누락 {sorted(set(bkeys) - set(keys))}")
        if sorted(set(keys) - set(bkeys)):
            problems.append(f"③에 없는 블록의 {name} 판정 {sorted(set(keys) - set(bkeys))}")
    if problems:
        raise InpaintInputError(f"{where}: " + "; ".join(problems))
    by_label = {d.block_key: d.is_product_label for d in label.labels}
    by_logo = {d.block_key: d for d in logo.decisions}
    for bk in bkeys:
        is_label, lg = by_label[bk], by_logo[bk]
        if type(is_label) is not bool:
            problems.append(f"{bk}: ④ 값이 boolean이 아니다({is_label!r})")
        elif is_label and not (lg.is_brand_logo is None and lg.basis == "product_label"):
            problems.append(f"{bk}: ④ true인데 ⑤가 생략(product_label/null)이 아니다({lg.basis}/{lg.is_brand_logo})")
        elif not is_label and type(lg.is_brand_logo) is not bool:
            problems.append(f"{bk}: ④ false인데 ⑤ 값이 boolean이 아니다({lg.basis}/{lg.is_brand_logo}) — 미판정을 false로 바꾸지 않는다")
    if problems:
        raise InpaintInputError(f"{where}: " + "; ".join(problems))

    # 지문: ④는 현재 ③ 블록으로, ⑤는 현재 ③ 블록 · ④ 결과로 판정했어야 한다
    fp_blocks = input_fingerprint(merged.blocks)
    fp_label = sha256_json(label.model_dump(mode="json"))
    fp_logo = sha256_json(logo.model_dump(mode="json"))
    if label.checked.input_fingerprint != fp_blocks:
        problems.append(f"④ 입력 지문 {label.checked.input_fingerprint[:12]}… ≠ 현재 ③ 블록 {fp_blocks[:12]}…")
    if not isinstance(logo_record, dict):
        problems.append("⑤ 기록(logo_debug)이 객체가 아니다")
    else:
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
        raise InpaintInputError(f"{where}: " + "; ".join(problems))

    # 기하 · 분류
    score_min, require_text, ratio = ip["score_min"], ip["require_text"], ip["dilate_ratio"]
    regions: list[RegionPlan] = []
    protected_blocks: list[BlockProtection] = []
    for b in sorted(merged.blocks, key=lambda b: (b.block_order, b.block_key)):
        if bbox_out_of_bounds(b.bbox, W, H):
            problems.append(f"{b.block_key}: 블록 bbox {b.bbox.model_dump()}가 영상 [0,{W}]×[0,{H}] 밖")
        block_reasons = tuple(r for r, on in (("product_label", by_label[b.block_key]),
                                              ("brand_logo", by_logo[b.block_key].is_brand_logo is True)) if on)
        if block_reasons:
            if b.bbox.w == 0 or b.bbox.h == 0:
                problems.append(f"{b.block_key}: 보호할 블록 bbox의 면적이 0 {b.bbox.model_dump()} — 보호를 보장할 수 없다")
            protected_blocks.append(BlockProtection(b.block_key, block_reasons[0], b.bbox))
        for ln in b.source_lines:
            for r in ln.regions:
                problems += _region_geometry_problems(r, W, H)
                area2 = poly_area2(r.poly)
                blank = not r.text.strip()
                base = dict(region_key=r.region_key, line_key=ln.line_key, block_key=b.block_key, score=r.score, text_blank=blank,
                            poly=tuple(r.poly), bbox=r.bbox, area2=area2)
                if block_reasons:
                    regions.append(RegionPlan(kind=IN_PROTECTED_BLOCK, reasons=block_reasons, radius=None, **base))
                    continue
                low = r.score < score_min
                no_text = require_text and blank
                if not (low or no_text):
                    if area2 == 0:
                        regions.append(RegionPlan(kind=ZERO_AREA, reasons=(), radius=None, **base))
                    else:
                        regions.append(RegionPlan(kind=TARGET, reasons=(), radius=dilate_radius(r.bbox.h, ratio), **base))
                else:
                    if area2 == 0:
                        problems.append(f"{b.block_key}/{r.region_key}: 보호할 저신뢰 · 빈 텍스트 영역의 면적이 0 — 보호를 보장할 수 없다")
                    reasons = tuple(x for x, on in (("low_score", low), ("blank_text", no_text)) if on)
                    regions.append(RegionPlan(kind=PROTECTED_REGION, reasons=reasons, radius=None, **base))
    if problems:
        raise InpaintInputError(f"{where}: " + "; ".join(problems))
    return SectionPlan(
        image_id=image_id, section_key=key, width=W, height=H, regions=tuple(regions), protected_blocks=tuple(protected_blocks),
        fingerprints={"blocks": fp_blocks, "label": fp_label, "logo": fp_logo, "logo_record": sha256_json(logo_record)},
        config={k: ip[k] for k in ("model", "score_min", "require_text", "dilate_ratio", "dilate_retry")},
    )


def _region_geometry_problems(r: OcrRegion, width: int, height: int) -> list[str]:
    out: list[str] = []
    oob = out_of_bounds(list(r.poly), width, height)
    if oob:
        out.append(f"{r.region_key}: poly 꼭짓점 {oob}가 영상 [0,{width}]×[0,{height}] 밖 — 자르거나 보정하지 않는다")
    if r.bbox != BBox.from_poly(list(r.poly)):
        out.append(f"{r.region_key}: bbox {r.bbox.model_dump()} ≠ poly 외접 사각형 — 글자 높이의 근거가 모호하다")
    return out


# ---------------------------------------------------------------------------
# 마스크
# ---------------------------------------------------------------------------
@dataclass
class RegionPixels:
    filled_px: int = 0  # poly 채움 픽셀
    dilated_px: int = 0  # 팽창 후 픽셀(TARGET)
    conflict_px: int = 0  # 팽창 마스크 ∩ 보호 마스크 — 보호가 이겨 지우지 않은 픽셀
    final_px: int = 0  # 팽창 마스크 ∩ 최종 마스크


@dataclass
class SectionMasks:
    delete: np.ndarray  # 삭제 대상 영역별 팽창의 합집합(보호 차감 전)
    protect: np.ndarray  # 보호 마스크
    final: np.ndarray  # delete − protect
    region_pixels: dict[str, RegionPixels]
    counts: dict[str, int]


def build_masks(plan: SectionPlan) -> SectionMasks:
    """영역별 poly 채움 → 영역별 원형 팽창 → 합집합 → 보호 차감. 입력 계획은 바꾸지 않는다."""
    W, H = plan.width, plan.height
    delete = np.zeros((H, W), dtype=bool)
    protect = np.zeros((H, W), dtype=bool)
    for pb in plan.protected_blocks:
        protect[pb.bbox.y:pb.bbox.y2, pb.bbox.x:pb.bbox.x2] = True
    windows: dict[str, tuple[int, int, np.ndarray]] = {}
    pixels: dict[str, RegionPixels] = {}
    for rp in plan.regions:
        b = rp.bbox
        filled = fill_polygon(list(rp.poly), b.x, b.y, b.w, b.h)  # 픽셀 중심이 poly 외접 사각형 밖일 수 없으므로 창 = bbox
        px = RegionPixels(filled_px=int(filled.sum()))
        pixels[rp.region_key] = px
        if rp.kind == PROTECTED_REGION:
            protect[b.y:b.y2, b.x:b.x2] |= filled
        elif rp.kind == TARGET:
            r = rp.radius or 0
            x0, y0 = max(b.x - r, 0), max(b.y - r, 0)
            x1, y1 = min(b.x2 + r, W), min(b.y2 + r, H)
            win = np.zeros((y1 - y0, x1 - x0), dtype=bool)
            win[b.y - y0:b.y - y0 + b.h, b.x - x0:b.x - x0 + b.w] = filled
            grown = dilate(win, r)
            windows[rp.region_key] = (x0, y0, grown)
            px.dilated_px = int(grown.sum())
            delete[y0:y1, x0:x1] |= grown  # 영역마다 따로 팽창한 뒤 합집합 — 합친 마스크를 다시 팽창하지 않는다
    final = delete & ~protect  # 보호 우선, 마지막에 차감
    for rk, (x0, y0, grown) in windows.items():
        h, w = grown.shape
        pixels[rk].conflict_px = int((grown & protect[y0:y0 + h, x0:x0 + w]).sum())
        pixels[rk].final_px = int((grown & final[y0:y0 + h, x0:x0 + w]).sum())
    kinds = [rp.kind for rp in plan.regions]
    counts = {
        "regions": len(kinds), "target": kinds.count(TARGET), "zero_area": kinds.count(ZERO_AREA),
        "protected_region": kinds.count(PROTECTED_REGION), "in_protected_block": kinds.count(IN_PROTECTED_BLOCK),
        "protected_blocks": len(plan.protected_blocks),
        "delete_px": int(delete.sum()), "protect_px": int(protect.sum()), "conflict_px": int((delete & protect).sum()),
        "final_px": int(final.sum()),
    }
    return SectionMasks(delete=delete, protect=protect, final=final, region_pixels=pixels, counts=counts)


def region_diagnostics(plan: SectionPlan, masks: SectionMasks) -> list[dict[str, Any]]:
    """영역별 처리 내역(대상 · 제외 이유 · 반경 · 픽셀 수 · 충돌). #12: 번역 · 렌더 정책이 아니라 ⑥의 처리 기록이다."""
    out = []
    for rp in plan.regions:
        px = masks.region_pixels[rp.region_key]
        out.append({
            "region_key": rp.region_key, "line_key": rp.line_key, "block_key": rp.block_key, "kind": rp.kind, "reasons": list(rp.reasons),
            "score": rp.score, "text_blank": rp.text_blank, "bbox": rp.bbox.model_dump(), "area2": rp.area2, "radius": rp.radius,
            "filled_px": px.filled_px, "dilated_px": px.dilated_px, "conflict_px": px.conflict_px, "final_px": px.final_px,
        })
    return out


# ---------------------------------------------------------------------------
# 모델 호출 · 합성
# ---------------------------------------------------------------------------
def check_image(image: np.ndarray, plan: SectionPlan) -> None:
    """섹션 원본 배열: (H, W, 3) uint8 RGB여야 한다(색 공간 변환 · 크기 보정을 하지 않는다)."""
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.shape != (plan.height, plan.width, 3):
        got = (type(image).__name__, getattr(image, "dtype", None), getattr(image, "shape", None))
        raise InpaintInputError(f"{plan.image_id}/{plan.section_key}: 원본 배열이 ({plan.height}, {plan.width}, 3) uint8이 아니다 {got}")


def check_model_output(out: Any, image: np.ndarray) -> np.ndarray:
    """모델 결과는 원본과 같은 (H, W, 3) uint8 배열이어야 한다. 아니면 InpaintModelError(보정 · 변환 없음)."""
    if not isinstance(out, np.ndarray):
        raise InpaintModelError(f"모델 출력이 numpy 배열이 아니다({type(out).__name__})")
    if out.dtype != np.uint8:
        raise InpaintModelError(f"모델 출력 dtype {out.dtype} — uint8이어야 한다")
    if out.shape != image.shape:
        raise InpaintModelError(f"모델 출력 크기 {out.shape} ≠ 원본 {image.shape}")
    return out


def composite(image: np.ndarray, model_out: np.ndarray, final: np.ndarray) -> np.ndarray:
    """최종 마스크 안 픽셀만 모델 결과에서, 나머지는 원본에서 복사한 새 배열."""
    out = image.copy()
    out[final] = model_out[final]
    return out


@dataclass
class ApplyResult:
    background: np.ndarray
    model_called: bool


def apply(image: np.ndarray, plan: SectionPlan, masks: SectionMasks, model: InpaintModel) -> ApplyResult:
    """빈 마스크면 모델을 부르지 않고 원본 복사본을 돌려준다. 아니면 모델을 한 번 불러 검증 · 합성한다.
    모델 예외 · 잘못된 출력은 InpaintModelError. 원본 배열은 바꾸지 않는다(모델에는 복사본을 넘긴다)."""
    check_image(image, plan)
    if not masks.final.any():
        return ApplyResult(background=image.copy(), model_called=False)
    mask_u8 = masks.final.astype(np.uint8) * 255
    try:
        out = model.inpaint(image.copy(), mask_u8.copy())
    except Exception as e:  # noqa: BLE001 — 모델 구현의 모든 예외를 섹션 실패로
        raise InpaintModelError(f"모델 호출 실패: {e.__class__.__name__}: {e}") from e
    check_model_output(out, image)
    return ApplyResult(background=composite(image, out, masks.final), model_called=True)
