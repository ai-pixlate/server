"""규칙 검사 결과 → 공개 신호(OpenAPI confirmReview 「규칙 검사 연결」 · contract 7.1).

- 저장: 규칙 규제 flag 의 원판정값(dictionary_verdict_status)을 compliance_flags 에 남긴다. 사전이 나중에 바뀌어도 저장값을 쓴다.
- 대응: mandatory_term_unapplied → mandatory_term_unapplied / regulatory(regulated · conditional) → prohibited_expression.
  allowed · 그 밖의 값 · 값 없음(이전 저장분)은 대응하지 않는다. 다른 producer 도 내지 않는다.
- 표시: 그 블록 검사 상태가 settled 일 때만. 블록 · 코드당 1개. 사유 등은 null, taskId · retryable 은 translation_failed 전용이라 null.
- GET /blocks 와 블록 저장 응답이 같은 규칙. 확정 확인 집합 · taskId 판정(#120)에는 섞이지 않는다.
DB 시험의 공급(M3) · 키(A1)는 test_text_check_connect 의 대역을 쓴다. 규칙 검사 자체는 실제 pipeline 코드다.
"""
from __future__ import annotations

import json

import pytest

from app.flows import text_check
from app.flows.text_check import SETTLED, _flag_db, check_signals

MT = {"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": 1, "glossary_id": 7}


def reg(status="regulated", **kw):
    f = {"type": "regulatory", "producer": "translation_validation", "revision": 1, "dictionary_id": 3, "problem_text": "cure",
         "start": 0, "end": 4, "detected_in": "translation", "detected_by": "rule"}
    if status is not None:
        f["dictionary_verdict_status"] = status
    return {**f, **kw}


SETTLED_CHECK = {"state": SETTLED, "inputKey": "k"}
CHECK_CODES = {"mandatory_term_unapplied", "prohibited_expression"}


def codes(sig):
    return [s["code"] for s in sig]


# ── 순수 함수 ─────────────────────────────────────────────────────────────
def test_flag_db_keeps_verdict_status_and_converts_ids():
    out = _flag_db({"type": "regulatory", "producer": "translation_validation", "revision": 2, "external_id": "RG-1",
                    "dictionary_verdict_status": "conditional", "start": 1, "end": 3}, {"RG-1": 55})
    assert out == {"type": "regulatory", "producer": "translation_validation", "revision": 2, "dictionary_id": 55,
                   "dictionary_verdict_status": "conditional", "start": 1, "end": 3}
    assert _flag_db({"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": 2, "glossary_id": "9"}, {}) == \
        {"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": 2, "glossary_id": 9}


@pytest.mark.parametrize("status,want", [("regulated", ["prohibited_expression"]), ("conditional", ["prohibited_expression"]),
                                         ("allowed", []), ("irrelevant", []), ("needs_fix", []), ("unknown_value", []), (None, [])])
def test_regulatory_maps_only_regulated_and_conditional(status, want):
    assert codes(check_signals([reg(status)], SETTLED_CHECK)) == want


def test_mandatory_term_maps_to_same_code_and_fields_are_null():
    assert check_signals([MT], SETTLED_CHECK) == [{"code": "mandatory_term_unapplied", "reason": None, "basisArticle": None,
                                                   "evidenceUrl": None, "taskId": None, "retryable": None}]


@pytest.mark.parametrize("check", [None, {"state": "in_progress", "inputKey": None}, {"state": "in_progress", "inputKey": "old"},
                                   {"state": "outdated", "inputKey": "old"}, {"state": "failed", "inputKey": "k"},
                                   {"state": "failed", "inputKey": None}, {"state": "missing", "inputKey": None}])
def test_only_settled_shows_signals(check):
    assert check_signals([MT, reg()], check) == []


def test_one_signal_per_code_and_order_follows_first_flag():
    flags = [reg("conditional"), MT, reg("regulated", start=5, end=9), dict(MT, glossary_id=8), reg("allowed")]
    assert codes(check_signals(flags, SETTLED_CHECK)) == ["prohibited_expression", "mandatory_term_unapplied"]


def test_other_producers_and_malformed_flags_are_ignored():
    llm = reg("regulated", producer="translation_llm_review", detected_by="llm")
    wrong_pair = dict(MT, producer="translation_validation")
    assert check_signals([llm, wrong_pair, None, "x", 3, {"type": "regulatory"}], SETTLED_CHECK) == []
    assert check_signals(None, SETTLED_CHECK) == [] and check_signals([], SETTLED_CHECK) == []
    assert check_signals(json.dumps([MT]), SETTLED_CHECK)[0]["code"] == "mandatory_term_unapplied"  # jsonb 를 문자열로 받은 경우


# ── DB · API(실제 규칙 검사 + 대역 공급 · 키) ───────────────────────────────
from tests.exec_db import requires_db  # noqa: E402
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: E402,F401
from tests.test_text_check_connect import TC, _blocks, _proceed, _word_block, rule_on  # noqa: E402,F401

needs_db = requires_db


def _target(ids):
    """검사 대상(번역문 있음 · 포함 섹션 · 라벨 아님) 블록 하나."""
    cb = [b for b in _blocks(ids).values() if b["check"] is not None]
    assert cb
    return cb[0]


def _patch(ids, bid, txt):
    b = _blocks(ids)[bid]
    r = ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{bid}", json={"trans1": txt, "revision": b["revision"]})
    assert r.status_code == 200, r.text
    return r.json()


def _flags(bid):
    return _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=bid)[0]["compliance_flags"] or []


@needs_db
def test_regulatory_status_stored_and_signal_on_blocks_and_patch(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env)
    assert _step(ids) == ("review", "N5")
    blk = _target(ids)
    # 시험 사전: RG-901 cure(regulated) · RG-902 anti-wrinkle(conditional) — 실제 규칙 검사가 번역문에서 찾는다
    resp = _patch(ids, blk["id"], "This cure is anti-wrinkle care")
    # 저장 응답 시점: 새 검사가 등록돼 in_progress — 이전 결과 · 새 결과 모두 신호로 내지 않는다
    assert resp["block"]["check"]["state"] == "in_progress"
    assert "prohibited_expression" not in codes(resp["block"]["signals"])
    env["q"].drain()
    got = _blocks(ids)[blk["id"]]
    assert got["check"]["state"] == "settled"
    regs = [f for f in _flags(blk["id"]) if f["type"] == "regulatory"]
    assert sorted(f["dictionary_verdict_status"] for f in regs) == ["conditional", "regulated"]  # 원판정값 보존(되읽기)
    assert all("external_id" not in f and isinstance(f["dictionary_id"], int) for f in regs)
    sig = [s for s in got["signals"] if s["code"] == "prohibited_expression"]
    assert sig == [{"code": "prohibited_expression", "reason": None, "basisArticle": None, "evidenceUrl": None, "taskId": None,
                    "retryable": None}]  # flag 2개 → 신호 1개
    # 같은 값 저장(no-op) 응답과 GET /blocks 가 같은 신호
    same = _patch(ids, blk["id"], got["trans1"])
    assert same["block"]["signals"] == _blocks(ids)[blk["id"]]["signals"] == got["signals"]
    # 사전이 나중에 바뀌어도 저장된 원판정값 · 신호 그대로(현재 사전 행을 다시 읽지 않는다)
    _q("UPDATE expression_dictionary SET verdict_status = 'allowed' WHERE external_id IN ('RG-901', 'RG-902')")
    assert sorted(f["dictionary_verdict_status"] for f in _flags(blk["id"]) if f["type"] == "regulatory") == ["conditional", "regulated"]
    assert "prohibited_expression" in codes(_blocks(ids)[blk["id"]]["signals"])


@needs_db
def test_mandatory_term_signal_hidden_when_not_settled(env, rule_on):
    from app import db as app_db
    from sqlalchemy import text

    ids = _to_n3(env)
    blk, word = _word_block(ids)
    with app_db.engine.begin() as c:
        c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, term_kind, "
                       "version) VALUES (:k, 'ZZTARGET', 'en', 'common', 'enforced', 'GL-C995', 'certification', 1)"), {"k": word})
    _proceed(ids, env)
    b = _blocks(ids)[blk["id"]]
    assert b["check"]["state"] == "settled" and codes(b["signals"]).count("mandatory_term_unapplied") == 1
    # 번역문 변경 → 새 검사 대기(in_progress). 이전 flag 는 DB 에 남아 있어도 신호로 내지 않는다
    resp = _patch(ids, blk["id"], "Totally different text")
    assert resp["block"]["check"]["state"] == "in_progress" and "mandatory_term_unapplied" not in codes(resp["block"]["signals"])
    assert any(f["type"] == "mandatory_term_unapplied" for f in _flags(blk["id"]))
    assert "mandatory_term_unapplied" not in codes(_blocks(ids)[blk["id"]]["signals"])
    env["q"].drain()
    after = _blocks(ids)[blk["id"]]
    assert after["check"]["state"] == "settled" and codes(after["signals"]).count("mandatory_term_unapplied") == 1  # 여전히 미적용
    # outdated: 등록 경로 없이 현재 입력(키)이 바뀜(용어 삭제) — DB 에 남은 flag 를 신호로 내지 않는다
    _q("DELETE FROM glossary WHERE external_id = 'GL-C995'")
    o = _blocks(ids)[blk["id"]]
    assert o["check"]["state"] == "outdated" and any(f["type"] == "mandatory_term_unapplied" for f in _flags(blk["id"]))
    assert "mandatory_term_unapplied" not in codes(o["signals"])


@needs_db
def test_stored_flags_without_status_or_allowed_are_not_shown(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env)
    blk = _target(ids)
    assert blk["check"]["state"] == "settled"
    # 원판정값이 없는 이전 저장분 · allowed · 다른 producer — 신호 없음. 강제 용어 flag 는 그대로 대응(검사 상태는 키로 정해져 settled 유지)
    legacy = reg(None)
    stored = [legacy, reg("allowed"), reg("regulated", producer="translation_llm_review", detected_by="llm"), MT]
    _q("UPDATE text_block SET compliance_flags = CAST(:f AS jsonb) WHERE id = :b", f=json.dumps(stored), b=blk["id"])
    got = _blocks(ids)[blk["id"]]
    assert got["check"]["state"] == "settled"
    assert [c for c in codes(got["signals"]) if c in CHECK_CODES] == ["mandatory_term_unapplied"]  # 기존 신호(width_overflow 등)는 그대로


@needs_db
def test_check_signals_do_not_enter_confirm_ack_set(env, rule_on):
    from app import db as app_db
    from app.flows import final

    ids = _to_n3(env)
    _proceed(ids, env)
    blk = _target(ids)
    _patch(ids, blk["id"], "This cure works")
    env["q"].drain()
    assert "prohibited_expression" in codes(_blocks(ids)[blk["id"]]["signals"])
    with app_db.SessionLocal() as db:
        assert final.unresolved_warnings(db, ids["job"]) == []  # 필수 확인 = translation_failed 만
        assert all(s["code"] in final.REQUIRED_ACK_CODES or s["code"] == "width_overflow"
                   for ss in final.block_signals(db, ids["job"]).values() for s in ss)  # 확정 경로 신호 원천은 그대로
    # 검사 신호 확인을 보내도 무시되고 확정 통과(확인 대상 아님)
    ack = [{"blockId": blk["id"], "code": "prohibited_expression"}]
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": ack}).status_code == 200


@needs_db
def test_inactive_checks_show_no_check_signals(env, monkeypatch):
    monkeypatch.delenv(text_check.ACTIVE_ENV, raising=False)
    ids = _to_n3(env)
    _proceed(ids, env)
    b = next(iter(x for x in _blocks(ids).values() if x["trans1"]))
    _q("UPDATE text_block SET compliance_flags = CAST(:f AS jsonb) WHERE id = :b", f=json.dumps([MT, reg()]), b=b["id"])
    got = _blocks(ids)[b["id"]]
    assert got["check"] is None and not ({"mandatory_term_unapplied", "prohibited_expression"} & set(codes(got["signals"])))
