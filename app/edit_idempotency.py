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
