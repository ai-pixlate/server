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
| run_render | cpu | N6 최종 렌더 |
| run_adopt | cpu | 원격 인계 검증·채택 |
"""
from app.celery_app import celery_app


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


@celery_app.task(name="app.tasks.run_preview")
def run_preview(attempt_id: int) -> dict:
    from app.flows.downstream import execute_preview

    return execute_preview(attempt_id)


@celery_app.task(name="app.tasks.run_render")
def run_render(attempt_id: int) -> dict:
    from app.flows.final import execute_final_render

    return execute_final_render(attempt_id)


@celery_app.task(name="app.tasks.run_adopt")
def run_adopt(handoff_id: int) -> dict:
    from app.flows.gpu_worker import adopt_remote_handoff

    return adopt_remote_handoff(handoff_id)
