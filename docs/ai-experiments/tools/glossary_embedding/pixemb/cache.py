"""임베딩 캐시 — (모델 · 정규화 · 최대 길이 · 텍스트)가 같으면 저장된 벡터를 쓴다.

키에 모델 설정을 넣으므로 설정이 바뀌면 자동으로 캐시 미스가 난다. 캐시가 계산 결과와 같은지는
verify_cache()로 표본을 다시 임베딩해 확인한다(실험 05의 12번 항목).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Sequence

import numpy as np

import config


def cache_key(text: str) -> str:
    spec = f"{config.MODEL_NAME}|norm={config.NORMALIZE}|maxlen={config.MAX_SEQ_LENGTH}|"
    return hashlib.sha256((spec + text).encode("utf-8")).hexdigest()


class EmbedCache:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.EMBED_CACHE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute("CREATE TABLE IF NOT EXISTS emb (key TEXT PRIMARY KEY, dim INTEGER, vec BLOB)")

    def get_many(self, texts: Sequence[str]) -> dict[str, np.ndarray]:
        keys = {cache_key(t): t for t in texts}
        out: dict[str, np.ndarray] = {}
        items = list(keys)
        for i in range(0, len(items), 900):  # SQLite 변수 개수 한도
            chunk = items[i:i + 900]
            q = f"SELECT key, dim, vec FROM emb WHERE key IN ({','.join('?' * len(chunk))})"
            for k, dim, blob in self.db.execute(q, chunk):
                out[keys[k]] = np.frombuffer(blob, dtype=np.float32, count=dim)
        return out

    def put_many(self, texts: Sequence[str], vectors: np.ndarray) -> None:
        rows = [(cache_key(t), int(v.shape[0]), v.astype(np.float32).tobytes()) for t, v in zip(texts, vectors)]
        self.db.executemany("INSERT OR REPLACE INTO emb VALUES (?, ?, ?)", rows)
        self.db.commit()

    def count(self) -> int:
        return self.db.execute("SELECT count(*) FROM emb").fetchone()[0]

    def close(self) -> None:
        self.db.close()


def encode_cached(embedder, texts: Sequence[str], cache: EmbedCache | None = None,
                  show_progress: bool = False) -> tuple[np.ndarray, dict]:
    """캐시에 있으면 꺼내고 없는 것만 임베딩한다. (벡터, 통계) 반환.

    embedder 는 Embedder 또는 Embedder 를 돌려주는 함수(get_embedder). 함수면 캐시 미스가 있을 때만 모델을 올린다.
    """
    own = cache is None
    cache = cache or EmbedCache()
    try:
        uniq = list(dict.fromkeys(texts))
        hit = cache.get_many(uniq)
        miss = [t for t in uniq if t not in hit]
        if miss:
            if not hasattr(embedder, "encode"):
                embedder = embedder()
            vecs = embedder.encode(miss, show_progress=show_progress or len(miss) > 200)
            cache.put_many(miss, vecs)
            hit.update(zip(miss, vecs))
        out = np.stack([hit[t] for t in texts]) if texts else np.zeros((0, config.EMBED_DIM), np.float32)
        return out, {"texts": len(texts), "unique": len(uniq), "cache_hit": len(uniq) - len(miss), "embedded": len(miss)}
    finally:
        if own:
            cache.close()


def verify_cache(embedder, texts: Sequence[str], cache: EmbedCache | None = None) -> dict:
    """캐시 벡터와 지금 다시 계산한 벡터의 코사인 최소값 · 최대 절대 오차."""
    own = cache is None
    cache = cache or EmbedCache()
    try:
        stored = cache.get_many(texts)
        texts = [t for t in texts if t in stored]
        if not texts:
            return {"n": 0}
        fresh = embedder.encode(texts)
        old = np.stack([stored[t] for t in texts])
        cos = (fresh * old).sum(1)
        return {"n": len(texts), "cos_min": float(cos.min()), "max_abs_diff": float(np.abs(fresh - old).max())}
    finally:
        if own:
            cache.close()
