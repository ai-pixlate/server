"""N4 하류 — proceed → ④ 라벨 → ⑤ 로고 → ⑥⑦⑧(섹션별, 병렬) → ⑨ 초기 미리보기 → N5.

실행 구조: 대표(run_kind=downstream) + 섹션별 시도. run_scope 에 포함 섹션·근거 분석 실행·브랜드 스냅샷을 고정한다.
- proceed 는 job 잠금 아래 단계·활성 실행 확인, 대표·섹션별 ④ 시도 생성, N4 전환을 한 트랜잭션으로(D1·D2).
- ④⑤가 정상 완료 조합으로 채택되면 같은 트랜잭션에서 그 섹션의 ⑥⑦⑧ 시도를 함께 만든다(서로 기다리지 않음).
- ⑥⑦⑧이 모두 끝나면(성공·정상 생략·승인된 대체) ⑨ 시도를 만들고, 모든 섹션의 ⑨가 끝나면 N5 를 연다(D9-3·5.27).
  대체 결과를 성공으로 기록하지 않는다: 인페인트 실패 = 원본 배경·warning_badge processing_failed, 스타일 실패 = 역할 기본값,
  미리보기 실패 = 원본 표시·render 시도 failed(재시도 가능), 번역 일부 실패 = 성공분 보존·실패 칸 비움.
- ④⑤ 최종 실패·번역 대상이 있는데 성공 0건(자동 재시도 후) = N4 오류(status=failed, step=N4). 실행은 running 으로 두어
  [다시 시도](JOB-06)와 [중단](JOB-07 → N3)이 가능하다.
- 하류 결과는 이 실행의 현재 시도에서 채택한 것만 쓴다. 이전 실행의 플래그·번역은 proceed 에서 지운다(D1: 옛 DB 값으로 완료 판단 금지).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ai_adapters, artifacts, execution, html_render, layout_adjust, render_input, typeset
from app import db as app_db
from app.execution import AdoptContext, AdoptionRejected, AdoptOutcome, ArtifactSpec, Envelope
from app.flows import common, text_check
from app.flows.common import block_key, failed_envelope, json_value, report, section_key, set_job_state, submit
from app.dictionary_bundle import ai_alternatives, entries_by_ref
from app.manifest import fingerprint, sha256_bytes, sha256_text, stable_hash
from app.storage import ObjectMissing, get_store
from pipeline.handoff import style_defaults as sd

log = logging.getLogger(__name__)

# 단계별 자동 재시도·누적 한도(개발 기본값 — 운영 수치 미정, N2 의 2회를 일반화하지 않음)
AUTO_RETRY = {"label": 1, "logo": 0, "inpaint": 0, "style": 0, "translate": 1, "preview": 0}
MAX_RETRY = {"label": 1, "logo": 0, "inpaint": 1, "style": 1, "translate": 2, "preview": 2}
TRANSLATE_ALL_FAILED = "TRANSLATE_ALL_FAILED"
TRANSLATE_PARTIAL = "TRANSLATE_PARTIAL"
BLOCKING = {"label", "logo"}


class ProceedRejected(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------------------------------------------------
# 입력 조립
# ---------------------------------------------------------------------------------------------------------
def _section_row(db: Session, section_id: int) -> dict[str, Any]:
    return dict(db.execute(text("SELECT * FROM section WHERE id = :s"), {"s": section_id}).mappings().one())


def _image_sha(db: Session, key: str) -> str:
    sha = db.execute(text("SELECT sha256 FROM task_artifact WHERE verified_key = :k"), {"k": key}).scalar()
    if sha is None:
        raise AdoptionRejected(f"섹션 이미지 {key} 의 검증 기록 없음", code="INPUT_MISSING")
    return sha


def _block_rows(db: Session, section_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(
        text("SELECT * FROM text_block WHERE section_id = :s ORDER BY block_order, id"), {"s": section_id}
    ).mappings().all()]


def _text_blocks(section_id: int, rows: list[dict[str, Any]]):
    from pipeline.types import TextBlock

    return [
        TextBlock(block_key=block_key(r["id"]), section_key=section_key(section_id), block_order=r["block_order"],
                  source_ko=r["source_ko"] or "", source_lines=json_value(r["source_lines"]), bbox=json_value(r["bbox"]),
                  role=r["role"], ocr_confidence=float(r["ocr_confidence"]) if r["ocr_confidence"] is not None else None)
        for r in rows
    ]


def _blocks_fp(rows: list[dict[str, Any]]) -> str:
    return stable_hash([[r["id"], r["block_order"], r["source_ko"], r["role"], json_value(r["bbox"]), json_value(r["source_lines"]),
                         str(r["ocr_confidence"])] for r in rows])


def _pipeline_section(sec: dict[str, Any], image_path: str):
    from pipeline.types import Section

    return Section(section_key=section_key(sec["id"]), source_image_id=sec["source_image_id"], section_order=sec["section_order"],
                   top_offset=sec["top_offset"], height=sec["height"], width=json_value(sec["bbox"])["w"], image_path=image_path)


def _image_id(sec: dict[str, Any]) -> str:
    return f"src_{sec['source_image_id']}"


def _adopted_payload(db: Session, run_id: int, stage: str, section_id: int) -> tuple[int, dict[str, Any]] | None:
    r = db.execute(
        text(
            "SELECT h.id, h.payload FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
            "WHERE a.parent_task_id = :r AND a.stage = :st AND a.unit_id = :s AND a.is_current AND h.state = 'adopted' "
            "ORDER BY h.id DESC LIMIT 1"
        ),
        {"r": run_id, "st": stage, "s": section_id},
    ).first()
    return (r[0], json_value(r[1])) if r else None


def _download_section(sec: dict[str, Any], expect_sha: str, tmp: Path) -> Path:
    data = get_store().get(sec["image_key"])
    if sha256_bytes(data) != expect_sha:
        raise AdoptionRejected("섹션 이미지가 고정 입력과 다르다", code="INPUT_CHANGED")
    p = tmp / f"{section_key(sec['id'])}.png"
    p.write_bytes(data)
    return p


def _brand_meta(scope: dict[str, Any], image_ids: list[str]) -> dict[str, Any]:
    b = scope["brand"]
    return {"products": [{"image_group": f"job_{scope['job_id']}", "product_name": None, "images": image_ids,
                          "name_ko": b["name_ko"], "name_ko_status": "provided", "name_en": b["name_en"], "name_en_status": "provided"}]}


# ---------------------------------------------------------------------------------------------------------
# proceed (SEC-04)
# ---------------------------------------------------------------------------------------------------------
def _clear_previous(db: Session, section_ids: list[int]) -> None:
    """이전(중단된) 하류 실행이 남긴 파생값을 지운다. N3 단계라 사용자 번역 수정은 없다."""
    db.execute(
        text(
            "UPDATE text_block SET revision = revision + CASE WHEN trans_1 IS NOT NULL OR layout_adjust IS NOT NULL THEN 1 ELSE 0 END, "
            "is_product_label = NULL, is_brand_logo = NULL, style = NULL, trans_1 = NULL, char_count = NULL, overflow = false, "
            "compliance_flags = NULL, block_status = 'machine', updated_at = now() WHERE section_id = ANY(:ids)"
        ),
        {"ids": section_ids},
    )
    db.execute(
        text(
            "UPDATE section SET inpaint_status = NULL, inpaint_image_url = NULL, mask_image_url = NULL, residual_ratio = NULL, "
            "warning_badge = NULL, render_image_key = NULL, updated_at = now() WHERE id = ANY(:ids)"
        ),
        {"ids": section_ids},
    )


def proceed(db: Session, job_id: int, seller_id: int) -> dict[str, Any]:
    if not db.execute(text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"), {"j": job_id, "s": seller_id}).first():
        raise ProceedRejected(404, "JOB_NOT_FOUND", "job not found")
    job = execution.lock_job(db, job_id)
    active = execution.active_run(db, job_id, "downstream")
    if active is not None:
        db.rollback()
        return {"jobId": job_id, "accepted": True, "taskId": active["id"], "existing": True}
    if job["status"] == "cancelled" or job["current_step"] != "N3" or job["status"] != "review":
        raise ProceedRejected(409, "INVALID_STATE", f"proceed 는 N3 에서만({job['current_step']}/{job['status']})")
    if job["current_analysis_task_id"] is None:
        raise ProceedRejected(409, "INVALID_STATE", "채택된 초기 분석이 없다")
    secs = common.visible_sections(db, job_id)
    inc = [s for s in secs if s["bucket"] == "include"]
    if not inc:
        raise ProceedRejected(409, "ALL_SECTIONS_EXCLUDED", "all sections are excluded")
    brand = db.execute(text("SELECT id, name_ko, name_en FROM brand WHERE id = :b"), {"b": job["brand_id"]}).mappings().one()
    _clear_previous(db, [s["id"] for s in secs])
    scope = {
        "job_id": job_id, "analysis_task_id": job["current_analysis_task_id"], "section_ids": [s["id"] for s in inc],
        "source_image_ids": sorted({s["source_image_id"] for s in secs}),
        "brand": {"id": brand["id"], "name_ko": brand["name_ko"], "name_en": brand["name_en"]},
        "target_lang": job["target_lang"] or "en",
        "active_checks": text_check.configured_active_checks(),  # 이 실행에서 돌릴 검사(be-handoff 8.1) — 등록·집계·확정이 같은 값
    }
    run_id = execution.create_run(db, job_id, "downstream", scope)
    run = execution.lock_task(db, run_id)
    impl = ai_adapters.labeler().impl_version
    ids = []
    for s in inc:
        rows = _block_rows(db, s["id"])
        m = {"stage": "label", "section_id": s["id"], "section_image_sha256": _image_sha(db, s["image_key"]),
             "blocks_fp": _blocks_fp(rows), "labeler": impl}
        ids.append(execution.create_attempt(db, run=run, stage="label", unit_id=s["id"], manifest=m, target_count=len(rows),
                                            max_retry=MAX_RETRY["label"]))
    set_job_state(db, job_id, status="processing", step="N4", ufs="translating")
    db.commit()
    execution.dispatch(ids)
    return {"jobId": job_id, "accepted": True, "taskId": run_id, "existing": False}


def abort_downstream(db: Session, job_id: int) -> bool:
    run = execution.active_run(db, job_id, "downstream")
    if run is None:
        return False
    execution.cancel_run(db, run["id"])
    return True


# ---------------------------------------------------------------------------------------------------------
# 공통 워커 골격
# ---------------------------------------------------------------------------------------------------------
def _run_scope(db: Session, run_id: int) -> dict[str, Any]:
    return json_value(db.execute(text("SELECT run_scope FROM job_async_task WHERE id = :r"), {"r": run_id}).scalar_one())


def _guard(attempt_id: int, fn) -> dict[str, Any]:
    lease = execution.acquire(attempt_id, execution.worker_identity())
    if lease is None:
        return {"attemptId": attempt_id, "ran": False}
    try:
        return fn(lease)
    except execution.LeaseLost:
        return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
    except AdoptionRejected as e:  # 입력 검증 실패(고정 입력과 다름 등)
        return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), e.retryable)))
    except ai_adapters.AdapterUnavailable as e:
        return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), False)))
    except ai_adapters.AdapterFailed as e:  # AI 보고의 단계 실패(블록별 결과 없음)·BE 검증 실패
        return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), e.retryable)))
    except Exception as e:  # noqa: BLE001
        log.exception("%s 실패 attempt=%s", lease.stage, attempt_id)
        execution.fail_attempt(lease, f"{lease.stage.upper()}_UNEXPECTED", f"{e.__class__.__name__}: {e}", retryable=True)
        return {"attemptId": attempt_id, "ran": True, "failed": True}


def _load(lease: execution.Lease) -> dict[str, Any]:
    m = lease.manifest
    db = app_db.SessionLocal()
    try:
        sec = _section_row(db, m["section_id"])
        rows = _block_rows(db, m["section_id"])
        scope = _run_scope(db, lease.run_id)
        label = _adopted_payload(db, lease.run_id, "label", m["section_id"])
        logo = _adopted_payload(db, lease.run_id, "logo", m["section_id"])
    finally:
        db.close()
    if "blocks_fp" in m and _blocks_fp(rows) != m["blocks_fp"]:
        raise AdoptionRejected("블록이 고정 입력과 다르다", code="INPUT_CHANGED")
    return {"sec": sec, "rows": rows, "scope": scope, "label": label, "logo": logo}


def _check_ref(m: dict[str, Any], name: str, got: tuple[int, dict[str, Any]] | None) -> dict[str, Any]:
    want = m.get(name)
    if got is None or want is None or got[0] != want["handoff_id"] or stable_hash(got[1]) != want["payload_sha256"]:
        raise AdoptionRejected(f"{name} 인계가 고정 입력과 다르다", code="INPUT_CHANGED")
    return got[1]


# ---------------------------------------------------------------------------------------------------------
# ④ 라벨
# ---------------------------------------------------------------------------------------------------------
def execute_label(attempt_id: int) -> dict[str, Any]:
    def work(lease):
        d = _load(lease)
        sec, rows = d["sec"], d["rows"]
        if not rows:  # 블록 0개: 호출 없이 정상 생략(D1)
            env = Envelope(outcome="skipped", skip_reason="no_targets", target_count=0, input_fingerprint=lease.fingerprint,
                           payload={"label": None})
            return report(attempt_id, submit(lease, env))
        lb = ai_adapters.labeler()
        with tempfile.TemporaryDirectory(prefix=f"px-label-{attempt_id}-") as tmp:
            img = _download_section(sec, lease.manifest["section_image_sha256"], Path(tmp).resolve())
            with common.Heartbeat(lease) as hb:
                res = lb.label(_pipeline_section(sec, str(img)), _text_blocks(sec["id"], rows))
            if hb.lost:
                return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
        payload = {"label": res.model_dump(mode="json")}
        if res.status != "ok":
            env = Envelope(outcome="failed", target_count=len(rows), input_fingerprint=lease.fingerprint, impl_version=lb.impl_version,
                           payload={**payload, "error_code": "LABEL_FAILED", "message": res.error or "", "retryable": True})
        else:
            env = Envelope(outcome="done", target_count=len(rows), input_fingerprint=lease.fingerprint, impl_version=lb.impl_version,
                           payload=payload)
        return report(attempt_id, submit(lease, env))

    return _guard(attempt_id, work)


class LabelHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        from pipeline.types import LabelResult

        h = ctx.handoff
        p = json_value(h["payload"])
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "LABEL_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        sid = ctx.attempt["unit_id"]
        rows = _block_rows(db, sid)
        if h["outcome"] == "skipped":
            if rows:
                raise AdoptionRejected("블록이 있는데 정상 생략", code="LABEL_RESULT_INVALID")
            return AdoptOutcome(status="done", skip_reason="no_targets")
        res = LabelResult.model_validate(p["label"])
        from pipeline.stages.label import input_fingerprint

        if res.section_key != section_key(sid) or res.checked.input_fingerprint != input_fingerprint(_text_blocks(sid, rows)):
            raise AdoptionRejected("④ 결과의 섹션·입력 지문 불일치", code="LABEL_RESULT_INVALID")
        by_key = {block_key(r["id"]): r["id"] for r in rows}
        keys = [d.block_key for d in res.labels]
        if sorted(keys) != sorted(by_key) or len(set(keys)) != len(keys):
            raise AdoptionRejected("④ 판정 대상 키 누락·중복·미등록", code="LABEL_RESULT_INVALID")
        for d in res.labels:
            db.execute(text("UPDATE text_block SET is_product_label = :v, is_brand_logo = NULL, updated_at = now() WHERE id = :b"),
                       {"v": d.is_product_label, "b": by_key[d.block_key]})
        return AdoptOutcome(status="done", verify_result={"labels": {str(by_key[d.block_key]): d.is_product_label for d in res.labels}})

    def after_done(self, db: Session, att: dict[str, Any]) -> list[int]:
        run = execution.lock_task(db, att["parent_task_id"])
        sid = att["unit_id"]
        sec = _section_row(db, sid)
        rows = _block_rows(db, sid)
        label = _adopted_payload(db, run["id"], "label", sid)
        hid, payload = label if label else (None, None)
        m = {"stage": "logo", "section_id": sid, "blocks_fp": _blocks_fp(rows), "brand": _run_scope(db, run["id"])["brand"],
             "label": {"handoff_id": hid, "payload_sha256": stable_hash(payload)} if hid else None,
             "logo": ai_adapters.logo().impl_version, "section_image_sha256": _image_sha(db, sec["image_key"])}
        return [execution.create_attempt(db, run=run, stage="logo", unit_id=sid, manifest=m, target_count=len(rows),
                                         max_retry=MAX_RETRY["logo"])]

    def after_failure(self, db, att, code, retryable):
        return _after_failure(db, att, code, retryable)


# ---------------------------------------------------------------------------------------------------------
# ⑤ 로고
# ---------------------------------------------------------------------------------------------------------
def execute_logo(attempt_id: int) -> dict[str, Any]:
    def work(lease):
        from pipeline.types import LabelResult, MergeResult

        d = _load(lease)
        sec, rows = d["sec"], d["rows"]
        if not rows:
            env = Envelope(outcome="skipped", skip_reason="no_targets", target_count=0, input_fingerprint=lease.fingerprint,
                           payload={"logo": None, "logo_record": None})
            return report(attempt_id, submit(lease, env))
        label = LabelResult.model_validate(_check_ref(lease.manifest, "label", d["label"])["label"])
        merged = MergeResult(section_key=section_key(sec["id"]), blocks=_text_blocks(sec["id"], rows))
        lg = ai_adapters.logo()
        image_ids = [f"src_{i}" for i in d["scope"]["source_image_ids"]]
        result, record = lg.logo(_image_id(sec), merged, label, _brand_meta(d["scope"], image_ids))
        env = Envelope(outcome="done", target_count=len(rows), input_fingerprint=lease.fingerprint, impl_version=lg.impl_version,
                       payload={"logo": result.model_dump(mode="json"), "logo_record": record})
        return report(attempt_id, submit(lease, env))

    def guarded(lease):
        from pipeline.stages.logo import LogoInputError

        try:
            return work(lease)
        except LogoInputError as e:
            return report(attempt_id, submit(lease, failed_envelope(lease, "LOGO_INPUT_ERROR", str(e), False)))

    return _guard(attempt_id, guarded)


class LogoHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        from pipeline.types import LogoResult

        h = ctx.handoff
        p = json_value(h["payload"])
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "LOGO_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        sid = ctx.attempt["unit_id"]
        rows = _block_rows(db, sid)
        if h["outcome"] == "skipped":
            if rows:
                raise AdoptionRejected("블록이 있는데 정상 생략", code="LOGO_RESULT_INVALID")
            return AdoptOutcome(status="done", skip_reason="no_targets")
        res = LogoResult.model_validate(p["logo"])
        rec = p.get("logo_record")
        if res.status != "ok" or not isinstance(rec, dict) or rec.get("status") != "ok":
            raise AdoptionRejected("⑤ 결과·기록이 ok 가 아니다", code="LOGO_RESULT_INVALID")
        by_key = {block_key(r["id"]): r for r in rows}
        keys = [d.block_key for d in res.decisions]
        if sorted(keys) != sorted(by_key) or len(set(keys)) != len(keys):
            raise AdoptionRejected("⑤ 판정 대상 키 누락·중복·미등록", code="LOGO_RESULT_INVALID")
        bad = []
        for d in res.decisions:
            r = by_key[d.block_key]
            if (r["is_product_label"], d.is_brand_logo) not in ((True, None), (False, True), (False, False)):
                bad.append(d.block_key)  # D1 정상 완료 조합이 아니다
        if bad:
            raise AdoptionRejected(f"④⑤ 정상 완료 조합이 아닌 블록 {bad}", code="LOGO_RESULT_INVALID")
        for d in res.decisions:
            db.execute(text("UPDATE text_block SET is_brand_logo = :v, updated_at = now() WHERE id = :b"),
                       {"v": d.is_brand_logo, "b": by_key[d.block_key]["id"]})
        return AdoptOutcome(status="done")

    def after_done(self, db: Session, att: dict[str, Any]) -> list[int]:
        return _create_parallel(db, att)

    def after_failure(self, db, att, code, retryable):
        return _after_failure(db, att, code, retryable)


def _create_parallel(db: Session, att: dict[str, Any]) -> list[int]:
    """⑤ 채택 직후 같은 트랜잭션에서 ⑥⑦⑧ 시도를 함께 만든다(D1, 5.32 섹션별 후속 생성)."""
    run = execution.lock_task(db, att["parent_task_id"])
    sid = att["unit_id"]
    sec = _section_row(db, sid)
    rows = _block_rows(db, sid)
    scope = _run_scope(db, run["id"])
    label = _adopted_payload(db, run["id"], "label", sid)
    logo = _adopted_payload(db, run["id"], "logo", sid)
    refs = {
        "label": {"handoff_id": label[0], "payload_sha256": stable_hash(label[1])} if label else None,
        "logo": {"handoff_id": logo[0], "payload_sha256": stable_hash(logo[1])} if logo else None,
    }
    sha = _image_sha(db, sec["image_key"])
    base = {"section_id": sid, "section_image_sha256": sha, "blocks_fp": _blocks_fp(rows), **refs}
    ids = [
        execution.create_attempt(db, run=run, stage="inpaint", unit_id=sid,
                                 manifest={**base, "stage": "inpaint", "inpainter": ai_adapters.inpainter().impl_version},
                                 target_count=sum(1 for r in rows if r["is_excluded"] is False), max_retry=MAX_RETRY["inpaint"]),
        execution.create_attempt(db, run=run, stage="style", unit_id=sid,
                                 manifest={**base, "stage": "style", "styler": ai_adapters.styler().impl_version},
                                 target_count=sum(1 for r in rows if r["is_excluded"] is False), max_retry=MAX_RETRY["style"]),
    ]
    targets = _translate_targets(rows)
    supply = _translate_supply(db, run["id"], sid, targets) if targets else None
    ids.append(execution.create_attempt(
        db, run=run, stage="translate", unit_id=sid,
        manifest=_translate_manifest(sid, rows, targets, scope["target_lang"], preserved=[], supply=supply),
        target_count=len(targets), max_retry=MAX_RETRY["translate"]))
    return ids


# ---------------------------------------------------------------------------------------------------------
# ⑦ 스타일 — 원본 섹션 이미지 사용(⑥ 결과를 기다리지 않음)
# ---------------------------------------------------------------------------------------------------------
STYLE_KEYS = ("font_color", "bg_color", "est_font_px", "align")


def execute_style(attempt_id: int) -> dict[str, Any]:
    def work(lease):
        from pipeline.types import LabelResult, LogoResult, MergeResult

        d = _load(lease)
        sec, rows = d["sec"], d["rows"]
        if not any(r["is_excluded"] is False for r in rows):
            env = Envelope(outcome="skipped", skip_reason="no_targets", target_count=0, input_fingerprint=lease.fingerprint,
                           payload={"style": None})
            return report(attempt_id, submit(lease, env))
        label = LabelResult.model_validate(_check_ref(lease.manifest, "label", d["label"])["label"])
        lp = _check_ref(lease.manifest, "logo", d["logo"])
        logo, record = LogoResult.model_validate(lp["logo"]), lp["logo_record"]
        st = ai_adapters.styler()
        with tempfile.TemporaryDirectory(prefix=f"px-style-{attempt_id}-") as tmp:
            img = _download_section(sec, lease.manifest["section_image_sha256"], Path(tmp).resolve())
            res = st.style(_image_id(sec), _pipeline_section(sec, str(img)),
                           MergeResult(section_key=section_key(sec["id"]), blocks=_text_blocks(sec["id"], rows)), label, logo, record)
        env = Envelope(outcome="done", target_count=sum(1 for r in rows if r["is_excluded"] is False), input_fingerprint=lease.fingerprint,
                       impl_version=st.impl_version, payload={"style": res.model_dump(mode="json")})
        return report(attempt_id, submit(lease, env))

    def guarded(lease):
        from pipeline.stages.style import StyleInputError

        try:
            return work(lease)
        except StyleInputError as e:
            return report(attempt_id, submit(lease, failed_envelope(lease, "STYLE_INPUT_ERROR", str(e), False)))

    return _guard(attempt_id, guarded)


class StyleHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        from pipeline.types import parse_style_result

        h = ctx.handoff
        p = json_value(h["payload"])
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "STYLE_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        sid = ctx.attempt["unit_id"]
        rows = _block_rows(db, sid)
        if h["outcome"] == "skipped":
            if any(r["is_excluded"] is False for r in rows):
                raise AdoptionRejected("처리 대상이 있는데 정상 생략", code="STYLE_RESULT_INVALID")
            return AdoptOutcome(status="done", skip_reason="no_targets")
        # 계산 당시 형식(schema_version)대로 검증하고 그때의 측정값을 그대로 채택한다 — 배포 전 계산된 스키마 1 결과도 살린다.
        # 알 수 없는 버전 · 형식 위반은 거절(인계 rejected · 시도 failed → 스타일 실패 후속). 일시 오류로 다루지 않는다(반복 채택 방지)
        try:
            res = parse_style_result(p.get("style"))
        except ValueError as e:  # pydantic ValidationError 포함
            raise AdoptionRejected(f"⑦ 결과 형식 오류: {e}"[:1500], code="STYLE_RESULT_INVALID") from e
        by_key = {block_key(r["id"]): r for r in rows}
        keys = [b.block_key for b in res.blocks]
        if sorted(keys) != sorted(by_key):
            raise AdoptionRejected("⑦ 결과의 블록 누락·미등록", code="STYLE_RESULT_INVALID")
        for b in res.blocks:
            r = by_key[b.block_key]
            if b.status == "excluded":
                if r["is_excluded"] is not True:
                    raise AdoptionRejected(f"{b.block_key}: 제외 블록이 아닌데 excluded", code="STYLE_RESULT_INVALID")
                continue
            style = {k: getattr(b, k) for k in STYLE_KEYS}  # 측정 불가는 명시적 NULL(5.12)
            db.execute(text("UPDATE text_block SET style = CAST(:s AS jsonb), updated_at = now() WHERE id = :b"),
                       {"s": json.dumps(style), "b": r["id"]})
        return AdoptOutcome(status="done")

    def after_done(self, db, att):
        return progress(db, att["parent_task_id"], att["unit_id"])

    def after_failure(self, db, att, code, retryable):
        return _after_failure(db, att, code, retryable)


# ---------------------------------------------------------------------------------------------------------
# ⑧ 번역
# ---------------------------------------------------------------------------------------------------------
def _translate_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """번역 대상: 라벨·로고가 아닌(is_excluded IS FALSE) 비어 있지 않은 원문 블록. 빈 문자열 블록은 제외(D1·D3)."""
    return [r for r in rows if r["is_excluded"] is False and (r["source_ko"] or "").strip()]


# ⑧ 표현 지시 공급 규칙(#71). 시도 명세 supply 에 이 값이 없는 시도(활성화 전에 만든 시도)는 지시 없이 공급을 다시 만들어
# 대조·실행한다 — 기존 대기 시도가 INPUT_CHANGED 로 멈추지 않고, 재시도로 새로 만드는 시도부터 지시를 넣는다.
INSTRUCTIONS_RULE = "translate-instructions@2026-11-03.1"
_PLACEHOLDER = re.compile(r"\[[^\[\]\n]+\]")  # 템플릿 대체 표현(예: [value]) — 번역 지시로 쓰지 않는다(D9-2, PR #55 규칙과 같음)


def _analysis_bundle(db: Session, analysis_run_id: int) -> tuple[int, dict[str, Any]]:
    """이 N4 실행의 근거 분석 실행이 고정한 사전 묶음(분석 인계 payload). 실행 중 새 묶음을 만들지 않는다(D5)."""
    r = db.execute(
        text("SELECT h.id, h.payload FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
             "WHERE a.parent_task_id = :r AND a.stage = 'analyze' AND h.state = 'adopted' ORDER BY h.id DESC LIMIT 1"),
        {"r": analysis_run_id},
    ).first()
    if r is None:
        raise AdoptionRejected(f"분석 실행 {analysis_run_id} 의 고정 사전 묶음 없음", code="INPUT_MISSING")
    return r[0], json_value(r[1])["bundle"]


def _glossary_categories(db: Session, sid: int) -> list[str]:
    """적용 분류: 작업 internal_category ∪ common. 작업 분류가 NULL이면 common만(통합 D10). 국가 조건은 없다(용어집에 국가 열 없음)."""
    cat = db.execute(text("SELECT j.internal_category FROM section s JOIN job j ON j.id = s.job_id WHERE s.id = :s"), {"s": sid}).scalar()
    return sorted({"common"} | ({cat} if cat is not None else set()))


def select_glossary(rows: list[dict[str, Any]], targets: list[dict[str, Any]], selection: dict[str, Any]) -> dict[str, Any]:
    """DB 용어 행(id 순) 중 이번 번역 대상 블록 원문에서 유효한 용어만 고른다(`pipeline.glossary`, 통합 D10).
    0건은 빈 목록 성공, 데이터 오류(빈 원문·정규화 원문 충돌 등)는 status=failed — 빈 성공으로 숨기지 않는다.
    version 은 선별 규칙·적용 분류·선택 행 내용(원본 version 포함)의 지문이다."""
    from pipeline import glossary as gl

    try:
        chosen = gl.select([(block_key(r["id"]), r["source_ko"] or "") for r in targets],
                           [gl.Entry(str(g["id"]), g["term_ko"], g["term_target"], g["enforcement"], g.get("term_kind"))
                            for g in rows])  # term_kind = DB 값 그대로(NULL → None, 접두어로 추정하지 않음)
    except gl.GlossaryDataError as e:
        return {"status": "failed", "version": None, "reason": f"용어집 선별 실패: {e}", "terms": []}
    ids = {e.glossary_id for e in chosen}
    picked = [g for g in rows if str(g["id"]) in ids]
    terms = [{"glossary_id": str(g["id"]), "term_ko": g["term_ko"], "term_target": g["term_target"], "enforcement": g["enforcement"]}
             for g in picked]
    content = [{**t, "internal_category": g["internal_category"], "row_version": g["version"]} for t, g in zip(terms, picked)]
    return {"status": "ok", "version": "db-sha256:" + fingerprint({"selection": selection, "terms": content})[:16], "reason": None,
            "terms": terms}


def _glossary_supply(db: Session, lang: str, sid: int, targets: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """용어집 공급 — 대상 언어·적용 분류 행에서 이번 대상 블록에 나오는 용어만(통합 D10, PR #56 임시 '대상 언어 전체'를 대체).
    반환: (AI에 넘길 공급, 공급 지문에 넣을 선별 입력)."""
    from pipeline import glossary as gl

    cats = _glossary_categories(db, sid)
    selection = {"rules": gl.SELECT_RULES_VERSION, "target_lang": lang, "categories": cats}
    rows = db.execute(
        text("SELECT id, term_ko, term_target, enforcement, internal_category, version, term_kind FROM glossary "
             "WHERE target_lang = :l AND internal_category = ANY(:c) AND retired_at IS NULL ORDER BY id"),
        {"l": lang, "c": cats},
    ).mappings().all()
    return select_glossary([dict(r) for r in rows], targets, selection), selection


def _translate_instructions(db: Session, sid: int, target_ids: set[int], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """표현 지시: N3 채택 판정 중 대체 표현이 있는 금지형(regulated) 규제 판정의 매칭을 이번 대상 블록에 한해 넘긴다.
    대체 표현 본문은 AI가 고정 묶음에서 꺼낸다(#71 구분 규칙으로 나눈 배열). 템플릿 대체 표현(RG-021/022 등)은 원문 수치
    유지라 넘기지 않는다(D9-2). 판정 단계에서 억제된 매칭(overlaps kind=suppressed — contained · allowed_wrap 등)은 판정의
    매칭이 아니므로 지시하지 않는다(audit matches 에는 억제된 원시 매칭도 남아 있다)."""
    det = json_value(db.execute(
        text("SELECT detail FROM audit_log WHERE action_type = :a AND target_type = 'section' AND target_id = :s ORDER BY id DESC LIMIT 1"),
        {"a": common.ACT_ANALYSIS_ADOPTED, "s": sid},
    ).scalar()) or {}
    refs = entries_by_ref(bundle)
    suppressed = {o["match_key"] for o in det.get("overlaps", []) if o.get("kind") == "suppressed" and o.get("match_key")}
    out: list[dict[str, Any]] = []
    for v in det.get("verdicts", []):
        e = refs.get(v["dictionary_ref"])
        if e is None or e["dict_type"] != "regulatory" or v["verdict_status"] != "regulated":
            continue
        alts = ai_alternatives(e["alternative_expression"])
        if not isinstance(alts, list) or not alts or any(_PLACEHOLDER.search(a) for a in alts):
            continue  # 구분 규칙 위반(원문 문자열) · 대체 표현 없음 · 템플릿은 지시하지 않는다
        for m in det.get("matches", []):
            if m.get("match_key") in suppressed:
                continue
            if m["snapshot_key"] == v["dictionary_ref"] and m["finding_key"] == v["finding_key"] and m["block_id"] in target_ids:
                item = {"block_key": block_key(m["block_id"]), "external_id": v["dictionary_ref"], "matched_text": m["matched_text"]}
                if item not in out:
                    out.append(item)
    return out


def _translate_supply(db: Session, run_id: int, sid: int, targets: list[dict[str, Any]], *,
                      instructions: bool = True) -> dict[str, Any]:
    """instructions=False: 표현 지시 활성화 전 공급 규칙(지시 없음) — 그 전에 만든 시도의 대조·실행에만 쓴다."""
    scope = _run_scope(db, run_id)
    hid, bundle = _analysis_bundle(db, scope["analysis_task_id"])
    glossary, selection = _glossary_supply(db, scope["target_lang"], sid, targets)
    ins = _translate_instructions(db, sid, {r["id"] for r in targets}, bundle) if instructions else []
    return {"bundle_handoff_id": hid, "bundle": bundle, "glossary": glossary, "glossary_selection": selection,
            "instructions": ins, "instructions_rule": INSTRUCTIONS_RULE if instructions else None}


def _supply_ref(supply: dict[str, Any]) -> dict[str, Any]:
    """시도 명세에 고정할 공급 지문. 실행 때 다시 만든 공급과 대조한다(5.32 입력 고정)."""
    ref = {"bundle_handoff_id": supply["bundle_handoff_id"], "bundle_sha256": supply["bundle"]["sha256"],
           "glossary_sha256": fingerprint(supply["glossary"]), "glossary_selection": supply["glossary_selection"],
           "instructions": supply["instructions"]}
    if supply["instructions_rule"] is not None:  # 활성화 전 시도의 명세에는 이 키가 없다
        ref["instructions_rule"] = supply["instructions_rule"]
    return ref


def _instructions_enabled(fixed: dict[str, Any] | None) -> bool:
    """이 시도에 표현 지시를 넣는가. 고정 공급에 instructions_rule 이 없으면 활성화 전에 만든 시도 — 지시 없이 대조·실행한다."""
    return fixed is None or "instructions_rule" in fixed


def _translate_manifest(sid: int, rows: list[dict[str, Any]], targets: list[dict[str, Any]], lang: str,
                        preserved: list[dict[str, Any]], supply: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "stage": "translate", "section_id": sid, "blocks_fp": _blocks_fp(rows), "target_lang": lang,
        "expected": [r["id"] for r in _translate_targets(rows)],
        "targets": [{"block_id": r["id"], "revision": r["revision"]} for r in targets],
        "preserved": preserved, "translator": ai_adapters.translator().impl_version,
        "supply": _supply_ref(supply) if supply is not None else None,
    }


def execute_translate(attempt_id: int) -> dict[str, Any]:
    def work(lease):
        m = lease.manifest
        d = _load(lease)
        sec, rows = d["sec"], d["rows"]
        if not m["targets"]:
            env = Envelope(outcome="skipped", skip_reason="no_targets", target_count=0, input_fingerprint=lease.fingerprint,
                           payload={"translate": None})
            return report(attempt_id, submit(lease, env))
        target_ids = {t["block_id"] for t in m["targets"]}
        tblocks = [ai_adapters.TranslateBlock(key=block_key(r["id"]), block_order=r["block_order"], role=r["role"],
                                              source_ko=r["source_ko"] or "", is_target=r["id"] in target_ids)
                   for r in rows if r["is_excluded"] is False and (r["source_ko"] or "").strip()]
        db = app_db.SessionLocal()
        try:
            supply = _translate_supply(db, lease.run_id, sec["id"], [r for r in rows if r["id"] in target_ids],
                                       instructions=_instructions_enabled(m.get("supply")))
        finally:
            db.close()
        if m.get("supply") is not None and _supply_ref(supply) != m["supply"]:
            raise AdoptionRejected("번역 공급(사전 묶음·용어집·표현 지시)이 고정 입력과 다르다", code="INPUT_CHANGED")
        bundle = supply["bundle"]
        handoff = {
            "execution_id": str(lease.run_id), "attempt_id": str(attempt_id),
            "target_country": bundle["target_country"], "regulatory_class": bundle["regulatory_class"],
            "blocks": [b.model_dump(mode="json") for b in _text_blocks(sec["id"], rows)],
            "targets": [{"block_key": block_key(t["block_id"]), "revision": t["revision"]} for t in m["targets"]],
            "preserved": [{"block_key": block_key(p["block_id"]), "revision": p["revision"], "translation_sha256": p["sha256"],
                           "source_attempt_id": str(p.get("source_attempt") or attempt_id)} for p in m["preserved"]],
            "instructions": supply["instructions"], "glossary": supply["glossary"], "bundle": bundle,
        }
        tr = ai_adapters.translator()
        with common.Heartbeat(lease) as hb:
            out = tr.translate(section_key(sec["id"]), tblocks, target_lang=m["target_lang"],
                               context={"section_id": sec["id"], "job_id": lease.job_id, "handoff": handoff})
        if hb.lost:
            return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
        if not isinstance(out, ai_adapters.TranslateOutcome):
            out = ai_adapters.TranslateOutcome.model_validate(out)
        env = Envelope(outcome="done", target_count=len(m["targets"]), input_fingerprint=lease.fingerprint,
                       impl_version=tr.impl_version, payload={"translate": out.model_dump(mode="json")})
        return report(attempt_id, submit(lease, env))

    return _guard(attempt_id, work)


class TranslateHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        h = ctx.handoff
        p = json_value(h["payload"])
        m = ctx.attempt["input_manifest"]
        sid = ctx.attempt["unit_id"]
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "TRANSLATE_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        if h["outcome"] == "skipped":
            if m["targets"]:
                raise AdoptionRejected("번역 대상이 있는데 정상 생략", code="TRANSLATE_RESULT_INVALID")
            return AdoptOutcome(status="done", skip_reason="no_targets")
        out = ai_adapters.TranslateOutcome.model_validate(p["translate"])
        want = {block_key(t["block_id"]): t for t in m["targets"]}
        keys = [i.key for i in out.items]
        if out.section_key != section_key(sid) or sorted(keys) != sorted(want) or len(set(keys)) != len(keys):
            raise AdoptionRejected("번역 대상 키 누락·중복·미등록(구조 실패)", code="TRANSLATE_RESULT_INVALID", retryable=True)
        for i in out.items:
            if (i.status == "ok") != (i.text is not None and i.text.strip() != ""):
                raise AdoptionRejected(f"{i.key}: ok 이면 비어 있지 않은 번역문, failed 면 번역문 없음", code="TRANSLATE_RESULT_INVALID",
                                       retryable=True)
        # 보존 성공분 대조(실패 대상 재시도): 출처·문장 해시·revision 이 그대로인 것만 이전 성공으로 센다(5.32)
        preserved_ok, preserved_stale = [], []
        for ref in m["preserved"]:
            r = db.execute(text("SELECT revision, trans_1 FROM text_block WHERE id = :b"), {"b": ref["block_id"]}).mappings().first()
            if r and r["revision"] == ref["revision"] and r["trans_1"] is not None and sha256_text(r["trans_1"]) == ref["sha256"]:
                preserved_ok.append(ref["block_id"])
            else:
                preserved_stale.append(ref["block_id"])
        applied, conflicts, failed = [], [], []
        for i in out.items:
            t = want[i.key]
            if i.status == "failed":
                failed.append(t["block_id"])
                continue
            r = db.execute(
                text(
                    "UPDATE text_block SET trans_1 = :t, char_count = char_length(:t), block_status = 'machine', "
                    "revision = revision + 1, overflow = false, updated_at = now() "
                    "WHERE id = :b AND revision = :rev AND block_status = 'machine' RETURNING revision"
                ),
                {"t": i.text, "b": t["block_id"], "rev": t["revision"]},
            ).first()
            if r is None:
                conflicts.append(t["block_id"])  # 사용자 수정 이후 — 자동 번역으로 덮어쓰지 않는다
            else:
                applied.append({"block_id": t["block_id"], "revision": r[0], "sha256": sha256_text(i.text),
                                "source_attempt": ctx.attempt["id"], "source_handoff": h["id"]})
        # 사용자가 직접 입력한 블록(edited)은 실패로 세지 않는다
        edited = {r[0] for r in db.execute(
            text("SELECT id FROM text_block WHERE id = ANY(:ids) AND block_status = 'edited'"),
            {"ids": failed + conflicts + preserved_stale}).all()}
        still_failed = [b for b in failed + preserved_stale if b not in edited]
        verify = {"applied": applied, "failed": still_failed, "conflicts": conflicts, "preserved": preserved_ok,
                  "user_edited": sorted(edited)}
        successes = len(applied) + len(preserved_ok) + len(edited)
        if not still_failed:
            return AdoptOutcome(status="done", verify_result=verify)
        code = TRANSLATE_ALL_FAILED if successes == 0 else TRANSLATE_PARTIAL
        return AdoptOutcome(status="failed", error_code=code, error_message=f"번역하지 못한 블록 {len(still_failed)}개",
                            verify_result=verify, retryable=code == TRANSLATE_ALL_FAILED)

    def after_done(self, db, att):
        # 실제 저장된 문구로 활성 검사 등록(등록 보장 ①②) — 같은 채택 트랜잭션
        return text_check.after_translate(db, att["parent_task_id"], att["unit_id"]) + progress(db, att["parent_task_id"], att["unit_id"])

    def after_failure(self, db, att, code, retryable):
        if code == TRANSLATE_PARTIAL:
            ids = text_check.after_translate(db, att["parent_task_id"], att["unit_id"])  # 저장된 성공분만 검사 대상
            return ids + progress(db, att["parent_task_id"], att["unit_id"])  # 성공분 보존·실패 칸 비움으로 N5 진행(D9-3)
        return _after_failure(db, att, code, retryable)


def translate_retry_plan(db: Session, attempt: dict[str, Any]) -> dict[str, Any]:
    """⑧ 실패 대상 재시도: 실패 블록만 계산 대상으로, 채택된 성공분은 출처·해시·revision 참조로 고정한다(5.32)."""
    sid = attempt["unit_id"]
    rows = _block_rows(db, sid)
    expected = set(attempt["input_manifest"]["expected"])
    vr = db.execute(
        text("SELECT verify_result FROM task_handoff WHERE task_id = :t AND state = 'adopted'"), {"t": attempt["id"]}
    ).scalar()
    vr = json_value(vr) or {}
    by_id = {r["id"]: r for r in rows}
    failed_ids = [b for b in expected if by_id.get(b) and by_id[b]["block_status"] == "machine" and by_id[b]["trans_1"] is None]
    # 보존 성공분의 출처 시도: 같은 논리 작업에서 채택된 번역 인계의 반영 기록(나중 것이 우선)
    origin: dict[int, int] = {}
    for (vr_i,) in db.execute(
        text("SELECT h.verify_result FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
             "WHERE a.parent_task_id = :r AND a.stage = 'translate' AND a.unit_id = :s AND h.state = 'adopted' ORDER BY h.id"),
        {"r": attempt["parent_task_id"], "s": sid},
    ).all():
        for ap in (json_value(vr_i) or {}).get("applied", []):
            origin[ap["block_id"]] = ap["source_attempt"]
    preserved = [{"block_id": b, "revision": by_id[b]["revision"], "sha256": sha256_text(by_id[b]["trans_1"]),
                  "source_attempt": origin.get(b)}
                 for b in sorted(expected) if by_id.get(b) and by_id[b]["trans_1"] is not None and by_id[b]["block_status"] == "machine"]
    targets = [by_id[b] for b in sorted(failed_ids)]
    if not vr and not targets:
        targets = [by_id[b] for b in sorted(expected) if by_id.get(b)]
    scope_lang = attempt["input_manifest"]["target_lang"]
    supply = _translate_supply(db, attempt["parent_task_id"], sid, targets) if targets else None
    return {"manifest": _translate_manifest(sid, rows, targets, scope_lang, preserved, supply=supply), "target_count": len(targets)}


# ---------------------------------------------------------------------------------------------------------
# ⑥ 인페인트 채택(원격 GPU 인계 — 산출물 고정은 app.flows.gpu_worker)
# ---------------------------------------------------------------------------------------------------------
class InpaintHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        h = ctx.handoff
        p = json_value(h["payload"])
        sid = ctx.attempt["unit_id"]
        if h["outcome"] == "failed":
            db.execute(text("UPDATE section SET inpaint_status = 'failed', warning_badge = 'processing_failed', updated_at = now() "
                            "WHERE id = :s"), {"s": sid})
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "INPAINT_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        if h["outcome"] == "skipped":
            rows = _block_rows(db, sid)
            if any(r["is_excluded"] is False for r in rows):
                raise AdoptionRejected("처리 대상이 있는데 정상 생략", code="INPAINT_RESULT_INVALID")
            sec = _section_row(db, sid)
            db.execute(text("UPDATE section SET inpaint_status = 'done', inpaint_image_url = :k, mask_image_url = NULL, "
                            "residual_ratio = NULL, warning_badge = NULL, updated_at = now() WHERE id = :s"),
                       {"k": sec["image_key"], "s": sid})
            return AdoptOutcome(status="done", skip_reason="no_targets")
        arts = {a["kind"]: a for a in ctx.artifacts}
        status = p.get("status")
        need = {"background", "delete_mask", "protect_mask"}
        missing = sorted(k for k in need if k not in arts or arts[k]["state"] != "verified")
        if missing:
            raise AdoptionRejected(f"⑥ 필수 산출물 누락 {missing}", code="INPAINT_RESULT_INVALID")
        sec = _section_row(db, sid)
        w, hgt = json_value(sec["bbox"])["w"], sec["height"]
        for k in need:
            if (arts[k]["width"], arts[k]["height"]) != (w, hgt):
                raise AdoptionRejected(f"⑥ {k} 크기 {(arts[k]['width'], arts[k]['height'])} ≠ 섹션 {(w, hgt)}", code="INPAINT_RESULT_INVALID")
        counts = p.get("counts") or {}
        if status == "unchanged":
            if arts["background"]["source_ref"] is None or counts.get("final_px") != 0:
                raise AdoptionRejected("unchanged 는 마스크 0 증거와 원본 배경 참조가 필요하다", code="INPAINT_RESULT_INVALID")
        elif status == "inpainted":
            if arts["background"]["verified_key"] is None or not counts.get("final_px"):
                raise AdoptionRejected("inpainted 는 처리 배경 파일과 마스크 픽셀이 필요하다", code="INPAINT_RESULT_INVALID")
        else:
            raise AdoptionRejected(f"⑥ 결과 상태 {status!r}", code="INPAINT_RESULT_INVALID")
        db.execute(
            text("UPDATE section SET inpaint_status = 'done', inpaint_image_url = :bg, mask_image_url = :mk, residual_ratio = NULL, "
                 "warning_badge = NULL, updated_at = now() WHERE id = :s"),
            {"bg": artifacts.adopted_key(arts["background"]), "mk": artifacts.adopted_key(arts["delete_mask"]), "s": sid},
        )
        return AdoptOutcome(status="done", skip_reason=None)

    def after_done(self, db, att):
        return progress(db, att["parent_task_id"], att["unit_id"])

    def after_failure(self, db, att, code, retryable):
        if att["stage"] == "inpaint":
            db.execute(text("UPDATE section SET inpaint_status = 'failed', warning_badge = 'processing_failed', updated_at = now() "
                            "WHERE id = :s"), {"s": att["unit_id"]})
        return _after_failure(db, att, code, retryable)


# ---------------------------------------------------------------------------------------------------------
# ⑨ 초기 미리보기
# ---------------------------------------------------------------------------------------------------------
class RenderInputInvalid(ValueError):
    """렌더 대상 블록의 적용 박스를 읽을 수 없다(bbox NULL·좌표 키 누락·수가 아닌 값, 배치 조정값 형식 오류).
    고정 입력을 만들지 않는다 — 임의 값으로 채우거나 KeyError 로 흘리지 않는다(/preview `inputError` RENDER_INPUT_INVALID)."""
    code = "RENDER_INPUT_INVALID"

    def __init__(self, block_ids: list[int]):
        super().__init__(f"적용 박스를 읽을 수 없는 블록 {block_ids}")
        self.block_ids = sorted(block_ids)


def _applied_box(r: dict[str, Any]) -> dict[str, Any] | None:
    """적용 박스 = 셀러 조정 박스(layout_adjust.user)가 있으면 그것, 없으면 원본 bbox. 저장값 그대로(절삭 없음).
    형식이 맞지 않으면 None(호출자가 RenderInputInvalid 로 모은다). 범위(섹션 안·양의 크기) 검사는 여기서 하지 않는다."""
    box = layout_adjust.parse(r["layout_adjust"]).placement_source(json_value(r["bbox"]))
    if not isinstance(box, dict):
        return None
    out = {}
    for k in ("x", "y", "w", "h"):
        v = box.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return None
        out[k] = v
    return out


def _render_inputs(db: Session, run_id: int, sid: int, *, style_defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """⑨ 고정 입력(미리보기 시도·최종 렌더·/preview `inputFingerprint` 공통). 지문 = `fingerprint(이 값)`.
    박스는 저장값 그대로 싣는다(소수 절삭 없음 — 계약 PreviewRenderBlock.box). 정수 값은 JCS 직렬화에서 10 과 10.0 이 같은 "10" 이라
    정수 좌표 블록의 지문은 절삭하던 때와 같다. style_defaults 는 한 요청에서 여러 섹션을 만들 때 한 번 읽은 값을 넘긴다."""
    sec = _section_row(db, sid)
    rows = _block_rows(db, sid)
    inp = db.execute(
        text("SELECT status FROM job_async_task WHERE parent_task_id = :r AND stage = 'inpaint' AND unit_id = :s AND is_current"),
        {"r": run_id, "s": sid},
    ).scalar()
    bg_key = sec["inpaint_image_url"] if inp == "done" and sec["inpaint_image_url"] else sec["image_key"]
    blocks, bad = [], []
    for r in rows:
        if r["is_excluded"] is not False or not (r["source_ko"] or "").strip() or not (r["trans_1"] or "").strip():
            # 라벨·로고·빈 원문·번역 없음(실패 칸 NULL·빈 문자열·공백만)은 그리지 않는다 — resolve_block 의 조판 제외 규칙
            # (be-handoff 6.3)과 같게 둬야 미리보기 채택의 대상 블록 집합이 렌더 결과와 맞는다
            continue
        box = _applied_box(r)
        if box is None:
            bad.append(r["id"])
            continue
        block = {"block_id": r["id"], "revision": r["revision"], "role": r["role"], "trans_sha256": sha256_text(r["trans_1"]),
                 "box": box, "style_sha256": stable_hash(json_value(r["style"]))}
        # 셀러 배율은 있을 때만 싣는다 — 조정하지 않은 블록의 입력·지문은 배치 저장 전과 같다
        scale = layout_adjust.parse(r["layout_adjust"]).applied_scale
        if scale != 1:
            block["font_scale"] = scale
        blocks.append(block)
    if bad:
        raise RenderInputInvalid(bad)
    bg_sha = db.execute(text("SELECT sha256 FROM task_artifact WHERE verified_key = :k OR source_ref->>'key' = :k LIMIT 1"),
                        {"k": bg_key}).scalar()
    # 섹션의 ⑦ 단계 결과 — 기본값 적용 규칙(6.3)이 done / failed 를 나눈다(style 값만으로 판단하지 않는다)
    style_stage = db.execute(
        text("SELECT status FROM job_async_task WHERE parent_task_id = :r AND stage = 'style' AND unit_id = :s AND is_current"),
        {"r": run_id, "s": sid},
    ).scalar()
    return {"stage": "preview", "section_id": sid, "background": {"key": bg_key, "sha256": bg_sha,
                                                                    "source": "inpaint" if bg_key != sec["image_key"] else "original"},
            "blocks": blocks, "style_stage": style_stage, "renderer": typeset.RENDERER_VERSION, "bundle": html_render.BUNDLE_ID,
            "style_defaults": typeset.style_inputs() if style_defaults is None else style_defaults}


def _render_section(m: dict[str, Any], texts: dict[int, str], styles: dict[int, Any],
                    sources: dict[int, str | None]) -> typeset.RenderResult:
    data = get_store().get(m["background"]["key"])
    if m["background"]["sha256"] and sha256_bytes(data) != m["background"]["sha256"]:
        raise AdoptionRejected("배경이 고정 입력과 다르다", code="INPUT_CHANGED")
    if mismatch := common.renderer_mismatch(m):  # 미리보기·최종 렌더 공통(최종 렌더는 고정 입력의 renderer 를 넘긴다)
        raise AdoptionRejected(mismatch, code="INPUT_CHANGED")
    current = typeset.style_inputs()
    if "unavailable" in current:
        raise typeset.StyleDefaultsUnavailable(current["unavailable"])
    if current != m.get("style_defaults"):  # 설정 자료·폰트·규칙 버전 중 하나라도 다르면(6.6 ⑤)
        raise AdoptionRejected("스타일 기본값 자료가 고정 입력과 다르다", code="INPUT_CHANGED")
    defaults = typeset.load_role_defaults()
    # 원본 폭(기본 크기 환산 기준) = 섹션 배경 폭 — Pillow 렌더와 같은 기준(MVP 섹션은 원본 전체 폭)
    with Image.open(io.BytesIO(data)) as im:
        section_width = im.width
    blocks = []
    for b in m["blocks"]:
        try:
            # /preview renderInput 과 같은 함수 — N5 가 그린 style 8키를 서버도 그대로 쓴다(계약 6.4)
            built = render_input.build_block(block_id=b["block_id"], revision=b["revision"], role=b["role"], is_excluded=False,
                                             source_ko=sources[b["block_id"]], text=texts[b["block_id"]], style=styles[b["block_id"]],
                                             style_stage=m.get("style_stage"), box=b["box"],
                                             font_scale=b.get("font_scale", render_input.DEFAULT_FONT_SCALE),
                                             section_width=section_width, defaults=defaults.data)
        except sd.StyleInputInvalid as e:
            raise typeset.StyleInputInvalid(str(e)) from e
        if built is not None:
            blocks.append(built)
    return html_render.render(data, blocks)


def execute_preview(attempt_id: int) -> dict[str, Any]:
    def work(lease):
        m = lease.manifest
        db = app_db.SessionLocal()
        try:
            rows = {r["id"]: r for r in _block_rows(db, m["section_id"])}
        finally:
            db.close()
        texts, styles, sources = {}, {}, {}
        for b in m["blocks"]:
            r = rows.get(b["block_id"])
            if r is None or r["revision"] != b["revision"] or r["trans_1"] is None or sha256_text(r["trans_1"]) != b["trans_sha256"]:
                raise AdoptionRejected("블록 번역문·revision 이 고정 입력과 다르다(옛 렌더)", code="INPUT_CHANGED")
            if stable_hash(json_value(r["style"])) != b["style_sha256"]:
                raise AdoptionRejected("블록 스타일이 고정 입력과 다르다", code="INPUT_CHANGED")
            texts[b["block_id"]], styles[b["block_id"]], sources[b["block_id"]] = r["trans_1"], json_value(r["style"]), r["source_ko"]
        try:
            res = _render_section(m, texts, styles, sources)
        except (typeset.StyleDefaultsUnavailable, typeset.StyleInputInvalid) as e:
            return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), False, len(m["blocks"]))))
        env = Envelope(outcome="done", target_count=len(m["blocks"]), input_fingerprint=lease.fingerprint,
                       impl_version=typeset.RENDERER_VERSION,
                       payload={"render": {"width": res.width, "height": res.height,
                                           "blocks": {str(k): {"overflow": v.overflow, "applied": v.applied, "source": v.source,
                                                               "lines": v.lines, "ink_visible": v.ink_visible}
                                                      for k, v in res.blocks.items()}}},
                       artifacts=[ArtifactSpec(kind="render_image", part_key=section_key(m["section_id"]), data=res.png)])
        return report(attempt_id, submit(lease, env, {("render_image", section_key(m["section_id"])): res.png}))

    return _guard(attempt_id, work)


class PreviewHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        h = ctx.handoff
        p = json_value(h["payload"])
        m = ctx.attempt["input_manifest"]
        sid = ctx.attempt["unit_id"]
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "PREVIEW_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        art = next((a for a in ctx.artifacts if a["kind"] == "render_image" and a["state"] == "verified"), None)
        sec = _section_row(db, sid)
        if art is None or (art["width"], art["height"]) != (json_value(sec["bbox"])["w"], sec["height"]):
            raise AdoptionRejected("미리보기 이미지 없음·크기 불일치", code="PREVIEW_RESULT_INVALID")
        blocks = p["render"]["blocks"]
        if sorted(int(k) for k in blocks) != sorted(b["block_id"] for b in m["blocks"]):
            raise AdoptionRejected("미리보기 측정 대상 불일치", code="PREVIEW_RESULT_INVALID")
        stale = []
        for b in m["blocks"]:
            r = db.execute(
                text("UPDATE text_block SET overflow = :o WHERE id = :b AND revision = :rev RETURNING id"),
                {"o": bool(blocks[str(b["block_id"])]["overflow"]), "b": b["block_id"], "rev": b["revision"]},
            ).first()
            if r is None:
                stale.append(b["block_id"])
        if stale:  # 편집 이후 도착한 옛 렌더 — 이미지·overflow 를 반영하지 않는다
            raise AdoptionRejected(f"옛 revision 의 렌더 {stale}", code="PREVIEW_STALE")
        db.execute(text("UPDATE section SET render_image_key = :k, updated_at = now() WHERE id = :s"),
                   {"k": artifacts.adopted_key(art), "s": sid})
        return AdoptOutcome(status="done")

    def after_done(self, db, att):
        return progress(db, att["parent_task_id"], att["unit_id"])

    def after_failure(self, db, att, code, retryable):
        return _after_failure(db, att, code, retryable)

    def retry_block_reason(self, att):
        m = json_value(att["input_manifest"])
        if is_input_error_record(m):  # 렌더 입력을 만들지 못한 실패 기록 — 같은 입력으로는 다시 할 수 없다
            return f"렌더 입력을 만들 수 없다({m['input_error'].get('code')})"
        return common.renderer_mismatch(att["input_manifest"])


# ---------------------------------------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------------------------------------
def _after_failure(db: Session, att: dict[str, Any], code: str, retryable: bool) -> list[int]:
    run = db.execute(text("SELECT * FROM job_async_task WHERE id = :r"), {"r": att["parent_task_id"]}).mappings().one()
    if run["status"] != "running":
        return []
    if retryable and att["retry_count"] < min(AUTO_RETRY.get(att["stage"], 0), att["max_retry"]):
        plan = translate_retry_plan(db, att) if att["stage"] == "translate" else {}
        return [execution.create_retry(db, att["id"], "auto", **plan)]
    return progress(db, run["id"], att["unit_id"])


def _final(a: dict[str, Any] | None) -> bool:
    return a is not None and a["status"] in ("done", "failed")


def progress(db: Session, run_id: int, section_id: int | None = None) -> list[int]:
    """섹션별 다음 단계 생성과 N5·N4 오류 전이. 같은 트랜잭션(잠금 아래)에서 호출한다. 멱등."""
    run = dict(db.execute(text("SELECT * FROM job_async_task WHERE id = :r"), {"r": run_id}).mappings().one())
    if run["status"] != "running":
        return []
    job0 = db.execute(text("SELECT status, current_step FROM job WHERE id = :j"), {"j": run["job_id"]}).mappings().one()
    if job0["current_step"] == "N4" and job0["status"] != "processing":
        # N4 오류(처리 시간 초과 · 단계 실패) 동안은 후속 처리(대체 배경 · 미리보기)를 새로 만들지 않는다(README 7.8 ⑤ 늦은 완료 미채택).
        # [다시 시도](JOB-06)로 다시 처리 중이 되면 그때 집계가 이어서 만든다
        return []
    scope = json_value(run["run_scope"])
    atts = execution.current_attempts(db, run_id)
    by = {(a["stage"], a["unit_id"]): a for a in atts}
    ids: list[int] = []
    blocking = False
    all_ready = True
    tr_outcome = _translation_outcome(db, run_id, scope["section_ids"], by)
    for sid in scope["section_ids"]:
        lab, lg = by.get(("label", sid)), by.get(("logo", sid))
        inp, sty, tr, pv = by.get(("inpaint", sid)), by.get(("style", sid)), by.get(("translate", sid)), by.get(("preview", sid))
        if (lab and lab["status"] == "failed") or (lg and lg["status"] == "failed"):
            blocking = True
            all_ready = False
            continue
        if (tr and tr["status"] == "failed" and (tr["target_count"] or 0) > 0 and execution.timed_out(tr)
                and _limit_left(tr)):
            # 시간 초과로 닫혔고 횟수가 남은 번역 — N4 오류에 머물고 [다시 시도] 때 다시 실행한다(PM 11-27 1차: 시간 초과 = N4,
            # 남은 횟수 안에서 재실행). 빈 칸으로 넘기는 것은 다시 할 수 없는 번역(소진 · 일반 실패 최종)만이다
            blocking = True
            all_ready = False
            continue
        if tr and tr["status"] == "failed" and (tr["target_count"] or 0) > 0 and tr_outcome != "ok":
            # 작업 단위 번역 판정(PM 11-27): 작업 어디에도 번역 성공 블록이 아직 없다(= 이 섹션도 성공분 0).
            # "zero"(번역 대상 섹션이 모두 끝남) → 막힘(N4). "pending"(다른 섹션 번역이 아직 진행 중) → 미리보기를 만들지 않고 기다린다.
            # 성공 블록이 하나라도 생기면("ok") 실패 섹션은 성공분 보존 · 실패 칸 빈 칸으로 아래에서 진행한다
            # (섹션 하나의 성공분 0 이 다른 섹션의 성공분까지 막지 않는다). 대상 0개 정상 생략은 failed 가 아니다
            blocking = blocking or tr_outcome == "zero"
            all_ready = False
            continue
        if not (_final(inp) and _final(sty) and _final(tr)):
            all_ready = False
            continue
        if text_check.waiting(db, run, sid, by.get(("text_check", sid))):  # 활성 검사가 끝나야 N5(be-handoff 8.2)
            all_ready = False
        if pv is None:
            m, err = _preview_inputs(db, run_id, sid)
            if err is not None:
                # 렌더 입력을 만들 수 없다 — 실패 시도로 남기고(전달 안 함) 다른 미리보기 실패와 같게 최종 실패로 본다
                # (렌더 실패는 원본 표시로 진행 — D9-3, 기존 규칙). 이 섹션 때문에 다른 섹션 처리를 멈추지 않는다
                _record_input_error(db, run, sid, m, err, supersedes=None)
                continue
            ids.append(execution.create_attempt(db, run=run, stage="preview", unit_id=sid, manifest=m, target_count=len(m["blocks"]),
                                                max_retry=MAX_RETRY["preview"]))
            all_ready = False
            continue
        if not _final(pv):
            all_ready = False
            continue
        # 미리보기 이후 입력이 바뀌었으면(N5 번역 재시도 성공 등) 현재 입력으로 다시 그린다. 재시도 한도는 쓰지 않는다.
        # 현재 시도가 완료든 최종 실패든 지문이 다를 때만 — 같은 입력의 최종 실패는 그대로 둔다(실패 상태·재시도 횟수 보존).
        # 입력 오류도 같다: 같은 오류 상태(오류 고정 입력의 지문이 같음)면 새로 만들지 않고, 입력이 고쳐지면 정상 시도를 만든다.
        # 실행이 진행 중일 때만 여기까지 온다(위 run status 검사) — 취소·종료된 실행은 재개하지 않는다
        m, err = _preview_inputs(db, run_id, sid)
        if fingerprint(m) != pv["input_fingerprint"]:
            pv_row = execution.lock_task(db, pv["id"])
            if pv_row["is_current"] and pv_row["status"] in ("done", "failed"):  # 잠근 뒤 다시 확인 — 중복 호출에서 한 건만
                if err is not None:
                    _record_input_error(db, run, sid, m, err, supersedes=dict(pv_row))
                else:
                    ids.append(execution.create_attempt(db, run=run, stage="preview", unit_id=sid, manifest=m,
                                                        target_count=len(m["blocks"]), supersedes=pv_row, retry_origin="auto",
                                                        count_retry=False))
    job = db.execute(text("SELECT id, status, current_step FROM job WHERE id = :j"), {"j": run["job_id"]}).mappings().one()
    if job["current_step"] == "N4":
        if blocking and not ids and job["status"] == "processing":
            set_job_state(db, job["id"], status="failed", step="N4", ufs="failed")
        elif all_ready and not ids and job["status"] == "processing":
            # 처리 중일 때만 N5 로 — 처리 시간 초과(README 7.8 ⑤)로 오류가 된 작업은 늦게 끝난 결과로 다음 단계로 가지 않는다
            set_job_state(db, job["id"], status="review", step="N5", ufs="reviewing")
    return ids


def _preview_inputs(db: Session, run_id: int, sid: int) -> tuple[dict[str, Any], RenderInputInvalid | None]:
    """⑨ 고정 입력, 또는 만들 수 없으면(적용 박스를 읽을 수 없음 — RenderInputInvalid) 입력 오류 상태를 적은 고정 입력과 그 오류.
    입력 오류 고정 입력은 렌더 입력이 아니다(박스 · 배경 · 지문을 지어내지 않는다): 오류 코드와 대상 블록 id 만 적어,
    같은 오류 상태인지(지문 비교) 가리는 데만 쓴다. revision 은 넣지 않는다 — 나쁜 블록의 번역문만 다시 저장해도 오류 상태는 같으므로
    새 실패 시도를 만들지 않는다(입력이 고쳐졌는지는 정상 입력을 만들 수 있게 됐는지로 안다).
    이 경로 밖의 장애(DB 오류 등)는 잡지 않는다 — 호출자의 오류 · 롤백 그대로."""
    try:
        return _render_inputs(db, run_id, sid), None
    except RenderInputInvalid as e:
        return {"stage": "preview", "section_id": sid,
                "input_error": {"code": RenderInputInvalid.code, "block_ids": list(e.block_ids)}}, e


def is_input_error_record(manifest: Any) -> bool:
    """시도의 고정 입력이 입력 오류 기록(_preview_inputs 의 오류 쪽)인가 — 재시도 차단(retry_block_reason)과
    /preview 공개 지문(preview_view.current_attempt)이 같은 판별을 쓴다. 정상 렌더 입력에는 최상위 `input_error` 가 없다."""
    return isinstance(manifest, dict) and "input_error" in manifest


def _record_input_error(db: Session, run: dict[str, Any], sid: int, manifest: dict[str, Any], err: RenderInputInvalid,
                        supersedes: dict[str, Any] | None, origin: str = "auto") -> int:
    """README 7.2 ② · reviewPreview 「구현 상태」 ②: 렌더 입력을 만들 수 없는 섹션의 실패를 미리보기 시도로 남긴다.
    바로 failed(errorCode RENDER_INPUT_INVALID) — 큐로 보내지 않는다(실행할 입력이 없다). 재시도 횟수를 쓰지 않는다(입력 상태 기록).
    같은 실패를 다시 시도해도 같은 결과라 사용자 재시도 불가(PreviewHandler.retry_block_reason). 호출자가 잠금 · 커밋하고, 반환 id 를 전달하지 않는다."""
    if supersedes is not None and supersedes["status"] in ("pending", "running"):
        db.execute(text("UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
                        "lease_token_hash = NULL, lease_expires_at = NULL WHERE id = :t"), {"t": supersedes["id"]})
    tid = execution.create_attempt(db, run=run, stage="preview", unit_id=sid, manifest=manifest, target_count=0,
                                   supersedes=supersedes, retry_origin=origin if supersedes is not None else None,
                                   count_retry=False, max_retry=MAX_RETRY["preview"])
    db.execute(text("UPDATE job_async_task SET status = 'failed', finished_at = now(), error_code = :c, error_message = :m WHERE id = :t"),
               {"t": tid, "c": RenderInputInvalid.code, "m": str(err)[:500]})
    log.warning("섹션 %s 미리보기 입력 오류 기록(시도 %s) — %s", sid, tid, err)
    return tid


def ensure_preview_for_restore(db: Session, job_id: int, section_id: int) -> int | None:
    """N5 섹션 복원 뒤: 그 섹션의 현재 입력에 유효한 ⑨ 시도가 있으면 그대로 쓰고, 없을 때만 새로 만든다(호출자가 커밋·dispatch).
    - 현재 시도의 입력 지문이 지금 입력과 같으면 대기·실행 중·완료·**최종 실패** 어느 것이든 새로 만들지 않는다 —
      같은 입력의 실패 상태와 재시도 횟수(retry_count)를 보존한다(재시도는 셀러가 /tasks/{taskId}/retry 로).
    - 지문이 다르거나 현재 시도가 취소됐으면 입력 변경 재렌더(rerender_section 과 같은 방식 — 재시도 한도를 쓰지 않음),
      현재 시도가 없으면 첫 시도를 만든다.
    - ⑥⑦⑧ 이 끝나지 않았거나 진행 중인 하류 실행이 없으면 만들지 않는다(입력을 만들 수 없음).
    제외는 기존 시도를 취소하지 않으므로 보통은 재사용된다."""
    run = execution.active_run(db, job_id, "downstream")
    if run is None:
        return None
    run = execution.lock_task(db, run["id"])
    by = {(a["stage"], a["unit_id"]): a for a in execution.current_attempts(db, run["id"])}
    if not all(_final(by.get((st, section_id))) for st in ("inpaint", "style", "translate")):
        return None
    cur = db.execute(
        text("SELECT * FROM job_async_task WHERE parent_task_id = :r AND stage = 'preview' AND unit_id = :s AND is_current FOR UPDATE"),
        {"r": run["id"], "s": section_id},
    ).mappings().first()
    m, err = _preview_inputs(db, run["id"], section_id)
    if err is not None:
        # 적용 박스를 읽을 수 없으면 입력을 만들 수 없다 — 복원 자체는 막지 않고, 실패를 시도로 남긴다(전달 안 함 · 반환 None).
        # 같은 오류 상태의 실패 시도가 이미 현재면 새로 만들지 않는다. /preview 는 그대로 inputError RENDER_INPUT_INVALID
        if cur is None or dict(cur)["input_fingerprint"] != fingerprint(m) or cur["status"] != "failed":
            _record_input_error(db, run, section_id, m, err, supersedes=dict(cur) if cur is not None else None, origin="user")
        return None
    if cur is None:
        return execution.create_attempt(db, run=run, stage="preview", unit_id=section_id, manifest=m, target_count=len(m["blocks"]),
                                        max_retry=MAX_RETRY["preview"])
    cur = dict(cur)
    if cur["status"] in ("pending", "running", "done", "failed") and cur["input_fingerprint"] == fingerprint(m):
        return None  # 현재 입력의 유효한 시도 — 재사용
    if cur["status"] in ("pending", "running"):
        db.execute(text("UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
                        "lease_token_hash = NULL, lease_expires_at = NULL WHERE id = :t"), {"t": cur["id"]})
    return execution.create_attempt(db, run=run, stage="preview", unit_id=section_id, manifest=m, target_count=len(m["blocks"]),
                                    supersedes=cur, retry_origin="user", count_retry=False)


def rerender_section(db: Session, job_id: int, section_id: int) -> int | None:
    """N5 수정 후 재렌더: 현재 입력(revision)으로 새 ⑨ 시도를 만든다. 이전 시도는 대체돼 늦은 결과가 반영되지 않는다."""
    run = execution.active_run(db, job_id, "downstream")
    if run is None:
        return None
    run = execution.lock_task(db, run["id"])
    cur = db.execute(
        text("SELECT * FROM job_async_task WHERE parent_task_id = :r AND stage = 'preview' AND unit_id = :s AND is_current FOR UPDATE"),
        {"r": run["id"], "s": section_id},
    ).mappings().first()
    m, err = _preview_inputs(db, run["id"], section_id)
    if cur is None:
        return None
    cur = dict(cur)
    if err is not None:
        # 저장은 막지 않고 실패를 시도로 남긴다(전달 안 함 → rerenderTaskId null). 같은 오류 상태의 실패 시도가 현재면 그대로
        if not (cur["status"] == "failed" and cur["input_fingerprint"] == fingerprint(m)):
            _record_input_error(db, run, section_id, m, err, supersedes=cur, origin="user")
        return None
    if cur["status"] in ("pending", "running"):
        db.execute(text("UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
                        "lease_token_hash = NULL, lease_expires_at = NULL WHERE id = :t"), {"t": cur["id"]})
    return execution.create_attempt(db, run=run, stage="preview", unit_id=section_id, manifest=m, target_count=len(m["blocks"]),
                                    supersedes=cur, retry_origin="user", count_retry=False)


# ---------------------------------------------------------------------------------------------------------
# N4 작업 단위 복구(판정 · 생성 로직. 공개 경로는 JOB-08 `/resume` · JOB-05 `jobRetry` — routers/jobs.py · flows/status.py)
# 근거: PM 결정 2026-11-27(한도 = 가 · N4 복구 규칙) — plans/2026-11-26_send/N4-재시도계약-보완·abort·resume-20261126.md 11-27 절
# ---------------------------------------------------------------------------------------------------------
RETRY, FALLBACK, BLOCK = "retry", "fallback", "block"

# 시간 초과(PROCESSING_TIMEOUT)로 닫힌 시도의 복구 정책(PM 11-27). 횟수는 단계별 기존 누적 한도(MAX_RETRY)를 그대로 쓴다 —
# 시간 초과라는 이유로 새 횟수를 주지 않는다(한도 = 가). 일반 실패(모델 오류 등)는 지금 동작 그대로다(_classify 참고).
_POLICY: dict[str, Any] = {
    "inpaint": RETRY,  # 남은 횟수 안에서 다시 실행, 소진이면 대체(원본 배경 · 실행 실패 표시 — 시간 초과 처리가 이미 남긴 기록)
    "style": RETRY,  # 남은 횟수 안에서 다시 실행, 소진이면 대체(역할 기본값 — 실패 시도는 최종 상태로 렌더가 기본값 적용)
    # 남은 횟수 안에서 다시 실행. 소진은 지금처럼 막힘 — PM 확정 정책(11-27: 일부 섹션 실패 = 원본 + 실행 실패 배지로 N5,
    # 처리 대상 블록 전부 실패만 N4)은 후순위 구현(11번 → 10번 → 13번 뒤). 그 전까지 이 막힘이 임시 동작(출구 [중단] → N3 → 섹션 제외)
    "label": RETRY,
    "logo": RETRY,  # 라벨과 같음(로고 누적 한도는 기본값 0 — 그래서 소진 = 막힘)
    "text_check": FALLBACK,  # 규칙 검사: 검사 실패 · 결과 없음 유지로 N5(지금 동작 유지 — 이번 결정 범위 밖)
    # 번역(PM 11-27 2차 회신): 실패로 끝난 섹션은 오류 종류와 관계없이 성공분 보존 · 실패 칸 빈 칸으로 N5 진행(섹션 단위로 막지 않음).
    # 막힘은 작업 단위 — 번역 대상이 있는 섹션의 번역이 모두 끝났는데 성공 블록이 0 일 때만(_translation_outcome = "zero").
    # progress() 와 job_retry_plan 이 같은 판정을 쓴다. 이전 D9-3 「빈 N5 로 보내지 않음」의 섹션 단위 차단을 대신한다
}


class JobRetryRejected(Exception):
    """작업 단위 복구를 받을 수 없는 상태(내부 예외 — 공개 응답은 JOB-08 라우터가 409 INVALID_STATE 로 바꾼다). 아무것도 바꾸지 않았다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class JobRetryBlocked(JobRetryRejected):
    """판정에서 「재시도 불가 · 출구 없음」이 하나 이상 — 진행이 막히는 경우만(라벨 · 로고 소진 — PM 확정 정책의 후순위 구현 전 임시 동작,
    작업 전체 번역 성공 0, 라벨 · 로고의 소진 아닌 재시도 불가). 새 시도 0개. blocking = 막힌 섹션 · 단계 목록(내부 사유 — 공개 값은 public_blocking 이 바꾼다, JOB-08 409 details.blocking)."""

    def __init__(self, blocking: list[dict[str, Any]]):
        super().__init__("JOB_RETRY_BLOCKED", f"재시도할 수 없는 처리 {len(blocking)}건")
        self.blocking = blocking


class JobRetryUnsupported(JobRetryRejected):
    """판정은 났지만 진행 쪽 구현이 없는 정책 조합 — 안전장치. 현재 _POLICY 조합에서는 생기지 않는다(정책 표를 바꿀 때 막는 자리).
    아무것도 바꾸지 않았다."""


def translate_logical_successes(db: Session, run_id: int, section_id: int) -> dict[str, Any]:
    """번역 성공분을 논리 작업(같은 실행 · 같은 섹션) 단위로 센다(1-4). 작업 단위 판정(_translation_outcome)이 쓴다.
    현재 번역 시도의 고정 입력 `preserved`(앞 시도에서 채택한 성공분) 중 지금도 블록에 그대로 남은 것(revision · 문장 해시 일치,
    TranslateHandler.adopt 의 보존 대조와 같은 규칙) + 현재 시도가 채택돼 반영한 것 + 사용자가 직접 입력한 대상 블록.
    마지막 시도가 시간 초과로 닫혀 채택 기록이 없어도 앞 시도의 성공분은 0 으로 치지 않는다."""
    cur = db.execute(
        text("SELECT * FROM job_async_task WHERE parent_task_id = :r AND stage = 'translate' AND unit_id = :s AND is_current"),
        {"r": run_id, "s": section_id},
    ).mappings().first()
    if cur is None:
        return {"attemptId": None, "preserved": 0, "applied": 0, "userEdited": 0, "remaining": 0, "hasSuccess": False}
    m = json_value(cur["input_manifest"]) or {}
    rows = {r["id"]: r for r in _block_rows(db, section_id)}
    kept: set[int] = set()
    for ref in m.get("preserved") or []:
        r = rows.get(ref["block_id"])
        if r and r["revision"] == ref["revision"] and r["trans_1"] is not None and sha256_text(r["trans_1"]) == ref["sha256"]:
            kept.add(ref["block_id"])
    preserved = len(kept)
    vr = json_value(db.execute(text("SELECT verify_result FROM task_handoff WHERE task_id = :t AND state = 'adopted'"),
                               {"t": cur["id"]}).scalar()) or {}
    applied = len(vr.get("applied") or [])
    expected = set(m.get("expected") or [])
    # 사용자 직접 입력: 실패 대상 + 보존분(보존분을 N5 에서 고치면 revision 이 달라 preserved 에서 빠지므로 여기서 센다 —
    # 성공분 판정이 편집 뒤에 거짓으로 뒤집히지 않게)
    candidates = (expected | {ref["block_id"] for ref in m.get("preserved") or []}) - kept  # 한 블록은 한 번만 센다
    edited = sum(1 for b in candidates if rows.get(b) and rows[b]["block_status"] == "edited")
    remaining = sum(1 for b in expected if rows.get(b) and rows[b]["block_status"] == "machine" and rows[b]["trans_1"] is None)
    return {"attemptId": cur["id"], "preserved": preserved, "applied": applied, "userEdited": edited, "remaining": remaining,
            "hasSuccess": preserved + applied + edited > 0}


def _translate_in_scope(tr: dict[str, Any]) -> bool:
    """이 섹션의 논리 번역 작업(같은 실행 · 같은 섹션)에 번역 대상이 있는지.
    현재 시도의 target_count 는 재시도면 「이번에 다시 할 실패 블록 수」라, 0 이어도 섹션 번역 대상(고정 입력 expected)이
    있으면 대상 섹션이다 — 앞 시도의 유효 성공분을 작업 단위 집계에서 빠뜨리지 않는다(독립 검토 11-27).
    첫 시도는 expected 와 대상이 같으므로 target_count 0 = 번역 대상 없는 섹션(정상 생략)이다."""
    if (tr["target_count"] or 0) > 0:
        return True
    return bool((json_value(tr.get("input_manifest")) or {}).get("expected"))


def _translate_has_success(db: Session, run_id: int, tr: dict[str, Any]) -> bool:
    """이 섹션의 현재 번역(논리 단위)에 성공 블록이 하나라도 있는지. 대상이 있는 시도의 완료 = 대상 전부 성공, 부분 실패 = 성공분 있음,
    그 밖(실패 · 진행 중 재시도 · 이번 재시도 대상 0 으로 끝난 시도)은 앞 시도의 유효 보존분 · 채택분 · 사용자 직접 입력으로 센다
    (translate_logical_successes — 지금 블록과 revision · 해시가 맞는 보존분만, 무효가 된 과거 결과는 세지 않는다)."""
    if tr["status"] == "done" and (tr["target_count"] or 0) > 0:
        return True
    if tr["status"] == "failed" and tr["error_code"] == TRANSLATE_PARTIAL:
        return True
    return translate_logical_successes(db, run_id, tr["unit_id"])["hasSuccess"]


def _translation_may_change(retry_items: list[dict[str, Any]]) -> bool:
    """[다시 시도]로 작업의 번역 성공 여부가 아직 바뀔 수 있는지 — 번역 재시도, 또는 번역을 새로 만들 라벨 · 로고 재시도가 있을 때."""
    return any(i["stage"] in ("translate", "label", "logo") for i in retry_items)


def _translation_outcome(db: Session, run_id: int, section_ids: list[int], by: dict[tuple[str, int], dict[str, Any]]) -> str:
    """작업 단위 번역 결과(PM 11-27): "none"(번역 대상이 있는 섹션 없음) · "ok"(성공 블록 하나 이상) ·
    "zero"(번역 대상이 있는 섹션의 번역이 모두 끝났는데 성공 블록 0 — 이때만 N4) · "pending"(아직 판단 못 함).
    셀러가 제외한 섹션은 실행 범위(section_ids)에 없다. 라벨 · 로고가 실패한 섹션은 번역 시도가 없어 셈에서 빠진다(그 섹션은 따로 막힘)."""
    targets: list[dict[str, Any]] = []
    for sid in section_ids:
        tr = by.get(("translate", sid))
        if tr is None:
            lab, lg = by.get(("label", sid)), by.get(("logo", sid))
            if (lab and lab["status"] == "failed") or (lg and lg["status"] == "failed"):
                continue
            return "pending"  # 번역 시도가 아직 없다(라벨 · 로고 채택 뒤에 생김)
        if _translate_in_scope(tr):  # 이번 재시도 대상 0 이어도 섹션 번역 대상이 있으면 센다
            targets.append(tr)
    if not targets:
        return "none"
    if any(_translate_has_success(db, run_id, t) for t in targets):
        return "ok"
    return "zero" if all(_final(t) for t in targets) else "pending"


def _limit_left(att: dict[str, Any]) -> bool:
    """사용자 재시도 가능 여부 — JOB-05 items[].retryable 과 같은 누적 한도 · 차단 조건(단계 종류 조건만 뺌).
    한도 「시간 초과 추가 1회」(PM 결정 1절 나안)를 받으면 이 판정과 create_retry 의 한도 검사를 함께 바꾼다."""
    return (att["retry_count"] < att["max_retry"] and att.get("error_code") != "AI_ADAPTER_UNAVAILABLE"
            and not execution.retry_blocked(att))


def _exhausted(att: dict[str, Any]) -> bool:
    """누적 한도 소진 — 소진이 아닌 재시도 불가(연결 불가 · 렌더러 · 번들 불일치 · 기록 누락)와 구분한다(그쪽은 지금 차단 유지)."""
    return att["retry_count"] >= att["max_retry"]


def _no_retry_reason(att: dict[str, Any]) -> str:
    return "retry_limit" if _exhausted(att) else "not_retryable"


def _classify(db: Session, run: dict[str, Any], att: dict[str, Any], policy: dict[str, Any]) -> tuple[str, str]:
    """현재 실패 시도 하나 → (RETRY | FALLBACK | BLOCK, 사유). 시도를 만들지 않는다."""
    stage, timeout = att["stage"], execution.timed_out(att)
    left = _limit_left(att)
    if stage in ("label", "logo"):
        if timeout and policy[stage] == RETRY:
            return (RETRY, "timeout_retry") if left else (BLOCK, _no_retry_reason(att))
        # 지금: 최종 실패 = N4 막힘. 시간 초과가 아닌 실패는 사용자 재시도 종류(JOB-06 translate · render)가 아니라 다시 하지 않는다 —
        # 사유만 소진(retry_limit, 자동 재시도로 다 쓴 흔한 경우) / 소진 아님(not_retryable)으로 나눈다
        return BLOCK, "timeout_policy_block" if timeout else _no_retry_reason(att)
    if stage in ("inpaint", "style", "text_check"):
        if timeout and policy[stage] == RETRY and left:
            return RETRY, "timeout_retry"
        return FALLBACK, "fallback"  # 지금: 대체 결과(원본 배경 + 경고 / 역할 기본값 / 검사 실패 유지)로 진행
    if stage == "translate":
        if (att["target_count"] or 0) == 0:
            return FALLBACK, "partial_kept"  # 대상 0 — 다시 할 것이 없다
        if left:
            # 시간 초과든 일반 실패(부분 실패 포함)든 남은 횟수가 있으면 함께 다시(PM 11-27 「재시도 가능한 일반 실패도 함께」).
            # 실패 블록만 다시 — 성공분은 고정 입력 preserved 로 그대로(translate_retry_plan)
            return RETRY, "retry"
        if att["error_code"] == TRANSLATE_PARTIAL:
            return FALLBACK, "partial_kept"  # 횟수 소진 부분 실패: 성공분 보존 · 실패 칸 비움
        # 다시 할 수 없는 번역(소진 · 연결 불가 등 — 시간 초과 · 일반 실패 모두): 섹션 단위로 막지 않고 성공분 보존 · 실패 칸 빈 칸(PM 11-27).
        # 작업 전체 번역 성공 0 이면 job_retry_plan 이 작업 단위로 막는다(progress 와 같은 판정)
        return FALLBACK, "blank_kept"
    if stage == "preview":
        if left:
            return RETRY, "retry"
        # 지금: 최종 실패 = 원본 표시로 N5(기존 진입 조건 — 미리보기 실패는 원래 진행을 막지 않음).
        # 렌더러 · 번들 불일치 · 기록 누락(소진 아님)은 progress 가 새 입력으로 다시 그린다 — 사유만 구분
        return FALLBACK, "original_shown" if _exhausted(att) else "not_retryable_rerender"
    return BLOCK, "unknown_stage"


def job_retry_plan(db: Session, job_id: int, *, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """N4 작업 단위 재시도 판정만(시도 생성 · 상태 변경 없음, 1-2 ②). retry_job(JOB-08) · JOB-05 jobRetry 가 같이 쓴다.
    현재 하류 실행의 현재 시도 중 failed 인 것을 단계별로 RETRY / FALLBACK / BLOCK 으로 나눈다.
    available = 작업이 failed · N4 이고 · 진행 중 하류 실행이 있고 · BLOCK 이 없고 · 미구현 정책 조합이 없을 때."""
    pol = _POLICY if policy is None else policy
    job = db.execute(text("SELECT id, status, current_step FROM job WHERE id = :j"), {"j": job_id}).mappings().first()
    out: dict[str, Any] = {"jobId": job_id, "available": False, "state": None, "runId": None,
                           "retry": [], "fallback": [], "blocking": [], "unsupported": []}
    if job is None:
        out["state"] = "not_found"
        return out
    if (job["status"], job["current_step"]) != ("failed", "N4"):
        out["state"] = "invalid_state"
        return out
    run = execution.active_run(db, job_id, "downstream")
    if run is None:
        out["state"] = "no_active_run"
        return out
    out["state"], out["runId"] = "ok", run["id"]
    atts = execution.current_attempts(db, run["id"])
    targets: dict[int, int] = {}
    for a in atts:
        if a["status"] != "failed":
            continue
        decision, reason = _classify(db, run, a, pol)
        item = {"taskId": a["id"], "stage": a["stage"], "sectionId": a["unit_id"], "errorCode": a["error_code"], "reason": reason,
                "retryCount": a["retry_count"], "maxRetry": a["max_retry"]}
        targets[a["id"]] = a["target_count"] or 0
        out[{RETRY: "retry", FALLBACK: "fallback", BLOCK: "blocking"}[decision]].append(item)
    # 작업 단위 번역 판정(PM 11-27, progress 와 같은 기준): 다시 실행할 번역이 없고 번역 대상이 있는 섹션 전부 성공 0 이면,
    # 빈 칸으로 넘길 번역을 막힘으로 옮긴다(작업 전체가 번역 없는 N5 가 되지 않게). 번역 결과가 아직 바뀔 수 있으면 기다린다 —
    # 다시 실행할 번역이 있거나, 다시 실행할 라벨 · 로고가 있을 때(그 섹션은 채택 뒤 번역이 새로 생긴다 — progress 도 "pending")
    if not _translation_may_change(out["retry"]):
        by = {(a["stage"], a["unit_id"]): a for a in atts}
        if _translation_outcome(db, run["id"], json_value(run["run_scope"])["section_ids"], by) == "zero":
            moved = [i for i in out["fallback"] if i["stage"] == "translate" and targets.get(i["taskId"], 0) > 0]
            out["fallback"] = [i for i in out["fallback"] if i not in moved]
            out["blocking"] += [{**i, "reason": "job_translation_zero"} for i in moved]
    out["available"] = not out["blocking"] and not out["unsupported"]
    return out


# 공개 응답의 차단 항목(JOB-08 409 details.blocking · JOB-05 jobRetry.blocking). 필드 3개(sectionId · stage · reason)는 FE 11-27 요청 그대로.
# 아래 값 목록과 작업 전체 차단의 표현은 FE 11-28 합의(README 7.8 ⑤ 「응답 세부」 · openapi JobRetryBlockingItem enum).
# 내부 차단 사유(_classify · job_retry_plan 이 내는 BLOCK 사유 전부)와 공개 값의 대응표. 대응이 없는 사유를 조용히 다른 값으로 바꾸지 않는다.
BLOCK_REASONS = frozenset({"retry_limit", "not_retryable", "timeout_policy_block", "unknown_stage", "job_translation_zero"})
_PUBLIC_BLOCK_REASON = {
    "retry_limit": "retry_limit_exceeded",         # 누적 한도 소진(라벨 · 로고 — 자동 재시도로 다 쓴 흔한 경우 포함)
    "not_retryable": "not_retryable",               # 한도는 남았지만 다시 할 수 없음(라벨 · 로고의 일반 실패 — 사용자 재시도 종류 아님)
    "job_translation_zero": "job_translation_zero",  # 작업 전체 번역 성공 0 — 섹션 하나의 문제가 아님(sectionId = null 항목 하나)
}
# 공개 대응을 두지 않는 사유(지금 정책에서 생기지 않음 — 생기면 정책 표를 바꾼 것이므로 공개 값부터 정해야 한다):
# timeout_policy_block(_POLICY 의 라벨 · 로고가 RETRY 가 아닐 때만) · unknown_stage(등록 단계 밖)
_UNPUBLISHED_BLOCK_REASONS = frozenset({"timeout_policy_block", "unknown_stage"})
# 차단 단계: 기존 TaskType 을 바꾸지 않고 차단 항목 전용 값(내부 단계 이름 그대로). 지금 차단이 생길 수 있는 단계만.
_PUBLIC_BLOCK_STAGES = frozenset({"label", "logo", "translate"})


class UnmappedBlockReason(RuntimeError):
    """공개 대응이 없는 차단 사유 · 단계 — 정책 표를 바꿨는데 공개 계약을 정하지 않은 경우(프로그램 오류로 드러낸다)."""


def public_blocking(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        reason = _PUBLIC_BLOCK_REASON.get(it["reason"])
        if reason is None or it["stage"] not in _PUBLIC_BLOCK_STAGES:
            raise UnmappedBlockReason(f"공개 대응 없는 차단: stage={it['stage']} reason={it['reason']}")
        if reason == "job_translation_zero":
            row = {"sectionId": None, "stage": it["stage"], "reason": reason}  # 작업 전체 차단
        else:
            row = {"sectionId": it["sectionId"], "stage": it["stage"], "reason": reason}  # 그 섹션의 그 처리가 막힘
        if row not in out:
            out.append(row)
    return out


def retry_job(db: Session, job_id: int) -> dict[str, Any]:
    """N4 작업 단위 복구(원자적). 호출자가 소유 확인을 마친 뒤 부른다(JOB-08 `/resume` 라우터).
    작업 행 잠금 → 상태 재확인 → 전부 판정 → BLOCK 이 하나라도 있으면 아무것도 만들지 않고 JobRetryBlocked.
    아니면 한 트랜잭션에서: 재시도 시도 생성(누적 한도 · 고정 입력 규칙은 JOB-06 과 같음) · 대체로 닫을 것 기록 ·
    작업 processing · N4(30분 새로 셈) · 집계(대체로 끝난 섹션의 미리보기 등) → 커밋 → 큐 전송.
    중복 · 동시 요청: 잠금 아래 상태가 이미 processing 이라 JobRetryRejected(INVALID_STATE) — 새 시도는 한 벌만."""
    from app.flows import retry_plan

    try:
        execution.lock_job(db, job_id)
        plan = job_retry_plan(db, job_id)
        if plan["state"] != "ok":
            raise JobRetryRejected("INVALID_STATE", f"작업 단위 재시도는 failed · N4 의 진행 중 하류 실행에서만({plan['state']})")
        if plan["blocking"]:
            raise JobRetryBlocked(plan["blocking"])
        if plan["unsupported"]:
            raise JobRetryUnsupported("POLICY_NOT_WIRED", "번역 부분 진행 정책은 progress() 판정 변경과 함께만 켠다")
        run = execution.lock_task(db, plan["runId"])
        ids: list[int] = []
        for it in plan["retry"]:
            att = execution.lock_task(db, it["taskId"])
            ids.append(execution.create_retry(db, att["id"], "user", **retry_plan(db, att)))
        inp = [it["sectionId"] for it in plan["fallback"] if it["stage"] == "inpaint"]
        if inp:  # 인페인트 대체 기록(원본 배경 · 경고) — 실패 · 시간 초과 처리에서 이미 남겼으면 같은 값(멱등)
            db.execute(text("UPDATE section SET inpaint_status = 'failed', warning_badge = 'processing_failed', updated_at = now() "
                            "WHERE id = ANY(:s) AND job_id = :j"), {"s": inp, "j": job_id})
        set_job_state(db, job_id, status="processing", step="N4", ufs="translating")
        follow = progress(db, run["id"])  # 대체로 끝난 섹션의 다음 단계 — 오류 동안 만들지 않았던 미리보기 등. 모두 끝났으면 여기서 N5
        job = db.execute(text("SELECT status, current_step FROM job WHERE id = :j"), {"j": job_id}).mappings().one()
        db.commit()
    except Exception:
        db.rollback()
        raise
    execution.dispatch(ids + follow)
    return {"jobId": job_id, "status": job["status"], "currentStep": job["current_step"], "retriedTaskIds": ids,
            "createdTaskIds": follow,
            "fallbackClosed": [{"sectionId": it["sectionId"], "stage": it["stage"]} for it in plan["fallback"]]}


for _stage, _h in (("label", LabelHandler()), ("logo", LogoHandler()), ("style", StyleHandler()), ("translate", TranslateHandler()),
                   ("inpaint", InpaintHandler()), ("preview", PreviewHandler())):
    execution.register_handler(_stage, _h)
