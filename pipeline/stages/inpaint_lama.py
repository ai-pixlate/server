"""⑥ 인페인팅 LaMa 어댑터 — `InpaintModel` 구현(2026-09-30, pipeline.md 7.6절 · open-questions.md #72).

정본 채택 기술은 LaMa(iopaint)다[개발계획 6장]. iopaint 1.6.0 패키지는 Python 3.12에서 설치가 어렵고(Pillow==9.5.0 고정 등)
LaMa 하나를 불러와도 모든 모델 의존성을 import하므로, **같은 가중치와 같은 전처리 · 후처리를 재현**한다(사용자 결정 2026-09-30).

재현 기준 — iopaint 1.6.0(PyPI 휠 SHA-256 b38c6987…a878):
- 가중치 `big-lama.pt`(TorchScript) · 기본 URL `LAMA_URL` · MD5 `LAMA_MD5`(iopaint/model/lama.py 상수). iopaint와 같은 캐시 위치
  `torch.hub.get_dir()/checkpoints/big-lama.pt`(= `$TORCH_HOME/hub/checkpoints`)를 읽는다. **이 어댑터는 내려받지 않는다** — 가중치는
  별도 단계로 준비하고, 없으면 `ModelNotAvailable`(종료 코드 3)이다.
- `InpaintModel._pad_forward`(iopaint/model/base.py): 이미지 · 마스크를 **아래 · 오른쪽으로만** 8의 배수까지 `np.pad(mode="symmetric")`
  → forward → 결과를 [0:H, 0:W]로 자른다.
- `LaMa.forward`(iopaint/model/lama.py): `norm_img` = HWC → CHW · float32 / 255, 마스크는 `(mask/255 > 0) * 1`(정수 0/1),
  `model(image[1,3,H,W], mask[1,1,H,W])` → `clip(out × 255, 0, 255).astype(uint8)`(반올림이 아니라 버림).

iopaint와 **다르게 하는 것**(원본 해상도 · 우리 입출력 규약):
- HD 전략: iopaint 요청 기본값은 `hd_strategy=CROP`(긴 변 800 초과 시 마스크 박스 주변 128px 여백을 잘라 여러 번 추론)이다.
  섹션당 원본 해상도 1회 추론이라는 계획과 충돌하므로 쓰지 않고 `ORIGINAL`과 같은 경로(내부 리사이즈 · 분할 없음)만 둔다.
- 색 순서: iopaint `forward`는 결과를 RGB → BGR로 바꿔 돌려주지만 여기서는 **RGB 그대로** 돌려준다(입출력 모두 RGB).
- 마스크 밖 복원(`sd_keep_unmasked_area`)은 하지 않는다 — `inpaint.composite`가 최종 마스크 안만 교체한다.

실행 기준: CUDA · FP32(가중치 dtype을 확인만 하고 바꾸지 않음, autocast 없음) · `torch.inference_mode` · 순차 · 호출당 추론 1회.
자동 축소 · 분할 · 재시도 · CPU 대체 없음. CUDA를 쓸 수 없거나 가중치 MD5 불일치 · 로드 실패는 초기화 실패(RuntimeError → 종료 코드 4).
torch는 함수 안에서만 불러온다 — torch가 없는 로컬 환경에서도 모듈 import와 전처리 · 후처리 테스트가 된다.
"""
from __future__ import annotations

import hashlib
import platform
import time
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.stages.inpaint import ModelNotAvailable

LAMA_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
LAMA_MD5 = "e3aa4aaa15225a33ec84f9f4bc47e500"  # iopaint 1.6.0 iopaint/model/lama.py LAMA_MODEL_MD5
LAMA_FILENAME = "big-lama.pt"
PAD_MOD = 8  # iopaint LaMa.pad_mod
REFERENCE = "iopaint==1.6.0 (model/lama.py LaMa.forward + model/base.py InpaintModel._pad_forward, hd_strategy=ORIGINAL)"
SIZE_POLICY = ("원본 해상도 1회 추론 — 내부 리사이즈 · 분할(HD CROP/RESIZE) 없음, 아래 · 오른쪽 symmetric 패딩으로 8의 배수 → "
               "결과를 원래 H×W로 자름")


# ---------------------------------------------------------------------------
# 전처리 · 후처리 (numpy만 — 로컬 테스트 대상)
# ---------------------------------------------------------------------------
def ceil_modulo(x: int, mod: int) -> int:
    return x if x % mod == 0 else (x // mod + 1) * mod


def pad_to_modulo(arr: np.ndarray, mod: int = PAD_MOD) -> np.ndarray:
    """iopaint `pad_img_to_modulo`(square=False · min_size=None)와 같다: (H, W) · (H, W, C) → 아래 · 오른쪽 symmetric 패딩, 채널 축 유지."""
    if arr.ndim == 2:
        arr = arr[:, :, np.newaxis]
    h, w = arr.shape[:2]
    return np.pad(arr, ((0, ceil_modulo(h, mod) - h), (0, ceil_modulo(w, mod) - w), (0, 0)), mode="symmetric")


def preprocess(image: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(H, W, 3) uint8 RGB + (H, W) uint8 → 모델 입력 (1, 3, H', W') float32 [0, 1] · (1, 1, H', W') int64 {0, 1}. H' · W'는 8의 배수."""
    img = pad_to_modulo(image).transpose(2, 0, 1).astype(np.float32) / 255
    m = pad_to_modulo(mask).transpose(2, 0, 1).astype(np.float32) / 255
    m = (m > 0).astype(np.int64)
    return img[np.newaxis], m[np.newaxis]


def postprocess(out: np.ndarray, height: int, width: int) -> np.ndarray:
    """모델 출력 (3, H', W') float → 원래 크기 (H, W, 3) uint8 RGB. iopaint와 같이 clip(×255) 뒤 버림 변환.
    변환 전에 모양(3채널 · 8의 배수 패딩 크기)과 유한값을 검사한다 — NaN · Inf가 clip · uint8 변환으로 0 · 255가 되어
    정상 이미지처럼 저장되지 않게 ValueError(추론 실패)로 올린다."""
    expected = (3, ceil_modulo(height, PAD_MOD), ceil_modulo(width, PAD_MOD))
    if not isinstance(out, np.ndarray) or out.shape != expected:
        raise ValueError(f"모델 출력 모양 {getattr(out, 'shape', type(out).__name__)} ≠ 기대 {expected}")
    if not np.issubdtype(out.dtype, np.floating):
        raise ValueError(f"모델 출력 dtype {out.dtype} — 실수여야 한다")
    bad = int((~np.isfinite(out)).sum())
    if bad:
        raise ValueError(f"모델 출력에 NaN · Inf 값 {bad}개 — 추론 실패로 본다(0 · 255로 바꿔 저장하지 않음)")
    res = np.clip(out.transpose(1, 2, 0) * 255, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(res[:height, :width, :])


def file_digests(path: Path) -> dict[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
    return {"md5": md5.hexdigest(), "sha256": sha.hexdigest()}


def default_weights_path() -> Path:
    """iopaint와 같은 캐시 위치: torch.hub.get_dir()/checkpoints/big-lama.pt (TORCH_HOME을 따른다)."""
    import torch

    return Path(torch.hub.get_dir()) / "checkpoints" / LAMA_FILENAME


# ---------------------------------------------------------------------------
# 모델
# ---------------------------------------------------------------------------
class LamaModel:
    """big-lama.pt(TorchScript) GPU · FP32 어댑터. 생성 = 초기화(가중치 확인 · 로드). `inpaint`는 호출당 추론 1회."""

    def __init__(self, weights_path: str | Path | None = None, device: str = "cuda:0"):
        try:
            import torch
        except ImportError as e:  # 모델 미설치 — 종료 코드 3
            raise ModelNotAvailable(f"torch를 불러올 수 없다({e}) — LaMa는 GPU 환경에서만 실행한다(#72)") from e
        self._torch = torch
        path = Path(weights_path) if weights_path else default_weights_path()
        if not path.is_file():
            raise ModelNotAvailable(f"LaMa 가중치가 없다: {path} — 어댑터는 내려받지 않는다(별도 준비, 기준 URL {LAMA_URL})")
        if not str(device).startswith("cuda") or not torch.cuda.is_available():
            raise RuntimeError(f"CUDA를 쓸 수 없다(device={device}, available={torch.cuda.is_available()}) — CPU 대체 없음")
        t0 = time.perf_counter()
        digests = file_digests(path)
        if digests["md5"] != LAMA_MD5:
            raise RuntimeError(f"LaMa 가중치 MD5 {digests['md5']} ≠ iopaint 기준 {LAMA_MD5}: {path}")
        hash_s = time.perf_counter() - t0
        self.device = torch.device(device)
        t1 = time.perf_counter()
        model = torch.jit.load(str(path), map_location="cpu")
        model = model.to(self.device).eval()
        torch.cuda.synchronize(self.device)
        load_s = time.perf_counter() - t1
        dtypes = sorted({str(p.dtype) for p in model.parameters()} | {str(b.dtype) for b in model.buffers() if b.is_floating_point()})
        if dtypes != ["torch.float32"]:
            raise RuntimeError(f"가중치 dtype {dtypes} — FP32만 사용한다(변환하지 않음)")
        self.model = model
        self.weights = {"path": str(path), "bytes": path.stat().st_size, **digests, "expected_md5": LAMA_MD5, "source_url": LAMA_URL,
                        "param_dtypes": dtypes}
        self.init = {"hash_s": round(hash_s, 3), "load_s": round(load_s, 3), "total_s": round(time.perf_counter() - t0, 3)}
        self.calls: list[dict[str, Any]] = []  # 호출별 측정(실험 기록용, 결과 형식 아님)

    def describe(self) -> dict[str, Any]:
        """개발용 모델 설명(재현 정보). BE 계약이 아니다."""
        t = self._torch
        props = t.cuda.get_device_properties(self.device)
        return {
            "name": "lama", "adapter": "pipeline.stages.inpaint_lama.LamaModel", "reference": REFERENCE,
            "weights": self.weights, "device": str(self.device), "gpu": props.name,
            "gpu_total_mib": props.total_memory // 2**20, "capability": f"{props.major}.{props.minor}",
            "precision": "fp32", "autocast": False, "grad": "inference_mode", "batch": 1, "pad_mod": PAD_MOD, "pad_mode": "symmetric (bottom/right)",
            "size_policy": SIZE_POLICY, "color": "RGB in / RGB out", "mask_threshold": "mask/255 > 0 → 1 (int64)",
            "output_cast": "clip(x*255, 0, 255).astype(uint8) (truncate)",
            "torch": t.__version__, "cuda": t.version.cuda, "cudnn": t.backends.cudnn.version(),
            "cudnn_benchmark": t.backends.cudnn.benchmark, "cudnn_deterministic": t.backends.cudnn.deterministic,
            "numpy": np.__version__, "python": platform.python_version(), "init": self.init,
            "retry": False, "auto_resize": False, "cpu_fallback": False,
        }

    def inpaint(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        t = self._torch
        h, w = image.shape[:2]
        t0 = time.perf_counter()
        img, m = preprocess(image, mask)
        t.cuda.synchronize(self.device)
        t.cuda.reset_peak_memory_stats(self.device)
        t1 = time.perf_counter()
        with t.inference_mode():
            x = t.from_numpy(img).to(self.device)
            xm = t.from_numpy(m).to(self.device)
            t.cuda.synchronize(self.device)
            t2 = time.perf_counter()
            y = self.model(x, xm)
            t.cuda.synchronize(self.device)
            t3 = time.perf_counter()
            out = y[0].detach().float().cpu().numpy()
        t4 = time.perf_counter()
        res = postprocess(out, h, w)
        self.calls.append({
            "shape": [h, w], "padded": list(img.shape[2:]), "mask_px": int((mask > 0).sum()),
            "pre_s": round(t1 - t0, 4), "h2d_s": round(t2 - t1, 4), "forward_s": round(t3 - t2, 4), "d2h_s": round(t4 - t3, 4),
            "post_s": round(time.perf_counter() - t4, 4), "total_s": round(time.perf_counter() - t0, 4),
            "peak_allocated_mib": round(t.cuda.max_memory_allocated(self.device) / 2**20, 1),
            "peak_reserved_mib": round(t.cuda.max_memory_reserved(self.device) / 2**20, 1),
        })
        return res
