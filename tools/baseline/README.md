# baseline 준비·기록 도구

평가 기준은 [baseline 명세](../../docs/ai-baseline/README.md)가 소유한다. 이 폴더의 JSON 필드는 측정 도구의 로컬 파일 형식이며 서비스 API·DB 계약을 추가하지 않는다. 실제 제품 코드는 수정하지 않고 지정된 릴리즈 checkout에서 가져온다.

## 구성

| 파일 | 역할 |
|---|---|
| `run.py` | 준비·기록·집계 CLI. 기본 명령은 모델·DB를 호출하지 않음 |
| `core.py` | 34장·9영역 확인, 평가 이력 검증, 품질·실행·정책 집계 |
| `be.py` | 기존 BE 흐름 호출, 순차 작업 전달, 파일 저장소·증거 수집, SDK 사용량 기록 |
| `review.py` | 필수·표본·확대 검수 대상과 원본 등급 전이표 |
| `evaluation-prompt.md` | 확정 평가 기준에 따른 평가자 지시. K01~K09 기준 보정 후 회차에 동결 |

## 모델 호출 없는 사용

저장소 루트에서 실행한다. 출력 폴더와 파일은 새 경로를 지정하며 기존 결과를 덮어쓰지 않는다. 예시의 `.cache/`는 Git 제외된 로컬 준비 위치일 뿐, 실제 원본의 위치가 아니다.

```powershell
python -m tools.baseline.run template .cache/baseline-config.json
python -m tools.baseline.run doctor
# 제공받은 실제 값으로 설정 파일을 채운 뒤:
python -m tools.baseline.run prepare .cache/baseline-config.json .cache/baseline-preparation
python -m tools.baseline.run packet .cache/baseline-preparation .cache/blind-packet.json
python -m tools.baseline.run record .cache/baseline-preparation .cache/evaluation-event.json
python -m tools.baseline.run summary .cache/baseline-preparation .cache/summary-01.json
```

`template`은 실제 값이 없는 설정을 만든다. `prepare`는 지정된 `input_root` 아래 `GS-01/GS-01_001.jpg` 등의 34장만 검사한다. 임의 경로에서 표본을 찾지 않으며 없는 파일을 대체하지 않는다. 크기·면적·SHA-256·핵심 영역 좌표 범위를 확인하고 238행의 빈 원본별 평가표를 만든다. 평가 기준 자료·평가 프롬프트와 명세를 복사·해시로 보관한다. 자료 누락은 `preparation.json`에 기록하며 공식 결과가 아니다.

설정의 `products`는 상품별 실제 `product_name`, `regulatory_class`, `internal_category`, 브랜드 `name_ko`·`name_en`, 미국·영어를 받는다. 임의 상품명·분류·기대 처리를 채우지 않는다. `reference_file`은 사람이 확인할 원본별 정보·보호 대상·처리 기대값 자료이며, 제품에 넣는 규제사전·용어집 DB와 구분한다. `evaluation_model`에는 모델 ID·설정을, `viewing_conditions`에는 실제 표시·확대 조건을 기록한다. 확인자·보정 확인·예산 항목은 실제 담당자와 승인 조건으로 채운다. 확인 이름을 적는 것이 검수 자체를 수행하는 것은 아니다.

## 격리 BE 실행 조건

실제 모델 호출은 별도 승인 후 `execute <준비묶음> --execute-models`로만 시작한다. 이 명령은 도구 검증용이 아니며 지금 실행해서는 안 된다. 사용 예시에 실제 실행 명령은 포함하지 않았다.

- `product_root`: 해당 릴리즈 코드가 checkout된 절대 경로. `product_commit`: 축약하지 않은 전체 HEAD SHA. 실행 코드의 미커밋 변경을 거부한다. 현재 계획 브랜치를 1.0.0 제품으로 자동 간주하지 않는다.
- 별도 PostgreSQL에 해당 릴리즈의 Alembic 마이그레이션과 확인된 참조 데이터를 준비한다. 운영 DB를 복제해 직접 쓰지 않는다. 접속 정보는 설정 JSON에 넣지 않고 `BASELINE_DATABASE_URL` 환경변수로 전달한다. 도구는 localhost의 `px_baseline_*` DB만 허용하며 job·seller·brand가 이미 있으면 중단한다. DB 생성·삭제·참조 데이터 적재는 자동 수행하지 않는다.
- PostgreSQL/BE 의존성과 실제 모델 실행 의존성, Gemini 키, OCR 실행 장치, LaMa 가중치·GPU가 필요하다. 실제 사전·용어집은 담당자가 확인한 데이터로 준비한다. 빈 용어집이 맞는지도 별도로 확인한다.
- 기본 제품 설정·정책·스타일 기본값은 선택한 릴리즈에서 로드한다. 기본 스타일 폰트를 포함한 파일 해시, DB 참조 자료, 제품 커밋과 도구 해시를 남긴다. 별도 프로세스에서 실행하여 다른 작업의 어댑터 설정을 공유하지 않는다.
- 시험 도구의 가짜 모델을 가져오지 않는다. 순차 작업 전달은 기존 태스크 함수를 호출하며 재시도·제외·확정 판단은 BE에 남긴다. 원본별 N1 작업을 만들고, N3 기본 판단을 수용하고, N5 경고를 그대로 확인한다. 최종 렌더 결과는 저장소 파일로 보존한다. 보관함 UI의 저장·ZIP 내보내기는 실행하지 않는다.
- 원본 하나에 대한 생성은 한 번만 수행한다. `started.json`이 있으면 같은 묶음 재실행을 거부한다. 중단 시에는 보존된 DB·파일·실행 이력을 확인해야 하며 자동 재개로 이전 결과를 덮어쓰지 않는다.
- 예산은 `max_calls`, `max_amount`, `currency`, `reserve_per_call`, `basis`로 기록한다. SDK 요청 전에 호출당 보수적 예약 금액을 확보한다. 실패한 요청도 한도를 소비한다. `reserve_per_call`의 상한 근거와 SDK 내부 재시도 설정을 실제 실행 전에 확인해야 한다. 이 금액은 청구액이 아니며 실제 금액을 추정 없이 채우지 않는다. OCR·LaMa 장비 비용과 AI 평가 비용은 별도 정산한다.

실제 DB 연결과 실제 모델 전체 연결 검증은 별개다. 단위 검증 통과만으로 공식 실행 가능 또는 34장 처리 성공으로 간주하지 않는다.

### 로컬 환경 준비

Windows의 측정 전용 Python 환경에서 CPU OCR과 로컬 GPU LaMa를 사용한다. 로컬 PostgreSQL은 Docker 없이 독립 실행형으로 준비할 수 있다. localhost 전용 접속·별도 데이터 폴더·측정용 DB를 사용하며, 비밀번호는 Git 제외된 로컬 파일 또는 환경변수로만 관리한다. 학교 서버의 워커 환경·패키지 버전을 그대로 복제하지 않는다.

제품 사본은 원격 `release/1.0.0`의 최신 커밋을 확인해 별도 경로에 고정한다. Python·PyTorch·Paddle·PostgreSQL·드라이버 버전, 모델 파일 해시와 설치 출처를 준비 기록에 보존한다. DB 스키마는 그 제품 사본의 마이그레이션으로 만든다. 사전·용어집은 사용자가 지정한 확인된 자료만 적재한다.

모델 호출 없는 준비 검증은 패키지 의존성·import, CUDA 장치 인식, 모델 파일 해시, 전용 DB 연결·마이그레이션·스키마, 가짜 모델 기반 도구 테스트까지다. OCR/LaMa 엔진 생성·모델 로딩·추론과 Gemini API 요청은 이 검증에 포함하지 않는다. 실제 모델 전체 연결·입력 메모리 수용 여부는 별도 승인된 사전 실행에서 확인한다.

## 결과 묶음

| 파일 | 내용 |
|---|---|
| `config.json`, `inputs.json`, `frozen.json`, `protocol/` | 조건·입력 해시·고정 기준 자료 |
| `regions.json`, `evaluation-sheet.csv`, `reference-sheet.csv` | K01~K09, 빈 원본×지표 평가표와 기준 자료 작성표 |
| `runtime.json`, `references.json`, `bindings.jsonl` | 실제 제품·참조 데이터·원본과 DB 작업 대응 |
| `execution.jsonl`, `stages.jsonl`, `usage.jsonl`, `warnings.jsonl` | 단계·작업 이력, 원시 출력, SDK 사용량, 확인한 경고 |
| `evidence/<원본>.json` | 원본·섹션·블록·판정·시도·인계·산출물 DB 증거 |
| `objects/`, `object-index.json` | SHA-256 파일과 객체 키 대응. 삭제 호출 후에도 증거 바이트 보존 |
| `evaluations.jsonl`, `policy.jsonl`, `defects.jsonl` | AI·사람 판정과 정책 기대/실제·결함 추적 이력 |

파일 저장소는 객체 키를 파일 경로로 해석하지 않으며 해시 파일로 보존한다. DB 증거에서 실행 권한 토큰·임시 인증 주소는 제외한다. 실행 실패 때도 증거를 남긴다. 결과 묶음의 별도 백업 확인은 책임자가 수행한다.

## 평가 기록 형식

`record`는 JSON 한 건을 검증한 뒤 JSONL에 추가한다. 실제 이미지 판단을 대신 수행하거나 등급을 자동 생성하지 않는다.

평가 필수 필드: `id`, `source_id`, `metric`(1~7), `role`(`primary`·`independent`·`human`), `reviewer`, `session`, `scope`, `grade`, `evidence`, `reason`.

- `scope`: 원본 전체는 `original`, 핵심 영역은 K01~K09, 결함 영역은 `D-`로 시작하는 안정적인 ID. 영역 사람 판정을 원본 전체 판정으로 올리지 않는다.
- `grade`: A/B/C 또는 `판단 불가`·`해당 없음`·`산출물 없음`. A는 해당 범위를 충분히 확인한 `coverage_complete: true`가 필요하다.
- B/C: `defect_ids`, `locations`, `observation`, `expected`, `defect_type`을 함께 기록한다. 좌표는 원본 픽셀, 끝 좌표 제외 규칙을 따른다.
- 독립 재검토: `blind_review: true`와 1차와 다른 `session`을 기록한다. `packet`은 1차 판정을 포함하지 않는 자료 색인이다. 세션의 실제 독립성은 평가자가 지킨다.
- 동일 대상·지표·범위·역할 재평가: 직전 `supersedes` ID와 `revision_reason`이 필요하다. 최초 판정을 삭제하지 않는다.
- 사람 표본에서 오판 발견: `misgrade_detected: true`를 기록하면 같은 상품·지표 전체를 확대 검수 대상으로 표시한다.

`record --kind policy`는 `id`, `source_id`, `region_id`, `location`, `text`, `basis`, `evidence`, `reviewer`와 `expected_exclude`, `actual_exclude`, `expected_replace`, `actual_replace`를 받는다. 기대/실제 값은 true·false·null이며 실제 값이 null이면 `exception_reason`을 기록한다. 대체 지시 문구의 적합성은 별도 근거와 지표 7 판정에 기록한다. 동일 기준 영역을 수정할 때는 직전 ID와 수정 이유를 남긴다.

`record --kind defects`는 `id`, `source_id`, `defect_id`, `location`, `metric`, `defect_type`, `observation`, `expected`, `evidence`, `reviewer`를 받는다. 후속 비교에는 `baseline_defect_id`, `change`(해결·완화·유지·악화·평가 불가·신규·기존 결함 뒤늦은 발견), 대응 근거를 기록한다. `recurrence: true`에는 `previous_resolved_run`이 필요하다. 분할·병합 관계와 애매한 대응의 사람 확인도 근거에 남긴다. 좌표 겹침으로 대응을 자동 확정하지 않는다.

`summary`는 상품별·전체 품질 분포, 평가 가능 건수, 완료율·중앙값·P90·최댓값, 정책 혼동행렬, 비용·사용량, 검수 대상 목록과 근거 연결 오류를 만든다. AI 불일치는 사람 판정 전까지 미판정으로 남긴다. 비용 미확인은 0으로 채우지 않는다. 출력은 항상 잠정 집계이며 실제 사람 검수·백업 완료를 자동 인증하지 않는다.

`compare <이전묶음> <후속묶음> <새출력.json>`은 동일한 34장 해시·평가 조건·고정 기준 자료의 실제 파일 해시를 확인한 뒤 지표별 3×3 등급 전이와 예외 상태 전이, 결함 변화 집계·대응 누락, 후속 필수 검수 목록을 만든다. 기준을 바꾼 비교는 두 회차를 동일 기준으로 재평가한 뒤 수행한다.

## 검증 가능한 기록 연결

아래는 확정 평가 기준을 기록하는 도구의 로컬 형식이다. 서비스 API·모델 처리 규칙을 변경하지 않는다.

- `evidence`는 문자열 대신 `{path, sha256, source_id}` 객체 배열이다. `path`는 회차 묶음 내부 상대 경로다. 객체 저장소의 이미지에는 `object_key`도 기록한다. 파일 존재·해시·원본 ID를 검사하고, 이미지 객체 키가 해당 원본의 DB 증거에 있는지 확인한다. DB 증거는 `bindings.jsonl`의 작업 ID와도 대조한다. 별도 작성한 비교 이미지·근거 문서는 묶음 내부에 보존하고 같은 형식으로 연결한다.
- `phase`는 `formal`(기본) 또는 `calibration`이다. 보정 판정은 원시 이력에 보존하지만 공식 집계와 검수 대상 계산에서 제외한다.
- 영역 사람 판정이 추가되면 원본 재판정에 `region_evaluation_ids`로 해당 판정 ID를 연결한다. 반영되지 않은 정정이나 같은 지표의 영역 결함보다 좋은 원본 등급은 `미판정`으로 보류한다. 일부 영역의 A를 원본 A나 전체 사람 검수 완료로 올리지 않는다.
- 영역의 `판단 불가`·`산출물 없음`은 해당 ID를 참조했다고 해소되지 않는다. 같은 지표의 원본 A는 보류하며, 확인된 다른 결함만으로 명확한 B/C는 그 판정을 유지한다.
- K01~K09는 지표별로 독립 재검토·사람 확인 여부를 표시한다. 대상이 없는 지표도 `해당 없음`과 근거를 기록한다. `summary ... --baseline <baseline묶음>`은 baseline B/C 문제 영역을 독립 재검토에, C 문제 영역을 필수 사람 검수에 포함한다. 원본 판정의 `defect_ids`도 대상에 반영한다.
- 후속 검수와 baseline 결함 목록은 같은 범위의 사람 최종 판정을 우선한다. 사람이 철회한 AI 결함을 필수 검수나 결함 상세 누락으로 되살리지 않는다. 사람 정정 전 AI 불일치는 최종 채택 판정으로 간주하지 않는다.
- 후속 결함 기록의 `match_reason`은 같은 원본·내용/대상·유형에 대한 대응 근거다. `baseline_defect_id`가 가리키는 결함이 없거나 지표가 다르면 비교 오류로 표시한다. 여러 후속 박스가 같은 baseline 결함에 대응해도 baseline 건수는 한 번만 센다. 분할 결과의 변화 상태가 다르면 자동으로 하나를 선택하지 않는다.

결함 변화는 전체와 `by_metric`의 지표별 집계를 함께 제공한다. 재발을 확인하려면 `compare ... --resolved-run <이전후속회차>`로 해결을 확인한 회차를 명시적으로 제공한다(여러 회차는 옵션 반복). `previous_resolved_run`은 그 회차의 `run_id`와 일치해야 한다. 실제 묶음의 입력·평가 조건·동결 자료·증거 파일과 baseline 이후/현재 이전 실행 시점을 확인한 뒤, 같은 baseline 결함·지표의 최신 대응 기록이 모두 `해결`인 경우에만 재발을 센다. 존재하지 않는 회차명이나 확인되지 않은 해결 이력은 집계 오류로 남기며 재발 수에 더하지 않는다. 현재도 해결 또는 평가 불가인 결함을 재발로 계산하지 않는다.

결함 정정 이력은 원본·결함 ID별 최신 기록으로 먼저 정리한 뒤 지표별로 나눈다. 지표를 변경한 이전 기록을 중복 집계하지 않는다. 후속 회차에도 최종 채택 판정을 적용해 철회된 신규·잔존 결함을 제외하며 `excluded_current`에 남긴다. 최종 B/C 판정의 상세 결함 기록이 없으면 오류로 표시한다. 해결 추적 기록은 후속 A라는 이유로 삭제하지 않으며, 후속 최종 B/C에 같은 결함이 남아 있으면 해결로 계산하지 않는다.

### 정책 기준 자료

`reference_file`은 확인된 기준 자료를 담은 JSON이며, `policy_regions` 배열을 포함한다. 각 영역은 `source_id`, `region_id`, `location`, `text`, `basis`, `expected_exclude`, `expected_replace`를 가진다. 기대값은 사람이 확인한 bool이며 실행 결과에서 만들지 않는다. 핵심 정보·보호 대상 등 나머지 기준 자료도 이 JSON에 함께 보존한다.

`policy_coverage` 배열에는 34장 각각을 중복 없이 기록한다. 항목은 `source_id`, `status`(`complete` 또는 `not_applicable`), `reason`, `confirmed_by`다. `complete`인 원본에는 기준 영역이 하나 이상 있어야 하며, `not_applicable`에는 영역이 없어야 한다. 빈 영역 목록만으로 자료 준비 완료를 인정하지 않는다. 대상이 실제로 없는 원본도 확인 근거를 명시한다.

집계 분모는 동결된 `policy_regions`다. 미기록·기준 불일치·목록 밖 영역을 표시하며, `판단 불가`와 `산출물 없음`은 분리한다. 실제 처리 값이 null인 기록에는 `exception_state`와 `exception_reason`이 필요하다. 기존 준비 묶음의 기준 파일을 덮어쓰지 않고 확인된 자료로 새 묶음을 만든다.

### 비용 기록

`record --kind cost`는 구분별 회차 총액을 `costs.jsonl`에 보존한다. 필드는 `id`, `category`(`generation`·`evaluation`·`infrastructure`·`calibration`), `kind`(`actual`·`estimate`·`unpriced`), `amount`, `currency`, `applied_at`, `basis`, `evidence`, `reviewer`다. 추정에는 `rate_source`, 미산정에는 null 금액과 `reason`을 기록한다. 회차 공통 증거의 `source_id`는 null로 둔다. 같은 구분의 총액 정정은 `supersedes`와 `revision_reason`으로 연결해 이중 합산하지 않는다.

생성·인프라만 작업당 실행 비용에 합산한다. 어느 항목이 미산정이거나 통화가 다르면 합계를 만들지 않는다. 시도 작업 수와 완료 작업 수로 나눈 값을 각각 표시한다. AI 평가·보정 비용은 따로 남긴다. 실제 청구·단가·포함 범위의 타당성은 확인자가 검증한다. 모델 호출에는 BE 작업명과 인자가 연결되므로 DB 시도 기록과 대조해 재시도 사용량도 추적할 수 있다.

### 실행 환경과 수집 오류

설정의 `environment`에는 `hardware`, `resource_limits`, `concurrent_work`, `model_assets`를 기록한다. 없거나 제한이 없는 경우도 그 사실을 명시한다. `model_assets`는 사용하는 OCR·인페인트 모델 파일의 `{model, path, sha256}` 목록이며 실행 전에 실제 파일 해시를 대조한다. 실행 기록은 OS·CPU 정보, 실행 관련 환경변수, 라이브러리 버전과 초기화된 모델이 제공하는 장치·설정 정보를 함께 보존한다. 목록과 실제 로더 선택의 일치는 사전 연결 점검에서도 확인한다.

수집 오류는 `RecordingStop`으로 중단한다. BE의 일반 예외 재시도·대체 처리로 넘기지 않으며 정상 반환값을 가공하지 않는다. 이 중단은 제품 품질 실패와 구분한다. 저장 매체 자체에 문제가 있으면 기록이 끝까지 남는다고 보장할 수 없으므로 해당 묶음을 완료로 취급하지 않는다.

저장소 교체 직전의 저장소·작업 전달기·어댑터 객체를 보존하고, 사전 DB/사전 점검부터 실행 종료까지 정상·예외·중단 모두에서 복구한다. 복구를 위해 기존 모델을 재생성하거나 호출하지 않는다.

공식 `execute`와 공개 함수 `run_prepared`는 새 Python 프로세스에서만 제품 실행을 수행한다. 부모 프로세스의 `sys.path`, 환경변수, 이미 로드된 BE·모델 객체는 변경하지 않는다. 자식은 Python 격리 모드(`-I`)로 도구·제품 경로를 명시적으로 연결하며, 제품 모듈이 이미 로드된 실행은 거부한다. DB 접속 전에 실제 SQLAlchemy 엔진 URL과 세션 팩토리의 연결 엔진을 지정된 격리 DB와 대조한다. 내부 `_run_prepared`는 전용 `worker.py` 진입점에서만 사용한다. 패키지는 해당 Python 환경에 설치돼 있어야 하며 부모의 `PYTHONPATH`에 의존하지 않는다.

처리 시간은 BE 종료 직후 확정한다. 이후 DB 증거 수집은 `collection_seconds`로 분리하며 처리 시간 요약에 넣지 않는다. 생성 중 증거 수집 오버헤드는 실행기 버전과 함께 해석한다. 완료 건수는 최초 시도 완료·재시도 후 완료·재시도 정보 없음으로 나누며 분모는 여전히 34건이다.

## 검증

```powershell
python -m unittest discover -s tests -p "test_baseline*.py" -v
```

합성 이미지와 가짜 요청 함수만 사용한다. 외부 모델·DB·GPU를 호출하지 않는다. Windows 샌드박스의 기본 임시 폴더 접근이 제한되면 쓰기 가능한 작업 폴더를 TEMP·TMP로 지정한다.
