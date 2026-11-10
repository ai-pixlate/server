"""시험 공통 — 로그인 세션 저장소 연결 · 실제 S3 차단.

실제 S3 차단(2026-11-10, tests/s3_guard.py): 시험마다 app.s3 의 boto3 클라이언트(`app.s3._s3` — `app.storage.S3Store` 도 이것을 쓴다)를
「부르면 실패하는 대역」으로 바꾼다. AWS 자격이 있는 PC 에서도 일반 시험이 실제 버킷에 쓰거나 지우지 못하게 하기 위해서다
(#124 처음 판의 소유권 시험이 실제 공유 버킷에 시험 로고를 남겼다). 호출이 예외 처리로 삼켜져도 시험 끝에 실패로 잡는다.
- 시험이 스스로 `s3._s3`·`s3.upload_fileobj` 등을 대역으로 바꾸면 그쪽이 우선한다(기존 시험 방식 그대로).
- 실제 S3 시험(`requires_real_s3`)은 PIXLATE_TEST_REAL_S3=1 **과** 시험 전용 버킷 PIXLATE_TEST_S3_BUCKET(공유 버킷이면 거절)이
  모두 있을 때만 돌고, 그때는 app.s3.S3_BUCKET 을 그 시험 버킷으로 바꾼다. 하나라도 없으면 건너뛰고 차단은 그대로다.

로그인·인증이 세션(Redis)을 거치게 되어(app.auth_sessions), 실제 API 로 로그인하는 시험은 세션 저장소가 필요하다.
- PIXLATE_TEST_REDIS_URL 이 있으면 그 Redis 를 쓴다(CI). 키 접두어 `pixlate:auth:sess:` — 다른 시험의 키와 섞이지 않는다.
- 없으면 같은 연산의 메모리 대역(tests/auth_session_fake.py). 세션 자체의 시험은 대역을 쓰지 않는다.
"""
from __future__ import annotations

import os

import pytest

# refresh·logout 의 허용 Origin(시험 값 — .test 도메인. 운영 값이 아니다). 허용 목록이 없으면 쿠키 엔드포인트는 모두 거절한다.
TEST_FE_ORIGIN = "https://fe.pixlate.test"
os.environ.setdefault("PIXLATE_AUTH_ORIGINS", TEST_FE_ORIGIN)


from tests import s3_guard  # noqa: E402

_REAL_S3_BUCKET, _REAL_S3_SKIP = s3_guard.real_s3_bucket()
requires_real_s3 = pytest.mark.skipif(_REAL_S3_BUCKET is None, reason=_REAL_S3_SKIP or "실제 S3 시험")


@pytest.fixture(autouse=True)
def _block_real_s3(monkeypatch):
    with s3_guard.guard(monkeypatch) as blocked:
        yield blocked


@pytest.fixture(autouse=True)
def _block_real_oauth_providers():
    """소셜 로그인(C): 시험은 실제 카카오·Google 을 부르지 않는다 — 제공자 HTTP 를 「부르면 실패」로 막는다(시험이 대역을 끼우면 그쪽이 우선)."""
    import httpx

    from app import social_auth

    def _blocked(request):
        raise AssertionError(f"실제 OAuth 제공자 호출 차단: {request.url.host}")

    social_auth.set_transport(httpx.MockTransport(_blocked))
    yield
    social_auth.set_transport(None)
    social_auth.clock_ms = lambda: None


@pytest.fixture(autouse=True, scope="session")
def _auth_session_store():
    from app import auth_sessions

    url = os.getenv("PIXLATE_TEST_REDIS_URL")
    if url:
        import redis

        auth_sessions.set_client(redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2))
    else:
        from tests.auth_session_fake import FakeSessionRedis

        auth_sessions.set_client(FakeSessionRedis())
    yield
    auth_sessions.set_client(None)
