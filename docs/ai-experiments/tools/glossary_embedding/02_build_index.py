"""2단계: 용어집을 BGE-M3로 임베딩해 Qdrant 컬렉션에 적재한다.

실행 예
  python 02_build_index.py                        # config.GLOSSARY_SOURCE (기본 db_snapshot: 운영 DB 스냅샷 20,653행)
  python 02_build_index.py --source team          # 데이터팀 전달본 xlsx 3종
  python 02_build_index.py --source sample        # 샘플 58행(로컬 파일 필요)
  python 02_build_index.py --limit-ingredient 2000   # 성분만 앞 2000행 (CPU 속도 확인용)
  python 02_build_index.py --glossary "D:/다른_용어집.xlsx"
  python 02_build_index.py --text-mode ko_en

적재 규칙(데이터팀 00_읽는법): 문구 적재여부=Y · 성분·인증 구역=A
주의: 컬렉션을 새로 만든다(--append 가 아니면). 매니페스트 확인 · 증분 반영은 05_experiments.py build 를 쓴다.
      memory 모드는 이 스크립트가 끝나면 인덱스가 사라진다.
"""
import argparse

import config
from pixemb import setup_console
from pixemb.glossary import load_db_snapshot, load_glossary, load_team_glossaries
from pixemb.store import collection_count, get_client, index_glossary


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["db_snapshot", "team", "sample"], default=config.GLOSSARY_SOURCE)
    ap.add_argument("--glossary", default=None, help="CSV/xlsx 파일 하나를 직접 지정 (source 무시)")
    ap.add_argument("--sheet", default=None, help="xlsx 시트명 (생략 시 source_ko 머리글 자동 탐색)")
    ap.add_argument("--mode", choices=["local", "memory"], default=config.QDRANT_MODE)
    ap.add_argument("--text-mode", choices=["ko", "ko_en"], default=config.EMBED_TEXT_MODE)
    ap.add_argument("--limit-ingredient", type=int, default=None, help="team: 성분만 앞 N행")
    ap.add_argument("--append", action="store_true",
                    help="컬렉션을 지우지 않고 추가/갱신 (같은 gl_id는 덮어씀)")
    args = ap.parse_args()

    config.GLOSSARY_SOURCE = args.source
    if args.glossary:
        df, source = load_glossary(args.glossary, sheet=args.sheet), args.glossary
    elif args.source == "db_snapshot":
        df, source = load_db_snapshot(), "db_snapshot"
    elif args.source == "team":
        df, source = load_team_glossaries(limit_ingredient=args.limit_ingredient), "team"
    else:
        df, source = load_glossary(config.GLOSSARY_CSV), "sample"
    if args.limit_ingredient:
        source += f"(성분 {args.limit_ingredient}행)"
    print(df.groupby(["term_kind", "enforcement"], dropna=False).size().rename("건수").to_string(), "\n")

    if args.glossary or args.source == "sample":
        name = f"glossary__{'sample' if args.source == 'sample' and not args.glossary else 'custom'}__{args.text_mode}"
    else:
        name = config.collection(text_mode=args.text_mode)  # db_snapshot · team 은 전달본/스냅샷 이름이 들어간다
    client = get_client(args.mode, name=name)
    try:
        index_glossary(client, df, name=name, text_mode=args.text_mode, recreate=not args.append, source=source)
        print(f"[qdrant] 컬렉션 '{name}' 현재 {collection_count(client, name)}건")
    finally:
        client.close()


if __name__ == "__main__":
    main()
