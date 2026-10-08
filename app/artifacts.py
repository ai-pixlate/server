"""산출물 검증·고정 — BE 가 직접 잰 바이트만 검증 키에 쓴다(integration-decisions.md 5.30·5.32 산출물 고정·채택 절차).

- 업로더가 주장한 해시·ETag 로 검증을 대신하지 않는다. 주장 해시(declared_sha256)는 대조에만 쓴다.
- 측정 메타·예정 키를 권한 조건으로 먼저 기록하고(복구 시 같은 키로 재시도), 조건부 쓰기 후 검증 완료를 권한 조건으로 기록한다.
- 같은 키에 이미 객체가 있으면 내용이 같을 때만 멱등 성공, 다르면 덮어쓰지 않고 거절한다.
- source_ref(⑥ unchanged 원본 배경 등)는 같은 job 의 불변 키와 그 내용 해시를 대조해 검증한다.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app import db as app_db
from app.execution import AdoptionRejected, Lease, check_lease, lock_chain
from app.manifest import canonical_json, sha256_bytes
from app.storage import ObjectMissing, get_store

PART_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
FORMAT_MEDIA = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "WEBP": ("image/webp", "webp")}
STAGE_KINDS = {
    "analyze": {"section_image"},
    "inpaint": {"background", "delete_mask", "protect_mask"},
    "preview": {"render_image"},
    "final_render": {"render_image"},
}


class ArtifactConflict(AdoptionRejected):
    def __init__(self, message: str):
        super().__init__(message, code="ARTIFACT_CONFLICT")


@dataclass
class Measured:
    byte_size: int
    sha256: str
    media_type: str
    ext: str
    width: int
    height: int
    mode: str


def measure_image(data: bytes) -> Measured:
    """바이트를 직접 열어 형식·크기를 잰다. 이미지가 아니거나 깨졌으면 AdoptionRejected."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            w, h, mode = im.size[0], im.size[1], im.mode
            im.load()
    except Exception as e:  # noqa: BLE001 — 열 수 없는 파일은 산출물 불량
        raise AdoptionRejected(f"이미지를 열 수 없다: {e.__class__.__name__}: {e}", code="ARTIFACT_INVALID") from e
    if fmt not in FORMAT_MEDIA:
        raise AdoptionRejected(f"허용하지 않는 이미지 형식 {fmt}", code="ARTIFACT_INVALID")
    media, ext = FORMAT_MEDIA[fmt]
    return Measured(byte_size=len(data), sha256=sha256_bytes(data), media_type=media, ext=ext, width=w, height=h, mode=mode)


def staging_key(job_id: int, task_id: int, epoch: int, kind: str, part_key: str) -> str:
    if not PART_KEY_RE.match(part_key):
        raise AdoptionRejected(f"part_key 형식 {part_key!r}", code="ARTIFACT_INVALID")
    return f"staging/{job_id}/{task_id}/{epoch}/{kind}/{part_key}"


def verified_key(job_id: int, task_id: int, handoff_id: int, artifact_id: int, sha: str, ext: str) -> str:
    return f"verified/{job_id}/{task_id}/{handoff_id}/{artifact_id}/{sha}.{ext}"


def _artifact(db, artifact_id: int) -> dict[str, Any]:
    r = db.execute(text("SELECT * FROM task_artifact WHERE id = :a"), {"a": artifact_id}).mappings().one()
    return dict(r)


def fix_bytes(lease: Lease, artifact_id: int, data: bytes) -> dict[str, Any]:
    """검증한 같은 바이트를 검증 키에 고정한다. 반환: 갱신된 task_artifact 행."""
    m = measure_image(data)
    store = get_store()
    db = app_db.SessionLocal()
    try:
        art = _artifact(db, artifact_id)
        if art["declared_sha256"] and art["declared_sha256"] != m.sha256:
            raise AdoptionRejected(f"산출물 {art['kind']}/{art['part_key']}: 주장 해시와 실제 내용이 다르다", code="ARTIFACT_HASH_MISMATCH")
        if art["state"] == "verified" and art["sha256"] == m.sha256:
            return art
        if art["sha256"] is not None and art["sha256"] != m.sha256:
            raise ArtifactConflict(f"산출물 {art['id']}: 이미 측정한 내용과 다르다(교체 금지)")
        h = db.execute(text("SELECT task_id FROM task_handoff WHERE id = :h"), {"h": art["handoff_id"]}).mappings().one()
        lock_chain(db, h["task_id"])
        if not check_lease(db, lease):
            db.rollback()
            raise AdoptionRejected("권한을 잃어 산출물을 고정하지 않는다", code="LEASE_LOST", retryable=True)
        key = art["verified_key"] or verified_key(art["job_id"], h["task_id"], art["handoff_id"], art["id"], m.sha256, m.ext)
        db.execute(
            text(
                "UPDATE task_artifact SET verified_key = :k, byte_size = :b, sha256 = :s, media_type = :mt, width = :w, height = :hh "
                "WHERE id = :a"
            ),
            {"k": key, "b": m.byte_size, "s": m.sha256, "mt": m.media_type, "w": m.width, "hh": m.height, "a": artifact_id},
        )
        db.commit()
    finally:
        db.close()

    if not store.put_if_absent(key, data, content_type=m.media_type):
        try:
            existing = store.get(key)
        except ObjectMissing as e:  # 경쟁으로 사라짐 — 다시 시도하게 한다
            raise AdoptionRejected(f"검증 키 상태 불명 {key}", code="ARTIFACT_STORE", retryable=True) from e
        if sha256_bytes(existing) != m.sha256 or len(existing) != m.byte_size:
            raise ArtifactConflict(f"검증 키 {key}에 다른 내용이 있다 — 덮어쓰지 않는다")

    db = app_db.SessionLocal()
    try:
        h = db.execute(text("SELECT h.task_id FROM task_handoff h JOIN task_artifact a ON a.handoff_id = h.id WHERE a.id = :a"),
                       {"a": artifact_id}).mappings().one()
        lock_chain(db, h["task_id"])
        if not check_lease(db, lease):
            db.rollback()
            raise AdoptionRejected("권한을 잃어 산출물 검증을 기록하지 않는다", code="LEASE_LOST", retryable=True)
        db.execute(text("UPDATE task_artifact SET state = 'verified' WHERE id = :a AND state = 'registered'"), {"a": artifact_id})
        db.commit()
        return _artifact(db, artifact_id)
    finally:
        db.close()


def fix_staged(lease: Lease, artifact_id: int) -> dict[str, Any]:
    """원격 업로드(스테이징 키)를 내려받아 같은 바이트를 고정한다. 검증 후 스테이징이 덮어써져도 결과는 바뀌지 않는다."""
    db = app_db.SessionLocal()
    try:
        art = _artifact(db, artifact_id)
    finally:
        db.close()
    if not art["staging_key"]:
        raise AdoptionRejected(f"산출물 {art['kind']}/{art['part_key']}: 업로드 키 없음", code="ARTIFACT_MISSING")
    try:
        data = get_store().get(art["staging_key"])
    except ObjectMissing as e:
        raise AdoptionRejected(f"업로드된 파일 없음 {art['kind']}/{art['part_key']}", code="ARTIFACT_MISSING") from e
    return fix_bytes(lease, artifact_id, data)


def fix_source_ref(lease: Lease, artifact_id: int, *, allowed_keys: dict[str, str]) -> dict[str, Any]:
    """기존 채택 파일 참조 검증. allowed_keys: 이번 실행의 고정 입력으로 승인된 {불변 키: sha256}.
    가변 최신 키·만료 URL·소속을 입증하지 못하는 참조는 거절한다."""
    db = app_db.SessionLocal()
    try:
        art = _artifact(db, artifact_id)
    finally:
        db.close()
    ref = art["source_ref"] or {}
    key, sha = ref.get("key"), ref.get("sha256")
    if not key or key not in allowed_keys or allowed_keys[key] != sha:
        raise AdoptionRejected(f"source_ref 가 승인된 고정 입력이 아니다: {canonical_json(ref)}", code="ARTIFACT_SOURCE_REF")
    try:
        data = get_store().get(key)
    except ObjectMissing as e:
        raise AdoptionRejected(f"source_ref 원천이 없다 {key}", code="ARTIFACT_SOURCE_REF") from e
    m = measure_image(data)
    if m.sha256 != sha:
        raise AdoptionRejected(f"source_ref 내용 해시 불일치 {key}", code="ARTIFACT_SOURCE_REF")
    db = app_db.SessionLocal()
    try:
        h = db.execute(text("SELECT task_id FROM task_handoff WHERE id = :h"), {"h": art["handoff_id"]}).mappings().one()
        lock_chain(db, h["task_id"])
        if not check_lease(db, lease):
            db.rollback()
            raise AdoptionRejected("권한을 잃었다", code="LEASE_LOST", retryable=True)
        db.execute(
            text(
                "UPDATE task_artifact SET byte_size = :b, sha256 = :s, media_type = :mt, width = :w, height = :hh, state = 'verified' "
                "WHERE id = :a AND state = 'registered'"
            ),
            {"b": m.byte_size, "s": m.sha256, "mt": m.media_type, "w": m.width, "hh": m.height, "a": artifact_id},
        )
        db.commit()
        return _artifact(db, artifact_id)
    finally:
        db.close()


def artifacts_of(handoff_id: int) -> list[dict[str, Any]]:
    db = app_db.SessionLocal()
    try:
        return [dict(r) for r in db.execute(
            text("SELECT * FROM task_artifact WHERE handoff_id = :h ORDER BY id"), {"h": handoff_id}
        ).mappings().all()]
    finally:
        db.close()


def adopted_key(art: dict[str, Any]) -> str:
    """업무 컬럼에 넣을 불변 키 — 검증 키 또는 검증된 원천의 불변 키."""
    if art["verified_key"]:
        return art["verified_key"]
    return (art["source_ref"] or {})["key"]
