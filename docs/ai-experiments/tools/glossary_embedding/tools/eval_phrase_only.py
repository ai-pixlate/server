"""문구(GL-M)만 임베딩한 컬렉션으로 문구 · 음성 · 카테고리 차단 문항을 다시 평가한다.

합의된 임베딩 대상은 마케팅 문구다. 메모리 모드(`:memory:`) 컬렉션을 새로 만들고 문구 행만 넣는다(성분 · 인증 제외).
벡터는 임베딩 캐시에서만 꺼낸다 — 캐시에 없으면 멈추고 모델을 올리지 않는다
(먼저 `tools/embed_corpus.py` · `05_experiments.py eval` 로 캐시를 채운다). 기존 영속 인덱스는 건드리지 않는다.
문구 벡터는 행마다 따로 계산되므로 전체 용어집을 임베딩할 때 만든 캐시 벡터를 그대로 써도 같다.

비교 기준(선택): 같은 문항을 전체 컬렉션(20,653행)에서 검색한 `05_experiments.py eval --tag <base-tag>` 결과.
비교 전에 같은 실험인지 확인하고 다르면 거부한다(BaseMismatch):
  - 평가셋 해시 · 용어집 원본(소스 · 파일 해시) · 임베딩 방식 · 모델 설정이 eval 결과 JSON의 `index` 기록과 같은지
  - 비교 문항의 ID · 질문 · 유형이 같은지(요약은 문항 ID를 맞춘 뒤 계산)

같은 임계값 비교 외에, 이번 일반 음성 문항에서 오탐 허용 건수(k건 이하)를 만족하도록 계산한 임계값과 같은 평가셋에서의
회수율도 낸다 — ko · ko_en 점수 분포가 달라서다. 같은 표본으로 임계값을 고르고 평가하므로 독립 데이터에서 오탐 k건을 보장하지 않는다.
검색 시간은 캐시된 쿼리 벡터로 검색만 한 시간이다(쿼리 임베딩 제외).

실행: python tools/eval_phrase_only.py --text-mode ko_en [--base-tag r3-phrase-base] [--tag phrase-only]
"""
import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd  # noqa: E402
from qdrant_client import QdrantClient, models  # noqa: E402

import config  # noqa: E402
import experiments_eval as E  # noqa: E402
from pixemb.store import PAYLOAD_FIELDS, model_spec, point_id  # noqa: E402

TYPES = ["phrase_pair", "phrase_avoid", "negative", "negative_locale", "category_blocked"]
BASES = ("all:nofix:whole", "split:fix:split")
COLS = ["hit@1", "hit@3", "hit@5", "hit@10", "rr", "success@5"]
TS = [0.5, 0.55, 0.57, 0.6, 0.65, 0.7]
FP_LEVELS = (0, 1, 2, 4)


class BaseMismatch(RuntimeError):
    """비교 기준 결과가 지금 실험과 다른 데이터 · 문항 · 설정으로 만들어졌다."""


def no_model():
    raise RuntimeError("임베딩 캐시 미스 — 이 도구는 모델을 올리지 않는다. 먼저 캐시를 채운다")


def phrase_rows(g: pd.DataFrame) -> pd.DataFrame:
    """임베딩 대상 = 마케팅 문구(GL-M)만. 성분(GL-I) · 인증(GL-C)은 넣지 않는다."""
    return g[g.gl_id.str.startswith("GL-M")].reset_index(drop=True)


def build_collection(client: QdrantClient, name: str, rows: pd.DataFrame, vectors) -> None:
    client.create_collection(name, vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE))
    client.upsert(name, points=[
        models.PointStruct(id=point_id(r.gl_id), vector=list(map(float, v)),
                           payload={k: (None if pd.isna(r.get(k)) else r.get(k)) for k in PAYLOAD_FIELDS if k in r.index})
        for (_, r), v in zip(rows.iterrows(), vectors)])


def provenance(g: pd.DataFrame, mode: str, eval_path: Path = None) -> dict:
    eval_path = eval_path or E.EVAL_V2
    return {"eval_set_sha256": hashlib.sha256(Path(eval_path).read_bytes()).hexdigest(),
            "source": config.GLOSSARY_SOURCE, "files": dict(g.attrs.get("files", {})), "text_mode": mode, "model": model_spec()}


def load_base(out_dir: Path, mode: str, sub: pd.DataFrame, want: dict, variants=BASES) -> dict:
    """비교 기준 결과를 읽고 같은 실험인지 확인한다. 다르면 BaseMismatch. 반환은 sub 의 문항 순서로 맞춘 결과."""
    jp = out_dir / f"eval_{mode}.json"
    if not jp.exists():
        raise BaseMismatch(f"{jp} 없음")
    j = json.loads(jp.read_text(encoding="utf-8"))
    idx = j.get("index") or {}
    checks = {"평가셋 해시": (j.get("eval_set_sha256"), want["eval_set_sha256"]),
              "용어집 소스": (idx.get("source"), want["source"]),
              "용어집 파일 해시": (idx.get("files"), want["files"]),
              "임베딩 방식": (idx.get("text_mode"), want["text_mode"]),
              "모델 설정": (idx.get("model"), want["model"])}
    bad = {k: v for k, v in checks.items() if v[0] != v[1]}
    if bad:
        raise BaseMismatch("비교 기준이 지금 실험과 다르다: " + "; ".join(f"{k} 기준={a!r} 지금={b!r}" for k, (a, b) in bad.items()))
    out = {}
    for v in variants:
        p = out_dir / f"eval_{mode}_{v.replace(':', '_')}.csv"
        if not p.exists():
            raise BaseMismatch(f"{p} 없음")
        b = pd.read_csv(p, encoding="utf-8-sig", dtype={"qid": str}).fillna({"category": ""}).set_index("qid")
        missing = sorted(set(sub.qid) - set(b.index))
        if missing:
            raise BaseMismatch(f"{v}: 비교 문항 {len(missing)}개 없음 {missing[:5]}")
        b = b.loc[list(sub.qid)].reset_index()
        for col in ("query", "query_type"):
            diff = (b[col].astype(str).values != sub[col].astype(str).values)
            if diff.any():
                raise BaseMismatch(f"{v}: {col} 불일치 {int(diff.sum())}건 예 {b.qid[diff].tolist()[:5]}")
        out[v] = b
    return out


def recall_at_fp(sub: pd.DataFrame, pq: pd.DataFrame, levels=FP_LEVELS) -> list[dict]:
    """이 평가셋의 일반 음성에서 오탐 k건 이하를 만족하도록 계산한 가장 낮은 임계값과, 같은 평가셋에서의 양성 회수율.

    임계값을 고른 표본과 평가한 표본이 같다 — 독립 데이터에서 오탐 k건 이하를 보장하는 값이 아니다.
    """
    s = sorted(pq[pq.query_type.isin(E.NEG_TYPES)].top1_score, reverse=True)
    out = []
    for k in levels:
        t = round(s[k] + 1e-6, 6) if k < len(s) else 0.0
        r = E.threshold_table(sub, pq, grid=[t]).iloc[0]
        out.append({"max_fp": k, "t": t, "pos_recovered": float(r.pos_recovered), "neg_fp_rate": float(r.neg_fp_rate)})
    return out


def main() -> None:
    from pixemb import setup_console
    from pixemb.cache import encode_cached
    from pixemb.glossary import embed_text, load_source
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--text-mode", default=config.EMBED_TEXT_MODE, choices=["ko", "ko_en"])
    ap.add_argument("--base-tag", default="r3-phrase-base", help="비교 기준(전체 컬렉션 eval) 결과 하위 폴더. 빈 값이면 비교 안 함")
    ap.add_argument("--tag", default="phrase-only", help="결과 하위 폴더")
    args = ap.parse_args()
    mode = args.text_mode
    day = config.ROOT / "results" / dt.date.today().isoformat()

    g = load_source()
    ph = phrase_rows(g)
    gv, st = encode_cached(no_model, [embed_text(r, mode) for _, r in ph.iterrows()])
    print(f"[{mode}] 문구 {len(ph)}행 · 캐시 {st['cache_hit']} · 새로 {st['embedded']}")
    want = provenance(g, mode)

    df = E.load_eval_v2()
    sub = df[df.query_type.isin(TYPES)].reset_index(drop=True)
    base = load_base(day / args.base_tag, mode, sub, want) if args.base_tag else {}
    if base:
        print(f"비교 기준 확인 통과 — {args.base_tag}: 평가셋 · 용어집 · 임베딩 방식 · 모델 설정 · 문항 {len(sub)}개 일치")

    name = "phrase_only"
    client = QdrantClient(":memory:")
    build_collection(client, name, ph, gv)
    uniq = list(dict.fromkeys(sub["query"]))
    qv, _ = encode_cached(no_model, uniq)
    pq = E.run_variant(client, name, sub, [[q] for q in sub["query"]], dict(zip(uniq, qv)), kind="all")
    client.close()
    print(f"검색(캐시된 쿼리 벡터 · 쿼리 임베딩 제외) {pq.attrs['requests']}요청 · {pq.attrs['search_s']:.2f}s")

    pos = sub[sub.positive]
    no_m = pos[pos.apply(lambda r: not any(x.startswith("GL-M") for x in r["must"] + r["any"]), axis=1)]
    print(f"정답에 문구 ID가 없는 양성(이 컬렉션에선 못 찾음): {len(no_m)}")

    summary = {}
    for qt in ["phrase_pair", "phrase_avoid"]:
        rows = {"phrase_only": pq[pq.query_type == qt][COLS].mean().round(3).to_dict()}
        rows.update({v: b[b.query_type == qt][COLS].mean().round(3).to_dict() for v, b in base.items()})
        summary[qt] = rows
        print(f"\n== {qt} n={int((pq.query_type == qt).sum())}")
        print(pd.DataFrame(rows).T.to_string())

    th = E.threshold_table(sub, pq)
    fp = recall_at_fp(sub, pq)
    print("\n== 임계값(문구 전용 · 양성 = 문구 문항 상위 5개, 음성 = 일반 음성)")
    print(th.set_index("t").loc[TS].to_string())
    print("-- 같은 오탐 건수에서:", fp)
    base_th, base_fp = {}, {}
    for v, b in base.items():
        tb = E.threshold_table(sub, b)
        base_th[v], base_fp[v] = tb.to_dict("records"), recall_at_fp(sub, b)
        print(f"-- 같은 문항, 전체 컬렉션 {v}")
        print(tb.set_index("t").loc[TS, ["pos_recovered", "neg_fp_rate"]].to_string())
        print("   같은 오탐 건수에서:", base_fp[v])

    neg = pq[pq.query_type.isin(E.NEG_TYPES)].sort_values("top1_score", ascending=False)
    cat = E.category_check(pq)
    print("\n일반 음성 1등 상위 3:", neg[["query", "top1", "top1_score"]].head(3).to_dict("records"))
    print("카테고리:", {k: v for k, v in cat.items() if k != "blocked_top1"})

    out_dir = day / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    pq.to_csv(out_dir / f"phrase_only_{mode}.csv", index=False, encoding="utf-8-sig")
    p = out_dir / f"phrase_only_{mode}.json"
    p.write_text(json.dumps({
        "provenance": want, "base_tag": args.base_tag or None, "base_verified": bool(base),
        "rows": len(ph), "questions": len(sub), "unreachable_positives": len(no_m),
        "search_requests": pq.attrs["requests"], "search_s_cached_query_vectors": round(pq.attrs["search_s"], 2),
        "summary": summary, "threshold": th.to_dict("records"), "recall_at_fp": fp,
        "base_threshold": base_th, "base_recall_at_fp": base_fp, "category": cat,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {p}")


if __name__ == "__main__":
    main()
