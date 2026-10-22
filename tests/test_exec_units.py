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
    """승인된 역할별 기본값 정본(D11) — 운영과 같은 로더로 읽는다."""
    return typeset.load_role_defaults()


def _blk(i, style, *, role="body", text="ok", stage="done", source="원문", box=None):
    return typeset.RenderBlock(id=i, role=role, text=text, box=box or {"x": 0, "y": 0, "w": 300, "h": 100}, style=style,
                               style_stage=stage, source_ko=source)


def test_typeset_overflow_width_and_height():
    bg = png_bytes(300, 200)
    style = {"font_color": "#112233", "bg_color": "#FFFFFF", "est_font_px": 20.0, "align": "center"}
    blocks = [
        _blk(1, style, text="ok", box={"x": 10, "y": 10, "w": 200, "h": 40}),
        _blk(2, style, text="Supercalifragilistic", box={"x": 10, "y": 60, "w": 30, "h": 40}),
        _blk(3, style, text="a b c d e f g h i j k l", box={"x": 10, "y": 110, "w": 20, "h": 20}),
    ]
    res = typeset.render(bg, blocks, _defaults())
    assert (res.width, res.height) == (300, 200)
    assert res.blocks[1].overflow is False and res.blocks[2].overflow is True and res.blocks[3].overflow is True
    assert res.blocks[1].source == {"font_color": "measured", "est_font_px": "measured", "align": "measured"}


def test_typeset_null_measurements_use_approved_defaults_only_for_null_fields():
    bg = png_bytes(1000, 400)
    # F1 작업 15 재현(합성): ⑦ 완료, align 만 NULL → align 만 승인 기본값, 측정된 글자색·크기는 그대로
    res = typeset.render(bg, [_blk(1, {"font_color": "#424B55", "bg_color": "#FEFFFF", "est_font_px": 70.2, "align": None}, role="title")],
                         _defaults())
    assert res.blocks[1].applied == {"font_color": "#424B55", "est_font_px": 70.2, "align": "left"}
    assert res.blocks[1].source == {"font_color": "measured", "est_font_px": "measured", "align": "role_default"}
    # 측정값 3개가 모두 있으면 기본값과 다르더라도 그대로(덮지 않는다)
    res = typeset.render(bg, [_blk(2, {"font_color": "#C2CA6B", "bg_color": None, "est_font_px": 9.5, "align": "right"}, role="title")],
                         _defaults())
    assert res.blocks[2].applied == {"font_color": "#C2CA6B", "est_font_px": 9.5, "align": "right"}
    assert set(res.blocks[2].source.values()) == {"measured"}


def test_typeset_style_stage_failed_uses_all_defaults_scaled_by_source_width():
    # ⑦ 실패(style 객체 NULL) → 세 항목 모두 승인 기본값. 크기 = 기준 크기 × 원본 폭 / 기준 폭(반올림 없음, be-handoff 6.3)
    res = typeset.render(png_bytes(1000, 400), [_blk(1, None, role="title", stage="failed")], _defaults())
    assert res.blocks[1].applied == {"font_color": "#222222", "est_font_px": 32 * 1000 / 375, "align": "left"}
    assert set(res.blocks[1].source.values()) == {"role_default"}


def test_typeset_skips_non_targets_and_rejects_contract_violations():
    bg = png_bytes(300, 200)
    measured = {"font_color": "#111111", "bg_color": None, "est_font_px": 20.0, "align": "left"}
    # 빈 번역문·빈 원문은 그리지 않는다(기본값으로 되살리지 않음)
    res = typeset.render(bg, [_blk(1, measured, text=""), _blk(2, measured, source="  ")], _defaults())
    assert res.blocks == {}
    # ⑦ 완료인데 style 없음 / ⑦ 실패인데 style 있음 / 알 수 없는 단계 → 계약 위반(기본값으로 덮지 않는다)
    for blk in (_blk(3, None, stage="done"), _blk(4, measured, stage="failed"), _blk(5, measured, stage=None)):
        with pytest.raises(typeset.StyleInputInvalid) as e:
            typeset.render(bg, [blk], _defaults())
        assert e.value.code == "STYLE_RESULT_INVALID"


def test_style_defaults_unreadable_or_font_mismatch_is_not_silently_ignored(monkeypatch, tmp_path):
    import shutil

    from pipeline.handoff.style_defaults import STYLE_DEFAULTS_PATH

    # 대체 경로가 없는 파일
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", str(tmp_path / "nope.json"))
    with pytest.raises(typeset.StyleDefaultsUnavailable):
        typeset.load_role_defaults()
    assert "unavailable" in typeset.style_inputs()
    # 자료는 같지만 그 위치에 폰트 파일이 없음(폰트 검증 실패 → 내장 폰트로 넘어가지 않는다)
    copy = tmp_path / "style_defaults.json"
    shutil.copy(STYLE_DEFAULTS_PATH, copy)
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", str(copy))
    with pytest.raises(typeset.StyleDefaultsUnavailable) as e:
        typeset.load_role_defaults()
    assert "폰트" in str(e.value)
    # 대체 경로를 비우면 저장소 정본 — 지문은 설정 버전·SHA-256·폰트 SHA-256·규칙 버전(6.6 ⑤)
    monkeypatch.delenv("PIXLATE_STYLE_DEFAULTS")
    inputs = typeset.style_inputs()
    assert inputs["style_defaults"]["version"] == "role-style@2026-10-12.1" and set(inputs) == {"style_defaults", "fonts", "rule"}


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
