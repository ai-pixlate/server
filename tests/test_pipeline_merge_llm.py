"""③ llm_assist v1 검증 — pipeline.md 7.2절. 실제 API는 부르지 않고 가짜 호출자·재생 호출자를 쓴다.

섹션(폭 1000) 입력 영역:
- T  제목 한 줄(높이 40)                → 휴리스틱 블록 1개
- P1 · P2 본문 두 줄(세로 간격 30 > 12)  → 휴리스틱 블록 2개(과분할)
- F1 · F2 같은 줄 조각(가로 간격 30 > 21) → 휴리스틱 블록 2개(같은 줄 과분할)
- E  빈 텍스트(score 0.0) · Z 크기 0      → 단독 블록, LLM에 보내지 않음
"""
import json
import math
from collections import Counter
from pathlib import Path

import pytest

from pipeline import config as cfgmod
from pipeline import run as cli
from pipeline.stages import merge
from pipeline.types import BBox, OcrRegion, OcrResult, Section, SourceImage, SplitResult
from pipeline.vlm import (
    GeminiMergeAssistant,
    LlmReply,
    ReplayMergeAssistant,
    VlmError,
    VlmReplayMismatch,
    sha256_text,
)


def R(key, x, y, w, h, text="가", score=0.9):
    return OcrRegion(region_key=key, text=text, score=score,
                     poly=[(x, y), (x + w, y), (x + w, y + h), (x, y + h)], bbox=BBox(x=x, y=y, w=w, h=h))


def SEC(key="sec_1_01", order=1, top=0, height=1000, width=1000):
    return Section(section_key=key, source_image_id=1, section_order=order, top_offset=top, height=height,
                   width=width, image_path="unused.png")


def CFG(**over):
    cfg = cfgmod.load_config()
    cfg["merge"].update(over)
    return cfg


REGIONS = [
    R("reg_0001", 100, 0, 300, 40, "피부 진정"),
    R("reg_0002", 100, 100, 400, 20, "첫째 줄 문장이"),
    R("reg_0003", 100, 150, 400, 20, "이어지는 문장."),
    R("reg_0004", 100, 300, 200, 40, "10종 쿨링성분"),
    R("reg_0005", 330, 305, 40, 30, "으로"),
    R("reg_0006", 600, 0, 50, 20, "", 0.0),
    R("reg_0007", 700, 0, 0, 20, "글", 0.5),
]
OCR = OcrResult(section_key="sec_1_01", regions=REGIONS)


class FakeLlm:
    """payload의 텍스트로 묶음을 정한다. plan: [(텍스트 목록, 역할)], 또는 raw 응답 / 예외."""

    def __init__(self, plan=None, raw=None, exc=None, shuffle=False):
        self.config = {"model": "fake", "temperature": 0, "timeout_s": 60}
        self.plan, self.raw, self.exc, self.shuffle = plan, raw, exc, shuffle
        self.calls = []

    def __call__(self, prompt, payload):
        self.calls.append((prompt, payload))
        if self.exc is not None:
            raise self.exc
        if self.raw is not None:
            return LlmReply(self.raw, {"total_token_count": 1})
        by_text = {b["text"]: b["id"] for b in json.loads(payload)["blocks"]}
        blocks = [{"members": [by_text[t] for t in texts], "role": role} for texts, role in self.plan]
        if self.shuffle:  # 멤버·묶음 순서를 뒤집어도 결과가 같아야 한다
            blocks = [{"members": list(reversed(b["members"])), "role": b["role"]} for b in reversed(blocks)]
        return LlmReply(json.dumps({"blocks": blocks}, ensure_ascii=False), {"total_token_count": 42})


PLAN = [
    (["피부 진정"], "title"),
    (["첫째 줄 문장이", "이어지는 문장."], "body"),
    (["10종 쿨링성분", "으로"], "caption"),
]


def run_llm(llm, ocr=OCR, cfg=None, recorder=None):
    return merge.run(SEC(), ocr, cfg or CFG(), llm=llm, recorder=recorder)


def region_keys(b):
    return [r.region_key for ln in b.source_lines for r in ln.regions]


# ---------------------------------------------------------------------------
# 입력 · 조립
# ---------------------------------------------------------------------------
def test_payload_sends_only_text_blocks_with_expected_fields():
    heur = merge.heuristic(SEC(), OCR, CFG())
    payload, ids = merge.build_payload(SEC(), heur)
    assert payload["section"] == {"width": 1000, "height": 1000}
    assert [b["text"] for b in payload["blocks"]] == ["피부 진정", "첫째 줄 문장이", "이어지는 문장.", "10종 쿨링성분", "으로"]
    assert [b["id"] for b in payload["blocks"]] == ["b1", "b2", "b3", "b4", "b5"]
    first = payload["blocks"][0]
    assert first["bbox"] == [100, 0, 300, 40] and first["lines"] == 1 and first["font_h"] == 40
    sent = {r for b in ids.values() for r in region_keys(b)}
    assert "reg_0006" not in sent and "reg_0007" not in sent  # 빈 텍스트 · 크기 0 블록은 보내지 않는다


def test_llm_merges_blocks_sets_roles_and_keeps_same_line_fragments_as_separate_lines():
    res = run_llm(FakeLlm(PLAN))
    by_first = {region_keys(b)[0]: b for b in res.blocks}
    assert by_first["reg_0001"].role == "title"
    assert by_first["reg_0002"].source_ko == "첫째 줄 문장이\n이어지는 문장." and by_first["reg_0002"].role == "body"
    # v1은 블록 병합만 — 같은 줄 조각은 한 블록이 돼도 별도 줄로 남는다(줄 복구 없음)
    frag = by_first["reg_0004"]
    assert frag.source_ko == "10종 쿨링성분\n으로" and len(frag.source_lines) == 2
    assert [b.block_order for b in res.blocks] == list(range(1, len(res.blocks) + 1))
    assert [ln.line_key for b in res.blocks for ln in b.source_lines] == [f"line_{i:03d}" for i in range(1, 8)]


def test_invariants_regions_blocks_and_standalone_blocks_are_preserved():
    heur = merge.heuristic(SEC(), OCR, CFG())
    res = run_llm(FakeLlm(PLAN))
    out = [r for b in res.blocks for ln in b.source_lines for r in ln.regions]
    assert Counter(r.region_key for r in out) == Counter(r.region_key for r in REGIONS)  # 정확히 한 번
    by_key = {r.region_key: r for r in REGIONS}
    assert all(r.model_dump() == by_key[r.region_key].model_dump() for r in out)  # 값 불변
    owner = {rk: b.block_key for b in res.blocks for rk in region_keys(b)}
    for hb in heur.blocks:  # 휴리스틱 블록 하나의 영역이 여러 출력 블록으로 나뉘지 않는다
        assert len({owner[rk] for rk in region_keys(hb)}) == 1
        # 휴리스틱 블록 안의 줄 순서 · 줄 안의 영역 순서 유지
        ob = next(b for b in res.blocks if b.block_key == owner[region_keys(hb)[0]])
        seq = [r.region_key for ln in ob.source_lines for r in ln.regions]
        pos = [seq.index(rk) for rk in region_keys(hb)]
        assert pos == sorted(pos)
    for rk in ("reg_0006", "reg_0007"):  # 보내지 않은 단독 블록은 그대로(역할 · 텍스트 · 신뢰도)
        hb = next(b for b in heur.blocks if region_keys(b) == [rk])
        ob = next(b for b in res.blocks if region_keys(b) == [rk])
        assert (ob.role, ob.source_ko, ob.ocr_confidence, ob.bbox) == (hb.role, hb.source_ko, hb.ocr_confidence, hb.bbox)


def test_result_does_not_depend_on_member_or_block_order_in_response():
    a = run_llm(FakeLlm(PLAN)).model_dump()
    b = run_llm(FakeLlm(PLAN, shuffle=True)).model_dump()
    assert a == b


# ---------------------------------------------------------------------------
# 응답 검증 · 실패 · 기록
# ---------------------------------------------------------------------------
GOOD = [["b1"], ["b2", "b3"], ["b4", "b5"]]


def resp(groups, role="body"):
    return json.dumps({"blocks": [{"members": g, "role": role} for g in groups]})


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("not json", "JSON"),
        (json.dumps({"x": 1}), "형식"),
        (json.dumps({"blocks": [{"members": ["b1"]}]}), "role"),
        (resp([["b1"], ["b2", "b3"], ["b4"]]), "빠진 ID b5"),
        (resp(GOOD + [["b1"]]), "두 번 이상 나온 ID b1"),
        (resp(GOOD + [["b9"]]), "모르는 ID b9"),
        (resp(GOOD + [[]]), "빈 members"),
        (resp(GOOD, role="heading"), "모르는 role"),
        (json.dumps({"blocks": [{"members": "b1", "role": "body"}]}), "members"),
    ],
)
def test_invalid_responses_fail_and_are_recorded(raw, reason):
    recs = []
    with pytest.raises(merge.LlmResponseError) as ei:
        run_llm(FakeLlm(raw=raw), recorder=recs.append)
    assert isinstance(ei.value, VlmError)
    assert any(reason in r for r in ei.value.reasons)
    rec = recs[-1]
    assert rec["status"] == "validation_failed" and rec["response_text"] == raw and rec["error"] == ei.value.reasons


def test_call_failure_is_recorded_without_response_and_propagated():
    recs = []
    with pytest.raises(VlmError, match="timeout"):
        run_llm(FakeLlm(exc=VlmError("LLM 호출 실패: timeout")), recorder=recs.append)
    assert recs[-1]["status"] == "call_failed" and recs[-1]["response_text"] is None and "timeout" in recs[-1]["error"]


def failing_recorder(rec):
    raise OSError("디스크 가득 참")


def test_record_failure_does_not_hide_the_original_error():
    with pytest.raises(VlmError) as ei:
        run_llm(FakeLlm(exc=VlmError("원래 오류")), recorder=failing_recorder)
    assert "원래 오류" in str(ei.value)
    assert any("기록 저장 실패" in n for n in ei.value.__notes__)


def test_record_failure_after_success_fails_the_run():
    with pytest.raises(OSError, match="디스크"):
        run_llm(FakeLlm(PLAN), recorder=failing_recorder)


def test_success_record_has_inputs_response_mapping_and_usage():
    recs = []
    llm = FakeLlm(PLAN)
    cfg = CFG()
    res = run_llm(llm, cfg=cfg, recorder=recs.append)
    rec = recs[-1]
    prompt, payload = llm.calls[0]
    assert rec["status"] == "ok" and rec["error"] is None and rec["usage"] == {"total_token_count": 42}
    assert rec["model_config"] == merge.llm_config(cfg)
    assert rec["prompt_sha256"] == sha256_text(prompt) and rec["payload_sha256"] == sha256_text(payload)
    assert rec["payload"] == json.loads(payload)
    assert rec["ids"]["b1"] == "blk_001"
    final_keys = {b.block_key for b in res.blocks}
    heur = merge.heuristic(SEC(), OCR, cfg)
    assert set(rec["result"]["block_map"]) == {b.block_key for b in heur.blocks}  # 휴리스틱 키 → 섹션 최종 키
    assert set(rec["result"]["block_map"].values()) == final_keys


def test_no_sendable_blocks_skips_the_call():
    only_empty = OcrResult(section_key="sec_1_01", regions=[R("reg_0001", 10, 10, 50, 20, "", 0.0)])
    llm, recs = FakeLlm(PLAN), []
    res = run_llm(llm, ocr=only_empty, recorder=recs.append)
    assert llm.calls == [] and recs[-1]["status"] == "skipped"
    assert res == merge.heuristic(SEC(), only_empty, CFG())


# ---------------------------------------------------------------------------
# 설정 검증(실행 모드별)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "over",
    [
        {"llm_model": ""}, {"llm_model": "  "}, {"llm_model": 3},
        {"llm_temperature": True}, {"llm_temperature": math.nan}, {"llm_temperature": -0.1},
        {"llm_temperature": 2.1}, {"llm_temperature": "0"},
        {"llm_timeout_s": 0}, {"llm_timeout_s": -1}, {"llm_timeout_s": True}, {"llm_timeout_s": math.inf},
        {"prompt_path": ""}, {"prompt_path": "pipeline/prompts/없는_파일.md"}, {"prompt_path": 1},
    ],
)
def test_llm_config_bad_values_are_rejected(over):
    with pytest.raises(ValueError):
        merge.validate_llm_config(CFG(**over), need_api_key=False)


@pytest.mark.parametrize("over", [{"llm_temperature": 0}, {"llm_temperature": 2}, {"llm_timeout_s": 0.001}])
def test_llm_config_boundaries_are_accepted(over):
    assert merge.validate_llm_config(CFG(**over), need_api_key=False).strip()


def test_prompt_file_must_be_readable_nonempty_utf8(tmp_path):
    empty, bad = tmp_path / "empty.md", tmp_path / "bad.md"
    empty.write_text("  \n", encoding="utf-8")
    bad.write_bytes("프롬프트".encode("cp949"))
    for p in (empty, bad):
        with pytest.raises(ValueError, match="prompt_path"):
            merge.validate_llm_config(CFG(prompt_path=str(p)), need_api_key=False)


def test_prompt_path_relative_to_repo_root_and_absolute_as_is(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 작업 디렉터리와 무관
    assert "합치기" in merge.validate_llm_config(CFG(), need_api_key=False)
    own = tmp_path / "p.md"
    own.write_text("내 프롬프트", encoding="utf-8")
    assert merge.validate_llm_config(CFG(prompt_path=str(own)), need_api_key=False) == "내 프롬프트"


def test_no_llm_needs_neither_prompt_nor_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    res = merge.run(SEC(), OCR, CFG(prompt_path="없음.md", llm_temperature=True), use_llm=False)
    assert res == merge.heuristic(SEC(), OCR, CFG())


def test_injected_caller_needs_no_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert run_llm(FakeLlm(PLAN)).blocks


def test_gemini_caller_passes_timeout_and_no_retry_options(monkeypatch):
    import google.genai as genai

    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(genai, "Client", FakeClient)
    GeminiMergeAssistant(model="m", temperature=0, timeout_s=1.5)._client_or_raise()
    assert seen["http_options"].timeout == 1500
    assert seen["http_options"].retry_options is None  # SDK 2.24.0: 미지정이면 1회만 시도


# ---------------------------------------------------------------------------
# 재생
# ---------------------------------------------------------------------------
def record_of(llm, tmp_path, cfg=None):
    run_llm(llm, cfg=cfg, recorder=merge.json_recorder(tmp_path))
    return tmp_path / "sec_1_01.json"


def test_replay_reproduces_the_result(tmp_path):
    cfg = CFG()
    first = run_llm(FakeLlm(PLAN), cfg=cfg)
    path = record_of(FakeLlm(PLAN), tmp_path, cfg)
    replay = ReplayMergeAssistant.from_file(path, merge.llm_config(cfg))
    assert run_llm(replay, cfg=cfg) == first
    replay.finish()


def test_replay_rejects_changed_config_prompt_or_input(tmp_path):
    cfg = CFG()
    path = record_of(FakeLlm(PLAN), tmp_path, cfg)
    with pytest.raises(VlmReplayMismatch, match="모델 설정"):
        ReplayMergeAssistant.from_file(path, merge.llm_config(CFG(llm_temperature=0.5)))
    other = tmp_path / "other.md"
    other.write_text("다른 프롬프트", encoding="utf-8")
    with pytest.raises(VlmReplayMismatch, match="prompt_sha256"):
        run_llm(ReplayMergeAssistant.from_file(path, merge.llm_config(cfg)), cfg=CFG(prompt_path=str(other)))
    changed = OcrResult(section_key="sec_1_01", regions=REGIONS[:3])
    with pytest.raises(VlmReplayMismatch, match="payload_sha256"):
        run_llm(ReplayMergeAssistant.from_file(path, merge.llm_config(cfg)), ocr=changed, cfg=cfg)


def test_replayed_responses_go_through_the_same_validation(tmp_path):
    path = tmp_path / "bad" / "sec_1_01.json"
    with pytest.raises(merge.LlmResponseError):
        record_of(FakeLlm(raw=resp([["b1"]])), path.parent)
    with pytest.raises(merge.LlmResponseError):
        run_llm(ReplayMergeAssistant.from_file(path, merge.llm_config(CFG())))
    failed = tmp_path / "failed" / "sec_1_01.json"
    with pytest.raises(VlmError):
        record_of(FakeLlm(exc=VlmError("boom")), failed.parent)
    with pytest.raises(VlmError, match="기록된 호출 실패"):
        run_llm(ReplayMergeAssistant.from_file(failed, merge.llm_config(CFG())))


def test_unused_replay_record_is_a_mismatch(tmp_path):
    path = record_of(FakeLlm(PLAN), tmp_path)
    replay = ReplayMergeAssistant.from_file(path, merge.llm_config(CFG()))
    only_empty = OcrResult(section_key="sec_1_01", regions=[R("reg_0001", 10, 10, 50, 20, "", 0.0)])
    run_llm(replay, ocr=only_empty)  # 보낼 블록이 없어 호출하지 않음
    with pytest.raises(VlmReplayMismatch, match="쓰이지 않았다"):
        replay.finish()


# ---------------------------------------------------------------------------
# analyze() · CLI
# ---------------------------------------------------------------------------
def test_analyze_records_each_section_and_run_key_mapping(monkeypatch, tmp_path):
    from pipeline import analyze as an

    secs = [SEC(key="sec_1_01", order=1, top=0, height=500), SEC(key="sec_1_02", order=2, top=500, height=500)]
    split = SplitResult(source_image_id=1, source_width=1000, source_height=1000, sections=secs)
    regions = [R("reg_0001", 100, 0, 400, 20, "위"), R("reg_0002", 100, 300, 400, 20, "아래")]
    monkeypatch.setattr(an.section_split, "run", lambda src, cfg, out: split)
    monkeypatch.setattr(an.ocr, "run", lambda sec, cfg: OcrResult(section_key=sec.section_key, regions=regions))
    llm = FakeLlm([(["위"], "title"), (["아래"], "body")])
    res = an.analyze([SourceImage(source_image_id=1, upload_order=1, path="unused.png")], CFG(), tmp_path, llm=llm)
    assert len(llm.calls) == 2
    for key in ("sec_1_01", "sec_1_02"):
        assert json.loads((tmp_path / "merge_debug" / f"{key}.json").read_text(encoding="utf-8"))["status"] == "ok"
    keymap = json.loads((tmp_path / "merge_debug" / "block_keys.json").read_text(encoding="utf-8"))
    assert [(k["section_key"], k["section_block_key"], k["run_block_key"]) for k in keymap] == [
        ("sec_1_01", "blk_001", "blk_001"), ("sec_1_01", "blk_002", "blk_002"),
        ("sec_1_02", "blk_001", "blk_003"), ("sec_1_02", "blk_002", "blk_004"),
    ]
    assert [b.block_key for b in res.blocks] == ["blk_001", "blk_002", "blk_003", "blk_004"]
    assert [b.role for b in res.blocks] == ["title", "body", "title", "body"]


def test_analyze_validates_llm_config_before_split_and_ocr(monkeypatch, tmp_path):
    from pipeline import analyze as an

    calls = []
    monkeypatch.setattr(an.section_split, "run", lambda *a, **k: calls.append("split"))
    monkeypatch.setattr(an.ocr, "run", lambda *a, **k: calls.append("ocr"))
    with pytest.raises(ValueError, match="llm_temperature"):
        an.analyze([SourceImage(source_image_id=1, upload_order=1, path="unused.png")], CFG(llm_temperature=True),
                   tmp_path, llm=FakeLlm(PLAN))
    assert calls == []


SAMPLE_EXP = Path(__file__).resolve().parent.parent / "pipeline" / "samples" / "synthetic_01" / "expected"


def test_cli_rejects_no_llm_together_with_replay(tmp_path):
    with pytest.raises(SystemExit) as ei:
        cli.main(["merge", "--split", str(SAMPLE_EXP / "split.json"), "--ocr", str(SAMPLE_EXP / "ocr/sec_1_01.json"),
                  "--out", str(tmp_path), "--no-llm", "--llm-replay", "x.json"])
    assert ei.value.code == 2


def test_cli_merge_replay_writes_result_and_record(tmp_path):
    from pipeline import jsonio

    cfg = CFG()
    split = jsonio.load_split(SAMPLE_EXP / "split.json")
    ocr = jsonio.load_ocr(SAMPLE_EXP / "ocr/sec_1_01.json")
    sec = next(s for s in split.sections if s.section_key == ocr.section_key)

    class EachAlone(FakeLlm):
        def __call__(self, prompt, payload):
            ids = [b["id"] for b in json.loads(payload)["blocks"]]
            return LlmReply(json.dumps({"blocks": [{"members": [i], "role": "body"} for i in ids]}))

    first_dir = tmp_path / "first"
    expected = merge.run(sec, ocr, cfg, llm=EachAlone(), recorder=merge.json_recorder(first_dir))
    out = tmp_path / "replay"
    rc = cli.main(["merge", "--split", str(SAMPLE_EXP / "split.json"), "--ocr", str(SAMPLE_EXP / "ocr/sec_1_01.json"),
                   "--out", str(out), "--llm-replay", str(first_dir / "sec_1_01.json")])
    assert rc == 0
    assert jsonio.load_merge(out / "merge" / "sec_1_01.json") == expected
    assert json.loads((out / "merge_debug" / "sec_1_01.json").read_text(encoding="utf-8"))["status"] == "ok"
    run_rec = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert run_rec["use_llm"] is True and run_rec["llm_replay"].endswith("sec_1_01.json")
