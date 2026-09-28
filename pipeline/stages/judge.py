"""③-1 AI 섹션 판정 — 설정 검증과 뼈대(2026-09-28 착수 범위: 사전 · 타입 · config · 설정 검증까지).

pipeline.md 단계표 ③-1: 섹션 이미지 + 블록(+ 앞뒤 섹션 텍스트)으로 콘텐츠 항목별 해당 · 애매 · 근거만 기록 → content_findings.
설계: docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md(잠정, open-questions.md #60).
- ③-1a 검출(규칙): 사전의 모든 행을 리터럴 매칭해 원시 매칭만 만든다. 정책 필드를 읽지 않는다.
- ③-1b 맥락 판정(LLM): 현지부적합 LC 항목만. 호출 범위는 judge.call_scope.
검출 · 호출 · 조립은 다음 착수 범위이며 run()은 아직 없다(NotImplementedError).
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from pipeline.dictionary import Dictionaries, load_dictionaries
from pipeline.types import JudgeContext, JudgeResult, Section, TextBlock

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KEY_ENV = "GEMINI_API_KEY"
CALL_SCOPES = ("all", "matched")
MATCH_MODES = ("substring", "eojeol_prefix")
MATCH_RULES_VERSION = "match@2026-09-28.1"  # 매칭 규칙(정규화 · 부분 문자열) 버전. 규칙을 바꾸면 올린다(설계 1절 재실행 조건)


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


def run(section: Section, blocks: list[TextBlock], ctx: JudgeContext, cfg: dict[str, Any], *, dicts: Dictionaries | None = None,
        llm=None, recorder=None) -> JudgeResult:
    """③-1 실행 — 검출 · 맥락 판정 · 조립은 다음 착수 범위(설계 9절 3~4번). 시그니처만 고정한다."""
    validate_config(cfg)
    raise NotImplementedError("③-1 judge.run은 아직 구현되지 않았다 — docs/ai/status.md 2절 · 설계 v1 9절")
