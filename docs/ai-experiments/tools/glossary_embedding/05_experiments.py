"""5단계: 검증 실험 모음 — 항목별로 따로 실행하고 결과를 results/<날짜>/<항목>.json 에 남긴다.

모델이 필요 없는 항목(캐시 벡터 사용)과 필요한 항목을 나눴다. RAM 8GB PC에서 모델과 2만 건 인덱스를
한 프로세스에 함께 올리면 메모리가 모자랄 수 있어서다.

  python 05_experiments.py build      # 전달본 컬렉션 적재/동기화(ko는 캐시로 — 모델 불필요, 캐시 미스가 있으면 모델 사용)
  python 05_experiments.py ids        # 11) external_id · 점 ID · payload 연결 검증              (모델 불필요)
  python 05_experiments.py stale      # 12) 재실행 · 증분 갱신 · 오래된 벡터 재사용 검증          (ko: 모델 불필요)
  python 05_experiments.py filters    # 7)  언어 · 카테고리 필터 · common 폴백                     (모델 불필요)
  python 05_experiments.py tokens     # 10) 긴 성분명의 토큰 잘림                                 (토크나이저만)
  python 05_experiments.py ocrfix     # 5)  치환 규칙이 정상 성분명을 바꾸는지                       (모델 불필요)
  python 05_experiments.py sanity     # 1)  1024차원 · 정규화 · 장치 · 처리량                       (모델)
  python 05_experiments.py eval       # 2·3·4·5·6·8·9) 평가셋 v2 전 항목                          (모델)
  python 05_experiments.py latency    # 검색 지연 · 임베딩 처리량 · 메모리                          (모델)

공통 옵션: --delivery 2026-09-28 --text-mode ko
"""
import argparse
import datetime as dt
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import config
from pixemb import setup_console

RESULTS = config.ROOT / "results" / dt.date.today().isoformat()


def save(part: str, obj) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"{part}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {p}")


def rss_gb() -> float:
    import psutil
    return round(psutil.Process().memory_info().rss / 1e9, 2)


def _run_meta() -> dict:
    import platform
    import sentence_transformers, qdrant_client, torch  # noqa: E401
    from pixemb.glossary import file_hashes
    return {
        "at": dt.datetime.now().isoformat(timespec="seconds"), "delivery": config.GLOSSARY_DELIVERY,
        "files": file_hashes(config.DELIVERIES_DIR / config.GLOSSARY_DELIVERY),
        "text_mode": config.EMBED_TEXT_MODE, "collection": config.collection(),
        "model": config.MODEL_NAME, "max_seq_length": config.MAX_SEQ_LENGTH, "normalize": config.NORMALIZE,
        "python": platform.python_version(), "torch": torch.__version__,
        "sentence_transformers": sentence_transformers.__version__, "qdrant_client": __import__("importlib.metadata").metadata.version("qdrant-client"),
        "cuda": torch.cuda.is_available(),
    }


# ── build ──────────────────────────────────────────────────────────────────
def cmd_build(args) -> None:
    from pixemb.store import open_index, collection_count
    t0 = time.perf_counter()
    client = open_index("local", source="team", sync=True)
    try:
        n = collection_count(client)
    finally:
        client.close()
    save(f"build_{config.EMBED_TEXT_MODE}", {**_run_meta(), "points": n, "seconds": round(time.perf_counter() - t0, 1)})


# ── 11) ID 연결 ───────────────────────────────────────────────────────────
def cmd_ids(args) -> None:
    from pixemb.glossary import load_source
    from pixemb.store import get_client, point_id
    gl = load_source()  # GLOSSARY_SOURCE=db_snapshot 이면 DB 내부 PK(glossary_pk)까지 대조한다
    name = config.collection()
    client = get_client("local", name=name)
    try:
        pts, offset, got = [], None, {}
        while True:
            batch, offset = client.scroll(name, limit=4096, offset=offset,
                                          with_payload=["gl_id", "external_id", "glossary_pk", "term_ko"])
            for p in batch:
                got[str(p.id)] = p.payload
            if offset is None:
                break
    finally:
        client.close()
    file_ids = list(gl.gl_id)
    expected_pids = {point_id(g): g for g in file_ids}
    ko_by_id = dict(zip(gl.gl_id, gl.term_ko))
    pk_by_id = dict(zip(gl.gl_id, gl["glossary_pk"])) if "glossary_pk" in gl else {}
    res = {
        "file_rows": len(gl), "file_ids_unique": len(set(file_ids)) == len(file_ids),
        "id_format_ok": int(gl.gl_id.str.fullmatch(r"GL-(C|I|M)\d+").sum()),
        "points": len(got),
        "uuid5_collisions": len(file_ids) - len(expected_pids),
        "points_missing": len(set(expected_pids) - set(got)),
        "points_extra": len(set(got) - set(expected_pids)),
        "payload_gl_id_mismatch": sum(1 for pid, pl in got.items() if expected_pids.get(pid) != pl.get("gl_id")),
        "payload_external_id_mismatch": sum(1 for pl in got.values() if pl.get("external_id") != pl.get("gl_id")),
        "term_ko_mismatch": int(sum(1 for pid, pl in got.items()
                                    if pid in expected_pids and pl.get("term_ko") != ko_by_id[expected_pids[pid]])),
        "source": config.GLOSSARY_SOURCE, "collection": name,
        "glossary_pk_filled": sum(1 for pl in got.values() if pl.get("glossary_pk") is not None),
        "glossary_pk_mismatch": (sum(1 for pid, pl in got.items() if pid in expected_pids
                                     and pl.get("glossary_pk") != pk_by_id.get(expected_pids[pid])) if pk_by_id else None),
        "glossary_pk_unique": (len({pl.get("glossary_pk") for pl in got.values()}) == len(got)) if pk_by_id else None,
        "note": ("DB 스냅샷의 내부 PK(glossary.id)를 payload glossary_pk 로 싣고 대조했다. 운영 연결에서 이 값을 "
                 "compliance_flags.glossary_id 로 쓸지, 조회 시점에 external_id 로 다시 찾을지는 합의 필요(재적재 시 PK는 유지되나 스냅샷 시점 고정)."
                 if pk_by_id else
                 "파일 소스라 glossary_pk 가 비어 있다. GLOSSARY_SOURCE=db_snapshot 으로 DB 내부 PK까지 대조할 수 있다."),
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
    save("ids", res)


# ── 12) 오래된 벡터 재사용 · 증분 갱신 ─────────────────────────────────────
def cmd_stale(args) -> None:
    from pixemb.glossary import load_team_glossaries
    from pixemb.store import StaleIndexError, apply_sync, get_client, plan_sync, ensure_collection, open_index
    out = {}
    tm = config.EMBED_TEXT_MODE
    # (a) 예전 open_index 규칙: (glossary_source, embed_text_mode) 한 점만 비교 → 전달본이 바뀌어도 재사용했을 것
    client = get_client("local", name=config.LEGACY_COLLECTION)
    try:
        p, _ = client.scroll(config.LEGACY_COLLECTION, limit=1, with_payload=True)
        legacy = {k: p[0].payload.get(k) for k in ("glossary_source", "embed_text_mode")}
    finally:
        client.close()
    out["a_legacy_rule"] = {"legacy_payload": legacy,
                            "would_reuse_for_2026-09-28": legacy == {"glossary_source": "team", "embed_text_mode": "ko"},
                            "note": "이전 규칙은 파일 내용을 보지 않았다 — 전달본 pre-0928 로 만든 인덱스를 2026-09-28 에도 그대로 썼을 것"}

    # (b) 새 규칙: pre-0928 로 만든 컬렉션을 2026-09-28 파일로 계획 → 바뀐 3행이 보여야 한다 (메모리 모드, 벡터는 캐시)
    old = load_team_glossaries(config.DELIVERIES_DIR / "pre-0928")
    new = load_team_glossaries(config.DELIVERIES_DIR / "2026-09-28")
    mem = get_client("memory")
    name = f"stale_test__{tm}"
    ensure_collection(mem, name, recreate=True)
    t0 = time.perf_counter()
    first = apply_sync(mem, old, name, tm, "team", plan_sync(mem, old, name, tm, "team"))
    out["b_build_pre0928"] = {**first, "seconds": round(time.perf_counter() - t0, 1)}
    plan = plan_sync(mem, new, name, tm, "team")
    out["b_plan_0928_vs_pre0928"] = plan.summary()
    t0 = time.perf_counter()
    out["b_apply"] = {**apply_sync(mem, new, name, tm, "team", plan), "seconds": round(time.perf_counter() - t0, 1)}
    again = plan_sync(mem, new, name, tm, "team")
    out["b_replan_after_apply"] = again.summary()
    # 바뀐 행의 payload 가 새 값인지
    from qdrant_client import models
    chk = {}
    for gid in ("GL-M038", "GL-M043", "GL-M056"):
        r, _ = mem.scroll(name, scroll_filter=models.Filter(must=[models.FieldCondition(
            key="gl_id", match=models.MatchValue(value=gid))]), limit=1, with_payload=["term_target", "version"])
        chk[gid] = r[0].payload if r else None
    out["b_payload_after"] = chk
    # (c) 파일에서 행이 빠지면 → index_only 로 세고 지우지 않는다
    dropped = new[new.gl_id != "GL-M001"]
    out["c_row_removed_plan"] = plan_sync(mem, dropped, name, tm, "team").summary()
    mem.close()

    # (d) open_index 가 매니페스트 불일치 시 재사용을 거부하는지 — DB 스냅샷으로 만든 로컬 컬렉션을
    #     전달본 pre-0928 파일로 열어 본다(파일 해시 · 내용이 달라 거부해야 한다. 아무것도 쓰지 않음)
    prev = config.GLOSSARY_DELIVERY
    target = f"glossary__{config.DB_SNAPSHOT.replace('glossary_', '')}__{tm}"
    try:
        config.GLOSSARY_DELIVERY = "pre-0928"
        c = open_index("local", source="team", name=target)
        c.close()
        out["d_open_index_refuses"] = False
    except StaleIndexError as e:
        out["d_open_index_refuses"] = True
        out["d_message"] = str(e)[:400]
    finally:
        config.GLOSSARY_DELIVERY = prev
    out["d_target"] = target
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    save(f"stale_{tm}", out)


# ── 7) 필터 ────────────────────────────────────────────────────────────────
def cmd_filters(args) -> None:
    from pixemb.cache import EmbedCache
    from pixemb.glossary import embed_text, load_team_glossaries
    from pixemb.store import build_filter, get_client
    gl = load_team_glossaries()
    sun = gl[gl.internal_category == "Sunscreens & Tanning Products"]
    cache = EmbedCache()
    vecs = cache.get_many([embed_text(r) for _, r in sun.iterrows()])
    cache.close()
    name = config.collection()
    client = get_client("local", name=name)
    rows = []
    try:
        for _, r in sun.iterrows():
            v = vecs[embed_text(r)].tolist()
            for cat, lang, expect in [(None, "en", True), ("Sunscreens & Tanning Products", "en", True),
                                      ("Face", "en", False), ("common", "en", False), ("Body", "en", False),
                                      ("Sunscreens & Tanning Products", "ja", False)]:
                res = client.query_points(name, query=v, query_filter=build_filter(cat, lang), limit=20,
                                          with_payload=["gl_id", "internal_category", "target_lang"]).points
                found = any(p.payload["gl_id"] == r.gl_id for p in res)
                leak = [p.payload["gl_id"] for p in res if cat and cat != "common" and
                        p.payload["internal_category"] not in (cat, "common")]
                leak += [p.payload["gl_id"] for p in res if cat == "common" and p.payload["internal_category"] != "common"]
                rows.append({"gl_id": r.gl_id, "term_ko": r.term_ko, "category": cat or "(없음)", "lang": lang,
                             "expected_found": expect, "found": found, "ok": found == expect and not leak,
                             "results": len(res), "category_leak": leak[:3]})
        # common 폴백: Face 작업에서도 common 항목은 나와야 한다
        face_common = client.query_points(name, query=vecs[embed_text(sun.iloc[0])].tolist(),
                                          query_filter=build_filter("Face"), limit=20,
                                          with_payload=["internal_category"]).points
    finally:
        client.close()
    df = pd.DataFrame(rows)
    res = {"checks": len(df), "failed": int((~df.ok).sum()),
           "by_case": {f"{c}|{l}": v for (c, l), v in df.groupby(["category", "lang"]).ok.mean().round(3).items()},
           "face_query_categories": sorted({p.payload["internal_category"] for p in face_common}),
           "failures": df[~df.ok].to_dict("records")[:10],
           "note": "카테고리 CHECK 는 DB에 없다(0005는 enforcement CHECK 만). 필터는 payload 값 문자열 일치로 동작한다."}
    print(json.dumps({k: v for k, v in res.items() if k != "failures"}, ensure_ascii=False, indent=2))
    save("filters", res)


# ── 10) 토큰 잘림 ──────────────────────────────────────────────────────────
def cmd_tokens(args) -> None:
    from transformers import AutoTokenizer
    from pixemb.glossary import embed_text, load_team_glossaries
    tok = AutoTokenizer.from_pretrained(config.MODEL_NAME)
    gl = load_team_glossaries()
    res = {"max_seq_length": config.MAX_SEQ_LENGTH}
    for mode in ("ko", "ko_en"):
        texts = [embed_text(r, mode) for _, r in gl.iterrows()]
        n = np.array([len(x) for x in tok(texts, add_special_tokens=True)["input_ids"]])
        over = np.where(n > config.MAX_SEQ_LENGTH)[0]
        res[mode] = {"max_tokens": int(n.max()), "p99": int(np.percentile(n, 99)), "median": int(np.median(n)),
                     "over_limit": int(len(over)),
                     "over_examples": [{"gl_id": gl.iloc[i].gl_id, "tokens": int(n[i]), "chars": len(texts[i])} for i in over[:5]],
                     "longest": [{"gl_id": gl.iloc[i].gl_id, "tokens": int(n[i]), "chars": len(texts[i])}
                                 for i in np.argsort(-n)[:3]]}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    save("tokens", res)


# ── 5) OCR 치환 규칙 손상 ──────────────────────────────────────────────────
def cmd_ocrfix(args) -> None:
    from pixemb.glossary import load_team_glossaries
    from pixemb.textnorm import OCR_FIX_RULES, ocr_fix_ingredient
    gl = load_team_glossaries()
    ing = gl[gl.term_kind == "ingredient"]
    names = set(ing.term_ko)
    changed = ing[ing.term_ko.map(lambda t: ocr_fix_ingredient(t) != t)]
    rows = [{"gl_id": r.gl_id, "term_ko": r.term_ko, "after": ocr_fix_ingredient(r.term_ko),
             "after_is_other_ingredient": ocr_fix_ingredient(r.term_ko) in names} for _, r in changed.iterrows()]
    per_rule = {f"{a}→{b}": int(ing.term_ko.str.contains(a).sum()) for a, b in OCR_FIX_RULES}
    res = {"ingredients": len(ing), "changed": len(rows), "per_rule_contains": per_rule,
           "collides_with_other_name": sum(r["after_is_other_ingredient"] for r in rows), "rows": rows}
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, ensure_ascii=False, indent=2))
    for r in rows[:30]:
        print(f"  {r['gl_id']}  {r['term_ko']} → {r['after']}")
    save("ocrfix_damage", res)


# ── 1) sanity ─────────────────────────────────────────────────────────────
def cmd_sanity(args) -> None:
    from pixemb.embedder import get_embedder
    e = get_embedder()
    v = e.encode(["인체적용시험", "clinical study", "에칠헥실트리아존", "에틸헥실트리아존"])
    sample = [f"피부 속까지 촉촉한 속보습 크림 {i}" for i in range(64)]
    t0 = time.perf_counter()
    e.encode(sample)
    dt_ = time.perf_counter() - t0
    res = {**_run_meta(), "device": e.device, "dim": int(v.shape[1]), "norms": np.linalg.norm(v, axis=1).round(6).tolist(),
           "cos_ko_en": float(v[0] @ v[1]), "cos_old_spelling": float(v[2] @ v[3]),
           "load_seconds": round(e.load_seconds, 1), "short_sentences_per_s": round(64 / dt_, 1), "rss_gb": rss_gb()}
    print(json.dumps(res, ensure_ascii=False, indent=2))
    save("sanity", res)


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("part", choices=["build", "ids", "stale", "filters", "tokens", "ocrfix", "sanity", "eval", "latency"])
    ap.add_argument("--delivery", default=None)
    ap.add_argument("--text-mode", choices=["ko", "ko_en"], default=None)
    args, rest = ap.parse_known_args()
    if args.delivery:
        config.GLOSSARY_DELIVERY = args.delivery
    if args.text_mode:
        config.EMBED_TEXT_MODE = args.text_mode
    if args.part in ("eval", "latency"):
        import experiments_eval
        getattr(experiments_eval, f"cmd_{args.part}")(rest)
        return
    globals()[f"cmd_{args.part}"](args)


if __name__ == "__main__":
    main()
