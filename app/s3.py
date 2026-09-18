"""S3 유틸 (boto3).

인증은 EC2 인스턴스 IAM 역할이 자동 제공(액세스 키 미사용). 버킷·리전은
환경변수로 주입하며 기본값은 pixate-storage-2026 / ap-northeast-2.
DB에는 S3 오브젝트 키만 저장하고, 조회 시 presigned URL을 발급한다.
"""
import os
import uuid

import boto3
from botocore.config import Config

S3_BUCKET = os.getenv("S3_BUCKET", "pixate-storage-2026")
S3_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
PRESIGN_TTL = int(os.getenv("S3_PRESIGN_TTL", "300"))  # 5분

_s3 = boto3.client("s3", region_name=S3_REGION, config=Config(signature_version="s3v4"))


def make_key(prefix: str, filename: str | None) -> str:
    """prefix/uuid.ext 형태의 오브젝트 키 생성."""
    ext = "bin"
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
    return f"{prefix}/{uuid.uuid4().hex}.{ext}"


def upload_fileobj(fileobj, key: str, content_type: str | None = None) -> str:
    extra = {}
    if content_type:
        extra["ContentType"] = content_type
    _s3.upload_fileobj(fileobj, S3_BUCKET, key, ExtraArgs=extra)
    return key


def delete_object(key: str) -> None:
    if key:
        _s3.delete_object(Bucket=S3_BUCKET, Key=key)


def presigned_get(key: str | None, ttl: int = PRESIGN_TTL) -> str | None:
    if not key:
        return None
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=ttl,
    )
