"""기존 데이터가 있는 0012 DB → 0013 업그레이드 → API 저장 → 0012 downgrade → 재업그레이드 경로(일회용 시험 DB).

`test_block_layout_adjust.test_migration_on_real_rows_up_and_down`은 DB 값의 변환·복원만 본다. 여기서는 실제 작업의 블록에
이전 값을 넣고 올린 뒤 API(/blocks·/preview·PATCH)로 legacy 보존·명시적 해제·셀러 조정을 거치고, 다시 내렸을 때
이전 서버 규칙으로 어떤 박스가 되는지와 다시 올렸을 때의 분류를 확인한다. 뒤의 두 단계는 마이그레이션 머리말
downgrade 표의 한계를 그대로 고정한 것이며, 바람직한 동작이라는 뜻이 아니다.
"""
from __future__ import annotations

import json

from alembic import command

from app import layout_adjust as la
from app.flows.downstream import _applied_box
from tests.exec_db import alembic_config, requires_db
from tests.test_block_layout_adjust import OLD_BOX, _block, _body, _migration, _old_applied_box, _patch, _render_block
from tests.test_exec_downstream import _pvs, _q, _to_n5, dburl, env  # noqa: F401

pytestmark = requires_db

ROLES = ["user_box", "user_scale", "system", "legacy_scale", "legacy_clear", "legacy_kept", "null_then_seller", "null_kept"]


def _targets(env, n):
    """렌더 대상 블록 n개 — (client, job, block, section w, h). 한 작업에 모자라면 작업을 더 만든다."""
    out = []
    while len(out) < n:
        ids, _ = _to_n5(env)
        c, job = ids["c"], ids["job"]
        for b in c.get(f"/v1/jobs/{job}/blocks").json():
            if b["trans1"] and len(out) < n:
                s = _pvs(c, job, b["sectionId"])["renderInput"]["section"]
                out.append((c, job, b, s["width"], s["height"]))
    return out


def _auto(bid):
    return _q("SELECT auto_adjust::text AS v FROM text_block WHERE id = :b", b=bid)[0]["v"]


def _layout(bid):
    v = _q("SELECT layout_adjust::text AS v FROM text_block WHERE id = :b", b=bid)[0]["v"]
    return None if v is None else json.loads(v)


def _pv_block(c, job, b):
    return _render_block(_pvs(c, job, b["sectionId"]), b["id"])


def test_existing_0012_rows_upgrade_then_api_then_downgrade(env, dburl, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", dburl)  # migrations/env.py 가 읽는다 — 이 모듈의 일회용 DB
    cfg = alembic_config()
    mig = _migration()
    t = dict(zip(ROLES, _targets(env, len(ROLES))))
    bbox = {k: v[2]["bbox"] for k, v in t.items()}
    seller_box = {k: {"x": 2, "y": 3, "w": max(1, min(v[3] - 4, v[2]["bbox"]["w"])), "h": max(1, min(v[4] - 5, v[2]["bbox"]["h"]))}
                  for k, v in t.items()}
    old = {
        "user_box": {"schema_version": "1", "layout_bbox": seller_box["user_box"], "updated_by": "user"},
        "user_scale": {"font_scale": 1.2, "updated_by": "user"},
        "system": {"font_scale": 0.85, "line_break_applied": True},
        "legacy_scale": {"layout_bbox": OLD_BOX},
        "legacy_clear": {"layout_bbox": OLD_BOX},
        "legacy_kept": {"layout_bbox": OLD_BOX},
        "null_then_seller": None,
        "null_kept": None,
    }

    # 1) 0012 로 내리고 이전 값을 넣는다 — revision 은 그대로
    command.downgrade(cfg, mig.down_revision)
    rev = {}
    for k, (_, _, b, _, _) in t.items():
        _q("UPDATE text_block SET auto_adjust = CAST(:a AS jsonb) WHERE id = :b",
           a=None if old[k] is None else json.dumps(old[k]), b=b["id"])
        rev[k] = _q("SELECT revision FROM text_block WHERE id = :b", b=b["id"])[0]["revision"]

    # 2) 0013 으로 올린다 — 변환값, 응답, 적용 박스가 4절 전후 표와 같다
    command.upgrade(cfg, "head")
    up = {k: _layout(v[2]["id"]) for k, v in t.items()}
    for k, (c, job, b, _, _) in t.items():
        assert _q("SELECT revision FROM text_block WHERE id = :b", b=b["id"])[0]["revision"] == rev[k], k
        assert up[k] == (None if old[k] is None else mig.to_v2(old[k])), k
        pv = _pv_block(c, job, b)
        assert pv["box"] == _old_applied_box(old[k], bbox[k]), k  # 박스는 이전 서버 규칙과 같다
    blk = {k: _block(v[0], v[1], v[2]["id"]) for k, v in t.items()}
    assert blk["user_box"]["layoutAdjust"] == {"box": seller_box["user_box"], "fontScale": None}
    assert blk["user_scale"]["layoutAdjust"] == {"box": None, "fontScale": 1.2}
    c, job, b, _, _ = t["user_scale"]
    assert _pv_block(c, job, b)["style"]["fontScale"] == 1.2  # 달라지는 렌더: 셀러 배율은 이제 적용
    assert blk["system"]["layoutAdjust"] is None and blk["system"]["autoAdjust"] == {"fontScale": 0.85, "lineBreakApplied": True}
    c, job, b, _, _ = t["system"]
    assert _pv_block(c, job, b)["style"]["fontScale"] == 1.0  # 시스템 값은 렌더에 미적용
    for k in ("legacy_scale", "legacy_clear", "legacy_kept"):
        assert blk[k]["layoutAdjust"] is None and up[k]["legacy"] == old[k], k  # legacy 는 보존, 셀러 조정으로 안 나옴

    # 3) API 저장 — legacy 보존·명시적 해제·셀러 조정
    def save(k, adj):
        c, job, b, _, _ = t[k]
        r = _patch(c, job, b["id"], _body(_block(c, job, b["id"]), layoutAdjust=adj))
        assert r.status_code == 200, (k, r.text)
        return r.json()

    save("legacy_scale", {"fontScale": 1.3})
    out = save("legacy_clear", None)
    assert out["rerenderTaskId"] is not None and out["block"]["layoutAdjust"] is None  # 첫 해제는 revision·재렌더
    save("system", {"box": seller_box["system"]})
    save("null_then_seller", {"box": seller_box["null_then_seller"], "fontScale": 1.1})
    saved = {k: _layout(v[2]["id"]) for k, v in t.items()}
    applied = {k: _pv_block(v[0], v[1], v[2])["box"] for k, v in t.items()}
    assert applied["legacy_scale"] == OLD_BOX and saved["legacy_scale"]["legacy"] == old["legacy_scale"]
    assert not saved["legacy_scale"].get("legacy_released")
    assert applied["legacy_clear"] == bbox["legacy_clear"] and saved["legacy_clear"]["legacy_released"] is True
    assert saved["legacy_clear"]["legacy"] == old["legacy_clear"]
    assert applied["system"] == seller_box["system"] and saved["system"]["system"] == up["system"]["system"]
    assert applied["null_then_seller"] == seller_box["null_then_seller"]
    assert applied["legacy_kept"] == OLD_BOX and saved["legacy_kept"] == up["legacy_kept"]

    # 4) 0012 로 내린다 — 복원값과, 이전 서버 규칙으로 그릴 박스
    command.downgrade(cfg, mig.down_revision)
    down = {k: (lambda v: None if v is None else json.loads(v))(_auto(v[2]["id"])) for k, v in t.items()}
    for k in ROLES:
        assert down[k] == mig.from_v2(saved[k]), k
    assert down["user_box"] == old["user_box"] and down["legacy_kept"] == old["legacy_kept"] and down["null_kept"] is None
    assert down["user_scale"] == {"schema_version": "1", "updated_by": "user", "font_scale": 1.2}  # schema_version "1" 표기로 복원
    old_rule = {k: _old_applied_box(down[k], bbox[k]) for k in ROLES}
    assert old_rule["user_box"] == seller_box["user_box"] and old_rule["user_scale"] == bbox["user_scale"]
    assert old_rule["system"] == seller_box["system"] and old_rule["null_then_seller"] == seller_box["null_then_seller"]
    assert old_rule["legacy_clear"] == bbox["legacy_clear"]  # 해제한 박스는 이전 코드에서도 되살아나지 않는다
    assert old_rule["legacy_kept"] == OLD_BOX
    # 한계: legacy 위에 셀러 배율만 저장한 행은 legacy 가 보존 키로 내려가 이전 코드가 원본 bbox 로 그린다
    assert down["legacy_scale"]["legacy"] == old["legacy_scale"] and old_rule["legacy_scale"] == bbox["legacy_scale"]

    # 5) 다시 올린다 — 셀러·시스템이 섞였거나 보존 키가 있는 값은 출처 불명(legacy)으로 분류된다
    command.upgrade(cfg, "head")
    again = {k: _layout(v[2]["id"]) for k, v in t.items()}
    for k in ("user_box", "user_scale", "legacy_kept", "null_kept"):
        assert again[k] == up[k], k
    assert la.parse(again["null_then_seller"]).user_box == seller_box["null_then_seller"]
    for k in ("system", "legacy_scale", "legacy_clear"):
        assert again[k]["user"] is None and again[k]["system"] is None and again[k]["legacy"] == down[k], k
    re_applied = {k: _applied_box({"layout_adjust": again[k], "bbox": bbox[k]}) for k in ROLES}
    assert re_applied["system"] == seller_box["system"]  # 박스는 legacy 규칙으로 그대로 그림(셀러 조정으로는 안 나옴)
    assert re_applied["legacy_clear"] == bbox["legacy_clear"]
    assert re_applied["legacy_scale"] == bbox["legacy_scale"]  # 한계: 원래 legacy 박스는 돌아오지 않는다
