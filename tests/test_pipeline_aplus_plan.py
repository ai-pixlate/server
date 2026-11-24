"""A+ 7칸 구성 — 모델은 템플릿 · 블록 ID · 사진 자리만 고르고, 문구 · 순서 · 한도 · 재료 규칙은 코드가 강제한다(가짜 호출자, 실제 API 호출 없음).

개발용 v1(open-questions.md 2.9절 · #94): 번역문은 그대로 이어 붙인다 · 원래 순서 · 한도 직전에서 중단 · 첫 블록이 넘으면 그 블록만 넣고 초과로 남김 ·
칸 밖 블록 / 이미 쓴 블록 / 없는 ID / 역할이 그 칸에 맞지 않는 블록은 빼고 기록 · label은 블록에서 받지 않음 · 구조가 틀린 응답은 전체 실패.
"""
import json

import pytest
from PIL import Image

from pipeline import config as cfgmod
from pipeline.aplus import content, plan
from pipeline.aplus.spec import load_spec
from pipeline.types import AplusSectionLabel, Section
from pipeline.vlm import LlmReply, VlmError

SPEC = load_spec()
LABELS = ["HERO", "BENEFIT", "SOCIAL", "BRAND"]


@pytest.fixture
def world(tmp_path):
    sections, labels = [], []
    for i, lab in enumerate(LABELS, start=1):
        key = f"sec_1_{i:02d}"
        p = tmp_path / f"{key}.png"
        Image.new("RGB", (1000, 2000), (240, 240, 240)).save(p)
        sections.append(Section(section_key=key, source_image_id=1, section_order=i, top_offset=0, height=2000, width=1000, image_path=str(p)))
        labels.append(AplusSectionLabel(section_key=key, label=lab, confidence="high", reason="r"))

    def B(sec: int, order: int, text: str, role: str = "body") -> plan.TranslatedBlock:
        return plan.TranslatedBlock(f"k{sec}_{order}", f"sec_1_{sec:02d}", order, role, text)

    blocks = [B(1, 1, "Calming Sun\nCream", "title"), B(1, 2, "Daily mineral care"), B(1, 3, "x" * 100), B(1, 4, "y" * 30),
              B(2, 1, "Why you'll love it", "title"), B(2, 2, "72-hour hydration", "title"), B(2, 3, "Soothes redness", "title"),
              B(2, 4, "Non-sticky finish", "title"), B(2, 5, "Deep moisture all day"),
              B(3, 1, "No.1 best seller"), B(4, 1, "Our story", "title"), B(4, 2, "z" * 331)]
    return sections, labels, blocks


class Fake:
    config = {"model": "fake"}

    def __init__(self, reply) -> None:
        self.reply, self.calls = reply, []

    def __call__(self, prompt, payload, images):
        self.calls.append((prompt, payload, images))
        if isinstance(self.reply, Exception):
            raise self.reply
        return LlmReply(text=self.reply if isinstance(self.reply, str) else json.dumps(self.reply), usage=None)


def slot(n, template=None, **kw):
    base = {"slot": n, "template": template, "layout": "default", "mode": None, "why": f"why{n}", "label": [], "headline": [],
            "subheadline": [], "body": [], "image": None, "mark": None, "items": []}
    base.update(kw)
    return base


def answer(*filled):
    by = {s["slot"]: s for s in filled}
    return {"slots": [by.get(n, slot(n)) for n in range(1, 8)]}


IMG = {"section": "s1", "box_2d": [100, 200, 600, 700], "fit": "contain"}


def run(world, reply, recs=None):
    sections, labels, blocks = world
    return plan.run(sections, labels, blocks, SPEC, cfgmod.load_config(), llm=Fake(reply), recorder=recs.append if recs is not None else None)


def test_payload_lists_blocks_only_for_slot_sections(world):
    sections, labels, blocks = world
    payload, sid_to_key, block_ids = plan.build_payload(sections, labels, blocks, SPEC)
    assert sid_to_key == {"s1": "sec_1_01", "s2": "sec_1_02", "s3": "sec_1_03", "s4": "sec_1_04"}
    assert [s["candidates"] for s in payload["slots"]] == [["s1"], ["s2"], [], [], [], [], ["s4"]]
    s1, s2, s3, s4 = payload["sections"]
    assert s1["blocks"][0] == {"id": "s1.b1", "role": "title", "text": "Calming Sun Cream", "chars": 17}  # 줄바꿈 → 공백
    assert "blocks" not in s3 and s3["photo_ok"] is False and s3["slot"] is None  # 7칸 밖 · 사진을 가져오지 않는 라벨
    assert s4["photo_ok"] is True and set(block_ids) >= {"s1.b1", "s2.b4", "s4.b2"} and "s3.b1" not in block_ids
    assert payload["templates"]["2A"]["char_limits"] == SPEC.template("2A")["char_limits"]
    assert set(payload["role_fields"]) == {"title", "body", "caption"}
    assert payload["role_fields"]["title"] == {"fields": ["headline", "items.title"], "by_template": {}, "item_rule": None}
    cap = payload["role_fields"]["caption"]
    assert cap["fields"] == [] and cap["by_template"]["1A"] == ["subheadline"] and cap["by_template"]["4B"] == ["items.title"]
    assert set(cap["by_template"]) == {"1A", "3A", "4B", "5A", "5B", "6A", "3B", "7B"} and cap["item_rule"] == "title_if_no_title_block_else_body"


def test_text_is_verbatim_in_original_order_and_stops_before_limit(world):
    res = run(world, answer(slot(1, "1A", headline=["s1.b1"], body=["s1.b4", "s1.b2", "s1.b3"], image=IMG)))
    s = res.slots[0]
    assert res.status == "ok" and s.errors == []
    assert s.module.headline.text == "Calming Sun Cream" and s.module.headline.origin_block_keys == ["k1_1"]
    # 1A body 한도 120: b2(18자) 뒤에 b3(100자)를 더하면 119자 → 들어가고, b4(30자)를 더하면 넘으므로 그 앞에서 멈춘다
    assert s.module.body.text == "Daily mineral care " + "x" * 100 and s.module.body.origin_block_keys == ["k1_2", "k1_3"]
    assert [(d.field, d.block_keys, d.reason) for d in s.dropped] == [("body", ["k1_4"], "limit")]
    assert "[규칙] body: 블록을 원래 순서로 다시 놓음" in s.flags and s.sources == ["sec_1_01"]
    assert content.limit_excesses(s.module, SPEC) == []


def test_first_block_over_limit_is_kept_whole_and_marked(world):
    res = run(world, answer(slot(7, "7A", headline=["s4.b1"], body=["s4.b2"], image=IMG)))
    m = res.slots[6].module
    assert m.body.text == "z" * 331 and res.slots[6].dropped == []  # 자르지 않는다
    assert [(c.field, c.chars, c.max_chars) for c in content.limit_excesses(m, SPEC)] == [("body", 331, 330)]


def test_blocks_outside_slot_reused_or_unknown_are_dropped_with_reason(world):
    res = run(world, answer(
        slot(1, "1A", headline=["s1.b1"], image=IMG),
        slot(2, "2A", headline=["s2.b1", "s1.b2"], image=IMG,
             items=[{"title": ["s2.b2"], "body": []}, {"title": ["s2.b3", "s9.b9"], "body": []}, {"title": ["s2.b2"], "body": []}])))
    s2 = res.slots[1]
    assert s2.module.headline.origin_block_keys == ["k2_1"]
    assert [it.title.text for it in s2.module.items] == ["72-hour hydration", "Soothes redness"]  # 재료가 남지 않은 항목은 빠진다
    assert {(d.field, tuple(d.block_keys), d.reason) for d in s2.dropped} == {
        ("headline", ("k1_2",), "not_candidate"), ("items[1].title", ("s9.b9",), "unknown"), ("items[2].title", ("k2_2",), "already_used")}
    assert s2.errors == []  # 2A는 항목 2~4개


def test_price_and_caution_blocks_are_not_slot_material(world):
    """PM 결정 2026-11-24(open-questions.md #94): price · caution 역할 블록은 모델에게 보내지 않고, 응답이 가리켜도 쓰지 않는다."""
    sections, labels, blocks = world
    extra = [plan.TranslatedBlock("k1_5", "sec_1_01", 5, "price", "$19.99"),
             plan.TranslatedBlock("k1_6", "sec_1_01", 6, "caution", "For external use only"),
             plan.TranslatedBlock("k1_7", "sec_1_01", 7, "caption", "SPF 50+")]
    payload, _, block_ids = plan.build_payload(sections, labels, blocks + extra, SPEC)
    assert [b["id"] for b in payload["sections"][0]["blocks"]] == ["s1.b1", "s1.b2", "s1.b3", "s1.b4", "s1.b7"]  # 블록 ID는 block_order 그대로
    assert "s1.b5" not in block_ids and "s1.b6" not in block_ids and "$19.99" not in json.dumps(payload)
    recs = []
    res = plan.run(sections, labels, blocks + extra, SPEC, cfgmod.load_config(), recorder=recs.append,
                   llm=Fake(answer(slot(1, "1A", headline=["s1.b1"], subheadline=["s1.b5", "s1.b7"], body=["s1.b6"], image=IMG))))
    s = res.slots[0]
    assert s.module.subheadline.text == "SPF 50+" and s.module.body is None and s.errors == []
    assert {(d.field, tuple(d.block_keys), d.reason) for d in s.dropped} == {("subheadline", ("s1.b5",), "unknown"), ("body", ("s1.b6",), "unknown")}
    assert recs[0]["excluded_role_block_keys"] == ["k1_5", "k1_6"]


def test_template_layout_mode_and_unused_fields_are_corrected(world):
    res = run(world, answer(
        slot(1, "2A", headline=["s1.b1"], image=IMG),
        slot(2, "2A", layout="cards", mode="before_after", headline=["s2.b1"], subheadline=["s2.b2"], image=IMG,
             items=[{"title": ["s2.b3"], "body": ["s2.b4"]}])))
    s1, s2 = res.slots[0], res.slots[1]
    assert s1.template is None and s1.module is None and "쓸 수 없는 템플릿 '2A'" in s1.flags[0]
    assert s2.module.layout == "default" and s2.module.mode is None and s2.module.subheadline is None
    assert s2.module.items[0].body is None
    assert any("layout 'cards'" in f for f in s2.flags) and any("mode 'before_after'" in f for f in s2.flags)
    assert any("subheadline 칸을 쓰지 않아" in f for f in s2.flags) and any("항목 body 칸을 쓰지 않아" in f for f in s2.flags)
    assert s2.errors == ["items 개수 1개 — 2A는 2~4개"]  # 다른 템플릿으로 바꾸거나 채워 넣지 않고 오류로 남긴다
    single = run(world, answer(slot(2, "2A", layout="single", headline=["s2.b1"], body=["s2.b5"], image=IMG,
                                    items=[{"title": ["s2.b3"], "body": []}])))
    assert single.slots[1].module.items == [] and single.slots[1].errors == [] and "k2_3" not in str(single.slots[1].module)


def test_images_boxes_fit_rules_and_donor_photo(world):
    banned = {"section": "s3", "box_2d": [0, 0, 500, 1200], "fit": "cover"}
    res = run(world, answer(
        slot(1, "1B", headline=["s1.b1"], image={"section": "s1", "box_2d": [100, 200, 600, 700], "fit": "cover"}),
        slot(4, "4A", headline=["s1.b2"], image=banned, mark={"section": "s8", "box_2d": [0, 0, 10, 10], "fit": "contain"}),
        slot(7, "7A", headline=["s4.b1"], image={"section": "s4", "box_2d": [500, 500, 500, 900], "fit": "contain"})))
    s1, s4, s7 = res.slots[0], res.slots[3], res.slots[6]
    assert s1.module.image.bbox.model_dump() == {"x": 200, "y": 200, "w": 500, "h": 1000} and s1.module.image.fit == "cover"
    assert s4.module.image.section_key == "sec_1_03" and s4.module.image.fit == "contain"  # 근거 그림은 통째로, 범위 밖 좌표는 자른다
    assert s4.module.image.bbox.model_dump() == {"x": 0, "y": 0, "w": 1000, "h": 1000}
    assert any("[확인] 큰 사진이 sec_1_03(SOCIAL)" in f for f in s4.flags) and any("없는 섹션 's8'" in f for f in s4.flags)
    assert s4.module.mark is None and s4.module.headline is None  # 칸 4 후보가 아닌 블록은 빠진다
    assert s4.errors == ["필수 칸 없음: headline"]
    assert any("넓이가 0" in f for f in s7.flags)
    assert s7.module.image == s1.module.image and "[확인] 큰 사진이 없어 다른 칸의 큰 사진을 다시 씀" in s7.flags


def test_block_role_decides_the_field_and_label_never_comes_from_blocks(world):
    """역할 → 칸(규격 데이터 block_roles, PM 확정 2026-11-24): title → headline · 항목 제목, body → body · 항목 설명,
    caption → 1A · 3A의 subheadline. 맞지 않으면 빼고 기록한다. label은 블록에서 받지 않고 고정 문구 템플릿은 코드가 채운다."""
    sections, labels, blocks = world
    extra = [plan.TranslatedBlock("k1_7", "sec_1_01", 7, "caption", "SPF 50+"), plan.TranslatedBlock("k2_7", "sec_1_02", 7, "caption", "With niacinamide")]
    payload, _, block_ids = plan.build_payload(sections, labels, blocks + extra, SPEC)
    assert "s1.b7" in block_ids and "s2.b7" not in block_ids  # 칸 2(2A · 2B)에는 caption이 들어갈 칸이 없어 보내지 않는다
    recs = []
    res = plan.run(sections, labels, blocks + extra, SPEC, cfgmod.load_config(), recorder=recs.append, llm=Fake(answer(
        slot(1, "1A", label=["s1.b1"], headline=["s1.b2"], subheadline=["s1.b7", "s1.b4"], body=["s1.b1", "s1.b3"], image=IMG),
        slot(2, "2A", headline=["s2.b1"], image=IMG, items=[{"title": ["s2.b2"], "body": []}, {"title": ["s2.b5"], "body": []},
                                                            {"title": ["s2.b3"], "body": []}]),
        slot(7, "7A", headline=["s4.b1"], body=["s4.b2"], image=IMG))))
    s1, s2, s7 = res.slots[0], res.slots[1], res.slots[6]
    assert s1.module.headline is None and s1.module.label is None and s1.errors == ["필수 칸 없음: headline"]  # 채워 넣지 않고 오류로 남긴다
    assert s1.module.subheadline.text == "SPF 50+" and s1.module.body.origin_block_keys == ["k1_3"]
    assert {(d.field, tuple(d.block_keys), d.reason) for d in s1.dropped} == {
        ("headline", ("k1_2",), "role"), ("subheadline", ("k1_4",), "role"), ("body", ("k1_1",), "role")}
    assert "[규칙] label 칸은 블록에서 가져오지 않아 뺌" in s1.flags
    assert [it.title.text for it in s2.module.items] == ["72-hour hydration", "Soothes redness"]
    assert [(d.field, d.block_keys, d.reason) for d in s2.dropped] == [("items[1].title", ["k2_5"], "role")]
    assert s7.module.label.text == "OUR STORY" and s7.module.label.origin_block_keys == [] and s7.errors == []
    assert recs[0]["excluded_role_block_keys"] == ["k2_7"]
    odd = blocks + [plan.TranslatedBlock("k1_9", "sec_1_01", 9, "heading", "x")]
    with pytest.raises(ValueError, match="없는 블록 역할"):
        plan.run(sections, labels, odd, SPEC, cfgmod.load_config(), llm=Fake(answer()))
    caption_in_1b = run((sections, labels, blocks + extra), answer(slot(1, "1B", headline=["s1.b1"], body=["s1.b7"], image=dict(IMG, fit="cover"))))
    assert caption_in_1b.slots[0].module.body is None and caption_in_1b.slots[0].dropped[0].reason == "role"  # caption은 1A · 3A에만


def test_caption_goes_to_item_title_or_body_depending_on_title_block(world):
    """caption의 항목 연결(데이터 담당 답변 2026-11-24): 그 항목에 title 블록이 없으면 항목 제목, 있으면 항목 설명.
    반대쪽 칸에 넣은 caption은 빼고 기록한다."""
    sections, labels, blocks = world

    def X(order: int, role: str, text: str) -> plan.TranslatedBlock:
        return plan.TranslatedBlock(f"k4_{order}", "sec_1_04", order, role, text)

    extra = [X(3, "title", "Clean"), X(4, "caption", "Vegan formula"), X(5, "caption", "Since 2010"), X(6, "caption", "Reef safe"),
             X(7, "caption", "No animal testing"), X(8, "title", "Kind"), X(9, "caption", "Gentle care")]
    res = run((sections, labels, blocks + extra), answer(slot(7, "7B", headline=["s4.b1"], items=[
        {"title": ["s4.b3"], "body": ["s4.b4"]},             # title 블록이 있다 → caption은 설명
        {"title": ["s4.b5"], "body": []},                    # title 블록이 없다 → caption이 제목
        {"title": ["s4.b6"], "body": ["s4.b7"]},             # title 블록이 없다 → 설명에 넣은 caption은 뺀다
        {"title": ["s4.b8", "s4.b9"], "body": []}])))        # title 블록이 있다 → 제목에 넣은 caption은 뺀다
    s7 = res.slots[6]
    got = [(it.title.text if it.title else None, it.body.text if it.body else None) for it in s7.module.items]
    assert got == [("Clean", "Vegan formula"), ("Since 2010", None), ("Reef safe", None), ("Kind", None)] and s7.errors == []
    assert [(d.field, d.block_keys, d.reason) for d in s7.dropped] == [("items[2].body", ["k4_7"], "role"), ("items[3].title", ["k4_9"], "role")]


def test_cutout_request_follows_the_model_answer(world):
    """누끼 여부는 모델이 사진마다 답한다(open-questions.md #98). 코드는 그 답을 바꾸지 않고, 답이 없으면 누끼하지 않는다."""
    res = run(world, answer(
        slot(1, "1A", headline=["s1.b1"], image=dict(IMG, cutout=True)),
        slot(2, "2A", headline=["s2.b1"], image=dict(IMG, fit="cover", cutout=True),
             items=[{"title": ["s2.b2"], "body": []}, {"title": ["s2.b3"], "body": []}]),
        slot(7, "7A", headline=["s4.b1"], image=IMG)))
    s1, s2, s7 = res.slots[0], res.slots[1], res.slots[6]
    assert s1.module.image.cutout is True and s1.flags == []
    assert s2.module.image.fit == "cover" and s2.module.image.cutout is True and s2.flags == []
    assert s7.module.image.cutout is False
    assert "cutout" in plan.response_schema()["properties"]["slots"]["items"]["properties"]["image"]["required"]
    bad = run(world, answer(slot(1, "1A", headline=["s1.b1"], image=dict(IMG, cutout="yes"))))
    assert bad.status == "failed" and "cutout이 true · false가 아니다" in bad.error
    donor = run(world, answer(slot(1, "1A", headline=["s1.b1"], image=dict(IMG, cutout=True)), slot(7, "7A", headline=["s4.b1"])))
    assert donor.slots[6].module.image.cutout is True  # 다른 칸의 큰 사진을 다시 쓸 때 누끼 요청도 함께 온다


def test_alternate_sides_follow_reference_zigzag(world):
    res = run(world, answer(slot(1, "1A", headline=["s1.b1"], image=IMG),
                            slot(2, "2A", headline=["s2.b1"], image=IMG, items=[{"title": ["s2.b2"], "body": []}, {"title": ["s2.b3"], "body": []}]),
                            slot(7, "7A", headline=["s4.b1"], image=IMG)))
    assert [s.module.mirror for s in res.slots if s.module] == [False, True, False]
    same = run(world, answer(slot(1, "1B", headline=["s1.b1"], image=IMG), slot(7, "7A", headline=["s4.b1"], image=IMG)))
    assert [s.module.mirror for s in same.slots if s.module] == [False, True]  # 1B는 바꾸지 않고 세기만 한다(글 상자 왼쪽 → 다음 장은 오른쪽)


@pytest.mark.parametrize("reply, word", [
    ("nope", "JSON 아님"),
    ({"plan": []}, "slots 배열"),
    ({"slots": [slot(n) for n in range(1, 7)]}, "칸 누락 [7]"),
    ({"slots": [slot(n) for n in range(1, 8)] + [slot(3)]}, "칸 3 중복"),
    (answer(slot(1, "1A", headline="s1.b1")), "headline가 블록 ID 배열이 아니다"),
    (answer(slot(1, "1A", image={"section": "s1", "box_2d": [1, 2, 3], "fit": "contain"})), "box_2d가 정수 4개가 아니다"),
    (answer(slot(2, "2A", items=[{"title": "s2.b2"}])), "items[0] 형식"),
])
def test_structural_errors_fail_whole_plan(world, reply, word):
    recs = []
    res = run(world, reply, recs)
    assert res.status == "failed" and res.slots is None and word in res.error and recs[0]["status"] == "validation_failed"


def test_call_failure_and_input_errors(world):
    sections, labels, blocks = world
    assert run(world, VlmError("시간 초과")).status == "failed"
    cfg = cfgmod.load_config()
    with pytest.raises(ValueError, match="labels의 섹션"):
        plan.run(sections, labels[:-1], blocks, SPEC, cfg, llm=Fake(answer()))
    with pytest.raises(ValueError, match="block_key가 겹치거나"):
        plan.run(sections, labels, blocks + [blocks[0]], SPEC, cfg, llm=Fake(answer()))
    odd = [labels[0].model_copy(update={"label": "REVIEW"})] + labels[1:]
    with pytest.raises(ValueError, match="없는 라벨"):
        plan.run(sections, odd, blocks, SPEC, cfg, llm=Fake(answer()))
    assert cfgmod.flatten(cfg)["aplus_plan.prompt_path"] == "pipeline/prompts/aplus_plan.md"


def test_empty_answer_gives_seven_empty_slots_and_one_call(world):
    sections, labels, blocks = world
    fake, recs = Fake(answer()), []
    res = plan.run(sections, labels, blocks, SPEC, cfgmod.load_config(), llm=fake, recorder=recs.append)
    assert [s.slot for s in res.slots] == list(range(1, 8)) and all(s.module is None for s in res.slots)
    assert len(fake.calls) == 1 and [sid for sid, _ in fake.calls[0][2]] == ["s1", "s2", "s3", "s4"]
    assert recs[0]["status"] == "ok" and recs[0]["images"]["s1"]["sent_size"] == [512, 1024]
