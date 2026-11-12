"""판매자 계정 식별 — 제공자 + 제공자 회원번호(PRD v3.5.10 F-ACC-05).

소셜 로그인(카카오·Google)을 받을 저장 구조만 만든다. 소셜 계정을 만드는 API 는 이 마이그레이션 범위가 아니다.

| 열 | 뜻 |
|---|---|
| auth_provider | 계정에 들어오는 경로. `local`(이메일 간이 로그인) · `kakao` · `google` |
| provider_subject | 제공자 회원번호(카카오 `id` · Google `sub`, 문자열). local 은 NULL |
| display_name | 제공자가 준 이름(카카오 닉네임 · Google 이름). NULL 허용 — 제공자가 주지 않으면 비워 두고 서버가 이름을 만들지 않는다 |

- 같은 제공자의 같은 회원번호는 한 행이다(부분 유일 인덱스). 같은 이메일이어도 제공자가 다르면 다른 행이다 — email 에는 유일 제약을 두지 않는다.
- email · cognito_sub 의 NOT NULL 을 푼다. 소셜 행의 cognito_sub 는 비워 둔다(Cognito 가 발급한 값처럼 보이지 않게).
  소셜 행의 email 은 수집하되(PM 11-09 — 계정 확인·서비스 안내 목적) 계정 식별·연결에는 쓰지 않는다. 수집 정책과 별개로 NULL 을 허용한다 —
  제공자에게서 확인된 이메일을 받지 못하면 저장하지 않는다. local 행은 CHECK 로 email · cognito_sub 를 계속 요구한다.
- display_name 은 PM 11-09 저장 정보(닉네임/이름)를 담는다. 빈 문자열·공백만 있는 값은 CHECK 로 막는다(없으면 NULL). 화면 응답(`Seller`)에는 아직 싣지 않는다.
- 이 마이그레이션은 저장 구조와 제약만 정한다. 제공자 응답 중 무엇을 「확인된 값」으로 볼지, 재로그인 때 이메일·이름을 언제 바꾸거나 지울지
  (명시적 철회·무효 판정과 일시적 조회 실패·필드 누락의 구분 포함)는 소셜 로그인(C) 범위다.

기존 행: 모두 `local` 이 된다(열 기본값, 다른 열 값은 바꾸지 않음).
- 코드 이력 근거(확인함): 이 저장소의 판매자 생성 경로는 간이 로그인(`cognito_sub = 'local:<email>'`), 시험 픽스처, 기준선 도구(`baseline:<case>`)뿐이고,
  9월 고정 판매자 시절의 수동 행이 있을 수 있다. 어느 경로도 외부 인증 제공자를 거치지 않았고, 모두 지금 이메일 간이 로그인으로만 들어올 수 있다.
  `local` 은 그 접근 경로를 그대로 적은 것이다.
- 실제 대상 DB 근거(확인 안 함): DB 적용 전에 `cognito_sub` 형식 분포·email 중복을 읽기 전용으로 확인한다(적용 승인 절차). 예상 밖 형식이 나오면 적용하지 않고 판별안을 다시 정한다.

downgrade: 소셜 행이 하나라도 있으면 아무것도 바꾸기 전에 멈춘다(회원번호 매핑이 사라지고, 비어 있는 email 때문에 NOT NULL 을 되돌릴 수 없다).
local 행의 display_name 은 지금 채우는 경로가 없다(있으면 downgrade 때 함께 사라진다).
소셜 행이 없으면 열·제약·인덱스를 지우고 NOT NULL 을 되돌린다 — local 행은 CHECK 로 두 값이 있으므로 실패하지 않는다.

배포: 이전 서버 코드와 함께 쓸 수 있다(이전 코드는 새 열을 모르고, 기존 행 값은 그대로). 소셜 행이 생긴 뒤에는
이메일만으로 판매자를 찾는 이전 `/auth/login` 코드로 되돌리지 않는다.

revision 연결: 처음에는 2026-11-10 develop head 0014 뒤의 0015 였다. 2026-11-11 develop 에 0015_text_check_stage(#127 번역문 규칙 검사 —
revision 0015, down 0014)가 먼저 머지돼 ID 가 겹쳤으므로 이 마이그레이션을 0016(down 0015)으로 옮겼다. 계정 식별 동작은 바꾸지 않았다.
0015_text_check_stage 는 job_async_task 제약·트리거만, 이 마이그레이션은 seller 만 바꾼다(서로 겹치지 않음). 실제 서버 DB 에는 적용한 적 없다.
이전 번호(0015)로 이 마이그레이션을 적용해 둔 로컬 시험 DB 는 새 번호 체계와 맞지 않는다 — stamp 로 맞추지 말고 새 DB 로 시험한다.
이 PR 도 머지 직전에 그 시점의 develop head 와 열린 PR 의 마이그레이션을 다시 확인하고, 필요하면 down_revision 을 다시 잇는다.

Revision ID: 0016
Revises: 0015
"""
from typing import Union

from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels = None
depends_on = None


DOWN_PRECHECK = """
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM seller WHERE auth_provider <> 'local';
  IF n > 0 THEN
    RAISE EXCEPTION '0016 downgrade 중단 - 소셜 계정 %개. 회원번호 매핑이 사라지고 email NOT NULL 을 되돌릴 수 없어 진행하지 않음', n;
  END IF;
END $$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE seller ADD COLUMN auth_provider VARCHAR(20) NOT NULL DEFAULT 'local'")
    op.execute("ALTER TABLE seller ADD COLUMN provider_subject VARCHAR(255) NULL")
    op.execute("ALTER TABLE seller ADD COLUMN display_name VARCHAR(255) NULL")
    op.execute("ALTER TABLE seller ALTER COLUMN email DROP NOT NULL")
    op.execute("ALTER TABLE seller ALTER COLUMN cognito_sub DROP NOT NULL")
    op.execute(
        "ALTER TABLE seller ADD CONSTRAINT ck_seller_auth_provider "
        "CHECK (auth_provider IN ('local', 'kakao', 'google'))"
    )
    op.execute(
        "ALTER TABLE seller ADD CONSTRAINT ck_seller_auth_identity CHECK ("
        "(auth_provider = 'local' AND provider_subject IS NULL AND cognito_sub IS NOT NULL AND email IS NOT NULL) "
        "OR (auth_provider IN ('kakao', 'google') AND provider_subject IS NOT NULL AND btrim(provider_subject) <> '' "
        "AND cognito_sub IS NULL))"
    )
    op.execute(
        "ALTER TABLE seller ADD CONSTRAINT ck_seller_display_name "
        "CHECK (display_name IS NULL OR btrim(display_name) <> '')"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_seller_provider_subject ON seller (auth_provider, provider_subject) "
        "WHERE provider_subject IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(DOWN_PRECHECK)
    op.execute("DROP INDEX IF EXISTS uq_seller_provider_subject")
    op.execute("ALTER TABLE seller DROP CONSTRAINT IF EXISTS ck_seller_display_name")
    op.execute("ALTER TABLE seller DROP CONSTRAINT IF EXISTS ck_seller_auth_identity")
    op.execute("ALTER TABLE seller DROP CONSTRAINT IF EXISTS ck_seller_auth_provider")
    op.execute("ALTER TABLE seller ALTER COLUMN cognito_sub SET NOT NULL")
    op.execute("ALTER TABLE seller ALTER COLUMN email SET NOT NULL")
    op.execute("ALTER TABLE seller DROP COLUMN IF EXISTS display_name")
    op.execute("ALTER TABLE seller DROP COLUMN IF EXISTS provider_subject")
    op.execute("ALTER TABLE seller DROP COLUMN IF EXISTS auth_provider")
