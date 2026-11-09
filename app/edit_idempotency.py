"""편집 저장(블록·섹션 PATCH) Idempotency-Key — 계약 docs/openapi.yaml blockPatch 「저장 공통」.

POST /jobs(app.idempotency, Redis)와 다르다:
- 기록은 DB(edit_idempotency)에 두고 값 변경과 **같은 트랜잭션**으로 커밋한다 — 변경만 남거나 기록만 남지 않는다.
- 같은 키라도 **요청이 다르면**(오퍼레이션·대상 ID·정규화한 본문 중 하나라도) 최초 응답을 주지 않고 409 DUPLICATE 로 거절한다.
- 같은 작업의 저장은 작업 행 잠금으로 직렬화되므로 동시 재전송도 앞 요청이 커밋된 뒤 기록을 본다.
- 2xx 응답만 기록한다(no-op 200 포함). 보관 24시간 — 지난 기록은 없는 것으로 보고 새 요청으로 처리한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

TTL_HOURS = 24
KEY_PATTERN = r"^[!-~]+$"  # 공백 없는 ASCII(계약 EditIdempotencyKey), 길이 1~128
REPLAYED_HEADER = "Idempotent-Replayed"


def lock_timeout_ms() -> int:
    """작업 행 잠금 대기 한도(ms). 넘으면 503 SERVICE_UNAVAILABLE(retryable). 계약은 값 자체를 정하지 않는다."""
    return int(os.getenv("PIXLATE_EDIT_LOCK_TIMEOUT_MS", "5000"))


def request_sha256(operation: str, target: dict[str, Any], body: dict[str, Any]) -> str:
    """같은 요청 판별값 — 오퍼레이션 + 경로의 대상 ID(작업·블록 등) + 정규화한 본문(키 정렬, 생략과 null 구분)."""
    canon = json.dumps({"op": operation, "target": target, "body": body}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def lookup(db: Session, *, seller_id: int, job_id: int, operation: str, key: str) -> dict[str, Any] | None:
    """보관 기한 안의 기록. 호출자가 작업 행을 잠근 뒤 부른다."""
    r = db.execute(
        text("SELECT request_sha256, status_code, response FROM edit_idempotency "
             "WHERE seller_id = :s AND job_id = :j AND operation = :o AND idem_key = :k AND expires_at > now()"),
        {"s": seller_id, "j": job_id, "o": operation, "k": key},
    ).mappings().first()
    return dict(r) if r else None


def record(db: Session, *, seller_id: int, job_id: int, operation: str, key: str, sha: str, status_code: int,
           response: dict[str, Any]) -> None:
    """성공 응답을 기록한다(호출자의 트랜잭션 안). 기한이 지난 같은 키 기록은 덮어쓴다."""
    db.execute(
        text(
            "INSERT INTO edit_idempotency (seller_id, job_id, operation, idem_key, request_sha256, status_code, response, expires_at) "
            "VALUES (:s, :j, :o, :k, :h, :c, CAST(:r AS jsonb), now() + make_interval(hours => :ttl)) "
            "ON CONFLICT (seller_id, job_id, operation, idem_key) DO UPDATE SET "
            "request_sha256 = EXCLUDED.request_sha256, status_code = EXCLUDED.status_code, response = EXCLUDED.response, "
            "created_at = now(), expires_at = EXCLUDED.expires_at WHERE edit_idempotency.expires_at <= now()"
        ),
        {"s": seller_id, "j": job_id, "o": operation, "k": key, "h": sha, "c": status_code,
         "r": json.dumps(response, ensure_ascii=False), "ttl": TTL_HOURS},
    )


def delete_for_job(db: Session, job_id: int) -> None:
    db.execute(text("DELETE FROM edit_idempotency WHERE job_id = :j"), {"j": job_id})


def purge_expired(db: Session) -> int:
    return db.execute(text("DELETE FROM edit_idempotency WHERE expires_at <= now()")).rowcount or 0


# ---- 블록·섹션 PATCH 공통(라우터가 같은 처리 순서를 쓰게) --------------------------------------------------------
LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL lock_not_available(lock_timeout 초과)


def integer_value(v):
    """OpenAPI `integer`와 같은 판단 — 정수 값인 JSON 수만(0·0.0은 같은 0). 불리언·문자열·0.5·null 은 ValueError(→ 422)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("must be an integer")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError("must be an integer")
        return int(v)
    return v


def busy_error():
    from fastapi import HTTPException

    return HTTPException(status_code=503, detail={"code": "SERVICE_UNAVAILABLE", "message": "job is busy — retry with the same request"})


@contextmanager
def lock_window(db: Session):
    """이 블록 안의 잠금 대기를 lock_timeout_ms 로 제한한다. 넘으면 트랜잭션을 정리(rollback)하고 503(retryable).
    블록이 정상으로 끝나면 원래 한도로 되돌린다(잠금을 얻은 뒤의 처리는 기존 한도)."""
    from sqlalchemy.exc import DBAPIError

    orig = db.execute(text("SELECT current_setting('lock_timeout')")).scalar()
    db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": f"{lock_timeout_ms()}ms"})
    try:
        yield
    except DBAPIError as e:
        db.rollback()
        if getattr(e.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
            raise busy_error() from e
        raise
    db.execute(text("SELECT set_config('lock_timeout', :v, true)"), {"v": orig})


def replay_or_reject(db: Session, *, seller_id: int, job_id: int, operation: str, key: str | None, sha: str):
    """키가 있고 기한 안의 기록이 있으면: 같은 요청 → 최초 응답(재전송 헤더), 다른 요청 → 409 DUPLICATE. 그 밖엔 None(계속 처리).
    호출자가 작업 행을 잠그고 작업 상태를 확인한 뒤 부른다(재전송이 소유·상태 검사를 건너뛰지 않게)."""
    if key is None:
        return None
    prev = lookup(db, seller_id=seller_id, job_id=job_id, operation=operation, key=key)
    if prev is None:
        return None
    db.rollback()
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    if prev["request_sha256"] != sha:
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE", "message": "Idempotency-Key was used for a different request",
                                                     "reason": "idempotency_key_reused"})
    return JSONResponse(prev["response"], status_code=prev["status_code"], headers={REPLAYED_HEADER: "true"})
