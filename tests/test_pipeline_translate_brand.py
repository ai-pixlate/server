"""⑧ 번역의 브랜드 · 키워드 입력(`stages.translate.BrandContext`) — 모델 요청 반영 · 빈 입력 · 기존 동작 불변.

모델은 가짜(요청 기록)만 쓴다. 인계 요청(`handoff.translation`) 연결은 BE 확인 전이라 범위 밖이다(open-questions #90).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.handoff import examples as ex
from pipeline.handoff import translation
from pipeline.stages import translate as tr
from pipeline.vlm import sha256_text

CTX = [tr.ContextBlock("b1", 0, "headline", "가짜브랜드 크림"), tr.ContextBlock("b2", 1, "body", "보습 크림 3만원"),
       tr.ContextBlock("b3", 2, "body", "참고 문맥")]
TARGETS = ["b1", "b2"]
TERMS = [tr.Term("7", "크림", "cream", "enforced")]
BASE_PROMPT = "기본 프롬프트\n"


class Spy(ex.FakeTranslate):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, prompt, payload, image):
        self.calls.append((prompt, json.loads(payload)))
        return super().__call__(prompt, payload, image)


def _run(brand):
    spy = Spy()
    outs, diag = tr.translate_section(CTX, TARGETS, TERMS, [], ex.config(), target_lang="en", target_country="US",
                                      prompt=BASE_PROMPT, llm=spy, placeholder_tokens=set(), brand=brand)
    assert len(spy.calls) == 1
    return outs, diag, spy.calls[0]


def test_brand_values_reach_model_request():
    brand = tr.BrandContext(name_ko=" 가짜브랜드 ", name_en="FakeBrand", overview="저자극 더마 브랜드",
                            core_audience="20대 민감성 피부", keywords=["보습", " 저자극 ", "보습", ""])
    outs, diag, (prompt, payload) = _run(brand)
    assert payload["brand"] == {"name_ko": "가짜브랜드", "name_en": "FakeBrand", "overview": "저자극 더마 브랜드",
                                "core_audience": "20대 민감성 피부", "keywords": ["보습", "저자극"]}
    assert prompt == BASE_PROMPT.rstrip("\n") + "\n\n" + tr.brand_prompt()
    assert diag["prompt_sha256"] == sha256_text(prompt)  # 기록 지문은 실제로 보낸 프롬프트
    # 기존 입력은 그대로 함께 간다
    assert [b.get("id") for b in payload["blocks"]] == ["t1", "t2", None]
    assert payload["glossary"] == [{"term_ko": "크림", "term_target": "cream", "enforcement": "enforced"}]
    assert [o.outcome for o in outs] == ["completed", "completed"]


@pytest.mark.parametrize("brand", [None, tr.BrandContext(),
                                   tr.BrandContext(name_ko=" ", name_en="", overview="\n", core_audience=None, keywords=[" ", ""])])
def test_absent_or_blank_brand_keeps_request_identical(brand):
    """선택 입력이 없거나 공백뿐이면 중립 톤 — 모델 요청(프롬프트 · 페이로드)이 브랜드 인자 없는 호출과 바이트 단위로 같다."""
    spy = Spy()
    _, base_diag = tr.translate_section(CTX, TARGETS, TERMS, [], ex.config(), target_lang="en", target_country="US",
                                        prompt=BASE_PROMPT, llm=spy, placeholder_tokens=set())
    _, diag, (prompt, payload) = _run(brand)
    assert prompt == BASE_PROMPT and "brand" not in payload
    assert (prompt, payload) == spy.calls[0]
    assert diag["payload_sha256"] == base_diag["payload_sha256"] and diag["prompt_sha256"] == base_diag["prompt_sha256"]


def test_partial_brand_sends_only_present_keys():
    _, _, (prompt, payload) = _run(tr.BrandContext(overview="비건 스킨케어"))
    assert payload["brand"] == {"overview": "비건 스킨케어"}
    assert prompt.endswith(tr.brand_prompt())
    _, _, (_, payload) = _run(tr.BrandContext(keywords=("수분",)))
    assert payload["brand"] == {"keywords": ["수분"]}
    # 영문명 없이 한글명만 — 허용 입력이며, 치환 조건은 보충 프롬프트가 정한다(아래 시험)
    _, _, (_, payload) = _run(tr.BrandContext(name_ko="가짜브랜드", name_en=" "))
    assert payload["brand"] == {"name_ko": "가짜브랜드"}


def test_brand_prompt_conditions_name_substitution():
    """영문명 치환은 `name_ko` · `name_en`이 둘 다 있을 때만이고, 없으면 기본 브랜드명 규칙으로 돌아간다고 적혀 있어야 한다."""
    line = next(l for l in tr.brand_prompt().splitlines() if l.startswith("- `name_ko`"))
    assert "둘 다 있을 때만" in line and "하나라도 없으면" in line and "제품명 · 브랜드명" in line


@pytest.mark.parametrize("kwargs", [{"keywords": "보습"}, {"keywords": None}, {"keywords": ["보습", 3]}, {"overview": 1},
                                    {"name_en": ["FakeBrand"]}])
def test_brand_type_errors(kwargs):
    with pytest.raises(ValueError):
        tr.BrandContext(**kwargs)


def test_unreadable_brand_prompt_fails_before_call(monkeypatch, tmp_path):
    monkeypatch.setattr(tr, "BRAND_PROMPT_PATH", str(tmp_path / "missing.md"))
    spy = Spy()
    with pytest.raises(ValueError, match="브랜드 보충 프롬프트"):
        tr.translate_section(CTX, TARGETS, TERMS, [], ex.config(), target_lang="en", target_country="US", prompt=BASE_PROMPT,
                             llm=spy, placeholder_tokens=set(), brand=tr.BrandContext(overview="x"))
    assert spy.calls == []
    # 브랜드 값이 없으면 보충 프롬프트를 읽지 않는다 — 기존 호출은 영향 없음
    tr.translate_section(CTX, TARGETS, TERMS, [], ex.config(), target_lang="en", target_country="US", prompt=BASE_PROMPT,
                         llm=spy, placeholder_tokens=set(), brand=tr.BrandContext())
    assert len(spy.calls) == 1


def test_brand_prompt_file_covers_supplied_keys():
    text = tr.brand_prompt()
    for key in ("name_ko", "name_en", "overview", "core_audience", "keywords"):
        assert f"`{key}`" in text


@pytest.fixture(scope="module")
def work(tmp_path_factory):
    w = tmp_path_factory.mktemp("work")
    ex.generate(tmp_path_factory.mktemp("examples"), work_root=w)
    return Path(w).resolve()


def test_handoff_request_unchanged_and_not_connected(work):
    """인계 계층은 아직 브랜드를 받지 않는다: 기존 요청의 모델 입력 · 지문은 기본 프롬프트 그대로이고, `brand` 필드는 형식 오류다."""
    cfg = ex.config()
    req = next(r for n, r, _ in ex.cases(work / "h1") if n == "translate.completed")
    spy = Spy()
    rep = translation.run_translate(req, cfg, llm=spy)
    assert rep.outcome == "completed"
    prompt, payload = spy.calls[0]
    assert "brand" not in payload and prompt == tr.validate_config(cfg, need_api_key=False)
    assert rep.input_manifest["prompt_sha256"] == sha256_text(prompt)
    bad = dict(req, brand={"overview": "x"})
    rep = translation.run_translate(bad, cfg, llm=Spy())
    assert rep.outcome == "failed" and rep.failure.kind == "input_invalid"
