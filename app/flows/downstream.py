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
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ai_adapters, artifacts, execution, typeset
from app import db as app_db
from app.execution import AdoptContext, AdoptionRejected, AdoptOutcome, ArtifactSpec, Envelope
from app.flows import common
from app.flows.common import block_key, failed_envelope, json_value, report, section_key, set_job_state, submit
from app.dictionary_bundle import entries_by_ref
from app.manifest import fingerprint, sha256_bytes, sha256_text, stable_hash
from app.storage import ObjectMissing, get_store

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
            "UPDATE text_block SET revision = revision + CASE WHEN trans_1 IS NOT NULL OR auto_adjust IS NOT NULL THEN 1 ELSE 0 END, "
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
    if job["status"] == "archived" or job["current_step"] != "N3" or job["status"] != "review":
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
        from pipeline.types import StyleResult

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
        res = StyleResult.model_validate(p["style"])
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


def _glossary_supply(db: Session, lang: str) -> dict[str, Any]:
    """용어집 공급 — 대상 언어 전체(2026-10-08 사용자 결정, #11 검색 방식 확정 전 임시). 버전은 내용 지문."""
    rows = db.execute(text("SELECT id, term_ko, term_target, enforcement FROM glossary WHERE target_lang = :l ORDER BY id"),
                      {"l": lang}).mappings().all()
    terms = [{"glossary_id": str(r["id"]), "term_ko": r["term_ko"], "term_target": r["term_target"], "enforcement": r["enforcement"]}
             for r in rows]
    return {"status": "ok", "version": "db-sha256:" + fingerprint(terms)[:16], "reason": None, "terms": terms}


def _translate_instructions(db: Session, sid: int, target_ids: set[int], bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """표현 지시: N3 채택 판정 중 대체 표현이 있는 금지형(regulated) 규제 판정의 매칭을 이번 대상 블록에 한해 넘긴다.
    대체 표현 본문은 AI가 고정 묶음에서 꺼낸다. 템플릿 대체 표현(RG-021/022 등)은 원문 수치 유지라 넘기지 않는다(D9-2)."""
    det = json_value(db.execute(
        text("SELECT detail FROM audit_log WHERE action_type = :a AND target_type = 'section' AND target_id = :s ORDER BY id DESC LIMIT 1"),
        {"a": common.ACT_ANALYSIS_ADOPTED, "s": sid},
    ).scalar()) or {}
    refs = entries_by_ref(bundle)
    out: list[dict[str, Any]] = []
    for v in det.get("verdicts", []):
        e = refs.get(v["dictionary_ref"])
        alt = (e or {}).get("alternative_expression")
        if e is None or e["dict_type"] != "regulatory" or v["verdict_status"] != "regulated" or not alt or not alt.strip():
            continue
        if _PLACEHOLDER.search(alt):
            continue
        for m in det.get("matches", []):
            if m["snapshot_key"] == v["dictionary_ref"] and m["finding_key"] == v["finding_key"] and m["block_id"] in target_ids:
                item = {"block_key": block_key(m["block_id"]), "external_id": v["dictionary_ref"], "matched_text": m["matched_text"]}
                if item not in out:
                    out.append(item)
    return out


def _translate_supply(db: Session, run_id: int, sid: int, targets: list[dict[str, Any]]) -> dict[str, Any]:
    scope = _run_scope(db, run_id)
    hid, bundle = _analysis_bundle(db, scope["analysis_task_id"])
    glossary = _glossary_supply(db, scope["target_lang"])
    instructions = _translate_instructions(db, sid, {r["id"] for r in targets}, bundle)
    return {"bundle_handoff_id": hid, "bundle": bundle, "glossary": glossary, "instructions": instructions}


def _supply_ref(supply: dict[str, Any]) -> dict[str, Any]:
    """시도 명세에 고정할 공급 지문. 실행 때 다시 만든 공급과 대조한다(5.32 입력 고정)."""
    return {"bundle_handoff_id": supply["bundle_handoff_id"], "bundle_sha256": supply["bundle"]["sha256"],
            "glossary_sha256": fingerprint(supply["glossary"]), "instructions": supply["instructions"]}


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
            supply = _translate_supply(db, lease.run_id, sec["id"], [r for r in rows if r["id"] in target_ids])
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
        return progress(db, att["parent_task_id"], att["unit_id"])

    def after_failure(self, db, att, code, retryable):
        if code == TRANSLATE_PARTIAL:
            return progress(db, att["parent_task_id"], att["unit_id"])  # 성공분 보존·실패 칸 비움으로 N5 진행(D9-3)
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
def _render_inputs(db: Session, run_id: int, sid: int) -> dict[str, Any]:
    sec = _section_row(db, sid)
    rows = _block_rows(db, sid)
    inp = db.execute(
        text("SELECT status FROM job_async_task WHERE parent_task_id = :r AND stage = 'inpaint' AND unit_id = :s AND is_current"),
        {"r": run_id, "s": sid},
    ).scalar()
    bg_key = sec["inpaint_image_url"] if inp == "done" and sec["inpaint_image_url"] else sec["image_key"]
    blocks = []
    for r in rows:
        if r["is_excluded"] is not False or not (r["source_ko"] or "").strip() or r["trans_1"] is None:
            continue  # 라벨·로고·빈 원문·번역 없음(실패 칸)은 그리지 않는다
        adj = json_value(r["auto_adjust"]) or {}
        box = adj.get("layout_bbox") or json_value(r["bbox"])
        blocks.append({"block_id": r["id"], "revision": r["revision"], "role": r["role"], "trans_sha256": sha256_text(r["trans_1"]),
                       "box": {k: int(box[k]) for k in ("x", "y", "w", "h")}, "style_sha256": stable_hash(json_value(r["style"]))})
    bg_sha = db.execute(text("SELECT sha256 FROM task_artifact WHERE verified_key = :k OR source_ref->>'key' = :k LIMIT 1"),
                        {"k": bg_key}).scalar()
    defaults = typeset.load_role_defaults()
    return {"stage": "preview", "section_id": sid, "background": {"key": bg_key, "sha256": bg_sha,
                                                                    "source": "inpaint" if bg_key != sec["image_key"] else "original"},
            "blocks": blocks, "renderer": typeset.RENDERER_VERSION,
            "style_defaults": {"version": defaults.version, "sha256": defaults.sha256} if defaults else None}


def _render_section(m: dict[str, Any], texts: dict[int, str], styles: dict[int, Any]) -> typeset.RenderResult:
    data = get_store().get(m["background"]["key"])
    if m["background"]["sha256"] and sha256_bytes(data) != m["background"]["sha256"]:
        raise AdoptionRejected("배경이 고정 입력과 다르다", code="INPUT_CHANGED")
    defaults = typeset.load_role_defaults()
    if (defaults.sha256 if defaults else None) != (m["style_defaults"] or {}).get("sha256"):
        raise AdoptionRejected("스타일 기본값이 고정 입력과 다르다", code="INPUT_CHANGED")
    rbs = [typeset.RenderBlock(id=b["block_id"], role=b["role"], text=texts[b["block_id"]], box=b["box"], style=styles[b["block_id"]])
           for b in m["blocks"]]
    return typeset.render(data, rbs, defaults)


def execute_preview(attempt_id: int) -> dict[str, Any]:
    def work(lease):
        m = lease.manifest
        db = app_db.SessionLocal()
        try:
            rows = {r["id"]: r for r in _block_rows(db, m["section_id"])}
        finally:
            db.close()
        texts, styles = {}, {}
        for b in m["blocks"]:
            r = rows.get(b["block_id"])
            if r is None or r["revision"] != b["revision"] or r["trans_1"] is None or sha256_text(r["trans_1"]) != b["trans_sha256"]:
                raise AdoptionRejected("블록 번역문·revision 이 고정 입력과 다르다(옛 렌더)", code="INPUT_CHANGED")
            if stable_hash(json_value(r["style"])) != b["style_sha256"]:
                raise AdoptionRejected("블록 스타일이 고정 입력과 다르다", code="INPUT_CHANGED")
            texts[b["block_id"]], styles[b["block_id"]] = r["trans_1"], json_value(r["style"])
        try:
            res = _render_section(m, texts, styles)
        except typeset.StyleDefaultsUnavailable as e:
            return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), False, len(m["blocks"]))))
        env = Envelope(outcome="done", target_count=len(m["blocks"]), input_fingerprint=lease.fingerprint,
                       impl_version=typeset.RENDERER_VERSION,
                       payload={"render": {"width": res.width, "height": res.height,
                                           "blocks": {str(k): {"overflow": v.overflow, "applied": v.applied, "source": v.source,
                                                               "lines": v.lines} for k, v in res.blocks.items()}}},
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
    scope = json_value(run["run_scope"])
    atts = execution.current_attempts(db, run_id)
    by = {(a["stage"], a["unit_id"]): a for a in atts}
    ids: list[int] = []
    blocking = False
    all_ready = True
    for sid in scope["section_ids"]:
        lab, lg = by.get(("label", sid)), by.get(("logo", sid))
        inp, sty, tr, pv = by.get(("inpaint", sid)), by.get(("style", sid)), by.get(("translate", sid)), by.get(("preview", sid))
        if (lab and lab["status"] == "failed") or (lg and lg["status"] == "failed"):
            blocking = True
            all_ready = False
            continue
        if tr and tr["status"] == "failed" and tr["error_code"] != TRANSLATE_PARTIAL and (tr["target_count"] or 0) > 0:
            # 번역 대상이 있는데 성공분 없이 끝남 — 오류 코드와 무관하게 빈 N5 로 보내지 않는다(D9-3).
            # 부분 실패(TRANSLATE_PARTIAL)만 성공분 보존·실패 칸 비움으로 진행한다. 대상 0개 정상 생략은 failed 가 아니다
            blocking = True
            all_ready = False
            continue
        if not (_final(inp) and _final(sty) and _final(tr)):
            all_ready = False
            continue
        if pv is None:
            m = _render_inputs(db, run_id, sid)
            ids.append(execution.create_attempt(db, run=run, stage="preview", unit_id=sid, manifest=m, target_count=len(m["blocks"]),
                                                max_retry=MAX_RETRY["preview"]))
            all_ready = False
            continue
        if not _final(pv):
            all_ready = False
            continue
        # 미리보기 이후 입력이 바뀌었으면(N5 번역 재시도 성공 등) 현재 입력으로 다시 그린다. 재시도 한도는 쓰지 않는다
        m = _render_inputs(db, run_id, sid)
        if fingerprint(m) != pv["input_fingerprint"] and pv["status"] == "done":
            pv_row = execution.lock_task(db, pv["id"])
            ids.append(execution.create_attempt(db, run=run, stage="preview", unit_id=sid, manifest=m, target_count=len(m["blocks"]),
                                                supersedes=pv_row, retry_origin="auto", count_retry=False))
    job = db.execute(text("SELECT id, status, current_step FROM job WHERE id = :j"), {"j": run["job_id"]}).mappings().one()
    if job["current_step"] == "N4":
        if blocking and not ids and job["status"] == "processing":
            set_job_state(db, job["id"], status="failed", step="N4", ufs="failed")
        elif all_ready and not ids:
            set_job_state(db, job["id"], status="review", step="N5", ufs="reviewing")
    return ids


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
    m = _render_inputs(db, run["id"], section_id)
    if cur is None:
        return None
    cur = dict(cur)
    if cur["status"] in ("pending", "running"):
        db.execute(text("UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
                        "lease_token_hash = NULL, lease_expires_at = NULL WHERE id = :t"), {"t": cur["id"]})
    return execution.create_attempt(db, run=run, stage="preview", unit_id=section_id, manifest=m, target_count=len(m["blocks"]),
                                    supersedes=cur, retry_origin="user", count_retry=False)


for _stage, _h in (("label", LabelHandler()), ("logo", LogoHandler()), ("style", StyleHandler()), ("translate", TranslateHandler()),
                   ("inpaint", InpaintHandler()), ("preview", PreviewHandler())):
    execution.register_handler(_stage, _h)
