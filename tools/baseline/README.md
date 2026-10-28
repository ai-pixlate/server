# Baseline 비과금 준비·기록·집계 도구

> 최신 승인 상태: **$50·최대 1,367회·호출당 예약 $1.04 승인**. 동결 전 생성 허용·동결 후 H01~H04 채점. N3·N5 확인은 예람. 과거 승인 보류 문구보다 [현재 승인 범위](../../docs/ai-baseline/cost-estimate.md#pr-제출-시-승인-상태)를 우선한다. 이번 작업은 리뷰 반영까지만 수행하며 생성·채점은 실행하지 않는다.
정본은 [Baseline 명세](../../docs/ai-baseline/README.md)와 [회차 구조](../../docs/ai-baseline/runs/README.md)다. 현재 승인 상한은 $50·1,367회·$1.04이며 이번 작업에서 실제 실행하지 않는다. CLI `execute`, BE 공개 진입점, worker는 제품 import 전에 사용자 승인·범위·상한을 대조하며, 현재 템플릿의 user_execution=null 상태에서는 거부한다. 실행 플래그만으로 우회할 수 없다. PM의 비용 합의는 완료됐지만 사용자의 구체적 실행 승인과 실제 수집 연결 검증을 대신하지 않는다.

## 사용법

2026-10-28 오프라인 재현 환경은 `.cache/bl-venv-locked-20261028/Scripts/python.exe`다. 아래 기존 명령의 Python 경로를 이 환경으로 바꾸어 사용한다. [Windows 해시 lock](requirements-windows-py312.lock.txt)과 [환경 차이·점검표](../../docs/ai-baseline/protocols/v1/execution.md#실행-환경-최종-점검--2026-10-28)를 함께 보존한다. 기존 venv는 변경하지 않았다.

새 템플릿은 H01~H04/`confirmation_only` 범위를 기록한다. N3·N5에서 사람이 `continue`와 필요한 경고 확인 목록을 입력해야 하며 `edit`는 차단한다. H05 값·분자·분모는 null, 사유는 `미측정(별도 서비스 평가 예정)`이다. OP06 내부 계산은 유지하되 발표 제외 및 실제 사용자 승인 성과가 아니라는 설명을 기록한다. 과거 수정 가능 회차 읽기는 유지하지만 범위 미지정 또는 무수정 확인 승인이 없는 새 실행은 거부한다.

공식 회차의 `environment.dependency_lock`은 회차 내 lock 사본의 경로·SHA-256이다. 실행 전 전체 설치 버전·Python·OS를 확인한다. 예산 검토 후보를 템플릿의 사용자 승인으로 자동 복사하지 않는다.

아래 명령은 모델·DB·네트워크를 호출하지 않는다. 출력은 새 경로를 지정한다. 준비 묶음은 실제 결과가 아니며 `.cache/`에서 검증한다.

```powershell
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run template .cache/stage3-config.json
# input_root, evaluation_run_id 등 실제 확인한 값만 입력한 뒤:
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run prepare .cache/stage3-config.json .cache/stage3-preparation
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run validate .cache/stage3-preparation
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run packet .cache/stage3-preparation .cache/stage3-packet.json
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run record .cache/stage3-preparation .cache/event.json --kind evaluations
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run capture .cache/stage3-preparation .cache/artifact-metadata.json .cache/existing-artifact.png
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run summary .cache/stage3-preparation .cache/stage3-summary
.cache/bl-venv/Scripts/python.exe -B -m tools.baseline.run estimate .
```

`record --kind`는 `reference_units`, `execution`, `evaluations`, `defects`, `human_review`를 받는다. 배열 배치가 아니라 기록 한 건의 JSON을 입력한다. 평가↔결함의 상호 참조는 양쪽 기록 후 `validate`로 검사하며 미연결 상태로 집계하지 않는다. `capture`는 이미 존재하는 파일을 해시 경로로 복사하고 manifest 색인에 연결할 뿐 이미지를 생성하지 않는다. 같은 산출물 ID를 덮어쓸 수 없다.

`prepare`는 34장·K01~K09·manifest 해시를 확인하고 원본 사본·명세·지침을 동결한다. manifest, reference_units.jsonl, execution.jsonl, artifacts/, evaluations.jsonl, defects.jsonl, human_review.csv, metrics.csv, summary.md를 만든다. 실제 결과는 비어 있으며 미확정 필드는 null로 둔다. 모델·기준 자료·비용 승인을 자동 채우지 않는다. 평가 세션·산출물 색인은 회차 manifest에 실제 확인값과 보존 파일 해시를 기록한다.

`summary`는 원본 회차를 덮어쓰지 않는 새 디렉터리에 metrics.csv, summary.md, 검수 대상과 검증 결과를 저장한다. 검토 후 채택할 집계본은 별도 선택한다. 출력은 항상 잠정이며 공식 완료·실행 승인을 자동 부여하지 않는다. 기존 파일이 있으면 덮어쓰지 않고 실패한다.

## 구조화 값의 도구 표현

외부 API·DB 스키마가 아니라 명세의 관찰값을 저장하기 위한 로컬 표현이다.

- `reference_units.applicability.H01`: boolean. false는 policy_expectation에서 expected_value.all_sections_excluded=true와 사람 확인 근거가 있는 경우만 허용한다.
- `critical_fact`의 `applicability.H04`: machine_snapshot/service_final 각각의 boolean 적용 여부.
- H04 `observed_value`: `accurate`, `readable` boolean. H02·H03은 `coverage_complete=true`일 때만 원본 단위 measured를 허용한다. 등급 입력은 사용하지 않는다.
- `evidence_refs`, `basis_refs`, `verification_ref`, `correction_evidence_refs`: 회차 내부 path·sha256 객체(복수 필드는 배열). 모델 호출 없는 파일 해시 검증을 수행한다.
- `user_edit`: 원본×N3/N5의 확인된 전체 최초·최종 대상 맵을 original_value/final_value에 기록한다. N3 값은 include/exclude, N5는 문구 문자열이다. target_id는 원본 키다. 같은 대상을 여러 번 저장해도 마지막 맵과 최초 맵의 차이로 센다. 변경 0건도 확인한 빈 맵 또는 동일한 맵을 명시해야 하며 기록 부재를 0건으로 보지 않는다. user_retry_count는 해당 단계의 누적 사용자 재시도 횟수다. 박스 수정 건수는 넣지 않는다.
- 실행 시도는 logical_execution_id별 attempt_number, 마지막 is_final_attempt 하나를 기록한다. 중간 오류 시도와 최종 상태를 중복 OP02 분모로 세지 않는다.
- 제품 사용량은 시도 행 또는 purpose=product_generation인 별도 usage 행 중 한 곳에만 기록한다. usage의 calls·prompt_token_count·candidates_token_count·thoughts_token_count는 확인된 정수만 쓴다. 중복 수집 방지는 수집 연결의 검토 항목이다.
- 사람의 reviewed_value는 실제 검수한 평가 상태/관찰값 또는 결함 valid/severity/origin_stage 필드만 포함한다. 관찰값 일부 수정은 나머지 필드를 유지한다. 영역 검수를 원본 전체 확인으로 확대하지 않는다.
- CSV의 배열·객체는 JSON 셀, null은 빈 셀이다. AI 최초 JSONL과 사람 CSV는 분리 보존한다.

## 구현과 검증 범위

| 모듈 | 역할 |
|---|---|
| protocol.py | 회차 준비·불변 산출물 복사·기록 검증, H01~H05, 원인 진단, OP02/OP03와 확인된 OP05 사용량, 전체·상품·원본 집계 |
| preflight.py | 원본 해시·패키지 메타데이터·제품 checkout·모델 파일 확인. 모델 import·DB 연결 없음 |
| estimate.py | 과거 저장 분할 JSON의 호출·섹션 수와 조건부 비용 산정용 호출량 |
| be.py | 기존 수집 어댑터·모의 checkpoint hook, 사용량 정산 예산계. 실제 실행은 보류 |
| core.py, review.py, audit.py | 과거 7지표 자료의 읽기·검증 호환 코드. 최신 공식 평가 집계에 사용하지 않음 |

H02·H03은 유효 root defect를 중복 제거해 등급을 계산한다. H04는 measured 분모와 평가 가능률을 구분하며 빈 기준 자료를 대상 없음으로 확정하지 않는다. H01은 검증된 기대 대상과 N6 실행 증거가 있어야 계산한다. H05의 미수집 수정 기록은 미확인이다. OP06은 발표에서 제외한다.

OP01·OP04·OP06·OP07 운영 증거 집계와 OP05 단가 추정 비용을 연결했다. 실제 청구액·GPU 장치 시간·근거 미확인 fallback은 미측정이다. N3·N5 입력을 기다리고 실제 제품 수정 함수를 사용하는 연결은 아래를 따른다. 사용자 실행 승인을 자동 생성하지 않는다.

## 예산계

기본 호환 모드는 호출마다 reserve_per_call을 소비한다. 새 `settle_usage=true` 모드는 요청 전 예약한 뒤 입력·응답·사고 토큰이 모두 확인되면 단가 기반 추정액으로 정산한다. 입력/출력 단가, 모델, 통화, 근거, sdk_attempts=1, reservation_verified=true가 필요하다. 이 설정값 자체는 승인 또는 검증 증거가 아니다.

- 호출 전 누적 소비액+예약액이 max_amount를 넘거나 max_calls를 넘으면 API 함수를 호출하지 않는다.
- 실패·사용량 누락은 예약액을 유지하며 0원 처리하지 않는다.
- 예약/결과 기록 실패와 예약 상한 초과는 이후 호출도 차단한다.
- 동시 진입을 직렬화한다. 예약·최종 이벤트는 같은 call ID로 연결되며 최종 집계에서 둘을 이중 청구로 합산하지 않는다.
- 정산은 실제 청구서가 아니라 토큰×단가 추정이다. 세금·환전은 별도다.
- 중단한 회차를 새로운 Meter로 자동 재개하지 않는다. 기존 started.json의 원본별 생성 1회 보호를 유지한다.

## 비과금 검증

```powershell
.cache/bl-venv/Scripts/python.exe -B -m unittest discover -s tests -p "test_baseline*.py" -v
```

합성 기록·가짜 호출자를 사용한다. 실제 모델·DB 전체 연결 통과를 의미하지 않는다. 환경 점검은 `doctor --input-root ... --product-root ... --expected-commit <SHA> --asset-manifest <환경기록.json>`으로 수행할 수 있다. DB 확인은 별도로 localhost 전용 DB에서 읽기 전용으로 수행하며 비밀번호·연결 문자열은 출력하지 않는다.


## 상품별 실행·사람 검수 연결

`generation_config`에 product_root, job_grouping=`case`, budget을 명시한다. case_inputs는 GS-01~03의 기존 상품 입력(name_ko, name_en, product_name, regulatory_class, internal_category, target_country=US, target_lang=en)을 사용한다. 순서는 GS-01→02→03이며 각 작업에 원본 2·14·18장을 업로드한다. 전체 회차의 Meter 하나로 예산을 공유한다. 원본별 추가 생성은 지원하지 않는다.

`interaction.py`는 검수 요청을 artifacts/에 보존하고 터미널에서 응답 JSON 경로를 기다린다. N3/N5 각각, 다음 형태의 응답을 **사람이 실제 요청을 읽고 작성**한다. 아래는 형식 예시이며 실행 또는 실제 판단이 아니다.

```json
{"request_id":"요청 파일의 ID","actor":"실제 확인자","action":"continue","acknowledgedWarnings":[]}
```

이번 회차에는 편집 없이 action=continue로 확인한다. N5에서는 acknowledgedWarnings 배열을 사람이 명시한다(필요한 경고를 확인한 항목 목록, 없으면 빈 배열). 과거 편집 회차용 edit 지원은 보존하지만 이번 회차에는 사용하지 않는다. 요청 파일에 원문·판정·번역·경고와 로컬 이미지 경로가 있다. 잘못된 입력·상태 충돌·EOF는 진행을 중단한다. 실행기는 경고 목록을 자동 승인하지 않는다.

`product_review.py`는 릴리즈 SEC-03·CFM-02 함수를 호출하므로 기존 잠금·revision 검사·재렌더 규칙을 유지한다. 최초 machine_snapshot은 N5 수정 전에 보존하고, 최종 service_final은 N6 렌더 성공 후 보존한다. 섹션별 미리보기 이미지와 state_ref를 묶어 원본 단위 H02를 검수한다. 체크포인트 이미지는 객체 키가 덮어써져도 해시 사본으로 유지된다. N6 저장 성공 사건이 있어야 OP01 완료로 센다.

## 운영 증거와 비용

operation 행은 event·values와 기존 공통 필드를 사용한다. `operations.py`의 EVENTS가 도구 입력 필드를 검증한다.

| event | values |
|---|---|
| job_created / job_saved / first_download | 실제 사건의 근거; 빈 객체 허용 |
| stage_started / stage_finished / stage_stopped | 단계 N2/N4/N6. finished는 seconds 필수 |
| human_wait | seconds(시스템 시간과 분리) |
| first_n5_confirmed | original/final 전체 블록 문자열 맵, 작업당 최초 1건 |
| regulation_checked | target_id, measured, findings(허용 제외 건수), auto_excluded, locale_unchecked |
| spec_checked | target_id, passed |
| resource | name, amount, unit |
| fallback | kind, target_id, eligible, applied. 종류는 기존 네 fallback 이름 |

확인된 규제·규격 결과는 DB 원문에서, 단계 시간은 실행기 경계에서, 수정·경고는 사람 응답과 제품 상태에서 수집한다. fallback 종류·적용 대상은 증거 대조 후 operation 기록으로 보완할 수 있으며 미확인 상태를 0건으로 만들지 않는다. P90은 방식 미정으로 숫자를 만들지 않는다. 인페인팅 호출 경과시간은 GPU 장치 사용시간과 구분한다.

SDK usage는 최종 호출 이벤트당 한 행이며 cost는 `{amount, currency, basis}` 또는 null이다. 예약 이벤트를 다시 청구로 더하지 않는다. stage·model과 원시 기록을 보존한다. 원본으로 배분할 근거가 없는 상품 작업 공통 비용은 원본별 비용에 임의 분배하지 않는다.

## 승인 기록과 재실행 방지

cost_approval의 pm_cost_agreed=true는 이미 합의된 비용 부담이다. user_execution은 현재 null이다. 실제 회차 준비 시 최신 승인 근거와 다음 정보를 연결한다. 이 도구는 승인 명령을 제공하거나 합성 시험의 승인값을 실제 회차에 복사하지 않는다.

- approved=true, approved_by, approved_at, evidence_ref(path·sha256)
- product_commit, 전체 source_keys, job_grouping=case
- official_generations_per_source=1, user_regeneration_allowed=false, human_intervention_scope
- max_calls, max_amount, reserve_per_call, currency: 실제 budget과 일치해야 함

`approval.py`는 위 기록과 실제 회차의 범위를 대조한다. 승인 후에도 명시적 실행 옵션이 필요하다. 최초 시작 파일과 상품별 claim은 배타 생성하며, 중단 후 새 Meter로 예산을 초기화하거나 같은 회차를 다시 처음부터 실행하지 않는다. 중단 회차 복구는 별도 검토 대상으로 남긴다.


## 소규모 연결 점검 후 환경

2026-10-28 별도 승인 점검에서 Pillow 12.3.0·google-genai 2.29.0 등 10개 의존성을 운영 기록과 맞췄다. lock·설치 파일 위치·남은 차이는 [최신 실행 환경](../../docs/ai-baseline/protocols/v1/execution.md#승인된-소규모-연결-점검--2026-10-28)을 따른다. Windows 공식 실행 진입점은 승인 뒤 Torch를 먼저 import해 Paddle/Torch DLL 충돌을 피하고 초기화 증거를 남긴다. 이 변경으로 공식 승인 검사를 생략하지 않는다. 연결 점검 전용 스크립트·합성 입력·$2 원장은 로컬 폴더에 격리했으며 공식 execute 승인으로 복사하지 않는다.


## 생성·채점 검증 구분

생성은 기준 자료 동결 전에도 가능하지만 회차 승인 근거·범위·환경·예산 검사는 유지한다. `readiness(..., purpose='generation')`은 기준 버전/동결/평가 세션을 요구하지 않는다. 채점 기록과 품질 집계는 PM 동결본 및 증거 해시를 확인한 뒤에만 가능하다. [필드와 동결 절차](../../docs/ai-baseline/runs/README.md#생성과-채점의-분리)를 따른다. N3·N5 승인 범위의 `human_confirmation_actor`와 사람이 작성하는 응답 `actor`는 `예람`이며 수정 없이 필수 확인만 수행한다. 비용 설정은 승인 근거와 $50·1,367회·$1.04·USD로 대조한다. 별도 실행 회차의 user_execution 승인 근거를 자동 생성하지 않는다.
