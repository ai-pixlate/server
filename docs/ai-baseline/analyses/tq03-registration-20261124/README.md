# TQ03 공식 관리 등록 — 기존 기록 대조

2026-11-24 사용자 요청에 따른 명세 정비 근거다. 기준 develop은 `8c0f850bc0c3e523002c1fdf4958857786b7bde9`이며, 기존 독립 PoC·Baseline·정답·평가 결과는 수정하지 않는다. 새 모델 호출·공식 채점은 하지 않았다.

**단일 평가모델(M-Prometheus 7B) · 독립 검증 없음 · 출력 형식 완전 충족 0개 · 점수 확보 449/491개**

## 대조한 자료

저장소 외부 `fluency-poc-20261119/full-eval-20261120/`의 `full_runner.py`, `protocol.py`, `validated-run-config.json`, `targets.jsonl`, `received-20261121/results.jsonl`, `summary.json`, `receipt-validation.json`을 읽었다. 파일별 SHA-256과 재계산 결과는 [audit.json](audit.json)에 기록한다. 원본 실행 ZIP `pixlate-full-eval-results.zip` SHA-256은 `fa37c99f5ff6cab869f9078e056b9588248d5ea5be4cb28bd60fda3a5245fb1e`다.

기본 표지 점수 449개 합계 970, 평균 2.1603563474를 원시 출력에서 재확인했다. 본문에만 있는 기존 GS-02-block-430의 2점은 기본 통계에서 제외한다. 소수 표지·복수의 유효 표지는 현재 결과에 없지만, 2026-11-24 후속 사용자 승인에 따라 정규식의 기존 동작을 포함한 측정 규칙을 그대로 확정했다. [확정 측정 명세](../../protocols/v1/tq03.md)을 참조한다.

팀이 같은 검증을 재현하려면 해시가 같은 외부 원본 묶음이 필요하다. 원시 텍스트·모델 가중치를 이번 PR에 복제하지 않았으며, 접근 가능한 보관 위치 확정은 별도 남은 사항이다.

## 후속 승인에 따른 측정 기준 확정

2026-11-24 사용자가 기존 방식을 그대로 공식 측정 기준으로 채택하도록 승인했다. 추출·본문 점수 보조 표시·집계·재개·명시 재시도·현재 항목 파일 선택은 기존 코드 동작으로 확정하며 새 예외 정책이나 최대 재시도 횟수를 추가하지 않는다. 버전 대응·비교·발표 방식은 측정 선행 조건이나 미결 사항에서 제외했다. 원시 자료의 팀 접근성·가중치 이용 조건은 별도 확인 상태다.

[확정 검증 기록](confirmation-validation.json): 원본 ZIP의 experiment/items를 임시 폴더에 복원하고 **기존 `full_runner.export`를 변경 없이 실행**했다. 결과 491개 JSON 객체 전체와 summary 객체 전체가 기존 자료와 일치했다. 기본 표지 점수 449개, 평균 2.1603563474 및 기존 본문 보조 통계도 그대로다. attempts 475개는 고유 키 475개이며 각 items 파일과 일치해 신규 평가의 재시도는 없음을 확인했다. 모델 실행 함수는 호출하지 않았고 원본 결과 해시는 유지됐다. 기존 audit.json은 확정 전 대조 이력으로 보존한다.

```powershell
python -B docs/ai-baseline/analyses/tq03-registration-20261124/audit.py <full-eval-20261120 경로>
```

이 스크립트는 읽기 전용이며 stdout으로 결과를 낸다. 모델·API·원시 산출물 생성이나 H01~H04 집계를 수행하지 않는다. 해시가 다른 입력은 중단하므로 새 최종판 채점기로 사용하지 않는다.

## H01~H04 회귀 확인

기존 합성 회귀 32개 통과:

```powershell
python -B -m unittest discover -s tests -p test_baseline_evaluation_prepare.py -v
python -B -m unittest discover -s tests -p test_baseline_protocol.py -v
python -B -m unittest discover -s tests -p test_baseline_scope.py -v
```

- 준비 변환·정답 보존·예외 연결 5개, 분모·등급·검수·예산 및 호출 차단 15개, 동결·범위·승인 회귀 12개다.
- `preparations/h01-h04-official-20261124/`의 입력·지침 사본, `references/`, `evaluations/`, 기존 `runs/`, `tools/baseline/`와 `tests/`는 이 변경에서 수정하지 않는다.
- H01/H04 분모와 H02/H03 등급, K01~K09 및 PM 검수 규칙은 유지한다. 공식 준비 묶음의 기존 해시/기준 커밋을 새 명세로 다시 찍지 않는다.
- TQ03 등록과 H01~H04 채점 승인은 별개다. 합성 테스트 통과는 실제 이미지 검수·공식 채점 수행을 뜻하지 않는다.
