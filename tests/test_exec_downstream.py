"""N3 proceed → ④⑤ → ⑥⑦⑧ → ⑨ → N5 → N6 — 실제 PostgreSQL + 메모리 저장소.

④ 라벨·⑧ 번역은 대역, ⑤ 로고·⑦ 스타일·⑥ 마스크 계산은 실제 파이프라인 코드(⑥ 모델만 가짜), ⑥ 은 원격 GPU 경로
(WorkerControl·InProcessControlClient·presigned 대역)로 실행한다. 대역 성공은 실제 AI 통합 성공이 아니다.
"""
from __future__ import annotations

import re
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
    from app import s3

    store = MemoryStore()
    set_store(store)
    # 화면 조회(프리뷰 등)의 presigned URL 도 대역으로 — 실제 서명은 AWS 자격이 있어야 해서 자격 없는 환경(CI)에서 실패한다
    monkeypatch.setattr(s3, "presigned_get", lambda key, ttl=300, download_name=None: f"https://presigned.test/{key}" if key else None)
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
    """F1 작업 15 재현(합성): ⑦ 완료인데 한 블록의 align 만 NULL → 그 항목만 역할별 승인 기본값, 글자색·크기는 측정값 유지.
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
    from pipeline.handoff.style_defaults import load_style_defaults
    role = _q("SELECT role FROM text_block WHERE id = :b", b=blk["id"])[0]["role"]
    assert rb["applied"]["align"] == load_style_defaults().data.pipeline_mapping.align[role]  # 역할별 승인값(pipeline_mapping.align)
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
    m = {"background": {"key": "k", "sha256": None}, "blocks": [], "style_stage": "done", "renderer": typeset.RENDERER_VERSION,
         "style_defaults": {**typeset.style_inputs(), "rule": "role-style-rule/0"}}  # 고정 당시 규칙 버전이 다름
    with pytest.raises(AdoptionRejected) as e:
        downstream._render_section(m, {}, {}, {})
    assert e.value.code == "INPUT_CHANGED" and "스타일" in str(e.value)


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


def _edit_signals(block_id):
    return _q("SELECT before_text, after_text FROM edit_signal WHERE text_block_id = :b ORDER BY id", b=block_id)


def test_block_patch_same_value_is_noop(env):
    """같은 값 저장(계약 blockPatch 「같은 값 저장」): revision·blockStatus·edit_signal·재렌더 그대로, 전체 TextBlock + rerenderTaskId null."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["trans1"]][0]
    cols = "SELECT trans_1, block_status, revision, char_count, updated_at FROM text_block WHERE id = :b"
    n_preview, before_db = _preview_count(job), _q(cols, b=b["id"])
    r = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": b["trans1"], "revision": b["revision"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "rerenderTaskId" in body and body["rerenderTaskId"] is None
    assert body["block"] == b  # 목록(CFM-01)과 같은 전체 TextBlock, 값 그대로
    assert _q(cols, b=b["id"]) == before_db
    assert _edit_signals(b["id"]) == [] and _preview_count(job) == n_preview and env["q"].messages == []
    # 같은 값을 다시 보내도 같다(revision 이 그대로라 연속 no-op 이 가능하다)
    again = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": b["trans1"], "revision": b["revision"]})
    assert again.status_code == 200 and again.json() == body
    # 다른 값 저장은 기존 동작 그대로
    ch = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": b["trans1"] + " now", "revision": b["revision"]})
    assert ch.status_code == 200 and ch.json()["rerenderTaskId"]
    assert (ch.json()["block"]["revision"], ch.json()["block"]["blockStatus"]) == (b["revision"] + 1, "edited")
    assert _edit_signals(b["id"]) == [{"before_text": b["trans1"], "after_text": b["trans1"] + " now"}]


def test_block_patch_same_value_keeps_running_rerender(env):
    """진행 중 재렌더가 있을 때 같은 값 저장: 새 task 없음, 진행 중 시도를 취소·대체하지 않는다(실행 권한 유지)."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["trans1"]][0]
    r1 = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Visibly smoother skin", "revision": b["revision"]}).json()
    t1, rev = r1["rerenderTaskId"], r1["block"]["revision"]
    lease = execution.acquire(t1, "slow-renderer")  # 재렌더 실행 중
    assert lease is not None
    n_preview = _preview_count(job)
    r2 = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Visibly smoother skin", "revision": rev})
    assert r2.status_code == 200 and r2.json()["rerenderTaskId"] is None
    assert r2.json()["block"]["revision"] == rev and _preview_count(job) == n_preview
    st = _q("SELECT status, is_current FROM job_async_task WHERE id = :t", t=t1)[0]
    assert st == {"status": "running", "is_current": True} and execution.heartbeat(lease) is True  # 권한 그대로
    assert len(_edit_signals(b["id"])) == 1  # 첫 수정 기록만


def test_block_patch_same_value_checks_come_first(env):
    """같은 값이어도 revision 불일치는 409 REVISION_CONFLICT, N5 가 아니면 409 INVALID_STATE(기존 검사가 먼저)."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["trans1"]][0]
    stale = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": b["trans1"], "revision": b["revision"] + 7})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert stale.json()["error"]["details"]["current"]["revision"] == b["revision"]
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    late = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": b["trans1"], "revision": b["revision"]})
    assert late.status_code == 409 and late.json()["error"]["code"] == "INVALID_STATE"


def test_block_patch_empty_string_on_null_is_an_edit(env):
    """번역문이 없는(NULL) 칸에 빈 문자열 저장은 같은 값이 아니다 — 기존대로 수정(edited·revision+1·기록·재렌더)."""
    ids, target = _to_n5(env, fail_first=True)
    c, job = ids["c"], ids["job"]
    b = [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["id"] == target][0]
    assert b["trans1"] is None
    r = c.patch(f"/v1/jobs/{job}/blocks/{target}", json={"trans1": "", "revision": b["revision"]})
    assert r.status_code == 200 and r.json()["rerenderTaskId"]
    assert (r.json()["block"]["revision"], r.json()["block"]["blockStatus"], r.json()["block"]["trans1"]) == (b["revision"] + 1, "edited", "")
    assert _edit_signals(target) == [{"before_text": None, "after_text": ""}]


# ---- 저장 공통(계약 blockPatch 「저장 공통」): 멱등 키·처리 순서·오류 구분 ------------------------------------------
def _patch(c, job, bid, body, key=None):
    return c.patch(f"/v1/jobs/{job}/blocks/{bid}", json=body, headers={"Idempotency-Key": key} if key else {})


def _idem_rows(job):
    return _q("SELECT operation, idem_key, status_code FROM edit_idempotency WHERE job_id = :j ORDER BY idem_key", j=job)


def _first_two(c, job):
    bs = [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["trans1"]]
    return bs[0], bs[1]


def test_save_replay_same_request_applies_once(env):
    """같은 키·같은 요청 재전송 = 최초 응답 그대로 + Idempotent-Replayed: true, 두 번 적용하지 않는다(revision 다시 비교 안 함)."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, _ = _first_two(c, job)
    body = {"trans1": "Visibly smoother skin", "revision": b["revision"]}
    r1 = _patch(c, job, b["id"], body, key="k-1")
    assert r1.status_code == 200 and "Idempotent-Replayed" not in r1.headers
    n_preview = _preview_count(job)
    r2 = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json=body, headers={"Idempotency-Key": "k-1", "Origin": "https://fe.example"})
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true" and r2.json() == r1.json()
    assert "idempotent-replayed" in r2.headers.get("access-control-expose-headers", "").lower()  # 교차 출처에서도 읽힌다
    assert _q("SELECT revision FROM text_block WHERE id = :b", b=b["id"])[0]["revision"] == b["revision"] + 1
    assert len(_edit_signals(b["id"])) == 1 and _preview_count(job) == n_preview
    assert _idem_rows(job) == [{"operation": "block_patch", "idem_key": "k-1", "status_code": 200}]


def test_save_same_key_different_body_or_target_is_rejected(env):
    """같은 키로 본문이 다르거나 대상 블록이 다르면 409 DUPLICATE(idempotency_key_reused) — 최초 응답을 재사용하지 않고 아무것도 적용하지 않는다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, other = _first_two(c, job)
    r1 = _patch(c, job, b["id"], {"trans1": "First", "revision": b["revision"]}, key="k-2")
    assert r1.status_code == 200
    diff_body = _patch(c, job, b["id"], {"trans1": "Second", "revision": b["revision"] + 1}, key="k-2")
    diff_target = _patch(c, job, other["id"], {"trans1": "First", "revision": b["revision"]}, key="k-2")
    for r in (diff_body, diff_target):
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "DUPLICATE" and err["details"] == {"reason": "idempotency_key_reused"} and "Idempotent-Replayed" not in r.headers
    assert _q("SELECT trans_1, revision FROM text_block WHERE id = :b", b=b["id"])[0] == {"trans_1": "First", "revision": b["revision"] + 1}
    assert _q("SELECT trans_1, revision FROM text_block WHERE id = :b", b=other["id"])[0] == {"trans_1": other["trans1"], "revision": other["revision"]}
    # 같은 본문이라도 revision 이 다르면 다른 요청이다
    stale_rev = _patch(c, job, b["id"], {"trans1": "First", "revision": b["revision"] + 1}, key="k-2")
    assert stale_rev.status_code == 409 and stale_rev.json()["error"]["code"] == "DUPLICATE"


def test_save_noop_response_is_recorded_and_replayed(env):
    """no-op 200 도 기록한다. 그 뒤 다른 저장으로 값이 바뀌어도 같은 키 재전송은 최초(no-op) 응답을 돌려주고 아무것도 되돌리지 않는다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, _ = _first_two(c, job)
    same = {"trans1": b["trans1"], "revision": b["revision"]}
    r1 = _patch(c, job, b["id"], same, key="k-noop")
    assert r1.status_code == 200 and r1.json()["rerenderTaskId"] is None
    assert _idem_rows(job) == [{"operation": "block_patch", "idem_key": "k-noop", "status_code": 200}]
    assert _patch(c, job, b["id"], {"trans1": "Changed later", "revision": b["revision"]}, key="k-other").status_code == 200
    r2 = _patch(c, job, b["id"], same, key="k-noop")
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true" and r2.json() == r1.json()
    assert _q("SELECT trans_1 FROM text_block WHERE id = :b", b=b["id"])[0]["trans_1"] == "Changed later"


def test_save_concurrent_same_request_applies_once(env):
    """같은 키 동시 요청: 작업 행 잠금으로 직렬화 — 하나만 적용되고 다른 하나는 최초 응답 재전송."""
    from app import db as app_db

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, _ = _first_two(c, job)
    body = {"trans1": "Concurrent", "revision": b["revision"]}
    results = []
    holder = app_db.engine.connect()
    tx = holder.begin()
    holder.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": job})  # 두 요청을 같은 시점에 막아 둔다
    from app.main import app

    def go():
        cl = TestClient(app)  # 스레드마다 따로(같은 판매자 토큰)
        cl.headers["Authorization"] = c.headers["Authorization"]
        results.append(_patch(cl, job, b["id"], body, key="k-c"))

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    threading.Event().wait(0.5)
    tx.commit()
    holder.close()
    for t in ts:
        t.join(timeout=30)
    assert sorted(r.status_code for r in results) == [200, 200]
    assert sorted(r.headers.get("Idempotent-Replayed", "no") for r in results) == ["no", "true"]
    assert results[0].json() == results[1].json()
    assert _q("SELECT revision FROM text_block WHERE id = :b", b=b["id"])[0]["revision"] == b["revision"] + 1
    assert len(_edit_signals(b["id"])) == 1


def test_save_expired_record_is_a_new_request(env):
    """보관 기한이 지난 기록은 없는 것으로 본다 — 같은 키 재전송은 새 요청으로 처리되고 revision 이 두 번째 방어선이 된다."""
    from app import recovery

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, other = _first_two(c, job)
    body = {"trans1": "Applied once", "revision": b["revision"]}
    assert _patch(c, job, b["id"], body, key="k-exp").status_code == 200
    noop = {"trans1": other["trans1"], "revision": other["revision"]}
    assert _patch(c, job, other["id"], noop, key="k-exp-noop").status_code == 200
    _q("UPDATE edit_idempotency SET expires_at = now() - interval '1 second' WHERE job_id = :j", j=job)
    again = _patch(c, job, b["id"], body, key="k-exp")
    assert again.status_code == 409 and again.json()["error"]["code"] == "REVISION_CONFLICT"  # 두 번 적용되지 않음
    noop_again = _patch(c, job, other["id"], noop, key="k-exp-noop")
    assert noop_again.status_code == 200 and "Idempotent-Replayed" not in noop_again.headers and noop_again.json()["rerenderTaskId"] is None
    assert len(_edit_signals(b["id"])) == 1
    # 만료 뒤 새로 처리한 성공 응답(no-op)은 같은 키로 다시 기록된다. 정리 주기는 지난 기록만 지운다
    assert [r["idem_key"] for r in _idem_rows(job)] == ["k-exp", "k-exp-noop"]
    recovery.sweep()
    assert [r["idem_key"] for r in _idem_rows(job)] == ["k-exp-noop"]


def test_save_after_cancel_and_other_seller_do_not_replay(env):
    """재전송도 소유·작업 상태 검사를 건너뛰지 않는다: 다른 판매자 = 404, 취소한 작업 = 409 INVALID_STATE. 취소는 그 작업의 기록을 지운다."""
    from app import db as app_db
    from app.main import app

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, _ = _first_two(c, job)
    body = {"trans1": "Before cancel", "revision": b["revision"]}
    assert _patch(c, job, b["id"], body, key="k-own").status_code == 200
    with app_db.engine.begin() as conn:
        stranger = seed_job(conn)
    sc = TestClient(app)
    sc.headers["Authorization"] = "Bearer " + sc.post("/v1/auth/login", json={"email": stranger["email"], "password": "x"}).json()["accessToken"]
    assert _patch(sc, job, b["id"], body, key="k-own").status_code == 404
    assert c.delete(f"/v1/jobs/{job}").status_code == 200
    assert _idem_rows(job) == []
    late = _patch(c, job, b["id"], body, key="k-own")
    assert late.status_code == 409 and late.json()["error"]["code"] == "INVALID_STATE" and "Idempotent-Replayed" not in late.headers
    assert late.json()["error"]["details"] == {"reason": "job_not_editable"}


def test_save_lock_timeout_returns_retryable_503(env, monkeypatch):
    """작업 행 잠금을 한도 안에 얻지 못하면 트랜잭션을 정리하고 503 SERVICE_UNAVAILABLE(retryable). 같은 요청으로 다시 보내면 한 번만 적용된다."""
    from app import db as app_db

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, _ = _first_two(c, job)
    monkeypatch.setenv("PIXLATE_EDIT_LOCK_TIMEOUT_MS", "200")
    body = {"trans1": "After busy", "revision": b["revision"]}
    holder = app_db.engine.connect()
    tx = holder.begin()
    holder.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": job})
    try:
        busy = _patch(c, job, b["id"], body, key="k-busy")
    finally:
        tx.rollback()
        holder.close()
    assert busy.status_code == 503, busy.text
    assert busy.json()["error"]["code"] == "SERVICE_UNAVAILABLE" and busy.json()["error"]["retryable"] is True
    assert _idem_rows(job) == [] and _edit_signals(b["id"]) == []
    ok = _patch(c, job, b["id"], body, key="k-busy")
    assert ok.status_code == 200 and "Idempotent-Replayed" not in ok.headers
    assert _q("SELECT revision FROM text_block WHERE id = :b", b=b["id"])[0]["revision"] == b["revision"] + 1


_CUR = object()  # 요청에 현재 revision(0)을 넣는 자리


@pytest.mark.parametrize("body,key", [
    ({"revision": _CUR}, "k-fmt"),                              # trans1 누락
    ({"trans1": "x"}, "k-fmt"),                                 # revision 누락
    ({"trans1": None, "revision": _CUR}, "k-fmt"),              # trans1 null
    ({"trans1": 123, "revision": _CUR}, "k-fmt"),               # trans1 숫자 — 문자열로 바꾸지 않는다
    ({"trans1": "x", "revision": False}, "k-fmt"),              # revision false — 0 으로 바꾸지 않는다
    ({"trans1": "x", "revision": "0"}, "k-fmt"),                # revision 문자열 — 0 으로 바꾸지 않는다
    ({"trans1": "x", "revision": 0.5}, "k-fmt"),                # revision 정수가 아닌 수
    ({"trans1": "x", "revision": _CUR, "trans2": "y"}, "k-fmt"),  # 정의되지 않은 키
    ({"trans1": "x", "revision": _CUR}, "has space"),           # 키 형식
    ({"trans1": "x", "revision": _CUR}, "k" * 129),             # 키 길이
])
def test_save_request_format_errors_are_422(env, body, key):
    """요청 형식 오류는 422 VALIDATION_ERROR — 불리언·문자열을 숫자로 바꾸거나 기본값으로 채워 저장하지 않는다.
    블록의 현재 revision 을 0 으로 두어, false·"0" 이 0 으로 변환되면 실제로 저장되는 상황에서 거절을 확인한다(블록·수정 기록·멱등 기록 쓰기 없음)."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, _ = _first_two(c, job)
    _q("UPDATE text_block SET revision = 0 WHERE id = :b", b=b["id"])
    before = _q("SELECT trans_1, revision, block_status, updated_at FROM text_block WHERE id = :b", b=b["id"])
    sent = {k: (0 if v is _CUR else v) for k, v in body.items()}
    r = _patch(c, job, b["id"], sent, key=key)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _q("SELECT trans_1, revision, block_status, updated_at FROM text_block WHERE id = :b", b=b["id"]) == before
    assert _idem_rows(job) == [] and _edit_signals(b["id"]) == []
    # 같은 블록에 올바른 요청(revision 0)은 저장된다 — 위 거절이 revision 불일치 때문이 아니었다
    ok = _patch(c, job, b["id"], {"trans1": "x", "revision": 0}, key="k-ok")
    assert ok.status_code == 200 and ok.json()["block"]["revision"] == 1


def test_save_revision_integer_value_float_is_accepted(env):
    """OpenAPI integer 와 같게: revision 0.0 은 정수 0 이다 — 현재 revision 0 인 블록에 저장되고, 같은 키의 0 과 0.0 은 같은 요청이다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b, _ = _first_two(c, job)
    _q("UPDATE text_block SET revision = 0 WHERE id = :b", b=b["id"])
    r = _patch(c, job, b["id"], {"trans1": "Float rev", "revision": 0.0}, key="k-float")
    assert r.status_code == 200, r.text
    assert (r.json()["block"]["revision"], r.json()["block"]["trans1"]) == (1, "Float rev")
    again = _patch(c, job, b["id"], {"trans1": "Float rev", "revision": 0}, key="k-float")
    assert again.status_code == 200 and again.headers["Idempotent-Replayed"] == "true" and again.json() == r.json()
    assert len(_edit_signals(b["id"])) == 1


# ---- 섹션 제외·복원 PATCH(계약 sectionPatch): revision·같은 상태 no-op·복원 사유·전체 Section·멱등 -----------------
def _spatch(c, job, sid, body, key=None):
    return c.patch(f"/v1/jobs/{job}/sections/{sid}", json=body, headers={"Idempotency-Key": key} if key else {})


def _srow(sid):
    return _q("SELECT bucket, exclusion_reason, excluded_stage, revision, warning_badge, original_verdict, updated_at FROM section WHERE id = :s",
              s=sid)[0]


def _sdetail(c, job, sid):
    d = c.get(f"/v1/jobs/{job}/sections/{sid}").json()
    d.pop("contentFindings", None)
    return d


def _included(ids):
    return [r["id"] for r in _q("SELECT id FROM section WHERE job_id = :j AND bucket = 'include' ORDER BY section_order, id", j=ids["job"])]


def test_section_patch_full_section_revision_and_restore_to_original(env):
    """직접 제외 → 복원: 응답은 상세와 같은 전체 Section, revision 은 실제 변경 때만 +1, 최초 판정이 포함이면 사유 없음. 판정·배지 그대로."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    sid = _included(ids)[0]
    before = _sdetail(c, job, sid)
    assert before["revision"] == 0 and before["bucket"] == "include"
    r = _spatch(c, job, sid, {"action": "exclude", "revision": 0})
    assert r.status_code == 200, r.text
    ex = r.json()
    assert (ex["bucket"], ex["exclusionReason"], ex["excludedStage"], ex["revision"]) == ("exclude", "user_manual", "N3", 1)
    assert ex == _sdetail(c, job, sid)  # 상세와 같은 모양·값(전체 Section)
    listed = c.get(f"/v1/jobs/{job}/sections").json()
    assert [x["revision"] for x in listed["exclude"] if x["id"] == sid] == [1]  # 목록도 revision 을 싣는다
    back = _spatch(c, job, sid, {"action": "restore", "revision": 1}).json()
    assert (back["bucket"], back["exclusionReason"], back["excludedStage"], back["revision"]) == ("include", None, None, 2)
    assert (back["verdicts"], back["warningBadge"]) == (before["verdicts"], before["warningBadge"])


def test_section_restore_reasons_for_auto_and_restored_once(env):
    """자동 제외의 복원 = restored_by_user. 그 섹션(최초 판정 제외)을 다시 직접 제외한 뒤 복원해도 restored_by_user(제외 전 상태)."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    sid = _included(ids)[0]
    _q("UPDATE section SET bucket = 'exclude', exclusion_reason = 'auto_regulatory', excluded_stage = NULL, "
       "original_verdict = jsonb_set(original_verdict, '{bucket}', '\"exclude\"') WHERE id = :s", s=sid)
    r1 = _spatch(c, job, sid, {"action": "restore", "revision": 0}).json()
    assert (r1["bucket"], r1["exclusionReason"], r1["revision"]) == ("include", "restored_by_user", 1)
    _spatch(c, job, sid, {"action": "exclude", "revision": 1})
    r2 = _spatch(c, job, sid, {"action": "restore", "revision": 2}).json()
    assert (r2["bucket"], r2["exclusionReason"], r2["revision"]) == ("include", "restored_by_user", 3)
    assert _srow(sid)["original_verdict"]["bucket"] == "exclude"  # 최초 판정 기록은 그대로


def test_section_same_state_is_noop_and_stale_revision_conflicts_first(env):
    """같은 상태 요청 = 200 변경 없음(revision·사유·단계 그대로). 오래된 revision 은 같은 상태 요청이어도 먼저 409 REVISION_CONFLICT."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    sid = _included(ids)[0]
    _spatch(c, job, sid, {"action": "exclude", "revision": 0})
    row = _srow(sid)
    again = _spatch(c, job, sid, {"action": "exclude", "revision": 1})
    assert again.status_code == 200 and again.json()["revision"] == 1 and _srow(sid) == row
    other = _included(ids)[0]
    inc = _spatch(c, job, other, {"action": "restore", "revision": 0})
    assert inc.status_code == 200 and inc.json()["revision"] == 0 and inc.json()["exclusionReason"] is None
    stale = _spatch(c, job, sid, {"action": "exclude", "revision": 0})  # 같은 상태지만 오래된 revision
    assert stale.status_code == 409, stale.text
    err = stale.json()["error"]
    assert err["code"] == "REVISION_CONFLICT" and err["details"]["current"] == _sdetail(c, job, sid) and _srow(sid) == row


def test_section_restore_without_original_verdict_is_refused(env):
    """PM-R8: 최초 판정 기록이 없는 직접 제외 섹션을 실제로 복원해야 하면 409 restore_basis_missing — 섹션·revision·멱등 성공 기록 쓰기 없음.
    이미 포함된 섹션의 restore 는 근거가 없어도 200 변경 없음."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    a, b = _included(ids)[:2]
    _spatch(c, job, a, {"action": "exclude", "revision": 0})
    for ov in ("NULL", "'{\"schema_version\": \"1\"}'::jsonb"):  # 기록 없음 · bucket 없는 기록
        _q(f"UPDATE section SET original_verdict = {ov} WHERE id = :s", s=a)
        row = _srow(a)
        r = _spatch(c, job, a, {"action": "restore", "revision": 1}, key=f"k-r8-{len(ov)}")
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "INVALID_STATE" and r.json()["error"]["details"] == {"reason": "restore_basis_missing"}
        assert _srow(a) == row and _idem_rows(job) == []
    _q("UPDATE section SET original_verdict = NULL WHERE id = :s", s=b)
    ok = _spatch(c, job, b, {"action": "restore", "revision": 0}, key="k-r8-noop")
    assert ok.status_code == 200 and ok.json()["revision"] == 0 and ok.json()["bucket"] == "include"


def test_section_state_rejections(env):
    """N5 에서 N3 제외 섹션 복원 = 409 n3_excluded_section(같은 상태 판정보다 먼저). N3·N5 가 아니면 409 job_not_editable."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    sid = _included(ids)[0]
    _spatch(c, job, sid, {"action": "exclude", "revision": 0})  # N3 에서 제외
    c.post(f"/v1/jobs/{job}/sections/proceed")
    env["q"].drain()
    assert _step(ids) == ("review", "N5")
    r = _spatch(c, job, sid, {"action": "restore", "revision": 1})
    assert r.status_code == 409 and r.json()["error"]["details"] == {"reason": "n3_excluded_section"}
    assert _srow(sid)["revision"] == 1
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    late = _spatch(c, job, _included(ids)[0], {"action": "exclude", "revision": 0})
    assert late.status_code == 409 and late.json()["error"]["details"] == {"reason": "job_not_editable"}


def test_section_idempotency_replay_conflicts_and_key_needs_revision(env):
    """블록 PATCH 와 같은 멱등: 같은 요청 재전송 = 최초 응답(1회 적용), 같은 키의 다른 action·revision·섹션 = 409 DUPLICATE,
    no-op 응답도 재전송, 키가 있는데 revision 이 없으면 422(쓰기 없음)."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    a, b = _included(ids)[:2]
    r1 = _spatch(c, job, a, {"action": "exclude", "revision": 0}, key="s-1")
    r2 = _spatch(c, job, a, {"action": "exclude", "revision": 0}, key="s-1")
    assert r1.status_code == r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true" and r2.json() == r1.json()
    assert _srow(a)["revision"] == 1
    for body, sid in (({"action": "restore", "revision": 0}, a), ({"action": "exclude", "revision": 1}, a), ({"action": "exclude", "revision": 0}, b)):
        d = _spatch(c, job, sid, body, key="s-1")
        assert d.status_code == 409 and d.json()["error"]["details"] == {"reason": "idempotency_key_reused"}, (body, sid, d.text)
    assert _srow(a)["revision"] == 1 and _srow(b)["revision"] == 0
    n1 = _spatch(c, job, b, {"action": "restore", "revision": 0}, key="s-noop")
    _spatch(c, job, b, {"action": "exclude", "revision": 0}, key="s-other")
    n2 = _spatch(c, job, b, {"action": "restore", "revision": 0}, key="s-noop")
    assert n2.headers["Idempotent-Replayed"] == "true" and n2.json() == n1.json() and _srow(b)["bucket"] == "exclude"
    row = _srow(a)
    nk = _spatch(c, job, a, {"action": "restore"}, key="s-norev")
    assert nk.status_code == 422 and nk.json()["error"]["code"] == "VALIDATION_ERROR" and _srow(a) == row
    assert "s-norev" not in [x["idem_key"] for x in _idem_rows(job)]
    # 키 없는 요청의 revision 생략은 호환 기간 동작 — 충돌 검사 없이 처리된다
    assert _spatch(c, job, a, {"action": "restore"}).status_code == 200


def test_section_expired_key_replay_of_first_request_conflicts(env):
    """만료된 키로 최초 요청을 다시 보내면 새 요청으로 처리되고 revision 이 막는다:
    A(제외, rev 0→1) → B(복원, rev 1→2) → A 기록 만료 → A·rev 0 재전송 = 409 REVISION_CONFLICT, 상태·사유·revision 그대로, 성공 기록 새로 없음."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    sid = _included(ids)[0]
    first = {"action": "exclude", "revision": 0}
    assert _spatch(c, job, sid, first, key="key-A").json()["revision"] == 1
    assert _spatch(c, job, sid, {"action": "restore", "revision": 1}, key="key-B").json()["revision"] == 2
    _q("UPDATE edit_idempotency SET expires_at = now() - interval '1 second' WHERE job_id = :j AND idem_key = 'key-A'", j=job)
    row = _srow(sid)
    rec_before = _q("SELECT idem_key, request_sha256, created_at, expires_at FROM edit_idempotency WHERE job_id = :j ORDER BY idem_key", j=job)
    r = _spatch(c, job, sid, first, key="key-A")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "REVISION_CONFLICT" and "Idempotent-Replayed" not in r.headers
    assert r.json()["error"]["details"]["current"]["revision"] == 2
    after = _srow(sid)
    assert (after["bucket"], after["exclusion_reason"], after["revision"]) == ("include", None, 2) and after == row
    assert _q("SELECT idem_key, request_sha256, created_at, expires_at FROM edit_idempotency WHERE job_id = :j ORDER BY idem_key", j=job) == rec_before


@pytest.mark.parametrize("body", [
    {},                                          # action 누락 — 기본값으로 제외하지 않는다
    {"action": "delete", "revision": 0},         # 잘못된 action
    {"action": "exclude", "revision": 0, "x": 1},  # 정의되지 않은 키
    {"action": "exclude", "revision": False},
    {"action": "exclude", "revision": "0"},
    {"action": "exclude", "revision": 0.5},
    {"action": "exclude", "revision": None},
])
def test_section_request_format_errors_are_422(env, body):
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    sid = _included(ids)[0]
    row = _srow(sid)
    r = _spatch(c, job, sid, body, key="s-fmt")
    assert r.status_code == 422, r.text
    assert _srow(sid) == row and _idem_rows(job) == []


def test_section_revision_integer_float_and_lock_timeout(env, monkeypatch):
    """revision 0.0 은 0 과 같다. 작업 행 잠금을 한도 안에 못 얻으면 503(쓰기 없음), 같은 요청으로 다시 보내면 한 번 적용된다."""
    from app import db as app_db

    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    a, b = _included(ids)[:2]
    assert _spatch(c, job, a, {"action": "exclude", "revision": 0.0}).json()["revision"] == 1
    monkeypatch.setenv("PIXLATE_EDIT_LOCK_TIMEOUT_MS", "200")
    holder = app_db.engine.connect()
    tx = holder.begin()
    holder.execute(text("SELECT id FROM job WHERE id = :j FOR UPDATE"), {"j": job})
    try:
        busy = _spatch(c, job, b, {"action": "exclude", "revision": 0}, key="s-busy")
    finally:
        tx.rollback()
        holder.close()
    assert busy.status_code == 503 and busy.json()["error"]["retryable"] is True
    assert _srow(b)["revision"] == 0 and _idem_rows(job) == []
    ok = _spatch(c, job, b, {"action": "exclude", "revision": 0}, key="s-busy")
    assert ok.status_code == 200 and ok.json()["revision"] == 1 and "Idempotent-Replayed" not in ok.headers


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
    assert rows[0] == ["sectionId", "blockOrder", "role", "sourceKo", "trans1", "layout_x", "layout_y", "layout_w", "layout_h", "font_scale"]
    assert rows[1][3:5] == ["'=SUM(1,2)", '\'=HYPERLINK("http://example.invalid","x")']
    assert rows[2][3:5] == ["건조하고, \"당기는\"\n피부", "'-10% off"]  # 구분자·따옴표·줄바꿈은 원문 그대로
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


# ── 렌더러 버전 호환(실행 시점 검사 + 재시도 생성 전 검사) ───────────────────────────────────
NEW_RENDERER = "pillow-typeset/test-new"


def _attempt(tid):
    return _q("SELECT id, stage, status, error_code, error_message, retry_count, max_retry, input_fingerprint "
              "FROM job_async_task WHERE id = :t", t=tid)[0]


def _item(c, job, tid):
    return [i for i in c.get(f"/v1/jobs/{job}/tasks").json()["items"] if i["taskId"] == tid][0]


def _preview_count(job):
    return _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j AND stage = 'preview'", j=job)[0]["n"]


def test_render_section_checks_renderer_match_mismatch_missing(monkeypatch):
    from app import typeset
    from app.execution import AdoptionRejected
    from app.flows import downstream
    from tests.exec_fakes import png_bytes

    bg = png_bytes(100, 100)
    monkeypatch.setattr(downstream, "get_store", lambda: type("S", (), {"get": staticmethod(lambda k: bg)})())
    base = {"background": {"key": "k", "sha256": None}, "blocks": [], "style_stage": "done", "style_defaults": typeset.style_inputs()}
    assert downstream._render_section({**base, "renderer": typeset.RENDERER_VERSION}, {}, {}, {}).png  # 일치 → 그린다
    for m, why in (({**base, "renderer": "pillow-typeset/0+old"}, "pillow-typeset/0+old"), (base, "기록 없음")):
        with pytest.raises(AdoptionRejected) as e:
            downstream._render_section(m, {}, {}, {})
        assert e.value.code == "INPUT_CHANGED" and e.value.retryable is False and why in str(e.value)


def test_final_render_renderer_mismatch_fails_and_retry_is_rejected_before_creation(env, monkeypatch):
    """고정 입력(옛 렌더러)으로 큐에 있던 최종 렌더 → 실행 시점 INPUT_CHANGED(자동 재시도 없음, 한도가 남아 실행 running).
    JOB-06·FIN-01 은 생성 전 거절(409) — 시도·큐 메시지·retry_count 증가 없음. JOB-05 retryable=false 와 일치."""
    from app import typeset

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})  # 고정 입력 = 현재 렌더러, 큐 대기
    monkeypatch.setattr(typeset, "RENDERER_VERSION", NEW_RENDERER)  # 배포로 렌더러가 바뀐 상황
    env["q"].drain()
    runs, cur = _final(job)
    a = _attempt(cur["id"])
    assert (a["status"], a["error_code"], a["retry_count"]) == ("failed", "INPUT_CHANGED", 0) and "렌더러" in a["error_message"]
    assert [r["status"] for r in runs] == ["running"] and env["q"].messages == []
    assert _item(c, job, cur["id"])["retryable"] is False
    before = _final_counts(job)
    for r in (c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry"), c.post(f"/v1/jobs/{job}/render")):
        assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert _final_counts(job) == before and env["q"].messages == [] and _attempt(cur["id"]) == a
    assert c.post(f"/v1/jobs/{job}/save").status_code == 409


def test_final_render_missing_renderer_is_rejected_like_mismatch(env, monkeypatch):
    from app.flows import final

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    real = final._final_inputs
    monkeypatch.setattr(final, "_final_inputs", lambda db, j: {k: v for k, v in real(db, j).items() if k != "renderer"})
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    env["q"].drain()
    _, cur = _final(job)
    a = _attempt(cur["id"])
    assert (a["status"], a["error_code"]) == ("failed", "INPUT_CHANGED") and "기록 없음" in a["error_message"]
    assert _item(c, job, cur["id"])["retryable"] is False
    assert c.post(f"/v1/jobs/{job}/tasks/{cur['id']}/retry").status_code == 409
    assert _final_counts(job) == {"runs": 1, "attempts": 1}


def test_preview_renderer_mismatch_and_missing(env, monkeypatch):
    """미리보기: 옛 렌더러·기록 없음 → INPUT_CHANGED, JOB-06 생성 전 거절(행·메시지 없음), retryable=false.
    N5 수정(PATCH)은 현재 렌더러로 새 고정 입력을 만들어 성공한다(기존 경로)."""
    from app import typeset
    from app.flows import downstream

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    blk = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b.get("trans1")][0]
    t1 = c.patch(f"/v1/jobs/{job}/blocks/{blk['id']}", json={"trans1": "Gentle care", "revision": blk["revision"]}).json()["rerenderTaskId"]
    monkeypatch.setattr(typeset, "RENDERER_VERSION", NEW_RENDERER)
    from app import tasks

    assert env["q"].messages == [("app.tasks.run_preview", [t1])]
    env["q"].messages.clear()
    tasks.run_preview(t1)  # t1 만 실행하고, 그 뒤 등록되는 시도는 아직 실행하지 않는다
    env["q"].drain(skip={"app.tasks.run_preview"})
    a = _attempt(t1)
    assert (a["status"], a["error_code"]) == ("failed", "INPUT_CHANGED") and "렌더러" in a["error_message"]
    sid = blk["sectionId"]
    # 렌더러가 바뀌어 현재 입력 지문이 달라졌으므로 진행 처리가 현재 렌더러로 새 시도 하나를 등록한다(입력 변경 재렌더 — 재시도 한도 미사용)
    auto = _q("SELECT id, supersedes_task_id, retry_origin, input_manifest FROM job_async_task "
              "WHERE stage = 'preview' AND unit_id = :s AND is_current", s=sid)[0]
    assert (auto["supersedes_task_id"], auto["retry_origin"]) == (t1, "auto") and auto["input_manifest"]["renderer"] == NEW_RENDERER
    n = _preview_count(job)
    r = c.post(f"/v1/jobs/{job}/tasks/{t1}/retry")  # 옛 렌더러 시도는 다시 시도하지 않는다
    assert r.status_code == 409 and r.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert _preview_count(job) == n and env["q"].messages == [] and _attempt(t1) == a
    tasks.run_preview(auto["id"])
    env["q"].drain()
    assert _attempt(auto["id"])["status"] == "done"
    blk = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == blk["id"]][0]
    t2 = c.patch(f"/v1/jobs/{job}/blocks/{blk['id']}", json={"trans1": "Gentle care.", "revision": blk["revision"]}).json()["rerenderTaskId"]
    env["q"].drain()
    assert _attempt(t2)["status"] == "done"  # 새 고정 입력(현재 렌더러)
    real = downstream._render_inputs
    monkeypatch.setattr(downstream, "_render_inputs", lambda db, run_id, sid, **kw: {k: v for k, v in real(db, run_id, sid, **kw).items() if k != "renderer"})
    blk = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["id"] == blk["id"]][0]
    t3 = c.patch(f"/v1/jobs/{job}/blocks/{blk['id']}", json={"trans1": "Gentle care!", "revision": blk["revision"]}).json()["rerenderTaskId"]
    env["q"].drain()
    a3 = _attempt(t3)
    assert (a3["status"], a3["error_code"]) == ("failed", "INPUT_CHANGED") and "기록 없음" in a3["error_message"]
    assert _item(c, job, t3)["retryable"] is False and c.post(f"/v1/jobs/{job}/tasks/{t3}/retry").status_code == 409
    assert _step(ids) == ("review", "N5")


def test_final_auto_retry_is_not_created_for_old_renderer_and_adoption_commits(env, monkeypatch):
    """retryable 실패(자동 재시도 대상)여도 고정 입력 렌더러가 다르면 자동 재시도를 만들지 않는다.
    예외로 채택 트랜잭션이 되돌아가지 않고(인계 adopted·시도 failed 기록), 한도가 남아 실행은 running."""
    from app import typeset

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    fr = _FinalRenderMode(monkeypatch)
    c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
    monkeypatch.setattr(typeset, "RENDERER_VERSION", NEW_RENDERER)
    fr.mode = "transient"  # 렌더러 검사 전에 일어나는 재시도 가능 실패
    env["q"].drain()
    runs, cur = _final(job)
    assert (cur["status"], cur["error_code"], cur["retry_count"]) == ("failed", "RENDER_FAILED", 0)
    assert _final_counts(job) == {"runs": 1, "attempts": 1} and env["q"].messages == []  # 자동 재시도 없음
    assert [r["status"] for r in runs] == ["running"]
    assert _q("SELECT state FROM task_handoff WHERE task_id = :t", t=cur["id"]) == [{"state": "adopted"}]
    assert _item(c, job, cur["id"])["retryable"] is False


def test_recovery_lease_expiry_skips_old_renderer_and_keeps_other_job(env, monkeypatch):
    """권한 만료 복구: 옛 렌더러 시도는 failed(LEASE_EXPIRED)만 기록하고 복구 재시도를 만들지 않는다(실행 running, 한도 남음).
    같은 sweep 의 다른 작업(현재 렌더러)은 기존대로 복구 재시도. 다음 sweep 이 같은 대상을 다시 처리하지 않는다."""
    from app import recovery, typeset

    a_ids, _ = _to_n5(env)
    b_ids, _ = _to_n5(env)

    def crash(c, job):
        c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []})
        env["q"].messages.clear()
        att = _final(job)[1]["id"]
        assert execution.acquire(att, "crashed-worker") is not None
        _q("UPDATE job_async_task SET lease_expires_at = now() - interval '1 second' WHERE id = :t", t=att)
        return att

    a_att = crash(a_ids["c"], a_ids["job"])  # 옛 렌더러로 고정
    monkeypatch.setattr(typeset, "RENDERER_VERSION", NEW_RENDERER)
    b_att = crash(b_ids["c"], b_ids["job"])  # 현재 렌더러로 고정
    recovery.sweep()
    a_runs, a_cur = _final(a_ids["job"])
    assert a_cur["id"] == a_att and (a_cur["status"], a_cur["error_code"]) == ("failed", "LEASE_EXPIRED")
    assert [r["status"] for r in a_runs] == ["running"] and _final_counts(a_ids["job"]) == {"runs": 1, "attempts": 1}
    b_runs, b_cur = _final(b_ids["job"])
    assert _attempt(b_att)["error_code"] == "LEASE_EXPIRED" and (b_cur["status"], b_cur["retry_count"]) == ("pending", 1)
    assert [r["status"] for r in b_runs] == ["running"]
    a_row = _q("SELECT status, error_code, lease_epoch, finished_at FROM job_async_task WHERE id = :t", t=a_att)
    recovery.sweep()
    assert _q("SELECT status, error_code, lease_epoch, finished_at FROM job_async_task WHERE id = :t", t=a_att) == a_row
    assert _final_counts(a_ids["job"]) == {"runs": 1, "attempts": 1}
    assert _item(a_ids["c"], a_ids["job"], a_att)["retryable"] is False
    env["q"].messages.clear()


# ── /preview 공통 렌더 입력·지문·시도/결과(계약 ReviewPreview — PRD v3.5.10 BE 계약안) · N5 복원 렌더 확보 ─────────────────
STYLE8 = {"fontFamily", "fontWeight", "fontSizePx", "fontScale", "fontColor", "align", "letterSpacingEm", "lineHeightRatio"}


def _pv(c, job):
    r = c.get(f"/v1/jobs/{job}/preview")
    assert r.status_code == 200, r.text
    return r.json()


def _pvs(c, job, sid):
    return [s for s in _pv(c, job)["sections"] if s["id"] == sid][0]


def _input_state(s):
    """FE 11-05 확정 조합: 제외 = 네 필드 null / 정상 = 입력·지문 있음·오류 null / 오류 = 입력·지문 null·오류 있음 / 준비 중 = 셋 다 null.
    일부만 있는 조합은 정상 응답이 아니다. 포함 섹션의 render 는 입력 상태와 상관없이 항상 있다."""
    ri, fp, er = s["renderInput"], s["inputFingerprint"], s["inputError"]
    if s["bucket"] != "include":
        assert (ri, fp, er, s["render"]) == (None, None, None, None), s
        return "excluded"
    assert s["render"] is not None and set(s["render"]) == {"attempt", "result", "isCurrent"}, s
    r = s["render"]
    assert r["isCurrent"] == (r["result"] is not None and fp is not None and r["result"]["inputFingerprint"] == fp)
    assert (s["renderedUrl"] is None) == (r["result"] is None)
    if r["result"] is not None and not r["isCurrent"]:
        assert r["result"]["overflow"] is None  # 최신이 아닌 결과의 넘침은 내지 않는다
    if ri is not None and fp is not None and er is None:
        return "ready"
    if ri is None and fp is None and er is not None:
        return "error"
    assert (ri, fp, er) == (None, None, None), f"일부 필드만 있는 조합: {s}"
    return "preparing"


def _cur_preview(sid):
    return _q("SELECT id, status, error_code, retry_count, retry_origin, supersedes_task_id, input_fingerprint FROM job_async_task "
              "WHERE stage = 'preview' AND unit_id = :s AND is_current", s=sid)[0]


def _image_key(sid):
    return _q("SELECT render_image_key FROM section WHERE id = :s", s=sid)[0]["render_image_key"]


def _text_block(c, job):
    return [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["trans1"]][0]


def _db_marks(job):
    return _q("SELECT (SELECT count(*) FROM job_async_task WHERE job_id = :j) AS tasks, (SELECT count(*) FROM task_handoff WHERE job_id = :j) AS h, "
              "(SELECT max(updated_at) FROM section WHERE job_id = :j) AS s, (SELECT max(tb.updated_at) FROM text_block tb "
              "JOIN section s ON s.id = tb.section_id WHERE s.job_id = :j) AS b, (SELECT updated_at FROM job WHERE id = :j) AS jb", j=job)[0]


def _render_boom(*a, **k):
    from app import typeset

    raise typeset.StyleDefaultsUnavailable("시험: 렌더 실패")


def test_preview_render_input_fingerprint_current_result_and_blocks(env):
    """포함 섹션: 공통 렌더 입력(style 8키·저장 박스·번역문·revision), 지문 = 현재 ⑨ 시도의 고정 입력 지문, 현재 시도·표시 결과·최신,
    넘침은 결과의 측정값. /blocks sectionInputFingerprint 가 같은 계산. 조회는 쓰지 않는다. 렌더러 = 현행 Pillow·번들 없음."""
    from app import typeset

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    marks = _db_marks(job)
    pv = _pv(c, job)
    assert _db_marks(job) == marks  # 조회만 — 시도·인계·섹션·블록·작업 행이 바뀌지 않는다
    assert pv["renderer"] == {"rendererId": typeset.RENDERER_VERSION, "bundleId": None}
    assert re.fullmatch(r"pillow-typeset/2\+pillow-\S+", pv["renderer"]["rendererId"])  # 현행 문자열(FE는 파싱하지 않음)
    blocks = c.get(f"/v1/jobs/{job}/blocks").json()
    assert pv["sections"]
    for s in pv["sections"]:
        cur = _cur_preview(s["id"])
        assert _input_state(s) == "ready"
        assert s["bucket"] == "include" and s["inputError"] is None and s["revision"] == 0
        assert s["inputFingerprint"] == cur["input_fingerprint"]
        r = s["render"]
        assert r["attempt"] == {"taskId": cur["id"], "status": "done", "errorCode": None, "retryable": False}
        assert r["isCurrent"] is True
        assert (r["result"]["taskId"], r["result"]["inputFingerprint"], r["result"]["rendererId"]) == (
            cur["id"], cur["input_fingerprint"], typeset.RENDERER_VERSION)
        assert s["renderedUrl"] == f"https://presigned.test/{_image_key(s['id'])}"
        ri = s["renderInput"]
        assert ri["section"]["id"] == s["id"] and ri["section"]["height"] == s["height"]
        assert ri["section"]["background"]["source"] in ("inpaint", "original")
        assert ri["section"]["background"]["url"].startswith("https://presigned.test/")
        want = [b for b in blocks if b["sectionId"] == s["id"] and b["isExcluded"] is False and (b["trans1"] or "").strip()
                and (b["sourceKo"] or "").strip()]
        assert [(x["blockId"], x["revision"], x["text"]) for x in ri["blocks"]] == [(b["id"], b["revision"], b["trans1"]) for b in want]
        assert set(r["result"]["overflow"]) == {str(b["id"]) for b in want}
        for x in ri["blocks"]:
            st = x["style"]
            assert set(st) == STYLE8 and st["fontScale"] == 1.0 and st["fontFamily"] == "Pretendard" and st["lineHeightRatio"] == 1.2
            assert st["fontWeight"] in (400, 600) and st["fontSizePx"] > 0 and st["align"] in ("left", "center", "right")
            assert re.fullmatch(r"#[0-9A-F]{6}", st["fontColor"])  # 대문자 #RRGGBB(FE 11-05)
            assert set(x["box"]) == {"x", "y", "w", "h"}
        assert {b["sectionInputFingerprint"] for b in blocks if b["sectionId"] == s["id"]} == {s["inputFingerprint"]}


def test_preview_new_attempt_pending_then_failed_keeps_adopted_previous_result(env, monkeypatch):
    """저장 직후: 현재 시도 = 새 시도(pending), 표시 결과 = 이전 채택 결과(최신 아님·넘침 null), 이미지 그대로, 지문 = 새 시도 입력.
    새 시도가 실패해도 이전 성공 이미지를 결과로 유지한다."""
    from app.flows import downstream

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    old = _pvs(c, job, b["sectionId"])
    t = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Edited text", "revision": b["revision"]}).json()["rerenderTaskId"]
    s = _pvs(c, job, b["sectionId"])
    assert (s["render"]["attempt"]["taskId"], s["render"]["attempt"]["status"]) == (t, "pending")
    assert s["render"]["result"] == {**old["render"]["result"], "overflow": None} and s["render"]["isCurrent"] is False
    assert s["renderedUrl"] == old["renderedUrl"]
    assert s["inputFingerprint"] == _attempt(t)["input_fingerprint"] != old["inputFingerprint"]
    assert [x["text"] for x in s["renderInput"]["blocks"] if x["blockId"] == b["id"]] == ["Edited text"]
    monkeypatch.setattr(downstream, "_render_section", _render_boom)
    env["q"].drain()
    s = _pvs(c, job, b["sectionId"])
    att = s["render"]["attempt"]
    assert (att["taskId"], att["status"], att["errorCode"]) == (t, "failed", "STYLE_DEFAULTS_UNAPPROVED")
    assert s["render"]["result"]["taskId"] == old["render"]["result"]["taskId"] and s["render"]["isCurrent"] is False
    assert s["render"]["result"]["overflow"] is None and s["renderedUrl"] == old["renderedUrl"]


def test_preview_result_is_the_adopted_record_of_the_current_image(env, monkeypatch):
    """표시 결과는 지금 이미지 키를 만든 채택 기록이다 — 최신 성공 시도가 아니다. 채택되지 않은 성공 보고·모르는 키·
    현재 분석에 연결되지 않은 하류 실행이면 근거 없음(result·renderedUrl null). 현재 시도는 따로 그대로 낸다."""
    from app import execution as ex
    from app.flows import preview_view

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    first = _pvs(c, job, sid)["render"]["result"]["taskId"]
    first_key = _image_key(sid)
    t = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Second text", "revision": b["revision"]}).json()["rerenderTaskId"]
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["result"]["taskId"] == t and s["render"]["isCurrent"] is True
    second_key = _image_key(sid)
    assert second_key != first_key
    # (1) 이미지 키가 이전 채택 결과를 가리키면 그 기록(최신 성공 t 가 아니다) — 최신 아님, 넘침 null
    _q("UPDATE section SET render_image_key = :k WHERE id = :s", k=first_key, s=sid)
    s = _pvs(c, job, sid)
    assert s["render"]["result"]["taskId"] == first and s["render"]["isCurrent"] is False and s["render"]["result"]["overflow"] is None
    assert s["renderedUrl"] == f"https://presigned.test/{first_key}" and s["render"]["attempt"]["taskId"] == t
    # (2) 채택되지 않은 성공 보고(인계 verified, 산출물 verified)의 키 → 근거 없음
    a = _attempt(t)
    hid = _q("INSERT INTO task_handoff (task_id, job_id, lease_epoch, contract_version, impl_version, input_fingerprint, outcome, target_count, "
             "payload, manifest_sha256, state) VALUES (:t, :j, 999, 'test', 'pillow-typeset/test', :fp, 'done', 0, '{}'::jsonb, :fp, 'verified') "
             "RETURNING id", t=t, j=job, fp=a["input_fingerprint"])[0]["id"]
    _q("INSERT INTO task_artifact (handoff_id, job_id, kind, part_key, verified_key, byte_size, sha256, media_type, width, height, state) "
       "VALUES (:h, :j, 'render_image', :pk, 'verified/test/unadopted.png', 1, :sha, 'image/png', 1, 1, 'verified')",
       h=hid, j=job, pk=f"sec_{sid}", sha="0" * 64)
    for key in ("verified/test/unadopted.png", "legacy/before-execution-records.png"):  # (3) 실행 기록에 없는 키
        _q("UPDATE section SET render_image_key = :k WHERE id = :s", k=key, s=sid)
        s = _pvs(c, job, sid)
        assert s["render"]["result"] is None and s["renderedUrl"] is None and s["render"]["isCurrent"] is False, key
        assert s["render"]["attempt"]["taskId"] == t and s["inputFingerprint"] == a["input_fingerprint"]
    # (4) 현재 분석에 연결되지 않은 하류 실행(이전 실행) → 그 실행의 결과·입력을 쓰지 않는다
    _q("UPDATE section SET render_image_key = :k WHERE id = :s", k=second_key, s=sid)
    assert _pvs(c, job, sid)["render"]["result"]["taskId"] == t
    real = ex.latest_run

    def other_analysis(db, job_id, kind):
        run = real(db, job_id, kind)
        return {**run, "run_scope": {**run["run_scope"], "analysis_task_id": -1}} if run else run

    monkeypatch.setattr(preview_view.execution, "latest_run", other_analysis)
    s = _pvs(c, job, sid)
    assert s["render"] == {"attempt": None, "result": None, "isCurrent": False} and s["renderedUrl"] is None
    assert s["renderInput"] is None and s["inputFingerprint"] is None and s["inputError"] is None


def test_preview_excluded_section_nulls_and_zero_target_blocks(env):
    """N5 제외 섹션 = renderInput·inputFingerprint·render null(renderedUrl 현행 그대로). 렌더 대상 블록 0개 = blocks [] · 넘침 {}."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    secs = [s["id"] for s in _pv(c, job)["sections"]]
    assert len(secs) >= 2
    a, z = secs[0], secs[1]
    url = _pvs(c, job, a)["renderedUrl"]
    assert _spatch(c, job, a, {"action": "exclude", "revision": 0}).status_code == 200
    s = _pvs(c, job, a)
    assert _input_state(s) == "excluded"
    assert (s["bucket"], s["revision"], s["renderInput"], s["inputFingerprint"], s["inputError"], s["render"]) == (
        "exclude", 1, None, None, None, None)
    assert s["renderedUrl"] == url
    assert {b["sectionInputFingerprint"] for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["sectionId"] == a} == {None}
    for b in [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["sectionId"] == z and x["trans1"]]:
        assert c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "", "revision": b["revision"]}).status_code == 200
    env["q"].drain()
    s = _pvs(c, job, z)
    assert s["renderInput"]["blocks"] == [] and s["render"]["isCurrent"] is True and s["render"]["result"]["overflow"] == {}


def test_preview_input_errors_are_classified_without_inventing_input(env, monkeypatch, tmp_path):
    """입력 구성 실패: renderInput·inputFingerprint null, inputError 3코드(박스 → RENDER_INPUT_INVALID, ⑦ 결과 계약 위반 →
    STYLE_RESULT_INVALID, 승인 자료 사용 불가 → STYLE_DEFAULTS_UNAPPROVED). 이전 결과는 최신 아님으로 남는다."""
    import json

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    prev = _pvs(c, job, sid)
    w = prev["renderInput"]["section"]["width"]
    for bad in ({"x": -5, "y": 0, "w": 10, "h": 10}, {"x": 0, "y": 0, "w": 0, "h": 10}, {"x": w - 5, "y": 0, "w": 10, "h": 10}):
        _q("UPDATE text_block SET layout_adjust = CAST(:a AS jsonb) WHERE id = :b",
           a=json.dumps({"schema_version": 2, "user": {"layout_bbox": bad, "font_scale": None}, "system": None}), b=b["id"])
        s = _pvs(c, job, sid)
        assert _input_state(s) == "error"
        assert s["inputError"] == {"code": "RENDER_INPUT_INVALID", "blockIds": [b["id"]]}, bad
        assert s["renderInput"] is None and s["inputFingerprint"] is None
        assert s["render"]["result"]["taskId"] == prev["render"]["result"]["taskId"] and s["render"]["isCurrent"] is False
        assert s["render"]["result"]["overflow"] is None and s["renderedUrl"] == prev["renderedUrl"]
        assert {x["sectionInputFingerprint"] for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["sectionId"] == sid} == {None}
    _q("UPDATE text_block SET layout_adjust = NULL WHERE id = :b", b=b["id"])
    assert _pvs(c, job, sid)["inputFingerprint"] == prev["inputFingerprint"]
    style = _q("SELECT style FROM text_block WHERE id = :b", b=b["id"])[0]["style"]
    _q("UPDATE text_block SET style = CAST(:s AS jsonb) WHERE id = :b",
       s=json.dumps({"font_color": "red", "bg_color": None, "est_font_px": None, "align": None}), b=b["id"])
    s = _pvs(c, job, sid)
    assert s["inputError"] == {"code": "STYLE_RESULT_INVALID", "blockIds": [b["id"]]} and s["renderInput"] is None
    # 소문자 색은 고쳐 쓰지 않는다 — 대문자 #RRGGBB 만 정상 입력(FE 11-05)
    _q("UPDATE text_block SET style = CAST(:s AS jsonb) WHERE id = :b",
       s=json.dumps({"font_color": "#ff0000", "bg_color": None, "est_font_px": None, "align": None}), b=b["id"])
    s = _pvs(c, job, sid)
    assert _input_state(s) == "error" and s["inputError"] == {"code": "STYLE_RESULT_INVALID", "blockIds": [b["id"]]}
    _q("UPDATE text_block SET style = CAST(:s AS jsonb) WHERE id = :b", s=json.dumps(style) if style is not None else None, b=b["id"])
    assert _pvs(c, job, sid)["inputFingerprint"] == prev["inputFingerprint"]
    monkeypatch.setenv("PIXLATE_STYLE_DEFAULTS", str(tmp_path / "missing-style-defaults.json"))
    for s in _pv(c, job)["sections"]:
        assert s["inputError"] == {"code": "STYLE_DEFAULTS_UNAPPROVED", "blockIds": []} and s["inputFingerprint"] is None
        assert s["render"]["isCurrent"] is False and s["render"]["result"] is not None


def test_render_input_box_number_forms_and_fingerprint_compat(env):
    """지문 직렬화(JCS): 10 과 10.0 은 같은 "10" — 정수 좌표를 소수 표기로 저장해도 지문·최신성이 그대로다. 소수 좌표는 절삭하지 않아
    지문이 바뀌고(이전 절삭 방식이면 바뀌지 않아 최신으로 잘못 보였다), 새 시도가 소수 박스 그대로 그려 채택된다."""
    from app import db as app_db
    from app.flows import downstream
    from app.manifest import canonical_json, fingerprint

    base = {"box": {"x": 10, "y": 20, "w": 30, "h": 40}}
    floats = {"box": {"x": 10.0, "y": 20.0, "w": 30.0, "h": 40.0}}
    assert canonical_json(floats) == canonical_json(base) == '{"box":{"h":40,"w":30,"x":10,"y":20}}'
    assert fingerprint(floats) == fingerprint(base) != fingerprint({"box": {**base["box"], "w": 29.5}})

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    s0 = _pvs(c, job, sid)
    assert s0["render"]["isCurrent"] is True
    bb = _q("SELECT bbox FROM text_block WHERE id = :b", b=b["id"])[0]["bbox"]
    assert all(isinstance(bb[k], int) for k in ("x", "y", "w", "h"))
    _q("UPDATE text_block SET bbox = jsonb_build_object('x', (bbox->>'x')::numeric + 0.0, 'y', (bbox->>'y')::numeric + 0.0, "
       "'w', (bbox->>'w')::numeric + 0.0, 'h', (bbox->>'h')::numeric + 0.0) WHERE id = :b", b=b["id"])
    stored = _q("SELECT bbox::text AS t, bbox FROM text_block WHERE id = :b", b=b["id"])[0]
    assert ".0" in stored["t"] and all(isinstance(stored["bbox"][k], float) for k in ("x", "y", "w", "h"))  # 실제 저장·읽기 표현이 10.0
    s1 = _pvs(c, job, sid)
    assert s1["inputFingerprint"] == s0["inputFingerprint"] and s1["render"]["isCurrent"] is True
    # 절삭하면 원래 정수로 돌아가는 소수(x + 0.5) — 절삭 방식은 이 변경을 지문에서 놓친다
    _q("UPDATE text_block SET bbox = jsonb_set(bbox, '{x}', to_jsonb((bbox->>'x')::numeric + 0.5)) WHERE id = :b", b=b["id"])
    s2 = _pvs(c, job, sid)
    assert s2["inputError"] is None, s2["inputError"]
    assert s2["inputFingerprint"] != s0["inputFingerprint"] and s2["render"]["isCurrent"] is False
    assert [x["box"]["x"] for x in s2["renderInput"]["blocks"] if x["blockId"] == b["id"]] == [bb["x"] + 0.5]
    run = _q("SELECT parent_task_id FROM job_async_task WHERE id = :t", t=_cur_preview(sid)["id"])[0]["parent_task_id"]
    with app_db.SessionLocal() as db:  # 이전 절삭 방식이었다면 지문이 그대로라 이전 결과를 최신으로 보였다
        m = downstream._render_inputs(db, run, sid)
        trunc = {**m, "blocks": [{**x, "box": {k: int(v) for k, v in x["box"].items()}} for x in m["blocks"]]}
        assert fingerprint(m) == s2["inputFingerprint"] and fingerprint(trunc) == s0["inputFingerprint"]
    t = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Float box", "revision": b["revision"]}).json()["rerenderTaskId"]
    env["q"].drain()
    a = _attempt(t)
    assert a["status"] == "done"
    man = _q("SELECT input_manifest FROM job_async_task WHERE id = :t", t=t)[0]["input_manifest"]
    assert [x["box"]["x"] for x in man["blocks"] if x["block_id"] == b["id"]] == [bb["x"] + 0.5]
    s3_ = _pvs(c, job, sid)
    assert s3_["render"]["isCurrent"] is True and s3_["inputFingerprint"] == a["input_fingerprint"] == s3_["render"]["result"]["inputFingerprint"]


def test_preview_is_one_snapshot_while_save_and_adoption_commit(env, monkeypatch):
    """조회 도중 다른 연결에서 저장(번역문·revision)과 이미지 교체가 커밋돼도 그 응답은 조회 시작 시점 값 하나로 만든다."""
    from app import db as app_db
    from app.flows import preview_view

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    before = _pvs(c, job, sid)
    real = preview_view.section_input
    fired = []

    def mid(ctx, s):
        if not fired:
            fired.append(s)
            with app_db.engine.begin() as other:
                other.execute(text("UPDATE text_block SET trans_1 = 'Changed meanwhile', revision = revision + 1 WHERE id = :b"), {"b": b["id"]})
                other.execute(text("UPDATE section SET render_image_key = 'verified/test/meanwhile.png' WHERE id = :s"), {"s": sid})
        return real(ctx, s)

    monkeypatch.setattr(preview_view, "section_input", mid)
    assert _pvs(c, job, sid) == before and fired
    after = _pvs(c, job, sid)  # 다음 조회에는 반영된다
    assert after["inputFingerprint"] != before["inputFingerprint"] and after["render"]["result"] is None and after["renderedUrl"] is None


def test_preview_during_n4_input_preparation_is_not_a_style_error(env):
    """N4: ⑥⑦⑧ 이 끝나지 않아 입력을 만들 수 없는 섹션 = 입력 준비 중(세 필드 모두 null, FE 11-05 확정). 스타일 오류로 내지 않는다."""
    ids = _to_n3(env)
    c, job = ids["c"], ids["job"]
    assert c.post(f"/v1/jobs/{job}/sections/proceed").status_code == 202
    for s in _pv(c, job)["sections"]:
        assert _input_state(s) == "preparing"
        assert (s["renderInput"], s["inputFingerprint"], s["inputError"]) == (None, None, None)
        assert s["render"] == {"attempt": None, "result": None, "isCurrent": False} and s["renderedUrl"] is None
    assert {b["sectionInputFingerprint"] for b in c.get(f"/v1/jobs/{job}/blocks").json()} == {None}


def test_n5_restore_reuses_valid_attempt_and_preserves_same_input_failure(env, monkeypatch):
    """N5 복원: 현재 입력과 지문이 같은 시도(완료·최종 실패·대기)는 재사용 — 새 시도·전송 없음, 실패 상태·재시도 횟수 보존."""
    from app.flows import downstream

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    cur0, n0 = _cur_preview(sid), _preview_count(job)
    assert _spatch(c, job, sid, {"action": "exclude", "revision": 0}).status_code == 200
    assert _spatch(c, job, sid, {"action": "restore", "revision": 1}).status_code == 200
    assert (_preview_count(job), _cur_preview(sid), env["q"].messages) == (n0, cur0, [])
    real_render = downstream._render_section
    monkeypatch.setattr(downstream, "_render_section", _render_boom)
    t = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Fails twice", "revision": b["revision"]}).json()["rerenderTaskId"]
    env["q"].drain()
    r = c.post(f"/v1/jobs/{job}/tasks/{t}/retry")
    assert r.status_code in (200, 202), r.text
    env["q"].drain()
    failed = _cur_preview(sid)
    assert failed["status"] == "failed" and failed["retry_count"] == 1
    n1 = _preview_count(job)
    assert _spatch(c, job, sid, {"action": "exclude", "revision": 2}).status_code == 200
    assert _spatch(c, job, sid, {"action": "restore", "revision": 3}).status_code == 200
    assert (_preview_count(job), _cur_preview(sid), env["q"].messages) == (n1, failed, [])
    monkeypatch.setattr(downstream, "_render_section", real_render)
    b = [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["id"] == b["id"]][0]
    t2 = c.patch(f"/v1/jobs/{job}/blocks/{b['id']}", json={"trans1": "Pending now", "revision": b["revision"]}).json()["rerenderTaskId"]
    sent = list(env["q"].messages)
    n2 = _preview_count(job)
    assert _spatch(c, job, sid, {"action": "exclude", "revision": 4}).status_code == 200
    assert _spatch(c, job, sid, {"action": "restore", "revision": 5}).status_code == 200
    assert _preview_count(job) == n2 and _cur_preview(sid)["id"] == t2 and _cur_preview(sid)["status"] == "pending"
    assert env["q"].messages == sent
    env["q"].drain()
    assert _pvs(c, job, sid)["render"]["isCurrent"] is True


def test_n5_restore_creates_attempt_only_when_input_changed_not_on_replay_or_rejection(env):
    """제외 중 입력이 바뀐 섹션의 복원 = 입력 변경 재렌더 1건(이전 시도 대체·재시도 횟수 그대로·커밋 뒤 전송).
    거절(오래된 revision·복원 근거 없음)과 같은 요청 재전송은 시도를 만들지 않는다."""
    import json

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    assert _spatch(c, job, sid, {"action": "exclude", "revision": 0}).status_code == 200
    # 시도를 만들지 않는 입력 변경(번역 재시도 성공과 같은 효과)
    _q("UPDATE text_block SET trans_1 = 'Changed while excluded', revision = revision + 1 WHERE id = :b", b=b["id"])
    cur0, n0 = _cur_preview(sid), _preview_count(job)
    stale = _spatch(c, job, sid, {"action": "restore", "revision": 0}, key="rs-stale")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    ov = _srow(sid)["original_verdict"]
    _q("UPDATE section SET original_verdict = NULL WHERE id = :s", s=sid)
    nb = _spatch(c, job, sid, {"action": "restore", "revision": 1}, key="rs-nobasis")
    assert nb.status_code == 409 and nb.json()["error"]["details"] == {"reason": "restore_basis_missing"}
    _q("UPDATE section SET original_verdict = CAST(:v AS jsonb) WHERE id = :s", v=json.dumps(ov), s=sid)
    assert (_preview_count(job), _cur_preview(sid), env["q"].messages) == (n0, cur0, [])
    r1 = _spatch(c, job, sid, {"action": "restore", "revision": 1}, key="rs-1")
    assert r1.status_code == 200, r1.text
    cur1 = _cur_preview(sid)
    assert _preview_count(job) == n0 + 1 and cur1["status"] == "pending"
    assert (cur1["supersedes_task_id"], cur1["retry_count"], cur1["retry_origin"]) == (cur0["id"], cur0["retry_count"], "user")
    assert env["q"].messages == [("app.tasks.run_preview", [cur1["id"]])]
    r2 = _spatch(c, job, sid, {"action": "restore", "revision": 1}, key="rs-1")
    assert r2.headers["Idempotent-Replayed"] == "true" and r2.json() == r1.json()
    assert _preview_count(job) == n0 + 1 and len(env["q"].messages) == 1
    env["q"].drain()
    s = _pvs(c, job, sid)
    assert s["render"]["isCurrent"] is True and s["render"]["result"]["taskId"] == cur1["id"]


def test_preview_unreadable_bbox_is_render_input_invalid_not_500(env):
    """적용 박스를 읽을 수 없는 블록(bbox NULL·좌표 키 누락·수가 아닌 값·배치 조정값 형식 오류)과 섹션 크기 없음:
    /preview·/blocks 는 200, 그 섹션만 RENDER_INPUT_INVALID·지문 null(비교 불가), 다른 섹션은 그대로."""
    import json

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    before = {s["id"]: s for s in _pv(c, job)["sections"]}
    others = {k: v["inputFingerprint"] for k, v in before.items() if k != sid}
    assert others
    orig = _q("SELECT bbox, layout_adjust FROM text_block WHERE id = :b", b=b["id"])[0]
    bb = orig["bbox"]
    cases = [
        ("bbox", None),
        ("bbox", {k: v for k, v in bb.items() if k != "w"}),
        ("bbox", {**bb, "x": str(bb["x"])}),
        ("bbox", {**bb, "y": True}),
        ("layout_adjust", ["layout_bbox"]),
        ("layout_adjust", {"layout_bbox": {"x": 1, "y": 1, "w": 5}}),
        ("layout_adjust", {"layout_bbox": "0,0,5,5"}),
    ]
    for col, val in cases:
        _q(f"UPDATE text_block SET {col} = CAST(:v AS jsonb) WHERE id = :b", v=None if val is None else json.dumps(val), b=b["id"])
        r = c.get(f"/v1/jobs/{job}/preview")
        assert r.status_code == 200, (col, val, r.text)
        secs = {s["id"]: s for s in r.json()["sections"]}
        s = secs[sid]
        assert s["inputError"] == {"code": "RENDER_INPUT_INVALID", "blockIds": [b["id"]]}, (col, val)
        assert s["renderInput"] is None and s["inputFingerprint"] is None and s["render"]["isCurrent"] is False
        assert s["render"]["result"]["taskId"] == before[sid]["render"]["result"]["taskId"]
        assert {k: secs[k]["inputFingerprint"] for k in others} == others
        bl = c.get(f"/v1/jobs/{job}/blocks")
        assert bl.status_code == 200, (col, val, bl.text)
        fps = {x["sectionId"]: x["sectionInputFingerprint"] for x in bl.json()}
        assert fps[sid] is None and {k: fps[k] for k in others} == others
        _q("UPDATE text_block SET bbox = CAST(:bb AS jsonb), layout_adjust = CAST(:aa AS jsonb) WHERE id = :b", bb=json.dumps(bb),
           aa=None if orig["layout_adjust"] is None else json.dumps(orig["layout_adjust"]), b=b["id"])
    assert _pvs(c, job, sid)["inputFingerprint"] == before[sid]["inputFingerprint"]
    sbox = _q("SELECT bbox FROM section WHERE id = :s", s=sid)[0]["bbox"]
    for sql in ("UPDATE section SET bbox = NULL WHERE id = :s", "UPDATE section SET bbox = bbox - 'w' WHERE id = :s"):
        _q(sql, s=sid)
        s = _pvs(c, job, sid)
        assert s["inputError"] == {"code": "RENDER_INPUT_INVALID", "blockIds": []} and s["inputFingerprint"] is None, sql
        assert c.get(f"/v1/jobs/{job}/blocks").status_code == 200
        _q("UPDATE section SET bbox = CAST(:v AS jsonb) WHERE id = :s", v=json.dumps(sbox), s=sid)


def test_unlinked_image_keys_are_classified_and_never_current(env, monkeypatch):
    """이미지 키는 있지만 채택 기록에 연결하지 못하는 경우를 구분한다(레거시·이전 실행·N6 최종 렌더·알 수 없음).
    어느 경우든 result·renderedUrl null — 근거 없는 이미지를 최신 결과로 인정하지 않는다."""
    from app import db as app_db
    from app import execution as ex
    from app.flows import preview_view as pvw

    logged = []  # 로거 설정(다른 시험의 전파 설정)에 기대지 않고 호출 자체를 본다
    monkeypatch.setattr(pvw.log, "warning", lambda msg, *a: logged.append(msg % a))

    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    key = _image_key(sid)

    def reason():
        with app_db.SessionLocal() as db:
            return pvw.unlinked_reason(pvw.PreviewContext(db, job), sid, _image_key(sid))

    _q("UPDATE section SET render_image_key = 'legacy/unknown.png' WHERE id = :s", s=sid)
    assert reason() == pvw.UNLINKED_UNKNOWN
    s = _pvs(c, job, sid)
    assert s["render"]["result"] is None and s["renderedUrl"] is None
    assert any(f"섹션 {sid}" in m and "unknown" in m for m in logged), logged
    _q("UPDATE section SET render_image_key = :k WHERE id = :s", k=key, s=sid)
    real = ex.latest_run
    monkeypatch.setattr(pvw.execution, "latest_run",
                        lambda db, j, kind: (lambda r: {**r, "run_scope": {**r["run_scope"], "analysis_task_id": -1}} if r else r)(real(db, j, kind)))
    assert reason() == pvw.UNLINKED_PREVIOUS_RUN and _pvs(c, job, sid)["renderedUrl"] is None
    monkeypatch.setattr(pvw.execution, "latest_run", lambda db, j, kind: None)  # 실행 기록 이전 방식(하류 실행 기록 없음)
    assert reason() == pvw.UNLINKED_LEGACY
    s = _pvs(c, job, sid)
    assert s["render"] == {"attempt": None, "result": None, "isCurrent": False} and s["renderedUrl"] is None
    monkeypatch.setattr(pvw.execution, "latest_run", real)
    assert c.post(f"/v1/jobs/{job}/confirm", json={"acknowledgedWarnings": []}).status_code == 200
    env["q"].drain()
    assert _image_key(sid) != key  # N6 최종 렌더가 섹션 이미지를 최종 이미지로 바꿨다
    assert reason() == pvw.UNLINKED_FINAL_RENDER
    s = _pvs(c, job, sid)
    assert s["render"]["result"] is None and s["renderedUrl"] is None


def test_n5_restore_creates_exactly_one_attempt_when_current_is_missing_or_cancelled(env):
    """현재 미리보기 시도가 없거나(기록 없음) 취소됐으면 복원 때 새 시도가 정확히 하나 생기고 커밋 뒤 한 번 전송된다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    secs = [s["id"] for s in _pv(c, job)["sections"]]
    a, z = secs[0], secs[1]
    # 취소된 현재 시도(대기 중 시도가 취소된 채 남음 — 완료 시도는 DB 가드가 상태 변경을 막는다) → 대체하는 새 시도 1건
    blk = [x for x in c.get(f"/v1/jobs/{job}/blocks").json() if x["sectionId"] == a and x["trans1"]][0]
    c.patch(f"/v1/jobs/{job}/blocks/{blk['id']}", json={"trans1": "Will be cancelled", "revision": blk["revision"]})
    env["q"].messages.clear()
    assert _spatch(c, job, a, {"action": "exclude", "revision": 0}).status_code == 200
    cur = _cur_preview(a)
    assert cur["status"] == "pending"
    _q("UPDATE job_async_task SET status = 'cancelled', finished_at = now() WHERE id = :t", t=cur["id"])
    n0 = _preview_count(job)
    assert _spatch(c, job, a, {"action": "restore", "revision": 1}, key="r-cancelled").status_code == 200
    new = _cur_preview(a)
    assert _preview_count(job) == n0 + 1 and new["supersedes_task_id"] == cur["id"] and new["status"] == "pending"
    assert env["q"].messages == [("app.tasks.run_preview", [new["id"]])]
    assert _spatch(c, job, a, {"action": "restore", "revision": 1}, key="r-cancelled").headers["Idempotent-Replayed"] == "true"
    assert _preview_count(job) == n0 + 1 and len(env["q"].messages) == 1
    env["q"].drain()
    # 현재 시도 없음(미리보기 시도 기록이 없는 섹션) → 첫 시도 1건
    assert _spatch(c, job, z, {"action": "exclude", "revision": 0}).status_code == 200
    _q("DELETE FROM job_async_task WHERE stage = 'preview' AND unit_id = :s", s=z)
    n1 = _preview_count(job)
    assert _spatch(c, job, z, {"action": "restore", "revision": 1}).status_code == 200
    first = _cur_preview(z)
    assert _preview_count(job) == n1 + 1 and first["supersedes_task_id"] is None and first["retry_count"] == 0
    assert env["q"].messages == [("app.tasks.run_preview", [first["id"]])]
    env["q"].drain()
    assert _pvs(c, job, a)["render"]["isCurrent"] is True
    assert _attempt(first["id"])["status"] == "done"


def test_n5_restore_during_translation_retry_is_not_secured_at_restore(env):
    """미확보 경로 기록: 번역 재시도가 진행 중(⑧ 미완료)인 섹션을 복원하면 그 시점에는 미리보기 시도를 만들지 않는다.
    번역이 끝나면 기존 진행 규칙(progress)이 이전 성공 미리보기의 입력 변경을 보고 다시 그린다 — 이 경로는 이전 시도가 done 일 때만 닫힌다."""
    ids, target = _to_n5(env, fail_first=True)
    c, job = ids["c"], ids["job"]
    sid = _q("SELECT section_id FROM text_block WHERE id = :b", b=target)[0]["section_id"]
    tr = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current", j=job, s=sid)[0]
    pv0 = _cur_preview(sid)
    assert pv0["status"] == "done"
    rev = _srow(sid)["revision"]
    assert _spatch(c, job, sid, {"action": "exclude", "revision": rev}).status_code == 200
    ai_adapters.set_adapters(translator=FakeTranslator())
    assert c.post(f"/v1/jobs/{job}/tasks/{tr['id']}/retry").status_code == 200
    n0 = _preview_count(job)
    sent = list(env["q"].messages)
    assert _spatch(c, job, sid, {"action": "restore", "revision": rev + 1}).status_code == 200
    assert _preview_count(job) == n0 and _cur_preview(sid) == pv0 and env["q"].messages == sent  # 복원 시점 미확보
    assert _pvs(c, job, sid)["renderInput"] is None  # 입력 준비 중(번역 재시도) — N5 에서도 생기는 조합
    env["q"].drain()
    cur = _cur_preview(sid)
    assert cur["id"] != pv0["id"] and cur["status"] == "done" and _pvs(c, job, sid)["render"]["isCurrent"] is True


def _progress_now(run_id, sid):
    """진행 처리를 한 번 직접 부른다(대표 실행을 잠근 트랜잭션 — 실제 호출과 같은 조건)."""
    from app import db as app_db
    from app.flows import downstream

    with app_db.SessionLocal() as db:
        execution.lock_task(db, run_id)
        made = downstream.progress(db, run_id, sid)
        db.commit()
    return made


def test_n5_restore_after_failed_preview_gets_one_new_preview_once_translation_is_adopted(env, monkeypatch):
    """이전 미리보기 실패 → 제외 → 번역 재시도 중 복원(그 시점엔 미확보) → 바뀐 번역 채택 → 진행 처리가 새 미리보기 하나를 등록 → 채택.
    같은 입력의 최종 실패에서는 진행 처리가 새 시도를 만들지 않고, 중복 호출에서도 시도는 하나만 생긴다."""
    from app import tasks
    from app.flows import downstream

    ids, target = _to_n5(env, fail_first=True)
    c, job = ids["c"], ids["job"]
    sid = _q("SELECT section_id FROM text_block WHERE id = :b", b=target)[0]["section_id"]
    run_id = _q("SELECT parent_task_id FROM job_async_task WHERE id = :t", t=_cur_preview(sid)["id"])[0]["parent_task_id"]
    others = [b for b in c.get(f"/v1/jobs/{job}/blocks").json() if b["sectionId"] == sid and b["trans1"] and b["id"] != target]
    assert others, "번역 실패 블록과 같은 섹션에 번역된 블록이 있어야 한다"
    # ① 이전 미리보기 실패(같은 섹션의 다른 블록 저장 → 재렌더 실패)
    real_render = downstream._render_section
    monkeypatch.setattr(downstream, "_render_section", _render_boom)
    o = others[0]
    c.patch(f"/v1/jobs/{job}/blocks/{o['id']}", json={"trans1": "Saved before failure", "revision": o["revision"]})
    env["q"].drain()
    failed = _cur_preview(sid)
    assert failed["status"] == "failed"
    monkeypatch.setattr(downstream, "_render_section", real_render)
    assert _progress_now(run_id, sid) == []  # 같은 입력의 최종 실패 — 진행 처리가 새로 만들지 않는다(실패·재시도 횟수 보존)
    assert _cur_preview(sid) == failed
    # ② 제외 → 번역 재시도 → 재시도 진행 중 복원(⑧ 미완료라 복원 시점에는 미리보기를 만들지 않는다)
    rev = _srow(sid)["revision"]
    assert _spatch(c, job, sid, {"action": "exclude", "revision": rev}).status_code == 200
    ai_adapters.set_adapters(translator=FakeTranslator())
    tr = _q("SELECT id FROM job_async_task WHERE job_id = :j AND stage = 'translate' AND unit_id = :s AND is_current", j=job, s=sid)[0]
    assert c.post(f"/v1/jobs/{job}/tasks/{tr['id']}/retry").status_code == 200
    n0 = _preview_count(job)
    assert _spatch(c, job, sid, {"action": "restore", "revision": rev + 1}).status_code == 200
    assert _preview_count(job) == n0 and _cur_preview(sid) == failed
    # 입력 준비 중(번역 재시도 진행)이어도 렌더 시도 상태는 따로 낸다 — 실패를 표시하고, 이전 결과는 최신 아님·넘침 없음
    s = _pvs(c, job, sid)
    assert _input_state(s) == "preparing"
    assert s["render"]["attempt"]["taskId"] == failed["id"] and s["render"]["attempt"]["status"] == "failed"
    assert s["render"]["isCurrent"] is False
    # ③ 바뀐 번역 채택(미리보기 실행은 아직 하지 않는다) → 진행 처리가 새 미리보기 하나를 등록
    sent = env["q"].drain(skip={"app.tasks.run_preview"})
    assert _q("SELECT trans_1 FROM text_block WHERE id = :b", b=target)[0]["trans_1"].startswith("EN ")
    new = _cur_preview(sid)
    assert _preview_count(job) == n0 + 1 and new["status"] == "pending"
    assert (new["supersedes_task_id"], new["retry_count"], new["retry_origin"]) == (failed["id"], failed["retry_count"], "auto")
    assert new["input_fingerprint"] != failed["input_fingerprint"] == _attempt(failed["id"])["input_fingerprint"]
    assert [a for n, a in sent if n == "app.tasks.run_preview"] == [[new["id"]]]
    assert _pvs(c, job, sid)["inputFingerprint"] == new["input_fingerprint"]
    # ④ 진행 처리 중복 호출 — 시도는 하나뿐
    assert _progress_now(run_id, sid) == [] and _progress_now(run_id, sid) == []
    assert _preview_count(job) == n0 + 1 and _cur_preview(sid)["id"] == new["id"]
    # ⑤ 새 미리보기 실행·채택
    tasks.run_preview(new["id"])
    env["q"].drain()
    assert _attempt(new["id"])["status"] == "done" and _preview_count(job) == n0 + 1
    s = _pvs(c, job, sid)
    assert s["render"]["isCurrent"] is True and s["render"]["result"]["taskId"] == new["id"]
    assert _progress_now(run_id, sid) == []  # 최신 입력의 완료 시도 — 더 만들지 않는다


def test_progress_does_not_resume_cancelled_run_for_changed_input(env):
    """취소·종료된 실행은 재개하지 않는다: 실행이 진행 중이 아니면 입력이 바뀌어도 진행 처리가 미리보기를 만들지 않는다."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    run_id = _q("SELECT parent_task_id FROM job_async_task WHERE id = :t", t=_cur_preview(sid)["id"])[0]["parent_task_id"]
    _q("UPDATE text_block SET trans_1 = 'Changed after run closed', revision = revision + 1 WHERE id = :b", b=b["id"])
    _q("UPDATE job_async_task SET status = 'cancelled', cancelled_at = now(), finished_at = now() WHERE id = :r", r=run_id)
    n0 = _preview_count(job)
    assert _progress_now(run_id, sid) == [] and _preview_count(job) == n0


def test_n5_restore_with_unreadable_box_succeeds_without_attempt(env):
    """적용 박스를 읽을 수 없는 섹션의 복원은 200(섹션 상태는 바뀜)이고 미리보기 시도는 만들지 않는다 — /preview 는 RENDER_INPUT_INVALID."""
    ids, _ = _to_n5(env)
    c, job = ids["c"], ids["job"]
    b = _text_block(c, job)
    sid = b["sectionId"]
    assert _spatch(c, job, sid, {"action": "exclude", "revision": 0}).status_code == 200
    _q("UPDATE text_block SET bbox = NULL WHERE id = :b", b=b["id"])
    n0, cur0 = _preview_count(job), _cur_preview(sid)
    r = _spatch(c, job, sid, {"action": "restore", "revision": 1})
    assert r.status_code == 200 and r.json()["bucket"] == "include"
    assert (_preview_count(job), _cur_preview(sid), env["q"].messages) == (n0, cur0, [])
    assert _pvs(c, job, sid)["inputError"] == {"code": "RENDER_INPUT_INVALID", "blockIds": [b["id"]]}
