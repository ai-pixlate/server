"""실제 제공자 로컬 시험 진입점(tools/local_oauth/real) 자체 시험 — 시작 조건·허용 호스트 전송·S3 차단 준비. 네트워크·제공자를 부르지 않는다."""
from __future__ import annotations

import os
import subprocess
import sys

import httpx
import pytest

pytest.importorskip("tools.local_oauth", reason="운영 이미지에는 시험 전용 진입점이 없다(.dockerignore)")

from tools.local_oauth import real  # noqa: E402

GOOD = {
    "PIXLATE_OAUTH_PROVIDERS": "kakao,google", "PIXLATE_OAUTH_REDIRECT_BASE": "http://localhost:3000",
    "PIXLATE_AUTH_ORIGINS": "http://localhost:3000", "PIXLATE_OAUTH_KAKAO_CLIENT_ID": "abcdef0123456789",
    "PIXLATE_OAUTH_KAKAO_APP_ID": "1234567", "PIXLATE_OAUTH_GOOGLE_CLIENT_ID": "123-x.apps.googleusercontent.com",
    "PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET": "s3cr3t-value", "JWT_SECRET": "local-fake-jwt",
    "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:55451/pixlate_local_real",
    "PIXLATE_AUTH_REDIS_URL": "redis://127.0.0.1:56451/12", "CELERY_BROKER_URL": "redis://127.0.0.1:56451/13",
    "CELERY_RESULT_BACKEND": "redis://127.0.0.1:56451/14",
}


def test_good_environment_passes():
    assert real.check_environment(GOOD, True) == []


@pytest.mark.parametrize("change,needle", [
    ({}, "명시적 실행 인자"),
    ({"PIXLATE_OAUTH_KAKAO_CLIENT_ID": "local-fake-kakao"}, "KAKAO_CLIENT_ID"),
    ({"PIXLATE_OAUTH_GOOGLE_CLIENT_SECRET": ""}, "GOOGLE_CLIENT_SECRET"),
    ({"AWS_PROFILE": "default"}, "AWS_PROFILE"),
    ({"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"}, "AWS_ACCESS_KEY_ID"),
    ({"DATABASE_URL": "postgresql+psycopg://u:p@pixlate-db.example.com:5432/pixlate_local_real"}, "DATABASE_URL"),
    ({"DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:55451/pixlate"}, "DB 이름"),
    ({"CELERY_BROKER_URL": ""}, "CELERY_BROKER_URL"),
    ({"PIXLATE_ACTIVE_CHECKS": "rule"}, "PIXLATE_ACTIVE_CHECKS"),
    ({"PIXLATE_OAUTH_REDIRECT_BASE": "https://dev.example"}, "REDIRECT_BASE"),
])
def test_bad_environment_refuses_without_echoing_values(change, needle):
    problems = real.check_environment({**GOOD, **change}, bool(change))
    assert any(needle in x for x in problems), problems
    joined = " ".join(problems)
    assert "s3cr3t" not in joined and "AKIAEXAMPLE" not in joined and "abcdef0123456789" not in joined


def test_allowlist_transport_forwards_only_provider_hosts():
    seen = []
    inner = httpx.MockTransport(lambda r: (seen.append(r.url.host), httpx.Response(200, json={}))[1])
    t = real.AllowlistTransport(inner)
    with httpx.Client(transport=t) as c:
        for host in sorted(real.PROVIDER_HOSTS):
            assert c.get(f"https://{host}/x").status_code == 200
        for url in ("https://example.com/", "https://pixlate-storage-2026.s3.ap-northeast-2.amazonaws.com/",
                    "http://kauth.kakao.com/oauth/token", "https://accounts.google.com/o/oauth2/v2/auth"):
            with pytest.raises(httpx.ConnectError, match="허용 밖"):
                c.get(url)
    assert sorted(seen) == sorted(real.PROVIDER_HOSTS)  # 막힌 요청은 안쪽 전송에 닿지 않았다


def test_prepared_process_points_s3_at_a_dead_loopback_endpoint_without_credentials():
    """새 파이썬 프로세스에서 prepare_process_env() 뒤 boto3 S3 클라이언트를 만들면 주소가 127.0.0.1:9 이고 자격이 없다(요청은 보내지 않음)."""
    code = (
        "import os; os.environ['AWS_PROFILE']='x'; os.environ['AWS_ACCESS_KEY_ID']='AKIAEXAMPLE'\n"
        "from tools.local_oauth import real; real.prepare_process_env()\n"
        "import boto3; c = boto3.client('s3', region_name='ap-northeast-2')\n"
        "print(c.meta.endpoint_url); print(boto3.session.Session().get_credentials() is None)\n"
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split()
    assert lines[0] == real.DEAD_S3_ENDPOINT and lines[1] == "True"


def test_blocked_s3_fails_every_call_without_network_and_names_the_mode():
    from tools.local_oauth.serve import BlockedS3

    b = BlockedS3("실제 제공자 로컬 시험")
    for call in (lambda: b.upload_fileobj(None, "bucket", "key"), lambda: b.generate_presigned_url("get_object"),
                 lambda: b.put_object(Bucket="pixlate-storage-2026", Key="k", Body=b"x"), lambda: b.delete_object(Bucket="b", Key="k")):
        with pytest.raises(RuntimeError, match="실제 제공자 로컬 시험: S3 호출 차단"):
            call()
