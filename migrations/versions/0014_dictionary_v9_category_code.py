"""현지부적합 v9 · 카테고리 v3 적재 지원 — 판정 CHECK 확장 · 판매 사이트 키 · 판단 근거 · 카테고리 표시 코드.

- ck_expr_dict_verdict_status: dict_type=local 허용값에 v9 판정값 must_fix · needs_check 를 더한다. 옛 값
  (irrelevant · needs_fix · cultural)은 3차 「현지 판정 이름 · 표시」 작업까지 남긴다(코드 · 시험이 아직 쓴다).
  값을 옛 enum 으로 바꿔 저장하지 않는다.
- expression_dictionary.site_key VARCHAR(40) NULL: 전달 파일의 판매 사이트 키 원값 보존 자리. 열 이름 · 값(예 amazon)은
  전달 파일 그대로이며 판매 사이트 마스터 계약 · 조회 규칙을 확정한 것이 아니다(DA-5, 3차).
- expression_dictionary.judgment_basis TEXT NULL: v9 「판단 근거」 원문.
- category_master.code VARCHAR(20) NULL + 부분 UNIQUE: 표시 카테고리 코드(SK-01 등). 코드는 고정 · 재사용 금지(카테고리 v3
  README 5절)라 재적재 대조의 키로 쓴다. FE 는 쓰지 않는다(DB id 사용). 리프에만 값이 있고 상위 노드는 NULL.
- 이 마이그레이션은 행을 바꾸지 않는다. 판정 · 분석 묶음 · API enum 은 바꾸지 않는다(3차).

downgrade 는 새 값 · 새 열에 값이 있으면 멈춘다(지우면 저장값을 잃는다).

revision 번호: develop head 0013 바로 뒤(2026-11-09 develop ffee548 기준). 이보다 먼저 다른 마이그레이션이
병합되면 병합 직전 실제 head 뒤로 번호와 down_revision 을 옮긴다.

Revision ID: 0014
Revises: 0013
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels = None
depends_on = None

VERDICT_V9 = (
    "(dict_type = 'regulatory' AND verdict_status IN ('regulated', 'conditional', 'allowed'))"
    " OR (dict_type = 'local' AND verdict_status IN ('irrelevant', 'needs_fix', 'cultural', 'must_fix', 'needs_check'))"
    " OR (dict_type = 'channel' AND verdict_status = 'policy')"
)
VERDICT_0006 = (
    "(dict_type = 'regulatory' AND verdict_status IN ('regulated', 'conditional', 'allowed'))"
    " OR (dict_type = 'local' AND verdict_status IN ('irrelevant', 'needs_fix', 'cultural'))"
    " OR (dict_type = 'channel' AND verdict_status = 'policy')"
)

DOWN_PRECHECK = """
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM expression_dictionary
   WHERE (dict_type = 'local' AND verdict_status IN ('must_fix', 'needs_check'))
      OR site_key IS NOT NULL OR judgment_basis IS NOT NULL;
  IF n > 0 THEN
    RAISE EXCEPTION '0014 downgrade 중단 - v9 판정값 · site_key · judgment_basis 를 쓰는 사전 행 %개. 지우지 않음', n;
  END IF;
  SELECT count(*) INTO n FROM category_master WHERE code IS NOT NULL;
  IF n > 0 THEN
    RAISE EXCEPTION '0014 downgrade 중단 - 표시 코드가 있는 카테고리 %개. 지우지 않음', n;
  END IF;
END $$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("expression_dictionary", sa.Column("site_key", sa.String(40), nullable=True))
    op.add_column("expression_dictionary", sa.Column("judgment_basis", sa.Text(), nullable=True))
    op.drop_constraint("ck_expr_dict_verdict_status", "expression_dictionary", type_="check")
    op.create_check_constraint("ck_expr_dict_verdict_status", "expression_dictionary", VERDICT_V9)
    op.add_column("category_master", sa.Column("code", sa.String(20), nullable=True))
    op.create_index("uq_category_master_code", "category_master", ["code"], unique=True,
                    postgresql_where=sa.text("code IS NOT NULL"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(DOWN_PRECHECK)
    op.drop_index("uq_category_master_code", table_name="category_master")
    op.drop_column("category_master", "code")
    op.drop_constraint("ck_expr_dict_verdict_status", "expression_dictionary", type_="check")
    op.create_check_constraint("ck_expr_dict_verdict_status", "expression_dictionary", VERDICT_0006)
    op.drop_column("expression_dictionary", "judgment_basis")
    op.drop_column("expression_dictionary", "site_key")
