"""#120(확인 기준 taskId) × 규칙 검사 연결의 confirm() 통합 — 실제 PostgreSQL + 메모리 저장소 + 큐 대역(격리 통합 11-10).

공급 · inputKey 는 시험 대역(FakeSupply · FakeKeys)이고 번역은 FakeTranslator 다. 실제 A1 · A3 연결 결과가 아니다.
여기서 보는 것은 두 변경이 같은 confirm() 에서 만날 때의 흐름이다:
- 보조 검사 등록은 커밋 · 큐 전달 뒤 그 확정 요청을 409 check_not_settled 로 거절한다(같은 요청에서 N6 으로 가지 않는다).
- 다음 확정 요청은 잠금 아래 작업 · 검사 상태 · 확인 집합 · taskId 를 처음부터 다시 본다.
- 큐 전달 실패 · 중복 등록 · 취소 · 입력 변경 · 배치만 저장 · no-op · 멱등 재전송.
- 오류 사이의 우선순위(검사 조건 ↔ 확인 집합 ↔ taskId)는 **미확정**이다. 현재 구현 순서를 기록할 뿐 계약으로 단정하지 않는다.
- I8: 확정 성공 때 최종 렌더 실행 · N6 전환이 1회. 감사 기록은 미구현이라 여기서 세지 않는다(후속 I8-감사).
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app import ai_adapters, execution, recovery
from tests.exec_db import requires_db
from tests.exec_fakes import FakeTranslator
from tests.test_exec_downstream import _patch as _patch_raw
from tests.test_exec_downstream import _q, _step, _to_n3, dburl, env  # noqa: F401  (픽스처)
from tests.test_text_check_connect import TC, rule_on  # noqa: F401  (픽스처)

pytestmark = requires_db

TF = "translation_failed"


# ── 준비 ────────────────────────────────────────────────────────────────
def _failed_n5_checked(env):
    """규칙 검사가 켜진 채 번역 일부 실패로 N5 에 온 작업(검사는 모두 끝남). 반환: dict."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id", j=job)
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
    tr = FakeTranslator(fail_keys={f"blk_{target}"})
    ai_adapters.set_adapters(translator=tr)
    c.post(f"/v1/jobs/{job}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    att = _cur_failed_translate(job)
    blocks = _blocks(c, job)
    assert blocks[target]["check"] is None  # 번역 실패 칸은 검사 대상이 아니다
    assert all(b["check"]["state"] == "settled" for b in blocks.values() if b["check"] is not None)
    return {"ids": ids, "c": c, "job": job, "target": target, "att": att, "tr": tr}


def _cur_failed_translate(job):
    att = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current AND status = 'failed'", j=job)
    assert len(att) == 1
    return att[0]["id"]


def _blocks(c, job):
    return {b["id"]: b for b in c.get(f"/v1/jobs/{job}/blocks").json()}


def _make_outdated(s, *, skip_sections=(), ext="GL-C995"):
    """번역 실패 블록이 아닌 검사 대상 블록 원문의 단어를 강제 용어로 넣어 그 블록의 현재 입력(키)을 바꾼다(등록 경로 밖 변경)."""
    from app import db as app_db

    for b in sorted(_blocks(s["c"], s["job"]).values(), key=lambda x: x["id"]):
        if b["id"] == s["target"] or b["check"] is None or b["sectionId"] in skip_sections:
            continue
        src = _q("SELECT source_ko FROM text_block WHERE id = :b", b=b["id"])[0]["source_ko"] or ""
        m = re.search(r"[A-Za-z가-힣]{5,}", src)
        if m:
            with app_db.engine.begin() as cn:
                cn.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, "
                                "term_kind, version) VALUES (:k, 'ZZTARGET', 'en', 'common', 'enforced', :e, 'certification', 1)"),
                           {"k": m.group(0), "e": ext})
            assert _blocks(s["c"], s["job"])[b["id"]]["check"]["state"] == "outdated"
            return b["id"]
    pytest.fail("단어가 있는 검사 대상 블록 없음")


def _checks(job):
    sql = "SELECT id, unit_id, status, is_current, retry_origin, last_dispatched_at, dispatch_count FROM job_async_task WHERE job_id = :j AND stage = 'text_check'"
    return _q(sql + " ORDER BY id", j=job)


def _confirm(s, acks):
    return s["c"].post(f"/v1/jobs/{s['job']}/confirm", json={"acknowledgedWarnings": acks})


def _ack(s, task_id="cur"):
    a = {"blockId": s["target"], "code": TF}
    if task_id == "cur":
        a["taskId"] = s["att"]
    elif task_id is not None:
        a["taskId"] = task_id
    return [a]


def _err(r):
    """409 INVALID_STATE 와 공통 Error 형식(I10: code · message · retryable · traceId · details)."""
    assert r.status_code == 409, r.text
    e = r.json()["error"]
    assert e["code"] == "INVALID_STATE" and isinstance(e["message"], str) and e["message"]
    assert e["retryable"] is False and e["traceId"]
    return e["details"]


def _n5_no_final(job):
    assert _q("SELECT status, current_step FROM job WHERE id = :j", j=job)[0] == {"status": "review", "current_step": "N5"}
    assert _final_runs(job) == 0


def _final_runs(job):
    return _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND run_kind = 'final_render'", j=job)[0]["n"]


def _final_attempts(job):
    return _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'final_render'", j=job)[0]["n"]


def _queued(env, ids):
    return [args[0] for name, args in env["q"].messages if name == TC and args and args[0] in ids]


# ── 보조 등록: 커밋 · 큐 전달 뒤 그 요청은 거절, 다음 요청에서 전부 다시 확인 ──────────────
def test_registration_commits_dispatches_rejects_then_next_confirm_runs_final_once(env, rule_on):
    s = _failed_n5_checked(env)
    blk = _make_outdated(s)
    before = {c["id"] for c in _checks(s["job"])}
    d = _err(_confirm(s, _ack(s)))  # taskId 는 현재 실패 시도 — 확인 자체는 맞다
    assert d["reason"] == "check_not_settled" and blk in d["registeredBlockIds"]
    assert {"blockId": blk, "state": "outdated"} in d["blocks"]
    new = [c for c in _checks(s["job"]) if c["id"] not in before]
    assert len(new) == 1 and new[0]["status"] == "pending" and new[0]["is_current"] and new[0]["retry_origin"] == "user"  # 확정 경로 등록은 기존 대체 시도와 같은 retry_origin(user)
    assert new[0]["dispatch_count"] == 1 and _queued(env, {new[0]["id"]}) == [new[0]["id"]]  # 커밋 뒤 큐 전달
    _n5_no_final(s["job"])  # 같은 요청에서 N6 확정 처리를 계속하지 않는다
    assert _cur_failed_translate(s["job"]) == s["att"]  # 경고 원천(번역 실패 시도)은 그대로
    # 검사가 끝나기 전 다시 확정 — 진행 중 블록은 새로 등록하지 않는다(중복 등록 없음)
    d2 = _err(_confirm(s, _ack(s)))
    assert d2["reason"] == "check_not_settled" and d2["registeredBlockIds"] == [] and {"blockId": blk, "state": "in_progress"} in d2["blocks"]
    assert len(_checks(s["job"])) == len(before) + 1
    env["q"].drain()
    assert _blocks(s["c"], s["job"])[blk]["check"]["state"] == "settled"
    r = _confirm(s, _ack(s))  # 다음 요청 — 잠금 아래 검사 · 집합 · taskId 를 다시 본 뒤 통과
    assert r.status_code == 200, r.text
    assert r.json()["currentStep"] == "N6"
    # I8: 최종 렌더 실행 · 시도 1, N6 전환 1회. 다시 확정해도 늘지 않는다
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)
    again = s["c"].post(f"/v1/jobs/{s['job']}/confirm", json={"acknowledgedWarnings": _ack(s)})
    assert again.status_code == 409 and (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)
    assert _q("SELECT current_step FROM job WHERE id = :j", j=s["job"])[0]["current_step"] == "N6"


def test_dispatch_failure_after_registration_keeps_one_pending_and_recovery_resends_same_attempt(env, rule_on):
    s = _failed_n5_checked(env)
    blk = _make_outdated(s)
    n0 = len(_checks(s["job"]))
    env["q"].fail_send = True  # 브로커 장애 — 등록은 커밋됐고 전송만 실패
    d = _err(_confirm(s, _ack(s)))
    assert d["reason"] == "check_not_settled" and blk in d["registeredBlockIds"]  # 500 이 아니라 같은 거절
    new = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"]
    assert len(new) == 1 and new[0]["last_dispatched_at"] is None and new[0]["dispatch_count"] == 0
    d2 = _err(_confirm(s, _ack(s)))  # 전송 실패 뒤 재요청 — 같은 시도가 pending 이라 다시 등록하지 않는다
    assert d2["registeredBlockIds"] == [] and len(_checks(s["job"])) == n0 + 1
    _n5_no_final(s["job"])
    env["q"].fail_send = False
    st = recovery.sweep()
    assert st["redispatched"] >= 1
    row = [c for c in _checks(s["job"]) if c["id"] == new[0]["id"]][0]
    assert row["dispatch_count"] == 1 and row["status"] == "pending"  # 같은 시도 id 로 재전달
    env["q"].drain()
    assert len(_checks(s["job"])) == n0 + 1
    r = _confirm(s, _ack(s))
    assert r.status_code == 200, r.text
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)


# ── 현재 구현의 거절 순서(우선순위 미확정 — 기록만) ─────────────────────────────────
def test_current_order_check_condition_before_ack_set_and_task_id(env, rule_on):
    s = _failed_n5_checked(env)
    blk = _make_outdated(s)
    # 낡은 taskId + 검사 미종료 → 현재 구현은 검사 조건을 먼저 본다(등록 · 커밋 후 check_not_settled)
    d = _err(_confirm(s, _ack(s, s["att"] + 1)))
    assert d["reason"] == "check_not_settled" and blk in d["registeredBlockIds"]
    env["q"].drain()
    n = len(_checks(s["job"]))
    # 검사가 끝난 뒤: 집합이 다르면 mismatch, 집합이 같고 taskId 가 다르면 outdated — 어느 쪽도 검사를 다시 등록하지 않는다
    d = _err(_confirm(s, []))
    assert d == {"reason": "acknowledgement_mismatch", "warnings": [{"blockId": s["target"], "code": TF}]}
    d = _err(_confirm(s, _ack(s, s["att"] + 1)))
    assert d == {"reason": "acknowledgement_outdated", "warnings": [{"blockId": s["target"], "code": TF}]}
    assert len(_checks(s["job"])) == n
    _n5_no_final(s["job"])
    assert _confirm(s, _ack(s)).status_code == 200


def test_missing_ack_while_check_not_settled_still_registers(env, rule_on):
    s = _failed_n5_checked(env)
    blk = _make_outdated(s)
    d = _err(_confirm(s, []))  # 필수 확인 누락 + 검사 미종료 — 현재 구현은 검사 조건이 먼저(등록은 커밋)
    assert d["reason"] == "check_not_settled" and blk in d["registeredBlockIds"]
    env["q"].drain()
    assert _err(_confirm(s, []))["reason"] == "acknowledgement_mismatch"
    assert _confirm(s, _ack(s, None)).status_code == 200  # taskId 생략 — 기존 집합 판정


def test_translate_retry_between_requests_changes_task_id_basis(env, rule_on):
    """등록으로 거절된 뒤 다음 요청 전에 번역을 재시도해 다시 실패 — 다음 요청은 새 taskId 를 기준으로 본다."""
    s = _failed_n5_checked(env)
    _make_outdated(s)
    _err(_confirm(s, _ack(s)))
    env["q"].drain()
    r = s["c"].post(f"/v1/jobs/{s['job']}/tasks/{s['att']}/retry")
    assert r.status_code == 200, r.text
    env["q"].drain()
    new = _cur_failed_translate(s["job"])
    assert new != s["att"]
    assert _err(_confirm(s, _ack(s)))["reason"] == "acknowledgement_outdated"  # 과거 실패 시도에 대한 확인
    _n5_no_final(s["job"])
    r = _confirm(s, _ack(s, new))
    assert r.status_code == 200, r.text
    assert _final_runs(s["job"]) == 1


# ── 배치만 · no-op · 멱등 재전송은 검사를 등록하지 않는다(확인 기준도 유지) ──────────────────
def test_layout_only_noop_and_idempotent_resend_do_not_register_and_ack_stays_valid(env, rule_on):
    s = _failed_n5_checked(env)
    c, job = s["c"], s["job"]
    sec = _blocks(c, job)[s["target"]]["sectionId"]
    other = [b for b in _blocks(c, job).values() if b["check"] is not None and b["id"] != s["target"]]
    same_sec = [b for b in other if b["sectionId"] == sec] or other
    b = same_sec[0]
    n = len(_checks(job))
    r = _patch_raw(c, job, b["id"], {"trans1": b["trans1"], "revision": b["revision"], "layoutAdjust": {"fontScale": 1.25}})  # 배치만
    assert r.status_code == 200 and r.json()["block"]["revision"] == b["revision"] + 1, r.text
    b = _blocks(c, job)[b["id"]]
    assert _patch_raw(c, job, b["id"], {"trans1": b["trans1"], "revision": b["revision"]}).status_code == 200  # 같은 값(no-op)
    assert len(_checks(job)) == n and _blocks(c, job)[b["id"]]["check"]["state"] == "settled"
    # 번역문 변경 + 같은 Idempotency-Key 재전송 — 등록 1건
    b = _blocks(c, job)[b["id"]]
    body = {"trans1": "Edited once", "revision": b["revision"]}
    r1 = _patch_raw(c, job, b["id"], body, key="int-idem-1")
    r2 = _patch_raw(c, job, b["id"], body, key="int-idem-1")
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert len(_checks(job)) == n + 1
    env["q"].drain()
    assert len(_checks(job)) == n + 1
    assert _cur_failed_translate(job) == s["att"]
    r = _confirm(s, _ack(s))  # revision 이 올라도 같은 실패 시도에 대한 확인은 유효
    assert r.status_code == 200, r.text


# ── 취소 · 입력 변경 뒤 오래된 실행 결과 채택 차단(확정이 등록한 검사) ─────────────────────────
def test_input_change_after_confirm_registration_supersedes_and_old_result_not_adopted(env, rule_on):
    from app.execution import Envelope
    from app.flows.common import submit

    s = _failed_n5_checked(env)
    blk = _make_outdated(s)
    _err(_confirm(s, _ack(s)))
    reg = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"][0]
    env["q"].messages.clear()  # 큐 대역의 메시지를 비우고 직접 권한을 잡는다(늦게 도는 검사)
    lease = execution.acquire(reg["id"], "slow-checker")
    b = _blocks(s["c"], s["job"])[blk]
    _patch_raw(s["c"], s["job"], blk, {"trans1": "Changed during check", "revision": b["revision"]})  # 입력 변경 — 새 시도가 대체
    assert execution.heartbeat(lease) is False
    flags = _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=blk)[0]["compliance_flags"]
    env_ = Envelope(outcome="done", target_count=1, input_fingerprint=lease.fingerprint, payload={"text_check": {"blocks": []}})
    with pytest.raises(execution.LeaseLost):
        submit(lease, env_)
    assert _q("SELECT compliance_flags FROM text_block WHERE id = :b", b=blk)[0]["compliance_flags"] == flags
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=reg["id"])[0]["status"] == "cancelled"
    env["q"].drain()
    assert _confirm(s, _ack(s)).status_code == 200


def test_cancel_after_confirm_registration_blocks_late_result(env, rule_on):
    from app.execution import Envelope
    from app.flows.common import submit

    s = _failed_n5_checked(env)
    _make_outdated(s)
    _err(_confirm(s, _ack(s)))
    reg = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"][0]
    env["q"].messages.clear()
    lease = execution.acquire(reg["id"], "late-checker")
    r = s["c"].delete(f"/v1/jobs/{s['job']}")
    assert r.status_code == 200, r.text
    env_ = Envelope(outcome="done", target_count=0, input_fingerprint=lease.fingerprint, payload={"text_check": {"blocks": []}})
    with pytest.raises((execution.HandoffRejected, execution.LeaseLost)):
        submit(lease, env_)
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t", t=reg["id"])[0]["n"] == 0
    assert _final_runs(s["job"]) == 0


# ── I1~I14 대조(인계 메모 3절) — 기대값은 권장 순서 기준이며 계약 확정이 아니다 ─────────────────────
def test_i1_in_progress_stale_task_and_set_mismatch_then_each_reason_in_turn(env, rule_on):
    s = _failed_n5_checked(env)
    c, job = s["c"], s["job"]
    b = [x for x in _blocks(c, job).values() if x["check"] is not None][0]
    c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Edited for I1", "revision": b["revision"]})  # 검사 in_progress
    ok = [x["id"] for x in _blocks(c, job).values() if x["trans1"] and x["id"] != s["target"]][0]
    bad = [{"blockId": s["target"], "code": TF, "taskId": s["att"] + 7}, {"blockId": ok, "code": TF}]  # 오래된 taskId + 집합 불일치
    d = _err(_confirm(s, bad))
    assert d["reason"] == "check_not_settled" and d["registeredBlockIds"] == [] and {"blockId": b["id"], "state": "in_progress"} in d["blocks"]
    env["q"].drain()
    assert _err(_confirm(s, bad))["reason"] == "acknowledgement_mismatch"
    assert _err(_confirm(s, _ack(s, s["att"] + 7)))["reason"] == "acknowledgement_outdated"
    assert _confirm(s, _ack(s)).status_code == 200


def test_i3_translate_retry_in_progress_comes_before_check_condition(env, rule_on):
    s = _failed_n5_checked(env)
    _make_outdated(s)
    n = len(_checks(s["job"]))
    assert s["c"].post(f"/v1/jobs/{s['job']}/tasks/{s['att']}/retry").status_code == 200  # 재시도 pending(큐에 있음 · 아직 안 돎)
    r = _confirm(s, _ack(s))
    assert r.status_code == 409 and r.json()["error"]["message"] == "진행 중인 번역 재시도가 있다"  # 검사 조건보다 먼저
    assert len(_checks(s["job"])) == n  # 검사 등록도 하지 않는다
    _n5_no_final(s["job"])


def test_i5_retry_fills_block_then_check_registered_and_old_ack_is_mismatch(env, rule_on):
    s = _failed_n5_checked(env)
    s["tr"].fail_keys = set()
    assert s["c"].post(f"/v1/jobs/{s['job']}/tasks/{s['att']}/retry").status_code == 200
    env["q"].drain(skip={TC})  # 번역 채택까지 — 그 섹션 검사는 등록됐지만 아직 안 돎
    b = _blocks(s["c"], s["job"])[s["target"]]
    assert b["trans1"] and b["check"]["state"] == "in_progress"
    d = _err(_confirm(s, _ack(s)))
    assert d["reason"] == "check_not_settled" and d["registeredBlockIds"] == []
    execution.dispatch([c["id"] for c in _checks(s["job"]) if c["status"] == "pending"])
    env["q"].drain()
    assert _err(_confirm(s, _ack(s))) == {"reason": "acknowledgement_mismatch", "warnings": []}
    assert _confirm(s, []).status_code == 200


def test_i7_layout_only_save_during_check_keeps_check_and_task_ack(env, rule_on):
    s = _failed_n5_checked(env)
    c, job = s["c"], s["job"]
    sec = _blocks(c, job)[s["target"]]["sectionId"]
    pair = [x for x in _blocks(c, job).values() if x["sectionId"] == sec and x["check"] is not None]
    if len(pair) < 2:
        pytest.skip("실패 블록 섹션에 검사 대상 블록이 2개 미만")
    a, b = pair[0], pair[1]
    c.patch(f"/v1/jobs/{job}/blocks/{a['id']}", json={"trans1": "Edited for I7", "revision": a["revision"]})  # 섹션 검사 in_progress
    n = len(_checks(job))
    r = _patch_raw(c, job, b["id"], {"trans1": b["trans1"], "revision": b["revision"], "layoutAdjust": {"fontScale": 1.25}})
    assert r.status_code == 200, r.text
    assert len(_checks(job)) == n and _blocks(c, job)[a["id"]]["check"]["state"] == "in_progress"
    env["q"].drain()
    assert _confirm(s, _ack(s)).status_code == 200


def test_i8_concurrent_double_confirm_only_one_wins(env, rule_on):
    import threading

    s = _failed_n5_checked(env)
    out = []

    def go():
        out.append(_confirm(s, _ack(s)).status_code)

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(out) == [200, 409], out
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)
    assert _q("SELECT current_step FROM job WHERE id = :j", j=s["job"])[0]["current_step"] == "N6"


def test_i9_supply_unresolved_is_server_error_and_key_unavailable_is_failed_null(env, rule_on):
    """공급 기준(대상 언어 등)을 정할 수 없으면 상태 판정이 예외를 내 확정이 500(확정 쓰기 · 검사 등록 없음 — 현재 동작 기록).
    현재 키를 계산할 수 없으면(U1) failed · inputKey null 로 판정해 확정을 막지 않는다(PM 11-10 · API 6-10)."""
    from app.flows import text_check

    s = _failed_n5_checked(env)
    _make_outdated(s)
    n = len(_checks(s["job"]))
    rule_on.fail = True
    with pytest.raises(text_check.SupplyBasisUnresolved):  # TestClient 는 서버 예외를 그대로 올린다(실서버에서는 500)
        _confirm(s, _ack(s))
    assert len(_checks(s["job"])) == n
    _n5_no_final(s["job"])
    rule_on.fail = False
    rule_on.keys.fail = True  # U1(11-11 API 6-10): 현재 키를 계산할 수 없으면 failed · inputKey null — 시도 행 없이 확정 통과
    st = _blocks(s["c"], s["job"])
    assert all(v["check"] == {"state": "failed", "inputKey": None} for v in st.values() if v["check"] is not None)
    r = _confirm(s, _ack(s))
    assert r.status_code == 200, r.text
    assert len(_checks(s["job"])) == n  # 검사 등록 · 실패 기록 없음
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)


def test_i11_cancel_after_registration_then_next_confirm_not_n5(env, rule_on):
    s = _failed_n5_checked(env)
    _make_outdated(s)
    _err(_confirm(s, _ack(s)))
    reg = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"][0]
    assert s["c"].delete(f"/v1/jobs/{s['job']}").status_code == 200
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=reg["id"])[0]["status"] == "cancelled"
    r = _confirm(s, _ack(s))
    # 계약: 404 는 「없음 / 미소유(존재 은닉)」 전용, 취소(archived)된 자기 작업은 상태 충돌 409 INVALID_STATE(N5 아님). reason 은 없다(관찰)
    assert r.status_code == 409, r.text
    e = r.json()["error"]
    assert e["code"] == "INVALID_STATE" and e["retryable"] is False and "reason" not in (e.get("details") or {})
    assert _final_runs(s["job"]) == 0
    env["q"].drain()
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t", t=reg["id"])[0]["n"] == 0


def test_i13_redispatch_keeps_retry_count(env, rule_on):
    s = _failed_n5_checked(env)
    _make_outdated(s)
    env["q"].fail_send = True
    _err(_confirm(s, _ack(s)))
    reg = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"][0]
    before = _q("SELECT retry_count, attempt_no FROM job_async_task WHERE id = :t", t=reg["id"])[0]
    env["q"].fail_send = False
    recovery.sweep()
    assert _q("SELECT retry_count, attempt_no FROM job_async_task WHERE id = :t", t=reg["id"])[0] == before


def test_i14_partial_registration_reports_only_new_blocks(env, rule_on):
    """한 섹션은 새로 등록(outdated), 다른 섹션은 진행 중 시도 재사용 — registeredBlockIds 와 새 시도는 등록분만.
    「미해결로 등록 못 한 섹션」 변형은 I9 의 현재 동작(상태 판정 예외) 때문에 확정까지 오지 못해 여기서 보지 않는다."""
    s = _failed_n5_checked(env)
    b1 = _make_outdated(s)
    sec1 = _blocks(s["c"], s["job"])[b1]["sectionId"]
    others = [x for x in _blocks(s["c"], s["job"]).values() if x["check"] is not None and x["sectionId"] != sec1]
    if not others:
        pytest.skip("다른 섹션에 검사 대상 블록 없음")
    o = others[0]
    s["c"].patch(f"/v1/jobs/{s['job']}/blocks/{o['id']}", json={"trans1": "Edited for I14", "revision": o["revision"]})  # 다른 섹션 in_progress
    before = {c["id"] for c in _checks(s["job"])}
    d = _err(_confirm(s, _ack(s)))
    assert b1 in d["registeredBlockIds"] and o["id"] not in d["registeredBlockIds"]
    assert {"blockId": o["id"], "state": "in_progress"} in d["blocks"]
    new = [c for c in _checks(s["job"]) if c["id"] not in before]
    assert [c["unit_id"] for c in new] == [sec1]  # 커밋 범위 = 등록분



# ── 요청 객체 저장 실패(공급 · 키 계산은 성공) — 그 입력의 실패를 기록해 failed · 확정 통과(PM 11-10). errorCode 표현은 API 확인 전 잠정 ─────
class _SectionStore:
    """env 메모리 저장소 앞의 장애 대역. 지정한 블록 키가 든 고정 요청(input/)만 쓰기 실패 또는 되읽기 불일치로 만든다."""

    def __init__(self, inner):
        self.inner = inner
        self.fail_blocks: set[int] = set()
        self.mode = "put"
        self.bad_keys: set[str] = set()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def _hit(self, data):
        return any(f'"blk_{b}"'.encode() in data for b in self.fail_blocks)

    def put_if_absent(self, key, data, content_type=None):
        if key.startswith("input/") and self._hit(data):
            if self.mode == "put":
                raise ConnectionError("대역 — 쓰기 장애")
            self.bad_keys.add(key)
        return self.inner.put_if_absent(key, data, content_type)

    def get(self, key):
        if key in self.bad_keys and self.mode == "readback":
            return b'{"other":1}'
        return self.inner.get(key)


@pytest.fixture
def sec_store(env):
    from app.storage import set_store

    f = _SectionStore(env["store"])
    set_store(f)
    yield f
    set_store(env["store"])


def _two_outdated_sections(s):
    b1 = _make_outdated(s)
    sec1 = _blocks(s["c"], s["job"])[b1]["sectionId"]
    try:
        b2 = _make_outdated(s, skip_sections={sec1}, ext="GL-C996")
    except pytest.fail.Exception:
        pytest.skip("두 번째 섹션에 단어가 있는 검사 대상 블록 없음")
    return b1, sec1, b2, _blocks(s["c"], s["job"])[b2]["sectionId"]


def _section_blocks(s, sid):
    return {b["id"] for b in _blocks(s["c"], s["job"]).values() if b["sectionId"] == sid and b["check"] is not None}


def _http_409(r):
    """실제 HTTP 상태 · Error 형식을 그대로 돌려준다(관찰 기록용)."""
    assert r.status_code == 409, r.text
    e = r.json()["error"]
    return e["code"], e["retryable"], e["details"]


@pytest.mark.parametrize("mode", ["put", "readback"])
def test_store_failure_all_sections_records_failed_and_confirm_passes(env, rule_on, sec_store, mode):
    """확정 보조 등록이 두 섹션 모두 요청 객체 저장에 실패 — 실행할 시도는 없고 각 입력의 실패를 기록한다. 같은 잠금 안에서 다시 판정하면
    모두 settled · failed 라 그 확정 요청이 통과한다(409 반복 없음). 확인 집합 · taskId 조건은 그대로 적용된다."""
    s = _failed_n5_checked(env)
    b1, sec1, b2, sec2 = _two_outdated_sections(s)
    sec_store.mode = mode
    sec_store.fail_blocks = _section_blocks(s, sec1) | _section_blocks(s, sec2)
    before = {c["id"] for c in _checks(s["job"])}
    env["q"].messages.clear()
    d = _err(_confirm(s, []))  # 검사 조건은 통과 — 확인 집합 조건은 그대로 거절(이 요청의 기록도 요청과 함께 롤백)
    assert d["reason"] == "acknowledgement_mismatch"
    _n5_no_final(s["job"])
    assert {c["id"] for c in _checks(s["job"])} == before
    r = _confirm(s, _ack(s))
    assert r.status_code == 200, r.text
    new = [c for c in _checks(s["job"]) if c["id"] not in before]
    assert sorted((c["unit_id"], c["status"]) for c in new) == sorted([(sec1, "failed"), (sec2, "failed")])
    assert [n for n, _a in env["q"].messages if n == TC] == []  # 실행 · 전달 없음
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)


@pytest.mark.parametrize("mode", ["put", "readback"])
def test_store_failure_partial_commits_and_dispatches_only_successful_sections(env, rule_on, sec_store, mode):
    s = _failed_n5_checked(env)
    b1, sec1, b2, sec2 = _two_outdated_sections(s)
    sec_store.mode = mode
    sec_store.fail_blocks = _section_blocks(s, sec2)  # sec2 만 저장 실패
    before = {c["id"] for c in _checks(s["job"])}
    env["q"].messages.clear()
    code, retryable, d = _http_409(_confirm(s, _ack(s)))
    assert (code, retryable, d["reason"]) == ("INVALID_STATE", False, "check_not_settled")  # sec1 은 실제 검사 등록 — 끝날 때까지 대기
    assert b1 in d["registeredBlockIds"] and b2 not in d["registeredBlockIds"]
    new = [c for c in _checks(s["job"]) if c["id"] not in before]
    assert sorted((c["unit_id"], c["status"]) for c in new) == sorted([(sec1, "pending"), (sec2, "failed")])  # sec2 는 입력 실패 기록
    sec1_att = [c["id"] for c in new if c["unit_id"] == sec1]
    assert _queued(env, {c["id"] for c in new}) == sec1_att  # 실행할 시도만 커밋 · 전달
    assert _blocks(s["c"], s["job"])[b2]["check"]["state"] == "failed"
    _n5_no_final(s["job"])
    # 장애가 남은 채 재요청 — sec1 은 진행 중(대기 유지), sec2 는 같은 입력 기록 재사용(행 없음)
    code, retryable, d = _http_409(_confirm(s, _ack(s)))
    assert d["registeredBlockIds"] == [] and len(_checks(s["job"])) == len(before) + 2
    env["q"].drain()
    assert _confirm(s, _ack(s)).status_code == 200
    assert len(_checks(s["job"])) == len(before) + 2  # 중복 없음
    assert (_final_runs(s["job"]), _final_attempts(s["job"])) == (1, 1)


# ── I4 · I12: 번역 재시도 채택 → 검사 등록 · 대체(번역문 수정이 아니라 재시도 채택 경로 그대로) ──────────────────
def _outdated_in_target_section(s):
    sec = _blocks(s["c"], s["job"])[s["target"]]["sectionId"]
    others = {b["sectionId"] for b in _blocks(s["c"], s["job"]).values()} - {sec}
    try:
        return _make_outdated(s, skip_sections=others)
    except pytest.fail.Exception:
        pytest.skip("실패 블록 섹션에 단어가 있는 다른 검사 대상 블록 없음")


def _retry_attempt(job, prev):
    rows = _q("SELECT id, status, error_code, is_current FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND id > :p "
              "ORDER BY id DESC LIMIT 1", j=job, p=prev)
    return rows[0] if rows else None


def test_i4_retry_fails_again_partial_adoption_registers_section_check(env, rule_on):
    """재시도 시도는 실패 블록만 계산하고 같은 섹션의 기존 성공분을 보존(preserved)하므로 다시 실패하면 TRANSLATE_PARTIAL —
    downstream.TranslateHandler.after_failure 가 text_check.after_translate 로 그 섹션 검사를 등록한다(키가 바뀌었을 때만 새 시도)."""
    s = _failed_n5_checked(env)
    blk = _outdated_in_target_section(s)  # 재시도 전 그 섹션 현재 입력을 바꿔 둔다 — 재시도 채택 때 새 검사가 생겨야 한다
    sec = _blocks(s["c"], s["job"])[s["target"]]["sectionId"]
    before = {c["id"] for c in _checks(s["job"])}
    assert s["c"].post(f"/v1/jobs/{s['job']}/tasks/{s['att']}/retry").status_code == 200
    env["q"].drain(skip={TC})  # 번역 재시도 실행 · 채택까지
    ra = _retry_attempt(s["job"], s["att"])
    assert ra["status"] == "failed" and ra["error_code"] == "TRANSLATE_PARTIAL", ra
    new = [c for c in _checks(s["job"]) if c["id"] not in before]
    assert len(new) == 1 and new[0]["unit_id"] == sec and new[0]["retry_origin"] == "auto"  # 재시도 채택 경로의 등록
    assert _blocks(s["c"], s["job"])[blk]["check"]["state"] == "in_progress"
    d = _err(_confirm(s, _ack(s, ra["id"])))
    assert d["reason"] == "check_not_settled" and d["registeredBlockIds"] == []
    execution.dispatch([new[0]["id"]])
    env["q"].drain()
    assert _err(_confirm(s, _ack(s)))["reason"] == "acknowledgement_outdated"  # 옛 실패 시도
    assert _confirm(s, _ack(s, ra["id"])).status_code == 200


def test_i4_retry_fails_again_without_input_change_reuses_check(env, rule_on):
    """입력이 그대로면 재시도 채택의 등록은 재사용(REUSED) — 새 검사 시도를 만들지 않는다."""
    s = _failed_n5_checked(env)
    before = {c["id"] for c in _checks(s["job"])}
    assert s["c"].post(f"/v1/jobs/{s['job']}/tasks/{s['att']}/retry").status_code == 200
    env["q"].drain()
    ra = _retry_attempt(s["job"], s["att"])
    assert ra["error_code"] == "TRANSLATE_PARTIAL"
    assert {c["id"] for c in _checks(s["job"])} == before
    assert _confirm(s, _ack(s, ra["id"])).status_code == 200


def test_i12_retry_success_after_confirm_registration_supersedes_registered_check(env, rule_on):
    from app.execution import Envelope
    from app.flows.common import submit

    s = _failed_n5_checked(env)
    blk = _outdated_in_target_section(s)
    d = _err(_confirm(s, _ack(s)))
    assert blk in d["registeredBlockIds"]
    x1 = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"][0]
    env["q"].messages.clear()
    lease = execution.acquire(x1["id"], "slow-checker")  # 확정이 등록한 검사를 느린 워커가 잡고 있음
    s["tr"].fail_keys = set()
    assert s["c"].post(f"/v1/jobs/{s['job']}/tasks/{s['att']}/retry").status_code == 200
    env["q"].drain(skip={TC})  # 번역 재시도 채택(칸 채움) → after_done → after_translate
    ra = _retry_attempt(s["job"], s["att"])
    assert ra["status"] == "done"
    cur = [c for c in _checks(s["job"]) if c["is_current"] and c["unit_id"] == x1["unit_id"]]
    assert len(cur) == 1 and cur[0]["id"] != x1["id"] and cur[0]["retry_origin"] == "auto"  # 재시도 채택이 새 시도로 대체
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=x1["id"])[0]["status"] == "cancelled"
    env_ = Envelope(outcome="done", target_count=1, input_fingerprint=lease.fingerprint, payload={"text_check": {"blocks": []}})
    with pytest.raises(execution.LeaseLost):  # 대체된 등록 시도의 결과는 채택되지 않는다
        submit(lease, env_)
    execution.dispatch([cur[0]["id"]])
    env["q"].drain()
    assert _err(_confirm(s, _ack(s))) == {"reason": "acknowledgement_mismatch", "warnings": []}  # 경고가 사라졌다 — 새 상태로 판정
    assert _confirm(s, []).status_code == 200


def test_i12_retry_fails_again_after_confirm_registration_reuses_registered_check(env, rule_on):
    s = _failed_n5_checked(env)
    blk = _outdated_in_target_section(s)
    d = _err(_confirm(s, _ack(s)))
    assert blk in d["registeredBlockIds"]
    x1 = [c for c in _checks(s["job"]) if c["is_current"] and c["status"] == "pending"][0]
    env["q"].messages.clear()
    assert s["c"].post(f"/v1/jobs/{s['job']}/tasks/{s['att']}/retry").status_code == 200
    env["q"].drain(skip={TC})
    ra = _retry_attempt(s["job"], s["att"])
    assert ra["error_code"] == "TRANSLATE_PARTIAL"
    cur = [c for c in _checks(s["job"]) if c["is_current"] and c["unit_id"] == x1["unit_id"]]
    assert [c["id"] for c in cur] == [x1["id"]]  # 같은 입력 — 확정이 등록한 시도 재사용
    execution.dispatch([x1["id"]])
    env["q"].drain()
    assert _err(_confirm(s, _ack(s)))["reason"] == "acknowledgement_outdated"
    assert _confirm(s, _ack(s, ra["id"])).status_code == 200


# ── query 와 confirm 의 공급 기준(origin 무관) · 실제 데이터 변경과 구분 ─────────────────────────────────────
def test_query_and_confirm_build_same_supply_and_keys_until_data_changes(env, rule_on, monkeypatch):
    from app import db as app_db
    from app.manifest import stable_hash

    seen: list[tuple[str, int, str]] = []
    real = rule_on.glossary

    def spy(db, run, section_id, targets, *, origin):
        g = real(db, run, section_id, targets, origin=origin)
        seen.append((origin, section_id, stable_hash({k: v for k, v in g.items() if k != "supply_record"})))
        return g

    monkeypatch.setattr(rule_on, "glossary", spy)
    s = _failed_n5_checked(env)
    blk = _make_outdated(s)
    sec = _blocks(s["c"], s["job"])[blk]["sectionId"]
    seen.clear()
    d = _err(_confirm(s, _ack(s)))  # 조회와 같은 판정(origin=query) 뒤 등록(origin=confirm)
    assert blk in d["registeredBlockIds"]
    st = _blocks(s["c"], s["job"])[blk]["check"]  # 조회(origin=query)
    assert st["state"] == "in_progress"  # 등록 때 고정한 키 = 조회 때 계산한 키(기준이 같다)
    by = {}
    for origin, sid, h in seen:
        if sid == sec:
            by.setdefault(origin, set()).add(h)
    assert set(by) >= {"query", "confirm"} and len(by["query"] | by["confirm"]) == 1, by  # 같은 입력 · 같은 사전 → 같은 공급
    # 실제 데이터 변경(그 블록 원문에 나오는 새 강제 용어) — 키가 달라지는 정상 경우
    src = _q("SELECT source_ko FROM text_block WHERE id = :b", b=blk)[0]["source_ko"]
    have = {r["term_ko"] for r in _q("SELECT term_ko FROM glossary WHERE target_lang = 'en' AND internal_category = 'common'")}
    words = [w for w in sorted(set(re.findall(r"[A-Za-z가-힣]{4,}", src))) if w not in have]
    if not words:
        pytest.skip("원문에 아직 용어가 아닌 단어 없음")
    word = words[0]
    with app_db.engine.begin() as cn:
        cn.execute(text("INSERT INTO glossary (term_ko, term_target, target_lang, internal_category, enforcement, external_id, "
                        "term_kind, version) VALUES (:k, 'ZZTARGET2', 'en', 'common', 'enforced', 'GL-C997', 'certification', 1)"),
                   {"k": word})
    assert _blocks(s["c"], s["job"])[blk]["check"]["state"] == "outdated"
