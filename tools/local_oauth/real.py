"""실제 카카오·Google 로컬 시험 전용 진입점 — 가짜 제공자 없이 BE(app.main)를 127.0.0.1 로 띄운다.

    python -m tools.local_oauth.real --real-providers-local [--port 8000]

serve(가짜 제공자)와 달리 OAuth 제공자 HTTP 는 실제로 나간다. 이 진입점이 막는 범위는 아래 두 가지뿐이다:
  ① OAuth 제공자 HTTP 전송(app.social_auth 의 httpx 전송) — https 이고 허용 호스트 4개
     (kauth.kakao.com·kapi.kakao.com·oauth2.googleapis.com·www.googleapis.com)일 때만 실제 네트워크로 보내고, 그 밖은 보내지 않고 실패.
  ② S3 — app.s3._s3(앱의 유일한 boto3 클라이언트. app.storage 도 호출 때마다 이것을 쓴다)를 차단 대역으로 바꾼다(클라이언트 차단).
     또 app 을 import 하기 전에 AWS 자격 환경 변수를 지우고 자격·설정 파일을 빈 파일로, 인스턴스 메타데이터 조회를 끄고,
     AWS_ENDPOINT_URL_S3 를 닫힌 루프백 주소(127.0.0.1:9)로 둔다(자격·엔드포인트 격리).
**프로세스의 모든 외부 통신을 막지는 않는다.** DB·Redis 는 시작 조건(루프백 주소)으로만 제한하고, 그 밖의 라이브러리 통신은 가로채지 않는다.
대기 주소는 127.0.0.1 고정. 비밀값은 환경 변수로만 받고 출력하지 않는다. 운영 이미지에는 들어가지 않는다(.dockerignore tools/local_oauth).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Mapping

import httpx

from tools.local_oauth.serve import DB_PREFIX, HOST, LOOPBACK, TEST_PREFIX, BlockedS3, _host, _local_origin, port_free
from urllib.parse import urlsplit

FLAG = "--real-providers-local"
PROVIDER_HOSTS = frozenset({"kauth.kakao.com", "kapi.kakao.com", "oauth2.googleapis.com", "www.googleapis.com"})
DEAD_S3_ENDPOINT = "http://127.0.0.1:9"  # discard 포트 — 연결 거부(네트워크 밖으로 나가지 않음)


class AllowlistTransport(httpx.BaseTransport):
    """허용 호스트(https)만 안쪽 전송으로 넘기고, 그 밖은 요청을 보내지 않고 실패시킨다. 넘긴 요청은 호스트·경로만 기록한다(값 없음)."""

    def __init__(self, inner: httpx.BaseTransport, allowed: frozenset[str] = PROVIDER_HOSTS):
        self.inner, self.allowed = inner, allowed
        self.log: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.url.scheme != "https" or host not in self.allowed:
            self.log.append(f"blocked {host}")
            raise httpx.ConnectError(f"실제 제공자 로컬 시험: 허용 밖 외부 호출 차단 — {host}")
        self.log.append(f"{request.method} {host}{request.url.path}")
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


def check_environment(env: Mapping[str, str], flag_given: bool) -> list[str]:
    """시작 조건(값은 메시지에 싣지 않는다)."""
    p: list[str] = []
    if not flag_given:
        p.append(f"명시적 실행 인자 {FLAG} 없음")
    providers = [x.strip() for x in env.get("PIXLATE_OAUTH_PROVIDERS", "").split(",") if x.strip()]
    if not providers or any(x not in ("kakao", "google") for x in providers):
        p.append("PIXLATE_OAUTH_PROVIDERS 는 kakao·google 중 하나 이상")
    for prov in providers:
        up = prov.upper()
        cid = env.get(f"PIXLATE_OAUTH_{up}_CLIENT_ID", "")
        if not cid or cid.startswith(TEST_PREFIX):
            p.append(f"PIXLATE_OAUTH_{up}_CLIENT_ID 는 실제 콘솔 값이어야 함(가짜 제공자 시험은 serve 사용)")
    if "google" in providers and not env.get("PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET"):
        p.append("PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET 없음(환경 변수로만)")
    if "kakao" in providers and not env.get("PIXLATE_OAUTH_KAKAO_APP_ID", "").isdigit():
        p.append("PIXLATE_OAUTH_KAKAO_APP_ID(숫자) 없음")
    if not env.get("JWT_SECRET", "").startswith(TEST_PREFIX):
        p.append(f"JWT_SECRET 는 로컬 시험 값({TEST_PREFIX}…)")
    if env.get("PIXLATE_ACTIVE_CHECKS", "").strip():  # 규칙 검사 공급자 등록(app.text_check_supply) — 로그인 시험에서 켜지 않는다
        p.append("PIXLATE_ACTIVE_CHECKS 가 설정돼 있음 — 비우고 실행(이 진입점은 번역 검사를 켜지 않는다)")
    db = env.get("DATABASE_URL", "")
    if _host(db) not in LOOPBACK:
        p.append("DATABASE_URL 은 루프백 주소의 로컬 DB 여야 함")
    elif not urlsplit(db).path.lstrip("/").startswith(DB_PREFIX):
        p.append(f"DATABASE_URL 의 DB 이름은 {DB_PREFIX}…")
    for name in ("PIXLATE_AUTH_REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        if _host(env.get(name, "")) not in LOOPBACK:
            p.append(f"{name} 는 루프백 주소의 로컬 Redis 로 명시해야 함")
    if not _local_origin(env.get("PIXLATE_OAUTH_REDIRECT_BASE", "")):
        p.append("PIXLATE_OAUTH_REDIRECT_BASE 는 http://localhost:<FE 포트>")
    origins = [o.strip() for o in env.get("PIXLATE_AUTH_ORIGINS", "").split(",") if o.strip()]
    if not origins or not all(_local_origin(o) for o in origins):
        p.append("PIXLATE_AUTH_ORIGINS 는 http://localhost:<FE 포트> 만")
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        if env.get(name):
            p.append(f"{name} 가 설정돼 있음 — 비우고 실행(이 진입점은 S3 를 쓰지 않는다)")
    return p


def prepare_process_env() -> None:
    """app 을 import 하기 전에 — AWS 자격을 지우고 S3 주소를 닫힌 루프백으로 둔다."""
    for k in [k for k in os.environ if k.startswith("AWS_")]:
        del os.environ[k]
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    os.environ["AWS_CONFIG_FILE"] = os.devnull
    os.environ["AWS_ENDPOINT_URL_S3"] = DEAD_S3_ENDPOINT
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"  # 인스턴스 자격 조회도 하지 않음
    os.environ["S3_BUCKET"] = "local-real-provider-blocked-bucket"


def install() -> tuple[object, AllowlistTransport]:
    prepare_process_env()
    from app import s3, social_auth
    from app.main import app

    s3._s3 = BlockedS3("실제 제공자 로컬 시험")
    t = AllowlistTransport(httpx.HTTPTransport())
    social_auth.set_transport(t)
    return app, t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.local_oauth.real", description="실제 제공자 로컬 시험 전용 BE(OAuth 제공자 전송 허용 호스트 제한·S3 차단)")
    ap.add_argument(FLAG, dest="flag", action="store_true")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    problems = check_environment(os.environ, args.flag)
    if not port_free(args.port):
        problems.append(f"{HOST}:{args.port} 이미 사용 중 — 다른 프로세스를 멈추지 않고 다른 포트를 고르세요")
    if problems:
        print("[local_oauth.real] 시작하지 않음:", file=sys.stderr)
        for x in problems:
            print("  -", x, file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app, _ = install()
    print(f"[local_oauth.real] BE http://{HOST}:{args.port} · OAuth 제공자 전송 허용 호스트 {sorted(PROVIDER_HOSTS)} · S3 클라이언트 차단 · "
          f"FE 오리진 {os.environ['PIXLATE_OAUTH_REDIRECT_BASE']}", flush=True)
    import uvicorn

    # 접근 로그는 끈다 — 요청 줄에 값이 실릴 수 있는 경로를 남기지 않는다(판정은 app 로그·브라우저 기록)
    asyncio.run(uvicorn.Server(uvicorn.Config(app, host=HOST, port=args.port, log_level="info", access_log=False)).serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
