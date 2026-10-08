"""N3 proceed → ④⑤ → ⑥⑦⑧ → ⑨ → N5 → N6 — 실제 PostgreSQL + 메모리 저장소.

④ 라벨·⑧ 번역은 대역, ⑤ 로고·⑦ 스타일·⑥ 마스크 계산은 실제 파이프라인 코드(⑥ 모델만 가짜), ⑥ 은 원격 GPU 경로
(WorkerControl·InProcessControlClient·presigned 대역)로 실행한다. 대역 성공은 실제 AI 통합 성공이 아니다.
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import ai_adapters, execution
from app.manifest import sha256_bytes
from app.storage import MemoryStore, set_store
from tests.exec_db import bound_app_db, fresh_database, requires_db, seed_job
from tests.exec_fakes import (FakeAnalyzer, FakeInpaintModel, FakeJudge, FakeLabeler, FakeTranslator, QueueDispatcher,
                              sample_source_png, seed_dictionary, style_defaults_file)

pytestmark = requires_db


@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def env(dburl, tmp_path, monkeypatch):
    from app import db as app_db

    store = MemoryStore()
    set_store(store)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    monkeypatch.setenv("PIXLATE_GPU_WORK_DIR", str(tmp_path / "gpu"))
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", style_defaults_file(tmp_path))
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


def _q(sql, **p):
    from app import db as app_db

    with app_db.engine.begin() as c:
        res = c.execute(text(sql), p)
        return [dict(r) for r in res.mappings().all()] if res.returns_rows else None


def _to_n3(env):
    from app import db as app_db
    from app.main import app

    with app_db.engine.begin() as c:
        ids = seed_job(c)
        data = sample_source_png()
        key = f"source/{ids['job']}/src.png"
        env["store"].put(key, data)
        c.execute(text("INSERT INTO source_image (job_id, upload_order, file_url, width, height, sha256) VALUES (:j, 1, :k, 600, 1000, :h)"),
                  {"j": ids["job"], "k": key, "h": sha256_bytes(data)})
    cl = TestClient(app)
    tok = cl.post("/v1/auth/login", json={"email": ids["email"], "password": "x"}).json()["accessToken"]
    cl.headers["Authorization"] = f"Bearer {tok}"
    assert cl.post(f"/v1/jobs/{ids['job']}/analyze").status_code == 202
    env["q"].drain()
    assert cl.get(f"/v1/jobs/{ids['job']}").json()["currentStep"] == "N3"
    ids["c"] = cl
    ids["sections"] = sorted(r["id"] for r in _q("SELECT id FROM section WHERE job_id = :j", j=ids["job"]))
    return ids


def _step(ids):
    j = ids["c"].get(f"/v1/jobs/{ids['job']}").json()
    return j["status"], j["currentStep"]


def test_full_flow_to_n6_save(env):
    ids = _to_n3(env)
    c = ids["c"]
    r = c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    assert r.status_code == 202
    assert _step(ids) == ("processing", "N4")
    # proceed 시점에는 ④만 — 인페인트 등 하류를 미리 만들지 않는다(D1)
    assert {m[0] for m in env["q"].messages} == {"app.tasks.run_label"}
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    # ④ 라벨($ 블록) → ⑤ 생략(null) / 나머지 false·false
    rows = _q("SELECT tb.source_ko, tb.is_product_label, tb.is_brand_logo, tb.is_excluded, tb.trans_1, tb.style "
              "FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id", j=ids["job"])
    price = [r for r in rows if r["source_ko"].startswith("$")][0]
    assert (price["is_product_label"], price["is_brand_logo"], price["is_excluded"], price["trans_1"], price["style"]) == (True, None, True, None, None)
    others = [r for r in rows if not r["source_ko"].startswith("$")]
    assert all(r["is_excluded"] is False and r["trans_1"].startswith("EN ") for r in others)
    assert all(set(r["style"]) == {"font_color", "bg_color", "est_font_px", "align"} for r in others)  # 실제 ⑦ 측정
    secs = _q("SELECT inpaint_status, inpaint_image_url, mask_image_url, render_image_key, warning_badge FROM section WHERE job_id = :j",
              j=ids["job"])
    assert all(s["inpaint_status"] == "done" and s["inpaint_image_url"].startswith("verified/") and s["render_image_key"] for s in secs)
    assert env["model"].calls == 2  # ⑥ 실제 마스크 → 가짜 모델 호출
    # 원격 GPU 경로: 발급 기록·인계·검증 키
    grants = _q("SELECT g.worker_id, g.close_reason FROM task_lease_grant g WHERE g.job_id = :j", j=ids["job"])
    assert len(grants) == 2 and all(g["close_reason"] == "handed_off" for g in grants)
    tasks = c.get(f"/v1/jobs/{ids['job']}/tasks").json()
    assert tasks["currentStep"] == "N5" and tasks["failedCount"] == 0
    assert {s["key"]: s["status"] for s in tasks["stages"]} == {"inpaint": "done", "translate": "done", "verify": "pending", "render": "done"}
    pv = c.get(f"/v1/jobs/{ids['job']}/preview").json()
    assert all(s["renderedUrl"] for s in pv["sections"])
    # N5 확정 → N6 최종 렌더 → 저장
    r = c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 200, r.text
    assert c.post(f"/v1/jobs/{ids['job']}/save").status_code == 409  # 최종 렌더 전 저장 불가
    env["q"].drain()
    d = c.get(f"/v1/jobs/{ids['job']}/deliverables").json()["deliverables"]
    assert len(d) == 1 and d[0]["renderStatus"] == "done"  # 원본 1장 = 산출물 1건(포함 섹션 세로 배치)
    ds = _q("SELECT stack_offset, order_no FROM deliverable_section ORDER BY deliverable_id DESC, order_no LIMIT 2")
    assert [x["order_no"] for x in ds] == [1, 2] and ds[0]["stack_offset"] == 0
    assert c.post(f"/v1/jobs/{ids['job']}/save").status_code == 200
    assert _step(ids) == ("done", "N6")


def test_save_after_full_cancel_is_rejected(env):
    # PR #56 AI 리뷰 2: 최종 렌더가 성공한 뒤 전체 취소하면 저장이 archived 를 done 으로 덮어쓰지 않는다(D9-4)
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert _q("SELECT status FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=ids["job"]) == [
        {"status": "done"}]
    assert c.delete(f"/v1/jobs/{ids['job']}").status_code == 200
    r = c.post(f"/v1/jobs/{ids['job']}/save")
    assert r.status_code == 409
    job = _q("SELECT status, is_saved FROM job WHERE id = :j", j=ids["job"])[0]
    assert job == {"status": "archived", "is_saved": False}


def test_proceed_rejects_all_excluded_and_is_idempotent(env):
    ids = _to_n3(env)
    c = ids["c"]
    for s in ids["sections"]:
        c.patch(f"/v1/jobs/{ids['job']}/sections/{s}", json={"action": "exclude"})
    r = c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ALL_SECTIONS_EXCLUDED"
    c.patch(f"/v1/jobs/{ids['job']}/sections/{ids['sections'][0]}", json={"action": "restore"})
    # 동시 proceed 두 번 → 대표 실행 하나
    from app import db as app_db
    from app.flows.downstream import proceed

    results, errors = [], []

    def go():
        db = app_db.SessionLocal()
        try:
            results.append(proceed(db, ids["job"], ids["seller"]))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            db.close()

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert len({r["taskId"] for r in results}) == 1
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND run_kind = 'downstream'", j=ids["job"])[0]["n"] == 1
    # 포함 섹션 1개만 ④ 시도
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'label'", j=ids["job"])[0]["n"] == 1


def test_translation_partial_failure_goes_to_n5_and_retries_only_failed(env):
    ids = _to_n3(env)
    c = ids["c"]
    rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id",
              j=ids["job"])
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
    tr = FakeTranslator(fail_keys={f"blk_{target}"})
    ai_adapters.set_adapters(translator=tr)
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")  # 일부 실패는 N5 진행(D9-3)
    att = _q("SELECT id, status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current "
             "AND status = 'failed'", j=ids["job"])
    assert att == [{"id": att[0]["id"], "status": "failed", "error_code": "TRANSLATE_PARTIAL"}]  # 성공으로 기록하지 않는다
    blocks = c.get(f"/v1/jobs/{ids['job']}/blocks").json()
    failed = [b for b in blocks if b["id"] == target][0]
    assert failed["trans1"] is None and failed["signals"][0]["code"] == "translation_failed"
    assert failed["signals"][0]["taskId"] == att[0]["id"] and failed["signals"][0]["retryable"] is True
    # 확정은 빈 번역 확인이 필요하다
    r = c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    assert r.status_code == 409 and r.json()["error"]["warnings"] == [{"blockId": target, "code": "translation_failed"}]
    # [다시 시도] — 실패 블록만 계산, 성공분은 다시 쓰지 않는다
    before = {b["id"]: b["revision"] for b in blocks}
    tr.fail_keys = set()
    r = c.post(f"/v1/jobs/{ids['job']}/tasks/{att[0]['id']}/retry")
    assert r.status_code == 200 and r.json()["taskId"] != att[0]["id"]
    env["q"].drain()
    assert tr.calls[-1][1] == [f"blk_{target}"]
    after = {b["id"]: b for b in c.get(f"/v1/jobs/{ids['job']}/blocks").json()}
    assert after[target]["trans1"].startswith("EN ")
    assert all(after[i]["revision"] == before[i] for i in before if i != target)
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=r.json()["taskId"])[0]["status"] == "done"
    # 재시도 성공분이 반영되도록 그 섹션 미리보기를 다시 그렸다(재시도 한도 사용 없음)
    sec = _q("SELECT section_id FROM text_block WHERE id = :b", b=target)[0]["section_id"]
    pv = _q("SELECT attempt_no, status, retry_count, input_manifest FROM job_async_task WHERE stage = 'preview' AND unit_id = :s "
            "AND is_current", s=sec)[0]
    assert pv["attempt_no"] == 2 and pv["status"] == "done" and pv["retry_count"] == 0
    assert target in [b["block_id"] for b in pv["input_manifest"]["blocks"]]
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200


def test_translation_all_failed_is_n4_error_then_user_retry(env):
    ids = _to_n3(env)
    c = ids["c"]
    tr = FakeTranslator(fail_all=99)
    ai_adapters.set_adapters(translator=tr)
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("failed", "N4")  # 빈 N5 로 보내지 않는다
    cur = _q("SELECT id, attempt_no, retry_origin, error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate' "
             "AND is_current ORDER BY id", j=ids["job"])
    assert all(a["attempt_no"] == 2 and a["retry_origin"] == "auto" and a["error_code"] == "TRANSLATE_ALL_FAILED" for a in cur)
    tr.fail_all = 0
    for a in cur:
        assert c.post(f"/v1/jobs/{ids['job']}/tasks/{a['id']}/retry").status_code == 200
    assert _step(ids) == ("processing", "N4")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")


class _ConfigBrokenTranslator:
    impl_version = "broken/1"

    def translate(self, section_key, blocks, *, target_lang, context):
        raise ai_adapters.AdapterFailed("TRANSLATE_CONFIG_INVALID", "합성: 설정 오류", retryable=False)


def test_translation_failed_with_other_code_is_still_n4_error(env):
    # PR #56 AI 리뷰 1: 성공 0건이 TRANSLATE_ALL_FAILED 가 아닌 코드로 끝나도 빈 N5 로 보내지 않는다(D9-3)
    ids = _to_n3(env)
    ai_adapters.set_adapters(translator=_ConfigBrokenTranslator())
    ids["c"].post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("failed", "N4")
    cur = _q("SELECT error_code FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND is_current", j=ids["job"])
    assert cur and all(a["error_code"] == "TRANSLATE_CONFIG_INVALID" for a in cur)
    assert not _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=ids["job"])


def test_inpaint_failure_falls_back_to_original_and_stays_failed(env):
    ids = _to_n3(env)
    c = ids["c"]
    env["model"].fail = True
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    secs = _q("SELECT id, image_key, inpaint_status, warning_badge, render_image_key FROM section WHERE job_id = :j", j=ids["job"])
    assert all(s["inpaint_status"] == "failed" and s["warning_badge"] == "processing_failed" and s["render_image_key"] for s in secs)
    pv = _q("SELECT input_manifest FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=ids["job"])
    assert all(p["input_manifest"]["background"]["source"] == "original" for p in pv)
    tasks = c.get(f"/v1/jobs/{ids['job']}/tasks").json()
    inp = [i for i in tasks["items"] if i["taskType"] == "inpaint"]
    assert all(i["status"] == "failed" and i["retryable"] is False for i in inp)  # 재시도 버튼 없음
    r = c.post(f"/v1/jobs/{ids['job']}/tasks/{inp[0]['taskId']}/retry")
    assert r.status_code == 409


class FailingStyler:
    impl_version = "fake-style-fail/1"

    def style(self, *a, **k):
        from pipeline.stages.style import StyleInputError

        raise StyleInputError("측정 불가(대역)")


def test_style_failure_uses_role_defaults(env):
    ai_adapters.set_adapters(styler=FailingStyler())
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    st = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'style' AND is_current", j=ids["job"])
    assert st and all(s == {"status": "failed", "error_code": "STYLE_INPUT_ERROR"} for s in st)  # 실패는 실패로
    h = _q("SELECT h.payload FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id WHERE a.job_id = :j AND a.stage = 'preview' "
           "AND h.state = 'adopted'", j=ids["job"])
    srcs = {v for x in h for b in x["payload"]["render"]["blocks"].values() for v in b["source"].values()}
    assert srcs == {"role_default"}  # 적용값 출처를 측정값과 분리해 기록
    assert _q("SELECT count(*) AS n FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j AND tb.style IS NOT NULL",
              j=ids["job"])[0]["n"] == 0  # 측정값을 기본값으로 덮지 않는다
    c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    assert c.post(f"/v1/jobs/{ids['job']}/save").status_code == 200


def test_no_approved_defaults_render_fails_n5_opens_save_blocked(env, monkeypatch):
    monkeypatch.delenv("PIXLATE_STYLE_DEFAULTS")
    ai_adapters.set_adapters(styler=FailingStyler())
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    pv = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'preview' AND is_current", j=ids["job"])
    assert pv and all(p == {"status": "failed", "error_code": "STYLE_DEFAULTS_UNAPPROVED"} for p in pv)
    assert _step(ids) == ("review", "N5")  # 렌더 실패는 원본 표시로 진행(D9-3)
    preview = c.get(f"/v1/jobs/{ids['job']}/preview").json()
    assert all(s["renderedUrl"] is None and s["originalUrl"] for s in preview["sections"])
    items = [i for i in c.get(f"/v1/jobs/{ids['job']}/tasks").json()["items"] if i["taskType"] == "render"]
    assert all(i["unitType"] == "section" and i["status"] == "failed" for i in items)
    c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    fin = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=ids["job"])
    assert fin == [{"status": "failed", "error_code": "STYLE_DEFAULTS_UNAPPROVED"}]
    assert c.post(f"/v1/jobs/{ids['job']}/save").status_code == 409  # 최종 렌더 성공 전 저장 불가


def test_label_failure_is_n4_error_abort_returns_n3_and_reproceed(env):
    ids = _to_n3(env)
    c = ids["c"]
    lab = FakeLabeler(fail_sections={f"sec_{s}": 99 for s in ids["sections"]})
    ai_adapters.set_adapters(labeler=lab)
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("failed", "N4")
    r = c.post(f"/v1/jobs/{ids['job']}/abort")
    assert r.json()["returnTo"] == "N3" and _step(ids) == ("review", "N3")
    # 입력·버킷 유지
    assert len(c.get(f"/v1/jobs/{ids['job']}/sections").json()["include"]) == 2
    ai_adapters.set_adapters(labeler=FakeLabeler())
    assert c.post(f"/v1/jobs/{ids['job']}/sections/proceed").status_code == 202
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    runs = _q("SELECT status FROM job_async_task WHERE job_id = :j AND run_kind = 'downstream' ORDER BY id", j=ids["job"])
    assert [r["status"] for r in runs] == ["cancelled", "running"]


def test_abort_then_late_gpu_result_is_not_adopted(env):
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain(skip={"app.tasks.run_inpaint"})  # ⑥ 메시지는 GPU 가 아직 처리하지 않음
    inpaint_ids = [r["id"] for r in _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint'", j=ids["job"])]
    from app.flows.gpu_worker import InProcessControlClient

    cl = InProcessControlClient("gpu-test-1")
    got = cl.acquire(inpaint_ids[0])
    assert got is not None and got["token"]
    assert c.post(f"/v1/jobs/{ids['job']}/abort").json()["returnTo"] == "N3"
    assert cl.heartbeat(inpaint_ids[0], got["epoch"], got["token"]) is False
    with pytest.raises(execution.LeaseLost):
        cl.upload_url(inpaint_ids[0], got["epoch"], got["token"], "background", f"sec_{ids['sections'][0]}")
    env_body = {"outcome": "failed", "target_count": 1, "payload": {"error_code": "X", "message": "", "retryable": False},
                "input_fingerprint": got["fingerprint"], "artifacts": []}
    reg = cl.register(inpaint_ids[0], got["epoch"], got["token"], env_body)
    assert reg["state"] == "rejected" and reg["late"] is True  # 인증된 늦은 통지 = 이력만, 미채택
    assert cl.status(inpaint_ids[0], got["epoch"], got["token"])["discard"] is True
    secs = _q("SELECT inpaint_status FROM section WHERE job_id = :j", j=ids["job"])
    assert all(s["inpaint_status"] is None for s in secs)


def test_gpu_auth_and_renotify(env):
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    tid = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' ORDER BY id LIMIT 1", j=ids["job"])[0]["id"]
    from app.flows.gpu_worker import InProcessControlClient, WorkerControl

    cl = InProcessControlClient("gpu-test-2")
    got = cl.acquire(tid)
    # 다른 워커 주체·틀린 토큰은 거절
    with pytest.raises(execution.AuthRejected):
        WorkerControl().register("intruder", tid, got["epoch"], got["token"], {"outcome": "failed", "target_count": 1, "payload": {},
                                                                              "input_fingerprint": got["fingerprint"]})
    with pytest.raises(execution.AuthRejected):
        cl.register(tid, got["epoch"], "wrong-token", {"outcome": "failed", "target_count": 1, "payload": {},
                                                       "input_fingerprint": got["fingerprint"]})
    body = {"outcome": "failed", "target_count": 1, "input_fingerprint": got["fingerprint"],
            "payload": {"error_code": "INPAINT_FAILED", "message": "oom", "retryable": False}, "artifacts": []}
    a = cl.register(tid, got["epoch"], got["token"], body)
    b = cl.register(tid, got["epoch"], got["token"], body)  # 같은 본문 재통지 = 멱등
    assert a["handoff_id"] == b["handoff_id"]
    with pytest.raises(execution.HandoffConflict):
        cl.register(tid, got["epoch"], got["token"], {**body, "payload": {**body["payload"], "message": "different"}})
    # 발급하지 않은 업로드 키는 거절
    with pytest.raises(execution.AuthRejected):
        cl.register(tid, got["epoch"], got["token"], {**body, "outcome": "done", "payload": {"status": "inpainted"},
                                                      "artifacts": [{"kind": "background", "part_key": "x", "staging_key": "staging/1/2/3/background/x"}]})
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE task_id = :t", t=tid)[0]["n"] == 1


def test_user_edit_wins_over_late_translation(env):
    ids = _to_n3(env)
    c = ids["c"]
    rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id",
              j=ids["job"])
    target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
    tr = FakeTranslator(fail_keys={f"blk_{target}"})
    ai_adapters.set_adapters(translator=tr)
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    att = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND status = 'failed' AND is_current", j=ids["job"])[0]
    tr.fail_keys = set()
    c.post(f"/v1/jobs/{ids['job']}/tasks/{att['id']}/retry")  # 재시도 큐에 있음(아직 실행 전)
    blk = [b for b in c.get(f"/v1/jobs/{ids['job']}/blocks").json() if b["id"] == target][0]
    r = c.patch(f"/v1/jobs/{ids['job']}/blocks/{target}", json={"trans1": "Seller wrote this", "revision": blk["revision"]})
    assert r.status_code == 200 and r.json()["rerenderTaskId"]
    env["q"].drain()
    after = [b for b in c.get(f"/v1/jobs/{ids['job']}/blocks").json() if b["id"] == target][0]
    assert after["trans1"] == "Seller wrote this" and after["blockStatus"] == "edited"  # 자동 번역이 덮어쓰지 않는다
    es = _q("SELECT before_text, after_text FROM edit_signal WHERE text_block_id = :b", b=target)
    assert es == [{"before_text": None, "after_text": "Seller wrote this"}]
    # 사용자 입력으로 채워진 칸은 미해결 경고가 아니다
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200


def test_rerender_supersedes_old_preview(env):
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    blk = [b for b in c.get(f"/v1/jobs/{ids['job']}/blocks").json() if b["trans1"]][0]
    r1 = c.patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "v1", "revision": blk["revision"]}).json()
    old_task = r1["rerenderTaskId"]
    lease = execution.acquire(old_task, "slow-renderer")  # 옛 재렌더가 실행 중
    r2 = c.patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "v2", "revision": r1["block"]["revision"]}).json()
    assert r2["rerenderTaskId"] != old_task
    assert execution.heartbeat(lease) is False  # 대체된 시도의 권한은 끝났다
    env["q"].drain()
    cur = _q("SELECT status, retry_count, retry_origin FROM job_async_task WHERE id = :t", t=r2["rerenderTaskId"])[0]
    assert cur == {"status": "done", "retry_count": 0, "retry_origin": "user"}  # 입력 변경 재렌더는 재시도 한도를 쓰지 않는다
    assert _q("SELECT status FROM job_async_task WHERE id = :t", t=old_task)[0]["status"] == "cancelled"


def test_full_cancel_deletes_content_and_blocks_late_results(env):
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain(skip={"app.tasks.run_inpaint"})
    tid = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'inpaint' ORDER BY id LIMIT 1", j=ids["job"])[0]["id"]
    from app.flows.gpu_worker import InProcessControlClient

    cl = InProcessControlClient("gpu-test-3")
    got = cl.acquire(tid)
    up = cl.upload_url(tid, got["epoch"], got["token"], "background", f"sec_{ids['sections'][0]}")
    assert c.delete(f"/v1/jobs/{ids['job']}").status_code == 200
    j = ids["job"]
    assert env["store"].keys(f"source/{j}/") == [] and env["store"].keys(f"verified/{j}/") == []
    for table in ("section", "source_image", "task_handoff", "deliverable", "job_keyword"):
        assert _q(f"SELECT count(*) AS n FROM {table} WHERE job_id = :j", j=j)[0]["n"] == 0
    audits = _q("SELECT action_type, detail, actor_type FROM audit_log WHERE (target_type = 'job' AND target_id = :j) "
                "OR (detail->'execution'->>'job_id') = CAST(:j AS text) OR target_type = 'section'", j=j)
    assert any(a["action_type"] == "job_content_deleted" and a["actor_type"] == "seller" for a in audits)
    assert all(a["detail"] is None for a in audits if a["action_type"] in ("job_content_deleted",))
    assert _q("SELECT count(*) AS n FROM audit_log WHERE detail IS NOT NULL AND detail->'execution'->>'job_id' = CAST(:j AS text)",
              j=j)[0]["n"] == 0
    # 취소 전에 발급된 URL 로 늦게 올린 파일 → 등록 거절(기록 안 함) → 복구가 접두사를 다시 지운다
    cl.put(up["url"], up["key"], b"late")
    with pytest.raises(execution.HandoffRejected):
        cl.register(tid, got["epoch"], got["token"], {"outcome": "done", "target_count": 1, "input_fingerprint": got["fingerprint"],
                                                      "payload": {"status": "inpainted"},
                                                      "artifacts": [{"kind": "background", "part_key": f"sec_{ids['sections'][0]}",
                                                                     "staging_key": up["key"]}]})
    assert _q("SELECT count(*) AS n FROM task_handoff WHERE job_id = :j", j=j)[0]["n"] == 0
    assert cl.status(tid, got["epoch"], got["token"])["discard"] is True
    from app import recovery

    recovery.sweep()
    assert env["store"].keys(f"staging/{j}/") == []
    job = c.get(f"/v1/jobs/{j}").json()
    assert job["status"] == "archived" and job["productName"] is None
