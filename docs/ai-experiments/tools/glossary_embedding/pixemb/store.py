"""Qdrant 로컬 모드(서버·Docker 없음) 래퍼 — 적재 · 동기화 · 검색.

config.QDRANT_MODE 하나로 저장 방식을 바꾼다.
    "local"  → QdrantClient(path=...)   디스크 저장 · 재실행해도 유지
    "memory" → QdrantClient(":memory:")  프로세스 종료 시 소멸

오래된 벡터 재사용 방지
- 점마다 content_hash(임베딩 텍스트 + 검색·프롬프트에 쓰는 필드)와 embed_hash(임베딩 텍스트 + 모델 설정)를 둔다.
- 컬렉션마다 매니페스트(전달본 · 파일 SHA-256 · 모델 설정 · 행 수)를 config.MANIFEST_PATH 에 남긴다.
- open_index()는 파일 · 설정이 매니페스트와 다르면 plan_sync()로 차이를 계산하고, sync=True 가 아니면 재사용을 거부한다.
- 파일에 없는 기존 점(index_only)은 지우지 않고 센다 — 서버 로더(app/glossary_ingest.py)의 "파일에 없는 기존 행 유지"와 같다.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
import pandas as pd
from qdrant_client import QdrantClient, models

import config
from .cache import EmbedCache, cache_key, encode_cached
from .embedder import Embedder, get_embedder
from .glossary import embed_text, load_source

# 검색 결과 · 번역 프롬프트에 쓰는 필드. 이 값이 바뀌면 payload 를 갱신해야 한다.
PAYLOAD_FIELDS = [
    "gl_id", "term_ko", "term_target", "term_kind", "enforcement", "target_lang", "internal_category",
    "example_sentence", "zone", "label", "is_representative", "version",
    "glossary_pk",  # DB 내부 PK — db_snapshot 소스에만 값이 있다(team 파일 소스는 None)
]


class StaleIndexError(RuntimeError):
    pass


def get_client(mode: str | None = None, path=None, name: str | None = None) -> QdrantClient:
    """local 모드는 컬렉션마다 폴더가 다르다(config.qdrant_path). name 을 주면 그 컬렉션의 폴더를 연다."""
    mode = mode or config.QDRANT_MODE
    if mode == "memory":
        print("[qdrant] 메모리 모드 (:memory:) — 프로세스가 끝나면 인덱스가 사라집니다.")
        return QdrantClient(":memory:")
    if mode == "local":
        path = path or config.qdrant_path(name)
        print(f"[qdrant] 로컬 파일 모드 — {path}")
        # 로컬 모드는 한 폴더를 한 프로세스만 열 수 있다(파일 잠금).
        # 노트북 커널이 열어 둔 상태에서 스크립트를 돌리면 "already accessed" 오류가 난다.
        return QdrantClient(path=str(path))
    raise ValueError(f"QDRANT_MODE는 'local' 또는 'memory': {mode!r}")


def point_id(gl_id: str) -> str:
    """Qdrant 점 ID는 정수/UUID만 된다 → 원본 ID(external_id)에서 결정적 UUID. 재적재해도 같은 점."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pixate:glossary:{gl_id}"))


def model_spec() -> dict:
    return {"model": config.MODEL_NAME, "dim": config.EMBED_DIM, "normalize": config.NORMALIZE,
            "max_seq_length": config.MAX_SEQ_LENGTH}


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def _record_payload(rec: dict, text: str, text_mode: str, source: str) -> dict:
    fields = {k: rec.get(k) for k in PAYLOAD_FIELDS}
    return {
        **fields,
        "external_id": rec.get("gl_id"),
        "embed_text": text,
        "embed_text_mode": text_mode,
        "embed_hash": cache_key(text)[:16],
        "content_hash": _hash([fields, text]),
        "glossary_source": source,
    }


# ── 매니페스트 ─────────────────────────────────────────────────────────────

def _load_manifests() -> dict:
    try:
        return json.loads(config.MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def read_manifest(name: str) -> dict | None:
    return _load_manifests().get(f"{config.qdrant_path(name)}::{name}")


def write_manifest(name: str, entry: dict) -> None:
    all_ = _load_manifests()
    all_[f"{config.qdrant_path(name)}::{name}"] = entry
    config.MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.MANIFEST_PATH.write_text(json.dumps(all_, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 컬렉션 · 동기화 ────────────────────────────────────────────────────────

def collection_count(client: QdrantClient, name: str | None = None) -> int:
    name = name or config.collection()
    if not client.collection_exists(name):
        return 0
    return client.count(name, exact=True).count


def ensure_collection(client: QdrantClient, name: str, dim: int = config.EMBED_DIM, recreate: bool = False) -> None:
    if recreate and client.collection_exists(name):
        client.delete_collection(name)
    if not client.collection_exists(name):
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
        )
        return
    size = client.get_collection(name).config.params.vectors.size
    if size != dim:
        raise ValueError(f"컬렉션 '{name}' 벡터 차원 {size} ≠ {dim}. 새 컬렉션을 쓰세요.")


def _existing_points(client: QdrantClient, name: str) -> dict[str, dict]:
    """gl_id → {id, embed_hash, content_hash}. 전체를 훑는다(2만 건 로컬 모드 수 초)."""
    out, offset = {}, None
    while True:
        pts, offset = client.scroll(name, limit=2048, offset=offset,
                                    with_payload=["gl_id", "embed_hash", "content_hash"], with_vectors=False)
        for p in pts:
            out[p.payload.get("gl_id")] = {"id": str(p.id), **p.payload}
        if offset is None:
            return out


@dataclass
class SyncPlan:
    insert: list[str] = field(default_factory=list)          # 새 원본 ID
    reembed: list[str] = field(default_factory=list)         # 임베딩 텍스트가 바뀜 → 벡터 다시 계산
    payload_only: list[str] = field(default_factory=list)    # 벡터는 그대로, payload 만 갱신
    unchanged: int = 0
    index_only: list[str] = field(default_factory=list)      # 파일에 없는 기존 점(지우지 않음)
    id_mismatch: list[str] = field(default_factory=list)     # 점 ID ≠ uuid5(gl_id)

    @property
    def dirty(self) -> bool:
        return bool(self.insert or self.reembed or self.payload_only or self.id_mismatch)

    def summary(self) -> dict:
        return {"insert": len(self.insert), "reembed": len(self.reembed), "payload_only": len(self.payload_only),
                "unchanged": self.unchanged, "index_only": len(self.index_only), "id_mismatch": len(self.id_mismatch),
                "examples": {"reembed": self.reembed[:5], "payload_only": self.payload_only[:5],
                             "index_only": self.index_only[:5]}}


def plan_sync(client: QdrantClient, df: pd.DataFrame, name: str, text_mode: str, source: str) -> SyncPlan:
    plan = SyncPlan()
    existing = _existing_points(client, name) if client.collection_exists(name) else {}
    incoming = set()
    for _, r in df.iterrows():
        rec = r.where(r.notna(), None).to_dict()
        gid = rec["gl_id"]
        incoming.add(gid)
        want = _record_payload(rec, embed_text(r, text_mode), text_mode, source)
        cur = existing.get(gid)
        if cur is None:
            plan.insert.append(gid)
        elif cur["id"] != point_id(gid):
            plan.id_mismatch.append(gid)
        elif cur.get("embed_hash") != want["embed_hash"]:
            plan.reembed.append(gid)
        elif cur.get("content_hash") != want["content_hash"]:
            plan.payload_only.append(gid)
        else:
            plan.unchanged += 1
    plan.index_only = sorted(set(existing) - incoming)
    return plan


def apply_sync(client: QdrantClient, df: pd.DataFrame, name: str, text_mode: str, source: str,
               plan: SyncPlan, embedder: Embedder | None = None, upsert_batch: int = 256) -> dict:
    """plan 의 insert · reembed · payload_only 만 반영한다. 벡터는 임베딩 캐시를 거친다."""
    if plan.id_mismatch:
        raise StaleIndexError(f"점 ID가 원본 ID와 맞지 않는 행 {len(plan.id_mismatch)}개 — 새 컬렉션으로 다시 만드세요")
    ensure_collection(client, name)
    todo = set(plan.insert) | set(plan.reembed) | set(plan.payload_only)
    sub = df[df["gl_id"].isin(todo)]
    recs = [r.where(r.notna(), None).to_dict() for _, r in sub.iterrows()]
    texts = [embed_text(r, text_mode) for _, r in sub.iterrows()]
    t0 = time.perf_counter()
    need_vec = [i for i, rec in enumerate(recs) if rec["gl_id"] not in set(plan.payload_only)]
    vec_stats = {"embedded": 0, "cache_hit": 0}
    vectors = {}
    if need_vec:
        vecs, vec_stats = encode_cached(embedder or get_embedder, [texts[i] for i in need_vec])
        vectors = {recs[i]["gl_id"]: vecs[j] for j, i in enumerate(need_vec)}
    encode_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    # 점은 배치마다 만든다 — 2만 건을 한 번에 파이썬 리스트로 만들면 수백 MB가 든다
    batch = []
    for rec, text in zip(recs, texts):
        payload = _record_payload(rec, text, text_mode, source)
        if rec["gl_id"] in vectors:
            batch.append(models.PointStruct(id=point_id(rec["gl_id"]), vector=vectors[rec["gl_id"]].tolist(),
                                            payload=payload))
            if len(batch) >= upsert_batch:
                client.upsert(collection_name=name, points=batch)
                batch = []
        else:
            client.overwrite_payload(collection_name=name, payload=payload, points=[point_id(rec["gl_id"])])
    if batch:
        client.upsert(collection_name=name, points=batch)
    stats = {**plan.summary(), "encode_s": round(encode_s, 1), "upsert_s": round(time.perf_counter() - t0, 1),
             **{f"vec_{k}": v for k, v in vec_stats.items() if k in ("embedded", "cache_hit")}}
    stats.pop("examples", None)
    print(f"[sync] {name}: {stats}")
    return stats


def index_glossary(client: QdrantClient, df: pd.DataFrame, embedder: Embedder | None = None,
                   name: str | None = None, text_mode: str | None = None,
                   recreate: bool = True, source: str = "custom", **_ignored) -> dict:
    """전체 적재(recreate=True면 컬렉션을 새로 만든다). 메모리 모드 비교 실험 · 샘플용."""
    text_mode = text_mode or config.EMBED_TEXT_MODE
    name = name or config.collection(text_mode=text_mode)
    ensure_collection(client, name, recreate=recreate)
    plan = plan_sync(client, df, name, text_mode, source)
    return apply_sync(client, df, name, text_mode, source, plan, embedder)


def open_index(mode: str | None = None, rebuild: bool = False, glossary_path=None,
               text_mode: str | None = None, source: str | None = None, sync: bool = False,
               name: str | None = None) -> QdrantClient:
    """인덱스를 열고 파일 · 설정과 맞는지 확인한다.

    - 매니페스트(파일 해시 · 모델 설정 · 행 수)가 같으면 재사용.
    - 다르면 plan_sync 로 차이를 계산한다. sync=True 면 차이만 반영(캐시로 벡터 재사용), 아니면 StaleIndexError.
    - 메모리 모드는 매번 새로 만든다.
    """
    text_mode = text_mode or config.EMBED_TEXT_MODE
    source = str(glossary_path) if glossary_path else (source or config.GLOSSARY_SOURCE)
    if source == "sample":
        name = name or f"glossary__sample__{text_mode}"
    name = name or config.collection(text_mode=text_mode)
    client = get_client(mode, name=name)
    df = load_source(source, glossary_path) if glossary_path else load_source(source)
    want = {"collection": name, "source": source, "delivery": config.GLOSSARY_DELIVERY if source == "team" else None,
            "files": df.attrs.get("files", {}), "text_mode": text_mode, "model": model_spec(), "rows": len(df)}
    if rebuild:
        ensure_collection(client, name, recreate=True)
    have = read_manifest(name) if (mode or config.QDRANT_MODE) == "local" else None
    same = have and {k: have.get(k) for k in want} == want and collection_count(client, name) == len(df)
    if same and not rebuild:
        print(f"[qdrant] 기존 인덱스 재사용 — {name} · {len(df)}건 · 매니페스트 일치")
        return client
    plan = plan_sync(client, df, name, text_mode, source)
    if plan.dirty and collection_count(client, name) and not (sync or rebuild):
        client.close()
        raise StaleIndexError(f"{name}: 인덱스가 현재 파일·설정과 다릅니다 {plan.summary()} — "
                              f"확인 후 --sync 로 차이만 반영하세요")
    if plan.dirty:
        apply_sync(client, df, name, text_mode, source, plan)
    write_manifest(name, {**want, "built_at": dt.datetime.now().isoformat(timespec="seconds"),
                          "index_only": len(plan.index_only)})
    return client


# ── 검색 ─────────────────────────────────────────────────────────────────

def build_filter(category: str | None = None, lang: str = config.TARGET_LANG,
                 enforcement: str | None = None, term_kind: str | None = None,
                 exclude_kinds: Sequence[str] = ()) -> models.Filter:
    """PRD F-SRC-04b 분기 규칙을 필터로 옮긴 것.

    category=None      → 카테고리 무관(전체) — 탐색용
    category="common"  → 공용만 (내부 카테고리 미매핑 작업)
    category="Face" 등 → [해당 카테고리, common]  (카테고리 용어집 + 공용 폴백)
    term_kind          → 한 종류만 (예: 전성분 문단 → ingredient)
    exclude_kinds      → 뺄 종류 (예: 일반 문구 블록 → ingredient 제외)
    """
    must = [models.FieldCondition(key="target_lang", match=models.MatchValue(value=lang))]
    if category == "common":
        must.append(models.FieldCondition(key="internal_category", match=models.MatchValue(value="common")))
    elif category:
        must.append(models.FieldCondition(key="internal_category",
                                          match=models.MatchAny(any=[category, "common"])))
    if enforcement:
        must.append(models.FieldCondition(key="enforcement", match=models.MatchValue(value=enforcement)))
    if term_kind:
        must.append(models.FieldCondition(key="term_kind", match=models.MatchValue(value=term_kind)))
    must_not = [models.FieldCondition(key="term_kind", match=models.MatchAny(any=list(exclude_kinds)))] if exclude_kinds else None
    return models.Filter(must=must, must_not=must_not)


def _to_rows(points) -> list[dict]:
    return [{"score": round(p.score, 4), **(p.payload or {})} for p in points]


def search(client: QdrantClient, query: str, category: str | None = None,
           top_k: int = config.TOP_K, score_threshold: float | None = None,
           enforcement: str | None = None, term_kind: str | None = None, exclude_kinds: Sequence[str] = (),
           embedder: Embedder | None = None, name: str | None = None, lang: str = config.TARGET_LANG) -> list[dict]:
    vec = (embedder or get_embedder()).encode([query])[0]
    res = client.query_points(
        collection_name=name or config.collection(),
        query=vec.tolist(),
        query_filter=build_filter(category, lang, enforcement, term_kind, exclude_kinds),
        limit=top_k,
        with_payload=True,
        score_threshold=score_threshold,
    )
    return _to_rows(res.points)


def search_batch(client: QdrantClient, queries: Sequence[str], categories: Sequence[str | None],
                 top_k: int = config.TOP_K, embedder: Embedder | None = None, name: str | None = None,
                 term_kinds: Sequence[str | None] | None = None,
                 exclude_kinds: Sequence[Sequence[str]] | None = None, chunk: int = 64,
                 vectors: np.ndarray | None = None) -> tuple[list[list[dict]], np.ndarray]:
    """여러 쿼리를 한 번에 임베딩·검색. (결과, 쿼리 벡터) 반환. vectors를 주면 임베딩을 건너뛴다."""
    vecs = vectors if vectors is not None else (embedder or get_embedder()).encode(
        list(queries), show_progress=len(queries) > 200)
    n = len(queries)
    term_kinds = term_kinds or [None] * n
    exclude_kinds = exclude_kinds or [()] * n
    requests = [
        models.QueryRequest(query=v.tolist(), filter=build_filter(c, term_kind=k, exclude_kinds=x),
                            limit=top_k, with_payload=True)
        for v, c, k, x in zip(vecs, categories, term_kinds, exclude_kinds)
    ]
    results = []
    for start in range(0, len(requests), chunk):
        responses = client.query_batch_points(collection_name=name or config.collection(),
                                              requests=requests[start:start + chunk])
        results.extend(_to_rows(r.points) for r in responses)
    return results, vecs
