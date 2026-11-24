"""확정의 translation_failed 확인 기준 taskId(계약 ConfirmRequest 「확인 기준 taskId」) — 실제 PostgreSQL + 메모리 저장소.

- taskId 는 선택 필드다. 보내면 그 경고가 지금도 미해결인지(확인 집합)와 그 블록을 실패시킨 현재 번역 시도인지를 함께 본다.
- 집합이 다르면 409 INVALID_STATE `acknowledgement_mismatch`, 집합은 같고 시도가 다르면 `acknowledgement_outdated`. 둘 다 `details.warnings` = 서버 집합.
- 블록 revision 은 확인 기준이 아니다 — 배치만 저장해 revision 이 올라도 같은 실패 시도에 대한 확인은 유효하다.
- 생략은 기존 판정, 명시적 null 은 422(요청 스키마 nullable 아님). 거절(409·422)은 작업 상태·시도·감사 기록을 바꾸지 않는다.
번역은 대역(FakeTranslator)이다. 감사 기록·검사 연결과의 순서는 이 파일 범위 밖이다(착수안 Q4·Q5·Q7 미정).
"""
from __future__ import annotations

import pytest

from app import ai_adapters
from tests.exec_db import requires_db
from tests.exec_fakes import FakeTranslator
from tests.test_exec_downstream import _patch, _q, _step, _to_n3, dburl, env  # noqa: F401

pytestmark = requires_db

TF = "translation_failed"


def _failed_n5(env):
    """번역 일부 실패로 N5 에 온 작업. 반환: (client, job, 실패 블록 id, 실패 번역 시도 id, 번역 대역)."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id", j=job)
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
    tr = FakeTranslator(fail_keys={f"blk_{target}"})
    ai_adapters.set_adapters(translator=tr)
    c.post(f"/v1/jobs/{job}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    return c, job, target, _current_failed_translate(job), tr


def _current_failed_translate(job):
    att = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current AND status = 'failed'", j=job)
    assert len(att) == 1
    return att[0]["id"]


def _confirm(c, job, acks):
    return c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": acks})


def _rejected(r, reason, target):
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "INVALID_STATE" and err["retryable"] is False and err["traceId"]
    assert err["details"] == {"reason": reason, "warnings": [{"blockId": target, "code": TF}]}  # warnings = 서버 집합 그대로


def _still_n5(job):
    assert _q("SELECT status, current_step FROM job WHERE id = :j", j=job)[0] == {"status": "review", "current_step": "N5"}
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND run_kind = 'final_render'", j=job)[0]["n"] == 0


def _writes(job):
    """거절된 확정이 남기면 안 되는 쓰기의 관측값: 작업 상태·갱신 시각, 시도·실행 행 수와 상태, 감사 기록 수."""
    return (_q("SELECT status, current_step, updated_at FROM job WHERE id = :j", j=job)[0],
            _q("SELECT id, status, is_current FROM job_async_task WHERE job_id = :j ORDER BY id", j=job),
            _q("SELECT count(*) AS n FROM audit_log WHERE target_type = 'job' AND target_id = :j", j=job)[0]["n"])


def _confirmed(r, job):
    assert r.status_code == 200, r.text
    assert r.json()["currentStep"] == "N6"
    assert _q("SELECT current_step FROM job WHERE id = :j", j=job)[0]["current_step"] == "N6"


# ── taskId 생략 — 기존 판정 그대로 ─────────────────────────────────────────────────────────────────────────
def test_without_task_id_set_rule_is_kept_and_mismatch_has_reason(env):
    c, job, target, att, _ = _failed_n5(env)
    before = _writes(job)
    blocks = {b["id"]: b for b in c.get(f"/v1/jobs/{job}/blocks").json()}
    assert blocks[target]["signals"][0] == {**blocks[target]["signals"][0], "code": TF, "taskId": att}  # 화면이 보는 확인 기준
    _rejected(_confirm(c, job, []), "acknowledgement_mismatch", target)
    _still_n5(job)
    assert _writes(job) == before
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF}]), job)


def test_explicit_null_task_id_is_422_and_omitted_is_not(env):
    """요청 스키마의 taskId 는 integer(nullable 아님). 생략과 명시적 null 을 구분한다 — null 은 형식 오류, 생략은 기존 판정."""
    c, job, target, _, _ = _failed_n5(env)
    before = _writes(job)
    r = _confirm(c, job, [{"blockId": target, "code": TF, "taskId": None}])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert (err["code"], err["message"], err["retryable"]) == ("VALIDATION_ERROR", "request validation failed", False)
    assert err["details"] == {"fields": [{"loc": ["body", "acknowledgedWarnings", 0, "taskId"], "msg": "Value error, must be an integer"}]}  # 명세 예시 taskIdNull
    assert _writes(job) == before
    # 다른 코드 항목의 null 도 같은 스키마라 형식 오류다
    r = _confirm(c, job, [{"blockId": target, "code": TF}, {"blockId": target, "code": "width_overflow", "taskId": None}])
    assert r.status_code == 422 and _writes(job) == before
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF}]), job)


# ── taskId 대조 ────────────────────────────────────────────────────────────────────────────────────────
def test_current_task_id_confirms(env):
    c, job, target, att, _ = _failed_n5(env)
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": att}]), job)


@pytest.mark.parametrize("delta", [1, 1000], ids=["next-id", "far"])
def test_other_task_id_is_outdated_and_changes_nothing(env, delta):
    c, job, target, att, _ = _failed_n5(env)
    before = _writes(job)
    _rejected(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": att + delta}]), "acknowledgement_outdated", target)
    _still_n5(job)
    assert _writes(job) == before
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": att}]), job)  # 재조회 뒤 현재 시도로 다시 확인


@pytest.mark.parametrize("value", [0, -1], ids=["zero", "negative"])
def test_zero_or_negative_task_id_is_not_a_valid_basis(env, value):
    """스키마에 최솟값이 없어 형식 오류(422)로 만들지 않는다. 그런 시도는 없으므로 확인 기준으로 받지 않는다."""
    c, job, target, _, _ = _failed_n5(env)
    _rejected(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": value}]), "acknowledgement_outdated", target)
    _still_n5(job)


def test_retry_that_fails_again_makes_previous_task_outdated(env):
    c, job, target, att, _ = _failed_n5(env)
    r = c.post(f"/v1/jobs/{job}/tasks/{att}/retry")  # 대역은 여전히 그 블록을 실패시킨다
    assert r.status_code == 200, r.text
    env["q"].drain()
    new = _current_failed_translate(job)
    assert new != att
    signal = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == target][0]["signals"][0]
    assert signal["code"] == TF and signal["taskId"] == new
    _rejected(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": att}]), "acknowledgement_outdated", target)  # 과거 실패에 대한 확인
    _still_n5(job)
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": new}]), job)


def test_retry_that_fills_the_block_makes_previous_ack_a_mismatch(env):
    c, job, target, att, tr = _failed_n5(env)
    tr.fail_keys = set()
    assert c.post(f"/v1/jobs/{job}/tasks/{att}/retry").status_code == 200
    env["q"].drain()
    assert [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == target][0]["trans1"]
    r = _confirm(c, job, [{"blockId": target, "code": TF, "taskId": att}])  # 이미 해소된 경고를 확인 — 집합이 다르다
    assert r.status_code == 409 and r.json()["error"]["details"] == {"reason": "acknowledgement_mismatch", "warnings": []}
    _still_n5(job)
    _confirmed(_confirm(c, job, []), job)


def test_revision_change_alone_does_not_invalidate_the_ack(env):
    """같은 섹션 다른 블록의 배치만 저장(revision+1·재렌더)하고, 실패 블록 revision 도 올려 본다 — 실패 시도는 그대로라 확인은 유효하다."""
    c, job, target, att, _ = _failed_n5(env)
    blocks = c.get(f"/v1/jobs/{job}/blocks").json()
    sec = [b for b in blocks if b["id"] == target][0]["sectionId"]
    other = [b for b in blocks if b["sectionId"] == sec and b["id"] != target and b["trans1"]][0]
    r = _patch(c, job, other["id"], {"trans1": other["trans1"], "revision": other["revision"], "layoutAdjust": {"fontScale": 1.25}})
    assert r.status_code == 200 and r.json()["block"]["revision"] == other["revision"] + 1, r.text
    _q("UPDATE text_block SET revision = revision + 1 WHERE id = :b", b=target)
    assert _current_failed_translate(job) == att
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": att}]), job)


# ── 순서·중복·대상 밖 ──────────────────────────────────────────────────────────────────────────────────
def test_mismatch_is_checked_before_outdated(env):
    c, job, target, att, _ = _failed_n5(env)
    ok = [b["id"] for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["trans1"] and b["id"] != target][0]
    r = _confirm(c, job, [{"blockId": target, "code": TF, "taskId": att + 1}, {"blockId": ok, "code": TF}])
    _rejected(r, "acknowledgement_mismatch", target)


def test_duplicate_ack_with_one_stale_task_is_outdated(env):
    c, job, target, att, _ = _failed_n5(env)
    acks = [{"blockId": target, "code": TF, "taskId": att}, {"blockId": target, "code": TF, "taskId": att + 1}]
    _rejected(_confirm(c, job, acks), "acknowledgement_outdated", target)
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": att}, {"blockId": target, "code": TF}]), job)


def test_task_id_outside_required_scope_is_ignored(env):
    c, job, target, att, _ = _failed_n5(env)
    ok = [b["id"] for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["trans1"] and b["id"] != target][0]
    acks = [{"blockId": target, "code": TF, "taskId": att}, {"blockId": ok, "code": "width_overflow", "taskId": 1},
            {"blockId": ok, "code": "width_overflow"}]
    _confirmed(_confirm(c, job, acks), job)


# ── 형식(task ID 스키마 integer · int64) ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [True, "9120", 0.5, 2**63, -(2**63) - 1, [1], {"id": 1}],
                         ids=["bool", "string", "fraction", "over-int64", "under-int64", "array", "object"])
def test_task_id_format_errors_are_422(env, value):
    c, job, target, _, _ = _failed_n5(env)
    before = _writes(job)
    r = _confirm(c, job, [{"blockId": target, "code": TF, "taskId": value}])
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    msg = "Value error, must be a 64-bit integer" if isinstance(value, int) and not isinstance(value, bool) else "Value error, must be an integer"
    assert err["code"] == "VALIDATION_ERROR" and err["details"] == {"fields": [{"loc": ["body", "acknowledgedWarnings", 0, "taskId"], "msg": msg}]}
    _still_n5(job)
    assert _writes(job) == before


def test_integral_number_task_id_is_the_same_integer(env):
    """계약 `integer`(JSON 수 중 정수 값) — 9120.0 은 정수 9120 과 같다. 블록 PATCH revision 과 같은 판단이다."""
    c, job, target, att, _ = _failed_n5(env)
    _confirmed(_confirm(c, job, [{"blockId": target, "code": TF, "taskId": float(att)}]), job)
