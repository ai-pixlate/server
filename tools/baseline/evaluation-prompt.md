# Baseline Codex 채팅 검수 지침

평가용 LLM API를 사용하지 않는다. 동결된 `protocol-evaluation.md`, `protocol-dataset.md`, `protocol-aggregation.md`와 검증된 기준 자료를 읽고 원본·결과·확대 영역·중간 산출물을 Codex 채팅에서 직접 확인한다.

- `machine_snapshot`과 `service_final`을 각각 평가한다. 없는 산출물을 A/B/C로 바꾸지 않는다.
- H01·H05는 실행·수정 기록에서 계산한다. Codex가 성공 여부나 수정 횟수를 추정하지 않는다.
- H02·H03은 상태·결함·근거를 기록한다. 등급은 도구가 critical/major 건수로 계산한다. critical 0·major 0은 A, critical 0·major 1 이상은 B, critical 1 이상은 C다. minor만 있으면 A다.
- H04는 적용 critical_fact별 값의 정확성과 실제 이미지 판독 가능 여부를 각각 기록한다. measured와 not_applicable·undetermined·no_output·evaluation_failed를 구분한다.
- 결함 유형·심각도·이미지 좌표·관찰·기대값·증거·최초 원인 단계를 기록한다. 원인 불명은 origin_stage=undetermined다. 전파 결함은 root_defect_id로 연결해 중복 가산하지 않는다.
- 사용자 수정 기능이 있어도 자동 결과의 결함 심각도를 낮추지 않는다. 최종 해결은 별도 체크포인트와 수정 근거로 남긴다.
- 허용 표현은 결함이 아니다. 조건부 표현은 필요한 N5 경고가 있으면 결함이 아니고, 필요한 경고가 없으면 결함이다.
- 정상 전체 제외 여부는 사람이 확인한 정책 기준으로 판단한다. 잘못된 전부 제외는 not_applicable로 숨기지 않는다.
- 사람은 K01~K09, critical/major, 불확실한 판단, A등급 일부 표본을 검수한다. 표본 수·검수자·제한 시간은 미정이며 임의로 확정하지 않는다.
- 일부 영역 확인을 원본 전체 검수로 확대하지 않는다. AI 최초 기록을 덮어쓰지 않고 사람 검수·재평가를 이전 ID에 연결한다.
- 실제 채팅 모델·설정·지침·응답을 보존한다. 확인되지 않은 모델명을 자동 대입하지 않는다.

결과는 `docs/ai-baseline/runs/README.md`의 evaluations.jsonl·defects.jsonl 필드와 도구 README의 구조화 관찰값을 사용한다. 실제 관찰하지 못한 값은 미확인 사유와 함께 남긴다. OI-54·NFR-15는 이번 검수에 합산하지 않는다.
