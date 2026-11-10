"""일반 시험의 실제 S3 차단(tests/s3_guard.py) 회귀 시험 — 실제 AWS 호출 없음.

차단 대역이 직접 업로드 · 기본 저장소 쓰기 · 예외를 삼킨 삭제 · 목록 조회를 모두 실패로 잡는지,
실제 S3 시험의 실행 조건(옵트인 + 시험 전용 버킷, 공유 버킷 거절)이 지켜지는지 본다.
"""
from __future__ import annotations

import io

import pytest

from tests import s3_guard


@pytest.fixture
def restore_store():
    from app import storage

    before = storage._store
    yield
    storage.set_store(before)


def _guarded_must_fail(action):
    """차단된 호출이 있었으면 guard 가 끝날 때 AssertionError 를 내야 한다."""
    with pytest.MonkeyPatch.context() as mp:
        with pytest.raises(AssertionError, match="실제 S3 호출 시도") as exc:
            with s3_guard.guard(mp, env={}):
                action()
    return str(exc.value)


def test_every_test_runs_with_the_blocker():
    from app import s3

    assert isinstance(s3._s3, s3_guard.BlockedS3Client)  # conftest 의 자동 고정물


def test_direct_upload_is_blocked_and_fails_the_test():
    from app import s3

    def upload():
        with pytest.raises(RuntimeError, match="실제 S3 호출 차단"):
            s3.upload_fileobj(io.BytesIO(b"x"), "logos/0/never.png", content_type="image/png")

    assert "upload_fileobj" in _guarded_must_fail(upload)


def test_default_store_write_is_blocked_and_fails_the_test(restore_store):
    from app import storage

    def put():
        storage.set_store(None)  # 기본 저장소 = S3Store(app.s3._s3 를 쓴다)
        with pytest.raises(RuntimeError, match="실제 S3 호출 차단"):
            storage.get_store().put("staging/never", b"x")

    assert "put_object" in _guarded_must_fail(put)


def test_swallowed_delete_still_fails_the_test():
    from app import s3

    def swallow():
        try:
            s3.delete_object("logos/0/never.png")
        except RuntimeError:
            pass  # 라우터가 예외를 삼키는 경우

    assert "delete_object" in _guarded_must_fail(swallow)


def test_listing_and_presign_are_blocked_too(restore_store):
    from app import s3, storage

    def read_paths():
        storage.set_store(None)
        for call in (lambda: storage.get_store().delete_prefix("staging/never/"), lambda: s3.presigned_get("logos/0/never.png")):
            try:
                call()
            except RuntimeError:
                pass

    msg = _guarded_must_fail(read_paths)
    assert "get_paginator" in msg and "generate_presigned_url" in msg


def test_no_s3_call_passes_cleanly():
    with pytest.MonkeyPatch.context() as mp:
        with s3_guard.guard(mp, env={}) as blocked:
            pass
    assert blocked.calls == []


@pytest.mark.parametrize("env,ok", [
    ({}, False),
    ({"PIXLATE_TEST_REAL_S3": "1"}, False),  # 옵트인만 있고 시험 버킷 없음
    ({"PIXLATE_TEST_REAL_S3": "1", "S3_BUCKET": "pixlate-storage-2026"}, False),  # 기존 버킷 설정으로 자동 연결하지 않는다
    ({"PIXLATE_TEST_REAL_S3": "1", "PIXLATE_TEST_S3_BUCKET": "pixlate-storage-2026"}, False),  # 공유(운영)
    ({"PIXLATE_TEST_REAL_S3": "1", "PIXLATE_TEST_S3_BUCKET": "pixlate-storage-dev-2026"}, False),  # 공유(dev)
    ({"PIXLATE_TEST_REAL_S3": "1", "PIXLATE_TEST_S3_BUCKET": "  "}, False),
    ({"PIXLATE_TEST_REAL_S3": "0", "PIXLATE_TEST_S3_BUCKET": "pixlate-test-only"}, False),  # 옵트인 없음
    ({"PIXLATE_TEST_REAL_S3": "1", "PIXLATE_TEST_S3_BUCKET": "pixlate-test-only"}, True),
])
def test_real_s3_requires_opt_in_and_dedicated_bucket(env, ok):
    bucket, why = s3_guard.real_s3_bucket(env)
    assert (bucket is not None) == ok, why
    if ok:
        assert bucket == "pixlate-test-only" and why == ""


def test_real_s3_mode_points_app_at_the_test_bucket_only():
    """조건을 채우면 그 시험 동안만 app.s3.S3_BUCKET 을 시험 버킷으로 바꾼다(여기서는 S3 를 부르지 않는다)."""
    from app import s3

    shared = s3.S3_BUCKET
    with pytest.MonkeyPatch.context() as mp:
        with s3_guard.guard(mp, env={"PIXLATE_TEST_REAL_S3": "1", "PIXLATE_TEST_S3_BUCKET": "pixlate-test-only"}) as blocked:
            assert blocked is None and s3.S3_BUCKET == "pixlate-test-only"
    assert s3.S3_BUCKET == shared
