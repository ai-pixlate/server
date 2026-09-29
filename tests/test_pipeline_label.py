"""④ 제품 라벨 판정 — 응답 검증 · 섹션 단위 실패 · 경계 사례 · 입력 불변성 · 재생 · 설정 · 타입(가짜 호출자, 실제 API 호출 없음).

단독 개발 방침(open-questions #66, 사용자 승인 2026-09-29): 블록마다 정확히 하나의 JSON boolean · 누락 · 중복 · 미등록 키 · 잘못된 타입은 실패 ·
실패는 섹션 전체 실패(labels=None)이며 false · 부분 성공으로 바꾸지 않는다 · 공백 블록은 VLM에 보내지 않고 false · 블록 없음은 호출 없이 빈 목록.
"""
import copy
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from pipeline import config as cfgmod
from pipeline import jsonio
from pipeline.stages import label
from pipeline.types import BBox, LabelChecked, LabelDecision, LabelResult, Line, MergeResult, OcrRegion, Section, TextBlock
from pipeline.vlm import LlmReply, ReplayLabelAssistant, VlmError, VlmReplayMismatch

W, H = 1000, 3000  # 세로로 긴 섹션 — 긴 변 1024 기준이면 341x1024로 줄어든다


def _block(key: str, text: str, order: int, y: int, score: float = 0.9, section: str = "sec_1_01") -> TextBlock:
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text, score=score, poly=[[100, y], [400, y], [400, y + 50], [100, y + 50]],
                    bbox=BBox(x=100, y=y, w=300, h=50))
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=BBox(x=100, y=y, w=300, h=50), regions=[reg])],
                     bbox=BBox(x=100, y=y, w=300, h=50), role="body", ocr_confidence=score)


BLOCKS = [
    _block("blk_001", "구달 맑은 어성초 진정 수분 선크림", 1, 100),  # 제품명(페이지 디자인 글자일 수 있음)
    _block("blk_002", "", 2, 600),  # 빈 텍스트 블록(#36)
    _block("blk_003", "HEARTLEAF\nSUN CREAM 50ml", 3, 1200, score=0.31),  # 저신뢰 — 신뢰도만으로 빼지 않는다
    _block("blk_004", "   ", 4, 1800),  # 공백만
    _block("blk_005", "MINERAL FILTER 촉촉한 사용감", 5, 2400),  # 라벨 + 일반 문구 과병합(혼합)
]


@pytest.fixture
def section(tmp_path) -> Section:
    img = tmp_path / "sec_1_01.png"
    Image.new("RGB", (W, H), (240, 240, 240)).save(img)
    return Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=H, width=W, image_path=str(img))


@pytest.fixture
def cfg(tmp_path):
    prompt = tmp_path / "label.md"
    prompt.write_text("라벨 판정 프롬프트(테스트)", encoding="utf-8")
    return cfgmod.load_config(overrides=[f"label.prompt_path='{prompt.as_posix()}'"])


class FakeLlm:
    """가짜 호출자. labels(임시 ID → 값)를 주면 그 응답, exc를 주면 호출 실패(시간 초과 포함), raw를 주면 그 원문."""

    def __init__(self, labels=None, exc: BaseException | None = None, raw: str | None = None):
        self.config = {"model": "gemini-3.8-flash", "temperature": 0, "timeout_s": 60}
        self.labels, self.exc, self.raw = labels, exc, raw
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, prompt, payload, image):
        self.calls.append((prompt, payload, image))
        if self.exc is not None:
            raise self.exc
        if self.raw is not None:
            text = self.raw
        else:
            text = json.dumps({"labels": [{"id": k, "is_product_label": v} for k, v in self.labels.items()]})
        return LlmReply(text=text, usage={"prompt_token_count": 1200, "candidates_token_count": 40, "total_token_count": 1240})


OK = {"b1": False, "b2": True, "b3": True}  # 보내는 블록은 blk_001 · blk_003 · blk_005(b1 · b2 · b3)


# ---------------------------------------------------------------------------
# 응답 검증
# ---------------------------------------------------------------------------
SENT = ["b1", "b2", "b3"]


def _raw(items, **top):
    return json.dumps({"labels": items, **top})


def test_parse_labels_accepts_exactly_one_boolean_per_block():
    assert label.parse_labels(_raw([{"id": "b3", "is_product_label": True}, {"id": "b1", "is_product_label": False},
                                    {"id": "b2", "is_product_label": False}]), SENT) == {"b1": False, "b2": False, "b3": True}


@pytest.mark.parametrize(
    "text, msg",
    [
        (_raw([{"id": "b1", "is_product_label": True}, {"id": "b2", "is_product_label": True}]), "판정 누락"),  # 누락은 false가 아니다
        (_raw([{"id": "b1", "is_product_label": True}] * 2 + [{"id": "b2", "is_product_label": True}, {"id": "b3", "is_product_label": True}]), "중복"),
        (_raw([{"id": x, "is_product_label": True} for x in SENT] + [{"id": "b9", "is_product_label": True}]), "미등록 블록 ID"),
        (_raw([{"id": "b1", "is_product_label": "true"}, {"id": "b2", "is_product_label": True}, {"id": "b3", "is_product_label": True}]), "boolean이 아니다"),
        (_raw([{"id": "b1", "is_product_label": 1}, {"id": "b2", "is_product_label": True}, {"id": "b3", "is_product_label": True}]), "boolean이 아니다"),
        (_raw([{"id": "b1", "is_product_label": None}, {"id": "b2", "is_product_label": True}, {"id": "b3", "is_product_label": True}]), "boolean이 아니다"),
        (_raw([{"id": "b1"}, {"id": "b2", "is_product_label": True}, {"id": "b3", "is_product_label": True}]), "is_product_label 없음"),
        (_raw([{"id": x, "is_product_label": True, "reason": "x"} for x in SENT]), "모르는 키"),
        (_raw([{"id": x, "is_product_label": True} for x in SENT], note="x"), "최상위에 모르는 키"),
        (_raw([{"id": 1, "is_product_label": True}]), "미등록 블록 ID"),
        (_raw(["b1"]), "객체가 아니다"),
        ("not json", "JSON"),
        (json.dumps({"result": []}), "labels 배열"),
        (json.dumps([]), "labels 배열"),
    ],
)
def test_parse_labels_rejects(text, msg):
    with pytest.raises(label.LabelResponseError, match=msg):
        label.parse_labels(text, SENT)


# ---------------------------------------------------------------------------
# run — 정상 · 경계 사례
# ---------------------------------------------------------------------------
def test_run_ok_sends_nonblank_blocks_with_image_and_boxes(cfg, section):
    llm = FakeLlm(OK)
    records = []
    res = label.run(section, BLOCKS, cfg, llm=llm, recorder=records.append)
    assert res.status == "ok" and res.error is None and res.checked.llm_called
    got = [(d.block_key, d.is_product_label, d.basis) for d in res.labels]
    assert got == [
        ("blk_001", False, "vlm"),
        ("blk_002", False, "blank_text"),  # 빈 텍스트 — VLM 대상 제외, false
        ("blk_003", True, "vlm"),  # 저신뢰 블록도 보냈다
        ("blk_004", False, "blank_text"),  # 공백만
        ("blk_005", True, "vlm"),  # 혼합 블록은 통째로 true — 나누지 않는다(블록 1개 = 판정 1개)
    ]
    assert res.checked.sent_block_keys == ["blk_001", "blk_003", "blk_005"] and res.checked.blank_block_keys == ["blk_002", "blk_004"]
    prompt, payload, image = llm.calls[0]
    assert prompt == "라벨 판정 프롬프트(테스트)"
    assert image.size == (341, 1024)  # 긴 변 1024(정본)
    p = json.loads(payload)
    assert [b["id"] for b in p["blocks"]] == ["b1", "b2", "b3"]
    assert p["blocks"][1] == {"id": "b2", "text": "HEARTLEAF\nSUN CREAM 50ml", "box_2d": [400, 100, 417, 400]}
    assert "role" not in payload and "ocr_confidence" not in payload and "score" not in payload
    rec = records[0]
    assert rec["status"] == "ok" and rec["image"] == {"original_size": [W, H], "sent_size": [341, 1024], "long_side_px": 1024, "scale": 0.341333}
    for k in ("payload_sha256", "prompt_sha256", "image_sha256", "input_fingerprint", "response_text", "usage", "call_duration_s", "duration_s"):
        assert rec[k] is not None, k
    assert rec["model_config"] == {"model": "gemini-3.8-flash", "temperature": 0, "timeout_s": 60}
    assert rec["validation"] == {"ok": True, "reasons": []} and rec["bias"] == "label"


def test_run_small_image_is_not_upscaled(cfg, tmp_path):
    img = tmp_path / "small.png"
    Image.new("RGB", (800, 400), (255, 255, 255)).save(img)
    sec = Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=400, width=800, image_path=str(img))
    llm = FakeLlm({"b1": True})
    res = label.run(sec, [_block("blk_001", "x", 1, 10)], cfg, llm=llm)
    assert res.status == "ok" and llm.calls[0][2].size == (800, 400)


def test_run_without_blocks_is_ok_empty_and_no_call(cfg, section):
    llm = FakeLlm(OK)
    records = []
    res = label.run(section, [], cfg, llm=llm, recorder=records.append)
    assert res.status == "ok" and res.labels == [] and not res.checked.llm_called and llm.calls == []
    assert records[0]["status"] == "skipped" and records[0]["skip_reason"] == "no_blocks"


def test_run_blank_only_is_all_false_without_call_or_prompt(section, monkeypatch):
    monkeypatch.delenv(label.API_KEY_ENV, raising=False)
    c = cfgmod.load_config(overrides=["label.prompt_path='does/not/exist.md'"])  # 호출하지 않으면 프롬프트 · API 키가 필요 없다
    blanks = [BLOCKS[1], BLOCKS[3]]
    records = []
    res = label.run(section, blanks, c, recorder=records.append)
    assert res.status == "ok" and [(d.is_product_label, d.basis) for d in res.labels] == [(False, "blank_text")] * 2
    assert records[0]["skip_reason"] == "blank_only" and not res.checked.llm_called


def test_run_does_not_mutate_inputs(cfg, section, tmp_path):
    before = copy.deepcopy([b.model_dump() for b in BLOCKS])
    mp = tmp_path / "merge.json"
    mp.write_text(MergeResult(section_key="sec_1_01", blocks=BLOCKS).model_dump_json(indent=2), encoding="utf-8")
    raw_before = mp.read_bytes()
    img_before = Path(section.image_path).read_bytes()
    label.run(section, jsonio.load_merge(mp).blocks, cfg, llm=FakeLlm(OK))
    label.run(section, BLOCKS, cfg, llm=FakeLlm(exc=VlmError("x")))
    assert [b.model_dump() for b in BLOCKS] == before
    assert mp.read_bytes() == raw_before and Path(section.image_path).read_bytes() == img_before


def test_result_is_independent_of_llm_response_order(cfg, section):
    a = label.run(section, BLOCKS, cfg, llm=FakeLlm(OK))
    b = label.run(section, BLOCKS, cfg, llm=FakeLlm(dict(reversed(list(OK.items())))))
    shuffled = [BLOCKS[4], BLOCKS[0], BLOCKS[3], BLOCKS[2], BLOCKS[1]]
    c = label.run(section, shuffled, cfg, llm=FakeLlm(OK))  # 입력 배열 순서가 아니라 block_order
    assert a.labels == b.labels == c.labels


# ---------------------------------------------------------------------------
# 섹션 단위 실패 — 부분 성공 · false 대체 없음
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc", [VlmError("LLM 호출 실패: 504 DEADLINE_EXCEEDED"), VlmError("401 UNAUTHENTICATED")])
def test_call_failure_fails_whole_section(cfg, section, exc):
    records = []
    res = label.run(section, BLOCKS, cfg, llm=FakeLlm(exc=exc), recorder=records.append)
    assert res.status == "failed" and res.labels is None and str(exc) in res.error
    assert records[0]["status"] == "call_failed" and records[0]["response_text"] is None and records[0]["call_duration_s"] is not None


def test_validation_failure_fails_whole_section_even_if_some_blocks_valid(cfg, section):
    records = []
    res = label.run(section, BLOCKS, cfg, llm=FakeLlm({"b1": True, "b2": True}), recorder=records.append)  # b3 누락
    assert res.status == "failed" and res.labels is None and "판정 누락" in res.error
    assert records[0]["status"] == "validation_failed" and records[0]["validation"]["ok"] is False and records[0]["response_text"]
    res2 = label.run(section, BLOCKS, cfg, llm=FakeLlm(raw=""))
    assert res2.status == "failed" and "JSON" in res2.error


def test_record_failure(cfg, section):
    def bad(rec):
        raise OSError("disk")

    with pytest.raises(OSError):  # 성공 결과는 기록 없이 남기지 않는다
        label.run(section, BLOCKS, cfg, llm=FakeLlm(OK), recorder=bad)
    res = label.run(section, BLOCKS, cfg, llm=FakeLlm(exc=VlmError("x")), recorder=bad)
    assert res.status == "failed" and "기록 저장 실패" in res.error


def test_input_errors_raise(cfg, section, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="section_key"):
        label.run(section, [_block("blk_001", "x", 1, 10, section="sec_9_09")], cfg, llm=FakeLlm(OK))
    with pytest.raises(ValueError, match="같은 block_key"):
        label.run(section, [_block("blk_001", "x", 1, 10), _block("blk_001", "y", 2, 90)], cfg, llm=FakeLlm(OK))
    bad = section.model_copy(update={"width": 999})
    with pytest.raises(ValueError, match="메타데이터"):
        label.run(bad, BLOCKS, cfg, llm=FakeLlm(OK))
    monkeypatch.delenv(label.API_KEY_ENV, raising=False)
    with pytest.raises(ValueError, match=label.API_KEY_ENV):
        label.run(section, BLOCKS, cfg)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "override, msg",
    [
        ("label.bias='text'", "label.bias"),
        ("label.long_side_px=1024.0", "long_side_px"),
        ("label.long_side_px=true", "long_side_px"),
        ("label.temperature=true", "temperature"),
        ("label.temperature=3", "temperature"),
        ("label.timeout_s=0", "timeout_s"),
        ("label.timeout_s=nan", "timeout_s"),
        ("label.model=''", "label.model"),
        ("label.prompt_path='nope/none.md'", "prompt_path"),
    ],
)
def test_llm_config_validation(override, msg):
    c = cfgmod.load_config(overrides=[override])
    with pytest.raises(ValueError, match=msg):
        label.validate_llm_config(c, need_api_key=False)


def test_default_config_is_valid_and_prompt_exists():
    c = cfgmod.load_config()
    assert c["label"] == {"model": "gemini-3.8-flash", "long_side_px": 1024, "bias": "label", "temperature": 0, "timeout_s": 60,
                          "prompt_path": "pipeline/prompts/label.md"}
    assert label.validate_llm_config(c, need_api_key=False).strip()


# ---------------------------------------------------------------------------
# 타입
# ---------------------------------------------------------------------------
def _checked(**kw):
    return LabelChecked(input_fingerprint="0" * 64, llm_called=True, **kw)


def test_label_types_reject_inconsistent_states():
    with pytest.raises(ValidationError):
        LabelDecision(block_key="blk_001", is_product_label="true", basis="vlm")  # 묵시 변환 금지
    with pytest.raises(ValidationError):
        LabelDecision(block_key="blk_001", is_product_label=1, basis="vlm")
    with pytest.raises(ValidationError, match="None"):
        LabelResult(section_key="s", status="failed", labels=[], checked=_checked(), error="x")
    with pytest.raises(ValidationError, match="error"):
        LabelResult(section_key="s", status="failed", labels=None, checked=_checked())
    with pytest.raises(ValidationError, match="labels가 있어야"):
        LabelResult(section_key="s", status="ok", labels=None, checked=_checked())
    d = LabelDecision(block_key="blk_001", is_product_label=True, basis="vlm")
    with pytest.raises(ValidationError, match="두 번"):
        LabelResult(section_key="s", status="ok", labels=[d, d], checked=_checked())
    with pytest.raises(ValidationError):
        LabelResult(section_key="s", status="skipped", labels=[], checked=_checked())


# ---------------------------------------------------------------------------
# 재생
# ---------------------------------------------------------------------------
def test_replay_reproduces_and_detects_drift(cfg, section):
    records = []
    first = label.run(section, BLOCKS, cfg, llm=FakeLlm(OK), recorder=records.append)
    replay = ReplayLabelAssistant(records[0], label.llm_config(cfg))
    again = label.run(section, BLOCKS, cfg, llm=replay)
    replay.finish()
    assert again == first
    # 보낸 블록 텍스트가 바뀌면 페이로드 불일치
    changed = [BLOCKS[0].model_copy(update={"source_ko": "다른 문구"})] + BLOCKS[1:]
    res = label.run(section, changed, cfg, llm=ReplayLabelAssistant(records[0], label.llm_config(cfg)))
    assert res.status == "failed" and "재생 입력 불일치" in res.error
    # 보내지 않는 공백 블록이 바뀌어도 입력 지문 불일치
    extra_blank = BLOCKS + [_block("blk_006", " ", 6, 2900)]
    res = label.run(section, extra_blank, cfg, llm=ReplayLabelAssistant(records[0], label.llm_config(cfg)))
    assert res.status == "failed" and "input_fingerprint" in res.error
    # 이미지가 바뀌면 불일치
    Image.new("RGB", (W, H), (0, 0, 0)).save(section.image_path)
    res = label.run(section, BLOCKS, cfg, llm=ReplayLabelAssistant(records[0], label.llm_config(cfg)))
    assert res.status == "failed" and "image_sha256" in res.error
    with pytest.raises(VlmReplayMismatch, match="모델 설정"):
        ReplayLabelAssistant(records[0], {**label.llm_config(cfg), "temperature": 1})


def test_replay_revalidates_response(cfg, section):
    records = []
    label.run(section, BLOCKS, cfg, llm=FakeLlm(OK), recorder=records.append)
    bad = {**records[0], "response_text": json.dumps({"labels": [{"id": "b1", "is_product_label": "false"}]})}
    res = label.run(section, BLOCKS, cfg, llm=ReplayLabelAssistant(bad, label.llm_config(cfg)))
    assert res.status == "failed" and "boolean" in res.error


def test_replay_of_failed_and_skipped_records(cfg, section):
    records = []
    label.run(section, BLOCKS, cfg, llm=FakeLlm(exc=VlmError("504")), recorder=records.append)
    res = label.run(section, BLOCKS, cfg, llm=ReplayLabelAssistant(records[0], label.llm_config(cfg)))
    assert res.status == "failed" and "기록된 호출 실패" in res.error
    records = []
    blanks = [BLOCKS[1]]
    label.run(section, blanks, cfg, recorder=records.append)
    replay = ReplayLabelAssistant(records[0], label.llm_config(cfg))
    again = label.run(section, blanks, cfg, llm=replay)
    replay.finish()
    assert again.status == "ok" and again.labels[0].basis == "blank_text"
    res = label.run(section, BLOCKS, cfg, llm=ReplayLabelAssistant(records[0], label.llm_config(cfg)))  # 이번엔 호출이 필요
    assert res.status == "failed" and "input_fingerprint" in res.error
