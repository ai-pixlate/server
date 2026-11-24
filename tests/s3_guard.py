"""시험의 실제 S3 차단과, 실제 S3 시험의 실행 조건(2026-11-10).

배경: 이 저장소의 `app.s3` 는 import 때 boto3 클라이언트를 만들고, 기본 버킷은 공유 버킷 `pixlate-storage-2026` 이다.
AWS 자격이 있는 PC 에서 대역 없이 시험을 돌리면 실제 공유 버킷에 쓰고 지운다(#124 처음 판의 소유권 시험이 그랬다).

- 일반 시험: `app.s3._s3`(boto3 클라이언트 — `app.storage.S3Store` 도 이것을 쓴다)를 BlockedS3Client 로 바꾼다.
  어떤 메서드든 부르면 예외를 내고 호출을 기록한다. 예외가 삼켜져도 시험 끝에 기록을 보고 실패시킨다.
- 실제 S3 시험: 아래 두 가지가 **모두** 있어야 돈다(`requires_real_s3`). 하나라도 없으면 건너뛰고, 차단은 그대로다.
    PIXLATE_TEST_REAL_S3=1
    PIXLATE_TEST_S3_BUCKET=<시험 전용 버킷>  — 공유 버킷(SHARED_BUCKETS)이면 거절
  조건을 채우면 그 시험 동안 `app.s3.S3_BUCKET` 을 시험 전용 버킷으로 바꾼다(기존 S3_BUCKET 설정으로 자동 연결하지 않는다).
"""
from __future__ import annotations

import contextlib
import os

# 공유(운영·dev) 버킷 — 시험 전용 버킷으로 지정할 수 없다(pixate-deployment-setup · 샌드박스 계획서 기준)
SHARED_BUCKETS = frozenset({"pixlate-storage-2026", "pixlate-storage-dev-2026"})


class BlockedS3Client:
    """boto3 S3 클라이언트 자리에 두는 차단 대역. 어떤 메서드든 부르면 기록하고 실패한다(네트워크 없음)."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def blocked(*a, **k):
            self.calls.append(name)
            raise RuntimeError(f"시험 중 실제 S3 호출 차단: {name} — 실제 S3 시험은 PIXLATE_TEST_REAL_S3=1 과 시험 전용 버킷이 있을 때만")

        return blocked


def real_s3_bucket(env=None) -> tuple[str | None, str]:
    """실제 S3 시험을 돌릴 수 있으면 (시험 버킷, ""), 아니면 (None, 이유)."""
    env = os.environ if env is None else env
    if env.get("PIXLATE_TEST_REAL_S3") != "1":
        return None, "실제 S3 시험 — PIXLATE_TEST_REAL_S3=1 과 시험 전용 버킷(PIXLATE_TEST_S3_BUCKET)이 있는 환경에서만"
    bucket = (env.get("PIXLATE_TEST_S3_BUCKET") or "").strip()
    if not bucket:
        return None, "PIXLATE_TEST_S3_BUCKET(시험 전용 버킷) 없음 — 기존 S3_BUCKET 설정으로 자동 연결하지 않는다"
    if bucket in SHARED_BUCKETS:
        return None, f"공유 버킷 {bucket} 은 시험 버킷으로 쓸 수 없다"
    return bucket, ""


@contextlib.contextmanager
def guard(monkeypatch, env=None):
    """시험 하나를 감싼다. 실제 S3 조건을 채우지 않으면 차단하고, 끝날 때 차단된 호출이 있었으면 실패시킨다."""
    from app import s3

    bucket, _ = real_s3_bucket(env)
    if bucket:
        monkeypatch.setattr(s3, "S3_BUCKET", bucket)
        yield None
        return
    blocked = BlockedS3Client()
    monkeypatch.setattr(s3, "_s3", blocked)
    yield blocked
    assert blocked.calls == [], f"실제 S3 호출 시도(차단됨): {blocked.calls}"
