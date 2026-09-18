"""Celery 태스크 (비동기 뼈대).

실제 OCR/분석 모델은 아직 없어, run_analyze/run_translate 는 파이프라인 "배선"만
시연한다. run_render 는 실제 렌더 엔진(Pillow 조판 + 규격 검증 + S3 업로드)이다.
워커는 자체 DB 세션(SessionLocal)을 연다(FastAPI 요청 세션과 무관).
"""
import io
import json
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


@celery_app.task(name="app.tasks.run_translate")
def run_translate(job_id: int) -> dict:
    """N4 번역(스텁): 포함된 섹션마다 text_block 1개 생성 → 완료 후 N5(검수)."""
    db = SessionLocal()
    try:
        # 1) job → 번역중(N4)
        db.execute(
            text(
                "UPDATE job SET status='processing', current_step='N4', "
                "user_facing_status='translating', updated_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
        # 2) 태스크 행(running)
        row = db.execute(
            text(
                "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, started_at) "
                "VALUES (:j, 'translate', 'job', :j, 'running', now()) RETURNING id"
            ),
            {"j": job_id},
        ).mappings().one()
        task_row_id = row["id"]
        db.commit()

        # 3) 실제 번역(LLM) 대체(스텁)
        time.sleep(3)

        # 4) 포함(include) 섹션마다 번역 블록 생성
        secs = db.execute(
            text("SELECT id FROM section WHERE job_id = :j AND bucket = 'include' ORDER BY section_order, id"),
            {"j": job_id},
        ).mappings().all()
        created = 0
        for s in secs:
            db.execute(
                text(
                    "INSERT INTO text_block (section_id, block_order, role, source_ko, trans_1, block_status) "
                    "VALUES (:sid, 1, 'body', :ko, :en, 'machine')"
                ),
                {"sid": s["id"], "ko": "원문 텍스트(스텁)", "en": "Translated text (stub)"},
            )
            created += 1

        # 5) 태스크 완료 + job → 검수(N5)
        db.execute(
            text("UPDATE job_async_task SET status='done', finished_at=now() WHERE id=:t"),
            {"t": task_row_id},
        )
        db.execute(
            text(
                "UPDATE job SET status='review', current_step='N5', "
                "user_facing_status='reviewing', updated_at=now() WHERE id=:id"
            ),
            {"id": job_id},
        )
        db.commit()
        return {"jobId": job_id, "producedBlocks": created}
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


def _load_spec(db, job_id: int) -> dict:
    """작업의 채널 규격(module_spec)에서 렌더/검증 규격을 로드."""
    from app import render

    row = db.execute(
        text(
            "SELECT ms.char_limit FROM job j "
            "LEFT JOIN module_spec ms ON ms.channel_spec_id = j.channel_spec_id "
            "WHERE j.id = :j ORDER BY ms.id LIMIT 1"
        ),
        {"j": job_id},
    ).mappings().first()
    return {"maxWidth": render.CANVAS_WIDTH, "charLimit": row["char_limit"] if row else None}


@celery_app.task(name="app.tasks.run_render")
def run_render(job_id: int) -> dict:
    """N6 렌더(실제): include 섹션마다 조판→PNG→S3 업로드→규격검증→deliverable 생성.

    job 상태 전이는 하지 않는다(N6 done 전이는 FIN-06 save가 소유 · 아키텍처).
    """
    from app import render, s3

    db = SessionLocal()
    task_row_id = None
    try:
        row = db.execute(
            text(
                "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, started_at) "
                "VALUES (:j, 'render', 'job', :j, 'running', now()) RETURNING id"
            ),
            {"j": job_id},
        ).mappings().one()
        task_row_id = row["id"]
        db.commit()

        spec = _load_spec(db, job_id)
        secs = db.execute(
            text(
                "SELECT id, source_image_id FROM section "
                "WHERE job_id = :j AND bucket = 'include' ORDER BY section_order, id"
            ),
            {"j": job_id},
        ).mappings().all()

        produced = 0
        for s in secs:
            section_id = s["id"]
            blocks = db.execute(
                text(
                    "SELECT id, role, source_ko, trans_1, is_excluded, char_limit "
                    "FROM text_block WHERE section_id = :sid ORDER BY block_order, id"
                ),
                {"sid": section_id},
            ).mappings().all()
            block_dicts = [dict(b) for b in blocks]

            png, w, h = render.render_section_png(block_dicts)
            key = s3.make_key(f"deliverable/job-{job_id}", "section.png")
            s3.upload_fileobj(io.BytesIO(png), key, content_type="image/png")

            vr = render.validate_deliverable(block_dicts, w, h, spec)
            db.execute(
                text(
                    "INSERT INTO deliverable (job_id, source_image_id, usage_type, image_url, "
                    "format, color_space, file_size, render_status, validation_result) "
                    "VALUES (:j, :src, 'detail', :url, 'png', 'sRGB', :size, 'done', "
                    "CAST(:vr AS jsonb))"
                ),
                {"j": job_id, "src": s["source_image_id"], "url": key,
                 "size": len(png), "vr": json.dumps(vr)},
            )
            db.execute(
                text("UPDATE section SET render_image_key = :k, updated_at = now() WHERE id = :sid"),
                {"k": key, "sid": section_id},
            )
            # 규격 글자수 한도가 있으면 블록 char_limit·overflow 반영
            if spec.get("charLimit"):
                db.execute(
                    text(
                        "UPDATE text_block SET char_limit = :cl, "
                        "overflow = (char_length(coalesce(trans_1, '')) > :cl) "
                        "WHERE section_id = :sid AND is_excluded IS NOT TRUE"
                    ),
                    {"cl": spec["charLimit"], "sid": section_id},
                )
            produced += 1

        db.execute(
            text("UPDATE job_async_task SET status='done', finished_at=now() WHERE id=:t"),
            {"t": task_row_id},
        )
        db.commit()
        return {"jobId": job_id, "producedDeliverables": produced}
    except Exception:
        db.rollback()
        if task_row_id is not None:
            db.execute(
                text("UPDATE job_async_task SET status='failed', finished_at=now() WHERE id=:t"),
                {"t": task_row_id},
            )
            db.commit()
        raise
    finally:
        db.close()
