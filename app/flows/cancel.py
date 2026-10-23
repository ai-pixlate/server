"""✕ 전체 작업 취소 = 콘텐츠 삭제(D9-4). 중단(JOB-07, 입력 유지)과 다르다.

순서
1) job 잠금 아래 archived 로 바꾸고 모든 진행 실행을 중단한다(시도 cancelled·세대+1·원격 토큰 해시 제거). 이후 권한 획득·갱신·
   업로드 URL·인계 등록·채택이 모두 거절된다 — 늦은 결과가 콘텐츠를 다시 남기지 않는다.
2) 같은 트랜잭션에서 이미지·문구가 담긴 DB 자료를 지운다: 섹션(블록·판정·산출물 연결 포함)·원본·산출물·내보내기·수정 기록·키워드·
   인계 payload·산출물 행·event_log·audit_log.detail(콘텐츠 없는 행위자·시각·대상 행은 남김)·시도 오류 메시지·상품명.
   정상 작업의 스냅샷 보존 규칙을 취소 콘텐츠 보존 근거로 쓰지 않는다.
3) 커밋 후 S3 를 job 접두사 단위로 지운다(원본·검증·스테이징·산출물·내보내기). 실패하거나 늦은 업로드가 생겨도 복구 프로세스가
   최근 취소 작업의 접두사를 다시 지운다(app.recovery.purge_archived).
GPU 로컬 자료: 상태 조회가 discard=True 를 돌려 GPU 워커가 지운다(app.flows.gpu_worker).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import execution
from app.flows import common
from app.storage import get_store

log = logging.getLogger(__name__)


def job_prefixes(job_id: int) -> list[str]:
    return [f"source/{job_id}/", f"verified/{job_id}/", f"staging/{job_id}/", f"deliverable/job-{job_id}/", f"export/{job_id}/",
            f"inpaint/job-{job_id}/"]


def cancel_job(db: Session, job_id: int, seller_id: int) -> dict[str, Any] | None:
    if not db.execute(text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"), {"j": job_id, "s": seller_id}).first():
        return None
    job = execution.lock_job(db, job_id)
    keys: list[str] = []
    if job["status"] != "archived":
        db.execute(text("UPDATE job SET status = 'archived', user_facing_status = 'archived', updated_at = now() WHERE id = :j"),
                   {"j": job_id})
    for run in db.execute(
        text("SELECT id FROM job_async_task WHERE job_id = :j AND parent_task_id IS NULL AND execution_schema_version = 1 "
             "AND status IN ('pending', 'running') ORDER BY id"), {"j": job_id}
    ).all():
        execution.cancel_run(db, run[0])
    db.execute(text("UPDATE job_async_task SET status = 'cancelled', finished_at = now() "
                    "WHERE job_id = :j AND execution_schema_version = 0 AND status IN ('pending', 'running')"), {"j": job_id})
    # 참조 키 수집(접두사 밖 레거시 키 포함)
    for sql in (
        "SELECT file_url FROM source_image WHERE job_id = :j",
        "SELECT unnest(ARRAY[image_key, inpaint_image_url, mask_image_url, render_image_key]) FROM section WHERE job_id = :j",
        "SELECT image_url FROM deliverable WHERE job_id = :j",
        "SELECT file_url FROM export_artifact WHERE job_id = :j",
        "SELECT unnest(ARRAY[staging_key, verified_key]) FROM task_artifact WHERE job_id = :j",
    ):
        keys += [r[0] for r in db.execute(text(sql), {"j": job_id}).all() if r[0]]
    section_ids = [r[0] for r in db.execute(text("SELECT id FROM section WHERE job_id = :j"), {"j": job_id}).all()]
    # audit: 콘텐츠(detail) 제거, 행위자·시각·대상은 유지
    db.execute(
        text(
            "UPDATE audit_log SET detail = NULL WHERE detail IS NOT NULL AND ("
            "(target_type = 'job' AND target_id = :j) OR (target_type = 'section' AND target_id = ANY(:sids)) "
            "OR (detail->'execution'->>'job_id') = CAST(:j AS text))"
        ),
        {"j": job_id, "sids": section_ids},
    )
    db.execute(text("UPDATE job SET current_analysis_task_id = NULL WHERE id = :j"), {"j": job_id})
    for sql in (
        "DELETE FROM task_handoff WHERE job_id = :j",  # payload·산출물 행(CASCADE)
        "DELETE FROM deliverable WHERE job_id = :j",
        "DELETE FROM section WHERE job_id = :j",  # 블록·판정·산출물 연결 CASCADE
        "DELETE FROM source_image WHERE job_id = :j",
        "DELETE FROM export_artifact WHERE job_id = :j",
        "DELETE FROM edit_signal WHERE job_id = :j",
        "DELETE FROM job_keyword WHERE job_id = :j",
        "DELETE FROM event_log WHERE job_id = :j",
        "UPDATE job_async_task SET error_message = NULL WHERE job_id = :j AND error_message IS NOT NULL",
        "UPDATE job SET product_name = NULL, product_code = NULL WHERE id = :j",
    ):
        db.execute(text(sql), {"j": job_id})
    common.write_audit(db, action_type=common.ACT_JOB_CONTENT_DELETED, target_type="job", target_id=job_id, detail=None,
                       actor_type=common.ACTOR_SELLER, actor_id=seller_id)
    db.commit()
    purge_storage(job_id, keys)
    return {"jobId": job_id, "status": "archived"}


def purge_storage(job_id: int, keys: list[str] | None = None) -> int:
    store = get_store()
    n = 0
    for p in job_prefixes(job_id):
        try:
            n += store.delete_prefix(p)
        except Exception:  # noqa: BLE001 — 복구 프로세스가 다시 지운다
            log.exception("콘텐츠 삭제 실패 prefix=%s", p)
    for k in keys or []:
        try:
            store.delete(k)
            n += 1
        except Exception:  # noqa: BLE001
            log.exception("콘텐츠 삭제 실패 key=%s", k)
    return n
