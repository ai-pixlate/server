"""규제사전·현지부적합 사전 적재 구조: 무결성 제약, 현지 맥락 열, 근거 원본 ID.

결정은 계약 정본 v1.3 §1-1·§4-1, CHECK_enum_허용값_v3.4.1, ERD_수정문_v3.4.2 E4·M6·M7에 있었으나
0001이 SQL_Preview(제약 미포함)로 만들어져 빠져 있던 것을 채운다.

1) expression_dictionary
   - uq_expr_dict_external_id : 항목 ID(RG-###·LC-##) 한 행 — 재적재 멱등 키
   - uq_expr_dict_content     : (dict_type, target_country, regulatory_class, source_expression) 한 행
     (source_expression 은 NULL 허용이라 NULL 끼리는 막지 못한다 — 로더가 필수로 검사)
   - ck_expr_dict_dict_type   : regulatory · local · channel
   - ck_expr_dict_verdict_status : dict_type별 허용값
       regulatory → regulated·conditional·allowed / local → irrelevant·needs_fix·cultural / channel → policy
   - exclusion_context · keep_context TEXT : 현지부적합 '제외하는 맥락'·'제외하지 않는 맥락'(사용설명서 §3)
   - source_verdict_status VARCHAR(20) : 시트에 적힌 판정 원값(예: rewritable). 서비스 판정은 verdict_status 를
     쓰고 이 열은 원본 보존용이다(데이터팀 요청, 2026-09-30). 마지막으로 적재한 원값이며 변경 이력이 아니고,
     원본에서 이미 합쳐진 분류는 복원하지 못한다. CHECK 없음(로더가 시트 값을 검사).
   regulatory_class 에는 CHECK 를 두지 않는다(국가별 확장 — 9/8 합의).
2) expression_dictionary_evidence
   - external_id VARCHAR(32) : 원본 근거 ID(WL-###·MN-###·MP-###…). 같은 근거가 여러 사전 항목에
     연결되므로 단독 UNIQUE 가 아니라 uq_expr_evidence_link (dictionary_id, external_id)
   - uq_expr_evidence_primary : 사전 항목당 대표 근거(is_primary) 최대 1건 (부분 UNIQUE 인덱스)

기존 데이터가 제약을 어기면 자동으로 고치지 않고 멈춘다(DO 블록 — offline SQL에도 포함).
downgrade 는 추가한 열을 지우므로 그 값(현지 맥락·판정 원값·근거 원본 ID)이 사라진다.

Revision ID: 0006
Revises: 0005
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels = None
depends_on = None

VERDICT_BY_TYPE = (
    "(dict_type = 'regulatory' AND verdict_status IN ('regulated', 'conditional', 'allowed'))"
    " OR (dict_type = 'local' AND verdict_status IN ('irrelevant', 'needs_fix', 'cultural'))"
    " OR (dict_type = 'channel' AND verdict_status = 'policy')"
)

PRECHECK = f"""
DO $$
DECLARE
  n integer;
BEGIN
  SELECT count(*) INTO n FROM (
    SELECT 1 FROM expression_dictionary GROUP BY external_id HAVING count(*) > 1) d;
  IF n > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0006 중단 - expression_dictionary.external_id 중복 ' || n || '개. 자동 병합하지 않음';
  END IF;

  -- UNIQUE 는 NULL 끼리 충돌하지 않으므로 source_expression 이 NULL 인 행은 검사에서 뺀다(제약과 같게)
  SELECT count(*) INTO n FROM (
    SELECT 1 FROM expression_dictionary WHERE source_expression IS NOT NULL
    GROUP BY dict_type, target_country, regulatory_class, source_expression HAVING count(*) > 1) d;
  IF n > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0006 중단 - (dict_type, target_country, regulatory_class, source_expression) 중복 '
      || n || '개. 자동 병합하지 않음';
  END IF;

  SELECT count(*) INTO n FROM expression_dictionary
  WHERE NOT (dict_type IN ('regulatory', 'local', 'channel')) OR NOT ({VERDICT_BY_TYPE});
  IF n > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0006 중단 - dict_type/verdict_status 허용값 밖의 행 ' || n || '개. 값을 추정해 바꾸지 않음';
  END IF;

  SELECT count(*) INTO n FROM (
    SELECT 1 FROM expression_dictionary_evidence WHERE is_primary
    GROUP BY dictionary_id HAVING count(*) > 1) d;
  IF n > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0006 중단 - 대표 근거가 2건 이상인 사전 항목 ' || n || '개. 대표를 정리한 뒤 다시 실행';
  END IF;
END $$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(PRECHECK)
    op.add_column("expression_dictionary", sa.Column("exclusion_context", sa.Text(), nullable=True))
    op.add_column("expression_dictionary", sa.Column("keep_context", sa.Text(), nullable=True))
    op.add_column("expression_dictionary", sa.Column("source_verdict_status", sa.String(20), nullable=True))
    op.create_unique_constraint("uq_expr_dict_external_id", "expression_dictionary", ["external_id"])
    op.create_unique_constraint(
        "uq_expr_dict_content", "expression_dictionary",
        ["dict_type", "target_country", "regulatory_class", "source_expression"],
    )
    op.create_check_constraint(
        "ck_expr_dict_dict_type", "expression_dictionary", "dict_type IN ('regulatory', 'local', 'channel')"
    )
    op.create_check_constraint("ck_expr_dict_verdict_status", "expression_dictionary", VERDICT_BY_TYPE)

    op.add_column("expression_dictionary_evidence", sa.Column("external_id", sa.String(32), nullable=True))
    op.create_unique_constraint(
        "uq_expr_evidence_link", "expression_dictionary_evidence", ["dictionary_id", "external_id"]
    )
    op.create_index(
        "uq_expr_evidence_primary", "expression_dictionary_evidence", ["dictionary_id"],
        unique=True, postgresql_where=sa.text("is_primary"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("uq_expr_evidence_primary", table_name="expression_dictionary_evidence")
    op.drop_constraint("uq_expr_evidence_link", "expression_dictionary_evidence", type_="unique")
    op.drop_column("expression_dictionary_evidence", "external_id")
    op.drop_constraint("ck_expr_dict_verdict_status", "expression_dictionary", type_="check")
    op.drop_constraint("ck_expr_dict_dict_type", "expression_dictionary", type_="check")
    op.drop_constraint("uq_expr_dict_content", "expression_dictionary", type_="unique")
    op.drop_constraint("uq_expr_dict_external_id", "expression_dictionary", type_="unique")
    op.drop_column("expression_dictionary", "source_verdict_status")
    op.drop_column("expression_dictionary", "keep_context")
    op.drop_column("expression_dictionary", "exclusion_context")
