"""A+ 압축(2차 번역) — 템플릿 한도를 넘은 글 칸의 문구를 한도 안으로 줄인다. 개발용 v1(2026-11-26, 운영 계약 아님. open-questions.md #94).

**고쳐 쓰는 것은 모델, 세는 것과 채택은 코드**(사용자 결정 2026-11-26):
- 자리: 7칸 구성 다음이다. 받는 것은 7칸 구성 결과(`AplusPlan`)뿐이다 — 블록 목록 · 이미지 · 한국어 원문은 받지 않는다
  (문구는 1차 번역에서만 온다. 한국어 원문에서 영어를 새로 쓰지 않는다 — aplus.md 4.1).
- 선별을 다시 하지 않는다. 7칸 구성이 한도 직전에서 뺀 블록을 다시 넣지 않고, 칸의 블록 구성 · 출처(`origin_block_keys`)도 바꾸지 않는다.
- 대상: 템플릿 한도(`char_limits`)를 넘은 글 칸(`content.limit_excesses`). `label` 칸은 블록에서 오지 않으므로 대상이 아니다.
- 호출: 상품당 1회, 글만 보낸다(이미지 없음). 대상이 있는 칸(모듈)마다 그 칸의 글 전부를 문맥으로 함께 보낸다.
  채택되지 않은 칸만 실제 글자 수 · 문제와 함께 다시 묻는다(`aplus_compress.max_retries`회까지).
- 채택: 글자 수는 코드가 센다. 답이 템플릿 한도 안이고, 비어 있지 않고, 1차 문구에 없던 숫자가 없을 때만 문구를 바꾼다.
  한도 안이면 1차 문구보다 반드시 짧으므로 길이 비교는 따로 하지 않는다. 덜 줄어든 답은 쓰지 않는다.
- 못 맞춘 칸은 1차 번역 그대로 둔다(PM 결정 2026-11-24 — aplus.md 4.4). 1차 번역이 가이드 상한까지 넘는 칸도 같다 — 새 규칙을 만들지 않고
  기록에 두 한도를 남긴다(`AplusCompressedField.guide_max_chars`).
- 어느 칸의 문구가 바뀌었는지는 결과의 `fields`(`outcome`)로만 안다. `AplusText` · `AplusModule`의 모양은 바꾸지 않는다(인계 형식은 #97).

첫 호출의 실패(호출 · 시간 초과 · 응답 구조)는 예외가 아니라 `status=failed` 결과다 — 호출자는 7칸 구성 결과를 그대로 쓴다.
다시 묻는 호출이 실패하면 그때까지 채택된 칸은 살리고 남은 칸을 1차 번역으로 둔다(`reason=call_failed`). 입력 오류는 예외다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.aplus import content
from pipeline.aplus.label import API_KEY_ENV, REPO_ROOT, Recorder, _emit, _finite_number, llm_config
from pipeline.aplus.plan import normalize_text
from pipeline.aplus.spec import AplusSpec
from pipeline.types import (AplusCompressChecked, AplusCompressedField, AplusCompressResult, AplusModule, AplusPlan, AplusSlotPlan,
                            AplusText)
from pipeline.vlm import GeminiMultiImageAssistant, LlmReply, MultiImageAssistant, VlmError, sha256_text

TABLE = "aplus_compress"
SKIP_FIELDS = ("label",)  # 블록에서 오지 않는 칸(템플릿마다 정해진 값)
RETRYABLE = ("over_limit", "new_number", "blank")  # 다시 묻는 사유. declined(모델이 줄일 수 없다고 답함)는 다시 묻지 않는다
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
    missing = [k for k in ("model", "temperature", "timeout_s", "max_retries", "prompt_path") if k not in c]
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


def find_targets(slots: list[AplusSlotPlan], spec: AplusSpec) -> list[Target]:
    """템플릿 한도를 넘은 글 칸(칸 순서 → 칸 안의 자리 순). `label` 칸은 뺀다."""
    out: list[Target] = []
    for s in slots:
        if s.module is None:
            continue
        for c in content.limit_excesses(s.module, spec):
            if c.field in SKIP_FIELDS:
                continue
            out.append(Target(id=f"c{len(out) + 1}", slot=s.slot, check=c, primary=get_text(s.module, c.field).text))
    return out


def numbers(text: str) -> set[str]:
    """문구 안의 숫자 토큰(자릿수 구분 쉼표는 뺀다). `892.35%` → `892.35`, `SPF50+` → `50`, `1,000` → `1000`."""
    return {m.replace(",", "") for m in _NUMBER.findall(text)}


# ---------------------------------------------------------------------------
# 페이로드 · 응답
# ---------------------------------------------------------------------------
def response_schema() -> dict[str, Any]:
    item = {"type": "object",
            "properties": {"id": {"type": "string"}, "failed": {"type": "boolean"}, "text": {"type": "string", "nullable": True},
                           "reason": {"type": "string", "nullable": True}},
            "required": ["id", "failed"]}
    return {"type": "object", "properties": {"targets": {"type": "array", "items": item}}, "required": ["targets"]}


def build_payload(modules: dict[int, AplusModule], pending: list[Target], spec: AplusSpec) -> dict[str, Any]:
    """대상이 있는 칸마다 그 칸의 글 전부(대상에는 `target` · `max_chars`, 앞선 답이 있으면 `tried`). `modules`는 지금 문구 기준이다."""
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
                entry.update(target=t.id, max_chars=t.check.max_chars)
                if t.tried:
                    entry["tried"] = t.tried
            texts.append(entry)
        slots.append({"slot": n, "name": spec.raw["slots"][str(n)]["name_ko"], "template": m.template, "texts": texts})
    return {"slots": slots}


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


def judge(t: Target, answer: dict[str, Any]) -> None:
    """답 하나를 판정해 `t.reason` · `t.answer`에 남긴다. 채택되지 않은 답은 `t.tried`에 더한다.
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
    if chars > t.check.max_chars:
        t.reason = "over_limit"
    elif numbers(text) - numbers(t.primary):
        t.reason = "new_number"
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
        max_chars=t.check.max_chars, guide_max_chars=t.check.guide_max_chars, outcome="compressed" if fit else "kept", reason=t.reason,
        answer_text=t.answer, answer_chars=content.count_chars(t.answer) if t.answer is not None else None, attempts=t.attempts)


def run(plan: AplusPlan, spec: AplusSpec, cfg: dict[str, Any], *, llm: MultiImageAssistant | None = None,
        recorder: Recorder | None = None) -> AplusCompressResult:
    """상품 하나의 압축. `plan`은 성공한 7칸 구성 결과다. 대상이 없으면 모델을 부르지 않고 받은 칸을 그대로 돌려준다."""
    if plan.status != "ok" or plan.slots is None:
        raise ValueError("실패한 7칸 구성 결과는 압축할 수 없다")
    if plan.checked.spec_version != spec.version:
        raise ValueError(f"7칸 구성의 규격 버전 {plan.checked.spec_version!r}이 지금 규격 {spec.version!r}과 다르다")
    targets = find_targets(plan.slots, spec)
    prompt = validate_config(cfg, need_api_key=llm is None and bool(targets))
    max_retries = int(cfg[TABLE]["max_retries"])
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    fp = input_fingerprint(plan.slots)
    calls: list[dict[str, Any]] = []
    rec: dict[str, Any] = {
        "stage": TABLE, "status": None, "input_fingerprint": fp, "spec_version": spec.version, "spec_sha256": spec.sha256,
        "started_at": started.isoformat(timespec="seconds"), "prompt_sha256": sha256_text(prompt), "model_config": llm_config(cfg, TABLE),
        "max_retries": max_retries, "targets": [{"id": t.id, "slot": t.slot, "field": t.field} for t in targets], "calls": calls, "error": None,
    }

    def checked() -> AplusCompressChecked:
        return AplusCompressChecked(input_fingerprint=fp, spec_version=spec.version, llm_called=bool(calls), calls=len(calls))

    modules = {s.slot: s.module for s in plan.slots if s.module is not None}
    if targets:
        c = llm_config(cfg, TABLE)
        assistant = llm if llm is not None else GeminiMultiImageAssistant(model=c["model"], temperature=c["temperature"],
                                                                          timeout_s=c["timeout_s"], response_schema=response_schema())
        pending = targets
        for attempt in range(1, max_retries + 2):
            payload = build_payload(modules, pending, spec)
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
            for t in pending:
                judge(t, answers[t.id])
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
