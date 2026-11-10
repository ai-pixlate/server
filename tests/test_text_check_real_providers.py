"""규칙 검사 실제 공급자 연결 시험 — A1 `text_check_input_keys` · M3/A3 `GlossarySelectionSupply`(app.text_check_supply).

대역 시험(`test_text_check_connect.py` 의 FakeSupply · FakeKeys)과 나눈다. 여기서는 공급 · 키를 **실제 함수**로 등록하고
실제 PostgreSQL 용어집 행 → BE 공급 → AI 선별 · 키 함수 → 등록 · 실행(run_text_check, 모델 호출 없음) · 채택 · 조회 · 확정을 본다.
번역은 FakeTranslator(대역), 큐 · 저장소는 대역이다. 외부 모델 · S3 · 워커 프로세스는 쓰지 않는다.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app import db as app_db
from app import execution
from app.flows import downstream, text_check
from app.manifest import stable_hash
from app.text_check_supply import AiInputKeys, GlossarySelectionSupply, install_from_env
from tests.exec_db import requires_db
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401
from tests.test_text_check_connect import TC, _word_block

pytestmark = requires_db
EXT = "GL-T9"


@pytest.fixture
def real_on(monkeypatch, env):
    monkeypatch.setenv(text_check.ACTIVE_ENV, "rule")
    _q("DELETE FROM glossary WHERE external_id LIKE 'GL-T9%'")
    assert install_from_env() is True
    yield
    _q("DELETE FROM glossary WHERE external_id LIKE 'GL-T9%'")
    text_check.set_providers()


def _term(term_ko, *, cat="common", enforcement="enforced", kind="certification", target="ZZT9", n):
    with app_db.engine.begin() as c:
        return c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, "
                              "term_kind, version) VALUES (:k, :t, 'en', :c, :e, :x, :kind, 1) RETURNING id"),
                         {"k": term_ko, "t": target, "c": cat, "e": enforcement, "x": f"{EXT}{n:02d}", "kind": kind}).scalar_one()


def _checks(job):
    return _q("SELECT id, unit_id, status, is_current, input_manifest FROM job_async_task WHERE job_id = :j AND stage = 'text_check' "
              "ORDER BY id", j=job)


def _body(env, att):
    return json.loads(env["store"].get(att["input_manifest"]["request_ref"]["key"]))


def _n5(env, *, category=None):
    ids = _to_n3(env)
    if category:
        _q("UPDATE job SET internal_category = :c WHERE id = :j", c=category, j=ids["job"])
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    return ids


def _run(job):
    return _q("SELECT * FROM job_async_task WHERE job_id = :j AND run_kind = 'downstream' ORDER BY id DESC LIMIT 1", j=job)[0]


def _blocks(ids):
    return {b["id"]: b for b in ids["c"].get(f"/v1/jobs/{ids['job']}/blocks").json()}


# ── 프로세스 연결 · 기본 꺼짐 ────────────────────────────────────────────────────
def test_install_default_off_and_rule_installs_same_providers_for_api_and_worker(monkeypatch):
    from app import celery_app

    text_check.set_providers()
    monkeypatch.delenv(text_check.ACTIVE_ENV, raising=False)
    assert install_from_env() is False and not text_check.providers_ready()  # 기본 꺼짐 — 등록하지 않음
    assert text_check.configured_active_checks() == []
    monkeypatch.setenv(text_check.ACTIVE_ENV, "rule")
    try:
        celery_app._install_check_providers()  # 워커 시작 신호가 부르는 함수(API 는 app.main 이 같은 install_from_env)
        assert text_check.providers_ready()
        assert (text_check._supply.name, text_check._keys.name) == (GlossarySelectionSupply.name, AiInputKeys.name)
        assert text_check.configured_active_checks() == ["rule"]
    finally:
        text_check.set_providers()


# ── 실제 공급 · 키로 등록 → 실행 → 채택 → 조회 → 확정 ────────────────────────────────
def test_real_providers_full_flow_with_enforced_term(env, real_on):
    ids = _to_n3(env)
    blk, word = _word_block(ids)
    gid = _term(word, n=1)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    att = [a for a in _checks(ids["job"]) if a["unit_id"] == blk["section_id"]][-1]
    m = att["input_manifest"]
    assert (m["supply_policy"], m["key_provider"]) == (GlossarySelectionSupply.name, AiInputKeys.name)
    assert m["glossary_ref"]["status"] == "ok" and set(m["input_keys"]) == {f"blk_{t['block_id']}" for t in m["targets"]}
    body = _body(env, att)
    assert [t["glossary_id"] for t in body["glossary"]["terms"]] == [str(gid)]  # 검사 공급 = 강제 용어만
    rec = body["supply_record"]
    assert m["supply_record_sha256"] == stable_hash(rec)  # 불변 명세에는 기록 해시만
    assert rec["candidates"]["excludes_retired"] is True and any(r["glossary_id"] == str(gid) and r["version"] == 1 for r in rec["candidates"]["rows"])
    assert {"block_key": f"blk_{blk['id']}", "glossary_ids": [str(gid)]} in rec["check_selection"]["blocks"]
    assert "blocks" in rec["translation_selection"]
    flags = _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=blk["id"])[0]["compliance_flags"]
    assert any(f["type"] == "mandatory_term_unapplied" and str(f["glossary_id"]) == str(gid) for f in flags)  # 실제 규칙 검사 결과
    b = _blocks(ids)[blk["id"]]["check"]
    assert b["state"] == "settled" and b["inputKey"] == m["input_keys"][f"blk_{blk['id']}"]  # 조회 현재 키 = 등록 고정 키
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200


def test_real_keys_text_change_during_check_is_not_adopted(env, real_on):
    """실행은 고정 요청, 채택은 현재 입력의 실제 키 — 실행 중 번역문이 바뀌면 보고 전체를 채택하지 않는다."""
    ids = _n5(env)
    blk = [b for b in _blocks(ids).values() if b["check"] is not None][0]
    r = ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited once", "revision": blk["revision"]})
    assert r.status_code == 200
    att = [a for a in _checks(ids["job"]) if a["is_current"] and a["status"] == "pending"][0]
    env["q"].messages.clear()
    _q("UPDATE text_block SET trans_1 = 'Changed behind the check' WHERE id = :b", b=blk["id"])  # 등록 경로 밖 변경
    text_check.execute_text_check(att["id"])
    row = _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=att["id"])[0]
    assert row["status"] == "failed" and row["error_code"] in ("INPUT_CHANGED", "CHECK_KEY_UNAVAILABLE"), row
    assert _blocks(ids)[blk["id"]]["check"]["state"] in ("outdated", "missing", "in_progress")  # 자동 재등록이면 in_progress


def test_real_supply_same_for_every_origin_until_data_changes(env, real_on, monkeypatch):
    seen: dict[tuple[int, str], set[str]] = {}
    real = GlossarySelectionSupply.glossary

    def spy(self, db, run, section_id, targets, *, origin):
        g = real(self, db, run, section_id, targets, origin=origin)
        seen.setdefault((section_id, origin), set()).add(stable_hash(g))  # 공급 + 기록 전체
        return g

    monkeypatch.setattr(GlossarySelectionSupply, "glossary", spy)
    ids = _to_n3(env)
    blk, word = _word_block(ids)
    _term(word, n=2)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    _blocks(ids)  # query
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200  # confirm 판정(query)
    sid = blk["section_id"]
    hashes = set().union(*[v for (s, _o), v in seen.items() if s == sid])
    origins = {o for (s, o) in seen if s == sid}
    assert {"translate", "adopt", "query"} <= origins and len(hashes) == 1, (origins, hashes)  # 목적이 달라도 같은 공급 · 기록


def test_data_change_makes_outdated_with_real_keys(env, real_on):
    ids = _n5(env)
    blk, word = _word_block(ids)
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"
    _term(word, n=3)  # 그 블록 원문에 나오는 새 강제 용어 — 실제 데이터 변경
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated"
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]


def test_retry_adoption_registers_all_section_targets(env, real_on):
    """번역 재시도 채택의 검사 등록은 그 섹션 검사 대상 전부(재시도 대상 블록만이 아님)."""
    from app import ai_adapters
    from tests.exec_fakes import FakeTranslator

    ids = _to_n3(env)
    rows = _q("SELECT tb.id, tb.source_ko, tb.section_id FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
              "ORDER BY tb.id", j=ids["job"])
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]
    tr = FakeTranslator(fail_keys={f"blk_{target['id']}"})
    ai_adapters.set_adapters(translator=tr)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    att = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current AND status = 'failed'", j=ids["job"])[0]
    tr.fail_keys = set()
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/tasks/{att['id']}/retry").status_code == 200
    env["q"].drain(skip={TC})
    cur = [a for a in _checks(ids["job"]) if a["is_current"] and a["unit_id"] == target["section_id"]][0]
    want = {r["id"] for r in text_check.checkable(downstream._block_rows(app_db.SessionLocal(), target["section_id"]))}
    assert {t["block_id"] for t in cur["input_manifest"]["targets"]} == want and target["id"] in want and len(want) > 1


# ── A3 · term_kind: 실제 DB 행 → BE 공급 함수(번역 · 검사) ──────────────────────────────────
def _supplies(env, ids, sources):
    """같은 DB 행 · 같은 적용 분류로 번역 공급(downstream._glossary_supply)과 검사 공급(GlossarySelectionSupply)을 부른다.
    대상 블록 원문은 시험 문장(블록 id 는 실제 섹션 블록)."""
    run = _run(ids["job"])
    sid = _q("SELECT id FROM section WHERE job_id = :j AND bucket = 'include' ORDER BY id LIMIT 1", j=ids["job"])[0]["id"]
    bids = [r["id"] for r in _q("SELECT id FROM text_block WHERE section_id = :s ORDER BY id", s=sid)][: len(sources)]
    assert len(bids) == len(sources)
    targets = [{"id": b, "source_ko": s} for b, s in zip(bids, sources)]
    db = app_db.SessionLocal()
    try:
        tr, _sel = downstream._glossary_supply(db, "en", sid, targets)
        chk = GlossarySelectionSupply().glossary(db, run, sid, targets, origin="test")
    finally:
        db.close()
    return bids, tr, chk


def test_same_span_certification_over_phrase_in_translation_and_check(env, real_on):
    ids = _n5(env, category="G05")
    cert = _term("Vegan Certified", n=10)
    phrase = _term("Vegan Certified", cat="G05", enforcement="reference", kind="marketing", target="vegan-certified", n=11)
    bids, tr, chk = _supplies(env, ids, ["This is Vegan Certified"])
    assert tr["status"] == "ok" and [t["glossary_id"] for t in tr["terms"]] == [str(cert)]  # 번역: 인증 우선 · 문구 제외
    assert chk["status"] == "ok" and [t["glossary_id"] for t in chk["terms"]] == [str(cert)]
    rec = chk["supply_record"]
    ex = rec["translation_selection"]["exclusions"]
    assert [(x["reference_id"], x["enforced_ids"], x["rule"]) for x in ex] == [(str(phrase), [str(cert)], "certification_over_phrase")]
    assert rec["excluded_entries"] == [{"glossary_id": str(phrase), "term_ko": "Vegan Certified", "term_target": "vegan-certified",
                                        "enforcement": "reference", "term_kind": "marketing"}]  # 제외 후보 내용 보존


def test_mixed_conflict_and_normal_phrase_in_one_block(env, real_on):
    """같은 블록에서 문구가 충돌 구간(인증과 같은 구간)과 정상 구간에 함께 매칭 — 번역 공급은 실패(빈 성공 아님), 검사 공급은 강제 용어로 계산."""
    ids = _n5(env, category="G05")
    cert = _term("Vegan Certified", n=12)
    _term("Vegan Certified", cat="G05", enforcement="reference", kind="marketing", target="vegan-certified", n=13)
    ingr = _term("Vegan Certified Oil", kind="ingredient", target="VCO", n=14)  # 첫 번째 위치의 인증을 억제 → 거기선 문구가 정상 후보
    bids, tr, chk = _supplies(env, ids, ["Vegan Certified Oil and Vegan Certified"])
    assert tr["status"] == "failed" and "혼재" in tr["reason"] and tr["terms"] == []
    assert chk["status"] == "ok" and sorted(t["glossary_id"] for t in chk["terms"]) == sorted([str(cert), str(ingr)])
    assert "error" in chk["supply_record"]["translation_selection"] and chk["supply_record"]["excluded_entries"] is None


def test_effective_supply_differs_per_block(env, real_on):
    """블록 1 은 문구가 충돌 제외, 블록 2 는 문구가 정상 — 한 목록 번역 공급은 실패, 검사 Selection 은 블록별 관계를 기록."""
    ids = _n5(env, category="G05")
    cert = _term("Vegan Certified", n=15)
    phrase = _term("Vegan Certified", cat="G05", enforcement="reference", kind="marketing", target="vegan-certified", n=16)
    ingr = _term("Vegan Certified Oil", kind="ingredient", target="VCO", n=17)
    bids, tr, chk = _supplies(env, ids, ["Only Vegan Certified here", "Contains Vegan Certified Oil"])
    assert tr["status"] == "failed" and tr["terms"] == []  # 블록별 관계가 필요한 합집합 — 한 목록으로 공급하지 않는다
    sel = chk["supply_record"]["translation_selection"]
    per = {b["block_key"]: b["glossary_ids"] for b in sel["blocks"]}
    assert per == {f"blk_{bids[0]}": [str(cert)], f"blk_{bids[1]}": [str(phrase), str(ingr)]}
    chk_per = {b["block_key"]: b["glossary_ids"] for b in chk["supply_record"]["check_selection"]["blocks"]}
    assert chk_per == {f"blk_{bids[0]}": [str(cert)], f"blk_{bids[1]}": [str(ingr)]}
    assert chk["status"] == "ok"


def test_reference_conflict_does_not_block_check_but_enforced_conflict_fails_supply(env, real_on):
    ids = _n5(env, category="G05")
    _term("Hydra Boost", enforcement="reference", kind="marketing", target="hydra-boost", n=18)
    _term("HYDRA  BOOST", cat="G05", enforcement="reference", kind="marketing", target="Hydra-Boost", n=19)
    bids, tr, chk = _supplies(env, ids, ["New Hydra Boost serum"])
    assert tr["status"] == "failed"  # 참고끼리 정규화 충돌 — 번역 후보 선별 실패
    assert chk["status"] == "ok" and chk["terms"] == []  # 검사 공급(강제 용어만)은 막히지 않는다
    assert "error" in chk["supply_record"]["translation_selection"]
    a = _term("Aqua Shield", n=20)
    b = _term("AQUA  SHIELD", cat="G05", n=21)
    bids, tr, chk = _supplies(env, ids, ["With Aqua Shield inside"])
    assert chk["status"] == "failed" and chk["terms"] == [] and "검사 용어 선별 실패" in chk["reason"]  # 강제 용어 자체 충돌 — 빈 성공 아님
    assert "error" in chk["supply_record"]["check_selection"] and str(a) in chk["reason"] and str(b) in chk["reason"]


def test_failed_check_supply_keys_ok_but_manifest_rejected_registration_unresolved(env, real_on):
    """강제 용어 자체 충돌로 검사 공급이 failed 일 때(관찰 · 정책 미정):
    - 실제 A1 키는 계산된다(text_check_input_keys 는 실패 공급을 받아들인다).
    - 실제 AI 지문 함수 text_check_manifest 는 실패 공급을 거부(ValueError)한다 → BE 는 시도를 만들지 않고 미해결로 둔다(500 · 롤백 아님).
    - 그 블록은 outdated 로 남고 확정은 보조 등록도 못 한 409(registeredBlockIds []) — 공급 실패를 검사 실패로 통과시킬지는 정하지 않았다."""
    from pipeline.handoff.translation import text_check_input_keys, text_check_manifest

    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    _term(word, n=22)
    _term(word.upper() if word.upper() != word else word.lower(), cat="G05", n=23)  # 정규화 후 같은 원문의 다른 강제 용어
    db = app_db.SessionLocal()
    try:
        run = _run(ids["job"])
        rows = downstream._block_rows(db, blk["section_id"])
        g, _rec = text_check.split_supply(GlossarySelectionSupply().glossary(db, run, blk["section_id"], text_check.checkable(rows),
                                                                             origin="test"))
        req = text_check.build_request(db, run, blk["section_id"], rows, g)
    finally:
        db.close()
    assert g["status"] == "failed"
    assert set(text_check_input_keys(req)) == {t["block_key"] for t in req["targets"]}  # 키는 계산된다
    with pytest.raises(ValueError):
        text_check_manifest(req)  # 지문은 거부 — 두 AI 함수의 실패 공급 처리 차이(AI 확인 항목)
    n = len(_checks(ids["job"]))
    b = _blocks(ids)[blk["id"]]
    r = ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited for supply failure", "revision": b["revision"]})
    assert r.status_code == 200, r.text  # 저장은 성공, 검사 등록만 미해결
    assert len(_checks(ids["job"])) == n
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated"
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"] == []  # 진행 없는 409(관찰)
    assert len(_checks(ids["job"])) == n


def test_null_term_kind_is_not_inferred_from_external_id(env, real_on):
    ids = _n5(env, category="G05")
    with app_db.engine.begin() as c:  # GL-C 처럼 보이는 외부 ID 지만 term_kind NULL
        c.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, term_kind, "
                       "version) VALUES ('Clean Beauty', 'CB', 'en', 'common', 'enforced', 'GL-T9C30', NULL, 1)"))
    _term("Clean Beauty", cat="G05", enforcement="reference", kind="marketing", target="clean-beauty", n=31)
    bids, tr, chk = _supplies(env, ids, ["A Clean Beauty pick"])
    assert tr["status"] == "failed"  # 충돌 예외 미적용(NULL) — 접두어로 인증이라 추정하지 않는다
    assert chk["status"] == "ok" and len(chk["terms"]) == 1
    rec = chk["supply_record"]  # 검사 공급자도 NULL 을 그대로 둔다 — 같은 행으로 번역 후보 선별이 같은 이유로 실패해야 한다
    assert {e["glossary_id"]: e["term_kind"] for e in rec["check_selection"]["entries"]} == {chk["terms"][0]["glossary_id"]: None}
    assert "error" in rec["translation_selection"] and rec["excluded_entries"] is None


def test_db_query_failure_is_not_empty_supply(env, real_on, monkeypatch):
    from sqlalchemy.exc import OperationalError

    ids = _n5(env)
    run = _run(ids["job"])
    sid = _q("SELECT id FROM section WHERE job_id = :j AND bucket = 'include' ORDER BY id LIMIT 1", j=ids["job"])[0]["id"]

    def boom(self, db, lang, cats):
        raise OperationalError("SELECT glossary", {}, Exception("대역 — DB 조회 실패"))

    monkeypatch.setattr(GlossarySelectionSupply, "candidate_rows", boom)
    db = app_db.SessionLocal()
    try:
        with pytest.raises(OperationalError):
            GlossarySelectionSupply().glossary(db, run, sid, [{"id": 1, "source_ko": "x"}], origin="test")
        n = len(_checks(ids["job"]))
        with pytest.raises(OperationalError):  # 등록 경로도 빈 공급으로 등록하지 않는다(예외 — 미등록)
            text_check.ensure_for_job(db, ids["job"], sid, origin="user")
        db.rollback()
    finally:
        db.close()
    assert len(_checks(ids["job"])) == n


# ── 강제 용어 충돌이 status=ok 로 전달되는 경우(AI 11-10 회신 3) ─────────────────────────────────
def _ok_conflict_supply(monkeypatch):
    """대역 1곳: 실제 검사 공급이 「강제 용어 선별 실패」면 같은 후보의 시험 강제 용어(GL-T9)를 그대로 status=ok 로 넘긴다 —
    BE 선별을 거치지 않은 공급(실제 공급자에서는 생기지 않는 조건). 그 밖에는 실제 공급 그대로."""
    real = GlossarySelectionSupply.glossary

    def fake(self, db, run, section_id, targets, *, origin):
        g = real(self, db, run, section_id, targets, origin=origin)
        if g["status"] == "failed" and "검사 용어 선별 실패" in (g.get("reason") or ""):
            rows = db.execute(text(
                "SELECT id, term_ko, term_target FROM glossary WHERE external_id LIKE 'GL-T9%' AND enforcement = 'enforced' "
                "AND retired_at IS NULL ORDER BY id")).mappings().all()
            terms = [{"glossary_id": str(r["id"]), "term_ko": r["term_ko"], "term_target": r["term_target"], "enforcement": "enforced"}
                     for r in rows]
            return {"status": "ok", "version": "test-ok-conflict", "reason": None, "terms": terms, "supply_record": g["supply_record"]}
        return g

    monkeypatch.setattr(GlossarySelectionSupply, "glossary", fake)


def test_ok_supply_with_enforced_conflict_is_real_input_invalid_but_never_registered(env, real_on, monkeypatch):
    """실제 AI 함수: 지문은 통과하고 run_text_check 는 지문이 있는 input_invalid 실패 보고를 낸다(일반 검사 실패로 채택하면 확정 통과).
    BE 는 같은 AI 함수(applicable_enforced_ids)로 등록 전에 걸러 미해결로 둔다 → 시도 없음 · 확정 차단 · 플래그 미기록."""
    from pipeline.handoff.translation import run_text_check, text_check_manifest

    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    sid = blk["section_id"]
    _term(word, n=60)
    _term(word.upper() if word.upper() != word else word.lower(), cat="G05", n=61)
    _ok_conflict_supply(monkeypatch)
    db = app_db.SessionLocal()
    try:
        run = _run(ids["job"])
        rows = downstream._block_rows(db, sid)
        g, _rec = text_check.split_supply(GlossarySelectionSupply().glossary(db, run, sid, text_check.checkable(rows), origin="test"))
        req = text_check.build_request(db, run, sid, rows, g)
        _, msha = text_check_manifest(req)  # 지문은 통과
        rep = run_text_check({**req, "attempt_id": "t", "expected_input_manifest_sha256": msha})
        assert g["status"] == "ok" and rep.outcome == "failed" and rep.failure.kind == "input_invalid"
        assert rep.input_manifest_sha256 == msha  # 같은 고정 입력에 대한 실패 보고(지문 있음) — 지문 없는 요청 형식 · 실패 공급 input_invalid 와 다른 모양(계약 보장 여부는 AI 확인)
        reg = text_check.register(db, execution.lock_task(db, run["id"]), sid, origin="test")
        assert reg.status == text_check.UNRESOLVED_REQUEST_INVALID and "강제 용어 적용 대상 판단 불가" in reg.reason
    finally:
        db.rollback()
        db.close()
    flags = _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=blk["id"])[0]["compliance_flags"]
    n = len(_checks(ids["job"]))
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited ok-conflict", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    assert len(_checks(ids["job"])) == n  # 시도 없음 — 일반 failed 로 흐르지 않는다
    assert _blocks(ids)[blk["id"]]["check"]["state"] in ("outdated", "missing")
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"] == []  # 확정 차단
    assert _step(ids) == ("review", "N5")
    own = [f for f in (_q("SELECT compliance_flags FROM text_block WHERE id = :b", b=blk["id"])[0]["compliance_flags"] or [])
           if f.get("producer") in text_check.OWN_PRODUCERS]
    assert own == [f for f in (flags or []) if f.get("producer") in text_check.OWN_PRODUCERS]  # 검사 결과 · flag 로 채택하지 않음


def test_ok_conflict_recovers_after_data_fix_with_new_request_and_separate_confirm(env, real_on, monkeypatch):
    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    _term(word, n=62)
    other = _term(word.upper() if word.upper() != word else word.lower(), cat="G05", n=63)
    _ok_conflict_supply(monkeypatch)
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"] == []
    _q("UPDATE glossary SET retired_at = now() WHERE id = :i", i=other)  # 데이터 수정(충돌 행 비활성)
    r = ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]  # 새 요청 등록 — 이 요청은 409 로 끝남
    assert _step(ids) == ("review", "N5")
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"  # 정상 검사
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200  # 별도 확정


# ── 공급 기록(supply_record@2) — AI A3 조건부 수용(11-10): 후보 내용 보존 · 실패 단계 · 독립성 · 고정 요청 일치 · 취소 삭제 ──────────
def test_record_keeps_candidate_inputs_when_selection_fails_before_selecting(env, real_on):
    """선별 전 실패(Prepared 데이터 검증 — term_kind 빈 문자열): 두 선별 모두 실패, 후보 입력값이 기록에 남아 같은 함수로 재현된다."""
    from pipeline import glossary as gl

    ids = _n5(env, category="G05")
    bad = _term("Broken Kind", kind="", target="BK", n=70)  # term_kind '' — 선별 함수 입력 검증에서 실패
    src = ["Has Broken Kind inside"]
    bids, tr, chk = _supplies(env, ids, src)
    rec = chk["supply_record"]
    assert rec["format"] == "text-check-supply-record@2" and chk["status"] == "failed" and chk["terms"] == []
    assert [f["stage"] for f in rec["failures"]] == ["translation_selection", "check_selection"]
    row = [r for r in rec["candidates"]["rows"] if r["glossary_id"] == str(bad)]
    assert row == [{"glossary_id": str(bad), "term_ko": "Broken Kind", "term_target": "BK", "enforcement": "enforced", "term_kind": "",
                    "internal_category": "common", "version": 1}]
    assert rec["candidates"]["block_keys"] == [f"blk_{b}" for b in bids]
    blocks = list(zip(rec["candidates"]["block_keys"], src))  # 원문은 고정 요청 request.blocks 에 있다(여기서는 시험 입력)
    entries = [gl.Entry(r["glossary_id"], r["term_ko"], r["term_target"], r["enforcement"], r["term_kind"])
               for r in rec["candidates"]["rows"] if r["enforcement"] == "enforced"]
    with pytest.raises(gl.GlossaryDataError) as e:
        gl.select_by_block(blocks, entries)  # 기록의 후보 입력값만으로 같은 실패를 재현
    assert str(e.value) == rec["check_selection"]["error"]


def test_reference_failure_does_not_block_check_and_fixed_request_matches_record(env, real_on):
    """참고 선별 실패 + 강제 검사 공급 성공. 실제 등록된 고정 요청의 공급 = check_selection = 후보 행 내용, 명세 해시 = 기록."""
    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    gid = _term(word, kind="ingredient", n=71)  # 인증이 아님 — 인증 ↔ 문구 예외가 적용되지 않게
    _term(word, cat="G05", enforcement="reference", kind="marketing", target="ref-a", n=72)
    _term(word.upper() if word.upper() != word else word.lower(), cat="G05", enforcement="reference", kind="marketing",
          target="ref-b", n=73)  # 참고 · 강제가 같은 원문 — 번역 후보 선별은 실패(인증 ↔ 문구 예외 아님)
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited record", "revision": b["revision"]}).status_code == 200
    att = [a for a in _checks(ids["job"]) if a["is_current"] and a["unit_id"] == blk["section_id"]][-1]
    body = _body(env, att)
    rec, g, m = body["supply_record"], body["glossary"], att["input_manifest"]
    assert g["status"] == "ok" and [f["stage"] for f in rec["failures"]] == ["translation_selection"]
    assert "error" in rec["translation_selection"] and rec["translation_selection_basis"] == "check_registration_recompute"
    chk_ids = [e["glossary_id"] for e in rec["check_selection"]["entries"]]
    assert [t["glossary_id"] for t in g["terms"]] == chk_ids and str(gid) in chk_ids  # 요청 공급 = 검사 선별 기록
    rows = {r["glossary_id"]: r for r in rec["candidates"]["rows"]}
    for t, e in zip(g["terms"], rec["check_selection"]["entries"]):
        assert (t["term_ko"], t["term_target"]) == (e["term_ko"], e["term_target"]) == (rows[t["glossary_id"]]["term_ko"],
                                                                                        rows[t["glossary_id"]]["term_target"])
    assert m["supply_record_sha256"] == stable_hash(rec) and m["glossary_ref"]["sha256"] == stable_hash(g)


def test_zero_candidates_is_normal_record_but_query_failure_has_none(env, real_on, monkeypatch):
    from sqlalchemy.exc import OperationalError

    ids = _n5(env)
    run = _run(ids["job"])
    sid = _q("SELECT id FROM section WHERE job_id = :j AND bucket = 'include' ORDER BY id LIMIT 1", j=ids["job"])[0]["id"]
    zero = {**run, "run_scope": {**(run["run_scope"] or {}), "target_lang": "zz"}}  # 행이 없는 대상 언어 — 실제 조회 0건
    db = app_db.SessionLocal()
    try:
        g = GlossarySelectionSupply().glossary(db, zero, sid, [{"id": 1, "source_ko": "anything"}], origin="test")
        assert g["status"] == "ok" and g["terms"] == []
        rec = g["supply_record"]
        assert rec["candidates"]["rows"] == [] and rec["failures"] == [] and rec["check_selection"]["entries"] == []

        def boom(self, db, lang, cats):
            raise OperationalError("SELECT glossary", {}, Exception("대역 — DB 조회 실패"))

        monkeypatch.setattr(GlossarySelectionSupply, "candidate_rows", boom)
        with pytest.raises(OperationalError):  # 조회 장애 — 기록 · 공급 없음(빈 성공 아님)
            GlossarySelectionSupply().glossary(db, run, sid, [{"id": 1, "source_ko": "anything"}], origin="test")
    finally:
        db.close()


def test_record_content_lives_only_in_job_object_and_is_deleted_on_cancel(env, real_on):
    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    _term(word, n=74)
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited cancel", "revision": b["revision"]}).status_code == 200
    att = [a for a in _checks(ids["job"]) if a["is_current"] and a["unit_id"] == blk["section_id"]][-1]
    m = att["input_manifest"]
    assert m["request_ref"]["key"].startswith(f"input/{ids['job']}/") and _body(env, att)["supply_record"]["candidates"]["rows"]
    assert "candidates" not in json.dumps(m, ensure_ascii=False) and word not in json.dumps(m, ensure_ascii=False)  # 명세에는 내용 없음
    assert ids["c"].delete(f"/v1/jobs/{ids['job']}").status_code == 200
    assert env["store"].keys(f"input/{ids['job']}/") == []  # 기존 취소 삭제 범위(input/{job_id}/)에 함께 지워진다


# ── 실행 단계 공급 · 입력 · 데이터 오류(AI 11-10 추가 회신 · API 6-7): input_invalid · bundle_invalid 는 일반 최종 실패가 아니다 ──────
def _confirm(ids):
    return ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})


def _own_flags(bid):
    return [f for f in (_q("SELECT compliance_flags FROM text_block WHERE id = :b", b=bid)[0]["compliance_flags"] or [])
            if f.get("producer") in text_check.OWN_PRODUCERS]


def _cur_check(ids, sid):
    return _q("SELECT id, status, error_code, retry_count, input_manifest FROM job_async_task WHERE job_id = :j AND stage = 'text_check' "
              "AND unit_id = :s AND is_current", j=ids["job"], s=sid)[0]


def test_bypassed_fixed_request_runs_real_input_invalid_and_cannot_pass_confirm(env, real_on, monkeypatch):
    """실제 AI 함수 재현: 등록 전 확인을 우회해 등록된 고정 요청(= 그 확인 이전 코드로 등록된 시도)을 확인을 켠 뒤 실제 run_text_check 로 실행.
    지문 있는 input_invalid → 확정 우회 차단 · 정상 flag 보존 · 반복 확정에 새 시도 없음 · 데이터 수정 뒤 새 요청 · 정상 채택 · 별도 확정 200."""
    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    sid = blk["section_id"]
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"
    flags0 = _own_flags(blk["id"])
    _term(word, n=80)
    other = _term(word.upper() if word.upper() != word else word.lower(), cat="G05", n=81)
    _ok_conflict_supply(monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(text_check, "_enforced_terms_unresolvable", lambda targets, glossary: None)  # 우회 등록
        r = _confirm(ids)
        assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]
    env["q"].drain()  # 확인을 켠 뒤 실행 — 실제 run_text_check
    att = _cur_check(ids, sid)
    assert att["status"] == "failed" and att["error_code"] == "TEXT_CHECK_INPUT_INVALID"
    h = _q("SELECT payload->'report'->>'input_manifest_sha256' AS s FROM task_handoff WHERE task_id = :t", t=att["id"])[0]
    assert h["s"] == att["input_manifest"]["request_manifest_sha256"]  # 같은 고정 입력에 대한 실패 보고(지문 있음)
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated"  # 오류 시도를 빼고 판단 — failed 아님
    assert _own_flags(blk["id"]) == flags0  # 오류 시도가 기존 정상 flag 를 덮어쓰지 않는다
    n = len(_checks(ids["job"]))
    for _ in range(3):
        r = _confirm(ids)
        assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"] == []
    env["q"].drain()
    assert len(_checks(ids["job"])) == n and _step(ids) == ("review", "N5")  # 같은 잘못된 입력을 반복 등록하지 않음
    _q("UPDATE glossary SET retired_at = now() WHERE id = :i", i=other)  # 데이터 수정
    r = _confirm(ids)
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"] and _step(ids) == ("review", "N5")
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled" and _cur_check(ids, sid)["status"] == "done"
    assert _confirm(ids).status_code == 200


@pytest.mark.parametrize("kind,with_manifest", [("input_invalid", False), ("bundle_invalid", False), ("bundle_invalid", True)])
def test_stand_in_input_and_bundle_errors_block_confirm_and_recover(env, real_on, monkeypatch, kind, with_manifest):
    """대역(AI 보고 생성 함수 failed_report 사용): 지문 없는 input_invalid · bundle_invalid, 지문 있는 bundle_invalid 모두 확정 우회 차단 ·
    반복 등록 없음 · 번역문 수정(입력 변경) 뒤 정상 검사 · 별도 확정 200."""
    from pipeline.handoff import envelope, translation

    real = translation.run_text_check
    state = {"fail": True}

    def run(raw, cfg=None):
        if not state["fail"]:
            return real(raw, cfg)
        good = real(raw, cfg)
        man = good.input_manifest if with_manifest else None
        return envelope.failed_report(envelope.identity_fallback(raw, "text_check"), kind, "대역 오류", retryable=False, manifest=man,
                                      target_count=good.target_count)

    ids = _n5(env)
    blk, _word = _word_block(ids)
    flags0 = _own_flags(blk["id"])
    monkeypatch.setattr(translation, "run_text_check", run)
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited err", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    att = _cur_check(ids, blk["section_id"])
    assert att["status"] == "failed" and att["error_code"] == f"TEXT_CHECK_{kind.upper()}" and att["retry_count"] == 0
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated" and _own_flags(blk["id"]) == flags0
    n = len(_checks(ids["job"]))
    for _ in range(2):
        r = _confirm(ids)
        assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"] == []
    assert len(_checks(ids["job"])) == n and _step(ids) == ("review", "N5")
    db = app_db.SessionLocal()
    try:  # 같은 입력은 「유효한 시도 재사용」이 아니라 미해결로 보고된다(일괄 등록 CLI 보고 구분)
        reg = text_check.register(db, execution.lock_task(db, _run(ids["job"])["id"]), blk["section_id"], origin="test")
        assert reg.status == text_check.UNRESOLVED_REQUEST_INVALID and reg.attempt_id is None
    finally:
        db.rollback()
        db.close()
    state["fail"] = False  # (원인 해소) + 입력 수정 — 키가 바뀌어야 새 요청이 등록된다
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited fixed", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"
    assert _confirm(ids).status_code == 200


def test_manifest_mismatch_report_is_rejected_by_existing_validation(env, real_on, monkeypatch):
    """지문 불일치 보고: 기존 validate_report 가 거절(CHECK_RESULT_INVALID) — 채택 · flag 없음. 이번 분류(공급 · 입력 오류) 대상이 아니다."""
    from pipeline.handoff import envelope, translation

    def run(raw, cfg=None):
        return envelope.failed_report(envelope.identity_fallback(raw, "text_check"), "input_invalid", "대역", retryable=False,
                                      manifest={"other": "request"})

    ids = _n5(env)
    blk, _word = _word_block(ids)
    flags0 = _own_flags(blk["id"])
    monkeypatch.setattr(translation, "run_text_check", run)
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited mismatch", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    att = _cur_check(ids, blk["section_id"])
    assert att["status"] == "failed" and att["error_code"] == "CHECK_RESULT_INVALID"  # 기존 검증 거절(재시도 후 최종 — 기존 정책)
    assert not _q("SELECT 1 FROM task_handoff WHERE task_id = :t AND state = 'adopted' AND outcome = 'done'", t=att["id"])
    assert _own_flags(blk["id"]) == flags0


def test_bundle_conversion_failure_at_execution_blocks_confirm_without_reuse_or_repeat(env, real_on, monkeypatch):
    """규칙 검사 단계의 AI_ADAPTER_UNAVAILABLE(실행 때 고정 사전 묶음 변환 실패 — 대역: to_ai_bundle 이 BundleError).
    등록은 성공 · 실행 때만 실패 → 일반 failed 로 확정 통과 안 함 · 기존 flag 보존 · 같은 입력은 「재사용」이 아니라 미해결 · 반복 등록 없음.
    변환이 실패하는 동안은 현재 키 계산도 같은 변환을 써서 /blocks 가 500 이다(7.2 ⑨ — 관찰). 회복은 키가 바뀌는 입력 수정에서만."""
    from fastapi.testclient import TestClient

    from app import dictionary_bundle

    def boom(bundle):
        raise dictionary_bundle.BundleError("대역 — 정책 규칙이 묶음에 없다", retryable=False, code="POLICY_RULES_UNAVAILABLE")

    ids = _n5(env)
    blk, _w = _word_block(ids)
    sid = blk["section_id"]
    flags0 = _own_flags(blk["id"])
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited conv", "revision": b["revision"]}).status_code == 200
    c2 = TestClient(ids["c"].app, raise_server_exceptions=False)
    c2.headers.update(ids["c"].headers)
    with monkeypatch.context() as m:
        m.setattr(dictionary_bundle, "to_ai_bundle", boom)  # 등록 뒤 · 실행 때만
        env["q"].drain()
        assert c2.get(f"/v1/jobs/{ids['job']}/blocks").status_code == 500  # 변환 실패가 이어지는 동안(관찰)
    att = _cur_check(ids, sid)
    assert att["status"] == "failed" and att["error_code"] == "AI_ADAPTER_UNAVAILABLE" and att["retry_count"] == 0
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated" and _own_flags(blk["id"]) == flags0  # failed 아님 · flag 그대로
    n = len(_checks(ids["job"]))
    for _ in range(2):
        r = _confirm(ids)
        assert r.status_code == 409 and r.json()["error"]["details"]["registeredBlockIds"] == []
    assert len(_checks(ids["job"])) == n and _step(ids) == ("review", "N5")
    db = app_db.SessionLocal()
    try:
        reg = text_check.register(db, execution.lock_task(db, _run(ids["job"])["id"]), sid, origin="test")
        assert reg.status == text_check.UNRESOLVED_REQUEST_INVALID and reg.attempt_id is None  # 「유효한 시도 재사용」이 아님
    finally:
        db.rollback()
        db.close()
    b = _blocks(ids)[blk["id"]]  # 키가 바뀌는 입력 수정(번역문) — 지원되는 회복 조건
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited conv fixed", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"
    assert _confirm(ids).status_code == 200
