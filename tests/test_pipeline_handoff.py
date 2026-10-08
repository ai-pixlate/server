"""운영 인계 어댑터 계약 테스트 — 합성 입력 · 가짜 모델(실제 모델 · GPU · BE 연결 아님)."""
from __future__ import annotations

import copy
import json
import math
import threading
from pathlib import Path

import pytest

from pipeline.handoff import analysis, downstream, examples as ex, judgment, translation
from pipeline.handoff.bundle import BundleError, bundle_content_sha256, check_bundle
from pipeline.handoff.canonical import CanonicalError, canonical_json, sha256_canonical
from pipeline.handoff.envelope import StageReport
from pipeline.handoff.validate import validate_report
from pipeline.vlm import LlmReply, VlmError

REPO = Path(__file__).resolve().parents[1]
COMMITTED = REPO / "pipeline" / "samples" / "handoff"


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    work = tmp_path_factory.mktemp("work")
    out = tmp_path_factory.mktemp("examples")
    results = ex.generate(out, work_root=work)
    return out, results, work


# ---------------------------------------------------------------------------
# JCS
# ---------------------------------------------------------------------------
def test_jcs_numbers_and_key_order():
    assert canonical_json({"b": [1, 0.98, 1e-7, 1e21, 123.0, -0.5, 0.000001], "a": None}) == \
        '{"a":null,"b":[1,0.98,1e-7,1e+21,123,-0.5,0.000001]}'
    assert canonical_json({"é": 1, "z": 2, "\U0001F600": 3, "｡": 4}) == '{"z":2,"é":1,"😀":3,"｡":4}'  # UTF-16 코드 단위 순
    assert canonical_json("한\n") == '"한\\n"'  # 문자열은 정규화하지 않는다


@pytest.mark.parametrize("bad", [math.nan, math.inf, {1: "x"}, {"a": object()}])
def test_jcs_rejects(bad):
    with pytest.raises(CanonicalError):
        canonical_json(bad)


# ---------------------------------------------------------------------------
# 봉투 불변식
# ---------------------------------------------------------------------------
def _base(**kw):
    m = {"stage": "label"}
    d = dict(execution_id="e", attempt_id="a", stage="label", input_manifest=m, input_manifest_sha256=sha256_canonical(m),
             outcome="completed", target_count=1, payload={})
    d.update(kw)
    return d


@pytest.mark.parametrize("kw", [
    {"outcome": "failed"},  # failure 없음
    {"outcome": "skipped", "target_count": 0},  # skip_reason 없음
    {"outcome": "skipped", "skip_reason": "x", "target_count": 2},
    {"payload": None},
    {"input_manifest_sha256": "0" * 64},
    {"outcome": "failed", "failure": {"kind": "internal", "retryable": False, "message": "m"}, "skip_reason": "x"},
])
def test_report_invariants(kw):
    with pytest.raises(ValueError):
        StageReport(**_base(**kw))


# ---------------------------------------------------------------------------
# 예제 — 생성 · 검증 · 커밋본 재현
# ---------------------------------------------------------------------------
def test_every_example_passes_be_validation(generated):
    _, results, _ = generated
    assert results and all(v == [] for v in results.values()), {k: v for k, v in results.items() if v}


def test_committed_examples_match_regeneration(generated):
    out, _, _ = generated
    got = sorted(p.name for p in out.glob("*.json"))
    want = sorted(p.name for p in COMMITTED.glob("*.json"))
    assert got == want
    for name in got:
        a = json.loads((out / name).read_text(encoding="utf-8"))
        b = json.loads((COMMITTED / name).read_text(encoding="utf-8"))
        assert a == b, name


def _doc(generated, name):
    out, _, work = generated
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name, outcome", [
    ("analyze.completed", "completed"), ("analyze.completed_split_fallback", "completed"), ("analyze.failed_input_hash", "failed"),
    ("judge.completed", "completed"), ("judge.completed_local_failed", "completed"), ("judge.failed_bundle_hash", "failed"),
    ("label.skipped_no_blocks", "skipped"), ("label.failed_model", "failed"), ("logo.skipped_all_label", "skipped"),
    ("inpaint.completed", "completed"), ("inpaint.skipped_empty_mask", "skipped"), ("inpaint.failed_model", "failed"),
    ("style.skipped_no_targets", "skipped"), ("translate.failed_partial", "failed"), ("translate.completed_retry_target_only", "completed"),
    ("translate.skipped_no_targets", "skipped"),
])
def test_example_outcomes(generated, name, outcome):
    assert _doc(generated, name)["report"]["outcome"] == outcome


def test_split_fallback_is_whole_image_and_recorded(generated):
    p = _doc(generated, "analyze.completed_split_fallback")["report"]["payload"]
    secs = p["analyze_result"]["sections"]
    assert len(secs) == 1 and secs[0]["top_offset"] == 0 and secs[0]["height"] == 800
    assert p["split_fallbacks"][0]["kind"] == "section_split_whole_image"
    assert p["analyze_result"]["blocks"], "대체 섹션도 OCR · 병합을 수행한다"


def test_judge_findings_one_per_item_and_regulatory_once(generated):
    s = _doc(generated, "judge.completed")["report"]["payload"]["sections"]
    for sec in s:
        types = [f["content_type"] for f in sec["content_findings"]["findings"]]
        assert len(types) == len(set(types)) and sum(t.startswith("local_") for t in types) == 8
    # 감싸기: 허용 표현이 금지 표현을 완전히 감싸면 판정 행이 없다 — 원시 근거는 보존
    sec2 = s[1]
    assert sec2["verdicts"] == [] and sec2["bucket_recommendation"] == "include"
    assert sec2["audit"]["suppressed"][0]["rule"] == "allowed_wrap" and len(sec2["audit"]["matches"]) == 2


def test_judge_local_failure_excludes_but_report_completes(generated):
    p = _doc(generated, "judge.completed_local_failed")["report"]["payload"]
    for sec in p["sections"]:
        assert sec["local_status"] == "failed" and sec["bucket_recommendation"] == "exclude"
        assert "local_judgment_failed" in sec["recommendation_basis"]
        assert not any(f["content_type"].startswith("local_") for f in sec["content_findings"]["findings"])  # uncertain · absent로 바꾸지 않음


def test_judge_local_not_checked_does_not_exclude(generated):
    p = _doc(generated, "judge.completed_local_not_checked")["report"]["payload"]
    sec2 = p["sections"][1]
    assert sec2["local_status"] == "not_checked" and sec2["bucket_recommendation"] == "include"


def test_translate_retry_returns_only_targets(generated):
    p = _doc(generated, "translate.completed_retry_target_only")["report"]["payload"]
    assert [b["block_key"] for b in p["blocks"]] == ["blk_003"]
    assert [x["block_key"] for x in p["preserved"]] == ["blk_001", "blk_002"]


def test_translate_partial_keeps_successes(generated):
    r = _doc(generated, "translate.failed_partial")["report"]
    by = {b["block_key"]: b for b in r["payload"]["blocks"]}
    assert by["blk_003"]["outcome"] == "failed" and by["blk_003"]["trans_1"] is None
    assert by["blk_001"]["outcome"] == "completed" and by["blk_001"]["trans_1"]
    assert r["failure"]["targets"] == ["blk_003"]


def test_inpaint_skip_references_original_background(generated):
    r = _doc(generated, "inpaint.skipped_empty_mask")["report"]
    assert r["source_refs"][0]["ref"] == "section_image"
    assert {a["kind"] for a in r["artifacts"]} == {"delete_mask", "protect_mask"}


def test_style_has_no_role_defaults(generated):
    p = _doc(generated, "style.completed")["report"]["payload"]
    for b in p["blocks"]:
        for k, v in b["null_reasons"].items():
            assert b[k] is None and v


# ---------------------------------------------------------------------------
# 검증기 — 변조 거절
# ---------------------------------------------------------------------------
def _rerun(generated, name):
    """예제 요청을 실제 경로로 다시 실행(커밋본은 경로를 바꿨으므로 생성 폴더의 사례를 다시 만든다)."""
    _, _, work = generated
    for n, req, fn in ex.cases(Path(work).resolve() / "rerun" / name.replace(".", "_")):
        if n == name:
            return req, fn(copy.deepcopy(req))
    raise KeyError(name)


def test_validator_rejects_tampering(generated):
    req, rep = _rerun(generated, "translate.completed")
    d = rep.model_dump(mode="json")
    d2 = copy.deepcopy(d)
    d2["payload"]["blocks"].pop()
    assert validate_report(d2, req)
    d3 = copy.deepcopy(d)
    d3["attempt_id"] = "other"
    assert validate_report(d3, req)
    req2, rep2 = _rerun(generated, "judge.completed")
    d4 = rep2.model_dump(mode="json")
    d4["payload"]["sections"][0]["audit"]["matches"][0]["matched_text"] = "변조"
    assert validate_report(d4, req2)
    d5 = rep2.model_dump(mode="json")
    d5["payload"]["sections"][0]["content_findings"]["findings"][0]["content_type"] = "before_after"
    assert validate_report(d5, req2)


def test_validator_remeasures_artifacts(generated):
    req, rep = _rerun(generated, "inpaint.completed")
    Path(rep.artifacts[0].path).write_bytes(b"not a png")
    assert any("해시" in p for p in validate_report(rep, req))


# ---------------------------------------------------------------------------
# 단계별 경계
# ---------------------------------------------------------------------------
@pytest.fixture()
def cfg():
    return ex.config()


def test_expected_manifest_mismatch_is_input_invalid(generated, cfg):
    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "m1") if n == "label.completed")
    req["expected_input_manifest_sha256"] = "0" * 64
    rep = downstream.run_label(req, cfg, llm=ex.FakeLabel())
    assert rep.outcome == "failed" and rep.failure.kind == "input_invalid" and "지문" in rep.failure.message


def test_judge_rejects_matched_scope(generated, cfg):
    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "j1") if n == "judge.completed")
    cfg["judge"]["call_scope"] = "matched"
    rep = judgment.run_judgment(req, cfg, llm=ex.FakeJudge())
    assert rep.failure.kind == "config_invalid"


def test_bundle_rejects_overrides_and_same_text_conflict():
    b = ex.bundle()
    b["rules"]["overrides"] = [{"keep": "RG-901", "drop": ["RG-902"], "basis": "x"}]
    b["bundle_sha256"] = bundle_content_sha256(b)
    with pytest.raises(BundleError, match="overrides"):
        check_bundle(b, target_country="US")
    b = ex.bundle()
    b["regulation"]["entries"][3]["variant_ko"] = ["가짜 치료"]  # allowed가 금지 항목과 정규화 결과가 같다
    b["bundle_sha256"] = bundle_content_sha256(b)
    with pytest.raises(BundleError, match="금지/허용"):
        check_bundle(b, target_country="US")
    b = ex.bundle()
    b["local"]["entries"].pop()
    b["bundle_sha256"] = bundle_content_sha256(b)
    with pytest.raises(BundleError, match="대응표"):
        check_bundle(b, target_country="US")


def test_judge_otc_uses_otc_rows(generated, cfg):
    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "j2") if n == "judge.completed")
    req["regulatory_class"] = "otc"
    rep = judgment.run_judgment(req, cfg, llm=ex.FakeJudge())
    ins = rep.payload["sections"][0]["audit"]["inspection"]["regulatory"]
    assert "RG-901" not in ins["entries_checked"] and "RG-906" in ins["entries_checked"]


def test_label_response_missing_block_fails_section(generated, cfg):
    class Missing(ex.FakeLabel):
        def __call__(self, prompt, payload, image):
            p = json.loads(payload)
            return LlmReply(text=json.dumps({"labels": [{"id": p["blocks"][0]["id"], "is_product_label": False}]}), usage=None)

    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "l1") if n == "label.completed")
    rep = downstream.run_label(req, cfg, llm=Missing())
    assert rep.outcome == "failed" and rep.failure.kind == "response_invalid"


def test_downstream_rejects_unadopted_payload(generated, cfg):
    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "s1") if n == "style.completed")
    req["logo_record"] = {**req["logo_record"], "status": "edited"}
    rep = downstream.run_style(req, cfg)
    assert rep.failure.kind == "input_invalid" and "logo_record" in rep.failure.message


def test_inpaint_cancel_before_model(generated, cfg):
    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "i1") if n == "inpaint.completed")
    ev = threading.Event()
    ev.set()
    rep = downstream.run_inpaint(req, cfg, model=ex.FakeInpaintModel(), cancel=ev)
    assert rep.outcome == "failed" and rep.failure.kind == "cancelled"


def test_inpaint_limits_recorded_as_dev_default(generated, cfg):
    rep = _rerun(generated, "inpaint.completed")[1]
    assert rep.implementation["limits"]["source"] == "dev_config" and rep.implementation["limits"]["operational"] is False


def test_translate_structure_failure_fails_all_targets(generated, cfg):
    class Dup(ex.FakeTranslate):
        def __call__(self, prompt, payload, image):
            r = super().__call__(prompt, payload, image)
            d = json.loads(r.text)
            d["blocks"].append(d["blocks"][0])
            return LlmReply(text=json.dumps(d), usage=None)

    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "t1") if n == "translate.completed")
    rep = translation.run_translate(req, cfg, llm=Dup())
    assert rep.outcome == "failed" and rep.failure.kind == "response_invalid"
    assert all(b["outcome"] == "failed" and b["trans_1"] is None for b in rep.payload["blocks"])


def test_translate_placeholder_leak_fails_block(generated, cfg):
    class Leak(ex.FakeTranslate):
        def __call__(self, prompt, payload, image):
            r = super().__call__(prompt, payload, image)
            d = json.loads(r.text)
            d["blocks"][0]["translation"] += " [value]"
            return LlmReply(text=json.dumps(d), usage=None)

    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "t2") if n == "translate.completed")
    rep = translation.run_translate(req, cfg, llm=Leak())
    first = rep.payload["blocks"][0]
    assert first["outcome"] == "failed" and "자리표시자" in first["error"]


def test_translate_rejects_overlapping_preserved(generated, cfg):
    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "t3") if n == "translate.completed")
    req["preserved"] = [{"block_key": "blk_001", "revision": 1, "translation_sha256": "a" * 64, "source_attempt_id": "a-0"}]
    rep = translation.run_translate(req, cfg, llm=ex.FakeTranslate())
    assert rep.failure.kind == "input_invalid" and "겹친다" in rep.failure.message


def test_text_check_alternative_term_is_unverifiable(generated):
    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "c1") if n == "text_check.completed_with_flags")
    req["glossary"]["terms"][0]["term_target"] = "cream; creme"
    rep = translation.run_text_check(req)
    b = rep.payload["blocks"][0]
    assert b["terms"][0]["status"] == "unverifiable" and b["complete"] is False
    assert not any(f["type"] == "mandatory_term_unapplied" for f in b["flags"])


def test_analyze_llm_failure_is_not_heuristic_fallback(generated, cfg, monkeypatch):
    class Boom:
        config = {"model": "fake", "temperature": 0, "timeout_s": 1}

        def __call__(self, prompt, payload):
            raise VlmError("합성 실패")

    _, _, work = generated
    req = next(r for n, r, _ in ex.cases(Path(work).resolve() / "a1") if n == "analyze.completed")
    req["use_llm"] = True
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-used")
    rep = analysis.run_analyze(req, cfg, llm=Boom(), ocr_engine=ex.FakeOcr([["가짜치료 크림", "가격"], ["가짜완화"]]))
    assert rep.outcome == "failed" and rep.failure.kind == "model_call_failed"
