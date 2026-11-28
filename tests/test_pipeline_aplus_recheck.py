"""A+ 압축 뒤 규칙 검사 — 압축이 바꾼 문구에 번역문 규칙 검사(`stages.text_check.check_block`)를 그대로 돌리고 기록만 한다(모델 호출 없음).

개발용 v1(open-questions.md #96): 대상은 압축이 문구를 바꾼 칸뿐 · 문구를 바꾸지 않음 · 용어 목록을 받지 못하면 강제 용어를 검사하지 않고
그 사실을 남김 · 사전 · 용어 데이터 오류는 전체 실패 · 입력 오류는 예외.
"""
import copy
import json

import pytest

from pipeline import config as cfgmod
from pipeline.aplus import compress, recheck
from pipeline.aplus.spec import load_spec
from pipeline.stages.text_check import GlossaryTerm, RegRow
from pipeline.types import AplusItem, AplusModule, AplusPlan, AplusPlanChecked, AplusSlotPlan, AplusText
from pipeline.vlm import LlmReply

SPEC = load_spec()
LONG_HEAD = "Gentle on skin formulated exclusively with zinc"  # 47자 — 2A headline 한도 28
LONG_ITEM = "Fresh and seamless adherence with 24H wear and no cast"  # 54자 — 2A 항목 3개 한도 45
SOURCES = {"k2_1": "오직 징크 성분으로 피부에 순한", "k2_3": "24시간 들뜸 없이 산뜻한 밀착", "k2_2": "#SPF50+ PA++++"}
ROWS = [RegRow("RG-1", "regulated", ("anti-aging", "wrinkle free")), RegRow("RG-2", "conditional", ("soothes acne",))]
ZINC = GlossaryTerm("GL-1", "징크", "zinc", "enforced")


def T(text, *keys):
    return AplusText(text=text, origin_block_keys=list(keys))


class Fake:
    config = {"model": "fake"}

    def __init__(self, reply) -> None:
        self.reply = reply

    def __call__(self, prompt, payload, images):
        return LlmReply(text=json.dumps(self.reply), usage={}, finish_reason="STOP")


def compressed(c1="Gentle on skin with zinc", c2=None):
    """칸 2(2A)의 제목(c1) · 항목 하나(c2)가 압축 대상인 압축 결과. 값이 None이면 모델이 줄일 수 없다고 답한 칸(1차 번역 유지)."""
    mod = AplusModule(template="2A", headline=T(LONG_HEAD, "k2_1"),
                      items=[AplusItem(title=T("#SPF50+ PA++++", "k2_2")), AplusItem(title=T(LONG_ITEM, "k2_3")),
                             AplusItem(title=T("Soothes skin", "k2_4"))])
    slots = [AplusSlotPlan(slot=n, template="2A" if n == 2 else None, why=f"why{n}", module=mod if n == 2 else None) for n in range(1, 8)]
    plan = AplusPlan(status="ok", slots=slots, checked=AplusPlanChecked(input_fingerprint="fp", spec_version=SPEC.version, llm_called=True))
    answers = [{"id": k, "failed": True, "reason": "줄일 수 없다"} if v is None else {"id": k, "failed": False, "text": v}
               for k, v in (("c1", c1), ("c2", c2))]
    return compress.run(plan, SPEC, cfgmod.load_config(overrides=["aplus_compress.over_ratio=1"]), llm=Fake({"targets": answers}))


def test_only_fields_changed_by_compression_are_checked():
    result = compressed(c2=None)
    before = copy.deepcopy(result)
    out = recheck.run(result, SOURCES, ROWS, [])
    assert result == before  # 압축 결과를 고치지 않는다
    assert out.status == "ok" and out.error is None
    assert [(f.slot, f.field, f.block_key, f.text) for f in out.fields] == [(2, "headline", "k2_1", "Gentle on skin with zinc")]
    f = out.fields[0]
    assert f.flags == [] and f.regulatory["matches"] == [] and f.regulatory["checked_ids"] == ["RG-1", "RG-2"]
    assert out.rules == recheck.RULES


def test_regulated_expression_in_compressed_text_is_flagged_without_revision():
    out = recheck.run(compressed(c1="Anti-aging zinc care", c2="Fresh 24H wear, wrinkle-free"), SOURCES, ROWS, [])
    head, item = out.fields
    assert head.flags == [{"type": "regulatory", "producer": "translation_validation", "external_id": "RG-1",
                           "dictionary_verdict_status": "regulated", "problem_text": "Anti-aging", "start": 0, "end": 10,
                           "detected_in": "translation", "detected_by": "rule"}]
    assert [m["external_id"] for m in head.regulatory["matches"]] == ["RG-1"]
    assert item.block_key == "k2_3" and item.flags[0]["problem_text"] == "wrinkle-free"  # 하이픈 · 공백은 같은 구분자다(영어 매칭 규칙)
    assert all("revision" not in flag for f in out.fields for flag in f.flags)


def test_dropped_enforced_term_is_flagged_and_kept_term_is_applied():
    dropped = recheck.run(compressed(c1="Gentle on skin"), SOURCES, ROWS, [ZINC]).fields[0]
    assert dropped.terms == [{"glossary_id": "GL-1", "status": "unapplied"}]
    assert dropped.flags == [{"type": "mandatory_term_unapplied", "producer": "glossary_validation", "glossary_id": "GL-1"}]
    kept = recheck.run(compressed(c1="Gentle on skin with zinc"), SOURCES, ROWS, [ZINC]).fields[0]
    assert kept.terms == [{"glossary_id": "GL-1", "status": "applied"}] and kept.flags == [] and kept.complete is True


def test_term_absent_from_source_is_not_required():
    out = recheck.run(compressed(c1="Gentle on skin"), SOURCES, ROWS, [GlossaryTerm("GL-2", "나이아신아마이드", "niacinamide", "enforced")])
    assert out.fields[0].terms == [] and out.fields[0].flags == []


def test_missing_term_list_is_recorded_as_not_checked():
    out = recheck.run(compressed(), SOURCES, ROWS, None)
    assert out.terms_checked is False and out.fields[0].terms == [] and out.fields[0].complete is False
    supplied = recheck.run(compressed(), SOURCES, ROWS, [])  # 빈 목록 = 적용할 강제 용어 없음. 받지 못한 것과 다르다
    assert supplied.terms_checked is True and supplied.fields[0].complete is True


def test_regulation_row_without_english_pattern_is_recorded_as_unchecked():
    out = recheck.run(compressed(), SOURCES, [*ROWS, RegRow("RG-9", "regulated", ())], [])
    f = out.fields[0]
    assert f.regulatory["unchecked_ids"] == ["RG-9"] and f.complete is False


def test_no_changed_field_means_empty_result():
    out = recheck.run(compressed(c1=None, c2=None), SOURCES, ROWS, [ZINC])
    assert (out.status, out.fields, out.terms_checked) == ("ok", [], True)


def test_dictionary_conflict_is_whole_failure():
    recs = []
    rows = [RegRow("RG-1", "regulated", ("zinc",)), RegRow("RG-A", "allowed", ("zinc",))]  # 같은 구간의 금지/허용 — 임의로 풀지 않는다
    out = recheck.run(compressed(), SOURCES, rows, [], recorder=recs.append)
    assert (out.status, out.fields) == ("failed", []) and "MatchConflictError" in out.error
    assert recs[0]["status"] == "failed" and recs[0]["error"] == out.error


def test_glossary_data_error_is_whole_failure():
    terms = [ZINC, GlossaryTerm("GL-3", "징크", "zinc oxide", "enforced")]  # 같은 원문의 강제 용어 둘 — 적용 대상을 정할 수 없다
    out = recheck.run(compressed(), SOURCES, ROWS, terms)
    assert out.status == "failed" and "GlossaryDataError" in out.error


def test_record_has_targets_and_result():
    recs = []
    out = recheck.run(compressed(), SOURCES, ROWS, None, recorder=recs.append)
    r = recs[0]
    assert (r["stage"], r["status"], r["terms_checked"], r["regulation_rows"], r["terms"]) == ("aplus_recheck", "ok", False, 2, None)
    assert r["targets"] == [{"slot": 2, "field": "headline", "block_key": "k2_1"}]
    assert r["result"] == [f.model_dump(mode="json") for f in out.fields]


def test_failed_compress_result_is_input_error():
    failed = compressed().model_copy(update={"status": "failed", "slots": None, "fields": [], "error": "시간 초과"})
    with pytest.raises(ValueError, match="실패한 압축 결과"):
        recheck.run(failed, SOURCES, ROWS, [])


def test_missing_source_is_input_error():
    with pytest.raises(ValueError, match="한국어 원문이 없는 출처 블록"):
        recheck.run(compressed(), {"k2_3": "x"}, ROWS, [])


def test_changed_field_with_several_origin_blocks_is_input_error():
    result = compressed()
    fields = [result.fields[0].model_copy(update={"origin_block_keys": ["k2_1", "k2_2"]}), *result.fields[1:]]
    with pytest.raises(ValueError, match="출처 블록이 1개가 아닌"):
        recheck.run(result.model_copy(update={"fields": fields}), SOURCES, ROWS, [])


def test_record_text_that_differs_from_slot_text_is_input_error():
    result = compressed()
    fields = [result.fields[0].model_copy(update={"answer_text": "Other text", "answer_chars": 10}), *result.fields[1:]]
    with pytest.raises(ValueError, match="칸의 문구와 압축 기록의 문구가 다르다"):
        recheck.run(result.model_copy(update={"fields": fields}), SOURCES, ROWS, [])
