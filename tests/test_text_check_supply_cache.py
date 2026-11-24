"""규칙 검사 용어집 후보 재사용(app.text_check_supply — 11-19 dev GET /blocks 500 성능 수정).

같은 후보 내용이면 행 · Prepared 를 다시 만들지 않지만, 공급 결과(용어 · 버전 · 상태 · 기록 · 오류 문구)는 재사용 전과 완전히 같아야 하고,
용어집 내용이 하나라도 바뀌면 새로 읽어야 한다. 실제 PostgreSQL 용어집 행으로 본다(test_text_check_real_providers 와 같은 고정물).
"""
from __future__ import annotations

import copy

import pytest
from sqlalchemy import text

from app import db as app_db
from app import text_check_supply as tcs
from app.flows import downstream, text_check
from app.text_check_supply import GlossarySelectionSupply, clear_candidate_cache
from tests.exec_db import requires_db
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401
from tests.test_text_check_connect import _word_block
from tests.test_text_check_real_providers import _n5, _run, _term, real_on  # noqa: F401

pytestmark = requires_db


def _section_targets(db, sid):
    return text_check.checkable(downstream._block_rows(db, sid))


def _supply(db, run, sid):
    return GlossarySelectionSupply().glossary(db, run, sid, _section_targets(db, sid), origin="test")


def _setup(env):
    ids = _n5(env)
    blk, word = _word_block(ids)
    gid = _term(word, n=81)
    return ids, blk, gid


def test_reused_supply_is_identical_to_fresh_and_skips_rebuild(env, real_on, monkeypatch):
    ids, blk, gid = _setup(env)
    run, sid = _run(ids["job"]), blk["section_id"]
    calls = []
    real_derive = tcs._derive
    monkeypatch.setattr(tcs, "_derive", lambda rows: calls.append(len(rows)) or real_derive(rows))
    db = app_db.SessionLocal()
    try:
        clear_candidate_cache()
        first = _supply(db, run, sid)
        rows1 = GlossarySelectionSupply().candidate_rows(db, "en", downstream._glossary_categories(db, sid))
        second = _supply(db, run, sid)
        rows2 = GlossarySelectionSupply().candidate_rows(db, "en", downstream._glossary_categories(db, sid))
        assert rows1 is rows2  # 같은 후보 내용 — 같은 목록을 다시 쓴다
        assert len(calls) == 1  # Prepared · Entry 계산은 처음 한 번만
        clear_candidate_cache()
        fresh = _supply(db, run, sid)
        assert len(calls) == 2
    finally:
        db.close()
    assert first == second == fresh  # 용어 · 버전 · 상태 · 국소 오류 · 공급 기록까지 같다
    assert first["status"] == "ok" and [t["glossary_id"] for t in first["terms"]] == [str(gid)]


def test_glossary_change_is_seen_immediately(env, real_on):
    ids, blk, gid = _setup(env)
    run, sid = _run(ids["job"]), blk["section_id"]
    db = app_db.SessionLocal()
    try:
        clear_candidate_cache()
        before = _supply(db, run, sid)
        _q("UPDATE glossary SET term_target = 'ZZT9-CHANGED' WHERE id = :i", i=gid)  # 내용 변경 — 지문이 바뀐다
        db.rollback()
        after = _supply(db, run, sid)
        clear_candidate_cache()
        fresh = _supply(db, run, sid)
        _q("UPDATE glossary SET retired_at = now() WHERE id = :i", i=gid)  # 은퇴 — 후보에서 빠진다
        db.rollback()
        retired = _supply(db, run, sid)
    finally:
        db.close()
    assert [t["term_target"] for t in before["terms"]] == ["ZZT9"]
    assert [t["term_target"] for t in after["terms"]] == ["ZZT9-CHANGED"] and after == fresh
    assert after["version"] != before["version"]
    assert retired["terms"] == [] and str(gid) not in {r["glossary_id"] for r in retired["supply_record"]["candidates"]["rows"]}


def test_prepared_failure_is_reported_the_same_from_reuse(env, real_on):
    """Prepared 를 만들 수 없는 후보(빈 term_kind)면 재사용 때도 같은 단계 · 같은 문구로 실패를 기록한다."""
    from pipeline import glossary as gl

    ids, blk, gid = _setup(env)
    run, sid = _run(ids["job"]), blk["section_id"]
    try:
        with app_db.engine.begin() as c:
            bad = c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, "
                                 "term_kind, version) VALUES ('T9빈종류', 'ZZT9B', 'en', 'common', 'enforced', 'GL-T982', ' ', 1) RETURNING id")
                            ).scalar_one()
    except Exception as e:  # noqa: BLE001 — DB 제약이 빈 term_kind 를 막으면 이 경로는 생기지 않는다
        pytest.skip(f"빈 term_kind 를 넣을 수 없다(DB 제약): {type(e).__name__}")
    db = app_db.SessionLocal()
    try:
        clear_candidate_cache()
        miss = _supply(db, run, sid)
        hit = _supply(db, run, sid)
    finally:
        db.close()
    with pytest.raises(gl.GlossaryDataError) as want:
        gl.Prepared([gl.Entry(str(bad), "T9빈종류", "ZZT9B", "enforced", " ")])
    assert miss == hit and miss["status"] == "failed"
    assert miss["supply_record"]["check_selection"]["error"] == str(want.value)
    assert miss["supply_record"]["translation_selection"]["error"] == str(want.value)


def _fp(cats):
    db = app_db.SessionLocal()
    try:
        return tcs._candidate_fingerprint(db, "en", cats)
    finally:
        db.close()


def test_fingerprint_changes_on_every_result_field(env, real_on):
    """[검토 1] 추가 · 삭제 · 은퇴와 결과에 쓰이는 열(용어 · 번역어 · 강제 · 분류 · 버전 · 종류) 변경마다 지문이 바뀐다.
    한 행이라도 다르면 이전 어느 지문과도 겹치지 않아야 한다(옛 행을 다시 쓰지 않는다)."""
    cats = ["common", "G01"]
    base = _fp(cats)
    gid = _term("지문시험", n=83)  # 추가
    seen = [base, _fp(cats)]
    for sql in (
        "UPDATE glossary SET term_ko = '지문시험2' WHERE id = :i",
        "UPDATE glossary SET term_target = 'ZZT9-F' WHERE id = :i",
        "UPDATE glossary SET enforcement = 'reference' WHERE id = :i",
        "UPDATE glossary SET internal_category = 'G01' WHERE id = :i",  # 같은 후보 범위 안에서 분류만 이동
        "UPDATE glossary SET version = version + 1 WHERE id = :i",
        "UPDATE glossary SET term_kind = NULL WHERE id = :i",
    ):
        _q(sql, i=gid)
        seen.append(_fp(cats))
    assert len(set(seen)) == len(seen)  # 단계마다 모두 다르다
    _q("UPDATE glossary SET retired_at = now() WHERE id = :i", i=gid)  # 은퇴 — 후보에서 빠진다
    assert _fp(cats) == base  # 추가 전과 같은 후보 내용 = 같은 지문(같은 행이므로 재사용해도 결과 동일)
    _q("UPDATE glossary SET retired_at = NULL WHERE id = :i", i=gid)
    assert _fp(cats) == seen[-1]
    _q("DELETE FROM glossary WHERE id = :i", i=gid)  # 행 삭제
    assert _fp(cats) == base
    db = app_db.SessionLocal()
    try:  # 행과 함께 읽은 지문 = 지문만 읽은 값(후보 0건 포함)
        rows, read_fp = tcs._read_candidates(db, "en", cats)
        assert read_fp == base and [r["id"] for r in rows] == sorted(r["id"] for r in rows)
        empty, empty_fp = tcs._read_candidates(db, "en", ["__no_such_category__"])
        assert empty == [] and empty_fp == tcs._candidate_fingerprint(db, "en", ["__no_such_category__"])
    finally:
        db.close()


def test_stored_under_the_fingerprint_of_rows_actually_read(env, real_on, monkeypatch):
    """[검토 2] 지문 조회와 후보 조회 사이에 용어집이 바뀌어도, 저장 키는 실제로 읽은 행의 지문이다(행 · 키 어긋남 없음)."""
    ids, blk, gid = _setup(env)
    sid = blk["section_id"]
    real_fp = tcs._candidate_fingerprint
    monkeypatch.setattr(tcs, "_candidate_fingerprint", lambda db, lang, cats: "0:stale-before-change")
    db = app_db.SessionLocal()
    try:
        cats = downstream._glossary_categories(db, sid)
        clear_candidate_cache()
        rows = GlossarySelectionSupply().candidate_rows(db, "en", cats)
        assert [k[2] for k in tcs._cache] == [real_fp(db, "en", cats)]  # 앞선(바뀌기 전) 지문으로 저장하지 않는다
        monkeypatch.setattr(tcs, "_candidate_fingerprint", real_fp)
        assert GlossarySelectionSupply().candidate_rows(db, "en", cats) is rows  # 현재 지문으로 적중
        _q("UPDATE glossary SET term_target = 'ZZT9-R' WHERE id = :i", i=gid)
        db.rollback()
        again = GlossarySelectionSupply().candidate_rows(db, "en", cats)
        assert again is not rows and {r["term_target"] for r in again if r["id"] == gid} == {"ZZT9-R"}
    finally:
        db.close()


def test_concurrent_misses_agree_and_leave_one_entry(env, real_on):
    """[검토 3] 동시에 놓친 요청은 각자 계산할 수 있지만(중복 계산 허용) 결과는 같고, 항목은 하나만 남는다."""
    import threading

    ids, blk, gid = _setup(env)
    run, sid = _run(ids["job"]), blk["section_id"]
    clear_candidate_cache()
    n = 4
    gate, out, errs = threading.Barrier(n), [None] * n, []

    def work(i):
        db = app_db.SessionLocal()
        try:
            gate.wait()
            out[i] = _supply(db, run, sid)
        except Exception as e:  # noqa: BLE001
            errs.append(e)
        finally:
            db.close()

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    assert not errs and all(o == out[0] for o in out) and out[0]["status"] == "ok"
    assert len(tcs._cache) == 1
    db = app_db.SessionLocal()
    try:
        assert _supply(db, run, sid) == out[0]
    finally:
        db.close()


def test_returned_record_is_not_shared_with_cache(env, real_on):
    """[검토 3] 밖으로 나간 공급 기록을 바꿔도 다음 요청 결과는 바뀌지 않는다(기록 행은 요청마다 새 객체)."""
    ids, blk, gid = _setup(env)
    run, sid = _run(ids["job"]), blk["section_id"]
    db = app_db.SessionLocal()
    try:
        clear_candidate_cache()
        first = _supply(db, run, sid)
        keep = copy.deepcopy(first)
        for r in first["supply_record"]["candidates"]["rows"]:
            r["term_target"] = "MUTATED"
        first["supply_record"]["candidates"]["rows"].clear()
        first["terms"].clear()
        assert _supply(db, run, sid) == keep
    finally:
        db.close()


def test_recovers_after_data_fix_and_transient_errors(env, real_on, monkeypatch):
    """[검토 4] 실패를 저장해 둔 후보도 데이터가 고쳐지면 바로 정상으로 돌아오고, 일시 오류(DB · 계산 예외)는 저장되지 않는다."""
    from sqlalchemy.exc import OperationalError

    ids, blk, gid = _setup(env)
    run, sid = _run(ids["job"]), blk["section_id"]
    with app_db.engine.begin() as c:
        bad = c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, "
                             "term_kind, version) VALUES ('T9회복', 'ZZT9H', 'en', 'common', 'enforced', 'GL-T984', ' ', 1) RETURNING id")
                        ).scalar_one()
    db = app_db.SessionLocal()
    try:
        clear_candidate_cache()
        assert _supply(db, run, sid)["status"] == "failed"
        assert _supply(db, run, sid)["status"] == "failed"  # 저장된 실패 재사용
        _q("UPDATE glossary SET term_kind = NULL WHERE id = :i", i=bad)  # 데이터 수정
        db.rollback()
        fixed = _supply(db, run, sid)
        assert fixed["status"] == "ok" and str(gid) in [t["glossary_id"] for t in fixed["terms"]]

        clear_candidate_cache()
        real_read, real_derive = tcs._read_candidates, tcs._derive
        state = {"read": 1, "derive": 1}

        def flaky_read(*a):
            if state["read"]:
                state["read"] -= 1
                raise OperationalError("SELECT", {}, Exception("일시 장애"))
            return real_read(*a)

        def flaky_derive(rows):
            if state["derive"]:
                state["derive"] -= 1
                raise RuntimeError("계산 예외")
            return real_derive(rows)

        monkeypatch.setattr(tcs, "_read_candidates", flaky_read)
        monkeypatch.setattr(tcs, "_derive", flaky_derive)
        with pytest.raises(OperationalError):
            _supply(db, run, sid)
        db.rollback()
        assert not tcs._cache  # DB 오류는 저장하지 않는다
        with pytest.raises(RuntimeError):
            _supply(db, run, sid)
        assert all(c.derived is None for c in tcs._cache.values())  # 계산 예외도 저장하지 않는다
        assert _supply(db, run, sid) == fixed  # 다음 요청은 정상
    finally:
        db.close()


def test_cache_size_is_bounded(env, real_on):
    """[검토 5] 항목은 최대 3개. 밀려난 항목의 행 · Prepared 는 실제로 해제되고, 캐시 밖 행으로 만든 계산값은 보관하지 않는다."""
    import gc
    import weakref

    db = app_db.SessionLocal()
    try:
        clear_candidate_cache()
        refs = []
        for cat in ("common", "G01", "G02", "G03", "G05"):
            rows = GlossarySelectionSupply().candidate_rows(db, "en", [cat])
            refs.append(weakref.ref(tcs._derived(rows)))
            del rows
        assert len(tcs._cache) == tcs._CANDIDATE_CACHE_SIZE
        assert all(c.derived is not None for c in tcs._cache.values())
        tcs._derived([dict(r) for r in next(iter(tcs._cache.values())).rows])  # 캐시 밖 목록 — 계산만 하고 보관하지 않는다
        gc.collect()
        assert [r() is None for r in refs] == [True, True, False, False, False]  # 밀려난 2개만 해제
        assert {id(c.derived) for c in tcs._cache.values()} == {id(r()) for r in refs[2:]}
    finally:
        db.close()
        clear_candidate_cache()
