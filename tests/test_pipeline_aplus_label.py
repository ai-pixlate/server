"""A+ 섹션 내용 라벨 — 응답 검증 · 전체 실패 · 입력 오류 · 기록 · 라벨→칸 후보(가짜 호출자, 실제 API 호출 없음).

개발용 v1(open-questions.md 2.9절): 보낸 섹션마다 정확히 하나의 분류 · 누락/중복/미등록 ID/모르는 키/모르는 라벨은 실패 ·
실패는 전체 실패(labels=None)이며 OTHER나 부분 성공으로 바꾸지 않는다.
"""
import json

import pytest
from PIL import Image
from pydantic import ValidationError

from pipeline import config as cfgmod
from pipeline.aplus import label
from pipeline.aplus.spec import load_spec
from pipeline.types import AplusLabelChecked, AplusLabelResult, AplusSectionLabel, BBox, Line, OcrRegion, Section, TextBlock
from pipeline.vlm import LlmReply, VlmError, section_label_response_schema

SPEC = load_spec()
CODES = tuple(SPEC.raw["labels"])


def _block(key: str, text: str, order: int, section: str) -> TextBlock:
    box = BBox(x=10, y=10 * order, w=100, h=8)
    reg = OcrRegion(region_key=f"reg_{order:04d}", text=text, score=0.9, poly=[[10, 10], [110, 10], [110, 18], [10, 18]], bbox=box)
    return TextBlock(block_key=key, section_key=section, block_order=order, source_ko=text,
                     source_lines=[Line(line_key=f"line_{order:03d}", text=text, bbox=box, regions=[reg])], bbox=box, role="body", ocr_confidence=0.9)


@pytest.fixture
def sections(tmp_path):
    out = []
    texts = [["수분 크림", "하루 종일 촉촉하게"], ["72시간 보습", "진정 케어"], []]
    for i, lines in enumerate(texts, start=1):
        key = f"sec_1_{i:02d}"
        p = tmp_path / f"{key}.png"
        Image.new("RGB", (200, 2000 if i == 2 else 300), (250 - i, 250, 250)).save(p)
        sec = Section(section_key=key, source_image_id=1, section_order=i, top_offset=0, height=2000 if i == 2 else 300, width=200, image_path=str(p))
        out.append((sec, [_block(f"blk_{i}{j:02d}", t, j, key) for j, t in enumerate(lines, start=1)]))
    return out


class Fake:
    config = {"model": "fake"}

    def __init__(self, reply: str | Exception) -> None:
        self.reply, self.calls = reply, []

    def __call__(self, prompt, payload, images):
        self.calls.append((prompt, payload, images))
        if isinstance(self.reply, Exception):
            raise self.reply
        return LlmReply(text=self.reply, usage={"total_token_count": 10})


def _answer(*items) -> str:
    return json.dumps({"sections": [{"id": i, "label": lab, "sub": sub, "confidence": "high", "reason": "이유"} for i, lab, sub in items]},
                      ensure_ascii=False)


OK = _answer(("s1", "HERO", []), ("s2", "BENEFIT", ["CERT"]), ("s3", "LOGO", []))


def test_run_ok_sends_all_sections_once(sections):
    cfg = cfgmod.load_config()
    fake, recs = Fake(OK), []
    res = label.run(sections, SPEC, cfg, llm=fake, recorder=recs.append)
    assert res.status == "ok" and [(x.section_key, x.label, x.sub) for x in res.labels] == [
        ("sec_1_01", "HERO", []), ("sec_1_02", "BENEFIT", ["CERT"]), ("sec_1_03", "LOGO", [])]
    assert res.checked.sent_section_keys == ["sec_1_01", "sec_1_02", "sec_1_03"] and res.checked.spec_version == SPEC.version
    assert len(fake.calls) == 1
    _, payload, images = fake.calls[0]
    sent = json.loads(payload)
    assert sent == {"sections": [{"id": "s1", "text": "수분 크림\n하루 종일 촉촉하게"}, {"id": "s2", "text": "72시간 보습\n진정 케어"},
                                 {"id": "s3", "text": ""}]}  # 글 없는 섹션도 이미지로 판정하므로 보낸다
    assert [sid for sid, _ in images] == ["s1", "s2", "s3"]
    assert images[1][1].size == (102, 1024) and images[0][1].size == (200, 300)  # 긴 변 1024로만 줄인다
    rec = recs[0]
    assert rec["status"] == "ok" and rec["validation"] == {"ok": True, "reasons": []} and rec["sections"]["s2"] == "sec_1_02"
    assert rec["images"]["s2"]["sent_size"] == [102, 1024] and rec["spec_sha256"] == SPEC.sha256


@pytest.mark.parametrize("reply, word", [
    ("not json", "JSON 아님"),
    (json.dumps({"labels": []}), "sections 배열"),
    (_answer(("s1", "HERO", []), ("s2", "BENEFIT", [])), "분류 누락 ['s3']"),
    (_answer(("s1", "HERO", []), ("s1", "HERO", []), ("s2", "BENEFIT", []), ("s3", "LOGO", [])), "s1 중복 분류"),
    (_answer(("s1", "HERO", []), ("s2", "BENEFIT", []), ("s3", "LOGO", []), ("s4", "FAQ", [])), "미등록 섹션 ID 's4'"),
    (_answer(("s1", "REVIEW", []), ("s2", "BENEFIT", []), ("s3", "LOGO", [])), "모르는 라벨 'REVIEW'"),
    (_answer(("s1", "HERO", ["HERO"]), ("s2", "BENEFIT", []), ("s3", "LOGO", [])), "주 라벨과 다른 코드"),
    (_answer(("s1", "HERO", ["CERT", "PROOF", "BRAND"]), ("s2", "BENEFIT", []), ("s3", "LOGO", [])), "0~2개"),
    (OK.replace('"confidence": "high"', '"confidence": "sure"', 1), "confidence 'sure'"),
    (OK.replace('"reason": "이유"', '"reason": " "', 1), "reason이 비어 있다"),
    (OK.replace('"reason": "이유"', '"reason": "이유", "summary": "요약"', 1), "모르는 키 ['summary']"),
    (OK.replace('"sub": []', '"sub": [], "label": "FAQ"', 1), "중복 키"),
])
def test_invalid_response_fails_whole_result(sections, reply, word):
    recs = []
    res = label.run(sections, SPEC, cfgmod.load_config(), llm=Fake(reply), recorder=recs.append)
    assert res.status == "failed" and res.labels is None and word in res.error
    assert recs[0]["status"] == "validation_failed" and recs[0]["response_text"] == reply


def test_call_failure_is_result_not_exception(sections):
    recs = []
    res = label.run(sections, SPEC, cfgmod.load_config(), llm=Fake(VlmError("시간 초과")), recorder=recs.append)
    assert res.status == "failed" and "시간 초과" in res.error and recs[0]["status"] == "call_failed"


def test_input_errors_raise(sections, tmp_path):
    cfg = cfgmod.load_config()
    with pytest.raises(ValueError, match="섹션이 없다"):
        label.run([], SPEC, cfg, llm=Fake(OK))
    with pytest.raises(ValueError, match="두 번"):
        label.run([sections[0], sections[0]], SPEC, cfg, llm=Fake(OK))
    stray = (sections[0][0], sections[1][1])
    with pytest.raises(ValueError, match="section_key가 섹션"):
        label.run([stray], SPEC, cfg, llm=Fake(OK))
    wrong = sections[0][0].model_copy(update={"height": 999})
    with pytest.raises(ValueError, match="메타데이터"):
        label.run([(wrong, [])], SPEC, cfg, llm=Fake(OK))


def test_config_validation(monkeypatch):
    cfg = cfgmod.load_config()
    assert "라벨 19개" in label.validate_config(cfg, need_api_key=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        label.validate_config(cfg, need_api_key=True)
    bad = cfgmod.load_config(overrides=["aplus_label.long_side_px=10", "aplus_label.temperature=3"])
    with pytest.raises(ValueError) as e:
        label.validate_config(bad, need_api_key=False)
    assert "long_side_px" in str(e.value) and "temperature" in str(e.value)
    flat = cfgmod.flatten(cfg)
    assert flat["aplus_label.prompt_path"] == "pipeline/prompts/aplus_label.md" and flat["aplus_label.long_side_px"] == 1024


def test_prompt_lists_exactly_the_spec_labels():
    """프롬프트의 라벨 표와 규격 데이터의 라벨 코드가 같아야 한다(코드 목록의 정본은 spec.json)."""
    prompt = label.validate_config(cfgmod.load_config(), need_api_key=False)
    rows = [ln.split("|")[1].strip() for ln in prompt.splitlines() if ln.startswith("| ") and ln.split("|")[1].strip().isupper()]
    assert rows == list(CODES)
    schema = section_label_response_schema(list(CODES))
    assert schema["properties"]["sections"]["items"]["properties"]["label"]["enum"] == list(CODES)


def test_slot_candidates_use_main_label_only():
    def L(key, lab, sub=()):
        return AplusSectionLabel(section_key=key, label=lab, sub=list(sub), confidence="high", reason="r")

    labels = [L("a", "HERO"), L("b", "BENEFIT", ["CERT"]), L("c", "PROOF"), L("d", "CERT"), L("e", "SOCIAL"), L("f", "FEATURE"),
              L("g", "HOWTO"), L("h", "BENEFIT"), L("i", "FAQ"), L("j", "LINEUP")]
    assert label.slot_candidates(labels, SPEC) == {1: ["a"], 2: ["b", "h"], 3: [], 4: ["c", "d"], 5: ["j"], 6: ["g"], 7: []}


def test_result_types_enforce_status_and_order():
    chk = AplusLabelChecked(input_fingerprint="f", spec_version="v", llm_called=True, sent_section_keys=["a", "b"])
    one = AplusSectionLabel(section_key="a", label="HERO", confidence="high", reason="r")
    with pytest.raises(ValidationError):
        AplusLabelResult(status="ok", labels=[one], checked=chk)  # 보낸 섹션과 집합이 다르다
    with pytest.raises(ValidationError):
        AplusLabelResult(status="failed", labels=None, checked=chk)  # 실패에는 사유가 있어야 한다
    with pytest.raises(ValidationError):
        AplusSectionLabel(section_key="a", label="HERO", sub=["HERO"], confidence="high", reason="r")
    assert AplusLabelResult(status="failed", labels=None, checked=chk, error="x").labels is None
