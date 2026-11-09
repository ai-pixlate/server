"""CFM — 검수/텍스트 블록 (API-CFM-01~04, 🟢9월).

CFM-01(블록 표·signals)·CFM-02(셀 수정·낙관적 잠금·재렌더)·CFM-03(프리뷰)·CFM-04(확정 → N6 최종 렌더) 실제 DB(+S3 presigned).
현재 채택한 분석 실행의 섹션만 보인다(app.flows.common.VISIBLE).
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import edit_idempotency as idem
from app import render, s3
from app.db import get_db
from app.flows import preview_view
from app.flows.common import VISIBLE
from app.security import get_current_seller

router = APIRouter(tags=["Review"])


def _require_job_owned(db: Session, job_id: int, seller_id: int) -> None:
    if not db.execute(
        text("SELECT 1 FROM job WHERE id = :j AND seller_id = :s"),
        {"j": job_id, "s": seller_id},
    ).first():
        raise HTTPException(status_code=404, detail="job not found")


def _block_dict(r, signals: dict, fps: dict) -> dict:
    return {
        "id": r["id"],
        "sectionId": r["section_id"],
        "blockOrder": r["block_order"],
        "role": r["role"],
        "isProductLabel": r["is_product_label"],
        "isBrandLogo": r["is_brand_logo"],
        "isExcluded": r["is_excluded"],
        "sourceKo": r["source_ko"],
        "trans1": r["trans_1"],
        "trans2": r["trans_2"],
        "blockStatus": r["block_status"],
        "bbox": r["bbox"],
        "charCount": r["char_count"],
        "charLimit": r["char_limit"],
        "overflow": r["overflow"],
        "autoAdjust": r["auto_adjust"],
        "signals": signals.get(r["id"], []),
        "revision": r["revision"],
        # /preview sections[].inputFingerprint 와 같은 계산 — 같은 섹션의 블록은 같은 값. 제외·입력 구성 실패·입력 준비 중은 null
        "sectionInputFingerprint": fps.get(r["section_id"]),
    }


def _blocks(db: Session, job_id: int, *, section_id: Optional[int] = None, block_id: Optional[int] = None) -> list[dict]:
    """계약 TextBlock 목록(현재 보이는 섹션의 블록). CFM-01 목록과 CFM-02 응답·충돌 정보가 같은 모양을 쓴다."""
    from app.flows.final import block_signals

    sql = (
        "SELECT tb.id, tb.section_id, tb.block_order, tb.role, "
        "tb.is_product_label, tb.is_brand_logo, tb.is_excluded, "
        "tb.source_ko, tb.trans_1, tb.trans_2, tb.block_status, tb.bbox, "
        "tb.char_count, tb.char_limit, tb.overflow, tb.auto_adjust, tb.revision "
        "FROM text_block tb JOIN section s ON s.id = tb.section_id JOIN job j ON j.id = s.job_id "
        "WHERE s.job_id = :j AND " + VISIBLE
    )
    params = {"j": job_id}
    if section_id is not None:
        sql += " AND tb.section_id = :sid"
        params["sid"] = section_id
    if block_id is not None:
        sql += " AND tb.id = :bid"
        params["bid"] = block_id
    sql += " ORDER BY tb.section_id, tb.block_order, tb.id"

    rows = db.execute(text(sql), params).mappings().all()
    signals = block_signals(db, job_id)
    sids = sorted({r["section_id"] for r in rows})
    secs = [dict(x) for x in db.execute(text("SELECT id, bucket FROM section WHERE id = ANY(:s)"), {"s": sids}).mappings().all()]
    fps = preview_view.section_fingerprints(preview_view.PreviewContext(db, job_id), secs) if secs else {}
    return [_block_dict(r, signals, fps) for r in rows]


# ── CFM-01: 실제 DB ───────────────────────────────────────────────
@router.get("/jobs/{job_id}/blocks", summary="API-CFM-01 텍스트 블록 표(대조) (DB)")
def list_blocks(job_id: int, sectionId: Optional[int] = Query(default=None), db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    preview_view.read_snapshot(db)  # 블록 값과 sectionInputFingerprint 를 한 조회 시점으로
    _require_job_owned(db, job_id, seller_id)
    return _blocks(db, job_id, section_id=sectionId)


# ── CFM-02: 셀 수정 (DB · 낙관적 잠금 · 저장 공통) ─────────────────
class BlockUpdate(BaseModel):
    """요청 형식(계약 BlockPatch): trans1(문자열)·revision(정수) 필수, 정의되지 않은 키는 거절 — 어기면 422 VALIDATION_ERROR.
    revision 은 OpenAPI `integer`(JSON 수 중 정수 값)와 같게 판단한다: 0·0.0 은 같은 정수 0, false·"0"·0.5 는 정수가 아니다.
    문자열·불리언을 숫자로 바꾸지 않는다 — 바꾸면 현재 revision 이 0 인 블록에 잘못된 요청이 저장될 수 있다. trans1 도 숫자를 문자열로 바꾸지 않는다."""
    model_config = ConfigDict(extra="forbid")

    trans1: StrictStr
    revision: int  # 낙관적 잠금: 현재 블록 revision과 일치해야 함

    @field_validator("revision", mode="before")
    @classmethod
    def _integer_value(cls, v):
        return idem.integer_value(v)


_OP_BLOCK_PATCH = "block_patch"


@router.patch("/jobs/{job_id}/blocks/{block_id}", summary="API-CFM-02 번역문 셀 수정 (DB·낙관적 잠금·멱등)")
def update_block(
    job_id: int,
    block_id: int,
    body: BlockUpdate,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128,
                                            pattern=idem.KEY_PATTERN),
    db: Session = Depends(get_db),
    seller_id: int = Depends(get_current_seller),
):
    """N5 에서만 수정한다. 처리 순서(계약 blockPatch 「저장 공통」 1~9):
    형식(422) → 작업 소유(404) → 작업 행 잠금(한도 초과 503) → 작업 상태(409 INVALID_STATE) → 멱등 기록 조회(같은 요청이면 최초 응답 재전송,
    다른 요청이면 409 DUPLICATE) → 대상 블록(404) → revision(409 REVISION_CONFLICT) → 같은 값이면 변경 없음 → 적용.
    적용 = revision+1 · edited · 수정 전후 기록(edit_signal) · 그 섹션 미리보기 재렌더 시도. 변경·부수 처리와 멱등 기록은 한 트랜잭션이다.
    같은 값이면 revision·blockStatus·edit_signal·진행 중 렌더 그대로, 전체 TextBlock 과 rerenderTaskId=null(이 응답도 멱등 기록 대상).
    재전송도 소유·작업 상태 검사를 먼저 통과해야 한다."""
    from app import execution
    from app.flows.downstream import rerender_section

    _require_job_owned(db, job_id, seller_id)
    sha = idem.request_sha256(_OP_BLOCK_PATCH, {"jobId": job_id, "blockId": block_id}, body.model_dump())
    with idem.lock_window(db):  # 잠금 대기 한도 초과 → 트랜잭션 정리 후 503
        job = execution.lock_job(db, job_id)
        if job["current_step"] != "N5" or job["status"] != "review":
            raise HTTPException(status_code=409, detail={"code": "INVALID_STATE", "message": "번역문은 N5 검수 중에만 수정한다",
                                                         "reason": "job_not_editable"})
        cur = db.execute(
            text(
                "SELECT tb.revision, tb.trans_1, tb.section_id, tb.is_excluded FROM text_block tb JOIN section s ON s.id = tb.section_id "
                "JOIN job j ON j.id = s.job_id WHERE tb.id = :b AND s.job_id = :j AND " + VISIBLE + " FOR UPDATE OF tb"
            ),
            {"b": block_id, "j": job_id},
        ).mappings().first()
    replayed = idem.replay_or_reject(db, seller_id=seller_id, job_id=job_id, operation=_OP_BLOCK_PATCH, key=idempotency_key, sha=sha)
    if replayed is not None:
        return replayed
    if not cur:
        db.rollback()
        raise HTTPException(status_code=404, detail="block not found")
    if cur["revision"] != body.revision:
        # 낙관적 잠금 충돌 — 최신 블록(계약 TextBlock)을 details.current 로 반환(4xx 는 멱등 기록 안 함)
        latest = _blocks(db, job_id, block_id=block_id)[0]
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": "REVISION_CONFLICT", "message": "block was changed", "current": latest},
        )

    rerender = None
    if cur["trans_1"] != body.trans1:
        db.execute(
            text(
                "UPDATE text_block SET trans_1 = :t, block_status = 'edited', "
                "char_count = char_length(:t), revision = revision + 1, updated_at = now() "
                "WHERE id = :b"
            ),
            {"t": body.trans1, "b": block_id},
        )
        db.execute(
            text("INSERT INTO edit_signal (job_id, text_block_id, signal_type, before_text, after_text) "
                 "VALUES (:j, :b, 'user_edited', :bt, :at)"),
            {"j": job_id, "b": block_id, "bt": cur["trans_1"], "at": body.trans1},
        )
        rerender = rerender_section(db, job_id, cur["section_id"])
    # 같은 값(no-op)이면 쓰기·기록·재렌더 없음 — 재렌더 등록 가능 여부는 묻지 않는다. NULL 칸에 빈 문자열은 같은 값이 아니다
    block = _blocks(db, job_id, block_id=block_id)[0]  # 계약 BlockPatchResponse.block = 전체 TextBlock(FE 가 캐시를 이것으로 바꾼다)
    resp = {"block": block, "rerenderTaskId": rerender}
    if idempotency_key is not None:
        idem.record(db, seller_id=seller_id, job_id=job_id, operation=_OP_BLOCK_PATCH, key=idempotency_key, sha=sha,
                    status_code=200, response=resp)
    db.commit()
    if rerender:
        execution.dispatch([rerender])
    return resp


@router.get("/jobs/{job_id}/preview", summary="API-CFM-03 검수 뷰어 프리뷰(다폭) (DB+S3)")
def preview(job_id: int, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """N3 제외 섹션은 빼고 N5 제외는 bucket=exclude 로 포함한다(계약). 포함 섹션은 공통 렌더 입력·지문·현재 시도·표시 결과를 싣는다
    (app.flows.preview_view). 한 응답은 한 조회 시점(REPEATABLE READ·READ ONLY)으로 만든다. 쓰기 없음."""
    preview_view.read_snapshot(db)
    _require_job_owned(db, job_id, seller_id)
    rows = db.execute(
        text(
            "SELECT s.id, s.section_order, s.source_image_id, s.bucket, s.excluded_stage, s.height, s.revision, "
            "s.image_key, s.render_image_key, si.width AS src_width "
            "FROM section s JOIN job j ON j.id = s.job_id LEFT JOIN source_image si ON si.id = s.source_image_id "
            "WHERE s.job_id = :j AND " + VISIBLE + " AND NOT (s.bucket = 'exclude' AND s.excluded_stage IS DISTINCT FROM 'N5') "
            "ORDER BY si.upload_order NULLS LAST, s.section_order, s.id"
        ),
        {"j": job_id},
    ).mappings().all()
    ctx = preview_view.PreviewContext(db, job_id)

    # 섹션 식별자는 계약(ReviewPreview.sections[].id)대로 `id`로 내보낸다 —
    # `sectionId`로 주면 FE N5 어댑터가 식별자 없음으로 보고 예외를 던진다.
    sections = []
    display_top = 0
    max_w = max([r["src_width"] or render.CANVAS_WIDTH for r in rows] or [render.CANVAS_WIDTH])
    for r in rows:
        height = r["height"] or 1500
        view = preview_view.section_view(ctx, dict(r))
        sections.append({
            "id": r["id"],
            "sectionOrder": r["section_order"],
            "sourceImageId": r["source_image_id"],
            "bucket": r["bucket"],
            "excludedStage": r["excluded_stage"],
            "width": r["src_width"] or render.CANVAS_WIDTH,
            "displayTop": display_top,
            "height": height,
            "originalUrl": s3.presigned_get(r["image_key"]),
            "renderedUrl": view["renderedUrl"],
            "renderInput": view["renderInput"],
            "inputFingerprint": view["inputFingerprint"],
            "inputError": view["inputError"],
            "render": view["render"],
            "signals": [],
            "revision": r["revision"],
        })
        display_top += height

    scale = round(500 / max_w, 2)
    return {
        "previewWidth": 500,
        "maxOriginalWidth": max_w,
        "originalHeight": display_top,
        "scale": scale,
        "previewHeight": round(display_top * scale),
        "align": "left",
        "renderer": preview_view.renderer(),
        "sections": sections,
    }


class Ack(BaseModel):
    blockId: int
    code: str


class ConfirmBody(BaseModel):
    acknowledgedWarnings: list[Ack] = Field(default_factory=list)


@router.post("/jobs/{job_id}/confirm", summary="API-CFM-04 검수 확정(N5→N6) (DB)")
def confirm(job_id: int, body: Optional[ConfirmBody] = None, db: Session = Depends(get_db), seller_id: int = Depends(get_current_seller)):
    """미해결 경고(번역하지 못한 칸)를 확인받고 확정한다(D9-3). 확정 트랜잭션에서 최종 렌더 시도를 서버가 등록한다."""
    from app.flows.final import ConfirmRejected
    from app.flows.final import confirm as do_confirm

    acks = [a.model_dump() for a in (body.acknowledgedWarnings if body else [])]
    try:
        r = do_confirm(db, job_id, seller_id, acks)
    except ConfirmRejected as e:
        db.rollback()
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message, **e.details}) from e
    return {"jobId": r["jobId"], "status": r["status"], "currentStep": r["currentStep"]}
