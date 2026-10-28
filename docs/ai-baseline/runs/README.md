# 회차별 측정 결과

제품 버전과 측정 회차별 실제 결과를 보관한다. 현재 공식 baseline 측정 결과는 없다. 아래는 2026-10-27 합의를 구체화한 8단계 구조다. [비과금 도구](../../../tools/baseline/README.md)로 준비·기록·집계를 검증했으며, `.cache/`의 합성/빈 자료는 공식 결과가 아니다. 실제 제품 함수의 사람 검수·체크포인트 수집 연결은 비과금으로 검증하며 비용·실행 승인은 보류다.

## 회차 디렉터리

[reference-preparation-20261027](reference-preparation-20261027/README.md)은 **기준 자료 검토 전 초안 묶음**이다. 공식 실행 회차가 아니며 manifest·실행/평가/집계 결과와 동결된 `reference_units.jsonl`을 만들지 않았다. 초안은 `artifacts/reference_units.draft.jsonl`로 분리하며 아래 완성 회차 구조와 혼동하지 않는다.

```txt
docs/ai-baseline/runs/<run_id>/
  manifest.json
  reference_units.jsonl
  execution.jsonl
  artifacts/
  evaluations.jsonl
  defects.jsonl
  human_review.csv
  metrics.csv
  summary.md
```

`<run_id>`는 기존 `evaluation_run_id`와 같은 값이다. 제품 버전·평가 프로토콜·평가셋 버전을 구분하고 다른 회차를 덮어쓰지 않는다. 이 구조는 평가 기록용이며 DB/API 계약이 아니다.

## 이번 회차의 범위 기록

도구의 로컬 manifest에는 `measurement_scope`를 보존한다. 이번 회차의 값은 `{"measured_quality_metrics":["H01","H02","H03","H04"],"human_intervention":"confirmation_only"}`다. 제품 API/DB 상태가 아닌 측정 범위 기록이다. 기존 범위 미지정 회차는 과거 집계를 읽는 용도로 유지하지만 새 공식 실행에는 이 범위와 사용자 승인 기록의 `human_intervention_scope=confirmation_only` 일치를 요구한다. 범위 오류·무수정 회차의 변경 기록은 정상 집계/진행을 거부한다.

[자동 파이프라인 평가 범위](../plan.md#이번-회차-측정-범위--pm-승인-완료)는 **PM 승인이 완료됐다(2026-10-28 사용자 확인)**. H01~H04의 기존 지표 정의와 평가 상태 enum은 변경하지 않는다. H05는 정의를 남기고 `미측정(별도 서비스 평가 예정)`으로 표시한다. 이는 실험 범위 제외 사유이며 not_applicable·undetermined·evaluation_failed로 재분류하지 않는다.

향후 metrics.csv에서는 H05의 수치·분자·분모를 null(빈 셀)로 두고 기존 `display_reason`에 위 사유를 남기는 방식으로 도구를 보완한다. 범위 제외를 품질 평가 상태 건수나 0건 수정 분포에 넣지 않는다. 요약에도 H05 미측정과 사용자 수정 효과의 후속 이관을 명시한다. 현재 도구가 이 출력을 지원한다고 가정하지 않는다.

N3·N5 최초/확인 후 값이 같다는 원시 기록과 담당자·확인 시각·필수 경고 확인 근거는 보존한다. 이 증거의 change_count=0과 회차 H05 미측정은 서로 다른 의미다. 기록 없는 확인을 만들어 채우지 않는다. OP06은 내부 기록과 기존 정의를 유지하고 `include_in_presentation=false`, 무수정 강제 실험이라는 해석 한계를 함께 남긴다. 사용자 승인 성과로 보고하지 않는다.

PM의 기준 자료·평가 결과 검수 이력은 기존 구조대로 보존한다. 이번 범위 변경과 검토 상태는 회차 요약 및 근거 자료에 연결하며 제품 스키마나 새로운 평가 상태값을 추가하지 않는다.

## 공통 필드와 연결 규칙

- 모든 JSONL 행과 CSV 행은 `evaluation_run_id`를 포함한다. 회차 내 ID는 유일한 문자열이다. `dataset_id`, `case_id`, `source_key`는 [고정 식별 단위](../protocols/v1/dataset.md#고정-식별-단위)를 재사용한다. 실행 DB ID는 회차 내 연결에만 쓰며 고정 평가 분모가 아니다.
- 체크포인트는 기존 `machine_snapshot`, `service_final`을 사용한다. 단계 산출물·N3 기록 등 체크포인트에 해당하지 않는 기록은 `checkpoint=null`과 실제 `stage`를 남긴다.
- JSONL은 UTF-8의 한 줄당 JSON 객체다. CSV는 UTF-8 헤더를 사용하며 배열·객체 셀은 JSON 문자열로 기록하고 CSV 인용 규칙을 적용한다. JSON의 `null`은 CSV에서 빈 셀로 대응한다. 값이 확인되지 않았으면 `null`과 `unavailable_reason`을 기록하며 0·빈 배열·성공으로 대체하지 않는다. 실제 확인한 0건과 확인 불가를 구분한다.
- 시각은 시간대가 있는 ISO 8601, 해시는 SHA-256, 파일 경로는 회차 디렉터리 기준 상대 경로로 기록한다. 외부 원본 참조도 경로와 해시를 기록하며 접근 불가를 정상 산출물로 취급하지 않는다.
- 평가 상태·실행 상태·결함 유형·심각도·원인 단계·해결 상태는 [평가 기준](../protocols/v1/evaluation.md)과 [집계 규칙](../protocols/v1/aggregation.md#공통-집계-규칙)을 그대로 재사용한다. 새 품질 등급이나 예외값을 만들지 않는다.
- `location`은 `{artifact_id, bbox, description}`으로 표현한다. `bbox`는 해당 이미지 픽셀 좌표 `[x_min,y_min,x_max,y_max]`이며 좌상단 원점·끝 좌표 제외를 따른다. 이미지 전체·비시각 기록 등 박스가 없으면 `null`과 설명을 남긴다. 원본과 결과 좌표를 혼용하지 않는다.
- AI 최초 기록은 불변으로 보존한다. 후속 평가·검수는 새 ID와 이전 기록 ID로 연결한다. 같은 항목의 재평가·검수 이력을 여러 측정 단위로 중복 집계하지 않는다.

## manifest.json

회차당 객체 1개. 회차 설정과 파일·산출물·평가 세션의 색인을 소유한다.

| 최소 필드 | 내용 |
|---|---|
| `evaluation_run_id`, `created_at` | 디렉터리와 같은 회차 ID·생성 시각 |
| `product_version`, `product_commit` | 제품 릴리즈와 실제 배포 커밋 SHA |
| `protocol_version`, `protocol_commit` | 사용한 평가 문서 버전과 승인 커밋 |
| `dataset_id`, `dataset_manifest_path`, `dataset_manifest_sha256` | 원본 평가셋 버전·목록·해시 |
| `country`, `language`, `case_inputs` | 미국·영어 및 상품별 규제 분류·카테고리 등 고정 입력 |
| `source_keys` | 이번 회차 전체 대상 목록. 실패·미산출 원본도 제외하지 않음 |
| `environment`, `generation_config` | 장비·OS·의존성·모델·설정·사전·폰트 등 실행 조건과 버전·해시 또는 보존 파일 참조 |
| `reference_version`, `reference_freeze` | 기준 자료 버전, 확인자·확인 시각·범위와 확인 기록 경로·해시 |
| `evaluation_sessions` | 아래 평가 세션 목록 |
| `artifacts` | 아래 산출물 색인 목록 |
| `files` | 파일별 상대 경로·해시. manifest 자신의 해시는 넣지 않음 |
| `cost_approval` | PM 비용 승인·부담 합의와 사용자의 실제 실행 범위·한도 승인을 구분해 기록. 부담 주체·실행 승인자·범위·한도·통화·시각·승인 근거 참조. 미확인 값은 사유와 함께 null |
| `comparison` | 별도 비교 회차라면 비교 대상 회차·동일 원본 목록·PM 제공 생성 정보, 아니면 null |

평가 세션 항목: `session_id`, `channel`(Codex 채팅), `started_at`, `model_name`, `model_settings`, `model_evidence_ref`, `instruction_path`, `instruction_sha256`, `conversation_ref`, `transcript_path`, `transcript_sha256`, `unavailable_reason`. 표시 모델·설정은 실제 확인한 값만 기록하고 `GPT-6 Astra (light)`를 자동 대입하지 않는다. 세션·설정이 달라지면 별도 항목을 만든다. 채팅 링크만 남기지 않고 평가 지침 전문과 응답의 보존본도 `artifacts/`에 연결한다.

## reference_units.jsonl

검증·동결된 기준 단위당 한 행이다. 초안과 미확인 기대값은 검증된 기준으로 섞지 않고 `artifacts/`의 기준 자료 초안·확인 이력에 보존한다. 미확인 범위 자체도 기록해 대상에서 조용히 누락하지 않는다.

최소 필드: `evaluation_run_id`, `dataset_id`, `case_id`, `source_key`, `reference_unit_id`, `reference_version`, `unit_type`, `location`, `expected_value`, `applicability`, `importance`, `basis_refs`, `verified_by`, `verified_at`, `verification_ref`.

- `unit_type`·`importance`는 기존 기준 단위·중요도를 재사용한다. 중요도가 적용되지 않으면 null이다.
- `applicability`는 지표·체크포인트별 적용 여부와 이유를 담는다. 정상 제외 여부는 사람이 확인한 정책 기대값에 근거하며 시스템 결과만으로 결정하지 않는다.
- 기준 오류를 수정하면 새 버전과 확인 이력을 남기고 비교 대상에 같은 기준을 적용한다.

## execution.jsonl

실행·자동 재시도·fallback·사용량과 N3·N5 수정 증거를 기록한다. `record_kind`는 파일 내 기록 구분이며 제품 상태값이 아니다.

공통 최소 필드: `evaluation_run_id`, `record_id`, `record_kind`, `case_id`, `source_keys`, `job_id`, `stage`, `checkpoint`, `started_at`, `ended_at`, `evidence_refs`, `unavailable_reason`.

| 기록 구분 | 추가 최소 필드 |
|---|---|
| `attempt` — 실행 시도당 한 행 | `logical_execution_id`, `target_id`, `attempt_number`, `execution_status`, `error_codes`, `fallback_type`, `is_final_attempt`, `applied_to_result`, `artifact_ids`, `usage`, `cost` |
| `user_edit` — N3/N5 수정 이력 | `source_key`, `target_id`, `original_value`, `final_value`, `change_count`, `user_retry_count`, `confirmed_at`, `n3_intervention` |
| `usage` — 실행 시도와 별도인 평가·공통 사용량 | `session_id`, `purpose`, `usage`, `cost` |

`logical_execution_id`는 회차+단계+처리 대상 ID로 연결한다. OP02는 마지막 논리 실행 결과를 사용하고 시도 수를 분모로 삼지 않는다. 모든 시도와 반영 여부는 보존한다. 작업 전체를 가리키면 `source_keys`에 영향 원본을 기록하며 원본별 복제로 호출·비용을 중복 합산하지 않는다.

`user_edit`의 최종 수정 건수는 [기존 N3·N5 규칙](../protocols/v1/evaluation.md#사용자-수정-규칙-초안)으로 산출한다. 저장 이력이 여러 개여도 N5 같은 블록의 수정은 한 번만 센다. 사용자 재시도와 자동 재시도를 구분한다. OP06의 최초 확정·완료 작업 여부를 확인할 실행 증거도 연결한다.

`usage`는 용도·모델·호출·토큰·GPU·렌더 등 확인 가능한 수량과 단위를, `cost`는 금액·통화·실제 또는 추정 구분·단가 근거를 담는다. 제품 생성과 Codex 평가 사용량은 분리한다. 채팅 사용량을 확인할 수 없으면 미확인으로 기록하며 평가 API 호출량을 만들어 넣지 않는다. 이미 시도 행에 기록한 사용량은 별도 usage 행에 재합산하지 않는다.

## artifacts/

원본 참조, 중간 산출물, 최초 미리보기, 최종 결과, 확대 영역, 평가 지침·채팅 기록·기준 초안과 확인 이력을 보존한다. 이미지 생성·수정 결과를 증거 원본으로 덮어쓰지 않는다.

manifest의 각 산출물 항목 최소 필드: `artifact_id`, `case_id`, `source_keys`, `kind`, `stage`, `checkpoint`, `path`, `sha256`, `media_type`, `width`, `height`, `parent_artifact_id`, `crop_bbox`, `created_at`, `unavailable_reason`.

이미지가 아닌 자료의 폭·높이는 null, 확대본은 부모 산출물 ID와 부모 이미지상의 crop 좌표를 기록한다. 원본 파일은 고정 평가셋의 파일·해시를 참조할 수 있다. 없는 산출물의 경로를 임의로 만들지 않고 실행·평가 예외 기록에 연결한다.

## evaluations.jsonl

Codex의 최초 평가 및 재평가 기록이다. 기본 단위는 원본×체크포인트×지표이며 H04는 적용 핵심 사실별 행을 사용한다. H01·H05의 값은 실행·수정 기록에서 집계하며 이 파일의 AI 추정값으로 대체하지 않는다.

최소 필드: `evaluation_run_id`, `evaluation_id`, `session_id`, `case_id`, `source_key`, `checkpoint`, `metric_id`, `reference_unit_id`, `reference_version`, `artifact_ids`, `evaluation_status`, `observed_value`, `expected_value`, `reason`, `evidence_refs`, `defect_record_ids`, `supersedes_evaluation_id`, `evaluated_at`.

- H04의 `observed_value`에는 값의 정확성·이미지 판독 가능 여부와 근거를 담는다. 원본 단위 지표의 `reference_unit_id`는 null일 수 있다.
- AI의 최초 평가에는 `supersedes_evaluation_id=null`, 재평가에는 이전 ID를 연결한다. 최초 기록은 삭제하지 않는다.
- H02·H03 등급은 이 파일의 자유 서술로 확정하지 않고, 정상 평가 상태와 연결된 유효 결함 수로 계산한다. 산출물·평가 실패는 [기존 예외 규칙](../protocols/v1/aggregation.md#공통-집계-규칙)을 따른다.

## defects.jsonl

결함의 체크포인트별 관찰·판정 기록당 한 행이다. 같은 결함의 최초·최종 관찰은 같은 `defect_id`로 묶되 `defect_record_id`를 구분한다.

최소 필드: `evaluation_run_id`, `defect_record_id`, `defect_id`, `evaluation_id`, `recorded_by`, `case_id`, `source_key`, `checkpoint`, `reference_unit_ids`, `defect_type`, `severity`, `location`, `observed`, `expected`, `evidence_refs`, `origin_stage`, `detected_stage`, `root_defect_id`, `resolution_status`, `correction_evidence_refs`.

전파 현상은 `root_defect_id`로 연결하고 독립 오류만 별도 결함으로 센다. 원인을 확인할 수 없으면 `origin_stage=undetermined`다. 사용자 수정으로 해결돼도 자동 결과의 결함·심각도를 삭제하지 않는다. 최초·최종 체크포인트의 결함 수는 각각 계산하고 동일 root defect의 관찰 행을 중복 가산하지 않는다.

## human_review.csv

사람 검수 한 건당 한 행. AI 최초 평가와 분리하며 사람의 평가 수정은 제품 사용자의 N3·N5 수정 건수에 넣지 않는다.

최소 열: `evaluation_run_id`, `review_id`, `evaluation_id`, `defect_record_id`, `source_key`, `checkpoint`, `reference_unit_id`, `review_trigger`, `review_scope`, `reviewer_id`, `reviewed_at`, `ai_value`, `reviewed_value`, `reason`, `evidence_refs`, `supersedes_review_id`.

`review_trigger`에는 K01~K09·critical/major·불확실·A등급 표본 중 해당 이유를 남긴다. `reviewed_value`에는 확인한 평가 상태·핵심 사실 판정·결함의 유효 여부·심각도·원인 단계 등 실제 검수한 필드만 담는다. 검수하지 않은 값은 덮어쓰지 않는다. 사람이 발견한 신규 결함은 `recorded_by`에 사람 검수자 ID를 남긴 새 결함 기록과 검수 행을 연결한다. 이를 AI 최초 발견으로 표시하지 않는다. 추가 검수는 이전 review ID를 연결하며 검수자의 신원이 아직 미정인 상태를 검수 완료로 기록하지 않는다.

## metrics.csv

집계 대상×지표×체크포인트×하위 결과당 한 행이다. 핵심·보조 지표·원인 단계별 결함 건수와 내부 파생 분석을 함께 저장하되 구분한다.

최소 열: `evaluation_run_id`, `metric_id`, `scope`, `case_id`, `source_key`, `checkpoint`, `breakdown`, `numerator`, `denominator`, `value`, `unit`, `applicable_count`, `measured_count`, `not_applicable_count`, `undetermined_count`, `no_output_count`, `evaluation_failed_count`, `ai_only_count`, `human_checked_count`, `display_reason`, `evidence_refs`, `aggregation_basis`, `include_in_presentation`.

- `scope`는 전체·상품·원본 등 집계 범위를 명시한다. `breakdown`은 A/B/C, 수정 건수 구간, 원인 단계×심각도 등 기존 결과 구분을 담는다. 미사용 ID·수치 셀은 null이다.
- `metric_id`는 기존 H01~H05·OP01~OP07을 재사용한다. 별도 지표 ID가 없는 원인 진단·파생 분석은 ID를 임의의 새 핵심 지표로 만들지 않고 null과 설명을 사용한다.
- `aggregation_basis`에 사용한 실행·평가 ID와 사람 검수 반영 내역, 집계 방식·버전을 연결한다. 최신 유효 평가와 해당 항목의 사람 수정만 반영하고 이전 이력을 중복 계산하지 않는다.
- 분자·분모·0건 표시·예외 건수는 [집계 명세](../protocols/v1/aggregation.md)를 그대로 적용한다. 정상 측정 0건을 0%나 100%로 채우지 않는다. H01처럼 개별 분모가 양수인 경우에는 개별 규칙이 우선한다.
- OP06 무수정 승인률의 `include_in_presentation`은 false다. 내부 기록과 기존 계산식은 유지하며 H05는 별도다.

## summary.md

회차·제품/프로토콜/평가셋 버전, 실행·평가·검수 범위, 측정 대상 지표 결과와 분모·예외 건수 및 H05 미측정 사유, 원인 단계·심각도·영향 원본·원인 불명 건수, 주요 전후 이미지와 근거 링크, 검수 현황·미정/미완료 사항, 사용량·비용과 한계, 비교 조건을 기록한다. 내부용 OP06은 metrics.csv에 유지하되 발표 표에는 싣지 않는다. 정해진 일부 표본을 전체 성능으로 일반화하지 않는다.

## 일반 LLM 비교 기록

PM이 생성한 4장을 별도 비교 회차로 기록한다. manifest의 `comparison`에 PM 제공 결과라는 사실, 동일 원본 4개의 `source_key`, 생성 모델·설정·지침과 확인 불가 항목, Pixlate 회차·`machine_snapshot` 산출물 ID를 연결한다. 생성 실패·누락도 대상 목록에서 제거하지 않는다.

같은 기준 자료와 H01·H02·H04 정의를 사용한다. 일반 LLM 제공 결과는 해당 비교의 최초 결과로 평가하며 Pixlate의 `service_final`과 섞지 않는다. 비교 metrics는 두 결과의 회차·원본·체크포인트를 명시한다. 생성 모델·지침·사용량이 미확인인 경우 이를 기록하고 추정으로 채우지 않는다. 34장 baseline의 전체 집계와 4장 비교를 합산하지 않는다.

## 파일 간 검증

1. manifest의 회차·원본 목록과 각 파일의 ID가 일치하며, 참조한 평가·결함·검수·산출물 ID가 존재하는지 확인한다.
2. 체크포인트·기준 버전·좌표의 부모 이미지가 맞고, 경로·해시가 실제 보존 자료와 일치하는지 확인한다.
3. 재시도·재평가·전파 결함·사람 수정의 중복 집계가 없는지 확인한다. AI 최초 결과와 검수 반영 결과의 근거를 모두 추적할 수 있어야 한다.
4. H01은 실행 증거, H02·H03은 상태와 결함 수, H04는 기준 단위별 판정으로 재계산 가능한지 확인한다. 이번 회차 H05는 미측정 표시를 확인하고 후속 서비스 평가에서만 수정 증거로 계산한다.
5. 미실행·누락·불확실·평가 실패를 정상 또는 대상 없음으로 바꾸지 않았는지, OP06이 발표에서 제외됐는지 확인한다.

이 검증은 결과 기록의 요구사항이며 이번 문서 변경에서 실제 회차 데이터나 측정 도구를 생성하지 않는다.


## 수집 도구의 운영 증거 표현

Windows 격리 실행은 Torch를 Paddle보다 먼저 import하며 `native-runtime.json`에 초기화 순서·버전·경과시간을 기록한다. 이는 DLL 충돌을 피하기 위한 실행 환경 준비이며 모델 로딩/추론·제품 파이프라인 변경이 아니다. 이미 Paddle만 로딩된 프로세스에서는 새 프로세스를 요구한다. 초기화는 공식 실행 승인 검사 뒤에서만 수행한다.

`environment.dependency_lock`에는 회차 내부로 보존한 Windows 패키지 lock의 `path`와 `sha256`을 기록한다. 새 실행은 해시 및 전체 설치 버전·Windows x64·Python 3.12.10 일치를 먼저 검증한다. wheel 목록은 [해시 고정 설치 목록](../../../tools/baseline/requirements-windows-py312.lock.txt)을 사용한다. 패키지 lock이 모델/폰트/평가셋/사전 해시 확인이나 실행 승인을 대신하지 않는다.

기존 실행 기록의 공통 최소 필드를 재사용하는 `record_kind=operation`은 제품 enum이 아닌 수집기 기록 구분이다. 추가 필드는 `event`, `values`이며 실제 근거 파일 참조가 필요하다. 지원 사건은 작업 생성·저장·첫 다운로드, N2/N4/N6 시작·완료·중단, 사람 대기시간, 최초 N5 확정, 규제 판정·규격 검사, 자원 사용량, 종류별 fallback이다. 세부 필드와 입력 방법은 [도구 안내](../../../tools/baseline/README.md)를 따른다. 없는 사건을 성공이나 0으로 만들어 넣지 않는다.

H01의 원본별 최종 이미지 보존 요약은 `attempt`의 `result_only=true`로 구분하고 OP02·OP03의 실제 실행 시도 분모에서 제외한다. 실제 BE 시도는 parent task·stage·unit ID와 attempt_no로 연결하며 자동 재시도·사용자 재렌더의 원인을 구분한다. 용도별 모델 사용량은 SDK 최종 이벤트당 usage 한 행에만 기록하고 예약 이벤트를 중복 합산하지 않는다. 토큰 단가 추정 비용과 실제 청구액은 구분하며 실제 청구액이 없으면 미확인으로 남긴다.

N5의 원본별 최초 결과는 해당 원본에 연결된 미리보기 섹션 이미지 묶음과 순서·위치·원본 연결 증거로 보존한다. 새 제품 렌더를 호출해 체크포인트를 만들지 않는다. `service_final`은 실제 N6 원본별 산출물을 보존한다. 각각의 산출물 ID를 H02·H03 평가에 연결하며 필수 확인 응답은 실행 증거로 연결한다. 이번 회차에는 이를 H05 결과로 집계하지 않는다.

P90 방식은 계속 미정이다. N5 박스 수정은 [v1.0.1 기능 확인](../protocols/v1/evaluation.md#n5-박스-수정-기능-확인)에 따라 값 null·사유 `기능 미지원`으로 참고 자료/요약에 기록한다. H05 계산에는 더하지 않는다. fallback 종류별 분모를 원시 증거에서 확인하지 못한 경우에는 미측정으로 두고, 확인된 대상·적용 여부와 근거를 추가 기록해 재집계한다. 실제 GPU 장치 실행시간과 호스트에서 관찰한 추론 호출 경과시간을 동일시하지 않는다.
