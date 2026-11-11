"""로컬 FE·BE 브라우저 시험 전용 진입점 — BE(app.main) + 가짜 카카오·Google 로그인 화면을 한 프로세스에서 127.0.0.1 로만 띄운다.

    python -m tools.local_oauth.serve --local-fake-providers [--port 8000] [--fake-port 8900] [--init-db]

운영 실행(`uvicorn app.main:app` — Dockerfile CMD · deploy/start-pixlate.sh)은 이 파일을 부르지 않고, `app/` 에는 가짜 제공자를 켜는 설정이 없다.
이 진입점만 프로세스 안에서 바꿔 끼운다(시험이 쓰는 방식과 같음):
  ① social_auth.set_transport(대역) — 서버가 제공자에게 보내는 HTTP 는 프로세스 안 대역이 받는다. 대역에 없는 주소는 실패.
  ② 브라우저용 인가 주소 상수 두 개만 가짜 로그인 화면(127.0.0.1:<fake-port>)으로 바꾼다.
  ③ app.s3._s3 를 차단 대역으로 바꾸고 AWS 자격 환경 변수를 지운다 — S3 를 부르면 실패한다.
  ④ 세션 저장소 장애 흉내(가짜 화면의 /_control store_down)를 위해 auth_sessions.client 를 감싼다.
시작 조건(하나라도 아니면 아무것도 띄우지 않고 끝낸다) — check_environment 참고. 대기 주소는 127.0.0.1 고정(바꾸는 인자 없음).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from typing import Mapping
from urllib.parse import urlsplit

FLAG = "--local-fake-providers"
TEST_PREFIX = "local-fake-"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DB_PREFIX = "pixlate_local_"
HOST = "127.0.0.1"


def _host(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _local_origin(value: str) -> bool:
    try:
        u = urlsplit(value)
        return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1") and u.port is not None \
            and u.path in ("", "/") and not u.query and not u.fragment
    except ValueError:
        return False


def check_environment(env: Mapping[str, str], flag_given: bool) -> list[str]:
    """시작 조건. 돌려주는 값 = 어긴 조건 목록(값은 싣지 않는다 — 이름과 이유만)."""
    p: list[str] = []
    if not flag_given:
        p.append(f"명시적 시험 실행 인자 {FLAG} 없음")
    providers = [x.strip() for x in env.get("PIXLATE_OAUTH_PROVIDERS", "").split(",") if x.strip()]
    if not providers or any(x not in ("kakao", "google") for x in providers):
        p.append("PIXLATE_OAUTH_PROVIDERS 는 kakao·google 중 하나 이상")
    for prov in providers:
        up = prov.upper()
        if not env.get(f"PIXLATE_OAUTH_{up}_CLIENT_ID", "").startswith(TEST_PREFIX):
            p.append(f"PIXLATE_OAUTH_{up}_CLIENT_ID 는 시험 값({TEST_PREFIX}…)이어야 함")
        sec = env.get(f"PIXLATE_OAUTH_{up}_CLIENT_SECRET")
        if (prov == "google" or sec) and not (sec or "").startswith(TEST_PREFIX):
            p.append(f"PIXLATE_OAUTH_{up}_CLIENT_SECRET 는 시험 값({TEST_PREFIX}…)이어야 함")
    if "kakao" in providers and not env.get("PIXLATE_OAUTH_KAKAO_APP_ID", "").isdigit():
        p.append("PIXLATE_OAUTH_KAKAO_APP_ID(숫자 시험 값) 없음")
    for name in ("JWT_SECRET",):
        if not env.get(name, "").startswith(TEST_PREFIX):
            p.append(f"{name} 는 시험 값({TEST_PREFIX}…)이어야 함")
    if env.get("PIXLATE_AUTH_REFRESH_SECRET") and not env["PIXLATE_AUTH_REFRESH_SECRET"].startswith(TEST_PREFIX):
        p.append(f"PIXLATE_AUTH_REFRESH_SECRET 는 비우거나 시험 값({TEST_PREFIX}…)")
    if env.get("PIXLATE_ACTIVE_CHECKS", "").strip():  # 규칙 검사 공급자 등록(app.text_check_supply) — 로그인 시험에서 켜지 않는다
        p.append("PIXLATE_ACTIVE_CHECKS 가 설정돼 있음 — 비우고 실행(이 진입점은 번역 검사를 켜지 않는다)")
    db = env.get("DATABASE_URL", "")
    if _host(db) not in LOOPBACK:
        p.append("DATABASE_URL 은 루프백 주소의 로컬 DB 여야 함")
    elif not urlsplit(db).path.lstrip("/").startswith(DB_PREFIX):
        p.append(f"DATABASE_URL 의 DB 이름은 {DB_PREFIX}… (다른 시험·채팅의 DB 를 쓰지 않음)")
    for name in ("PIXLATE_AUTH_REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"):
        if _host(env.get(name, "")) not in LOOPBACK:
            p.append(f"{name} 는 루프백 주소의 로컬 Redis 로 명시해야 함")
    if not _local_origin(env.get("PIXLATE_OAUTH_REDIRECT_BASE", "")):
        p.append("PIXLATE_OAUTH_REDIRECT_BASE 는 http://localhost:<FE 포트>")
    origins = [o.strip() for o in env.get("PIXLATE_AUTH_ORIGINS", "").split(",") if o.strip()]
    if not origins or not all(_local_origin(o) for o in origins):
        p.append("PIXLATE_AUTH_ORIGINS 는 http://localhost:<FE 포트> 만")
    elif env.get("PIXLATE_OAUTH_REDIRECT_BASE", "").rstrip("/") not in [o.rstrip("/") for o in origins]:
        p.append("PIXLATE_OAUTH_REDIRECT_BASE 가 PIXLATE_AUTH_ORIGINS 에 없음")
    return p


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


class BlockedS3:
    """app.s3._s3 자리에 둔다 — 어떤 메서드든 부르면 네트워크 없이 실패한다."""

    def __init__(self, label: str = "로컬 가짜 제공자 실행"):
        self._label = label

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        label = self._label

        def blocked(*a, **k):
            raise RuntimeError(f"{label}: S3 호출 차단({name})")
        return blocked


class _DownRedis:
    def __getattr__(self, name):
        def fail(*a, **k):
            import redis

            raise redis.ConnectionError("local fake store_down")
        return fail


def init_db(url: str) -> None:
    """DATABASE_URL 의 DB(이름 pixlate_local_…)가 없으면 만들고 마이그레이션 head 까지 올린다. 다른 DB 는 건드리지 않는다."""
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url

    u = make_url(url)
    admin = sa.create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            if not c.execute(sa.text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": u.database}).first():
                c.execute(sa.text(f'CREATE DATABASE "{u.database}"'))
                print(f"[local_oauth] DB 생성: {u.database}")
    finally:
        admin.dispose()
    from alembic import command
    from alembic.config import Config

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = Config(os.path.join(root, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(root, "migrations"))
    command.upgrade(cfg, "head")


def install(fake_port: int):
    """앱을 import 하고 가짜 제공자·차단을 끼운다. 돌려주는 값 = (BE 앱, 가짜 화면 앱, FakeProviders)."""
    for k in [k for k in os.environ if k.startswith("AWS_")]:
        del os.environ[k]
    os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    os.environ["AWS_CONFIG_FILE"] = os.devnull
    os.environ["S3_BUCKET"] = "local-fake-blocked-bucket"
    from app import auth_sessions, s3, social_auth
    from app.main import app

    from tools.local_oauth.fake import FakeProviders, Settings, make_page_app

    s3._s3 = BlockedS3()
    fake = FakeProviders(Settings(
        kakao_client_id=os.getenv("PIXLATE_OAUTH_KAKAO_CLIENT_ID", ""), kakao_secret=os.getenv("PIXLATE_OAUTH_KAKAO_CLIENT_SECRET") or None,
        kakao_app_id=int(os.getenv("PIXLATE_OAUTH_KAKAO_APP_ID", "0") or 0),
        google_client_id=os.getenv("PIXLATE_OAUTH_GOOGLE_CLIENT_ID", ""), google_secret=os.getenv("PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET", "")))
    social_auth.set_transport(fake.transport())
    social_auth.KAKAO_AUTHORIZE = f"http://{HOST}:{fake_port}/kakao/authorize"
    social_auth.GOOGLE_AUTHORIZE = f"http://{HOST}:{fake_port}/google/authorize"
    real_client = auth_sessions.client
    auth_sessions.client = lambda: _DownRedis() if fake.store_down else real_client()
    return app, make_page_app(fake), fake


async def _serve(be_app, page_app, port: int, fake_port: int) -> None:
    import uvicorn

    # 두 서버가 같은 uvicorn 로거를 쓰므로 수준을 같게 둔다(나중 설정이 앞 설정을 덮어씀). 가짜 화면은 접근 로그를 끈다
    servers = [uvicorn.Server(uvicorn.Config(be_app, host=HOST, port=port, log_level="info")),
               uvicorn.Server(uvicorn.Config(page_app, host=HOST, port=fake_port, log_level="info", access_log=False))]
    await asyncio.gather(*(s.serve() for s in servers))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.local_oauth.serve", description="로컬 브라우저 시험 전용 BE + 가짜 OAuth 제공자")
    ap.add_argument(FLAG, dest="flag", action="store_true", help="명시적 시험 실행(없으면 시작하지 않음)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--fake-port", type=int, default=8900)
    ap.add_argument("--init-db", action="store_true", help="pixlate_local_… DB 생성·마이그레이션")
    args = ap.parse_args(argv)
    problems = check_environment(os.environ, args.flag)
    for port in (args.port, args.fake_port):
        if not port_free(port):
            problems.append(f"{HOST}:{port} 이미 사용 중 — 다른 프로세스를 멈추지 않고 다른 포트를 고르세요")
    if problems:
        print("[local_oauth] 시작하지 않음:", file=sys.stderr)
        for x in problems:
            print("  -", x, file=sys.stderr)
        return 2
    if args.init_db:
        init_db(os.environ["DATABASE_URL"])
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")  # app 로그(pixlate.*) — 값 없는 판정 기록
    be_app, page_app, _ = install(args.fake_port)
    print(f"[local_oauth] BE http://{HOST}:{args.port} · 가짜 로그인 화면 http://{HOST}:{args.fake_port} · "
          f"FE 오리진 {os.environ['PIXLATE_OAUTH_REDIRECT_BASE']} · 실제 제공자·S3 호출 차단")
    asyncio.run(_serve(be_app, page_app, args.port, args.fake_port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
