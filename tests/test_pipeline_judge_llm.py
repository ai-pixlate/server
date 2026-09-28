"""③-1b 맥락 판정 — 페이로드 · 응답 검증 · 조립 · 실패 · 기록 · 재생 · CLI(가짜 호출자, 실제 API 호출 없음).

승인 조건(2026-09-29): 정상 응답뿐 아니라 누락 · 중복 · 알 수 없는 항목 ID · 잘못된 블록 참조 · 타임아웃을 검증하고,
실패 · 미검사를 absent로 바꾸지 않는다. 앞뒤 섹션은 참고용이며 근거로 귀속하지 않는다.
"""
import json
from pathlib import Path

import pytest
from PIL import Image

from pipeline import config as cfgmod
from pipeline import run as runmod
from pipeline.dictionary import load_dictionaries
from pipeline.stages import judge
from pipeline.types import BBox, JudgeContext, JudgeResult, Line, MergeResult, OcrRegion, Section, SplitResult, TextBlock
from pipeline.vlm import LlmReply, ReplayJudgeAssistant, VlmError, VlmReplayMismatch

SYNTH = "pipeline/data/dict/synthetic"
LC_IDS = ["LC-91", "LC-92", "LC-93"]


def _block(key: str, text: str, order: int, section="sec_1_01") -> TextBlock:
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text or " ", score=0.9, poly=[[0, 0], [10, 0], [10, 10], [0, 10]], bbox=BBox(x=0, y=0, w=10, h=10))
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=BBox(x=0, y=0, w=10, h=10), regions=[reg])],
                     bbox=BBox(x=0, y=0, w=10, h=10), role="body", ocr_confidence=0.9)


@pytest.fixture
def section(tmp_path) -> Section:
    img = tmp_path / "sec_1_01.png"
    Image.new("RGB", (1000, 300), (250, 250, 250)).save(img)
    return Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=300, width=1000, image_path=str(img))


@pytest.fixture
def cfg(tmp_path):
    prompt = tmp_path / "judge_context.md"
    prompt.write_text("판정 프롬프트(테스트)", encoding="utf-8")
    return cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH}", f"judge.prompt_path='{prompt.as_posix()}'"])


BLOCKS = [_block("blk_001", "가짜센터로 문의하세요", 1), _block("blk_002", "가짜방수 기능", 2), _block("blk_003", "일반 설명", 3)]


class FakeLlm:
    """가짜 호출자. items를 주면 그 응답, exc를 주면 호출 실패(시간 초과 포함)를 흉내 낸다."""

    def __init__(self, items=None, exc: BaseException | None = None, raw: str | None = None):
        self.config = {"model": "gemini-3.8-flash", "temperature": 0, "timeout_s": 60}
        self.items, self.exc, self.raw = items, exc, raw
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, prompt, payload, image):
        self.calls.append((prompt, payload, image))
        if self.exc is not None:
            raise self.exc
        return LlmReply(text=self.raw if self.raw is not None else json.dumps({"items": self.items}, ensure_ascii=False), usage={"total_token_count": 10})


def _ok_items(**over):
    base = {
        "LC-91": {"id": "LC-91", "status": "present", "evidence_source": "text", "evidence": ["b1"], "reason": "국내 문의처 안내"},
        "LC-92": {"id": "LC-92", "status": "absent", "evidence_source": "text", "evidence": ["b3"], "reason": "기획 구성 없음"},
        "LC-93": {"id": "LC-93", "status": "uncertain", "evidence_source": "image", "evidence": [], "reason": "가격인지 불명"},
    }
    for k, v in over.items():
        if v is None:
            base.pop(k)
        else:
            base[k] = {**base.get(k, {"id": k}), **v}
    return list(base.values())


# ---------------------------------------------------------------------------
# 페이로드
# ---------------------------------------------------------------------------
def test_payload_sends_only_lc_items_and_context_as_reference(cfg):
    view = load_dictionaries(SYNTH).judge_view()
    matches = judge.detect("sec_1_01", BLOCKS, view, cfg)
    ctx = JudgeContext(prev_section_text="앞 섹션: 원 가격", next_section_text=None)
    payload, block_ids, sent = judge.build_payload("sec_1_01", BLOCKS, view, ctx, cfg, matches)
    assert sent == LC_IDS  # call_scope=all → LC 8개(합성은 3개) 전부, 규제 항목 없음
    assert [b["id"] for b in payload["blocks"]] == ["b1", "b2", "b3"] and block_ids["b2"] == "blk_002"
    lc91 = next(i for i in payload["items"] if i["id"] == "LC-91")
    assert lc91["candidate_blocks"] == ["b1"] and lc91["matched_patterns"] == ["가짜센터"]
    assert lc91["exclude_when"] and lc91["keep_when"]
    assert payload["context"]["prev"] == "앞 섹션: 원 가격" and "참고" in payload["context"]["note"]
    text = judge.payload_text(payload)
    for forbidden in ("verdict_status", "irrelevant", "needs_fix", "셀러 문장", "kr_freq", "RG-"):
        assert forbidden not in text


def test_payload_matched_scope_sends_only_matched_items(cfg):
    c = cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH}", "judge.call_scope='matched'", "judge.context_sections=0"])
    view = load_dictionaries(SYNTH).judge_view()
    matches = judge.detect("sec_1_01", BLOCKS, view, c)
    payload, _, sent = judge.build_payload("sec_1_01", BLOCKS, view, JudgeContext(), c, matches)
    assert sent == ["LC-91"] and "context" not in payload


# ---------------------------------------------------------------------------
# 응답 검증 — 누락 · 중복 · 모르는 ID · 잘못된 블록 참조 · 근거 규칙
# ---------------------------------------------------------------------------
BLOCK_IDS = {"b1": "blk_001", "b2": "blk_002", "b3": "blk_003"}


def test_parse_items_accepts_valid_response():
    out = judge.parse_items(json.dumps({"items": _ok_items()}), LC_IDS, BLOCK_IDS)
    assert out["LC-91"] == {"status": "present", "evidence_source": "text", "evidence": ["blk_001"], "reason": "국내 문의처 안내"}
    assert out["LC-93"]["evidence"] == []


@pytest.mark.parametrize(
    "items, msg",
    [
        (_ok_items(**{"LC-93": None}), "응답에 없는 항목"),  # 누락 — absent가 아니다
        (_ok_items() + [_ok_items()[0]], "중복"),
        (_ok_items() + [{"id": "LC-99", "status": "absent", "evidence_source": "text", "evidence": ["b1"], "reason": "x"}], "모르는 항목"),
        (_ok_items(**{"LC-91": {"evidence": ["b9"]}}), "없는 블록 참조"),
        (_ok_items(**{"LC-91": {"evidence": ["prev"]}}), "없는 블록 참조"),  # 앞뒤 섹션은 근거가 될 수 없다
        (_ok_items(**{"LC-91": {"evidence": ["b1", "b1"]}}), "evidence 중복"),
        (_ok_items(**{"LC-92": {"evidence": []}}), "근거 블록이 없다"),  # text 근거인데 비어 있음
        (_ok_items(**{"LC-91": {"status": "maybe"}}), "status"),
        (_ok_items(**{"LC-91": {"evidence_source": "both"}}), "evidence_source"),
        (_ok_items(**{"LC-91": {"reason": " "}}), "reason"),
        (_ok_items(**{"LC-91": {"evidence": "b1"}}), "문자열 배열"),
    ],
)
def test_parse_items_rejects(items, msg):
    with pytest.raises(judge.JudgeResponseError, match=msg):
        judge.parse_items(json.dumps({"items": items}, ensure_ascii=False), LC_IDS, BLOCK_IDS)


def test_parse_items_rejects_non_json_and_missing_items():
    with pytest.raises(judge.JudgeResponseError, match="JSON"):
        judge.parse_items("not json", LC_IDS, BLOCK_IDS)
    with pytest.raises(judge.JudgeResponseError, match="items"):
        judge.parse_items(json.dumps({"result": []}), LC_IDS, BLOCK_IDS)


# ---------------------------------------------------------------------------
# run — 조립 · skipped · 실패
# ---------------------------------------------------------------------------
def test_run_assembles_findings_and_binds_matches(cfg, section):
    llm = FakeLlm(_ok_items())
    records = []
    res = judge.run(section, BLOCKS, JudgeContext(prev_section_text="앞"), cfg, llm=llm, recorder=records.append)
    assert isinstance(res, JudgeResult) and res.status == "ok"
    f = {x.content_type: x for x in res.content_findings.findings}
    assert set(f) == {"LC-91", "LC-92", "LC-93", "RG-902"}  # LC 전부 + 매칭된 RG만
    assert f["LC-91"].status == "present" and f["LC-91"].evidence_block_ids == ["blk_001"] and f["LC-91"].evidence_source == "text"
    assert f["LC-93"].status == "uncertain" and f["LC-93"].evidence_source == "image" and f["LC-93"].evidence_block_ids == []
    assert f["RG-902"].status == "present" and f["RG-902"].evidence_source == "text" and f["RG-902"].evidence_block_ids == ["blk_002"]
    assert [m.finding_key for m in res.matches] == [f["LC-91"].finding_key, f["RG-902"].finding_key]
    assert res.checked.llm_called and set(res.checked.items) >= set(LC_IDS) | {"RG-902", "RG-905"}
    # 호출 입력: 프롬프트 · 페이로드 · 축소 이미지(폭 768)
    prompt, payload, image = llm.calls[0]
    assert prompt == "판정 프롬프트(테스트)" and '"LC-91"' in payload and image.size == (768, 230)
    rec = records[0]
    assert rec["status"] == "ok" and rec["image"]["sent_size"] == [768, 230] and rec["response_text"] and rec["payload_sha256"]
    assert rec["result"]["findings"][0]["content_type"] == "LC-91"


def test_run_skipped_when_matched_scope_has_no_lc_match(cfg, section):
    c = cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH}", "judge.call_scope='matched'"])
    llm = FakeLlm(_ok_items())
    res = judge.run(section, [_block("blk_001", "가짜방수만 있음", 1)], JudgeContext(), c, llm=llm)
    assert res.status == "skipped" and llm.calls == [] and res.checked.llm_called is False
    assert [x.content_type for x in res.content_findings.findings] == ["RG-902"]  # LC finding 없음(누락, absent 아님)
    assert res.checked.items == [i.id for i in load_dictionaries(SYNTH).judge_view().regulatory_items()]


@pytest.mark.parametrize("exc, status", [(VlmError("504 DEADLINE_EXCEEDED"), "call_failed"), (VlmError("boom"), "call_failed")])
def test_run_call_failure_and_timeout_yield_failed_not_absent(cfg, section, exc, status):
    records = []
    res = judge.run(section, BLOCKS, JudgeContext(), cfg, llm=FakeLlm(exc=exc), recorder=records.append)
    assert res.status == "failed" and res.content_findings is None and res.matches == []
    assert "DEADLINE" in res.error or "boom" in res.error
    assert records[0]["status"] == status and len(records[0]["matches"]) == 2  # 매칭은 기록에만


def test_run_validation_failure_yields_failed(cfg, section):
    records = []
    res = judge.run(section, BLOCKS, JudgeContext(), cfg, llm=FakeLlm(_ok_items(**{"LC-93": None})), recorder=records.append)
    assert res.status == "failed" and res.content_findings is None and "응답에 없는 항목" in res.error
    assert records[0]["status"] == "validation_failed" and records[0]["response_text"]


def test_run_record_failure_on_success_is_an_error(cfg, section):
    def bad_recorder(rec):
        raise OSError("disk")

    with pytest.raises(OSError):
        judge.run(section, BLOCKS, JudgeContext(), cfg, llm=FakeLlm(_ok_items()), recorder=bad_recorder)
    res = judge.run(section, BLOCKS, JudgeContext(), cfg, llm=FakeLlm(exc=VlmError("x")), recorder=bad_recorder)
    assert res.status == "failed" and "기록 저장 실패" in res.error


def test_run_rejects_foreign_blocks_and_needs_key_without_llm(cfg, section, monkeypatch):
    with pytest.raises(ValueError, match="section_key"):
        judge.run(section, [_block("blk_001", "x", 1, section="sec_9_09")], JudgeContext(), cfg, llm=FakeLlm(_ok_items()))
    monkeypatch.delenv(judge.API_KEY_ENV, raising=False)
    with pytest.raises(ValueError, match=judge.API_KEY_ENV):
        judge.run(section, BLOCKS, JudgeContext(), cfg)


# ---------------------------------------------------------------------------
# 재생
# ---------------------------------------------------------------------------
def test_replay_reproduces_and_detects_drift(cfg, section):
    records = []
    first = judge.run(section, BLOCKS, JudgeContext(), cfg, llm=FakeLlm(_ok_items()), recorder=records.append)
    replay = ReplayJudgeAssistant(records[0], judge.llm_config(cfg))
    again = judge.run(section, BLOCKS, JudgeContext(), cfg, llm=replay)
    replay.finish()
    assert again.content_findings == first.content_findings and again.matches == first.matches
    drift = ReplayJudgeAssistant(records[0], judge.llm_config(cfg))
    res = judge.run(section, BLOCKS + [_block("blk_004", "추가", 4)], JudgeContext(), cfg, llm=drift)
    assert res.status == "failed" and "재생 입력 불일치" in res.error
    with pytest.raises(VlmReplayMismatch, match="모델 설정"):
        ReplayJudgeAssistant(records[0], {**judge.llm_config(cfg), "temperature": 1})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _write_inputs(tmp_path: Path, section: Section) -> tuple[Path, Path]:
    split = SplitResult(source_image_id=1, source_width=1000, source_height=300, sections=[section])
    sp = tmp_path / "split.json"
    sp.write_text(split.model_dump_json(indent=2), encoding="utf-8")
    mp = tmp_path / "merge" / "sec_1_01.json"
    mp.parent.mkdir()
    mp.write_text(MergeResult(section_key="sec_1_01", blocks=BLOCKS).model_dump_json(indent=2), encoding="utf-8")
    return sp, mp


def test_cli_judge_full_mode_with_replay(tmp_path, section, cfg, capsys):
    sp, mp = _write_inputs(tmp_path, section)
    records = []
    judge.run(section, BLOCKS, JudgeContext(), cfg, llm=FakeLlm(_ok_items()), recorder=records.append)
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps(records[0], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    code = runmod.main(["judge", "--merge", str(mp), "--split", str(sp), "--out", str(out), "--llm-replay", str(rec),
                        "--set", f"judge.dict_dir={SYNTH}", "--set", f"judge.prompt_path='{cfg['judge']['prompt_path']}'"])
    assert code == 0
    res = json.loads((out / "judge" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert res["status"] == "ok" and len(res["content_findings"]["findings"]) == 4
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run["mode"] == "full" and run["llm_replay"] == str(rec) and run["status"] == "ok"
    assert "판정 ok" in capsys.readouterr().out


def test_cli_judge_full_mode_failed_exit_code(tmp_path, section, cfg, capsys):
    sp, mp = _write_inputs(tmp_path, section)
    records = []
    judge.run(section, BLOCKS, JudgeContext(), cfg, llm=FakeLlm(exc=VlmError("504")), recorder=records.append)
    rec = tmp_path / "rec.json"
    rec.write_text(json.dumps(records[0], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    code = runmod.main(["judge", "--merge", str(mp), "--split", str(sp), "--out", str(out), "--llm-replay", str(rec),
                        "--set", f"judge.dict_dir={SYNTH}", "--set", f"judge.prompt_path='{cfg['judge']['prompt_path']}'"])
    assert code == 4
    res = json.loads((out / "judge" / "sec_1_01.json").read_text(encoding="utf-8"))
    assert res["status"] == "failed" and res["content_findings"] is None
    assert "판정 실패" in capsys.readouterr().err
