"""BGE-M3 dense 임베딩 래퍼 (sentence-transformers)."""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Iterable

import numpy as np

import config


def _pick_device(device: str | None) -> str:
    if device:
        return device
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Embedder:
    """BGE-M3를 dense 1024차원 · L2 정규화로 쓴다.

    BGE-M3는 bge-v1.5와 달리 쿼리 앞에 instruction 접두어를 붙이지 않는다.
    그래서 문서(용어)와 쿼리(OCR 블록) 모두 같은 encode()를 쓴다.
    """

    def __init__(self, model_name: str = config.MODEL_NAME, device: str | None = config.DEVICE):
        from sentence_transformers import SentenceTransformer

        self.device = _pick_device(device)
        t0 = time.perf_counter()
        self.model = SentenceTransformer(model_name, device=self.device)
        if self.device == "cuda":
            self.model.half()  # GPU면 fp16 — 메모리 절반, 속도 향상
        self.model.max_seq_length = config.MAX_SEQ_LENGTH
        self.load_seconds = time.perf_counter() - t0
        self.model_name = model_name
        # sentence-transformers 6.x에서 이름이 바뀜 (구버전 호환)
        get_dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dim = get_dim()
        if self.dim != config.EMBED_DIM:
            raise ValueError(f"임베딩 차원 {self.dim} ≠ 설정값 {config.EMBED_DIM}")

    def encode(
        self,
        texts: Iterable[str],
        batch_size: int = config.BATCH_SIZE,
        show_progress: bool = False,
    ) -> np.ndarray:
        """texts → (N, 1024) float32, 각 행 L2 norm = 1."""
        return self.model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=config.NORMALIZE,
            convert_to_numpy=True,
            show_progress_bar=show_progress,
        ).astype(np.float32)

    def __repr__(self) -> str:
        return f"Embedder({self.model_name}, device={self.device}, dim={self.dim})"


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """모델 로드는 수십 초 걸리므로 프로세스(노트북 커널)당 한 번만 한다."""
    return Embedder()


def cosine_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """정규화된 벡터끼리는 내적이 곧 코사인 유사도."""
    return a @ b.T
