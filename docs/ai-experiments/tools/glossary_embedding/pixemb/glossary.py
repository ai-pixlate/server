"""용어집 로더 — 샘플 CSV와 데이터팀 용어집 xlsx(glossary_ingredient·certification·phrase)를 같은 형태로 읽는다.

반환 DataFrame 컬럼 = DB glossary 테이블 + 테스트용 보조 컬럼
    gl_id, term_ko, term_target, term_kind, enforcement,
    target_lang, internal_category, example_sentence, note,
    zone(구역), label(라벨·문구만), is_representative(대표여부·문구만), aliases(이명·성분만)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

import config

COLUMNS = [
    "gl_id", "term_ko", "term_target", "term_kind", "enforcement",
    "target_lang", "internal_category", "example_sentence", "note",
    "zone", "label", "is_representative", "aliases", "version",
]

# 데이터팀 시트 열 이름 → DB/내부 열 이름
_RENAME = {
    "id": "gl_id",
    "source_ko": "term_ko",
    "target_text": "term_target",
    "구역": "zone",
    "라벨": "label",
    "대표여부": "is_representative",
    "이명": "aliases",
    "적재여부": "load_flag",
}

# 시트는 한글(강제/참고), DB CHECK는 영문(enforced/reference)
_ENFORCEMENT = {"강제": "enforced", "참고": "reference", "enforced": "enforced", "reference": "reference"}

# 시트 표기 → DB CHECK 값. 인증·문구 시트는 "Sunscreens & Tanning"으로 적혀 있다 (DB는 "... Products")
_CATEGORY_ALIAS = {"Sunscreens & Tanning": "Sunscreens & Tanning Products"}


def _read_xlsx(path: Path, sheet: str | None) -> pd.DataFrame:
    """머리글이 1행이 아닐 수도 있어서 'source_ko' 또는 'term_ko'가 있는 행을 찾아 머리글로 쓴다."""
    sheets = pd.read_excel(path, sheet_name=sheet if sheet else None, header=None, dtype=str)
    if isinstance(sheets, pd.DataFrame):
        sheets = {sheet: sheets}
    for name, raw in sheets.items():
        for i in range(min(10, len(raw))):
            header = [str(v).strip() for v in raw.iloc[i].tolist()]
            if "source_ko" in header or "term_ko" in header:
                df = raw.iloc[i + 1:].copy()
                df.columns = header
                print(f"[glossary] {path.name} · 시트 '{name}' · 머리글 {i + 1}행")
                return df
    raise ValueError(f"{path}: 'source_ko' 또는 'term_ko' 머리글이 있는 시트를 찾지 못했습니다.")


def _apply_load_rule(df: pd.DataFrame, name: str, all_zones: bool) -> pd.DataFrame:
    """데이터팀 적재 규칙.
    - 적재여부 열이 있으면(문구) 적재여부 = Y 만
    - 없고 구역 열이 있으면(성분·인증) 구역 = A 만  (B 확인 후 · C 보류 · 보류 · 이관은 제외)
    """
    if all_zones:
        return df
    before = len(df)
    if "load_flag" in df.columns and df["load_flag"].notna().any():
        df = df[df["load_flag"] == "Y"]
        rule = "적재여부=Y"
    elif df["zone"].notna().any():
        df = df[df["zone"] == "A"]
        rule = "구역=A"
    else:
        return df
    print(f"[glossary] {name}: 적재 규칙({rule}) → {before}행 중 {len(df)}행")
    return df


def load_glossary(path: str | Path | None = None, sheet: str | None = None,
                  limit: int | None = None, all_zones: bool = False) -> pd.DataFrame:
    path = Path(path or config.GLOSSARY_CSV)
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        df = _read_xlsx(path, sheet)
    else:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")

    df = df.rename(columns=_RENAME)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[COLUMNS + (["load_flag"] if "load_flag" in df.columns else [])].copy()

    # 서버 로더(app/glossary_ingest.py)와 같게 값은 원문 그대로(앞뒤 공백 포함) 두고,
    # 공백뿐인 칸만 빈 값으로 본다.
    for col in df.columns:
        blank = df[col].isna() | df[col].astype(str).str.strip().isin(["", "nan", "None"])
        df.loc[blank, col] = None

    before = len(df)
    df = df[df["term_ko"].notna()]
    df = _apply_load_rule(df, path.name, all_zones)
    no_target = int(df["term_target"].isna().sum())
    # 영문명이 없는 행은 번역에 쓸 수 없으므로 인덱스에서 뺀다
    df = df[df["term_target"].notna()].drop(columns=["load_flag"], errors="ignore").copy()

    # id가 비어 있는 행은 한글명으로 임시 id (행 순서가 아니라 이름 기준이라 재적재해도 같은 점을 덮어씀)
    missing = df["gl_id"].isna()
    df.loc[missing, "gl_id"] = "AUTO:" + df.loc[missing, "term_ko"]

    df["enforcement"] = df["enforcement"].map(lambda v: _ENFORCEMENT.get(v, "reference") if v else "reference")
    df["target_lang"] = df["target_lang"].fillna(config.TARGET_LANG)
    df["internal_category"] = df["internal_category"].fillna("common").replace(_CATEGORY_ALIAS)

    bad_cat = ~df["internal_category"].isin(config.INTERNAL_CATEGORIES)
    if bad_cat.any():
        print(f"[glossary] ⚠ 알 수 없는 internal_category {bad_cat.sum()}건 → 'common'으로 처리: "
              f"{sorted(df.loc[bad_cat, 'internal_category'].unique())[:5]}")
        df.loc[bad_cat, "internal_category"] = "common"

    dup = df["gl_id"].duplicated()
    if dup.any():
        raise ValueError(f"gl_id 중복: {df.loc[dup, 'gl_id'].tolist()[:10]}")

    if limit:
        df = df.head(limit)
    print(f"[glossary] {path.name}: {before}행 → 인덱스 대상 {len(df)}행"
          + (f" (영문명 없음 제외 {no_target}행)" if no_target else ""))
    return df.reset_index(drop=True)


def load_team_glossaries(data_dir: str | Path | None = None, limit_ingredient: int | None = None,
                         kinds: tuple[str, ...] = ("certification", "phrase", "ingredient")) -> pd.DataFrame:
    """전달본 폴더(config.DELIVERIES_DIR/<GLOSSARY_DELIVERY>)의 데이터팀 용어집 3종을 적재 규칙대로 읽어 합친다."""
    data_dir = Path(data_dir or config.DELIVERIES_DIR / config.GLOSSARY_DELIVERY)
    frames = []
    for kind in kinds:
        path = data_dir / config.TEAM_GLOSSARY_FILES[kind]
        if not path.exists():
            print(f"[glossary] ⚠ {path.name} 없음 — 건너뜀")
            continue
        frames.append(load_glossary(path, limit=limit_ingredient if kind == "ingredient" else None))
    df = pd.concat(frames, ignore_index=True)
    dup = df["gl_id"].duplicated()
    if dup.any():
        raise ValueError(f"파일 간 gl_id 중복: {df.loc[dup, 'gl_id'].tolist()[:10]}")
    print(f"[glossary] 합계 {len(df)}행 · " + " · ".join(f"{k} {v}" for k, v in df['term_kind'].value_counts().items()))
    df.attrs["files"] = file_hashes(data_dir, kinds)
    return df


def file_hashes(data_dir: str | Path, kinds=("certification", "phrase", "ingredient")) -> dict[str, str]:
    """전달본 파일 SHA-256 — 인덱스 매니페스트에 남겨 어떤 파일로 만든 인덱스인지 식별한다."""
    import hashlib
    out = {}
    for kind in kinds:
        p = Path(data_dir) / config.TEAM_GLOSSARY_FILES[kind]
        if p.exists():
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def load_db_snapshot(name: str | None = None, data_dir: str | Path | None = None,
                     verify: bool = True) -> pd.DataFrame:
    """EC2에서 내보낸 운영 DB glossary 스냅샷(JSONL + meta.json)을 읽는다.

    DB 열 이름 → 이 실험의 열 이름: external_id→gl_id · id→glossary_pk(내부 PK) · representative→is_representative.
    값은 DB에 저장된 그대로(enforcement 는 이미 enforced/reference, 카테고리는 정규화된 값).
    verify=True 면 meta.json 의 행 수 · PK 지문(서버 로더 file_fingerprint 와 같은 계산)을 다시 계산해 대조한다.
    """
    import hashlib
    import json
    name = name or config.DB_SNAPSHOT
    d = Path(data_dir or config.DB_SNAPSHOT_DIR)
    jsonl, meta_path = d / f"{name}.jsonl", d / f"{name}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    raw = jsonl.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    df = pd.DataFrame(rows)
    if verify:
        pairs = sorted((r["external_id"], r["id"]) for r in rows if r.get("external_id"))
        fp = hashlib.md5(",".join(f"{e}:{i}" for e, i in pairs).encode("utf-8")).hexdigest()
        problems = []
        if len(rows) != meta["rows"]:
            problems.append(f"행 수 {len(rows)} ≠ meta {meta['rows']}")
        if fp != meta["pk_fingerprint_md5"]:
            problems.append(f"PK 지문 {fp} ≠ meta {meta['pk_fingerprint_md5']}")
        if df["external_id"].isna().any() or df["external_id"].duplicated().any():
            problems.append("external_id 빈 값 또는 중복")
        if problems:
            raise ValueError(f"{jsonl.name}: 스냅샷 검증 실패 — {problems}")
    df = df.rename(columns={"external_id": "gl_id", "id": "glossary_pk", "term_ko": "term_ko",
                            "representative": "is_representative"})
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[COLUMNS + ["glossary_pk"]].astype(object).where(df[COLUMNS + ["glossary_pk"]].notna(), None)
    df["glossary_pk"] = df["glossary_pk"].astype(int)
    df.attrs["files"] = {jsonl.name: hashlib.sha256(raw).hexdigest()}
    df.attrs["snapshot_meta"] = meta
    print(f"[glossary] DB 스냅샷 {jsonl.name}: {len(df)}행 · alembic {meta.get('alembic')} · "
          f"내보낸 시각 {meta.get('exported_at')} · PK 지문 {meta['pk_fingerprint_md5']}"
          + (" (검증 통과)" if verify else ""))
    return df.reset_index(drop=True)


def load_source(source: str | None = None, path=None) -> pd.DataFrame:
    """config.GLOSSARY_SOURCE("team" | "db_snapshot" | "sample")에 따라 인덱스에 넣을 용어집을 고른다. path를 주면 그 파일."""
    if path:
        return load_glossary(path)
    source = source or config.GLOSSARY_SOURCE
    if source == "team":
        return load_team_glossaries()
    if source == "db_snapshot":
        return load_db_snapshot()
    if source == "sample":
        return load_glossary(config.GLOSSARY_CSV)
    raise ValueError(f"GLOSSARY_SOURCE는 'team' · 'db_snapshot' · 'sample': {source!r}")


def embed_text(row: pd.Series, mode: str | None = None) -> str:
    """벡터로 만들 문자열. 쿼리(OCR 한국어)와 비교되는 쪽이므로 기본은 한국어 용어만."""
    mode = mode or config.EMBED_TEXT_MODE
    if mode == "ko":
        return row["term_ko"]
    if mode == "ko_en":
        return f"{row['term_ko']} | {row['term_target']}"
    raise ValueError(f"EMBED_TEXT_MODE는 'ko' 또는 'ko_en': {mode!r}")
