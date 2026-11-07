"""블록 배치 저장(계약 blockPatch 「블록 배치」) — 실제 PostgreSQL + 메모리 저장소로 API 를 호출한다.

처리 분기 넷, 정규화, 업무 검증, 멱등, 렌더 입력·넘침·최신성, 최종 렌더, content.csv, 이전 값 변환을 확인한다.
렌더러는 현행 Pillow 다. FE 공통 렌더와의 줄 바꿈 일치는 여기서 확인하지 않는다.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json

import pytest
from alembic import command
from PIL import Image, ImageChops
from sqlalchemy import create_engine, text

from app import edit_idempotency as idem
from app import layout_adjust as la
from app import typeset
from tests.exec_db import ROOT, alembic_config, fresh_database, requires_db, seed_job
from tests.test_exec_downstream import _cur_preview, _edit_signals, _pv, _pvs, _q, _text_block, _to_n5, dburl, env  # noqa: F401
from tests.test_exec_units import _blk, _defaults, png_bytes

pytestmark = requires_db


def _patch(c, job, bid, body, key=None):
    return c.patch(f"/v1/jobs/{job}/blocks/{bid}", json=body, headers={"Idempotency-Key": key} if key else None)


def _block(c, job, bid):
    return [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == bid][0]


def _setup(env):
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    s = _pvs(c, job, b["sectionId"])["renderInput"]["section"]
    return c, job, b, s["width"], s["height"]


def _body(b, **kw):
    return {"trans1": b["trans1"], "revision": b["revision"], **kw}


def _attempts(sid):
    return _q("SELECT count(*) AS n FROM job_async_task WHERE stage = 'preview' AND unit_id = :s", s=sid)[0]["n"]


def _box_inside(b, w, h):
    return {"x": 1.5, "y": 2.25, "w": min(w - 2, b["bbox"]["w"] + 1), "h": min(h - 3, b["bbox"]["h"] + 1)}


# ── 처리 분기 넷 ───────────────────────────────────────────────────────────────────────────────
def test_translation_only_change(env):
    c, job, b, _, _ = _setup(env)
    n = _attempts(b["sectionId"])
    r = _patch(c, job, b["id"], {"trans1": "Changed text", "revision": b["revision"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["block"]["revision"] == b["revision"] + 1 and out["block"]["blockStatus"] == "edited"
    assert out["rerenderTaskId"] is not None and _attempts(b["sectionId"]) == n + 1
    assert [e["after_text"] for e in _edit_signals(b["id"])] == ["Changed text"]
    assert out["block"]["layoutAdjust"] is None


def test_layout_only_change_is_not_a_translation_edit(env):
    """번역문이 같아도 배치가 다르면 no-op 이 아니다: revision+1·재렌더, edited·수정 기록 없음."""
    c, job, b, w, h = _setup(env)
    n = _attempts(b["sectionId"])
    box = _box_inside(b, w, h)
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": box, "fontScale": 1.25}))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["block"]["revision"] == b["revision"] + 1
    assert out["block"]["blockStatus"] == b["blockStatus"] and out["block"]["blockStatus"] != "edited"
    assert out["block"]["trans1"] == b["trans1"] and out["block"]["bbox"] == b["bbox"]  # 원본 bbox 는 바뀌지 않는다
    assert out["block"]["layoutAdjust"] == {"box": box, "fontScale": 1.25}
    assert out["rerenderTaskId"] is not None and _attempts(b["sectionId"]) == n + 1
    assert _edit_signals(b["id"]) == []


def test_both_change_bumps_revision_once_with_one_render(env):
    c, job, b, w, h = _setup(env)
    n = _attempts(b["sectionId"])
    r = _patch(c, job, b["id"], {"trans1": "Both", "revision": b["revision"], "layoutAdjust": {"fontScale": 1.1}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["block"]["revision"] == b["revision"] + 1 and out["block"]["blockStatus"] == "edited"
    assert out["block"]["layoutAdjust"] == {"box": None, "fontScale": 1.1} and out["rerenderTaskId"] is not None
    assert _attempts(b["sectionId"]) == n + 1 and len(_edit_signals(b["id"])) == 1


def test_noop_only_when_translation_and_normalized_layout_are_both_same(env):
    c, job, b, w, h = _setup(env)
    box = _box_inside(b, w, h)
    first = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": box, "fontScale": 1.2})).json()
    cur = first["block"]
    pending = _cur_preview(b["sectionId"])
    assert pending["status"] == "pending"
    n = _attempts(b["sectionId"])
    # 같은 배치를 다시(소수 표기만 다른 수 포함) — 변경 없음, 진행 중 렌더 유지
    same = {k: float(v) for k, v in box.items()}
    for la_body in ({"box": same, "fontScale": 1.2}, {"fontScale": 1.2}, {"box": same}):
        r = _patch(c, job, b["id"], {"trans1": cur["trans1"], "revision": cur["revision"], "layoutAdjust": la_body})
        assert r.status_code == 200, r.text
        assert r.json()["block"]["revision"] == cur["revision"] and r.json()["rerenderTaskId"] is None
    # layoutAdjust 를 보내지 않은 같은 값 저장도 변경 없음(기존 조정 유지)
    r = _patch(c, job, b["id"], {"trans1": cur["trans1"], "revision": cur["revision"]})
    assert r.json()["rerenderTaskId"] is None and r.json()["block"]["layoutAdjust"] == {"box": box, "fontScale": 1.2}
    assert _attempts(b["sectionId"]) == n and _cur_preview(b["sectionId"])["id"] == pending["id"]
    assert _cur_preview(b["sectionId"])["status"] == "pending" and _edit_signals(b["id"]) == []
    # 번역문은 같고 배치만 다르면 변경 없음이 아니다
    r = _patch(c, job, b["id"], {"trans1": cur["trans1"], "revision": cur["revision"], "layoutAdjust": {"fontScale": 1.3}})
    assert r.json()["block"]["revision"] == cur["revision"] + 1 and r.json()["rerenderTaskId"] is not None


# ── 생략·null·정규화 ────────────────────────────────────────────────────────────────────────────
def test_omit_and_null_semantics(env):
    c, job, b, w, h = _setup(env)
    box = _box_inside(b, w, h)

    def save(cur, adj):
        r = _patch(c, job, b["id"], {"trans1": cur["trans1"], "revision": cur["revision"], "layoutAdjust": adj})
        assert r.status_code == 200, r.text
        return r.json()["block"]

    cur = save(b, {"box": box, "fontScale": 1.5})
    cur = save(cur, {"fontScale": 1.75})  # 안쪽 box 생략 = 유지
    assert cur["layoutAdjust"] == {"box": box, "fontScale": 1.75}
    cur = save(cur, {"fontScale": None})  # 안쪽 null = 그 값만 해제
    assert cur["layoutAdjust"] == {"box": box, "fontScale": None}
    cur = save(cur, {"box": None, "fontScale": 2})
    assert cur["layoutAdjust"] == {"box": None, "fontScale": 2}
    cur = save(cur, None)  # 전체 null = 셀러 조정 전부 해제
    assert cur["layoutAdjust"] is None
    assert _q("SELECT layout_adjust FROM text_block WHERE id = :b", b=b["id"])[0]["layout_adjust"] is None


def test_normalization_each_field_alone(env):
    c, job, b, w, h = _setup(env)
    rev = b["revision"]
    # 기본값만 보내면 조정 없음과 같다 — 변경 없음
    same_box = {k: float(v) for k, v in b["bbox"].items()}
    r = _patch(c, job, b["id"], {"trans1": b["trans1"], "revision": rev, "layoutAdjust": {"box": same_box, "fontScale": 1.0}})
    assert r.json()["block"]["layoutAdjust"] is None and r.json()["block"]["revision"] == rev and r.json()["rerenderTaskId"] is None
    # 한 필드만 기본값이면 그 필드만 정규화
    r = _patch(c, job, b["id"], {"trans1": b["trans1"], "revision": rev, "layoutAdjust": {"box": same_box, "fontScale": 1.2}})
    assert r.json()["block"]["layoutAdjust"] == {"box": None, "fontScale": 1.2}
    cur = r.json()["block"]
    box = _box_inside(b, w, h)
    r = _patch(c, job, b["id"], {"trans1": b["trans1"], "revision": cur["revision"], "layoutAdjust": {"box": box, "fontScale": 1.0}})
    assert r.json()["block"]["layoutAdjust"] == {"box": box, "fontScale": None}


# ── 요청 형식 422 ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("adj", [
    {},
    {"box": {"x": 1, "y": 1, "w": 5}},
    {"box": {"x": 1, "y": 1, "w": 5, "h": 5, "z": 1}},
    {"box": {"x": "1", "y": 1, "w": 5, "h": 5}},
    {"box": {"x": True, "y": 1, "w": 5, "h": 5}},
    {"box": []},
    {"fontScale": 0},
    {"fontScale": -1},
    {"fontScale": True},
    {"fontScale": "1.2"},
    {"other": 1},
    [],
    "x",
])
def test_layout_format_errors_are_422_and_change_nothing(env, adj):
    c, job, b, _, _ = _setup(env)
    r = _patch(c, job, b["id"], {"trans1": "Would change", "revision": b["revision"], "layoutAdjust": adj})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_ERROR" and r.json()["error"]["details"]["fields"]
    after = _block(c, job, b["id"])
    assert after["revision"] == b["revision"] and after["trans1"] == b["trans1"]


def test_missing_trans1_is_422_even_for_layout_only(env):
    c, job, b, _, _ = _setup(env)
    r = _patch(c, job, b["id"], {"revision": b["revision"], "layoutAdjust": {"fontScale": 1.1}})
    assert r.status_code == 422


# ── 업무 검증 ─────────────────────────────────────────────────────────────────────────────────
def test_box_range_400_keeps_everything_and_boundary_is_allowed(env):
    c, job, b, w, h = _setup(env)
    for box in ({"x": -0.5, "y": 0, "w": 10, "h": 10}, {"x": 0, "y": -1, "w": 10, "h": 10}, {"x": 0, "y": 0, "w": 0, "h": 10},
                {"x": 0, "y": 0, "w": 10, "h": 0}, {"x": w - 5 + 0.5, "y": 0, "w": 5, "h": 10}, {"x": 0, "y": h - 5, "w": 5, "h": 5.5}):
        r = _patch(c, job, b["id"], {"trans1": "Would change", "revision": b["revision"], "layoutAdjust": {"box": box}})
        assert r.status_code == 400, (box, r.text)
        e = r.json()["error"]
        assert e["code"] == "VALIDATION_ERROR" and e["retryable"] is False
        assert e["details"]["fields"][0]["loc"] == ["body", "layoutAdjust", "box"]
        after = _block(c, job, b["id"])  # 원자성 — 번역문도 저장하지 않는다. 보정하지 않는다
        assert after["revision"] == b["revision"] and after["trans1"] == b["trans1"] and after["layoutAdjust"] is None
    # 경계와 정확히 같으면 허용(허용 오차 없음, 십진수 비교 — 이진 소수 오차로 거절하지 않는다)
    edge = {"x": 0.1, "y": 0.2, "w": float(f"{w - 1}.9"), "h": float(f"{h - 1}.8")}
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": edge}))
    assert r.status_code == 200, r.text
    assert r.json()["block"]["layoutAdjust"]["box"] == edge


def test_not_render_target_409(env):
    c, job, b, w, h = _setup(env)
    blocks = c.get(f"/v1/jobs/{job}/blocks").json()
    label = [x for x in blocks if x["isExcluded"] is True][0]
    for adj in ({"fontScale": 1.1}, None):  # 전체 해제도 배치 저장이다
        r = _patch(c, job, label["id"], {"trans1": "x", "revision": label["revision"], "layoutAdjust": adj})
        assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE", r.text
        assert r.json()["error"]["details"] == {"reason": "not_render_target"}
    # 빈 번역문 — 같은 요청의 trans1 이 채우면 대상, 비어 있으면 거절(번역문도 저장 안 함)
    _q("UPDATE text_block SET trans_1 = NULL WHERE id = :b", b=b["id"])
    r = _patch(c, job, b["id"], {"trans1": "  ", "revision": b["revision"], "layoutAdjust": {"fontScale": 1.1}})
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "not_render_target"
    assert _block(c, job, b["id"])["trans1"] is None
    r = _patch(c, job, b["id"], {"trans1": "Filled", "revision": b["revision"], "layoutAdjust": {"fontScale": 1.1}})
    assert r.status_code == 200, r.text
    # 빈 원문
    other = [x for x in blocks if x["id"] not in (label["id"], b["id"]) and x["trans1"]][0]
    _q("UPDATE text_block SET source_ko = '' WHERE id = :b", b=other["id"])
    r = _patch(c, job, other["id"], {"trans1": other["trans1"], "revision": other["revision"], "layoutAdjust": {"fontScale": 1.1}})
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "not_render_target"
    # 범위 400 이 먼저다(저장 공통 7단계 순서)
    r = _patch(c, job, label["id"], {"trans1": "x", "revision": label["revision"], "layoutAdjust": {"box": {"x": -1, "y": 0, "w": 1, "h": 1}}})
    assert r.status_code == 400


def test_revision_conflict_comes_first_and_current_has_layout(env):
    c, job, b, _, _ = _setup(env)
    cur = _patch(c, job, b["id"], _body(b, layoutAdjust={"fontScale": 1.1})).json()["block"]
    r = _patch(c, job, b["id"], {"trans1": b["trans1"], "revision": b["revision"], "layoutAdjust": {"box": {"x": -1, "y": 0, "w": 1, "h": 1}}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_CONFLICT"
    assert r.json()["error"]["details"]["current"]["layoutAdjust"] == cur["layoutAdjust"]


# ── 멱등 ──────────────────────────────────────────────────────────────────────────────────────
def test_idempotent_replay_and_key_reuse(env):
    c, job, b, _, _ = _setup(env)
    body = _body(b, layoutAdjust={"fontScale": 1.1})
    r1 = _patch(c, job, b["id"], body, key="layout-key-1")
    r2 = _patch(c, job, b["id"], body, key="layout-key-1")
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true" and r2.json() == r1.json()
    assert _block(c, job, b["id"])["revision"] == b["revision"] + 1
    for other in (_body(b, layoutAdjust={"fontScale": 1.2}), _body(b, layoutAdjust=None), _body(b)):  # 생략과 null 은 다른 요청
        r = _patch(c, job, b["id"], other, key="layout-key-1")
        assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "idempotency_key_reused", other
    # 숫자 표기만 다른 같은 본문은 같은 요청
    r3 = _patch(c, job, b["id"], _body(b, layoutAdjust={"fontScale": 1.1}), key="layout-key-1")
    assert r3.headers["Idempotent-Replayed"] == "true"


def test_request_hash_without_layout_is_unchanged(env):
    """layoutAdjust 를 생략한 요청의 해시는 배치 저장 전과 같다 — 배포 전에 남은 기록도 재전송된다."""
    from app.routers.cfm import BlockUpdate

    body = BlockUpdate.model_validate({"trans1": "t", "revision": 3})
    assert idem.request_sha256("block_patch", {"jobId": 1, "blockId": 2}, la.canonical_number(body.model_dump(exclude_unset=True))) == \
        idem.request_sha256("block_patch", {"jobId": 1, "blockId": 2}, {"trans1": "t", "revision": 3})


# ── 렌더 입력·넘침·최신성 ─────────────────────────────────────────────────────────────────────
def _render_block(section, bid):
    return [x for x in section["renderInput"]["blocks"] if x["blockId"] == bid][0]


def test_saved_layout_reaches_render_input_and_new_render_is_current(env):
    c, job, b, w, h = _setup(env)
    sid = b["sectionId"]
    before = _pvs(c, job, sid)
    others = {s["id"]: s["inputFingerprint"] for s in _pv(c, job)["sections"] if s["id"] != sid}
    box = _box_inside(b, w, h)
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": box, "fontScale": 1.25}))
    task = r.json()["rerenderTaskId"]
    s = _pvs(c, job, sid)
    rb = _render_block(s, b["id"])
    assert rb["box"] == box and rb["style"]["fontScale"] == 1.25
    assert rb["style"]["fontSizePx"] == _render_block(before, b["id"])["style"]["fontSizePx"]  # 기준 크기는 그대로, 배율은 따로
    assert s["inputFingerprint"] != before["inputFingerprint"]
    assert s["render"]["isCurrent"] is False and s["render"]["attempt"]["status"] == "pending"
    assert _block(c, job, b["id"])["sectionInputFingerprint"] == s["inputFingerprint"]
    # 다른 섹션의 입력은 바뀌지 않는다
    assert {x["id"]: x["inputFingerprint"] for x in _pv(c, job)["sections"] if x["id"] != sid} == others
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["isCurrent"] is True and s["render"]["attempt"]["taskId"] == task
    assert s["render"]["result"]["inputFingerprint"] == s["inputFingerprint"] and s["renderedUrl"] != before["renderedUrl"]
    man = _q("SELECT input_manifest FROM job_async_task WHERE id = :t", t=task)[0]["input_manifest"]
    mb = [x for x in man["blocks"] if x["block_id"] == b["id"]][0]
    assert mb["box"] == box and mb["font_scale"] == 1.25


def test_unadjusted_blocks_have_no_scale_in_manifest(env):
    c, job, b, _, _ = _setup(env)
    man = _q("SELECT input_manifest FROM job_async_task WHERE stage = 'preview' AND unit_id = :s AND is_current", s=b["sectionId"])[0]["input_manifest"]
    assert man["blocks"] and all("font_scale" not in x for x in man["blocks"])
    assert all(x["style"]["fontScale"] == 1.0 for x in _pvs(c, job, b["sectionId"])["renderInput"]["blocks"])


def test_overflow_follows_new_render(env):
    c, job, b, w, h = _setup(env)
    sid = b["sectionId"]

    def save_and_render(cur, adj):
        r = _patch(c, job, b["id"], {"trans1": "Visibly smoother skin every day", "revision": cur["revision"], "layoutAdjust": adj})
        assert r.status_code == 200, r.text
        assert _pvs(c, job, sid)["render"]["isCurrent"] is False  # 렌더 완료 전에는 최신이 아니다
        assert _pvs(c, job, sid)["render"]["result"]["overflow"] is None  # 이전 결과의 넘침은 현재 입력의 판정이 아니다
        env["q"].drain()
        s = _pvs(c, job, sid)
        assert s["render"]["isCurrent"] is True
        return _block(c, job, b["id"]), s["render"]["result"]["overflow"]

    # 박스를 줄이면 넘침
    cur, ov = save_and_render(b, {"box": {"x": 0, "y": 0, "w": 3, "h": 3}})
    assert ov[str(b["id"])] is True
    assert cur["overflow"] is True
    # 넉넉한 박스로 되돌리면 해소
    cur, ov = save_and_render(cur, {"box": {"x": 0, "y": 0, "w": w, "h": h}})
    assert ov[str(b["id"])] is False and cur["overflow"] is False
    # 같은 박스에서 배율을 크게 하면 넘침 — 배율이 실제 조판에 쓰인다
    cur, ov = save_and_render(cur, {"fontScale": 20})
    assert ov[str(b["id"])] is True and cur["overflow"] is True
    # 배율 해제로 다시 해소
    cur, ov = save_and_render(cur, {"fontScale": None})
    assert ov[str(b["id"])] is False and cur["overflow"] is False


def test_typeset_uses_absolute_scale_once():
    bg = png_bytes(600, 400)
    style = {"font_color": "#112233", "bg_color": "#FFFFFF", "est_font_px": 20.0, "align": "left"}
    plain = _blk(1, style, text="word word word word word word", box={"x": 0, "y": 0, "w": 200, "h": 60})
    big = _blk(2, style, text="word word word word word word", box={"x": 0, "y": 100, "w": 200, "h": 60})
    big.font_scale = 2.0
    res = typeset.render(bg, [plain, big], _defaults())
    assert res.blocks[1].overflow is False and res.blocks[2].overflow is True and res.blocks[2].lines > res.blocks[1].lines
    assert res.blocks[1].applied["est_font_px"] == 20.0 and "font_scale" not in res.blocks[1].applied  # 배율 없는 기록은 그대로
    assert res.blocks[2].applied["est_font_px"] == 20.0 and res.blocks[2].applied["font_scale"] == 2.0  # 기준값은 그대로, 배율 따로
    assert res.png != typeset.render(bg, [plain], _defaults()).png


# ── 섹션보다 큰 글자: 그릴 수 있으면 넘침, 그릴 수 없으면 렌더 실패 ───────────────────────────────
STYLE_20 = {"font_color": "#112233", "bg_color": "#FFFFFF", "est_font_px": 20.0, "align": "left"}


def test_typeset_small_section_normal_text_is_drawn_not_skipped():
    """배율 없는 보통 글자가 낮은 섹션보다 커도 글자를 뺀 이미지를 내지 않는다 — 그리고 넘침으로 판정한다."""
    bg = png_bytes(400, 24)
    style = {**STYLE_20, "est_font_px": 40.0}
    res = typeset.render(bg, [_blk(1, style, text="word", box={"x": 0, "y": 0, "w": 400, "h": 24})], _defaults())
    assert res.blocks[1].overflow is True and res.blocks[1].lines == 1
    assert res.png != typeset.render(bg, [], _defaults()).png


@pytest.mark.parametrize("scale", [1.5, 30])
def test_typeset_drawable_scale_is_drawn_with_overflow(scale):
    """줄 높이가 섹션 높이를 넘어도(30) 글자가 일부라도 보이면 그린다."""
    bg = png_bytes(600, 400)
    blk = _blk(1, STYLE_20, text="word", box={"x": 0, "y": 0, "w": 30, "h": 30})
    blk.font_scale = scale
    res = typeset.render(bg, [blk], _defaults())
    assert res.blocks[1].overflow is True and res.blocks[1].lines == 1 and res.blocks[1].ink_visible is True
    assert res.png != typeset.render(bg, [], _defaults()).png


@pytest.mark.parametrize("scale", [1e3, 1e5, 1e9, 1e300])
def test_typeset_resource_limit_raises_instead_of_omitting(scale):
    """그릴 수 없을 만큼 크면 예외다 — 글자를 뺀 성공 결과가 아니고, 승인 폰트 문제로 분류되지도 않는다."""
    blk = _blk(1, STYLE_20, text="word", box={"x": 0, "y": 0, "w": 200, "h": 60})
    blk.font_scale = scale
    with pytest.raises((Image.DecompressionBombError, OSError)):
        typeset.render(png_bytes(600, 400), [blk], _defaults())


@pytest.mark.parametrize("scale,box", [
    (200, {"x": 0, "y": 0, "w": 30, "h": 30}),  # 4000px — 글자 위 여백만 섹션에 걸린다
    (1, {"x": 0, "y": 399, "w": 200, "h": 1}),  # 보통 크기 — 섹션 맨 아래에 둔 박스
])
def test_typeset_no_visible_ink_is_recorded_not_failed(scale, box):
    """그리기는 끝났는데 글자 픽셀이 섹션 안에 없는 경우 — 실패로 확정하지 않았다(PM 확인 전). 넘침 판정 그대로, 기록만 남는다.
    실행 실패(위 resource_limit 시험)와 구별된다: 예외가 없고 결과가 있다."""
    bg = png_bytes(600, 400)
    blk = _blk(1, STYLE_20, text="word", box=box)
    blk.font_scale = scale
    res = typeset.render(bg, [blk], _defaults())
    assert res.blocks[1].overflow is True and res.blocks[1].lines == 1 and res.blocks[1].ink_visible is False
    assert res.png == typeset.render(bg, [], _defaults()).png  # 지금 이 경우 화면에는 번역문이 보이지 않는다 — PM 확인 항목


def test_visible_check_ignores_text_color():
    """배경과 같은 색 글자도 픽셀이 있는 것으로 기록한다 — 색 비교로 판단하지 않는다."""
    bg = png_bytes(600, 400)
    with Image.open(io.BytesIO(bg)) as im:
        color = "#{:02X}{:02X}{:02X}".format(*im.convert("RGB").getpixel((0, 0)))
    blk = _blk(1, {**STYLE_20, "font_color": color}, text="word word word", box={"x": 0, "y": 0, "w": 30, "h": 3})
    res = typeset.render(bg, [blk], _defaults())
    assert res.blocks[1].overflow is True and res.blocks[1].ink_visible is True


def test_blocks_without_overflow_are_not_checked_and_stay_visible():
    res = typeset.render(png_bytes(600, 400), [_blk(1, STYLE_20, text="word", box={"x": 0, "y": 0, "w": 300, "h": 60})], _defaults())
    assert res.blocks[1].overflow is False and res.blocks[1].ink_visible is True


def test_missing_font_file_is_still_style_defaults_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(typeset.sd, "font_path", lambda data, face: tmp_path / "missing.otf")
    with pytest.raises(typeset.StyleDefaultsUnavailable):
        typeset.render(png_bytes(600, 400), [_blk(1, STYLE_20, text="word", box={"x": 0, "y": 0, "w": 200, "h": 60})], _defaults())


def _image(env, key):
    with Image.open(io.BytesIO(env["store"].get(key))) as im:
        return im.convert("RGB")


def _section_key(sid):
    return _q("SELECT render_image_key FROM section WHERE id = :s", s=sid)[0]["render_image_key"]


def _preview_measure(sid, bid):
    rows = _q("SELECT h.payload FROM task_handoff h JOIN job_async_task t ON t.id = h.task_id "
              "WHERE t.stage = 'preview' AND t.unit_id = :s AND t.is_current AND h.state = 'adopted'", s=sid)
    return rows[0]["payload"]["render"]["blocks"][str(bid)]


def _final_ink(job, sid, bid):
    rows = _q("SELECT h.payload FROM task_handoff h JOIN job_async_task t ON t.id = h.task_id "
              "WHERE t.job_id = :j AND t.stage = 'final_render' AND t.is_current AND h.state = 'adopted'", j=job)
    return rows[0]["payload"]["renders"][str(sid)]["ink_visible"][str(bid)]


def test_over_section_scale_is_drawn_and_adopted_through_preview_and_n6(env, monkeypatch):
    """예전 분기가 글자를 빼던 구간(줄 높이 > 섹션 높이)을 API 로 끝까지 — 미리보기·최종 모두 글자가 있고 넘침으로 표시된다."""
    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    c, job, b, w, h = _setup(env)
    sid = b["sectionId"]
    px = _render_block(_pvs(c, job, sid), b["id"])["style"]["fontSizePx"]
    scale = 1.5 * h / px
    assert px * scale * typeset.LINE_SPACING > h
    assert _patch(c, job, b["id"], _body(b, layoutAdjust={"fontScale": scale})).status_code == 200
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["isCurrent"] is True and s["render"]["result"]["overflow"][str(b["id"])] is True
    measured = _preview_measure(sid, b["id"])
    assert measured["lines"] >= 1 and measured["ink_visible"] is True  # 예전 분기는 lines 0
    preview = _image(env, _section_key(sid))

    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert c.get(f"/v1/jobs/{job}/deliverables").json()["deliverables"][0]["renderStatus"] == "done"
    assert _q("SELECT overflow FROM text_block WHERE id = :b", b=b["id"])[0]["overflow"] is True
    final = _image(env, _section_key(sid))
    assert ImageChops.difference(preview, final).getbbox() is None  # 같은 입력이면 미리보기와 최종이 같은 그림


def test_no_ink_at_normal_scale_is_adopted_as_overflow_and_recorded(env, monkeypatch):
    """정상 배율 재현 — 섹션 맨 아래 1px 박스(범위 검증 통과). 실패로 확정하지 않았으므로 미리보기·N6 모두 넘침으로 채택되고 기록만 남는다."""
    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    c, job, b, w, h = _setup(env)
    sid = b["sectionId"]
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": {"x": 0, "y": h - 1, "w": w, "h": 1}}))
    assert r.status_code == 200, r.text
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["attempt"]["status"] == "done" and s["render"]["isCurrent"] is True
    assert s["render"]["result"]["overflow"][str(b["id"])] is True
    assert _preview_measure(sid, b["id"])["ink_visible"] is False
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert c.get(f"/v1/jobs/{job}/deliverables").json()["deliverables"][0]["renderStatus"] == "done"
    assert _final_ink(job, sid, b["id"]) is False


@pytest.mark.parametrize("scale", [1e3, 1e9, 1e300])
def test_unrenderable_scale_fails_preview_and_keeps_previous_image(env, scale):
    c, job, b, _, h = _setup(env)
    sid = b["sectionId"]
    before = _section_key(sid)
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"fontScale": scale}))
    assert r.status_code == 200, r.text  # 상한이 없으므로 저장 자체는 받는다
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["attempt"]["status"] == "failed"
    assert s["render"]["attempt"]["errorCode"] == "PREVIEW_UNEXPECTED"  # 기존 실패 코드 — 승인 폰트 문제로 분류하지 않는다
    assert s["render"]["isCurrent"] is False
    assert _section_key(sid) == before  # 글자를 뺀 이미지로 바꾸지 않는다
    # 셀러가 배율을 고치면 다시 그린다
    r = _patch(c, job, b["id"], _body(r.json()["block"], layoutAdjust={"fontScale": None}))
    assert r.status_code == 200, r.text
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["attempt"]["status"] == "done" and s["render"]["isCurrent"] is True


def test_unrenderable_scale_at_n6_makes_no_deliverable(env, monkeypatch):
    """확정은 막지 않는다(현행). 최종 렌더가 실패하면 산출물을 만들지 않고, 저장도 막힌다. 2차에는 N6→N5 복귀가 없다(7.4)."""
    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    c, job, b, _, _ = _setup(env)
    sid = b["sectionId"]
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"fontScale": 1e9}))
    assert r.status_code == 200, r.text
    env["q"].drain()
    before = _section_key(sid)
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()

    def final_attempt():
        return _q("SELECT id, status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=job)[0]

    a = final_attempt()
    assert (a["status"], a["error_code"]) == ("failed", "RENDER_UNEXPECTED")
    assert _q("SELECT count(*) AS n FROM deliverable WHERE job_id = :j", j=job)[0]["n"] == 0
    assert _section_key(sid) == before
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409
    # 재시도해도 입력이 같으므로 결과가 같고 산출물이 생기지 않는다
    assert c.post(f"/v1/jobs/{job}/tasks/{a['id']}/retry").status_code == 200
    env["q"].drain()
    assert final_attempt()["status"] == "failed"
    assert _q("SELECT count(*) AS n FROM deliverable WHERE job_id = :j", j=job)[0]["n"] == 0
    # 2차에서 N6 에서 배율을 고치는 경로는 없다
    assert _patch(c, job, b["id"], _body(_block(c, job, b["id"]), layoutAdjust={"fontScale": None})).status_code == 409
    assert c.post(f"/v1/jobs/{job}/abort").status_code == 409


def _final_manifest(job):
    return _q("SELECT input_manifest FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=job)[0]["input_manifest"]


def test_final_render_uses_same_box_scale_and_updates_overflow(env, monkeypatch):
    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    c, job, b, w, h = _setup(env)
    box = {"x": 0, "y": 0, "w": 3, "h": 3}
    p = _patch(c, job, b["id"], {"trans1": "Visibly smoother skin every day", "revision": b["revision"], "layoutAdjust": {"box": box, "fontScale": 1.5}})
    env["q"].drain()
    pv = _render_block(_pvs(c, job, b["sectionId"]), b["id"])
    assert _q("SELECT overflow FROM text_block WHERE id = :b", b=b["id"])[0]["overflow"] is True
    _q("UPDATE text_block SET overflow = false WHERE id = :b", b=b["id"])  # 최종 렌더가 다시 계산하는지 본다
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    m = _final_manifest(job)
    fb = [x for s in m["sections"] for x in s["blocks"] if x["block_id"] == b["id"]][0]
    assert fb["box"] == pv["box"] == box and fb["font_scale"] == pv["style"]["fontScale"] == 1.5
    assert fb["revision"] == p.json()["block"]["revision"]
    assert _q("SELECT overflow FROM text_block WHERE id = :b", b=b["id"])[0]["overflow"] is True
    assert c.get(f"/v1/jobs/{job}/deliverables").json()["deliverables"][0]["renderStatus"] == "done"

    # content.csv 배치 5열 — 셀러 조정값, 조정 없는 블록은 빈칸
    r = c.post(f"/v1/jobs/{job}/export", json={"components": ["content.csv"]})
    assert r.status_code == 201, r.text
    key = _q("SELECT file_url FROM export_artifact WHERE job_id = :j AND artifact_type = 'csv'", j=job)[0]["file_url"]
    rows = list(csv.DictReader(io.StringIO(store.get(key).decode("utf-8-sig"))))
    assert list(rows[0]) == ["sectionId", "blockOrder", "role", "sourceKo", "trans1", "layout_x", "layout_y", "layout_w", "layout_h", "font_scale"]
    adjusted = [x for x in rows if x["trans1"] == "Visibly smoother skin every day"][0]
    assert (adjusted["layout_x"], adjusted["layout_y"], adjusted["layout_w"], adjusted["layout_h"], adjusted["font_scale"]) == ("0", "0", "3", "3", "1.5")
    plain = [x for x in rows if x is not adjusted][0]
    assert all(plain[k] == "" for k in ("layout_x", "layout_y", "layout_w", "layout_h", "font_scale"))


def test_csv_blank_combinations():
    assert la.csv_values(la.EMPTY) == [None] * 5
    box = {"x": 27.5, "y": 211.05, "w": 546, "h": 50.4}
    assert la.csv_values(la.LayoutState(user_box=box)) == [27.5, 211.05, 546, 50.4, None]
    assert la.csv_values(la.LayoutState(user_scale=1.05)) == [None, None, None, None, 1.05]


# ── 저장 형태: system·legacy ───────────────────────────────────────────────────────────────────
def _set_raw(bid, value):
    _q("UPDATE text_block SET layout_adjust = CAST(:a AS jsonb) WHERE id = :b", a=None if value is None else json.dumps(value), b=bid)


def test_auto_adjust_comes_from_system_and_is_not_rendered(env):
    c, job, b, w, h = _setup(env)
    sid = b["sectionId"]
    fp = _pvs(c, job, sid)["inputFingerprint"]
    _set_raw(b["id"], {"schema_version": 2, "user": None, "system": {"font_scale": 0.85, "line_break_applied": True}})
    got = _block(c, job, b["id"])
    assert got["autoAdjust"] == {"fontScale": 0.85, "lineBreakApplied": True} and got["layoutAdjust"] is None
    assert _pvs(c, job, sid)["inputFingerprint"] == fp  # 자동 조정은 렌더에 적용하지 않는다
    # 셀러 저장·해제가 시스템 값을 지우지 않는다
    r = _patch(c, job, b["id"], _body(got, layoutAdjust={"fontScale": 1.2}))
    assert r.json()["block"]["autoAdjust"] == got["autoAdjust"] and r.json()["block"]["layoutAdjust"]["fontScale"] == 1.2
    r = _patch(c, job, b["id"], _body(r.json()["block"], layoutAdjust=None))
    assert r.json()["block"]["autoAdjust"] == got["autoAdjust"] and r.json()["block"]["layoutAdjust"] is None
    assert _q("SELECT layout_adjust FROM text_block WHERE id = :b", b=b["id"])[0]["layout_adjust"]["system"]["font_scale"] == 0.85


def test_auto_adjust_is_null_without_system(env):
    c, job, b, _, _ = _setup(env)
    assert b["autoAdjust"] is None and b["layoutAdjust"] is None


OLD_BOX = {"x": 1, "y": 1, "w": 5, "h": 5}


@pytest.mark.parametrize("value,expected", [
    ({"note": "x"}, "bbox"),  # 배치 키가 없는 객체 — 이전 렌더도 쓰지 않던 값
    ({"layout_bbox": None}, "bbox"),
    ({"layout_bbox": OLD_BOX}, OLD_BOX),  # 출처 불명이지만 형식이 맞는 박스 — 이전처럼 그린다
    ({"layout_bbox": OLD_BOX, "updated_by": "system"}, OLD_BOX),
    ({"layout_bbox": OLD_BOX, "font_scale": -1, "updated_by": "user"}, OLD_BOX),  # 셀러 값으로 확인되지 않은 배율은 쓰지 않는다
    ({"layout_bbox": "0,0,5,5"}, "invalid"),
    ({"layout_bbox": {"x": 1, "y": 1, "w": 5}}, "invalid"),
    ({"layout_bbox": {"x": "1", "y": 1, "w": 5, "h": 5}}, "invalid"),
    (["layout_bbox"], "invalid"),
    ("x", "invalid"),
    ({"schema_version": 2, "user": {"layout_bbox": "0,0,5,5"}, "system": None}, "invalid"),  # 깨진 v2
])
def test_unknown_value_is_kept_and_not_treated_as_seller(env, value, expected):
    c, job, b, w, h = _setup(env)
    sid = b["sectionId"]
    _set_raw(b["id"], value)
    got = _block(c, job, b["id"])
    assert got["layoutAdjust"] is None and got["autoAdjust"] is None  # 어느 경우든 셀러 조정으로 내지 않는다
    assert got["bbox"] == b["bbox"]
    s = _pvs(c, job, sid)
    if expected == "invalid":
        assert s["inputError"] == {"code": "RENDER_INPUT_INVALID", "blockIds": [b["id"]]}
    else:
        pv = _render_block(s, b["id"])
        assert s["inputError"] is None and pv["box"] == (b["bbox"] if expected == "bbox" else expected)
        assert pv["style"]["fontScale"] == 1.0
    assert _q("SELECT layout_adjust FROM text_block WHERE id = :b", b=b["id"])[0]["layout_adjust"] == value
    assert la.csv_values(la.parse(value)) == [None] * 5


def _old_applied_box(value, bbox):
    """이 작업 전 서버의 적용 규칙(develop 2489762 `_applied_box`) — 호환성 기준으로 시험에 고정한다."""
    if value is not None and not isinstance(value, dict):
        return None
    lb = (value or {}).get("layout_bbox")
    if lb is not None and not isinstance(lb, dict):
        return None
    box = lb or bbox
    if not isinstance(box, dict):
        return None
    out = {}
    for k in ("x", "y", "w", "h"):
        v = box.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        out[k] = v
    return out


def test_migrated_values_render_with_the_same_box_as_before():
    """변환 뒤 적용 박스는 모든 이전 값에서 이전 규칙과 같다 — 셀러·시스템·출처 불명·훼손을 가리지 않는다."""
    from app.flows.downstream import _applied_box

    mig = _migration()
    bbox = {"x": 40, "y": 120, "w": 520, "h": 64}
    for old in [v for v, _ in CONVERSIONS] + LEGACY:
        assert _applied_box({"layout_adjust": mig.to_v2(old), "bbox": bbox}) == _old_applied_box(old, bbox), old


def _raw_adjust(bid):
    return _q("SELECT layout_adjust FROM text_block WHERE id = :b", b=bid)[0]["layout_adjust"]


def _applied(c, job, sid, bid):
    return _render_block(_pvs(c, job, sid), bid)


def _legacy_setup(env, value=None):
    c, job, b, w, h = _setup(env)
    _set_raw(b["id"], {"layout_bbox": OLD_BOX} if value is None else value)
    return c, job, _block(c, job, b["id"]), w, h


@pytest.mark.parametrize("clear", [None, {"box": None}], ids=["layoutAdjust-null", "box-null"])
def test_explicit_clear_goes_to_original_bbox_and_keeps_legacy(env, clear):
    c, job, b, _, _ = _legacy_setup(env)
    sid = b["sectionId"]
    assert _applied(c, job, sid, b["id"])["box"] == OLD_BOX
    r = _patch(c, job, b["id"], _body(b, layoutAdjust=clear))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["block"]["revision"] == b["revision"] + 1 and out["rerenderTaskId"] is not None  # 적용 박스가 바뀌었으므로 no-op 이 아니다
    assert out["block"]["layoutAdjust"] is None and out["block"]["bbox"] == b["bbox"]
    assert _applied(c, job, sid, b["id"])["box"] == b["bbox"]  # 계약: 해제 = 원본 bbox
    raw = _raw_adjust(b["id"])
    assert raw["legacy"] == {"layout_bbox": OLD_BOX} and raw["legacy_released"] is True  # 자료는 보존
    # 같은 해제를 다시 보내면 no-op
    again = _patch(c, job, b["id"], _body(out["block"], layoutAdjust=clear))
    assert again.status_code == 200 and again.json()["rerenderTaskId"] is None
    assert again.json()["block"]["revision"] == out["block"]["revision"]


def test_sending_original_bbox_normalizes_and_releases(env):
    c, job, b, _, _ = _legacy_setup(env)
    sid = b["sectionId"]
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": b["bbox"]}))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["block"]["layoutAdjust"] is None  # 2차 정규화 — 원본 bbox 와 같은 box 는 null
    assert out["rerenderTaskId"] is not None and _applied(c, job, sid, b["id"])["box"] == b["bbox"]
    assert _raw_adjust(b["id"])["legacy_released"] is True
    again = _patch(c, job, b["id"], _body(out["block"], layoutAdjust={"box": b["bbox"]}))
    assert again.json()["rerenderTaskId"] is None and again.json()["block"]["revision"] == out["block"]["revision"]


def test_font_scale_only_and_translation_only_keep_legacy_box(env):
    """해제가 아닌 저장은 기존 적용을 건드리지 않는다."""
    c, job, b, _, _ = _legacy_setup(env)
    sid = b["sectionId"]
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"fontScale": 1.25}))
    assert r.status_code == 200, r.text
    block = r.json()["block"]
    assert block["layoutAdjust"] == {"box": None, "fontScale": 1.25}
    pv = _applied(c, job, sid, b["id"])
    assert pv["box"] == OLD_BOX and pv["style"]["fontScale"] == 1.25
    assert "legacy_released" not in _raw_adjust(b["id"])
    r = _patch(c, job, b["id"], {"trans1": "Translation only", "revision": block["revision"]})
    assert r.status_code == 200, r.text
    assert _applied(c, job, sid, b["id"])["box"] == OLD_BOX
    assert "legacy_released" not in _raw_adjust(b["id"])
    # 배율 해제도 박스 해제가 아니다
    r = _patch(c, job, b["id"], _body(r.json()["block"], layoutAdjust={"fontScale": None}))
    assert r.status_code == 200, r.text
    assert r.json()["block"]["layoutAdjust"] is None
    assert _applied(c, job, sid, b["id"])["box"] == OLD_BOX and "legacy_released" not in _raw_adjust(b["id"])


def test_seller_box_over_legacy_then_clear_goes_to_bbox(env):
    c, job, b, w, h = _legacy_setup(env)
    sid = b["sectionId"]
    box = _box_inside(b, w, h)
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": box}))
    assert r.status_code == 200, r.text
    assert r.json()["block"]["layoutAdjust"]["box"] == box and _applied(c, job, sid, b["id"])["box"] == box
    assert _raw_adjust(b["id"])["legacy"] == {"layout_bbox": OLD_BOX} and "legacy_released" not in _raw_adjust(b["id"])
    # 전에 그리던 박스를 셀러가 직접 고르면 셀러 조정이다(원본 bbox 가 아니므로 정규화하지 않는다)
    r = _patch(c, job, b["id"], _body(r.json()["block"], layoutAdjust={"box": OLD_BOX}))
    assert r.json()["block"]["layoutAdjust"]["box"] == OLD_BOX
    r = _patch(c, job, b["id"], _body(r.json()["block"], layoutAdjust=None))
    assert r.status_code == 200, r.text
    assert r.json()["block"]["layoutAdjust"] is None and _applied(c, job, sid, b["id"])["box"] == b["bbox"]
    assert _raw_adjust(b["id"])["legacy"] == {"layout_bbox": OLD_BOX}


def test_undo_by_sending_previous_applied_box_restores_screen_not_storage(env):
    """Undo 는 이전 적용 박스(/preview renderInput.box)를 명시적으로 보내 화면을 되돌린다. 저장 상태까지 legacy 그대로 돌아가는 것은 아니다:
    셀러 조정으로 저장되고 legacy 는 보존된다."""
    c, job, b, w, h = _legacy_setup(env)
    sid = b["sectionId"]
    before_box = _applied(c, job, sid, b["id"])["box"]  # FE 가 편집 시작값으로 쓰는 적용 박스
    assert before_box == OLD_BOX
    before_raw = _raw_adjust(b["id"])
    # 셀러가 박스를 옮긴 뒤 해제까지 한 상태에서
    r = _patch(c, job, b["id"], _body(b, layoutAdjust={"box": _box_inside(b, w, h)}))
    r = _patch(c, job, b["id"], _body(r.json()["block"], layoutAdjust=None))
    assert _applied(c, job, sid, b["id"])["box"] == b["bbox"]
    # Undo — 이전 적용 박스를 그대로 보낸다
    r = _patch(c, job, b["id"], _body(r.json()["block"], layoutAdjust={"box": before_box}))
    assert r.status_code == 200, r.text
    assert _applied(c, job, sid, b["id"])["box"] == before_box  # 화면상 복원
    assert r.json()["block"]["layoutAdjust"] == {"box": OLD_BOX, "fontScale": None}  # 처음(null)과 다르다 — 셀러 조정으로 저장
    raw = _raw_adjust(b["id"])
    assert raw["legacy"] == before_raw and raw["user"]["layout_bbox"] == OLD_BOX  # legacy 는 보존, 저장 모양은 처음과 다르다
    assert la.csv_values(la.parse(raw))[:4] == [OLD_BOX[k] for k in ("x", "y", "w", "h")]  # content.csv 에도 셀러 조정으로 나온다


def test_explicit_clear_replays_with_same_key(env):
    c, job, b, _, _ = _legacy_setup(env)
    body = _body(b, layoutAdjust=None)
    first = _patch(c, job, b["id"], body, key="clear-1")
    assert first.status_code == 200, first.text
    replay = _patch(c, job, b["id"], body, key="clear-1")
    assert replay.status_code == 200 and replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.json() == first.json()
    assert _block(c, job, b["id"])["revision"] == first.json()["block"]["revision"]
    assert _patch(c, job, b["id"], _body(b, layoutAdjust={"box": None}), key="clear-1").status_code == 409  # 같은 키의 다른 요청


@pytest.mark.parametrize("value", [{"note": "x"}, {"layout_bbox": None}], ids=["no-box-key", "null-box"])
def test_clear_is_noop_when_legacy_does_not_affect_placement(env, value):
    c, job, b, _, _ = _legacy_setup(env, value)
    r = _patch(c, job, b["id"], _body(b, layoutAdjust=None))
    assert r.status_code == 200, r.text
    assert r.json()["rerenderTaskId"] is None and r.json()["block"]["revision"] == b["revision"]
    assert _raw_adjust(b["id"]) == value


def test_explicit_clear_recovers_block_with_damaged_legacy_box(env):
    """훼손 박스는 입력 오류였지만 셀러가 해제하면 원본 bbox 로 그릴 수 있다. 훼손 값은 지우지 않는다."""
    damaged = {"layout_bbox": "0,0,5,5"}
    c, job, b, _, _ = _legacy_setup(env, damaged)
    sid = b["sectionId"]
    assert _pvs(c, job, sid)["inputError"] == {"code": "RENDER_INPUT_INVALID", "blockIds": [b["id"]]}
    r = _patch(c, job, b["id"], _body(b, layoutAdjust=None))
    assert r.status_code == 200, r.text
    s = _pvs(c, job, sid)
    assert s["inputError"] is None and _render_block(s, b["id"])["box"] == b["bbox"]
    assert _raw_adjust(b["id"])["legacy"] == damaged


def test_release_flag_round_trip_and_bad_values():
    state = la.LayoutState(legacy={"layout_bbox": OLD_BOX}, legacy_released=True)
    saved = la.dump(state)
    assert saved["legacy_released"] is True and la.parse(saved) == state
    bbox = {"x": 40, "y": 120, "w": 520, "h": 64}
    assert la.parse(saved).placement_source(bbox) == bbox
    for bad in ({"schema_version": 2, "user": None, "system": None, "legacy": {"note": 1}, "legacy_released": "yes"},
                {"schema_version": 2, "user": None, "system": None, "legacy_released": True}):
        got = la.parse(bad)
        assert got.legacy == bad and got.placement_source(bbox) is None  # 형식이 깨진 값은 추정하지 않는다


def test_release_survives_downgrade_without_reviving_box():
    """내렸다 올려도 해제한 박스가 다시 적용되지 않는다."""
    from app.flows.downstream import _applied_box

    mig = _migration()
    bbox = {"x": 40, "y": 120, "w": 520, "h": 64}
    released = {"schema_version": 2, "user": None, "system": None, "legacy": {"layout_bbox": OLD_BOX}, "legacy_released": True}
    down = mig.from_v2(released)
    assert _old_applied_box(down, bbox) == bbox  # 이전 코드도 원본 bbox
    assert _applied_box({"layout_adjust": mig.to_v2(down), "bbox": bbox}) == bbox
    with_system = {**released, "system": {"font_scale": 0.9}}
    assert _old_applied_box(mig.from_v2(with_system), bbox) == bbox


def test_unknown_box_renders_through_preview_and_final(env, monkeypatch):
    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    c, job, b, _, _ = _setup(env)
    sid = b["sectionId"]
    _set_raw(b["id"], {"layout_bbox": OLD_BOX})
    # 번역문 저장으로 새 시도를 만든다 — 지문·입력이 이전 박스로 고정된다
    assert _patch(c, job, b["id"], {"trans1": "Visibly smoother skin every day", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["isCurrent"] is True and _render_block(s, b["id"])["box"] == OLD_BOX
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    fb = [x for sec in _final_manifest(job)["sections"] for x in sec["blocks"] if x["block_id"] == b["id"]][0]
    assert fb["box"] == OLD_BOX and "font_scale" not in fb
    assert c.get(f"/v1/jobs/{job}/deliverables").json()["deliverables"][0]["renderStatus"] == "done"


# ── 이전 값 변환 ──────────────────────────────────────────────────────────────────────────────
def _migration():
    path = ROOT / "migrations" / "versions" / "0013_layout_adjust.py"
    spec = importlib.util.spec_from_file_location("mig_layout_adjust", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


USER_V1 = {"schema_version": "1", "layout_bbox": {"x": 10, "y": 20, "w": 220, "h": 80.5}, "updated_by": "user"}
CONVERSIONS = [
    (USER_V1, {"user": {"layout_bbox": USER_V1["layout_bbox"], "font_scale": None}, "system": None}),
    ({"font_scale": 1.2, "updated_by": "user"}, {"user": {"layout_bbox": None, "font_scale": 1.2}, "system": None}),
    ({"font_scale": 0.85, "line_break_applied": True}, {"user": None, "system": {"font_scale": 0.85, "line_break_applied": True}}),
    ({"line_break_applied": False, "updated_by": "system"}, {"user": None, "system": {"line_break_applied": False}}),
]
LEGACY = [
    {"layout_bbox": {"x": 1, "y": 1, "w": 5, "h": 5}},  # updated_by 없음 — 셀러인지 모른다
    {"layout_bbox": {"x": 1, "y": 1, "w": 5, "h": 5}, "updated_by": "system"},
    {"layout_bbox": {"x": 1, "y": 1, "w": 5}, "updated_by": "user"},
    {"layout_bbox": "0,0,5,5", "updated_by": "user"},
    {"font_scale": -1},
    {"font_scale": True},
    {"line_break_applied": "yes"},
    {"font_scale": 1.2, "updated_by": "user", "other": 1},
    {"note": "x"},
    {},
    ["layout_bbox"],
    "x",
    7,
]


@pytest.mark.parametrize("old,want", CONVERSIONS)
def test_conversion_known_shapes(old, want):
    assert _migration().to_v2(old) == {"schema_version": 2, **want}
    # 앱 파서가 같은 뜻으로 읽는다
    st = la.parse(_migration().to_v2(old))
    assert st.legacy is None


@pytest.mark.parametrize("old", LEGACY)
def test_conversion_unknown_values_go_to_legacy_and_restore_exactly(old):
    m = _migration()
    new = m.to_v2(old)
    assert new == {"schema_version": 2, "user": None, "system": None, "legacy": old}
    assert m.from_v2(new) == old
    st = la.parse(new)
    assert st.user_box is None and st.user_scale is None and st.system is None and st.legacy == old


def test_downgrade_restore_range():
    m = _migration()
    assert m.from_v2(m.to_v2(USER_V1)) == USER_V1
    both = {"schema_version": 2, "user": {"layout_bbox": {"x": 1, "y": 1, "w": 5, "h": 5}, "font_scale": 1.5}, "system": {"font_scale": 0.9}, "legacy": {"note": "x"}}
    assert m.from_v2(both) == {"schema_version": "1", "updated_by": "user", "layout_bbox": {"x": 1, "y": 1, "w": 5, "h": 5},
                               "font_scale": 1.5, "system": {"font_scale": 0.9}, "legacy": {"note": "x"}}
    assert m.from_v2({"schema_version": 2, "user": None, "system": {"font_scale": 0.9}}) == {"font_scale": 0.9}
    assert m.from_v2({"schema_version": 2, "user": None, "system": None}) is None
    # 셀러 배율이 있는 값은 다시 올려도 셀러 조정으로 돌아온다(이전 서버 코드는 배율을 적용하지 않는다)
    v1 = m.from_v2({"schema_version": 2, "user": {"layout_bbox": None, "font_scale": 1.5}, "system": None})
    assert m.to_v2(v1)["user"]["font_scale"] == 1.5


def test_migration_on_real_rows_up_and_down():
    """실제 DB — 이전 값이 있는 행을 올리고 내려서 복원 범위를 확인한다."""
    from tests.exec_db import ADMIN_URL  # noqa: F401

    with fresh_database("head") as url:
        cfg = alembic_config()
        current = _migration().down_revision
        command.downgrade(cfg, current)
        eng = create_engine(url)
        values = [USER_V1, {"font_scale": 0.85, "line_break_applied": True}, {"layout_bbox": {"x": 1, "y": 1, "w": 5, "h": 5}}, ["layout_bbox"], "x", None]
        ids = []
        with eng.begin() as c:
            job = seed_job(c)["job"]
            sec = c.execute(text("INSERT INTO section (job_id, source_image_id, section_order) VALUES (:j, 0, 1) RETURNING id"), {"j": job}).scalar()
            for i, v in enumerate(values):
                ids.append(c.execute(text("INSERT INTO text_block (section_id, block_order, auto_adjust, revision) "
                                          "VALUES (:s, :o, CAST(:a AS jsonb), 3) RETURNING id"),
                                     {"s": sec, "o": i + 1, "a": None if v is None else json.dumps(v)}).scalar())
        command.upgrade(cfg, "head")
        with eng.connect() as c:
            cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'text_block'"))}
            assert "layout_adjust" in cols and "auto_adjust" not in cols
            got = {r[0]: r[1] for r in c.execute(text("SELECT id, layout_adjust FROM text_block"))}
            assert all(r[0] == 3 for r in c.execute(text("SELECT revision FROM text_block")))  # 변환이 revision·지문을 건드리지 않는다
        assert la.parse(got[ids[0]]).user_box == USER_V1["layout_bbox"]
        assert la.auto_view(la.parse(got[ids[1]])) == {"fontScale": 0.85, "lineBreakApplied": True}
        assert got[ids[2]]["legacy"] == values[2] and la.parse(got[ids[2]]).placement_source({}) == values[2]["layout_bbox"]
        assert got[ids[3]]["legacy"] == values[3] and got[ids[4]]["legacy"] == "x" and got[ids[5]] is None
        command.downgrade(cfg, current)
        with eng.connect() as c:
            back = {r[0]: r[1] for r in c.execute(text("SELECT id, auto_adjust FROM text_block"))}
        assert [back[i] for i in ids] == values
        command.upgrade(cfg, "head")  # 다시 올려도 같다
        with eng.connect() as c:
            again = {r[0]: r[1] for r in c.execute(text("SELECT id, layout_adjust FROM text_block"))}
        assert again == got
        eng.dispose()
