# GS-03 v1.0.1 보완 실험 실행 준비 — 2026-11-26

## 현재 상태: r3 무대기 복제본 준비·비과금 검증 완료

사용자가 N3/N5 사람 검수를 하지 않는 방향으로 변경하고 비과금 구현을 요청했다. 기존 r3/공식 Baseline의 기록을 고치지 않고 **별도 복제본**에서만 이 정책을 적용한다. 유료 실행 승인은 아직 없다.

- 원본 r3는 N3/review, 작업 40개 done, 인계 39개 adopted, N4 작업 0개 상태로 보존한다. 원본 DB와 파일의 전후 일치를 확인했다.
- 복제 경로: `.cache/gs03-supplement-20261126-r3-recovery1`. 전용 DB: `px_baseline_gs03_supplement_20261126_r3_recovery1`. 접속파일은 `.cache/gs03-db-20261126-r3-recovery1/database-url.txt`에만 보존하며 커밋하지 않는다.
- 원본 r3 전체 파일을 복제본 `parent-r3/`에 보존하고 해시로 연결했다. 보존 DB 덤프의 SHA-256을 확인한 뒤 새 DB에 복원했으며, 공개 스키마 전체 테이블 내용 해시가 원본과 일치한다. 복원 시 원본 DB의 상태를 변경하거나 started/claim을 삭제하지 않는다.
- 실행 전 점검은 원본/보존 사본 해시, 복제 DB 내용·참조 자료·N3 상태, 완료 작업/채택 인계, 원본 18장 연결, 저장 객체 56개의 바이트와 DB 해시 연결을 확인한다. 기존 120초 설정·환경 lock·모델 자산·제품 파일 검증도 유지한다.
- 과거 r3의 usage.jsonl을 그대로 이어받는다. 호출 89회·점유 $3.69037875를 복원하고 **다음 호출은 90번**이다. 성공·실패·예약 이벤트의 같은 호출 번호는 마지막 상태로 합치고, 비용 미확정 3건은 각각 $1.04 예약을 유지한다. 총 500회/$25이며 추가 잔여는 411회/$21.30962125다. r2는 여전히 별도다.
- N2는 호출하지 않는다. 기존 N3 상태에서 제품의 `downstream.proceed()`를 호출하여 N4부터 수행한다. 새 seller/job/source_image를 만들지 않으며, 제품 v1.0.1 로직·재시도·모델·프롬프트·용어집은 그대로다.
- N3/N5 상태와 경고를 보존하고 수정 없이 진행한다. N5의 `translation_failed` 필수 경고 목록도 사전 승인 정책에 따라 제품 `confirm()`에 전달한다. 번역 실패를 성공으로 바꾸거나 추가 재시도하지 않는다. 예상 밖 필수 경고나 검수 시점 상태 변경은 중단한다.
- `machine_snapshot`은 N5 자동 진행 전에, `service_final`은 실제 N6 완료 후 각각 수집한다. 자동 처리 기록은 `actor=experiment_automation`, `human_reviewed=false`, `preauthorized_no_review`로 구분한다. 예람의 user_edit/검수 완료/실사용 무수정 승인 성과를 생성하지 않는다. 사람 검수 조건의 기존 공식 Baseline과 동일 조건으로 표현하지 않는다.
- `gs03_launch --detach`는 Windows 독립 프로세스를 사용하고 stdin을 닫는다. stdout/stderr는 복제본 파일에 남긴다. 시작 예약·started·claim의 중복 차단은 유지하며, 프로세스 종료 후 자동 재개/재생성은 하지 않는다. 컴퓨터 종료나 실제 모델 오류까지 해결하는 기능은 아니다.

### 비과금 검증 결과

- Baseline 테스트 **168개 통과**: 실제 Windows spawn, 부모 종료 후 독립 자식 실행 유지, N2 미호출/N4→최초 스냅샷→N5→N6→최종 스냅샷 순서, 경고 보존·무수정·사람 검수 미기록, 누적 호출 번호·미확정 예약 복원, 상한 도달 시 호출 전 차단, 손상 객체/미완료 작업/미승인 실행 차단 포함.
- 실제 복제 DB `preflight` 재확인: `read_only=true, database_checked=true, objects=56, calls=89, occupied_usd=3.69037875, executed=false`.
- 실행 승인은 `manifest.recovery.execution_approval=null`. GPU/제품 로딩 전 미승인 차단을 시험했다. 이번 작업의 모델 호출·제품 생성 0회. 실제 GPU 추론과 최종 렌더 성공은 미검증이다.

### 실행 전 필요한 승인과 명령

필요한 승인은 **이 복제본의 무대기 유료 실행 및 별도 H02~H04 채점**이다. 범위는 v1.0.1 GS-03 18장·병합 120초·기존 자동 재시도, r3 누적 총 500회/$25(이미 89회/$3.69037875 사용), N3/N5 무수정 자동 진행과 translation_failed 경고 보존 후 진행이다. PM 검수는 결과 생성 후 별도다. 현재 구현 요청은 실제 모델 호출 승인이 아니며 과거 r3 사람 확인 승인을 재사용하지 않는다.

실제 사용자 승인이 생기면 `recovery.execution_approval`에 approved/approved_by/approved_at/evidence_ref(path,sha256), evaluation_run_id/product_commit/source_run/budget/seed/policy를 현재 manifest와 일치하게 기록한다. 코드의 `require_execution()`이 대조한다. 가격 유효 기간 이후에는 재확인이 필요하다.

```powershell
# 비과금 사전 점검 — 복제 완료 DB에 prepare/restore를 반복하지 않는다.
& $py -B -m tools.baseline.gs03_recovery preflight <복제 경로> --url-file <복제 DB 접속파일>
# 별도 유료 실행 승인 기록이 있을 때만 수행. 이번 작업에서는 실행하지 않음.
& $py -B -m tools.baseline.gs03_launch <복제 경로> --url-file <복제 DB 접속파일> --execute-models --detach
```

상태는 `dispatch.jsonl`, `usage.jsonl`, `execution.jsonl`, `console.stdout.log`, `console.stderr.log`로 확인한다. 복제본 usage에는 부모 r3 사용량이 이미 포함되므로 두 파일을 더해 중복 집계하지 않는다. `parent-r3/`는 변경하지 않는다. 새 복제본에서 다시 중단되면 임의 재실행하지 않고 증거를 보존한다.

## r3 이력: 신규 회차 비과금 준비 완료

2026-11-26 r2를 보존한 채 신규 회차를 준비했다. **실행·비용 승인은 아직 없고 모델 호출·제품 생성은 0회**다. 아래 r2 중단 및 최초 준비 상태는 이력으로 보존한다.

- 회차/저장 경로: 원본 저장소 `.cache/gs03-supplement-20261126-r3`.
- 전용 DB: `px_baseline_gs03_supplement_20261126_r3`. 기존 전용 PostgreSQL 인스턴스 안에 별도 논리 DB로 생성했다. r2 DB를 복원 대상으로 사용하거나 수정하지 않았다.
- 접속파일/비과금 검증 증거: `.cache/gs03-db-20261126-r3/`의 Git 제외 로컬 파일. 접속 문자열은 커밋하지 않는다.
- 제품 `bfa3b700`, GS-03 원본 18장, 기존 모델·프롬프트·참조 데이터 유지. 병합 타임아웃만 120초, N3/N5 무수정 사람 확인.
- 실제 v1.0.1 마이그레이션 0001~0008 적용. expression_dictionary 24행, evidence 16행, glossary 20,653행 복원 및 당시 참조 자료와 일치. 업무 데이터는 모두 0행.
- 준비 묶음 검증 및 실제 DB 읽기 전용 preflight 2회 통과: `database_checked=true, read_only=true, executed=false`. 시작 파일·생성 claim 없음. `require_approval`은 미승인으로 차단됨.
- 실행 진입점은 `9955546`의 `tools.baseline.gs03_launch` 그대로다. 실제 Windows spawn을 포함한 비과금 Baseline 테스트 156개 통과. 모델·GPU 추론은 검증하지 않았다.
- r2 파일 445개 전체 해시와 공개 스키마 테이블 내용의 작업 전후 해시가 일치한다. r2 DB 내용 SHA-256: `ca6928fd31029e066f44250561d4c5a481bcbb94c41285c7288987405613c13c`. 기존 공식 Baseline·제품 파일도 준비 검증으로 대조했다.
- GPU 조회: RTX 4060 Laptop 8,188 MiB, 여유 7,191 MiB(점검 시점). 실행 시 가용성을 다시 확인한다.

| 구분 | 실제 요청 | 예산 점유 | 승인/상한 |
|---|---:|---:|---|
| r2 중단 회차 | 124회 | $4.88021225 | 과거 승인·중단 기록 유지 |
| r3 신규 회차 | 0회 | $0 | 별도 500회/$25 **제안, 미승인** |

r3 승인을 받더라도 r2의 기록·예약을 없애지 않는다. r3 상한을 전부 사용하는 경우 두 회차 합산은 624회/$29.88021225 점유이며, 실제 청구액을 뜻하지 않는다. r3의 예산계는 신규 회차 안에서만 집계한다.

남은 필수 조건은 **r3 신규 전체 실행(18장, 120초, 별도 500회/$25, 기존 재시도, N3/N5 직접 무수정 확인)의 사용자 승인**이다. 승인 전에는 실행하지 않는다. 실행 직전 DB/GPU·환경 및 동일 모델·요금 유효성을 다시 확인하며, 현재 세션의 계정 쿼터/실제 추론 성공은 이번 비과금 준비에서 검증하지 않았다. 승인 여부와 실제 모델/이미지 생성 성공은 구분한다.

새 회차 준비 명령에는 `--database-name px_baseline_gs03_supplement_20261126_r3`을 지정했다. 복원 도구는 manifest의 DB명과 접속파일이 일치해야 하며, 기존 DB가 있으면 계속 덮어쓰기를 거부한다. 이미 복원된 r3에는 복원 명령을 반복하지 않고 preflight만 수행한다.

## r2 이력: 실행 중단 및 진입점 수정

아래 준비 당시의 미실행·미승인·DB 빈 상태는 과거 기록이다. 2026-11-26 사용자가 GS-03 18장, 병합 120초, 500회/$25를 승인한 뒤 r2를 실행했다. N2 완료 및 N3 무수정 확인 후 N4 이미지 복원에서 중단했다. 이번 수정 작업에서는 추가 모델 호출·DB 변경·재개를 하지 않았다.

- 원인: Git 밖의 `r2/launch-approved.py`가 최상위에서 preflight와 실행을 호출했다. Windows spawn이 이 파일을 `__mp_main__`으로 다시 읽으면서 `started.json` 차단에 걸렸다. 제품 로직이나 N2의 120초 설정 문제가 아니다. 이 시작 파일은 중단 증거로 그대로 보존한다.
- 수정: 버전 관리하는 `tools.baseline.gs03_launch`로 대체한다. 실행 및 환경 설정은 `main()` 안에 두고 `__name__ == '__main__'`와 `freeze_support()`로 보호한다. import·spawn 시 제품/모델을 로드하거나 승인을 실행하지 않는다. 별도 최상위 `runpy` 래퍼를 만들지 않는다.
- 비과금 회귀: 실제 Windows spawn을 파일/모듈 진입 방식 각각으로 수행하며, 자식 정상 종료·부모 preflight/승인 각 1회·접속값 로그 미포함을 확인한다. `__mp_main__` 환경 불변과 시작된 회차 차단도 검증한다. 실제 LaMa 추론 성공까지 검증한 것은 아니다.

새 회차가 준비되고 **새 실행 승인을 받은 뒤에만** 저장소 루트에서 다음 진입점을 사용한다. 과거 r2에는 실행하지 않는다.

```powershell
& $py -B -m tools.baseline.gs03_launch <새 준비묶음> --url-file <새 전용 DB 접속파일> --execute-models
```

### r2의 N4 재개 검토

보존 위치는 원본 저장소 `.cache/gs03-supplement-20261126-r2`다. DB 사후 덤프와 `evidence/GS-03-interrupted.json`, N3 응답 및 호출/단계/객체 기록을 읽기 전용으로 확인했다.

| 확인 항목 | 보존 상태 |
|---|---|
| N2/N3 | 원본 18장, 섹션 37개(포함 36/제외 1), 블록 344개. N3 예람 무수정 확인 보존 |
| DB | job=N4/processing. analyze done 1, judge done 37, label done 36/failed 1, logo done 35/pending 1 |
| N4 중간 상태 | inpaint running 2/pending 33, style done 1/pending 34, translate done 1/pending 34. DB 상태를 변경하지 않음 |
| 결과 | deliverable 0. machine_snapshot/service_final 미도달, H02~H04 미채점 |
| 저장소 | objects/objects.jsonl 55행 및 해시 객체 보존. 정상 종료 시 쓰는 object-index.json은 없음 |
| 비용 | 요청 124회(성공 123/실패 1), 알려진 토큰 추정 $0.72021225 + 미확정 4건 예약 $4.16 = 점유 $4.88021225. 실제 청구 미확정 |

**현재 도구로 안전한 N4 재개는 지원하지 않는다. 데이터가 모두 소실됐거나 원리적으로 복구 불가능하다는 뜻은 아니다.**

- `gs03_runner`는 빈 업무 DB·새 started/claim을 요구하고 job을 새로 생성한다. `drive_job`도 항상 N2부터 시작한다. N4 진입점이 없다.
- 제품 `downstream.proceed()`는 활성 run이 있으면 기존 ID만 반환한다. 이 호출만으로 중단된 inpaint lease/handoff 채택과 메모리 작업 큐를 복구한다는 근거는 없다. 두 running 작업의 채택 상태를 구분해야 한다.
- `FileStore` 색인과 순차 dispatcher 큐는 초기화되며 `Meter`도 0회/$0에서 시작한다. 그대로 재호출하면 기존 객체·완료 작업·예약액을 잃거나 중복 호출할 위험이 있다.
- N4 재개를 별도로 구현하려면 보존 DB의 복제본에서 lease/handoff와 채택된 산출물의 무결성 검증, 객체 로그 색인 복구, 미완료 작업만의 재배치, 완료 작업 중복 방지, 누적 124회/$4.88021225 예산 복원, N3 확인 연결, 이후 N5/두 체크포인트 수집을 먼저 비과금으로 검증해야 한다. 제품 상태를 임의로 pending으로 되돌리는 방식은 채택하지 않는다. 이번 변경에는 재개 기능을 넣지 않았다.

**권장: 새 DB·새 경로·새 회차로 전체 재실행.** 추가 호출은 들지만 검증되지 않은 복구 경로 대신 기존 신규 회차 흐름을 사용한다. N2 결과가 달라질 수 있으므로 r2 N3 확인을 새 결과 확인으로 재사용하지 않는다. 기존 공식 Baseline 및 r2와도 구분한다.

추가 승인은 새 회차의 전체 실행·예산 범위 및 N3/N5 확인을 대상으로 받는다. 기존 점유를 합산한 총 500회/$25를 유지한다면 신규 회차 허용 잔여는 **376회/$20.11978775**이며, 현재 준비 도구는 고정 500회/$25를 요구하므로 이를 적용하려면 예산 설정·승인 검사도 별도 보완해야 한다. 새 회차에 다시 500회/$25를 승인받는 경우 기존 r2 점유가 별도임을 명시한다. 현재 승인을 임의로 초기화·재사용하지 않는다.

근거 SHA-256(로컬 자료 자체는 커밋하지 않음):

- `evidence/GS-03-interrupted.json`: `3bd5744ac068d47a285564c8b8ea5a339b6272230406af251982ad3705443e1d`
- `interruption-summary.json`: `0009613afc40c14b63df6ad40d557d7c45b6a39746ac1838778869200ff715ba`
- `objects/objects.jsonl`: `b03f67d5289243a1d8c1f18ff1951bf9e57311ec5e509ab4bc21985c9d2e112b`
- `artifacts/n3-user-confirmation.json`: `d09ab949e96c25b2869be839641fc9e27cc9a5674923cbf81d3af2c44cae4ed4`

## 현재 DB 준비 상태 — 후속 승인 반영

아래 최초 준비/추가 검토의 DB 미확인 기록은 당시 상태다. 후속 사용자 승인에 따라 전용 DB 준비를 완료했다.

- PostgreSQL 16.15를 별도 데이터 폴더·localhost 포트로 실행했다. 기존 Baseline 인스턴스는 시작·접속·수정하지 않았다.
- 신규 DB `px_baseline_gs03_supplement_20261126`에 v1.0.1 checkout의 Alembic 0001~0008을 적용했다. develop의 최신 스키마로 대체하지 않았다.
- 당시 참조 자료를 정확히 복원했다: expression_dictionary 24행, expression_dictionary_evidence 16행, glossary 20,653행. master_country/master_language/category_master/category_mapping/master_data_version은 당시와 같이 0행이다.
- 실제 PostgreSQL DATE를 포함한 전체 참조 행을 정규화하여 보존 사본과 대조했다. 두 집합 SHA-256은 `20831c3d953034d1844c0d269608e02c18d05a3dd86eadf1520aab69713d5872`로 일치한다.
- 실제 preflight 결과: `database_checked=true, read_only=true, executed=false`. 업무 테이블은 전부 비어 있고 started.json·generation-claims는 없다. 사용자 비용·실행 승인은 계속 null이다.
- 처음 새 서버를 제한된 실행 환경에서 시작했을 때 Permission denied/접속 timeout이 발생했다. 신규 데이터 폴더의 프로세스만 확인하여 재시작한 뒤 연결·복원·검증이 성공했다. 기존 DB는 조치 대상이 아니었다.
- 비밀값·접속 문자열·DB 파일은 Git 제외 로컬 폴더에만 보존했다. 보고서에도 접속 문자열을 넣지 않는다.
- 날짜 비교와 프로세스 격리 수정 커밋 `bba83dd`는 [전체 CI](https://github.com/ai-pixlate/server/actions/runs/45766616388)에서 러너 전체 시험과 이미지 빌드·시험을 통과했다. PR에서는 게시 job이 정상적으로 제외된다.

### DB 복원·재점검 방법

`tools.baseline.gs03_database`는 비과금 DB 준비 전용 도구다. 모델 import나 API 호출을 하지 않는다. 새 전용 DB가 이미 있으면 덮어쓰지 않고 중단한다. 스키마를 0008까지 적용하고 참조 자료를 한 트랜잭션으로 적재한 뒤 preflight를 수행한다. 명시 ID 적재 후 해당 참조 테이블 시퀀스도 맞춘다.

```powershell
# 아직 존재하지 않는 새 전용 DB를 최초 복원할 때만 사용한다.
& $py -B -m tools.baseline.gs03_database <준비묶음> --url-file <Git에서 제외한 접속파일>
# 이번 장비에서는 복원 완료 상태이므로 복원 명령을 반복하지 않는다.
$env:BASELINE_DATABASE_URL = (Get-Content -LiteralPath <Git에서 제외한 접속파일> -Raw).Trim()
& $py -B -m tools.baseline.gs03_supplement preflight <준비묶음>
Remove-Item Env:BASELINE_DATABASE_URL
```

현재 로컬 준비 묶음은 `.cache/gs03-supplement-20261126-r2`, 별도 DB 관리 폴더는 `.cache/gs03-db-20261126`이다. 서버 재시작은 해당 관리 폴더의 pgdata만 대상으로 하며 기존 `.cache/baseline-local/pgdata`는 사용하지 않는다. PostgreSQL 실행 파일은 기존 설치를 읽기 전용으로 재사용했다. DB를 시스템 서비스로 등록하거나 전역 환경변수를 바꾸지 않았다.

남은 실제 실행 차단 사항은 동일 Gemini 모델의 계정 접근·쿼터 확인, 실행 직전 요금·GPU 가용성 확인, 500회/$25 제안 상한 및 GS-03/120초 범위에 대한 사용자 승인이다. DB 준비 성공이 실행 승인이나 모델 실행 성공을 의미하지 않는다.

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
- 비과금 회귀 테스트 150개 통과: 승인 없음 차단, GS-03 선택, 타임아웃 단일 변경, 호출/금액 상한, 실패 예약 유지, 불변 저장, 기존 N3/N5 확인과 경고 미자동승인, 최초/최종 체크포인트 순서 포함.

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

## PR #161 추가 검토 반영

- 이전 준비 도구는 dataset_manifest_path의 파일을 복사하지 않아 공통 protocol.validate()를 통과하지 못했다. 원본의 34장 평가셋 manifest를 변형하지 않고 원래 경로·해시 그대로 복사한다. 이 파일은 출처이며 실행 source_keys와 원본 산출물은 GS-03 18개로 제한된다.
- 보완 validate가 protocol.validate도 수행하므로 prepare, validate, 실행 승인 점검, 실행 직전 preflight에서 동일한 공통 검증을 거친다. 공통 검증기나 기존 Baseline 실행기는 수정하지 않았다.
- `python -B -m tools.baseline.gs03_supplement preflight <준비 경로>`는 모델·제품 import 없이 환경 lock, 코드, 파일, 신규 전용 DB를 확인한다. 비용 승인을 생성하거나 요구하지 않는 비과금 명령이다. 실제 execute에는 별도 승인 조건이 그대로 적용된다.
- DB는 연결 제한 3초와 읽기 전용 트랜잭션으로 점검한다. 업무 테이블 존재/빈 상태, 마이그레이션 버전 및 참조 테이블 내용이 당시 사본과 같은지 확인한다. 테이블을 생성하거나 사전을 적재하지 않는다.
- 실행기는 위 점검 후 실제 제품 DB 연결을 다시 검증하며, 사전 재대조까지 성공한 뒤 업무 데이터 생성 전에 started.json을 배타 생성한다. DB 점검 실패로 실행 시작 기록을 남기지 않는다. 이미 시작한 회차의 기록 제거·자동 재실행은 허용하지 않는다.
- 새 로컬 준비 묶음 `.cache/gs03-supplement-20261126-r2`는 양쪽 검증기 모두 통과했다. 실제 DB preflight는 OperationalError로 차단되었고 started.json·generation-claims가 생성되지 않았다. 이전 준비 묶음은 수정하지 않았다.
- 추가 합성 테스트는 준비→양쪽 검증→실행 직전 점검 연결, manifest 누락 차단, 연결/스키마/사전 실패 시 무시작, 읽기 전용 SQL, 기존 시작 기록 유지 등을 검증한다. 승인 검사는 테스트 내 mock으로만 분리했고 실제 승인 자료는 생성하지 않았다.
- 실제 실행 차단 사항은 신규 전용 DB 접속·스키마/사전 준비, 모델 계정 권한·쿼터 및 비용/범위 승인이다. 실제 생성·모델 호출은 하지 않았다.
