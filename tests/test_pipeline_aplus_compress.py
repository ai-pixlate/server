"""A+ 압축(2차 번역) — 템플릿 한도를 넘은 글 칸만 모델이 줄여 쓰고, 글자 수 · 채택은 코드가 한다(가짜 호출자, 실제 API 호출 없음).

개발용 v1(open-questions.md #94): 대상은 한도를 넘은 글 칸(`label` 제외) · 상품당 1회 호출에 채택되지 않은 칸만 다시 묻기 ·
한도 안 · 비어 있지 않음 · 새 숫자 없음일 때만 채택 · 못 맞춘 칸은 1차 번역 그대로 · 첫 호출 실패는 전체 실패 ·
다시 묻는 호출이 실패하면 채택된 칸은 살림 · 출처 블록과 다른 칸은 바꾸지 않음 · 규제 행을 받으면 `avoid`를 주고
1차 문구에 없던 규제 매칭이 생긴 답은 채택하지 않음.
"""
import copy
import json

import pytest

from pipeline import config as cfgmod
from pipeline.aplus import compress
from pipeline.aplus.spec import load_spec
from pipeline.stages.text_check import RegRow
from pipeline.types import AplusItem, AplusModule, AplusPlan, AplusPlanChecked, AplusSlotPlan, AplusText
from pipeline.vlm import LlmReply, VlmError

SPEC = load_spec()
LONG_HEAD = "Gentle on skin formulated exclusively with zinc"  # 47자 — 2A headline 한도 28
LONG_ITEM = "Fresh and seamless adherence with 24H wear and no cast"  # 54자 — 2A 항목 3개 한도 45
OK_ITEM = "#SPF50+ PA++++"


def T(text, *keys):
    return AplusText(text=text, origin_block_keys=list(keys))


def make_plan(extra=None):
    """칸 2(2A): 제목과 항목 하나가 한도 초과. 칸 7(7A): 한도 안. 나머지는 빈 칸."""
    mods = {
        2: AplusModule(template="2A", headline=T(LONG_HEAD, "k2_1"),
                       items=[AplusItem(title=T(OK_ITEM, "k2_2")), AplusItem(title=T(LONG_ITEM, "k2_3")), AplusItem(title=T("Soothes skin", "k2_4"))]),
        7: AplusModule(template="7A", label=T("OUR STORY"), headline=T("Good for all of us", "k7_1"), body=T("We care for every step.", "k7_2")),
    }
    mods.update(extra or {})
    slots = [AplusSlotPlan(slot=n, template=mods[n].template if n in mods else None, why=f"why{n}", module=mods.get(n),
                           flags=[f"flag{n}"] if n in mods else []) for n in range(1, 8)]
    return AplusPlan(status="ok", slots=slots, checked=AplusPlanChecked(input_fingerprint="fp", spec_version=SPEC.version, llm_called=True))


class Fake:
    config = {"model": "fake"}

    def __init__(self, *replies) -> None:
        self.replies, self.calls = list(replies), []

    def __call__(self, prompt, payload, images):
        self.calls.append((prompt, json.loads(payload), images))
        reply = self.replies[len(self.calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        return LlmReply(text=reply if isinstance(reply, str) else json.dumps(reply), usage={"n": len(self.calls)}, finish_reason="STOP")


def ans(**by_id):
    """c1="문구" → 답, c1=None → 모델이 줄일 수 없다고 답함."""
    return {"targets": [{"id": k, "failed": True, "reason": "줄일 수 없다"} if v is None else {"id": k, "failed": False, "text": v}
                        for k, v in by_id.items()]}


def run(plan, *replies, recs=None, sets=(), regulation=None):
    fake = Fake(*replies)
    out = compress.run(plan, SPEC, cfgmod.load_config(overrides=sets), regulation=regulation, llm=fake,
                       recorder=recs.append if recs is not None else None)
    return out, fake


def by_field(out):
    return {(f.slot, f.field): f for f in out.fields}


def test_targets_are_over_limit_text_fields_in_slot_order():
    plan = make_plan()
    targets = compress.find_targets(plan.slots, SPEC)
    assert [(t.id, t.slot, t.field, t.check.chars, t.check.max_chars) for t in targets] == [("c1", 2, "headline", 47, 28),
                                                                                           ("c2", 2, "items[1].title", 54, 45)]
    assert targets[0].primary == LONG_HEAD


def test_label_field_is_never_a_target():
    plan = make_plan({1: AplusModule(template="1A", label=T("x" * 25), headline=T("Calming Sun Cream", "k1_1"))})  # 1A label 한도 20
    assert all(t.field != "label" for t in compress.find_targets(plan.slots, SPEC))


def test_no_target_means_no_call_and_same_slots(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    plan = make_plan({2: AplusModule(template="2A", headline=T("Gentle zinc care", "k2_1"), items=[AplusItem(title=T(OK_ITEM, "k2_2"))] * 2)})
    out = compress.run(plan, SPEC, cfgmod.load_config())  # 대상이 없으면 호출자 · API 키가 없어도 된다
    assert out.status == "ok" and out.fields == [] and out.slots == plan.slots
    assert out.checked.llm_called is False and out.checked.calls == 0


def test_payload_sends_only_slots_with_targets_and_marks_targets():
    out, fake = run(make_plan(), ans(c1="Zinc-only gentle care", c2="Fresh, seamless 24H wear, no cast"))
    prompt, payload, images = fake.calls[0]
    assert images == [] and "max_chars" in prompt
    assert [s["slot"] for s in payload["slots"]] == [2]  # 칸 7에는 대상이 없다
    s2 = payload["slots"][0]
    assert s2["template"] == "2A" and s2["name"] == SPEC.raw["slots"]["2"]["name_ko"]
    assert s2["texts"] == [
        {"field": "headline", "text": LONG_HEAD, "chars": 47, "target": "c1", "max_chars": 28},
        {"field": "items[0].title", "text": OK_ITEM, "chars": 14},
        {"field": "items[1].title", "text": LONG_ITEM, "chars": 54, "target": "c2", "max_chars": 45},
        {"field": "items[2].title", "text": "Soothes skin", "chars": 12},
    ]
    assert out.checked.llm_called is True and out.checked.calls == 1


def test_fitting_answer_replaces_text_and_keeps_origin_and_everything_else():
    plan = make_plan()
    before = copy.deepcopy(plan)
    out, _ = run(plan, ans(c1="Zinc-only gentle care", c2="Fresh, seamless 24H wear, no cast"))
    assert plan == before  # 받은 결과를 고치지 않는다
    m = out.slots[1].module
    assert m.headline == T("Zinc-only gentle care", "k2_1")
    assert m.items[1].title == T("Fresh, seamless 24H wear, no cast", "k2_3")
    assert m.items[0] == plan.slots[1].module.items[0] and m.items[2] == plan.slots[1].module.items[2]
    assert out.slots[6] == plan.slots[6] and out.slots[1].flags == ["flag2"] and out.slots[1].why == "why2"
    f = by_field(out)[(2, "headline")]
    assert (f.outcome, f.reason, f.attempts) == ("compressed", "fit", 1)
    assert (f.primary_text, f.primary_chars, f.max_chars, f.guide_max_chars) == (LONG_HEAD, 47, 28, 160)
    assert (f.answer_text, f.answer_chars, f.origin_block_keys) == ("Zinc-only gentle care", 21, ["k2_1"])
    assert [(x.slot, x.field) for x in out.fields] == [(2, "headline"), (2, "items[1].title")]


def test_answer_whitespace_is_normalized_before_counting():
    out, _ = run(make_plan(), ans(c1="Zinc-only\n gentle   care ", c2=None))
    assert out.slots[1].module.headline.text == "Zinc-only gentle care"


def test_over_limit_answer_is_asked_again_with_actual_count():
    still_long = "Gentle on skin with only zinc"  # 29자
    out, fake = run(make_plan(), ans(c1=still_long, c2="Fresh, seamless 24H wear, no cast"), ans(c1="Gentle, zinc-only care"))
    assert len(fake.calls) == 2 and out.checked.calls == 2
    texts = fake.calls[1][1]["slots"][0]["texts"]
    assert texts[0] == {"field": "headline", "text": LONG_HEAD, "chars": 47, "target": "c1", "max_chars": 28,
                        "tried": [{"text": still_long, "chars": 29, "problem": "over_limit"}]}
    assert texts[2] == {"field": "items[1].title", "text": "Fresh, seamless 24H wear, no cast", "chars": 33}  # 채택된 칸은 새 문구 · 대상 아님
    f = by_field(out)
    assert (f[(2, "headline")].outcome, f[(2, "headline")].attempts, f[(2, "headline")].answer_text) == ("compressed", 2, "Gentle, zinc-only care")
    assert f[(2, "items[1].title")].attempts == 1


def test_field_that_never_fits_keeps_primary_translation():
    a, b = "Gentle on skin with only zinc", "Gentle on skin, zinc-only care"
    out, fake = run(make_plan(), ans(c1=a, c2=None), ans(c1=b))
    assert len(fake.calls) == 2
    assert out.slots[1].module.headline == T(LONG_HEAD, "k2_1")
    f = by_field(out)[(2, "headline")]
    assert (f.outcome, f.reason, f.attempts, f.answer_text, f.answer_chars) == ("kept", "over_limit", 2, b, 30)
    assert fake.calls[1][1]["slots"][0]["texts"][0]["tried"] == [{"text": a, "chars": 29, "problem": "over_limit"}]


def test_max_retries_zero_asks_once():
    out, fake = run(make_plan(), ans(c1="Gentle on skin with only zinc", c2=None), sets=["aplus_compress.max_retries=0"])
    assert len(fake.calls) == 1 and by_field(out)[(2, "headline")].reason == "over_limit"


def test_declined_field_is_not_asked_again():
    out, fake = run(make_plan(), ans(c1=None, c2=None))
    assert len(fake.calls) == 1
    f = by_field(out)[(2, "headline")]
    assert (f.outcome, f.reason, f.answer_text, f.answer_chars, f.attempts) == ("kept", "declined", None, None, 1)
    assert out.slots == make_plan().slots


def test_new_number_is_rejected_and_asked_again():
    out, fake = run(make_plan(), ans(c1="Zinc-only gentle care", c2="Fresh, seamless 48H wear, no cast"),
                    ans(c2="Fresh, seamless wear, no cast"), sets=["aplus_compress.max_retries=1"])
    assert fake.calls[1][1]["slots"][0]["texts"][2]["tried"] == [{"text": "Fresh, seamless 48H wear, no cast", "chars": 33, "problem": "new_number"}]
    f = by_field(out)[(2, "items[1].title")]
    assert (f.outcome, f.attempts) == ("compressed", 2)  # 숫자를 빼는 것은 막지 않는다 — 새 숫자만 막는다
    out2, _ = run(make_plan(), ans(c1="Zinc-only gentle care", c2="Fresh, seamless 48H wear"), sets=["aplus_compress.max_retries=0"])
    assert by_field(out2)[(2, "items[1].title")].reason == "new_number"
    assert out2.slots[1].module.items[1].title.text == LONG_ITEM


@pytest.mark.parametrize("text, expected", [("892.35% Improvement", {"892.35"}), ("#SPF50+ PA++++", {"50"}), ("1,000 users in 2 weeks", {"1000", "2"}),
                                            ("No numbers", set())])
def test_numbers(text, expected):
    assert compress.numbers(text) == expected


def test_blank_answer_is_rejected():
    out, fake = run(make_plan(), ans(c1="  ", c2=None), ans(c1="Zinc-only gentle care"))
    assert fake.calls[1][1]["slots"][0]["texts"][0]["tried"] == [{"text": "", "chars": 0, "problem": "blank"}]
    assert by_field(out)[(2, "headline")].outcome == "compressed"
    out2, _ = run(make_plan(), ans(c1="", c2=None), sets=["aplus_compress.max_retries=0"])
    f = by_field(out2)[(2, "headline")]
    assert (f.outcome, f.reason, f.answer_text) == ("kept", "blank", None)


@pytest.mark.parametrize("second, reason", [(None, "declined"), ("  ", "blank")])
def test_record_follows_last_answer_when_retry_gives_no_text(second, reason):
    first = "Gentle on skin with only zinc"  # 29자 — 한도 28 초과라 다시 묻는다
    out, fake = run(make_plan(), ans(c1=first, c2=None), ans(c1=second))
    assert len(fake.calls) == 2
    assert out.slots[1].module.headline == T(LONG_HEAD, "k2_1")
    f = by_field(out)[(2, "headline")]
    assert (f.outcome, f.reason, f.attempts, f.answer_text, f.answer_chars) == ("kept", reason, 2, None, None)  # 첫 답의 문구가 남지 않는다


def test_first_call_failure_is_whole_failure():
    recs = []
    out, _ = run(make_plan(), VlmError("시간 초과"), recs=recs)
    assert (out.status, out.slots, out.fields) == ("failed", None, []) and "시간 초과" in out.error
    assert out.checked.llm_called is True and out.checked.calls == 1
    assert recs[0]["status"] == "call_failed" and recs[0]["calls"][0]["error"]


@pytest.mark.parametrize("reply", [
    "not json", {"answers": []}, ans(c1="Zinc-only gentle care"),  # c2 누락
    {"targets": [{"id": "c1", "failed": False, "text": "a"}, {"id": "c1", "failed": False, "text": "b"}, {"id": "c2", "failed": True}]},
    {"targets": [{"id": "c9", "failed": False, "text": "a"}, {"id": "c1", "failed": True}, {"id": "c2", "failed": True}]},
    {"targets": [{"id": "c1", "failed": False, "text": None}, {"id": "c2", "failed": True}]},
    {"targets": [{"id": "c1", "failed": "no", "text": "a"}, {"id": "c2", "failed": True}]},
])
def test_first_call_bad_structure_is_whole_failure(reply):
    recs = []
    out, _ = run(make_plan(), reply, recs=recs)
    assert out.status == "failed" and out.slots is None
    assert recs[0]["status"] == "validation_failed" and recs[0]["calls"][0]["validation"]["ok"] is False


@pytest.mark.parametrize("second", [VlmError("끊김"), "not json"])
def test_retry_failure_keeps_what_was_already_adopted(second):
    first = "Gentle on skin with only zinc"
    out, fake = run(make_plan(), ans(c1=first, c2="Fresh, seamless 24H wear, no cast"), second)
    assert out.status == "ok" and len(fake.calls) == 2
    assert out.slots[1].module.items[1].title.text == "Fresh, seamless 24H wear, no cast"
    assert out.slots[1].module.headline.text == LONG_HEAD
    f = by_field(out)[(2, "headline")]
    assert (f.outcome, f.reason, f.attempts, f.answer_text) == ("kept", "call_failed", 2, first)


def test_record_has_every_call():
    recs = []
    run(make_plan(), ans(c1="Gentle on skin with only zinc", c2=None), ans(c1="Zinc-only gentle care"), recs=recs)
    (rec,) = recs
    assert rec["stage"] == "aplus_compress" and rec["status"] == "ok" and rec["spec_sha256"] == SPEC.sha256
    assert rec["targets"] == [{"id": "c1", "slot": 2, "field": "headline"}, {"id": "c2", "slot": 2, "field": "items[1].title"}]
    assert [(c["attempt"], c["sent"], c["validation"]["ok"], c["finish_reason"]) for c in rec["calls"]] == [(1, ["c1", "c2"], True, "STOP"),
                                                                                                           (2, ["c1"], True, "STOP")]
    assert [r["outcome"] for r in rec["result"]] == ["compressed", "kept"]


def test_input_errors_raise():
    plan = make_plan()
    failed = AplusPlan(status="failed", slots=None, checked=plan.checked, error="x")
    with pytest.raises(ValueError, match="실패한 7칸 구성"):
        compress.run(failed, SPEC, cfgmod.load_config(), llm=Fake())
    other = plan.model_copy(update={"checked": plan.checked.model_copy(update={"spec_version": "old"})})
    with pytest.raises(ValueError, match="규격 버전"):
        compress.run(other, SPEC, cfgmod.load_config(), llm=Fake())


@pytest.mark.parametrize("override", ["aplus_compress.max_retries=-1", "aplus_compress.max_retries=1.5", "aplus_compress.temperature=3",
                                      "aplus_compress.timeout_s=0", "aplus_compress.model=''", "aplus_compress.prompt_path='no/such.md'"])
def test_bad_config_raises(override):
    with pytest.raises(ValueError, match="aplus_compress"):
        compress.run(make_plan(), SPEC, cfgmod.load_config(overrides=[override]), llm=Fake())


def test_missing_config_table_and_api_key_raise(monkeypatch):
    cfg = cfgmod.load_config()
    with pytest.raises(ValueError, match="표가 없다"):
        compress.run(make_plan(), SPEC, {k: v for k, v in cfg.items() if k != "aplus_compress"}, llm=Fake())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        compress.run(make_plan(), SPEC, cfg)


def test_replace_text_on_item_and_unknown_field():
    m = make_plan().slots[1].module
    new = compress.replace_text(m, "items[2].title", "Soothes")
    assert new.items[2].title == T("Soothes", "k2_4") and m.items[2].title.text == "Soothes skin"
    with pytest.raises(KeyError):
        compress.get_text(m, "body")


# ---------------------------------------------------------------------------
# 규제 반영 — 규제 행을 받았을 때만
# ---------------------------------------------------------------------------
REG = [RegRow("RG-1", "regulated", ("anti-aging", "wrinkle free")), RegRow("RG-2", "conditional", ("soothes acne",)),
       RegRow("RG-A", "allowed", ("moisturizing",))]
GOOD_ITEM = "Fresh, seamless 24H wear, no cast"


def test_without_regulation_payload_has_no_avoid_and_record_says_not_applied():
    out, fake = run(make_plan(), ans(c1="Zinc-only gentle care", c2=GOOD_ITEM))
    assert "avoid" not in fake.calls[0][1] and out.checked.regulation_applied is False


def test_regulation_adds_avoid_list_of_non_allowed_expressions():
    recs = []
    out, fake = run(make_plan(), ans(c1="Zinc-only gentle care", c2=GOOD_ITEM), regulation=REG, recs=recs)
    assert fake.calls[0][1]["avoid"] == ["anti-aging", "wrinkle free", "soothes acne"]  # allowed 행의 표현은 주지 않는다
    assert out.checked.regulation_applied is True and by_field(out)[(2, "headline")].outcome == "compressed"
    assert recs[0]["regulation"] == {"applied": True, "rows": 3, "avoid": ["anti-aging", "wrinkle free", "soothes acne"]}


def test_answer_with_new_regulated_expression_is_rejected_and_asked_again():
    bad = "Anti-Aging zinc care"
    out, fake = run(make_plan(), ans(c1=bad, c2=GOOD_ITEM), ans(c1="Zinc-only gentle care"), regulation=REG)
    assert len(fake.calls) == 2
    assert fake.calls[1][1]["slots"][0]["texts"][0]["tried"] == [{"text": bad, "chars": 20, "problem": "new_regulated"}]
    assert fake.calls[1][1]["avoid"] == fake.calls[0][1]["avoid"]
    f = by_field(out)[(2, "headline")]
    assert (f.outcome, f.reason, f.attempts, f.answer_text) == ("compressed", "fit", 2, "Zinc-only gentle care")


def test_answer_that_stays_regulated_keeps_primary_translation():
    out, _ = run(make_plan(), ans(c1="Anti-aging zinc care", c2=GOOD_ITEM), ans(c1="Wrinkle-free zinc care"), regulation=REG)
    assert out.slots[1].module.headline == T(LONG_HEAD, "k2_1")
    f = by_field(out)[(2, "headline")]
    assert (f.outcome, f.reason, f.attempts, f.answer_text) == ("kept", "new_regulated", 2, "Wrinkle-free zinc care")


def test_regulated_expression_already_in_primary_may_stay():
    head = "Anti-aging care formulated exclusively with zinc"  # 48자 — 1차 문구에 이미 규제 표현이 있다
    plan = make_plan({2: AplusModule(template="2A", headline=T(head, "k2_1"),
                                     items=[AplusItem(title=T(OK_ITEM, "k2_2")), AplusItem(title=T("Soothes skin", "k2_4"))])})
    kept, _ = run(plan, ans(c1="Anti-aging zinc care"), regulation=REG)
    assert by_field(kept)[(2, "headline")].outcome == "compressed"  # 원래 있던 표현은 새 매칭이 아니다
    other, _ = run(plan, ans(c1="Wrinkle-free zinc care"), regulation=REG, sets=["aplus_compress.max_retries=0"])
    assert by_field(other)[(2, "headline")].reason == "new_regulated"  # 같은 항목이어도 1차 문구에 없던 표현은 새 매칭이다


def test_expression_wrapped_by_allowed_expression_is_not_a_new_match():
    rows = [RegRow("RG-1", "regulated", ("zinc care",)), RegRow("RG-A", "allowed", ("gentle zinc care",))]
    out, _ = run(make_plan(), ans(c1="Gentle zinc care", c2=GOOD_ITEM), regulation=rows)
    assert by_field(out)[(2, "headline")].outcome == "compressed"


def test_regulation_data_conflict_in_primary_is_whole_failure_without_call():
    recs = []
    rows = [RegRow("RG-1", "regulated", ("zinc",)), RegRow("RG-A", "allowed", ("zinc",))]  # 같은 구간의 금지/허용 — 임의로 풀지 않는다
    out, fake = run(make_plan(), ans(c1="x", c2="y"), regulation=rows, recs=recs)
    assert (out.status, out.slots, out.fields) == ("failed", None, []) and "MatchConflictError" in out.error
    assert fake.calls == [] and out.checked.llm_called is False and out.checked.regulation_applied is True
    assert recs[0]["status"] == "regulation_failed"


def test_regulation_data_conflict_in_answer_is_whole_failure():
    rows = [RegRow("RG-1", "regulated", ("mild",)), RegRow("RG-A", "allowed", ("mild",))]
    out, fake = run(make_plan(), ans(c1="Mild zinc care", c2=GOOD_ITEM), regulation=rows)
    assert (out.status, out.slots) == ("failed", None) and len(fake.calls) == 1 and out.checked.calls == 1


def test_no_target_with_regulation_makes_no_call(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    plan = make_plan({2: AplusModule(template="2A", headline=T("Gentle zinc care", "k2_1"), items=[AplusItem(title=T(OK_ITEM, "k2_2"))] * 2)})
    out = compress.run(plan, SPEC, cfgmod.load_config(), regulation=REG)
    assert out.status == "ok" and out.fields == [] and out.checked.regulation_applied is True and out.checked.calls == 0


def test_avoid_list_and_regulated_hits():
    assert compress.avoid_list([*REG, RegRow("RG-3", "regulated", ("anti-aging",))]) == ["anti-aging", "wrinkle free", "soothes acne"]
    assert compress.regulated_hits("An Anti-Aging, wrinkle-free cream", REG) == {("RG-1", "anti-aging"), ("RG-1", "wrinkle free")}
    assert compress.regulated_hits("Moisturizing cream", REG) == frozenset()
