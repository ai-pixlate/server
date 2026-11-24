# 최초 Baseline GS-01 부분 평가 — AI 최초 결과

**범위는 GS-01 2장뿐이며 전체 34장 성능이 아니다. PM의 생성 결과 사람 검수는 아직 0건이다.**

원본 회차는 `baseline-v1-0-1-gs-v1-20261030-01`, 제품은 v1.0.1 `bfa3b700144467f4adf7d6802b7b2e93791fe6de`다. 원본 회차의 이미지·manifest·로그를 수정하지 않고 GS-01 증거 사본을 별도 묶었다. 이 폴더는 새 생성 회차가 아니다.

## 기준 및 추적

- [GS-01 동결 안내](../../references/gs-v1/gs01-v1-20261103/README.md), [동결본 사본](frozen-reference/reference_units.jsonl), [원 승인](frozen-reference/pm-approval.json).
- [manifest](manifest.json)의 source_keys는 2개이며 GS-02·03은 포함하지 않는다. 원본의 dataset manifest는 34장 맥락 증거로 그대로 보존했다.
- [평가용 기준](reference_units.jsonl)은 동결본의 evaluation_run_id 및 근거 상대경로만 바꾼 58개 사본이다. 두 SHA-256과 변환 설명은 manifest.reference_freeze에 있다.
- [실행 기록](execution.jsonl)은 GS-01에만 연결된 행을 추출하고 evaluation_run_id만 바꿨다. 별도 비용 승인이나 새로운 모델 사용량을 뜻하지 않는다.
- [평가 지침](evidence/evaluation-instructions.md), [AI 관찰 기록](evidence/ai-observations.json), [이미지 비교와 섹션 연결](evidence/views/index.json), [평가 JSONL](evaluations.jsonl), [결함 JSONL](defects.jsonl), [지표](metrics.csv).
- AI 기록은 Codex 채팅의 직접 이미지 대조다. 실제 모델 표시명/설정과 전체 채팅 내보내기 증빙은 확보하지 못해 null/사유로 기록했다. 재추론의 동일 결과까지 보장하지 않는다.

## 부분 수치 — PM 검수 전

| 지표 | 결과 | 분모·예외 |
|---|---|---|
| H01 기대 산출물 생성률 | 1/1 = 100% | GS-01_001 정상 전체 제외 1장 미적용 |
| H02 최초 결과 등급 | A 0 · B 0 · C 1 (C 100%) | measured 1장, 정상 제외 1장 |
| H03 최종 결과 사용 가능률 | A 0/1 = 0% · B 0 · C 1 | measured 1장, 정상 제외 1장 |
| H04 machine_snapshot | 23/28 = 82.14% | 정상 제외 원본의 핵심 사실 4개 미적용 |
| H04 service_final | 23/28 = 82.14% | 동일 분모, 별도 체크포인트 기록 |
| H04 평가 가능률 | 각 28/28 = 100% | undetermined/no_output/evaluation_failed 0개 |
| H05 | 미측정(별도 서비스 평가 예정) | 0건 성과로 해석하지 않음 |

각 체크포인트 결함은 **critical 7 · major 20 · minor 2**로 기록됐다. A/B/C는 기존 도구가 이 결함 수에서 계산했다. 23개 machine_snapshot과 service_final의 해당 섹션이 모두 픽셀까지 같으므로 동일 결함이 두 체크포인트에 남는다. 58개 기록을 서로 다른 58개 근본 결함으로 더하지 않는다. 영향 원본은 GS-01_002 한 장이다.

H04 비보존 단위는 GS-01_002 REF-001(제품명 겹침), REF-002(50ml→50m), REF-007(95% 항목 문구 중첩), REF-020(15.92% 가독성), REF-025(6.78% 가독성)다. 숫자가 번역 기록에 존재해도 이미지에서 읽기 어려운 경우 보존 통과로 처리하지 않았다. 가독성의 경계 사례는 PM 재검수 대상이다.

주요 결함은 용량 단위 누락, 원문에 없는 broad spectrum SPF 추가, Good all goodal 보호 로고 훼손, 반복적인 글자 겹침·잘림, N5의 필요한 조건부 경고 미표시다. 경고는 원 판정 DB에 존재하지만 실제 N5 전달 JSON에는 없었다. 생성 도구/제품 경계의 최초 누락 위치는 확정하지 않고 undetermined로 기록했다. 허용 acne-prone 및 DarkSpotCoverage 자체는 결함으로 세지 않았다.

## 검증과 사람 검수

기존 비과금 합성 테스트 15개 통과. [사전 검증](evidence/preparation-validation-final.json)에서 동결 근거·2원본 범위·H01 1/1·타 상품 집계 제외·미평가 A 오인 방지를 확인했다. [채점 데이터 검증](evidence/scoring-validation.json)은 오류 0건이다. 처음 준비 시 누락됐던 dataset manifest 사본은 원본 해시와 일치하는 파일로 보완한 뒤 채점을 시작했다. 원본 2,028개 파일의 변경 여부는 최종 무결성 기록으로 확인한다.

[human_review.csv](human_review.csv)는 헤더만 있다. PM은 K01~03, critical/major 전건, 원인 undetermined 3개, H04 비보존 5개 및 경계 사례를 확인해야 한다. A등급은 없어 A 표본 대상도 없다. 기준 자료 승인으로 사람의 결과 검수를 대체하지 않는다. 수정 의견은 기존 AI 기록을 덮어쓰지 않고 human_review 또는 supersedes 연결로 남긴 후 집계한다.

기존 도구는 지정한 manifest 범위를 독립 집계하므로 코드/지표 변경은 없었다. 재집계는 저장소 Python에서 `tools.baseline.protocol.aggregate(Path(이_폴더))`로 가능하며, `aggregate.ai.json`은 최초 AI 집계의 보존본이다. 사본의 과거 실행 안내와 비용 상태는 해당 시점의 증거이며 새 실행 허가가 아니다.
