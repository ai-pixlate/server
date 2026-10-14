"""N1 draft-first 계약(B1~B8) — 실제 BE 라우트 + 격리 PostgreSQL(+ 격리 Redis) + 메모리 S3 대역.

생성(brandId 만) → 업로드 → N1 PATCH → 조회 → reorder → analyze(큐 발행까지, 실행·모델 호출 없음).
소유권·상태·부분 갱신·오류 시 부작용 없음·Idempotency-Key 중복 생성 방지를 본다.

  $env:PIXLATE_TEST_DATABASE_URL = "postgresql+psycopg://<user>:<pw>@localhost:<port>/postgres"   # 격리 postgres:16
  $env:PIXLATE_TEST_REDIS_URL    = "redis://localhost:<port>/0"                                     # 격리 redis(없으면 멱등 시험만 skip)
운영 DB·S3·Redis 주소를 넣지 않는다.
"""
from __future__ import annotations

import io
import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text

from app import ai_adapters, execution, idempotency, s3
from app.storage import MemoryStore, set_store
from tests.exec_db import bound_app_db, fresh_database, requires_db
from tests.exec_fakes import FakeAnalyzer, QueueDispatcher

pytestmark = requires_db
REDIS_URL = os.getenv("PIXLATE_TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(not REDIS_URL, reason="PIXLATE_TEST_REDIS_URL 없음")


@pytest.fixture(scope="module")
def dburl():
    with fresh_database() as u:
        with bound_app_db(u):
            yield u


@pytest.fixture
def env(dburl, monkeypatch):
    store = MemoryStore()
    set_store(store)
    # S3 대역: 업로드 바이트는 분석이 읽는 저장소(MemoryStore)에 넣고, presigned 는 가짜 주소. 실제 S3 클라이언트는 비워 둔다
    monkeypatch.setattr(s3, "_s3", None)
    monkeypatch.setattr(s3, "upload_fileobj", lambda f, key, content_type=None: store.put(key, f.read()) or key)
    monkeypatch.setattr(s3, "presigned_get", lambda key, ttl=300: f"https://presigned.test/{key}" if key else None)
    monkeypatch.setattr(s3, "delete_object", lambda key: None)
    q = QueueDispatcher()
    execution.set_dispatcher(q)
    ai_adapters.reset_adapters()
    ai_adapters.set_adapters(analyzer=FakeAnalyzer())
    yield {"store": store, "q": q}
    execution.set_dispatcher(None)
    set_store(None)
    ai_adapters.reset_adapters()


def _q(sql, **p):
    from app import db as app_db

    with app_db.engine.begin() as c:
        res = c.execute(text(sql), p)
        return [dict(r) for r in res.mappings().all()] if res.returns_rows else None


def _seller(raise_server_exceptions=True):
    """새 판매자(로그인이 만든다) + 그 판매자의 브랜드. (client, brand_id, seller_id)"""
    from app.main import app

    c = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    email = f"n1-{uuid.uuid4().hex[:8]}@example.com"
    r = c.post("/v1/auth/login", json={"email": email, "password": "x"})
    c.headers["Authorization"] = f"Bearer {r.json()['accessToken']}"
    b = c.post("/v1/brands", json={"nameKo": "구달", "nameEn": "goodal"})
    assert b.status_code in (200, 201), b.text
    return c, b.json()["id"], r.json()["seller"]["id"]


def _png(w=60, h=80, color=(240, 240, 240), fmt="PNG"):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format=fmt)
    return buf.getvalue()


def _upload(c, job_id, names=("a.png", "b.png", "c.jpg")):
    files = [("files", (n, _png(fmt="JPEG" if n.endswith(".jpg") else "PNG"), "image/jpeg" if n.endswith(".jpg") else "image/png"))
             for n in names]
    r = c.post(f"/v1/jobs/{job_id}/source-images", files=files)
    assert r.status_code == 200, r.text
    return r.json()


N1 = {"productName": "수분 크림 50ml", "productCode": "SKU-1", "targetCountry": "US", "targetLanguage": "en",
      "regulatoryClass": "cosmetic", "specType": "original", "imageType": "multi_section", "keywords": ["moisture", "cream"]}


def _order(job_id):
    return [r["id"] for r in _q("SELECT id FROM source_image WHERE job_id = :j ORDER BY upload_order, id", j=job_id)]


# ── 주 흐름 ───────────────────────────────────────────────────────────
def test_draft_first_flow_create_upload_patch_get_reorder_analyze(env):
    c, brand, _ = _seller()
    r = c.post("/v1/jobs", json={"brandId": brand})  # B1: brandId 만
    assert r.status_code == 201, r.text
    job = r.json()
    assert "jobId" not in job and isinstance(job["id"], int)  # B2: 계약 Job, 호환 키 없음
    assert (job["status"], job["currentStep"], job["productName"], job["brandId"]) == ("draft", "N1", None, brand)
    assert {"imageType", "keywords", "createdAt", "updatedAt", "isSaved"} <= job.keys()
    jid = job["id"]

    up = _upload(c, jid)
    assert [x["ok"] for x in up["results"]] == [True, True, True]
    for si in up["sourceImages"]:  # B8
        assert si["fileUrl"].startswith("https://presigned.test/") and si["createdAt"] and si["imageType"] == "detail"
        assert "url" not in si

    p = c.patch(f"/v1/jobs/{jid}", json=N1)  # B3
    assert p.status_code == 200, p.text
    g = c.get(f"/v1/jobs/{jid}").json()  # B5: 조회도 같은 Job
    assert p.json() == g and g["id"] == jid and "jobId" not in g
    for k, v in N1.items():
        assert g[k] == v, k

    ids = [x["sourceImageId"] for x in up["results"]]
    want = [ids[2], ids[0], ids[1]]
    ro = c.patch(f"/v1/jobs/{jid}/source-images/reorder", json={"order": want})  # B7
    assert ro.status_code == 200, ro.text
    assert [x["id"] for x in ro.json()] == want and [x["uploadOrder"] for x in ro.json()] == [1, 2, 3]
    lst = c.get(f"/v1/jobs/{jid}/source-images").json()
    assert [x["id"] for x in lst] == want and all(x["fileUrl"] and x["createdAt"] for x in lst)

    a = c.post(f"/v1/jobs/{jid}/analyze")
    assert a.status_code == 202, a.text
    assert [m[0] for m in env["q"].messages] == ["app.tasks.run_analyze"]  # 큐 발행까지만(실행·모델 호출 없음)
    assert c.get(f"/v1/jobs/{jid}").json()["currentStep"] == "N2"
    # 분석 입력(manifest)은 바뀐 순서를 따른다
    m = _q("SELECT input_manifest AS manifest FROM job_async_task WHERE job_id = :j AND stage = 'analyze'", j=jid)[0]["manifest"]
    assert [s["source_image_id"] for s in m["sources"]] == want


# ── B6: 상품명은 analyze 게이트에서 ──────────────────────────────────
@pytest.mark.parametrize("name", [None, "   "])
def test_analyze_rejects_missing_product_name_without_side_effects(env, name):
    c, brand, _ = _seller()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    _upload(c, jid, ("a.png",))
    patch = {k: v for k, v in N1.items() if k != "productName"} | ({"productName": name} if name is not None else {})
    assert c.patch(f"/v1/jobs/{jid}", json=patch).status_code == 200
    r = c.post(f"/v1/jobs/{jid}/analyze")
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", r.text
    assert env["q"].messages == []
    assert _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=jid)[0]["n"] == 0
    g = c.get(f"/v1/jobs/{jid}").json()
    assert (g["status"], g["currentStep"]) == ("draft", "N1")


# ── B3: 부분 갱신·null·검증 ─────────────────────────────────────────
def test_patch_partial_update_and_null_semantics(env):
    c, brand, _ = _seller()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    assert c.patch(f"/v1/jobs/{jid}", json=N1).status_code == 200

    g = c.patch(f"/v1/jobs/{jid}", json={"productCode": "SKU-2"}).json()  # 보낸 필드만
    assert g["productCode"] == "SKU-2" and g["productName"] == N1["productName"] and g["keywords"] == N1["keywords"]
    g = c.patch(f"/v1/jobs/{jid}", json={"productCode": None}).json()  # null = 비움
    assert g["productCode"] is None and g["targetCountry"] == "US"
    g = c.patch(f"/v1/jobs/{jid}", json={"keywords": ["a", "b", "c"]}).json()  # 전체 교체
    assert g["keywords"] == ["a", "b", "c"]
    g = c.patch(f"/v1/jobs/{jid}", json={"unknownField": 1}).json()  # 모르는 필드 무시, 변화 없음
    assert g["keywords"] == ["a", "b", "c"] and g["productName"] == N1["productName"]
    assert c.patch(f"/v1/jobs/{jid}", json={}).status_code == 200


@pytest.mark.parametrize("body", [
    {"keywords": None}, {"specType": None}, {"regulatoryClass": "drug"}, {"imageType": "panorama"},
    {"keywords": "a"}, {"channelSpecId": "x"}, ["productName"], None,
])
def test_patch_validation_error_changes_nothing(env, body):
    c, brand, _ = _seller()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    before = c.patch(f"/v1/jobs/{jid}", json=N1).json()
    r = c.patch(f"/v1/jobs/{jid}", json=body) if body is not None else c.patch(f"/v1/jobs/{jid}")
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", r.text
    assert c.get(f"/v1/jobs/{jid}").json() == before


def test_patch_ownership_and_state(env):
    c, brand, _ = _seller()
    other, _, _ = _seller()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    before = c.patch(f"/v1/jobs/{jid}", json=N1).json()

    for r in (other.patch(f"/v1/jobs/{jid}", json={"productName": "x"}), c.patch("/v1/jobs/987654321", json={"productName": "x"})):
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert other.get(f"/v1/jobs/{jid}").status_code == 404
    assert c.get(f"/v1/jobs/{jid}").json() == before

    _upload(c, jid, ("a.png",))
    assert c.post(f"/v1/jobs/{jid}/analyze").status_code == 202  # → N2
    r = c.patch(f"/v1/jobs/{jid}", json={"productName": "바뀜"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert c.get(f"/v1/jobs/{jid}").json()["productName"] == N1["productName"]


# ── B7: reorder 검증·부작용 없음 ──────────────────────────────────────
def test_reorder_validation_and_no_side_effects(env):
    c, brand, _ = _seller()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    ids = [x["sourceImageId"] for x in _upload(c, jid)["results"]]
    other_job = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    foreign = _upload(c, other_job, ("z.png",))["results"][0]["sourceImageId"]
    before = _order(jid)

    cases_400 = [None, {}, {"order": "1,2"}, {"order": [ids[0], "x", ids[2]]}, {"order": [ids[0], True, ids[2]]},
                 {"order": [ids[0], ids[0], ids[1], ids[2]]}, {"order": [ids[0], ids[1]]}, {"order": []}, [ids[0]]]
    for body in cases_400:
        r = (c.patch(f"/v1/jobs/{jid}/source-images/reorder", json=body) if body is not None
             else c.patch(f"/v1/jobs/{jid}/source-images/reorder"))
        assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", (body, r.text)
        assert _order(jid) == before

    for body in ({"order": [ids[0], ids[1], ids[2], 987654321]}, {"order": [ids[0], ids[1], foreign]},
                 {"order": [ids[2], ids[1], ids[0], foreign]}):
        r = c.patch(f"/v1/jobs/{jid}/source-images/reorder", json=body)
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND", (body, r.text)
        assert _order(jid) == before

    other, _, _ = _seller()
    r = other.patch(f"/v1/jobs/{jid}/source-images/reorder", json={"order": list(reversed(ids))})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert _order(jid) == before
    # 기본값 [2,1] 같은 숨은 동작이 없다: 빈 작업에 빈 order 는 그대로 200 []
    empty = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    assert c.patch(f"/v1/jobs/{empty}/source-images/reorder", json={"order": []}).json() == []


# ── B4: Idempotency-Key ──────────────────────────────────────────────
@pytest.fixture
def redis_env(env):
    import redis

    r = redis.Redis.from_url(REDIS_URL)
    for k in r.scan_iter("pixlate:idem:*"):
        r.delete(k)
    idempotency.set_client(r)
    yield r
    idempotency.set_client(None)


def _jobs_of(seller_id):
    return _q("SELECT count(*) AS n FROM job WHERE seller_id = :s", s=seller_id)[0]["n"]


@requires_redis
def test_idempotency_replays_first_response_and_scopes_by_seller(redis_env):
    c, brand, seller = _seller()
    key = uuid.uuid4().hex
    r1 = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    r2 = c.post("/v1/jobs", json={"brandId": brand, "productName": "다른 본문"}, headers={"Idempotency-Key": key})
    assert r1.status_code == r2.status_code == 201
    assert r2.json() == r1.json() and r2.json()["productName"] is None  # 본문이 달라도 최초 응답
    assert _jobs_of(seller) == 1
    assert c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": uuid.uuid4().hex}).json()["id"] != r1.json()["id"]
    assert c.post("/v1/jobs", json={"brandId": brand}).status_code == 201  # 키 없음 → 매번 생성
    assert _jobs_of(seller) == 3
    ttl = redis_env.ttl(next(iter(redis_env.scan_iter("pixlate:idem:*"))))
    assert 0 < ttl <= 24 * 3600

    other, obrand, oseller = _seller()  # 다른 판매자의 같은 키는 별개
    r3 = other.post("/v1/jobs", json={"brandId": obrand}, headers={"Idempotency-Key": key})
    assert r3.status_code == 201 and r3.json()["id"] != r1.json()["id"] and _jobs_of(oseller) == 1


@requires_redis
def test_idempotency_concurrent_requests_create_one_job(redis_env):
    c, brand, seller = _seller()
    key = uuid.uuid4().hex
    out, start = [], threading.Barrier(6)

    def go():
        start.wait()
        r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
        out.append((r.status_code, r.json()))

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert _jobs_of(seller) == 1  # 중복 생성 없음
    created = {b["id"] for s, b in out if s == 201}
    assert len(created) == 1
    # 처리 중에 도착한 같은 키 요청은 409 DUPLICATE(리뷰어 권고 반영)
    assert all(s == 201 or (s == 409 and b["error"]["code"] == "DUPLICATE") for s, b in out)
    assert c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key}).json()["id"] in created


@requires_redis
def test_idempotency_store_unavailable_creates_nothing(env):
    import redis

    c, brand, seller = _seller()
    idempotency.set_client(redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2))
    try:
        r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": "k"})
    finally:
        idempotency.set_client(None)
    assert r.status_code == 503 and r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"  # 키를 받은 요청에서 Redis 불가 → 503·생성 안 함(리뷰어 권고 반영)
    assert _jobs_of(seller) == 0


# ── 브랜드 소유권 ────────────────────────────────────────────────────
def test_create_rejects_missing_or_foreign_brand(env):
    c, _, seller = _seller()
    _, other_brand, _ = _seller()
    for brand in (other_brand, 987654321):
        r = c.post("/v1/jobs", json={"brandId": brand})
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND", r.text
    assert _jobs_of(seller) == 0


@requires_redis
def test_create_foreign_brand_with_key_releases_key(redis_env):
    c, brand, seller = _seller()
    _, other_brand, _ = _seller()
    key = uuid.uuid4().hex
    r = c.post("/v1/jobs", json={"brandId": other_brand}, headers={"Idempotency-Key": key})
    assert r.status_code == 404 and _jobs_of(seller) == 0
    assert redis_env.get(idempotency.rkey(seller, "post_jobs", key)) is None  # 저장 안 됨이 확실 → 키 삭제
    r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})  # 같은 키로 다시 시도 가능
    assert r.status_code == 201 and _jobs_of(seller) == 1


# ── Idempotency 장애 구간 ─────────────────────────────────────────────
import json as _json  # noqa: E402

from redis.exceptions import ConnectionError as _RedisDown  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session as _SASession  # noqa: E402


def _state(r, seller, key):
    raw = r.get(idempotency.rkey(seller, "post_jobs", key))
    return None if raw is None else _json.loads(raw)["state"]


def _arm_commit(monkeypatch, mode):
    """다음 커밋 한 번을 실패시킨다. after = 커밋된 뒤 예외(결과 불명이지만 실제로는 저장), before = 커밋 안 하고 예외."""
    orig = _SASession.commit
    armed = {"on": True}

    def fake(self):
        if not armed["on"]:
            return orig(self)
        armed["on"] = False
        if mode == "after":
            orig(self)
        else:
            self.rollback()
        raise OperationalError("COMMIT", {}, Exception("connection lost during commit"))

    monkeypatch.setattr(_SASession, "commit", fake)


@requires_redis
def test_commit_error_but_committed_is_confirmed(redis_env, monkeypatch):
    c, brand, seller = _seller(raise_server_exceptions=False)
    key = uuid.uuid4().hex
    _arm_commit(monkeypatch, "after")
    r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert r.status_code == 201, r.text  # 새 연결로 작업 행 확인 → 확정
    assert _state(redis_env, seller, key) == "done" and _jobs_of(seller) == 1
    again = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert again.json() == r.json() and _jobs_of(seller) == 1


@requires_redis
def test_commit_error_not_committed_keeps_key_no_duplicate(redis_env, monkeypatch):
    c, brand, seller = _seller(raise_server_exceptions=False)
    key = uuid.uuid4().hex
    _arm_commit(monkeypatch, "before")
    r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert r.status_code == 500 and _jobs_of(seller) == 0
    assert _state(redis_env, seller, key) == "committing"  # 커밋 결과 불명 → 키를 지우지 않는다
    again = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert again.status_code == 409 and again.json()["error"]["code"] == "DUPLICATE"
    assert _jobs_of(seller) == 0  # 같은 키로 중복 생성하지 않는다
    assert c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": uuid.uuid4().hex}).status_code == 201


@requires_redis
def test_commit_error_and_check_unavailable_resolves_on_retry(redis_env, monkeypatch):
    from app.routers import jobs as jobs_router

    c, brand, seller = _seller(raise_server_exceptions=False)
    key = uuid.uuid4().hex
    _arm_commit(monkeypatch, "after")  # 실제로는 저장됐지만
    real = jobs_router._committed
    monkeypatch.setattr(jobs_router, "_committed", lambda j, s: None)  # 확인 불가
    r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert r.status_code == 500 and _state(redis_env, seller, key) == "committing" and _jobs_of(seller) == 1
    monkeypatch.setattr(jobs_router, "_committed", real)
    again = c.post("/v1/jobs", json={"brandId": brand, "productName": "다른 본문"}, headers={"Idempotency-Key": key})
    assert again.status_code == 201 and again.json()["productName"] is None  # 저장해 둔 최초 응답
    assert _state(redis_env, seller, key) == "done" and _jobs_of(seller) == 1


@requires_redis
def test_redis_fails_after_commit_then_retry_replays(redis_env, monkeypatch):
    c, brand, seller = _seller()
    key = uuid.uuid4().hex
    real_put = idempotency._put

    def put(rk, value, nx=False):
        if value["state"] == "done":
            raise _RedisDown("down after commit")
        return real_put(rk, value, nx=nx)

    monkeypatch.setattr(idempotency, "_put", put)
    r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert r.status_code == 201 and _state(redis_env, seller, key) == "committing" and _jobs_of(seller) == 1
    monkeypatch.setattr(idempotency, "_put", real_put)
    again = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert again.status_code == 201 and again.json() == r.json()  # DB 로 확인 → 최초 응답 재전송
    assert _state(redis_env, seller, key) == "done" and _jobs_of(seller) == 1


@requires_redis
def test_redis_fails_before_commit_creates_nothing(redis_env, monkeypatch):
    c, brand, seller = _seller()
    key = uuid.uuid4().hex
    real_put = idempotency._put

    def put(rk, value, nx=False):
        if value["state"] == "committing":
            raise _RedisDown("down before commit")
        return real_put(rk, value, nx=nx)

    monkeypatch.setattr(idempotency, "_put", put)
    r = c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key})
    assert r.status_code == 503 and r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert _jobs_of(seller) == 0 and _state(redis_env, seller, key) is None  # 롤백 + 키 삭제
    monkeypatch.setattr(idempotency, "_put", real_put)
    assert c.post("/v1/jobs", json={"brandId": brand}, headers={"Idempotency-Key": key}).status_code == 201
