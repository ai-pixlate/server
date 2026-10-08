"""단계 흐름 — 각 모듈이 import 될 때 execution 에 단계 핸들러를 등록한다."""
from app.flows import analysis  # noqa: F401


def retry_plan(db, attempt: dict) -> dict:
    """사용자 재시도의 새 고정 입력. 단계별 규칙(⑧ 실패 대상만 등)이 있으면 그 모듈이 정한다."""
    if attempt["stage"] == "translate":
        from app.flows.downstream import translate_retry_plan

        return translate_retry_plan(db, attempt)
    return {}
