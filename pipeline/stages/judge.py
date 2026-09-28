"""③-1 AI 섹션 판정 — ③-1a 검출(규칙) 구현 · ③-1b 맥락 판정은 다음 착수 범위.

pipeline.md 단계표 ③-1 · 7.3절, 설계 docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md(잠정, open-questions.md #60).

③-1a 검출(2026-09-28 승인 범위: "현재 승인한 실험용 매칭 규칙의 구현"):
- 대상 사전 행 = 현재 적용 묶음(`judge.dict_dir`)의 규제 16행 · 현지부적합 8행 전부. `allowed` 행과 모든 규제 분류를 포함하고, 보류 · 백업
  시트는 애초에 묶음에 없다. 검출은 판정값 · 규제 분류 같은 정책 필드를 읽지 않는다(`dictionary.judge_view`).
- 정규화: 문자 단위로 NFKC → 소문자 → 공백 · 줄바꿈 제거. 각 정규화 문자가 원문 `source_ko`의 몇 번째 문자(코드 포인트)에서 왔는지 대응표를
  만든다. 문자 간 합성은 하지 않는다(한 원문 문자가 여러 정규화 문자가 될 수는 있다 — 예 `㎖` → `ml`).
- 매칭(`judge.match_mode = substring`): 정규화 텍스트에서 정규화 패턴의 **모든** 부분 문자열 출현(겹침 · 반복 포함), 블록 안에서만.
  빈 패턴(정규화 후 빈 문자열)은 거부한다.
- 원문 복원: 매칭 [p, p+len) → raw_span = [offset[p], offset[p+len-1] + 1) — 원문 문자 인덱스 **반개구간**. matched_text = source_ko[start:end].
- 출력은 `DetectionResult`(status `detect_only`) — 판정 결과가 아니며 ③-1'의 입력이 될 수 없다. 원료 · 연구원 매칭은 의도된 후보다.
- `eojeol_prefix`는 비교 실험용 대안 값이며 미구현(NotImplementedError).

③-1b(LLM) · finding 조립 · run()은 다음 착수 범위다(NotImplementedError).
"""
from __future__ import annotations

import math
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline.dictionary import Dictionaries, JudgeDictView, JudgeItem, load_dictionaries
from pipeline.types import DetectionResult, JudgeChecked, JudgeContext, JudgeResult, Match, Section, Span, TextBlock, match_key

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY_ENV = "GEMINI_API_KEY"
CALL_SCOPES = ("all", "matched")
MATCH_MODES = ("substring", "eojeol_prefix")
MATCH_RULES_VERSION = "match@2026-09-28.1"  # 정규화 · 부분 문자열 규칙의 버전. 규칙을 바꾸면 올린다(설계 1절 재실행 조건)
NORMALIZATION = "per-char NFKC -> lower -> drop whitespace(str.isspace) ; substring, overlapping ; span=[start,end) code points"

Recorder = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
def _finite_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def resolve_repo_path(p: str) -> Path:
    """상대 경로는 레포 루트(pipeline 패키지의 상위 폴더) 기준(③ L12와 통일)."""
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path


def validate_config(cfg: dict[str, Any]) -> None:
    """[judge] 공통 설정 검증(LLM 여부와 무관). 어기면 ValueError. 값은 실험 기준(설계 5절)."""
    if "judge" not in cfg:
        raise ValueError("config에 [judge] 표가 없다")
    j = cfg["judge"]
    missing = [k for k in ("call_scope", "image_width_px", "context_sections", "match_mode", "dict_dir") if k not in j]
    if missing:
        raise ValueError("잘못된 [judge] 설정: 없는 키 " + ", ".join(f"judge.{k}" for k in missing))
    errors: list[str] = []
    if j["call_scope"] not in CALL_SCOPES:
        errors.append(f"judge.call_scope={j['call_scope']!r} — {CALL_SCOPES} 중 하나")
    w = j["image_width_px"]
    if not (isinstance(w, int) and not isinstance(w, bool)) or w < 64:
        errors.append(f"judge.image_width_px={w!r} — 정수 · 64 이상")
    c = j["context_sections"]
    if not (isinstance(c, int) and not isinstance(c, bool)) or c < 0:
        errors.append(f"judge.context_sections={c!r} — 정수 · 0 이상")
    if j["match_mode"] not in MATCH_MODES:
        errors.append(f"judge.match_mode={j['match_mode']!r} — {MATCH_MODES} 중 하나")
    d = j["dict_dir"]
    if not isinstance(d, str) or not d.strip():
        errors.append(f"judge.dict_dir={d!r} — 비어 있지 않은 문자열")
    if errors:
        raise ValueError("잘못된 [judge] 설정: " + "; ".join(errors))


def validate_llm_config(cfg: dict[str, Any], *, need_api_key: bool) -> str:
    """LLM 실행 · 재생 모드의 설정 검증(③ merge.validate_llm_config와 같은 기준). 통과하면 프롬프트 원문을 돌려준다."""
    j = cfg["judge"]
    missing = [k for k in ("llm_model", "llm_temperature", "llm_timeout_s", "prompt_path") if k not in j]
    if missing:
        raise ValueError("잘못된 [judge] LLM 설정: 없는 키 " + ", ".join(f"judge.{k}" for k in missing))
    errors: list[str] = []
    if not isinstance(j["llm_model"], str) or not j["llm_model"].strip():
        errors.append(f"judge.llm_model={j['llm_model']!r} — 비어 있지 않은 문자열")
    t = j["llm_temperature"]
    if not _finite_number(t) or not 0 <= t <= 2:
        errors.append(f"judge.llm_temperature={t!r} — 수치(bool 제외) · 유한값 · 0 이상 2 이하")
    to = j["llm_timeout_s"]
    if not _finite_number(to) or not to > 0:
        errors.append(f"judge.llm_timeout_s={to!r} — 수치(bool 제외) · 유한값 · 0 초과")
    prompt = ""
    pp = j["prompt_path"]
    if not isinstance(pp, str) or not pp.strip():
        errors.append(f"judge.prompt_path={pp!r} — 비어 있지 않은 문자열")
    else:
        path = resolve_repo_path(pp)
        try:
            prompt = path.read_text(encoding="utf-8")
            if not prompt.strip():
                errors.append(f"judge.prompt_path — 프롬프트가 비어 있다: {path}")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"judge.prompt_path — 파일을 UTF-8로 읽을 수 없다: {path} ({e.__class__.__name__})")
    if need_api_key and not os.environ.get(API_KEY_ENV):
        errors.append(f"환경변수 {API_KEY_ENV}가 없다 — 실제 LLM 호출에 필요하다")
    if errors:
        raise ValueError("잘못된 [judge] LLM 설정: " + "; ".join(errors))
    return prompt


def load_dicts(cfg: dict[str, Any]) -> Dictionaries:
    """judge.dict_dir의 정규화 사전 묶음을 읽는다(레포 루트 기준 상대 경로)."""
    validate_config(cfg)
    return load_dictionaries(resolve_repo_path(cfg["judge"]["dict_dir"]))


# ---------------------------------------------------------------------------
# ③-1a 검출 — 정규화 · 오프셋 대응 · 부분 문자열
# ---------------------------------------------------------------------------
def normalize_with_offsets(text: str) -> tuple[str, list[int]]:
    """정규화 문자열과 대응표. offsets[k] = 정규화 k번째 문자가 온 원문 문자 인덱스(코드 포인트)."""
    out: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKC", ch).lower():
            if c.isspace():
                continue
            out.append(c)
            offsets.append(i)
    return "".join(out), offsets


def normalize_pattern(pattern: str) -> str:
    """패턴도 같은 규칙으로. 정규화 후 빈 문자열이면 거부(모든 위치에 매칭되는 문제)."""
    norm, _ = normalize_with_offsets(pattern)
    if not norm:
        raise ValueError(f"빈 패턴(정규화 후 빈 문자열)은 매칭에 쓸 수 없다: {pattern!r}")
    return norm


def find_all(hay: str, needle: str) -> list[int]:
    """겹침을 포함한 모든 출현 위치(오름차순)."""
    if not needle:
        raise ValueError("빈 needle")
    pos: list[int] = []
    i = hay.find(needle)
    while i != -1:
        pos.append(i)
        i = hay.find(needle, i + 1)
    return pos


def raw_span_of(offsets: list[int], start: int, length: int) -> Span:
    """정규화 구간 [start, start+length) → 원문 반개구간 [offsets[start], offsets[start+length-1] + 1)."""
    return Span(start=offsets[start], end=offsets[start + length - 1] + 1)


def detect(section_key: str, blocks: list[TextBlock], view: JudgeDictView, cfg: dict[str, Any]) -> list[Match]:
    """사전 항목(뷰 순서) → 패턴 순 → 블록 순(block_order) → 출현 위치 순으로 원시 매칭을 만든다. finding_key는 None."""
    validate_config(cfg)
    mode = cfg["judge"]["match_mode"]
    if mode != "substring":
        raise NotImplementedError(f"judge.match_mode={mode!r}는 비교 실험용 대안이며 미구현이다(승인 규칙은 substring)")
    normalized = [(b, *normalize_with_offsets(b.source_ko)) for b in sorted(blocks, key=lambda b: b.block_order)]
    matches: list[Match] = []
    n = 0
    for item in view.items:
        for pattern in item.patterns_ko:
            npat = normalize_pattern(pattern)
            for block, ntext, offsets in normalized:
                for p in find_all(ntext, npat):
                    n += 1
                    span = raw_span_of(offsets, p, len(npat))
                    matches.append(
                        Match(
                            match_key=match_key(n), finding_key=None, dictionary_ref=item.id, pattern=pattern,
                            block_key=block.block_key, raw_span=span, matched_text=block.source_ko[span.start:span.end],
                        )
                    )
    return matches


def candidates_of(matches: list[Match]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in matches:
        out.setdefault(m.dictionary_ref, []).append(m.match_key)
    return out


def detect_only(
    section_key: str, blocks: list[TextBlock], cfg: dict[str, Any], *, dicts: Dictionaries | None = None,
    recorder: Recorder | None = None,
) -> DetectionResult:
    """③-1a만 실행한다(`judge --no-llm`). 섹션 이미지가 필요 없어 section_key만 받는다.
    결과는 판정이 아니라 후보 검출이다. 기록은 항상 남기고 저장 실패는 실행 실패다."""
    validate_config(cfg)
    started = datetime.now(timezone.utc)
    d = dicts if dicts is not None else load_dicts(cfg)
    view = d.judge_view()
    bad = [b.block_key for b in blocks if b.section_key != section_key]
    if bad:
        raise ValueError(f"블록의 section_key가 섹션과 다르다: {bad}")
    matches = detect(section_key, blocks, view, cfg)
    checked = JudgeChecked(
        dictionary_version=dict(view.dictionary_version), dictionary_fingerprint=dict(view.fingerprint),
        match_rules_version=MATCH_RULES_VERSION, items=[i.id for i in view.items], llm_called=False,
    )
    result = DetectionResult(section_key=section_key, matches=matches, candidates=candidates_of(matches), checked=checked)
    if recorder is not None:
        ended = datetime.now(timezone.utc)
        recorder(
            {
                "stage": "judge",
                "status": "detect_only",  # 판정 완료가 아니다
                "section_key": section_key,
                "blocks": len(blocks),
                "dictionary_version": checked.dictionary_version,
                "dictionary_fingerprint": checked.dictionary_fingerprint,
                "match_rules_version": MATCH_RULES_VERSION,
                "match_mode": cfg["judge"]["match_mode"],
                "normalization": NORMALIZATION,
                "items_checked": checked.items,
                "matches": [m.model_dump(mode="json") for m in matches],
                "candidates": result.candidates,
                "per_item_counts": {k: len(v) for k, v in result.candidates.items()},
                "started_at": started.isoformat(timespec="seconds"),
                "duration_s": round((ended - started).total_seconds(), 3),
                "note": "검출 전용 — content_findings 없음. 원료·연구원 같은 매칭은 후보이지 판정이 아니다",
            }
        )
    return result


def json_recorder(debug_dir: Path) -> Recorder:
    """`<debug_dir>/<section_key>.json`에 기록을 쓴다."""
    import json

    def write(rec: dict[str, Any]) -> None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"{rec['section_key']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    return write


def run(section: Section, blocks: list[TextBlock], ctx: JudgeContext, cfg: dict[str, Any], *, dicts: Dictionaries | None = None,
        llm=None, recorder: Recorder | None = None) -> JudgeResult:
    """③-1 전체(검출 → 맥락 판정 → 조립) — 맥락 판정 · 조립은 다음 착수 범위(설계 9절 4번). 검출만은 detect_only()."""
    validate_config(cfg)
    raise NotImplementedError("③-1 judge.run(맥락 판정 · 조립)은 아직 구현되지 않았다 — 검출만은 judge.detect_only / CLI judge --no-llm")
