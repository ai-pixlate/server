"""실행 모듈 단위 — 정규 직렬화·조판·갱신 루프. DB 가 필요한 것은 PIXLATE_TEST_DATABASE_URL 이 있을 때만."""
from __future__ import annotations

import time

import pytest

from app import typeset
from app.manifest import ManifestError, canonical_json, fingerprint
from tests.exec_fakes import png_bytes


def test_canonical_json_sorts_utf16_and_keeps_strings():
    # U+E000(BMP 사설) 과 U+1F600(보조 평면): 코드 포인트 순과 UTF-16 코드 단위 순이 다르다(JCS 3.2.3)
    obj = {"": 1, "\U0001f600": 2, "a": None, "b": [True, False, "줄\n바꿈"]}
    assert canonical_json(obj) == '{"a":null,"b":[true,false,"줄\\n바꿈"],"😀":2,"":1}'
    assert fingerprint({"x": 1, "y": 2}) == fingerprint({"y": 2, "x": 1})
    assert fingerprint({"x": None}) != fingerprint({})  # NULL 과 키 누락 구분


def test_canonical_json_matches_ai_handoff_and_rejects_lone_surrogates():
    from pipeline.handoff.canonical import sha256_canonical

    # BE·AI 가 같은 JCS 구현을 쓴다 — 실수·큰 정수에서도 지문이 같아야 BE 고정 지문과 AI 계산 지문을 대조할 수 있다
    obj = {"score": 0.5, "t": 1e21, "n": 2**60, "s": "줄\n바꿈"}
    assert canonical_json({"score": 0.5}) == '{"score":0.5}'
    assert fingerprint(obj) == sha256_canonical(obj)
    with pytest.raises(ManifestError):
        canonical_json({"score": float("nan")})
    with pytest.raises(ManifestError):
        canonical_json({"s": "\ud800"})


def test_lease_repr_hides_token():
    # PR #56 AI 리뷰 4: 시도 토큰 원문이 repr·로그·예외 메시지에 찍히지 않는다(R17)
    from app.execution import Lease

    lease = Lease(attempt_id=1, job_id=2, run_id=3, stage="inpaint", unit_type="section", unit_id=4, epoch=1, owner="w",
                  manifest={}, fingerprint="0" * 64, token="secret-token-value")
    assert "secret-token-value" not in repr(lease) and "secret-token-value" not in str(lease)
    assert lease.token == "secret-token-value"


def _defaults():
    roles = {r: {"font_color": "#000000", "est_font_px": 20, "align": "left"} for r in typeset.ROLES}
    return typeset.RoleDefaults(version="t", roles=roles, sha256="0" * 64)


def test_typeset_overflow_width_and_height():
    bg = png_bytes(300, 200)
    style = {"font_color": "#112233", "bg_color": "#FFFFFF", "est_font_px": 20.0, "align": "center"}
    blocks = [
        typeset.RenderBlock(id=1, role="body", text="ok", box={"x": 10, "y": 10, "w": 200, "h": 40}, style=style),
        typeset.RenderBlock(id=2, role="body", text="Supercalifragilistic", box={"x": 10, "y": 60, "w": 30, "h": 40}, style=style),
        typeset.RenderBlock(id=3, role="body", text="a b c d e f g h i j k l", box={"x": 10, "y": 110, "w": 20, "h": 20}, style=style),
    ]
    res = typeset.render(bg, blocks, None)
    assert (res.width, res.height) == (300, 200)
    assert res.blocks[1].overflow is False and res.blocks[2].overflow is True and res.blocks[3].overflow is True
    assert res.blocks[1].source == {"font_color": "measured", "est_font_px": "measured", "align": "measured"}


def test_typeset_requires_approved_defaults_for_null_measurements():
    bg = png_bytes(100, 100)
    b = typeset.RenderBlock(id=1, role="title", text="x", box={"x": 0, "y": 0, "w": 100, "h": 50},
                            style={"font_color": None, "bg_color": None, "est_font_px": 18.0, "align": None})
    with pytest.raises(typeset.StyleDefaultsUnavailable):
        typeset.render(bg, [b], None)
    res = typeset.render(bg, [b], _defaults())
    assert res.blocks[1].applied["font_color"] == "#000000" and res.blocks[1].source["font_color"] == "role_default"
    assert res.blocks[1].applied["est_font_px"] == 18.0 and res.blocks[1].source["est_font_px"] == "measured"


@pytest.mark.skipif(not __import__("os").getenv("PIXLATE_TEST_DATABASE_URL"), reason="PIXLATE_TEST_DATABASE_URL 없음")
def test_heartbeat_marks_lost_after_cancel():
    from sqlalchemy import text

    from app import db as app_db
    from app import execution
    from app.flows.common import Heartbeat
    from tests.exec_db import bound_app_db, fresh_database, seed_job

    with fresh_database() as u, bound_app_db(u):
        with app_db.engine.begin() as c:
            ids = seed_job(c)
        db = app_db.SessionLocal()
        try:
            execution.lock_job(db, ids["job"])
            run = execution.create_run(db, ids["job"], "downstream", {"x": 1})
            r = execution.lock_task(db, run)
            att = execution.create_attempt(db, run=r, stage="label", unit_id=1, manifest={"m": 1}, target_count=0)
            db.commit()
        finally:
            db.close()
        lease = execution.acquire(att, "w")
        with Heartbeat(lease, interval_s=0.05) as hb:
            time.sleep(0.15)
            assert hb.lost is False
            db = app_db.SessionLocal()
            try:
                execution.lock_job(db, ids["job"])
                execution.cancel_run(db, run)
                db.commit()
            finally:
                db.close()
            time.sleep(0.3)
            assert hb.lost is True
        with app_db.engine.begin() as c:
            assert c.execute(text("SELECT status FROM job_async_task WHERE id = :t"), {"t": att}).scalar() == "cancelled"
