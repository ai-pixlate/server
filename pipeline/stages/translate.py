"""⑧ 로컬라이징 번역 — 섹션 문맥 1회 호출 · 블록별 결과 [contract.md 5.1 · 통합 D9-2 · D9-3 · 5.25 · 5.32].

- 모델 gemini + BE가 검색해 공급한 용어(glossary.id · 원문 · 번역어 · enforcement). AI는 용어를 검색하지 않는다.
- 입력 블록은 원본 순서와 섹션 문맥을 유지한다. 대상(id 있는 블록)만 결과를 낸다 — 재시도면 이번 실패 대상만(기존 성공분 · 사용자 수정값 불변).
- 응답 구조 검증: 보낸 대상 id가 정확히 한 번씩(누락 · 중복 · 모르는 id는 **전체 응답 실패**, 일부를 임의 성공으로 추출하지 않음).
- 블록 검증: 비공백 원문의 성공 번역문은 비어 있지 않아야 하고, 금지 자리표시자(묶음의 템플릿 토큰)가 원문에 없는데 나오면 그 블록 실패.
  모델이 명시한 실패(`failed: true`)는 그 블록 실패. 실패 블록에 원문 · 빈 문장을 성공 번역으로 저장하지 않는다.
- `char_limit`로 축약하지 않는다. RG-021/022 같은 템플릿 대체 표현은 번역 지시로 보내지 않는다(원문 수치 유지, D9).
- 별도의 번역문 LLM 규제 판정은 하지 않는다(D9-2, PM 후속 회신 대상). 규제 표현 영어 재대조 · 강제 용어 검사는 `text_check`가 따로 한다.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.vlm import GeminiTranslateAssistant, JudgeAssistant, VlmError, sha256_text

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY_ENV = "GEMINI_API_KEY"
TRANSLATE_IMPL_VERSION = "translate@2026-10-08.1"


class TranslateResponseError(VlmError):
    """응답 구조가 대상 목록과 맞지 않는다(누락 · 중복 · 모르는 id · 형식). 전체 호출 실패."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("⑧ 응답 검증 실패: " + "; ".join(reasons))


@dataclass(frozen=True)
class ContextBlock:
    block_key: str
    block_order: int
    role: str
    source_ko: str


@dataclass(frozen=True)
class Term:
    glossary_id: str
    term_ko: str
    term_target: str
    enforcement: str  # enforced · reference


@dataclass(frozen=True)
class Instruction:
    block_key: str
    external_id: str
    matched_text: str
    alternatives: tuple[str, ...]


@dataclass
class BlockOutcome:
    block_key: str
    outcome: str  # completed · failed
    translation: str | None = None
    error: str | None = None
    applied_instructions: list[str] = field(default_factory=list)


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def validate_config(cfg: dict[str, Any], *, need_api_key: bool) -> str:
    """[translate] 검증. 통과하면 프롬프트 원문. 어기면 ValueError."""
    t = cfg.get("translate")
    if not isinstance(t, dict):
        raise ValueError("config에 [translate] 표가 없다")
    missing = [k for k in ("model", "prompt_path", "temperature", "timeout_s") if k not in t]
    if missing:
        raise ValueError("잘못된 [translate] 설정: 없는 키 " + ", ".join(f"translate.{k}" for k in missing))
    errors = []
    if not isinstance(t["model"], str) or not t["model"].strip():
        errors.append("translate.model — 비어 있지 않은 문자열")
    if not _finite(t["temperature"]) or not 0 <= t["temperature"] <= 2:
        errors.append(f"translate.temperature={t['temperature']!r} — 0 이상 2 이하 수치")
    if not _finite(t["timeout_s"]) or not t["timeout_s"] > 0:
        errors.append(f"translate.timeout_s={t['timeout_s']!r} — 0 초과 수치")
    prompt = ""
    p = Path(t["prompt_path"]) if isinstance(t["prompt_path"], str) else None
    if p is None:
        errors.append("translate.prompt_path — 문자열")
    else:
        p = p if p.is_absolute() else REPO_ROOT / p
        try:
            prompt = p.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"translate.prompt_path — 빈 프롬프트 {p}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"translate.prompt_path — 읽을 수 없다 {p} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다")
    if errors:
        raise ValueError("잘못된 [translate] 설정: " + "; ".join(errors))
    return prompt


def model_config(cfg: dict[str, Any]) -> dict[str, Any]:
    t = cfg["translate"]
    return {"model": t["model"], "temperature": t["temperature"], "timeout_s": t["timeout_s"]}


def default_assistant(cfg: dict[str, Any]) -> GeminiTranslateAssistant:
    c = model_config(cfg)
    return GeminiTranslateAssistant(model=c["model"], temperature=c["temperature"], timeout_s=c["timeout_s"])


def build_payload(context: list[ContextBlock], targets: list[str], terms: list[Term], instructions: list[Instruction],
                  target_lang: str, target_country: str) -> tuple[dict[str, Any], dict[str, str]]:
    """반환: (페이로드, 임시 id → block_key). 블록 원문 순서 그대로. 대상 블록에만 id를 붙인다."""
    ordered = sorted(context, key=lambda b: (b.block_order, b.block_key))
    tids = {k: f"t{i + 1}" for i, k in enumerate(k for k in (b.block_key for b in ordered) if k in set(targets))}
    blocks = []
    for b in ordered:
        item: dict[str, Any] = {"role": b.role, "text": b.source_ko}
        if b.block_key in tids:
            item = {"id": tids[b.block_key], **item}
        blocks.append(item)
    payload = {
        "target": {"lang": target_lang, "country": target_country},
        "blocks": blocks,
        "glossary": [{"term_ko": t.term_ko, "term_target": t.term_target, "enforcement": t.enforcement} for t in terms],
        "instructions": [{"id": tids[i.block_key], "matched_text": i.matched_text, "alternatives": list(i.alternatives)}
                         for i in instructions],
    }
    return payload, {v: k for k, v in tids.items()}


def parse_response(text: str, sent_ids: list[str]) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError as e:
        raise TranslateResponseError([f"JSON 아님: {e}"]) from e
    items = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise TranslateResponseError(["최상위 blocks 배열이 없다"])
    reasons: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            reasons.append(f"blocks[{i}] 객체가 아니다")
            continue
        bid = it.get("id")
        if bid not in sent_ids:
            reasons.append(f"blocks[{i}] 모르는 id {bid!r}")
            continue
        if bid in seen:
            reasons.append(f"{bid} 중복 응답")
            continue
        failed = it.get("failed", False)
        if not isinstance(failed, bool):
            reasons.append(f"{bid} failed는 boolean이어야 한다")
            continue
        tr = it.get("translation")
        if failed:
            if tr not in (None, ""):
                reasons.append(f"{bid} failed인데 translation이 있다")
                continue
            seen[bid] = {"failed": True, "reason": str(it.get("reason") or "모델이 번역하지 못했다고 답함")}
        else:
            if not isinstance(tr, str):
                reasons.append(f"{bid} translation 문자열이 없다")
                continue
            seen[bid] = {"failed": False, "translation": tr}
    missing = [x for x in sent_ids if x not in seen]
    if missing:
        reasons.append(f"응답에 없는 대상 {missing}")
    if reasons:
        raise TranslateResponseError(reasons)
    return seen


def translate_section(context: list[ContextBlock], targets: list[str], terms: list[Term], instructions: list[Instruction],
                      cfg: dict[str, Any], *, target_lang: str, target_country: str, prompt: str, llm: JudgeAssistant,
                      placeholder_tokens: set[str]) -> tuple[list[BlockOutcome], dict[str, Any]]:
    """대상 블록마다 BlockOutcome 1개(대상 순서 = 원문 순서). 호출 · 구조 실패는 VlmError로 올린다(호출자가 대상 전체 실패로 기록)."""
    by_key = {b.block_key: b for b in context}
    payload, ids = build_payload(context, targets, terms, instructions, target_lang, target_country)
    ptext = json.dumps(payload, ensure_ascii=False, indent=2)
    diag: dict[str, Any] = {"payload_sha256": sha256_text(ptext), "prompt_sha256": sha256_text(prompt), "model": model_config(cfg)}
    reply = llm(prompt, ptext, None)
    diag["response_sha256"] = sha256_text(reply.text)
    diag["usage"] = reply.usage
    parsed = parse_response(reply.text, list(ids))
    outs = []
    for tid, key in ids.items():
        src = by_key[key].source_ko
        r = parsed[tid]
        applied = [i.external_id for i in instructions if i.block_key == key]
        if r["failed"]:
            outs.append(BlockOutcome(key, "failed", error=f"모델 명시 실패: {r['reason']}", applied_instructions=applied))
            continue
        tr = r["translation"]
        if not tr.strip():
            outs.append(BlockOutcome(key, "failed", error="비공백 원문에 빈 번역문", applied_instructions=applied))
            continue
        leaked = sorted(t for t in placeholder_tokens if t in tr and t not in src)
        if leaked:
            outs.append(BlockOutcome(key, "failed", error=f"미치환 자리표시자 {leaked}", applied_instructions=applied))
            continue
        outs.append(BlockOutcome(key, "completed", translation=tr, applied_instructions=applied))
    return outs, diag
