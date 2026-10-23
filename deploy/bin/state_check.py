"""정지·전환 뒤 상태 재조회(읽기 전용) — Redis 큐·unacked 와 DB 의 진행 중 시도·열린 인계 개수만 출력한다.

    sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env -v /opt/pixlate/bin:/opt/pixlate/bin:ro \
      <API 이미지> python /opt/pixlate/bin/state_check.py

값·주소·메시지 본문은 출력하지 않는다. 종료 코드: 0 조회 성공 / 1 조회 실패(연결 오류 등 — 유형만 출력).
결과는 조회 시점의 값이다. 정지된 요청·작업이 늦게 만든 메시지가 있을 수 있으므로 0 이 아니면 그대로 보고한다.
"""
import os
import sys


def main() -> int:
    try:
        import redis
        from sqlalchemy import create_engine, text

        r = redis.Redis.from_url(os.environ["CELERY_BROKER_URL"], socket_connect_timeout=10, socket_timeout=10)
        queues = {k: r.llen(k) for k in ("ocr", "cpu", "gpu", "celery")}
        print("redis queues:", queues, "| unacked:", r.hlen("unacked"), "| unacked_index:", r.zcard("unacked_index"))
        e = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
        with e.connect() as c:
            one = lambda sql: c.execute(text(sql)).scalar()  # noqa: E731
            print("db revision:", one("SELECT version_num FROM alembic_version"),
                  "| pending/running attempts:", one("SELECT count(*) FROM job_async_task WHERE status IN ('pending','running')"),
                  "| open handoffs:", one("SELECT count(*) FROM task_handoff WHERE state IN ('received','verifying','verified')"))
    except Exception as e:  # noqa: BLE001 — 오류 원문(주소 포함 가능)은 출력하지 않는다
        print("state check failed:", type(e).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
