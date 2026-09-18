"""전체 API E2E 헬스체크 — 로그인부터 저장까지 파이프라인 전 구간을 순서대로 호출.

실제 DB + Celery 워커(ocr/cpu/gpu)가 떠 있어야 한다(비동기 단계 폴링).
컨테이너에서 실행:  docker exec pixlate-api python -m pytest tests/test_e2e_pipeline.py -v
로컬(PostgreSQL 미연결)에서는 모듈 전체 skip.

테스트 함수는 정의 순서대로 실행되며 CTX(dict)로 상태(토큰·id)를 공유한다.
앞 단계가 실패하면 뒤 단계가 연쇄로 드러나므로 "어디서 깨졌는지"가 명확하다.
"""
import io
import time

import pytest
from PIL import Image
from sqlalchemy import text


def _db_ok() -> bool:
    try:
        from app.db import engine
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="database not reachable")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)
CTX: dict = {}
E2E_EMAIL = "e2e@example.com"


def _png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(b, "PNG")
    return b.getvalue()


def _h() -> dict:
    return CTX["headers"]


def _poll_step(job_id: int, target: str, timeout: float = 30.0) -> str:
    """job.currentStep 이 target 이 될 때까지 폴링(비동기 워커 대기)."""
    deadline = time.time() + timeout
    step = None
    while time.time() < deadline:
        r = client.get(f"/v1/jobs/{job_id}", headers=_h())
        assert r.status_code == 200
        step = r.json()["currentStep"]
        if step == target:
            return step
        time.sleep(1)
    raise AssertionError(f"job {job_id} step {step!r} != {target!r} (워커 미동작?)")


# ── 헬스/인증 ─────────────────────────────────────────────────────
def test_health():
    assert client.get("/").status_code == 200
    assert client.get("/health").json()["status"] == "ok"


def test_auth_login():
    r = client.post("/v1/auth/login", json={"email": E2E_EMAIL, "password": "x"})
    assert r.status_code == 200
    CTX["token"] = r.json()["accessToken"]
    CTX["headers"] = {"Authorization": f"Bearer {CTX['token']}"}


def test_auth_me():
    r = client.get("/v1/auth/me", headers=_h())
    assert r.status_code == 200 and r.json()["email"] == E2E_EMAIL


def test_auth_refresh():
    r = client.post("/v1/auth/refresh", headers=_h())
    assert r.status_code == 200 and r.json()["accessToken"]


def test_consents():
    r = client.post("/v1/consents", json={"agreed": True}, headers=_h())
    assert r.status_code == 200


# ── 마스터(공개) ──────────────────────────────────────────────────
def test_master_endpoints():
    assert client.get("/v1/master/countries").status_code == 200
    assert client.get("/v1/master/languages").status_code == 200
    assert client.get("/v1/master/regulatory-classes?country=US").status_code == 200
    assert client.get("/v1/master/categories").status_code == 200
    specs = client.get("/v1/master/channel-specs")
    assert specs.status_code == 200 and len(specs.json()) >= 1
    spec_id = specs.json()[0]["id"]
    assert client.get(f"/v1/master/channel-specs/{spec_id}/upload-guide").status_code == 200
    assert client.get("/v1/master/versions").status_code == 200


# ── 브랜드 + 로고 ─────────────────────────────────────────────────
def test_brand_create_and_read():
    r = client.post("/v1/brands", json={"nameKo": "E2E브랜드", "nameEn": "E2EBrand"}, headers=_h())
    assert r.status_code == 201
    CTX["brand_id"] = r.json()["id"]
    assert client.get("/v1/brands", headers=_h()).status_code == 200
    assert client.get(f"/v1/brands/{CTX['brand_id']}", headers=_h()).status_code == 200
    r = client.patch(f"/v1/brands/{CTX['brand_id']}", json={"overview": "updated"}, headers=_h())
    assert r.status_code == 200


def test_brand_logo():
    r = client.post(
        f"/v1/brands/{CTX['brand_id']}/logos",
        files={"file": ("logo.png", _png(), "image/png")}, headers=_h(),
    )
    assert r.status_code == 201
    CTX["logo_id"] = r.json()["id"]
    logos = client.get(f"/v1/brands/{CTX['brand_id']}/logos", headers=_h())
    assert logos.status_code == 200 and len(logos.json()) >= 1


# ── 작업 생성 + 원본 이미지 ───────────────────────────────────────
def test_job_create_and_get():
    r = client.post(
        "/v1/jobs",
        json={"brandId": CTX["brand_id"], "productName": "E2E 수분크림", "keywords": ["moisture"]},
        headers=_h(),
    )
    assert r.status_code == 201
    CTX["job_id"] = r.json()["jobId"]
    assert client.get(f"/v1/jobs/{CTX['job_id']}", headers=_h()).status_code == 200


def test_source_images():
    r = client.post(
        f"/v1/jobs/{CTX['job_id']}/source-images",
        files=[("files", ("a.png", _png(), "image/png"))], headers=_h(),
    )
    assert r.status_code == 200
    imgs = client.get(f"/v1/jobs/{CTX['job_id']}/source-images", headers=_h())
    assert imgs.status_code == 200 and len(imgs.json()) >= 1
    CTX["source_image_id"] = imgs.json()[0]["id"]
    r = client.patch(
        f"/v1/jobs/{CTX['job_id']}/source-images/reorder",
        json={"orderedIds": [CTX["source_image_id"]]}, headers=_h(),
    )
    assert r.status_code == 200


# ── N2 분석(ocr 워커) → N3 ────────────────────────────────────────
def test_analyze_to_n3():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/analyze", headers=_h())
    assert r.status_code == 202
    _poll_step(CTX["job_id"], "N3")
    tasks = client.get(f"/v1/jobs/{CTX['job_id']}/tasks", headers=_h())
    assert tasks.status_code == 200


def test_sections():
    r = client.get(f"/v1/jobs/{CTX['job_id']}/sections", headers=_h())
    assert r.status_code == 200
    inc = r.json()["include"]
    assert len(inc) >= 1
    CTX["section_id"] = inc[0]["id"]
    assert client.get(f"/v1/jobs/{CTX['job_id']}/sections/{CTX['section_id']}", headers=_h()).status_code == 200
    # 제외 → 되살리기 (최종 include 유지)
    assert client.patch(f"/v1/jobs/{CTX['job_id']}/sections/{CTX['section_id']}",
                        json={"action": "exclude"}, headers=_h()).status_code == 200
    assert client.patch(f"/v1/jobs/{CTX['job_id']}/sections/{CTX['section_id']}",
                        json={"action": "restore"}, headers=_h()).status_code == 200


def test_inpaint_result():
    # gpu 워커가 채운 결과(폴링). 미완이어도 200 이면 통과.
    r = client.get(f"/v1/jobs/{CTX['job_id']}/sections/{CTX['section_id']}/inpaint", headers=_h())
    assert r.status_code == 200


# ── N4 번역(cpu 워커) → N5 ────────────────────────────────────────
def test_proceed_to_n5():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/sections/proceed", headers=_h())
    assert r.status_code == 202
    _poll_step(CTX["job_id"], "N5")


def test_blocks():
    r = client.get(f"/v1/jobs/{CTX['job_id']}/blocks", headers=_h())
    assert r.status_code == 200 and len(r.json()) >= 1
    block = r.json()[0]
    CTX["block_id"] = block["id"]
    r = client.patch(
        f"/v1/jobs/{CTX['job_id']}/blocks/{CTX['block_id']}",
        json={"trans1": "Edited translation", "revision": block["revision"]}, headers=_h(),
    )
    assert r.status_code == 200


def test_preview():
    r = client.get(f"/v1/jobs/{CTX['job_id']}/preview", headers=_h())
    assert r.status_code == 200 and "sections" in r.json()


# ── N6 확정 → 렌더(cpu 워커) → 산출물 ─────────────────────────────
def test_confirm_to_n6():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/confirm", headers=_h())
    assert r.status_code == 200 and r.json()["currentStep"] == "N6"


def test_render_and_deliverables():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/render", headers=_h())
    assert r.status_code == 202
    # deliverable 생성될 때까지 폴링
    deadline = time.time() + 30
    delivered = []
    while time.time() < deadline:
        d = client.get(f"/v1/jobs/{CTX['job_id']}/deliverables", headers=_h())
        assert d.status_code == 200
        delivered = d.json()["deliverables"]
        if delivered:
            break
        time.sleep(1)
    assert delivered, "렌더 산출물 미생성(cpu 워커 미동작?)"
    v = client.get(f"/v1/jobs/{CTX['job_id']}/validation", headers=_h())
    assert v.status_code == 200


def test_export_and_download():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/export", json={"components": ["content.csv"]}, headers=_h())
    assert r.status_code == 201
    CTX["artifact_id"] = r.json()["artifactId"]
    d = client.get(f"/v1/jobs/{CTX['job_id']}/exports/{CTX['artifact_id']}/download", headers=_h())
    assert d.status_code == 200 and d.json()["url"]


def test_save_and_library():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/save", headers=_h())
    assert r.status_code == 200 and r.json()["isSaved"] is True
    lib = client.get("/v1/library", headers=_h())
    assert lib.status_code == 200
    assert any(item["jobId"] == CTX["job_id"] for item in lib.json())


# ── 엣지/에러/정리 ────────────────────────────────────────────────
def test_retry_nonexistent_task_404():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/tasks/99999999/retry", headers=_h())
    assert r.status_code == 404


def test_abort_returns_200():
    r = client.post(f"/v1/jobs/{CTX['job_id']}/abort", headers=_h())
    assert r.status_code == 200 and "returnTo" in r.json()


def test_cleanup_deletes():
    assert client.delete(f"/v1/brands/{CTX['brand_id']}/logos/{CTX['logo_id']}", headers=_h()).status_code == 204
    assert client.delete(
        f"/v1/jobs/{CTX['job_id']}/source-images/{CTX['source_image_id']}", headers=_h()
    ).status_code == 204
    # 작업 취소(archived)
    assert client.delete(f"/v1/jobs/{CTX['job_id']}", headers=_h()).status_code == 200
