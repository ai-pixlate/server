"""DB 연결 설정 (SQLAlchemy + PostgreSQL).

DATABASE_URL 환경변수로 접속 정보를 받는다. 기본값은 docker 네트워크상의
pixate-db 컨테이너. 실제 운영에서는 RDS 주소를 환경변수/Secrets로 주입한다.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://pixate:pixate_dev_pw@pixate-db:5432/pixate",
)

# create_engine 은 지연 연결이라 앱 임포트 시 DB가 없어도 안전하다.
# pool_pre_ping: 끊긴 커넥션 자동 감지·재연결.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """FastAPI 의존성: 요청마다 세션을 열고 끝나면 닫는다."""
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
