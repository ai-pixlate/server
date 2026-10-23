# AI baseline

릴리즈 1.0.0과 후속 고도화 버전의 품질을 비교하기 위한 측정 문서다. 제품 버전과 평가 명세 버전은 구분하며, 같은 평가 기준과 측정 방식으로 고도화 전후를 비교한다.

측정 계획과 평가 명세는 확정됐다. 회차별 실제 값의 기록, 기준 자료 작성과 도구 준비는 명세에 따른 실행 작업이다. 계획 결정 완료는 공식 측정 완료를 의미하지 않는다.

## 문서 구성

| 파일 | 역할 | 내용 |
|---|---|---|
| [README.md](README.md) | 진입점 | 문서 구성과 읽는 순서 |
| [plan.md](plan.md) | 측정 계획 | 대상 버전과 측정 방향 |
| [decisions.md](decisions.md) | 확정 결정 | 측정 목적·로컬 실행 환경과 1.0.0 baseline의 OCR 표본 실행 제한 해제 |
| [protocols/v1/dataset.md](protocols/v1/dataset.md) | 평가 대상과 기대값 | 원본 34장, K01~K09와 좌표, 처리 기대값·기준 자료 |
| [protocols/v1/evaluation.md](protocols/v1/evaluation.md) | 평가와 검수 | 10개 지표, A/B/C 기준, AI 평가·사람 검수 |
| [protocols/v1/aggregation.md](protocols/v1/aggregation.md) | 집계와 비교 | 원본별 집계, 예외 상태·원본 이미지 기술 처리 완료 비율·시간, 판정 혼동행렬과 결함 추적 |
| [protocols/v1/execution.md](protocols/v1/execution.md) | 실행과 결과 관리 | 격리 BE 실행 경로·입력·사전 점검, 비용·도구·보관·완료 기준·보류 범위 |
| [runs/README.md](runs/README.md) | 회차별 결과 | 실제 측정 결과의 보관 위치 |
| [comparisons/README.md](comparisons/README.md) | 버전 비교 결과 | 고도화 전후 비교 결과의 보관 위치 |

## 읽는 순서

처음 작업하는 에이전트는 이 README에서 문서 역할을 확인하고, 다음 순서로 읽는다.

1. [plan.md](plan.md)와 [decisions.md](decisions.md): 측정 목적·범위·로컬 실행 환경과 기존 OCR 실험에 대한 적용 예외를 확인한다.
2. [dataset.md](protocols/v1/dataset.md): 어떤 원본과 영역을 어떤 기대값으로 평가하는지 확인한다.
3. [evaluation.md](protocols/v1/evaluation.md): 지표·등급과 AI·사람의 판정 책임을 확인한다.
4. [aggregation.md](protocols/v1/aggregation.md): 평가 단위·분모·예외 상태와 버전 비교 규칙을 확인한다.
5. [execution.md](protocols/v1/execution.md): 실제 실행 조건·예산·도구·보관·완료 조건을 확인한다.

공식 측정 준비에는 위 명세 네 파일을 모두 적용한다. 회차 결과와 비교 결과는 해당 작업 시 `runs/`와 `comparisons/`에서 확인한다. 평가 명세와 실제 측정 결과는 구분해 보관한다.

실행·기록 도구의 사용법과 검증 범위는 [측정 도구 안내](../../tools/baseline/README.md)를 따른다. 도구의 준비·잠정 집계 출력은 공식 측정 결과와 구분한다.

실행하는 AI 에이전트는 비용이 발생하는 모델 호출 전에 [사전 예산 승인](protocols/v1/execution.md#ai-실행자의-사전-예산-승인)에 따라 예상 금액·실행 범위·상한을 제시하고 사용자 승인을 받아야 한다. 이 규칙은 사전 점검과 AI 평가에도 적용한다.
