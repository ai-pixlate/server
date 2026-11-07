"""데이터팀 전달본(config.GLOSSARY_DELIVERY)의 '정답'으로 평가셋 v2(data/eval_team_v2.csv)를 만든다.

정답 열 두 가지 (v1의 expected_ids 하나를 나눔)
  must_ids : 전부 검색돼야 하는 항목 — 한 블록에 든 강제 용어 여러 개, 성분명
  any_ids  : 하나만 검색되면 되는 대체 가능한 정답 — 문구 라벨의 대표·대안
  둘 다 비면 음성(용어집 해당 없음)

context
  ingredient_list : 전성분 문단 (성분만 검색 · OCR 치환 규칙 대상)
  general         : 그 밖의 블록 (성분 제외 검색 대상)

source · independent — 용어집 구축에 쓴 원자료에서 나온 문항(N)과 독립 문항(Y)을 구분한다
  data_team_ocr_table  성분「표기차이_주의목록」표1·표2 — 올리브영 OCR에서 측정, 식약처 용어집과 독립     Y
  data_team_trunc      표3 미복구 중 정답이 분명한 것(정답 지정은 수작업)                                  partial
  pair_raw_data        문구「쌍_대응원자료」ko_expr — 문구 용어집을 만든 원자료                             N
  excluded_rows        문구 매칭후보 C·보류 행 — 같은 파일                                                  N
  generated            용어집 행에서 기계적으로 만든 문항(정확 일치 · 띄어쓰기 삽입 · 치환 손상 · 전성분 묶음) generated
  handmade             이 실험 작성자가 쓴 문항 — 용어집을 본 뒤 작성                                     partial
  locale_dict_context  현지부적합 사전「제외하는 맥락」예시를 문장으로 옮긴 음성 문항                         partial

실행: python make_eval_team.py [--seed 0]
"""
import argparse
import random

import pandas as pd

import config
from pixemb import setup_console
from pixemb.glossary import load_team_glossaries
from pixemb.textnorm import ocr_fix_ingredient

DATA = config.DELIVERIES_DIR / config.GLOSSARY_DELIVERY
OUT = config.ROOT / "data" / "eval_team_v2.csv"

# (query, category, must 용어, any 용어, query_type, note) — 정답은 term_ko 로 적고 id 로 바꾼다
HANDMADE = [
    ("인체적용시험", "common", ["인체적용시험"], [], "cert_exact", ""),
    ("논코메도제닉", "common", ["논코메도제닉"], ["모공 막지 않는(논코메도제닉)"], "cert_exact", "인증 강제 + 문구 대안"),
    ("피부과테스트완료", "common", ["피부과 테스트 완료"], [], "cert_spacing", "OCR 띄어쓰기 없음"),
    ("피부첩포시험 완료", "common", ["피부 첩포 시험"], [], "cert_spacing", ""),
    ("인체 적용 시험 완료", "common", ["인체적용시험"], [], "cert_spacing", ""),
    ("한국피부과학연구원에서 인체적용시험을 완료했습니다", "common", ["한국피부과학연구원", "인체적용시험"], [], "cert_multi", "강제 2개"),
    ("피엔케이피부임상연구센타 인체적용시험 · 피부 첩포 시험 완료", "common",
     ["피엔케이피부임상연구센타", "인체적용시험", "피부 첩포 시험"], [], "cert_multi", "강제 3개"),
    ("비건 · 크루얼티프리 · 무향 포뮬러", "common", ["비건", "크루얼티프리", "무향"], [], "cert_multi", "강제 3개"),
    ("무향 · 저자극 포뮬러", "common", ["무향"], ["순한·저자극", "저자극"], "cert_multi", "강제 1 + 문구 대안"),
    ("옥시벤존 · 옥티노세이트 불검출 선크림", "Sunscreens & Tanning Products", ["옥시벤존 불검출"], [], "cert_sentence", ""),
    ("바다를 생각한 리프 프렌들리 선크림", "Sunscreens & Tanning Products", ["리프프렌들리"], [], "cert_sentence", ""),
    ("민감한 피부도 안심하고 쓸 수 있어요", "common", [], ["민감성 피부 사용적합", "민감 피부 사용 적합", "순한·저자극"], "cert_paraphrase", ""),
    ("여드름성 피부 사용적합 테스트 완료", "common", ["여드름성 피부 사용적합"], ["여드름성 피부"], "cert_sentence", ""),
    ("식약처 인증 미백 기능성 화장품", "common", ["식품의약품안전처", "기능성화장품"], ["미백·브라이트닝"], "cert_multi", "강제 2 + 문구 대안"),
    ("향료 무첨가", "common", ["무향"], [], "cert_paraphrase", ""),
    ("동물실험을 하지 않았어요", "common", ["크루얼티프리"], [], "cert_paraphrase", ""),
    ("알레르기 유발 테스트 완료", "common", ["알러지 테스트 완료"], [], "cert_paraphrase", "알러지/알레르기"),
    ("Dermatest 엑설런트 등급 획득", "common", ["더마테스트"], [], "cert_mixed_lang", ""),
    ("PETA 공식 인증 비건", "common", ["PETA 인증"], ["비건"], "cert_mixed_lang", ""),
    ("clinical study 완료", "common", ["인체적용시험"], [], "cert_mixed_lang", ""),
]

# 카테고리 필터 문항 — 선크림 전용 용어가 다른 카테고리 작업에서 나오면 안 된다
CATEGORY_BLOCKED = [
    ("리프 프렌들리 포뮬러", "Face", "선크림 전용(GL-C010) → Face 작업에서는 나오면 안 됨"),
    ("백탁 없는 가벼운 선크림", "Face", "선크림 전용(GL-M018)"),
]

NEGATIVE_HANDMADE = [
    "배송비 무료 이벤트", "2+1 한정 특가", "사용 전 반드시 사용법을 확인하세요", "제조국: 대한민국",
    "제조번호 및 사용기한 별도 표기", "자세한 내용은 상세페이지 하단을 참고해 주세요",
]
# 현지부적합 사전(locale_unsuitable_dict.xlsx) 「제외하는 맥락」 예시를 블록 문장으로 옮긴 것
NEGATIVE_LOCALE = [
    ("LC-01", "리필 기획 세트 구성 (본품 50ml + 리필 50ml)"), ("LC-01", "대용량 더블기획 한정 구성"),
    ("LC-02", "고객센터 1588-0000 (평일 10시~18시)"), ("LC-02", "카카오톡 채널 추가하고 쿠폰 받기"),
    ("LC-02", "고객만족센터 1644-0000"),
    ("LC-03", "1만원 이상 구매 시 100% 증정"), ("LC-03", "전원증정 미니어처 키링 GIFT"),
    ("LC-04", "교환 및 반품은 상품 수령 후 7일 이내 가능합니다"), ("LC-04", "소비자분쟁해결기준에 따라 보상받을 수 있습니다"),
    ("LC-05", "올영세일 기간 한정 1+1"), ("LC-05", "구매 후기 이벤트 참여하고 적립금 받기"),
    ("LC-05", "선착순 100명 추첨 당첨자 발표"),
    ("LC-06", "정가 32,000원 → 할인가 25,600원"), ("LC-06", "3만원 이상 구매 시 무료배송"), ("LC-06", "5천원 상당 사은품"),
    ("LC-07", "공식판매처 외 구매 제품은 A/S가 불가합니다"), ("LC-07", "정품 인증 라벨을 확인하세요"),
    ("LC-07", "병행수입 제품 구매에 주의하세요"),
    ("LC-08", "쿠팡, 지마켓, 11번가, 네이버 스마트스토어에서 구매 가능"),
]

TRUNCATED = [
    ("2-헥산다이올", ["1,2-헥산다이올"], "숫자 앞부분 잘림 · 34회로 최다"),
    ("3-부탄다이올", ["1,3-부탄다이올"], "숫자 앞부분 잘림"),
    ("광물성오일", ["미네랄오일"], "다른 이름(이명)"),
]

# 전성분 묶음 생성용 — 한국 화장품 전성분에 흔한 성분(데이터팀 표1·표2 정답 + 흔한 성분). 용어집에 있는 것만 쓴다
COMMON_INGREDIENTS = [
    "정제수", "글리세린", "부틸렌글라이콜", "나이아신아마이드", "판테놀", "1,2-헥산다이올", "병풀추출물", "아데노신",
    "세라마이드엔피", "소듐하이알루로네이트", "알란토인", "카보머", "잔탄검", "다이메티콘", "에틸헥실글리세린",
    "트로메타민", "다이소듐이디티에이", "향료", "토코페롤", "베타인", "스쿠알란", "하이알루로닉애씨드", "마데카소사이드",
    "알부틴", "징크옥사이드", "티타늄디옥사이드", "에틸헥실메톡시신나메이트", "호모살레이트", "세테아릴알코올",
    "글리세릴스테아레이트", "페녹시에탄올", "프로판다이올", "소듐시트레이트", "시트릭애씨드", "레티놀", "콜라겐",
]


def read_table(path, sheet, title_prefix):
    raw = pd.read_excel(path, sheet_name=sheet, header=None, dtype=str)
    col0 = raw[0].fillna("")
    start = col0[col0.str.startswith(title_prefix)].index[0] + 2
    rows = []
    for i in range(start, len(raw)):
        v = col0.iloc[i]
        if not v or v.startswith("[표") or v.startswith("■"):
            break
        rows.append(raw.iloc[i].tolist())
    return rows


def main() -> None:
    setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--n-exact", type=int, default=200)
    ap.add_argument("--n-blocks", type=int, default=60)
    args = ap.parse_args()
    rnd = random.Random(args.seed)

    gl = load_team_glossaries()
    by_ko = gl.groupby("term_ko")["gl_id"].apply(list).to_dict()
    ing = gl[gl.term_kind == "ingredient"]
    phrase = gl[gl.term_kind == "marketing"]
    label_ids = phrase.groupby("label")["gl_id"].apply(list).to_dict()
    rows, skipped = [], []

    def ids(terms):
        out, miss = [], []
        for t in terms:
            (out.extend(by_ko[t]) if t in by_ko else miss.append(t))
        return out, miss

    def add(query, category, must, any_, qtype, context, source, independent, note="", forbidden=()):
        rows.append({"query": query, "category": category or "", "must_ids": "|".join(must), "any_ids": "|".join(any_),
                     "forbidden_ids": "|".join(forbidden),
                     "query_type": qtype, "context": context, "source": source, "independent": independent,
                     "note": note})

    # ── 성분: 데이터팀 표기차이_주의목록 ──
    path = DATA / config.TEAM_GLOSSARY_FILES["ingredient"]
    for qtype, title in [("ingredient_ocr", "[표1]"), ("ingredient_spelling", "[표2]")]:
        for r in read_table(path, "표기차이_주의목록", title):
            got, miss = ids([r[1]])
            if miss:
                skipped.append((qtype, r[0], r[1]))
                continue
            add(r[0], "common", got, [], qtype, "ingredient_list", "data_team_ocr_table", "Y",
                f"{r[3] if len(r) > 3 else ''} · 정답 {r[1]}")
    for q, terms, note in TRUNCATED:
        got, miss = ids(terms)
        if miss:
            skipped.append(("ingredient_trunc", q, terms))
            continue
        add(q, "common", got, [], "ingredient_trunc", "ingredient_list", "data_team_trunc", "partial", note)

    # ── 성분: 생성 문항 ──
    pool = ing.sample(n=args.n_exact, random_state=args.seed)
    for _, r in pool.iterrows():
        add(r.term_ko, "common", [r.gl_id], [], "ingredient_exact", "ingredient_list", "generated", "generated")
    for _, r in ing.sample(n=args.n_exact, random_state=args.seed + 1).iterrows():
        t = r.term_ko
        if len(t) < 4:
            continue
        cut = rnd.randint(2, len(t) - 2)
        add(t[:cut] + " " + t[cut:], "common", [r.gl_id], [], "ingredient_space", "ingredient_list", "generated",
            "generated", "OCR 띄어쓰기 삽입")
    # 치환 규칙이 정상 성분명을 바꾸는 경우 — 원래 이름을 쿼리로 둔다(--ocr-fix 시 손상 여부 측정)
    damaged = ing[ing.term_ko.map(lambda t: ocr_fix_ingredient(t) != t)]
    for _, r in damaged.iterrows():
        add(r.term_ko, "common", [r.gl_id], [], "ingredient_fix_damage", "ingredient_list", "generated", "generated",
            f"치환 시 → {ocr_fix_ingredient(r.term_ko)}")
    # 전성분 묶음 — 3~6개 성분을 ', '로 이은 블록. 성분 전부가 must
    common = [t for t in COMMON_INGREDIENTS if t in by_ko]
    missing_common = [t for t in COMMON_INGREDIENTS if t not in by_ko]
    for _ in range(args.n_blocks):
        names = rnd.sample(common, rnd.randint(3, 6))
        add(", ".join(names), "common", [by_ko[n][0] for n in names], [], "ingredient_block", "ingredient_list",
            "generated", "generated", f"{len(names)}개")

    # ── 문구 ──
    pairs = pd.read_excel(DATA / config.TEAM_GLOSSARY_FILES["phrase"], sheet_name="쌍_대응원자료", dtype=str)
    pairs = pairs[(pairs["check"] == "OK") & pairs["ko_expr"].notna() & pairs["type"].isin(["대응", "한국만"])]
    pairs = pairs[pairs["label"].isin(label_ids)].drop_duplicates("ko_expr")
    for _, r in pairs.iterrows():
        add(r["ko_expr"], "", [], label_ids[r["label"]], "phrase_pair", "general", "pair_raw_data", "N",
            f"라벨 {r['label']} · {r['type']}")
    cand = pd.read_excel(DATA / config.TEAM_GLOSSARY_FILES["phrase"], sheet_name="매칭후보", dtype=str)
    for _, r in cand[cand["구역"].isin(["C", "보류"]) & cand["라벨"].isin(label_ids)].iterrows():
        add(r["source_ko"], "", [], label_ids[r["라벨"]], "phrase_avoid", "general", "excluded_rows", "N",
            f"{r['구역']} {r['id']} {r['target_text']} → 라벨 {r['라벨']}")

    # ── 인증 · 섞인 블록 (수작업) ──
    for q, cat, must_t, any_t, qtype, note in HANDMADE:
        m, miss1 = ids(must_t)
        a, miss2 = ids(any_t)
        if miss1 or miss2:
            print(f"  ⚠ 정답 용어 없음 {miss1 + miss2} — '{q}'")
        if not (m or a):
            skipped.append((qtype, q, must_t + any_t))
            continue
        add(q, cat, m, a, qtype, "general", "handmade", "partial", note)

    # ── 음성 · 카테고리 차단 ──
    # 카테고리 차단 — 음성이 아니다(공용 용어는 정답일 수 있음). 그 카테고리 작업에서 나오면 안 되는 ID 를 forbidden 으로 둔다:
    # internal_category 가 {작업 카테고리, common} 밖인 모든 행(현재 선크림 전용 8행)
    for q, cat, note in CATEGORY_BLOCKED:
        forbidden = sorted(gl.loc[~gl.internal_category.isin([cat, "common"]), "gl_id"])
        add(q, cat, [], [], "category_blocked", "general", "handmade", "partial", note, forbidden=forbidden)
    for q in NEGATIVE_HANDMADE:
        add(q, "common", [], [], "negative", "general", "handmade", "partial")
    for lc, q in NEGATIVE_LOCALE:
        add(q, "common", [], [], "negative_locale", "general", "locale_dict_context", "partial", lc)

    df = pd.DataFrame(rows)
    df.insert(0, "qid", [f"V{i + 1:04d}" for i in range(len(df))])
    df.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"\n저장: {OUT} · {len(df)}문항 · 전달본 {config.GLOSSARY_DELIVERY}")
    print(df.groupby(["query_type", "source", "independent"]).size().rename("n").to_string())
    print(f"\n치환 규칙이 바꾸는 정상 성분명: {len(damaged)}개 / {len(ing)}")
    if missing_common:
        print(f"용어집에 없어 묶음 생성에서 뺀 흔한 성분: {missing_common}")
    if skipped:
        print(f"정답이 적재 용어집에 없어 제외: {skipped}")


if __name__ == "__main__":
    main()
