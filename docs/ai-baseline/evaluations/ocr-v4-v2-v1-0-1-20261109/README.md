# v1.0.1 OCR Text v2 전체 고정 섹션 평가

2026-11-09 사용자 승인으로 로컬 OCR 보완과 CER v4 측정을 수행했다. GS-01~03 원본 34장에 대응하는 112개 고정 섹션의 독립 OCR 평가다. 최초 Baseline 전체 파이프라인 실행 성능과 구분한다.

## 결과

| 범위 | 계획 섹션 | CER 산정 섹션 | 오류 문자 / 정답 문자 | CER |
|---|---:|---:|---:|---:|
| 전체 | 112 | 110 | 10,074 / 23,543 | 42.79% |
| GS-01 | 25 | 25 | 1,694 / 6,100 | 27.77% |
| GS-02 | 48 | 48 | 6,290 / 9,903 | 63.52% |
| GS-03 | 39 | 37 | 2,090 / 7,540 | 27.72% |

전체 출력 112개가 검증됐다. 110개는 measured, 문자 없는 GS-03_003/sec_1_01과 GS-03_007/sec_1_01은 not_applicable이다. 이 2개의 출력 문자 수도 results-112.json에 보존한다. 실패·누락·undetermined·evaluation_failed는 0개다. 전체 CER은 Σ(S+D+I)/ΣN이다. 치환 1,986자, 삭제 2,036자, 삽입 6,052자다.

## 실행과 출처

- 제품 v1.0.1, SHA bfa3b700144467f4adf7d6802b7b2e93791fe6de.
- 재사용 70개 = 최초 Baseline 67개 + 2026-11-05 별도 보완 3개. 기존 파일 색인과 입력 이미지·객체·분할 기록 및 OCR 반환 경계를 재검증했다.
- 신규 보완 42개 = GS-01 2개 + GS-02 4개 + GS-03 36개. 모두 성공, 엔진 호출 총 42회, 섹션별 1회. 두 번째 엔진 호출과 네트워크를 차단했다. 재시도·fallback 0회.
- Windows CPU, Paddle 3.3.1 / PaddleOCR 3.7.0 / PaddleX 3.7.2, PP-OCRv5_server_det + korean_PP-OCRv5_mobile_rec. 패키지·설정·모델 자산 해시는 기존 runtime 기록과 일치했다. 상세는 supplement/environment.json과 model.json에 보존했다.
- 보완 OCR 처리시간 합계 11분 34초, 모델 초기화 0분 13초, 실행 시작~완료 11분 57초. 시작~완료에는 초기화·저장 등 부대 시간이 포함된다.
- 외부 API 0회, 추가 API 비용 USD 0. 로컬 전력·장비 사용 비용은 미산정이다.
- 실제 서비스의 pipeline.stages.ocr.run 반환을 저장했다. v1.0.1에 없는 이후 버전 보정 기능은 적용하지 않았다.

## 동결 기준과 해석 한계

정답은 [OCR Text v2](../../references/ocr-text/ocr-text-v2-20261109/README.md)의 112개·1,861줄이다. lines[] 순서, NFC·지정 공백 제거, 문자 가중 합산을 그대로 사용했다.

16개 섹션의 판독 불가·미전사 부분은 사용자 결정으로 빈 정답을 수용했다. 해당 영역의 OCR 출력도 삭제 없이 연결하므로 추가 문자가 편집거리에 반영될 수 있다. 신규 100개 중 사용자 검수는 47개, AI만 검수한 섹션은 53개다. 오류 발견 0건 회신의 범위는 47개에 한정한다. 같은 채팅의 AI 전사·재대조이며 정확한 Codex 모델·설정 증빙은 미확보다.

기존 12개 부분 CER 13.69%(730/5,333자)는 기존 경로에서 보존한다. 이번 112개 평가와 분모가 다르므로 성능 개선·악화 수치로 직접 비교하지 않는다. 추가 정답 수정·재검수는 수행하지 않았다.

## 보존 자료

- initial/ — 최초 Baseline 출력 연결 근거와 제외·보완 사유, 입력 검증.
- reused-inputs.json / reuse-validation.json — 기존 70개 재검증.
- execution-plan.json / supplement.py / console.log / supplement/ — 승인 범위, 42개 입력·시도·원시 반환·진단·시간·해시·환경.
- inputs.json / validation.json / results-112.json / summary.json — 전체 색인·검증·섹션별 및 GS별 집계.
- protected-files.json / integrity.json — 기존 동결본·12개 평가 파일 32개 불변 및 공식 회차 색인 내 2,027개 파일 해시 검증.
- reproduction/ / reproduction-index.json — 이번 채점·연결 도구와 고정 입력 색인 스냅샷.
- file-index.json — 이번 결과 파일별 SHA-256과 크기(색인 자체 제외).

기존 원시 결과는 참조하고 신규 결과만 이 폴더에 저장했다. 고정 입력 이미지 112개는 inventory에 기록한 로컬 경로를 사용한다. 이미지·모델 자산 및 기존 회차 증거는 이 폴더만 복사해서 확보되지 않으므로 다른 장비 재현 시 해당 자료를 함께 준비하고 해시를 대조해야 한다.

## 재현과 다음 통합 비교

이번 입력만 재채점할 경우 저장소 루트에서 다음 명령을 실행한다. 출력 경로는 새 파일로 지정한다.

```text
python -m tools.baseline.ocr verify --truth docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109
python -m tools.baseline.ocr score --truth docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109 --inputs docs/ai-baseline/evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json --out <새 결과.json>
```

supplement.py는 이미 실행된 supplement 폴더가 있으면 재실행을 거부한다. 동일 보완 회차를 다시 생성하지 않는다. assemble-results.py는 검증·집계 재현용이며 기존 출력 파일이 있으면 덮어쓰기를 거부한다.

2·3차 통합은 같은 이미지 112개 바이트, 같은 정답 동결본·명세·채점기 해시로 새 제품 SHA의 OCR 최종 반환을 별도 회차에 수집한다. 신규 버전의 출력 경계·환경·설정·모델 자산 해시를 다시 검증하고 같은 not_applicable 2개와 유효 분모를 공개한다. 제품 파이프라인 전체 평가는 별도로 진행한다.
