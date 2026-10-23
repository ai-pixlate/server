"""S3 유틸 (boto3).

인증은 EC2 인스턴스 IAM 역할이 자동 제공(액세스 키 미사용). 버킷·리전은
환경변수로 주입하며 기본값은 pixlate-storage-2026 / ap-northeast-2.
DB에는 S3 오브젝트 키만 저장하고, 조회 시 presigned URL을 발급한다.
"""
import os
import uuid

import boto3
from botocore.config import Config

S3_BUCKET = os.getenv("S3_BUCKET", "pixlate-storage-2026")
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


def content_disposition(name: str) -> str:
    """attachment 다운로드 이름(RFC 6266·5987).
    제어문자(C0·DEL·C1 — CR·LF 포함)는 filename·filename* 양쪽에서 _ 로 바꾼다(헤더·파일명에 들어가지 않게).
    한글·공백 등 정상 문자는 filename*(UTF-8 퍼센트 인코딩)에 그대로 두고, ASCII 대체 filename 에서만
    비ASCII·따옴표·역슬래시를 _ 로 바꾼다."""
    from urllib.parse import quote

    clean = "".join("_" if ord(ch) < 32 or 127 <= ord(ch) < 160 else ch for ch in name)
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in clean)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(clean, safe='')}"


def presigned_get(key: str | None, ttl: int = PRESIGN_TTL, download_name: str | None = None) -> str | None:
    """조회용 presigned URL. download_name 을 주면 S3 응답에 Content-Disposition(attachment) 을 붙여
    브라우저가 그 이름으로 저장한다(다운로드 링크 전용 — 화면 표시용 호출은 그대로)."""
    if not key:
        return None
    params = {"Bucket": S3_BUCKET, "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = content_disposition(download_name)
    return _s3.generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=ttl,
    )
