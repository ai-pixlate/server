"""DB 스냅샷(data/db_snapshot/<이름>.jsonl)과 전달본 파일(data/deliveries/<전달본>/)을 한 칸씩 비교한다.

EC2의 plan(삽입 0 · 갱신 0 · 변경 없음 20653)과 같은 결론이 PC 쪽에서도 나오는지 확인하는 용도다.
비교 기준은 서버 로더(app/glossary_ingest.py)가 파일에서 만드는 값 = 이 실험의 load_team_glossaries 결과.

실행: python tools/compare_snapshot_vs_files.py [--snapshot glossary_db_20261001] [--delivery 2026-09-28]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
from pixemb import setup_console  # noqa: E402
from pixemb.glossary import load_db_snapshot, load_team_glossaries  # noqa: E402

FIELDS = ["term_ko", "term_target", "term_kind", "enforcement", "target_lang", "internal_category",
          "example_sentence", "zone", "label", "is_representative", "version"]


def norm(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    s = str(v)
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s  # 1.0 ↔ 1


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=config.DB_SNAPSHOT)
    ap.add_argument("--delivery", default=config.GLOSSARY_DELIVERY)
    args = ap.parse_args()
    db = load_db_snapshot(args.snapshot).set_index("gl_id")
    files = load_team_glossaries(config.DELIVERIES_DIR / args.delivery).set_index("gl_id")
    only_db, only_file = sorted(set(db.index) - set(files.index)), sorted(set(files.index) - set(db.index))
    print(f"\nID: DB {len(db)} · 파일 {len(files)} · DB에만 {len(only_db)} · 파일에만 {len(only_file)}")
    diffs = {}
    for gid in db.index.intersection(files.index):
        for f in FIELDS:
            a, b = norm(db.at[gid, f]), norm(files.at[gid, f])
            if a != b:
                diffs.setdefault(f, []).append((gid, a, b))
    if not diffs and not only_db and not only_file:
        print(f"✓ 모든 행 · {len(FIELDS)}개 필드가 같다 — DB 스냅샷 = 전달본 {args.delivery}")
    for f, rows in diffs.items():
        print(f"✗ {f}: {len(rows)}행 다름 — 예 {rows[:3]}")
    pk = db["glossary_pk"]
    print(f"내부 PK: {pk.min()}~{pk.max()} · 중복 {int(pk.duplicated().sum())} · 대역별 "
          + ", ".join(f"{p} {g.min()}~{g.max()}" for p, g in pk.groupby(db.index.str[:4])))


if __name__ == "__main__":
    main()
