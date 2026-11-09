"""번역 후 검사 BE 연결 준비 — A1 블록별 검사 의미 입력 키 · A2 강제 용어 검사와 참고 용어 충돌 분리 ·
A3 인증 강제 ↔ 문구 참고 충돌 예외 [contract.md 5.1.1 · 5.3]. 합성 값만 쓴다(실제 용어집 · 사전 값 아님)."""
from __future__ import annotations

import copy

import pytest

from pipeline import glossary as gl
from pipeline.handoff import examples as ex
from pipeline.handoff import translation
from pipeline.handoff.bundle import bundle_content_sha256
from pipeline.handoff.validate import validate_report
from pipeline.stages import text_check as tc


@pytest.fixture(scope="module")
def creq(tmp_path_factory):
    root = tmp_path_factory.mktemp("readiness").resolve()
    return next(r for n, r, _ in ex.cases(root) if n == "text_check.completed_with_flags")


def keys(req):
    return translation.text_check_input_keys(copy.deepcopy(req))


def _block(req, key):
    return next(b for b in req["blocks"] if b["block_key"] == key)


def _target(req, key):
    return next(t for t in req["targets"] if t["block_key"] == key)


def _rebundle(req):
    req["bundle"]["bundle_sha256"] = bundle_content_sha256(req["bundle"])


# ---------------------------------------------------------------------------
# A1 — 블록별 검사 의미 입력 키
# ---------------------------------------------------------------------------
def test_input_keys_one_per_target_and_opaque(creq):
    k = keys(creq)
    assert sorted(k) == ["blk_001", "blk_004"] and all(len(v) == 64 for v in k.values()) and k["blk_001"] != k["blk_004"]
    assert keys(creq) == k  # 결정적


def test_revision_and_layout_changes_keep_key_but_change_request_manifest(creq):
    base = keys(creq)
    r = copy.deepcopy(creq)
    _target(r, "blk_001")["revision"] += 5  # 배치 · 배율 저장으로 revision만 오른 경우
    b = _block(r, "blk_001")
    b["bbox"] = {**b["bbox"], "w": b["bbox"]["w"] + 40}
    b["role"] = "caption"
    assert keys(r) == base
    # 고정 요청 · 시도 검증 지문은 revision을 그대로 포함한다(별개 용도)
    assert translation.text_check_manifest(copy.deepcopy(r))[1] != translation.text_check_manifest(copy.deepcopy(creq))[1]


def test_translation_or_source_change_changes_only_that_block(creq):
    base = keys(creq)
    r = copy.deepcopy(creq)
    _target(r, "blk_001")["text"] = "Fake cure cream!"
    k = keys(r)
    assert k["blk_001"] != base["blk_001"] and k["blk_004"] == base["blk_004"]
    r = copy.deepcopy(creq)
    _block(r, "blk_001")["source_ko"] = "가짜치료 크림 리뉴얼"
    k = keys(r)
    assert k["blk_001"] != base["blk_001"] and k["blk_004"] == base["blk_004"]


def test_unrelated_other_block_change_keeps_key(creq):
    base = keys(creq)
    r = copy.deepcopy(creq)
    _block(r, "blk_002")["source_ko"] = "전혀 다른 문장"  # 검사 대상이 아닌 블록
    _target(r, "blk_004")["text"] = "Fake block SPF50 PA++++ (new)"  # 다른 대상 블록의 번역문
    assert keys(r)["blk_001"] == base["blk_001"]


def test_applicable_enforced_term_change_changes_key_reference_does_not(creq):
    base = keys(creq)
    r = copy.deepcopy(creq)
    next(t for t in r["glossary"]["terms"] if t["glossary_id"] == "G-1")["term_target"] = "Cream (new)"
    k = keys(r)
    assert k["blk_001"] != base["blk_001"] and k["blk_004"] == base["blk_004"]  # G-1은 blk_001에만 적용된다
    r = copy.deepcopy(creq)
    r["glossary"]["terms"].append({"glossary_id": "R-1", "term_ko": "가짜치료", "term_target": "x", "enforcement": "reference"})
    assert keys(r) == base  # 참고 용어는 규칙 검사가 소비하지 않는다


def test_consumed_regulation_row_change_changes_key_unconsumed_does_not(creq):
    base = keys(creq)
    r = copy.deepcopy(creq)
    next(e for e in r["bundle"]["regulation"]["entries"] if e["external_id"] == "RG-901")["variant_en"].append("fake remedy")
    _rebundle(r)
    assert all(keys(r)[b] != base[b] for b in base)
    r = copy.deepcopy(creq)  # cosmetic 작업에서 otc 행은 소비하지 않는다
    next(e for e in r["bundle"]["regulation"]["entries"] if e["external_id"] == "RG-906")["variant_en"] = ["fake potency"]
    _rebundle(r)
    assert keys(r) == base


def test_class_or_rule_version_change_changes_key(creq, monkeypatch):
    base = keys(creq)
    r = copy.deepcopy(creq)
    r["regulatory_class"] = "otc"
    assert all(keys(r)[b] != base[b] for b in base)
    monkeypatch.setattr(tc, "TERM_RULES_VERSION", "term-check@test.1")
    assert all(keys(creq)[b] != base[b] for b in base)


def test_key_is_computable_when_glossary_supply_failed(creq):
    base = keys(creq)
    r = copy.deepcopy(creq)
    r["glossary"] = {"status": "failed", "version": None, "reason": "합성: 공급 실패", "terms": []}
    k = keys(r)
    assert sorted(k) == sorted(base) and k != base
    rep = translation.run_text_check(copy.deepcopy(r))
    assert rep.outcome == "failed"  # 검사는 실패하지만 그 실패가 현재 입력의 것인지 키로 비교할 수 있다


def test_key_material_excludes_revision_and_layout(creq):
    m = translation.text_check_input_key_material(copy.deepcopy(creq))["blk_001"]
    assert "revision" not in m and "bbox" not in m and "active_checks" not in m
    assert m["enforced_terms"] == [{"glossary_id": "G-1", "term_ko": "크림", "term_target": "cream"}]


def test_invalid_request_raises(creq):
    r = copy.deepcopy(creq)
    r["bundle"]["regulation"]["entries"][0]["reason"] = "변조"  # 지문 불일치
    with pytest.raises(ValueError):
        translation.text_check_input_keys(r)


# ---------------------------------------------------------------------------
# A2 — 강제 용어 검사와 참고 용어 충돌 분리
# ---------------------------------------------------------------------------
def _with_terms(req, extra):
    r = copy.deepcopy(req)
    r["glossary"]["terms"] = r["glossary"]["terms"] + extra
    return r


def _terms_by_block(rep):
    return {b["block_key"]: (b["terms"], [f for f in b["flags"] if f["type"] == "mandatory_term_unapplied"]) for b in rep.payload["blocks"]}


@pytest.mark.parametrize("extra", [
    # 참고 ↔ 참고 — 정규화 원문이 같은 서로 다른 참고 용어
    [{"glossary_id": "R-1", "term_ko": "가짜치료", "term_target": "fake cure", "enforcement": "reference"},
     {"glossary_id": "R-2", "term_ko": "가짜 치료", "term_target": "pseudo cure", "enforcement": "reference"}],
    # 참고 ↔ 강제 — 강제 용어 G-1과 같은 원문의 참고 용어
    [{"glossary_id": "R-3", "term_ko": "크림", "term_target": "creme", "enforcement": "reference"}],
])
def test_reference_conflict_does_not_fail_enforced_check(creq, extra):
    base = translation.run_text_check(copy.deepcopy(creq))
    r = _with_terms(creq, extra)
    rep = translation.run_text_check(copy.deepcopy(r))
    assert rep.outcome == "completed" and validate_report(rep, r) == []
    assert _terms_by_block(rep) == _terms_by_block(base)  # 강제 용어 적용 · 미적용 결과 그대로
    # 공급 단계의 미해결 충돌은 그대로 GlossaryDataError(A2는 공급 오류를 무시하지 않는다)
    entries = [gl.Entry(t["glossary_id"], t["term_ko"], t["term_target"], t["enforcement"]) for t in r["glossary"]["terms"]]
    with pytest.raises(gl.GlossaryDataError):
        gl.select([(b["block_key"], b["source_ko"]) for b in r["blocks"]], entries)


def test_enforced_detection_applied_and_unapplied_kept(creq):
    rep = translation.run_text_check(copy.deepcopy(creq))
    t = _terms_by_block(rep)
    assert t["blk_001"][0] == [{"glossary_id": "G-1", "status": "applied"}] and t["blk_001"][1] == []
    assert t["blk_004"][0] == [{"glossary_id": "G-2", "status": "unapplied"}] and [f["glossary_id"] for f in t["blk_004"][1]] == ["G-2"]


def test_enforced_conflict_and_bad_enforced_data_still_fail(creq):
    r = _with_terms(creq, [{"glossary_id": "G-9", "term_ko": "크 림", "term_target": "creme", "enforcement": "enforced"}])
    rep = translation.run_text_check(r)
    assert rep.outcome == "failed" and rep.failure.kind == "input_invalid" and "임의로 고르지 않는다" in rep.failure.message
    r = _with_terms(creq, [{"glossary_id": "G-8", "term_ko": "크림", "term_target": " ", "enforcement": "enforced"}])
    rep = translation.run_text_check(r)
    assert rep.outcome == "failed" and rep.failure.kind == "input_invalid"


def test_slash_in_enforced_target_stays_unverifiable():
    terms = [tc.GlossaryTerm("I-1", "히알루론산", "Hyaluronic Acid/Sodium Hyaluronate", "enforced"),
             tc.GlossaryTerm("R-1", "히알루론산", "HA", "reference")]  # 같은 원문의 참고 용어가 있어도 검사는 막히지 않는다
    r = tc.check_block("b", 0, "with Hyaluronic Acid", "히알루론산 함유", [], terms)
    assert r["terms"] == [{"glossary_id": "I-1", "status": "unverifiable",
                           "reason": "term_target에 대안 구분 문자가 있어 적용 판단 규칙이 정해지지 않았다"}]
    assert r["flags"] == [] and r["complete"] is False


# ---------------------------------------------------------------------------
# A3 — 인증 강제 ↔ 문구 참고 충돌 예외
# ---------------------------------------------------------------------------
def C(gid, ko, en="Cert", kind="certification"):
    return gl.Entry(gid, ko, en, "enforced", kind)


def M(gid, ko, en="phrase", kind="marketing"):
    return gl.Entry(gid, ko, en, "reference", kind)


def ids(entries):
    return [e.glossary_id for e in entries]


# G05 · G06 · G07과 같은 구조(인증 강제와 문구 참고가 같은 블록의 같은 구간에 매칭). ID · 값은 합성이다
@pytest.mark.parametrize("ko", ["무향", "오일프리", "비건"])
def test_certification_wins_same_span_and_phrase_is_excluded(ko):
    entries = [C("C-1", ko), M("M-1", ko), M("M-2", "산뜻한")]
    blocks = [("b1", f"{ko} 크림 산뜻한 마무리")]
    sel = gl.select_by_block(blocks, entries)
    assert ids(gl.select(blocks, entries)) == ["C-1", "M-2"]
    assert sel.blocks == (gl.BlockSupply("b1", ("C-1", "M-2")),) and sel.union_conflicts == ()
    assert [(x.block_key, x.reference_id, x.enforced_ids, x.start, x.end, x.rule) for x in sel.exclusions] == \
        [("b1", "M-1", ("C-1",), 0, len(ko), "certification_over_phrase")]


def test_excluded_phrase_is_not_resupplied_through_union():
    entries = [C("C-1", "무향"), M("M-1", "무향"), M("M-2", "산뜻한")]
    blocks = [("b1", "무향 크림"), ("b2", "산뜻한 무향 로션"), ("b3", "산뜻한 마무리")]
    sel = gl.select_by_block(blocks, entries)
    assert [(b.block_key, b.glossary_ids) for b in sel.blocks] == [("b1", ("C-1",)), ("b2", ("C-1", "M-2")), ("b3", ("M-2",))]
    assert ids(gl.select(blocks, entries)) == ["C-1", "M-2"]  # M-1은 어느 블록에서도 공급되지 않는다


def test_phrase_valid_in_other_block_is_kept_per_block_but_not_flattened():
    """b1은 인증과 같은 구간 충돌(문구 제외), b2는 인증 매칭이 더 긴 강제 용어에 억제돼 문구가 정상 유효."""
    entries = [C("C-1", "무향"), gl.Entry("I-1", "무향료", "Fragrance", "enforced", "ingredient"), M("M-1", "무향")]
    blocks = [("b1", "무향 크림"), ("b2", "무향료 함유")]
    sel = gl.select_by_block(blocks, entries)
    assert [(b.block_key, b.glossary_ids) for b in sel.blocks] == [("b1", ("C-1",)), ("b2", ("I-1", "M-1"))]
    assert sel.union_conflicts == (("C-1", "M-1"),)
    with pytest.raises(gl.GlossaryDataError, match="합집합에 다시 들어간다"):
        gl.select(blocks, entries)  # 블록별 공급 계약 전 — 합집합 하나로는 공급하지 않는다


def test_mixed_conflict_and_normal_in_same_block_is_error():
    entries = [C("C-1", "무향"), gl.Entry("I-1", "무향료", "Fragrance", "enforced", "ingredient"), M("M-1", "무향")]
    with pytest.raises(gl.GlossaryDataError, match="블록 안 혼재"):
        gl.select_by_block([("b", "무향 크림, 무향료 함유")], entries)


def test_containment_and_partial_overlap_are_not_same_span():
    # 문구가 인증을 포함 · 인증이 문구를 포함 — 구간이 다르므로 예외가 아니고 원문도 달라 둘 다 공급
    assert ids(gl.select([("b", "비건인증 제품")], [C("C-1", "비건"), M("M-1", "비건인증")])) == ["C-1", "M-1"]
    assert ids(gl.select([("b", "비건인증 제품")], [C("C-1", "비건인증"), M("M-1", "비건")])) == ["C-1", "M-1"]
    assert gl.select_by_block([("b", "비건인증 제품")], [C("C-1", "비건"), M("M-1", "비건인증")]).exclusions == ()


@pytest.mark.parametrize("entries", [
    [gl.Entry("I-1", "무향", "x", "enforced", "ingredient"), M("M-1", "무향")],  # 다른 강제 유형으로 확대하지 않는다
    [gl.Entry("C-1", "무향", "x", "enforced"), M("M-1", "무향")],  # term_kind 없음 — 기존 동작
    [C("C-1", "무향"), gl.Entry("M-1", "무향", "x", "reference")],  # 문구 유형이 확인되지 않음
    [C("C-1", "무향"), gl.Entry("R-1", "무향", "x", "reference", "certification")],  # 문구가 아닌 참고 행
])
def test_unconfirmed_conflicts_stay_glossary_data_error(entries):
    with pytest.raises(gl.GlossaryDataError, match="임의로 고르지 않는다"):
        gl.select([("b", "무향 크림")], entries)


def test_all_phrases_on_certification_span_are_excluded():
    # 같은 원문의 문구 두 행이 모두 인증과 같은 구간이면 둘 다 제외된다(남는 충돌 없음)
    sel = gl.select_by_block([("b", "무향 크림")], [C("C-1", "무향"), M("M-1", "무향"), M("M-2", "무 향")])
    assert ids(sel.entries) == ["C-1"] and sorted(x.reference_id for x in sel.exclusions) == ["M-1", "M-2"]


def test_two_certifications_same_span_stay_error():
    with pytest.raises(gl.GlossaryDataError):
        gl.select([("b", "무향 크림")], [C("C-1", "무향"), C("C-2", "무 향"), M("M-1", "무향")])


def test_supply_and_check_enforced_targets_match_per_block():
    """번역 공급(충돌 예외 적용)과 번역 후 검사의 강제 용어 적용 대상이 블록마다 같다."""
    entries = [C("C-1", "무향"), M("M-1", "무향"), gl.Entry("I-1", "병풀", "Centella", "enforced", "ingredient"),
               gl.Entry("I-2", "병풀추출물", "Centella Extract", "enforced", "ingredient"), M("M-2", "산뜻한")]
    terms = [tc.GlossaryTerm(e.glossary_id, e.term_ko, e.term_target, e.enforcement) for e in entries]
    by_id = {e.glossary_id: e for e in entries}
    blocks = [("b1", "무향 병풀추출물 크림"), ("b2", "산뜻한 병풀"), ("b3", "관련 없음")]
    sel = gl.select_by_block(blocks, entries)
    for (key, src), supply in zip(blocks, sel.blocks):
        supplied = {i for i in supply.glossary_ids if by_id[i].enforcement == "enforced"}
        assert supplied == tc.applicable_enforced_ids(key, src, terms), key


def test_term_kind_must_be_text_when_given():
    with pytest.raises(gl.GlossaryDataError):
        gl.select([("b", "무향")], [gl.Entry("C-1", "무향", "x", "enforced", " ")])


def test_existing_four_field_entries_behave_as_before():
    # term_kind 없이 만든 기존 공급(BE select_glossary 현행)은 결과 · 오류가 이전과 같다
    assert ids(gl.select([("b", "병풀 크림")], [gl.Entry("1", "병풀", "x", "enforced"), gl.Entry("2", "크림", "y", "reference")])) == ["1", "2"]
    with pytest.raises(gl.GlossaryDataError, match="임의로 고르지 않는다"):
        gl.select([("b", "자외선차단")], [gl.Entry("10", "자외선차단", "a", "enforced"), gl.Entry("20", "자외선 차단", "b", "enforced")])


def test_rule_versions_bumped():
    assert gl.SELECT_RULES_VERSION == "glossary-select@2026-11-08.1"
    assert tc.TERM_RULES_VERSION == "term-check@2026-11-08.1"
    assert translation.TEXT_CHECK_INPUT_KEY_VERSION == "text-check-input-key@2026-11-08.1"
