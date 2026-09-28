"""③-1' 정책 적용 — 적용 행 선택(D9) · 예외 쌍(D2) · 충돌 표시(D2-b) · 매핑(D10 · D11 · D12) · 집계 · 미완료(D7) · problem_text(D13).

합성 사전(pipeline/data/dict/synthetic)만 쓴다. RG-901 cosmetic 교체형 · RG-902 otc 금지형 · RG-903 otc 조건형 · RG-904 cosmetic 완충형 ·
RG-905 cosmetic allowed · RG-906 common 금지형 / LC-91 irrelevant · LC-92 needs_fix · LC-93 irrelevant / overrides: keep RG-903 drop RG-902.
"""
import json
from pathlib import Path

import pytest

from pipeline import config as cfgmod
from pipeline import run as runmod
from pipeline.dictionary import load_dictionaries
from pipeline.stages import policy
from pipeline.types import (
    BBox,
    ContentFinding,
    ContentFindings,
    JudgeChecked,
    JudgeContext,
    JudgeResult,
    Line,
    Match,
    MergeResult,
    OcrRegion,
    Span,
    TextBlock,
    blocks_fingerprint,
)

SYNTH = "pipeline/data/dict/synthetic"


@pytest.fixture(scope="module")
def dicts():
    return load_dictionaries(SYNTH)


@pytest.fixture
def cfg():
    return cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH}"])


def _block(key: str, text: str, order: int, section="sec_1_01") -> TextBlock:
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text or " ", score=0.9, poly=[[0, 0], [10, 0], [10, 10], [0, 10]], bbox=BBox(x=0, y=0, w=10, h=10))
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=BBox(x=0, y=0, w=10, h=10), regions=[reg])],
                     bbox=BBox(x=0, y=0, w=10, h=10), role="body", ocr_confidence=0.9)


BLOCKS = [_block("blk_001", "가짜센터 문의 0000-1234", 1), _block("blk_002", "가짜 방수 시험 통과", 2), _block("blk_003", "가짜치료 효과", 3)]
CHECKED = JudgeChecked(dictionary_version={"regulation": "r", "local": "l", "rules": "p"}, dictionary_fingerprint={}, match_rules_version="m", items=[], llm_called=True,
                       input_fingerprint=blocks_fingerprint(BLOCKS))


def _f(n, ref, status="present", blocks=("blk_001",), src="text", reason="r"):
    return ContentFinding(finding_key=f"f_{n:02d}", content_type=ref, status=status, evidence_block_ids=list(blocks), evidence_source=src, reason=reason)


def _m(n, fk, ref, block, start, end, text, pattern=None):
    return Match(match_key=f"m_{n:03d}", finding_key=fk, dictionary_ref=ref, pattern=pattern or text, block_key=block, raw_span=Span(start=start, end=end), matched_text=text)


def _judge(findings, matches=(), status="ok", section="sec_1_01"):
    return JudgeResult(section_key=section, status=status, content_findings=ContentFindings(findings=findings), matches=list(matches), checked=CHECKED)


def _run(jr, cls, dicts, cfg, blocks=BLOCKS):
    return policy.run(jr, blocks, JudgeContext(regulatory_class=cls), cfg, dicts=dicts)


# ---------------------------------------------------------------------------
# 매핑(D10 · D11 · D12)
# ---------------------------------------------------------------------------
def test_mapping_rows_and_aggregation(dicts, cfg):
    findings = [
        _f(1, "RG-901", blocks=("blk_003",)),  # 교체형 → 포함
        _f(2, "RG-904", blocks=("blk_003",)),  # 완충형 → regulated 정규화 · 포함
        _f(3, "RG-905", blocks=("blk_003",)),  # allowed → verdict 없음
        _f(4, "LC-92", status="absent", blocks=("blk_001",)),  # absent → 없음
    ]
    matches = [_m(1, "f_01", "RG-901", "blk_003", 0, 4, "가짜치료"), _m(2, "f_02", "RG-904", "blk_003", 5, 7, "효과", "가짜완충"),
               _m(3, "f_03", "RG-905", "blk_001", 0, 4, "가짜센터", "가짜허용")]  # 서로 겹치지 않는 구간
    res = _run(_judge(findings, matches), "cosmetic", dicts, cfg)
    assert res.status == "ok" and res.bucket_recommendation == "include"
    v = {x.dictionary_ref: x for x in res.verdicts}
    assert set(v) == {"RG-901", "RG-904"}
    assert v["RG-901"].verdict_status == "regulated" and v["RG-901"].alternative_expression == ["fake-friendly"] and v["RG-901"].problem_text == "가짜치료"
    assert v["RG-904"].verdict_status == "regulated" and v["RG-904"].source_verdict_status == "rewritable"  # D11
    assert v["RG-901"].reason == "합성: 교체형" and v["RG-901"].basis_article == "§ 0.0" and v["RG-901"].evidence_url == "https://example.invalid/e"
    assert all(x.finding_status == "present" and x.conflict_group is None for x in res.verdicts)
    assert res.applied.regulatory_class == "cosmetic" and res.applied.applied_classes == ["cosmetic", "common"]
    assert res.applied.dict_coverage == "selected_class_all_entries" and res.applied.rules_version == "policy@2026-09-28.0"


def test_forbidden_and_local_verdicts_exclude(dicts, cfg):
    findings = [_f(1, "RG-902", blocks=("blk_002",)), _f(2, "RG-903", blocks=("blk_002",)), _f(3, "LC-91", blocks=("blk_001",))]
    matches = [_m(1, "f_01", "RG-902", "blk_002", 0, 5, "가짜 방수"), _m(2, "f_02", "RG-903", "blk_002", 0, 8, "가짜 방수 시험")]
    # 예외 쌍(RG-903 keep → RG-902 drop)이 겹친 매칭을 억제한다 → RG-902는 매칭이 전부 억제돼 verdict 없음
    res = _run(_judge(findings, matches), "otc", dicts, cfg)
    assert [x.dictionary_ref for x in res.verdicts] == ["RG-903", "LC-91"]
    assert res.suppressed == [policy.SuppressedMatch(match_key="m_001", finding_key="f_01", suppressed_by="m_002")]
    assert res.conflicts == []
    lc = res.verdicts[1]
    assert lc.verdict_status == "irrelevant" and lc.reason == "합성 안내문 1" and lc.problem_text == "가짜센터 문의 0000-1234" and lc.alternative_expression == []
    assert res.bucket_recommendation == "exclude"  # LC irrelevant가 제외형


def test_residual_match_keeps_verdict_after_suppression(dicts, cfg):
    findings = [_f(1, "RG-902", blocks=("blk_002", "blk_001")), _f(2, "RG-903", blocks=("blk_002",))]
    matches = [_m(1, "f_01", "RG-902", "blk_002", 0, 5, "가짜 방수"), _m(2, "f_02", "RG-903", "blk_002", 0, 8, "가짜 방수 시험"),
               _m(3, "f_01", "RG-902", "blk_001", 0, 4, "가짜방수")]  # 독립 매칭 — 정상 반영
    res = _run(_judge(findings, matches), "otc", dicts, cfg)
    v = {x.dictionary_ref: x for x in res.verdicts}
    assert set(v) == {"RG-902", "RG-903"} and v["RG-902"].problem_text == "가짜방수"  # 억제되지 않은 첫 매칭
    assert res.bucket_recommendation == "exclude" and v["RG-902"].conflict_group is None


def test_unresolved_overlap_is_conservative_with_conflict_group(dicts, cfg):
    # 예외 목록에 없는 겹침(RG-901 교체형 · RG-906 common 금지형) → 둘 다 계산에 넣고 conflict_group으로 잠정 표시(D2-b)
    findings = [_f(1, "RG-901", blocks=("blk_003",)), _f(2, "RG-906", blocks=("blk_003",))]
    matches = [_m(1, "f_01", "RG-901", "blk_003", 0, 4, "가짜치료"), _m(2, "f_02", "RG-906", "blk_003", 2, 6, "치료 효"), ]
    res = _run(_judge(findings, matches), "cosmetic", dicts, cfg)
    assert len(res.conflicts) == 1
    g = res.conflicts[0]
    assert g.dictionary_refs == ["RG-901", "RG-906"] and g.match_keys == ["m_001", "m_002"] and g.block_key == "blk_003"
    assert all(x.conflict_group == g.group_key for x in res.verdicts)
    assert res.bucket_recommendation == "exclude"  # 금지형이 있으므로 보수적으로 제외 권고
    assert all(x.reason in ("합성: 교체형", "합성: 공통 분류 금지형") for x in res.verdicts)  # reason은 사전 스냅샷 그대로


def test_uncertain_local_is_excluded_with_status_kept(dicts, cfg):
    res = _run(_judge([_f(1, "LC-92", status="uncertain", blocks=("blk_001",))]), "cosmetic", dicts, cfg)
    v = res.verdicts[0]
    assert v.verdict_status == "needs_fix" and v.finding_status == "uncertain" and v.reason == "합성 안내문 2"
    assert res.bucket_recommendation == "exclude"


# ---------------------------------------------------------------------------
# 규제 분류(D9)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls, expected_refs, coverage",
    [
        ("cosmetic", {"RG-901", "RG-906", "LC-91"}, "selected_class_all_entries"),
        ("otc", {"RG-902", "RG-906", "LC-91"}, "selected_class_all_entries"),
        ("combination", {"RG-902", "RG-906", "LC-91"}, "partial_class_combination"),
        ("unknown", {"RG-901", "RG-906", "LC-91"}, "unverified_class"),
    ],
)
def test_regulatory_class_filter_and_coverage(dicts, cfg, cls, expected_refs, coverage):
    findings = [_f(1, "RG-901", blocks=("blk_003",)), _f(2, "RG-902", blocks=("blk_002",)), _f(3, "RG-906", blocks=("blk_003",)), _f(4, "LC-91")]
    matches = [_m(1, "f_01", "RG-901", "blk_003", 0, 4, "가짜치료"), _m(2, "f_02", "RG-902", "blk_002", 0, 5, "가짜 방수"),
               _m(3, "f_03", "RG-906", "blk_003", 5, 7, "효과", "가짜공통")]
    res = _run(_judge(findings, matches), cls, dicts, cfg)
    assert {x.dictionary_ref for x in res.verdicts} == expected_refs
    assert res.applied.regulatory_class == cls and res.applied.dict_coverage == coverage


def test_missing_regulatory_class_is_input_error_not_unknown(dicts, cfg):
    res = _run(_judge([_f(1, "LC-91")]), None, dicts, cfg)
    assert res.status == "input_error" and res.bucket_recommendation is None and res.verdicts == [] and res.applied is None
    assert "unknown" in res.error and "재시도" in res.error
    ok = _run(_judge([_f(1, "LC-91")]), "unknown", dicts, cfg)
    assert ok.status == "ok" and ok.applied.dict_coverage == "unverified_class"


# ---------------------------------------------------------------------------
# 미완료 · 입력 오류 · problem_text(D7 · D13)
# ---------------------------------------------------------------------------
def test_incomplete_judge_yields_no_recommendation(dicts, cfg):
    failed = JudgeResult(section_key="sec_1_01", status="failed", content_findings=None, checked=CHECKED, error="timeout")
    res = _run(failed, "cosmetic", dicts, cfg)
    assert res.status == "incomplete" and res.bucket_recommendation is None and res.verdicts == [] and "timeout" in res.error


def test_skipped_judge_is_processed(dicts, cfg):
    skipped = _judge([_f(1, "RG-906", blocks=("blk_003",))], [_m(1, "f_01", "RG-906", "blk_003", 0, 4, "가짜공통")], status="skipped")
    res = _run(skipped, "cosmetic", dicts, cfg)
    assert res.status == "ok" and res.bucket_recommendation == "exclude"


def test_problem_text_truncation_and_image_only(dicts, cfg):
    c = cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH}", "policy.problem_text_max_chars=8"])
    res = _run(_judge([_f(1, "LC-91", blocks=("blk_001", "blk_003"))]), "cosmetic", dicts, c)
    assert res.verdicts[0].problem_text == "가짜센터 문의…" and len(res.verdicts[0].problem_text) == 8  # 상한 안에서 자르고 … 표시
    img = _run(_judge([_f(1, "LC-93", blocks=(), src="image")]), "cosmetic", dicts, cfg)
    assert img.verdicts[0].problem_text is None and img.verdicts[0].verdict_status == "irrelevant"


def test_input_errors(dicts, cfg):
    with pytest.raises(policy.PolicyInputError, match="근거 블록"):
        _run(_judge([_f(1, "LC-91", blocks=("blk_999",))]), "cosmetic", dicts, cfg)
    with pytest.raises(policy.PolicyInputError, match="사전에 없는"):
        _run(_judge([_f(1, "LC-99")]), "cosmetic", dicts, cfg)
    with pytest.raises(policy.PolicyInputError, match="매칭이 없다"):
        _run(_judge([_f(1, "RG-901", blocks=("blk_003",))]), "cosmetic", dicts, cfg)
    with pytest.raises(policy.PolicyInputError, match="section_key"):
        _run(_judge([_f(1, "LC-91")]), "cosmetic", dicts, cfg, blocks=[_block("blk_001", "x", 1, section="sec_9_09")])


def test_policy_refuses_blocks_changed_since_judgement(dicts, cfg):
    """같은 ID의 텍스트가 바뀐 블록에 이전 finding을 적용하지 않는다(2026-09-29 검토). 순서 · 구성 변경도 같다."""
    jr = _judge([_f(1, "LC-91")])
    changed = [_block("blk_001", "바뀐 텍스트", 1), *BLOCKS[1:]]
    with pytest.raises(policy.PolicyInputError, match="판정 당시 블록과 다르다"):
        _run(jr, "cosmetic", dicts, cfg, blocks=changed)
    with pytest.raises(policy.PolicyInputError, match="판정 당시 블록과 다르다"):
        _run(jr, "cosmetic", dicts, cfg, blocks=BLOCKS[:2])
    assert _run(jr, "cosmetic", dicts, cfg, blocks=list(reversed(BLOCKS))).status == "ok"  # 순서만 다른 목록은 block_order로 정렬되므로 같다


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_policy(tmp_path, dicts, cfg, capsys):
    jr = _judge([_f(1, "LC-91"), _f(2, "RG-902", blocks=("blk_002",))], [_m(1, "f_02", "RG-902", "blk_002", 0, 5, "가짜 방수")])
    jp = tmp_path / "judge" / "sec_1_01.json"
    jp.parent.mkdir()
    jp.write_text(jr.model_dump_json(indent=2), encoding="utf-8")
    mp = tmp_path / "merge" / "sec_1_01.json"
    mp.parent.mkdir()
    mp.write_text(MergeResult(section_key="sec_1_01", blocks=BLOCKS).model_dump_json(indent=2), encoding="utf-8")
    out = tmp_path / "out"
    code = runmod.main(["policy", "--judge", str(jp), "--merge", str(mp), "--regulatory-class", "otc", "--out", str(out), "--set", f"judge.dict_dir={SYNTH}"])
    assert code == 0
    res = json.loads((out / "policy" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert res["status"] == "ok" and res["bucket_recommendation"] == "exclude" and {v["dictionary_ref"] for v in res["verdicts"]} == {"LC-91", "RG-902"}
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run["stage"] == "policy" and run["regulatory_class"] == "otc" and run["rules_version"] == "policy@2026-09-28.0" and run["policy_impl_version"]
    assert "exclude" in capsys.readouterr().out
    # 분류 누락 → input_error 저장 · 종료 코드 2
    code = runmod.main(["policy", "--judge", str(jp), "--merge", str(mp), "--out", str(tmp_path / "out2"), "--set", f"judge.dict_dir={SYNTH}"])
    assert code == 2
    res2 = json.loads((tmp_path / "out2" / "policy" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert res2["status"] == "input_error"
