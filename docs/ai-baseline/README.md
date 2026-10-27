# AI baseline

> 상태: 1~5단계 PR #89 병합 기준 유지 · 6~8단계 프로토콜 문서화 초안 · 공식 실측 미실행

릴리즈 1.0.0과 후속 고도화 버전의 품질을 같은 평가셋과 방법으로 비교하기 위한 문서다. 제품 버전과 평가 명세 버전은 구분한다.

## 기준과 승인 상태

1~5단계 평가 기준은 [PR #89](https://github.com/ai-pixlate/server/pull/89)의 develop 병합 커밋 `33bd1efe3f415a756879e1a391db18577c865b7c`을 기준으로 유지한다. GitHub 문서와 승인 커밋이 공식 정본이며 Notion은 계획 입력·이력·링크 용도다. 두 내용이 다르면 GitHub 기준을 따른다.

PR #89의 과거 입력본은 [Notion 1~5단계 확정안](https://app.notion.com/p/40587148aa578152805dd9687049ef82)(당시 최종 편집 2026-10-26 16:59:09 KST)이며 이력 링크로만 남긴다.

이번 6~8단계 구체화는 2026-10-27 사용자가 전달한 합의와 PM 최종 의견을 반영한 문서화 초안이다. 제품 코드·DB/API·AI 파이프라인 계약을 변경하지 않으며, 문서 작성은 실측·제품 생성·유료 작업 승인이 아니다. 평가용 LLM API는 사용하지 않고 Codex 채팅에서 직접 검수한다.

단계별 상태와 목표일은 [계획](plan.md#확정-범위와-후속-작업), 평가·사람 검수 절차는 [평가 문서](protocols/v1/evaluation.md#ai-평가-방식), 결과 파일의 최소 필드와 연결은 [회차 결과 구조](runs/README.md)가 소유한다.

## 문서 구성과 읽는 순서

| 순서 | 파일 | 소유 범위 |
|---|---|---|
| 1 | [plan.md](plan.md) | 목적·대상 버전·범위·후속 작업 상태 |
| 2 | [decisions.md](decisions.md) | 보존할 실행 환경 결정·실행 예외 |
| 3 | [dataset.md](protocols/v1/dataset.md) | 고정 평가셋·식별자·기준 단위·좌표·처리 기대값 |
| 4 | [evaluation.md](protocols/v1/evaluation.md) | 체크포인트·실패와 심각도·판정 기준·Codex 평가 및 사람 검수 |
| 5 | [aggregation.md](protocols/v1/aggregation.md) | 핵심 지표 5개·원인 단계별 결함 집계·Baseline 보조 운영지표 7개·파생 분석·별도 평가 트랙의 계산·예외·비교 |
| 6 | [execution.md](protocols/v1/execution.md) | 실행 환경·입력·비용 승인·보관 원칙·후속 실행 설계 제안 |
| 필요 시 | [runs/README.md](runs/README.md) | 8단계 결과 구조·필드·연결 관계·실제 회차 결과 |
| 필요 시 | [comparisons/README.md](comparisons/README.md) | 버전 비교 결과 |

지표 명칭과 계산 정의는 aggregation.md를 소유 문서로 하고, 다른 문서는 링크로 참조한다. 미정으로 표시한 담당자·제한 시간·표본 세부 조건을 임의로 채우지 않는다.

[기존 측정 도구 안내](../../tools/baseline/README.md)는 기존 구현의 사용법·검증 범위만 설명한다. 도구 출력이 최신 명세와 일치한다고 가정하지 않으며 도구 정비·실행은 별도 작업이다.

비용이 발생하는 모델 호출은 [사전 예산 승인](protocols/v1/execution.md#ai-실행자의-사전-예산-승인)을 따른다. 문서 검토·승인만으로 사전 점검·제품 생성·실측을 허용하지 않는다.
