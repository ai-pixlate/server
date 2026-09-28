"""③-1a 검출 — 정규화 · 원문 오프셋 복원 · 부분 문자열 · 블록 경계 · 검출 전용 출력 경계(설계 v1 2.2 · 2.5절, #60 D1).

합성 사전(pipeline/data/dict/synthetic)과 손으로 만든 블록만 쓴다. 원료 · 연구원 매칭은 의도된 후보 생성이지 판정 성공이 아니다.
"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline import config as cfgmod
from pipeline import run as runmod
from pipeline.dictionary import JudgeDictView, JudgeItem, load_dictionaries
from pipeline.stages import judge, policy
from pipeline.types import (
    BBox,
    DetectionResult,
    JudgeChecked,
    JudgeContext,
    Line,
    Match,
    MergeResult,
    OcrRegion,
    Section,
    Span,
    TextBlock,
)

SYNTH = "pipeline/data/dict/synthetic"


@pytest.fixture
def cfg():
    return cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH}"])


def _block(key: str, text: str, order: int, section="sec_1_01") -> TextBlock:
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text or " ", score=0.9, poly=[[0, 0], [10, 0], [10, 10], [0, 10]], bbox=BBox(x=0, y=0, w=10, h=10))
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=BBox(x=0, y=0, w=10, h=10), regions=[reg])],
                     bbox=BBox(x=0, y=0, w=10, h=10), role="body", ocr_confidence=0.9)


def _section(key="sec_1_01") -> Section:
    return Section(section_key=key, source_image_id=1, section_order=1, top_offset=0, height=100, width=100, image_path="x.png")


def _view(*items: JudgeItem) -> JudgeDictView:
    return JudgeDictView(tuple(items), {"regulation": "r@0", "local": "l@0", "rules": "p@0"}, {"regulation.json": "0" * 64})


def _item(item_id: str, *patterns: str, dict_type="local") -> JudgeItem:
    return JudgeItem(item_id, dict_type, item_id, tuple(patterns), "e" if dict_type == "local" else None, "k" if dict_type == "local" else None)


def _texts(matches: list[Match]) -> list[str]:
    return [m.matched_text for m in matches]


# ---------------------------------------------------------------------------
# 정규화와 오프셋
# ---------------------------------------------------------------------------
def test_normalize_drops_whitespace_and_maps_offsets():
    norm, off = judge.normalize_with_offsets("10,000 원\n상당")
    assert norm == "10,000원상당"
    assert off == [0, 1, 2, 3, 4, 5, 7, 9, 10]  # 공백(6) · 줄바꿈(8)은 대응표에서 빠진다


def test_normalize_nfkc_changes_char_count_and_lowercases():
    norm, off = judge.normalize_with_offsets("50㎖ ＧＩＦＴ")
    assert norm == "50mlgift"  # ㎖ → ml(1→2), 전각 → 반각 소문자
    assert off == [0, 1, 2, 2, 4, 5, 6, 7]
    assert judge.raw_span_of(off, 2, 2) == Span(start=2, end=3)  # "ml" ← 원문 ㎖ 한 글자
    assert judge.raw_span_of(off, 4, 4) == Span(start=4, end=8)


def test_normalize_pattern_rejects_empty():
    for p in ("", " ", "\n\t"):
        with pytest.raises(ValueError, match="빈 패턴"):
            judge.normalize_pattern(p)
    with pytest.raises(ValueError):
        judge.find_all("abc", "")


def test_find_all_returns_overlapping_and_repeated():
    assert judge.find_all("aaaa", "aa") == [0, 1, 2]
    assert judge.find_all("원원원", "원") == [0, 1, 2]
    assert judge.find_all("abc", "d") == []


# ---------------------------------------------------------------------------
# 2.2절 사례표 — 부분 문자열(실험 기준)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("10,000원", ["원"]),
        ("3만원", ["만원", "원"]),  # 만원 · 원 두 패턴이 각각 매칭(후보 2개)
        ("5천원", ["원"]),
        ("원료", ["원"]),  # 오탐 후보 — 판정이 아니다
        ("연구원", ["원"]),
        ("인원", ["원"]),
        ("원하시는", ["원"]),
    ],
)
def test_price_examples_all_produce_candidates(cfg, text, expected):
    view = _view(_item("LC-93", "원", "만원"))
    ms = judge.detect("sec_1_01", [_block("blk_001", text, 1)], view, cfg)
    assert sorted(m.pattern for m in ms) == sorted(expected)
    for m in ms:
        assert m.finding_key is None and m.matched_text == text[m.raw_span.start:m.raw_span.end]


def test_span_restores_original_text_across_whitespace_and_newline(cfg):
    view = _view(_item("LC-92", "가짜 세트"))
    text = "이번 가짜\n세트 구성"
    ms = judge.detect("sec_1_01", [_block("blk_001", text, 1)], view, cfg)
    assert len(ms) == 1
    m = ms[0]
    assert m.raw_span == Span(start=3, end=8) and m.matched_text == "가짜\n세트"  # 원문 줄바꿈이 구간 안에 남는다
    assert text[m.raw_span.start:m.raw_span.end] == m.matched_text


def test_span_when_nfkc_changes_length(cfg):
    view = _view(_item("RG-9", "50ml", dict_type="regulatory"))
    text = "용량 50㎖ 입니다"
    ms = judge.detect("sec_1_01", [_block("blk_001", text, 1)], view, cfg)
    assert [m.matched_text for m in ms] == ["50㎖"]
    assert ms[0].raw_span == Span(start=3, end=6)


def test_repeated_and_overlapping_matches_are_all_kept(cfg):
    view = _view(_item("LC-93", "원", "원원"))
    ms = judge.detect("sec_1_01", [_block("blk_001", "원원원", 1)], view, cfg)
    assert [(m.pattern, m.raw_span.start, m.raw_span.end) for m in ms] == [
        ("원", 0, 1), ("원", 1, 2), ("원", 2, 3), ("원원", 0, 2), ("원원", 1, 3),
    ]
    assert [m.match_key for m in ms] == ["m_001", "m_002", "m_003", "m_004", "m_005"]


def test_no_match_across_block_boundary(cfg):
    view = _view(_item("LC-92", "가짜기획"))
    blocks = [_block("blk_001", "가짜", 1), _block("blk_002", "기획", 2)]
    assert judge.detect("sec_1_01", blocks, view, cfg) == []
    assert len(judge.detect("sec_1_01", [_block("blk_001", "가짜기획", 1)], view, cfg)) == 1


def test_detect_reads_all_dictionary_rows_including_allowed(cfg):
    dicts = load_dictionaries(SYNTH)
    view = dicts.judge_view()
    text = "가짜 피부용 · 가짜방수 · 가짜센터 0000-1234"
    ms = judge.detect("sec_1_01", [_block("blk_001", text, 1)], view, cfg)
    refs = {m.dictionary_ref for m in ms}
    assert {"RG-905", "RG-902", "LC-91"} <= refs  # allowed(RG-905) · otc(RG-902) · local 모두 후보
    assert not any(hasattr(m, "verdict_status") for m in ms)


def test_detect_order_is_item_pattern_block_position(cfg):
    view = _view(_item("LC-92", "가짜기획", "가짜 세트"), _item("LC-93", "원"))
    blocks = [_block("blk_002", "가짜세트 2원", 2), _block("blk_001", "가짜기획 1원", 1)]
    ms = judge.detect("sec_1_01", blocks, view, cfg)
    assert [(m.dictionary_ref, m.pattern, m.block_key) for m in ms] == [
        ("LC-92", "가짜기획", "blk_001"), ("LC-92", "가짜 세트", "blk_002"), ("LC-93", "원", "blk_001"), ("LC-93", "원", "blk_002"),
    ]


def test_eojeol_prefix_mode_is_not_implemented():
    cfg = cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH}", "judge.match_mode='eojeol_prefix'"])
    with pytest.raises(NotImplementedError, match="eojeol_prefix"):
        judge.detect("sec_1_01", [_block("blk_001", "원", 1)], _view(_item("LC-93", "원")), cfg)


# ---------------------------------------------------------------------------
# 검출 전용 출력의 경계
# ---------------------------------------------------------------------------
def test_detect_only_result_is_not_a_judgement(cfg, tmp_path):
    records = []
    res = judge.detect_only("sec_1_01", [_block("blk_001", "가짜센터로 문의", 1)], cfg, recorder=records.append)
    assert isinstance(res, DetectionResult) and res.status == "detect_only"
    assert not hasattr(res, "content_findings")
    assert res.candidates == {"LC-91": ["m_001"]}
    assert res.checked.llm_called is False and res.checked.match_rules_version == judge.MATCH_RULES_VERSION
    assert set(res.checked.items) == {"LC-91", "LC-92", "LC-93", "RG-901", "RG-902", "RG-903", "RG-904", "RG-905", "RG-906"}
    assert res.checked.dictionary_version["local"] == "local@2026-09-28.0"
    rec = records[0]
    assert rec["status"] == "detect_only" and rec["matches"][0]["dictionary_ref"] == "LC-91"
    assert rec["dictionary_fingerprint"] and rec["match_mode"] == "substring" and "normalization" in rec
    with pytest.raises(ValidationError, match="묶이지"):
        DetectionResult(section_key="s", matches=[Match(**{**res.matches[0].model_dump(), "finding_key": "f_01"})], checked=res.checked)
    with pytest.raises(ValidationError, match="LLM"):
        DetectionResult(section_key="s", checked=JudgeChecked(**{**res.checked.model_dump(), "llm_called": True}))


def test_detect_only_rejects_blocks_from_other_section(cfg):
    with pytest.raises(ValueError, match="section_key"):
        judge.detect_only("sec_1_01", [_block("blk_001", "x", 1, section="sec_9_09")], cfg)


def test_policy_refuses_detection_result(cfg):
    res = judge.detect_only("sec_1_01", [_block("blk_001", "가짜방수", 1)], cfg)
    with pytest.raises(policy.PolicyInputError, match="검출 전용"):
        policy.run(res, [], JudgeContext(regulatory_class="otc"), cfg, dicts=load_dictionaries(SYNTH))


def test_judge_result_requires_matches_bound_to_findings():
    from pipeline.types import ContentFinding, ContentFindings, JudgeResult

    checked = JudgeChecked(dictionary_version={}, dictionary_fingerprint={}, match_rules_version="m", items=[], llm_called=True)
    m = Match(match_key="m_001", finding_key=None, dictionary_ref="RG-902", pattern="p", block_key="b", raw_span=Span(start=0, end=1), matched_text="x")
    with pytest.raises(ValidationError, match="묶여야"):
        JudgeResult(section_key="s", status="ok", content_findings=ContentFindings(), matches=[m], checked=checked)
    f = ContentFinding(finding_key="f_01", content_type="RG-902", status="present", evidence_block_ids=["b"], evidence_source="text", reason="r")
    JudgeResult(section_key="s", status="ok", content_findings=ContentFindings(findings=[f]), matches=[Match(**{**m.model_dump(), "finding_key": "f_01"})], checked=checked)


def test_run_still_not_implemented(cfg):
    with pytest.raises(NotImplementedError, match="detect_only"):
        judge.run(_section(), [_block("blk_001", "x", 1)], JudgeContext(), cfg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _write_merge(tmp_path: Path, text: str) -> Path:
    mr = MergeResult(section_key="sec_1_01", blocks=[_block("blk_001", text, 1)])
    p = tmp_path / "merge" / "sec_1_01.json"
    p.parent.mkdir(parents=True)
    p.write_text(mr.model_dump_json(indent=2), encoding="utf-8")
    return p


def test_cli_judge_no_llm_writes_detect_only_outputs(tmp_path, capsys):
    mp = _write_merge(tmp_path, "가짜기획 세트 10,000원")
    out = tmp_path / "out"
    code = runmod.main(["judge", "--merge", str(mp), "--out", str(out), "--no-llm", "--set", f"judge.dict_dir={SYNTH}"])
    assert code == 0
    res = json.loads((out / "judge_detect" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert res["status"] == "detect_only" and "content_findings" not in res
    assert set(res["candidates"]) == {"LC-92", "LC-93"}
    dbg = json.loads((out / "judge_debug" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert dbg["status"] == "detect_only" and dbg["match_rules_version"] == judge.MATCH_RULES_VERSION
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run["stage"] == "judge" and run["mode"] == "detect_only" and run["use_llm"] is False
    assert run["dictionary_version"]["rules"] == "policy@2026-09-28.0" and run["dictionary_fingerprint"]
    assert "판정 결과 아님" in capsys.readouterr().out


def test_cli_judge_without_no_llm_is_not_implemented(tmp_path, capsys):
    mp = _write_merge(tmp_path, "x")
    code = runmod.main(["judge", "--merge", str(mp), "--out", str(tmp_path / "out"), "--set", f"judge.dict_dir={SYNTH}"])
    assert code == 3
    assert "미구현" in capsys.readouterr().err
