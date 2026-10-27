"""정지·전환 뒤 상태 재조회(읽기 전용) — Redis 큐·unacked 와 DB 의 진행 중 시도·열린 인계 개수만 출력한다.

    sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env -v /opt/pixlate/bin:/opt/pixlate/bin:ro \
      <API 이미지> python /opt/pixlate/bin/state_check.py [--gate | --db-revision | --snapshot [--with-s3] | --compare A B]

값·주소·메시지 본문은 출력하지 않는다. 결과는 조회 시점의 값이다. 정지된 요청·작업이 늦게 만든 메시지가 있을 수 있으므로 0 이 아니면 그대로 보고한다.
- 인자 없음(pixlate-mode status 가 부른다): 기존 출력 그대로. 종료 코드 0 조회 성공 / 1 조회 실패(연결 오류 등 — 유형만 출력).
- --gate        : 전환 전 유휴 관문. 큐(ocr·cpu·gpu·celery)·unacked·unacked_index 0, 부모 있는 pending·running 시도 0, 열린 인계 0 이면 0.
                  하나라도 있으면 2, 조회 실패 1. 열린 대표 실행은 개수만 출력(관문 조건 아님).
- --db-revision : alembic_version 을 SQL 로 직접 읽는다(Alembic 스크립트 해석과 분리 — 이전 이미지가 모르는 revision 도 읽힌다).
                  행이 정확히 1개면 값 출력·0, 아니면 1.
- --snapshot    : 무실행 확인용 상태 지문(JSON 한 줄). 새 행뿐 아니라 기존 시도의 실행(status·lease_epoch 변경)·복구 정리·삭제를 잡는다.
                  --with-s3 이면 S3_BUCKET 객체 목록 지문(키·크기)도 넣는다. 이 지문은 실행 발생 감지용이며
                  "부작용 없음" 의 증거가 아니다(이미 지워진 접두사 재삭제·외부 호출은 보이지 않는다).
- --compare A B : 두 지문 파일 비교(DB·Redis 접속 없음). 같으면 0, 다르면 2(다른 항목 이름만 출력), 읽기 실패 1.
"""
import argparse
import hashlib
import json
import os
import sys

QUEUES = ("ocr", "cpu", "gpu", "celery")


def _redis():
    import redis

    return redis.Redis.from_url(os.environ["CELERY_BROKER_URL"], socket_connect_timeout=10, socket_timeout=10)


def _engine():
    from sqlalchemy import create_engine

    return create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


def legacy() -> int:
    try:
        from sqlalchemy import text

        r = _redis()
        queues = {k: r.llen(k) for k in QUEUES}
        print("redis queues:", queues, "| unacked:", r.hlen("unacked"), "| unacked_index:", r.zcard("unacked_index"))
        e = _engine()
        with e.connect() as c:
            one = lambda sql: c.execute(text(sql)).scalar()  # noqa: E731
            print("db revision:", one("SELECT version_num FROM alembic_version"),
                  "| pending/running attempts:", one("SELECT count(*) FROM job_async_task WHERE status IN ('pending','running')"),
                  "| open handoffs:", one("SELECT count(*) FROM task_handoff WHERE state IN ('received','verifying','verified')"))
    except Exception as e:  # noqa: BLE001 — 오류 원문(주소 포함 가능)은 출력하지 않는다
        print("state check failed:", type(e).__name__)
        return 1
    return 0


def gate() -> int:
    try:
        from sqlalchemy import text

        r = _redis()
        queues = {k: r.llen(k) for k in QUEUES}
        unacked = r.hlen("unacked")
        unacked_index = r.zcard("unacked_index")
        with _engine().connect() as c:
            one = lambda sql: c.execute(text(sql)).scalar()  # noqa: E731
            active = one("SELECT count(*) FROM job_async_task WHERE parent_task_id IS NOT NULL AND status IN ('pending','running')")
            handoffs = one("SELECT count(*) FROM task_handoff WHERE state IN ('received','verifying','verified')")
            runs = one("SELECT count(*) FROM job_async_task WHERE parent_task_id IS NULL AND status = 'running'")
    except Exception as e:  # noqa: BLE001
        print("gate check failed:", type(e).__name__)
        return 1
    busy = sum(queues.values()) + unacked + unacked_index + active + handoffs
    print("gate queues:", queues, "| unacked:", unacked, "| unacked_index:", unacked_index, "| active attempts:", active,
          "| open handoffs:", handoffs, "| open runs(참고):", runs)
    print("gate=" + ("idle" if busy == 0 else "busy"))
    return 0 if busy == 0 else 2


def db_revision() -> int:
    try:
        from sqlalchemy import text

        with _engine().connect() as c:
            rows = [r[0] for r in c.execute(text("SELECT version_num FROM alembic_version"))]
    except Exception as e:  # noqa: BLE001
        print("db revision failed:", type(e).__name__)
        return 1
    if len(rows) != 1:
        print("db revision failed: rows", len(rows))
        return 1
    print(rows[0])
    return 0


# 행 단위 지문 대상 — 표: 열. 상태가 바뀌면 값이 바뀌는 열만 고른다(acquire 는 status·lease_epoch, 복구의 lease 만료 처리는 lease_epoch).
FINGERPRINT = {
    "job_async_task": "id, status, lease_epoch, dispatch_count, is_current, cancelled_at, finished_at, retry_count",
    "task_handoff": "id, state",
    "job": "id, status, updated_at",
}


def db_snapshot(conn) -> dict:
    from sqlalchemy import text

    out = {}
    for table, cols in FINGERPRINT.items():
        sql = (f"SELECT encode(sha256(convert_to(coalesce(string_agg(concat_ws('|', {cols}), E'\\n' ORDER BY id), ''), 'UTF8')), 'hex') "
               f"FROM {table}")
        out[f"rows:{table}"] = conn.execute(text(sql)).scalar()
    for (t,) in conn.execute(text("SELECT table_name FROM information_schema.tables "
                                  "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name")).all():
        out[f"count:{t}"] = conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
    for t in ("task_lease_grant", "task_artifact"):
        out[f"max_id:{t}"] = conn.execute(text(f"SELECT coalesce(max(id), 0) FROM {t}")).scalar()
    return out


def redis_snapshot(r) -> dict:
    out = {f"queue:{k}": r.llen(k) for k in QUEUES}
    out["unacked"] = r.hlen("unacked")
    out["unacked_index"] = r.zcard("unacked_index")
    return out


def s3_list_digest(client, bucket: str) -> dict:
    """객체 목록(키·크기)의 지문. 키·버킷 이름은 출력하지 않는다."""
    lines = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents") or []:
            lines.append(f"{o['Key']}\t{o['Size']}")
    h = hashlib.sha256()
    for line in sorted(lines):
        h.update(line.encode("utf-8") + b"\n")
    return {"objects": len(lines), "digest": h.hexdigest()}


def snapshot(with_s3: bool) -> int:
    try:
        from sqlalchemy import text

        with _engine().connect() as c:
            c.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            snap = {"db": db_snapshot(c)}
            c.rollback()
        snap["redis"] = redis_snapshot(_redis())
        if with_s3:
            import boto3

            snap["s3"] = s3_list_digest(boto3.client("s3"), os.environ["S3_BUCKET"])
    except Exception as e:  # noqa: BLE001
        print("snapshot failed:", type(e).__name__, file=sys.stderr)
        return 1
    print(json.dumps(snap, sort_keys=True, default=str))
    return 0


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def compare(a: str, b: str) -> int:
    try:
        snaps = []
        for path in (a, b):
            with open(path, encoding="utf-8") as f:
                snaps.append(flatten(json.loads(f.read().strip().splitlines()[-1])))
    except Exception as e:  # noqa: BLE001
        print("compare failed:", type(e).__name__)
        return 1
    x, y = snaps
    diff = sorted(k for k in set(x) | set(y) if x.get(k) != y.get(k))
    if not diff:
        print("snapshot same")
        return 0
    print("snapshot differs:", ", ".join(diff))
    return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="state_check.py")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--gate", action="store_true")
    g.add_argument("--db-revision", action="store_true")
    g.add_argument("--snapshot", action="store_true")
    g.add_argument("--compare", nargs=2, metavar=("A", "B"))
    ap.add_argument("--with-s3", action="store_true")
    a = ap.parse_args(argv)
    if a.gate:
        return gate()
    if a.db_revision:
        return db_revision()
    if a.snapshot:
        return snapshot(a.with_s3)
    if a.compare:
        return compare(*a.compare)
    return legacy()


if __name__ == "__main__":
    sys.exit(main())
