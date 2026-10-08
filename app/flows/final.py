"""N5 확정 → N6 최종 렌더 → 저장. (5.27·D9-3)

- 확정(CFM-04): 서버의 미해결 경고 집합과 acknowledgedWarnings(blockId+code)가 같아야 한다. 빈 번역을 완료로 바꾸지 않는다.
  확정하면 하류 실행을 닫고(진행 중 재렌더 취소) 최종 렌더 실행(run_kind=final_render)과 job 단위 시도를 만든다.
- 최종 렌더는 미리보기를 재사용하지 않고 현재 번역문·배치·스타일·배경으로 다시 그린다. 원본(소스 이미지)별로 포함 섹션을
  세로로 쌓아 deliverable 1건, deliverable_section 에 위치(stack_offset)·순서(order_no)를 남긴다(contract 3.3).
  모든 섹션이 제외된 원본은 산출물에서 뺀다.
- 저장(FIN-06)은 현재 최종 렌더 시도가 성공했을 때만(D9-3). 실패면 409 와 재시도(JOB-06·FIN-01).
"""
from __future__ import annotations

import io
import json
import logging
from typing import Any

from PIL import Image
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import artifacts, execution, typeset
from app import db as app_db
from app.execution import AdoptContext, AdoptionRejected, AdoptOutcome, ArtifactSpec, Envelope
from app.flows import common
from app.flows.common import json_value, report, set_job_state, submit
from app.manifest import sha256_text, stable_hash

log = logging.getLogger(__name__)
FINAL_MAX_RETRY = 2  # 개발 기본값


class ConfirmRejected(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


# ---------------------------------------------------------------------------------------------------------
# 경고(signals) — 현재 입력에 유효한 원천에서 파생(5.15). 이름 치환만으로 정할 수 없는 코드는 만들지 않는다
# ---------------------------------------------------------------------------------------------------------
def block_signals(db: Session, job_id: int) -> dict[int, list[dict[str, Any]]]:
    """블록별 신호: translation_failed(현재 ⑧ 시도의 실패 대상, taskId·retryable 동봉), width_overflow(최신 렌더 초과)."""
    from app.flows.status import retryable

    out: dict[int, list[dict[str, Any]]] = {}
    run = execution.latest_run(db, job_id, "downstream")
    if run is None:
        return out
    rows = db.execute(
        text(
            "SELECT a.*, h.verify_result FROM job_async_task a LEFT JOIN task_handoff h ON h.task_id = a.id AND h.state = 'adopted' "
            "WHERE a.parent_task_id = :r AND a.stage = 'translate' AND a.is_current"
        ),
        {"r": run["id"]},
    ).mappings().all()
    for a in rows:
        if a["status"] != "failed":
            continue
        vr = json_value(a["verify_result"]) or {}
        failed = vr.get("failed")
        if failed is None:  # 구조 실패·호출 실패 — 대상 전체가 번역되지 않았다
            failed = [t["block_id"] for t in json_value(a["input_manifest"])["targets"]]
        current = {r[0] for r in db.execute(
            text("SELECT id FROM text_block WHERE id = ANY(:ids) AND trans_1 IS NULL AND block_status = 'machine'"), {"ids": failed}
        ).all()}
        for b in current:
            out.setdefault(b, []).append({"code": "translation_failed", "reason": a["error_code"], "basisArticle": None,
                                          "evidenceUrl": None, "taskId": a["id"], "retryable": retryable(dict(a), dict(run))})
    for r in db.execute(
        text(
            "SELECT tb.id FROM text_block tb JOIN section s ON s.id = tb.section_id JOIN job j ON j.id = s.job_id "
            "WHERE s.job_id = :j AND tb.overflow AND " + common.VISIBLE
        ),
        {"j": job_id},
    ).all():
        out.setdefault(r[0], []).append({"code": "width_overflow", "reason": None, "basisArticle": None, "evidenceUrl": None,
                                         "taskId": None, "retryable": None})
    return out


def unresolved_warnings(db: Session, job_id: int) -> list[dict[str, Any]]:
    """확정 전에 확인받아야 하는 블록 경고: 포함 섹션의 번역하지 못한 칸(D9-3 빈 번역)."""
    sig = block_signals(db, job_id)
    inc = {r[0] for r in db.execute(
        text("SELECT tb.id FROM text_block tb JOIN section s ON s.id = tb.section_id JOIN job j ON j.id = s.job_id "
             "WHERE s.job_id = :j AND s.bucket = 'include' AND " + common.VISIBLE), {"j": job_id}).all()}
    return sorted(({"blockId": b, "code": s["code"]} for b, ss in sig.items() if b in inc for s in ss
                   if s["code"] == "translation_failed"), key=lambda x: (x["blockId"], x["code"]))


# ---------------------------------------------------------------------------------------------------------
# 확정
# ---------------------------------------------------------------------------------------------------------
def confirm(db: Session, job_id: int, seller_id: int, acknowledged: list[dict[str, Any]]) -> dict[str, Any]:
    if not db.execute(text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"), {"j": job_id, "s": seller_id}).first():
        raise ConfirmRejected(404, "JOB_NOT_FOUND", "job not found")
    job = execution.lock_job(db, job_id)
    if job["current_step"] != "N5" or job["status"] != "review":
        raise ConfirmRejected(409, "INVALID_STATE", f"확정은 N5 에서만({job['current_step']}/{job['status']})")
    if not common.visible_sections(db, job_id, include_only=True):
        raise ConfirmRejected(409, "ALL_SECTIONS_EXCLUDED", "all sections are excluded")
    run = execution.active_run(db, job_id, "downstream")
    if run is not None:
        busy = db.execute(
            text("SELECT count(*) FROM job_async_task WHERE parent_task_id = :r AND is_current AND status IN ('pending', 'running') "
                 "AND stage <> 'preview'"), {"r": run["id"]}
        ).scalar()
        if busy:
            raise ConfirmRejected(409, "INVALID_STATE", "진행 중인 번역 재시도가 있다")
    want = unresolved_warnings(db, job_id)
    got = sorted(({"blockId": int(a["blockId"]), "code": a["code"]} for a in acknowledged), key=lambda x: (x["blockId"], x["code"]))
    if want != got:
        raise ConfirmRejected(409, "INVALID_STATE", "확인한 경고가 현재 미해결 경고와 다르다", {"warnings": want})
    if run is not None:
        # 하류 실행 종료: 진행 중 재렌더는 취소, 실행은 done(N5 결과 확정)
        db.execute(
            text("UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
                 "lease_token_hash = NULL, lease_expires_at = NULL WHERE parent_task_id = :r AND status IN ('pending', 'running')"),
            {"r": run["id"]},
        )
        execution.finish_run(db, run["id"], "done")
    attempt_id = start_final_render(db, job_id)
    set_job_state(db, job_id, status="review", step="N6", ufs="reviewing")
    db.commit()
    execution.dispatch([attempt_id])
    return {"jobId": job_id, "status": "review", "currentStep": "N6", "renderTaskId": attempt_id}


# ---------------------------------------------------------------------------------------------------------
# 최종 렌더
# ---------------------------------------------------------------------------------------------------------
def _final_inputs(db: Session, job_id: int) -> dict[str, Any]:
    from app.flows.downstream import _render_inputs

    run = execution.latest_run(db, job_id, "downstream")
    secs = common.visible_sections(db, job_id, include_only=True)
    sections = []
    for s in secs:
        m = _render_inputs(db, run["id"], s["id"]) if run else None
        if m is None:
            raise AdoptionRejected("하류 실행 없음", code="INPUT_MISSING")
        sections.append({"section_id": s["id"], "source_image_id": s["source_image_id"], "section_order": s["section_order"],
                         "height": s["height"], "background": m["background"], "blocks": m["blocks"]})
    defaults = typeset.load_role_defaults()
    return {"stage": "final_render", "job_id": job_id, "sections": sections, "renderer": typeset.RENDERER_VERSION,
            "style_defaults": {"version": defaults.version, "sha256": defaults.sha256} if defaults else None}


def start_final_render(db: Session, job_id: int) -> int:
    """최종 렌더 실행·시도 생성(호출자가 job 잠금·커밋·dispatch). 진행 중이면 그 시도를 돌려준다."""
    active = execution.active_run(db, job_id, "final_render")
    if active is not None:
        cur = db.execute(text("SELECT * FROM job_async_task WHERE parent_task_id = :r AND is_current"), {"r": active["id"]}).mappings().one()
        if cur["status"] == "failed":
            return execution.create_retry(db, cur["id"], "user")
        return cur["id"]
    m = _final_inputs(db, job_id)
    run_id = execution.create_run(db, job_id, "final_render", {"job_id": job_id, "section_ids": [s["section_id"] for s in m["sections"]]})
    run = execution.lock_task(db, run_id)
    return execution.create_attempt(db, run=run, stage="final_render", unit_id=job_id, manifest=m,
                                    target_count=len(m["sections"]), max_retry=FINAL_MAX_RETRY)


def execute_final_render(attempt_id: int) -> dict[str, Any]:
    from app.flows.downstream import _block_rows, _render_section

    lease = execution.acquire(attempt_id, execution.worker_identity())
    if lease is None:
        return {"attemptId": attempt_id, "ran": False}
    m = lease.manifest
    try:
        files: dict[tuple[str, str], bytes] = {}
        renders: dict[str, Any] = {}
        db = app_db.SessionLocal()
        try:
            rows = {}
            for s in m["sections"]:
                rows.update({r["id"]: r for r in _block_rows(db, s["section_id"])})
        finally:
            db.close()
        for s in m["sections"]:
            texts, styles = {}, {}
            for b in s["blocks"]:
                r = rows.get(b["block_id"])
                if (r is None or r["revision"] != b["revision"] or r["trans_1"] is None or sha256_text(r["trans_1"]) != b["trans_sha256"]
                        or stable_hash(json_value(r["style"])) != b["style_sha256"]):
                    raise AdoptionRejected("최종 렌더 입력이 확정 당시와 다르다", code="INPUT_CHANGED")
                texts[b["block_id"]], styles[b["block_id"]] = r["trans_1"], json_value(r["style"])
            res = _render_section({**s, "style_defaults": m["style_defaults"]}, texts, styles)
            files[("render_image", f"sec_{s['section_id']}")] = res.png
            renders[str(s["section_id"])] = {"width": res.width, "height": res.height,
                                             "overflow": {str(k): v.overflow for k, v in res.blocks.items()},
                                             "applied": {str(k): {"values": v.applied, "source": v.source} for k, v in res.blocks.items()}}
        # 원본별로 포함 섹션을 세로로 쌓은 산출물 이미지(contract 3.3). 파일 처리는 채택 트랜잭션 밖(워커)에서 한다
        stacks: dict[str, Any] = {}
        by_src: dict[int, list[dict[str, Any]]] = {}
        for s in sorted(m["sections"], key=lambda s: (s["source_image_id"], s["section_order"])):
            by_src.setdefault(s["source_image_id"], []).append(s)
        for src, secs in by_src.items():
            imgs = [Image.open(io.BytesIO(files[("render_image", f"sec_{s['section_id']}")])).convert("RGB") for s in secs]
            canvas = Image.new("RGB", (max(i.width for i in imgs), sum(i.height for i in imgs)), "white")
            y, offsets = 0, []
            for i in imgs:
                canvas.paste(i, (0, y))
                offsets.append(y)
                y += i.height
            buf = io.BytesIO()
            canvas.save(buf, format="PNG")
            files[("render_image", f"src_{src}")] = buf.getvalue()
            stacks[str(src)] = {"sections": [s["section_id"] for s in secs], "offsets": offsets,
                                "width": canvas.width, "height": canvas.height}
        env = Envelope(outcome="done", target_count=len(m["sections"]), input_fingerprint=lease.fingerprint,
                       impl_version=typeset.RENDERER_VERSION, payload={"renders": renders, "stacks": stacks},
                       artifacts=[ArtifactSpec(kind=k, part_key=p, data=d) for (k, p), d in files.items()])
        return report(attempt_id, submit(lease, env, files))
    except typeset.StyleDefaultsUnavailable as e:
        return report(attempt_id, submit(lease, common.failed_envelope(lease, e.code, str(e), False, len(m["sections"]))))
    except AdoptionRejected as e:
        return report(attempt_id, submit(lease, common.failed_envelope(lease, e.code, str(e), e.retryable, len(m["sections"]))))
    except execution.LeaseLost:
        return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
    except Exception as e:  # noqa: BLE001
        log.exception("final render 실패 attempt=%s", attempt_id)
        execution.fail_attempt(lease, "RENDER_UNEXPECTED", f"{e.__class__.__name__}: {e}", retryable=False)
        return {"attemptId": attempt_id, "ran": True, "failed": True}


def _spec(db: Session, job_id: int) -> dict[str, Any]:
    row = db.execute(
        text("SELECT ms.char_limit FROM job j LEFT JOIN module_spec ms ON ms.channel_spec_id = j.channel_spec_id "
             "WHERE j.id = :j ORDER BY ms.id LIMIT 1"),
        {"j": job_id},
    ).mappings().first()
    return {"charLimit": row["char_limit"] if row else None}


class FinalRenderHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        from app import render as legacy_render

        h = ctx.handoff
        p = json_value(h["payload"])
        m = ctx.attempt["input_manifest"]
        job_id = ctx.job["id"]
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=p.get("error_code", "RENDER_FAILED"), error_message=p.get("message"),
                                retryable=bool(p.get("retryable")))
        arts = {a["part_key"]: a for a in ctx.artifacts if a["kind"] == "render_image" and a["state"] == "verified"}
        missing = [s["section_id"] for s in m["sections"] if f"sec_{s['section_id']}" not in arts]
        if missing:
            raise AdoptionRejected(f"섹션 렌더 누락 {missing}", code="RENDER_RESULT_INVALID")
        for s in m["sections"]:
            for b in s["blocks"]:
                if db.execute(text("SELECT revision FROM text_block WHERE id = :b"), {"b": b["block_id"]}).scalar() != b["revision"]:
                    raise AdoptionRejected("확정 이후 블록이 바뀌었다", code="RENDER_STALE")
        stacks = p.get("stacks") or {}
        want_src = {str(s["source_image_id"]) for s in m["sections"]}
        if set(stacks) != want_src or any(f"src_{k}" not in arts for k in stacks):
            raise AdoptionRejected("원본별 산출물 이미지 누락", code="RENDER_RESULT_INVALID")
        old = [r[0] for r in db.execute(text("SELECT id FROM deliverable WHERE job_id = :j"), {"j": job_id}).all()]
        if old:
            db.execute(text("DELETE FROM deliverable WHERE id = ANY(:ids)"), {"ids": old})
        spec = _spec(db, job_id)
        made = []
        for src, st in sorted(stacks.items(), key=lambda kv: int(kv[0])):
            art = arts[f"src_{src}"]
            if (art["width"], art["height"]) != (st["width"], st["height"]):
                raise AdoptionRejected(f"원본 {src} 산출물 크기 불일치", code="RENDER_RESULT_INVALID")
            blocks = [dict(r) for r in db.execute(
                text("SELECT id, role, trans_1, is_excluded, char_limit FROM text_block WHERE section_id = ANY(:s)"),
                {"s": st["sections"]}).mappings().all()]
            vr = legacy_render.validate_deliverable(blocks, st["width"], st["height"], {"maxWidth": st["width"], "charLimit": spec["charLimit"]})
            vr["overflow"] = {str(sid): p["renders"][str(sid)]["overflow"] for sid in st["sections"]}
            did = db.execute(
                text("INSERT INTO deliverable (job_id, source_image_id, usage_type, image_url, format, color_space, file_size, "
                     "render_status, validation_result) VALUES (:j, :src, 'detail', :k, 'png', 'sRGB', :n, 'done', CAST(:vr AS jsonb)) "
                     "RETURNING id"),
                {"j": job_id, "src": int(src), "k": artifacts.adopted_key(art), "n": art["byte_size"], "vr": json.dumps(vr)},
            ).scalar_one()
            for order, (sid, off) in enumerate(zip(st["sections"], st["offsets"]), start=1):
                db.execute(text("INSERT INTO deliverable_section (deliverable_id, section_id, stack_offset, order_no) "
                                "VALUES (:d, :s, :o, :n)"), {"d": did, "s": sid, "o": off, "n": order})
                db.execute(text("UPDATE section SET render_image_key = :k, updated_at = now() WHERE id = :s"),
                           {"k": artifacts.adopted_key(arts[f"sec_{sid}"]), "s": sid})
                for bid, ov in p["renders"][str(sid)]["overflow"].items():
                    db.execute(text("UPDATE text_block SET overflow = :o WHERE id = :b"), {"o": bool(ov), "b": int(bid)})
            made.append(did)
        return AdoptOutcome(status="done", verify_result={"deliverables": made})

    def after_done(self, db, att):
        execution.finish_run(db, att["parent_task_id"], "done")
        return []

    def after_failure(self, db, att, code, retryable):
        if retryable and att["retry_count"] < min(1, att["max_retry"]):
            return [execution.create_retry(db, att["id"], "auto")]
        return []  # 실행은 running 으로 두어 JOB-06·FIN-01 재시도가 가능하다. 저장은 막힌다


def can_save(db: Session, job_id: int) -> bool:
    run = execution.latest_run(db, job_id, "final_render")
    if run is None:
        return False
    cur = db.execute(text("SELECT status FROM job_async_task WHERE parent_task_id = :r AND is_current"), {"r": run["id"]}).scalar()
    return cur == "done"


execution.register_handler("final_render", FinalRenderHandler())
