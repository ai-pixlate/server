"""로그인 허용 목록 — 시험 기간 운영 설정(PM 11-18 결정, 12/7 최종 발표까지 켜 둔다. PRD 밖).

배경: Google 「테스트 중」 상태라도 openid·email·profile 만 요청하면 테스트 사용자 목록 밖 계정이 로그인된다(Google 문서의 예외).
카카오 일반 앱은 원래 누구나 로그인된다. 그래서 팀원 한정을 서버에서 건다.

설정(환경 변수 — 값은 로그·응답에 남기지 않는다):
    PIXLATE_AUTH_ALLOWLIST         스위치. enabled | disabled 만. 미설정 = disabled(기존 동작 — 누구나).
                                   그 밖의 값(빈 문자열 포함)은 API 기동 실패. 메시지에는 설정 이름과 허용 값만 싣는다.
    PIXLATE_AUTH_ALLOWED_EMAILS    허용 이메일 목록(쉼표). 앞뒤 공백을 지우고 소문자로 비교한다.
                                   스위치가 켜져 있는데 목록이 비었거나 없으면 **아무도 허용하지 않는다**(설정 누락이 조용히 전부 여는 것을 막는다 — PM 11-18).

판정 대상 이메일은 호출부가 정한다 — 소셜 로그인은 제공자가 확인한 이메일(없으면 저장된 계정 이메일), 갱신·간이 로그인은 계정 이메일.
이메일이 없으면(None·빈 값) 목록과 맞출 수 없으므로 켜진 상태에서는 거절이다.
요청마다 환경 변수를 읽는다(다른 인증 설정과 같은 방식) — 재기동 없이 값을 바꾸는 운영은 하지 않지만 시험에서 바꿔 끼울 수 있다.
로그인 기록 로그(사용자 11-19): 스위치가 켜져 있을 때만 로그인 · 계정 생성 · 거절 로그에 판정 · 처리에 쓴 이메일을 남긴다(log_email).
목록 설정값 자체는 로그에 남기지 않는다. 스위치를 꺼도 이미 남은 로그는 지워지지 않는다.
"""
import os
from typing import Optional

SWITCH_ENV = "PIXLATE_AUTH_ALLOWLIST"
EMAILS_ENV = "PIXLATE_AUTH_ALLOWED_EMAILS"
SWITCH_VALUES = ("enabled", "disabled")
REASON = "account_not_allowed"


class AllowlistConfigError(RuntimeError):
    """PIXLATE_AUTH_ALLOWLIST 값이 허용 값이 아님(빈 문자열 포함). 입력값은 메시지에 넣지 않는다."""


def enabled() -> bool:
    raw = os.environ.get(SWITCH_ENV)
    if raw is None or raw == "disabled":
        return False
    if raw == "enabled":
        return True
    raise AllowlistConfigError(f"{SWITCH_ENV} 설정 오류 — 허용 값: {' · '.join(SWITCH_VALUES)} (미설정 = disabled)")


def allowed_emails() -> frozenset[str]:
    raw = os.environ.get(EMAILS_ENV) or ""
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_allowed(email: Optional[str]) -> bool:
    """스위치가 꺼져 있으면 항상 True. 켜져 있으면 이메일이 목록에 있을 때만 True(빈 목록 = 모두 False)."""
    if not enabled():
        return True
    if not isinstance(email, str) or not email.strip():
        return False
    return email.strip().lower() in allowed_emails()


def log_email(email: Optional[str]) -> str:
    """로그인 기록 로그에 붙일 이메일 조각(사용자 11-19 결정 — 「허용 목록 활성화 시 이메일 기록」, dev 전용 규칙이 아니다).
    스위치가 켜져 있으면 ` email=<repr>` — 판정·처리에 쓴 이메일을 %r 로(따옴표 안, 줄바꿈·제어문자는 이스케이프되어 한 줄 유지),
    꺼져 있으면 빈 문자열(이메일 필드 자체를 생략). 스위치를 꺼도 이미 남은 로그는 지워지지 않는다."""
    return f" email={email!r}" if enabled() else ""
