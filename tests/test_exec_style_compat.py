"""⑦ 결과 버전 호환 — 배포 전에 계산됐지만 채택되지 않은 스키마 1 결과와 신규 스키마 2 결과의 채택 · 복구 · 거절. 실제 PostgreSQL.

⑦ 계산은 실제 파이프라인 코드(PipelineStyler)로 하고, 스키마 1 결과는 같은 계산의 이전 규칙 값(tests/style_fixtures.to_schema_v1)으로 만든다.
스키마 1은 계산 당시 형식대로 검증해 그때의 정렬값을 그대로 채택하고, 알 수 없는 버전 · 형식 위반은 거절해 스타일 실패 후속으로 보낸다.
"""
from __future__ import annotations

import copy

import pytest
from sqlalchemy import text

from app import ai_adapters, execution, recovery
from app.storage import MemoryStore, set_store
from tests.exec_db import bound_app_db, fresh_database, requires_db
from tests.exec_fakes import (FakeAnalyzer, FakeInpaintModel, FakeJudge, FakeLabeler, FakeTranslator, QueueDispatcher, seed_dictionary,
                              style_defaults_file)
from tests.style_fixtures import to_schema_v1
from tests.test_exec_downstream import _q, _step, _to_n3

pytestmark = requires_db

STYLE_KEYS = ("font_color", "bg_color", "est_font_px", "align")


@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def env(dburl, tmp_path, monkeypatch):
    from app import db as app_db
    from app import s3

    store = MemoryStore()
    set_store(store)
    monkeypatch.setattr(s3, "presigned_get", lambda key, ttl=300, download_name=None: f"https://presigned.test/{key}" if key else None)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    monkeypatch.setenv("PIXLATE_GPU_WORK_DIR", str(tmp_path / "gpu"))
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", style_defaults_file(tmp_path))
    monkeypatch.setenv("PIXLATE_DISPATCH_STALE_S", "0")
    ai_adapters.reset_adapters()
    model = FakeInpaintModel()
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(), judge=FakeJudge(), labeler=FakeLabeler(), translator=FakeTranslator(),
                             inpainter=ai_adapters.PipelineInpainter(model_factory=lambda cfg: model))
    with app_db.engine.begin() as c:
        c.execute(text("DELETE FROM expression_dictionary"))
        seed_dictionary(c)
    yield {"store": store, "q": q, "model": model, "tmp": tmp_path}
    execution.set_dispatcher(None)
    set_store(None)
    ai_adapters.reset_adapters()


class _Dumped:
    def __init__(self, data: dict):
        self.data = data

    def model_dump(self, mode: str | None = None) -> dict:
        return copy.deepcopy(self.data)


class _PayloadStyler:
    """실제 ⑦ 계산 뒤 결과 JSON을 바꿔 돌려준다 — 배포 전 계산(스키마 1) · 손상 결과를 흉내 낸다."""

    def __init__(self, change, impl_version: str):
        self.change = change
        self.impl_version = impl_version

    def style(self, *a, **k):
        return _Dumped(self.change(ai_adapters.PipelineStyler().style(*a, **k).model_dump(mode="json")))


class _Crash(BaseException):
    """워커 프로세스 종료 흉내 — Exception 처리(_guard · adopt의 일시 오류 경로)를 거치지 않는다."""


def _style_rows(ids):
    return _q("SELECT a.id, a.status, a.error_code, a.error_message, h.id AS handoff_id, h.state, h.reject_reason, h.adopted_by, "
              "h.payload FROM job_async_task a JOIN task_handoff h ON h.task_id = a.id "
              "WHERE a.job_id = :j AND a.stage = 'style' AND a.is_current ORDER BY a.id, h.id", j=ids["job"])


def _assert_text_block_style_is(payload: dict) -> int:
    """⑦ 결과의 대상 블록 측정값이 text_block.style에 그대로 들어갔는지. 대상 블록 수를 돌려준다."""
    n = 0
    for b in payload["blocks"]:
        st = _q("SELECT style FROM text_block WHERE id = :b", b=int(b["block_key"].split("_")[1]))[0]["style"]
        if b["status"] == "excluded":
            continue
        assert st == {k: b[k] for k in STYLE_KEYS}, b["block_key"]
        n += 1
    return n


def _preview_align_sources(ids) -> set[str]:
    h = _q("SELECT h.payload FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j AND a.stage = 'preview' "
           "AND h.state = 'adopted'", j=ids["job"])
    return {b["source"]["align"] for x in h for b in x["payload"]["render"]["blocks"].values()}


def _proceed(ids):
    assert ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code in (200, 202)


def test_schema_2_result_is_adopted(env):
    """신규 계산(스키마 2)은 현재 형식으로 검증해 채택한다."""
    ids = _to_n3(env)
    _proceed(ids)
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    rows = _style_rows(ids)
    assert rows and all((r["status"], r["error_code"], r["state"]) == ("done", None, "adopted") for r in rows)
    assert all(r["payload"]["style"]["schema_version"] == "2" for r in rows)
    assert sum(_assert_text_block_style_is(r["payload"]["style"]) for r in rows) > 0


def test_schema_1_result_first_adoption_keeps_values_calculated_then(env):
    """배포 전 계산된 스키마 1 결과를 처음 채택 — 이전 형식대로 검증하고 계산 당시 정렬값을 그대로 쓴다(새 근거로 바꾸지 않음)."""
    ai_adapters.set_adapters(styler=_PayloadStyler(to_schema_v1, "pipeline.style@legacy-v1"))
    ids = _to_n3(env)
    _proceed(ids)
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    rows = _style_rows(ids)
    assert rows and all((r["status"], r["error_code"], r["state"]) == ("done", None, "adopted") for r in rows)
    payloads = [r["payload"]["style"] for r in rows]
    assert all(p["schema_version"] == "1" for p in payloads)
    bases = {b["align_basis"] for p in payloads for b in p["blocks"]}
    assert bases <= {"estimated", "default_single_line", "default_tie", None} and bases - {None}  # 이전 규칙의 근거 그대로
    assert sum(_assert_text_block_style_is(p) for p in payloads) > 0
    assert "measured" in _preview_align_sources(ids)  # 채택한 이전 정렬값이 렌더에 측정값으로 쓰였다


def test_schema_1_result_recovered_after_crash_then_duplicate_adoption_is_noop(env, monkeypatch):
    """스키마 1 결과 등록 직후 워커 종료(채택 전) → 권한 만료 → 복구자가 인수해 채택. 같은 인계를 다시 채택해도 바뀌지 않는다."""
    from app.flows.downstream import execute_style

    ai_adapters.set_adapters(styler=_PayloadStyler(to_schema_v1, "pipeline.style@legacy-v1"))
    ids = _to_n3(env)
    _proceed(ids)
    env["q"].drain(skip={"app.tasks.run_style"})
    tids = [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'style' AND is_current", j=ids["job"])]
    assert tids

    def crash(*a, **k):
        raise _Crash()

    for t in tids:
        with monkeypatch.context() as m:
            m.setattr(execution, "adopt", crash)
            with pytest.raises(_Crash):
                execute_style(t)
    before = _style_rows(ids)
    assert [(r["status"], r["state"]) for r in before] == [("running", "received")] * len(tids)  # 등록만 됨 — 채택 · 실패 기록 없음
    _q("UPDATE job_async_task SET lease_expires_at = now() - interval '1 second' WHERE id = ANY(:t)", t=tids)
    st = recovery.sweep()
    assert st["adoptions"] == len(tids)
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    rows = _style_rows(ids)
    assert all((r["status"], r["state"]) == ("done", "adopted") and r["adopted_by"].startswith("be-adopter") for r in rows)
    payloads = [r["payload"]["style"] for r in rows]
    assert all(p["schema_version"] == "1" for p in payloads) and sum(_assert_text_block_style_is(p) for p in payloads) > 0

    # 중복 채택 — 응답 유실 후 같은 인계를 다시 채택해도 결과 · 후속을 다시 만들지 않는다
    n_tasks = _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=ids["job"])[0]["n"]
    styles = _q("SELECT id, style, updated_at FROM text_block WHERE section_id = ANY(:s) ORDER BY id", s=ids["sections"])
    for r in rows:
        lease = execution.Lease(attempt_id=r["id"], job_id=ids["job"], run_id=0, stage="style", unit_type="section", unit_id=0,
                                epoch=1, owner="x", manifest={}, fingerprint="")
        assert execution.adopt(r["handoff_id"], lease) is None
    assert recovery.sweep()["adoptions"] == 0
    env["q"].drain()
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=ids["job"])[0]["n"] == n_tasks
    assert _q("SELECT id, style, updated_at FROM text_block WHERE section_id = ANY(:s) ORDER BY id", s=ids["sections"]) == styles


def _unknown_version(d):
    d["schema_version"] = "3"
    return d


def _v2_marked_v1(d):
    d["schema_version"] = "1"
    return d


def _contradictory_v1(d):
    v1 = to_schema_v1(d)
    for b in v1["blocks"]:
        if b["align_basis"] in ("default_single_line", "default_tie"):
            b["align"] = "center"  # 이전 규칙에서 기본값 근거면 left여야 한다
            return v1
    raise AssertionError("기본값 근거 블록이 없다")


def _missing(d):
    return None


@pytest.mark.parametrize("change, needle", [
    (_unknown_version, "알 수 없는 ⑦ 결과 버전 '3'"),
    (_v2_marked_v1, "⑦ 결과 형식 오류"),
    (_contradictory_v1, "이면 align은 left여야 한다"),
    (_missing, "객체가 아니다"),
])
def test_invalid_style_result_is_rejected_and_falls_back(env, change, needle):
    """알 수 없는 버전 · 다른 버전 형식 · 이전 형식의 모순 · 결과 없음은 호환 처리로 통과시키지 않는다 — 인계 거절, 시도 실패 기록,
    스타일 실패 후속(역할 기본값으로 N5 진행). 거절된 인계는 복구가 다시 채택하지 않는다."""
    ai_adapters.set_adapters(styler=_PayloadStyler(change, "pipeline.style@broken"))
    ids = _to_n3(env)
    _proceed(ids)
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    rows = _style_rows(ids)
    assert rows
    for r in rows:
        assert (r["status"], r["error_code"], r["state"]) == ("failed", "STYLE_RESULT_INVALID", "rejected")
        assert needle in r["error_message"] and r["reject_reason"].startswith("STYLE_RESULT_INVALID") and needle in r["reject_reason"]
    assert _q("SELECT count(*) AS n FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j AND tb.style IS NOT NULL",
              j=ids["job"])[0]["n"] == 0  # 거절한 결과를 반영하지 않았다
    assert _preview_align_sources(ids) == {"role_default"}
    n_tasks = _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=ids["job"])[0]["n"]
    assert recovery.sweep()["adoptions"] == 0
    env["q"].drain()
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=ids["job"])[0]["n"] == n_tasks
