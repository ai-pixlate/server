# 규제사전·현지부적합 사전 적재 (expression_dictionary)

데이터팀 규제사전(`regulation_dict.xlsx`)·현지부적합 사전(`locale_unsuitable_dict.xlsx`)을 RDS `expression_dictionary`·
`expression_dictionary_evidence`에 넣는 방법입니다. 스키마는 Alembic `0006`, 적재는 `python -m app.dictionary_ingest`가 합니다.

## 1. 무엇이 바뀌나

| 구분 | 내용 |
| --- | --- |
| 마이그레이션 `0006` | `expression_dictionary`: `UNIQUE(external_id)` · `UNIQUE(dict_type, target_country, regulatory_class, source_expression)` · `CHECK dict_type IN ('regulatory','local','channel')` · dict_type별 `verdict_status` CHECK · `exclusion_context`·`keep_context` TEXT 추가 |
| | `expression_dictionary_evidence`: `external_id` VARCHAR(32)(원본 근거 ID) · `UNIQUE(dictionary_id, external_id)` · 부분 UNIQUE 인덱스 `uq_expr_evidence_primary`(사전 항목당 대표 근거 최대 1건) |
| 로더 | `app/dictionary_ingest.py` — `validate`(파일만) · `plan`(DB 조회·비교만, 읽기 전용 트랜잭션·명시적 쓰기 차단 잠금 없음) · `load`(잠금 후 비교를 다시 하고 사전·근거를 한 트랜잭션으로 적재) |
| API·앱 코드 | 이번 적재 로더 외에 API·워커의 사전 조회 로직 변경은 없다. 마이그레이션 DDL 잠금과 운영에서의 실제 사용 여부는 적용 전에 확인한다 |

제약의 근거: 계약 정본 v1.3 §1-1·§4-1, `CHECK_enum_허용값_v3.4.1`, `ERD_수정문_v3.4.2` E4·M6·M7(0001이 제약 없는 SQL_Preview로 만들어져 빠져 있던 것).
dict_type별 판정 허용값: `regulatory → regulated·conditional·allowed` / `local → irrelevant·needs_fix·cultural` / `channel → policy`.
`regulatory_class`에는 CHECK를 두지 않는다(국가별 확장, 9/8 합의).

## 2. 저장 매핑

로더가 읽는 탭은 규제사전 파일의 「규제사전」·「근거」, 현지부적합 파일의 「01_현지부적합사전」뿐이다(각 파일 `00_읽는법`).

### 규제사전 → `expression_dictionary` (dict_type = regulatory)

| 원본 열 | DB 열 | 비고 |
| --- | --- | --- |
| id | `external_id` | `RG-###` |
| dict_type · target_country · regulatory_class | 같은 이름 | dict_type 은 `regulatory` 만, 분류는 `cosmetic`·`otc`·`common` |
| source_expression | 같은 이름 | |
| variant_expressions.ko / .en | `variant_ko` / `forbidden_en` | `; `로 나눈 JSON 배열 |
| alternative_expression | 같은 이름 | 문자열 그대로(여러 값의 `; ` 포함) |
| verdict_status | 같은 이름 | `rewritable` → `regulated` 로 바꿔 저장(9/21 PM 결정기록). 원래 값은 DB에 남지 않는다 |
| reason | 같은 이름 | |
| **verified_at (본문)** | **`confirmed_date`** | 필수 |
| evidence_id · evidence_source_type · evidence_article · evidence_url | (근거 테이블) | 「근거」 탭 대표 근거 행과 같은지 검사 |
| internal_category · confidence · source · version | 저장 안 함 | 검사는 한다 — 아래 3절 |

### 「근거」 탭 → `expression_dictionary_evidence` (규제 항목마다 대표 근거 1건)

| 원본 열 | DB 열 | 비고 |
| --- | --- | --- |
| evidence_id | `external_id` | 같은 근거가 여러 항목에 연결될 수 있다(예: MN-001 → RG-003·011·012) |
| source_type · quote · article · url | `evidence_source_type` · `evidence_quote` · `evidence_article` · `evidence_url` | |
| — | `evidence_document` | 시트에 해당 열이 없어 NULL |
| is_primary | `is_primary` | 이번 적재는 `Y` 인 대표 근거만(계약 정본 §9 "9월은 is_primary 1건만") |
| **verified_at (근거)** | **저장 안 함** | 근거 테이블에 열이 없다. 필수값 검사만 한다 |
| rg_id · company · issued_at | 저장 안 함 | rg_id 는 연결 검사에만 쓴다 |

### 현지부적합 → `expression_dictionary` (dict_type = local)

| 원본 열 | DB 열 | 비고 |
| --- | --- | --- |
| id | `external_id` | `LC-##` |
| (고정) | `target_country` = `US`, `regulatory_class` = `common` | **미국 작업의 모든 규제 분류에 공통 적용**한다는 뜻. 국가 범위를 없애는 것이 아니다 |
| 항목 · 패턴 · 판정 | `source_expression` · `variant_ko`(JSON 배열) · `verdict_status` | `forbidden_en`·`alternative_expression` 은 NULL |
| 제외하는 맥락 · 제외하지 않는 맥락 | `exclusion_context` · `keep_context` | 사용설명서 §3 신규 필드 |
| 셀러 문장 | `reason` | |
| **verified_at** | **`confirmed_date`** | 필수 |
| 판단 근거 · kr_freq · kr_corpus | 저장 안 함 | 이번 DB 적재에서 제외하고 **원본 파일로 보존**한다. "내부 기록용"이 DB 저장 금지를 뜻하지는 않는다. `판단 근거`를 빼므로 DB만으로 판정의 내부 배경을 복원할 수 없다 |

현지 항목에는 근거 행을 만들지 않는다.

## 3. 검증·재적재 규칙

- 필수값이 비거나 형식이 틀리면 오류. 오류가 한 건이라도 있으면 사전·근거 모두 쓰지 않는다.
  - 규제사전 필수(●): id · dict_type · target_country · regulatory_class · source_expression · variant ko/en · verdict_status · reason · evidence_id · evidence_source_type · evidence_url · verified_at · confidence · version
  - 시트 전용 열: `confidence`는 `high`만(9월 시드), `version`은 양의 정수, `internal_category`는 9월엔 비어 있어야 한다. DB에는 저장하지 않는다.
  - 대체 표현: `allowed`는 비어야 하고, `conditional`·`rewritable`은 채워져야 한다.
  - 대표 근거: 「근거」 탭에 있고, `is_primary=Y`이며 `rg_id`에 그 항목이 있고, 그 항목을 가리키는 대표가 정확히 1건, source_type·article·url 이 규제사전 행과 같아야 한다. 근거 ID 접두어(WL·GD·MN·MP)와 source_type 이 맞아야 하고, `시장 관행`은 allowed 행에만.
  - 근거 탭의 필수값(evidence_id·source_type·quote·url·verified_at)은 **적재할 대표 근거 행**에서 검사한다. 근거 ID 형식·중복은 탭 전체에서 검사한다.
  - 현지부적합 필수: id · 항목 · 패턴 · 판정 · 두 맥락 · 셀러 문장 · verified_at.
- 재적재
  - 사전: 같은 `external_id`는 내부 PK를 유지한다. 모든 열이 같으면 변경 없음, 다르면 갱신(9/8 "ON CONFLICT DO UPDATE" 합의).
    같은 ID의 `dict_type`·국가·규제 분류가 바뀌면 오류(번호 재사용 의심). 같은 내용 키에 다른 ID면 오류.
  - 대표 근거: `(사전 항목, 근거 ID)` 연결 행의 PK 를 유지한다. 대표가 다른 근거로 바뀌면 **이전 대표 연결은 삭제하지 않고 비대표(`is_primary=false`)로 보존**한다.
    **근거 내용의 변경 이력은 별도로 관리하지 않는다** — 같은 연결의 인용문·URL 이 바뀌면 그 행을 갱신한다.
  - 원본 근거 ID 가 없는 기존 대표 근거가 있으면 오류(연결 방법 결정 필요).
  - 적재 직후(커밋 전) 규제 항목마다 대표 근거가 정확히 1건이고 파일과 같은지, 현지 항목은 근거가 없는지 검사한다.
  - 파일에 없는 기존 행은 지우지 않고 "이번 파일에 없는 기존 행(유지)"로 센다.

### 제한

- **옛 파일로 최신 내용을 덮어쓰는 것을 막지 못한다.** DB에 version 을 저장하지 않기 때문이다.
  로더가 출력하는 파일 SHA-256 과 version 분포는 **파일 식별용**이며 역행 방지 장치가 아니다(특정 ID의 이전 버전 재적재를 판별하지 못한다).
  그래서 **적재한 원본 파일(S3 `seed/`)과 실행 기록(아래 E의 로그 파일)을 함께 보관**하고, 적재 전 이전 기록과 비교한다.
- `source_expression`은 NULL 허용이라 NULL 끼리는 내용 키 UNIQUE 가 막지 못한다(0006 사전 검사도 같은 기준으로 NULL 행을 뺀다). 로더는 필수로 검사한다.
- 대체 표현에 한국어가 남은 항목은 적재하되 `⚠ 미해결`로 따로 출력한다. **적재됐다고 프롬프트에 쓸 수 있는 상태가 아니다.**

## 4. 현재 전달본(2026-09-28) 상태 — 적재 보류

| 항목 | 상태 |
| --- | --- |
| **대표 근거 날짜 누락** | 「근거」 탭 `MN-001·002·003·004·006·007·008`의 필수 `verified_at`이 비어 있다(사용설명서 §9 데이터팀 미완료 항목). 이 근거를 쓰는 otc 10행(RG-003·011·012·013·014·015·016·020·021·022)이 검증에서 거부되어 **파일 전체를 적재하지 않는다.** 로더는 날짜를 채우지 않는다. 데이터팀이 날짜를 확정해 고치고, **전달본 「근거」 탭에 값이 실제로 들어갔는지** 다시 확인한 뒤 적재한다(「근거」 탭은 원천 탭을 고쳐도 전달본에서 자동으로 바뀌지 않는다) |
| **RG-021·022 대체 표현 자리표시자** | `[§ M020.80 시험 결과값]`(한국어)이 남아 있다. 적재는 가능하지만 번역 프롬프트에 쓰면 안 된다. 번역 연결 전 데이터 정정 또는 사용 단계의 주입 차단을 확인한다(모델팀·데이터팀 전달) |
| 날짜 보완 후 기대값 | 사전 24(규제 16: regulated 10·conditional 4·allowed 2, 현지 8: irrelevant 5·needs_fix 3) · 대표 근거 16(원본 근거 ID 13종) · rewritable→regulated 2(RG-008·030) |

## 5. 로컬에서 실행·테스트 (Windows PowerShell)

```powershell
python -m app.dictionary_ingest validate --regulatory "C:\경로\regulation_dict.xlsx" --local "C:\경로\locale_unsuitable_dict.xlsx"
```

| 환경변수 | 쓰는 테스트 |
| --- | --- |
| `GLOSSARY_TEST_DATABASE_URL` | `tests/test_dictionary_db.py`(용어집 DB 테스트와 같은 변수) — 테스트마다 전용 DB를 만들고 지운다. `DATABASE_URL`만 있으면 실행하지 않는다. 운영 주소인지 자동 판별하지 않으므로 운영 DB를 지정하지 않는다 |
| `DICT_XLSX_DIR` | 실제 전달본 검증 — `regulation_dict*.xlsx`·`locale_unsuitable_dict*.xlsx`가 있는 폴더. 원본이 날짜 누락으로 막히는지, 날짜를 채운 **테스트용 사본**(원본은 그대로)이 24·16·8로 두 번 적재되는지 본다 |

## 6. EC2 → RDS 적용 (Linux Bash) — 4절의 날짜 보완 후

| 자리표시자 | 값 |
| --- | --- |
| `<SHA>` | 이 변경이 develop에 머지된 승인된 커밋 |
| `<날짜>` | 데이터팀 전달본 날짜(S3 폴더 이름) |

DB 작업만 한다. API·워커는 재시작하지 않는다(Redis 큐가 비워진다 — `deploy/README.md`).

### A. 사전 확인 — 쓰기 없음

```bash
cd ~/pixlate-api && git status --short; git branch --show-current; git log --oneline -1
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api alembic current
sudo docker run --rm -i --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api python - <<'PY'
from sqlalchemy import text
from app.db import engine
Q = {
    "사전 행 수·분포": "SELECT dict_type, count(*) FROM expression_dictionary GROUP BY 1",
    "근거 행 수": "SELECT count(*) FROM expression_dictionary_evidence",
    "사전 제약": "SELECT conname FROM pg_constraint WHERE conrelid='expression_dictionary'::regclass ORDER BY 1",
}
with engine.connect() as c:
    for name, sql in Q.items():
        print(f"== {name}")
        for r in c.execute(text(sql)):
            print("  ", tuple(r))
PY
```

판단 — 하나라도 해당하면 **적용하지 않고 공유한다.**
- DB revision 이 단일 `0005`가 아니다. `(head)` 표시는 실행 이미지에 따라 달라지므로 판정에 쓰지 않는다. 더 낮으면 `upgrade 0006`이 이전 마이그레이션까지 적용한다.
- 사전·근거 행이 0이 아니다(기존 행이 있으면 0006 사전 검사·로더 재적재 규칙을 따로 검토).

### A-2. 백업

RDS 콘솔 `pixlate-db` → 유지 관리 및 백업 → 자동 백업·최근 복원 가능 시간 확인 → **스냅샷 생성** `pixlate-db-before-0006-<날짜>` → `사용 가능`까지 대기.

### B. 이미지 준비 (develop 머지 후)

```bash
cd ~/pixlate-api && git status --short && git fetch origin && git log --oneline -3 origin/develop
git merge --ff-only <SHA> && git rev-parse --short HEAD
docker build -t pixlate-api:dict-<SHA> .
sudo docker run --rm pixlate-api:dict-<SHA> alembic heads          # 0006 (head) 한 줄
sudo docker run --rm pixlate-api:dict-<SHA> python -m app.dictionary_ingest --help | head -3
```

### C. 적용할 SQL 보기 — DB 접속 없음 (A에서 `0005` 확인한 경우만)

```bash
sudo docker run --rm pixlate-api:dict-<SHA> alembic upgrade 0005:0006 --sql > ~/dictionary_0006.sql 2>/dev/null; cat ~/dictionary_0006.sql
```

사전 검사 `DO $$ … $$`(ID·내용 키 중복, 허용값 밖, 대표 근거 2건 이상)가 들어 있다. xlsx 검증은 E가 한다.

### D. 마이그레이션

```bash
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api:dict-<SHA> alembic upgrade 0006
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api:dict-<SHA> alembic current   # 단일 0006
```

### E. 파일 전달·검증·적재 — 실행 기록을 파일로 남긴다

S3 `pixlate-storage-2026/seed/dictionary/<날짜>/`에 `regulation_dict.xlsx`·`locale_unsuitable_dict.xlsx`를 올린다(퍼블릭 설정 없음). 로컬 해시를 적어 둔다.

**E-1. 파일 받기·해시**

```bash
mkdir -p ~/dictionary-input && chmod 700 ~/dictionary-input
aws s3 cp s3://pixlate-storage-2026/seed/dictionary/<날짜>/ ~/dictionary-input/ --recursive && sha256sum ~/dictionary-input/*.xlsx
```

**E-2. 공통 설정** — 같은 터미널에서 E-3·E-4·F를 실행한다(새 터미널이면 다시 실행).

```bash
set -o pipefail
IMG=pixlate-api:dict-<SHA>
FILES="--regulatory /input/regulation_dict.xlsx --local /input/locale_unsuitable_dict.xlsx"
LOG=~/dictionary-load-<날짜>.log
DB="--network pixlate-net --env-file /etc/pixlate/pixlate.env"
ingest() { sudo docker run --rm "$@" 2>&1 | tee -a "$LOG"; }
dict() { ingest $DB -v ~/dictionary-input:/input:ro $IMG python -m app.dictionary_ingest "$1" $FILES; }

dict_check() {   # E-3: validate 와 plan 이 모두 성공해야 0
  local rc
  ingest -v ~/dictionary-input:/input:ro $IMG python -m app.dictionary_ingest validate $FILES \
    || { rc=$?; echo "STOP: validate 실패(exit $rc) — load 하지 않는다. $LOG 확인"; return "$rc"; }
  dict plan || { rc=$?; echo "STOP: plan 실패(exit $rc) — load 하지 않는다. $LOG 확인"; return "$rc"; }
  echo "OK: validate·plan 통과 — 출력을 확인한 뒤 E-4"
}

dict_load() {    # E-4
  local rc
  dict load || { rc=$?; echo "STOP: 실행 또는 로그 기록 실패(exit $rc) — DB 반영 여부를 확인하고, 재실행 전에 plan과 로그를 점검한다"; return "$rc"; }
  echo "OK: load 완료"
}

dict_recheck() { # F
  local rc
  dict plan || { rc=$?; echo "STOP: 재비교 실패(exit $rc) — $LOG 확인"; return "$rc"; }
  echo "OK: 재비교 완료"
}
```

- `pipefail`: 로더가 실패하면 `tee`가 성공해도 `ingest`가 실패 코드를 돌려준다.
- `2>&1`: 오류 출력(트레이스백 등)도 로그 파일에 남는다.
- 세 함수는 실패하면 그 종료 코드를 `return` 한다. 바로 다음에 `echo $?`로 확인할 수 있다(성공 0).

**E-3. 파일 검증 → DB 비교** — 둘 다 성공해야 OK

```bash
dict_check
```

- `STOP`이면 멈추고 로그를 공유한다(4절 날짜 누락이면 validate 에서 막힌다). validate 가 실패하면 plan 은 실행하지 않는다.

**E-4. 적재** — E-3이 `OK`일 때만

```bash
dict_load
```

- `STOP`은 로더 실패뿐 아니라 Docker 실행 실패·로그 기록 실패에서도 나온다. 로더가 실패하면 사전·근거는 전체 롤백되지만,
  DB 커밋 후 로그 기록만 실패했을 수도 있으므로 **롤백됐다고 가정하지 않는다.** F의 `dict_recheck`(plan)로 DB 반영 여부를 먼저 확인한다.
- load 출력의 `PK 지문`을 기록한다. 로그 파일은 적재한 원본 파일(S3 `seed/`)과 함께 보관한다.

### F. 적용 후 확인

E-2 설정이 된 터미널에서:

```bash
dict_recheck
```

- `사전: 삽입 0 · 갱신 0 · 변경 없음 24`, `대표 근거: 삽입 0 · 갱신 0 · 변경 없음 16 · 대표 해제 0`
- `DB에 있는 행 24/24 · 대표 근거 16` 이고 **PK 지문이 load 출력과 같을 것**
- `⚠ 미해결` 목록(RG-021·022)을 모델팀·데이터팀에 전달한다.

### G. 영향과 복구

- 0006 은 사전 두 테이블에 짧은 배타 잠금을 잡는다(열 추가·제약·인덱스 생성). 운영 소요 시간은 실측하지 않았다. 잠금 대기는 `lock_timeout 5s`로 끊긴다.
- load 는 적재하는 동안 두 테이블의 다른 쓰기를 막는다(읽기는 된다). plan 은 명시적 쓰기 차단 잠금을 잡지 않는다.
- 문제가 생기면 전진 수정(새 전달본 재적재·새 마이그레이션) 또는 A-2 스냅샷을 새 인스턴스로 복원해 검토한다.
  `alembic downgrade 0005`는 추가한 열(현지 맥락·근거 원본 ID)과 그 값을 지운다. 적재한 행을 지우는 것은 영구 삭제이고
  `section_verdict.dictionary_id`·`compliance_flags.dictionary_id`가 PK 를 참조할 수 있으므로 기본 복구 절차가 아니다.

## 7. 이번에 하지 않은 것

| 항목 | 상태 |
| --- | --- |
| 근거 N건 전부 적재(D-9 정규화) | 10월 |
| 섹션 제외 사유 `auto_local_needs_check`, 판정 명칭 변경(needs_fix→needs_check) | 섹션·API 쪽, 10월 |
| 채널 사전(`channel`) · 12월 편집 콘솔 | 범위 밖 |
| 판정 엔진(리터럴 매칭)·프롬프트 주입 | 모델팀 |
| 적재 기록 DB 테이블(파일 해시·실행 이력) | 후속 — 지금은 S3 원본 + 로그 파일 |
| `source_expression` NOT NULL | 후속 검토 |
