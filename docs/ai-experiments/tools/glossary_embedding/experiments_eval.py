"""05_experiments.py eval · latency 본체 — 평가셋 v2(data/eval_team_v2.csv) 기준.

메모리(RAM 8GB) 때문에 단계를 나눈다:
  1) 모델로 쿼리 텍스트를 전부 임베딩(캐시에 저장) → 모델 해제
  2) 인덱스를 열고 저장된 벡터로 검색만 한다(Qdrant 로컬 query_batch_points — NumPy 가속 · 일치 검증은 없음)
두 번째 실행부터는 쿼리 벡터가 캐시에 있어 모델을 올리지 않는다.

비교 축(--variants):
  kind  : all = 종류 필터 없음 / split = 전성분 문단은 성분만 · 그 밖은 성분 제외
  fix   : 전성분 문단 쿼리에 데이터팀 OCR 치환 규칙 적용 여부
  block : 전성분 묶음을 통째로 1회 검색(whole) / 쉼표로 나눠 성분마다 검색(split)

후보 수 (PR #52 리뷰 반영, 2026-10-06)
  - 검색 요청마다 RETRIEVE_LIMIT(30)개를 받는다(통째 · 조각 모두 같음). 조각 결과는 ID별 최고 점수로 합친다.
  - 모든 지표는 **같은 최종 후보 수 K**(1·3·5·10·20·30)로 계산한다. 정답 개수로 K를 정하지 않는다.
  - 전성분 묶음은 추가로 `@5n` — n = **입력에서 센 조각 수**(정답과 무관)로 K = 5n.
  - 평가에 쓴 후보 전부를 ranked_full 로 보존하고, 화면 표시용 ranked_top10 만 따로 둔다. 임계값표도 ranked_full 을 쓴다.

지표: Hit@K · MRR(정답 = must ∪ any) · must_recall@K · all_must@K · any_hit@K · success@K(all_must 그리고 any 가 있으면 any_hit)
음성: negative · negative_locale(관련 용어가 없는 문항)만. 1등 점수 ≥ t 면 오탐.
카테고리 차단(category_blocked): 음성에서 뺐다. forbidden_ids 가 상위 K 안에 나왔는지 · 카테고리 누출(결과의
  internal_category 가 {작업 카테고리, common} 밖)이 있는지로 따로 센다. 누출은 카테고리를 지정한 모든 문항에서 센다.
"""
from __future__ import annotations

import gc
import json
import time

import numpy as np
import pandas as pd

import config

EVAL_V2 = config.ROOT / "data" / "eval_team_v2.csv"
RETRIEVE_LIMIT = 30
KS = (1, 3, 5, 10, 20, 30)
NEG_TYPES = {"negative", "negative_locale"}
BLOCKED_TYPE = "category_blocked"


def load_eval_v2(path=EVAL_V2) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    split = lambda s: [x for x in s.split("|") if x]  # noqa: E731
    df["must"] = df["must_ids"].map(split)
    df["any"] = df["any_ids"].map(split)
    df["forbidden"] = df["forbidden_ids"].map(split) if "forbidden_ids" in df.columns else [[] for _ in range(len(df))]
    df["positive"] = df.apply(lambda r: bool(r["must"] or r["any"]), axis=1)
    df["category"] = df["category"].replace({"": None})
    return df


def split_ingredients(q: str) -> list[str]:
    """전성분 문단을 쉼표로 나눈다. '1,2-헥산다이올'처럼 숫자 사이 쉼표는 이름의 일부로 다시 붙인다."""
    merged: list[str] = []
    for p in (x.strip() for x in q.split(",")):
        if not p:
            continue
        if merged and merged[-1][-1:].isdigit() and p[:1].isdigit():
            merged[-1] = merged[-1] + "," + p
        else:
            merged.append(p)
    return merged


def query_texts(df: pd.DataFrame, fix: bool, block: str) -> list[list[str]]:
    """문항마다 실제로 검색할 텍스트 목록(묶음 split 이면 여러 개)."""
    from pixemb.textnorm import ocr_fix_ingredient
    out = []
    for _, r in df.iterrows():
        q = r["query"]
        pieces = split_ingredients(q) if (block == "split" and r["query_type"] == "ingredient_block") else [q]
        if fix and r["context"] == "ingredient_list":
            pieces = [ocr_fix_ingredient(p) for p in pieces]
        out.append(pieces)
    return out


def embed_all(texts: list[str]) -> dict[str, np.ndarray]:
    from pixemb.cache import encode_cached
    from pixemb.embedder import get_embedder
    uniq = list(dict.fromkeys(texts))
    t0 = time.perf_counter()
    vecs, stats = encode_cached(get_embedder, uniq)
    print(f"[embed] 쿼리 {len(uniq)}개 · 캐시 {stats['cache_hit']} · 새로 {stats['embedded']} · {time.perf_counter() - t0:.1f}s")
    return dict(zip(uniq, vecs))


def eval_k(qt: str, query: str) -> int:
    """임계값표 · 기본 성공 지표에 쓰는 최종 후보 수 — 정답 개수가 아니라 입력으로 정한다."""
    return 5 * len(split_ingredients(query)) if qt == "ingredient_block" else 5


def run_variant(client, name: str, df: pd.DataFrame, pieces: list[list[str]], vec: dict, kind: str,
                limit: int = RETRIEVE_LIMIT) -> pd.DataFrame:
    from pixemb.store import build_filter
    from qdrant_client import models
    reqs, owner = [], []
    for i, (r, ps) in enumerate(zip(df.itertuples(), pieces)):
        tk = "ingredient" if (kind == "split" and r.context == "ingredient_list") else None
        ex = ("ingredient",) if (kind == "split" and r.context != "ingredient_list") else ()
        for p in ps:
            reqs.append(models.QueryRequest(query=vec[p].tolist(), filter=build_filter(r.category, term_kind=tk, exclude_kinds=ex),
                                            limit=limit, with_payload=["gl_id", "internal_category"]))
            owner.append(i)
    t0 = time.perf_counter()
    hits: list[list[tuple[str, float, str]]] = [[] for _ in range(len(df))]
    for s in range(0, len(reqs), 64):
        for j, res in enumerate(client.query_batch_points(name, requests=reqs[s:s + 64])):
            hits[owner[s + j]].extend((p.payload["gl_id"], p.score, p.payload.get("internal_category")) for p in res.points)
    search_s = time.perf_counter() - t0
    rows = []
    for i, ((_, r), h) in enumerate(zip(df.iterrows(), hits)):
        # 여러 조각 결과는 ID별 최고 점수로 합쳐 점수순 정렬(동점은 ID순 — 실행마다 같게)
        best: dict[str, float] = {}
        cat: dict[str, str] = {}
        for gid, sc, c in h:
            if sc > best.get(gid, -2.0):
                best[gid] = sc
            cat[gid] = c
        ranked = sorted(best.items(), key=lambda x: (-x[1], x[0]))
        ids = [g for g, _ in ranked]
        must, any_, forb = set(r["must"]), set(r["any"]), set(r["forbidden"])
        rel = must | any_
        allowed = {r["category"], "common"} if r["category"] else None
        k_in = eval_k(r["query_type"], r["query"])
        row = {"qid": r["qid"], "query_type": r["query_type"], "source": r["source"], "independent": r["independent"],
               "context": r["context"], "query": r["query"], "category": r["category"] or "", "positive": r["positive"],
               "n_pieces": len(pieces[i]), "k_eval": k_in, "candidates": len(ids),
               "top1": ids[0] if ids else "", "top1_score": ranked[0][1] if ranked else 0.0,
               "category_leak": int(sum(1 for g in ids if allowed and cat.get(g) not in allowed)),
               "ranked_top10": json.dumps(ranked[:10]),
               "ranked_full": json.dumps(ranked)}
        if forb:
            for kk in KS:
                row[f"forbidden@{kk}"] = float(bool(set(ids[:kk]) & forb))
        if r["positive"]:
            first = next((j + 1 for j, g in enumerate(ids) if g in rel), None)
            row["rr"] = 1 / first if first else 0.0
            ks = list(KS) + (["5n"] if r["query_type"] == "ingredient_block" else [])
            for kk in ks:
                top = set(ids[:k_in if kk == "5n" else kk])
                row[f"hit@{kk}"] = float(bool(top & rel))
                row[f"must_recall@{kk}"] = len(top & must) / len(must) if must else np.nan
                row[f"all_must@{kk}"] = float(must <= top) if must else np.nan
                row[f"any_hit@{kk}"] = float(bool(top & any_)) if any_ else np.nan
                row[f"success@{kk}"] = float((must <= top) and (bool(top & any_) if any_ else True))
        rows.append(row)
    out = pd.DataFrame(rows)
    out.attrs["search_s"] = search_s
    out.attrs["requests"] = len(reqs)
    return out


def summarize(pq: pd.DataFrame) -> pd.DataFrame:
    pos = pq[pq.positive]
    cols = ["hit@1", "hit@5", "hit@10", "rr", "must_recall@5", "all_must@5", "any_hit@5",
            "success@5", "success@10", "success@20", "success@30"]
    g = pos.groupby("query_type")[cols].mean()
    g.insert(0, "n", pos.groupby("query_type").size())
    g.insert(1, "independent", pos.groupby("query_type")["independent"].first())
    return g.rename(columns={"rr": "MRR"}).round(3)


def block_table(pq: pd.DataFrame) -> pd.DataFrame:
    """전성분 묶음 — 같은 최종 후보 수 K별 성분 전부 회수율 · 성분 회수율 + 입력 조각 기준 5n."""
    b = pq[pq.query_type == "ingredient_block"]
    rows = {}
    for kk in list(KS) + ["5n"]:
        rows[f"@{kk}"] = {"all_must": b[f"all_must@{kk}"].mean(), "must_recall": b[f"must_recall@{kk}"].mean()}
    return pd.DataFrame(rows).T.round(3)


def category_check(pq: pd.DataFrame) -> dict:
    blk = pq[pq.query_type == BLOCKED_TYPE]
    with_cat = pq[pq.category != ""]
    return {"blocked_queries": len(blk),
            "blocked_forbidden_hits": {f"@{k}": int(blk[f"forbidden@{k}"].sum()) for k in KS if f"forbidden@{k}" in blk},
            "blocked_top1": blk[["query", "category", "top1", "top1_score"]].to_dict("records"),
            "queries_with_category": len(with_cat), "queries_with_leak": int((with_cat.category_leak > 0).sum())}


def threshold_table(df_eval: pd.DataFrame, pq: pd.DataFrame, grid=None) -> pd.DataFrame:
    """임계값 t 별 양성 회수율 · 음성 오탐률 · 주입 정밀도.

    양성: 최종 후보 K(일반 5 · 전성분 5n — 입력으로 정함) 안에서 점수 ≥ t 인 결과만으로 성공 여부를 다시 본다.
          검색에 실패한 양성도 분모에 들어간다. 후보는 ranked_full(평가에 쓴 전부)을 쓴다.
    음성: negative · negative_locale 만(카테고리 차단 문항은 따로 — category_check).
    """
    grid = grid if grid is not None else np.round(np.arange(0.40, 0.91, 0.01), 2)
    ev = df_eval.set_index("qid")
    pos, neg = pq[pq.positive], pq[pq.query_type.isin(NEG_TYPES)]
    pos_rank = [(json.loads(r.ranked_full), set(ev.at[r.qid, "must"]), set(ev.at[r.qid, "any"]), int(r.k_eval))
                for r in pos.itertuples()]
    out = []
    for t in grid:
        ok = inj_tot = inj_ok = 0
        for ranked, must, any_, k in pos_rank:
            top = {g for g, s in ranked[:k] if s >= t}
            ok += (must <= top) and (bool(top & any_) if any_ else True)
            inj_tot += len(top)
            inj_ok += len(top & (must | any_))
        fp = float((neg.top1_score >= t).mean()) if len(neg) else np.nan
        out.append({"t": t, "pos_recovered": ok / len(pos_rank), "neg_fp_rate": fp, "neg_n": len(neg),
                    "injection_precision": inj_ok / inj_tot if inj_tot else np.nan, "injected_per_query": inj_tot / len(pos_rank)})
    return pd.DataFrame(out).round(3)


def cmd_eval(argv) -> None:
    import argparse
    from pixemb.store import get_client
    from experiments_util import save
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="all:nofix:whole,split:nofix:whole,split:fix:whole,split:nofix:split,split:fix:split")
    ap.add_argument("--embed-only", action="store_true", help="쿼리 임베딩만 하고 끝(모델 단계)")
    ap.add_argument("--tag", default="r2-review", help="결과 하위 폴더 — 이전 결과를 덮어쓰지 않는다")
    args = ap.parse_args(argv)
    df = load_eval_v2()
    variants = [tuple(v.split(":")) for v in args.variants.split(",")]
    plans = {v: query_texts(df, fix=(v[1] == "fix"), block=v[2]) for v in variants}
    all_texts = [p for ps in plans.values() for q in ps for p in q]

    # 1) 모델 단계
    vec = embed_all(all_texts)
    from pixemb import embedder as E
    E.get_embedder.cache_clear()
    gc.collect()
    if args.embed_only:
        return

    # 2) 검색 단계
    name = config.collection()
    client = get_client("local", name=name)
    results, summaries, curves, blocks, cats = {}, {}, {}, {}, {}
    try:
        for v in variants:
            key = ":".join(v)
            pq = run_variant(client, name, df, plans[v], vec, kind=v[0])
            results[key] = pq
            summaries[key] = summarize(pq)
            curves[key] = threshold_table(df, pq)
            blocks[key] = block_table(pq)
            cats[key] = category_check(pq)
            print(f"\n==== {key} · {config.collection()} · 요청 {pq.attrs['requests']} · 검색 {pq.attrs['search_s']:.1f}s", flush=True)
            print(summaries[key].to_string(), flush=True)
            print("-- 전성분 묶음(같은 최종 후보 수 K):", blocks[key].T.to_dict(), flush=True)
            print("-- 카테고리:", {k: v for k, v in cats[key].items() if k != "blocked_top1"}, flush=True)
    finally:
        client.close()

    import hashlib
    out_dir = config.ROOT / "results" / __import__("datetime").date.today().isoformat() / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    tm = config.EMBED_TEXT_MODE
    for key, pq in results.items():
        pq.to_csv(out_dir / f"eval_{tm}_{key.replace(':', '_')}.csv", index=False, encoding="utf-8-sig")
    save(f"eval_{tm}", {
        "collection": config.collection(), "eval_set": str(EVAL_V2), "rows": len(df),
        "eval_set_sha256": hashlib.sha256(EVAL_V2.read_bytes()).hexdigest(),
        "retrieve_limit": RETRIEVE_LIMIT, "ks": list(KS), "neg_types": sorted(NEG_TYPES),
        # 조건별 총 검색 요청 수 · 검색 시간(분할은 조각마다 요청하므로 요청이 늘어난다)
        "runs": {k: {"requests": pq.attrs["requests"], "search_s": round(pq.attrs["search_s"], 1)} for k, pq in results.items()},
        "summary": {k: s.reset_index().to_dict("records") for k, s in summaries.items()},
        "block": {k: b.reset_index().rename(columns={"index": "k"}).to_dict("records") for k, b in blocks.items()},
        "category": cats,
        "threshold": {k: c.to_dict("records") for k, c in curves.items()},
        "negatives": {k: pq[pq.query_type.isin(NEG_TYPES)][["qid", "query_type", "query", "top1", "top1_score"]].to_dict("records")
                      for k, pq in results.items()},
    }, sub=args.tag)


def cmd_latency(argv) -> None:
    import argparse
    import psutil
    from pixemb.embedder import get_embedder
    from pixemb.store import build_filter, get_client
    from experiments_util import save
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    args = ap.parse_args(argv)
    proc = psutil.Process()
    df = load_eval_v2()
    qs = df[df.query_type.isin(["phrase_pair", "ingredient_ocr", "cert_multi"])]["query"].tolist()[: args.n]
    res = {"rss_start_gb": round(proc.memory_info().rss / 1e9, 2)}
    t0 = time.perf_counter()
    e = get_embedder()
    res["model_load_s"] = round(time.perf_counter() - t0, 1)
    res["rss_after_model_gb"] = round(proc.memory_info().rss / 1e9, 2)
    e.encode(["워밍업"])
    enc = []
    for q in qs:
        t0 = time.perf_counter(); e.encode([q]); enc.append(time.perf_counter() - t0)
    t0 = time.perf_counter(); e.encode(qs); batch_s = time.perf_counter() - t0
    name = config.collection()
    t0 = time.perf_counter()
    client = get_client("local", name=name)
    res["index_open_s"] = round(time.perf_counter() - t0, 1)
    res["rss_after_index_gb"] = round(proc.memory_info().rss / 1e9, 2)
    try:
        vecs = e.encode(qs)
        lat = {"no_filter": [], "split_exclude_ingredient": [], "ingredient_only": []}
        for v in vecs:
            for key, f in [("no_filter", build_filter(None)), ("split_exclude_ingredient", build_filter("common", exclude_kinds=("ingredient",))),
                           ("ingredient_only", build_filter("common", term_kind="ingredient"))]:
                t0 = time.perf_counter()
                client.query_points(name, query=v.tolist(), query_filter=f, limit=10)
                lat[key].append(time.perf_counter() - t0)
        e2e = []
        for q in qs[:20]:
            t0 = time.perf_counter()
            v = e.encode([q])[0]
            client.query_points(name, query=v.tolist(), query_filter=build_filter("common", exclude_kinds=("ingredient",)), limit=5)
            e2e.append(time.perf_counter() - t0)
    finally:
        client.close()
    pct = lambda xs: {"p50_ms": round(1000 * float(np.percentile(xs, 50)), 1), "p95_ms": round(1000 * float(np.percentile(xs, 95)), 1)}  # noqa: E731
    res.update({"device": e.device, "points": 20653, "queries": len(qs),
                "encode_single": pct(enc), "encode_batch_per_query_ms": round(1000 * batch_s / len(qs), 1),
                "search": {k: pct(v) for k, v in lat.items()}, "end_to_end_single": pct(e2e),
                "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
                "note": "Qdrant 로컬 모드는 전수 비교(brute force)라 검색 지연은 점 수에 비례한다. CPU · RAM 8GB PC 실측."})
    print(json.dumps(res, ensure_ascii=False, indent=2))
    save(f"latency_{config.EMBED_TEXT_MODE}", res)
