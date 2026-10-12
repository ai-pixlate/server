"""로그 정제 래퍼 — EC2 컨테이너와 학교 GPU 워커 공용. 표준 라이브러리만 쓴다.

    python /opt/pixlate/bin/redact_run.py -- celery -A app.celery_app:celery_app worker -Q cpu --pool=solo ...

- 자식의 stdout·stderr 를 파이프로 받아 한 줄씩 정제한 뒤에만 내보낸다. 원문은 이 프로세스 메모리 밖으로 나가지 않는다
  (Docker 로그·journald·파일에는 정제된 줄만 도달한다).
- 정제 1단계: 이 프로세스 환경에 있는 비밀 변수의 실제 값(및 URL 의 비밀번호·호스트)을 문자열 그대로 가린다.
  정제 2단계: 연결 URL·AWS 호스트·서명·키 형태·password= 류·IPv4 를 정규식으로 가린다.
- 한 줄이 MAX_LINE 을 넘으면 나머지를 버리고 표시만 남긴다(경계에서 비밀이 잘려 새는 것을 막는다).
- 정제 중 오류가 나면 그 줄은 버리고 표시만 남긴다. 원문을 대신 내보내지 않는다.
- SIGTERM·SIGINT·SIGQUIT·SIGHUP·SIGUSR1·SIGUSR2 를 자식에게 그대로 전달한다(Celery 웜·콜드 종료 유지).
  docker run --init(tini) → 래퍼 → 자식 순으로 전달되고 좀비는 tini 가 거둔다.
- 자식이 끝나면 같은 종료 코드로, 시그널로 끝났으면 128+시그널 번호로 종료한다.
- 출력 자체를 쓸 수 없게 되면(로그 수집기 단절 등) 자식을 정리한다(SIGTERM → 유예 → SIGKILL).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

FORWARD = [signal.SIGTERM, signal.SIGINT, signal.SIGQUIT, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2]
KILL_GRACE_S = 20
MAX_LINE = 64 * 1024

# 값 자체를 가릴 환경 변수(값은 출력하지 않는다). 없는 이름은 건너뛴다
SECRET_ENV = (
    "DATABASE_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "REDIS_PASSWORD", "JWT_SECRET",
    "GEMINI_API_KEY", "S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
)

PATTERNS = [
    # 자격·주소가 들어갈 수 있는 연결 URL 전체
    (re.compile(r"\b(?:rediss?|redis\+socket|postgres(?:ql)?(?:\+\w+)?|mysql(?:\+\w+)?|amqps?|sqla\+\w+|s3)://[^\s'\"<>]+", re.I), "<redacted-url>"),
    # AWS·presigned URL(서명·버킷·키 노출)
    (re.compile(r"https?://[^\s'\"<>]*amazonaws\.com[^\s'\"<>]*", re.I), "<redacted-url>"),
    (re.compile(r"\b[\w.-]+\.amazonaws\.com\b", re.I), "<redacted-host>"),
    (re.compile(r"X-Amz-(?:Signature|Credential|Security-Token)=[^&\s]+", re.I), "X-Amz-<redacted>"),
    # 키 형태
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted-aws-key-id>"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "<redacted-api-key>"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|requirepass)\b(\s*[=:]\s*|\s+)([^\s,;'\"]+)"), r"\1\2<redacted>"),
    # IPv4
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
]


def secret_literals(env=os.environ) -> list[str]:
    out: set[str] = set()
    for name in SECRET_ENV:
        v = env.get(name)
        if not v:
            continue
        out.add(v)
        if "://" in v:
            try:
                u = urlsplit(v)
                for part in (u.password, u.hostname, u.netloc):
                    if part:
                        out.add(part)
                        out.add(unquote(part))
            except ValueError:
                pass
    # 너무 짧은 값은 일반 단어를 가릴 수 있어 제외한다(정규식 단계가 형태로 다시 가린다)
    return sorted((s for s in out if len(s) >= 6), key=len, reverse=True)


LITERALS = secret_literals()


def redact(line: str) -> str:
    for s in LITERALS:
        if s in line:
            line = line.replace(s, "<redacted>")
    for pat, rep in PATTERNS:
        line = pat.sub(rep, line)
    return line


def lines(stream):
    """한 줄씩 낸다. MAX_LINE 을 넘는 줄은 앞부분만 남기고 나머지는 줄 끝까지 버린다."""
    while True:
        raw = stream.readline(MAX_LINE)
        if not raw:
            return
        if len(raw) == MAX_LINE and not raw.endswith(b"\n"):
            while True:  # 같은 줄의 나머지 버림
                rest = stream.readline(MAX_LINE)
                if not rest or rest.endswith(b"\n"):
                    break
            yield None
            continue
        yield raw


def main(argv: list[str]) -> int:
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    if not argv:
        sys.stderr.write("usage: redact_run.py -- CMD [ARGS...]\n")
        return 2
    try:
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    except OSError as e:
        sys.stderr.write(f"redact_run: 실행 실패: {e.__class__.__name__}\n")
        return 127

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for s in FORWARD:
        signal.signal(s, forward)

    out = sys.stdout.buffer
    try:
        assert child.stdout is not None
        for raw in lines(child.stdout):
            if raw is None:
                safe = "redact_run: <긴 줄 생략>\n"
            else:
                try:
                    safe = redact(raw.decode("utf-8", errors="replace"))
                except Exception as e:  # noqa: BLE001 — 정제 실패 줄은 버린다(원문을 내보내지 않는다)
                    safe = f"redact_run: <정제 오류로 줄 생략 {e.__class__.__name__}>\n"
            out.write(safe.encode("utf-8", errors="replace"))
            out.flush()
    except Exception as e:  # noqa: BLE001 — 출력 불가 시에도 자식을 정리한다
        try:
            sys.stderr.write(f"redact_run: 출력 처리 오류 {e.__class__.__name__}\n")
        except Exception:  # noqa: BLE001
            pass
        if child.poll() is None:
            child.terminate()
            deadline = time.monotonic() + KILL_GRACE_S
            while child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            if child.poll() is None:
                child.kill()
    rc = child.wait()
    return 128 - rc if rc < 0 else rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
