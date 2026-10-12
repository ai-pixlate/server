"""역할별 스타일 기본값 — 설정 자료 · 폰트 자료 · 적용값 계산 · 참고 렌더(D11). DB · BE 연결 없음."""
from __future__ import annotations

import copy
import io
import json
import math
import shutil
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pipeline.data.fonts.tools import fetch_fonts
from pipeline.handoff import style_defaults as sd
from pipeline.handoff import style_reference as ref

REPO = Path(__file__).resolve().parents[1]
CASES = json.loads((REPO / "pipeline" / "samples" / "style_defaults" / "cases.json").read_text(encoding="utf-8"))
TOL = 1e-9

# 사용자 승인표(2026-10-12)를 테스트에 따로 적어 설정 자료와 대조한다: 역할 → (토큰, 기준 px, 굵기, 자간 %)
APPROVED = {
    "title": ("Heading / H2", 32, "SemiBold", -1.5),
    "body": ("Body / L", 16, "Regular", -3),
    "caption": ("Body / M", 14, "Regular", -1),
    "price": ("Heading / H3", 24, "SemiBold", -2),
    "caution": ("Body / L", 16, "Regular", -3),
}


@pytest.fixture(scope="module")
def d() -> sd.StyleDefaults:
    return sd.load_style_defaults()


def _raw() -> dict:
    return json.loads(sd.STYLE_DEFAULTS_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict, name: str = "style_defaults.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return p


def _fonts_placed(d: sd.StyleDefaults) -> bool:
    return all(sd.font_path(d, f).is_file() for f in d.data.fonts.faces)


needs_fonts = pytest.mark.skipif(not _fonts_placed(sd.load_style_defaults()),
                                 reason="폰트 미배치 — python -m pipeline.data.fonts.tools.fetch_fonts")


# ---------------------------------------------------------------------------
# 설정 자료
# ---------------------------------------------------------------------------
def test_approved_table_matches_config(d):
    m = d.data.pipeline_mapping
    assert (m.font_family, m.reference_width_px, m.font_color, m.align, m.line_height_ratio) == ("Pretendard", 375, "#222222", "left", 1.2)
    for role, (token, px, weight, pct) in APPROVED.items():
        s = sd.role_spec(d, role)
        assert (s.type_token, s.base_px, s.face, s.letter_spacing_pct) == (token, px, weight, pct)
    assert {f: d.data.fonts.faces[f].weight for f in d.data.fonts.faces} == {"Regular": 400, "SemiBold": 600}


def test_provenance_is_separated(d):
    assert d.data.type_tokens.source == "design_image"
    assert d.data.pipeline_mapping.source == "user_approved"
    assert d.data.approval.approved_by == "user" and d.data.approval.pm_reply_recorded is False
    # 역할 대응 · 보완값은 매핑 쪽에만, 토큰 값은 토큰 쪽에만 있다
    assert "roles" not in d.data.type_tokens.model_dump() and "tokens" not in d.data.pipeline_mapping.model_dump()


@pytest.mark.parametrize("mutate, msg", [
    (lambda r: r["pipeline_mapping"]["roles"].pop("caution"), "5종"),
    (lambda r: r["pipeline_mapping"]["roles"].update(label="Body / M"), "5종"),
    (lambda r: r["pipeline_mapping"]["roles"].update(title="Heading / H1"), "type_tokens에 없다"),
    (lambda r: r["fonts"]["faces"].pop("SemiBold"), "폰트 파일"),
    (lambda r: r["pipeline_mapping"].update(font_color="#222"), "font_color"),
    (lambda r: r["pipeline_mapping"].update(reference_width_px=0), "reference_width_px"),
    (lambda r: r["pipeline_mapping"].update(bg_color="#FFFFFF"), "bg_color"),
    (lambda r: r["fonts"]["faces"]["Regular"].update(file="/abs/Pretendard-Regular.otf"), "상대 경로"),
    (lambda r: r["fonts"]["faces"]["Regular"].update(file="../x.otf"), "상대 경로"),
    (lambda r: r["type_tokens"].update(source="user_approved"), "source"),
])
def test_invalid_config_rejected(tmp_path, mutate, msg):
    raw = _raw()
    mutate(raw)
    with pytest.raises(sd.StyleDefaultsError, match=msg):
        sd.load_style_defaults(_write(tmp_path, raw))


def test_render_inputs_track_config_fonts_and_rule(d, tmp_path):
    ri = sd.render_inputs(d)
    assert ri == {"style_defaults": {"version": d.version, "sha256": d.sha256},
                  "fonts": {"Regular": d.data.fonts.faces["Regular"].sha256, "SemiBold": d.data.fonts.faces["SemiBold"].sha256},
                  "rule": sd.RULE_VERSION}
    raw = _raw()
    raw["pipeline_mapping"]["font_color"] = "#111111"
    other = sd.load_style_defaults(_write(tmp_path, raw))
    assert sd.render_inputs_sha256(other) != sd.render_inputs_sha256(d)


# ---------------------------------------------------------------------------
# 환산
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("width, role, want", [
    (375, "title", 32), (375, "caption", 14), (750, "body", 32), (1080, "title", 92.16), (1080, "price", 69.12),
    (860, "body", 13760 / 375), (1, "caption", 14 / 375),
])
def test_default_size_is_base_times_width_over_375(d, width, role, want):
    assert math.isclose(sd.default_font_px(d, role, width), want, abs_tol=TOL)


@pytest.mark.parametrize("width", [0, -375, 375.0, "375", True, None])
def test_invalid_source_width(d, width):
    with pytest.raises(sd.StyleInputInvalid):
        sd.default_font_px(d, "body", width)


# ---------------------------------------------------------------------------
# BE용 사례 — 손 계산 기대값과 대조
# ---------------------------------------------------------------------------
def _block(b: dict) -> sd.BlockInput:
    return ref.to_block_input(b)


def _close(got, want, path=""):
    if isinstance(want, dict):
        assert isinstance(got, dict) and set(got) == set(want), f"{path}: 키 {sorted(got or {})} ≠ {sorted(want)}"
        for k in want:
            _close(got[k], want[k], f"{path}.{k}")
    elif isinstance(want, (int, float)) and not isinstance(want, bool):
        assert isinstance(got, (int, float)) and not isinstance(got, bool) and math.isclose(got, want, abs_tol=TOL), f"{path}: {got} ≠ {want}"
    else:
        assert got == want, f"{path}: {got!r} ≠ {want!r}"


CASE_BLOCKS = [(c["id"], b) for c in CASES["cases"] for b in c["input"]["blocks"]]


@pytest.mark.parametrize("case_id, block", CASE_BLOCKS, ids=[f"{c}-{b['id']}" for c, b in CASE_BLOCKS])
def test_cases_match_hand_computed_expectation(d, case_id, block):
    case = next(c for c in CASES["cases"] if c["id"] == case_id)
    want = case["expected"][str(block["id"])]
    r = sd.resolve_block(_block(block), case["input"]["source_width_px"], d)
    assert r.render is want["render"] and r.skip_reason == want["skip_reason"]
    _close(r.applied, want["applied"], "applied")
    _close(r.source, want["source"], "source")
    # 측정값 · 단계 상태 · 정렬 근거는 그대로 남고 적용값과 섞이지 않는다
    assert r.measured == block["style"] and r.style_stage == block["style_stage"]
    assert r.measured_align_basis == block.get("align_basis")
    if r.applied:
        assert "bg_color" not in r.applied


def test_cases_cover_required_scenarios():
    ids = [c["id"] for c in CASES["cases"]]
    for prefix in ("01_", "02_", "03_", "04_", "05_", "06", "07_", "08_"):
        assert any(i.startswith(prefix) for i in ids), prefix
    assert {c["input"]["source_width_px"] for c in CASES["cases"]} >= {375, 860, 1080}
    assert CASES["defaults_version"] == sd.load_style_defaults().version and CASES["rule"] == sd.RULE_VERSION


def test_measured_size_never_rescaled(d):
    style = {"font_color": "#000000", "bg_color": None, "est_font_px": 37.25, "align": "left"}
    for w in (375, 860, 1080, 3000):
        r = sd.resolve_block(sd.BlockInput(1, "body", False, "원문", "Text", style, "done"), w, d)
        assert r.applied["font_size_px"] == 37.25 and r.source["font_size_px"] == "measured"


def test_failed_stage_stays_failed_and_does_not_fake_measurement(d):
    r = sd.resolve_block(sd.BlockInput(1, "title", False, "원문", "Text", None, "failed"), 1080, d)
    assert r.style_stage == "failed" and r.measured is None
    assert {r.source[k] for k in ("font_size_px", "font_color", "align")} == {"role_default"}


@pytest.mark.parametrize("style, stage, msg", [
    ({"font_color": "#000000", "bg_color": None, "est_font_px": 20, "align": "left"}, "failed", "이전 결과"),
    (None, "done", "style 객체가 없다"),
    ({"font_color": "#000000", "bg_color": None, "est_font_px": 20}, "done", "4개"),
    ({"font_color": "#abcdef", "bg_color": None, "est_font_px": 20, "align": "left"}, "done", "font_color"),
    ({"font_color": "#000000", "bg_color": "red", "est_font_px": 20, "align": "left"}, "done", "bg_color"),
    ({"font_color": "#000000", "bg_color": None, "est_font_px": 0, "align": "left"}, "done", "est_font_px"),
    ({"font_color": "#000000", "bg_color": None, "est_font_px": float("nan"), "align": "left"}, "done", "est_font_px"),
    ({"font_color": "#000000", "bg_color": None, "est_font_px": True, "align": "left"}, "done", "est_font_px"),
    ({"font_color": "#000000", "bg_color": None, "est_font_px": "20", "align": "left"}, "done", "est_font_px"),
    ({"font_color": "#000000", "bg_color": None, "est_font_px": 20, "align": "justify"}, "done", "align"),
    ({"font_color": "#000000", "bg_color": None, "est_font_px": 20, "align": "left"}, "skipped", "style_stage"),
])
def test_contract_violations_are_rejected_not_defaulted(d, style, stage, msg):
    with pytest.raises(sd.StyleInputInvalid, match=msg):
        sd.resolve_block(sd.BlockInput(1, "body", False, "원문", "Text", style, stage), 1080, d)


def test_unknown_role_rejected(d):
    with pytest.raises(sd.StyleInputInvalid, match="역할"):
        sd.resolve_block(sd.BlockInput(1, "label", False, "원문", "Text", None, "failed"), 1080, d)


# ---------------------------------------------------------------------------
# 폰트 자료
# ---------------------------------------------------------------------------
@needs_fonts
def test_placed_fonts_match_config_and_metadata(d):
    metas = sd.verify_fonts(d)
    assert {f: (m.family, m.subfamily, m.weight_class, m.outline) for f, m in metas.items()} == {
        "Regular": ("Pretendard", "Regular", 400, "CFF"), "SemiBold": ("Pretendard", "SemiBold", 600, "CFF")}


@needs_fonts
def test_weight_comes_from_metadata_not_filename(d, tmp_path):
    """Regular 파일을 SemiBold 이름 · 해시로 등록하면 OS/2 굵기(400≠600)로 거부한다."""
    raw = _raw()
    reg = raw["fonts"]["faces"]["Regular"]
    (tmp_path / "fonts" / "pretendard").mkdir(parents=True)
    shutil.copyfile(sd.font_path(d, "Regular"), tmp_path / "fonts" / "pretendard" / "Pretendard-SemiBold.otf")
    shutil.copyfile(sd.font_path(d, "Regular"), tmp_path / "fonts" / "pretendard" / "Pretendard-Regular.otf")
    shutil.copyfile(d.base_dir / d.data.fonts.package.license_file, tmp_path / "fonts" / "pretendard" / "LICENSE.txt")
    raw["fonts"]["faces"]["SemiBold"].update(bytes=reg["bytes"], sha256=reg["sha256"])
    fake = sd.load_style_defaults(_write(tmp_path, raw))
    with pytest.raises(sd.FontAssetError, match="usWeightClass 400 ≠ 설정 600"):
        sd.verify_fonts(fake)


def test_missing_or_altered_font_is_error_not_fallback(tmp_path):
    raw = _raw()
    p = _write(tmp_path, raw)
    with pytest.raises(sd.FontAssetError, match="폰트 파일 없음"):
        sd.verify_fonts(sd.load_style_defaults(p))
    (tmp_path / "fonts" / "pretendard").mkdir(parents=True)
    for f in raw["fonts"]["faces"].values():
        (tmp_path / f["file"]).write_bytes(b"OTTO" + b"\0" * 32)
    with pytest.raises(sd.FontAssetError, match="SHA-256 불일치"):
        sd.verify_fonts(sd.load_style_defaults(p))


def test_fetch_refuses_zip_with_other_hash(tmp_path):
    raw = _raw()
    p = _write(tmp_path, raw)
    z = tmp_path / "fake.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(raw["fonts"]["package"]["license_member"], "not the license")
    with pytest.raises(sd.FontAssetError, match="공식"):
        fetch_fonts.place_from_zip(sd.load_style_defaults(p), z)
    assert not (tmp_path / "fonts").exists()  # 아무것도 쓰지 않는다


@needs_fonts
def test_fetch_check_mode_passes(capsys):
    assert fetch_fonts.main(["--check"]) == 0
    assert "usWeightClass 600" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 참고 렌더(Pillow) — 운영 렌더 아님
# ---------------------------------------------------------------------------
@needs_fonts
def test_reference_layout_matches_case_expectations(d):
    case = next(c for c in CASES["cases"] if c["id"] == "08_wrap_overflow")
    _, out = ref.run_case(case, d)
    checked = 0
    for bid, want in case["expected"].items():
        if "layout" not in want:
            continue
        got = out[bid]["layout"]
        assert {k: got[k] for k in want["layout"]} == want["layout"], bid
        checked += 1
    assert checked == 4


@needs_fonts
def test_reference_layout_never_shrinks_or_truncates(d):
    case = next(c for c in CASES["cases"] if c["id"] == "08_wrap_overflow")
    _, out = ref.run_case(case, d)
    for b in case["input"]["blocks"]:
        rec = out[str(b["id"])]
        assert rec["applied"]["font_size_px"] == b["style"]["est_font_px"]  # 넘쳐도 자동 축소 없음
        assert " ".join(rec["layout"]["lines"]).split() == b["text"].split()  # 문구 축약 없음


@needs_fonts
def test_reference_render_keeps_source_size(d):
    for case in CASES["cases"]:
        img, _ = ref.run_case(case, d)
        assert img.size == (case["input"]["source_width_px"], case["input"]["section_height_px"])


@needs_fonts
def test_reference_cli_writes_pngs_and_layout(tmp_path):
    assert ref.main(["--out", str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "layout.json").read_text(encoding="utf-8"))
    assert summary["render_inputs"]["rule"] == sd.RULE_VERSION and set(summary["cases"]) == {c["id"] for c in CASES["cases"]}
    with Image.open(io.BytesIO((tmp_path / "04_style_failed.png").read_bytes())) as im:
        assert im.size == (1080, 480)


def test_cases_json_has_no_engine_sensitive_expectation():
    """804(여러 줄 감기)는 줄 구성이 엔진 폭에 민감해 기대 layout을 두지 않는다(참고 렌더에서 한 줄이 1px 차로 갈린다)."""
    case = next(c for c in CASES["cases"] if c["id"] == "08_wrap_overflow")
    assert "layout" not in case["expected"]["804"] and copy.deepcopy(case)["note"]
