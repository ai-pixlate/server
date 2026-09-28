"""run_inpaint 멱등성 — acks_late 재전달로 두 번 실행돼도 태스크 행이 중복되지 않아야 한다.

실제 DB 필요(없으면 모듈 전체 skip). 태스크 함수를 워커 없이 직접 호출한다.
컨테이너에서 실행: docker exec pixlate-api python -m pytest tests/test_inpaint_idempotency.py -q
"""
import uuid

import pytest
from sqlalchemy import text


def _db_ok() -> bool:
    try:
        from app.db import engine
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_ok(), reason="database not reachable")

from app.db import SessionLocal  # noqa: E402
from app.tasks import run_inpaint  # noqa: E402


@pytest.fixture
def section_id(monkeypatch):
    monkeypatch.setattr("app.tasks.time.sleep", lambda _s: None)  # 스텁 대기 생략
    db = SessionLocal()
    email = f"inpaint-{uuid.uuid4().hex[:8]}@example.com"
    seller = db.execute(
        text("INSERT INTO seller (cognito_sub, email) VALUES (:sub, :e) RETURNING id"),
        {"sub": f"local:{email}", "e": email},
    ).scalar()
    brand = db.execute(
        text("INSERT INTO brand (seller_id, name_ko, name_en) VALUES (:s, '테스트', 'Test') RETURNING id"),
        {"s": seller},
    ).scalar()
    job = db.execute(
        text("INSERT INTO job (seller_id, brand_id, product_name) VALUES (:s, :b, 'inpaint test') RETURNING id"),
        {"s": seller, "b": brand},
    ).scalar()
    sec = db.execute(
        text(
            "INSERT INTO section (job_id, source_image_id, section_order, bucket, inpaint_status) "
            "VALUES (:j, 0, 1, 'include', 'pending') RETURNING id"
        ),
        {"j": job},
    ).scalar()
    db.commit()
    yield sec
    db.execute(text("DELETE FROM job WHERE id = :j"), {"j": job})  # section·job_async_task CASCADE
    db.execute(text("DELETE FROM brand WHERE id = :b"), {"b": brand})
    db.execute(text("DELETE FROM seller WHERE id = :s"), {"s": seller})
    db.commit()
    db.close()


def _inpaint_rows(section: int) -> list:
    db = SessionLocal()
    try:
        return db.execute(
            text(
                "SELECT id, status FROM job_async_task "
                "WHERE task_type = 'inpaint' AND unit_type = 'section' AND unit_id = :sid ORDER BY id"
            ),
            {"sid": section},
        ).mappings().all()
    finally:
        db.close()


def _section_status(section: int) -> str:
    db = SessionLocal()
    try:
        return db.execute(
            text("SELECT inpaint_status FROM section WHERE id = :sid"), {"sid": section}
        ).scalar()
    finally:
        db.close()


def test_twice_creates_single_task_row(section_id):
    first = run_inpaint(section_id)
    second = run_inpaint(section_id)

    assert first["inpaintStatus"] == "done"
    assert second == {"sectionId": section_id, "inpaintStatus": "done", "alreadyDone": True}
    rows = _inpaint_rows(section_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert _section_status(section_id) == "done"


def test_redelivery_resumes_running_row(section_id):
    # 앞선 실행이 처리 도중 워커와 함께 죽어 running 행만 남은 상황
    db = SessionLocal()
    stale = db.execute(
        text(
            "INSERT INTO job_async_task (job_id, task_type, unit_type, unit_id, status, started_at) "
            "SELECT job_id, 'inpaint', 'section', id, 'running', now() FROM section WHERE id = :sid "
            "RETURNING id"
        ),
        {"sid": section_id},
    ).scalar()
    db.execute(text("UPDATE section SET inpaint_status = 'running' WHERE id = :sid"), {"sid": section_id})
    db.commit()
    db.close()

    result = run_inpaint(section_id)

    assert result["inpaintStatus"] == "done"
    rows = _inpaint_rows(section_id)
    assert [r["id"] for r in rows] == [stale]  # 새 행 없이 남은 행을 이어서 씀
    assert rows[0]["status"] == "done"
    assert _section_status(section_id) == "done"


def test_missing_section_is_noop():
    assert run_inpaint(2_000_000_000) == {"sectionId": 2_000_000_000, "inpaintStatus": "missing"}
