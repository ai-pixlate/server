import pytest

from pipeline import config as cfgmod


def test_default_config_matches_pipeline_md_keys():
    cfg = cfgmod.load_config()
    flat = cfgmod.flatten(cfg)
    assert flat["ocr.det_model"] == "PP-OCRv5_server_det"
    assert flat["ocr.split_threshold_px"] == 4000
    assert flat["merge.llm_split"] is False
    assert flat["inpaint.score_min"] == 0.5
    assert flat["translate.prompt_path"] == "pipeline/prompts/translate.md"
    assert flat["section.vlm_model"] == "gemini-3.8-flash"  # open-questions #21 확정 2026-09-21
    assert flat["section.prompt_path"] == "pipeline/prompts/section_boundary.md"
    assert not any(k.startswith("judge.") for k in flat)  # ③-1 미정


def test_override_parses_toml_literals_and_strings():
    cfg = cfgmod.load_config(overrides=["merge.line_gap=0.8", "ocr.preprocess=gray", "inpaint.require_text=false"])
    assert cfgmod.get(cfg, "merge.line_gap") == 0.8
    assert cfgmod.get(cfg, "ocr.preprocess") == "gray"
    assert cfgmod.get(cfg, "inpaint.require_text") is False


def test_override_rejects_unknown_key():
    with pytest.raises(cfgmod.ConfigKeyError):
        cfgmod.load_config(overrides=["merge.typo=1"])
    with pytest.raises(cfgmod.ConfigKeyError):
        cfgmod.load_config(overrides=["judge.model=x"])  # ③-1 미정 키
