"""3단계: 한국어 OCR 블록 문장을 넣어 용어집 검색 결과를 눈으로 확인한다.

실행 예
  python 03_search.py                                    # 대화형 (빈 줄 입력 시 종료)
  python 03_search.py -q "한국피부과학연구원 인체적용시험 완료"
  python 03_search.py -q "리프 프렌들리" -c "Sunscreens & Tanning Products"
  python 03_search.py -q "무향 저자극" --enforced        # 강제 항목만 (F-TRN-02c 검사 대상)
  python 03_search.py -q "소듬하이알루로네이트" --kind ingredient --ocr-fix

-c/--category 규칙 (PRD F-SRC-04b)
  생략   → 카테고리 필터 없음(전체)
  common → 공용만
  Face 등 → [Face, common]
"""
import argparse

import config
from pixemb import setup_console
from pixemb.store import open_index, search
from pixemb.textnorm import ocr_fix_ingredient


def show(rows: list[dict], threshold: float) -> None:
    if not rows:
        print("  (결과 없음)")
        return
    for r in rows:
        mark = "✓" if r["score"] >= threshold else " "
        label = f"·{r['label']}({r.get('is_representative') or ''})" if r.get("label") else ""
        print(f"  {mark} {r['score']:.4f}  {r['gl_id']:<9} {r['term_ko']} → {r['term_target']}"
              f"   [{r['term_kind']}·{r['enforcement']}·{r['internal_category']}{label}]")
    print(f"  (✓ = 점수 ≥ SCORE_THRESHOLD {threshold})")


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query", help="검색 문장 (생략하면 대화형)")
    ap.add_argument("-c", "--category", default=None, choices=config.INTERNAL_CATEGORIES)
    ap.add_argument("-k", "--top-k", type=int, default=config.TOP_K)
    ap.add_argument("--enforced", action="store_true", help="enforcement=enforced만")
    ap.add_argument("--mode", choices=["local", "memory"], default=config.QDRANT_MODE)
    ap.add_argument("--kind", choices=["ingredient", "certification", "marketing"], default=None,
                    help="term_kind 하나만 (예: 전성분 문단이면 ingredient)")
    ap.add_argument("--ocr-fix", action="store_true", help="쿼리에 성분 OCR 치환 규칙 적용 (전성분 문단 전용)")
    ap.add_argument("--source", choices=["db_snapshot", "team", "sample"], default=config.GLOSSARY_SOURCE)
    ap.add_argument("--rebuild", action="store_true", help="용어집 인덱스 다시 만들기")
    args = ap.parse_args()

    client = open_index(args.mode, rebuild=args.rebuild, source=args.source)
    enforcement = "enforced" if args.enforced else None

    def run(q: str) -> None:
        if args.ocr_fix:
            q = ocr_fix_ingredient(q)
        print(f"\n▶ {q}   (category={args.category or '전체'}, kind={args.kind or '전체'})")
        show(search(client, q, category=args.category, top_k=args.top_k, enforcement=enforcement,
                    term_kind=args.kind), config.SCORE_THRESHOLD)

    try:
        if args.query:
            run(args.query)
        else:
            print("검색할 문장을 입력하세요. 빈 줄이면 종료.")
            while q := input("\n질의> ").strip():
                run(q)
    finally:
        client.close()


if __name__ == "__main__":
    main()
