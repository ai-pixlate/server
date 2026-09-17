"""Celery 태스크 (비동기 뼈대).

실제 OCR/분석 모델은 아직 없어, run_analyze 는 파이프라인 "배선"만 시연한다:
job 상태 전이 → job_async_task 생성 → (스텁 처리) → 섹션 1개 생성 → 완료 전이.
워커는 자체 DB 세션(SessionLocal)을 연다(FastAPI 요청 세션과 무관).
"""
import time

from sqlalchemy import text

from app.celery_app import celery_app
from app.db import SessionLocal


@celery_app.task(name="app.tasks.run_analyze")
def run_analyze(job_id: int) -> dict:
    db = SessionLocal()
    try:
        # 1) job → 처리중(N2)
        db.execute(
            text(
                "UPDATE job SET status='processing', current_step='N2', "
                "user_facing_status='analyzing', updated_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
        # 2) 비동기 태스크 행 생성(running)
        row = db.execute(
            text(
                "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, started_at) "
                "VALUES (:j, 'ocr', 'job', :j, 'running', now()) RETURNING id"
            ),
            {"j": job_id},
        ).mappings().one()
        task_row_id = row["id"]
        db.commit()

        # 3) 실제 OCR/분석 대체(스텁) — 처리 시간 시뮬레이션
        time.sleep(3)

        # 4) 산출물(섹션) 1개 생성 (source_image 없음 → 0, FK 미적용)
        db.execute(
            text(
                "INSERT INTO section (job_id, source_image_id, section_order, bucket) "
                "VALUES (:j, 0, 1, 'include')"
            ),
            {"j": job_id},
        )
        # 5) 태스크 완료 + job → 검수대기(N3)
        db.execute(
            text("UPDATE job_async_task SET status='done', finished_at=now() WHERE id=:t"),
            {"t": task_row_id},
        )
        db.execute(
            text(
                "UPDATE job SET status='review', current_step='N3', "
                "user_facing_status='section_review', updated_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
        db.commit()
        return {"jobId": job_id, "producedSections": 1}
    except Exception:
        db.rollback()
        db.execute(
            text("UPDATE job SET status='failed', updated_at=now() WHERE id=:id"),
            {"id": job_id},
        )
        db.commit()
        raise
    finally:
        db.close()
