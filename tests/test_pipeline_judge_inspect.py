"""③-1 오버레이 — 상태별 색 구분 · 이미지 전용 근거는 박스 없이 설명 · 실패 표시 · CLI."""
import json

from PIL import Image

from pipeline import inspect as insp
from pipeline import run as runmod
from pipeline.types import (
    BBox,
    ContentFinding,
    ContentFindings,
    JudgeChecked,
    JudgeResult,
    Line,
    Match,
    MergeResult,
    OcrRegion,
    PolicyApplied,
    PolicyResult,
    Span,
    TextBlock,
    VerdictDraft,
)


def _block(key, text, order, y):
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text, score=0.9, poly=[[10, y], [200, y], [200, y + 30], [10, y + 30]], bbox=BBox(x=10, y=y, w=190, h=30))
    return TextBlock(block_key=key, section_key="sec_1_01", block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=BBox(x=10, y=y, w=190, h=30), regions=[reg])],
                     bbox=BBox(x=10, y=y, w=190, h=30), role="body", ocr_confidence=0.9)


MERGE = MergeResult(section_key="sec_1_01", blocks=[_block("blk_001", "a", 1, 40), _block("blk_002", "b", 2, 100), _block("blk_003", "c", 3, 160)])
CHECKED = JudgeChecked(dictionary_version={}, dictionary_fingerprint={}, match_rules_version="m", items=[], llm_called=True, input_fingerprint="0" * 64)


def _judge_ok():
    findings = [
        ContentFinding(finding_key="f_01", content_type="LC-91", status="present", evidence_block_ids=["blk_001"], evidence_source="text", reason="r"),
        ContentFinding(finding_key="f_02", content_type="LC-92", status="absent", evidence_block_ids=["blk_002"], evidence_source="text", reason="r"),
        ContentFinding(finding_key="f_03", content_type="LC-93", status="uncertain", evidence_block_ids=[], evidence_source="image", reason="r"),
        ContentFinding(finding_key="f_04", content_type="RG-902", status="present", evidence_block_ids=["blk_003"], evidence_source="text", reason="r"),
    ]
    matches = [Match(match_key="m_001", finding_key="f_04", dictionary_ref="RG-902", pattern="p", block_key="blk_003", raw_span=Span(start=0, end=1), matched_text="c")]
    return JudgeResult(section_key="sec_1_01", status="ok", content_findings=ContentFindings(findings=findings), matches=matches, checked=CHECKED)


def _policy():
    v = VerdictDraft(verdict_key="v_01", finding_key="f_04", dictionary_ref="RG-902", verdict_status="regulated", source_verdict_status="regulated",
                     finding_status="present", problem_text="c", reason="r", conflict_group="c_01")
    applied = PolicyApplied(regulatory_class="combination", applied_classes=["otc", "common"], dict_coverage="partial_class_combination",
                            dictionary_version={}, dictionary_fingerprint={}, rules_version="p")
    return PolicyResult(section_key="sec_1_01", status="ok", bucket_recommendation="exclude", verdicts=[v], applied=applied)


def _pixel(path, xy):
    return Image.open(path).convert("RGB").getpixel(xy)


def test_overlay_uses_distinct_colors_and_no_box_for_image_only(tmp_path):
    img = tmp_path / "sec.png"
    Image.new("RGB", (400, 220), (255, 255, 255)).save(img)
    out = insp.overlay_judge(img, MERGE, _judge_ok(), tmp_path / "out.png", policy=_policy())
    im = Image.open(out)
    margin = im.height - 220  # 설명 줄은 이미지 위 흰 띠에 그려 콘텐츠를 덮지 않는다
    assert margin > 0
    # 근거 블록 테두리 색: present(blk_001) 빨강 · absent(blk_002) 초록 · 규제 present(blk_003) 빨강
    assert _pixel(out, (10, 55 + margin)) == insp.STATUS_COLORS["present"]
    assert _pixel(out, (10, 115 + margin)) == insp.STATUS_COLORS["absent"]
    assert _pixel(out, (10, 175 + margin)) == insp.STATUS_COLORS["present"]
    assert insp.STATUS_COLORS["absent"] != insp.STATUS_COLORS["uncertain"] != insp.FAILED_COLOR
    # 이미지 전용 근거(LC-93)는 박스가 없다 — 블록 밖 영역이 흰색 그대로
    assert _pixel(out, (300, 210 + margin)) == (255, 255, 255)


def test_overlay_marks_failed_result(tmp_path):
    img = tmp_path / "sec.png"
    Image.new("RGB", (400, 220), (255, 255, 255)).save(img)
    failed = JudgeResult(section_key="sec_1_01", status="failed", content_findings=None, checked=CHECKED, error="504")
    out = insp.overlay_judge(img, MERGE, failed, tmp_path / "out.png")
    margin = Image.open(out).height - 220
    assert _pixel(out, (2, 110 + margin)) == insp.FAILED_COLOR  # 회색 테두리
    assert _pixel(out, (10, 55 + margin)) == (255, 255, 255)  # 판정 박스 없음


def test_cli_inspect_judge(tmp_path, capsys):
    img = tmp_path / "sec.png"
    Image.new("RGB", (400, 220), (255, 255, 255)).save(img)
    mp, jp, pp = tmp_path / "merge.json", tmp_path / "judge.json", tmp_path / "policy.json"
    mp.write_text(MERGE.model_dump_json(), encoding="utf-8")
    jp.write_text(_judge_ok().model_dump_json(), encoding="utf-8")
    pp.write_text(_policy().model_dump_json(), encoding="utf-8")
    code = runmod.main(["inspect", "--judge", str(jp), "--merge", str(mp), "--policy", str(pp), "--image", str(img), "--out", str(tmp_path / "o.png")])
    assert code == 0 and (tmp_path / "o.png").exists()
    import pytest

    with pytest.raises(SystemExit, match="--merge"):
        runmod.main(["inspect", "--judge", str(jp), "--image", str(img), "--out", str(tmp_path / "o2.png")])
