"""④ 라벨 · ⑤ 로고 · ⑥ 인페인트 · ⑦ 스타일 — 운영 인계 어댑터 [통합 D1 · D9-3 · 5.24 · 5.31 · 5.32].

단독 개발 함수(stages/label · logo · inpaint · style)를 바꾸지 않고 감싼다. 공통 규칙:
- 키는 분석 때의 section_key · block_key를 그대로 쓴다(DB id 변환은 BE). 원본 섹션 이미지는 절대 경로 + SHA-256으로 받아 대조한다.
- ⑥⑦은 ④⑤의 **채택된 인계 payload 원본**(label_result · logo_result · logo_record)과 그 JCS SHA-256을 받는다. 해시가 다르거나
  payload를 복원할 수 없으면 거절한다(최신 판정으로 재구성하지 않음, 5.32). 블록 지문 · ④⑤ 지문 대조는 단계 함수가 한다.
- outcome: 처리 대상이 없으면 skipped(사유 · 대상 0), 입력 오류는 failed(input_invalid) — 빈 성공으로 바꾸지 않는다.
- ⑥ masked(배경 없음)는 운영 결과가 아니다. 빈 최종 마스크는 모델 없이 skipped + 원본 배경 참조(source_ref)와 두 마스크를 준다.
- ⑦ 측정 불가는 NULL과 사유. 역할 기본값(글자색 · 크기 · 폰트)을 채우지 않는다 — 적용값은 BE 조판이 별도 기록한다(D9-3).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Literal

import numpy as np
from pydantic import Field, ValidationError, field_validator

from pipeline.handoff.canonical import sha256_canonical, sha256_file, sha256_text
from pipeline.handoff.envelope import (
    CONTRACT_VERSION,
    HandoffInputError,
    RequestIdentity,
    SourceRef,
    StageReport,
    _Model,
    artifact_of,
    check_expected_manifest,
    failed_report,
    identity_fallback,
    manifest_of,
    parse_identity,
)
from pipeline.stages import inpaint as inpaint_stage
from pipeline.stages import label as label_stage
from pipeline.stages import logo as logo_stage
from pipeline.stages import style as style_stage
from pipeline.types import LabelResult, LogoResult, MergeResult, Section, TextBlock
from pipeline.vlm import JudgeAssistant

DOWNSTREAM_ADAPTER_VERSION = "downstream-handoff@2026-10-08.1"
SHA256 = r"^[0-9a-f]{64}$"


class SectionImage(_Model):
    path: str
    sha256: str = Field(pattern=SHA256)

    @field_validator("path")
    @classmethod
    def _abs(cls, v: str) -> str:
        if not Path(v).is_absolute():
            raise ValueError(f"섹션 이미지 경로는 절대 경로여야 한다: {v!r}")
        return v


class _SectionRequest(RequestIdentity):
    section: dict[str, Any]  # Section(AnalyzeResult v1의 섹션) — image_path는 section_image.path로 바꿔 쓴다
    section_image: SectionImage
    blocks: list[dict[str, Any]]  # 이 섹션의 TextBlock(분석 결과 그대로)


class LabelRequest(_SectionRequest):
    stage: Literal["label"]


class BrandSnapshot(_Model):
    brand_id: str = Field(min_length=1)
    name_ko: str  # brand.name_ko NOT NULL(0001). 정규화 후 빈 값이면 ⑤가 입력 오류로 거부
    name_en: str


class LogoRequest(RequestIdentity):
    stage: Literal["logo"]
    image_id: str = Field(min_length=1)  # 원본 이미지 식별(분석의 source_image_id 문자열)
    section_key: str = Field(min_length=1)
    blocks: list[dict[str, Any]]
    label_result: dict[str, Any]
    label_result_sha256: str = Field(pattern=SHA256)
    brand: BrandSnapshot


class _PrevStages(_SectionRequest):
    image_id: str = Field(min_length=1)
    label_result: dict[str, Any]
    label_result_sha256: str = Field(pattern=SHA256)
    logo_result: dict[str, Any]
    logo_result_sha256: str = Field(pattern=SHA256)
    logo_record: dict[str, Any]
    logo_record_sha256: str = Field(pattern=SHA256)


class InpaintRequest(_PrevStages):
    stage: Literal["inpaint"]
    out_dir: str  # 이 시도 전용 새 폴더(절대 경로)
    limits: dict[str, float] | None = None  # BE가 정한 운영 시간 제한(init_timeout_s · infer_timeout_s · kill_grace_s). 없으면 개발 기본값 — 기록에 남긴다


class StyleRequest(_PrevStages):
    stage: Literal["style"]


# ---------------------------------------------------------------------------
# 공통 검증
# ---------------------------------------------------------------------------
def _section_inputs(req: _SectionRequest) -> tuple[Section, list[TextBlock]]:
    try:
        sec = Section.model_validate({**req.section, "image_path": req.section_image.path})
        blocks = [TextBlock.model_validate(b) for b in req.blocks]
    except ValidationError as e:
        raise HandoffInputError(f"섹션 · 블록 형식 오류: {e}") from e
    p = Path(req.section_image.path)
    if not p.is_file():
        raise HandoffInputError(f"섹션 이미지가 없다: {p}")
    if sha256_file(p) != req.section_image.sha256:
        raise HandoffInputError(f"{sec.section_key}: 섹션 이미지 SHA-256 불일치")
    bad = [b.block_key for b in blocks if b.section_key != sec.section_key]
    keys = [b.block_key for b in blocks]
    if bad or len(set(keys)) != len(keys):
        raise HandoffInputError(f"{sec.section_key}: 다른 섹션 블록 {bad} 또는 블록 키 중복")
    return sec, blocks


def _verified(name: str, data: dict[str, Any], expected: str) -> dict[str, Any]:
    got = sha256_canonical(data)
    if got != expected:
        raise HandoffInputError(f"{name} 해시 불일치 — 채택된 인계 payload가 아니다(선언 {expected[:12]}… · 계산 {got[:12]}…)")
    return data


def _prev(req: _PrevStages) -> tuple[LabelResult, LogoResult, dict[str, Any]]:
    try:
        label = LabelResult.model_validate(_verified("label_result", req.label_result, req.label_result_sha256))
        logo = LogoResult.model_validate(_verified("logo_result", req.logo_result, req.logo_result_sha256))
    except ValidationError as e:
        raise HandoffInputError(f"④⑤ 결과 형식 오류: {e}") from e
    record = _verified("logo_record", req.logo_record, req.logo_record_sha256)
    return label, logo, record


def _begin(raw: Any, stage: str, model: type[RequestIdentity]):
    try:
        ident = parse_identity(raw, stage)
    except (ValidationError, HandoffInputError) as e:
        return None, None, failed_report(identity_fallback(raw, stage), "input_invalid", f"요청 식별 오류: {e}")
    try:
        return ident, model.model_validate(raw), None
    except ValidationError as e:
        return ident, None, failed_report(ident, "input_invalid", f"요청 형식 오류: {e}")


def _report(ident: RequestIdentity, manifest: dict[str, Any], msha: str, *, outcome: str, target_count: int, payload: dict[str, Any],
            impl: dict[str, Any], skip_reason: str | None = None, artifacts=None, source_refs=None, diagnostics=None) -> StageReport:
    return StageReport(
        execution_id=ident.execution_id, attempt_id=ident.attempt_id, stage=ident.stage, input_snapshot_id=ident.input_snapshot_id,
        input_manifest=manifest, input_manifest_sha256=msha, outcome=outcome, target_count=target_count, skip_reason=skip_reason,
        payload=payload, artifacts=artifacts or [], source_refs=source_refs or [], implementation=impl, diagnostics=diagnostics or {},
    )


# ---------------------------------------------------------------------------
# ④ 제품 라벨 판정
# ---------------------------------------------------------------------------
_LABEL_FAILURE = {"call_failed": "model_call_failed", "validation_failed": "response_invalid",
                  "replay_mismatch": "input_invalid", "input_mismatch": "input_invalid"}


def run_label(raw: Any, cfg: dict[str, Any], *, llm: JudgeAssistant | None = None) -> StageReport:
    ident, req, bad = _begin(raw, "label", LabelRequest)
    if bad:
        return bad
    impl = {"adapter": DOWNSTREAM_ADAPTER_VERSION, "model": {k: cfg["label"].get(k) for k in ("model", "temperature", "timeout_s", "long_side_px", "bias")}}
    try:
        label_stage.validate_config(cfg)
        prompt = label_stage.validate_llm_config(cfg, need_api_key=llm is None)
    except ValueError as e:
        return failed_report(ident, "config_invalid", str(e), implementation=impl)
    try:
        sec, blocks = _section_inputs(req)
        manifest, msha = manifest_of({
            "stage": "label", "contract_version": CONTRACT_VERSION, "adapter": DOWNSTREAM_ADAPTER_VERSION,
            "section": sec.model_dump(mode="json", exclude={"image_path"}), "image_sha256": req.section_image.sha256,
            "blocks": [b.model_dump(mode="json") for b in sorted(blocks, key=lambda b: b.block_order)],
            "config": impl["model"], "prompt_sha256": sha256_text(prompt),
        })
        check_expected_manifest(ident, msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)
    rec: dict[str, Any] = {}
    try:
        result = label_stage.run(sec, blocks, cfg, llm=llm, recorder=rec.update)
    except (ValueError, OSError) as e:
        return failed_report(ident, "input_invalid", f"④ 입력 오류: {e}", manifest=manifest, implementation=impl)
    except Exception as e:  # noqa: BLE001
        return failed_report(ident, "internal", f"{e.__class__.__name__}: {e}", manifest=manifest, implementation=impl)
    diag = {k: rec.get(k) for k in ("status", "payload_sha256", "prompt_sha256", "image_sha256", "usage", "validation")}
    if result.status == "failed":
        kind = _LABEL_FAILURE.get(str(rec.get("status")), "model_call_failed")
        return failed_report(ident, kind, result.error or "④ 판정 실패", manifest=manifest, target_count=len(blocks),
                             targets=[sec.section_key], implementation=impl, diagnostics=diag)
    lr = result.model_dump(mode="json")
    payload = {"section_key": sec.section_key,
               "decisions": [{"block_key": d.block_key, "is_product_label": d.is_product_label, "basis": d.basis} for d in result.labels or []],
               "label_result": lr, "label_result_sha256": sha256_canonical(lr), "llm_called": result.checked.llm_called}
    if not blocks:
        return _report(ident, manifest, msha, outcome="skipped", target_count=0, skip_reason="no_blocks", payload=payload, impl=impl,
                       diagnostics=diag)
    return _report(ident, manifest, msha, outcome="completed", target_count=len(blocks), payload=payload, impl=impl, diagnostics=diag)


# ---------------------------------------------------------------------------
# ⑤ 브랜드 로고 제외
# ---------------------------------------------------------------------------
def brand_meta_of(image_id: str, brand: BrandSnapshot) -> dict[str, Any]:
    """운영 브랜드 스냅샷(job.brand_id의 name_ko · name_en) → ⑤ 단독 함수의 브랜드 자료 형식. 별칭 · 번역 · 부분 일치 없음(#68 M2)."""
    return {"products": [{"image_group": f"brand-{brand.brand_id}", "images": [image_id], "product_name": None,
                          "name_ko": brand.name_ko, "name_ko_status": "provided", "name_en": brand.name_en, "name_en_status": "provided"}]}


def run_logo(raw: Any, cfg: dict[str, Any]) -> StageReport:
    ident, req, bad = _begin(raw, "logo", LogoRequest)
    if bad:
        return bad
    impl = {"adapter": DOWNSTREAM_ADAPTER_VERSION, "normalize": cfg.get("logo", {}).get("normalize"), "rule": logo_stage.NORMALIZE_RULE}
    try:
        blocks = [TextBlock.model_validate(b) for b in req.blocks]
        merged = MergeResult(section_key=req.section_key, blocks=blocks)
        label = LabelResult.model_validate(_verified("label_result", req.label_result, req.label_result_sha256))
        manifest, msha = manifest_of({
            "stage": "logo", "contract_version": CONTRACT_VERSION, "adapter": DOWNSTREAM_ADAPTER_VERSION,
            "image_id": req.image_id, "section_key": req.section_key,
            "blocks": [b.model_dump(mode="json") for b in sorted(blocks, key=lambda b: b.block_order)],
            "label_result_sha256": req.label_result_sha256, "brand": req.brand.model_dump(), "normalize": impl["normalize"],
        })
        check_expected_manifest(ident, msha)
    except ValidationError as e:
        return failed_report(ident, "input_invalid", f"⑤ 입력 형식 오류: {e}", implementation=impl)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)
    try:
        result, record = logo_stage.run(req.image_id, merged, label, brand_meta_of(req.image_id, req.brand), cfg)
    except logo_stage.LogoInputError as e:  # 브랜드 부재 · ④ 미판정을 false로 바꾸지 않는다
        return failed_report(ident, "input_invalid", f"⑤ 입력 오류: {e}", manifest=manifest, implementation=impl)
    except Exception as e:  # noqa: BLE001
        return failed_report(ident, "internal", f"{e.__class__.__name__}: {e}", manifest=manifest, implementation=impl)
    lr = result.model_dump(mode="json")
    payload = {"section_key": req.section_key,
               "decisions": [d.model_dump(mode="json") for d in result.decisions or []],
               "logo_result": lr, "logo_result_sha256": sha256_canonical(lr),
               "logo_record": record, "logo_record_sha256": sha256_canonical(record)}
    compared = record["counts"]["compared"]
    if compared == 0:
        reason = "no_blocks" if not blocks else "all_product_label"
        return _report(ident, manifest, msha, outcome="skipped", target_count=0, skip_reason=reason, payload=payload, impl=impl)
    return _report(ident, manifest, msha, outcome="completed", target_count=compared, payload=payload, impl=impl)


# ---------------------------------------------------------------------------
# ⑥ 인페인팅
# ---------------------------------------------------------------------------
ModelFactory = Callable[[dict[str, Any]], Any]


def _limits_cfg(cfg: dict[str, Any], limits: dict[str, float] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    keys = ("init_timeout_s", "infer_timeout_s", "kill_grace_s")
    if limits is None:
        return cfg, {"source": "dev_config", "operational": False, **{k: cfg["inpaint"][k] for k in keys}}
    unknown = sorted(set(limits) - set(keys))
    if unknown:
        raise HandoffInputError(f"limits에 모르는 키 {unknown}")
    merged = {**cfg, "inpaint": {**cfg["inpaint"], **limits}}
    return merged, {"source": "request", "operational": True, **{k: merged["inpaint"][k] for k in keys}}


def run_inpaint(raw: Any, cfg: dict[str, Any], *, model: Any | None = None, model_factory: ModelFactory | None = None,
                close_model: bool = True, cancel: threading.Event | None = None) -> StageReport:
    """model을 주면 그 모델을 쓰고 닫지 않는다(워커가 수명 관리). 없으면 비어 있지 않은 마스크에서 model_factory(기본 LaMa 자식 프로세스)로
    한 번 만들고 close_model이면 끝나고 닫는다(워커가 팩토리로 공유 모델을 주면 close_model=False). 빈 마스크면 모델을 만들지 않는다.
    cancel이 설정되면(실행 권한 상실 등) 모델 호출 전후로 멈추고 cancelled 실패를 낸다."""
    from PIL import Image

    from pipeline.run import save_png_verified

    ident, req, bad = _begin(raw, "inpaint", InpaintRequest)
    if bad:
        return bad
    impl: dict[str, Any] = {"adapter": DOWNSTREAM_ADAPTER_VERSION, "raster_rule": inpaint_stage.RASTER_RULE}
    try:
        inpaint_stage.validate_config(cfg)
        cfg_run, limits = _limits_cfg(cfg, req.limits)
        impl["limits"] = limits
        sec, blocks = _section_inputs(req)
        label, logo, record = _prev(req)
        out = Path(req.out_dir)
        if not out.is_absolute():
            raise HandoffInputError(f"out_dir는 절대 경로여야 한다: {out}")
        if out.exists() and any(out.iterdir()):
            raise HandoffInputError(f"out_dir가 비어 있지 않다: {out}")
        with Image.open(req.section_image.path) as im:
            if im.mode != "RGB":
                raise HandoffInputError(f"섹션 이미지 모드 {im.mode} — RGB만 지원(색 공간 변환 없음)")
            image = np.array(im)
        plan = inpaint_stage.validate_inputs(req.image_id, sec, (image.shape[1], image.shape[0]), MergeResult(section_key=sec.section_key, blocks=blocks),
                                             label, logo, record, cfg_run)
        ip = cfg["inpaint"]
        manifest, msha = manifest_of({
            "stage": "inpaint", "contract_version": CONTRACT_VERSION, "adapter": DOWNSTREAM_ADAPTER_VERSION,
            "image_id": req.image_id, "section": sec.model_dump(mode="json", exclude={"image_path"}), "image_sha256": req.section_image.sha256,
            "blocks": [b.model_dump(mode="json") for b in sorted(blocks, key=lambda b: b.block_order)],
            "label_result_sha256": req.label_result_sha256, "logo_result_sha256": req.logo_result_sha256,
            "logo_record_sha256": req.logo_record_sha256,
            "config": {k: ip[k] for k in ("model", "score_min", "require_text", "dilate_ratio", "dilate_retry")},
            "raster_rule": inpaint_stage.RASTER_RULE,
        })
        check_expected_manifest(ident, msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)
    except (inpaint_stage.InpaintInputError, ValueError) as e:
        return failed_report(ident, "input_invalid", f"⑥ 입력 오류: {e}", implementation=impl)
    except OSError as e:
        return failed_report(ident, "input_invalid", f"섹션 이미지를 열 수 없다: {e}", implementation=impl)

    def fail(kind: str, msg: str, **kw) -> StageReport:
        return failed_report(ident, kind, msg, manifest=manifest, target_count=1, targets=[sec.section_key], implementation=impl, **kw)

    masks = inpaint_stage.build_masks(plan)
    counts = masks.counts
    regions = inpaint_stage.region_diagnostics(plan, masks)
    base = out / req.image_id
    paths = {"delete_mask": base / "inpaint_mask" / f"{sec.section_key}.final.png",
             "protect_mask": base / "inpaint_mask" / f"{sec.section_key}.protect.png",
             "background": base / "inpaint_bg" / f"{sec.section_key}.png"}
    try:
        save_png_verified(paths["delete_mask"], masks.final.astype(np.uint8) * 255)
        save_png_verified(paths["protect_mask"], masks.protect.astype(np.uint8) * 255)
    except Exception as e:  # noqa: BLE001
        return fail("output_invalid", f"마스크 저장 실패: {e.__class__.__name__}: {e}")
    payload = {"section_key": sec.section_key, "status": None, "counts": counts,
               "protected_blocks": [{"block_key": p.block_key, "reason": p.reason} for p in plan.protected_blocks],
               "regions": [{k: r[k] for k in ("region_key", "line_key", "block_key", "kind", "reasons", "score", "text_blank", "final_px")}
                           for r in regions]}
    if not masks.final.any():  # 정상 생략 — 모델 호출 없음, 원본 섹션 배경 참조
        payload["status"] = "unchanged"
        arts = [artifact_of("delete_mask", sec.section_key, paths["delete_mask"]), artifact_of("protect_mask", sec.section_key, paths["protect_mask"])]
        refs = [SourceRef(kind="background", part_key=sec.section_key, ref="section_image", sha256=req.section_image.sha256)]
        return _report(ident, manifest, msha, outcome="skipped", target_count=0, skip_reason="empty_mask", payload=payload, impl=impl,
                       artifacts=arts, source_refs=refs)
    if cancel is not None and cancel.is_set():
        return fail("cancelled", "실행 권한 상실 · 취소 — 모델을 부르지 않았다")
    own = model is None
    try:
        if own:
            try:
                model = (model_factory or inpaint_stage.build_model)(cfg_run)
            except inpaint_stage.ModelNotAvailable as e:
                return fail("dependency_unavailable", f"모델 없음: {e}")
            except Exception as e:  # noqa: BLE001
                return fail("model_call_failed", f"모델 초기화 실패: {e.__class__.__name__}: {e}")
        impl["model"] = model.describe() if hasattr(model, "describe") else None
        try:
            applied = inpaint_stage.apply(image, plan, masks, model)
        except inpaint_stage.InpaintModelError as e:
            if cancel is not None and cancel.is_set():
                return fail("cancelled", f"취소로 추론 중단: {e}")
            return fail("model_call_failed", f"{e}")
        if cancel is not None and cancel.is_set():
            return fail("cancelled", "추론 뒤 실행 권한 상실 — 결과를 인계하지 않는다")
        try:
            save_png_verified(paths["background"], applied.background)
        except Exception as e:  # noqa: BLE001
            return fail("output_invalid", f"배경 저장 실패: {e.__class__.__name__}: {e}")
    finally:
        if own and close_model and model is not None and hasattr(model, "close"):
            try:
                model.close()
            except Exception:  # noqa: BLE001
                pass
    payload["status"] = "inpainted"
    arts = [artifact_of(k, sec.section_key, paths[k]) for k in ("background", "delete_mask", "protect_mask")]
    return _report(ident, manifest, msha, outcome="completed", target_count=1, payload=payload, impl=impl, artifacts=arts)


# ---------------------------------------------------------------------------
# ⑦ 스타일 추출
# ---------------------------------------------------------------------------
def run_style(raw: Any, cfg: dict[str, Any]) -> StageReport:
    ident, req, bad = _begin(raw, "style", StyleRequest)
    if bad:
        return bad
    impl = {"adapter": DOWNSTREAM_ADAPTER_VERSION, "style_schema": "1"}
    try:
        style_stage.validate_config(cfg)
        sec, blocks = _section_inputs(req)
        label, logo, record = _prev(req)
        st = cfg["style"]
        manifest, msha = manifest_of({
            "stage": "style", "contract_version": CONTRACT_VERSION, "adapter": DOWNSTREAM_ADAPTER_VERSION,
            "image_id": req.image_id, "section": sec.model_dump(mode="json", exclude={"image_path"}),
            "image_sha256": req.section_image.sha256,  # 개발 지문(StyleFingerprints)에 없던 이미지 · 설정을 넣는다(5.31)
            "blocks": [b.model_dump(mode="json") for b in sorted(blocks, key=lambda b: b.block_order)],
            "label_result_sha256": req.label_result_sha256, "logo_result_sha256": req.logo_result_sha256,
            "logo_record_sha256": req.logo_record_sha256,
            "config": {k: st[k] for k in ("method", "em_ratio", "align_tolerance")},
        })
        check_expected_manifest(ident, msha)
    except HandoffInputError as e:
        return failed_report(ident, e.kind, str(e), implementation=impl)
    except ValueError as e:
        return failed_report(ident, "config_invalid", str(e), implementation=impl)
    try:
        result = style_stage.run(req.image_id, sec, MergeResult(section_key=sec.section_key, blocks=blocks), label, logo, record, cfg)
    except style_stage.StyleInputError as e:
        return failed_report(ident, "input_invalid", f"⑦ 입력 오류: {e}", manifest=manifest, implementation=impl)
    except Exception as e:  # noqa: BLE001
        return failed_report(ident, "internal", f"{e.__class__.__name__}: {e}", manifest=manifest, implementation=impl)
    targets = [b for b in result.blocks if b.status != "excluded"]
    payload = {
        "section_key": sec.section_key,
        "blocks": [{"block_key": b.block_key, "font_color": b.font_color, "bg_color": b.bg_color, "est_font_px": b.est_font_px,
                    "align": b.align, "align_basis": b.align_basis, "null_reasons": dict(b.null_reasons), "measure_status": b.status,
                    "counts": b.counts.model_dump()} for b in targets],
        "excluded": [{"block_key": b.block_key, "reason": b.excluded_reason} for b in result.blocks if b.status == "excluded"],
        "units": {"color": "#RRGGBB sRGB", "est_font_px": "원본 섹션 이미지 px(반올림 · 배율 없음)"},
        "style_result_sha256": sha256_canonical(result.model_dump(mode="json")),
    }
    if not targets:
        return _report(ident, manifest, msha, outcome="skipped", target_count=0, skip_reason="no_targets", payload=payload, impl=impl)
    return _report(ident, manifest, msha, outcome="completed", target_count=len(targets), payload=payload, impl=impl)
