"""⑨ 조사 도구(docs/ai-experiments/tools/typeset_overflow_probe · style_section_align_review_page)의 배치 영역 해석 — PR #116 뒤 저장 형식.

도구는 보존된 text_block 행을 운영 렌더 입력과 같은 공통 해석(app.layout_adjust)으로 읽는다:
셀러 조정(user)만 적용, 자동 조정(system)은 미적용, 출처 불명 이전 값(legacy)·이전 보존본의 auto_adjust 는 이전 서버 규칙,
박스는 저장값 그대로, 글자 크기는 적용 크기 × 셀러 배율. 합성 보존본만 쓴다(회차 자료 불필요, 승인 폰트는 저장소에 있음).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app import typeset

TOOLS = Path(__file__).resolve().parents[1] / "docs" / "ai-experiments" / "tools"
BBOX = {"x": 40, "y": 120, "w": 520, "h": 64}
USER_BOX = {"x": 38.5, "y": 110.25, "w": 546.75, "h": 80.5}
LEGACY_BOX = {"x": 1, "y": 1, "w": 5, "h": 5}


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


probe = _load("typeset_overflow_probe")


def _v2(user=None, system=None, **extra):
    return {"schema_version": 2, "user": user, "system": system, **extra}


@pytest.mark.parametrize(("raw", "box", "scale"), [
    (None, BBOX, 1.0),
    (_v2(user={"layout_bbox": USER_BOX, "font_scale": 1.25}), USER_BOX, 1.25),  # 소수 박스 절삭 없음 · 셀러 배율 적용
    (_v2(user={"layout_bbox": None, "font_scale": 0.85}), BBOX, 0.85),  # 배율만 조정 → 원본 bbox
    (_v2(user={"layout_bbox": USER_BOX, "font_scale": None}), USER_BOX, 1.0),  # 박스만 조정
    (_v2(system={"font_scale": 0.9, "line_break_applied": True}), BBOX, 1.0),  # 자동 조정은 렌더에 미적용
    (_v2(legacy={"layout_bbox": LEGACY_BOX}), LEGACY_BOX, 1.0),  # 출처 불명 기존 박스는 이전 규칙대로 적용
    (_v2(legacy={"layout_bbox": LEGACY_BOX}, legacy_released=True), BBOX, 1.0),  # 셀러가 해제하면 원본 bbox
    (_v2(user={"layout_bbox": None, "font_scale": 1.1}, legacy={"layout_bbox": LEGACY_BOX}), LEGACY_BOX, 1.1),  # 배율만 저장 → legacy 박스 유지
    (json.dumps(_v2(user={"layout_bbox": USER_BOX, "font_scale": 1.25})), USER_BOX, 1.25),  # JSONB 문자열로 보존된 값
])
def test_layout_adjust_rows_follow_render_input(raw, box, scale):
    assert probe.applied_layout({"id": 1, "bbox": BBOX, "layout_adjust": raw}) == (box, scale)


@pytest.mark.parametrize(("raw", "box"), [
    (None, BBOX),
    ({"schema_version": "1", "layout_bbox": USER_BOX, "font_scale": 1.25, "updated_by": "user"}, USER_BOX),  # 배율은 이전 서버가 미적용
    ({"font_scale": 0.9, "line_break_applied": True}, BBOX),
])
def test_pre_0013_evidence_reads_auto_adjust_with_previous_server_rule(raw, box):
    """PR #116 전 보존본 — 그 회차를 그린 이전 서버의 규칙과 같다(배율 미적용)."""
    assert probe.applied_layout({"id": 1, "bbox": BBOX, "auto_adjust": raw}) == (box, 1.0)


@pytest.mark.parametrize("row", [
    {"id": 7, "bbox": BBOX, "layout_adjust": _v2(legacy={"layout_bbox": "0,0,5,5"})},
    {"id": 7, "bbox": BBOX, "layout_adjust": _v2(legacy=["layout_bbox"])},
    {"id": 7, "bbox": None, "layout_adjust": None},
    {"id": 7, "bbox": {"x": 1, "y": 1, "w": 5}, "auto_adjust": None},
])
def test_unreadable_box_stops_instead_of_filling(row):
    with pytest.raises(ValueError, match="RENDER_INPUT_INVALID"):
        probe.applied_layout(row)


def test_row_without_either_column_is_not_guessed():
    with pytest.raises(KeyError):
        probe.applied_layout({"id": 1, "bbox": BBOX})


def _evidence(blocks):
    sec = {"id": 10, "bbox": {"x": 0, "y": 0, "w": 860, "h": 400}}
    tasks = [{"id": 1, "stage": "style", "unit_id": 10, "status": "failed", "is_current": True}]
    rows = [{"id": i, "section_id": 10, "role": "body", "is_excluded": False, "source_ko": "원문", "trans_1": "Translated text",
             "source_lines": [{}], "style": None, "overflow": False, "bbox": BBOX, "layout_adjust": raw}
            for i, raw in blocks]
    return {"section": [sec], "job_async_task": tasks, "task_handoff": [], "text_block": rows}


def test_probe_collect_uses_applied_box_and_scaled_font(tmp_path):
    (tmp_path / "evidence").mkdir()
    adjusted = _v2(user={"layout_bbox": USER_BOX, "font_scale": 1.25})
    (tmp_path / "evidence" / "GS-01.json").write_text(json.dumps(_evidence([(1, None), (2, adjusted)])), encoding="utf-8")
    (tmp_path / "evidence" / "GS-02.json").write_text(json.dumps(_evidence([])), encoding="utf-8")
    rows = {r["id"]: r for r in probe.collect(tmp_path)}

    base = float(typeset.resolve_style(typeset.RenderBlock(id=1, role="body", text="x", box=BBOX, style=None, style_stage="failed",
                                                           source_ko="원문"), typeset.load_role_defaults(), 860)[0]["est_font_px"])
    assert rows[1]["box"] == BBOX and rows[1]["font_px"] == base and "font_scale" not in rows[1]  # 조정 없는 블록의 행은 그대로
    assert rows[2]["box"] == USER_BOX and rows[2]["font_px"] == base * 1.25 and rows[2]["font_scale"] == 1.25
    assert rows[2]["one_line_h_ratio"] == base * 1.25 * typeset.LINE_SPACING / USER_BOX["h"]
