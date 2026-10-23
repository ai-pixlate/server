"""브로커 연결 설정 — 소켓 읽기 시간 제한(2026-10-22 학교 워커 정지 대응). 실제 Redis 연결 없음."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = (
    "import json; from app.celery_app import celery_app as a; c = a.conf; "
    "print(json.dumps({'opts': dict(c.broker_transport_options), 'connect': c.redis_socket_connect_timeout, "
    "'keepalive': c.redis_socket_keepalive, 'backend_timeout': c.redis_socket_timeout}))"
)


def _conf(**env: str) -> dict:
    """새 프로세스에서 app.celery_app 를 읽어 적용값을 본다(모듈 수준 설정이라 환경변수를 바꿔 다시 import 해야 함)."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("CELERY_")}
    e.update(env)
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=e, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_broker_socket_timeout_default_30s():
    assert _conf()["opts"] == {"visibility_timeout": 3600, "socket_timeout": 30.0}


def test_broker_socket_timeout_env_override():
    assert _conf(CELERY_BROKER_SOCKET_TIMEOUT="45")["opts"]["socket_timeout"] == 45.0


def test_only_broker_read_timeout_changes():
    # 연결 제한·keepalive·결과 저장소 시간 제한은 이번 변경 대상이 아니다(Celery 기본값 그대로)
    c = _conf()
    assert c["connect"] is None and c["keepalive"] is False and c["backend_timeout"] == 120.0
