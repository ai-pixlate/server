"""평가 지표 회귀 테스트 — PR #52 리뷰(2026-10-06) 반영 사항을 고정한다.

- 임계값 · 성공 지표는 평가에 쓴 후보 전부(ranked_full)를 쓴다 — 16위 이후 정답이 있어도 계산된다
  (예전처럼 상위 15개로 잘라 저장하면 아래 테스트가 실패한다)
- 전성분 묶음의 평가 후보 수는 정답 개수가 아니라 입력 조각 수로 정한다(@5n)
- 카테고리 차단 문항은 음성 오탐률에 들어가지 않고 금지 ID 노출로 따로 센다

모델 · 인덱스 없이 돈다(가짜 Qdrant 클라이언트). pandas · qdrant-client 가 없으면 건너뛴다.
실행: python -m pytest tests/test_glossary_embedding_eval.py -q   (이 도구 폴더에서)
"""
import json
from types import SimpleNamespace

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("qdrant_client")

import experiments_eval as E  # noqa: E402


class FakeClient:
    """query_batch_points 요청 순서대로 미리 정한 (gl_id, score, category) 목록을 돌려준다."""

    def __init__(self, answers):
        self.answers = list(answers)

    def query_batch_points(self, name, requests):
        out = []
        for _ in requests:
            pts = [SimpleNamespace(payload={"gl_id": g, "internal_category": c}, score=s)
                   for g, s, c in self.answers.pop(0)]
            out.append(SimpleNamespace(points=pts))
        return out


def _eval_df(rows):
    df = pd.DataFrame(rows)
    for col in ("must_ids", "any_ids", "forbidden_ids", "category", "source", "independent"):
        if col not in df:
            df[col] = ""
    df = df.fillna("")
    split = lambda s: [x for x in s.split("|") if x]  # noqa: E731
    df["must"] = df["must_ids"].map(split)
    df["any"] = df["any_ids"].map(split)
    df["forbidden"] = df["forbidden_ids"].map(split)
    df["positive"] = df.apply(lambda r: bool(r["must"] or r["any"]), axis=1)
    df["category"] = df["category"].replace({"": None})
    return df


def _ranked(n, correct_at=None, correct_id="OK", start=0.90, step=0.01):
    """점수 내림차순 후보 n개. correct_at(1부터)에 정답 ID를 둔다."""
    out = []
    for i in range(1, n + 1):
        gid = correct_id if i == correct_at else f"X{i:03d}"
        out.append((gid, round(start - step * (i - 1), 4), "common"))
    return out


def test_eval_k_uses_input_pieces_not_answers():
    # '1,2-헥산다이올'의 숫자 쉼표는 이름의 일부 → 조각 2개 → K = 10
    assert E.split_ingredients("1,2-헥산다이올, 글리세린") == ["1,2-헥산다이올", "글리세린"]
    assert E.eval_k("ingredient_block", "1,2-헥산다이올, 글리세린") == 10
    assert E.eval_k("phrase_pair", "아무 문장, 쉼표 포함") == 5


def test_candidates_beyond_15_are_kept_and_scored():
    # 전성분 4조각(K=5n=20). 필수 정답 4개 중 하나가 17위 — 상위 15개로 자르면 실패해야 한다
    q = "가, 나, 다, 라"
    musts = ["M1", "M2", "M3", "M4"]
    df = _eval_df([{"qid": "Q1", "query": q, "query_type": "ingredient_block", "context": "ingredient_list",
                    "must_ids": "|".join(musts)},
                   {"qid": "N1", "query": "배송비 무료", "query_type": "negative", "context": "general",
                    "category": "common"}])
    ranked = _ranked(25)
    for gid, pos in zip(musts, (1, 2, 3, 17)):
        ranked[pos - 1] = (gid, ranked[pos - 1][1], "common")
    client = FakeClient([ranked, [("X999", 0.30, "common")]])
    pq = E.run_variant(client, "c", df, [[q], ["배송비 무료"]], {q: np.zeros(4), "배송비 무료": np.zeros(4)}, kind="split")
    row = pq.set_index("qid").loc["Q1"]
    assert row["candidates"] == 25 and len(json.loads(row["ranked_full"])) == 25
    assert len(json.loads(row["ranked_top10"])) == 10
    assert row["k_eval"] == 20
    assert row["all_must@10"] == 0.0 and row["all_must@20"] == 1.0 and row["all_must@5n"] == 1.0

    th = E.threshold_table(df, pq).set_index("t")
    assert th.loc[0.5, "pos_recovered"] == 1.0  # 17위 정답까지 본다

    # 예전 방식(상위 15개만 저장)으로 되돌리면 같은 문항이 실패한다 — 이 테스트가 그 회귀를 잡는다
    cut = pq.copy()
    cut["ranked_full"] = [json.dumps(json.loads(x)[:15]) for x in cut["ranked_full"]]
    assert E.threshold_table(df, cut).set_index("t").loc[0.5, "pos_recovered"] == 0.0


def test_split_merges_pieces_by_best_score_and_keeps_all():
    # 조각 2개가 각각 20개를 돌려준다. 겹치는 ID는 최고 점수 하나로 합치고, 합친 전부를 보존한다
    q = "가, 나"
    a = _ranked(20, correct_at=18, correct_id="M1")
    b = [(g.replace("X", "Y"), s, c) for g, s, c in _ranked(20, correct_at=19, correct_id="M2")]
    b[0] = ("X001", 0.99, "common")  # a 의 X001(0.90)과 겹침 → 0.99 로 합쳐야 한다
    df = _eval_df([{"qid": "B1", "query": q, "query_type": "ingredient_block", "context": "ingredient_list",
                    "must_ids": "M1|M2"}])
    pq = E.run_variant(FakeClient([a, b]), "c", df, [["가", "나"]], {"가": np.zeros(4), "나": np.zeros(4)}, kind="split")
    row = pq.iloc[0]
    ranked = json.loads(row["ranked_full"])
    assert pq.attrs["requests"] == 2
    assert row["candidates"] == 39 == len(ranked)  # 20 + 20 − 겹침 1
    assert ranked[0] == ["X001", 0.99]
    assert row["all_must@30"] == 0.0 and row["all_must@5n"] == 0.0  # K=10 안에 없음
    assert {g for g, _ in ranked} >= {"M1", "M2"}


def test_category_blocked_is_not_a_negative():
    # 차단 문항이 높은 점수(공용 용어)를 받아도 음성 오탐률에는 들어가지 않는다. 금지 ID 노출만 센다
    df = _eval_df([
        {"qid": "C1", "query": "백탁 없는 가벼운 선크림", "query_type": "category_blocked", "context": "general",
         "category": "Face", "forbidden_ids": "SUN1|SUN2"},
        {"qid": "N1", "query": "배송비 무료", "query_type": "negative", "context": "general", "category": "common"},
        {"qid": "P1", "query": "가벼운 사용감", "query_type": "phrase_pair", "context": "general", "any_ids": "OK"},
    ])
    client = FakeClient([[("COMMON1", 0.80, "common"), ("SUN1", 0.70, "Sunscreens & Tanning Products")],
                         [("X1", 0.40, "common")],
                         _ranked(5, correct_at=1)])
    pq = E.run_variant(client, "c", df, [[q] for q in df["query"]], {q: np.zeros(4) for q in df["query"]}, kind="split")
    th = E.threshold_table(df, pq).set_index("t")
    assert th.loc[0.5, "neg_n"] == 1 and th.loc[0.5, "neg_fp_rate"] == 0.0  # 차단 문항(0.80)은 음성 아님
    chk = E.category_check(pq)
    assert chk["blocked_queries"] == 1
    assert chk["blocked_forbidden_hits"]["@1"] == 0 and chk["blocked_forbidden_hits"]["@3"] == 1
    assert chk["queries_with_leak"] == 1  # Face 작업에 선크림 전용 카테고리 결과
