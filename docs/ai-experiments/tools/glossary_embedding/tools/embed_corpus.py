"""용어집 전체의 임베딩을 캐시에 채운다(인덱스는 열지 않는다 — 모델만 메모리에 올림).

1,000행씩 나눠 캐시에 저장하므로 중간에 멈춰도 다시 실행하면 이어서 한다.
그다음 `python 05_experiments.py build --text-mode <모드>` 가 캐시만으로 컬렉션을 만든다.

실행: python tools/embed_corpus.py --text-mode ko_en [--chunk 1000]
"""
import argparse
import gc
import sys
import time
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
from pixemb import setup_console  # noqa: E402
from pixemb.cache import EmbedCache, encode_cached  # noqa: E402
from pixemb.embedder import get_embedder  # noqa: E402
from pixemb.glossary import embed_text, load_source  # noqa: E402


def free_gb() -> float:
    return psutil.virtual_memory().available / 1e9


def wait_for_memory(min_free_gb: float, max_wait_s: int = 300) -> None:
    """여유 RAM 이 min_free_gb 아래면 메모리를 정리하고 회복될 때까지 기다린다(OOM 예방)."""
    if free_gb() >= min_free_gb:
        return
    gc.collect()
    t0 = time.perf_counter()
    while free_gb() < min_free_gb and time.perf_counter() - t0 < max_wait_s:
        print(f"[embed] 여유 RAM {free_gb():.2f}GB < {min_free_gb}GB — 대기 중", flush=True)
        time.sleep(10)
        gc.collect()


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-mode", choices=["ko", "ko_en"], default=config.EMBED_TEXT_MODE)
    ap.add_argument("--chunk", type=int, default=500, help="이만큼 임베딩할 때마다 캐시에 저장")
    ap.add_argument("--min-free-gb", type=float, default=0.5, help="여유 RAM 이 이보다 적으면 다음 묶음 전에 대기")
    args = ap.parse_args()
    df = load_source()
    texts = [embed_text(r, args.text_mode) for _, r in df.iterrows()]
    cache = EmbedCache()
    try:
        uniq = list(dict.fromkeys(texts))
        have = cache.get_many(uniq)
        todo = [t for t in uniq if t not in have]
        del have
        print(f"[embed] {args.text_mode}: 전체 {len(texts)} · 캐시에 없음 {len(todo)}")
        t0, done = time.perf_counter(), 0
        for i in range(0, len(todo), args.chunk):
            wait_for_memory(args.min_free_gb)
            part = todo[i:i + args.chunk]
            encode_cached(get_embedder, part, cache)
            done += len(part)
            el = time.perf_counter() - t0
            print(f"[embed] {done}/{len(todo)} · {done / el:.1f}건/s · 남은 시간 약 "
                  f"{(len(todo) - done) / max(done / el, 1e-9) / 60:.0f}분 · 여유 RAM {free_gb():.2f}GB · "
                  f"프로세스 {psutil.Process().memory_info().rss / 1e9:.2f}GB", flush=True)
        print(f"[embed] 완료 · 캐시 {cache.count()}건")
    finally:
        cache.close()


if __name__ == "__main__":
    main()
