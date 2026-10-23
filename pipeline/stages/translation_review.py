"""⑧ 번역문 LLM 규제 판정 — 번역기와 독립된 호출 · 프롬프트 [contract.md 5.1 · 7.1 · 7.2 · 통합 D5 · D9 · 사용자 결정 2026-10-12].

규칙 검사(`text_check`)와 독립된 검사다. 규칙 결과를 입력으로 받지 않으며 번역문 · 사용자 수정문 · 규칙 경고 · 강제 용어 결과를 바꾸지 않는다.
자동 번역 직후와 N5 사용자 수정 뒤에 같은 함수를 쓴다. 섹션 단위(섹션 문맥 전체)로 읽고 블록별 결과를 낸다.

검사 항목
- check_items: 적용 분류(applied_classes, common 포함)의 regulated · conditional 행 **전체**(문자열 매칭 여부와 무관).
- reference_items: 적용 분류의 allowed 행. 판정 대상이 아니며 "적용 분류 안에 같은 표현이 허용된다는 사전 기록"이라는 범위로만
  제공한다. 다른 항목의 위험이나 규칙 경고를 해제하지 않고, risk의 근거로 쓸 수 없다.
- 적용 분류 밖 행은 보내지 않고 ID만 기록한다.

응답 — (대상 × 항목) 쌍마다 정확히 1건. 판단과 주장 존재를 분리한다.
- mention: 항목과 관련된 표현 · 주장이 번역문에 나오는지(형식 무관). 부정 · 인용 · 가정은 위험 판단의 문맥 단서이며 결론 규칙이 아니다.
- judgment: risk · no_risk · undetermined(+ condition_unverifiable · insufficient_context · insufficient_evidence).
- 구조 규칙(`judgment_problems`): mention present ⟺ 위치 1개 이상. risk = present + 그 항목 자신의 사전 기록/근거 참조 1개 이상(allowed 참조 금지)
  + 미확인 조건 없음. no_risk = 미확인 조건 없음, present면 비해당을 설명하는 공급 근거 참조 1개 이상. condition_unverifiable = present
  + 미확인 조건 1개 이상. 그 밖의 undetermined = 미확인 조건 없음.
  이 규칙은 응답이 공급 범위 · 구조 안에 있는지만 확인한다. 판단 내용(조건 필요 여부 · 비해당 설명)이 맞는지는 보장하지 않는다.
- 위치: {quote, anchor}. anchor는 번역문에서 정확히 한 번, quote는 anchor 안에서 정확히 한 번 나와야 한다. 좌표는 정규화하지 않은
  번역문의 Unicode 코드 포인트 [start, end), 대조는 정확한 문자열 일치. 특정하지 못하면 응답 실패(초기 구현 한계 — 같은 문구가 반복되는
  짧은 문장처럼 anchor로 유일하게 가리킬 수 없는 위치는 표현하지 못한다).
- 근거 참조: dictionary_entry(서비스 사전 기록 — 외부 조문이 아님) · evidence(그 항목의 근거 행) · allowed_reference(참고 항목).
  근거 인용문은 모델이 출력하지 않는다(공급 스냅샷 원문을 그대로 보존). 판단 이유는 짧은 한국어.
- URL · 조문 표기 검사(`_text_problems`)는 보조 검사다. 공급 근거에 없는 URL · 조문 표기를 거절할 뿐 판단의 정확성을 보장하지 않는다.

분할 — 모든 분할에 같은 섹션 문맥 전체와 참고 항목 전체를 넣는다(문맥 분할 없음). 대상은 블록 순서, 항목은 묶음 순서로 결정적으로 나눈다.
분할하지 않는 최소 단위(섹션 문맥 + 대상 1 + 검사 항목 1 + 참고 항목 전체)가 입력 상한을 넘으면 호출하지 않는다(`ReviewInputTooLarge`).
입력 크기는 모델에 보내는 문자열 전체(프롬프트 원문 + 직렬화 페이로드)의 Unicode 코드 포인트 수다 — 토큰 한도를 보장하지 않는다.
분할 하나라도 호출 · 검증에 실패하면 이후 분할은 부르지 않고 시도 전체가 실패한다(부분 채택 없음).
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pipeline.vlm import GeminiTranslationReviewAssistant, JudgeAssistant, VlmError, sha256_text

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY_ENV = "GEMINI_API_KEY"
# 판정 규칙(항목 선정 · 응답 구조 규칙 · 위치 · 근거 참조 · 분할)의 버전. 바꾸면 올린다(입력 지문에 들어간다)
REVIEW_IMPL_VERSION = "translation-review@2026-10-12.1"

JUDGMENTS = ("risk", "no_risk", "undetermined")
MENTIONS = ("present", "absent")
UNDETERMINED_REASONS = ("condition_unverifiable", "insufficient_context", "insufficient_evidence")
BASIS_KINDS = ("dictionary_entry", "evidence", "allowed_reference")
BASIS_SUMMARIES = ("dictionary_only", "external_evidence", "both", "allowed_reference_only", "none")
CHUNK_STATUSES = ("validated", "call_failed", "response_invalid", "output_truncated", "not_called")
TRUNCATED_FINISH_REASONS = frozenset({"MAX_TOKENS"})
REFERENCE_NOTE = ("허용 표현 기록(적용 분류 안에 이 표현이 허용된다는 사전 기록). 판정 대상이 아니며 다른 항목의 위험을 해제하지 않는다. "
                  "risk의 근거로 쓰지 않는다")


class ReviewResponseError(VlmError):
    """응답이 구조 · 근거 · 위치 검증을 통과하지 못했다. 분할 전체 실패(일부를 임의 성공으로 추출하지 않음)."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("번역문 규제 판정 응답 검증 실패: " + "; ".join(reasons[:20]) + (" …" if len(reasons) > 20 else ""))


class ReviewInputTooLarge(ValueError):
    """분할하지 않는 최소 단위가 입력 상한을 넘는다. 잘라내지 않고 호출하지 않는다."""

    def __init__(self, measured: int, limit: int, detail: str) -> None:
        self.measured, self.limit = measured, limit
        super().__init__(f"입력 상한 초과 — 최소 단위 {measured} > {limit} 코드 포인트 ({detail})")


# ---------------------------------------------------------------------------
# 입력 단위
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Evidence:
    index: int  # 항목의 근거 배열 안 위치(0부터)
    external_id: str | None
    source_type: str | None
    document: str | None
    article: str | None
    quote: str | None
    url: str | None  # 모델에 보내지 않는다(판정에 불필요 · 복사 방지). 스냅샷에는 보존


@dataclass(frozen=True)
class Item:
    ref: str  # 모델 입력 ID — check 'r1' … · reference 'a1' …
    role: str  # check · reference
    external_id: str
    regulatory_class: str
    verdict_status: str
    source_verdict_status: str | None
    source_expression: str
    variant_en: tuple[str, ...]
    reason: str
    evidence: tuple[Evidence, ...]

    def evidence_ref(self, e: Evidence) -> str:
        return f"{self.ref}.e{e.index + 1}"


@dataclass(frozen=True)
class SectionBlock:
    """판정 문맥 한 블록. 번역문은 검사 대상에만 있다(문맥 블록은 번역문 없음)."""

    block_key: str
    block_order: int
    role: str
    source_ko: str
    is_product_label: bool | None
    is_brand_logo: bool | None
    target_id: str | None = None  # 검사 대상이면 't1' …
    translation: str | None = None


@dataclass(frozen=True)
class Chunk:
    index: int
    target_ids: tuple[str, ...]
    item_refs: tuple[str, ...]


@dataclass
class Location:
    quote: str
    anchor: str
    start: int
    end: int


@dataclass
class Judgment:
    target_id: str
    item_ref: str
    judgment: str
    mention: str
    undetermined_reason: str | None
    locations: list[Location]
    basis: list[str]
    unverified_conditions: list[str]
    reason: str
    chunk_index: int


@dataclass
class ChunkRecord:
    index: int
    target_ids: tuple[str, ...]
    item_refs: tuple[str, ...]
    status: str = "not_called"
    input_code_points: int = 0
    payload_sha256: str | None = None
    response_sha256: str | None = None
    finish_reason: str | None = None
    usage: Any = None
    error: str | None = None
    judgments: list[Judgment] = field(default_factory=list)  # validated일 때만


class ReviewRunFailed(Exception):
    """분할 하나가 호출 · 검증에 실패했다. 기록(성공 분할 포함)은 진단 · 이력용이며 현재 유효한 판정이 아니다."""

    def __init__(self, kind: str, message: str, records: list[ChunkRecord], *, truncated: bool) -> None:
        self.kind, self.records, self.truncated = kind, records, truncated
        super().__init__(message)


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pos_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def validate_config(cfg: dict[str, Any], *, need_api_key: bool) -> str:
    """[translation_review] 검증. 통과하면 프롬프트 원문. 어기면 ValueError. 값은 모두 개발용(운영 확정값 아님)."""
    t = cfg.get("translation_review")
    if not isinstance(t, dict):
        raise ValueError("config에 [translation_review] 표가 없다")
    keys = ("model", "prompt_path", "temperature", "timeout_s", "max_input_chars", "max_pairs_per_call", "max_output_tokens")
    missing = [k for k in keys if k not in t]
    if missing:
        raise ValueError("잘못된 [translation_review] 설정: 없는 키 " + ", ".join(f"translation_review.{k}" for k in missing))
    errors = []
    if not isinstance(t["model"], str) or not t["model"].strip():
        errors.append("translation_review.model — 비어 있지 않은 문자열")
    if not _finite(t["temperature"]) or not 0 <= t["temperature"] <= 2:
        errors.append(f"translation_review.temperature={t['temperature']!r} — 0 이상 2 이하 수치")
    if not _finite(t["timeout_s"]) or not t["timeout_s"] > 0:
        errors.append(f"translation_review.timeout_s={t['timeout_s']!r} — 0 초과 수치")
    for k in ("max_input_chars", "max_pairs_per_call", "max_output_tokens"):
        if not _pos_int(t[k]):
            errors.append(f"translation_review.{k}={t[k]!r} — 양의 정수")
    prompt = ""
    p = Path(t["prompt_path"]) if isinstance(t["prompt_path"], str) else None
    if p is None:
        errors.append("translation_review.prompt_path — 문자열")
    else:
        p = p if p.is_absolute() else REPO_ROOT / p
        try:
            prompt = p.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"translation_review.prompt_path — 빈 프롬프트 {p}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"translation_review.prompt_path — 읽을 수 없다 {p} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다")
    if errors:
        raise ValueError("잘못된 [translation_review] 설정: " + "; ".join(errors))
    return prompt


def model_config(cfg: dict[str, Any]) -> dict[str, Any]:
    t = cfg["translation_review"]
    return {"model": t["model"], "temperature": t["temperature"], "timeout_s": t["timeout_s"], "max_output_tokens": t["max_output_tokens"]}


def limits_config(cfg: dict[str, Any]) -> dict[str, Any]:
    t = cfg["translation_review"]
    return {"max_input_chars": t["max_input_chars"], "max_pairs_per_call": t["max_pairs_per_call"], "unit": "code_points"}


def default_assistant(cfg: dict[str, Any]) -> GeminiTranslationReviewAssistant:
    c = model_config(cfg)
    return GeminiTranslationReviewAssistant(model=c["model"], temperature=c["temperature"], timeout_s=c["timeout_s"],
                                            max_output_tokens=c["max_output_tokens"])


# ---------------------------------------------------------------------------
# 모델 입력 · 분할
# ---------------------------------------------------------------------------
def _item_payload(it: Item) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": it.ref, "regulatory_class": it.regulatory_class, "verdict_status": it.verdict_status,
        "original_verdict_status": it.source_verdict_status, "expression_ko": it.source_expression,
        "examples_en": list(it.variant_en), "dictionary_reason": it.reason,
        "evidence": [{"id": it.evidence_ref(e), "source_type": e.source_type, "document": e.document, "article": e.article,
                      "quote": e.quote} for e in it.evidence],
    }
    if it.role == "reference":
        out = {"id": it.ref, "note": REFERENCE_NOTE, **{k: v for k, v in out.items() if k != "id"}}
    return out


def build_payload(blocks: list[SectionBlock], chunk: Chunk, items: dict[str, Item], references: list[Item],
                  class_basis: dict[str, Any], target_lang: str, target_country: str) -> dict[str, Any]:
    ordered = sorted(blocks, key=lambda b: (b.block_order, b.block_key))
    sec = []
    for b in ordered:
        e: dict[str, Any] = {"order": b.block_order, "role": b.role, "source_ko": b.source_ko, "translation": b.translation,
                             "facts": {"is_product_label": b.is_product_label, "is_brand_logo": b.is_brand_logo,
                                       "source_blank": not b.source_ko.strip()}}
        if b.target_id is not None:
            e = {"id": b.target_id, **e}
        sec.append(e)
    return {
        "target": {"lang": target_lang, "country": target_country},
        "product_class": class_basis,
        "section_blocks": sec,
        "check_items": [_item_payload(items[r]) for r in chunk.item_refs],
        "reference_items": [_item_payload(r) for r in references],
        "required_pairs": {"targets": list(chunk.target_ids), "items": list(chunk.item_refs)},
    }


def serialize_payload(payload: dict[str, Any]) -> str:
    """모델에 실제로 보내는 페이로드 문자열(크기 측정 · 해시도 이 문자열 기준)."""
    return json.dumps(payload, ensure_ascii=False, indent=1)


def plan_chunks(target_ids: list[str], item_refs: list[str], size_of: Callable[[list[str], list[str]], int], *,
                max_input_chars: int, max_pairs_per_call: int) -> list[Chunk]:
    """결정적 분할. ① 대상을 블록 순서로 묶는다(묶음 × 어떤 항목 1개도 상한 안). ② 묶음마다 항목을 묶음 순서로 채운다.
    size_of(대상 ID 목록, 항목 ID 목록) = 그 분할의 입력 코드 포인트 수(프롬프트 + 페이로드). 최소 단위가 넘으면 ReviewInputTooLarge."""
    if not target_ids or not item_refs:
        return []

    def fits(ts: list[str], rs: list[str]) -> bool:
        return len(ts) * len(rs) <= max_pairs_per_call and size_of(ts, rs) <= max_input_chars

    for t in target_ids:
        for r in item_refs:
            n = size_of([t], [r])
            if n > max_input_chars:
                raise ReviewInputTooLarge(n, max_input_chars, f"대상 {t} · 항목 {r} + 섹션 문맥 · 참고 항목 전체")
    groups: list[list[str]] = []
    cur: list[str] = []
    for t in target_ids:
        trial = cur + [t]
        if cur and not all(fits(trial, [r]) for r in item_refs):
            groups.append(cur)
            cur = [t]
        else:
            cur = trial
    groups.append(cur)
    chunks: list[Chunk] = []
    for g in groups:
        rs: list[str] = []
        for r in item_refs:
            if rs and not fits(g, rs + [r]):
                chunks.append(Chunk(len(chunks), tuple(g), tuple(rs)))
                rs = [r]
            else:
                rs.append(r)
        chunks.append(Chunk(len(chunks), tuple(g), tuple(rs)))
    return chunks


# ---------------------------------------------------------------------------
# 응답 검증
# ---------------------------------------------------------------------------
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:gov|com|org|net|io|int|eu|kr)\b")
_CITATION = re.compile(r"(?i)\b\d+\s*(?:C\.?F\.?R\.?|U\.?S\.?C\.?)(?:\s*(?:§+\s*)?[\d.]+(?:\([a-z0-9]+\))*)?|§+\s*[\d.]+(?:\([a-z0-9]+\))*"
                       r"|\bSec(?:tion|\.)\s*\d[\d.]*(?:\([a-z0-9]+\))*|\b(?:FD&C|FDCA|MoCRA)\b")


def _norm_cite(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _text_problems(where: str, text: str, allowed_corpus: str) -> list[str]:
    """보조 검사: URL 금지 · 공급 근거에 없는 조문 표기 금지. 판단 내용의 정확성은 보장하지 않는다."""
    out = []
    if _URL.search(text):
        out.append(f"{where}: URL을 만들었다")
    for m in _CITATION.finditer(text):
        if _norm_cite(m.group(0)) not in allowed_corpus:
            out.append(f"{where}: 공급 근거에 없는 조문 표기 {m.group(0)!r}")
    return out


def citation_corpus(items: list[Item]) -> str:
    parts = []
    for it in items:
        parts += [it.reason, it.source_expression]
        for e in it.evidence:
            parts += [x for x in (e.document, e.article, e.quote, e.source_type) if x]
    return _norm_cite(" ".join(parts))


def _count(hay: str, needle: str) -> int:
    """겹치는 출현까지 센다."""
    n, i = 0, hay.find(needle)
    while i != -1:
        n += 1
        i = hay.find(needle, i + 1)
    return n


def locate(text: str, quote: str, anchor: str) -> tuple[int, int] | str:
    """(start, end) 또는 실패 사유. 정확한 문자열 일치 · 코드 포인트 좌표."""
    if not quote or not anchor:
        return "quote · anchor가 비었다"
    if _count(anchor, quote) != 1:
        return f"quote {quote!r}가 anchor 안에 정확히 한 번 나오지 않는다"
    if _count(text, anchor) != 1:
        return f"anchor {anchor!r}가 번역문에 정확히 한 번 나오지 않는다(위치를 특정할 수 없음)"
    a = text.find(anchor)
    s = a + anchor.find(quote)
    return s, s + len(quote)


def basis_kind(ref: str, item: Item, references: dict[str, Item]) -> str | None:
    if ref == item.ref:
        return "dictionary_entry"
    if any(ref == item.evidence_ref(e) for e in item.evidence):
        return "evidence"
    for a in references.values():
        if ref == a.ref or any(ref == a.evidence_ref(e) for e in a.evidence):
            return "allowed_reference"
    return None


def basis_summary(kinds: list[str]) -> str:
    d, e, a = "dictionary_entry" in kinds, "evidence" in kinds, "allowed_reference" in kinds
    if d and e:
        return "both"
    if e:
        return "external_evidence"
    if d:
        return "dictionary_only"
    return "allowed_reference_only" if a else "none"


def judgment_problems(j: dict[str, Any], kinds: list[str]) -> list[str]:
    """판단 조합 규칙(계약 v3 2.2). j: judgment · mention · undetermined_reason · locations · unverified_conditions. kinds: 근거 종류 목록."""
    p = []
    jd, mn, ur = j["judgment"], j["mention"], j["undetermined_reason"]
    n_loc, n_cond = len(j["locations"]), len(j["unverified_conditions"])
    own = [k for k in kinds if k in ("dictionary_entry", "evidence")]
    if (mn == "present") != (n_loc > 0):
        p.append("mention=present와 위치 1개 이상이 일치하지 않는다")
    if (jd == "undetermined") != (ur is not None):
        p.append("undetermined일 때만 undetermined_reason이 있어야 한다")
    if jd == "risk":
        if mn != "present":
            p.append("risk인데 mention이 present가 아니다")
        if not own:
            p.append("risk인데 그 항목의 사전 기록 · 근거 참조가 없다(근거 부족은 undetermined/insufficient_evidence)")
        if "allowed_reference" in kinds:
            p.append("risk의 근거로 allowed 참고 항목을 쓸 수 없다")
        if n_cond:
            p.append("risk에는 미확인 조건을 둘 수 없다(조건 미확인은 undetermined/condition_unverifiable)")
    elif jd == "no_risk":
        if n_cond:
            p.append("no_risk에는 미확인 조건을 둘 수 없다(조건 미확인은 undetermined/condition_unverifiable)")
        if mn == "present" and not kinds:
            p.append("mention=present인 no_risk는 비해당을 설명하는 공급 근거 참조가 있어야 한다")
    elif ur == "condition_unverifiable":
        if mn != "present":
            p.append("condition_unverifiable인데 mention이 present가 아니다")
        if not n_cond:
            p.append("condition_unverifiable인데 미확인 조건이 없다")
    elif n_cond:
        p.append(f"{ur}에는 미확인 조건을 두지 않는다")
    return p


def _str_list(v: Any) -> list[str] | None:
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        return None
    return list(v)


def parse_response(text: str, chunk: Chunk, targets: dict[str, SectionBlock], items: dict[str, Item],
                   references: dict[str, Item], corpus: str) -> list[Judgment]:
    """분할 응답 검증. 하나라도 어기면 ReviewResponseError(분할 전체 실패)."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ReviewResponseError([f"JSON 아님: {e}"]) from e
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ReviewResponseError(["최상위 results 배열이 없다"])
    reasons: list[str] = []
    seen: dict[tuple[str, str], Judgment] = {}
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            reasons.append(f"results[{i}] 객체가 아니다")
            continue
        t, it_ref = r.get("target"), r.get("item")
        if t not in chunk.target_ids or it_ref not in chunk.item_refs:
            reasons.append(f"results[{i}] 요청 밖 쌍 ({t!r}, {it_ref!r})")
            continue
        where = f"{t}×{it_ref}"
        if (t, it_ref) in seen:
            reasons.append(f"{where} 중복 응답")
            continue
        jd, mn, ur = r.get("judgment"), r.get("mention"), r.get("undetermined_reason")
        bad = []
        if jd not in JUDGMENTS:
            bad.append(f"judgment {jd!r}")
        if mn not in MENTIONS:
            bad.append(f"mention {mn!r}")
        if ur is not None and ur not in UNDETERMINED_REASONS:
            bad.append(f"undetermined_reason {ur!r}")
        reason = r.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            bad.append("reason이 비었다")
        conds = _str_list(r.get("unverified_conditions"))
        basis = _str_list(r.get("basis"))
        if conds is None:
            bad.append("unverified_conditions는 비어 있지 않은 문자열 배열")
        if basis is None:
            bad.append("basis는 비어 있지 않은 문자열 배열")
        locs_raw = r.get("locations")
        if not isinstance(locs_raw, list):
            bad.append("locations는 배열")
        if bad:
            reasons.append(f"{where}: " + ", ".join(bad))
            continue
        assert conds is not None and basis is not None and isinstance(locs_raw, list)
        tr = targets[t].translation or ""
        locs: list[Location] = []
        for k, lr in enumerate(locs_raw):
            if not isinstance(lr, dict) or not isinstance(lr.get("quote"), str) or not isinstance(lr.get("anchor"), str):
                reasons.append(f"{where} locations[{k}] 형식")
                continue
            got = locate(tr, lr["quote"], lr["anchor"])
            if isinstance(got, str):
                reasons.append(f"{where} locations[{k}]: {got}")
                continue
            locs.append(Location(lr["quote"], lr["anchor"], got[0], got[1]))
        if len({(x.start, x.end) for x in locs}) != len(locs):
            reasons.append(f"{where}: 같은 위치를 두 번 지목했다")
        item = items[it_ref]
        kinds = []
        if len(set(basis)) != len(basis):
            reasons.append(f"{where}: 근거 참조 중복")
        for ref in basis:
            k = basis_kind(ref, item, references)
            if k is None:
                reasons.append(f"{where}: 이 항목 · 참고 항목에 없는 근거 참조 {ref!r}")
            else:
                kinds.append(k)
        j = {"judgment": jd, "mention": mn, "undetermined_reason": ur, "locations": locs_raw, "unverified_conditions": conds}
        reasons += [f"{where}: {x}" for x in judgment_problems(j, kinds)]
        reasons += _text_problems(f"{where} reason", reason, corpus)
        for c in conds:
            reasons += _text_problems(f"{where} 미확인 조건", c, corpus)
        seen[(t, it_ref)] = Judgment(t, it_ref, jd, mn, ur, locs, basis, conds, reason.strip(), chunk.index)
    missing = [f"{t}×{r}" for t in chunk.target_ids for r in chunk.item_refs if (t, r) not in seen]
    if missing:
        reasons.append(f"응답에 없는 쌍 {missing[:20]}" + (" …" if len(missing) > 20 else ""))
    if reasons:
        raise ReviewResponseError(reasons)
    return [seen[(t, r)] for t in chunk.target_ids for r in chunk.item_refs]


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------
def review_section(blocks: list[SectionBlock], chunks: list[Chunk], items: dict[str, Item], references: list[Item],
                   class_basis: dict[str, Any], *, target_lang: str, target_country: str, prompt: str,
                   llm: JudgeAssistant) -> list[ChunkRecord]:
    """분할을 순서대로 부른다. 모두 검증되면 기록 목록(판정 포함), 하나라도 실패하면 ReviewRunFailed(이후 분할은 not_called)."""
    targets = {b.target_id: b for b in blocks if b.target_id is not None}
    refs = {r.ref: r for r in references}
    corpus = citation_corpus(list(items.values()) + references)
    records = [ChunkRecord(c.index, c.target_ids, c.item_refs) for c in chunks]
    for c, rec in zip(chunks, records):
        ptext = serialize_payload(build_payload(blocks, c, items, references, class_basis, target_lang, target_country))
        rec.payload_sha256, rec.input_code_points = sha256_text(ptext), len(prompt) + len(ptext)
        try:
            reply = llm(prompt, ptext, None)
        except VlmError as e:
            rec.status, rec.error = "call_failed", str(e)
            raise ReviewRunFailed("model_call_failed", f"분할 {c.index} 호출 실패: {e}", records, truncated=False) from e
        rec.response_sha256, rec.usage = sha256_text(reply.text), reply.usage
        rec.finish_reason = getattr(reply, "finish_reason", None)
        if rec.finish_reason in TRUNCATED_FINISH_REASONS:
            rec.status, rec.error = "output_truncated", f"공급자가 출력 한도로 종료했다({rec.finish_reason})"
            raise ReviewRunFailed("response_invalid", f"분할 {c.index} 응답 잘림 — 같은 설정으로 재시도해도 풀리지 않는다",
                                  records, truncated=True)
        try:
            rec.judgments = parse_response(reply.text, c, targets, items, refs, corpus)
        except ReviewResponseError as e:
            rec.status, rec.error = "response_invalid", str(e)
            raise ReviewRunFailed("response_invalid", f"분할 {c.index}: {e}", records, truncated=False) from e
        rec.status = "validated"
    return records
