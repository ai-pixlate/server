"""④ 제품 라벨 판정 — 단독 개발 v1(2026-09-29). pipeline.md 단계표 ④ · 7.4절, open-questions.md #66.

정본(확정): `vlm_relation` — `gemini-3.8-flash` + 섹션 이미지 동봉(긴 변 `label.long_side_px` = 1024), "애매하면 라벨" 편향.
입력 ③ 블록 + ① 섹션 이미지, 출력 블록별 `is_product_label`[계약 2.2]. 라벨로 판정돼도 원문 · 좌표 · 역할은 보존한다.

단독 개발 방침(사용자 승인 2026-09-29, #66 — 운영 API/DB 계약 아님):
- 섹션 하나당 VLM 1회 호출. 섹션 이미지와 판정 대상 블록(임시 ID b1… · 원문 · box_2d)을 함께 보낸다.
- 공백만 있는 블록(`source_ko.strip() == ""`)은 VLM에 보내지 않고 false(basis `blank_text`)를 기록한다. 원시 영역 · 블록은 그대로.
  텍스트가 있는 저신뢰 OCR 블록은 신뢰도만으로 빼지 않는다.
- 블록이 없으면 호출 없이 빈 판정 목록으로 정상 완료, 공백 블록만 있으면 호출 없이 각 블록 false.
- 응답 검증: 보낸 블록마다 정확히 하나의 JSON boolean. 누락 · 중복 · 미등록 ID · 모르는 키 · 잘못된 타입(문자열 · 숫자)은 실패.
- 호출 · 응답 검증 · 재생 불일치 실패는 섹션 전체 실패(`status=failed`, labels=None) — 일부만 성공으로 내보내거나 false로 바꾸지 않는다.
  예외가 아니라 결과로 돌려주므로 여러 섹션 실행은 다음 섹션을 계속 처리한다. 입력 오류(섹션 불일치 · 이미지 열기 · 크기 불일치 · 설정)는 예외다.
- 개발 기준 temperature 0 · 시간 제한 60초 · 애플리케이션 재시도 없음(SDK도 1회 시도, vlm.sdk_info). 운영값 아님(#65).
- 기록 label_debug/<section_key>.json: 입력 · 이미지 · 프롬프트 · 설정 지문, 페이로드, 원응답, 검증 결과, 소요 시간, 사용량. 재생은 vlm.ReplayLabelAssistant.
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

from pipeline.types import BBox, LabelChecked, LabelDecision, LabelResult, Section, TextBlock
from pipeline.vlm import GeminiLabelAssistant, JudgeAssistant, LlmReply, VlmError, VlmReplayMismatch, image_sha256, sha256_text

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY_ENV = "GEMINI_API_KEY"
BIASES = ("label",)  # 정본에 정의된 편향 값은 "label"(애매하면 라벨)뿐이다. 다른 값의 의미는 정하지 않았다
BOX_FORMAT = "box_2d = [ymin, xmin, ymax, xmax], 보낸 이미지 기준 0~1000 정규화 정수"

Recorder = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def resolve_repo_path(p: str) -> Path:
    """상대 경로는 레포 루트(pipeline 패키지의 상위 폴더) 기준(③ L12 · ③-1과 같음)."""
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def validate_config(cfg: dict[str, Any]) -> None:
    """[label] 공통 설정(호출 여부와 무관). 어기면 ValueError."""
    if "label" not in cfg:
        raise ValueError("config에 [label] 표가 없다")
    lb = cfg["label"]
    missing = [k for k in ("long_side_px", "bias") if k not in lb]
    if missing:
        raise ValueError("잘못된 [label] 설정: 없는 키 " + ", ".join(f"label.{k}" for k in missing))
    errors: list[str] = []
    ls = lb["long_side_px"]
    if not (isinstance(ls, int) and not isinstance(ls, bool)) or ls < 64:
        errors.append(f"label.long_side_px={ls!r} — 정수 · 64 이상")
    if lb["bias"] not in BIASES:
        errors.append(f"label.bias={lb['bias']!r} — {BIASES} 중 하나(다른 편향 값은 정의되지 않았다)")
    if errors:
        raise ValueError("잘못된 [label] 설정: " + "; ".join(errors))


def validate_llm_config(cfg: dict[str, Any], *, need_api_key: bool) -> str:
    """VLM 호출 · 재생 모드의 설정 검증(③ · ③-1과 같은 기준). 통과하면 프롬프트 원문을 돌려준다."""
    validate_config(cfg)
    lb = cfg["label"]
    missing = [k for k in ("model", "temperature", "timeout_s", "prompt_path") if k not in lb]
    if missing:
        raise ValueError("잘못된 [label] VLM 설정: 없는 키 " + ", ".join(f"label.{k}" for k in missing))
    errors: list[str] = []
    if not isinstance(lb["model"], str) or not lb["model"].strip():
        errors.append(f"label.model={lb['model']!r} — 비어 있지 않은 문자열")
    t = lb["temperature"]
    if not _finite_number(t) or not 0 <= t <= 2:
        errors.append(f"label.temperature={t!r} — 수치(bool 제외) · 유한값 · 0 이상 2 이하")
    to = lb["timeout_s"]
    if not _finite_number(to) or not to > 0:
        errors.append(f"label.timeout_s={to!r} — 수치(bool 제외) · 유한값 · 0 초과")
    prompt = ""
    pp = lb["prompt_path"]
    if not isinstance(pp, str) or not pp.strip():
        errors.append(f"label.prompt_path={pp!r} — 비어 있지 않은 문자열")
    else:
        path = resolve_repo_path(pp)
        try:
            prompt = path.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"label.prompt_path — 프롬프트가 비어 있다: {path}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"label.prompt_path — 파일을 UTF-8로 읽을 수 없다: {path} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다 — 실제 VLM 호출에 필요하다")
    if errors:
        raise ValueError("잘못된 [label] VLM 설정: " + "; ".join(errors))
    return prompt


def llm_config(cfg: dict[str, Any]) -> dict[str, Any]:
    lb = cfg["label"]
    return {"model": lb["model"], "temperature": lb["temperature"], "timeout_s": lb["timeout_s"]}


def default_assistant(cfg: dict[str, Any]) -> GeminiLabelAssistant:
    c = llm_config(cfg)
    return GeminiLabelAssistant(model=c["model"], temperature=c["temperature"], timeout_s=c["timeout_s"])


# ---------------------------------------------------------------------------
# 입력 · 페이로드
# ---------------------------------------------------------------------------
def input_fingerprint(blocks: list[TextBlock]) -> str:
    """판정 입력 블록 전체(block_order 순, 원문 · 좌표 · 역할 · 원시 OCR 포함)의 SHA-256. 재생 입력 일치 · 입력 불변성 확인용."""
    data = [b.model_dump(mode="json") for b in sorted(blocks, key=lambda b: b.block_order)]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def is_blank(block: TextBlock) -> bool:
    """공백만 있는 블록 — ③ #38 A의 빈 텍스트 정의(`strip() == ""`)와 같다. 빈 문자열 포함."""
    return not block.source_ko.strip()


def prepare_image(section: Section, long_side_px: int) -> tuple[Image.Image, dict[str, Any]]:
    """섹션 이미지를 긴 변 기준으로 줄인다(정본 label.long_side_px). 원본이 작으면 그대로. 크기가 Section 메타데이터와 다르면
    블록 좌표를 이미지에 대응시킬 수 없으므로 입력 오류(ValueError). 파일을 못 열면 OSError(입력 오류) — VLM 실패가 아니다."""
    img = Image.open(section.image_path).convert("RGB")
    ow, oh = img.size
    if (ow, oh) != (section.width, section.height):
        raise ValueError(f"섹션 이미지 크기 {ow}x{oh}가 Section 메타데이터 {section.width}x{section.height}와 다르다: {section.image_path}")
    scale = min(1.0, long_side_px / max(ow, oh))
    if scale < 1.0:
        img = img.resize((max(1, round(ow * scale)), max(1, round(oh * scale))), Image.LANCZOS)
    return img, {"original_size": [ow, oh], "sent_size": list(img.size), "long_side_px": long_side_px, "scale": round(scale, 6)}


def box_2d(bbox: BBox, width: int, height: int) -> list[int]:
    """섹션 로컬 bbox → [ymin, xmin, ymax, xmax], 0~1000 정규화(이미지 크기 기준이라 축소와 무관). 범위 밖은 자른다."""

    def n(v: int, size: int) -> int:
        return max(0, min(1000, round(v * 1000 / size)))

    return [n(bbox.y, height), n(bbox.x, width), n(bbox.y2, height), n(bbox.x2, width)]


def build_payload(section: Section, targets: list[TextBlock]) -> tuple[dict[str, Any], dict[str, str]]:
    """VLM에 보낼 페이로드와 임시 ID → block_key. 원문 · box_2d만 보낸다(역할 · 신뢰도는 보내지 않는다)."""
    ids = {f"b{i + 1}": b.block_key for i, b in enumerate(targets)}
    payload = {
        "section": section.section_key,
        "box_format": BOX_FORMAT,
        "blocks": [{"id": f"b{i + 1}", "text": b.source_ko, "box_2d": box_2d(b.bbox, section.width, section.height)} for i, b in enumerate(targets)],
    }
    return payload, ids


def payload_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 응답 검증
# ---------------------------------------------------------------------------
class LabelResponseError(VlmError):
    """VLM 응답이 응답 검증을 통과하지 못했다. reasons = 어긴 항목 목록. 섹션 전체 실패이며 false로 바꾸지 않는다."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("④ 응답 검증 실패: " + "; ".join(reasons))


class _DuplicateKeys(Exception):
    """JSON 객체 안의 중복 키. json.loads는 기본으로 마지막 값만 남기므로(상충하는 판정이 조용히 사라짐) 파싱 단계에서 막는다."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        super().__init__(f"중복 키 {keys}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    dups = sorted({k for k in keys if keys.count(k) > 1})
    if dups:
        raise _DuplicateKeys(dups)
    return dict(pairs)


def parse_labels(text: str, sent_ids: list[str]) -> dict[str, bool]:
    """응답 검증. 하나라도 어기면 LabelResponseError. 통과하면 임시 ID → bool(보낸 ID 전부).
    JSON 객체 안의 중복 키(예 한 항목에 is_product_label이 두 번)도 거부한다 — 표준 파서가 마지막 값으로 덮어써 상충을 숨기기 때문."""
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except _DuplicateKeys as e:
        raise LabelResponseError([f"JSON 객체에 중복 키 {e.keys}(상충 판정을 마지막 값으로 덮어쓰지 않는다)"]) from e
    except ValueError as e:
        raise LabelResponseError([f"JSON 아님: {e}"]) from e
    if not isinstance(data, dict) or not isinstance(data.get("labels"), list):
        raise LabelResponseError(["최상위 labels 배열이 없다"])
    reasons: list[str] = []
    extra_top = sorted(set(data) - {"labels"})
    if extra_top:
        reasons.append(f"최상위에 모르는 키 {extra_top}")
    seen: dict[str, bool] = {}
    flagged: set[str] = set()  # 응답에 있었지만 형식이 틀린 ID — 누락과 따로 센다
    for i, item in enumerate(data["labels"]):
        if not isinstance(item, dict):
            reasons.append(f"labels[{i}] 객체가 아니다")
            continue
        extra = sorted(set(item) - {"id", "is_product_label"})
        if extra:
            reasons.append(f"labels[{i}] 모르는 키 {extra}")
        bid = item.get("id")
        if not isinstance(bid, str) or bid not in sent_ids:
            reasons.append(f"labels[{i}] 미등록 블록 ID {bid!r}")
            continue
        if bid in seen or bid in flagged:
            reasons.append(f"{bid} 중복 판정")
            continue
        if "is_product_label" not in item:
            reasons.append(f"{bid} is_product_label 없음")
            flagged.add(bid)
            continue
        v = item["is_product_label"]
        if not isinstance(v, bool):  # "true" · 1 · null을 bool로 바꾸지 않는다
            reasons.append(f"{bid} is_product_label이 boolean이 아니다: {v!r}")
            flagged.add(bid)
            continue
        seen[bid] = v
    missing = [x for x in sent_ids if x not in seen and x not in flagged]
    if missing:
        reasons.append(f"판정 누락 {missing}(누락은 false가 아니다)")
    if reasons:
        raise LabelResponseError(reasons)
    return seen


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def json_recorder(debug_dir: Path) -> Recorder:
    """`<debug_dir>/<section_key>.json`에 기록을 쓴다."""

    def write(rec: dict[str, Any]) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{rec['section_key']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    return write


def _emit(recorder: Recorder | None, rec: dict[str, Any], failed: bool) -> None:
    """실패 결과의 기록 저장 오류는 error에 덧붙이고, 성공 결과의 기록 저장 오류는 실행 실패다(기록 없는 결과를 남기지 않는다)."""
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
    section: Section, blocks: list[TextBlock], cfg: dict[str, Any], *,
    llm: JudgeAssistant | None = None, recorder: Recorder | None = None,
) -> LabelResult:
    """④ 한 섹션. llm을 주지 않으면 기본 Gemini 호출자(API 키 필요). 재생은 vlm.ReplayLabelAssistant.
    실패(호출 · 시간 초과 · 응답 검증 · 재생 불일치)는 예외가 아니라 status=failed 결과(labels=None)다."""
    validate_config(cfg)
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    bad = [b.block_key for b in blocks if b.section_key != section.section_key]
    if bad:
        raise ValueError(f"블록의 section_key가 섹션과 다르다: {bad}")
    keys = [b.block_key for b in blocks]
    if len(set(keys)) != len(keys):
        raise ValueError(f"섹션 안에 같은 block_key가 있다: {sorted(k for k in set(keys) if keys.count(k) > 1)}")
    ordered = sorted(blocks, key=lambda b: b.block_order)
    blank = [b for b in ordered if is_blank(b)]
    targets = [b for b in ordered if not is_blank(b)]
    fp = input_fingerprint(blocks)
    rec: dict[str, Any] = {
        "stage": "label", "section_key": section.section_key, "status": None, "blocks": len(blocks),
        "sent_block_keys": [b.block_key for b in targets], "blank_block_keys": [b.block_key for b in blank],
        "input_fingerprint": fp, "started_at": started.isoformat(timespec="seconds"),
    }

    def checked(called: bool) -> LabelChecked:
        return LabelChecked(input_fingerprint=fp, llm_called=called, sent_block_keys=rec["sent_block_keys"],
                            blank_block_keys=rec["blank_block_keys"])

    def done(status: str) -> None:
        rec["status"] = status
        rec["duration_s"] = round(time.perf_counter() - t0, 3)

    def failed(status: str, err: str, *, called: bool) -> LabelResult:
        done(status)
        rec["error"] = err
        _emit(recorder, rec, failed=True)
        return LabelResult(section_key=section.section_key, status="failed", labels=None, checked=checked(called), error=rec["error"])

    check_input = getattr(llm, "check_input", None)  # 재생: 보내지 않는 블록까지 포함한 입력 지문 대조
    if not targets:  # 블록 없음 · 공백 블록만 → 호출하지 않는다
        rec["skip_reason"] = "no_blocks" if not blocks else "blank_only"
        rec["model_config"] = llm_config(cfg) if all(k in cfg["label"] for k in ("model", "temperature", "timeout_s")) else None
        if check_input is not None:
            try:
                check_input(fp)
            except VlmReplayMismatch as e:
                return failed("input_mismatch", str(e), called=False)
        labels = [LabelDecision(block_key=b.block_key, is_product_label=False, basis="blank_text") for b in blank]
        done("skipped")
        rec["result"] = [d.model_dump(mode="json") for d in labels]
        _emit(recorder, rec, failed=False)
        return LabelResult(section_key=section.section_key, status="ok", labels=labels, checked=checked(False))

    prompt = validate_llm_config(cfg, need_api_key=llm is None)
    image, image_meta = prepare_image(section, int(cfg["label"]["long_side_px"]))
    payload, ids = build_payload(section, targets)
    ptext = payload_text(payload)
    rec.update({
        "payload": payload, "payload_sha256": sha256_text(ptext), "prompt_sha256": sha256_text(prompt),
        "image": image_meta, "image_sha256": image_sha256(image), "model_config": llm_config(cfg), "bias": cfg["label"]["bias"],
        "response_text": None, "usage": None, "call_duration_s": None, "validation": None, "error": None,
    })
    if check_input is not None:
        try:
            check_input(fp)
        except VlmReplayMismatch as e:
            return failed("input_mismatch", str(e), called=False)
    assistant = llm if llm is not None else default_assistant(cfg)
    c0 = time.perf_counter()
    try:
        reply: LlmReply = assistant(prompt, ptext, image)
    except VlmReplayMismatch as e:  # 재생 불일치 — 이 섹션만 실패
        rec["call_duration_s"] = round(time.perf_counter() - c0, 3)
        return failed("replay_mismatch", str(e), called=True)
    except VlmError as e:  # 호출 실패 · 시간 초과 — 대체 처리 · 재시도 없음
        rec["call_duration_s"] = round(time.perf_counter() - c0, 3)
        return failed("call_failed", str(e), called=True)
    rec["call_duration_s"] = round(time.perf_counter() - c0, 3)
    rec["response_text"] = reply.text
    rec["usage"] = reply.usage
    try:
        judged = parse_labels(reply.text, list(ids))
    except LabelResponseError as e:
        rec["validation"] = {"ok": False, "reasons": e.reasons}
        return failed("validation_failed", "; ".join(e.reasons), called=True)
    rec["validation"] = {"ok": True, "reasons": []}
    by_key = {ids[k]: v for k, v in judged.items()}
    labels = [
        LabelDecision(block_key=b.block_key, is_product_label=by_key[b.block_key], basis="vlm") if not is_blank(b)
        else LabelDecision(block_key=b.block_key, is_product_label=False, basis="blank_text")
        for b in ordered
    ]
    done("ok")
    rec["result"] = [d.model_dump(mode="json") for d in labels]
    _emit(recorder, rec, failed=False)
    return LabelResult(section_key=section.section_key, status="ok", labels=labels, checked=checked(True))
