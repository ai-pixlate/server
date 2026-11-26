# H01~H04 공식 평가 입력 준비

`evaluation_prepare.py`는 동결 정답·기존 실행 기록을 기존 `protocol.py` 입력 형식으로 연결한다. 제품 실행, 채점, A 표본 추출, 사람 승인은 수행하지 않는다. 기존 생성 실행기 `execute`를 이 입력에 사용하지 않는다.

## 준비 및 복원

저장소 루트에서 실행하며 출력은 반드시 존재하지 않는 새 경로여야 한다.

```powershell
python -B -m tools.baseline.evaluation_prepare --output .cache/new-evaluation-preparation
python -B -m tools.baseline.evaluation_prepare --materialize docs/ai-baseline/preparations/h01-h04-official-20261124 --output .cache/official-evaluation-input
```

첫 명령은 최신 checkout의 지침 사본과 입력·예외 계획을 생성한다. 재현에는 보존된 준비 묶음과 두 번째 명령을 사용한다. `file-plan.json`은 저장소 상대 경로·커밋·SHA-256을 고정한다. 로컬 파일이 다르면 고정 커밋에서 읽고 해시가 맞지 않으면 중단한다. Git 이력이 필요하며 shallow checkout에서 근거 커밋이 없으면 복원하지 않는다. 네트워크 호출은 하지 않는다.

복원 시 약 400 MB의 기존 증거를 로컬 출력 아래에 복사한다. 원시 회차는 `generation/` 아래에 바이트 그대로 두고, 루트 execution.jsonl은 회차 ID·근거 경로만 변경한 파생 사본이다. 원시 파일과 이미지는 새 PR에 중복 커밋하지 않는다. 도중 실패한 출력은 평가에 사용하지 않으며 새 경로에서 원인을 해결한 뒤 다시 준비한다.

## 승인·변환·예외

- 통합 `reference_version`은 기존 승인 버전을 묶는 색인의 ID다. 새 GT 승인이나 재동결이 아니다. 각 원본 버전·파일 해시·줄 번호·변환 전후 JSON 객체 해시는 `reference-origins.json`에 보존한다.
- 기대값·ID·좌표·줄 순서·중요도·H01/H04 적용 여부·기존 승인자와 시각은 변경하지 않는다. 회차 ID·통합 버전·로컬 근거 경로만 바꾼다.
- manifest의 `reference_freeze.verified_at`은 두 기존 승인 기록 시각 중 나중 값이며 새 승인 시각이 아니다. GS-02·03은 사용자 전달 PM 구두 승인이고 실제 구두 승인 시각은 미확인이다.
- `exception-plan.json`의 `planned_status`는 **사전 준비용 필드**이며 평가 enum이나 평가 결과가 아니다. `pending_visual_evaluation`을 evaluations.jsonl에 기록하지 않는다. 실제 평가 시작 후 근거 확인을 거쳐 기존 measured/not_applicable/no_output/undetermined/evaluation_failed로 기록한다.
- GS-03 18장은 한 상품 분석 실패의 영향이다. H01 실패 분모에는 남기되 품질 결함 18건을 자동 생성하지 않는다.
- GS-01 Pilot 기록은 가져오지 않는다. 별도 공식 판정을 기록해야 한다.

## 채점 전·검수 후 확인

복원 마지막에 `protocol.validate()`와 동결 근거 해시 검사를 수행한다. 성공해도 Codex 실제 세션·모델 확인 근거·지침·응답 기록은 별도 필요하다. 복원본의 evaluations/defects JSONL과 human_review CSV는 비어 있다.

실제 평가자는 원본·최초 섹션 미리보기 전체·N6 최종 이미지를 각각 확인해야 한다. H04는 이미지 가독성까지 확인한다. 집계는 공식 채점 승인 후에만 수행한다.

PM 검수는 준비 묶음의 `review-checklist.json`을 사용해 K01~K09, critical/major, 불확실·예외 판단을 전수 확인하고 AI 기록과 구분한다. K04의 정당한 제외, K07~K09의 미생성을 실제 품질 결함으로 자동 변환하지 않는다. A 표본은 명세의 상품×체크포인트별 1장·최대 6쌍 SHA-256 정렬 추천안을 제시할 뿐, 사용자/PM 채택 전 선정하지 않는다. 표본 채택은 AI 1차 채점 착수와 구분하되 PM 최종 검수 완료 전 필요하다.

검증: `python -B -m unittest discover -s tests -p test_baseline_evaluation_prepare.py -v`. 기존 분모·등급·예외·사람 검수 회귀는 test_baseline_protocol.py 및 test_baseline_scope.py를 사용한다. 실제 데이터의 aggregate/export는 사전 준비에서 실행하지 않는다.
