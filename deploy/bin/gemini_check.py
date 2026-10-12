"""Gemini 설정 검증 — 키 존재 확인 또는 요청 1회. 키·응답 본문·오류 원문은 출력하지 않는다.

    python /opt/pixlate/bin/gemini_check.py --presence-only   # 요청 없이 키 주입 여부만
    python /opt/pixlate/bin/gemini_check.py                   # 실제 요청: SDK 총 시도 1회(재시도 없음)

HttpRetryOptions(attempts=1) 은 "첫 시도 포함 총 1회"다(오프라인 503 서버로 시도 횟수 1 확인).
실패해도 이 스크립트를 다시 실행하지 않는다.
"""
import os
import sys

sys.path.insert(0, os.getcwd())  # 스크립트는 레포 밖에 있으므로 작업 디렉터리(레포 루트)의 app·pipeline 을 import 한다


def http_options(base_url=None):
    from google.genai import types as t

    return t.HttpOptions(timeout=30_000, retry_options=t.HttpRetryOptions(attempts=1), base_url=base_url)


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("GEMINI_API_KEY: missing")
        return 3
    print("GEMINI_API_KEY: present")
    if "--presence-only" in sys.argv:
        return 0
    from google import genai

    from pipeline.config import load_config

    model = load_config()["label"]["model"]
    client = genai.Client(api_key=key, http_options=http_options())
    try:
        r = client.models.generate_content(model=model, contents="Reply with the single word OK.")
    except Exception as e:  # noqa: BLE001 — 오류 메시지 원문은 출력하지 않는다
        print("request: failed | model:", model, "|", type(e).__name__, getattr(e, "code", None))
        return 4
    print("request: ok | model:", model, "| text_nonempty:", bool((getattr(r, "text", "") or "").strip()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
