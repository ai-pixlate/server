# 용어집 적재 (glossary)

데이터팀 용어집 3종(성분·인증·문구 xlsx)을 RDS `glossary` 테이블에 넣는 방법입니다.
스키마는 Alembic `0005`, 적재는 `python -m app.glossary_ingest`가 합니다.

## 1. 무엇이 바뀌나

| 구분 | 내용 |
| --- | --- |
| 마이그레이션 `0005` | `term_ko`·`term_target` VARCHAR(200) → TEXT. 원본 열 13개 추가(모두 NULL 허용). `UNIQUE(external_id)` · `UNIQUE(target_lang, internal_category, term_ko)` · `CHECK(enforcement IN ('enforced','reference'))` |
| 로더 | `app/glossary_ingest.py` — `validate`(파일만) · `plan`(DB 조회·비교만, 읽기 전용 트랜잭션·명시적 쓰기 차단 잠금 없음) · `load`(잠금 후 비교를 다시 하고 적재) |
| 의존성 | `openpyxl` (xlsx 읽기) |
| API·앱 코드 | 이번 적재 로더 외에 API·워커의 glossary 조회 로직 변경은 없다. 마이그레이션 DDL 잠금과 운영에서의 실제 사용 여부(수동 조회·다른 서비스)는 적용 전에 확인한다 |

### 저장 열 (데이터팀 9/28 확정)

| 원본 열 | DB 열 | 타입 |
| --- | --- | --- |
| id | `external_id` | VARCHAR(32) |
| source_ko · target_text | `term_ko` · `term_target` | TEXT |
| target_lang · internal_category · enforcement · example_sentence | 같은 이름 | 기존 |
| 구역 | `zone` | VARCHAR(10) |
| term_kind | `term_kind` | VARCHAR(20) |
| frequency · corpus_size | 같은 이름 | INTEGER |
| corpus_version | 같은 이름 | VARCHAR(20) |
| verified_at | 같은 이름 | DATE |
| source · note | 같은 이름 | TEXT |
| version | 같은 이름 | INTEGER |
| 적재여부 · 라벨 · 대표여부 (문구) | `load_flag` · `label` · `representative` | VARCHAR(4) · VARCHAR(100) · VARCHAR(10) |

### 적재 규칙

- 대상: 인증·성분 `구역=A`, 문구 `구역=A` 그리고 `적재여부=Y`. 그 외는 적재하지 않고 사유별 건수만 보고한다.
- 구역은 파일별 안내 시트에 적힌 값만 허용한다 — 인증 `A·B·C`, 성분 `A·C`, 문구 `A·C·보류·이관`.
  그 밖의 값(`A `처럼 공백이 붙은 값, 오타, 빈칸)은 제외가 아니라 **오류**다.
- 원본 ID 형식(`GL-C`/`GL-I`/`GL-M` + 숫자)과 중복은 **제외 행까지 모든 데이터 행**에서, 세 파일을 가로질러 검사한다.
  번역어·버전 등 나머지 값 검사는 적재 대상 행에만 한다(제외 행은 대응 미정으로 비어 있을 수 있다).
- 값은 원본 그대로. 바꾸는 것은 `강제→enforced`·`참고→reference`, `Sunscreens & Tanning→Sunscreens & Tanning Products` 둘뿐.
- `target_text`의 `; `(대안)·`/`(표현 일부)는 나누지 않는다. 빈 숫자 칸은 0이 아니라 NULL.
- 오류가 한 건이라도 있으면 아무것도 쓰지 않는다. 오류에는 파일·엑셀 행 번호·원본 ID·열이 나온다.
- 재적재: 같은 원본 ID는 내부 PK(`glossary.id`, `compliance_flags.glossary_id`가 참조)를 유지한다.
  모든 열이 같으면 변경 없음, version이 올라갔으면 갱신. 아래는 **오류로 중단**한다.
  - version이 낮아짐 · 같은 version인데 내용이 다름
  - 같은 원본 ID인데 자연키(언어·카테고리·한국어)가 바뀜 — 번호 재사용 의심
  - 같은 자연키에 다른 원본 ID · 원본 ID 없는 기존 행과 자연키가 겹침
  - 이미 적재된 원본 ID가 새 파일에서 적재 제외(C·보류 등)로 바뀜
- 새 파일에 없는 기존 행은 지우지 않고 "이번 파일에 없는 기존 행(유지)"로 센다.
- **제한 — 같은 원본 ID의 한국어·언어·카테고리 정정**: 번호 재사용을 막기 위해 정상적인 오타 수정도 로더가 막는다.
  이런 정정은 로더로 반영되지 않으며, 별도로 검토·승인된 수동 정정 절차가 필요하다.
- **제한 — `term_ko` 길이**: 로더는 길이를 제한하지 않는다. 자연키 UNIQUE 인덱스(`target_lang, internal_category, term_ko`)에
  항목 크기 한도가 있다. 테스트한 PostgreSQL 16 환경에서 B-tree 인덱스 항목 크기 한도는 2,704바이트로 관찰됐다
  (압축되지 않는 한국어 `term_ko` 2,400바이트 통과 · 2,700바이트 거부, 반복 문자열 15,000바이트 통과).
  이것은 `term_ko` 단독의 고정 입력 한도가 아니며, 압축과 복합 키 구성에 따라 적재 가능 여부가 달라진다.
  초과하면 `load`의 INSERT에서 DB 오류로 **전체 롤백**되고, `plan`으로는 미리 알 수 없다. 이번 전달본 `term_ko` 최대는 754바이트다.

## 2. 로컬에서 실행·테스트 (Windows PowerShell)

```powershell
python -m app.glossary_ingest validate `
  --certification "C:\경로\glossary_certification.xlsx" `
  --ingredient    "C:\경로\glossary_ingredient.xlsx" `
  --phrase        "C:\경로\glossary_phrase.xlsx"
```

DB 테스트는 `GLOSSARY_TEST_DATABASE_URL`이 있을 때만 실행하며, 테스트마다 전용 DB를 만들고 지운다. `DATABASE_URL`만 설정된 경우에는 실행하지 않는다.
로더가 운영 주소인지 자동으로 판별하지는 않으므로, 테스트용 접속 주소에 운영 DB 환경을 지정하지 않는다. 두 환경변수가 없으면 해당 테스트는 건너뛴다.

| 환경변수 | 쓰는 테스트 |
| --- | --- |
| `GLOSSARY_TEST_DATABASE_URL` | `tests/test_glossary_db.py` — CREATE DATABASE 권한이 있는 접속(예: 로컬 postgres:16의 `postgres` DB). 테스트마다 새 DB를 만들고 지운다 |
| `GLOSSARY_XLSX_DIR` | 실제 전달본 검증 — `glossary_certification*.xlsx` 등 3개가 있는 폴더 |

```powershell
$env:GLOSSARY_TEST_DATABASE_URL = "postgresql+psycopg://<user>:<pw>@localhost:5432/postgres"
$env:GLOSSARY_XLSX_DIR = "C:\Users\<사용자>\Downloads"
python -m pytest tests/test_glossary_ingest.py tests/test_glossary_db.py -q
```

## 3. EC2 → RDS 적용 (Linux Bash, EC2 인스턴스 연결 터미널)

아래 자리표시자는 실행 전에 실제 값으로 바꾼다.

| 자리표시자 | 값 |
| --- | --- |
| `<SHA>` | 이 변경이 develop에 머지된 **승인된 커밋**(짧은 해시) |
| `<버킷>` | S3 버킷 이름 (`/etc/pixlate/pixlate.env`의 `S3_BUCKET`) |

이 절차는 **DB 마이그레이션과 적재만** 한다. 실행 중인 API·워커는 glossary를 쓰지 않으므로 재시작하지 않는다
(재시작하면 Redis 큐가 비워진다 — `deploy/README.md`). 이미지 교체는 평소 배포 때 따로 한다.

### A. 사전 확인 — 쓰기 없음

```bash
cd ~/pixlate-api
git status --short                 # 출력이 없어야 한다(수정 파일이 있으면 멈추고 확인)
git branch --show-current          # develop
git log --oneline -1               # 지금 서버 코드
sudo docker run --rm pixlate-api ls migrations/versions   # 지금 이미지에 든 마이그레이션(0004까지일 것)
```

현재 DB revision (접속 정보는 출력되지 않는다):

```bash
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api alembic current
```

glossary 현재 상태:

```bash
sudo docker run --rm -i --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api python - <<'PY'
from sqlalchemy import text
from app.db import engine
Q = {
    "열(이름·타입·길이·NULL·기본값)": "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default, is_identity FROM information_schema.columns WHERE table_name='glossary' ORDER BY ordinal_position",
    "제약": "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='glossary'::regclass",
    "행 수": "SELECT count(*) FROM glossary",
    "자연키 중복 조합": "SELECT count(*) FROM (SELECT 1 FROM glossary GROUP BY target_lang, internal_category, term_ko HAVING count(*)>1) d",
    "enforcement 분포": "SELECT enforcement, count(*) FROM glossary GROUP BY 1",
    "카테고리 분포": "SELECT internal_category, count(*) FROM glossary GROUP BY 1",
}
with engine.connect() as c:
    for name, sql in Q.items():
        print(f"== {name}")
        for r in c.execute(text(sql)):
            print("  ", tuple(r))
PY
```

판단 — 아래 중 하나라도 해당하면 **적용하지 않고 결과를 공유한다.**
- DB의 현재 revision이 단일 `0004`가 아니다. `(head)` 표시는 명령을 실행한 이미지의 마이그레이션 목록에 따라 붙거나 빠지므로
  합격 조건으로 쓰지 않는다(0005가 든 새 이미지로 확인하면 DB가 정상적으로 `0004`여도 `(head)`가 붙지 않는다).
  - 더 낮은 revision(예: `0003`)이면 `upgrade 0005`가 이번 용어집 변경 외의 **이전 마이그레이션까지 함께 적용**한다.
  - 여러 줄, 빈 출력, 모르는 revision도 같다. 이미지의 `alembic heads`(B)와 DB의 `alembic current`는 서로 다른 확인이다.
- glossary 열이 0004 기준(`id`·`term_ko` VARCHAR(200)·`term_target` VARCHAR(200)·`target_lang`·`internal_category`·`enforcement`·`example_sentence` 7개)과 다르다.
- 행 수가 0이 아니다. 원본 ID가 없는 예전 행이라, 새 데이터와 자연키가 겹치면 로더가 멈춘다(연결 방법을 따로 정한다).
- 자연키 중복이 있거나 enforcement가 `enforced`/`reference` 외 값이다(`0005`가 멈춘다).

### A-2. 백업 확인 — 쓰기 전에

1. AWS 콘솔 → RDS → `pixlate-db` → **유지 관리 및 백업** 탭 — 자동 백업 "활성", 보존 기간, **최근 복원 가능 시간**이 방금 전인지 확인.
2. 같은 화면 **작업 → 스냅샷 생성** — 이름 `pixlate-db-before-0005-<날짜>`. 상태가 "사용 가능"이 될 때까지 기다린다.
   (EC2 역할에는 RDS 권한이 없어 콘솔에서 한다.)

### B. 배포 이미지 준비 — 승인된 커밋 고정

develop에 머지된 뒤에 한다. 다른 브랜치를 섞지 않는다.

```bash
cd ~/pixlate-api
git status --short                 # 비어 있어야 한다
git branch --show-current          # develop
git fetch origin
git log --oneline -5 origin/develop   # <SHA> 가 있는지 확인
git merge --ff-only <SHA>
git rev-parse --short HEAD         # <SHA> 와 같아야 한다
docker build -t pixlate-api:glossary-<SHA> .
```

이미지에 새 코드가 들어갔는지:

```bash
sudo docker run --rm pixlate-api:glossary-<SHA> ls migrations/versions        # 0005_glossary_ingestion.py
sudo docker run --rm pixlate-api:glossary-<SHA> alembic heads                  # 0005 (head) 한 줄
sudo docker run --rm pixlate-api:glossary-<SHA> python -m app.glossary_ingest --help
```

`alembic heads`가 두 줄 이상이거나 0005가 아니면 멈춘다.

### C. 적용할 SQL 검토 — DB 접속 없음

A에서 `alembic current`가 `0004`로 확인된 경우에만, `0004`부터 `0005`까지만:

```bash
sudo docker run --rm pixlate-api:glossary-<SHA> alembic upgrade 0004:0005 --sql > ~/glossary_0005.sql
less ~/glossary_0005.sql           # 나올 때는 영문 입력으로 q
```

- 이 SQL은 **증분 변경분**이다. 빈 DB 전체 생성 SQL(`alembic upgrade head --sql`, 0001~0005)과 다르며 RDS에 쓰지 않는다.
- 기존 데이터 점검(`DO $$ … $$` — 자연키 중복·enforcement 값)은 이 SQL 안에 들어 있어 실행 시점에 다시 검사한다.
  xlsx 내용 검증은 SQL에 없고 E의 `validate`·`plan`이 한다.

### D. 마이그레이션 실행

A의 판단 조건을 모두 통과하고(DB가 `0004`), C의 SQL을 검토한 뒤에만 실행한다.

```bash
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api:glossary-<SHA> alembic upgrade 0005
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env pixlate-api:glossary-<SHA> alembic current   # 단일 0005
```

`head`가 아니라 `0005`를 지정한다. 이미지에 0005 이후 마이그레이션이 더 있어도 이번 변경까지만 적용된다.
(반대로 DB가 0004보다 낮으면 그 사이 마이그레이션도 적용되므로 A에서 멈춰야 한다.)
`0005 중단 - …`이 나오면 아무것도 바뀌지 않은 상태(트랜잭션 취소)다. 메시지를 공유한다.
잠금을 5초 넘게 기다리면 `lock timeout`으로 실패한다 — 잠시 뒤 다시 실행한다.

### E. 파일 전달·검증·적재

**E-1. 파일 전달** — S3 경유(EC2 역할로 받는다). 먼저 로컬 PC에서 해시를 적어 둔다.

```powershell
# 로컬 Windows PowerShell
Get-FileHash "C:\경로\glossary_*.xlsx" -Algorithm SHA256
```

AWS 콘솔 → S3 → `<버킷>` → 폴더 `seed/glossary/2026-09-28/` → 3개 파일을 아래 이름으로 업로드
(`glossary_certification.xlsx` · `glossary_ingredient.xlsx` · `glossary_phrase.xlsx`, 퍼블릭 설정 없음).

```bash
mkdir -p ~/glossary-input && chmod 700 ~/glossary-input
aws s3 cp s3://<버킷>/seed/glossary/2026-09-28/ ~/glossary-input/ --recursive
ls -l ~/glossary-input && sha256sum ~/glossary-input/*.xlsx   # 로컬 해시와 같아야 한다
```

**E-2. 파일만 검증** (DB 접속 없음, 입력 폴더는 읽기 전용 마운트):

```bash
IMG=pixlate-api:glossary-<SHA>
FILES="--certification /input/glossary_certification.xlsx --ingredient /input/glossary_ingredient.xlsx --phrase /input/glossary_phrase.xlsx"
sudo docker run --rm -v ~/glossary-input:/input:ro $IMG python -m app.glossary_ingest validate $FILES
```

이번 전달본 기대값: 인증 22 · 성분 20566 · 문구 65(제외 27) · 합계 20653 · 200자 초과 41 · `결과: 파일 검증 통과`.

**E-3. DB와 비교만** (읽기 전용 트랜잭션, 명시적 쓰기 차단 잠금 없음 — 다른 쓰기 트랜잭션을 기다리지 않는다):

```bash
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env -v ~/glossary-input:/input:ro $IMG python -m app.glossary_ingest plan $FILES
```

빈 테이블이면 `삽입 20653 · 갱신 0 · 변경 없음 0`. 오류가 있으면 적재하지 않고 공유한다.
plan 이후 DB가 바뀔 수 있으므로 load는 잠금을 잡은 뒤 비교를 처음부터 다시 한다.
`term_ko` 인덱스 크기 한도 초과(1절 제한)는 plan에서 보이지 않는다.

**E-4. 적재**:

```bash
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env -v ~/glossary-input:/input:ro $IMG python -m app.glossary_ingest load $FILES
```

`결과: 적재 완료 — 삽입 20653 …`. 한 트랜잭션이라 중간에 실패하면 아무것도 들어가지 않는다.
적재 중에는 `LOCK TABLE glossary IN SHARE ROW EXCLUSIVE MODE`로 다른 쓰기만 막는다(읽기는 된다).
출력의 `이번 파일 원본 ID 중 DB에 있는 행 20653/20653 · PK 지문 <md5>`를 **기록해 둔다**(F에서 비교).

### F. 적용 후 확인

새 터미널이면 `IMG=pixlate-api:glossary-<SHA>`와 `FILES=…`(E-2)를 다시 정의한다.

**F-1. 이번 파일 기준 검증 — 합격 판정은 이것으로 한다.** 이번 파일의 원본 ID 집합만 비교하므로 다른 전달본 행이 섞이지 않는다.

```bash
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env $IMG alembic current   # 단일 0005 (`(head)` 표시 여부는 판정에 쓰지 않는다)
sudo docker run --rm --network pixlate-net --env-file /etc/pixlate/pixlate.env -v ~/glossary-input:/input:ro $IMG python -m app.glossary_ingest plan $FILES
```

- `삽입 0 · 갱신 0 · 변경 없음 20653` — 파일의 모든 적재 행이 DB에 한 칸씩 같은 값으로 있다(200자 초과 41행의 내용 보존 포함).
- `이번 파일 원본 ID 중 DB에 있는 행 20653/20653 · PK 지문 <md5>` — E-4에서 기록한 지문과 같으면 이번 파일 행들의 내부 PK가 그대로다.
- 제외 행(C·보류·이관·적재여부=N)은 적재 대상 목록에 없으므로 위 비교에 들어가지 않는다. 들어가 있지 않은지는 F-2에서 본다.

**F-2. 스키마와 전체 통계 — 참고용.** 전체 glossary를 대상으로 하므로 다른 전달본이 있으면 이번 기대값과 다를 수 있다.

```bash

sudo docker run --rm -i --network pixlate-net --env-file /etc/pixlate/pixlate.env $IMG python - <<'PY'
from sqlalchemy import text
from app.db import engine
Q = {
    "제약": "SELECT conname FROM pg_constraint WHERE conrelid='glossary'::regclass ORDER BY 1",
    "term_ko/term_target 타입": "SELECT column_name, data_type FROM information_schema.columns WHERE table_name='glossary' AND column_name IN ('term_ko','term_target')",
    "원본 ID 대역별 건수(전체 통계)": "SELECT left(external_id,4), count(*) FROM glossary WHERE external_id LIKE 'GL-%' GROUP BY 1 ORDER BY 1",
    "200자 초과(길이)": "SELECT external_id, length(term_ko), length(term_target) FROM glossary WHERE length(term_ko)>200 OR length(term_target)>200 ORDER BY 1",
    "카테고리(전체 통계)": "SELECT internal_category, count(*) FROM glossary WHERE external_id LIKE 'GL-%' GROUP BY 1",
    "이번 전달본 제외 ID가 들어갔는지(0이어야 함)": "SELECT count(*) FROM glossary WHERE external_id IN ('GL-M004','GL-M050')",
    "구분자 보존": "SELECT external_id, term_target FROM glossary WHERE external_id IN ('GL-M038','GL-M039','GL-M043','GL-M056') ORDER BY 1",
}
with engine.connect() as c:
    for name, sql in Q.items():
        print(f"== {name}")
        for r in c.execute(text(sql)):
            print("  ", tuple(r))
PY
```

참고 기대값(빈 테이블에 처음 적재한 경우에만): 대역별 `GL-C 22 · GL-I 20566 · GL-M 65`, 200자 초과 41행,
카테고리 `common 20645 · Sunscreens & Tanning Products 8`. 다르면 F-1 결과를 기준으로 판단한다.

### G. 서비스 영향과 복구

**영향**
- 마이그레이션은 glossary에 배타 잠금을 잡는다. `ALTER COLUMN … TYPE TEXT`(VARCHAR→TEXT, 테이블 재작성 없음)와 열 추가(NULL 허용·기본값 없음)는 메타데이터 변경이고,
  UNIQUE 인덱스 생성은 표 전체를 읽는다. 운영 RDS에서의 소요 시간은 실측하지 않았다. 잠금 대기는 `lock_timeout 5s`로 끊긴다.
- 이 저장소에는 glossary를 읽는 코드가 없고 이번에 바꾼 소비 코드도 없어 API·워커 재시작은 필요 없다.
  다만 운영에서 glossary를 쓰는 다른 곳(수동 조회·다른 서비스)이 있는지는 적용 전에 확인한다.
- `plan`은 명시적 쓰기 차단 잠금을 잡지 않는다(일반 조회가 쓰는 읽기 잠금만). `load`는 적재하는 동안 다른 쓰기를 막는다(읽기는 된다).

**문제가 생겼을 때 — 먼저 검토할 것**
1. **전진 수정**: 잘못된 부분을 새 마이그레이션이나 새 전달본(version을 올린 재적재)으로 고친다. 기존 행과 PK가 유지된다.
2. **검증된 복구 절차**: A-2의 RDS 스냅샷을 **새 인스턴스로 복원**해 확인한 뒤 전환을 결정한다(기존 인스턴스를 덮어쓰지 않음). 스냅샷 이후의 다른 쓰기는 포함되지 않는다.

**downgrade의 한계**
- `alembic downgrade 0004`는 200자를 넘는 값이 있으면 **스스로 멈춘다**. 긴 원본을 자르지 않기 위한 차단이며, 이번 데이터를 적재한 뒤에는 41행 때문에 차단된다.
  이 차단을 풀려고 행을 지우는 것은 기본 복구 절차가 아니다(영구 삭제이고, `compliance_flags.glossary_id`가 PK를 참조할 수 있으며 재적재하면 PK가 바뀐다).
- downgrade가 진행되면 추가한 13개 열과 그 값이 사라진다.

## 4. 이번에 하지 않은 것 (미결)

| 항목 | 상태 |
| --- | --- |
| 문구 C 16행(쓰지 말 표현) 저장·전달 | 데이터팀 "10월 결정". 같은 테이블에 넣게 되면 조회 쪽이 `zone='A'`로 걸러야 한다 |
| 예문모음 114행 · 말투규칙 | 저장·검색 계약 없음 |
| 적재된 행이 새 파일에서 제외로 바뀔 때 처리(비활성화 등) | 지금은 로더가 멈춘다. 방식 결정 필요 |
| 원본 ID 없는 예전 행과 연결 | RDS에 예전 행이 있을 때 결정 |
| 같은 원본 ID의 한국어·카테고리 정정(오타 수정) | 로더가 막는다. 수동 정정 절차 필요 |
| 자연키 인덱스 항목 크기 한도(테스트 환경 관찰값 2,704바이트, `term_ko` 단독 한도 아님) | 로더 사전 제한 없음 — 넘으면 load에서 전체 롤백. 사전 제한·해시 인덱스 등 대응은 결정 필요 |
| `external_id` NOT NULL | 예전 행이 없음을 확인한 뒤 별도 마이그레이션 |
| 카테고리 CHECK 추가 | 이번 범위에서 제외. 허용 목록과 적용 여부 별도 확정 |
| 임베딩·pgvector·번역 프롬프트 | 범위 밖 |
| 규제사전·현지부적합 적재 | 범위 밖 (사용설명서 §9 백엔드 항목) |
| 성분 원천 데이터 이용허락(공공데이터) | 사용설명서 §6 "미확인" — PM 확인 |
