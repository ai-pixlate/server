"""A+ 규격 데이터 · 슬롯 내용 검사 — 개발용 v1(open-questions.md 2.9절). 모델 호출 · 렌더 없음.

규격 데이터(pipeline/data/aplus/spec.json)가 참고 자료에서 옮긴 그대로인지(템플릿 14 · 라벨 19 · 구역 67, 출처 해시 기록),
한도 조회가 조건이 많이 맞는 규칙을 고르는지, 자료에 없는 한도를 만들지 않는지, 슬롯 내용 검사가 참고 렌더러 규칙과 같은지 본다.
가이드 상한 표 · 템플릿 → 모듈 필드 대응 · 블록 역할 → 칸 표는 데이터 담당 엑셀(2026-11-24)의 값이다 — 기대값은 그 엑셀과 설명 문서에서 옮겼다.
"""
import copy
import json

import pytest
from pydantic import ValidationError

from pipeline.aplus import content
from pipeline.aplus.spec import SPEC_PATH, AplusSpecError, load_spec
from pipeline.types import AplusImage, AplusItem, AplusModule, AplusText, BBox

SPEC = load_spec()
ONE_SIDED = {"1A", "2A", "2B", "3A", "3B", "4A", "6B", "7A"}


def T(text: str, *keys: str) -> AplusText:
    return AplusText(text=text, origin_block_keys=list(keys))


def IMG(fit: str = "contain") -> AplusImage:
    return AplusImage(section_key="sec_1_01", bbox=BBox(x=0, y=0, w=300, h=300), fit=fit)


def minimal(tid: str, n: int = 3) -> AplusModule:
    """템플릿마다 필수 칸만 채운 최소 내용."""
    req = SPEC.requirements(tid)
    kw: dict = {"template": tid}
    for name in req.required:
        if name == "image":
            kw["image"] = IMG()
        elif name == "items":
            item: dict = {}
            for need in req.items.required:
                first = need.split("|")[0]
                item[first] = "#AABBCC" if first == "color" else IMG() if first == "image" else T("Item title")
            kw["items"] = [AplusItem(**item) for _ in range(max(n, req.items.min))]
        else:
            kw[name] = T("Headline")
    return AplusModule(**kw)


# ---------------------------------------------------------------- 규격 데이터
def test_spec_shape_and_source():
    raw = SPEC.raw
    src = raw["source"]
    assert SPEC.version == "aplus-spec@2026-11-24.2"
    assert raw["canvas"] == {"width": 970, "height": 300}
    assert len(SPEC.template_ids) == 14 and len(raw["labels"]) == 19
    assert sum(len(t["regions"]) for t in raw["templates"].values()) == 67
    assert {tid for tid, t in raw["templates"].items() if t["one_sided"]} == ONE_SIDED
    assert sum(t["amazon_module"] is not None for t in raw["templates"].values()) == 11  # 표준 모듈에 대응 11개, 자체 배치 3개
    assert {tid for tid, t in raw["templates"].items() if t["amazon_module"] is None} == {"2B", "3B", "5A"}  # 엑셀 「템플릿 대응」
    assert raw["templates"]["1B"]["module_ids"] == ["M5", "M6"] and raw["templates"]["2B"]["module_ids"] == []
    assert len(src["data_owner_sheet"]["files"]) == 2 and all(len(h) == 64 for h in src["data_owner_sheet"]["files"].values())
    assert src["renderer"]["commit"].startswith("a889a0c") and src["converter"]["commit"].startswith("c9d3297")
    assert all(len(h) == 64 for part in ("renderer", "converter") for h in src[part]["files"].values())


def test_slots_and_labels():
    for n in range(1, 8):
        assert SPEC.templates_for_slot(n) == (f"{n}A", f"{n}B")
    assert SPEC.labels_for_slot(4) == ("PROOF", "COMPARE", "CERT")
    assert SPEC.labels_for_slot(5) == ("LINEUP", "VARIANT") and SPEC.labels_for_slot(6) == ("HOWTO", "ROUTINE")
    assert [SPEC.slot_of_label(c) for c in ("HERO", "BENEFIT", "INGREDIENT", "BRAND")] == [1, 2, 3, 7]
    outside = {c for c, v in SPEC.raw["labels"].items() if v["slot"] is None}
    assert outside == {"SOCIAL", "TEXTURE", "FEATURE", "TARGET", "PROBLEM", "LOGO", "FAQ", "OTHER"}
    assert {c for c, v in SPEC.raw["labels"].items() if v["photo_banned"]} == {"SOCIAL", "FAQ", "LOGO"}
    assert {c for c, v in SPEC.raw["labels"].items() if v["fill_only"]} == {"FEATURE", "TARGET", "TEXTURE"}
    with pytest.raises(KeyError):
        SPEC.slot_of_label("REVIEW")


def test_char_limit_picks_most_specific_rule():
    assert SPEC.char_limit("2A", "headline") == 28
    assert SPEC.char_limit("2A", "headline", layout="single") == 40
    assert SPEC.char_limit("2A", "body", layout="single") == 160
    assert SPEC.char_limit("2A", "items.title", items=3) == 45 and SPEC.char_limit("2A", "items.title", items=4) == 40
    assert SPEC.char_limit("3B", "items.body", items=4) == 32
    assert SPEC.char_limit("4B", "items.title", items=2) == 30
    assert SPEC.char_limit("4B", "items.title", items=2, mode="before_after") == 12
    assert SPEC.char_limit("6B", "items.body", items=2) == 150 and SPEC.char_limit("6B", "items.body", items=3) == 60
    assert SPEC.char_limit("6B", "body", layout="prose") == 250  # 320 → 250(엑셀 2026-11-24, 가이드 칸당 상한에 맞춤)
    assert SPEC.char_limit("7A", "body") == 330


def test_guide_limits_come_from_module_spec_rows():
    """가이드 상한 = 엑셀 module_spec 시트의 칸당 글자 상한. 자체 배치 · 대응 필드 없는 칸은 None. 배치 변형 · 항목 개수와 무관하다."""
    rows = SPEC.raw["module_spec"]
    assert len(rows) == 15 and {r["module_id"] for r in rows} == {"M1", "M2", "M3", "M4", "M5", "M6"}
    assert all(r["source"].startswith("Awesome Dynamic") and r["confirmed_at"] == "2026-11-19" and r["amazon_screen_confirmed"] is False
               for r in rows)  # 출처 · 확인일은 행마다
    got = {(t, f): SPEC.guide_limit(t, f) for t, f in [
        ("1A", "headline"), ("1A", "body"), ("2A", "items.title"), ("2A", "body"), ("4B", "headline"), ("4B", "items.title"),
        ("5B", "items.body"), ("6A", "items.body"), ("1B", "headline"), ("1B", "body"), ("6B", "body"), ("6B", "items.body")]}
    assert list(got.values()) == [160, 1000, 200, 250, 200, 160, 250, 333, 70, 300, 250, 250]
    for t, f in [("1A", "label"), ("1A", "subheadline"), ("3A", "subheadline"), ("2B", "items.title"), ("3B", "headline"), ("5A", "items.title")]:
        assert SPEC.guide_limit(t, f) is None
    assert SPEC.guide_field("6A", "items.body")["slot_count"] == 3 and SPEC.guide_field("2A", "items.title")["decided_by_us"] is True
    assert SPEC.guide_limit("2A", "items.title") == 200 and SPEC.guide_limit("5B", "items.title") == 160  # 항목 개수와 무관
    with pytest.raises(KeyError):
        SPEC.guide_limit("1A", "image")
    for tid, t in SPEC.raw["templates"].items():  # 템플릿 한도는 가이드 상한보다 작거나 같다(설명 문서 2장)
        for r in t["char_limits"]:
            g = SPEC.guide_limit(tid, r["field"])
            assert g is None or r["max_chars"] <= g, (tid, r)


def test_block_roles_and_label_sources():
    """블록 역할 → 칸(설명 문서 6장, PM 확정 2026-11-24). caption의 항목 연결은 데이터 담당 답변(2026-11-24):
    사진 있는 항목형 4B · 5B · 6A · 3B · 7B · 5A에만, 2A · 2B · 6B에는 넣지 않는다."""
    assert set(SPEC.raw["block_roles"]["roles"]) == {"title", "body", "caption", "price", "caution"}
    assert SPEC.role_fields("title", "1A") == ("headline", "items.title") and SPEC.role_fields("body", "5B") == ("body", "items.body")
    assert SPEC.role_fields("caption", "1A") == SPEC.role_fields("caption", "3A") == ("subheadline",)
    assert all(SPEC.role_fields("caption", t) == ("items.title", "items.body") for t in ("5B", "6A", "3B", "7B"))
    assert SPEC.role_fields("caption", "4B") == SPEC.role_fields("caption", "5A") == ("items.title",)
    assert all(SPEC.role_fields("caption", t) == () for t in ("2A", "2B", "6B", "1B", "4A", "7A"))
    assert SPEC.item_rule("caption") == "title_if_no_title_block_else_body" and SPEC.item_rule("title") is None
    assert SPEC.role_fields("price", "1A") == () and SPEC.role_fields("caution", "7A") == ()
    with pytest.raises(KeyError):
        SPEC.role_fields("heading", "1A")
    src = {tid: (t["label_source"], t["default_label"]) for tid, t in SPEC.raw["templates"].items() if t["label_source"]}
    assert src == {"1A": ("brand_name", None), "3A": ("fixed", "KEY INGREDIENT"), "7A": ("fixed", "OUR STORY")}


def test_limits_added_by_data_owner_answer_and_confirmed_dates():
    """값이 없던 조합(데이터 담당 답변 2026-11-24)과 한도 행마다의 확인일. 기대값은 그 답변에서 옮겼다."""
    assert SPEC.char_limit("2A", "items.title", items=2) == 45
    assert (SPEC.char_limit("3B", "items.title", items=2), SPEC.char_limit("3B", "items.body", items=2)) == (26, 70)
    assert [SPEC.char_limit("5B", "items.title", items=n) for n in (2, 3, 4)] == [45, 26, 20] and SPEC.char_limit("5B", "items.body", items=2) == 40
    assert [SPEC.char_limit("6B", "items.body", items=n) for n in (2, 3, 4)] == [150, 60, 60] and SPEC.char_limit("6B", "items.title", items=4) == 40
    assert SPEC.char_limit("5A", "items.title", items=3) == SPEC.char_limit("5A", "items.title", items=12) == 15
    for tid, t in SPEC.raw["templates"].items():  # 항목 2~4개의 모든 조합에 한도가 있다
        if t["items"] and tid != "5A":
            for f in t["items"]["fields"]:
                if f in ("title", "body"):
                    assert all(SPEC.char_limit(tid, f"items.{f}", items=n) is not None for n in (2, 3, 4)), (tid, f)
    dates = {(tid, r["field"], json.dumps(r.get("when"), sort_keys=True)): r["confirmed_at"] for tid, t in SPEC.raw["templates"].items() for r in t["char_limits"]}
    assert len(dates) == 50 and set(dates.values()) == {"2026-11-10", "2026-11-18", "2026-11-24"}
    assert [k[:2] for k, v in dates.items() if v == "2026-11-18"] == [("2B", "items.title")]
    assert sorted(k[:2] for k, v in dates.items() if v == "2026-11-24") == [
        ("2A", "items.title"), ("3B", "items.body"), ("3B", "items.title"), ("5A", "items.title"), ("5B", "items.title"), ("5B", "items.title"),
        ("6B", "body"), ("6B", "items.body")]
    assert SPEC.raw["template_limit_source"]["source"].startswith("Pixlate 자체 산정")


def test_char_limit_missing_in_source_stays_none():
    """자료에 값이 없는 칸은 None — 가까운 값으로 채우지 않는다(#95)."""
    assert SPEC.char_limit("6B", "body") is None  # 줄글형에서만 쓰는 칸
    assert SPEC.char_limit("5A", "headline", items=3) is None and SPEC.char_limit("4B", "items.body", items=3) is None
    with pytest.raises(KeyError):
        SPEC.char_limit("2A", "items.color")


def test_requirements_follow_variants():
    assert SPEC.requirements("2A").required == ("headline", "image", "items")
    single = SPEC.requirements("2A", layout="single")
    assert single.items is None and "items" not in single.allowed and single.required == ("headline", "image")
    prose = SPEC.requirements("6B", layout="prose")
    assert prose.items is None and "body" in prose.required
    assert "body" not in SPEC.requirements("6B").allowed
    ba = SPEC.requirements("4B", mode="before_after")
    assert (ba.items.min, ba.items.max, ba.items.required) == (2, 2, ("title", "image"))
    assert SPEC.forbidden_layouts("4B", mode="before_after") == ("cards",) and SPEC.forbidden_layouts("4B", mode=None) == ()
    assert SPEC.requirements("5A").items.required == ("color|image",) and SPEC.requirements("5A").items.max == 12


def test_load_spec_rejects_broken_data(tmp_path):
    raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    cases = []
    a = copy.deepcopy(raw)
    a["templates"]["1A"]["regions"][0]["w"] = 2000
    cases.append((a, "캔버스"))
    b = copy.deepcopy(raw)
    b["templates"]["2A"]["char_limits"][0]["field"] = "items.color"
    cases.append((b, "모르는 칸"))
    c = copy.deepcopy(raw)
    c["labels"]["HERO"]["slot"] = 8
    cases.append((c, "1~7"))
    d = copy.deepcopy(raw)
    del d["templates"]["7B"]
    cases.append((d, "templates"))
    e1 = copy.deepcopy(raw)
    e1["templates"]["1A"]["char_limits"][1]["max_chars"] = 161  # 1A headline의 가이드 상한은 160
    cases.append((e1, "가이드 상한 160"))
    e2 = copy.deepcopy(raw)
    e2["templates"]["2B"]["char_limits"][0]["guide_field"] = "M1_headline"  # 자체 배치 템플릿
    cases.append((e2, "대응 모듈"))
    e3 = copy.deepcopy(raw)
    e3["module_spec"][0]["confirmed_at"] = ""
    cases.append((e3, "출처 · 확인일"))
    e4 = copy.deepcopy(raw)
    e4["block_roles"]["roles"]["caption"]["fields"] = ["label"]
    cases.append((e4, "block_roles[caption]"))
    e5 = copy.deepcopy(raw)
    e5["templates"]["3A"]["default_label"] = None
    cases.append((e5, "label_source=fixed"))
    e7 = copy.deepcopy(raw)
    del e7["templates"]["1A"]["char_limits"][0]["confirmed_at"]
    cases.append((e7, "확인일"))
    e8 = copy.deepcopy(raw)
    e8["block_roles"]["roles"]["caption"]["by_template"]["2A"] = ["items.body"]  # 2A 항목에는 설명 칸이 없다
    cases.append((e8, "그리지 않는 칸"))
    e6 = copy.deepcopy(raw)
    e6["templates"]["2A"]["char_limits"][0]["guide_field"] = "M2_body"  # headline 행마다 대응이 달라진다
    cases.append((e6, "한도 행마다 다르다"))
    for i, (data, word) in enumerate(cases):
        p = tmp_path / f"spec{i}.json"
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(AplusSpecError) as e:
            load_spec(p)
        assert any(word in r for r in e.value.reasons), e.value.reasons
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(AplusSpecError):
        load_spec(bad)


# ---------------------------------------------------------------- 슬롯 내용 타입
def test_types_reject_blank_text_duplicate_origin_and_unknown_keys():
    with pytest.raises(ValidationError):
        AplusText(text="  ")
    with pytest.raises(ValidationError):
        AplusText(text="a", origin_block_keys=["blk_001", "blk_001"])
    with pytest.raises(ValidationError):
        AplusImage(section_key="sec_1_01", bbox=BBox(x=0, y=0, w=0, h=10), fit="contain")
    with pytest.raises(ValidationError):
        AplusItem(color="#aabbcc")  # 대문자 #RRGGBB만
    with pytest.raises(ValidationError):
        AplusModule(template="1A", theme={"bg": "#FFFFFF"})
    with pytest.raises(ValidationError):
        AplusModule(template="4B", mode="split")
    assert AplusModule.model_validate_json(minimal("3B").model_dump_json()) == minimal("3B")


# ---------------------------------------------------------------- 슬롯 내용 검사
@pytest.mark.parametrize("tid", SPEC.template_ids)
def test_minimal_content_passes(tid):
    assert content.validate_module(minimal(tid), SPEC) == []


@pytest.mark.parametrize("tid", SPEC.template_ids)
def test_each_required_field_is_enforced(tid):
    for name in SPEC.requirements(tid).required:
        m = minimal(tid).model_copy(update={name: [] if name == "items" else None})
        assert f"필수 칸 없음: {name}" in content.validate_module(m, SPEC)


def test_unknown_template_layout_mode():
    assert "없는 템플릿" in content.validate_module(AplusModule(template="8A"), SPEC)[0]
    assert "layout 'cards'" in content.validate_module(minimal("1A").model_copy(update={"layout": "cards"}), SPEC)[0]
    assert "mode 'before_after'" in content.validate_module(minimal("5B").model_copy(update={"mode": "before_after"}), SPEC)[0]
    m = minimal("4B", n=2).model_copy(update={"mode": "before_after", "layout": "cards"})
    assert content.validate_module(m, SPEC) == ["4B mode 'before_after'에는 layout 'cards'을 쓸 수 없다"]


def test_items_count_and_item_fields():
    assert content.validate_module(minimal("2A", n=5), SPEC) == ["items 개수 5개 — 2A는 2~4개"]
    one = minimal("2A").model_copy(update={"items": [AplusItem(title=T("only"))]})
    assert content.validate_module(one, SPEC) == ["items 개수 1개 — 2A는 2~4개"]
    assert content.validate_module(minimal("5A", n=12), SPEC) == []
    assert content.validate_module(minimal("5A", n=13), SPEC) == ["items 개수 13개 — 5A는 2~12개"]
    no_img = minimal("4B").model_copy(update={"items": [AplusItem(title=T("a")), AplusItem(title=T("b"), image=IMG())]})
    assert content.validate_module(no_img, SPEC) == ["items[0]에 image 없음"]
    swatch = minimal("5A").model_copy(update={"items": [AplusItem(image=IMG("cover")), AplusItem(title=T("Rose"))]})
    assert content.validate_module(swatch, SPEC) == ["items[1]에 color 또는 image 없음"]
    stray = minimal("2A").model_copy(update={"items": [AplusItem(title=T("a"), body=T("b")), AplusItem(title=T("c"))]})
    assert content.validate_module(stray, SPEC) == ["items[0]: 이 템플릿은 쓰지 않는 칸에 값이 있다: body"]


def test_before_after_needs_exactly_two():
    ok = minimal("4B", n=2).model_copy(update={"mode": "before_after"})
    assert content.validate_module(ok, SPEC) == []
    three = minimal("4B", n=3).model_copy(update={"mode": "before_after"})
    assert content.validate_module(three, SPEC) == ["items 개수 3개 — 4B는 2~2개"]


def test_variant_changes_required_and_forbidden_fields():
    single = AplusModule(template="2A", layout="single", headline=T("One big benefit"), body=T("Short paragraph."), image=IMG())
    assert content.validate_module(single, SPEC) == []
    with_items = single.model_copy(update={"items": minimal("2A").items})
    assert content.validate_module(with_items, SPEC) == ["이 배치 변형에서는 쓰지 않는 칸에 값이 있다: items"]
    prose = AplusModule(template="6B", layout="prose", headline=T("How to use"), image=IMG())
    assert content.validate_module(prose, SPEC) == ["필수 칸 없음: body"]
    assert content.validate_module(prose.model_copy(update={"body": T("Apply evenly.")}), SPEC) == []


def test_fields_the_template_does_not_draw_are_errors():
    """참고 렌더러는 조용히 무시하지만, 채운 문구가 결과에 보이지 않는 것을 놓치지 않게 오류로 본다."""
    m = minimal("2A").model_copy(update={"subheadline": T("not drawn")})
    assert content.validate_module(m, SPEC) == ["이 템플릿은 쓰지 않는 칸에 값이 있다: subheadline"]
    m = minimal("6B").model_copy(update={"body": T("prose only")})
    assert content.validate_module(m, SPEC) == ["이 템플릿은 쓰지 않는 칸에 값이 있다: body"]
    assert content.validate_module(minimal("4A").model_copy(update={"mark": IMG()}), SPEC) == []
    assert content.validate_module(minimal("1A").model_copy(update={"mark": IMG()}), SPEC) == ["이 템플릿은 쓰지 않는 칸에 값이 있다: mark"]


# ---------------------------------------------------------------- 글자 수 한도
def test_limit_checks_report_without_cutting():
    long_title = "x" * 46
    m = AplusModule(template="2A", headline=T("h" * 28, "blk_001"), body=T("b" * 61, "blk_002", "blk_003"), image=IMG(),
                    items=[AplusItem(title=T(long_title, "blk_004")), AplusItem(title=T("ok")), AplusItem(title=T("ok"))])
    checks = {c.field: c for c in content.limit_checks(m, SPEC)}
    assert set(checks) == {"headline", "body", "items[0].title", "items[1].title", "items[2].title"}
    assert (checks["headline"].chars, checks["headline"].max_chars, checks["headline"].over) == (28, 28, 0)
    over = content.limit_excesses(m, SPEC)
    assert [(c.field, c.chars, c.max_chars, c.over, c.origin_block_keys) for c in over] == [
        ("body", 61, 60, 1, ("blk_002", "blk_003")), ("items[0].title", 46, 45, 1, ("blk_004",))]
    assert m.items[0].title.text == long_title  # 자르지 않는다
    assert (checks["headline"].guide_max_chars, checks["body"].guide_max_chars, checks["items[0].title"].guide_max_chars) == (160, 250, 200)
    assert content.guide_excesses(m, SPEC) == []  # 템플릿 한도는 넘었지만 가이드 상한 안이다
    far = m.model_copy(update={"headline": T("h" * 161), "items": [AplusItem(title=T("t" * 201))] + m.items[1:]})
    assert [(c.field, c.over_guide) for c in content.guide_excesses(far, SPEC)] == [("headline", 1), ("items[0].title", 1)]
    own = minimal("2B").model_copy(update={"items": [AplusItem(title=T("x" * 500), body=T("y"))] + minimal("2B").items[1:]})
    assert content.guide_excesses(own, SPEC) == [] and content.limit_excesses(own, SPEC)[0].guide_max_chars is None  # 자체 배치는 가이드 상한 없음


def test_limit_depends_on_item_count():
    four = minimal("2A", n=4).model_copy(update={"items": [AplusItem(title=T("y" * 41))] + minimal("2A", n=3).items})
    assert [(c.field, c.max_chars) for c in content.limit_excesses(four, SPEC)] == [("items[0].title", 40)]
    two = minimal("2A").model_copy(update={"items": [AplusItem(title=T("z" * 46)), AplusItem(title=T("ok"))]})
    assert [(c.field, c.max_chars, c.over) for c in content.limit_excesses(two, SPEC)] == [("items[0].title", 45, 1)]  # 항목 2개도 45
    assert content.count_chars("72-hour hydration") == 17  # 공백 포함
