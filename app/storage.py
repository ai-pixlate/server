"""오브젝트 저장소 경계 — 실행·인계가 쓰는 S3 동작만 모았다.

- 검증 키(`verified/…`)는 조건부 쓰기(If-None-Match: *)로만 만든다. 이미 있으면 덮어쓰지 않고 호출자가 내용을 대조한다
  (integration-decisions.md 5.32 산출물 고정·채택 절차 3).
- presigned PUT 은 BE가 허용한 키 하나에만 발급한다(5.28).
- 테스트는 `set_store(MemoryStore())`로 바꿔 끼운다. 기본은 S3(버킷·리전은 app.s3 와 같은 환경변수).
"""
from __future__ import annotations

import threading
from typing import Protocol


class ObjectMissing(KeyError):
    pass


class ObjectStore(Protocol):
    def put_if_absent(self, key: str, data: bytes, content_type: str | None = None) -> bool: ...
    def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def presign_put(self, key: str, ttl: int, content_type: str | None = None) -> str: ...
    def presign_get(self, key: str, ttl: int) -> str: ...


class S3Store:
    def __init__(self) -> None:
        from app import s3

        self._s3 = s3

    @property
    def _client(self):
        return self._s3._s3

    def put_if_absent(self, key: str, data: bytes, content_type: str | None = None) -> bool:
        from botocore.exceptions import ClientError

        extra = {"ContentType": content_type} if content_type else {}
        try:
            self._client.put_object(Bucket=self._s3.S3_BUCKET, Key=key, Body=data, IfNoneMatch="*", **extra)
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "ConditionalRequestConflict") or e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412:
                return False
            raise

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self._client.put_object(Bucket=self._s3.S3_BUCKET, Key=key, Body=data, **extra)

    def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            return self._client.get_object(Bucket=self._s3.S3_BUCKET, Key=key)["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                raise ObjectMissing(key) from e
            raise

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._s3.S3_BUCKET, Key=key)
            return True
        except ClientError as e:
            if e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise

    def delete(self, key: str) -> None:
        if key:
            self._client.delete_object(Bucket=self._s3.S3_BUCKET, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        if not prefix or not prefix.endswith("/"):
            raise ValueError(f"접두사 삭제는 '/'로 끝나는 접두사만: {prefix!r}")
        n = 0
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._s3.S3_BUCKET, Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            for i in range(0, len(keys), 1000):
                self._client.delete_objects(Bucket=self._s3.S3_BUCKET, Delete={"Objects": keys[i:i + 1000], "Quiet": True})
                n += len(keys[i:i + 1000])
        return n

    def presign_put(self, key: str, ttl: int, content_type: str | None = None) -> str:
        params = {"Bucket": self._s3.S3_BUCKET, "Key": key}
        if content_type:
            params["ContentType"] = content_type
        return self._client.generate_presigned_url("put_object", Params=params, ExpiresIn=ttl)

    def presign_get(self, key: str, ttl: int) -> str:
        return self._client.generate_presigned_url("get_object", Params={"Bucket": self._s3.S3_BUCKET, "Key": key}, ExpiresIn=ttl)


class MemoryStore:
    """테스트·로컬 검증용. 스레드 안전. presigned URL 은 'memory://' 형식의 식별 문자열이다(전송 수단 아님)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str | None] = {}
        self._lock = threading.Lock()
        self.issued_put_urls: list[str] = []

    def put_if_absent(self, key: str, data: bytes, content_type: str | None = None) -> bool:
        with self._lock:
            if key in self.objects:
                return False
            self.objects[key] = bytes(data)
            self.content_types[key] = content_type
            return True

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        with self._lock:
            self.objects[key] = bytes(data)
            self.content_types[key] = content_type

    def get(self, key: str) -> bytes:
        with self._lock:
            if key not in self.objects:
                raise ObjectMissing(key)
            return self.objects[key]

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self.objects

    def delete(self, key: str) -> None:
        with self._lock:
            self.objects.pop(key, None)
            self.content_types.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        if not prefix or not prefix.endswith("/"):
            raise ValueError(f"접두사 삭제는 '/'로 끝나는 접두사만: {prefix!r}")
        with self._lock:
            keys = [k for k in self.objects if k.startswith(prefix)]
            for k in keys:
                del self.objects[k]
                self.content_types.pop(k, None)
            return len(keys)

    def presign_put(self, key: str, ttl: int, content_type: str | None = None) -> str:
        url = f"memory://put/{key}?ttl={ttl}"
        self.issued_put_urls.append(url)
        return url

    def presign_get(self, key: str, ttl: int) -> str:
        return f"memory://get/{key}?ttl={ttl}"

    def keys(self, prefix: str = "") -> list[str]:
        with self._lock:
            return sorted(k for k in self.objects if k.startswith(prefix))


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store
    if _store is None:
        _store = S3Store()
    return _store


def set_store(store: ObjectStore | None) -> None:
    global _store
    _store = store
