# GS-03 v1.0.1 보완 실험 실행 준비 — 2026-11-26

## 범위와 상태

최신 develop `af2e716` 기반의 **별도 조건 실험**이다. 기존 공식 Baseline은 변경하지 않는다.
제품은 `bfa3b700144467f4adf7d6802b7b2e93791fe6de`, GS-03 18장만 실행한다.
변경은 `merge.llm_timeout_s=120` 하나다. 원래 60초 설정은 수정하지 않고 실행 사본에 적용한다.
나머지 모델·프롬프트·사전·자동 재시도 정책은 보존한다. 콩·쌀 수정은 포함하지 않는다.
현재 실제 모델 호출 0회, 제품 생성 0회, 사용자 비용·실행 승인은 null이다.

기존 `be.py`·공식 승인기를 변경하지 않았다. `gs03_runner.py`는 해당 커밋의 `_run_prepared` 흐름을 분리 복제하고 범위·승인·설정 경로·사전 일치 검사를 바꿨다. 공통 예산계·수집기·사람 확인·제품 흐름 함수는 재사용한다. 향후 공통 실행기 변경 시 별도 복제본에도 영향 검토가 필요하다.

## 비과금 준비 재현

저장소 루트의 상대 경로 예시다. 새 worktree에서는 아래 경로를 원본 저장소의 절대 경로로 지정한다.

```powershell
$py = '.cache/bl-venv-locked-20261028/Scripts/python.exe'
& $py -B -m tools.baseline.gs03_supplement prepare docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 .cache/baseline-release .cache/gs03-supplement-20261126
& $py -B -m tools.baseline.gs03_supplement validate .cache/gs03-supplement-20261126
& $py -B -m unittest discover -s tests -p 'test_baseline*.py' -q
```

출력 경로가 있으면 prepare는 덮어쓰지 않는다. 검증만 다시 수행한다.
준비 묶음은 원본 18장, 120초 설정 사본, 당시 사전·용어집 사본, 환경 lock, 빈 증거 기록 파일을 포함한다.
manifest에 이전 회차 전체 파일 및 제품 파일 해시를 기록하고 재검증한다. 승인·과거 채점·실행 결과는 복사하지 않는다.

## 확인 결과

- 기존 회차 2,028개 파일, 제품 실행 관련 추적 파일 161개를 대조/보존했다.
- 원본 18개와 모델 자산 31개가 보존 해시와 일치한다.
- Windows x64 / Python 3.12.10, lock 121개 설치 패키지가 모두 일치한다.
- GPU: RTX 4060 Laptop, 드라이버 555.97, 총 8,188 MiB, 점검 시 여유 7,092 MiB. 모델 로딩·추론은 하지 않았다.
- 로컬 저장소는 FileStore의 가짜 데이터 읽기·쓰기·기존 해시 바이트 보존을 검증했다. 외부 S3를 사용하지 않는다.
- 기존 로컬 PostgreSQL 연결 점검은 OperationalError로 실패했다. DB 접속 정보는 기록/출력하지 않았다. 신규 전용 DB 준비·스키마·참조 데이터 일치는 **미확인**이다.
- 패키지/파일 일치는 실제 Paddle/LaMa 실행 성공을 보장하지 않는다.
- 비과금 회귀 테스트 144개 통과: 승인 없음 차단, GS-03 선택, 타임아웃 단일 변경, 호출/금액 상한, 실패 예약 유지, 불변 저장, 기존 N3/N5 확인과 경고 미자동승인, 최초/최종 체크포인트 순서 포함.

## 모델과 비용

2026-11-26 [공식 모델 문서](https://aistudio.google.com/docs/models/gemini-3.8-flash)에서 stable `gemini-3.8-flash`, 입력 1,048,576 / 출력 65,536 토큰을 확인했다.
[공식 Standard 요금](https://aistudio.google.com/docs/pricing#gemini-3.8-flash)은 2026-12-31까지 입력 $0.75/M, 출력(사고 토큰 포함) $3.75/M이다. 2027-01-01부터 각각 $1.50/$7.50로 안내된다.
문서상 제공 여부만 확인했으며 계정 권한·잔여 할당량·실제 성공은 미확인이다. Models 조회·CountTokens·generate_content를 호출하지 않았다.

과거 `pipeline/samples/local/merge-input-v1/GS-03*/upstream_ocr_input_v1/origin/split_debug.json` 17장에는 분할 호출 14회·섹션 38개가 있다. 누락된 GS-03_016(높이 403)은 기존 estimate 방식으로 섹션 2개까지 잡는다. 이는 과거 분할 유지 가정이며 이번 N2 출력 예측이 아니다.

- 최초 호출 시나리오: `V+M+J+L+T`, 각 후속 단계를 모든 섹션에 적용하면 **166~174회**.
- 기존 자동 재시도 포함 시나리오: `3V+10S`로 **422~442회**. 분할 결과가 달라지면 이 범위를 넘을 수 있다.
- **권장 승인 상한: 500회·$25**, 먼저 도달한 한도에서 중단. 승인값이 아니라 제안값이다. 완료를 보장하는 상한이 아니다.
- 요청당 예약: `(1048576×0.75 + 65536×3.75)/1e6 = $1.032192`를 올림한 **$1.04**. 동일 Standard 모델, 도구/grounding 미사용, SDK 1시도 전제.
- 입력·출력·사고 토큰이 모두 있는 요청은 추정액으로 정산한다. 실패·토큰 누락은 예약을 유지한다. 예약액과 실제 청구를 구분한다.
- 완료된 GS-01/02의 알려진 요청 단가 평균을 166~174회에 적용한 참고 규모는 약 **$0.91~$1.21**이다(0.682434/98 및 1.082787/198). 미정 요청·반복 실패·입력 차이를 포함하지 않아 총비용 예상이나 보증으로 사용하지 않는다. 완전한 총비용은 실행 전 확정 불가다.
- 예산 점유 $25는 위 부분 비용보다 크다. 기존 GS-02에도 미확정 11회의 $11.44 예약이 있었고, GS-03은 504 재시도 위험이 있다. 예약을 포함한 보수적 중단선이다. 500회×$1.04를 실제 비용으로 표시하지 않는다.

## 실제 실행 전 남은 차단 조건

1. 신규 localhost PostgreSQL `px_baseline_gs03_supplement_20261126`을 준비한다. v1.0.1 마이그레이션과 당시 references.json의 사전 테이블을 그대로 적재해야 한다. 기존 Baseline DB는 사용하지 않는다. 이 PR에서는 DB 생성·적재하지 않았다.
2. DB 업무 테이블 job/seller/brand가 비어 있어야 한다. 실행기는 실제 참조 테이블 내용이 당시 사본과 같은지 검증한 후 업무 생성을 시작한다.
3. 사용자에게 회차·GS-03 18장·120초 override·500회/$25·무수정 사람 확인 범위의 승인을 받는다. 승인 근거를 별도 파일로 보존하고 해시로 연결한다. 기존 승인을 재사용하지 않는다.
4. 계정의 동일 모델 사용 권한/쿼터, 키 주입, 현재 요금, 장비 가용성을 실행 직전에 확인한다. 새로운 요금이면 기존 승인으로 실행하지 않는다.

승인 기록 `cost_approval.user_execution`은 approved, approved_by, approved_at, evidence_ref(path/sha256), source_keys, product_commit, override, human_confirmation_actor=예람, human_intervention_scope=confirmation_only, evaluation_run_id, user_regeneration_allowed=false, budget 전체를 실제 승인과 일치시켜야 한다. 준비 도구는 승인 기록을 만들지 않는다.
그 뒤 새 잠금 Python 프로세스에서 `python -B -m tools.baseline.gs03_supplement execute <새 회차> --execute-models`를 사용한다. BASELINE_DATABASE_URL은 해당 신규 DB를 가리켜야 한다. 이 명령은 **이번 작업에서 실행하지 않았다**.
중단된 회차의 예산을 초기화하여 자동 재시작하지 않는다.

## N3/N5와 H02~H04 증거

N2 이후 N3는 사람이 실제 요청 파일을 읽고 continue로 확인한다. N5는 최초 미리보기를 먼저 보존한 뒤 사람이 경고 목록을 명시하여 확인한다. 편집·경고 자동 승인·사용자 재생성은 허용하지 않는다. 기존 자동 재시도는 v1.0.1 제품 코드 그대로다.

- N5 이전 machine_snapshot: 섹션 미리보기 원본 바이트, 원본-섹션 연결, 상태 JSON, 순서와 해시.
- N6 이후 service_final: 최종 이미지 바이트·해시와 저장 성공 사건.
- usage/dispatch/stages/execution, DB terminal snapshot, 최초/최종 checkpoint 색인, N3/N5 요청·사람 응답을 신규 회차에 보존.
- 승인 동결본 `docs/ai-baseline/references/gs-v1/reference-set-20261124.json`의 GS-03 부분을 추후 별도 평가에 연결한다. 이번 준비는 기준 파일이나 평가 기록을 수정하지 않는다.
- H02/H03은 실제 이미지 A/B/C, H04는 적용 핵심 정보 67개를 두 체크포인트에서 각각 시각 검토한다. 미생성·판정 불가·예외와 Codex 최초 판정/PM 검수를 분리한다. 기존 K01~K09·중복 방지·A 표본 규칙을 유지한다.
- 기존 공식 H 결과에 합산하거나 실패를 성공으로 대체하지 않는다. 별도 보완 평가의 준비/생성/채점/PM 확정을 구분한다.
