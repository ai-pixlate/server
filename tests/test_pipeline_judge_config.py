"""③-1 · ③-1' 설정 검증 · 사전 로드 · 뼈대(run 미구현) — 개발용 잠정 config(open-questions #60)."""
import copy

import pytest

from pipeline import config as cfgmod
from pipeline.stages import judge, policy
from pipeline.types import BBox, JudgeContext, Line, OcrRegion, Section, TextBlock

SYNTH_DIR = "pipeline/data/dict/synthetic"


@pytest.fixture
def cfg():
    return cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH_DIR}"])


def test_default_judge_and_policy_keys_are_experimental_values():
    flat = cfgmod.flatten(cfgmod.load_config())
    assert flat["judge.call_scope"] == "all"  # D3-2 실측 기본
    assert flat["judge.image_width_px"] == 768  # D4
    assert flat["judge.context_sections"] == 1  # D5
    assert flat["judge.match_mode"] == "substring"  # D1 실험 기준
    assert flat["judge.dict_dir"] == "pipeline/samples/local/dict/normalized"  # 실제 값은 git 밖(D8)
    assert flat["judge.prompt_path"] == "pipeline/prompts/judge_context.md"
    assert flat["judge.llm_timeout_s"] == 60
    assert flat["policy.problem_text_max_chars"] == 300


def test_validate_config_accepts_defaults(cfg):
    judge.validate_config(cfg)
    policy.validate_config(cfg)


@pytest.mark.parametrize(
    "key, value, msg",
    [
        ("judge.call_scope", "'some'", "call_scope"),
        ("judge.image_width_px", "32", "image_width_px"),
        ("judge.image_width_px", "768.0", "image_width_px"),
        ("judge.image_width_px", "true", "image_width_px"),
        ("judge.context_sections", "-1", "context_sections"),
        ("judge.match_mode", "'regex'", "match_mode"),
        ("judge.dict_dir", "''", "dict_dir"),
    ],
)
def test_validate_config_rejects_bad_values(key, value, msg):
    cfg = cfgmod.load_config(overrides=[f"{key}={value}"])
    with pytest.raises(ValueError, match=msg):
        judge.validate_config(cfg)


def test_validate_config_reports_missing_keys(cfg):
    c = copy.deepcopy(cfg)
    del c["judge"]["match_mode"]
    with pytest.raises(ValueError, match="judge.match_mode"):
        judge.validate_config(c)
    c2 = copy.deepcopy(cfg)
    del c2["policy"]
    with pytest.raises(ValueError, match=r"\[policy\]"):
        policy.validate_config(c2)


@pytest.mark.parametrize("value", ["0", "1.5", "false"])
def test_policy_validate_rejects_bad_problem_text_limit(value):
    cfg = cfgmod.load_config(overrides=[f"policy.problem_text_max_chars={value}"])
    with pytest.raises(ValueError, match="problem_text_max_chars"):
        policy.validate_config(cfg)


def test_llm_config_requires_prompt_file_and_key(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv(judge.API_KEY_ENV, raising=False)
    assert judge.validate_llm_config(cfg, need_api_key=False).strip()  # 기본 prompt_path = 초안 pipeline/prompts/judge_context.md
    missing = cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH_DIR}", "judge.prompt_path='pipeline/prompts/does-not-exist.md'"])
    with pytest.raises(ValueError, match="prompt_path"):
        judge.validate_llm_config(missing, need_api_key=False)
    p = tmp_path / "prompt.md"
    p.write_text("판정 프롬프트", encoding="utf-8")
    c = cfgmod.load_config(overrides=[f"judge.dict_dir={SYNTH_DIR}", f"judge.prompt_path='{p.as_posix()}'"])
    assert judge.validate_llm_config(c, need_api_key=False) == "판정 프롬프트"
    with pytest.raises(ValueError, match=judge.API_KEY_ENV):
        judge.validate_llm_config(c, need_api_key=True)
    monkeypatch.setenv(judge.API_KEY_ENV, "k")
    judge.validate_llm_config(c, need_api_key=True)
    for k, v in (("judge.llm_temperature", "3"), ("judge.llm_timeout_s", "0"), ("judge.llm_model", "''")):
        bad = cfgmod.load_config(overrides=[f"judge.prompt_path='{p.as_posix()}'", f"{k}={v}"])
        with pytest.raises(ValueError, match=k.split(".")[1]):
            judge.validate_llm_config(bad, need_api_key=False)


def test_load_dicts_uses_repo_relative_dict_dir(cfg):
    dicts = judge.load_dicts(cfg)
    assert dicts.dir.is_absolute() and dicts.dir.name == "synthetic"
    assert dicts.dictionary_version["rules"] == "policy@2026-09-28.0"
    missing = cfgmod.load_config(overrides=["judge.dict_dir=pipeline/data/dict/does-not-exist"])
    with pytest.raises(Exception, match="사전 파일이 없다"):
        judge.load_dicts(missing)


def _section_and_block():
    sec = Section(section_key="sec_1_01", source_image_id=1, section_order=1, top_offset=0, height=100, width=100, image_path="x.png")
    reg = OcrRegion(region_key="reg_0001", text="t", score=0.9, poly=[[0, 0], [10, 0], [10, 10], [0, 10]], bbox=BBox(x=0, y=0, w=10, h=10))
    blk = TextBlock(block_key="blk_001", section_key="sec_1_01", block_order=1, source_ko="t",
                    source_lines=[Line(line_key="line_001", text="t", bbox=BBox(x=0, y=0, w=10, h=10), regions=[reg])],
                    bbox=BBox(x=0, y=0, w=10, h=10), role="body", ocr_confidence=0.9)
    return sec, [blk]


def test_run_validates_config_first(cfg):
    sec, blocks = _section_and_block()
    bad = cfgmod.load_config(overrides=["judge.match_mode='regex'"])
    with pytest.raises(ValueError, match="match_mode"):  # 설정 검증이 사전 로드 · 검출보다 먼저다
        judge.run(sec, blocks, JudgeContext(), bad)
    with pytest.raises(ValueError, match="problem_text_max_chars"):
        policy.run(None, blocks, JudgeContext(), cfgmod.load_config(overrides=["policy.problem_text_max_chars=0"]), dicts=None)
