"""A+ 섹션 내용 라벨 — 개발용 v1(2026-11-23, 운영 계약 아님. open-questions.md 2.9절 · #93).

상품 하나의 섹션 전체를 **1회 호출**로 보내 섹션마다 내용 라벨(규격 데이터 `labels`의 19종)을 받는다.
라벨은 그 섹션이 7칸 중 어느 칸의 재료가 될지 정하는 데만 쓴다(`slot_candidates`). 저장 필드가 아니다.

방침(참고 변환기 `step3_label.py`와 같은 점 · 다른 점):
- 같은 점: 섹션 이미지 + 그 섹션에서 읽은 글을 함께 보내고, 분류 기준은 분류가이드 v1.4의 19개 라벨이다. 섹션을 한꺼번에 보낸다.
- 다른 점: 모델은 이 레포의 설정(`[aplus_label]`, Gemini)이다. 응답 검증은 ④와 같은 기준으로 엄격하다 — 보낸 섹션마다 정확히 하나,
  누락 · 중복 · 미등록 ID · 모르는 키 · 모르는 라벨은 실패. 가이드의 옵션 종류(opt)와 요약(summary)은 뒤 단계가 쓰지 않아 받지 않는다.
- 섹션의 글은 ③ 블록의 `source_ko`를 `block_order` 순으로 줄바꿈해 붙인 것이다. 글이 없는 섹션도 이미지로 판정하므로 보낸다.
- 실패(호출 · 시간 초과 · 응답 검증)는 예외가 아니라 `status=failed` 결과이며 전체 실패다. 일부 섹션만 성공으로 내보내지 않는다.
  입력 오류(섹션 없음 · 섹션 키 중복 · 블록의 섹션 불일치 · 이미지 열기 · 크기 불일치 · 설정)는 예외다.
- 기록: `recorder`를 주면 입력 · 이미지 · 프롬프트 · 설정 지문, 페이로드, 원응답, 검증 결과, 소요 시간, 사용량을 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from pipeline.aplus.spec import SLOTS, AplusSpec
from pipeline.types import AplusLabelChecked, AplusLabelResult, AplusSectionLabel, Section, TextBlock
from pipeline.vlm import GeminiSectionLabelAssistant, LlmReply, MultiImageAssistant, VlmError, image_sha256, sha256_text

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY_ENV = "GEMINI_API_KEY"
CONFIDENCES = ("high", "low")
ITEM_KEYS = {"id", "label", "sub", "confidence", "reason"}

Recorder = Callable[[dict[str, Any]], None]
SectionInput = tuple[Section, list[TextBlock]]


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_config(cfg: dict[str, Any], *, need_api_key: bool, table: str = "aplus_label") -> str:
    """[aplus_label](또는 같은 키를 쓰는 `table`) 설정 검증. 통과하면 프롬프트 원문을 돌려준다. 어기면 ValueError."""
    if table not in cfg:
        raise ValueError(f"config에 [{table}] 표가 없다")
    c = cfg[table]
    missing = [k for k in ("model", "long_side_px", "temperature", "timeout_s", "prompt_path") if k not in c]
    if missing:
        raise ValueError(f"잘못된 [{table}] 설정: 없는 키 " + ", ".join(f"{table}.{k}" for k in missing))
    errors: list[str] = []
    if not isinstance(c["model"], str) or not c["model"].strip():
        errors.append(f"{table}.model={c['model']!r} — 비어 있지 않은 문자열")
    ls = c["long_side_px"]
    if not (isinstance(ls, int) and not isinstance(ls, bool)) or ls < 64:
        errors.append(f"{table}.long_side_px={ls!r} — 정수 · 64 이상")
    if not _finite_number(c["temperature"]) or not 0 <= c["temperature"] <= 2:
        errors.append(f"{table}.temperature={c['temperature']!r} — 수치(bool 제외) · 유한값 · 0 이상 2 이하")
    if not _finite_number(c["timeout_s"]) or not c["timeout_s"] > 0:
        errors.append(f"{table}.timeout_s={c['timeout_s']!r} — 수치(bool 제외) · 유한값 · 0 초과")
    prompt = ""
    pp = c["prompt_path"]
    if not isinstance(pp, str) or not pp.strip():
        errors.append(f"{table}.prompt_path={pp!r} — 비어 있지 않은 문자열")
    else:
        path = Path(pp) if Path(pp).is_absolute() else REPO_ROOT / pp
        try:
            prompt = path.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"{table}.prompt_path — 프롬프트가 비어 있다: {path}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"{table}.prompt_path — 파일을 UTF-8로 읽을 수 없다: {path} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다 — 실제 호출에 필요하다")
    if errors:
        raise ValueError(f"잘못된 [{table}] 설정: " + "; ".join(errors))
    return prompt


def llm_config(cfg: dict[str, Any], table: str = "aplus_label") -> dict[str, Any]:
    c = cfg[table]
    return {"model": c["model"], "temperature": c["temperature"], "timeout_s": c["timeout_s"]}


# ---------------------------------------------------------------------------
# 입력 · 페이로드
# ---------------------------------------------------------------------------
def section_text(blocks: list[TextBlock]) -> str:
    """섹션의 글 = 블록 원문을 block_order 순으로 줄바꿈해 붙인 것. 공백만 있는 블록은 뺀다."""
    return "\n".join(b.source_ko for b in sorted(blocks, key=lambda b: b.block_order) if b.source_ko.strip())


def input_fingerprint(sections: list[SectionInput]) -> str:
    data = [{"section": s.model_dump(mode="json", exclude={"image_path"}),
             "blocks": [b.model_dump(mode="json") for b in sorted(bs, key=lambda b: b.block_order)]} for s, bs in sections]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def prepare_image(section: Section, long_side_px: int) -> tuple[Image.Image, dict[str, Any]]:
    """섹션 이미지를 긴 변 기준으로 줄인다. 크기가 Section 메타데이터와 다르면 입력 오류(ValueError), 못 열면 OSError."""
    img = Image.open(section.image_path).convert("RGB")
    ow, oh = img.size
    if (ow, oh) != (section.width, section.height):
        raise ValueError(f"섹션 이미지 크기 {ow}x{oh}가 Section 메타데이터 {section.width}x{section.height}와 다르다: {section.image_path}")
    scale = min(1.0, long_side_px / max(ow, oh))
    if scale < 1.0:
        img = img.resize((max(1, round(ow * scale)), max(1, round(oh * scale))), Image.LANCZOS)
    return img, {"original_size": [ow, oh], "sent_size": list(img.size), "scale": round(scale, 6)}


def build_payload(sections: list[SectionInput]) -> tuple[dict[str, Any], dict[str, str]]:
    """보낼 페이로드와 임시 ID(s1 …) → section_key."""
    ids = {f"s{i + 1}": s.section_key for i, (s, _) in enumerate(sections)}
    payload = {"sections": [{"id": f"s{i + 1}", "text": section_text(bs)} for i, (_, bs) in enumerate(sections)]}
    return payload, ids


# ---------------------------------------------------------------------------
# 응답 검증
# ---------------------------------------------------------------------------
class AplusLabelResponseError(VlmError):
    """응답이 검증을 통과하지 못했다. reasons = 어긴 항목 목록. 전체 실패이며 OTHER 등으로 바꾸지 않는다."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("A+ 섹션 라벨 응답 검증 실패: " + "; ".join(reasons))


class _DuplicateKeys(Exception):
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"중복 키 {keys}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    dups = sorted({k for k in keys if keys.count(k) > 1})
    if dups:
        raise _DuplicateKeys(dups)
    return dict(pairs)


def parse_labels(text: str, sent_ids: list[str], codes: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """응답 검증. 하나라도 어기면 AplusLabelResponseError. 통과하면 임시 ID → {label, sub, confidence, reason}(보낸 ID 전부)."""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeys as e:
        raise AplusLabelResponseError([f"JSON 객체에 중복 키 {e.keys}"]) from e
    except ValueError as e:
        raise AplusLabelResponseError([f"JSON 아님: {e}"]) from e
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise AplusLabelResponseError(["최상위 sections 배열이 없다"])
    reasons: list[str] = []
    extra_top = sorted(set(data) - {"sections"})
    if extra_top:
        reasons.append(f"최상위에 모르는 키 {extra_top}")
    seen: dict[str, dict[str, Any]] = {}
    flagged: set[str] = set()
    for i, item in enumerate(data["sections"]):
        if not isinstance(item, dict):
            reasons.append(f"sections[{i}] 객체가 아니다")
            continue
        sid = item.get("id")
        if not isinstance(sid, str) or sid not in sent_ids:
            reasons.append(f"sections[{i}] 미등록 섹션 ID {sid!r}")
            continue
        if sid in seen or sid in flagged:
            reasons.append(f"{sid} 중복 분류")
            continue
        bad: list[str] = []
        extra = sorted(set(item) - ITEM_KEYS)
        if extra:
            bad.append(f"모르는 키 {extra}")
        missing = sorted(ITEM_KEYS - set(item))
        if missing:
            bad.append(f"없는 키 {missing}")
        label, sub, conf, reason = item.get("label"), item.get("sub"), item.get("confidence"), item.get("reason")
        if "label" in item and label not in codes:
            bad.append(f"모르는 라벨 {label!r}")
        if "sub" in item:
            if not isinstance(sub, list) or any(x not in codes for x in sub):
                bad.append(f"sub가 라벨 코드 배열이 아니다: {sub!r}")
            elif len(sub) > 2 or len(set(sub)) != len(sub) or label in sub:
                bad.append(f"sub는 주 라벨과 다른 코드 0~2개여야 한다: {sub!r}")
        if "confidence" in item and conf not in CONFIDENCES:
            bad.append(f"confidence {conf!r} — {CONFIDENCES} 중 하나")
        if "reason" in item and (not isinstance(reason, str) or not reason.strip()):
            bad.append("reason이 비어 있다")
        if bad:
            reasons.append(f"{sid} " + " · ".join(bad))
            flagged.add(sid)
            continue
        seen[sid] = {"label": label, "sub": list(sub), "confidence": conf, "reason": reason}
    missing_ids = [x for x in sent_ids if x not in seen and x not in flagged]
    if missing_ids:
        reasons.append(f"분류 누락 {missing_ids}(누락은 OTHER가 아니다)")
    if reasons:
        raise AplusLabelResponseError(reasons)
    return seen


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def json_recorder(path: Path) -> Recorder:
    """기록 한 건을 `path`(JSON 파일)에 쓴다."""

    def write(rec: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    return write


def _emit(recorder: Recorder | None, rec: dict[str, Any], failed: bool) -> None:
    if recorder is None:
        return
    try:
        recorder(rec)
    except Exception as w:  # noqa: BLE001
        if failed:
            rec["error"] = f"{rec.get('error')} (기록 저장 실패: {w.__class__.__name__}: {w})"
        else:
            raise


def run(
    sections: list[SectionInput], spec: AplusSpec, cfg: dict[str, Any], *,
    llm: MultiImageAssistant | None = None, recorder: Recorder | None = None,
) -> AplusLabelResult:
    """상품 하나의 섹션 전체(페이지 순서)를 1회 호출로 분류한다. llm을 주지 않으면 기본 Gemini 호출자(API 키 필요)."""
    prompt = validate_config(cfg, need_api_key=llm is None)
    if not sections:
        raise ValueError("분류할 섹션이 없다")
    keys = [s.section_key for s, _ in sections]
    if len(set(keys)) != len(keys):
        raise ValueError(f"같은 section_key가 두 번 있다: {sorted(k for k in set(keys) if keys.count(k) > 1)}")
    for s, bs in sections:
        bad = [b.block_key for b in bs if b.section_key != s.section_key]
        if bad:
            raise ValueError(f"블록의 section_key가 섹션 {s.section_key}와 다르다: {bad}")
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    codes = tuple(spec.raw["labels"])
    fp = input_fingerprint(sections)
    long_side = int(cfg["aplus_label"]["long_side_px"])
    images: list[tuple[str, Image.Image]] = []
    image_meta: dict[str, Any] = {}
    payload, ids = build_payload(sections)
    for sid, (s, _) in zip(ids, sections):
        img, meta = prepare_image(s, long_side)
        images.append((sid, img))
        image_meta[sid] = dict(meta, section_key=s.section_key, sha256=image_sha256(img))
    ptext = json.dumps(payload, ensure_ascii=False, indent=2)
    rec: dict[str, Any] = {
        "stage": "aplus_label", "status": None, "sections": ids, "input_fingerprint": fp, "spec_version": spec.version,
        "spec_sha256": spec.sha256, "started_at": started.isoformat(timespec="seconds"), "payload": payload,
        "payload_sha256": sha256_text(ptext), "prompt_sha256": sha256_text(prompt), "images": image_meta, "long_side_px": long_side,
        "model_config": llm_config(cfg), "response_text": None, "usage": None, "call_duration_s": None, "validation": None, "error": None,
    }
    checked = AplusLabelChecked(input_fingerprint=fp, spec_version=spec.version, llm_called=True, sent_section_keys=keys)

    def failed(status: str, err: str) -> AplusLabelResult:
        rec["status"] = status
        rec["duration_s"] = round(time.perf_counter() - t0, 3)
        rec["error"] = err
        _emit(recorder, rec, failed=True)
        return AplusLabelResult(status="failed", labels=None, checked=checked, error=rec["error"])

    c = llm_config(cfg)
    assistant = llm if llm is not None else GeminiSectionLabelAssistant(model=c["model"], temperature=c["temperature"],
                                                                         timeout_s=c["timeout_s"], labels=list(codes))
    c0 = time.perf_counter()
    try:
        reply: LlmReply = assistant(prompt, ptext, images)
    except VlmError as e:  # 호출 실패 · 시간 초과 — 대체 처리 · 재시도 없음
        rec["call_duration_s"] = round(time.perf_counter() - c0, 3)
        return failed("call_failed", str(e))
    rec["call_duration_s"] = round(time.perf_counter() - c0, 3)
    rec["response_text"] = reply.text
    rec["usage"] = reply.usage
    try:
        judged = parse_labels(reply.text, list(ids), codes)
    except AplusLabelResponseError as e:
        rec["validation"] = {"ok": False, "reasons": e.reasons}
        return failed("validation_failed", "; ".join(e.reasons))
    rec["validation"] = {"ok": True, "reasons": []}
    labels = [AplusSectionLabel(section_key=ids[sid], **judged[sid]) for sid in ids]
    rec["status"] = "ok"
    rec["duration_s"] = round(time.perf_counter() - t0, 3)
    rec["result"] = [x.model_dump(mode="json") for x in labels]
    _emit(recorder, rec, failed=False)
    return AplusLabelResult(status="ok", labels=labels, checked=checked)


# ---------------------------------------------------------------------------
# 라벨 → 7칸 후보
# ---------------------------------------------------------------------------
def slot_candidates(labels: list[AplusSectionLabel], spec: AplusSpec) -> dict[int, list[str]]:
    """칸마다 글 재료가 될 수 있는 섹션(페이지 순서). 주 라벨의 칸만 본다 — 한 섹션은 한 칸에만 들어간다.
    7칸 밖 라벨(SOCIAL · FAQ · LOGO · OTHER 등)의 섹션은 어느 칸에도 들어가지 않는다."""
    out: dict[int, list[str]] = {n: [] for n in SLOTS}
    for x in labels:
        slot = spec.slot_of_label(x.label)
        if slot is not None:
            out[slot].append(x.section_key)
    return out
