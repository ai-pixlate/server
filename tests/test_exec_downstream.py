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
    assert r.status_code == 409 and r.json()["error"]["details"]["warnings"] == [{"blockId": target, "code": "translation_failed"}]
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


def test_unreadable_style_defaults_render_fails_n5_opens_save_blocked(env, monkeypatch, tmp_path):
    # 대체 경로가 가리키는 자료를 읽을 수 없으면 렌더 실패 — 저장소 정본·내장 값으로 조용히 넘어가지 않는다
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", str(tmp_path / "missing-style-defaults.json"))
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


def test_align_only_missing_uses_approved_default_in_preview_and_final(env):
    """F1 작업 15 재현(합성): ⑦ 완료인데 한 블록의 align 만 NULL → 그 항목만 승인 기본값(left), 글자색·크기는 측정값 유지.
    미리보기·최종 렌더 모두 성공하고 저장까지 된다. text_block.style 은 그대로(측정값을 덮지 않는다)."""
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    blk = [b for b in c.get(f"/v1/jobs/{ids['job']}/blocks").json() if b.get("trans1")][0]
    before = _q("SELECT style FROM text_block WHERE id = :b", b=blk["id"])[0]["style"]
    assert before and all(before[k] is not None for k in ("font_color", "est_font_px", "align"))
    _q("UPDATE text_block SET style = jsonb_set(style, '{align}', 'null'::jsonb) WHERE id = :b", b=blk["id"])
    nulled = _q("SELECT style FROM text_block WHERE id = :b", b=blk["id"])[0]["style"]
    # 번역문을 고쳐 그 섹션 미리보기를 새 고정 입력으로 다시 만든다(같은 렌더 경로)
    r = c.patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": "Gentle care", "revision": blk["revision"]})
    assert r.status_code == 200 and r.json()["rerenderTaskId"]
    env["q"].drain()
    pv = _q("SELECT a.status, a.error_code, h.payload FROM job_async_task a LEFT JOIN task_handoff h ON h.task_id = a.id AND h.state = 'adopted' "
            "WHERE a.id = :t", t=r.json()["rerenderTaskId"])[0]
    assert pv["status"] == "done", pv["error_code"]
    rb = pv["payload"]["render"]["blocks"][str(blk["id"])]
    assert rb["source"] == {"font_color": "measured", "est_font_px": "measured", "align": "role_default"}
    assert rb["applied"]["align"] == "left"  # 승인값(pipeline_mapping.align)
    assert rb["applied"]["font_color"] == before["font_color"] and rb["applied"]["est_font_px"] == before["est_font_px"]
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    fin = _q("SELECT a.status, a.error_code, h.payload FROM job_async_task a LEFT JOIN task_handoff h ON h.task_id = a.id AND h.state = 'adopted' "
             "WHERE a.job_id = :j AND a.stage = 'final_render' AND a.is_current", j=ids["job"])
    assert [(f["status"], f["error_code"]) for f in fin] == [("done", None)]
    fb = [v for s in fin[0]["payload"]["renders"].values() for k, v in s["applied"].items() if k == str(blk["id"])][0]
    assert fb["source"]["align"] == "role_default" and fb["source"]["font_color"] == "measured"
    assert c.post(f"/v1/jobs/{ids['job']}/save").status_code == 200
    assert _q("SELECT style FROM text_block WHERE id = :b", b=blk["id"])[0]["style"] == nulled  # 측정값·NULL 그대로


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_translation_edit_rerenders_preview_and_final(env, blank):
    """N5 에서 번역문을 지우거나 공백만 남기면 그 블록은 조판하지 않는다(6.3) — 미리보기 고정 입력에서도 빠져 채택이 실패하지 않는다."""
    ids = _to_n3(env)
    c = ids["c"]
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    blk = [b for b in c.get(f"/v1/jobs/{ids['job']}/blocks").json() if b.get("trans1")][0]
    r = c.patch(f"/v1/jobs/{ids['job']}/blocks/{blk['id']}", json={"trans1": blank, "revision": blk["revision"]})
    assert r.status_code == 200 and r.json()["rerenderTaskId"]
    env["q"].drain()
    pv = _q("SELECT status, error_code, input_manifest FROM job_async_task WHERE id = :t", t=r.json()["rerenderTaskId"])[0]
    assert (pv["status"], pv["error_code"]) == ("done", None)
    assert blk["id"] not in [b["block_id"] for b in pv["input_manifest"]["blocks"]]
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    fin = _q("SELECT status, error_code FROM job_async_task WHERE job_id = :j AND stage = 'final_render' AND is_current", j=ids["job"])
    assert fin == [{"status": "done", "error_code": None}]


def test_render_input_changed_when_style_defaults_fingerprint_differs(monkeypatch):
    from app import typeset
    from app.flows import downstream
    from app.execution import AdoptionRejected
    from tests.exec_fakes import png_bytes

    bg = png_bytes(100, 100)
    monkeypatch.setattr(downstream, "get_store", lambda: type("S", (), {"get": staticmethod(lambda k: bg)})())
    m = {"background": {"key": "k", "sha256": None}, "blocks": [], "style_stage": "done",
         "style_defaults": {**typeset.style_inputs(), "rule": "role-style-rule/0"}}  # 고정 당시 규칙 버전이 다름
    with pytest.raises(AdoptionRejected) as e:
        downstream._render_section(m, {}, {}, {})
    assert e.value.code == "INPUT_CHANGED"


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


# ── 통합 계약(2026-10-14 묶음 A·B·C·D) ─────────────────────────────────
def _to_n5(env, fail_first=False):
    ids = _to_n3(env)
    c = ids["c"]
    target = None
    if fail_first:
        rows = _q("SELECT tb.id, tb.source_ko FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j ORDER BY tb.id",
                  j=ids["job"])
        target = [r for r in rows if r["source_ko"].startswith("Locks")][0]["id"]
        ai_adapters.set_adapters(translator=FakeTranslator(fail_keys={f"blk_{target}"}))
    c.post(f"/v1/jobs/{ids['job']}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    return ids, target


def _fe_acks(blocks):
    """FE buildAcknowledgedWarnings 와 같다: 불러온 블록의 신호 전부."""
    return [{"blockId": b["id"], "code": s["code"]} for b in blocks for s in b["signals"]]


def _overflow_other(ids, not_id=None):
    b = _q("SELECT tb.id FROM text_block tb JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j AND s.bucket = 'include' "
           "AND tb.trans_1 IS NOT NULL ORDER BY tb.id", j=ids["job"])
    oid = [x["id"] for x in b if x["id"] != not_id][0]
    _q("UPDATE text_block SET overflow = true WHERE id = :b", b=oid)
    return oid


def test_confirm_accepts_fe_style_acks_with_overflow_and_out_of_scope(env):  # A
    ids, target = _to_n5(env, fail_first=True)
    c = ids["c"]
    oid = _overflow_other(ids, not_id=target)
    blocks = c.get(f"/v1/jobs/{ids['job']}/blocks").json()
    codes = {(b["id"], s["code"]) for b in blocks for s in b["signals"]}
    assert (target, "translation_failed") in codes and (oid, "width_overflow") in codes  # 경고는 그대로 표시
    acks = _fe_acks(blocks) + [{"blockId": 987654321, "code": "translation_failed"}]  # 범위 밖 블록은 무시
    r = c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": acks})
    assert r.status_code == 200, r.text
    assert _step(ids) == ("review", "N6")


def test_confirm_rejects_missing_required_ack_with_details(env):  # A + E
    ids, target = _to_n5(env, fail_first=True)
    c = ids["c"]
    oid = _overflow_other(ids, not_id=target)
    r = c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": [{"blockId": oid, "code": "width_overflow"}]})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "INVALID_STATE" and err["details"]["warnings"] == [{"blockId": target, "code": "translation_failed"}]
    assert err["retryable"] is False and isinstance(err["traceId"], str)
    assert _step(ids) == ("review", "N5")


def test_confirm_overflow_only_does_not_block(env):  # A
    ids, _ = _to_n5(env)
    c = ids["c"]
    _overflow_other(ids)
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200


def test_block_patch_returns_full_block_and_conflict_current(env):  # D
    ids, _ = _to_n5(env)
    c = ids["c"]
    b = [x for x in c.get(f"/v1/jobs/{ids['job']}/blocks").json() if x["trans1"]][0]
    r = c.patch(f"/v1/jobs/{ids['job']}/blocks/{b['id']}", json={"trans1": "Edited", "revision": b["revision"]})
    assert r.status_code == 200, r.text
    nb = r.json()["block"]
    assert set(b) <= set(nb)  # 목록(CFM-01)과 같은 TextBlock 필드 전부
    assert (nb["id"], nb["sectionId"], nb["role"], nb["trans1"], nb["blockStatus"], nb["revision"]) == (
        b["id"], b["sectionId"], b["role"], "Edited", "edited", b["revision"] + 1)
    stale = c.patch(f"/v1/jobs/{ids['job']}/blocks/{b['id']}", json={"trans1": "Again", "revision": b["revision"]})
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "REVISION_CONFLICT" and err["details"]["current"]["id"] == b["id"]
    assert err["details"]["current"]["revision"] == b["revision"] + 1 and err["details"]["current"]["trans1"] == "Edited"
    assert set(b) <= set(err["details"]["current"])


def _mutate_regulatory(raw):
    raw["blocks"][1]["source_ko"] = "피부 치료 효과와 주름 개선"


def test_sections_list_and_detail_follow_contract(env):  # B
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(mutate=_mutate_regulatory))
    ids = _to_n3(env)
    c = ids["c"]
    secs = c.get(f"/v1/jobs/{ids['job']}/sections").json()
    allsec = secs["include"] + secs["exclude"]
    src = _q("SELECT id FROM source_image WHERE job_id = :j", j=ids["job"])[0]["id"]
    for s in allsec:
        assert s["sourceImageId"] == src and set(s["bbox"]) == {"x", "y", "w", "h"} and s["signals"] == []
        for v in s["verdicts"]:
            assert set(v) == {"id", "verdictStatus", "verdictType", "problemText", "alternativeExpression",
                              "basisArticle", "evidenceUrl", "reason"}
    ex = secs["exclude"][0]
    assert [v["verdictStatus"] for v in ex["verdicts"]] == ["regulated", "conditional"]  # 정렬 불변식
    assert ex["displayTop"] is None
    top = 0
    for s in sorted(secs["include"], key=lambda x: x["sectionOrder"]):
        assert s["displayTop"] == top
        top += s["height"] or 0
    det = c.get(f"/v1/jobs/{ids['job']}/sections/{ex['id']}").json()
    assert {k: v for k, v in det.items() if k != "contentFindings"} == ex and "contentFindings" in det
    # 목록의 포함 섹션은 썸네일만(판정 목록 비움, 계약 sectionList). 상세(SEC-02)는 판정 근거 전체
    inc = secs["include"][0]["id"]
    _q("INSERT INTO section_verdict (section_id, verdict_status, verdict_type, reason) VALUES (:s, 'conditional', 'regulatory_conditional', 't')",
       s=inc)
    again = c.get(f"/v1/jobs/{ids['job']}/sections").json()
    assert all(s["verdicts"] == [] for s in again["include"]) and again["exclude"][0]["verdicts"] == ex["verdicts"]
    assert [v["verdictStatus"] for v in c.get(f"/v1/jobs/{ids['job']}/sections/{inc}").json()["verdicts"]] == ["conditional"]
    # 판정 정렬 전체 순서(2026-10-14 확정): regulated → policy → irrelevant → needs_fix → conditional
    for st, vt in (("needs_fix", "needs_fix"), ("irrelevant", "local_irrelevant"), ("policy", "channel_policy")):
        _q("INSERT INTO section_verdict (section_id, verdict_status, verdict_type) VALUES (:s, :st, :vt)", s=ex["id"], st=st, vt=vt)
    got = [v["verdictStatus"] for v in c.get(f"/v1/jobs/{ids['job']}/sections").json()["exclude"][0]["verdicts"]]
    assert got == ["regulated", "policy", "irrelevant", "needs_fix", "conditional"]
    assert got == [v["verdictStatus"] for v in c.get(f"/v1/jobs/{ids['job']}/sections/{ex['id']}").json()["verdicts"]]
    # 제외 사유는 BE 저장값(PRD 문자열) 그대로 — 계약 enum 을 auto_local·auto_local_failed 로 맞춘다(2026-10-14)
    assert {s["exclusionReason"] for s in again["exclude"]} <= {"auto_regulatory", "auto_channel", "auto_local", "auto_local_failed",
                                                                   "user_manual", "restored_by_user"}


def test_export_bundle_types_names_and_html_excluded(env, monkeypatch):  # C
    import io as _io
    import zipfile
    from datetime import datetime

    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    names = []  # presigned_get 에 넘긴 저장 이름(Content-Disposition)
    monkeypatch.setattr(s3, "presigned_get",
                        lambda key, ttl=300, download_name=None: (names.append(download_name), f"https://presigned.test/{key}")[1] if key else None)
    ids, _ = _to_n5(env)
    c = ids["c"]
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()

    def n():
        return _q("SELECT count(*) AS n FROM export_artifact WHERE job_id = :j", j=ids["job"])[0]["n"]

    # 지원 밖(html·psd)이 하나라도 있으면 지원 형식과 섞여 있어도 전체 거절, 아무것도 만들지 않는다(2026-10-14 결정)
    for comps in (["html"], ["images", "html"], ["psd"], ["images", "psd"], ["content.csv", "images", "psd"], []):
        r = c.post(f"/v1/jobs/{ids['job']}/export", json={"components": comps})
        assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", (comps, r.text)
        assert r.json()["error"]["details"]["unsupported"] == [c for c in comps if c in ("html", "psd")]
        assert n() == 0

    r = c.post(f"/v1/jobs/{ids['job']}/export", json={"components": ["images", "content.csv"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["artifactType"] == "zip" and body["components"] == ["images", "csv"]
    d = c.get(f"/v1/jobs/{ids['job']}/exports/{body['artifactId']}/download").json()
    assert d["fileName"].endswith(".zip") and "expiresIn" not in d
    assert names[-1] == d["fileName"] == f"pixlate_export_{ids['job']}.zip"  # 응답 이름 = 다운로드 저장 이름
    assert datetime.fromisoformat(d["expiresAt"].replace("Z", "+00:00")).tzinfo is not None
    names = zipfile.ZipFile(_io.BytesIO(store.get(d["url"].split("presigned.test/", 1)[1]))).namelist()
    assert "content.csv" in names and any(x.startswith("images/deliverable_") and x.endswith(".png") for x in names)

    assert c.get(f"/v1/jobs/{ids['job']}/export/download", params={"artifactType": "images"}).json()["fileName"].endswith(".zip")
    assert c.get(f"/v1/jobs/{ids['job']}/export/download", params={"artifactType": "csv"}).json()["fileName"].endswith(".csv")
    assert names[-1] == f"pixlate_export_{ids['job']}.csv"
    assert c.get(f"/v1/jobs/{ids['job']}/export/download").json()["fileName"].endswith(".zip")  # 기본 zip
    assert c.get(f"/v1/jobs/{ids['job']}/export/download", params={"artifactType": "html"}).status_code == 404

    one = c.post(f"/v1/jobs/{ids['job']}/export", json={"components": ["content.csv"]}).json()
    assert one["artifactType"] == "csv"
    comps = {x["type"]: x for x in c.get(f"/v1/jobs/{ids['job']}/deliverables").json()["components"]}
    assert comps["html"]["isActive"] is False and comps["psd"]["isActive"] is False
    assert comps["images"]["isActive"] is True and comps["csv"]["isActive"] is True


# 수식으로 읽힐 첫 글자 → ' 접두, 그 밖은 원문 그대로(계약 FIN-04 formula injection 방어)
_CSV_CASES = [
    ("=1+1", "'=1+1"), ('=HYPERLINK("http://example.invalid","x")', '\'=HYPERLINK("http://example.invalid","x")'),
    ("+1 202 555 0100", "'+1 202 555 0100"), ("-10% off", "'-10% off"), ("@SUM(A1)", "'@SUM(A1)"),
    ("\t=1+1", "'\t=1+1"), ("\r=1+1", "'\r=1+1"), ("=cmd|' /C calc'!A0", "'=cmd|' /C calc'!A0"),
    ("a, b", "a, b"), ('say "hi"', 'say "hi"'), ("line1\nline2", "line1\nline2"),
    ("건조하고 당기는 피부", "건조하고 당기는 피부"), ("Dry, tight skin", "Dry, tight skin"), ("$29.00", "$29.00"),
    # LF 시작, 전각 수식 문자, 앞 공백(스페이스·NBSP·전각 공백·탭+공백) 뒤 수식 문자 — 2026-10-15 확정 규칙
    ("\n=1+1", "'\n=1+1"), ("＝1+1", "'＝1+1"), ("＋1", "'＋1"), ("－5", "'－5"), ("＠SUM(A1)", "'＠SUM(A1)"),
    (" =1+1", "' =1+1"), (" +1", "' +1"), ("　＝1+1", "'　＝1+1"), ("  @x", "'  @x"),
    # 음수·부호 문구도 문자열로 나간다(수용한 변화)
    ("-5", "'-5"), ("+82 10", "'+82 10"),
    # 그 밖은 원문 그대로 — 중간의 = · 문장 속 -, 공백만, 빈 값
    ("x=1", "x=1"), ("a - b", "a - b"), ("ℹ =1", "ℹ =1"), ("   ", "   "), ("", ""), (None, ""),
]


@pytest.mark.parametrize("raw,want", _CSV_CASES)
def test_csv_cell_neutralizes_formula_prefix_only(raw, want):
    from app.routers.finalize import _csv_cell

    assert _csv_cell(raw) == want


def test_export_csv_cells_neutralized_same_in_csv_and_zip(env, monkeypatch):
    import csv as _csv
    import io as _io
    import zipfile

    from app import s3

    store = env["store"]
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    monkeypatch.setattr(s3, "presigned_get", lambda key, ttl=300, download_name=None: f"https://presigned.test/{key}" if key else None)
    ids, _ = _to_n5(env)
    c = ids["c"]
    assert c.post(f"/v1/jobs/{ids['job']}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()

    blocks = _q("SELECT tb.id FROM text_block tb JOIN section s ON s.id = tb.section_id "
                "WHERE s.job_id = :j AND s.bucket = 'include' ORDER BY tb.section_id, tb.block_order, tb.id", j=ids["job"])
    assert len(blocks) >= 2
    a, b = blocks[0]["id"], blocks[1]["id"]
    _q("UPDATE text_block SET source_ko = :s, trans_1 = :t WHERE id = :b", s="=SUM(1,2)", t='=HYPERLINK("http://example.invalid","x")', b=a)
    _q("UPDATE text_block SET source_ko = :s, trans_1 = :t WHERE id = :b", s="건조하고, \"당기는\"\n피부", t="-10% off", b=b)

    def get(artifact_id):
        d = c.get(f"/v1/jobs/{ids['job']}/exports/{artifact_id}/download").json()
        return store.get(d["url"].split("presigned.test/", 1)[1])

    alone = get(c.post(f"/v1/jobs/{ids['job']}/export", json={"components": ["content.csv"]}).json()["artifactId"])
    zipped = zipfile.ZipFile(_io.BytesIO(get(
        c.post(f"/v1/jobs/{ids['job']}/export", json={"components": ["images", "content.csv"]}).json()["artifactId"]))).read("content.csv")
    assert alone == zipped  # 개별 CSV 와 zip 안 CSV 는 같은 처리·같은 바이트
    assert alone.startswith(b"\xef\xbb\xbf")
    rows = list(_csv.reader(_io.StringIO(alone.decode("utf-8-sig"))))
    assert rows[0] == ["sectionId", "blockOrder", "role", "sourceKo", "trans1"]
    assert rows[1][3:] == ["'=SUM(1,2)", '\'=HYPERLINK("http://example.invalid","x")']
    assert rows[2][3:] == ["건조하고, \"당기는\"\n피부", "'-10% off"]  # 구분자·따옴표·줄바꿈은 원문 그대로
    # DB 원문·번역문은 바꾸지 않는다
    assert _q("SELECT source_ko, trans_1 FROM text_block WHERE id = :b", b=a)[0] == {
        "source_ko": "=SUM(1,2)", "trans_1": '=HYPERLINK("http://example.invalid","x")'}


# ── 최종 렌더 실패 종료·누적 한도(작업 15 · task 272 재발 방지) ─────────────────────────────
# 운영 정리안(C)과 같은 문장: 진행 중 시도가 없고 현재 시도가 한도에 닿은 최종 렌더 실행만 failed 로 닫는다.
CLEANUP_SQL = (
    "UPDATE job_async_task r SET status = 'failed', finished_at = now(), error_code = a.error_code, error_message = a.error_message "
    "FROM job_async_task a "
    "WHERE r.id = :run AND r.job_id = :job AND r.parent_task_id IS NULL AND r.run_kind = 'final_render' AND r.status = 'running' AND r.cancelled_at IS NULL "
    "AND a.id = :att AND a.parent_task_id = r.id AND a.is_current AND a.status = 'failed' AND a.retry_count >= a.max_retry "
    "AND NOT EXISTS (SELECT 1 FROM job_async_task x WHERE x.parent_task_id = r.id AND x.status IN ('pending', 'running')) "
    "RETURNING r.id"
)


class _FinalRenderMode:
    """최종 렌더 섹션 조판을 mode 대로 바꾼다(ok · transient = 재시도 가능 실패 · fatal = 재시도 불가 실패). N5 도달 뒤 설치한다."""

    def __init__(self, monkeypatch):
        from app import typeset
        from app.execution import AdoptionRejected
        from app.flows import downstream

        self.mode = "ok"
        real = downstream._render_section

        def fake(*a, **k):
            if self.mode == "transient":
                raise AdoptionRejected("일시 렌더 실패(시험)", code="RENDER_FAILED", retryable=True)
            if self.mode == "fatal":
                raise typeset.StyleDefaultsUnavailable("승인된 역할별 스타일 기본값을 쓸 수 없다(시험)")
            return real(*a, **k)

        monkeypatch.setattr(downstream, "_render_section", fake)


def _final(job):
    """최종 렌더 대표 실행(오래된 순)과 현재 시도."""
    runs = _q("SELECT id, status, finished_at, error_code FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL "
              "AND run_kind = 'final_render' ORDER BY id", j=job)
    cur = _q("SELECT id, status, error_code, retry_count, max_retry, input_fingerprint FROM job_async_task WHERE job_id = :j "
             "AND stage = 'final_render' AND is_current ORDER BY id DESC LIMIT 1", j=job)
    return runs, (cur[0] if cur else None)


def _final_counts(job):
    return _q("SELECT count(*) FILTER (WHERE parent_task_id IS NULL AND run_kind = 'final_render') AS runs, "
              "count(*) FILTER (WHERE stage = 'final_render') AS attempts FROM job_async_task WHERE job_id = :j", j=job)[0]


def _assert_final_blocked(env, ids):
    """한도 초과·실패 종료 뒤: FIN-01·JOB-06 은 기존 409 RETRY_NOT_ALLOWED, 실행·시도·큐 메시지가 늘지 않고 저장도 막힌다."""
    c, job = ids["c"], ids["job"]
    _, cur = _final(job)
    before = _final_counts(job)
    assert env["q"].messages == []
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    r = c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert _final_counts(job) == before
    assert env["q"].messages == []
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409
    item = [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskId"] == cur["id"]][0]
    assert item["status"] == "failed" and item["retryable"] is False
    assert _step(ids) == ("review", "N6")


def test_final_render_retryable_failures_close_run_at_limit(env, monkeypatch):
    """재시도 가능 실패: 자동 1회 → 사용자 재시도(JOB-06) → 마지막 허용 시도 실패 시 실행 failed. 이후 FIN-01·JOB-06 우회 불가."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    fr.mode = "transient"
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    runs, cur = _final(job)
    # 첫 시도 실패 → 자동 재시도 1회도 실패. 한도(2) 안이라 실행은 running, 사용자 재시도 가능
    assert [r["status"] for r in runs] == ["running"]
    assert (cur["status"], cur["retry_count"], cur["max_retry"]) == ("failed", 1, 2)
    item = [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskId"] == cur["id"]][0]
    assert item["retryable"] is True
    assert c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry").status_code == 200
    env["q"].drain()
    runs, cur = _final(job)
    # 마지막 허용 시도(retry_count 2) 실패 → 실행 failed 로 종료
    assert (cur["status"], cur["retry_count"]) == ("failed", 2)
    assert [(r["status"], r["error_code"]) for r in runs] == [("failed", "RENDER_FAILED")] and runs[0]["finished_at"] is not None
    assert _final_counts(job) == {"runs": 1, "attempts": 3}
    fr.mode = "ok"  # 실패 원인이 사라져도 한도는 초기화되지 않는다
    _assert_final_blocked(env, ids)


def test_final_render_fin01_retry_within_limit_then_limit(env, monkeypatch):
    """FIN-01 로도 한도 안에서는 재시도 시도를 만들고(기존 동작), 한도에 닿으면 실행이 닫혀 FIN-01 이 새 실행을 만들지 않는다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    fr.mode = "transient"
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 202
    env["q"].drain()
    runs, cur = _final(job)
    assert cur["id"] == r.json()["renderTaskId"] and (cur["status"], cur["retry_count"]) == ("failed", 2)
    assert [r["status"] for r in runs] == ["failed"]
    _assert_final_blocked(env, ids)


def test_final_render_non_retryable_failure_keeps_user_retry_until_limit(env, monkeypatch):
    """retryable=false: 자동 재시도는 없다. 한도가 남아 있으면 실행은 running 이고 JOB-06·FIN-01 사용자 재시도가 기존대로 된다.
    마지막 허용 시도까지 실패하면 실행 failed, 이후 우회 불가."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    fr.mode = "fatal"
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    runs, cur = _final(job)
    assert (cur["status"], cur["error_code"], cur["retry_count"]) == ("failed", "STYLE_DEFAULTS_UNAPPROVED", 0)
    assert [r["status"] for r in runs] == ["running"] and runs[0]["finished_at"] is None
    assert _final_counts(job) == {"runs": 1, "attempts": 1} and env["q"].messages == []  # 자동 재시도 없음
    item = [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskId"] == cur["id"]][0]
    assert item["retryable"] is True  # 사용자 재시도 가능 여부는 retryable 과 별개
    assert c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry").status_code == 200  # JOB-06
    env["q"].drain()
    runs, cur = _final(job)
    assert [r["status"] for r in runs] == ["running"] and (cur["status"], cur["retry_count"]) == ("failed", 1)
    r = c.post(f"/v1/jobs/{job}/render")  # FIN-01 = 같은 실행의 재시도 시도
    assert r.status_code == 202
    env["q"].drain()
    runs, cur = _final(job)
    assert cur["id"] == r.json()["renderTaskId"] and (cur["status"], cur["retry_count"]) == ("failed", 2)
    assert [(r["status"], r["error_code"]) for r in runs] == [("failed", "STYLE_DEFAULTS_UNAPPROVED")]
    assert _final_counts(job) == {"runs": 1, "attempts": 3}
    fr.mode = "ok"
    _assert_final_blocked(env, ids)


def test_final_render_non_retryable_failure_user_retry_succeeds(env, monkeypatch):
    """retryable=false 실패 뒤 원인이 사라지면 한도 안의 사용자 재시도로 완료·저장된다(기존 동작 유지)."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    fr.mode = "fatal"
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    _, cur = _final(job)
    fr.mode = "ok"
    assert c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry").status_code == 200
    env["q"].drain()
    runs, cur = _final(job)
    assert [r["status"] for r in runs] == ["done"] and (cur["status"], cur["retry_count"]) == ("done", 1)
    assert c.post(f"/v1/jobs/{job}/save").status_code == 200


def test_final_render_new_run_after_success_is_kept(env, monkeypatch):
    """성공 완료 뒤 FIN-01 은 새 최종 렌더 실행을 만든다(기존 동작). 그 새 실행이 실패로 닫히면 이전 성공으로 우회하지 않는다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    assert [r["status"] for r in _final(job)[0]] == ["done"]
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 202
    env["q"].drain()
    runs, cur = _final(job)
    assert [r["status"] for r in runs] == ["done", "done"] and (cur["status"], cur["retry_count"]) == ("done", 0)
    assert c.post(f"/v1/jobs/{job}/save").status_code == 200
    # 저장 뒤(done/N6)에도 새 렌더는 기존대로 허용된다. 그 새 실행이 한도까지 실패하면 실행 failed, 이전 성공으로 우회 불가
    fr.mode = "fatal"
    for n in range(3):  # 새 실행(0) → 재시도(1·2)
        assert c.post(f"/v1/jobs/{job}/render").status_code == 202
        env["q"].drain()
        assert _final(job)[1]["retry_count"] == n
    assert [r["status"] for r in _final(job)[0]] == ["done", "done", "failed"]
    before = _final_counts(job)
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert _final_counts(job) == before and env["q"].messages == []


def test_legacy_running_final_run_at_limit_task272(env, monkeypatch):
    """수정 전 코드가 남긴 상태(작업 15 · task 272): 실행 running, 현재 시도 failed·retry_count 2/2, 진행 중 시도 없음.
    정리 전에도 FIN-01·JOB-06 은 409. 복구 sweep 은 건드리지 않는다. 운영 정리안 문장으로 실행만 failed 로 닫으면
    진행 중 0 이 되고, 그 뒤에도 FIN-01 이 새 실행을 만들지 못한다. 시도·고정 입력·횟수는 그대로다."""
    from app import recovery

    from app.flows import final

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    fr.mode = "fatal"
    # 수정 전 실패 후속(f5b050a): 자동 재시도 대상이 아니면 실행을 running 으로 둔다. 작업 15 와 같은 순서로 만든다
    # (재시도 불가 실패 → JOB-06 두 번 → 한도 소진). 시도 행의 횟수는 DB 트리거가 막아 직접 고칠 수 없다
    with monkeypatch.context() as m:
        def old_after_failure(self, db, att, code, retryable):
            if retryable and att["retry_count"] < min(1, att["max_retry"]):
                return [execution.create_retry(db, att["id"], "auto")]
            return []

        m.setattr(final.FinalRenderHandler, "after_failure", old_after_failure)
        c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
        env["q"].drain()
        for _ in range(2):
            assert c.post(f"/v1/jobs/{job}/tasks/{_final(job)[1]['id']}/retry").status_code == 200
            env["q"].drain()
    runs, cur = _final(job)
    assert [r["status"] for r in runs] == ["running"] and runs[0]["finished_at"] is None
    assert (cur["status"], cur["error_code"], cur["retry_count"], cur["max_retry"]) == ("failed", "STYLE_DEFAULTS_UNAPPROVED", 2, 2)
    assert _final_counts(job) == {"runs": 1, "attempts": 3}
    run_id, att_id = runs[0]["id"], cur["id"]
    live = "SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND status IN ('pending', 'running')"
    assert _q(live, j=job)[0]["n"] == 1  # 배포 전 상태 확인(state_check)이 세는 행 = 대표 실행
    fr.mode = "ok"
    # 정리 전: FIN-01 은 한도 초과 재시도로 409, JOB-06 도 409. 행·메시지 없음
    before = _final_counts(job)
    r = c.post(f"/v1/jobs/{job}/render")
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert c.post(f"/v1/jobs/{job}/tasks/{att_id}/retry").status_code == 409
    assert _final_counts(job) == before and env["q"].messages == []
    snap = ("SELECT id, status, retry_count, max_retry, attempt_no, is_current, input_fingerprint, input_manifest, error_code "
            "FROM job_async_task WHERE job_id = :j AND stage = 'final_render' ORDER BY id")
    attempts_before = _q(snap, j=job)
    rows = ("SELECT id, status, dispatch_count, lease_epoch, is_current, finished_at FROM job_async_task WHERE job_id = :j "
            "AND (stage = 'final_render' OR run_kind = 'final_render') ORDER BY id")
    rows_before = _q(rows, j=job)
    recovery.sweep()  # DB 전체를 본다 — 같은 모듈의 다른 시험이 남긴 행의 메시지는 비우고, 이 작업의 행만 판정한다
    env["q"].messages.clear()
    assert _q(rows, j=job) == rows_before and _final_counts(job) == before  # 재전송·만료·새 시도 없음
    # 운영 정리(C): 대상이 기대 상태일 때만 1행 바뀐다
    assert _q(CLEANUP_SQL, job=job, run=run_id, att=att_id) == [{"id": run_id}]
    assert _q(CLEANUP_SQL, job=job, run=run_id, att=att_id) == []  # 다시 실행해도 0행(이미 failed)
    assert [(r["status"], r["error_code"]) for r in _final(job)[0]] == [("failed", "STYLE_DEFAULTS_UNAPPROVED")]
    assert _q(live, j=job)[0]["n"] == 0
    assert _q(snap, j=job) == attempts_before  # 시도 행·고정 입력·횟수 그대로
    _assert_final_blocked(env, ids)
    assert _q(snap, j=job) == attempts_before


def test_cleanup_sql_refuses_unexpected_state(env, monkeypatch):
    """정리 문장은 한도 미만이거나 진행 중 시도가 있으면 0행 — 기대와 다르면 바꾸지 않는다(중단 조건)."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    fr.mode = "transient"
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    runs, cur = _final(job)
    assert runs[0]["status"] == "running" and cur["retry_count"] == 1  # 한도 미만
    assert _q(CLEANUP_SQL, job=job, run=runs[0]["id"], att=cur["id"]) == []
    r = c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry")
    assert r.status_code == 200  # 큐에서 꺼내지 않음 → 새 시도 pending
    new_id = r.json()["taskId"]
    assert _q(CLEANUP_SQL, job=job, run=runs[0]["id"], att=cur["id"]) == []  # 현재 시도 아님 + 진행 중 시도 있음
    assert _q(CLEANUP_SQL, job=job, run=runs[0]["id"], att=new_id) == []  # pending 현재 시도
    assert _final(job)[0][0]["status"] == "running"
    env["q"].messages.clear()


def test_final_render_lease_expiry_recovery_respects_limit(env, monkeypatch):
    """복구 경로: 한도가 남은 시도의 권한 만료는 복구 재시도(실행 running), 마지막 허용 시도의 권한 만료는 실행 failed.
    그 뒤 FIN-01·JOB-06 은 409 — 복구로도 한도를 넘지 않는다."""
    from app import recovery

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)

    def crash_current():
        """현재 시도를 워커가 잡은 채 죽은 상태로 만든다(큐 메시지는 버리고, 권한은 만료)."""
        env["q"].messages.clear()
        att = _final(job)[1]["id"]
        assert execution.acquire(att, "crashed-worker") is not None
        _q("UPDATE job_async_task SET lease_expires_at = now() - interval '1 second' WHERE id = :t", t=att)
        recovery.sweep()
        return att

    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    first = crash_current()  # retry_count 0 만료 → 복구 재시도
    runs, cur = _final(job)
    assert _q("SELECT status, error_code FROM job_async_task WHERE id = :t", t=first)[0] == {"status": "failed", "error_code": "LEASE_EXPIRED"}
    assert [r["status"] for r in runs] == ["running"] and (cur["status"], cur["retry_count"]) == ("pending", 1)
    fr.mode = "fatal"
    env["q"].drain()  # retry_count 1 은 재시도 불가 실패 — 한도가 남아 실행 running
    runs, cur = _final(job)
    assert [r["status"] for r in runs] == ["running"] and (cur["status"], cur["retry_count"]) == ("failed", 1)
    assert c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry").status_code == 200  # retry_count 2(마지막 허용)
    last = crash_current()  # 마지막 허용 시도 만료 → 복구 재시도 없이 실행 failed
    runs, cur = _final(job)
    assert cur["id"] == last and (cur["status"], cur["error_code"], cur["retry_count"]) == ("failed", "LEASE_EXPIRED", 2)
    assert [(r["status"], r["error_code"]) for r in runs] == [("failed", "LEASE_EXPIRED")]
    assert _final_counts(job) == {"runs": 1, "attempts": 3}
    env["q"].messages.clear()  # sweep 이 같은 모듈의 다른 시험 잔여 행에 보낸 메시지
    fr.mode = "ok"
    _assert_final_blocked(env, ids)
