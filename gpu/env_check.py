"""학교 GPU pod 워커 환경 점검 — 값 없이 존재·접근·일치 여부만 출력한다. 큐 소비·쓰기 없음.

    cd /data/server && (set -a; . /data/server.env; set +a; /data/venv/bin/python /data/pixlate/bin/env_check.py)
    (ocr 워커 환경은 /data/venv-ocr/bin/python 으로 같은 명령)
종료 코드: 0 전부 통과 / 1 하나라도 실패.

- 브로커·결과 저장소: Redis PING(인증 포함)만. 큐 길이·메시지는 읽지 않는다.
- DB: 연결 + alembic revision(값은 비밀이 아님) + 대상이 RDS·DB 이름 pixlate 인지(호스트는 출력 안 함).
- S3: 코드의 실제 설정(app.s3)으로 staging/ 접두사 목록 조회·리전 일치. 버킷 이름은 출력하지 않고 EC2 와 비교할 해시 앞 12자리만.
  자격 출처(환경 변수/파일/역할)는 종류만 출력한다.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.getcwd())  # 스크립트는 레포 밖에 있으므로 작업 디렉터리(레포 루트)의 app·pipeline 을 import 한다

ok = True


def check(name, fn):
    global ok
    try:
        print(f"{name}: {fn()}")
    except Exception as e:  # noqa: BLE001 — 오류 원문(주소 포함 가능)은 출력하지 않는다
        ok = False
        print(f"{name}: FAIL {type(e).__name__}")


for k in ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "DATABASE_URL"):
    print(f"{k}: {'present' if os.environ.get(k) else 'missing'}")
    ok = ok and bool(os.environ.get(k))


def redis_ping(var):
    import redis

    r = redis.Redis.from_url(os.environ[var], socket_connect_timeout=10, socket_timeout=10)
    return "ping ok" if r.ping() else "no pong"


check("broker", lambda: redis_ping("CELERY_BROKER_URL"))
check("result_backend", lambda: redis_ping("CELERY_RESULT_BACKEND"))


def db():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    u = make_url(os.environ["DATABASE_URL"])
    e = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    with e.connect() as c:
        rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return f"connect ok | revision {rev} | host_is_rds {(u.host or '').endswith('.rds.amazonaws.com')} | db_is_pixlate {u.database == 'pixlate'}"


check("db", db)


def s3():
    import boto3

    from app import s3 as s3mod

    sess = boto3.session.Session()
    cred = sess.get_credentials()
    if cred is None:
        raise RuntimeError("no credentials")
    cli = boto3.client("s3", region_name=s3mod.S3_REGION)
    # 최소 권한 정책(gpu/iam/school-worker-s3-policy.json)은 워커 접두사 목록만 허용하므로 HeadBucket 대신 접두사로 조회한다
    cli.list_objects_v2(Bucket=s3mod.S3_BUCKET, Prefix="staging/", MaxKeys=1)
    loc = cli.get_bucket_location(Bucket=s3mod.S3_BUCKET).get("LocationConstraint") or "us-east-1"
    h = hashlib.sha256(s3mod.S3_BUCKET.encode()).hexdigest()[:12]
    return (f"list ok | credential_source {cred.method} | region_match {loc == s3mod.S3_REGION} "
            f"| bucket_from_env {bool(os.environ.get('S3_BUCKET'))} | bucket_hash {h}")


check("s3", s3)
sys.exit(0 if ok else 1)
