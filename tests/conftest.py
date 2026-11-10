"""시험 공통 — 로그인 세션 저장소 연결.

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
