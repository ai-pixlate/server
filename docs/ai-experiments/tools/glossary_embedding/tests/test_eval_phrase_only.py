"""문구 전용 평가 도구(tools/eval_phrase_only.py) 테스트.

- 임베딩 대상은 문구(GL-M)만 — 성분 · 인증 행은 컬렉션에 들어가지 않는다
- 캐시 미스면 모델을 올리지 않고 멈춘다
- 비교 기준 결과가 다른 평가셋 · 용어집 · 임베딩 방식 · 모델 설정 · 문항으로 만들어졌으면 거부한다

모델 · 실제 데이터 없이 돈다. 실행: python -m pytest tests -q   (도구 폴더에서)
"""
import importlib.util
import json
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
qc = pytest.importorskip("qdrant_client")

_spec = importlib.util.spec_from_file_location(
    "eval_phrase_only", Path(__file__).resolve().parents[1] / "tools" / "eval_phrase_only.py")
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)


def _glossary():
    return pd.DataFrame({
        "gl_id": ["GL-C001", "GL-I00001", "GL-M001", "GL-M002"],
        "term_ko": ["비건 인증", "글리세린", "촉촉한 사용감", "끈적임 없는"],
        "term_target": ["Vegan certified", "Glycerin", "moisturizing feel", "non-sticky"],
        "term_kind": ["certification", "ingredient", "marketing", "marketing"],
        "internal_category": ["common"] * 4, "target_lang": ["en"] * 4,
    })


def test_only_phrase_rows_are_indexed():
    rows = T.phrase_rows(_glossary())
    assert list(rows.gl_id) == ["GL-M001", "GL-M002"]
    client = qc.QdrantClient(":memory:")
    T.build_collection(client, "p", rows, np.eye(2, 4, dtype=np.float32))
    pts, _ = client.scroll("p", limit=10, with_payload=["gl_id", "term_kind"])
    assert {p.payload["gl_id"] for p in pts} == {"GL-M001", "GL-M002"}
    assert {str(p.id) for p in pts} == {T.point_id("GL-M001"), T.point_id("GL-M002")}
    assert {p.payload["term_kind"] for p in pts} == {"marketing"}


def test_cache_miss_stops_without_loading_model(tmp_path):
    from pixemb.cache import EmbedCache, encode_cached
    cache = EmbedCache(tmp_path / "c.sqlite")
    try:
        cache.put_many(["있는 문장"], np.ones((1, 4), dtype=np.float32))
        vecs, st = encode_cached(T.no_model, ["있는 문장"], cache=cache)  # 캐시 적중이면 모델 함수를 부르지 않는다
        assert st["embedded"] == 0 and vecs.shape == (1, 4)
        with pytest.raises(RuntimeError, match="캐시 미스"):
            encode_cached(T.no_model, ["있는 문장", "없는 문장"], cache=cache)
    finally:
        cache.close()


def _write_base(d: Path, want: dict, sub, mode="ko_en", **override):
    idx = {"source": want["source"], "files": want["files"], "text_mode": want["text_mode"], "model": want["model"]}
    idx.update(override.pop("index", {}))
    j = {"eval_set_sha256": override.pop("eval_set_sha256", want["eval_set_sha256"]), "index": idx}
    (d / f"eval_{mode}.json").write_text(json.dumps(j), encoding="utf-8")
    b = sub[["qid", "query", "query_type"]].copy()
    b["category"] = ""
    for v in T.BASES:
        b.to_csv(d / f"eval_{mode}_{v.replace(':', '_')}.csv", index=False, encoding="utf-8-sig")


@pytest.fixture
def setup(tmp_path):
    want = {"eval_set_sha256": "e" * 64, "source": "db_snapshot", "files": {"glossary_db.jsonl": "f" * 64},
            "text_mode": "ko_en", "model": {"model": "BAAI/bge-m3", "dim": 1024, "normalize": True, "max_seq_length": 512}}
    sub = pd.DataFrame({"qid": ["V0001", "V0002"], "query": ["촉촉해요", "배송비 무료"], "query_type": ["phrase_pair", "negative"]})
    return tmp_path, want, sub


def test_base_matching_experiment_is_accepted(setup):
    d, want, sub = setup
    _write_base(d, want, sub)
    out = T.load_base(d, "ko_en", sub, want)
    assert set(out) == set(T.BASES) and list(out["split:fix:split"].qid) == ["V0001", "V0002"]


@pytest.mark.parametrize("override, msg", [
    ({"eval_set_sha256": "0" * 64}, "평가셋 해시"),
    ({"index": {"source": "team"}}, "용어집 소스"),
    ({"index": {"files": {"glossary_phrase.xlsx": "1" * 64}}}, "용어집 파일 해시"),
    ({"index": {"text_mode": "ko"}}, "임베딩 방식"),
    ({"index": {"model": {"model": "other", "dim": 1024, "normalize": True, "max_seq_length": 512}}}, "모델 설정"),
])
def test_base_with_different_setup_is_refused(setup, override, msg):
    d, want, sub = setup
    _write_base(d, want, sub, **override)
    with pytest.raises(T.BaseMismatch, match=msg):
        T.load_base(d, "ko_en", sub, want)


def test_base_without_index_record_is_refused(setup):
    d, want, sub = setup
    _write_base(d, want, sub)
    p = d / "eval_ko_en.json"
    j = json.loads(p.read_text(encoding="utf-8"))
    del j["index"]  # 인덱스 기록이 없는 예전 결과는 같은 실험인지 알 수 없다
    p.write_text(json.dumps(j), encoding="utf-8")
    with pytest.raises(T.BaseMismatch):
        T.load_base(d, "ko_en", sub, want)


def test_base_with_different_questions_is_refused(setup):
    d, want, sub = setup
    _write_base(d, want, sub)
    changed = sub.copy()
    changed.loc[0, "query"] = "다른 질문"
    with pytest.raises(T.BaseMismatch, match="query 불일치"):
        T.load_base(d, "ko_en", changed, want)
    more = pd.concat([sub, pd.DataFrame({"qid": ["V0003"], "query": ["새 문항"], "query_type": ["phrase_pair"]})])
    with pytest.raises(T.BaseMismatch, match="없음"):
        T.load_base(d, "ko_en", more.reset_index(drop=True), want)
