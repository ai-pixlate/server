"""DB 연결 설정 (SQLAlchemy + PostgreSQL).

DATABASE_URL 환경변수로 접속 정보를 받는다. 기본값은 docker 네트워크상의
pixlate-db 컨테이너. 실제 운영에서는 RDS 주소를 환경변수/Secrets로 주입한다.
"""
import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://pixlate:pixlate_dev_pw@pixlate-db:5432/pixlate",
)

# create_engine 은 지연 연결이라 앱 임포트 시 DB가 없어도 안전하다.
# pool_pre_ping: 끊긴 커넥션 자동 감지·재연결.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db(request: Request = None):  # type: ignore[assignment] — 직접 부르거나 대역으로 바꾸는 쪽은 요청 없이 쓴다
    """FastAPI 의존성: 요청마다 세션을 열고 끝나면 닫는다.
    작업을 바꾸는 요청(activity_job_id)이면 그 세션의 커밋마다 같은 트랜잭션에서 마지막 움직임을 기록한다(_touch_activity)."""
    db: Session = SessionLocal()
    job_id = activity_job_id(request) if request is not None else None
    if job_id is not None:
        db.info[_ACTIVITY_KEY] = job_id
    try:
        yield db
    finally:
        db.close()


# ---- 24시간 자동 취소의 「마지막 움직임」(docs/README.md 7.8 ④) ------------------------------------------------------------
# 작업을 바꾸는 요청 = `/v1/jobs/{jobId}` 아래 POST · PATCH · DELETE(원본 이미지 삭제 등). 작업 취소(DELETE /v1/jobs/{jobId})는 제외.
# 조회(GET — 폴링 포함)는 세지 않는다. 그 요청의 세션이 커밋하는 트랜잭션 안에서 job.last_activity_at = now() 를 같이 쓴다 —
# 변경과 활동 시각이 함께 커밋되거나 함께 롤백된다(롤백 · 커밋 전 4xx · 멱등 재전송처럼 커밋하지 않은 요청은 세지 않는다).
# 커밋 시점에 작업이 이미 취소돼 있으면(자동 취소가 먼저 잠금을 얻음) 커밋하지 않고 409 INVALID_STATE — 취소된 작업에 성공 응답을 내지 않는다.
_ACTIVITY_KEY = "pixlate_activity_job_id"
_ACTIVITY_METHODS = frozenset({"POST", "PATCH", "DELETE"})
_CANCEL_ROUTE_SUFFIX = "/jobs/{job_id}"  # DELETE 이면 작업 취소(API-JOB-04) — 세지 않는다


def activity_job_id(request: Request) -> int | None:
    """이 요청이 「작업을 바꾸는 요청」이면 그 작업 id. 경로 틀(route.path)로 판단한다(실제 등록된 엔드포인트만)."""
    if request.method not in _ACTIVITY_METHODS:
        return None
    route = request.scope.get("route")
    path = getattr(route, "path", "") or ""
    if "/jobs/{job_id}" not in path:
        return None
    if request.method == "DELETE" and path.endswith(_CANCEL_ROUTE_SUFFIX):
        return None
    raw = request.path_params.get("job_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class ActivityOnCancelledJob(Exception):
    """작업을 바꾸는 요청이 커밋하려는 때 작업이 이미 취소돼 있다 — app.main 이 409 INVALID_STATE 로 바꾼다."""


@event.listens_for(Session, "before_commit")
def _touch_activity(session: Session) -> None:
    job_id = session.info.get(_ACTIVITY_KEY)
    if job_id is None:
        return
    # 행 잠금 아래 확인 — 자동 취소(app.recovery._auto_cancel)도 같은 행을 잠그고 기한을 다시 본다(둘 중 먼저 잠근 쪽이 이긴다)
    st = session.execute(text("SELECT status FROM job WHERE id = :j FOR UPDATE"), {"j": job_id}).scalar()
    if st is None:
        return  # 작업이 없다 — 각 엔드포인트가 이미 404 로 끝낸다
    if st == "cancelled":
        raise ActivityOnCancelledJob(job_id)
    session.execute(text("UPDATE job SET last_activity_at = now() WHERE id = :j"), {"j": job_id})
