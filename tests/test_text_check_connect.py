"""번역문 규칙 검사(text_check) BE 연결 — 실제 PostgreSQL + 메모리 저장소 + 큐 대역.

공급(M3)과 inputKey(A1)는 **시험 대역**이다(FakeSupply · FakeKeys). 대역 결과는 실제 A1~A3 연동 결과가 아니며,
여기서 확인하는 것은 BE 흐름(등록 · N4 대기 · 채택 · producer 교체 · 잠금 · 늦은 결과 · 상태 판정 · 확정 거절 · CLI)이다.
규칙 검사 자체는 pipeline.handoff.translation.run_text_check 를 그대로 부른다.
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app import execution
from app.flows import common, text_check
from app.flows.common import json_value
from app.manifest import stable_hash
from tests.exec_db import requires_db
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401  (픽스처)

pytestmark = requires_db


class FakeSupply:
    """대역: 등록 · 조회 시점의 현재 DB 활성 용어집을 같은 선별 규칙으로 고른다(M3 정책 아님)."""

    name = "fake:test-current-db"

    def __init__(self):
        self.fail = False
        self.fail_sections: set[int] = set()

    def glossary(self, db, run, section_id, targets, *, origin):
        from app.flows import downstream

        if self.fail or section_id in self.fail_sections:
            raise text_check.SupplyBasisUnresolved("대역 — 공급 기준 없음")
        g, _ = downstream._glossary_supply(db, (json_value(run["run_scope"]) or {})["target_lang"], section_id, targets)
        return {**g, "supply_record": {"fake_record": section_id}}  # 공급 기록(예: Selection) — 요청 · 키에 넣지 않고 객체에만 보관


class FakeKeys:
    """대역: 블록별 키 = (번역문, 원문, 그 블록 원문에 나오는 공급 용어 ID)의 해시. A1 계산 규칙이 아니다."""

    name = "fake:test-keys"

    def __init__(self):
        self.fail = False
        self.drop: str | None = None

    def block_keys(self, req):
        if self.fail:
            raise text_check.InputKeysUnavailable("대역 — 키 계산 불가")
        by = {b["block_key"]: b for b in req["blocks"]}
        out = {}
        for t in req["targets"]:
            src = by[t["block_key"]]["source_ko"]
            ids = sorted(x["glossary_id"] for x in (req["glossary"].get("terms") or []) if x["term_ko"] in src)
            out[t["block_key"]] = stable_hash({"t": t["text"], "s": src, "g": ids})
        if self.drop:
            out.pop(self.drop, None)
        return out


@pytest.fixture
def rule_on(monkeypatch, env):
    sup = FakeSupply()
    sup.keys = FakeKeys()
    text_check.set_providers(supply=sup, keys=sup.keys)
    monkeypatch.setenv(text_check.ACTIVE_ENV, "rule")
    _q("DELETE FROM glossary WHERE external_id LIKE 'GL-C99%'")  # 이 파일이 넣는 시험 용어만
    yield sup
    _q("DELETE FROM glossary WHERE external_id LIKE 'GL-C99%'")
    text_check.set_providers()


TC = "app.tasks.run_text_check"


def _proceed(ids, env, *, skip=None):
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain(skip=skip)


def _checks(job):
    return _q("SELECT id, unit_id, status, is_current, retry_origin, input_manifest FROM job_async_task "
              "WHERE job_id = :j AND stage = 'text_check' ORDER BY id", j=job)


def _blocks(ids):
    return {b["id"]: b for b in ids["c"].get(f"/v1/jobs/{ids['job']}/blocks").json()}


def _checkable(ids):
    return [b for b in _blocks(ids).values() if b["check"] is not None]


def _two_block_section(ids):
    rows = _q("SELECT tb.id, tb.section_id FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
              "AND s.bucket = 'include' AND tb.is_excluded = false AND tb.trans_1 IS NOT NULL ORDER BY tb.section_id, tb.id", j=ids["job"])
    by: dict[int, list[int]] = {}
    for r in rows:
        by.setdefault(r["section_id"], []).append(r["id"])
    sid = next(s for s, bs in by.items() if len(bs) >= 2)
    return sid, by[sid]


def _patch(ids, bid, txt):
    b = _blocks(ids)[bid]
    r = ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{bid}", json={"trans1": txt, "revision": b["revision"]})
    assert r.status_code == 200, r.text
    return r.json()


# ── 구성 ────────────────────────────────────────────────────────────────
def test_config_default_off_and_rule_needs_providers(monkeypatch):
    monkeypatch.delenv(text_check.ACTIVE_ENV, raising=False)
    assert text_check.configured_active_checks() == []
    monkeypatch.setenv(text_check.ACTIVE_ENV, "rule")
    with pytest.raises(text_check.CheckConfigError):  # 공급 · 키 연결 전 — 켤 수 없다
        text_check.configured_active_checks()
    text_check.set_providers(supply=FakeSupply(), keys=FakeKeys())
    try:
        assert text_check.configured_active_checks() == ["rule"]
        monkeypatch.setenv(text_check.ACTIVE_ENV, "rule,llm")
        with pytest.raises(text_check.CheckConfigError):  # 2차는 rule 만
            text_check.configured_active_checks()
    finally:
        text_check.set_providers()


def test_inactive_keeps_current_flow_and_check_null(env, monkeypatch):
    monkeypatch.delenv(text_check.ACTIVE_ENV, raising=False)
    ids = _to_n3(env)
    _proceed(ids, env)
    assert _step(ids) == ("review", "N5")
    assert _checks(ids["job"]) == []
    scope = _q("SELECT run_scope FROM job_async_task WHERE job_id = :j AND run_kind = 'downstream'", j=ids["job"])[0]["run_scope"]
    assert scope["active_checks"] == []
    assert all(b["check"] is None for b in _blocks(ids).values())
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200


# ── N4: 등록 · 종료 대기 · 채택 ─────────────────────────────────────────
def test_n4_waits_for_check_then_n5_settled(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    assert _step(ids) == ("processing", "N4")  # 검사가 끝나기 전에는 N5 로 가지 않는다
    pend = _checks(ids["job"])
    assert pend and all(c["status"] == "pending" and c["is_current"] for c in pend)
    m = pend[0]["input_manifest"]
    assert m["active_checks"] == ["rule"] and m["glossary_ref"]["status"] == "ok" and m["supply_policy"] == "fake:test-current-db"
    assert set(m["input_keys"]) == {f"blk_{t['block_id']}" for t in m["targets"]}  # 고정 공급 · 블록별 키를 시도에 보존
    for c in pend:
        execution.dispatch([c["id"]])
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    cb = _checkable(ids)
    assert cb and all(b["check"]["state"] == "settled" and b["check"]["inputKey"] for b in cb)
    tasks = ids["c"].get(f"/v1/jobs/{ids['job']}/tasks").json()
    assert any(i["taskType"] == "verify" and i["unitType"] == "section" for i in tasks["items"])


def _word_block(ids):
    rows = _q("SELECT tb.id, tb.source_ko, tb.section_id FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
              "AND s.bucket = 'include' AND tb.is_excluded IS NOT TRUE AND tb.source_ko NOT LIKE '$%' ORDER BY tb.id", j=ids["job"])
    for r in rows:
        m = re.search(r"[A-Za-z가-힣]{5,}", r["source_ko"] or "")  # 표본 원문은 영어 — 원문에 나오는 단어를 강제 용어로
        if m:
            return r, m.group(0)
    pytest.fail("원문에 단어가 있는 블록 없음")


def test_flags_db_ids_and_other_producer_kept(env, rule_on):
    from app import db as app_db

    ids = _to_n3(env)
    blk, word = _word_block(ids)
    with app_db.engine.begin() as c:
        gid = c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, "
                             "term_kind, version) VALUES (:k, 'ZZTARGET', 'en', 'common', 'enforced', 'GL-C990', 'certification', 1) "
                             "RETURNING id"), {"k": word}).scalar_one()
    _proceed(ids, env)
    assert _step(ids) == ("review", "N5")
    flags = _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=blk["id"])[0]["compliance_flags"]
    mt = [f for f in flags if f["type"] == "mandatory_term_unapplied"]
    assert mt == [{"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": mt[0]["revision"], "glossary_id": gid}]
    # 다른 producer 항목을 넣고 같은 섹션을 다시 검사 — 자기 producer 만 교체, 다른 항목 보존
    other = {"type": "regulatory", "producer": "translation_llm_review", "revision": 1, "dictionary_id": 1}
    _q("UPDATE text_block SET compliance_flags = CAST(:f AS jsonb) WHERE id = :b", f=__import__("json").dumps(flags + [other]), b=blk["id"])
    sib = [b for b in _q("SELECT id FROM text_block WHERE section_id = :s AND trans_1 IS NOT NULL AND is_excluded = false AND id <> :b",
                         s=blk["section_id"], b=blk["id"])]
    if not sib:
        pytest.skip("같은 섹션의 다른 검사 대상 블록 없음")
    _patch(ids, sib[0]["id"], "Edited sibling")
    env["q"].drain()
    after = _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=blk["id"])[0]["compliance_flags"]
    assert other in after and [f for f in after if f["producer"] == "glossary_validation"] == mt


# ── 실패 · 재시도 ────────────────────────────────────────────────────────
def test_final_failure_goes_n5_failed_and_confirm_passes(env, rule_on, monkeypatch):
    from pipeline.handoff import envelope, translation

    def broken(raw, cfg=None):
        return envelope.failed_report(envelope.identity_fallback(raw, "text_check"), "internal", "대역 실패(일반 검사 실패 — 공급 · 입력 오류 아님)", retryable=False)

    monkeypatch.setattr(translation, "run_text_check", broken)
    ids = _to_n3(env)
    _proceed(ids, env)
    assert _step(ids) == ("review", "N5")  # 최종 실패는 N4 오류로 막지 않는다
    assert all(c["status"] == "failed" for c in _checks(ids["job"]))
    cb = _checkable(ids)
    assert cb and all(b["check"]["state"] == "failed" for b in cb)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200  # 2차 — 확인 없이 통과


def test_retryable_failure_is_retried_automatically(env, rule_on, monkeypatch):
    from pipeline.handoff import envelope, translation

    real = translation.run_text_check
    calls = {"n": 0}

    def flaky(raw, cfg=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return envelope.failed_report(envelope.identity_fallback(raw, "text_check"), "internal", "일시", retryable=True)
        return real(raw, cfg)

    monkeypatch.setattr(translation, "run_text_check", flaky)
    ids = _to_n3(env)
    _proceed(ids, env)
    assert _step(ids) == ("review", "N5")
    rows = _checks(ids["job"])
    assert any(r["retry_origin"] == "auto" for r in rows)
    assert all(b["check"]["state"] == "settled" for b in _checkable(ids))


# ── N5: 재검사 · 블록별 상태(M8) · 확정 ─────────────────────────────────
def test_n5_edit_rechecks_changed_block_and_keeps_unchanged_settled(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env)
    sid, (b1, b2) = _two_block_section(ids)[0], _two_block_section(ids)[1][:2]
    before = _blocks(ids)
    key2 = before[b2]["check"]["inputKey"]
    _patch(ids, b1, "Changed by seller")
    cur = [c for c in _checks(ids["job"]) if c["unit_id"] == sid and c["is_current"]]
    assert len(cur) == 1 and cur[0]["status"] == "pending" and cur[0]["retry_origin"] == "user"
    st = _blocks(ids)
    assert st[b1]["check"]["state"] == "in_progress"
    assert st[b2]["check"] == {"state": "settled", "inputKey": key2}  # 바뀌지 않은 블록의 유효 결과 유지
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409
    d = r.json()["error"]["details"]
    assert d["reason"] == "check_not_settled" and {"blockId": b1, "state": "in_progress"} in d["blocks"]
    assert d["registeredBlockIds"] == []  # 진행 중 블록은 새로 등록하지 않는다
    env["q"].drain()
    st = _blocks(ids)
    assert st[b1]["check"]["state"] == "settled" and st[b1]["check"]["inputKey"] != before[b1]["check"]["inputKey"]
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200


def test_layout_only_and_same_value_do_not_recheck(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env)
    sid, bs = _two_block_section(ids)
    n = len(_checks(ids["job"]))
    _q("UPDATE text_block SET bbox = jsonb_set(bbox, '{x}', to_jsonb((bbox->>'x')::int + 3)) WHERE id = :b", b=bs[0])  # 배치만 변경
    b = _blocks(ids)[bs[0]]
    assert b["check"]["state"] == "settled"
    ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{bs[0]}", json={"trans1": b["trans1"], "revision": b["revision"]})  # 같은 값
    assert len(_checks(ids["job"])) == n


def test_missing_and_outdated_are_registered_by_confirm(env, rule_on):
    from app import db as app_db

    ids = _to_n3(env)
    blk, word = _word_block(ids)
    _proceed(ids, env)
    # outdated: 그 블록 원문에 나오는 강제 용어가 새로 생겨 현재 입력(키)이 바뀐다 — 등록 경로를 거치지 않은 변경
    with app_db.engine.begin() as c:
        c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, term_kind, "
                       "version) VALUES (:k, 'ZZTARGET', 'en', 'common', 'enforced', 'GL-C991', 'certification', 1)"), {"k": word})
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated"
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409
    d = r.json()["error"]["details"]
    assert d["reason"] == "check_not_settled" and blk["id"] in d["registeredBlockIds"]
    assert {"blockId": blk["id"], "state": "outdated"} in d["blocks"]
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "in_progress"  # 등록은 커밋됐다
    env["q"].drain()
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200


def test_block_without_registration_is_missing(env, rule_on):
    ids = _to_n3(env)
    rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id",
              j=ids["job"])
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
    from app import ai_adapters
    from tests.exec_fakes import FakeTranslator

    ai_adapters.set_adapters(translator=FakeTranslator(fail_keys={f"blk_{target}"}))
    _proceed(ids, env)
    assert _blocks(ids)[target]["check"] is None  # 번역 실패 칸은 검사 대상이 아니다
    _q("UPDATE text_block SET trans_1 = 'filled outside', block_status = 'edited' WHERE id = :b", b=target)  # 등록 누락 대역
    assert _blocks(ids)[target]["check"] == {"state": "missing", "inputKey": None}
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and target in r.json()["error"]["details"]["registeredBlockIds"]


# ── 늦은 결과 · 취소 · 확정 보호 ─────────────────────────────────────────
def test_superseded_check_result_is_not_adopted(env, rule_on):
    from app.execution import Envelope
    from app.flows.common import submit

    ids = _to_n3(env)
    _proceed(ids, env)
    sid, (b1, b2) = _two_block_section(ids)[0], _two_block_section(ids)[1][:2]
    _patch(ids, b1, "first edit")
    old = [c for c in _checks(ids["job"]) if c["unit_id"] == sid and c["is_current"]][0]
    lease = execution.acquire(old["id"], "slow-checker")
    _patch(ids, b1, "second edit")  # 새 시도가 옛 시도를 대체
    assert execution.heartbeat(lease) is False
    flags_before = _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=b1)[0]["compliance_flags"]
    env_ = Envelope(outcome="done", target_count=1, input_fingerprint=lease.fingerprint,
                    payload={"text_check": {"blocks": [{"block_key": f"blk_{b1}", "flags": [
                        {"type": "mandatory_term_unapplied", "producer": "glossary_validation", "revision": 1, "glossary_id": "1"}]}]}})
    with pytest.raises(execution.LeaseLost):  # 대체된 시도의 결과 — 권한이 끝나 인계도 채택도 없다
        submit(lease, env_)
    assert _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=b1)[0]["compliance_flags"] == flags_before
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=old["id"])[0]["status"] == "cancelled"


def test_result_after_confirm_is_rejected(env, rule_on):
    from app.execution import Envelope
    from app.flows.common import submit

    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    pend = _checks(ids["job"])
    lease = execution.acquire(pend[0]["id"], "late-checker")
    for c in pend[1:]:
        execution.dispatch([c["id"]])
    env["q"].drain()
    # 이 섹션 검사가 끝나지 않아 N4 에 머문다 — 작업 전체 취소 뒤 늦은 결과
    assert _step(ids)[1] == "N4"
    r = ids["c"].delete(f"/v1/jobs/{ids['job']}")
    assert r.status_code == 200, r.text
    env_ = Envelope(outcome="done", target_count=0, input_fingerprint=lease.fingerprint, payload={"text_check": {"blocks": []}})
    with pytest.raises(execution.HandoffRejected):  # 전체 취소 뒤 — 인계를 기록하지 않는다
        submit(lease, env_)
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE job_id = :j", j=ids["job"])[0]["n"] == 0  # 늦은 결과가 자료를 남기지 않는다


def test_adoption_rejects_incomplete_report(env, rule_on, monkeypatch):
    from pipeline.handoff import translation, validate

    real = translation.run_text_check

    def drop_one(raw, cfg=None):
        rep = real(raw, cfg)
        if rep.outcome == "completed" and len(rep.payload["blocks"]) > 1:
            rep.payload["blocks"] = rep.payload["blocks"][1:]
        return rep

    monkeypatch.setattr(translation, "run_text_check", drop_one)
    monkeypatch.setattr(validate, "validate_report", lambda rep, req, **k: [])  # AI 검증을 지나 BE 채택까지 온 경우
    ids = _to_n3(env)
    _proceed(ids, env)
    bad = [c for c in _checks(ids["job"]) if len(c["input_manifest"]["targets"]) > 1]
    assert bad and all(c["status"] == "failed" for c in bad)
    rej = _q("SELECT reject_reason FROM task_handoff WHERE task_id = ANY(:t)", t=[c["id"] for c in bad])
    assert all(r["reject_reason"].startswith("CHECK_RESULT_INCOMPLETE") for r in rej)
    # M7 미확정 — 무결성 오류를 일반 검사 실패(확정 통과)로 분류하지 않는다. 조회 · 확정 모두 통과시키지 않는다
    from app import db as app_db

    with app_db.SessionLocal() as db:
        with pytest.raises(text_check.CheckIntegrityError):
            text_check.job_block_states(db, ids["job"])
    with pytest.raises(text_check.CheckIntegrityError):
        ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})


def test_adoption_rejects_result_for_text_changed_during_check(env, rule_on, monkeypatch):
    from pipeline.handoff import translation

    real = translation.run_text_check
    hit = {}

    def edit_during(raw, cfg=None):
        rep = real(raw, cfg)
        if not hit and raw["targets"]:
            bid = int(raw["targets"][0]["block_key"].split("_")[1])
            _q("UPDATE text_block SET trans_1 = 'changed while checking' WHERE id = :b", b=bid)  # 등록 경로 밖 변경(동시 수정 대역)
            hit["bid"], hit["attempt"] = bid, int(raw["attempt_id"])
        return rep

    monkeypatch.setattr(translation, "run_text_check", edit_during)
    ids = _to_n3(env)
    _proceed(ids, env)
    a = _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=hit["attempt"])[0]
    assert a == {"status": "failed", "error_code": "INPUT_CHANGED"}  # 보고 전체 거절 — 부분 채택 없음
    assert _q("SELECT state FROM task_handoff WHERE task_id = :t", t=hit["attempt"])[0]["state"] == "rejected"
    assert _blocks(ids)[hit["bid"]]["check"]["state"] in ("missing", "outdated")  # 바뀐 문구의 검사 결과로 보지 않는다


def test_integrity_error_is_not_treated_as_settled(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env)
    c = [x for x in _checks(ids["job"]) if len(x["input_manifest"]["targets"]) > 1][0]
    h = _q("SELECT id, payload FROM task_handoff WHERE task_id = :t AND state = 'adopted'", t=c["id"])[0]
    p = h["payload"]
    p["text_check"]["blocks"] = p["text_check"]["blocks"][1:]  # 저장 · 집계 무결성 문제 대역(채택 뒤 결과 누락)
    _q("UPDATE task_handoff SET payload = CAST(:p AS jsonb) WHERE id = :h", p=__import__("json").dumps(p), h=h["id"])
    from app import db as app_db

    with app_db.SessionLocal() as db:
        with pytest.raises(text_check.CheckIntegrityError):
            text_check.job_block_states(db, ids["job"])
    with pytest.raises(text_check.CheckIntegrityError):  # 확정도 통과시키지 않는다
        ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})


def test_decide_table():
    d = text_check.decide
    assert d(current_key="k", result_key="k", attempt_status="running", attempt_key="k2", attempt_done_without_result=False)["state"] == "settled"
    assert d(current_key="k", result_key=None, attempt_status="done", attempt_key="k", attempt_done_without_result=True)["state"] == "integrity_error"
    assert d(current_key="k", result_key="old", attempt_status="pending", attempt_key="k", attempt_done_without_result=False) == {
        "state": "in_progress", "inputKey": "old"}
    assert d(current_key="k", result_key=None, attempt_status="running", attempt_key="k", attempt_done_without_result=False) == {
        "state": "in_progress", "inputKey": None}
    assert d(current_key="k", result_key="old", attempt_status="failed", attempt_key="k", attempt_done_without_result=False)["state"] == "failed"
    assert d(current_key="k", result_key="old", attempt_status="failed", attempt_key="x", attempt_done_without_result=False)["state"] == "outdated"
    assert d(current_key="k", result_key=None, attempt_status=None, attempt_key=None, attempt_done_without_result=False) == {
        "state": "missing", "inputKey": None}


# ── 등록 보장 ②④ ────────────────────────────────────────────────────────
def test_translate_retry_registers_check_with_saved_text(env, rule_on):
    from app import ai_adapters
    from tests.exec_fakes import FakeTranslator

    ids = _to_n3(env)
    rows = _q("SELECT tb.id, tb.source_ko, tb.section_id FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
              "ORDER BY tb.id", j=ids["job"])
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]
    tr = FakeTranslator(fail_keys={f"blk_{target['id']}"})
    ai_adapters.set_adapters(translator=tr)
    _proceed(ids, env)
    att = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND status = 'failed' AND is_current", j=ids["job"])[0]
    tr.fail_keys = set()
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/tasks/{att['id']}/retry").status_code in (200, 202)
    env["q"].drain()
    cur = [c for c in _checks(ids["job"]) if c["unit_id"] == target["section_id"] and c["is_current"]][0]
    assert target["id"] in {t["block_id"] for t in cur["input_manifest"]["targets"]} and cur["status"] == "done"
    assert _blocks(ids)[target["id"]]["check"]["state"] == "settled"


def test_restore_reuses_valid_check(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env)
    sid, _ = _two_block_section(ids)
    n = len(_checks(ids["job"]))
    c = ids["c"]
    sec = c.get(f"/v1/jobs/{ids['job']}/sections/{sid}").json()
    r = c.patch(f"/v1/jobs/{ids['job']}/sections/{sid}", json={"action": "exclude", "revision": sec["revision"]})
    assert r.status_code == 200, r.text
    r = c.patch(f"/v1/jobs/{ids['job']}/sections/{sid}", json={"action": "restore", "revision": r.json()["revision"]})
    assert r.status_code == 200, r.text
    assert len(_checks(ids["job"])) == n  # 유효한 결과가 있는 섹션의 복원은 재검사하지 않는다


# ── 일괄 등록 CLI(격리 DB 에서만) ─────────────────────────────────────────
def test_backfill_cli_registers_only_active_runs(env, rule_on, monkeypatch, capsys):
    from app import check_backfill

    monkeypatch.delenv(text_check.ACTIVE_ENV, raising=False)
    off = _to_n3(env)
    _proceed(off, env)  # 활성 기록 없는 실행(활성화 전 N5 대역)
    monkeypatch.setenv(text_check.ACTIVE_ENV, "rule")
    on = _to_n3(env)
    _proceed(on, env)
    _q("UPDATE text_block SET trans_1 = 'filled outside' WHERE id = (SELECT tb.id FROM text_block tb JOIN section s ON s.id = tb.section_id "
       "WHERE s.job_id = :j AND s.bucket = 'include' AND tb.is_excluded = false AND tb.trans_1 IS NOT NULL ORDER BY tb.id LIMIT 1)",
       j=on["job"])
    n_on, n_off = len(_checks(on["job"])), len(_checks(off["job"]))
    assert check_backfill.main(["plan", "--job", str(off["job"]), "--job", str(on["job"])]) == 3  # 활성 기록 없는 작업이 있다
    assert len(_checks(on["job"])) == n_on  # plan 은 쓰지 않는다
    assert check_backfill.main(["apply", "--job", str(off["job"])]) == 3
    assert len(_checks(off["job"])) == n_off == 0
    assert check_backfill.main(["apply", "--job", str(on["job"])]) == 0
    assert len(_checks(on["job"])) == n_on + 1
    assert check_backfill.main(["apply", "--job", str(on["job"])]) == 0  # 다시 실행해도 같은 입력이면 새로 만들지 않는다
    assert len(_checks(on["job"])) == n_on + 1
    out = capsys.readouterr().out
    assert "활성 검사 기록 없음" in out


# ── 마이그레이션 ─────────────────────────────────────────────────────────
def test_migration_downgrade_stops_when_check_rows_exist():
    from alembic import command

    from tests.exec_db import alembic_config, fresh_database

    with fresh_database() as url:
        from sqlalchemy import create_engine

        eng = create_engine(url)
        try:
            command.downgrade(alembic_config(), "0012")  # 행이 없으면 내려간다
            command.upgrade(alembic_config(), "head")
            with eng.begin() as c:
                from tests.exec_db import seed_job

                ids = seed_job(c)
                run = c.execute(text("INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, run_kind, run_scope, "
                                     "max_retry) VALUES (:j, 'translate', 'job', :j, 'running', 'downstream', '{}'::jsonb, 0) RETURNING id"),
                                {"j": ids["job"]}).scalar_one()
                c.execute(text("INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, parent_task_id, stage, attempt_no, "
                               "is_current, retry_count, max_retry, input_manifest, input_fingerprint, target_count) VALUES "
                               "(:j, 'verify', 'section', 1, 'pending', :r, 'text_check', 1, true, 0, 2, '{}'::jsonb, :fp, 1)"),
                          {"j": ids["job"], "r": run, "fp": "0" * 64})
            with pytest.raises(Exception):  # verify 아닌 task_type 의 검사 stage 는 제약 위반
                with eng.begin() as c:
                    c.execute(text("INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, parent_task_id, stage, "
                                   "attempt_no, is_current, retry_count, max_retry, input_manifest, input_fingerprint, target_count) VALUES "
                                   "(:j, 'render', 'section', 2, 'pending', :r, 'text_check', 1, true, 0, 2, '{}'::jsonb, :fp, 1)"),
                              {"j": ids["job"], "r": run, "fp": "1" * 64})
            with pytest.raises(Exception):
                command.downgrade(alembic_config(), "0012")
        finally:
            eng.dispose()


# ── 보완(코덱스 검토): 채택 때 현재 inputKey 대조 · 고정 요청 · 일괄 등록 결과 구분 ─────────────
def _pending_for(ids, block_id):
    sec = _q("SELECT section_id FROM text_block WHERE id = :b", b=block_id)[0]["section_id"]
    return [c for c in _checks(ids["job"]) if c["unit_id"] == sec and c["is_current"]][0]


def _run_pending(ids, env):
    for c in _checks(ids["job"]):
        if c["status"] == "pending":
            execution.dispatch([c["id"]])
    env["q"].drain()


def _add_term(word, ext):
    _q("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, term_kind, version) "
       "VALUES (:k, 'ZZTARGET', 'en', 'common', 'enforced', :e, 'certification', 1)", k=word, e=ext)


def _flags_of(c):
    return {t["block_id"]: _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=t["block_id"])[0]["compliance_flags"]
            for t in c["input_manifest"]["targets"]}


def _attempt(tid):
    return _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=tid)[0]


def test_adoption_rejects_same_text_with_changed_key(env, rule_on):
    ids = _to_n3(env)
    blk, word = _word_block(ids)
    _proceed(ids, env, skip={TC})
    att = _pending_for(ids, blk["id"])
    before = _flags_of(att)
    _add_term(word, "GL-C992")  # 문구는 그대로, 그 블록의 현재 의미 키(공급 강제 용어)만 바뀐다
    _run_pending(ids, env)
    assert _attempt(att["id"]) == {"status": "failed", "error_code": "INPUT_CHANGED"}
    assert _q("SELECT state FROM task_handoff WHERE task_id = :t", t=att["id"])[0]["state"] == "rejected"
    assert _flags_of(att) == before  # 보고 전체를 채택하지 않았다 — flag 를 쓰지 않는다
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "missing"  # 그 입력의 검사 결과가 아니다(실패로 보지 않음)
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]


@pytest.mark.parametrize("mode", ["fail", "drop"])
def test_adoption_rejects_when_current_key_unavailable(env, rule_on, mode):
    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    att = [c for c in _checks(ids["job"]) if c["status"] == "pending"][0]
    before = _flags_of(att)
    keys = rule_on.keys
    if mode == "fail":
        keys.fail = True  # 채택 때 현재 키 계산 불가
    else:
        keys.drop = f"blk_{att['input_manifest']['targets'][0]['block_id']}"  # 한 블록의 현재 키 누락
    _run_pending(ids, env)
    keys.fail, keys.drop = False, None
    assert _attempt(att["id"]) == {"status": "failed", "error_code": "CHECK_KEY_UNAVAILABLE"}
    assert _flags_of(att) == before
    st = _blocks(ids)
    assert all(st[t["block_id"]]["check"]["state"] == "missing" for t in att["input_manifest"]["targets"])  # 검사 실패로 보지 않는다
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"]


def test_final_failure_report_is_current_failure_only_for_unchanged_input(env, rule_on, monkeypatch):
    from pipeline.handoff import envelope, translation

    ids = _to_n3(env)
    blk, word = _word_block(ids)
    added = {}

    def broken(raw, cfg=None):
        if not added:
            _add_term(word, "GL-C993")  # 실패 보고가 도착하기 전에 그 블록의 현재 키가 바뀐다
            added["x"] = True
        return envelope.failed_report(envelope.identity_fallback(raw, "text_check"), "internal", "대역 실패(일반 검사 실패 — 공급 · 입력 오류 아님)", retryable=False)

    monkeypatch.setattr(translation, "run_text_check", broken)
    _proceed(ids, env)
    st = _blocks(ids)
    assert st[blk["id"]]["check"]["state"] == "missing"  # 이전 입력의 실패 — 현재 입력의 failed 가 아니다
    others = [b for b in _checkable(ids) if b["id"] != blk["id"]]
    assert others and all(b["check"]["state"] == "failed" for b in others)  # 입력이 그대로인 블록은 현재 최종 실패
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]


def test_revision_only_change_before_run_uses_fixed_request(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    att = [c for c in _checks(ids["job"]) if c["status"] == "pending"][0]
    bid = att["input_manifest"]["targets"][0]["block_id"]
    rev0 = att["input_manifest"]["targets"][0]["revision"]
    _q("UPDATE text_block SET revision = revision + 1, bbox = jsonb_set(bbox, '{x}', to_jsonb((bbox->>'x')::int + 2)) WHERE id = :b", b=bid)
    _run_pending(ids, env)
    assert _attempt(att["id"])["status"] == "done"  # 고정 요청(등록 당시 revision)으로 실행 · 검증, 같은 의미 키로 채택
    h = _q("SELECT payload FROM task_handoff WHERE task_id = :t AND state = 'adopted'", t=att["id"])[0]["payload"]
    assert h["report"]["input_manifest_sha256"] == att["input_manifest"]["request_manifest_sha256"]
    res = [b for b in h["text_check"]["blocks"] if b["block_key"] == f"blk_{bid}"][0]
    assert res["revision"] == rev0  # 요청 revision 은 등록 당시 값 그대로(제거 · 교체하지 않음)
    assert _blocks(ids)[bid]["check"]["state"] == "settled"


def test_text_change_before_run_is_rejected_at_adoption(env, rule_on):
    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    att = [c for c in _checks(ids["job"]) if c["status"] == "pending"][0]
    bid = att["input_manifest"]["targets"][0]["block_id"]
    before = _flags_of(att)
    _q("UPDATE text_block SET trans_1 = 'changed before run' WHERE id = :b", b=bid)  # 등록 경로 밖 실제 번역문 변경
    _run_pending(ids, env)
    assert _attempt(att["id"]) == {"status": "failed", "error_code": "INPUT_CHANGED"}
    after = _flags_of(att)
    assert {k: v for k, v in after.items() if k != bid} == {k: v for k, v in before.items() if k != bid}
    assert _blocks(ids)[bid]["check"]["state"] == "missing"


def _fill_outside(job):
    _q("UPDATE text_block SET trans_1 = 'filled outside' WHERE id = (SELECT tb.id FROM text_block tb JOIN section s ON s.id = tb.section_id "
       "WHERE s.job_id = :j AND s.bucket = 'include' AND tb.is_excluded = false AND tb.trans_1 IS NOT NULL ORDER BY tb.id LIMIT 1)", j=job)
    return _q("SELECT tb.section_id FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
              "AND tb.trans_1 = 'filled outside'", j=job)[0]["section_id"]


def test_backfill_distinguishes_unresolved_reused_and_partial(env, rule_on, capsys):
    from app import check_backfill

    a, b = _to_n3(env), _to_n3(env)
    _proceed(a, env)
    _proceed(b, env)
    _fill_outside(a["job"])
    sb = _fill_outside(b["job"])
    na, nb = len(_checks(a["job"])), len(_checks(b["job"]))
    capsys.readouterr()
    rule_on.fail = True  # 공급 실패 — 등록하지 않고 미처리로 보고
    assert check_backfill.main(["apply", "--job", str(a["job"]), "--job", str(b["job"])]) == 3
    assert "unresolved_supply" in capsys.readouterr().out and len(_checks(a["job"])) == na
    rule_on.fail = False
    rule_on.keys.fail = True  # 키 실패
    assert check_backfill.main(["apply", "--job", str(a["job"])]) == 3
    assert "unresolved_keys" in capsys.readouterr().out and len(_checks(a["job"])) == na
    rule_on.keys.fail = False
    rule_on.fail_sections = {sb}  # 일부 작업만 처리
    assert check_backfill.main(["apply", "--job", str(a["job"]), "--job", str(b["job"])]) == 3
    out = capsys.readouterr().out
    assert f"job {b['job']} section {sb}: unresolved_supply" in out and "registered" in out
    assert len(_checks(a["job"])) == na + 1 and len(_checks(b["job"])) == nb
    rule_on.fail_sections = set()
    assert check_backfill.main(["apply", "--job", str(a["job"])]) == 0  # 같은 입력 — 재사용, 미처리 없음
    assert "reused" in capsys.readouterr().out and len(_checks(a["job"])) == na + 1


def test_decide_ignores_non_result_failures_and_flags_integrity():
    d = text_check.decide
    assert d(current_key="k", result_key=None, attempt_status="failed", attempt_key="k", attempt_done_without_result=False,
             attempt_error_code="INPUT_CHANGED")["state"] == "missing"
    assert d(current_key="k", result_key="o", attempt_status="failed", attempt_key="k", attempt_done_without_result=False,
             attempt_error_code="CHECK_KEY_UNAVAILABLE")["state"] == "outdated"
    assert d(current_key="k", result_key=None, attempt_status="failed", attempt_key="k", attempt_done_without_result=False,
             attempt_error_code="CHECK_RESULT_INCOMPLETE")["state"] == "integrity_error"
    assert d(current_key="k", result_key=None, attempt_status="failed", attempt_key="k", attempt_done_without_result=False,
             attempt_error_code="TEXT_CHECK_INTERNAL")["state"] == "failed"  # 일반 검사 최종 실패 — 기존 정책(확인 없이 통과)
    for code in ("TEXT_CHECK_INPUT_INVALID", "TEXT_CHECK_BUNDLE_INVALID", "AI_ADAPTER_UNAVAILABLE"):  # 공급 · 입력 · 데이터 오류 · 규칙 검사 묶음 변환 실패 — 그 시도를 빼고 판단
        assert d(current_key="k", result_key=None, attempt_status="failed", attempt_key="k", attempt_done_without_result=False,
                 attempt_error_code=code)["state"] == "missing"
        assert d(current_key="k", result_key="o", attempt_status="failed", attempt_key="k", attempt_done_without_result=False,
                 attempt_error_code=code)["state"] == "outdated"
        assert d(current_key="k", result_key="k", attempt_status="failed", attempt_key="k", attempt_done_without_result=False,
                 attempt_error_code=code) == {"state": "settled", "inputKey": "k"}  # 현재 키의 정상 결과는 무효화하지 않는다


# ── M7 검출 경로 일치: 실제 validate_report 를 거치는 보고 무결성 오류 ─────────────────────────
def _corrupt(mode):
    from pipeline.handoff import translation

    real = translation.run_text_check

    def run(raw, cfg=None):
        rep = real(raw, cfg)
        blocks = (rep.payload or {}).get("blocks") or []
        if rep.outcome != "completed" or not blocks:
            return rep
        if mode == "missing":
            rep.payload["blocks"] = blocks[1:] if len(blocks) > 1 else []
        elif mode == "duplicate":
            rep.payload["blocks"] = blocks + [dict(blocks[0])]
        elif mode == "unregistered":
            rep.payload["blocks"] = blocks + [{**blocks[0], "block_key": "blk_999999999"}]
        # 구조가 잘못돼 대상 집합을 판별할 수 없는 보고(코덱스 검토 3) — 분류 중 예외로 새면 *_UNEXPECTED 일반 실패가 된다
        elif mode == "null_block":
            rep.payload["blocks"] = [None]
        elif mode == "null_among_blocks":
            rep.payload["blocks"] = blocks + [None]
        elif mode == "string_blocks":
            rep.payload["blocks"] = blocks[0]["block_key"]
        elif mode == "object_blocks":
            rep.payload["blocks"] = {b["block_key"]: b for b in blocks}
        elif mode == "no_blocks":
            del rep.payload["blocks"]
        elif mode == "list_key":
            rep.payload["blocks"] = [{**blocks[0], "block_key": [blocks[0]["block_key"]]}] + blocks[1:]
        elif mode == "object_key":
            rep.payload["blocks"] = [{**blocks[0], "block_key": {"k": blocks[0]["block_key"]}}] + blocks[1:]
        elif mode == "null_key":
            rep.payload["blocks"] = [{k: v for k, v in blocks[0].items() if k != "block_key"}] + blocks[1:]
        elif mode == "invalid_field":  # 블록 집합은 맞고 내용만 틀린 일반 검증 실패
            rep.payload["blocks"] = [{**blocks[0], "revision": blocks[0]["revision"] + 7}] + blocks[1:]
        return rep

    return run


MALFORMED = ["null_block", "null_among_blocks", "string_blocks", "object_blocks", "no_blocks", "list_key", "object_key", "null_key"]


@pytest.mark.parametrize("mode", ["missing", "duplicate", "unregistered", *MALFORMED])
def test_integrity_error_found_by_validate_report_blocks_confirm_after_retries(env, rule_on, monkeypatch, mode):
    from app import db as app_db
    from pipeline.handoff import translation

    monkeypatch.setattr(translation, "run_text_check", _corrupt(mode))  # validate_report 는 그대로 — 우회하지 않는다
    ids = _to_n3(env)
    _proceed(ids, env)
    assert _step(ids) == ("review", "N5")
    rows = _checks(ids["job"])
    cur = [c for c in rows if c["is_current"]]
    codes = {_attempt(c["id"])["error_code"] for c in rows}
    assert codes == {"CHECK_RESULT_INCOMPLETE"}  # 어디서 잡히든 같은 사유
    msgs = [_q("SELECT error_message FROM job_async_task WHERE id = :t", t=c["id"])[0]["error_message"] for c in rows]
    assert all(m.startswith("AI 보고 검증 실패") for m in msgs)  # BE 채택 전에 실제 validate_report 가 먼저 잡은 경로
    assert any(c["retry_origin"] == "auto" for c in rows)  # 재시도까지 소진
    assert all(_attempt(c["id"])["status"] == "failed" for c in cur)
    with app_db.SessionLocal() as db:
        with pytest.raises(text_check.CheckIntegrityError):
            text_check.job_block_states(db, ids["job"])
    with pytest.raises(text_check.CheckIntegrityError):  # 일반 검사 실패처럼 확정을 통과시키지 않는다
        ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert _step(ids) == ("review", "N5")


def test_other_validation_failure_keeps_final_failure_confirm_path(env, rule_on, monkeypatch):
    from pipeline.handoff import translation

    monkeypatch.setattr(translation, "run_text_check", _corrupt("invalid_field"))
    ids = _to_n3(env)
    _proceed(ids, env)
    rows = _checks(ids["job"])
    assert {_attempt(c["id"])["error_code"] for c in rows} == {"CHECK_RESULT_INVALID"}  # 무결성 오류가 아닌 검증 실패
    cb = _checkable(ids)
    assert cb and all(b["check"]["state"] == "failed" for b in cb)
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200  # 기존 동작 유지


@pytest.mark.parametrize("payload", [
    {"blocks": [None]}, {"blocks": "blk_1"}, {"blocks": {"blk_1": {}}}, {}, {"blocks": None}, {"blocks": 3},
    {"blocks": [{"block_key": ["blk_1"]}]}, {"blocks": [{"block_key": {"k": "blk_1"}}]}, {"blocks": [{"block_key": None}]},
    {"blocks": [{"block_key": 1}]}, {"blocks": [{}]}, [], "x", 0,
])
def test_incomplete_blocks_never_raises_on_malformed_structure(payload):
    req = {"targets": [{"block_key": "blk_1"}]}
    assert text_check.result_block_keys(payload) is None  # 판별 불가 — 예외 대신 None
    assert text_check.incomplete_blocks(payload, req) is True  # 무결성 오류로 분류(일반 실패 · 확정 허용 경로로 보내지 않음)


def test_incomplete_blocks_keeps_exact_match_and_set_errors():
    req = {"targets": [{"block_key": "blk_1"}, {"block_key": "blk_2"}]}
    ok = {"blocks": [{"block_key": "blk_2"}, {"block_key": "blk_1"}]}
    assert text_check.incomplete_blocks(ok, req) is False and text_check.result_block_keys(ok) == ["blk_2", "blk_1"]
    assert text_check.incomplete_blocks({"blocks": [{"block_key": "blk_1"}]}, req) is True
    assert text_check.incomplete_blocks({"blocks": ok["blocks"] + [{"block_key": "blk_1"}]}, req) is True
    assert text_check.incomplete_blocks(None, req) is False  # 완료 보고가 아님 — 분류 대상 아님


# ── 고정 요청 보관(취소 가능한 객체 · A안) ─────────────────────────────────────────────────────────
def _objects(env, job):
    return env["store"].keys(f"input/{job}/")


def _body(env, att):
    import json

    return json.loads(env["store"].get(att["input_manifest"]["request_ref"]["key"]))


def _section_texts(ids):
    return [t for r in _q("SELECT tb.source_ko, tb.trans_1 FROM text_block tb JOIN section s ON s.id = tb.section_id "
                          "WHERE s.job_id = :j", j=ids["job"]) for t in (r["source_ko"], r["trans_1"]) if t and len(t.strip()) >= 4]


def test_manifest_keeps_refs_only_and_body_lives_in_job_object(env, rule_on):
    import json

    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    pend = _checks(ids["job"])
    assert pend
    for att in pend:
        m = att["input_manifest"]
        assert not {"request", "glossary"} & set(m)  # 본문 · 공급 본문은 불변 명세에 없다
        ref = m["request_ref"]
        assert ref["key"] == f"input/{ids['job']}/text_check/{ref['sha256']}.json" and ref["format"] == text_check.REQUEST_FORMAT
        raw = env["store"].get(ref["key"])
        assert text_check.sha256_bytes(raw) == ref["sha256"] and len(raw) == ref["size"]
        body = json.loads(raw)
        assert body["job_id"] == ids["job"] and body["section_id"] == att["unit_id"]
        assert {t["block_key"] for t in body["request"]["targets"]} == set(m["input_keys"])
        assert body["supply_record"] == {"fake_record": att["unit_id"]}  # 공급 기록은 객체에만
        assert m["supply_record_sha256"] == stable_hash(body["supply_record"])
        assert m["glossary_ref"]["sha256"] == stable_hash(body["glossary"]) and "supply_record" not in body["glossary"]
        dumped = json.dumps(m, ensure_ascii=False)
        assert not [t for t in _section_texts(ids) if t in dumped]  # 원문 · 번역문이 명세에 없다
    _run_pending(ids, env)
    assert _step(ids) == ("review", "N5")
    assert all(b["check"]["state"] == "settled" for b in _checkable(ids))  # 객체로 실행 · 검증 · 채택


class _FlakyStore:
    """env 메모리 저장소 앞에 끼우는 장애 대역. 대상은 input/ 키만."""

    def __init__(self, inner):
        self.inner = inner
        self.put_error = self.get_error = self.bad_key = None
        self.get_override: bytes | None = None
        self.pretend_exists = False
        self.get_fail_times = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def put_if_absent(self, key, data, content_type=None):
        if key.startswith("input/"):
            if self.put_error:
                raise self.put_error
            if self.pretend_exists:
                return False  # 같은 키가 이미 있다고 답한다 — 되읽은 내용으로 판단해야 한다
        return self.inner.put_if_absent(key, data, content_type)

    def get(self, key):
        if key.startswith("input/"):
            if self.get_fail_times:
                self.get_fail_times -= 1
                raise ConnectionError("대역 — 읽기 장애")
            if self.get_error and (self.bad_key is None or key == self.bad_key):
                raise self.get_error
            if self.get_override is not None:
                return self.get_override
        return self.inner.get(key)


@pytest.fixture
def flaky(env):
    from app.storage import set_store

    f = _FlakyStore(env["store"])
    set_store(f)
    yield f
    set_store(env["store"])


@pytest.mark.parametrize("mode", ["put_error", "readback_mismatch", "existing_key_other_content"])
def test_request_not_stored_means_no_attempt(env, rule_on, flaky, mode):
    ids = _to_n3(env)
    _proceed(ids, env)
    assert _step(ids) == ("review", "N5")
    blk, _ = _word_block(ids)
    sec = blk["section_id"]
    before = [c["id"] for c in _checks(ids["job"])]
    if mode == "put_error":
        flaky.put_error = ConnectionError("대역 — 쓰기 장애")
    elif mode == "readback_mismatch":
        flaky.get_override = b'{"other":1}'
    else:
        flaky.pretend_exists, flaky.get_override = True, b'{"format":"text-check-request@1","job_id":0}'
    _patch(ids, blk["id"], "changed while store is broken")  # 저장은 성공, 검사 등록만 안 된다
    assert [c["id"] for c in _checks(ids["job"])] == before  # 객체 저장 · 되읽기 · 해시 확인 실패 — 시도 없음
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated"  # 이전 결과만 있다(검사 실패로 보지 않음)
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"] == []  # 확정 보조 등록도 같은 이유로 못 함
    flaky.put_error = flaky.get_override = None
    flaky.pretend_exists = False
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]  # 복구되면 등록
    new = [c for c in _checks(ids["job"]) if c["id"] not in before]
    assert len(new) == 1 and new[0]["unit_id"] == sec


def test_store_request_same_key_checks_content():
    from app.storage import MemoryStore, set_store

    store = MemoryStore()
    set_store(store)
    try:
        body = {"format": text_check.REQUEST_FORMAT, "job_id": 7, "section_id": 1, "request": {"x": "본문"}, "glossary": {}, "supply_record": None}
        ref = text_check.store_request(7, body)
        assert text_check.store_request(7, body) == ref  # 같은 키 · 같은 내용 — 그대로 쓴다
        store.objects[ref["key"]] = b"tampered"
        with pytest.raises(text_check.RequestStoreFailed):  # 같은 키가 있어도 내용을 확인한다
            text_check.store_request(7, body)
    finally:
        set_store(None)


def test_rollback_after_store_leaves_object_reused_then_purged_on_cancel(env, rule_on):
    from app import db as app_db

    ids = _to_n3(env)
    _proceed(ids, env)
    blk, _ = _word_block(ids)
    _q("UPDATE text_block SET trans_1 = 'changed outside for rollback' WHERE id = :b", b=blk["id"])
    n0 = len(_checks(ids["job"]))
    with app_db.SessionLocal() as db:
        execution.lock_job(db, ids["job"])
        aid = text_check.ensure_for_job(db, ids["job"], blk["section_id"], origin="user")
        assert aid is not None
        db.rollback()  # 객체는 썼지만 DB 는 되돌림
    assert len(_checks(ids["job"])) == n0
    left = _objects(env, ids["job"])
    with app_db.SessionLocal() as db:
        execution.lock_job(db, ids["job"])
        aid = text_check.ensure_for_job(db, ids["job"], blk["section_id"], origin="user")
        db.commit()
    att = [c for c in _checks(ids["job"]) if c["id"] == aid][0]
    assert att["input_manifest"]["request_ref"]["key"] in left  # 남은 객체(같은 내용 주소)를 확인 후 재사용
    assert ids["c"].delete(f"/v1/jobs/{ids['job']}").status_code == 200
    assert _objects(env, ids["job"]) == []  # 전체 취소가 input/ 접두사를 지운다
    for c in _checks(ids["job"]):
        assert "request" not in c["input_manifest"] and "glossary" not in c["input_manifest"]


@pytest.mark.parametrize("first", ["cancel", "register"])
def test_cancel_and_register_race_leaves_no_request_object(env, rule_on, first):
    import threading

    from app import db as app_db
    from app.flows import cancel

    ids = _to_n3(env)
    _proceed(ids, env)
    blk, _ = _word_block(ids)
    _q("UPDATE text_block SET trans_1 = 'changed outside for race' WHERE id = :b", b=blk["id"])
    seller = _q("SELECT seller_id FROM job WHERE id = :j", j=ids["job"])[0]["seller_id"]
    holder = app_db.SessionLocal()
    execution.lock_job(holder, ids["job"])  # 먼저 잡은 쪽
    done = {}

    def other():
        with app_db.SessionLocal() as db:
            if first == "cancel":  # 등록이 job 잠금을 기다린다
                execution.lock_job(db, ids["job"])
                done["aid"] = text_check.ensure_for_job(db, ids["job"], blk["section_id"], origin="user")
                db.commit()
            else:  # 취소가 job 잠금을 기다린다
                done["cancel"] = cancel.cancel_job(db, ids["job"], seller)

    t = threading.Thread(target=other)
    if first == "cancel":
        t.start()
        cancel.cancel_job(holder, ids["job"], seller)  # 같은 세션 잠금 — 커밋까지
    else:
        done["aid0"] = text_check.ensure_for_job(holder, ids["job"], blk["section_id"], origin="user")
        assert _objects(env, ids["job"])  # 객체는 이미 썼다(커밋 전)
        t.start()
        holder.commit()
    t.join(30)
    holder.close()
    assert not t.is_alive()
    if first == "cancel":
        assert done["aid"] is None  # 취소된 실행 — 등록하지 않는다
    else:
        assert done["aid0"] is not None and done["cancel"]["status"] == "archived"
    assert _objects(env, ids["job"]) == []  # 어느 순서든 남는 고정 요청 객체가 없다


@pytest.mark.parametrize("mode,code", [("missing", "CHECK_REQUEST_MISSING"), ("tampered", "CHECK_REQUEST_HASH_MISMATCH"),
                                       ("read_error", "CHECK_REQUEST_READ_FAILED")])
def test_untrusted_request_is_not_a_check_failure(env, rule_on, flaky, mode, code):
    from app import db as app_db

    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    pend = _checks(ids["job"])
    key = pend[0]["input_manifest"]["request_ref"]["key"]
    if mode == "missing":
        env["store"].delete(key)
    elif mode == "tampered":
        env["store"].objects[key] = env["store"].objects[key].replace(b"blk_", b"blk-", 1)
    else:
        flaky.get_error = ConnectionError("대역 — 읽기 장애 지속")
    _run_pending(ids, env)
    flaky.get_error = None
    rows = [c for c in _checks(ids["job"]) if c["unit_id"] == pend[0]["unit_id"]]
    assert {_attempt(c["id"])["error_code"] for c in rows} == {code}
    assert any(c["retry_origin"] == "auto" for c in rows) is (mode == "read_error")  # 읽기 장애만 같은 입력 자동 재시도
    assert all(_attempt(c["id"])["status"] == "failed" for c in rows if c["is_current"])
    assert _q("SELECT count(*) AS n FROM task_handoff h WHERE h.task_id = ANY(:t) AND h.state = 'adopted' AND h.outcome = 'done'",
              t=[c["id"] for c in rows])[0]["n"] == 0  # 검사하지 않았다
    assert _step(ids) == ("review", "N5")
    with app_db.SessionLocal() as db:
        with pytest.raises(text_check.CheckIntegrityError):  # 일반 failed(확정 통과)로 분류하지 않는다
            text_check.job_block_states(db, ids["job"])
    with pytest.raises(text_check.CheckIntegrityError):
        ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert _step(ids) == ("review", "N5")


def test_transient_read_error_recovers_by_same_input_retry(env, rule_on, flaky):
    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    flaky.get_fail_times = 1
    _run_pending(ids, env)
    env["q"].drain()
    rows = _checks(ids["job"])
    assert any(_attempt(c["id"])["error_code"] == "CHECK_REQUEST_READ_FAILED" for c in rows)
    assert all(_attempt(c["id"])["status"] == "done" for c in rows if c["is_current"])  # 같은 참조로 재시도해 성공
    assert all(b["check"]["state"] == "settled" for b in _checkable(ids))


# ── 취소 삭제 · 삭제 실패 집계 ─────────────────────────────────────────────────────────────────────
def test_s3_delete_prefix_counts_only_deleted_objects():
    from types import SimpleNamespace

    from app.storage import DeleteIncomplete, S3Store

    class Pager:
        def paginate(self, **kw):
            return [{"Contents": [{"Key": f"input/1/text_check/{i}.json"} for i in range(3)]}]

    class Client:
        def get_paginator(self, name):
            return Pager()

        def delete_objects(self, **kw):
            return {"Errors": [{"Key": kw["Delete"]["Objects"][0]["Key"], "Code": "AccessDenied"}]}

    s = S3Store.__new__(S3Store)
    s._s3 = SimpleNamespace(_s3=Client(), S3_BUCKET="test-bucket")
    with pytest.raises(DeleteIncomplete) as e:
        s.delete_prefix("input/1/")
    assert (e.value.deleted, e.value.failed) == (2, 1)  # 개별 실패를 성공으로 세지 않는다

    class OkClient(Client):
        def delete_objects(self, **kw):
            return {}

    s._s3 = SimpleNamespace(_s3=OkClient(), S3_BUCKET="test-bucket")
    assert s.delete_prefix("input/1/") == 3


def test_purge_failure_is_not_counted_and_logged_near_window_end(env, rule_on, monkeypatch, caplog):
    import logging

    from app import recovery
    from app.flows import cancel
    from app.storage import DeleteIncomplete

    ids = _to_n3(env)
    _proceed(ids, env)
    assert "input/" in "".join(cancel.job_prefixes(ids["job"]))
    real = env["store"].delete_prefix

    def broken(prefix):
        if prefix == f"input/{ids['job']}/":
            raise DeleteIncomplete(prefix, 0, 1, ["AccessDenied"])
        return real(prefix)

    monkeypatch.setattr(env["store"], "delete_prefix", broken)
    for name in ("app.flows.cancel", "app.recovery"):  # 마이그레이션의 로깅 설정이 기존 로거를 끈다 — 이 시험에서만 다시 켠다
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    with caplog.at_level(logging.ERROR):
        assert ids["c"].delete(f"/v1/jobs/{ids['job']}").status_code == 200  # 취소 자체는 끝난다(DB 콘텐츠 삭제 · 커밋)
    assert any("삭제 미완료" in r.getMessage() and str(ids["job"]) in r.getMessage() for r in caplog.records)
    assert _objects(env, ids["job"])  # 아직 남아 있다 — 성공으로 세지 않았다
    res = cancel.purge_storage(ids["job"])
    assert res["failed"] == 1
    monkeypatch.setenv("PIXLATE_PURGE_WINDOW_H", "1")  # 마지막 1시간 = 감시 기간 전체 — 종료 임박 로그
    stats = {"purged_jobs": 0, "purge_failed_jobs": 0}
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        recovery._purge_archived(stats)
    assert stats["purge_failed_jobs"] >= 1
    assert any("감시 기간 종료 임박" in r.getMessage() and f"job={ids['job']}" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(env["store"], "delete_prefix", real)
    stats = {"purged_jobs": 0, "purge_failed_jobs": 0}
    recovery._purge_archived(stats)  # 장애가 풀리면 기간 안의 재삭제가 지운다
    assert _objects(env, ids["job"]) == [] and stats["purge_failed_jobs"] == 0


# ── 실행 시작 뒤 · 채택 전 요청 객체 유실(본문 삭제 뒤 도착한 보고) ──────────────────────────────────
def _during_run(target_id, action, outcome):
    from pipeline.handoff import envelope, translation

    real = translation.run_text_check
    hit = {}

    def run(raw, cfg=None):
        if outcome == "done":
            rep = real(raw, cfg)
        else:  # 검사 자체의 실패 보고 — 이것도 믿을 수 없는 요청이면 일반 실패로 받지 않는다
            rep = envelope.failed_report(envelope.identity_fallback(raw, "text_check"), "internal", "대역 실패(일반 검사 실패 — 공급 · 입력 오류 아님)", retryable=False)
        if raw.get("attempt_id") == str(target_id) and not hit:  # 실행 시작 확인은 통과한 뒤
            hit["x"] = True
            action()
        return rep

    return run, hit


@pytest.mark.parametrize("outcome", ["done", "failed"])
@pytest.mark.parametrize("mode,code", [("deleted", "CHECK_REQUEST_MISSING"), ("tampered", "CHECK_REQUEST_HASH_MISMATCH"),
                                       ("unreadable", "CHECK_REQUEST_READ_FAILED")])
def test_request_lost_after_start_rejects_whole_report(env, rule_on, flaky, monkeypatch, mode, code, outcome):
    from app import db as app_db
    from pipeline.handoff import translation

    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    target = _checks(ids["job"])[0]
    key = target["input_manifest"]["request_ref"]["key"]
    before = _flags_of(target)

    def lose():
        if mode == "deleted":
            env["store"].delete(key)  # 객체만 유실(작업은 그대로)
        elif mode == "tampered":
            env["store"].objects[key] = env["store"].objects[key].replace(b"blk_", b"blk-", 1)
        else:
            flaky.get_error, flaky.bad_key = ConnectionError("대역 — 채택 전 읽기 장애"), key

    run, hit = _during_run(target["id"], lose, outcome)
    monkeypatch.setattr(translation, "run_text_check", run)
    _run_pending(ids, env)
    flaky.get_error = flaky.bad_key = None
    assert hit  # 정상 객체로 실행을 시작했다
    rows = [c for c in _checks(ids["job"]) if c["unit_id"] == target["unit_id"]]
    assert {_attempt(c["id"])["error_code"] for c in rows} == {code}  # 일반 실패(TEXT_CHECK_*)로 받지 않았다
    h = _q("SELECT state FROM task_handoff WHERE task_id = :t", t=target["id"])
    assert [r["state"] for r in h] == ["rejected"]  # 보고는 기록됐지만 전체 거절
    assert _flags_of(target) == before  # flag 무변경
    assert any(c["retry_origin"] == "auto" for c in rows) is (mode == "unreadable")
    assert _step(ids) == ("review", "N5")
    with app_db.SessionLocal() as db:
        with pytest.raises(text_check.CheckIntegrityError):
            text_check.job_block_states(db, ids["job"])
    with pytest.raises(text_check.CheckIntegrityError):  # 확정 통과 없음
        ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert _step(ids) == ("review", "N5")


def test_cancel_during_run_blocks_report_before_adoption(env, rule_on, monkeypatch):
    from pipeline.handoff import translation

    ids = _to_n3(env)
    _proceed(ids, env, skip={TC})
    target = _checks(ids["job"])[0]

    def cancel():
        assert ids["c"].delete(f"/v1/jobs/{ids['job']}").status_code == 200  # 전체 취소 — 객체 · DB 콘텐츠 삭제

    run, hit = _during_run(target["id"], cancel, "done")
    monkeypatch.setattr(translation, "run_text_check", run)
    _run_pending(ids, env)
    assert hit
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE job_id = :j", j=ids["job"])[0]["n"] == 0  # 보고 자체를 기록하지 않는다
    assert _attempt(target["id"])["status"] == "cancelled"  # 요청 무결성 실패가 아니라 취소로 끝난다
    assert _objects(env, ids["job"]) == []
    assert _q("SELECT status FROM job WHERE id = :j", j=ids["job"])[0]["status"] == "archived"
