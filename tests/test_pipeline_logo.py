"""⑤ 브랜드 로고 제외 — 정규화 · 판정 · 입력 검증 · 결과 타입 불변식 · 입력 불변성(모델 호출 없음).

기대값은 구현이 아니라 승인 규칙에서 도출한다.
- 정본(계약 2.3): 정규화 NFKC → 소문자 → 공백 · 문장부호 제거, 완전 일치, 빈 문자열은 비교하지 않음, 부분 일치 없음. ④ 라벨 블록은 비교 생략(개발계획 2.1).
- 단독 개발 방침(open-questions #68): 소문자 = str.lower()(casefold 아님), 공백 = str.isspace(), 문장부호 = Unicode 범주 P*, 기호 S*는 유지.
  판정 근거 product_label → null · empty_text → false · exact_match → true · no_match → false.
  absent · unconfirmed · ④ 실패 · 누락 · NULL은 false로 바꾸지 않고 입력 오류. 브랜드는 명시된 images 목록으로만 연결(접두사 추정 없음).
  브랜드 자료 전체의 형식 · 상태 · 그룹/원본 중복 · not_in_input 상충을 검사하고, provided 제한은 연결 그룹에만.
"""
import copy

import pytest
from pydantic import ValidationError

from pipeline import config as cfgmod
from pipeline.stages import label as label_stage
from pipeline.stages import logo
from pipeline.types import BBox, LabelChecked, LabelDecision, LabelResult, Line, LogoDecision, LogoResult, MergeResult, OcrRegion, TextBlock

SEC = "sec_1_01"
IMG = "GS-01_001"


def _block(key: str, text: str, order: int, section: str = SEC) -> TextBlock:
    bb = BBox(x=10, y=40 * order, w=200, h=30)
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text, score=0.9,
                    poly=[[10, 40 * order], [210, 40 * order], [210, 40 * order + 30], [10, 40 * order + 30]], bbox=bb)
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=bb, regions=[reg])], bbox=bb, role="body", ocr_confidence=0.9)


def _merge(blocks: list[TextBlock], section: str = SEC) -> MergeResult:
    return MergeResult(section_key=section, blocks=blocks)


def _label(blocks: list[TextBlock], values: dict[str, bool], section: str = SEC) -> LabelResult:
    """③ 블록의 지문을 담은 ④ ok 결과(values에 없는 블록은 false)."""
    labels = [LabelDecision(block_key=b.block_key, is_product_label=values.get(b.block_key, False), basis="vlm")
              for b in sorted(blocks, key=lambda b: b.block_order)]
    return LabelResult(section_key=section, status="ok", labels=labels,
                       checked=LabelChecked(input_fingerprint=label_stage.input_fingerprint(blocks), llm_called=True,
                                            sent_block_keys=[b.block_key for b in blocks]))


def _brand(**over) -> dict:
    meta = {
        "version": "brand-meta-test",
        "products": [
            {"image_group": "GS-01", "product_name": "추적용 상품명 구달", "name_ko": "구달", "name_ko_status": "provided",
             "name_en": "goodal", "name_en_status": "provided", "images": ["GS-01_001", "GS-01_002"]},
            {"image_group": "GS-03", "product_name": "추적용", "name_ko": "비클리닉스", "name_ko_status": "provided",
             "name_en": "b.clinix", "name_en_status": "provided", "images": ["GS-03_001"]},
        ],
        "not_in_input": {"images": {"GS-01_009": "GS-01"}},
    }
    meta.update(over)
    return meta


@pytest.fixture
def cfg():
    return cfgmod.load_config()


def _decide(texts: list[str], cfg, *, labels: dict[str, bool] | None = None, image: str = IMG, brand=None):
    blocks = [_block(f"blk_{i + 1:03d}", t, i + 1) for i, t in enumerate(texts)]
    res, rec = logo.run(image, _merge(blocks), _label(blocks, labels or {}), brand or _brand(), cfg)
    return res, rec


# ---------------------------------------------------------------------------
# 정규화 — NFKC → str.lower() → str.isspace() · P* 제거, S* 유지
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("Goodal", "goodal"),
    ("ＧＯＯＤＡＬ", "goodal"),            # 전각 → NFKC → 소문자
    ("⑴", "1"),                          # NFKC가 먼저: ⑴(No) → "(1)" → 괄호(P*) 제거. 문장부호를 먼저 지우면 괄호가 남는다
    ("㈜구달", "주구달"),                 # ㈜(So) → NFKC "(주)" → 괄호 제거
    ("구 달", "구달"),
    ("구\n달", "구달"),                   # source_ko 줄바꿈(#24)도 공백
    ("구\t달　 ", "구달"),        # 탭 · 전각 공백 · NBSP
    ("b.clinix", "bclinix"),              # Po
    ("b-clinix_「구달」·", "bclinix구달"),  # Pd · Pc · Ps/Pe · Po
    ("goodal®", "goodal®"),               # ®(So)는 기호라 남는다
    ("goodal+$^", "goodal+$^"),           # Sm · Sc · Sk 유지
    ("goodal™", "goodaltm"),              # ™는 NFKC가 "TM"으로 바꾼 뒤 소문자
    ("Straße", "straße"),                 # str.lower(): casefold("ss") 아님
    ("Café", "café"),                     # 악센트 제거 없음
    ("Café", "café"),               # 결합 문자는 NFKC로 합성(Mn은 P*가 아니다)
    ("...!?", ""),
    ("   ", ""),
])
def test_normalize_rules(text, expected):
    assert logo.normalize(text) == expected


def test_config_must_match_approved_steps(cfg):
    logo.validate_config(cfg)  # 기본 config는 승인값
    for bad in (["nfkc", "lower"], ["lower", "nfkc", "strip_space_punct"], ["nfkc", "casefold", "strip_space_punct"], "nfkc"):
        c = copy.deepcopy(cfg)
        c["logo"]["normalize"] = bad
        with pytest.raises(logo.LogoInputError):
            logo.validate_config(c)
    c = copy.deepcopy(cfg)
    del c["logo"]["normalize"]
    with pytest.raises(logo.LogoInputError):
        logo.validate_config(c)


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------
def test_four_bases_and_values(cfg):
    texts = ["구달", "GOODAL", "Goodal ®", "구달 맑은 어성초 선크림", "", " \n ", "···", "셀리맥스", "B.CLINIX"]
    res, _ = _decide(texts, cfg, labels={"blk_009": False})
    got = [(d.block_key, d.is_brand_logo, d.basis) for d in res.decisions]
    assert got == [
        ("blk_001", True, "exact_match"),    # 한글명 완전 일치
        ("blk_002", True, "exact_match"),    # 영문명 — 대소문자만 다름
        ("blk_003", False, "no_match"),      # ® 유지 → 불일치(경고 아님)
        ("blk_004", False, "no_match"),      # 부분 일치 거부
        ("blk_005", False, "empty_text"),
        ("blk_006", False, "empty_text"),
        ("blk_007", False, "empty_text"),    # 문장부호만 — 정규화 후 빈 문자열
        ("blk_008", False, "no_match"),      # 다른 상품 그룹의 브랜드명은 비교 대상이 아니다
        ("blk_009", False, "no_match"),      # GS-01 원본이므로 b.clinix와 비교하지 않는다
    ]
    assert res.status == "ok" and res.error is None and res.image_id == IMG and res.section_key == SEC


def test_other_group_brand_matches_its_own_images(cfg):
    res, rec = _decide(["B.CLINIX", "비클리닉스", "bclinix"], cfg, image="GS-03_001")
    assert [d.basis for d in res.decisions] == ["exact_match", "exact_match", "exact_match"]
    assert rec["brand"]["name_en"] == "b.clinix" and rec["brand"]["name_en_normalized"] == "bclinix"


def test_product_label_true_skips_comparison(cfg):
    res, rec = _decide(["구달", "", "기타 문구"], cfg, labels={"blk_001": True, "blk_002": True})
    assert [(d.is_brand_logo, d.basis) for d in res.decisions] == [(None, "product_label"), (None, "product_label"), (False, "no_match")]
    rows = {r["block_key"]: r for r in rec["decisions"]}
    assert rows["blk_001"]["normalized"] is None  # 비교하지 않았으므로 정규화 값도 기록하지 않는다
    assert rows["blk_003"]["normalized"] == "기타문구"


def test_empty_brand_after_normalization_is_input_error(cfg):
    """브랜드명이 정규화 후 빈 문자열이면 비교할 수 없다 — 빈 블록과 '일치'시키지 않는다."""
    b = _brand()
    b["products"][0]["name_en"] = "..."
    with pytest.raises(logo.LogoInputError):
        _decide(["", "..."], cfg, brand=b)


def test_no_blocks_is_empty_success(cfg):
    res, rec = logo.run(IMG, _merge([]), _label([], {}), _brand(), cfg)
    assert res.status == "ok" and res.decisions == [] and rec["blocks"] == 0


def test_order_by_block_order_then_key(cfg):
    blocks = [_block("blk_003", "a", 2), _block("blk_001", "구달", 3), _block("blk_002", "b", 2)]
    res, _ = logo.run(IMG, _merge(blocks), _label(blocks, {}), _brand(), cfg)
    assert [d.block_key for d in res.decisions] == ["blk_002", "blk_003", "blk_001"]


def test_inputs_not_mutated(cfg):
    blocks = [_block("blk_001", "구달", 1), _block("blk_002", "라벨", 2)]
    merged, lab, brand = _merge(blocks), _label(blocks, {"blk_002": True}), _brand()
    before = (merged.model_dump(), lab.model_dump(), copy.deepcopy(brand))
    logo.run(IMG, merged, lab, brand, cfg)
    assert (merged.model_dump(), lab.model_dump(), brand) == before


def test_record_has_fingerprints_and_normalized_values(cfg):
    blocks = [_block("blk_001", "Goodal", 1)]
    _, rec = logo.run(IMG, _merge(blocks), _label(blocks, {}), _brand(), cfg)
    assert rec["blocks_fingerprint"] == label_stage.input_fingerprint(blocks) == rec["label_input_fingerprint"]
    assert len(rec["label_fingerprint"]) == 64 and len(rec["brand_entry_fingerprint"]) == 64
    assert rec["brand"]["name_ko_normalized"] == "구달" and rec["decisions"][0]["normalized"] == "goodal"
    assert rec["decisions"][0]["matched_names"] == ["name_en"]
    assert rec["counts"] == {"product_label": 0, "empty_text": 0, "exact_match": 1, "no_match": 0, "compared": 1}


# ---------------------------------------------------------------------------
# 브랜드 자료 검사
# ---------------------------------------------------------------------------
def _brand_error(cfg, brand, image=IMG):
    blocks = [_block("blk_001", "구달", 1)]
    with pytest.raises(logo.LogoInputError) as ei:
        logo.run(image, _merge(blocks), _label(blocks, {}), brand, cfg)
    return str(ei.value)


@pytest.mark.parametrize("status", ["absent", "unconfirmed"])
def test_linked_group_must_have_both_names_provided(cfg, status):
    b = _brand()
    b["products"][0]["name_en"], b["products"][0]["name_en_status"] = None, status
    assert "provided" in _brand_error(cfg, b)


def test_other_group_absent_is_allowed(cfg):
    """provided 제한은 연결 그룹에만(형식이 맞는 absent는 다른 그룹에 있어도 된다)."""
    b = _brand()
    b["products"][1]["name_en"], b["products"][1]["name_en_status"] = None, "absent"
    res, _ = _decide(["goodal"], cfg, brand=b)
    assert res.decisions[0].basis == "exact_match"


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(name_en=None),                                 # provided인데 값 없음
    lambda p: p.update(name_en=""),                                   # provided인데 빈 문자열
    lambda p: p.update(name_en_status="absent"),                      # absent인데 값 있음
    lambda p: p.update(name_en_status="없음"),                        # 모르는 상태
    lambda p: p.pop("name_en_status"),
    lambda p: p.update(images="GS-03_001"),                           # 목록 아님
    lambda p: p.update(image_group=""),
])
def test_format_checked_in_every_group(cfg, mutate):
    """연결되지 않은 그룹(GS-03)의 형식 오류도 거부한다."""
    b = _brand()
    mutate(b["products"][1])
    _brand_error(cfg, b)


@pytest.mark.parametrize("brand, image", [
    (_brand(), "GS-01_999"),                                           # 접두사가 같아도 목록에 없으면 연결 없음
    (_brand(), "GS-01_009"),                                           # not_in_input 제외 원본은 판정 대상이 아니다
    (_brand(products=[*_brand()["products"],
                      {"image_group": "GS-09", "name_ko": "가", "name_ko_status": "provided", "name_en": "a", "name_en_status": "provided",
                       "images": ["GS-01_001"]}]), IMG),               # 그룹 간 원본 중복
    (_brand(products=[{**_brand()["products"][0], "images": ["GS-01_001", "GS-01_001"]}]), IMG),  # 그룹 안 중복
    (_brand(products=[_brand()["products"][0], {**_brand()["products"][1], "image_group": "GS-01"}]), IMG),  # 그룹 이름 중복
    (_brand(not_in_input={"images": {"GS-03_001": "GS-03"}}), IMG),    # 제외 원본이 products에도 — 연결 상충(연결 그룹 밖이어도)
    (_brand(not_in_input={"images": ["GS-01_009"]}), IMG),             # not_in_input 형식
    (_brand(products=[]), IMG),
    ([], IMG),
])
def test_link_errors_checked_across_whole_data(cfg, brand, image):
    _brand_error(cfg, brand, image)


def test_no_prefix_inference(cfg):
    msg = _brand_error(cfg, _brand(), "GS-01_003")
    assert "GS-01_003" in msg


# ---------------------------------------------------------------------------
# ③ · ④ 입력 검사 — 실패 · 누락 · NULL을 false로 바꾸지 않는다
# ---------------------------------------------------------------------------
BLOCKS2 = [_block("blk_001", "구달", 1), _block("blk_002", "문구", 2)]


def _input_error(cfg, merged, lab):
    with pytest.raises(logo.LogoInputError):
        logo.run(IMG, merged, lab, _brand(), cfg)


def test_label_failed_rejected(cfg):
    lab = LabelResult(section_key=SEC, status="failed", labels=None, error="504",
                      checked=LabelChecked(input_fingerprint=label_stage.input_fingerprint(BLOCKS2), llm_called=True))
    _input_error(cfg, _merge(BLOCKS2), lab)


def test_section_key_mismatch_rejected(cfg):
    _input_error(cfg, _merge(BLOCKS2), _label(BLOCKS2, {}, section="sec_1_02"))


def test_block_section_key_mismatch_rejected(cfg):
    blocks = [_block("blk_001", "구달", 1), _block("blk_002", "문구", 2, section="sec_1_02")]
    _input_error(cfg, _merge(blocks), _label(blocks, {}))


def test_duplicate_block_keys_rejected(cfg):
    blocks = [_block("blk_001", "구달", 1), _block("blk_001", "문구", 2)]
    lab = _label(blocks[:1], {})
    lab.checked.input_fingerprint = label_stage.input_fingerprint(blocks)
    _input_error(cfg, _merge(blocks), lab)


def test_missing_label_rejected(cfg):
    lab = _label(BLOCKS2, {})
    lab.labels = lab.labels[:1]
    _input_error(cfg, _merge(BLOCKS2), lab)


def test_extra_label_rejected(cfg):
    lab = _label(BLOCKS2, {})
    lab.labels = [*lab.labels, LabelDecision(block_key="blk_099", is_product_label=False, basis="vlm")]
    _input_error(cfg, _merge(BLOCKS2), lab)


def test_duplicate_label_rejected(cfg):
    lab = _label(BLOCKS2, {})
    lab = LabelResult.model_construct(**{**lab.__dict__, "labels": [*lab.labels, lab.labels[0]]})  # 타입 검사를 우회한 입력
    _input_error(cfg, _merge(BLOCKS2), lab)


@pytest.mark.parametrize("bad", [None, 1, "true"])
def test_non_bool_label_rejected(cfg, bad):
    lab = _label(BLOCKS2, {})
    lab.labels[0] = LabelDecision.model_construct(block_key="blk_001", is_product_label=bad, basis="vlm")
    _input_error(cfg, _merge(BLOCKS2), lab)


def test_fingerprint_mismatch_rejected(cfg):
    lab = _label(BLOCKS2, {})
    changed = [_block("blk_001", "구달!", 1), BLOCKS2[1]]  # ④ 이후 ③ 블록이 바뀜
    _input_error(cfg, _merge(changed), lab)


def test_same_section_key_on_other_image_is_independent(cfg):
    """section_key는 원본마다 반복된다(sec_1_01) — 원본이 다르면 각각 판정한다."""
    r1, _ = _decide(["구달"], cfg, image="GS-01_001")
    r2, _ = _decide(["구달"], cfg, image="GS-01_002")
    assert (r1.image_id, r1.section_key) != (r2.image_id, r2.section_key) and r1.decisions == r2.decisions


# ---------------------------------------------------------------------------
# 결과 타입 불변식
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value, basis", [(True, "no_match"), (False, "exact_match"), (None, "no_match"), (False, "product_label"),
                                          (True, "empty_text"), (None, "exact_match")])
def test_decision_value_must_match_basis(value, basis):
    with pytest.raises(ValidationError):
        LogoDecision(block_key="b", is_brand_logo=value, basis=basis)


@pytest.mark.parametrize("value", ["true", 1, 0])
def test_decision_strict_bool(value):
    with pytest.raises(ValidationError):
        LogoDecision(block_key="b", is_brand_logo=value, basis="exact_match")


def test_decision_key_required_and_no_extra():
    with pytest.raises(ValidationError):
        LogoDecision.model_validate({"block_key": "b", "basis": "product_label"})  # 누락을 null로 채우지 않는다
    with pytest.raises(ValidationError):
        LogoDecision(block_key="b", is_brand_logo=None, basis="product_label", is_excluded=True)
    with pytest.raises(ValidationError):
        LogoDecision(block_key="b", is_brand_logo=False, basis="mismatch")


def test_result_invariants():
    ok = LogoDecision(block_key="b1", is_brand_logo=False, basis="no_match")
    assert LogoResult(image_id="i", section_key="s", status="ok", decisions=[ok]).schema_version == "1"
    assert LogoResult(image_id="i", section_key="s", status="ok", decisions=[]).decisions == []
    bad = [
        dict(status="ok", decisions=None),
        dict(status="ok", decisions=[ok], error="x"),
        dict(status="ok", decisions=[ok, ok]),
        dict(status="failed", decisions=[ok], error="x"),
        dict(status="failed", decisions=None),
        dict(status="failed", decisions=None, error="  "),
        dict(status="partial", decisions=[ok]),
    ]
    for kw in bad:
        with pytest.raises(ValidationError):
            LogoResult(image_id="i", section_key="s", **kw)
    failed = logo.failed_result("i", "s", "boom")
    assert failed.status == "failed" and failed.decisions is None and failed.error == "boom"
