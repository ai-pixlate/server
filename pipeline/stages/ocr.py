"""② 텍스트 추출 — PaddleOCR PP-OCRv5 [pipeline.md 단계표 ②].

- 전처리 없음(문서 방향 분류 · 왜곡 보정 · 줄 방향 분류 끔) [pipeline.md 3절 ocr.preprocess].
- 영역은 인식 결과 배열(rec_polys · rec_texts · rec_scores)에서만 만든다. dt_polys와 인덱스로 잇지 않는다.
  세 배열의 길이가 다르면 OCR_FAILED.
- 빈 텍스트 · 낮은 인식 신뢰도만을 이유로 영역을 버리지 않는다. 인식 신뢰도 임계값은 0으로 명시한다 [pipeline.md 7절].
- score = 영역의 인식 신뢰도(rec_scores[i]) [contract.md 2.5, open-questions.md 5절 #34].
- 좌표는 섹션 로컬 정수. 폴리곤 점은 섹션 범위 [0, W]×[0, H]로 자르고 bbox는 자른 폴리곤에서 만든다.
- 영역 순서는 라이브러리 정렬(위→아래, 왼→오른쪽) 그대로, region_key는 그 순서로 부여한다.
- 높이 > `ocr.split_threshold_px` 섹션은 OCR 처리용 타일로 나눠 돌리고 섹션 로컬 좌표로 복원한 뒤 겹침 중복을 정리한다
  (`ocr_tile.py`, 계약 3.2 · pipeline.md 7.9절). 보완 OCR 후에도 타일 경계 충돌이 남으면 OCR_FAILED. 이하 섹션은 한 번에 돈다.
- 텍스트 0개는 오류가 아니다(regions=[]). 섹션 이미지를 열 수 없거나 크기가 Section의 width·height와 다르면 IMAGE_OPEN_FAILED,
  엔진 초기화·추론·출력 형식 오류는 OCR_FAILED(재시도 가능), 타일 경계 충돌 미해결은 OCR_FAILED · 재시도 불가 — 원인별 표는
  errors.py(#33 타일 범위 결정, 나머지 세분은 후속).

의존성은 pipeline/requirements-ocr.txt (CPU로 로컬 실행 가능). paddle은 엔진을 만들 때만 import한다.
"""
from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Any, Callable

import numpy as np
from PIL import Image
from pydantic import ValidationError

from pipeline.errors import AnalyzeError, image_open_failed
from pipeline.stages import ocr_tile
from pipeline.types import BBox, OcrRegion, OcrResult, Section, region_key

REC_SCORE_THRESH = 0.0  # pipeline.md 7절: 빈 텍스트·저신뢰만을 이유로 제거하지 않는다
_REQUIRED_KEYS = ("rec_polys", "rec_texts", "rec_scores")

# 엔진: BGR uint8 배열 → PaddleOCR 결과 dict(rec_polys · rec_texts · rec_scores ...). info는 run.json 기록용.
Engine = Callable[[np.ndarray], dict[str, Any]]


class OcrEngineInitError(Exception):
    """엔진 생성 실패(의존성 · 모델명 · 모델 파일 · 실행 환경). CLI는 실행 전체를 멈춘다(dev.md 4절)."""


def _ocr_failed(section: Section, message: str, cause: BaseException | None = None, *, reason: str) -> AnalyzeError:
    """retryable은 기본값(True) — 원인별 분류는 errors.py 표(#33). reason은 진단용 내부 분류."""
    detail = f": {type(cause).__name__}: {cause}" if cause else ""
    return AnalyzeError("OCR_FAILED", f"OCR 실패 section={section.section_key} {message}{detail}", section.source_image_id,
                        reason=reason)


class PaddleOcrEngine:
    """PaddleOCR 3.x 어댑터. 설정값은 cfg["ocr"]와 실행 환경에서만 온다."""

    def __init__(self, ocr_cfg: dict[str, Any]) -> None:
        if ocr_cfg["preprocess"] != "none":
            raise ValueError(f"ocr.preprocess={ocr_cfg['preprocess']!r} 미지원 — 정본은 'none'(pipeline.md 3절)")
        kwargs: dict[str, Any] = dict(
            text_detection_model_name=ocr_cfg["det_model"],
            text_recognition_model_name=ocr_cfg["rec_model"],
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            text_rec_score_thresh=REC_SCORE_THRESH,
        )
        try:
            import paddle
            import paddleocr
            import paddlex
            from paddleocr import PaddleOCR
            from paddlex.utils.device import get_default_device

            # 장치는 설치된 paddle 빌드가 정한다: GPU 빌드 + GPU 있음 → gpu:0, 아니면 cpu (dev.md 6절).
            device = get_default_device()
            if device == "cpu":
                # 고정 버전에서 Windows·Linux CPU의 ConvertPirAttribute2RuntimeAttribute 오류 회피.
                # CPU에만 적용하며 GPU 설정은 유지한다(dev.md 6절).
                kwargs["enable_mkldnn"] = False
            self._ocr = PaddleOCR(**kwargs)
        except Exception as e:  # noqa: BLE001 — 초기화 실패는 유형과 무관하게 실행 전체 중단
            raise OcrEngineInitError(f"{type(e).__name__}: {e}") from e
        self.info = {
            "engine": "paddleocr",
            "versions": {"paddle": paddle.__version__, "paddleocr": paddleocr.__version__, "paddlex": paddlex.__version__,
                         "cuda": paddle.version.cuda() if paddle.device.is_compiled_with_cuda() else None},
            "platform": sys.platform,
            "device": device,
            "settings": {k: v for k, v in kwargs.items()},
            "enable_mkldnn": kwargs.get("enable_mkldnn", "library default"),
        }

    def __call__(self, image_bgr: np.ndarray) -> dict[str, Any]:
        res = self._ocr.predict(image_bgr)
        if len(res) != 1:
            raise ValueError(f"결과 페이지 수 {len(res)} ≠ 1")
        r = res[0]
        return {k: r[k] for k in r.keys()}


_ENGINES: dict[tuple, PaddleOcrEngine] = {}


def build_engine(cfg: dict[str, Any]) -> PaddleOcrEngine:
    """cfg["ocr"]의 모델·전처리 설정별로 한 번만 만든다. 실패하면 OcrEngineInitError."""
    oc = cfg["ocr"]
    key = (oc["det_model"], oc["rec_model"], oc["preprocess"])
    if key not in _ENGINES:
        _ENGINES[key] = PaddleOcrEngine(oc)
    return _ENGINES[key]


def load_section_image(section: Section) -> np.ndarray:
    """섹션 이미지 → BGR uint8 배열. 열 수 없으면 IMAGE_OPEN_FAILED(재시도 불가) [계약 8장].

    PIL로 연다(Windows 한글 경로에서 cv2.imread가 실패하는 문제를 피한다).
    """
    try:
        with Image.open(section.image_path) as im:
            rgb = np.asarray(im.convert("RGB"))
    except Exception as e:  # noqa: BLE001
        raise image_open_failed(section.image_path, section.source_image_id, e) from e
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _clip_poly(poly: Any, w: int, h: int) -> list[tuple[int, int]]:
    pts = np.asarray(poly, dtype=np.float64).reshape(-1, 2)
    pts = np.rint(pts)
    pts[:, 0] = np.clip(pts[:, 0], 0, w)
    pts[:, 1] = np.clip(pts[:, 1], 0, h)
    return [(int(x), int(y)) for x, y in pts]


def to_result(section: Section, raw: dict[str, Any], width: int, height: int) -> OcrResult:
    """엔진 결과 dict → OcrResult. 반환값 · 배열의 형식 오류와 길이 불일치는 모두 OCR_FAILED."""
    if not isinstance(raw, Mapping):
        raise _ocr_failed(section, f"엔진 결과가 dict가 아님({type(raw).__name__})", reason="ocr_output_invalid")
    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        raise _ocr_failed(section, f"엔진 결과에 키 없음 {missing}", reason="ocr_output_invalid")
    try:
        polys, texts, scores = (list(raw[k]) for k in _REQUIRED_KEYS)
    except TypeError as e:
        raise _ocr_failed(section, "엔진 결과 형식 오류(배열 아님)", e, reason="ocr_output_invalid") from e
    if not (len(polys) == len(texts) == len(scores)):
        raise _ocr_failed(section, f"배열 길이 불일치 rec_polys={len(polys)} rec_texts={len(texts)} rec_scores={len(scores)}",
                          reason="ocr_output_invalid")
    regions = []
    try:
        for i, (poly, text, score) in enumerate(zip(polys, texts, scores), 1):
            p = _clip_poly(poly, width, height)
            regions.append(OcrRegion(region_key=region_key(i), text=str(text), score=float(score), poly=p, bbox=BBox.from_poly(p)))
    except (ValidationError, ValueError, TypeError) as e:
        raise _ocr_failed(section, "엔진 결과 형식 오류", e, reason="ocr_output_invalid") from e
    return OcrResult(section_key=section.section_key, regions=regions)


def validate_config(cfg: dict[str, Any]) -> ocr_tile.TileParams:
    """② 임시 분할 설정을 검증한다(키 누락 · 범위 · 커서 정체 조건). 어기면 ValueError — 섹션을 돌리기 전에 부른다."""
    try:
        return ocr_tile.params_of(cfg)
    except KeyError as e:
        raise ValueError(f"ocr 설정에 키 없음 {e}") from e


def run(section: Section, cfg: dict[str, Any], engine: Engine | None = None, diag: dict[str, Any] | None = None) -> OcrResult:
    """섹션 하나를 OCR한다. engine은 테스트·실험용 주입(기본은 build_engine(cfg)).

    높이 > split_threshold_px면 타일 OCR(ocr_tile.run_tiled). diag에 dict를 주면 타일 섹션의 진단(타일 · 성분 · 보완 ·
    버린 검출 · 최종 대응)을 채운다 — 실패(OCR_FAILED)로 끝나도 그때까지의 내용이 남는다. 계약 값이 아니라 개발 확인용."""
    params = validate_config(cfg)
    image = load_section_image(section)
    h, w = image.shape[:2]
    if (w, h) != (section.width, section.height):
        # 메타데이터와 실제 이미지가 다르면 분할 판단·좌표 범위가 어긋난다 — 입력 불량으로 재시도 불가(계약 8장 코드)
        raise AnalyzeError(
            "IMAGE_OPEN_FAILED",
            f"섹션 이미지 크기 불일치 {section.section_key}: 이미지 {w}x{h} ≠ Section {section.width}x{section.height} ({section.image_path})",
            section.source_image_id,
        )
    if engine is None:
        try:
            engine = build_engine(cfg)
        except OcrEngineInitError as e:
            raise _ocr_failed(section, "엔진 초기화 실패", e, reason="ocr_engine_init") from e
    if h <= params.threshold_px:
        try:
            raw = engine(image)
        except Exception as e:  # noqa: BLE001 — 추론 예외는 OCR_FAILED로 감싼다(retryable 분류는 #33)
            raise _ocr_failed(section, "추론 실패", e, reason="ocr_inference") from e
        return to_result(section, raw, w, h)

    def recognize(y0: int, y1: int) -> list[OcrRegion]:
        try:
            raw = engine(image[y0:y1])
        except Exception as e:  # noqa: BLE001
            raise _ocr_failed(section, f"추론 실패(타일 y {y0}~{y1})", e, reason="ocr_inference") from e
        return to_result(section, raw, w, y1 - y0).regions

    regions = ocr_tile.run_tiled(section, image, params, recognize, diag)
    return OcrResult(section_key=section.section_key, regions=regions)
