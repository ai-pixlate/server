"""③-1' 정책 적용 — 설정 검증과 뼈대(2026-09-28 착수 범위: 사전 · 타입 · config · 설정 검증까지).

pipeline.md 단계표 ③-1': AI 판정 결과 × 정책 데이터 → 섹션별 유지 · 제외 · 확인 필요를 로컬 로직이 계산 → section_verdict 후보.
구현 소유자 AI 서버(open-questions.md 5절 #6). 설계: docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md 3절(잠정, #60).
- 순서: 적용 행 선택(regulatory_class) → 예외 쌍 · 겹침(개별 매칭 단위, D2 · D2-b) → 매핑(policy_rules.json) → 집계.
- 미완료 · 누락 입력(JudgeResult.status != ok, regulatory_class 누락)에서는 포함 권고를 만들지 않는다(3.4절, D7 · D9-c).
run()은 아직 없다(NotImplementedError).
"""
from __future__ import annotations

from typing import Any

from pipeline.dictionary import Dictionaries
from pipeline.types import JudgeContext, JudgeResult, PolicyResult, TextBlock


def validate_config(cfg: dict[str, Any]) -> None:
    """[policy] 설정 검증. 어기면 ValueError."""
    if "policy" not in cfg:
        raise ValueError("config에 [policy] 표가 없다")
    p = cfg["policy"]
    if "problem_text_max_chars" not in p:
        raise ValueError("잘못된 [policy] 설정: 없는 키 policy.problem_text_max_chars")
    n = p["problem_text_max_chars"]
    if not (isinstance(n, int) and not isinstance(n, bool)) or n < 1:
        raise ValueError(f"잘못된 [policy] 설정: policy.problem_text_max_chars={n!r} — 정수 · 1 이상")


def run(judge_result: JudgeResult, blocks: list[TextBlock], ctx: JudgeContext, cfg: dict[str, Any], *, dicts: Dictionaries) -> PolicyResult:
    """③-1' 실행 — 적용 행 선택 · 예외 · 매핑 · 집계는 다음 착수 범위(설계 9절 5번). 시그니처만 고정한다(블록은 정상 입력, 3.5절)."""
    validate_config(cfg)
    raise NotImplementedError("③-1' policy.run은 아직 구현되지 않았다 — docs/ai/status.md 2절 · 설계 v1 9절")
