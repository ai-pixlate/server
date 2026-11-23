# Baseline 기준 자료 — PM 최종 의견 반영 후보

> 2026-11-23: PR #93의 PM 회신을 반영했다. **GS-02·03은 미동결이며 이번 반영본의 확인·동결 승인은 별도**다. GS-01의 기존 58개 동결본과 모든 실측·평가 결과, 9/30 사전은 변경하지 않았다.

[최신 검토표](review-list.md) · [반영 안내](pm-review.md)

## 현재 자료와 보존 이력

| 파일 | 범위·용도 |
|---|---|
| [reference_units.pm-revised-20261123.jsonl](artifacts/reference_units.pm-revised-20261123.jsonl) | GS-02·03 32장·281개 수정 후보. 기존 273개 ID 유지, 신규 8개 추가. verified_*는 null |
| [pm-revision-index-20261123.json](artifacts/pm-revision-index-20261123.json) | 후보·근거 해시, 32장 원본 경로·크기·해시, PM 섹션 기준, 기존 GS-01 동결본 참조 |
| [pm-revision-changes-20261123.json](artifacts/pm-revision-changes-20261123.json) | 항목별 이전값→수정값, 적용한 코멘트와 처리 이력 |
| [pm-feedback-20261123.json](artifacts/pm-feedback-20261123.json) | PM 11/15(11/16 편집)·11/17·11/18·11/20 회신 원문과 URL |
| [GS-01 기존 동결본](../../references/gs-v1/gs01-v1-20261103/README.md) | 2장·58개. 변경 없이 참조하며 이 폴더에서 재동결하지 않음 |
| [reference_units.draft.jsonl](artifacts/reference_units.draft.jsonl) | 초기 34장·324개 AI 초안. **과거 이력**이며 최신 기대값으로 사용하지 않음 |
| [draft-index.json](artifacts/draft-index.json) | 초기 작성 당시 버전·해시·불확실성·승인 상태. 역사적 기록 그대로 보존 |
| [policy-source-index.json](artifacts/policy-source-index.json) · [protocol-dataset.txt](artifacts/protocol-dataset.txt) | 초기 사전 근거·당시 명세 사본. 최신 PM 결정이나 현재 명세를 대신하지 않음 |

최신 조회 범위는 GS-01 58개 + GS-02·03 281개 = **34장·339개**다. 초기 324개와의 차이는 GS-01 기존 추가 7개 및 이번 GS-02·03 추가 8개다. 과거 작성 우선순위(불확실한 21장 → 나머지 13장)는 최초 요청 이력이며 현재 미회신 수가 아니다.

## 회신 우선순위와 핵심 반영

1. GS-02는 [11/15 회신](https://github.com/ai-pixlate/server/pull/93#issuecomment-6880930734), GS-03은 [11/17 회신](https://github.com/ai-pixlate/server/pull/93#issuecomment-6936121830)을 기본으로 적용한다.
2. 같은 항목의 [11/18 정정](https://github.com/ai-pixlate/server/pull/93#issuecomment-6971755479) 및 [11/20 최종 답변](https://github.com/ai-pixlate/server/pull/93#issuecomment-7029221585)을 우선한다. 정정 전 문구는 회신 원문·변경 이력에만 남긴다.
3. 중요도·H04 두 체크포인트 적용 여부, H01 원본별 적용 여부, 좌표와 보호 영역 안 번역 예외를 명시했다. GS-02_001·002는 전체 제외가 정답이며, GS-03_018은 A~E 유지·F 상품정보 표 제외다. 실제 AI 분할·제외 결과를 정답으로 삼지 않는다.
4. 착색 케어 보류를 해소하고 RG-025를 적용한다. 착색 미백은 RG-025 겉보기 표현 하나 또는 RG-024·025 각각의 대체 표현 모두 허용한다. 규제 표현 적합성과 H04 사실 보존은 구분한다. 상세 정책의 소유 문서는 [dataset.md](../../protocols/v1/dataset.md#규제현지-판정의-기대-처리)다.

## 초안 해석과 도구 연결

좌표는 원본 픽셀 `[x_min,y_min,x_max,y_max]`다. PM 지정 범위를 반영했으며 이번 작업에서 원본을 새로 전사하거나 모델 결과로 정답을 만들지 않았다. 각 후보의 basis_refs는 이 폴더 기준으로 실제 원본 이미지·보존 사본·PM 회신에 연결한다. PM의 섹션 정의는 색인의 원본별 section_definition과 검토표에 보존한다. 제품 분할 로직을 변경하는 명세가 아니다.

후보에는 PM의 실제 내용 판단을 반영했지만, **반영본 검수·최종 동결을 대신하지 않는다.** human_confirmation_required=true, verified_by/verified_at/verification_ref=null, reference_freeze=null을 유지한다. 내용상 불확실성이 해소된 항목을 미회신으로 표시하지 않으며, 밝아짐 등 복수 정답 허용은 기대값에 남긴다. 검토표는 후보와 기존 GS-01 동결본의 읽기용 표현이며 공식 채점 입력이 아니다.

## 웹 열람과 보존 범위

34장 원본은 기존 공개 커밋의 이미지 링크와 로컬 원본 SHA-256으로 연결한다. 새로운 이미지·사전 XLSX·모델 자산을 추가하지 않는다. 이번 변경은 아직 커밋·push하지 않았으므로 **새 후보·검토표는 로컬에만 있고 GitHub 공유 완료가 아니다.** 초기 색인의 .cache 사전 경로도 당시 출처 기록이지 공유 링크가 아니다.

PR #93은 의견 수집용으로 병합하지 않는다. 지금 닫지 않으며, 실제 종료는 별도 요청 후 수행한다. 승인된 후속 동결본은 `docs/ai-baseline/references/gs-v1/`의 별도 버전과 PR로 관리하고 공식 회차에는 참조·복사 및 해시를 기록한다. [회신 반영과 동결 절차](pm-review.md#회신-반영과-동결-절차)를 따른다.

## 반영 검증 — 2026-11-23

- GS-02 144개·GS-03 137개, 합계 281개 후보의 ID 중복·누락 없음. 기존 273개 각각에 PM 회신이 연결되며 신규 8개의 유형·좌표를 회신과 대조했다.
- 32장 원본의 실제 파일 SHA-256·해상도 및 모든 후보 좌표의 범위를 확인했다. 각 basis_refs의 로컬 경로와 해시, 초기 초안·GS-01 동결본 참조 해시가 일치한다.
- PM 지정 중요도·좌표·H01/H04와 최종 착색 정정을 검사했다. 검토표 339개 항목은 후보 281개와 GS-01 동결본 58개의 기대값·좌표에 일치한다. 초기 초안의 미정값을 임의로 확정한 것이 아니라 회신의 명시값을 반영했다.
- 기존 동결본·평가 결과·실측 기록·원본·초기 근거 등 보호 파일 2,344개의 작업 전후 SHA-256이 일치한다. 과거 GS-01 평가의 dataset 근거는 당시 커밋과 보존 사본을 참조하며 그 사본도 변경하지 않았다.
- 변경 Markdown의 로컬 링크·앵커 130개와 CRLF 허용 공백 검사 통과. 외부 링크의 PM 계정 열람이나 후보의 GitHub 공유 완료를 검증한 것은 아니다.
- 후보 SHA-256: `6ac4c75035fbec915e42810dcf421203f946f212f8befdec3dbdc66967d481e5`.

검증은 문서·JSONL 정합성과 증거 보존 검사다. 모델 호출·실측·품질 채점·동결 및 제품/사전 동작 테스트는 수행하지 않았다. 남은 절차는 수정 반영본 확인과 별도 동결 승인이다.
