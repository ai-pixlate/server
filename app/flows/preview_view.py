"""검수 뷰어 `/preview`·`/blocks`의 렌더 입력·지문·시도/결과 읽기 모델(계약 ReviewPreview — PRD v3.5.10 BE 계약안). 쓰기 없음.

- 한 응답의 값은 한 조회 시점으로 만든다: 호출자가 `read_snapshot(db)`로 REPEATABLE READ·READ ONLY 트랜잭션을 연 뒤 이 모듈을 쓴다.
  조회 도중 저장·렌더 채택이 커밋돼도 이 응답의 입력·지문·시도·결과는 같은 시점 값이다(다음 조회에 반영).
- `inputFingerprint` = 미리보기 시도의 고정 입력 지문과 같은 계산(`downstream._render_inputs` → `fingerprint`).
- `render.result` = 지금 섹션에 걸린 이미지(`section.render_image_key`)를 실제로 만든 채택 기록. 최신 성공 시도를 고르지 않는다 —
  현재 분석에 연결된 최신 하류 실행의 ⑨ 시도이고, 그 인계가 채택(adopted)됐으며, 채택된 산출물의 불변 키가 지금 이미지 키와 같을 때만
  결과로 인정한다. 근거가 없으면(이전 실행·채택되지 않은 보고·최종 렌더가 덮은 이미지·실행 기록 이전 데이터) result = null.
- 렌더러: 현행 서버는 Pillow(`typeset.RENDERER_VERSION`)로 그리고 공통 렌더 번들을 쓰지 않는다(`bundleId = null`).
  Pillow 조판은 자간(`letterSpacingEm`)을 적용하지 않는다 — style 8키를 싣는 것은 공통 렌더 입력을 내는 것일 뿐 FE·서버 렌더 일치가 아니다.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import execution, s3, typeset
from app.flows import downstream
from app.flows.common import json_value, section_key
from app.flows.status import retryable as task_retryable
from app.manifest import fingerprint
from pipeline.handoff import style_defaults as sd

log = logging.getLogger(__name__)

# 입력 구성 실패 코드(계약 PreviewInputError — 역할 구분 AI 동의 11-04). 한 섹션에 여럿이면 앞의 것을 낸다
# (BE 해석: 승인 자료 → BE 구성값(섹션 크기·박스 형식·범위) → ⑦ 결과)
STYLE_DEFAULTS_UNAPPROVED = "STYLE_DEFAULTS_UNAPPROVED"
STYLE_RESULT_INVALID = "STYLE_RESULT_INVALID"
RENDER_INPUT_INVALID = "RENDER_INPUT_INVALID"
# fontScale: 셀러 배율 저장 칸이 아직 없다(blockPatch 「블록 배치」 — 별도 작업). 계약상 배율이 없으면 1.0
DEFAULT_FONT_SCALE = 1.0
_INPUT_STAGES = ("inpaint", "style", "translate")  # ⑨ 시도를 만들 수 있는 조건(downstream.progress 와 같다)


def read_snapshot(db: Session) -> None:
    """요청의 첫 문장으로 호출 — 이후 조회가 모두 같은 스냅샷을 본다."""
    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))


def renderer() -> dict[str, Any]:
    return {"rendererId": typeset.RENDERER_VERSION, "bundleId": None}


class PreviewContext:
    """한 요청에서 섹션마다 다시 읽지 않을 값(작업·현재 하류 실행·현재 시도·스타일 기본값 자료)."""

    def __init__(self, db: Session, job_id: int):
        self.db = db
        self.job = dict(db.execute(text("SELECT id, current_analysis_task_id, current_step, status FROM job WHERE id = :j"),
                                   {"j": job_id}).mappings().one())
        run = execution.latest_run(db, job_id, "downstream")
        self.any_run = run is not None  # 실행 기록(execution_schema_version 1) 방식의 하류 실행이 하나라도 있었나
        # 현재 채택한 분석에서 시작한 하류 실행만 쓴다(재분석 뒤의 이전 실행 결과를 섞지 않는다)
        if run is not None and (json_value(run["run_scope"]) or {}).get("analysis_task_id") != self.job["current_analysis_task_id"]:
            run = None
        self.run = run
        self.attempts: dict[tuple[str, int], dict[str, Any]] = (
            {(a["stage"], a["unit_id"]): a for a in execution.current_attempts(db, run["id"])} if run else {})
        try:
            self.defaults: typeset.RoleDefaults | None = typeset.load_role_defaults()
            self.style_inputs: dict[str, Any] = self.defaults.inputs
        except typeset.StyleDefaultsUnavailable as e:  # typeset.style_inputs() 와 같은 표현 — 지문 계산이 시도와 같게
            self.defaults, self.style_inputs = None, {"unavailable": str(e)}

    def inputs_ready(self, sid: int) -> bool:
        """⑥⑦⑧ 이 모두 끝나 ⑨ 입력을 만들 수 있는가. 아니면 N4 입력 준비 중(응답 분기는 FE 합의 대기)."""
        if self.run is None or sid not in (json_value(self.run["run_scope"]) or {}).get("section_ids", []):
            return False
        return all((a := self.attempts.get((st, sid))) is not None and a["status"] in ("done", "failed") for st in _INPUT_STAGES)


def _pos_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _box_ok(box: dict[str, Any], width: Any, height: Any) -> bool:
    """적용 박스가 섹션 안의 유한한 양의 크기인가(계약 RENDER_INPUT_INVALID — 섹션 밖·w·h ≤ 0)."""
    if not all(_finite(box.get(k)) for k in ("x", "y", "w", "h")) or not (_finite(width) and _finite(height)):
        return False
    x, y, w, h = (box[k] for k in ("x", "y", "w", "h"))
    return w > 0 and h > 0 and x >= 0 and y >= 0 and x + w <= width and y + h <= height


def section_input(ctx: PreviewContext, sid: int) -> dict[str, Any]:
    """섹션의 공통 렌더 입력. 결과 = {"state": "not_ready"} | {"state": "error", "inputError": …} |
    {"state": "ok", "renderInput": …, "inputFingerprint": …}."""
    if not ctx.inputs_ready(sid):
        return {"state": "not_ready"}
    db = ctx.db
    if ctx.defaults is None:
        return {"state": "error", "inputError": {"code": STYLE_DEFAULTS_UNAPPROVED, "blockIds": []}}
    sec = downstream._section_row(db, sid)
    sbox = json_value(sec["bbox"])
    width, height = (sbox.get("w") if isinstance(sbox, dict) else None), sec["height"]
    if not (_pos_int(width) and _pos_int(height)) or not sec["image_key"]:
        # 섹션 크기·원본 이미지가 없으면 렌더 입력을 만들 수 없다 — 섹션 단위 자료 문제(블록 없음)
        return {"state": "error", "inputError": {"code": RENDER_INPUT_INVALID, "blockIds": []}}
    try:
        m = downstream._render_inputs(db, ctx.run["id"], sid, style_defaults=ctx.style_inputs)
    except downstream.RenderInputInvalid as e:  # bbox NULL·좌표 키 누락 등 — 비교할 지문을 만들지 않는다
        return {"state": "error", "inputError": {"code": RENDER_INPUT_INVALID, "blockIds": e.block_ids}}
    if "unavailable" in m["style_defaults"]:
        return {"state": "error", "inputError": {"code": STYLE_DEFAULTS_UNAPPROVED, "blockIds": []}}
    rows = {r["id"]: r for r in downstream._block_rows(db, sid)}
    data, mapping = ctx.defaults.data, ctx.defaults.data.data.pipeline_mapping
    bad_style, bad_box, blocks = [], [], []
    for b in m["blocks"]:
        r = rows[b["block_id"]]
        if not _box_ok(b["box"], width, height):
            bad_box.append(r["id"])
            continue
        try:
            res = sd.resolve_block(sd.BlockInput(id=r["id"], role=r["role"], is_excluded=r["is_excluded"], source_ko=r["source_ko"],
                                                 text=r["trans_1"], style=json_value(r["style"]), style_stage=m["style_stage"]),
                                   width, data)
        except sd.StyleInputInvalid:
            bad_style.append(r["id"])
            continue
        a = res.applied
        blocks.append({
            "blockId": r["id"], "revision": r["revision"], "text": r["trans_1"], "box": dict(b["box"]),
            "style": {"fontFamily": a["font_family"], "fontWeight": a["font_weight"], "fontSizePx": a["font_size_px"],
                      "fontScale": DEFAULT_FONT_SCALE, "fontColor": a["font_color"], "align": a["align"],
                      "letterSpacingEm": a["letter_spacing_em"], "lineHeightRatio": mapping.line_height_ratio},
        })
    if bad_box:
        return {"state": "error", "inputError": {"code": RENDER_INPUT_INVALID, "blockIds": sorted(bad_box)}}
    if bad_style:
        return {"state": "error", "inputError": {"code": STYLE_RESULT_INVALID, "blockIds": sorted(bad_style)}}
    bg = m["background"]
    return {"state": "ok", "inputFingerprint": fingerprint(m), "renderInput": {
        "section": {"id": sid, "width": width, "height": height,
                    "background": {"url": s3.presigned_get(bg["key"]), "source": bg["source"], "sha256": bg["sha256"]}},
        "blocks": blocks,
    }}


def adopted_result(ctx: PreviewContext, sid: int, image_key: str | None) -> dict[str, Any] | None:
    """지금 이미지(`image_key`)를 만든 ⑨ 채택 기록. 근거가 정확히 하나일 때만 돌려준다."""
    if image_key is None or ctx.run is None:
        return None
    rows = ctx.db.execute(
        text(
            "SELECT t.id AS task_id, t.input_fingerprint AS t_fp, h.input_fingerprint AS h_fp, h.impl_version, h.payload "
            "FROM task_artifact a JOIN task_handoff h ON h.id = a.handoff_id JOIN job_async_task t ON t.id = h.task_id "
            "WHERE a.job_id = :j AND a.kind = 'render_image' AND a.part_key = :pk AND a.state = 'adopted' "
            "AND coalesce(a.verified_key, a.source_ref->>'key') = :k "
            "AND h.state = 'adopted' AND h.outcome = 'done' "
            "AND t.parent_task_id = :r AND t.stage = 'preview' AND t.unit_id = :s AND t.status = 'done'"
        ),
        {"j": ctx.job["id"], "pk": section_key(sid), "k": image_key, "r": ctx.run["id"], "s": sid},
    ).mappings().all()
    if len(rows) != 1:
        return None
    r = rows[0]
    if r["impl_version"] is None or r["t_fp"] is None or r["t_fp"] != r["h_fp"]:
        return None
    payload = json_value(r["payload"]) or {}
    measured = ((payload.get("render") or {}).get("blocks")) or {}
    return {"taskId": r["task_id"], "inputFingerprint": r["t_fp"], "rendererId": r["impl_version"],
            "_overflow": {str(k): bool(v.get("overflow")) for k, v in measured.items()}}


# 이미지 키는 있는데 지금 결과로 연결하지 못한 이유(응답 필드가 아니다 — 로그·조사용. 어느 경우든 최신 결과로 인정하지 않는다)
UNLINKED_LEGACY = "legacy"              # 실행 기록 방식의 하류 실행이 없다(그 이전 방식으로 만든 이미지)
UNLINKED_PREVIOUS_RUN = "previous_run"  # 현재 분석에 연결되지 않은 하류 실행 또는 다른 실행의 채택 결과
UNLINKED_FINAL_RENDER = "final_render"  # N6 최종 렌더가 섹션 이미지를 최종 이미지로 바꿨다
UNLINKED_UNKNOWN = "unknown"            # 실행 기록에서 채택된 산출물로 찾을 수 없는 키(채택 안 된 보고 포함)


def unlinked_reason(ctx: PreviewContext, sid: int, image_key: str) -> str:
    if not ctx.any_run:
        return UNLINKED_LEGACY
    rows = ctx.db.execute(
        text(
            "SELECT t.stage, t.parent_task_id FROM task_artifact a JOIN task_handoff h ON h.id = a.handoff_id "
            "JOIN job_async_task t ON t.id = h.task_id WHERE a.job_id = :j AND a.kind = 'render_image' AND a.state = 'adopted' "
            "AND h.state = 'adopted' AND coalesce(a.verified_key, a.source_ref->>'key') = :k"
        ),
        {"j": ctx.job["id"], "k": image_key},
    ).mappings().all()
    if any(r["stage"] == "final_render" for r in rows):
        return UNLINKED_FINAL_RENDER
    if ctx.run is None or any(r["stage"] == "preview" and r["parent_task_id"] != ctx.run["id"] for r in rows):
        return UNLINKED_PREVIOUS_RUN
    return UNLINKED_UNKNOWN


def current_attempt(ctx: PreviewContext, sid: int) -> dict[str, Any] | None:
    a = ctx.attempts.get(("preview", sid))
    if a is None or a["status"] not in ("pending", "running", "done", "failed"):
        return None  # 취소된 시도는 현재 시도로 내지 않는다
    return {"taskId": a["id"], "status": a["status"], "errorCode": a["error_code"] if a["status"] == "failed" else None,
            "retryable": task_retryable(a, ctx.run)}


def section_view(ctx: PreviewContext, sec: dict[str, Any]) -> dict[str, Any]:
    """ReviewPreview.sections[] 의 렌더 관련 필드(renderedUrl·renderInput·inputFingerprint·inputError·render)."""
    if sec["bucket"] != "include":
        # N5 제외 섹션: 렌더 상태를 판정하지 않는다. renderedUrl 은 현행 그대로(제외 전 이미지가 있으면 낸다)
        return {"renderedUrl": s3.presigned_get(sec["render_image_key"]), "renderInput": None, "inputFingerprint": None,
                "inputError": None, "render": None}
    inp = section_input(ctx, sec["id"])
    result = adopted_result(ctx, sec["id"], sec["render_image_key"])
    if result is None and sec["render_image_key"]:
        why = unlinked_reason(ctx, sec["id"], sec["render_image_key"])
        (log.info if why == UNLINKED_FINAL_RENDER else log.warning)(
            "preview: 섹션 %s 이미지 키를 채택 기록에 연결하지 못함(%s) — renderedUrl·result null", sec["id"], why)
    fp = inp.get("inputFingerprint")
    is_current = result is not None and fp is not None and result["inputFingerprint"] == fp
    shown = None
    if result is not None:
        shown = {k: v for k, v in result.items() if k != "_overflow"}
        shown["overflow"] = result["_overflow"] if is_current else None
    return {
        # 표시 결과의 이미지 — 채택 근거가 없으면 null(원본 표시)
        "renderedUrl": s3.presigned_get(sec["render_image_key"]) if result is not None else None,
        "renderInput": inp.get("renderInput"), "inputFingerprint": fp, "inputError": inp.get("inputError"),
        "render": {"attempt": current_attempt(ctx, sec["id"]), "result": shown, "isCurrent": is_current},
    }


def section_fingerprints(ctx: PreviewContext, sections: list[dict[str, Any]]) -> dict[int, str | None]:
    """`/blocks` 의 sectionInputFingerprint — /preview `inputFingerprint` 와 같은 계산. 제외 섹션·입력 구성 실패·입력 준비 중은 null."""
    out: dict[int, str | None] = {}
    for s in sections:
        out[s["id"]] = section_input(ctx, s["id"]).get("inputFingerprint") if s["bucket"] == "include" else None
    return out
