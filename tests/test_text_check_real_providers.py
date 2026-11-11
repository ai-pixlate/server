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


def test_reference_conflict_does_not_block_check_and_enforced_conflict_is_block_local(env, real_on):
    ids = _n5(env, category="G05")
    _term("Hydra Boost", enforcement="reference", kind="marketing", target="hydra-boost", n=18)
    _term("HYDRA  BOOST", cat="G05", enforcement="reference", kind="marketing", target="Hydra-Boost", n=19)
    bids, tr, chk = _supplies(env, ids, ["New Hydra Boost serum"])
    assert tr["status"] == "failed"  # 참고끼리 정규화 충돌 — 번역 후보 선별 실패
    assert chk["status"] == "ok" and chk["terms"] == []  # 검사 공급(강제 용어만)은 막히지 않는다
    assert "error" in chk["supply_record"]["translation_selection"]
    a = _term("Aqua Shield", n=20)
    b = _term("AQUA  SHIELD", cat="G05", n=21)
    bids, tr, chk = _supplies(env, ids, ["With Aqua Shield inside", "Plain sentence"])
    # 강제 용어 자체 충돌(정규화 같은 원문) — 후보 형식은 정상이라 블록 국소 오류: 그 블록만 local_failures, 공급은 status=ok(11-11 AI 회신)
    assert chk["status"] == "ok" and chk["local_failures"] == [f"blk_{bids[0]}"]
    assert sorted(t["glossary_id"] for t in chk["terms"]) == sorted([str(a), str(b)])  # 충돌 후보를 지우지 않는다(그 블록 키에만 반영)
    rec = chk["supply_record"]
    assert [x["block_key"] for x in rec["check_selection"]["local_failures"]] == [f"blk_{bids[0]}"]
    # 번역 후보 선별(기록 전용 재계산)은 섹션 전체라 같은 충돌로 실패를 남긴다 — 검사 공급 상태에는 영향 없음(위 status=ok)
    assert [(f["stage"], f.get("block_key")) for f in rec["failures"]] == [
        ("translation_selection", None), ("check_selection", f"blk_{bids[0]}")]
    assert "error" in rec["translation_selection"]
    assert {b["block_key"] for b in rec["check_selection"]["blocks"]} == {f"blk_{bids[1]}"}  # 정상 블록 선별


def test_common_supply_error_is_section_wide_failed_without_attempt_and_confirm_passes(env, real_on):
    """섹션 공통 오류(같은 후보 집합의 공통 검증 실패 — term_kind 빈 문자열): 검사 공급 status=failed. 실제 A1 키는 계산되고 실제 지문 함수는
    실패 공급을 거부한다 → 시도를 만들지 않는다. 조회는 현재 입력으로 다시 판단해 그 섹션 대상 블록 전부를 failed(inputKey = 현재 키) · 확정 통과."""
    from pipeline.handoff.translation import text_check_input_keys, text_check_manifest

    ids = _n5(env, category="G05")
    blk, _w = _word_block(ids)
    _term("Broken Kind", kind="", target="BK", n=22)  # 후보 형식 오류 — 블록과 무관한 공통 오류
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
    keys = text_check_input_keys(req)
    assert set(keys) == {t["block_key"] for t in req["targets"]}  # 키는 계산된다
    with pytest.raises(ValueError):
        text_check_manifest(req)  # 지문은 거부 — 실행할 요청은 만들지 않는다
    n = len(_checks(ids["job"]))
    b = _blocks(ids)[blk["id"]]
    r = ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited for supply failure", "revision": b["revision"]})
    assert r.status_code == 200, r.text  # 번역문 저장 성공 — 검사 등록만 안 됨
    assert len(_checks(ids["job"])) == n
    sec_blocks = {int(t["block_key"].split("_")[1]) for t in req["targets"]}
    st = _blocks(ids)
    assert all(st[x]["check"]["state"] == "failed" for x in sec_blocks)  # 공통 오류는 섹션 전체
    assert st[blk["id"]]["check"]["inputKey"] != b["check"]["inputKey"]
    assert _checks_pass(ids) and len(_checks(ids["job"])) == n
    assert _confirm(ids).status_code == 200 and _step(ids) == ("review", "N6")


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


# ── 블록 국소 강제 용어 충돌(11-11 AI 회신 — 규칙 검사에 한한 요청 대상 예외) ─────────────────────────────────
def test_block_local_conflict_is_excluded_from_request_and_rest_is_really_checked(env, real_on):
    """실제 AI 함수: 충돌 블록 A 를 넣은 전체 요청은 실행하면 input_invalid(섹션 전체 실패)다. 그래서 등록은 A 를 실행 요청에서 빼고
    (고정 명세 local_failures) 나머지만 실제로 검사한다. 원래 전체 대상 = 국소 실패 + 실제 요청 대상(누락 · 중복 없음). A 는 failed · flag 미기록."""
    from pipeline.handoff.translation import run_text_check, text_check_manifest

    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    sid = blk["section_id"]
    _clash(word, 60)
    db = app_db.SessionLocal()
    try:
        run = _run(ids["job"])
        rows = downstream._block_rows(db, sid)
        g, _rec = text_check.split_supply(GlossarySelectionSupply().glossary(db, run, sid, text_check.checkable(rows), origin="test"))
        req = text_check.build_request(db, run, sid, rows, g)  # 전체 대상(키 계산용)
        _, msha = text_check_manifest(req)
        rep = run_text_check({**req, "attempt_id": "t", "expected_input_manifest_sha256": msha})
        assert g["status"] == "ok" and rep.outcome == "failed" and rep.failure.kind == "input_invalid"  # A 를 넣으면 섹션 전체 실패
        reg = text_check.register(db, execution.lock_task(db, run["id"]), sid, origin="test")
        assert reg.status == text_check.REGISTERED
        m = db.execute(text("SELECT input_manifest FROM job_async_task WHERE id = :t"), {"t": reg.attempt_id}).scalar()
        all_ids = [r["id"] for r in text_check.checkable(rows)]
        run_ids = [t["block_id"] for t in m["targets"]]
        assert m["local_failures"] == [blk["id"]] and blk["id"] not in run_ids
        assert sorted(run_ids + m["local_failures"]) == sorted(all_ids)  # 누락 · 중복 없음
    finally:
        db.rollback()
        db.close()
    flags = _own_flags(blk["id"])
    r = _confirm(ids)  # A 는 현재 입력으로 failed(조회 판단), 나머지는 settled — 확정 검사 조건 통과
    assert r.status_code == 200, r.text
    assert _own_flags(blk["id"]) == flags  # A 실패를 정상 결과 · flag 로 만들지 않음


def test_conflict_before_translation_checks_sibling_and_only_conflict_block_fails(env, real_on):
    """A 의 강제 용어 충돌이 번역 채택 전부터 있어도: N4 에서 멈추지 않고, 같은 섹션 B 는 실제로 검사돼 settled, A 만 failed(inputKey = A 의
    실제 키 — 충돌이 그 블록 키에만 들어감). 실행 요청 대상 + 국소 실패 = 섹션 검사 대상 전체(누락 · 중복 없음)."""
    ids = _to_n3(env)
    _q("UPDATE job SET internal_category = 'G05' WHERE id = :j", j=ids["job"])
    blk, word = _word_block(ids)
    sid = blk["section_id"]
    _clash(word, 100)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    targets = _section_targets(sid)
    siblings = [b for b in targets if b != blk["id"]]
    assert siblings
    att = _cur_check(ids, sid)
    m = att["input_manifest"]
    run_ids = [t["block_id"] for t in m["targets"]]
    assert att["status"] == "done" and m["local_failures"] == [blk["id"]] and sorted(run_ids) == sorted(siblings)
    assert sorted(run_ids + m["local_failures"]) == sorted(targets) and not set(run_ids) & set(m["local_failures"])
    body = _body(env, att)
    assert [t["block_key"] for t in body["request"]["targets"]] == [f"blk_{b}" for b in siblings]  # 고정 요청 = 정상 블록만
    assert {b["block_key"] for b in body["request"]["blocks"]} >= {f"blk_{b}" for b in targets}  # 문맥은 섹션 전체
    st = _blocks(ids)
    assert st[blk["id"]]["check"] == {"state": "failed", "inputKey": m["input_keys"][f"blk_{blk['id']}"]}
    assert all(st[b]["check"] == {"state": "settled", "inputKey": m["input_keys"][f"blk_{b}"]} for b in siblings)
    assert _own_flags(blk["id"]) == []  # A 는 검사 결과로 채택하지 않음
    assert _confirm(ids).status_code == 200 and _step(ids) == ("review", "N6")


def test_conflict_in_a_keeps_settled_b_and_b_edit_is_rechecked(env, real_on):
    """N5 에서 B 가 settled 인 뒤 A 에 충돌이 생겨도 B 의 키 · 상태는 그대로(공급이 섹션 전체 failed 가 아니다). A 는 조회 때 현재 입력으로
    failed. A 오류 중에도 B 를 고치면 B 만 실제로 다시 검사된다(A 는 실행 요청에서 빠짐)."""
    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    sid = blk["section_id"]
    b_id = [b for b in _section_targets(sid) if b != blk["id"]][0]
    before = _blocks(ids)[b_id]["check"]
    assert before["state"] == "settled"
    _clash(word, 102)
    n = len(_checks(ids["job"]))
    st = _blocks(ids)
    assert st[blk["id"]]["check"]["state"] == "failed" and st[blk["id"]]["check"]["inputKey"] is not None
    assert st[b_id]["check"] == before  # B 키 · 상태 그대로
    assert _checks_pass(ids) and len(_checks(ids["job"])) == n
    b = st[b_id]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{b_id}", json={"trans1": "Sibling edited", "revision": b["revision"]}).status_code == 200
    att = _cur_check(ids, sid)
    assert att["status"] == "pending" and blk["id"] in att["input_manifest"]["local_failures"]
    assert b_id in {t["block_id"] for t in att["input_manifest"]["targets"]} and blk["id"] not in {t["block_id"] for t in att["input_manifest"]["targets"]}
    assert _blocks(ids)[b_id]["check"]["state"] == "in_progress"
    env["q"].drain()
    st = _blocks(ids)
    assert st[b_id]["check"]["state"] == "settled" and st[b_id]["check"]["inputKey"] != before["inputKey"]  # B 실제 재검사
    assert st[blk["id"]]["check"]["state"] == "failed"
    assert _confirm(ids).status_code == 200


def test_ok_conflict_recovers_after_data_fix_with_new_request_and_separate_confirm(env, real_on):
    """데이터 수정(충돌 행 비활성) 뒤 현재 공급을 다시 선별하면 A 의 오류가 풀리고 키가 바뀐다 — 오류 문자열이 같은지와 무관하게 다시 검사 대상."""
    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    other = _clash(word, 62)
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "failed" and _checks_pass(ids)  # 검사 오류로 확정을 막지 않음
    _q("UPDATE glossary SET retired_at = now() WHERE id = :i", i=other)  # 데이터 수정 — 현재 입력이 바뀐다
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated"  # 수정 전 입력의 실패를 현재 실패로 보지 않음
    r = _confirm(ids)
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]  # 새 요청 등록 — 이 요청은 409 로 끝남
    assert _step(ids) == ("review", "N5")
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"  # 정상 검사
    assert _confirm(ids).status_code == 200  # 별도 확정


def _match_conflict_on(monkeypatch, marker="ZZCONFLICT"):
    """대역 1곳: 번역문에 marker 가 있으면 AI check_block 이 MatchConflictError(금지/허용 같은 구간)를 낸다 — 실제 사전에서 같은 구간 충돌을
    만드는 행을 시험 묶음에 넣는 대신(실제 매칭 함수의 충돌은 7.8 · 7.18 real_reg_check 로 확인). run_text_check 의 처리 경로는 그대로."""
    from pipeline.matching import MatchConflictError
    from pipeline.stages import text_check as stc

    real = stc.check_block

    def check_block(block_key, revision, text_, source_ko, rows, terms):
        if marker in text_:
            raise MatchConflictError(f"블록 {block_key}: 대역 — 비허용과 allowed 가 같은 구간이다")
        return real(block_key, revision, text_, source_ko, rows, terms)

    monkeypatch.setattr(stc, "check_block", check_block)


def test_match_conflict_block_is_local_failure_sibling_checked_and_edit_reenters(env, real_on, monkeypatch):
    """금지 · 허용 같은 구간 충돌(MatchConflictError)도 블록 국소 실패: A 만 실행 요청에서 빠져 failed(inputKey = A 키), 같은 섹션 정상 블록은
    실제 검사. A 의 번역문을 고치면(충돌 없음) 키가 바뀌어 다시 실행 대상 → settled. 새 매칭 규칙 · 임의 해소 없음."""
    ids = _n5(env)
    blk, _w = _word_block(ids)
    sid = blk["section_id"]
    sib = [b for b in _section_targets(sid) if b != blk["id"]][0]
    flags0 = _own_flags(blk["id"])
    _match_conflict_on(monkeypatch)
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "has ZZCONFLICT span", "revision": b["revision"]}).status_code == 200
    att = _cur_check(ids, sid)
    m = att["input_manifest"]
    assert m["local_failures"] == [blk["id"]] and blk["id"] not in {t["block_id"] for t in m["targets"]} and sib in {t["block_id"] for t in m["targets"]}
    pend = _blocks(ids)
    assert pend[blk["id"]]["check"]["state"] == "failed" and pend[sib]["check"]["state"] == "settled"  # 실행 중에도 A 는 대기 대상 아님 · B 는 유효 결과 유지
    env["q"].drain()
    att = _cur_check(ids, sid)
    assert att["status"] == "done"  # 정상 블록 보고 전체 채택(부분 채택 아님)
    st = _blocks(ids)
    assert st[blk["id"]]["check"] == {"state": "failed", "inputKey": att["input_manifest"]["input_keys"][f"blk_{blk['id']}"]}
    assert st[sib]["check"]["state"] == "settled"
    assert _own_flags(blk["id"]) == flags0  # A 실패를 정상 결과로 만들지 않음
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "clean text now", "revision": b["revision"]}).status_code == 200
    att = _cur_check(ids, sid)
    assert att["input_manifest"]["local_failures"] == [] and blk["id"] in {t["block_id"] for t in att["input_manifest"]["targets"]}
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"
    assert _confirm(ids).status_code == 200


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


def _checks_pass(ids):
    """확정의 검사 조건만 본다(final._require_checks_settled — 통과하면 아무것도 쓰지 않고 돌아온다). N6 로 넘기지 않고 확인하려고 쓴다."""
    from app.flows import final

    db = app_db.SessionLocal()
    try:
        final._require_checks_settled(db, ids["job"])
        return True
    except final.ChecksNotSettled:
        return False
    finally:
        db.rollback()
        db.close()


def _states(ids):
    return {b: v["check"]["state"] for b, v in _blocks(ids).items() if v["check"] is not None}

# ── 실행 단계 공급 · 입력 · 데이터 오류(AI 11-10 추가 회신 · API 6-7): input_invalid · bundle_invalid 는 일반 최종 실패가 아니다 ──────
def _confirm(ids):
    return ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})


def _own_flags(bid):
    return [f for f in (_q("SELECT compliance_flags FROM text_block WHERE id = :b", b=bid)[0]["compliance_flags"] or [])
            if f.get("producer") in text_check.OWN_PRODUCERS]


def _cur_check(ids, sid):
    return _q("SELECT id, status, error_code, retry_count, input_manifest FROM job_async_task WHERE job_id = :j AND stage = 'text_check' "
              "AND unit_id = :s AND is_current", j=ids["job"], s=sid)[0]


def test_bypassed_fixed_request_runs_real_input_invalid_and_is_shown_failed(env, real_on, monkeypatch):
    """실제 AI 함수 재현: 등록 전 확인을 우회해 등록된 고정 요청을 확인을 켠 뒤 실제 run_text_check 로 실행 → 지문 있는 input_invalid.
    그 시도 대상 블록은 failed · 확정 검사 조건 통과 · 기존 정상 flag 보존 · 같은 입력은 재사용(새 시도 없음) · 데이터 수정 뒤 새 요청 → 정상 → 별도 확정."""
    ids = _n5(env, category="G05")
    blk, word = _word_block(ids)
    sid = blk["section_id"]
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"
    flags0 = _own_flags(blk["id"])
    _term(word, n=80)
    other = _term(word.upper() if word.upper() != word else word.lower(), cat="G05", n=81)
    with monkeypatch.context() as m:  # 우회 등록(조회도 이 동안은 우회) — 국소 오류 판단을 끄면 A 가 실행 요청에 들어간다
        m.setattr(text_check, "_glossary_local_failures", lambda targets, glossary, supplied: set())
        m.setattr(text_check, "_run_local_failures", lambda req, skip: set())
        r = _confirm(ids)
        assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]
        env["q"].drain()  # 실제 run_text_check
    att = _cur_check(ids, sid)
    assert att["status"] == "failed" and att["error_code"] == "TEXT_CHECK_INPUT_INVALID" and att["retry_count"] == 0
    h = _q("SELECT payload->'report'->>'input_manifest_sha256' AS s FROM task_handoff WHERE task_id = :t", t=att["id"])[0]
    assert h["s"] == att["input_manifest"]["request_manifest_sha256"]  # 같은 고정 입력에 대한 실패 보고(지문 있음)
    st = _blocks(ids)
    assert st[blk["id"]]["check"]["state"] == "failed"  # 고정 명세의 대상 중 현재 키 = 고정 키인 블록
    assert all(st[t["block_id"]]["check"]["state"] in ("failed", "settled") for t in att["input_manifest"]["targets"])  # 키가 그대로인 블록은 기존 정상 결과 유지
    assert _own_flags(blk["id"]) == flags0  # 오류 시도가 기존 정상 flag 를 덮어쓰지 않는다
    assert _checks_pass(ids)
    n = len(_checks(ids["job"]))
    db = app_db.SessionLocal()
    try:
        reg = text_check.register(db, execution.lock_task(db, _run(ids["job"])["id"]), sid, origin="test")
        assert reg.status in (text_check.REUSED, text_check.UNRESOLVED_REQUEST_INVALID) and reg.attempt_id in (None, att["id"])
    finally:
        db.rollback()
        db.close()
    assert len(_checks(ids["job"])) == n  # 같은 잘못된 입력을 다시 등록하지 않음
    _q("UPDATE glossary SET retired_at = now() WHERE id = :i", i=other)  # 데이터 수정
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "outdated"
    r = _confirm(ids)
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"] and _step(ids) == ("review", "N5")
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled" and _cur_check(ids, sid)["status"] == "done"
    assert _confirm(ids).status_code == 200


@pytest.mark.parametrize("kind,with_manifest", [("input_invalid", False), ("bundle_invalid", False), ("bundle_invalid", True)])
def test_stand_in_input_and_bundle_errors_are_failed_and_recover_on_edit(env, real_on, monkeypatch, kind, with_manifest):
    """대역(AI 보고 생성 함수 failed_report): 지문 없는 input_invalid · bundle_invalid, 지문 있는 bundle_invalid(금지/허용 같은 구간 충돌 모양)
    → 그 블록 failed · 재시도 0 · flag 그대로 · 같은 입력 새 시도 없음 · 확정 검사 조건 통과 · 번역문 수정(현재 입력 변경) 뒤 정상 검사."""
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
    assert _blocks(ids)[blk["id"]]["check"] == {"state": "failed", "inputKey": att["input_manifest"]["input_keys"][f"blk_{blk['id']}"]}
    assert _own_flags(blk["id"]) == flags0
    n = len(_checks(ids["job"]))
    assert _checks_pass(ids) and _checks_pass(ids)  # 반복해도 검사 조건 통과 · 등록 없음
    assert len(_checks(ids["job"])) == n and _step(ids) == ("review", "N5")
    state["fail"] = False  # (원인 해소) + 입력 수정 — 현재 입력이 바뀌어 새 요청이 등록된다
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


def test_bundle_conversion_failure_at_execution_is_failed_without_repeat(env, real_on, monkeypatch):
    """규칙 검사 단계의 AI_ADAPTER_UNAVAILABLE(실행 때 고정 사전 묶음 변환 실패 — 대역: to_ai_bundle 이 BundleError, 등록 뒤 실행 때만).
    시도 failed · 재시도 0 → 그 블록 failed · flag 보존 · 같은 입력 새 시도 없음 · 확정 통과. 변환 실패가 이어지는 동안은 현재 키 계산도
    같은 변환을 써서 키를 만들 수 없다 → 그 섹션은 failed · inputKey null(U1, API 6-10 — 목록 200)."""
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
        m.setattr(dictionary_bundle, "to_ai_bundle", boom)
        env["q"].drain()
        r = c2.get(f"/v1/jobs/{ids['job']}/blocks")
        assert r.status_code == 200 and {b["id"]: b["check"] for b in r.json()}[blk["id"]] == {"state": "failed", "inputKey": None}
    att = _cur_check(ids, sid)
    assert att["status"] == "failed" and att["error_code"] == "AI_ADAPTER_UNAVAILABLE" and att["retry_count"] == 0
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "failed" and _own_flags(blk["id"]) == flags0
    n = len(_checks(ids["job"]))
    assert _checks_pass(ids) and len(_checks(ids["job"])) == n
    assert _confirm(ids).status_code == 200

def test_check_errors_do_not_bypass_other_confirm_conditions_and_keep_other_sections(env, real_on, monkeypatch):
    """PM 11-10: 검사 오류 섹션만 failed. 다른 섹션의 결과 · 조회 · 편집은 그대로, 확정의 다른 조건(#120 확인 집합)은 그대로 적용.
    배치만 바꾼 저장은 검사를 등록하지 않는다. 대역: 한 섹션의 실행 보고만 input_invalid, 번역 1블록 실패(FakeTranslator)."""
    from app import ai_adapters
    from pipeline.handoff import envelope, translation
    from tests.exec_fakes import FakeTranslator

    ids = _to_n3(env)
    rows = _q("SELECT tb.id, tb.source_ko, tb.section_id FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j "
              "AND s.bucket = 'include' ORDER BY tb.id", j=ids["job"])
    by_sec: dict[int, list] = {}
    for r in rows:
        by_sec.setdefault(r["section_id"], []).append(r)
    secs = sorted(by_sec)
    assert len(secs) >= 2
    bad_sec, ok_sec = secs[0], secs[1]
    tf_block = by_sec[ok_sec][-1]["id"]  # 번역 실패(필수 확인 대상) — 정상 섹션 쪽
    ai_adapters.set_adapters(translator=FakeTranslator(fail_keys={f"blk_{tf_block}"}))
    real = translation.run_text_check

    def run(raw, cfg=None):
        good = real(raw, cfg)
        if raw.get("section_key") == f"sec_{bad_sec}":
            return envelope.failed_report(envelope.identity_fallback(raw, "text_check"), "input_invalid", "대역 오류", retryable=False,
                                          target_count=good.target_count)
        return good

    monkeypatch.setattr(translation, "run_text_check", run)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    st = _blocks(ids)
    bad = [b for b, v in st.items() if v["check"] is not None and v["sectionId"] == bad_sec]
    good = [b for b, v in st.items() if v["check"] is not None and v["sectionId"] == ok_sec]
    assert bad and all(st[b]["check"]["state"] == "failed" for b in bad)
    assert good and all(st[b]["check"]["state"] == "settled" for b in good)  # 다른 섹션 결과 보존
    # 다른 섹션 편집 — 저장 200 · 현재 입력으로 새 검사 등록
    g = st[good[0]]
    n = len(_checks(ids["job"]))
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{g['id']}", json={"trans1": "Edited other section", "revision": g["revision"]}).status_code == 200
    assert len(_checks(ids["job"])) == n + 1
    env["q"].drain()
    assert _blocks(ids)[good[0]]["check"]["state"] == "settled"
    # 실패 섹션 블록의 배치만 바꾼 저장 · 같은 값 저장은 검사를 등록하지 않는다
    f = _blocks(ids)[bad[0]]
    n = len(_checks(ids["job"]))
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{f['id']}", json={"trans1": f["trans1"], "revision": f["revision"]}).status_code == 200
    assert len(_checks(ids["job"])) == n and _blocks(ids)[bad[0]]["check"]["state"] == "failed"
    # 확정: 검사 조건은 통과하지만 #120 확인 집합은 그대로 — 확인 없으면 409 acknowledgement_mismatch, 서버 집합을 보내면 200
    r = _confirm(ids)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "acknowledgement_mismatch"
    warnings = r.json()["error"]["details"]["warnings"]
    assert {"blockId": tf_block, "code": "translation_failed"} in warnings
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": warnings}).status_code == 200
    assert _step(ids) == ("review", "N6")


def test_stale_failure_of_previous_input_is_not_current_failure(env, real_on, monkeypatch):
    """과거 입력의 실패를 현재 입력의 실패로 보지 않는다 — 실패 뒤 번역문을 바꾸고 새 검사가 진행 중이면 in_progress(대기 조건 유지)."""
    from pipeline.handoff import envelope, translation

    real = translation.run_text_check
    state = {"fail": True}

    def run(raw, cfg=None):
        good = real(raw, cfg)
        if state["fail"]:
            return envelope.failed_report(envelope.identity_fallback(raw, "text_check"), "bundle_invalid", "대역", retryable=False,
                                          target_count=good.target_count)
        return good

    ids = _n5(env)
    blk, _w = _word_block(ids)
    monkeypatch.setattr(translation, "run_text_check", run)
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited stale 1", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "failed"
    state["fail"] = False
    b = _blocks(ids)[blk["id"]]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Edited stale 2", "revision": b["revision"]}).status_code == 200
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "in_progress"  # 새 입력의 검사 대기 — 과거 실패로 통과시키지 않음
    r = _confirm(ids)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "check_not_settled"
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled" and _confirm(ids).status_code == 200


# ── 공용 도우미: 같은 블록 원문 단어로 정규화가 같은 서로 다른 강제 용어 두 개(블록 국소 충돌) · 섹션 검사 대상 ─────────────────────
def _clash(word, n):
    _term(word, n=n)
    return _term(word.upper() if word.upper() != word else word.lower(), cat="G05", n=n + 1)


def _section_targets(sid):
    db = app_db.SessionLocal()
    try:
        return [r["id"] for r in text_check.checkable(downstream._block_rows(db, sid))]
    finally:
        db.close()


def _tc_rows(job):
    return _q("SELECT id, unit_id, status, error_code, input_manifest FROM job_async_task WHERE job_id = :j AND stage = 'text_check' ORDER BY id",
              j=job)


def _sweep_n(env, n=3):
    """recovery 를 반복한다(진행 계산 · 채택 재전달 · 대기 재전달) — 반복 호출이 시도 · 저장소 호출을 늘리지 않는지 본다."""
    from app import recovery

    for _ in range(n):
        recovery.sweep()
        env["q"].drain()


# ── N4 종료 · 번역 저장 분리(PM 11-10 — 검사 부가 오류로 작업을 가두지 않는다) ───────────────────────────────────
def test_u2_request_store_failure_at_translate_records_failed_and_reaches_n5(env, real_on, monkeypatch):
    """U2: 번역 채택 때 검사 요청 객체를 저장하지 못함(장애 지속). 번역 저장 유지 · 실행할 시도 대신 그 입력의 실패 기록 → N4 대기 종료 →
    N5 에서 failed(inputKey = 실제 현재 키) · 확정 통과. 진행 계산 · recovery 반복과 저장소 회복 뒤에도 시도 · 저장소 호출이 늘지 않는다
    (PM 이 요구하지 않은 자동 재등록 없음). errorCode = CHECK_REQUEST_STORE_FAILED(API 6-10)."""
    ids = _to_n3(env)
    store = env["store"]
    real = store.put_if_absent
    fault = {"on": True, "puts": 0}

    def put(key, data, *a, **kw):
        if "/text_check/" in key:
            fault["puts"] += 1
            if fault["on"]:
                raise OSError("주입: 저장소 쓰기 장애")
        return real(key, data, *a, **kw)

    monkeypatch.setattr(store, "put_if_absent", put)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    rows = _tc_rows(ids["job"])
    assert rows and all(r["status"] == "failed" and r["error_code"] == "CHECK_REQUEST_STORE_FAILED" and "request_ref" not in r["input_manifest"]
                        and r["input_manifest"]["registration_failure"]["stage"] == "request_store" for r in rows)
    st = _blocks(ids)
    checked = [b for b, v in st.items() if v["check"] is not None]
    keys = {k: v for r in rows for k, v in r["input_manifest"]["input_keys"].items()}
    assert checked and all(st[b]["check"] == {"state": "failed", "inputKey": keys[f"blk_{b}"]} for b in checked)
    assert all(_own_flags(b) == [] for b in checked)  # 검사 결과 · flag 없음
    n, puts = len(rows), fault["puts"]
    _sweep_n(env)
    fault["on"] = False  # 저장소 회복
    _sweep_n(env)
    assert (len(_tc_rows(ids["job"])), fault["puts"]) == (n, puts)
    assert _confirm(ids).status_code == 200 and _step(ids) == ("review", "N6")


def _keys_fault(monkeypatch, sid):
    real = AiInputKeys.block_keys

    def keys(self, request):
        if request.get("section_key") == f"sec_{sid}":
            raise text_check.InputKeysUnavailable("주입: 키 계산 불가")
        return real(self, request)

    monkeypatch.setattr(AiInputKeys, "block_keys", keys)


def test_u1_keys_unavailable_at_translate_keeps_translation_and_reaches_n5(env, real_on, monkeypatch):
    """U1: 번역 채택 때 그 섹션 현재 inputKey 를 계산할 수 없음(장애 지속). 번역 저장 유지 · 기록 없음(키가 없어 입력과 대조할 수 없다)
    · N4 대기는 현재 입력으로 판단해 끝난다. 반복 진행 계산 · recovery 에도 시도가 생기지 않는다. N5 표시 · 확정은 아래 시험(failed/null)."""
    ids = _to_n3(env)
    blk, _w = _word_block(ids)
    sid = blk["section_id"]
    _keys_fault(monkeypatch, sid)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _q("SELECT count(trans_1) AS t FROM text_block WHERE id = :b", b=blk["id"])[0]["t"] == 1
    assert _step(ids) == ("review", "N5")
    assert not [r for r in _tc_rows(ids["job"]) if r["unit_id"] == sid]
    n = len(_tc_rows(ids["job"]))
    _sweep_n(env)
    assert len(_tc_rows(ids["job"])) == n and _step(ids) == ("review", "N5")


def test_u1_n5_blocks_failed_null_and_confirm_passes(env, real_on, monkeypatch):
    """U1(API 6-10): 키 계산 불가가 이어지는 동안 N5 블록 목록은 200, 그 섹션 대상 블록은 failed · inputKey null(시도 행 없음 · 폴링할
    검사 작업 없음), 다른 섹션은 settled, 확정 통과."""
    ids = _to_n3(env)
    blk, _w = _word_block(ids)
    sid = blk["section_id"]
    _keys_fault(monkeypatch, sid)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    st = _blocks(ids)
    assert all(st[b]["check"] == {"state": "failed", "inputKey": None} for b in _section_targets(sid))
    others = [b for b, v in st.items() if v["check"] is not None and v["sectionId"] != sid]
    assert others and all(st[b]["check"]["state"] == "settled" for b in others)
    assert not [r for r in _tc_rows(ids["job"]) if r["unit_id"] == sid]  # 대응하는 검사 작업 없음
    assert _confirm(ids).status_code == 200 and _step(ids) == ("review", "N6")


def test_u1_block_save_200_failed_null_no_null_reuse_then_recovers(env, real_on, monkeypatch):
    """U1 이 N5 에서 생김: 번역문 저장은 200 + block.check = failed/null(시도 행 없음). 다른 입력으로 다시 저장해도 null 끼리 같다고 재사용하거나
    기록하지 않고 매번 현재 입력으로 판정한다. 다른 섹션의 실제 검사 등록 · 대기(409)는 그대로. 키 계산이 회복되면 일반 등록 → 정상 검사 → 확정."""
    ids = _n5(env)
    blk, _w = _word_block(ids)
    sid = blk["section_id"]
    other = [b for b, v in _blocks(ids).items() if v["check"] is not None and v["sectionId"] != sid][0]
    real = AiInputKeys.block_keys
    fault = {"on": True}

    def keys(self, request):
        if fault["on"] and request.get("section_key") == f"sec_{sid}":
            raise text_check.InputKeysUnavailable("주입: 키 계산 불가")
        return real(self, request)

    monkeypatch.setattr(AiInputKeys, "block_keys", keys)
    n = len(_tc_rows(ids["job"]))
    for txt in ("U1 edit one", "U1 edit two"):  # 서로 다른 입력 — 둘 다 null
        b = _blocks(ids)[blk["id"]]
        r = ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": txt, "revision": b["revision"]})
        assert r.status_code == 200, r.text
        assert r.json()["block"]["trans1"] == txt and r.json()["block"]["check"] == {"state": "failed", "inputKey": None}
        assert _q("SELECT trans_1 FROM text_block WHERE id = :b", b=blk["id"])[0]["trans_1"] == txt  # 저장 커밋됨
        assert len(_tc_rows(ids["job"])) == n  # 시도 · 기록 없음
    o = _blocks(ids)[other]
    assert ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{other}", json={"trans1": "Other edit", "revision": o["revision"]}).status_code == 200
    assert _blocks(ids)[other]["check"]["state"] == "in_progress"  # 다른 섹션의 실제 검사 등록
    r = _confirm(ids)
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "check_not_settled"  # 진행 중 검사 대기는 그대로
    env["q"].drain()
    fault["on"] = False  # 키 계산 회복
    assert _blocks(ids)[blk["id"]]["check"]["state"] in ("missing", "outdated")  # 현재 입력으로 다시 판정 — 실패를 이어 쓰지 않음
    r = _confirm(ids)
    assert r.status_code == 409 and blk["id"] in r.json()["error"]["details"]["registeredBlockIds"]  # 일반 등록
    env["q"].drain()
    assert _blocks(ids)[blk["id"]]["check"]["state"] == "settled"
    assert _confirm(ids).status_code == 200


def test_db_error_during_block_save_check_state_rolls_back_save(env, real_on, monkeypatch):
    """검사 상태 계산 중 실제 DB 오류는 failed/null 로 삼키지 않는다 — 블록 저장 전체가 롤백된다(번역문 · revision 그대로)."""
    from sqlalchemy.exc import OperationalError

    ids = _n5(env)
    blk, _w = _word_block(ids)
    b = _blocks(ids)[blk["id"]]

    def bundle(db, run):
        raise OperationalError("SELECT bundle", {}, Exception("주입: DB 장애"))

    monkeypatch.setattr(text_check, "_bundle", bundle)
    with pytest.raises(OperationalError):  # TestClient 는 서버 예외를 그대로 올린다(실서버 500)
        ids["c"].patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Should roll back", "revision": b["revision"]})
    row = _q("SELECT trans_1, revision FROM text_block WHERE id = :b", b=blk["id"])[0]
    assert (row["trans_1"], row["revision"]) == (b["trans1"], b["revision"])
    # 등록을 거치지 않는 조회(상태 판정만)에서도 DB 오류는 failed/null 로 바뀌지 않고 그대로 올라간다(U1 은 키 계산 불가에 한정)
    with pytest.raises(OperationalError):
        _blocks(ids)


def test_check_registration_exception_does_not_roll_back_translation(env, real_on, monkeypatch):
    """N1: 등록 경로의 검사 부가 예외(요청 조립 — 사전 묶음 변환 실패)는 번역 채택(저장)을 되돌리지 않는다. 그 섹션은 등록되지 않고
    (기록 없음 — 키를 만들 수 없다) N4 대기는 현재 입력으로 판단해 끝난다."""
    from app import ai_adapters

    ids = _to_n3(env)
    blk, _w = _word_block(ids)
    sid = blk["section_id"]
    real = text_check.assemble

    def assemble(run_id, parts, glossary, bundle, *, attempt_id="pending"):
        if parts.get("section_key") == f"sec_{sid}":
            raise ai_adapters.AdapterUnavailable("주입: 규칙 검사를 시작할 수 없다(묶음 변환)")
        return real(run_id, parts, glossary, bundle, attempt_id=attempt_id)

    monkeypatch.setattr(text_check, "assemble", assemble)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    tr = _q("SELECT status FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current", j=ids["job"], s=sid)
    assert tr and tr[0]["status"] == "done"
    assert _q("SELECT count(trans_1) AS t FROM text_block WHERE id = :b", b=blk["id"])[0]["t"] == 1
    assert not [r for r in _tc_rows(ids["job"]) if r["unit_id"] == sid] and _step(ids) == ("review", "N5")


def test_db_error_in_check_registration_still_rolls_back_translation_adoption(env, real_on, monkeypatch):
    """N1 은 좁다: 등록 경로의 DB 오류(요청 조립 중 분석 묶음 조회)는 삼키지 않는다 — 번역 채택 전체를 기존대로 롤백(인계 유지 · 저장 없음)."""
    from sqlalchemy.exc import OperationalError

    ids = _to_n3(env)
    blk, _w = _word_block(ids)
    sid = blk["section_id"]
    real = text_check._bundle
    fault = {"on": True}

    def bundle(db, run):
        if fault["on"]:
            raise OperationalError("SELECT bundle", {}, Exception("주입: DB 장애"))
        return real(db, run)

    monkeypatch.setattr(text_check, "_bundle", bundle)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    try:
        env["q"].drain()
    except Exception:  # noqa: BLE001 — 채택 예외가 대역 큐 밖으로 나온다(기존 동작)
        pass
    tr = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current",
            j=ids["job"], s=sid)
    assert tr and tr[0]["status"] != "done"  # 채택되지 않음 — 기존 일반 예외 경로(번역 시도 실패 처리)
    assert tr[0]["status"] == "running" or tr[0]["error_code"] == "TRANSLATE_UNEXPECTED"
    assert _q("SELECT count(trans_1) AS t FROM text_block WHERE id = :b", b=blk["id"])[0]["t"] == 0  # 번역 저장 롤백
    assert not _q("SELECT 1 FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j AND a.stage = 'translate' "
                  "AND a.unit_id = :s AND h.state = 'adopted'", j=ids["job"], s=sid)
    assert _step(ids)[1] == "N4"  # N5 로 가지 않는다 — 실제 저장 실패의 기존 처리(번역 실패 · 재시도 소진이면 작업 failed)


def test_pending_check_still_holds_n4_until_it_finishes(env, real_on):
    """정상 등록된 검사가 대기 중이면 N4 에 머문다(건너뛰지 않음) — 실행 · 채택 뒤 N5."""
    ids = _to_n3(env)
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain(skip={TC})
    pending = [r for r in _tc_rows(ids["job"]) if r["status"] == "pending"]
    assert pending and _step(ids) == ("processing", "N4")
    for r in pending:
        text_check.execute_text_check(r["id"])
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
