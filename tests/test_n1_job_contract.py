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

    assert c.patch(f"/v1/jobs/{jid}", json={"categoryId": _tree()["leaf"]}).status_code == 200  # 최초 분석 필수(leaf)
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
    r = c.post(f"/v1/jobs/{jid}/analyze")  # 카테고리도 없지만 상품명 게이트가 먼저다
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", r.text
    assert r.json()["error"]["message"] == "productName is required" and r.json()["error"]["details"] is None
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
    before = c.patch(f"/v1/jobs/{jid}", json={**N1, "categoryId": _tree()["leaf"]}).json()

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


# ── 카테고리: /master/categories 평면 배열 · 작업 생성·수정의 leaf 검증(PRD F-SRC-04 J41② · F-SRC-04b) ──
CATEGORY_KEYS = {"id", "parentId", "name", "amazonNode", "isLeaf", "path"}


def _tree():
    """대분류 > 중분류 > 소분류 + 소분류 없는 중분류. 이름은 시험마다 고유(같은 모듈 DB를 쓴다)."""
    from app import db as app_db

    t = uuid.uuid4().hex[:6]

    def node(c, name, parent, leaf, mapping=None):
        path = f"{parent[1]} > {name}" if parent else name
        cid = c.execute(text("INSERT INTO category_master (parent_id, name, is_leaf, path) VALUES (:p, :n, :l, :path) RETURNING id"),
                        {"p": parent[0] if parent else None, "n": name, "l": leaf, "path": path}).scalar()
        if mapping:
            c.execute(text("INSERT INTO category_mapping (category_id, internal_category) VALUES (:c, :m)"), {"c": cid, "m": mapping})
        return cid, path

    with app_db.engine.begin() as c:
        root = node(c, f"스킨케어{t}", None, False)
        mid = node(c, "크림", root, False)
        leaf = node(c, "아이크림", mid, True, "G01")
        unmapped = node(c, "넥크림", mid, True)
        mid_leaf = node(c, "미스트", root, True, "G02")  # 소분류가 없는 중분류 = leaf
    return {"root": root[0], "mid": mid[0], "leaf": leaf[0], "unmapped": unmapped[0], "mid_leaf": mid_leaf[0]}


def _fields(r):
    return r.json()["error"]["details"]["fields"]


def test_master_categories_is_flat_category_array(env):
    c, _, _ = _seller()
    ids = _tree()
    body = c.get("/v1/master/categories").json()
    assert isinstance(body, list) and all(set(x) == CATEGORY_KEYS for x in body)  # 계약 Category — children 없음
    assert [x["id"] for x in body] == sorted(x["id"] for x in body)
    by = {x["id"]: x for x in body}
    assert all(isinstance(x["id"], int) and isinstance(x["isLeaf"], bool) and isinstance(x["name"], str)
               and (x["parentId"] is None or isinstance(x["parentId"], int)) for x in body)
    assert by[ids["root"]]["parentId"] is None and by[ids["mid"]]["parentId"] == ids["root"]
    assert by[ids["leaf"]]["parentId"] == ids["mid"] and by[ids["mid_leaf"]]["parentId"] == ids["root"]
    assert by[ids["leaf"]]["path"].endswith("크림 > 아이크림") and by[ids["leaf"]]["amazonNode"] is None
    parents = {x["parentId"] for x in body if x["parentId"] is not None}
    # 화면 규칙(parentId로 이은 트리에서 하위 없음 = 선택 가능)과 서버 isLeaf가 같다
    assert all(x["isLeaf"] == (x["id"] not in parents) for x in body)
    assert [k for k, v in ids.items() if by[v]["isLeaf"]] == ["leaf", "unmapped", "mid_leaf"]


def test_create_accepts_leaf_and_keeps_draft_unselected(env):
    c, brand, _ = _seller()
    ids = _tree()
    for cid, want in ((ids["leaf"], "G01"), (ids["mid_leaf"], "G02"), (ids["unmapped"], None)):  # 매핑 없는 leaf도 거절하지 않음
        r = c.post("/v1/jobs", json={"brandId": brand, "categoryId": cid})
        assert r.status_code == 201, r.text
        assert (r.json()["categoryId"], r.json()["internalCategory"]) == (cid, want)
    for body in ({"brandId": brand}, {"brandId": brand, "categoryId": None}):  # draft 미선택 허용
        r = c.post("/v1/jobs", json=body)
        assert r.status_code == 201 and r.json()["categoryId"] is None and r.json()["internalCategory"] is None


@pytest.mark.parametrize("kind, msg", [("root", "category is not a leaf"), ("mid", "category is not a leaf"),
                                       ("unknown", "unknown category"), ("huge", "unknown category"), ("zero", "unknown category")])
def test_create_rejects_parent_or_unknown_category(env, kind, msg):
    c, brand, seller = _seller()
    ids = _tree()
    cid = {"unknown": 987654321, "huge": 2**63, "zero": 0}.get(kind, ids.get(kind))
    r = c.post("/v1/jobs", json={"brandId": brand, "productName": "x", "categoryId": cid})
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", r.text
    assert _fields(r) == [{"loc": ["body", "categoryId"], "msg": msg}] and "reason" not in r.json()["error"]["details"]
    assert _jobs_of(seller) == 0


def test_create_brand_ownership_still_checked_first(env):
    c, _, seller = _seller()
    _, other_brand, _ = _seller()
    ids = _tree()
    r = c.post("/v1/jobs", json={"brandId": other_brand, "categoryId": ids["root"]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND" and _jobs_of(seller) == 0


@requires_redis
def test_create_rejected_category_with_key_releases_key(redis_env):
    c, brand, seller = _seller()
    ids = _tree()
    key = uuid.uuid4().hex
    r = c.post("/v1/jobs", json={"brandId": brand, "categoryId": ids["root"]}, headers={"Idempotency-Key": key})
    assert r.status_code == 400 and _jobs_of(seller) == 0
    assert redis_env.get(idempotency.rkey(seller, "post_jobs", key)) is None  # 저장 안 됨이 확실 → 키 삭제(기존 규칙)
    r = c.post("/v1/jobs", json={"brandId": brand, "categoryId": ids["leaf"]}, headers={"Idempotency-Key": key})
    assert r.status_code == 201 and r.json()["categoryId"] == ids["leaf"] and _jobs_of(seller) == 1


@pytest.mark.parametrize("kind", ["root", "mid", "unknown"])
def test_patch_category_rejection_changes_nothing(env, kind):
    c, brand, _ = _seller()
    ids = _tree()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    before = c.patch(f"/v1/jobs/{jid}", json={**N1, "categoryId": ids["leaf"]}).json()
    stamp = _q("SELECT updated_at FROM job WHERE id = :j", j=jid)[0]["updated_at"]
    cid = 987654321 if kind == "unknown" else ids[kind]
    r = c.patch(f"/v1/jobs/{jid}", json={"categoryId": cid, "productName": "바뀌면 안 됨", "keywords": ["x"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", r.text
    assert _fields(r)[0]["loc"] == ["body", "categoryId"]
    assert c.get(f"/v1/jobs/{jid}").json() == before  # 같은 요청의 다른 필드도 반영되지 않는다
    assert _q("SELECT updated_at FROM job WHERE id = :j", j=jid)[0]["updated_at"] == stamp


def test_patch_category_leaf_null_and_unmapped(env):
    c, brand, _ = _seller()
    ids = _tree()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    g = c.patch(f"/v1/jobs/{jid}", json={"categoryId": ids["leaf"]}).json()
    assert (g["categoryId"], g["internalCategory"]) == (ids["leaf"], "G01")
    g = c.patch(f"/v1/jobs/{jid}", json={"categoryId": ids["unmapped"]}).json()
    assert (g["categoryId"], g["internalCategory"]) == (ids["unmapped"], None)
    g = c.patch(f"/v1/jobs/{jid}", json={"categoryId": None}).json()  # null = 비움(draft 미선택)
    assert (g["categoryId"], g["internalCategory"]) == (None, None)


def test_stored_parent_category_is_not_rechecked_or_changed(env):
    """검증 이전에 상위 분류가 저장된 draft 작업 — 보정하지 않고, categoryId를 보내지 않는 수정은 그대로 통과한다.
    최초 분석은 카테고리 게이트에서 막힌다(아래 analyze 시험). 값은 바뀌지 않는다."""
    c, brand, _ = _seller()
    ids = _tree()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    _q("UPDATE job SET display_category_id = :c, internal_category = NULL WHERE id = :j", c=ids["root"], j=jid)
    g = c.patch(f"/v1/jobs/{jid}", json=N1)
    assert g.status_code == 200 and (g.json()["categoryId"], g.json()["internalCategory"]) == (ids["root"], None)
    _upload(c, jid, ("a.png",))
    assert c.post(f"/v1/jobs/{jid}/analyze").status_code == 400  # 최초 분석 게이트
    assert c.get(f"/v1/jobs/{jid}").json()["categoryId"] == ids["root"]


def test_patch_category_keeps_existing_auth_ownership_state_order(env):
    from app.main import app

    c, brand, _ = _seller()
    other, _, _ = _seller()
    ids = _tree()
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    assert TestClient(app).patch(f"/v1/jobs/{jid}", json={"categoryId": ids["root"]}).status_code == 401
    r = other.patch(f"/v1/jobs/{jid}", json={"categoryId": ids["root"]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    c.patch(f"/v1/jobs/{jid}", json={**N1, "categoryId": ids["leaf"]})
    _upload(c, jid, ("a.png",))
    assert c.post(f"/v1/jobs/{jid}/analyze").status_code == 202  # → N2
    r = c.patch(f"/v1/jobs/{jid}", json={"categoryId": ids["root"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"  # 상태 확인이 먼저(기존 순서)


# ── 분석 시작(ANL-01)의 카테고리 게이트 — 최초(N1)만, 기존 게이트 뒤·실행 생성 전 ──
def _analysis_rows(jid):
    return _q("SELECT count(*) AS n FROM job_async_task WHERE job_id = :j", j=jid)[0]["n"]


def _ready_job(c, brand, category, **n1):
    """원본 1장 + N1 값. category 는 저장할 categoryId(None = 미선택). 상위 분류는 검증 이전 데이터처럼 SQL로 넣는다."""
    jid = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]
    _upload(c, jid, ("a.png",))
    assert c.patch(f"/v1/jobs/{jid}", json={**N1, **n1}).status_code == 200
    if category is not None:
        _q("UPDATE job SET display_category_id = :c WHERE id = :j", c=category, j=jid)
    return jid


@pytest.mark.parametrize("kind, msg", [("none", "category is required"), ("root", "category is not a leaf"),
                                       ("mid", "category is not a leaf")])
def test_first_analysis_requires_leaf_category(env, kind, msg):
    c, brand, _ = _seller()
    ids = _tree()
    jid = _ready_job(c, brand, None if kind == "none" else ids[kind])
    before = c.get(f"/v1/jobs/{jid}").json()
    r = c.post(f"/v1/jobs/{jid}/analyze")
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION_ERROR", r.text
    assert r.json()["error"]["details"] == {"fields": [{"loc": ["body", "categoryId"], "msg": msg}]}
    assert _analysis_rows(jid) == 0 and env["q"].messages == []  # 실행·시도·큐 전달 없음
    assert c.get(f"/v1/jobs/{jid}").json() == before  # draft/N1 · 값 그대로


def test_first_analysis_accepts_mapped_and_unmapped_leaf(env):
    c, brand, _ = _seller()
    ids = _tree()
    for cid in (ids["leaf"], ids["unmapped"], ids["mid_leaf"]):
        jid = _ready_job(c, brand, None, categoryId=cid)
        assert c.post(f"/v1/jobs/{jid}/analyze").status_code == 202
        assert c.get(f"/v1/jobs/{jid}").json()["currentStep"] == "N2"


def test_first_analysis_gate_order_keeps_existing_gates_first(env):
    """여러 조건이 함께 틀리면 기존 순서가 먼저다: 404 → 409(취소·단계) → 400 상품명 → 409 규제 분류 → 409 국가 → 409 원본 → 400 카테고리."""
    from app.main import app

    c, brand, _ = _seller()
    other, _, _ = _seller()
    no_cat = _ready_job(c, brand, None)
    assert TestClient(app).post(f"/v1/jobs/{no_cat}/analyze").status_code == 401
    r = other.post(f"/v1/jobs/{no_cat}/analyze")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = c.post(f"/v1/jobs/{_ready_job(c, brand, None, productName=None)}/analyze")
    assert r.status_code == 400 and r.json()["error"]["message"] == "productName is required"
    r = c.post(f"/v1/jobs/{_ready_job(c, brand, None, regulatoryClass=None)}/analyze")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    r = c.post(f"/v1/jobs/{_ready_job(c, brand, None, targetCountry=None)}/analyze")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    bare = c.post("/v1/jobs", json={"brandId": brand}).json()["id"]  # 원본 없음 + 카테고리 없음
    assert c.patch(f"/v1/jobs/{bare}", json=N1).status_code == 200
    r = c.post(f"/v1/jobs/{bare}/analyze")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert c.delete(f"/v1/jobs/{no_cat}").status_code == 200  # 취소된 작업 + 카테고리 없음
    r = c.post(f"/v1/jobs/{no_cat}/analyze")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert env["q"].messages == []


@pytest.mark.parametrize("legacy", ["none", "root"])
def test_n2_failed_retry_keeps_current_behavior_and_abort_reapplies_gate(env, legacy):
    """게이트 이전에 시작한 기존 작업(카테고리 미선택·상위 분류)의 N2 실패 [다시 시도]는 기존 작업 재시도 호환을 위한 예외로 그대로 시작한다.
    [중단]으로 N1에 돌아오면 최초 분석이므로 게이트가 다시 적용되고, leaf로 고친 뒤 시작할 수 있다."""
    from pipeline.errors import AnalyzeError

    c, brand, _ = _seller()
    ids = _tree()
    jid = _ready_job(c, brand, None, categoryId=ids["leaf"])
    ai_adapters.set_adapters(analyzer=FakeAnalyzer(fail=AnalyzeError("IMAGE_OPEN_FAILED", "깨진 파일", 1)))
    assert c.post(f"/v1/jobs/{jid}/analyze").status_code == 202
    env["q"].drain()
    assert (c.get(f"/v1/jobs/{jid}").json()["status"], c.get(f"/v1/jobs/{jid}").json()["currentStep"]) == ("failed", "N2")
    # 게이트 이전에 시작한 작업처럼 만든다(카테고리 값만 — 실행 기록은 그대로)
    _q("UPDATE job SET display_category_id = :c, internal_category = NULL WHERE id = :j",
       c=None if legacy == "none" else ids["root"], j=jid)
    rows, sent = _analysis_rows(jid), len(env["q"].messages)

    r = c.post(f"/v1/jobs/{jid}/analyze")  # [다시 시도] — 카테고리 확인 안 함(현행)
    assert r.status_code == 202, r.text
    assert _analysis_rows(jid) > rows and len(env["q"].messages) == sent + 1  # 새 실행·시도 + 큐 전달
    env["q"].drain()
    assert c.get(f"/v1/jobs/{jid}").json()["status"] == "failed"

    assert c.post(f"/v1/jobs/{jid}/abort").status_code == 200  # [중단] → draft/N1
    assert c.get(f"/v1/jobs/{jid}").json()["currentStep"] == "N1"
    sent = len(env["q"].messages)
    r = c.post(f"/v1/jobs/{jid}/analyze")  # N1로 돌아온 작업 = 최초 분석 → 게이트
    assert r.status_code == 400 and r.json()["error"]["details"]["fields"][0]["loc"] == ["body", "categoryId"]
    assert len(env["q"].messages) == sent
    ai_adapters.set_adapters(analyzer=FakeAnalyzer())
    assert c.patch(f"/v1/jobs/{jid}", json={"categoryId": ids["unmapped"]}).status_code == 200
    assert c.post(f"/v1/jobs/{jid}/analyze").status_code == 202
