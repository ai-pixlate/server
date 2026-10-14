"""POST /jobs Idempotency-Key (계약 docs/openapi.yaml jobCreate · 공통 규칙 '멱등·동시성').

정책(2026-10-14 승인):
- 키 범위 = 판매자 + 작업 생성 API + 키. 다른 판매자의 같은 키와 섞이지 않는다.
- 같은 키의 완료된 요청은 본문이 달라도 최초 응답(201 Job)을 다시 보낸다. 다른 작업을 만들려면 새 키를 쓴다.
- 키 없는 요청은 그대로 처리한다(멱등 보장 없음). 기록 보관 24시간.
- 같은 키 동시 요청에서도 작업은 하나만 만든다(Redis SET NX).
- (리뷰어 권고 반영) 처리 중인 같은 키 → 409 DUPLICATE, 키를 받은 요청에서 Redis 를 쓸 수 없으면 503·생성 안 함,
  기록 위치 = Celery 결과 저장소(Redis db1)의 `pixlate:idem:` 접두어(응답 재전송 기록). 전용 설정 PIXLATE_IDEMPOTENCY_REDIS_URL 이 있으면 그쪽.

키 하나의 기록 상태:
  pending                  생성 시작(아직 작업 행 없음)
  committing(jobId, body)  작업 행과 응답 본문을 만들었고 DB 커밋 직전·중 — 커밋 결과는 DB 에서 확인할 수 있다
  done(body)               최초 응답 확정

장애 구간 처리(키 삭제는 DB 에 저장되지 않았음이 확실할 때만):
  커밋 시도 전 실패(검증·브랜드 404·INSERT 오류·committing 기록 실패) → 롤백 후 키 삭제 → 같은 키로 다시 시도 가능
  커밋 중 예외(결과 불명) → 새 연결로 작업 행 확인: 있으면 done 으로 확정하고 201 / 없거나 확인 불가면 committing 유지(삭제 안 함)
  커밋 후 Redis 저장 실패 → committing 유지. 같은 키 재요청은 DB 에서 작업 행을 확인해 저장해 둔 최초 응답을 재전송하고 done 으로 확정
  committing 인데 작업 행이 없다(커밋 실패 또는 아직 커밋 중) → 같은 키 재요청은 409 DUPLICATE(중복 생성하지 않는다)

보장 범위: "같은 키로 중복 생성 없음"은 위 장애 구간(시험함)과 **Redis 기록이 유지되는 동안**의 결과다.
Redis 기록 자체가 유실되면(비우기·교체·장애로 키 소실) 같은 키 재요청이 새 작업을 만들 수 있다 — 이 구현은 그것까지 보장하지 않는다.
pending·미확정 committing 도 24시간 유지한다(짧게 만료시키면 기존 요청이 살아 있는 동안 다른 요청이 생성을 시작할 수 있다).
결과 불명 응답(500)을 받은 클라이언트가 새 키로 자동 재시도하면 중복될 수 있다 — 생성 여부를 확인하지 않고 새 키를 쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import redis

TTL_S = 24 * 60 * 60
_client: redis.Redis | None = None


class InFlight(Exception):
    """같은 키의 요청이 처리 중이다(pending)."""


class Unavailable(Exception):
    """Redis 를 쓸 수 없다."""


@dataclass
class Record:
    state: str                      # "new"(이 요청이 차지함) | "committing" | "done"
    job_id: Optional[int] = None
    body: Optional[dict[str, Any]] = None


def _url() -> str:
    return os.getenv("PIXLATE_IDEMPOTENCY_REDIS_URL") or os.getenv("CELERY_RESULT_BACKEND", "redis://pixlate-redis:6379/1")


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(_url(), socket_connect_timeout=2, socket_timeout=2)
    return _client


def set_client(c: redis.Redis | None) -> None:
    """시험용: 격리 Redis 로 바꿔 끼운다(None 이면 다음 호출 때 설정으로 다시 만든다)."""
    global _client
    _client = c


def rkey(seller_id: int, operation: str, key: str) -> str:
    # 키 값은 해시로만 둔다(키 형식·길이 제한은 정본에 없다)
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"pixlate:idem:{operation}:{seller_id}:{h}"


def _put(rk: str, value: dict[str, Any], *, nx: bool = False) -> bool:
    return bool(client().set(rk, json.dumps(value, ensure_ascii=False), nx=nx, ex=TTL_S))


def begin(seller_id: int, operation: str, key: str) -> Record:
    """처음이면 pending 을 남기고 Record("new"). 기록이 있으면 그 상태(committing·done). pending 이면 InFlight."""
    rk = rkey(seller_id, operation, key)
    for _ in range(3):
        try:
            if _put(rk, {"state": "pending"}, nx=True):
                return Record("new")
            raw = client().get(rk)
        except redis.RedisError as e:
            raise Unavailable(str(e)) from e
        if raw is None:  # 그 사이 만료·삭제 → 다시 차지 시도
            continue
        rec = json.loads(raw)
        if rec["state"] == "pending":
            raise InFlight()
        return Record(rec["state"], rec.get("jobId"), rec.get("body"))
    raise InFlight()


def mark_committing(seller_id: int, operation: str, key: str, job_id: int, body: dict[str, Any]) -> None:
    """커밋 직전에 작업 id·응답 본문을 남긴다. 실패하면 Unavailable — 호출자는 커밋하지 않고 롤백한다."""
    try:
        _put(rkey(seller_id, operation, key), {"state": "committing", "jobId": job_id, "body": body})
    except redis.RedisError as e:
        raise Unavailable(str(e)) from e


def complete(seller_id: int, operation: str, key: str, body: dict[str, Any]) -> bool:
    """최초 응답을 done 으로 확정(24시간). 실패하면 False — committing 이 남아 재요청 때 DB 로 확인해 확정한다."""
    try:
        _put(rkey(seller_id, operation, key), {"state": "done", "body": body})
        return True
    except redis.RedisError:
        return False


def release(seller_id: int, operation: str, key: str) -> None:
    """커밋을 시도하기 전 실패(작업이 저장되지 않았음이 확실) — 키를 지워 같은 키로 다시 시도할 수 있게 한다."""
    try:
        client().delete(rkey(seller_id, operation, key))
    except redis.RedisError:
        pass  # 지우지 못하면 pending 이 남아 같은 키 재요청은 409(중복 생성은 없음)
