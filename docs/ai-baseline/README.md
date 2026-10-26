# AI baseline

> 상태: `v1-draft` · `pm_review_pending` · 1~5단계 사용자 확정 · PM 승인 전 · 공식 실측 승인 전

릴리즈 1.0.0과 후속 고도화 버전의 품질을 같은 평가셋과 방법으로 비교하기 위한 문서다. 제품 버전과 평가 명세 버전은 구분한다.

## 기준과 승인 상태

최신 입력본은 [Baseline 평가 명세 v1 — 1~5단계 확정안](https://app.notion.com/p/40587148aa578152805dd9687049ef82)이다. 반영한 최종 편집 시각은 **2026-10-26 15:19:17 KST**이며 H07 분리 정의와 분모·0건 표시·`no_output` 보정 기준을 포함한다.

이번 baseline 문서 정비의 우선순위는 저장소 작업 규칙 → 위 Notion 입력본 → 기존 baseline 문서다. Notion은 문서 정비 입력본이며 제품 코드·DB/API·AI 파이프라인 계약의 변경 근거가 아니다. PM 승인·병합 이후 승인된 GitHub 명세와 커밋을 실행 정본으로 사용한다. 현재 문서는 실행 승인이나 실측 완료를 뜻하지 않는다.

1~5단계 사용자 확정 범위와 미확정 후속 작업은 [계획](plan.md#확정-범위와-후속-작업)에서 관리한다. 기존의 명세 전체 확정 표현과 7개 품질·3개 실행 지표 체계는 최신 체계로 대체했다.

## 문서 구성과 읽는 순서

| 순서 | 파일 | 소유 범위 |
|---|---|---|
| 1 | [plan.md](plan.md) | 목적·대상 버전·범위·후속 작업 상태 |
| 2 | [decisions.md](decisions.md) | 보존할 실행 환경 결정·실행 예외 |
| 3 | [dataset.md](protocols/v1/dataset.md) | 고정 평가셋·식별자·기준 단위·좌표·처리 기대값 |
| 4 | [evaluation.md](protocols/v1/evaluation.md) | 체크포인트·실패와 심각도·판정 기준·평가 및 검수 제안 |
| 5 | [aggregation.md](protocols/v1/aggregation.md) | 핵심 품질 지표 8개·단계 진단 지표 8개·Baseline 보조 운영지표 7개의 단위·분자·분모·계산식·예외·비교 |
| 6 | [execution.md](protocols/v1/execution.md) | 실행 환경·입력·비용 승인·보관 원칙·후속 실행 설계 제안 |
| 필요 시 | [runs/README.md](runs/README.md) | 실제 회차 결과 |
| 필요 시 | [comparisons/README.md](comparisons/README.md) | 버전 비교 결과 |

지표 명칭과 계산 정의는 aggregation.md를 소유 문서로 하고, 다른 문서는 링크로 참조한다. 후속 검토 제안은 현재 확정 실행 규칙이 아니다.

[기존 측정 도구 안내](../../tools/baseline/README.md)는 기존 구현의 사용법·검증 범위만 설명한다. 도구 출력이 최신 명세와 일치한다고 가정하지 않으며 도구 정비·실행은 별도 작업이다.

비용이 발생하는 모델 호출은 [사전 예산 승인](protocols/v1/execution.md#ai-실행자의-사전-예산-승인)을 따른다. 문서 검토·승인만으로 사전 점검, 생성, AI 평가 호출을 허용하지 않는다.
