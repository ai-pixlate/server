import pytest

from pipeline import config as cfgmod


def test_default_config_matches_pipeline_md_keys():
    cfg = cfgmod.load_config()
    flat = cfgmod.flatten(cfg)
    assert flat["ocr.det_model"] == "PP-OCRv5_server_det"
    assert flat["ocr.split_threshold_px"] == 4000
    assert flat["merge.llm_split"] is False
    assert flat["merge.prompt_path"] == "pipeline/prompts/merge_assist.md"
    # ③ heuristic_v2 잠정 임계값(open-questions #38, pipeline.md 3절)
    assert flat["merge.line_v_overlap"] == 0.5
    assert flat["merge.line_gap_min"] == -0.5
    assert flat["merge.para_gap_min"] == -0.3
    assert flat["merge.left_align_tol"] == 0.5
    assert flat["merge.gutter_min_px"] == 20
    assert flat["merge.gutter_width_div"] == 16
    assert flat["merge.title_pct"] == 75
    assert flat["merge.caption_pct"] == 25
    assert flat["merge.title_max_lines"] == 2
    assert flat["merge.caption_max_chars"] == 25
    assert flat["merge.price_max_chars"] == 40
    assert flat["merge.llm_timeout_s"] == 60  # llm_assist 개발용 잠정값(open-questions #41 · #37)
    assert flat["inpaint.score_min"] == 0.5
    assert flat["translate.prompt_path"] == "pipeline/prompts/translate.md"
    assert flat["translate.temperature"] == 0 and flat["translate.timeout_s"] == 60  # ⑧ 개발용 잠정값(운영값 아님)
    assert flat["section.vlm_model"] == "gemini-3.8-flash"  # open-questions #21 확정 2026-09-21
    assert flat["section.prompt_path"] == "pipeline/prompts/section_boundary.md"
    assert flat["judge.match_mode"] == "substring"  # ③-1 설계 v1 잠정값(open-questions #60) — 실험 기준
    assert flat["policy.problem_text_max_chars"] == 300
    assert flat["label.long_side_px"] == 1024 and flat["label.bias"] == "label"  # 정본(개발계획 6장)
    assert flat["label.timeout_s"] == 60 and flat["label.temperature"] == 0  # ④ 단독 개발 기준(open-questions #66)


def test_override_parses_toml_literals_and_strings():
    cfg = cfgmod.load_config(overrides=["merge.line_gap=0.8", "ocr.preprocess=gray", "inpaint.require_text=false"])
    assert cfgmod.get(cfg, "merge.line_gap") == 0.8
    assert cfgmod.get(cfg, "ocr.preprocess") == "gray"
    assert cfgmod.get(cfg, "inpaint.require_text") is False


def test_override_rejects_unknown_key():
    with pytest.raises(cfgmod.ConfigKeyError):
        cfgmod.load_config(overrides=["merge.typo=1"])
    with pytest.raises(cfgmod.ConfigKeyError):
        cfgmod.load_config(overrides=["judge.model=x"])  # 없는 키(③-1 모델 키는 judge.llm_model)
    with pytest.raises(cfgmod.ConfigKeyError):
        cfgmod.load_config(overrides=["label.foo=1"])
