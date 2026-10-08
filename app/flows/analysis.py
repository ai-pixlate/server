"""N2 초기 분석 — 분석 시작 → ①②③ 실제 호출 → 검증·S3·임시 키 변환 → ③-1·③-1′ → D9-1 집계 → N3.

실행 구조: 대표(run_kind=analysis, task_type=ocr, D6) + 시도 analyze(1개) + 섹션별 judge 시도.
- 분석이 끝나기 전 새 섹션은 analysis_task_id 로 숨긴다. 완료할 때 job.current_analysis_task_id 를 바꾸고 이전 결과를
  audit 에 남긴 뒤 교체한다(한 트랜잭션, D6). 실패·중단한 실행의 숨은 섹션은 지운다(기존 채택 결과는 보존).
- D9-1: 규제 사전 조회·공통 묶음 실패 → N2 실패(자동 재시도 → 오류). 현지 AI 판정 실패 → 섹션 제외(auto_local_failed)·
  시도 failed 기록 후 N3. 현지 사전 조회 실패 → 제외 없이 현지 미검사 기록 후 N3. 그 밖의 선행 결과 실패 → 자동 재시도 2회 후 오류.
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ai_adapters, artifacts, execution
from app import db as app_db
from app.dictionary_bundle import BundleError, build_bundle, entries_by_ref, primary_evidence, verify_bundle
from app.execution import AdoptContext, AdoptionRejected, AdoptOutcome, ArtifactSpec, Envelope, Lease
from app.flows import common
from app.flows.common import (block_key, failed_envelope, json_value, report, section_key, set_job_state, submit,
                              write_audit)
from app.manifest import fingerprint, sha256_bytes, stable_hash
from app.storage import ObjectMissing, get_store

log = logging.getLogger(__name__)

N2_MAX_RETRY = 2  # PM 명시값(D9-1). 다른 단계에 일반화하지 않는다
ROLES = ("title", "body", "caption", "price", "caution")
VERDICT_TYPE = {  # OpenAPI VerdictType: verdict_status(+대체 표현 유무)에서 계산
    ("regulated", True): "regulatory_replaceable",
    ("regulated", False): "regulatory",
    ("conditional", True): "regulatory_conditional",
    ("conditional", False): "regulatory_conditional",
    ("irrelevant", True): "local_irrelevant",
    ("irrelevant", False): "local_irrelevant",
    ("needs_fix", True): "needs_fix",
    ("needs_fix", False): "needs_fix",
    ("policy", True): "channel_policy",
    ("policy", False): "channel_policy",
}
# section.exclusion_reason(사용자 결정 2026-10-08: PRD 문자열). 우선순위: 규제 > 현지 판정 실패 > 현지 제외
EXCL_REGULATORY, EXCL_LOCAL_FAILED, EXCL_LOCAL = "auto_regulatory", "auto_local_failed", "auto_local"
LOCAL_JUDGE_FAILED = "LOCAL_JUDGE_FAILED"
LOCAL_DICT_UNAVAILABLE = "LOCAL_DICT_UNAVAILABLE"


class StartRejected(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _content_allowlist() -> set[str] | None:
    """content_type 허용 목록. 목록 확정 전에는 BE가 허용값을 만들지 않는다(D7) — 설정이 없으면 검사하지 않는다."""
    import os

    raw = os.getenv("PIXLATE_CONTENT_TYPES")
    return {x.strip() for x in raw.split(",") if x.strip()} if raw else None


# ---------------------------------------------------------------------------------------------------------
# 시작 (ANL-01)
# ---------------------------------------------------------------------------------------------------------
def start_analysis(db: Session, job_id: int, seller_id: int) -> dict[str, Any]:
    owned = db.execute(text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"), {"j": job_id, "s": seller_id}).first()
    if not owned:
        raise StartRejected(404, "JOB_NOT_FOUND", "job not found")
    job = execution.lock_job(db, job_id)
    if job["status"] == "archived":
        raise StartRejected(409, "INVALID_STATE", "취소된 작업")
    active = execution.active_run(db, job_id, "analysis")
    if active is not None:
        cur = db.execute(
            text("SELECT id FROM job_async_task WHERE parent_task_id = :r AND stage = 'analyze' AND is_current"), {"r": active["id"]}
        ).scalar()
        db.rollback()
        return {"jobId": job_id, "accepted": True, "taskId": cur, "existing": True}
    if not (job["current_step"] == "N1" or (job["current_step"] == "N2" and job["status"] == "failed")):
        raise StartRejected(409, "INVALID_STATE", f"분석을 시작할 수 없는 단계({job['current_step']}/{job['status']})")
    if not job["regulatory_class"]:
        raise StartRejected(409, "INVALID_STATE", "규제 분류 미선택 — 분석을 시작하지 않는다(D8)")
    if not job["target_country"]:
        raise StartRejected(409, "INVALID_STATE", "대상 국가 미선택")
    rows = [dict(r) for r in db.execute(
        text("SELECT id, upload_order, file_url, width, height, sha256 FROM source_image WHERE job_id = :j ORDER BY upload_order, id"),
        {"j": job_id},
    ).mappings().all()]
    if not rows:
        raise StartRejected(409, "INVALID_STATE", "원본 이미지 없음")
    store = get_store()
    for r in rows:
        if r["sha256"] is None:  # 0008 이전 업로드: BE 가 지금 내용을 재서 고정한다
            try:
                r["sha256"] = sha256_bytes(store.get(r["file_url"]))
            except ObjectMissing as e:
                raise StartRejected(409, "INVALID_STATE", f"원본 이미지 파일 없음(source_image {r['id']})") from e
            db.execute(text("UPDATE source_image SET sha256 = :h WHERE id = :i AND sha256 IS NULL"), {"h": r["sha256"], "i": r["id"]})
    sources = [{"source_image_id": r["id"], "upload_order": r["upload_order"], "key": r["file_url"], "sha256": r["sha256"],
                "width": r["width"], "height": r["height"]} for r in rows]
    manifest = {
        "stage": "analyze", "job_id": job_id, "sources": sources, "target_country": job["target_country"],
        "regulatory_class": job["regulatory_class"], "analyzer": ai_adapters.analyzer().impl_version,
    }
    run_id = execution.create_run(db, job_id, "analysis", {
        "source_image_ids": [s["source_image_id"] for s in sources], "target_country": job["target_country"],
        "regulatory_class": job["regulatory_class"],
    })
    run = execution.lock_task(db, run_id)
    att = execution.create_attempt(db, run=run, stage="analyze", unit_id=job_id, manifest=manifest, target_count=len(sources),
                                   max_retry=N2_MAX_RETRY)
    set_job_state(db, job_id, status="processing", step="N2", ufs="analyzing")
    db.commit()
    execution.dispatch([att])
    return {"jobId": job_id, "accepted": True, "taskId": att, "existing": False}


# ---------------------------------------------------------------------------------------------------------
# 워커: analyze
# ---------------------------------------------------------------------------------------------------------
def execute_analyze(attempt_id: int) -> dict[str, Any]:
    from pipeline.errors import AnalyzeError
    from pipeline.types import SourceImage

    lease = execution.acquire(attempt_id, execution.worker_identity())
    if lease is None:
        return {"attemptId": attempt_id, "ran": False}
    m = lease.manifest
    files: dict[tuple[str, str], bytes] = {}
    try:
        with tempfile.TemporaryDirectory(prefix=f"px-analyze-{attempt_id}-") as tmp:
            tmpd = Path(tmp).resolve()
            try:
                bundle = build_bundle(m["target_country"], m["regulatory_class"])
            except BundleError as e:
                return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), e.retryable)))
            store = get_store()
            sources = []
            for s in m["sources"]:
                try:
                    data = store.get(s["key"])
                except ObjectMissing:
                    return report(attempt_id, submit(lease, failed_envelope(lease, "SOURCE_MISSING", f"원본 {s['source_image_id']} 없음", False)))
                if sha256_bytes(data) != s["sha256"]:
                    return report(attempt_id, submit(lease, failed_envelope(
                        lease, "SOURCE_CHANGED", f"원본 {s['source_image_id']} 내용이 분석 시작 때와 다르다", False)))
                ext = s["key"].rsplit(".", 1)[-1] if "." in s["key"] else "bin"
                p = tmpd / "src" / f"{s['source_image_id']}.{ext}"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                sources.append(SourceImage(source_image_id=s["source_image_id"], upload_order=s["upload_order"], path=str(p)))
            analyzer = ai_adapters.analyzer()
            with common.Heartbeat(lease) as hb:
                try:
                    if hasattr(analyzer, "analyze_with_fallbacks"):
                        result, fallbacks = analyzer.analyze_with_fallbacks(sources, tmpd / "out")
                    else:
                        result, fallbacks = analyzer.analyze(sources, tmpd / "out"), []
                except AnalyzeError as e:
                    if hb.lost:
                        return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
                    return report(attempt_id, submit(lease, failed_envelope(lease, e.code, e.message, e.retryable)))
                except ai_adapters.AdapterUnavailable as e:
                    return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), False)))
            if hb.lost:
                return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
            payload_result = result.model_dump(mode="json")
            for sec in result.sections:
                path = Path(sec.image_path)
                if not path.is_absolute():
                    path = (tmpd / "out" / path).resolve()
                if not str(path).startswith(str(tmpd)):
                    return report(attempt_id, submit(lease, failed_envelope(lease, "ARTIFACT_PATH", f"섹션 이미지가 작업 디렉터리 밖 {sec.section_key}", False)))
                try:
                    files[("section_image", sec.section_key)] = path.read_bytes()
                except OSError:
                    pass  # 채택 검증에서 누락으로 거절된다
            for s in payload_result["sections"]:
                s["image_path"] = f"artifact:section_image/{s['section_key']}"  # 로컬 경로는 공유 주소가 아니다
            env = Envelope(
                outcome="done", target_count=len(m["sources"]), input_fingerprint=lease.fingerprint,
                impl_version=analyzer.impl_version,
                payload={"analyze_result": payload_result, "bundle": bundle, "split_fallbacks": fallbacks},
                artifacts=[ArtifactSpec(kind="section_image", part_key=s.section_key, data=files.get(("section_image", s.section_key)))
                           for s in result.sections if ("section_image", s.section_key) in files],
            )
            return report(attempt_id, submit(lease, env, files))
    except execution.LeaseLost:
        return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
    except Exception as e:  # noqa: BLE001 — 예기치 않은 오류: 선행 결과 생성 실패로 보고 자동 재시도 대상(D9-1)
        log.exception("analyze 실패 attempt=%s", attempt_id)
        execution.fail_attempt(lease, "ANALYZE_UNEXPECTED", f"{e.__class__.__name__}: {e}", retryable=True)
        return {"attemptId": attempt_id, "ran": True, "failed": True}


# ---------------------------------------------------------------------------------------------------------
# 채택: analyze
# ---------------------------------------------------------------------------------------------------------
def _validate_analyze(payload: dict[str, Any], manifest: dict[str, Any], arts: dict[str, dict[str, Any]]):
    from pydantic import ValidationError

    from pipeline.types import AnalyzeResult

    try:
        res = AnalyzeResult.model_validate(payload["analyze_result"])
    except (ValidationError, KeyError, TypeError) as e:
        raise AdoptionRejected(f"AnalyzeResult 형식 오류: {e}", code="ANALYZE_RESULT_INVALID", retryable=True) from e
    if res.schema_version != "1":
        raise AdoptionRejected(f"AnalyzeResult 버전 {res.schema_version}", code="ANALYZE_RESULT_INVALID")
    srcs = {s["source_image_id"]: s for s in manifest["sources"]}
    problems: list[str] = []
    skeys = [s.section_key for s in res.sections]
    if len(set(skeys)) != len(skeys):
        problems.append("section_key 중복")
    orders = [(s.source_image_id, s.section_order) for s in res.sections]
    if len(set(orders)) != len(orders):
        problems.append("원본 내 section_order 중복")
    for s in res.sections:
        src = srcs.get(s.source_image_id)
        if src is None:
            problems.append(f"{s.section_key}: 이번 실행에 없는 원본 {s.source_image_id}")
            continue
        if s.width != src["width"] or s.top_offset + s.height > src["height"]:
            problems.append(f"{s.section_key}: 원본 범위 밖(폭 {s.width}/{src['width']}, 끝 {s.top_offset + s.height}/{src['height']})")
        art = arts.get(s.section_key)
        if art is None or art["state"] != "verified":
            problems.append(f"{s.section_key}: 섹션 이미지 없음")
        elif (art["width"], art["height"]) != (s.width, s.height):
            problems.append(f"{s.section_key}: 섹션 이미지 크기 {(art['width'], art['height'])} ≠ {(s.width, s.height)}")
    missing_src = sorted(set(srcs) - {s.source_image_id for s in res.sections})
    if missing_src:
        problems.append(f"섹션이 하나도 없는 원본 {missing_src}")
    known = set(skeys)
    bkeys = [b.block_key for b in res.blocks]
    if len(set(bkeys)) != len(bkeys):
        problems.append("block_key 중복")
    by_sec: dict[str, list[Any]] = {}
    for b in res.blocks:
        if b.section_key not in known:
            problems.append(f"{b.block_key}: 모르는 section_key {b.section_key}")
            continue
        by_sec.setdefault(b.section_key, []).append(b)
    secs = {s.section_key: s for s in res.sections}
    for k, blocks in by_sec.items():
        s = secs[k]
        if len({b.block_order for b in blocks}) != len(blocks):
            problems.append(f"{k}: block_order 중복")
        lkeys = [ln.line_key for b in blocks for ln in b.source_lines]
        rkeys = [r.region_key for b in blocks for ln in b.source_lines for r in ln.regions]
        if len(set(lkeys)) != len(lkeys) or len(set(rkeys)) != len(rkeys):
            problems.append(f"{k}: 섹션 안 line_key·region_key 중복")
        for b in blocks:
            bb = b.bbox
            if bb.x < 0 or bb.y < 0 or bb.x + bb.w > s.width or bb.y + bb.h > s.height:
                problems.append(f"{b.block_key}: bbox 가 섹션 밖")
    if problems:
        raise AdoptionRejected("; ".join(problems[:20]), code="ANALYZE_RESULT_INVALID", retryable=True)
    return res


class AnalyzeHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        h = ctx.handoff
        payload = json_value(h["payload"])
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=payload.get("error_code", "ANALYZE_FAILED"),
                                error_message=payload.get("message"), retryable=bool(payload.get("retryable")))
        manifest = ctx.attempt["input_manifest"]
        bundle = payload.get("bundle")
        try:
            verify_bundle(bundle)
        except (ValueError, TypeError, AttributeError) as e:
            raise AdoptionRejected(f"고정 사전 묶음 손상: {e}", code="BUNDLE_INTEGRITY") from e
        arts = {a["part_key"]: a for a in ctx.artifacts if a["kind"] == "section_image"}
        res = _validate_analyze(payload, manifest, arts)
        run, job = ctx.run, ctx.job
        key_map: dict[str, Any] = {"sections": {}, "blocks": {}}
        src_width = {s["source_image_id"]: s["width"] for s in manifest["sources"]}
        for s in res.sections:
            sid = db.execute(
                text(
                    "INSERT INTO section (job_id, source_image_id, section_order, top_offset, height, bbox, bucket, image_key, "
                    "analysis_task_id) VALUES (:j, :src, :o, :t, :h, CAST(:bb AS jsonb), 'include', :k, :run) RETURNING id"
                ),
                {"j": job["id"], "src": s.source_image_id, "o": s.section_order, "t": s.top_offset, "h": s.height,
                 "bb": json.dumps({"x": 0, "y": s.top_offset, "w": src_width[s.source_image_id], "h": s.height}),
                 "k": artifacts.adopted_key(arts[s.section_key]), "run": run["id"]},
            ).scalar_one()
            key_map["sections"][s.section_key] = sid
        for b in res.blocks:
            bid = db.execute(
                text(
                    "INSERT INTO text_block (section_id, block_order, role, source_ko, source_lines, bbox, ocr_confidence) "
                    "VALUES (:s, :o, :r, :ko, CAST(:sl AS jsonb), CAST(:bb AS jsonb), :c) RETURNING id"
                ),
                {"s": key_map["sections"][b.section_key], "o": b.block_order, "r": b.role, "ko": b.source_ko,
                 "sl": json.dumps([ln.model_dump(mode="json") for ln in b.source_lines], ensure_ascii=False),
                 "bb": json.dumps(b.bbox.model_dump()), "c": b.ocr_confidence},
            ).scalar_one()
            key_map["blocks"][b.block_key] = bid
        # 섹션별 ③-1·③-1′ 시도 — 같은 트랜잭션에서 만들고 커밋 후 보낸다
        judge_impl = ai_adapters.judge().impl_version
        ids = []
        order = sorted(res.sections, key=lambda s: (s.source_image_id, s.section_order))
        for s in order:
            sid = key_map["sections"][s.section_key]
            jm = _judge_manifest(db, run_id=run["id"], handoff_id=h["id"], bundle=bundle, section_id=sid,
                                 image_sha=arts[s.section_key]["sha256"], manifest=manifest, judge_impl=judge_impl)
            ids.append(execution.create_attempt(db, run=run, stage="judge", unit_id=sid, manifest=jm, target_count=1,
                                                max_retry=N2_MAX_RETRY))
        fallbacks = payload.get("split_fallbacks") or []
        src_ids = {s["source_image_id"] for s in manifest["sources"]}
        if not isinstance(fallbacks, list) or any(not isinstance(f, dict) or f.get("source_image_id") not in src_ids for f in fallbacks):
            raise AdoptionRejected("split_fallbacks 형식 또는 원본 대응 오류", code="ANALYZE_RESULT_INVALID")
        # ① 원본 전체 대체 기록(D9-1)은 채택 기록에 남긴다. 화면 표시 요구가 없어 API 로 노출하지 않는다
        verify = {"key_map": key_map, "warnings": [w.model_dump(mode="json") for w in res.warnings],
                  "bundle_sha256": bundle["sha256"], "local_dictionary": bundle["local"]["status"],
                  "split_fallbacks": fallbacks}
        return AdoptOutcome(status="done", verify_result=verify, dispatch=ids)

    def after_failure(self, db: Session, attempt: dict[str, Any], error_code: str, retryable: bool) -> list[int]:
        return _retry_or_fail_run(db, attempt, error_code, retryable)

    def after_done(self, db: Session, attempt: dict[str, Any]) -> list[int]:
        return []


def _blocks_of(db: Session, section_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(
        text("SELECT id, block_order, role, source_ko, bbox, source_lines FROM text_block WHERE section_id = :s ORDER BY block_order, id"),
        {"s": section_id},
    ).mappings().all()]


def _neighbors(db: Session, section_id: int) -> tuple[str | None, str | None]:
    me = db.execute(text("SELECT analysis_task_id, source_image_id, section_order FROM section WHERE id = :s"), {"s": section_id}).mappings().one()

    def text_of(op: str, order: str) -> str | None:
        r = db.execute(
            text(
                f"SELECT id FROM section WHERE analysis_task_id IS NOT DISTINCT FROM :a AND source_image_id = :src "
                f"AND section_order {op} :o ORDER BY section_order {order} LIMIT 1"
            ),
            {"a": me["analysis_task_id"], "src": me["source_image_id"], "o": me["section_order"]},
        ).first()
        if r is None:
            return None
        parts = [b["source_ko"] for b in _blocks_of(db, r[0]) if b["source_ko"]]
        return "\n".join(parts) if parts else None

    return text_of("<", "DESC"), text_of(">", "ASC")


def _judge_blocks_fp(blocks: list[dict[str, Any]]) -> str:
    return stable_hash([[b["id"], b["block_order"], b["role"], b["source_ko"], json_value(b["bbox"]), json_value(b["source_lines"])]
                        for b in blocks])


def _judge_manifest(db: Session, *, run_id: int, handoff_id: int, bundle: dict[str, Any], section_id: int, image_sha: str,
                    manifest: dict[str, Any], judge_impl: str) -> dict[str, Any]:
    blocks = _blocks_of(db, section_id)
    prev_t, next_t = _neighbors(db, section_id)
    return {
        "stage": "judge", "analysis_run": run_id, "analyze_handoff": handoff_id, "bundle_sha256": bundle["sha256"],
        "section_id": section_id, "section_image_sha256": image_sha, "blocks_fp": _judge_blocks_fp(blocks),
        "neighbors_fp": stable_hash([prev_t, next_t]), "regulatory_class": manifest["regulatory_class"],
        "target_country": manifest["target_country"], "judge": judge_impl,
    }


# ---------------------------------------------------------------------------------------------------------
# 워커: judge
# ---------------------------------------------------------------------------------------------------------
def execute_judge(attempt_id: int) -> dict[str, Any]:
    lease = execution.acquire(attempt_id, execution.worker_identity())
    if lease is None:
        return {"attemptId": attempt_id, "ran": False}
    m = lease.manifest
    try:
        db = app_db.SessionLocal()
        try:
            sec = db.execute(text("SELECT * FROM section WHERE id = :s"), {"s": m["section_id"]}).mappings().first()
            hrow = db.execute(text("SELECT payload FROM task_handoff WHERE id = :h AND state = 'adopted'"),
                              {"h": m["analyze_handoff"]}).mappings().first()
            blocks = _blocks_of(db, m["section_id"]) if sec else []
            prev_t, next_t = _neighbors(db, m["section_id"]) if sec else (None, None)
        finally:
            db.close()
        if sec is None or hrow is None:
            return report(attempt_id, submit(lease, failed_envelope(lease, "INPUT_MISSING", "섹션 또는 분석 인계 없음", False)))
        bundle = json_value(hrow["payload"])["bundle"]
        if (bundle.get("sha256") != m["bundle_sha256"] or _judge_blocks_fp(blocks) != m["blocks_fp"]
                or stable_hash([prev_t, next_t]) != m["neighbors_fp"]):
            return report(attempt_id, submit(lease, failed_envelope(lease, "INPUT_CHANGED", "판정 입력이 고정 입력과 다르다", False)))
        verify_bundle(bundle)
        data = get_store().get(sec["image_key"])
        if sha256_bytes(data) != m["section_image_sha256"]:
            return report(attempt_id, submit(lease, failed_envelope(lease, "INPUT_CHANGED", "섹션 이미지가 고정 입력과 다르다", False)))
        with tempfile.TemporaryDirectory(prefix=f"px-judge-{attempt_id}-") as tmp:
            img = Path(tmp).resolve() / f"{section_key(sec['id'])}.png"
            img.write_bytes(data)
            js = ai_adapters.JudgeSection(
                section_key=section_key(sec["id"]), image_path=str(img), width=json_value(sec["bbox"])["w"], height=sec["height"],
                blocks=[ai_adapters.JudgeBlock(key=block_key(b["id"]), block_order=b["block_order"], source_ko=b["source_ko"] or "",
                                               role=b["role"], bbox=json_value(b["bbox"]), source_lines=json_value(b["source_lines"]) or [])
                        for b in blocks],
                prev_section_text=prev_t, next_section_text=next_t,
                source_image_id=sec["source_image_id"], section_order=sec["section_order"], top_offset=sec["top_offset"],
                execution_id=str(lease.run_id), attempt_id=str(attempt_id),
            )
            jd = ai_adapters.judge()
            with common.Heartbeat(lease) as hb:
                try:
                    out = jd.judge(js, bundle, regulatory_class=m["regulatory_class"], target_country=m["target_country"])
                except ai_adapters.AdapterUnavailable as e:
                    return report(attempt_id, submit(lease, failed_envelope(lease, e.code, str(e), False, 1)))
            if hb.lost:
                return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
        if not isinstance(out, ai_adapters.JudgeOutcome):
            out = ai_adapters.JudgeOutcome.model_validate(out)
        payload = {"judge": out.model_dump(mode="json")}
        if out.regulatory.status != "ok":
            env = Envelope(outcome="failed", target_count=1, input_fingerprint=lease.fingerprint, impl_version=jd.impl_version,
                           payload={**payload, "error_code": "REGULATORY_JUDGE_FAILED", "message": out.regulatory.error or "",
                                    "retryable": out.regulatory.retryable})
        else:
            env = Envelope(outcome="done", target_count=1, input_fingerprint=lease.fingerprint, impl_version=jd.impl_version,
                           payload=payload)
        return report(attempt_id, submit(lease, env))
    except execution.LeaseLost:
        return {"attemptId": attempt_id, "ran": True, "leaseLost": True}
    except Exception as e:  # noqa: BLE001
        log.exception("judge 실패 attempt=%s", attempt_id)
        execution.fail_attempt(lease, "JUDGE_UNEXPECTED", f"{e.__class__.__name__}: {e}", retryable=True)
        return {"attemptId": attempt_id, "ran": True, "failed": True}


# ---------------------------------------------------------------------------------------------------------
# 채택: judge — 검증·임시 키 변환·저장(D6·D7·D9)
# ---------------------------------------------------------------------------------------------------------
def _persist_judge(db: Session, ctx: AdoptContext, out: ai_adapters.JudgeOutcome, bundle: dict[str, Any]) -> dict[str, Any]:
    m = ctx.attempt["input_manifest"]
    section_id = m["section_id"]
    if out.section_key != section_key(section_id):
        raise AdoptionRejected(f"section_key {out.section_key} ≠ {section_key(section_id)}", code="UNKNOWN_KEY")
    blocks = {block_key(b["id"]): b for b in _blocks_of(db, section_id)}
    refs = entries_by_ref(bundle)
    problems: list[str] = []
    allow = _content_allowlist()

    fkeys = [f.finding_key for f in out.findings]
    if len(set(fkeys)) != len(fkeys):
        problems.append("finding_key 중복")
    ctypes = [f.content_type for f in out.findings]
    if len(set(ctypes)) != len(ctypes):
        problems.append("섹션·항목당 finding 1건(D9-2) — content_type 중복")
    by_f = {f.finding_key: f for f in out.findings}
    for f in out.findings:
        if allow is not None and f.content_type not in allow:
            problems.append(f"{f.finding_key}: 허용 목록 밖 content_type {f.content_type}")
        unknown = [k for k in f.evidence_block_keys if k not in blocks]
        if unknown:
            problems.append(f"{f.finding_key}: 모르는 블록 키 {unknown}")
        if len(set(f.evidence_block_keys)) != len(f.evidence_block_keys):
            problems.append(f"{f.finding_key}: 근거 블록 중복")
        if f.evidence_source == "image" and f.evidence_block_keys:
            problems.append(f"{f.finding_key}: image 근거에는 블록을 연결하지 않는다")
        if f.evidence_source == "text" and not f.evidence_block_keys:
            problems.append(f"{f.finding_key}: text 근거인데 블록이 없다")
        bad_refs = [r for r in f.dictionary_refs if r not in refs]
        if bad_refs:
            problems.append(f"{f.finding_key}: 묶음 밖 사전 ID {bad_refs}")
    mkeys = [x.match_key for x in out.matches]
    if len(set(mkeys)) != len(mkeys):
        problems.append("match_key 중복")
    for x in out.matches:
        if x.finding_key not in by_f:
            problems.append(f"{x.match_key}: 모르는 finding {x.finding_key}")
        if x.dictionary_ref not in refs:
            problems.append(f"{x.match_key}: 묶음 밖 사전 ID {x.dictionary_ref}")
        b = blocks.get(x.block_key)
        if b is None:
            problems.append(f"{x.match_key}: 모르는 블록 키 {x.block_key}")
            continue
        src = b["source_ko"] or ""
        if not (0 <= x.start < x.end <= len(src)) or src[x.start:x.end] != x.matched_text:
            problems.append(f"{x.match_key}: 문자 구간 [{x.start},{x.end}) 이 원문과 맞지 않는다")
    vkeys = [v.verdict_key for v in out.verdicts]
    if len(set(vkeys)) != len(vkeys):
        problems.append("verdict_key 중복")
    for v in out.verdicts:
        f = by_f.get(v.finding_key)
        if f is None:
            problems.append(f"{v.verdict_key}: 모르는 finding {v.finding_key}")
        elif v.dictionary_ref not in f.dictionary_refs:
            problems.append(f"{v.verdict_key}: finding 이 사용하지 않은 사전 {v.dictionary_ref}")
        if v.dictionary_ref not in refs:
            problems.append(f"{v.verdict_key}: 묶음 밖 사전 ID {v.dictionary_ref}")
    # 현지 상태는 입력(묶음)과 일치해야 한다. 조회 실패(미공급)는 '제외 없음·검사 불가'이며 판정 실패 제외로 바꾸지 않는다(D9-1)
    if bundle["local"]["status"] != "ok" and out.local.status != "not_inspected":
        problems.append(f"현지 사전이 공급되지 않았는데 현지 검사 {out.local.status}(not_inspected 만 허용)")
    if out.local.status == "not_inspected" and bundle["local"]["status"] == "ok":
        problems.append("현지 사전이 공급됐는데 현지 미검사")
    if out.local.status != "ok":
        from pipeline.handoff.bundle import load_type_map

        local_types = {t.content_type for t in load_type_map().local}
        local_refs = {r for r, e in refs.items() if e["dict_type"] == "local"}
        bad_f = [f.finding_key for f in out.findings if f.content_type in local_types or set(f.dictionary_refs) & local_refs]
        bad_v = [v.verdict_key for v in out.verdicts if v.dictionary_ref in local_refs]
        bad_m = [x.match_key for x in out.matches if x.dictionary_ref in local_refs]
        if bad_f or bad_v or bad_m:
            problems.append(f"현지 검사 {out.local.status} 인데 현지 결과가 있다(finding {bad_f}·판정 {bad_v}·매칭 {bad_m})")
    if out.policy is None:
        problems.append("정책 적용 정보(policy) 누락")
    if not isinstance(out.inspection, dict) or not out.inspection:
        problems.append("검사 범위(inspection) 누락")
    if problems:
        raise AdoptionRejected("; ".join(problems[:20]), code="JUDGE_RESULT_INVALID", retryable=True)

    findings_db = [
        {"finding_key": f.finding_key, "content_type": f.content_type, "status": f.status,
         "evidence_block_ids": [blocks[k]["id"] for k in f.evidence_block_keys], "evidence_source": f.evidence_source,
         "reason": f.reason}
        for f in out.findings
    ]
    verdict_rows = []
    for v in out.verdicts:
        e = refs[v.dictionary_ref]
        pe = primary_evidence(e)
        alt = e["alternative_expression"]
        vt = VERDICT_TYPE[(v.verdict_status, bool(alt))]
        vid = db.execute(
            text(
                "INSERT INTO section_verdict (section_id, verdict_status, verdict_type, problem_text, dictionary_id, "
                "alternative_expression, basis_article, evidence_url, reason) "
                "VALUES (:s, :vs, :vt, :pt, :d, :alt, :ba, :url, :r) RETURNING id"
            ),
            {"s": section_id, "vs": v.verdict_status, "vt": vt, "pt": v.problem_text, "d": e["pk"], "alt": alt,
             "ba": pe["article"] if pe else None, "url": pe["url"] if pe else None, "r": e["reason"]},
        ).scalar_one()
        verdict_rows.append({"id": vid, "verdict_key": v.verdict_key, "finding_key": v.finding_key, "dictionary_ref": v.dictionary_ref,
                             "verdict_status": v.verdict_status, "verdict_type": vt, "bucket": v.bucket,
                             "finding_status": v.finding_status, "problem_text": v.problem_text, "dictionary_id": e["pk"],
                             "alternative_expression": alt, "basis_article": pe["article"] if pe else None,
                             "evidence_url": pe["url"] if pe else None, "reason": e["reason"]})
    excl_reg = any(r["bucket"] == "exclude" and refs[r["dictionary_ref"]]["dict_type"] == "regulatory" for r in verdict_rows)
    excl_local = any(r["bucket"] == "exclude" and refs[r["dictionary_ref"]]["dict_type"] == "local" for r in verdict_rows)
    local_failed = out.local.status == "failed"
    reason = EXCL_REGULATORY if excl_reg else EXCL_LOCAL_FAILED if local_failed else EXCL_LOCAL if excl_local else None
    bucket = "exclude" if reason else "include"
    original = {"schema_version": "1", "bucket": bucket, "exclusion_reason": reason,
                "verdict_ids": [r["id"] for r in verdict_rows], "regulatory_status": out.regulatory.status,
                "local_status": out.local.status, "analysis_task_id": ctx.run["id"]}
    content_findings = {"schema_version": "1", "findings": findings_db}
    db.execute(
        text(
            "UPDATE section SET content_findings = CAST(:cf AS jsonb), original_verdict = CAST(:ov AS jsonb), bucket = :b, "
            "exclusion_reason = :r, excluded_stage = :st, updated_at = now() WHERE id = :s"
        ),
        {"cf": json.dumps(content_findings, ensure_ascii=False), "ov": json.dumps(original), "b": bucket, "r": reason,
         "st": "N3" if reason else None, "s": section_id},
    )
    used = sorted({r for f in out.findings for r in f.dictionary_refs} | {x.dictionary_ref for x in out.matches}
                  | {v.dictionary_ref for v in out.verdicts})
    detail = {
        "schema_version": common.AUDIT_SCHEMA,
        "execution": {"analysis_task_id": ctx.run["id"], "attempt_id": ctx.attempt["id"], "job_id": ctx.job["id"],
                      "section_id": section_id},
        "inputs": {"bundle_sha256": bundle["sha256"], "bundle_schema": bundle["bundle_schema"],
                   "regulatory_class": bundle["regulatory_class"], "applied_classes": bundle["applied_classes"],
                   "class_notice": bundle["class_notice"], "policy": out.policy.model_dump() if out.policy else None,
                   "judge_impl": out.impl},
        "inspection": {**out.inspection, "regulatory": out.regulatory.model_dump(), "local": out.local.model_dump(),
                       "local_dictionary": bundle["local"]["status"]},
        "findings": findings_db,
        "dictionary_snapshots": [{"snapshot_key": r, "pk": refs[r]["pk"], "external_id": r, "verdict_status": refs[r]["verdict_status"],
                                  "source_verdict_status": refs[r]["source_verdict_status"], "content": refs[r]} for r in used],
        "matches": [{"match_key": x.match_key, "finding_key": x.finding_key, "block_id": blocks[x.block_key]["id"],
                     "source_ko": blocks[x.block_key]["source_ko"], "start": x.start, "end": x.end, "matched_text": x.matched_text,
                     "snapshot_key": x.dictionary_ref} for x in out.matches],
        "evidence": [],
        "verdicts": verdict_rows,
        "links": [{"finding_key": f.finding_key, "snapshot_keys": list(f.dictionary_refs),
                   "match_keys": [x.match_key for x in out.matches if x.finding_key == f.finding_key],
                   "verdict_ids": [r["id"] for r in verdict_rows if r["finding_key"] == f.finding_key]} for f in out.findings],
        "overlaps": out.overlaps,
        "original_verdict": original,
        "previous_result": None,
    }
    write_audit(db, action_type=common.ACT_ANALYSIS_ADOPTED, target_type="section", target_id=section_id, detail=detail)
    return {"section_id": section_id, "bucket": bucket, "exclusion_reason": reason, "verdict_ids": [r["id"] for r in verdict_rows]}


class JudgeHandler:
    def adopt(self, db: Session, ctx: AdoptContext) -> AdoptOutcome:
        h = ctx.handoff
        payload = json_value(h["payload"])
        if h["outcome"] == "failed":
            return AdoptOutcome(status="failed", error_code=payload.get("error_code", "JUDGE_FAILED"), error_message=payload.get("message"),
                                retryable=bool(payload.get("retryable")))
        try:
            out = ai_adapters.JudgeOutcome.model_validate(payload["judge"])
        except Exception as e:  # noqa: BLE001
            raise AdoptionRejected(f"JudgeOutcome 형식 오류: {e}", code="JUDGE_RESULT_INVALID", retryable=True) from e
        hb = db.execute(text("SELECT payload FROM task_handoff WHERE id = :h"), {"h": ctx.attempt["input_manifest"]["analyze_handoff"]}).scalar_one()
        bundle = json_value(hb)["bundle"]
        if bundle["sha256"] != ctx.attempt["input_manifest"]["bundle_sha256"]:
            raise AdoptionRejected("고정 사전 묶음 불일치", code="BUNDLE_INTEGRITY")
        saved = _persist_judge(db, ctx, out, bundle)
        if out.local.status == "failed":
            # 현지 AI 판정 실패: 규제 결과는 채택하고 실패는 실패로 남긴다(D9-1). 섹션 제외는 저장에서 처리
            return AdoptOutcome(status="failed", error_code=LOCAL_JUDGE_FAILED, error_message=out.local.error,
                                verify_result=saved, retryable=False)
        return AdoptOutcome(status="done", verify_result=saved)

    def after_failure(self, db: Session, attempt: dict[str, Any], error_code: str, retryable: bool) -> list[int]:
        if error_code == LOCAL_JUDGE_FAILED:
            return _progress(db, attempt["parent_task_id"])
        return _retry_or_fail_run(db, attempt, error_code, retryable)

    def after_done(self, db: Session, attempt: dict[str, Any]) -> list[int]:
        return _progress(db, attempt["parent_task_id"])


# ---------------------------------------------------------------------------------------------------------
# 집계·완료·실패
# ---------------------------------------------------------------------------------------------------------
def _retry_or_fail_run(db: Session, attempt: dict[str, Any], error_code: str, retryable: bool) -> list[int]:
    run = db.execute(text("SELECT * FROM job_async_task WHERE id = :r"), {"r": attempt["parent_task_id"]}).mappings().one()
    if run["status"] != "running":
        return []
    if retryable and attempt["retry_count"] < attempt["max_retry"]:
        return [execution.create_retry(db, attempt["id"], "auto")]
    fail_run(db, dict(run), error_code, attempt.get("error_message"))
    return []


def _hidden_sections_delete(db: Session, run_id: int) -> None:
    db.execute(
        text(
            "DELETE FROM section s USING job j WHERE s.job_id = j.id AND s.analysis_task_id = :r "
            "AND j.current_analysis_task_id IS DISTINCT FROM :r"
        ),
        {"r": run_id},
    )


def _close_remaining(db: Session, run_id: int) -> None:
    db.execute(
        text(
            "UPDATE job_async_task SET status = 'cancelled', finished_at = now(), lease_epoch = lease_epoch + 1, "
            "lease_token_hash = NULL, lease_expires_at = NULL WHERE parent_task_id = :r AND status IN ('pending', 'running')"
        ),
        {"r": run_id},
    )


def fail_run(db: Session, run: dict[str, Any], error_code: str, message: str | None) -> None:
    """분석 실행 실패: 남은 시도 종료, 숨은 섹션 삭제, 작업은 N2 오류(재시도·중단 안내). 기존 채택 결과는 보존(D6)."""
    execution.finish_run(db, run["id"], "failed", error_code, message)
    _close_remaining(db, run["id"])
    _hidden_sections_delete(db, run["id"])
    job = db.execute(text("SELECT current_step FROM job WHERE id = :j"), {"j": run["job_id"]}).mappings().one()
    if job["current_step"] == "N2":
        set_job_state(db, run["job_id"], status="failed", step="N2", ufs="failed")


def _progress(db: Session, run_id: int) -> list[int]:
    run = dict(db.execute(text("SELECT * FROM job_async_task WHERE id = :r"), {"r": run_id}).mappings().one())
    if run["status"] != "running":
        return []
    atts = execution.current_attempts(db, run_id)
    analyze = [a for a in atts if a["stage"] == "analyze"]
    judges = [a for a in atts if a["stage"] == "judge"]
    if not analyze or analyze[0]["status"] != "done":
        return []
    n_sections = db.execute(text("SELECT count(*) FROM section WHERE analysis_task_id = :r"), {"r": run_id}).scalar()
    if len(judges) != n_sections:
        return []
    for a in judges:
        if a["status"] == "done":
            continue
        if a["status"] == "failed" and a["error_code"] == LOCAL_JUDGE_FAILED:
            continue
        return []  # 진행 중·재시도 대기·다른 실패는 아직 N3 를 열지 않는다
    _complete(db, run)
    return []


def _complete(db: Session, run: dict[str, Any]) -> None:
    job = db.execute(text("SELECT * FROM job WHERE id = :j"), {"j": run["job_id"]}).mappings().one()
    prev = job["current_analysis_task_id"]
    old = [dict(r) for r in db.execute(
        text("SELECT id, bucket, exclusion_reason, excluded_stage, content_findings, original_verdict FROM section "
             "WHERE job_id = :j AND analysis_task_id IS NOT DISTINCT FROM :p ORDER BY id"),
        {"j": job["id"], "p": prev},
    ).mappings().all()]
    if old:
        verdicts = [dict(r) for r in db.execute(
            text("SELECT * FROM section_verdict WHERE section_id = ANY(:ids) ORDER BY id"), {"ids": [o["id"] for o in old]}
        ).mappings().all()]
        write_audit(db, action_type=common.ACT_ANALYSIS_REPLACED, target_type="job", target_id=job["id"], detail={
            "schema_version": common.AUDIT_SCHEMA,
            "execution": {"analysis_task_id": run["id"], "job_id": job["id"]},
            "previous_result": {"analysis_task_id": prev, "sections": old, "verdicts": verdicts},
        })
        db.execute(text("DELETE FROM section WHERE id = ANY(:ids)"), {"ids": [o["id"] for o in old]})
    db.execute(text("UPDATE job SET current_analysis_task_id = :r WHERE id = :j"), {"r": run["id"], "j": job["id"]})
    analyze_h = db.execute(
        text("SELECT h.verify_result FROM task_handoff h JOIN job_async_task a ON a.id = h.task_id "
             "WHERE a.parent_task_id = :r AND a.stage = 'analyze' AND h.state = 'adopted'"),
        {"r": run["id"]},
    ).scalar()
    local = (json_value(analyze_h) or {}).get("local_dictionary")
    execution.finish_run(db, run["id"], "done", LOCAL_DICT_UNAVAILABLE if local != "ok" else None,
                         "현지 기준 검사를 하지 못했습니다" if local != "ok" else None)
    set_job_state(db, job["id"], status="review", step="N3", ufs="section_review")


def abort_analysis(db: Session, job_id: int) -> bool:
    """N2 중단(호출자가 job 잠금): 진행 중 분석 실행 중단, 숨은 섹션 삭제. 입력은 유지한다."""
    run = execution.active_run(db, job_id, "analysis")
    if run is None:
        return False
    execution.cancel_run(db, run["id"])
    _hidden_sections_delete(db, run["id"])
    return True


execution.register_handler("analyze", AnalyzeHandler())
execution.register_handler("judge", JudgeHandler())
