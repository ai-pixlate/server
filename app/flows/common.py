"""흐름 공통 — 작업 상태 전이, 화면에 보이는 섹션, 임시 키, 감사 기록, 워커 갱신 루프."""
from __future__ import annotations

import json
import threading
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import artifacts, execution
from app.execution import AdoptionRejected, AdoptOutcome, Envelope, Lease
from app.manifest import stable_json

# 현재 채택한 분석 실행의 섹션만 보인다. 분석이 끝나기 전 새 섹션(다른 analysis_task_id)은 숨긴다(D3).
# 레거시(분석 실행 없이 만든) 섹션은 current_analysis_task_id 가 NULL 인 작업에서만 보인다.
VISIBLE = "s.analysis_task_id IS NOT DISTINCT FROM j.current_analysis_task_id"

# audit_log 내부 값(사용자 승인 2026-10-08)
ACTOR_SYSTEM, ACTOR_SELLER = "system", "seller"
ACT_ANALYSIS_ADOPTED = "analysis_result_adopted"
ACT_ANALYSIS_REPLACED = "analysis_result_replaced"
ACT_JOB_CONTENT_DELETED = "job_content_deleted"
AUDIT_SCHEMA = "1"


def section_key(section_id: int) -> str:
    return f"sec_{section_id}"


def block_key(block_id: int) -> str:
    return f"blk_{block_id}"


def parse_key(key: str, prefix: str) -> int | None:
    if not isinstance(key, str) or not key.startswith(prefix + "_"):
        return None
    tail = key[len(prefix) + 1:]
    return int(tail) if tail.isdigit() else None


def set_job_state(db: Session, job_id: int, *, status: str, step: str, ufs: str) -> None:
    """상태 전이. 상태 · 단계가 바뀌면 그 상태에 들어간 시각(state_entered_at — 처리 시간 상한 기산점, README 7.8 ⑤)을 새로 하고,
    전이 자체를 마지막 움직임(last_activity_at — 24시간 자동 취소 기산점, 7.8 ④)으로 센다(서버 처리가 끝나 단계가 넘어간 때 포함)."""
    db.execute(
        text("UPDATE job SET state_entered_at = CASE WHEN status IS DISTINCT FROM :st OR current_step IS DISTINCT FROM :cs "
             "THEN now() ELSE state_entered_at END, "
             "status = :st, current_step = :cs, user_facing_status = :u, updated_at = now(), last_activity_at = now() WHERE id = :j"),
        {"st": status, "cs": step, "u": ufs, "j": job_id},
    )


# 작업 이벤트(event_log · G17 3번 중 24 · 25번에 필요한 것). 문구 · 이미지를 담지 않는다(F-JOB-07). 취소해도 지우지 않는다
EV_JOB_CANCELLED = "job_cancelled"  # payload: reason(user | inactive_24h) · step · from_status
EV_JOB_FAILED = "job_failed"  # payload: reason(PROCESSING_TIMEOUT) · step · elapsed_s
CANCEL_USER, CANCEL_INACTIVE = "user", "inactive_24h"


def write_event(db: Session, job_id: int, event_type: str, payload: dict[str, Any]) -> None:
    db.execute(text("INSERT INTO event_log (job_id, event_type, payload) VALUES (:j, :t, CAST(:p AS jsonb))"),
               {"j": job_id, "t": event_type, "p": stable_json(payload)})


def write_audit(db: Session, *, action_type: str, target_type: str, target_id: int | None, detail: dict[str, Any] | None,
                actor_type: str = ACTOR_SYSTEM, actor_id: int | None = None) -> int:
    return db.execute(
        text(
            "INSERT INTO audit_log (actor_id, actor_type, action_type, target_type, target_id, detail) "
            "VALUES (:aid, :at, :act, :tt, :tid, CAST(:d AS jsonb)) RETURNING id"
        ),
        {"aid": actor_id, "at": actor_type, "act": action_type, "tt": target_type, "tid": target_id,
         "d": stable_json(detail) if detail is not None else None},
    ).scalar_one()


def visible_sections(db: Session, job_id: int, *, include_only: bool = False) -> list[dict[str, Any]]:
    sql = (
        "SELECT s.* FROM section s JOIN job j ON j.id = s.job_id WHERE s.job_id = :j AND " + VISIBLE
        + (" AND s.bucket = 'include'" if include_only else "") + " ORDER BY s.section_order, s.id"
    )
    return [dict(r) for r in db.execute(text(sql), {"j": job_id}).mappings().all()]


def has_source(source_ko: str | None) -> bool:
    """원문이 있는 블록인가 — 렌더·번역 대상 판정(downstream._render_inputs · _translate_targets)과 같은 기준.
    null 이거나 공백(줄바꿈·탭·전각 공백 포함)을 지우면 빈 문자열이면 빈 원문이다.
    N5 블록 목록(blockList)과 content.csv(exportCreate)는 빈 원문 블록을 응답에서 뺀다. DB 행은 지우지 않는다 — 인페인팅 보호 영역으로 쓴다."""
    return bool((source_ko or "").strip())


def json_value(v: Any) -> Any:
    """psycopg 가 jsonb 를 이미 파싱하므로 문자열일 때만 읽는다."""
    return json.loads(v) if isinstance(v, str) else v


def renderer_mismatch(manifest: Any) -> str | None:
    """렌더 고정 입력의 renderer(rendererId) · bundle(bundleId)이 지금 값(typeset.RENDERER_VERSION · html_render.BUNDLE_ID)과
    다르거나 기록이 없으면 사유, 둘 다 같으면 None. 기록이 없는 고정 입력을 현재 버전으로 간주하지 않는다(키가 없으면 불일치).
    실행 시점 검사(_render_section)와 재시도 생성 전 검사가 같이 쓴다."""
    from app import html_render, typeset

    m = json_value(manifest)
    if not isinstance(m, dict) or "renderer" not in m:
        return f"렌더러가 고정 입력과 다르다(고정 입력 기록 없음 · 실행 {typeset.RENDERER_VERSION})"
    if m["renderer"] != typeset.RENDERER_VERSION:
        return f"렌더러가 고정 입력과 다르다(고정 입력 {m['renderer'] or '기록 없음'} · 실행 {typeset.RENDERER_VERSION})"
    if "bundle" not in m:
        return f"FE 번들이 고정 입력과 다르다(고정 입력 기록 없음 · 실행 {html_render.BUNDLE_ID or '설정 없음'})"
    if m["bundle"] != html_render.BUNDLE_ID:
        return f"FE 번들이 고정 입력과 다르다(고정 입력 {m['bundle'] or '없음'} · 실행 {html_render.BUNDLE_ID or '설정 없음'})"
    return None


class Heartbeat:
    """긴 계산 동안 실행 권한을 독립 스레드로 갱신한다. 권한을 잃으면 lost=True — 호출자는 결과를 등록하지 않는다.
    모델 호출 자체를 즉시 멈추게 하지는 못한다(권한 상실 결과의 채택 차단은 BE가 따로 보장, 5.32)."""

    def __init__(self, lease: execution.Lease, interval_s: float | None = None):
        self.lease = lease
        self.interval = interval_s if interval_s is not None else max(1.0, execution.lease_ttl_s() / 3)
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"hb-{lease.attempt_id}", daemon=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not execution.heartbeat(self.lease):
                    self.lost = True
                    return
            except Exception:  # noqa: BLE001 — DB 접근 불가: 새 업로드·완료를 멈춘다(복구 후 검증)
                self.lost = True
                return

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def failed_envelope(lease: Lease, code: str, message: str, retryable: bool, target_count: int = 0) -> Envelope:
    return Envelope(outcome="failed", target_count=target_count, input_fingerprint=lease.fingerprint,
                    payload={"error_code": code, "message": message[:2000], "retryable": retryable})


def submit(lease: Lease, env: Envelope, files: dict[tuple[str, str], bytes] | None = None) -> AdoptOutcome | None:
    """신뢰된 EC2 워커: 등록 → (바이트 고정) → 채택."""
    h = execution.register_handoff(lease, env)
    if h["state"] != "received" and h["state"] != "verifying":
        return None
    try:
        for art in artifacts.artifacts_of(h["id"]):
            data = (files or {}).get((art["kind"], art["part_key"]))
            if data is None:
                raise AdoptionRejected(f"산출물 바이트 없음 {art['kind']}/{art['part_key']}", code="ARTIFACT_MISSING")
            artifacts.fix_bytes(lease, art["id"], data)
    except AdoptionRejected as e:
        execution.reject_and_fail(h["id"], lease, e)
        return None
    return execution.adopt(h["id"], lease)


def report(attempt_id: int, outcome: AdoptOutcome | None) -> dict[str, Any]:
    return {"attemptId": attempt_id, "ran": True, "status": outcome.status if outcome else None}
