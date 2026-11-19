"""번역문 검사 — LLM 규제 판정(translation_review) · 규칙 검사 검증 · 집계 계약 테스트 [통합 D12].

합성 입력 · 가짜 모델만 쓴다(실제 모델 · BE · DB 연결 아님). 요청은 계약 예제 생성기(`pipeline.handoff.examples`)의 사례를 재사용한다.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.handoff import examples as ex
from pipeline.handoff import review, translation
from pipeline.handoff.bundle import bundle_content_sha256
from pipeline.handoff.canonical import sha256_canonical
from pipeline.handoff.check_aggregate import AggregateError, CheckInput, aggregate_block_checks
from pipeline.handoff.validate import validate_report
from pipeline.stages import translation_review as rv
from pipeline.vlm import LlmReply, VlmError

FP = "a" * 64  # 현재 입력 지문(합성)
OLD = "b" * 64  # 옛 입력 지문(합성)


@pytest.fixture(scope="module")
def cases(tmp_path_factory):
    root = tmp_path_factory.mktemp("review-cases").resolve()
    return {name: (req, fn) for name, req, fn in ex.cases(root)}


@pytest.fixture()
def cfg():
    return ex.config()


def _run(cases, name):
    req, fn = cases[name]
    return req, fn(copy.deepcopy(req))


class Counting:
    """호출 횟수를 센다. 호출되면 응답 대신 실패한다(호출이 없어야 하는 경로 확인용)."""

    config = {"model": "fake"}

    def __init__(self):
        self.calls = 0

    def __call__(self, prompt, payload, image):
        self.calls += 1
        raise VlmError("호출되면 안 된다")


class Mutating:
    """가짜 판정 응답을 받아 변조한다(응답 검증 경로 확인용)."""

    config = {"model": "fake"}

    def __init__(self, mutate):
        self.base, self.mutate = ex.FakeReview(), mutate

    def __call__(self, prompt, payload, image):
        rep = self.base(prompt, payload, image)
        data = json.loads(rep.text)
        self.mutate(data["results"])
        return LlmReply(text=json.dumps(data, ensure_ascii=False), usage=None, finish_reason="STOP")


def _row(results, t, r):
    return next(x for x in results if x["target"] == t and x["item"] == r)


# ---------------------------------------------------------------------------
# LLM 판정 — 정상 · 생략 · 범위 0개
# ---------------------------------------------------------------------------
def test_completed_has_risk_no_risk_undetermined(cases):
    req, rep = _run(cases, "translation_review.completed")
    assert rep.outcome == "completed" and validate_report(rep, req) == []
    got = {(b["block_key"], j["external_id"]): j for b in rep.payload["blocks"] for j in b["judgments"]}
    assert got[("blk_001", "RG-901")]["judgment"] == "risk" and got[("blk_001", "RG-901")]["basis_summary"] == "both"
    assert got[("blk_002", "RG-902")]["judgment"] == "no_risk" and got[("blk_002", "RG-902")]["mention"] == "absent"
    u = got[("blk_004", "RG-903")]
    assert u["judgment"] == "undetermined" and u["undetermined_reason"] == "condition_unverifiable" and u["unverified_conditions"]
    scope = rep.payload["scope"]
    assert scope["check_item_ids"] == ["RG-901", "RG-902", "RG-903"] and scope["reference_item_ids"] == ["RG-905"]
    assert scope["out_of_scope_item_ids"] == ["RG-906"] and scope["pair_count"] == 9
    # flag: risk · undetermined만, 위치마다 1건
    flags = {b["block_key"]: b["flags"] for b in rep.payload["blocks"]}
    assert [f["judgment"] for f in flags["blk_001"]] == ["risk"] and flags["blk_002"] == []
    f = flags["blk_004"][0]
    assert f["producer"] == "translation_llm_review" and f["detected_by"] == "llm" and f["problem_text"] == "Fake block"
    assert req["targets"][2]["text"][f["start"]:f["end"]] == f["problem_text"] and f["location_key"] == "loc_001"


def test_no_targets_and_no_items_need_no_api_key_or_call(cases, cfg, monkeypatch):
    monkeypatch.delenv(rv.API_KEY_ENV, raising=False)
    req, _ = cases["translation_review.skipped_no_targets"]
    rep = review.run_translation_review(copy.deepcopy(req), cfg)  # llm 없음 · 키 없음
    assert rep.outcome == "skipped" and rep.skip_reason == "no_targets" and validate_report(rep, req) == []
    req, _ = cases["translation_review.completed_no_check_items"]
    rep = review.run_translation_review(copy.deepcopy(req), cfg)
    assert rep.outcome == "completed" and rep.payload["scope"]["pair_count"] == 0 and validate_report(rep, req) == []
    assert all(b["judgments"] == [] and b["flags"] == [] for b in rep.payload["blocks"])  # 범위 0개 — 안전 확인이 아니다


def test_api_key_checked_only_before_real_call(cases, cfg, monkeypatch):
    monkeypatch.delenv(rv.API_KEY_ENV, raising=False)
    req, _ = cases["translation_review.completed"]
    rep = review.run_translation_review(copy.deepcopy(req), cfg)
    assert rep.outcome == "failed" and rep.failure.kind == "config_invalid" and rep.payload is None


def test_input_too_large_fails_without_call(cases, cfg):
    req, _ = cases["translation_review.completed"]
    cfg["translation_review"]["max_input_chars"] = 500
    llm = Counting()
    rep = review.run_translation_review(copy.deepcopy(req), cfg, llm=llm)
    assert llm.calls == 0 and rep.failure.kind == "unsupported_input" and rep.failure.retryable is False
    assert rep.payload["size"]["unit"] == "code_points" and rep.payload["chunks"] == [] and validate_report(rep, req) == []


# ---------------------------------------------------------------------------
# LLM 판정 — 응답 검증
# ---------------------------------------------------------------------------
def _drop(rs):
    rs.pop()


def _dup(rs):
    rs.append(dict(rs[0]))


def _other_item_basis(rs):
    _row(rs, "t1", "r1")["basis"] = ["r1", "r2"]


def _risk_without_basis(rs):
    _row(rs, "t1", "r1")["basis"] = []


def _risk_with_allowed(rs):
    _row(rs, "t1", "r1")["basis"] = ["r1", "a1"]


def _no_risk_with_condition(rs):
    _row(rs, "t2", "r2")["unverified_conditions"] = ["합성 조건"]


def _url_in_reason(rs):
    _row(rs, "t1", "r1")["reason"] = "근거는 https://example.com/x 참고"


def _unknown_citation(rs):
    _row(rs, "t1", "r1")["reason"] = "21 CFR 700.3 위반"


def _present_without_location(rs):
    _row(rs, "t1", "r1")["locations"] = []


def _anchor_not_unique(rs):
    _row(rs, "t1", "r1")["locations"] = [{"quote": "e", "anchor": "e"}]


def _quote_not_in_text(rs):
    _row(rs, "t1", "r1")["locations"] = [{"quote": "missing", "anchor": "missing"}]


def _bad_enum(rs):
    _row(rs, "t1", "r1")["judgment"] = "violation"


@pytest.mark.parametrize("mutate", [_drop, _dup, _other_item_basis, _risk_without_basis, _risk_with_allowed, _no_risk_with_condition,
                                    _url_in_reason, _unknown_citation, _present_without_location, _anchor_not_unique,
                                    _quote_not_in_text, _bad_enum])
def test_invalid_response_rejected_whole(cases, cfg, mutate):
    req, _ = cases["translation_review.completed"]
    rep = review.run_translation_review(copy.deepcopy(req), cfg, llm=Mutating(mutate))
    assert rep.outcome == "failed" and rep.failure.kind == "response_invalid" and rep.failure.retryable is True
    assert "blocks" not in rep.payload and validate_report(rep, req) == []


def test_output_truncated_is_not_retryable(cases):
    req, rep = _run(cases, "translation_review.failed_output_truncated")
    assert rep.failure.kind == "response_invalid" and rep.failure.retryable is False
    assert [c["status"] for c in rep.payload["chunks"]] == ["output_truncated"] and validate_report(rep, req) == []


def test_call_failure_is_retryable(cases, cfg):
    req, _ = cases["translation_review.completed"]
    rep = review.run_translation_review(copy.deepcopy(req), cfg, llm=ex.FailingLlm())
    assert rep.failure.kind == "model_call_failed" and rep.failure.retryable is True and validate_report(rep, req) == []


def test_partial_chunk_failure_adopts_nothing_and_keeps_diagnostics(cases):
    req, rep = _run(cases, "translation_review.failed_chunk_call")
    assert [c["status"] for c in rep.payload["chunks"]] == ["validated", "call_failed", "not_called"]
    assert "blocks" not in rep.payload and rep.payload["non_adoptable_diagnostics"]
    assert {j["chunk_index"] for d in rep.payload["non_adoptable_diagnostics"] for j in d["judgments"]} == {0}
    assert validate_report(rep, req) == []


# ---------------------------------------------------------------------------
# LLM 판정 — 입력 · 지문
# ---------------------------------------------------------------------------
def test_missing_label_logo_value_is_not_assumed_false(cases, cfg):
    req, _ = cases["translation_review.completed"]
    bad = copy.deepcopy(req)
    del bad["targets"][0]["is_product_label"]
    rep = review.run_translation_review(bad, cfg, llm=ex.FakeReview())
    assert rep.outcome == "failed" and rep.failure.kind == "input_invalid"


def _change_text(r, c):
    r["targets"][0]["text"] = "Fake cure cream!"


def _change_target_revision(r, c):
    r["targets"][0]["revision"] = 2


def _change_context_revision(r, c):
    r["context"][0]["revision"] = 5


def _change_context_label(r, c):
    r["context"][0]["is_product_label"] = True
    r["context"][0]["is_brand_logo"] = None


def _change_neighbor_source(r, c):
    r["blocks"][2]["source_ko"] = "판독불가 ### 변경"
    r["blocks"][2]["source_lines"][0]["text"] = r["blocks"][2]["source_ko"]
    r["blocks"][2]["source_lines"][0]["regions"][0]["text"] = r["blocks"][2]["source_ko"]


def _change_class(r, c):
    r["regulatory_class"] = "unknown"


def _change_model(r, c):
    c["translation_review"]["model"] = "other-model"


def _change_limits(r, c):
    c["translation_review"]["max_pairs_per_call"] = 7


@pytest.mark.parametrize("change", [_change_text, _change_target_revision, _change_context_revision, _change_context_label,
                                    _change_neighbor_source, _change_class, _change_model, _change_limits])
def test_manifest_changes_with_inputs_affecting_judgment(cases, cfg, change):
    req, _ = cases["translation_review.completed"]
    _, base = review.review_manifest(copy.deepcopy(req), cfg)
    r2, c2 = copy.deepcopy(req), copy.deepcopy(cfg)
    change(r2, c2)
    _, other = review.review_manifest(r2, c2)
    assert other != base


def test_manifest_changes_with_prompt_content(cases, cfg, tmp_path):
    req, _ = cases["translation_review.completed"]
    _, base = review.review_manifest(copy.deepcopy(req), cfg)
    p = tmp_path / "prompt.md"
    p.write_text(Path(rv.REPO_ROOT / cfg["translation_review"]["prompt_path"]).read_text(encoding="utf-8") + "\n추가", encoding="utf-8")
    cfg["translation_review"]["prompt_path"] = str(p)
    assert review.review_manifest(copy.deepcopy(req), cfg)[1] != base


def test_manifest_ignores_attempt_identity(cases, cfg):
    req, _ = cases["translation_review.completed"]
    r2 = copy.deepcopy(req)
    r2["attempt_id"] = "a-9"
    assert review.review_manifest(copy.deepcopy(req), cfg)[1] == review.review_manifest(r2, cfg)[1]


# ---------------------------------------------------------------------------
# LLM 판정 의미 입력 키(contract 5.3 · 7.2) — 고정 요청 지문과 별개, revision · 배치 제외, 섹션 문맥 포함
# ---------------------------------------------------------------------------
def _keys(req, cfg):
    return review.review_input_keys(copy.deepcopy(req), cfg)


def test_input_keys_cover_targets_only_and_are_opaque(cases, cfg):
    req, _ = cases["translation_review.completed"]
    keys = _keys(req, cfg)
    assert list(keys) == [t["block_key"] for t in req["targets"]]
    assert all(len(v) == 64 and int(v, 16) >= 0 for v in keys.values())
    assert len(set(keys.values())) == len(keys)  # 블록마다 다르다
    assert keys == _keys(req, cfg)  # 결정적


def _move_bbox(r, c):
    for b in r["blocks"]:
        b["bbox"] = {k: v + 3 for k, v in b["bbox"].items()}


def _attempt(r, c):
    r["attempt_id"] = "a-9"


def _reg_row(r, external_id):
    return next(e for e in r["bundle"]["regulation"]["entries"] if e["external_id"] == external_id)


def _reseal(r):  # 묶음을 바꾼 뒤 BE가 다시 고정한 묶음처럼 지문을 갱신한다
    r["bundle"]["bundle_sha256"] = bundle_content_sha256(r["bundle"])


def _out_of_scope_row(r, c):  # 화장품 요청의 적용 분류 밖 OTC 행 — LLM이 소비하지 않는다
    _reg_row(r, "RG-906")["reason"] = "합성: 바뀐 OTC 사유"
    _reseal(r)


def _local_dict(r, c):  # 현지 사전 행 — LLM 판정이 소비하지 않는다
    r["bundle"]["local"]["entries"][0]["seller_message"] = "합성: 바뀐 셀러 안내"
    _reseal(r)


@pytest.mark.parametrize("change", [_change_target_revision, _change_context_revision, _move_bbox, _attempt, _out_of_scope_row,
                                    _local_dict])
def test_input_keys_ignore_revision_layout_and_unconsumed_rows(cases, cfg, change):
    req, _ = cases["translation_review.completed"]
    r2, c2 = copy.deepcopy(req), copy.deepcopy(cfg)
    change(r2, c2)
    if change in (_change_target_revision, _change_context_revision):
        assert review.review_manifest(r2, c2)[1] != review.review_manifest(copy.deepcopy(req), cfg)[1]  # 요청 지문은 바뀐다
    assert _keys(r2, c2) == _keys(req, cfg)


def _other_target_text(r, c):
    r["targets"][1]["text"] = r["targets"][1]["text"] + "!"


def _target_becomes_context(r, c):
    t = r["targets"].pop()
    r["context"].append({"block_key": t["block_key"], "revision": t["revision"], "is_product_label": False, "is_brand_logo": False})


def _check_row_reason(r, c):
    _reg_row(r, "RG-901")["reason"] = "합성: 바뀐 사유"
    _reseal(r)


def _reference_row(r, c):
    _reg_row(r, "RG-905")["variant_en"] = ["no fake cure ever"]
    _reseal(r)


def _change_lang(r, c):
    r["target_lang"] = "en-US"


@pytest.mark.parametrize("change", [_other_target_text, _change_context_label, _change_neighbor_source, _target_becomes_context,
                                    _check_row_reason, _reference_row, _change_class, _change_lang, _change_model, _change_limits])
def test_input_keys_change_for_every_target_when_consumed_input_changes(cases, cfg, change):
    req, _ = cases["translation_review.completed"]
    base = _keys(req, cfg)
    r2, c2 = copy.deepcopy(req), copy.deepcopy(cfg)
    change(r2, c2)
    other = _keys(r2, c2)
    assert all(other[k] != v for k, v in base.items() if k in other)  # 섹션 문맥을 소비하므로 남은 대상 블록 모두 바뀐다


def test_input_keys_change_with_prompt_content(cases, cfg, tmp_path):
    req, _ = cases["translation_review.completed"]
    base = _keys(req, cfg)
    p = tmp_path / "prompt.md"
    p.write_text(Path(rv.REPO_ROOT / cfg["translation_review"]["prompt_path"]).read_text(encoding="utf-8") + "\n추가", encoding="utf-8")
    cfg["translation_review"]["prompt_path"] = str(p)
    assert all(_keys(req, cfg)[k] != v for k, v in base.items())


def test_input_keys_refuse_invalid_input(cases, cfg):
    req, _ = cases["translation_review.completed"]
    bad = copy.deepcopy(req)
    del bad["targets"][0]["is_product_label"]  # 누락을 false로 추정하지 않는다
    with pytest.raises(ValueError):
        review.review_input_keys(bad, cfg)


# ---------------------------------------------------------------------------
# 판단 이유 전달 — 현재 보고만으로 flag 후보마다 판정 1건(판단 이유 · 미확인 조건 · 근거)을 찾을 수 있는지
# (flag 형식은 바꾸지 않는다. 외부 저장 · API 연결 형식은 미합의 — contract 7.1 제안)
# ---------------------------------------------------------------------------
def test_every_llm_flag_joins_one_judgment_with_reason(cases):
    _, rep = _run(cases, "translation_review.completed")
    n = 0
    for b in rep.payload["blocks"]:
        for f in b["flags"]:
            js = [j for j in b["judgments"] if j["external_id"] == f["external_id"]]
            assert len(js) == 1  # (블록, 항목)마다 판정 1건
            j = js[0]
            assert j["judgment"] == f["judgment"] != "no_risk" and j["reason"].strip()
            if f["location_key"] is None:
                assert not j["locations"]
            else:
                loc = [x for x in j["locations"] if x["location_key"] == f["location_key"]]
                assert len(loc) == 1 and (loc[0]["quote"], loc[0]["start"], loc[0]["end"]) == (f["problem_text"], f["start"], f["end"])
            if j["undetermined_reason"] == "condition_unverifiable":
                assert j["unverified_conditions"]
            n += 1
    assert n > 0


# ---------------------------------------------------------------------------
# 검증기 — 판정 ↔ 분할 · 실패 payload 혼입
# ---------------------------------------------------------------------------
def test_validator_rejects_wrong_chunk_index(cases):
    req, rep = _run(cases, "translation_review.completed")
    d = rep.model_dump(mode="json")
    d["payload"]["blocks"][0]["judgments"][0]["chunk_index"] = 999
    d["payload"]["result_sha256"] = sha256_canonical(d["payload"]["blocks"])
    assert any("chunk_index" in p for p in validate_report(d, req))


@pytest.mark.parametrize("extra", ["judgments", "flags", "blocks"])
def test_validator_rejects_judgments_in_failed_payload(cases, extra):
    req, rep = _run(cases, "translation_review.failed_chunk_call")
    d = rep.model_dump(mode="json")
    d["payload"][extra] = [{"external_id": "RG-901", "judgment": "risk"}]
    assert any("실패 payload" in p for p in validate_report(d, req))


def test_validator_rejects_tampered_flag(cases):
    req, rep = _run(cases, "translation_review.completed")
    d = rep.model_dump(mode="json")
    d["payload"]["blocks"][0]["flags"] = []
    d["payload"]["result_sha256"] = sha256_canonical(d["payload"]["blocks"])
    assert any("flags" in p for p in validate_report(d, req))


# ---------------------------------------------------------------------------
# 규칙 검사 검증 — 강제 용어 누락
# ---------------------------------------------------------------------------
def test_text_check_rejects_dropping_enforced_term_and_flag(cases):
    req, rep = _run(cases, "text_check.completed_with_flags")
    assert validate_report(rep, req) == []
    d = rep.model_dump(mode="json")
    blk = next(b for b in d["payload"]["blocks"] if any(t["glossary_id"] == "G-2" for t in b["terms"]))
    blk["terms"] = [t for t in blk["terms"] if t["glossary_id"] != "G-2"]
    blk["flags"] = [f for f in blk["flags"] if f.get("glossary_id") != "G-2"]
    d["payload"]["result_sha256"] = sha256_canonical(d["payload"]["blocks"])
    assert any("강제 용어" in p for p in validate_report(d, req))


def test_text_check_manifest_matches_run(cases):
    req, rep = _run(cases, "text_check.completed_with_flags")
    assert translation.text_check_manifest(copy.deepcopy(req))[1] == rep.input_manifest_sha256


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def results(cases):
    req, rule_rep = _run(cases, "text_check.completed_with_flags")
    rreq, llm_rep = _run(cases, "translation_review.completed")
    rule = {b["block_key"]: b for b in rule_rep.payload["blocks"]}
    llm = {b["block_key"]: b for b in llm_rep.payload["blocks"]}
    ids = review.check_item_ids(rreq["bundle"], "cosmetic", "US")
    return rule, llm, ids, llm_rep.payload["scope"]


def _done(result, fp=FP):
    return CheckInput("done", attempt_manifest_sha256=fp, current_manifest_sha256=FP, block_result=result)


def test_rule_only_settles_without_llm_attempt(results):
    rule, _, ids, _ = results
    a = aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=("rule",), rule=_done(rule["blk_001"]), llm=None)
    assert a["checks_settled"] is True and a["checks"]["llm"]["active"] is False
    assert {i["external_id"]: i["rule"] for i in a["items"]}["RG-901"] == "detected"
    assert all(i["llm"] == "inactive" for i in a["items"]) and a["terms"] == rule["blk_001"]["terms"]


def test_inactive_llm_past_result_is_not_used(results):
    rule, llm, ids, _ = results
    past = _done(llm["blk_001"])  # 과거 LLM 결과(risk 포함)를 넘겨도 비활성이면 읽지 않는다
    a = aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=("rule",), rule=_done(rule["blk_001"]), llm=past)
    assert all(i["llm"] == "inactive" for i in a["items"]) and a["references"]["llm_allowed_references"] == []


@pytest.mark.parametrize("state", ["none", "pending", "running", "cancelled"])
def test_both_active_unsettled_when_llm_not_finished(results, state):
    rule, _, ids, _ = results
    llm = CheckInput(state, attempt_manifest_sha256=None if state == "none" else FP, current_manifest_sha256=FP)
    a = aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=("rule", "llm"), rule=_done(rule["blk_001"]), llm=llm)
    assert a["checks_settled"] is False and a["checks"]["llm"]["active"] is True


def test_active_check_without_input_is_error_not_inactive(results):
    rule, _, ids, _ = results
    with pytest.raises(AggregateError):
        aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=("rule", "llm"), rule=_done(rule["blk_001"]), llm=None)


@pytest.mark.parametrize("bad", ["rule", (), ("rule", "rule"), ("rule", "gpt")])
def test_active_checks_must_be_explicit_valid_set(results, bad):
    rule, _, ids, _ = results
    with pytest.raises(AggregateError):
        aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=bad, rule=_done(rule["blk_001"]), llm=None)


def test_current_failure_and_skip_settle(results):
    rule, _, ids, scope = results
    failed = CheckInput("failed", attempt_manifest_sha256=FP, current_manifest_sha256=FP, failed_scope=scope)
    a = aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=("rule", "llm"), rule=_done(rule["blk_001"]), llm=failed)
    assert a["checks_settled"] is True and all(i["llm"] == "unavailable" for i in a["items"])
    skipped = CheckInput("skipped", attempt_manifest_sha256=FP, current_manifest_sha256=FP)
    b = aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=("rule",), rule=skipped, llm=None)
    assert b["checks_settled"] is True


@pytest.mark.parametrize("state", ["done", "failed"])
def test_stale_result_or_failure_does_not_settle(results, state):
    rule, llm, ids, scope = results
    old = CheckInput(state, attempt_manifest_sha256=OLD, current_manifest_sha256=FP,
                     block_result=llm["blk_001"] if state == "done" else None, failed_scope=scope if state == "failed" else None)
    a = aggregate_block_checks(block_key="blk_001", check_item_ids=ids, active_checks=("rule", "llm"), rule=_done(rule["blk_001"]), llm=old)
    assert a["checks_settled"] is False and a["checks"]["llm"]["freshness"] == "stale"
    assert all(i["llm"] == "unavailable" for i in a["items"])  # 옛 결과를 현재 판정으로 쓰지 않는다


def test_both_active_settled_keeps_independent_axes(results):
    rule, llm, ids, _ = results
    a = aggregate_block_checks(block_key="blk_004", check_item_ids=ids, active_checks=("rule", "llm"), rule=_done(rule["blk_004"]),
                               llm=_done(llm["blk_004"]))
    it = {i["external_id"]: i for i in a["items"]}
    assert a["checks_settled"] is True
    assert it["RG-903"]["rule"] == "detected" and it["RG-903"]["llm"] == "undetermined"  # LLM이 규칙 검출을 지우지 않는다
    assert any(t["glossary_id"] == "G-2" and t["status"] == "unapplied" for t in a["terms"])  # 강제 용어 결과 유지


def test_other_block_result_rejected(results):
    rule, llm, ids, scope = results
    with pytest.raises(AggregateError):
        aggregate_block_checks(block_key="blk_004", check_item_ids=ids, active_checks=("rule",), rule=_done(rule["blk_001"]), llm=None)
    with pytest.raises(AggregateError):
        aggregate_block_checks(block_key="blk_004", check_item_ids=ids, active_checks=("rule", "llm"), rule=_done(rule["blk_004"]),
                               llm=_done(llm["blk_001"]))
    with pytest.raises(AggregateError):
        aggregate_block_checks(block_key="blk_003", check_item_ids=ids, active_checks=("rule", "llm"),
                               rule=CheckInput("none"), llm=CheckInput("failed", FP, FP, failed_scope=scope))
