"""③-1 · ③-1' 개발용 잠정 타입의 불변식(설계 v1 2.5 · 3.4절). BE 계약 검증이 아니다."""
import pytest
from pydantic import ValidationError

from pipeline.types import (
    ConflictGroup,
    ContentFinding,
    ContentFindings,
    JudgeChecked,
    JudgeContext,
    JudgeResult,
    Match,
    PolicyApplied,
    PolicyResult,
    Span,
    VerdictDraft,
    finding_key,
    match_key,
    verdict_key,
)


def _checked(**over):
    base = {"dictionary_version": {"regulation": "regulation@2026-09-28.0", "local": "local@2026-09-28.0", "rules": "policy@2026-09-28.0"},
            "dictionary_fingerprint": {"regulation.json": "a" * 64}, "match_rules_version": "match@2026-09-28.1", "items": ["LC-91"], "llm_called": False}
    base.update(over)
    return JudgeChecked(**base)


def test_content_finding_contract_keys_only():
    f = ContentFinding(finding_key="f_01", content_type="LC-91", status="present", evidence_block_ids=["blk_001"], evidence_source="text", reason="r")
    assert set(f.model_dump()) == {"finding_key", "content_type", "status", "evidence_block_ids", "evidence_source", "reason"}  # [계약 4.1]
    with pytest.raises(ValidationError):
        ContentFinding(finding_key="f", content_type="x", status="maybe", evidence_source="text", reason="r")
    with pytest.raises(ValidationError, match="중복"):
        ContentFinding(finding_key="f", content_type="x", status="present", evidence_block_ids=["b", "b"], evidence_source="text", reason="r")
    with pytest.raises(ValidationError):
        ContentFinding(finding_key="f", content_type="x", status="present", evidence_source="text", reason="r", detected_by="rule")  # 계약 밖 키


def test_judge_result_failed_means_null_findings():
    ok = JudgeResult(section_key="s", status="ok", content_findings=ContentFindings(), checked=_checked())
    assert ok.content_findings.findings == [] and ok.matches == []
    JudgeResult(section_key="s", status="skipped", content_findings=ContentFindings(), checked=_checked())
    failed = JudgeResult(section_key="s", status="failed", content_findings=None, checked=_checked(), error="timeout")
    assert failed.content_findings is None
    with pytest.raises(ValidationError, match="None"):
        JudgeResult(section_key="s", status="failed", content_findings=ContentFindings(), checked=_checked())
    with pytest.raises(ValidationError, match="있어야"):
        JudgeResult(section_key="s", status="ok", content_findings=None, checked=_checked())


def test_match_span_and_keys():
    m = Match(match_key=match_key(1), finding_key=finding_key(2), dictionary_ref="RG-902", pattern="가짜방수", block_key="blk_001",
              raw_span=Span(start=3, end=7), matched_text="가짜방수")
    assert (m.match_key, m.finding_key, verdict_key(3)) == ("m_001", "f_02", "v_03")
    with pytest.raises(ValidationError, match="end"):
        Span(start=5, end=5)
    with pytest.raises(ValidationError):
        Match(match_key="m", finding_key="f", dictionary_ref="x", pattern="p", block_key="b", raw_span=Span(start=0, end=1), matched_text="")


def test_judge_context_distinguishes_missing_from_unknown():
    assert JudgeContext().regulatory_class is None
    assert JudgeContext(regulatory_class="unknown").regulatory_class == "unknown"
    with pytest.raises(ValidationError):
        JudgeContext(regulatory_class="cosmetics")


def _verdict(**over):
    base = {"verdict_key": "v_01", "finding_key": "f_01", "dictionary_ref": "RG-902", "verdict_status": "regulated",
            "source_verdict_status": "regulated", "finding_status": "present", "problem_text": "가짜방수", "reason": "사전 사유"}
    base.update(over)
    return VerdictDraft(**base)


def _applied():
    return PolicyApplied(regulatory_class="combination", applied_classes=["otc", "common"], dict_coverage="partial_class_combination",
                         dictionary_version={"regulation": "r", "local": "l", "rules": "p"}, dictionary_fingerprint={}, rules_version="p")


def test_policy_result_incomplete_has_no_recommendation_or_verdicts():
    ok = PolicyResult(section_key="s", status="ok", bucket_recommendation="exclude", verdicts=[_verdict()], applied=_applied())
    assert ok.verdicts[0].conflict_group is None
    inc = PolicyResult(section_key="s", status="incomplete", bucket_recommendation=None, applied=None, error="judge failed")
    assert inc.verdicts == []
    PolicyResult(section_key="s", status="input_error", bucket_recommendation=None, applied=None, error="regulatory_class 누락")
    with pytest.raises(ValidationError, match="None"):
        PolicyResult(section_key="s", status="incomplete", bucket_recommendation="include", applied=None)
    with pytest.raises(ValidationError, match="verdicts"):
        PolicyResult(section_key="s", status="input_error", bucket_recommendation=None, verdicts=[_verdict()], applied=None)
    with pytest.raises(ValidationError, match="있어야"):
        PolicyResult(section_key="s", status="ok", bucket_recommendation=None, applied=_applied())


def test_verdict_keeps_uncertainty_and_conflict_out_of_reason():
    v = _verdict(finding_status="uncertain", conflict_group="c_01", problem_text=None)
    assert v.reason == "사전 사유" and v.finding_status == "uncertain" and v.conflict_group == "c_01"
    with pytest.raises(ValidationError):
        _verdict(finding_status="absent")  # absent는 verdict를 만들지 않는다
    with pytest.raises(ValidationError):
        ConflictGroup(group_key="c", dictionary_refs=["RG-013"], match_keys=["m_001", "m_002"], block_key="b")  # 집합은 2개 이상
    with pytest.raises(ValidationError):
        PolicyApplied(regulatory_class="otc", applied_classes=["otc"], dict_coverage="full", dictionary_version={}, dictionary_fingerprint={}, rules_version="p")
