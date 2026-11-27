"""A+ 압축(2차 번역) — 템플릿 한도를 넘은 글 칸의 문구를 한도 안으로 줄인다. 개발용 v1(2026-11-26, 운영 계약 아님. open-questions.md #94).

**고쳐 쓰는 것은 모델, 세는 것과 채택은 코드**(사용자 결정 2026-11-26):
- 자리: 7칸 구성 다음이다. 받는 것은 7칸 구성 결과(`AplusPlan`)뿐이다 — 블록 목록 · 이미지 · 한국어 원문은 받지 않는다
  (문구는 1차 번역에서만 온다. 한국어 원문에서 영어를 새로 쓰지 않는다 — aplus.md 4.1).
- 선별을 다시 하지 않는다. 7칸 구성이 한도 직전에서 뺀 블록을 다시 넣지 않고, 칸의 블록 구성 · 출처(`origin_block_keys`)도 바꾸지 않는다.
- 대상: 템플릿 한도(`char_limits`)는 권장 길이다(데이터 담당 답변 2026-11-26 — 지켜야 하는 것은 가이드 상한이다). 1차 문구가
  「넘으면 안 되는 길이」(권장 길이 × `aplus_compress.over_ratio`, 가이드 상한보다 크면 가이드 상한)를 넘는 글 칸만 대상이다
  (사용자 결정 2026-11-26). 그 안에 드는 칸은 권장 길이를 넘어도 그대로 둔다. `label` 칸은 블록에서 오지 않으므로 대상이 아니다.
- 호출: 상품당 1회, 글만 보낸다(이미지 없음). 대상이 있는 칸(모듈)마다 그 칸의 글 전부를 문맥으로 함께 보낸다.
  채택되지 않은 칸만 실제 글자 수 · 문제와 함께 다시 묻는다(`aplus_compress.max_retries`회까지).
- 채택: 글자 수는 코드가 센다. 답이 넘으면 안 되는 길이 이하이고, 비어 있지 않고, 1차 문구에 없던 숫자가 없을 때만 문구를 바꾼다.
  그 길이 이하면 1차 문구보다 반드시 짧으므로 길이 비교는 따로 하지 않는다. 권장 길이는 모델에 목표로만 알려 준다 —
  권장 길이에 못 미치게 줄여도 넘으면 안 되는 길이 안이면 채택한다.
- 규제 반영(사용자 결정 2026-11-26): 적용 분류의 규제 행(`regulation`)을 받으면 allowed가 아닌 행의 영어 표현을 `avoid`로 모델에 주고,
  답에 1차 문구에 없던 규제 매칭이 새로 생기면 채택하지 않는다(`new_regulated` — 다시 묻는다). 매칭은 원본 규격 유지 경로의 번역문 규칙 검사
  (`stages.text_check.check_block`)와 같다. 1차 문구에 원래 있던 규제 표현은 고치지 않는다 — 압축의 일은 줄이는 것이다.
  규제 행을 받지 못하면 반영하지 않고 그 사실을 남긴다(`checked.regulation_applied=False`). 번역문 LLM 규제 판정은 하지 않는다.
- 못 맞춘 칸은 1차 번역 그대로 둔다(PM 결정 2026-11-24 — aplus.md 4.4). 1차 번역이 가이드 상한까지 넘는 칸도 같다 — 새 규칙을 만들지 않고
  기록에 한도들을 남긴다(`AplusCompressedField.max_chars` 권장 길이 · `allowed_chars` 넘으면 안 되는 길이 · `guide_max_chars`).
- 어느 칸의 문구가 바뀌었는지는 결과의 `fields`(`outcome`)로만 안다. `AplusText` · `AplusModule`의 모양은 바꾸지 않는다(인계 형식은 #97).

첫 호출의 실패(호출 · 시간 초과 · 응답 구조)는 예외가 아니라 `status=failed` 결과다 — 호출자는 7칸 구성 결과를 그대로 쓴다.
규제 사전 데이터 오류(같은 구간의 금지/허용 충돌 · 빈 패턴)도 전체 실패다 — 통과로 바꾸지 않는다.
다시 묻는 호출이 실패하면 그때까지 채택된 칸은 살리고 남은 칸을 1차 번역으로 둔다(`reason=call_failed`). 입력 오류는 예외다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pipeline.aplus import content
from pipeline.aplus.label import API_KEY_ENV, REPO_ROOT, Recorder, _emit, _finite_number, llm_config
from pipeline.aplus.plan import normalize_text
from pipeline.aplus.spec import AplusSpec
from pipeline.matching import MatchConflictError, PatternError
from pipeline.stages import text_check
from pipeline.types import (AplusCompressChecked, AplusCompressedField, AplusCompressResult, AplusModule, AplusPlan, AplusSlotPlan,
                            AplusText)
from pipeline.vlm import GeminiMultiImageAssistant, LlmReply, MultiImageAssistant, VlmError, sha256_text

TABLE = "aplus_compress"
SKIP_FIELDS = ("label",)  # 블록에서 오지 않는 칸(템플릿마다 정해진 값)
RETRYABLE = ("over_limit", "new_number", "new_regulated", "blank")  # 다시 묻는 사유. declined(모델이 줄일 수 없다고 답함)는 다시 묻지 않는다
_ITEM_FIELD = re.compile(r"^items\[(\d+)\]\.(title|body)$")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def validate_config(cfg: dict[str, Any], *, need_api_key: bool) -> str:
    """[aplus_compress] 설정 검증. 통과하면 프롬프트 원문을 돌려준다. 어기면 ValueError."""
    if TABLE not in cfg:
        raise ValueError(f"config에 [{TABLE}] 표가 없다")
    c = cfg[TABLE]
    missing = [k for k in ("model", "temperature", "timeout_s", "max_retries", "over_ratio", "prompt_path") if k not in c]
    if missing:
        raise ValueError(f"잘못된 [{TABLE}] 설정: 없는 키 " + ", ".join(f"{TABLE}.{k}" for k in missing))
    errors: list[str] = []
    if not isinstance(c["model"], str) or not c["model"].strip():
        errors.append(f"{TABLE}.model={c['model']!r} — 비어 있지 않은 문자열")
    if not _finite_number(c["temperature"]) or not 0 <= c["temperature"] <= 2:
        errors.append(f"{TABLE}.temperature={c['temperature']!r} — 수치(bool 제외) · 유한값 · 0 이상 2 이하")
    if not _finite_number(c["timeout_s"]) or not c["timeout_s"] > 0:
        errors.append(f"{TABLE}.timeout_s={c['timeout_s']!r} — 수치(bool 제외) · 유한값 · 0 초과")
    mr = c["max_retries"]
    if not (isinstance(mr, int) and not isinstance(mr, bool)) or mr < 0:
        errors.append(f"{TABLE}.max_retries={mr!r} — 정수 · 0 이상")
    if not _finite_number(c["over_ratio"]) or not c["over_ratio"] >= 1:
        errors.append(f"{TABLE}.over_ratio={c['over_ratio']!r} — 수치(bool 제외) · 유한값 · 1 이상")
    prompt = ""
    pp = c["prompt_path"]
    if not isinstance(pp, str) or not pp.strip():
        errors.append(f"{TABLE}.prompt_path={pp!r} — 비어 있지 않은 문자열")
    else:
        path = Path(pp) if Path(pp).is_absolute() else REPO_ROOT / pp
        try:
            prompt = path.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"{TABLE}.prompt_path — 프롬프트가 비어 있다: {path}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"{TABLE}.prompt_path — 파일을 UTF-8로 읽을 수 없다: {path} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다 — 실제 호출에 필요하다")
    if errors:
        raise ValueError(f"잘못된 [{TABLE}] 설정: " + "; ".join(errors))
    return prompt


# ---------------------------------------------------------------------------
# 대상 · 글 칸 읽기/바꾸기
# ---------------------------------------------------------------------------
@dataclass
class Target:
    """압축 대상 글 칸 하나와 그 진행 상태. `id`는 호출 안에서만 쓰는 임시 ID(`c1` …)."""

    id: str
    slot: int
    check: content.LimitCheck
    primary: str
    reason: str | None = None  # 마지막 판정(AplusCompressedField.reason). None = 아직 묻지 않음
    answer: str | None = None  # 모델의 마지막 답(정리한 문구)
    attempts: int = 0
    tried: list[dict[str, Any]] = dc_field(default_factory=list)  # 채택되지 않은 답(다시 물을 때 보낸다)
    primary_hits: frozenset[tuple[str, str]] = frozenset()  # 1차 문구에 이미 있는 규제 매칭(규제 행을 받았을 때만)
    allowed: int = 0  # 넘으면 안 되는 길이(allowed_chars). 답은 이 길이 이하여야 채택된다

    @property
    def field(self) -> str:
        return self.check.field


def get_text(m: AplusModule, name: str) -> AplusText:
    """글 칸 하나(`headline` · `items[2].title` 형식). 없는 칸이면 KeyError."""
    hit = _ITEM_FIELD.match(name)
    value = getattr(m.items[int(hit.group(1))], hit.group(2)) if hit else getattr(m, name) if name in content.TEXT_FIELDS else None
    if value is None:
        raise KeyError(f"없는 글 칸 {name!r}")
    return value


def replace_text(m: AplusModule, name: str, text: str) -> AplusModule:
    """글 칸 하나의 문구만 바꾼 새 모듈. 출처 블록(`origin_block_keys`)은 그대로 둔다."""
    new = get_text(m, name).model_copy(update={"text": text})
    hit = _ITEM_FIELD.match(name)
    if not hit:
        return m.model_copy(update={name: new})
    i = int(hit.group(1))
    items = list(m.items)
    items[i] = items[i].model_copy(update={hit.group(2): new})
    return m.model_copy(update={"items": items})


def allowed_chars(c: content.LimitCheck, over_ratio: float) -> int:
    """넘으면 안 되는 길이 — 권장 길이(템플릿 한도) × `over_ratio`(내림). 가이드 상한보다 크면 가이드 상한이다."""
    n = math.floor(c.max_chars * over_ratio + 1e-9)
    return min(n, c.guide_max_chars) if c.guide_max_chars is not None else n


def find_targets(slots: list[AplusSlotPlan], spec: AplusSpec, over_ratio: float = 1.0) -> list[Target]:
    """1차 문구가 넘으면 안 되는 길이(`allowed_chars`)를 넘는 글 칸(칸 순서 → 칸 안의 자리 순). `label` 칸은 뺀다.
    `over_ratio=1`이면 권장 길이(템플릿 한도)를 넘은 칸 전부다."""
    out: list[Target] = []
    for s in slots:
        if s.module is None:
            continue
        for c in content.limit_excesses(s.module, spec):
            if c.field in SKIP_FIELDS:
                continue
            allowed = allowed_chars(c, over_ratio)
            if c.chars <= allowed:
                continue
            out.append(Target(id=f"c{len(out) + 1}", slot=s.slot, check=c, primary=get_text(s.module, c.field).text, allowed=allowed))
    return out


def numbers(text: str) -> set[str]:
    """문구 안의 숫자 토큰(자릿수 구분 쉼표는 뺀다). `892.35%` → `892.35`, `SPF50+` → `50`, `1,000` → `1000`."""
    return {m.replace(",", "") for m in _NUMBER.findall(text)}


def avoid_list(rows: Sequence[text_check.RegRow]) -> list[str]:
    """모델에 주는 `avoid` — allowed가 아닌 규제 행의 영어 표현(행 순서 · 중복 없이)."""
    out: list[str] = []
    for r in rows:
        if r.verdict_status == "allowed":
            continue
        for p in r.variant_en:
            if p not in out:
                out.append(p)
    return out


def regulated_hits(text: str, rows: Sequence[text_check.RegRow]) -> frozenset[tuple[str, str]]:
    """문구의 규제 매칭 — (규제 항목 ID, 일치한 사전 표현). 번역문 규칙 검사(`text_check.check_block`)의 경고 후보와 같다
    (허용 표현이 감싼 매칭 · 포함된 짧은 매칭은 빠진다). 사전 데이터 오류는 MatchConflictError · PatternError."""
    r = text_check.check_block("aplus_compress", 0, text, "", list(rows), [])
    live = {(f["external_id"], f["start"], f["end"]) for f in r["flags"] if f["type"] == "regulatory"}
    return frozenset((m["external_id"], p) for m in r["regulatory"]["matches"]
                     if (m["external_id"], m["start"], m["end"]) in live for p in m["patterns"])


# ---------------------------------------------------------------------------
# 페이로드 · 응답
# ---------------------------------------------------------------------------
def response_schema() -> dict[str, Any]:
    item = {"type": "object",
            "properties": {"id": {"type": "string"}, "failed": {"type": "boolean"}, "text": {"type": "string", "nullable": True},
                           "reason": {"type": "string", "nullable": True}},
            "required": ["id", "failed"]}
    return {"type": "object", "properties": {"targets": {"type": "array", "items": item}}, "required": ["targets"]}


def build_payload(modules: dict[int, AplusModule], pending: list[Target], spec: AplusSpec, avoid: Sequence[str] = ()) -> dict[str, Any]:
    """대상이 있는 칸마다 그 칸의 글 전부(대상에는 `target` · `recommended_chars` 권장 길이 · `max_chars` 넘으면 안 되는 길이,
    앞선 답이 있으면 `tried`). `modules`는 지금 문구 기준이다.
    `avoid`가 있으면 덧붙인다(없으면 키 자체가 없다 — 규제 행을 받지 않았을 때의 페이로드와 같다)."""
    by_slot: dict[int, dict[str, Target]] = {}
    for t in pending:
        by_slot.setdefault(t.slot, {})[t.field] = t
    slots = []
    for n in sorted(by_slot):
        m = modules[n]
        texts = []
        for c in content.limit_checks(m, spec):
            entry: dict[str, Any] = {"field": c.field, "text": get_text(m, c.field).text, "chars": c.chars}
            t = by_slot[n].get(c.field)
            if t is not None:
                entry.update(target=t.id, recommended_chars=t.check.max_chars, max_chars=t.allowed)
                if t.tried:
                    entry["tried"] = t.tried
            texts.append(entry)
        slots.append({"slot": n, "name": spec.raw["slots"][str(n)]["name_ko"], "template": m.template, "texts": texts})
    payload: dict[str, Any] = {"slots": slots}
    if avoid:
        payload["avoid"] = list(avoid)
    return payload


class AplusCompressResponseError(VlmError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("A+ 압축 응답 검증 실패: " + "; ".join(reasons))


def parse_answers(text: str, sent_ids: list[str]) -> dict[str, dict[str, Any]]:
    """임시 ID → {failed, text, reason}(보낸 ID 전부). 구조가 틀리면 AplusCompressResponseError — 내용(한도 · 숫자)은 여기서 보지 않는다."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise AplusCompressResponseError([f"JSON 아님: {e}"]) from e
    if not isinstance(data, dict) or not isinstance(data.get("targets"), list):
        raise AplusCompressResponseError(["최상위 targets 배열이 없다"])
    reasons: list[str] = []
    out: dict[str, dict[str, Any]] = {}
    for i, item in enumerate(data["targets"]):
        tid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(tid, str) or tid not in sent_ids:
            reasons.append(f"targets[{i}] 보낸 적 없는 ID {tid!r}")
            continue
        if tid in out:
            reasons.append(f"{tid} 중복")
            continue
        failed, answer, reason = item.get("failed", False), item.get("text"), item.get("reason")
        if not isinstance(failed, bool):
            reasons.append(f"{tid} failed가 true · false가 아니다: {failed!r}")
        elif not failed and not isinstance(answer, str):
            reasons.append(f"{tid} text가 문자열이 아니다: {answer!r}")
        elif reason is not None and not isinstance(reason, str):
            reasons.append(f"{tid} reason이 문자열 · null이 아니다")
        out[tid] = {"failed": failed, "text": answer if isinstance(answer, str) else None, "reason": reason}
    missing = [x for x in sent_ids if x not in out]
    if missing:
        reasons.append(f"답 누락 {missing}")
    if reasons:
        raise AplusCompressResponseError(reasons)
    return out


def judge(t: Target, answer: dict[str, Any], rows: Sequence[text_check.RegRow] | None = None) -> None:
    """답 하나를 판정해 `t.reason` · `t.answer`에 남긴다. 채택되지 않은 답은 `t.tried`에 더한다.
    `rows`(규제 행)가 있으면 1차 문구에 없던 규제 매칭이 답에 새로 생겼는지도 본다(`new_regulated`).
    `t.answer`는 이번 답으로 바꾼다 — 줄일 수 없다는 답 · 빈 답이면 앞선 시도의 문구를 지운다(기록의 `answer_text`는 마지막 답이다)."""
    t.answer = None
    if answer["failed"]:
        t.reason = "declined"
        return
    text = normalize_text(answer["text"])
    chars = content.count_chars(text)
    if not text:
        t.reason = "blank"
        t.tried.append({"text": "", "chars": 0, "problem": "blank"})
        return
    t.answer = text
    if chars > t.allowed:
        t.reason = "over_limit"
    elif numbers(text) - numbers(t.primary):
        t.reason = "new_number"
    elif rows is not None and regulated_hits(text, rows) - t.primary_hits:
        t.reason = "new_regulated"
    else:
        t.reason = "fit"
        return
    t.tried.append({"text": text, "chars": chars, "problem": t.reason})


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def input_fingerprint(slots: list[AplusSlotPlan]) -> str:
    data = [s.model_dump(mode="json") for s in slots]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def _record(t: Target) -> AplusCompressedField:
    fit = t.reason == "fit"
    return AplusCompressedField(
        slot=t.slot, field=t.field, origin_block_keys=list(t.check.origin_block_keys), primary_text=t.primary, primary_chars=t.check.chars,
        max_chars=t.check.max_chars, allowed_chars=t.allowed, guide_max_chars=t.check.guide_max_chars, outcome="compressed" if fit else "kept", reason=t.reason,
        answer_text=t.answer, answer_chars=content.count_chars(t.answer) if t.answer is not None else None, attempts=t.attempts)


def run(plan: AplusPlan, spec: AplusSpec, cfg: dict[str, Any], *, regulation: Sequence[text_check.RegRow] | None = None,
        llm: MultiImageAssistant | None = None, recorder: Recorder | None = None) -> AplusCompressResult:
    """상품 하나의 압축. `plan`은 성공한 7칸 구성 결과다. 대상이 없으면 모델을 부르지 않고 받은 칸을 그대로 돌려준다.
    `regulation` = 적용 분류의 규제 행(None = 받지 못함 — 규제를 반영하지 않는다)."""
    if plan.status != "ok" or plan.slots is None:
        raise ValueError("실패한 7칸 구성 결과는 압축할 수 없다")
    if plan.checked.spec_version != spec.version:
        raise ValueError(f"7칸 구성의 규격 버전 {plan.checked.spec_version!r}이 지금 규격 {spec.version!r}과 다르다")
    prompt = validate_config(cfg, need_api_key=False)  # 대상 고르기 전에 설정(over_ratio)부터 본다
    over_ratio = float(cfg[TABLE]["over_ratio"])
    targets = find_targets(plan.slots, spec, over_ratio)
    prompt = validate_config(cfg, need_api_key=llm is None and bool(targets))
    max_retries = int(cfg[TABLE]["max_retries"])
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    fp = input_fingerprint(plan.slots)
    calls: list[dict[str, Any]] = []
    rec: dict[str, Any] = {
        "stage": TABLE, "status": None, "input_fingerprint": fp, "spec_version": spec.version, "spec_sha256": spec.sha256,
        "started_at": started.isoformat(timespec="seconds"), "prompt_sha256": sha256_text(prompt), "model_config": llm_config(cfg, TABLE),
        "max_retries": max_retries, "over_ratio": over_ratio,
        "targets": [{"id": t.id, "slot": t.slot, "field": t.field, "allowed_chars": t.allowed} for t in targets], "calls": calls, "error": None,
        "regulation": {"applied": regulation is not None, "rows": len(regulation) if regulation is not None else None},
    }

    def checked() -> AplusCompressChecked:
        return AplusCompressChecked(input_fingerprint=fp, spec_version=spec.version, llm_called=bool(calls), calls=len(calls),
                                    regulation_applied=regulation is not None, over_ratio=over_ratio)

    def regulation_failed(e: Exception) -> AplusCompressResult:  # 사전 데이터 오류 — 통과로 바꾸지 않는다. 문구는 하나도 바꾸지 않는다
        rec.update(status="regulation_failed", duration_s=round(time.perf_counter() - t0, 3),
                   error=f"규제 사전 데이터 오류: {e.__class__.__name__}: {e}")
        _emit(recorder, rec, failed=True)
        return AplusCompressResult(status="failed", slots=None, checked=checked(), error=rec["error"])

    modules = {s.slot: s.module for s in plan.slots if s.module is not None}
    avoid: list[str] = []
    if targets and regulation is not None:
        avoid = avoid_list(regulation)
        rec["regulation"]["avoid"] = avoid
        try:
            for t in targets:
                t.primary_hits = regulated_hits(t.primary, regulation)
        except (MatchConflictError, PatternError) as e:
            return regulation_failed(e)
    if targets:
        c = llm_config(cfg, TABLE)
        assistant = llm if llm is not None else GeminiMultiImageAssistant(model=c["model"], temperature=c["temperature"],
                                                                          timeout_s=c["timeout_s"], response_schema=response_schema())
        pending = targets
        for attempt in range(1, max_retries + 2):
            payload = build_payload(modules, pending, spec, avoid)
            ptext = json.dumps(payload, ensure_ascii=False, indent=1)
            call: dict[str, Any] = {"attempt": attempt, "sent": [t.id for t in pending], "payload": payload, "payload_sha256": sha256_text(ptext),
                                    "response_text": None, "usage": None, "finish_reason": None, "call_duration_s": None, "validation": None,
                                    "error": None}
            calls.append(call)
            for t in pending:
                t.attempts += 1
            c0 = time.perf_counter()
            error: str | None = None
            answers: dict[str, dict[str, Any]] = {}
            try:
                reply: LlmReply = assistant(prompt, ptext, [])
                call.update(response_text=reply.text, usage=reply.usage, finish_reason=reply.finish_reason)
                answers = parse_answers(reply.text, [t.id for t in pending])
                call["validation"] = {"ok": True, "reasons": []}
            except AplusCompressResponseError as e:
                call["validation"] = {"ok": False, "reasons": e.reasons}
                error = "; ".join(e.reasons)
            except VlmError as e:
                error = str(e)
            call["call_duration_s"] = round(time.perf_counter() - c0, 3)
            if error is not None:
                call["error"] = error
                if attempt == 1:  # 첫 호출 실패 = 전체 실패. 호출자는 7칸 구성 결과를 그대로 쓴다
                    rec.update(status="validation_failed" if call["validation"] else "call_failed", duration_s=round(time.perf_counter() - t0, 3),
                               error=error)
                    _emit(recorder, rec, failed=True)
                    return AplusCompressResult(status="failed", slots=None, checked=checked(), error=rec["error"])
                for t in pending:  # 다시 묻는 호출 실패 — 그때까지 채택된 칸은 살리고 남은 칸은 1차 번역
                    t.reason = "call_failed"
                break
            try:
                for t in pending:
                    judge(t, answers[t.id], regulation)
            except (MatchConflictError, PatternError) as e:
                return regulation_failed(e)
            for t in pending:
                if t.reason == "fit":
                    modules[t.slot] = replace_text(modules[t.slot], t.field, t.answer)
            pending = [t for t in pending if t.reason in RETRYABLE]
            if not pending:
                break
    slots = [s.model_copy(update={"module": modules[s.slot]}) if s.module is not None else s.model_copy() for s in plan.slots]
    fields = [_record(t) for t in targets]
    rec.update(status="ok", duration_s=round(time.perf_counter() - t0, 3), result=[f.model_dump(mode="json") for f in fields])
    _emit(recorder, rec, failed=False)
    return AplusCompressResult(status="ok", slots=slots, fields=fields, checked=checked())
