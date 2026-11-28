"""Celery 태스크 — 메시지에는 시도 id(job_async_task.id)만 담는다(integration-decisions.md 5.32 전달 수단).

브로커는 깨우기 신호일 뿐이다. 실행 여부·입력·결과 반영은 DB의 시도 행과 실행 권한(lease)으로 판단한다
(app.execution). 중복 전달·재전달은 권한 획득 조건부 갱신에서 하나만 실행된다.

| 태스크 | 큐 | 단계 |
|---|---|---|
| run_analyze | ocr | ①②③ + 고정 사전 묶음 |
| run_judge | cpu | ③-1·③-1′ (섹션별) |
| run_label · run_logo · run_style | cpu | ④ · ⑤ · ⑦ |
| run_inpaint | gpu | ⑥ (원격 GPU 어댑터 — app.flows.gpu_worker) |
| run_translate | cpu | ⑧ |
| run_preview | cpu | ⑨ 초기 미리보기(섹션별) |
| run_text_check | cpu | ⑧ 번역문 규칙 검사(섹션별, 모델 호출 없음) |
| run_render | cpu | N6 최종 렌더 |
| run_adopt | adopt | 원격 인계 검증·채택(모델 · 렌더 없음 — 전용 워커) |

run_preview · run_render 는 시도를 임대하기 전에 FE 내부 렌더 경로 준비를 확인하고, 준비 전이면 시도를 잡지 않고 미룬다(_defer_render).
"""
import logging

from app.celery_app import celery_app

log = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.run_analyze")
def run_analyze(attempt_id: int) -> dict:
    from app.flows.analysis import execute_analyze

    return execute_analyze(attempt_id)


@celery_app.task(name="app.tasks.run_judge")
def run_judge(attempt_id: int) -> dict:
    from app.flows.analysis import execute_judge

    return execute_judge(attempt_id)


@celery_app.task(name="app.tasks.run_label")
def run_label(attempt_id: int) -> dict:
    from app.flows.downstream import execute_label

    return execute_label(attempt_id)


@celery_app.task(name="app.tasks.run_logo")
def run_logo(attempt_id: int) -> dict:
    from app.flows.downstream import execute_logo

    return execute_logo(attempt_id)


@celery_app.task(name="app.tasks.run_style")
def run_style(attempt_id: int) -> dict:
    from app.flows.downstream import execute_style

    return execute_style(attempt_id)


@celery_app.task(name="app.tasks.run_inpaint")
def run_inpaint(attempt_id: int) -> dict:
    from app.flows.gpu_worker import execute_inpaint_remote

    return execute_inpaint_remote(attempt_id)


@celery_app.task(name="app.tasks.run_translate")
def run_translate(attempt_id: int) -> dict:
    from app.flows.downstream import execute_translate

    return execute_translate(attempt_id)


def _defer_render(stage: str, attempt_id: int) -> dict | None:
    """⑨ 미리보기 · N6 최종 렌더: 시도를 임대하기 전에 FE 내부 렌더 경로 준비를 본다(D.3).
    준비되지 않았으면 시도를 잡지 않고 끝낸다 — 시도는 pending 그대로이고 실패 · 재시도 소모 · 이미지 변경이 없다.
    다시 보내는 것은 복구 sweep(recovery._pending — 마지막 전달 뒤 PIXLATE_DISPATCH_STALE_S, 기본 300초)이다.
    여기서 다시 큐에 넣지 않는다(대기 시도가 많을 때 메시지가 불어나지 않게). 시도를 잡은 뒤의 실패는 기존 실패 경로로 간다."""
    from app import html_render

    reason = html_render.ensure_ready()
    if reason is None:
        return None
    log.warning("렌더 미룸(FE 내부 렌더 준비 안 됨) stage=%s attempt=%s 연속 %s회 사유=%s",
                stage, attempt_id, html_render._DEFERRED["count"], reason)
    return {"deferred": True, "stage": stage, "attempt_id": attempt_id}


@celery_app.task(name="app.tasks.run_preview")
def run_preview(attempt_id: int) -> dict:
    from app.flows.downstream import execute_preview

    if (deferred := _defer_render("preview", attempt_id)) is not None:
        return deferred
    return execute_preview(attempt_id)


@celery_app.task(name="app.tasks.run_text_check")
def run_text_check(attempt_id: int) -> dict:
    from app.flows.text_check import execute_text_check

    return execute_text_check(attempt_id)


@celery_app.task(name="app.tasks.run_render")
def run_render(attempt_id: int) -> dict:
    from app.flows.final import execute_final_render

    if (deferred := _defer_render("final_render", attempt_id)) is not None:
        return deferred
    return execute_final_render(attempt_id)


@celery_app.task(name="app.tasks.run_adopt")
def run_adopt(handoff_id: int) -> dict:
    from app.flows.gpu_worker import adopt_remote_handoff

    return adopt_remote_handoff(handoff_id)
