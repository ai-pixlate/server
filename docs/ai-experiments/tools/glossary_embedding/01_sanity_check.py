"""1단계: BGE-M3가 제대로 로드되고 벡터가 예상대로 나오는지 확인한다 (Qdrant 불필요).

확인 항목
  - 디바이스 / 로드 시간
  - 차원 = 1024, L2 norm = 1 (normalize_embeddings=True)
  - 도메인 쌍의 코사인 유사도 (옛 표기, 한↔영, 유의어, 무관한 문장)
  - 처리 속도

실행:  python 01_sanity_check.py
"""
import time

import numpy as np
import pandas as pd

import config
from pixemb import setup_console
from pixemb.embedder import cosine_matrix, get_embedder

# (A, B, 기대) — 기대: 높음 / 중간 / 낮음
PAIRS = [
    ("에칠헥실트리아존", "에틸헥실트리아존", "높음"),       # 식약처 옛 표기 ↔ 올리브영 표기
    ("티타늄디옥사이드", "티타늄다이옥사이드", "높음"),
    ("인체적용시험", "clinical study", "높음"),             # 한↔영 (다국어 모델)
    ("무향", "Fragrance-free", "높음"),
    ("속보습", "속수분", "높음"),                           # 유의어
    ("저자극", "gentle", "중간"),
    ("글리세릴올리에이트", "글리세릴라우레이트", "중간"),   # 다른 성분인데 글자가 비슷함 — 너무 높으면 위험
    ("벤질글라이콜", "벤질알코올", "중간"),
    ("인체적용시험", "배송비 무료 이벤트", "낮음"),
    ("나이아신아마이드", "카카오톡 채널 추가", "낮음"),
]


def main() -> None:
    setup_console()
    emb = get_embedder()
    print(f"모델: {emb}\n로드 시간: {emb.load_seconds:.1f}s\n")

    # ── 차원 / 정규화 ─────────────────────────────
    vecs = emb.encode(["인체적용시험", "clinical study"])
    norms = np.linalg.norm(vecs, axis=1)
    print(f"shape = {vecs.shape}  (기대: (2, {config.EMBED_DIM}))")
    print(f"L2 norm = {np.round(norms, 5)}  (기대: 1.0)")
    assert vecs.shape[1] == config.EMBED_DIM
    assert np.allclose(norms, 1.0, atol=1e-3)
    print("✓ 차원·정규화 OK\n")

    # ── 쌍 유사도 ────────────────────────────────
    a = emb.encode([p[0] for p in PAIRS])
    b = emb.encode([p[1] for p in PAIRS])
    sims = (a * b).sum(axis=1)  # 정규화돼 있으므로 내적 = 코사인
    df = pd.DataFrame({"A": [p[0] for p in PAIRS], "B": [p[1] for p in PAIRS],
                       "기대": [p[2] for p in PAIRS], "cosine": np.round(sims, 4)})
    print("도메인 쌍 코사인 유사도")
    print(df.to_string(index=False))
    print()

    # ── 작은 유사도 행렬 ──────────────────────────
    terms = ["인체적용시험", "피부과 테스트 완료", "비건", "무향", "미백", "clinical study", "Vegan", "brightening"]
    m = cosine_matrix(emb.encode(terms), emb.encode(terms))
    print("유사도 행렬")
    print(pd.DataFrame(np.round(m, 3), index=terms, columns=terms).to_string())
    print()

    # ── 속도 ─────────────────────────────────────
    sample = [f"피부 속까지 촉촉한 속보습 크림 {i}" for i in range(64)]
    t0 = time.perf_counter()
    emb.encode(sample)
    dt = time.perf_counter() - t0
    print(f"속도: 짧은 문장 {len(sample)}개 {dt:.2f}s → {len(sample) / dt:.1f}건/s "
          f"(device={emb.device}, batch={config.BATCH_SIZE})")


if __name__ == "__main__":
    main()
