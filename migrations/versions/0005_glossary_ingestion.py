"""용어집(glossary) 적재 구조: 원본 ID·메타데이터 열, 긴 용어 수용, 중복 방지 제약.

데이터팀 용어집 3종(성분·인증·문구, 9/28 확정 저장 열)을 그대로 담기 위한 변경.
1) term_ko · term_target VARCHAR(200) → TEXT
   성분 영문명 41행이 200자를 넘는다(최대 1,886자). 원본을 자르지 않는다.
2) 원본 열 추가 — 모두 NULL 허용(기존 행은 값 없음, 백필하지 않음)
   external_id(엑셀 id, GL-I/GL-C/GL-M…) · zone(구역) · term_kind · frequency · corpus_size ·
   corpus_version · verified_at · source · note · version · load_flag(적재여부) · label(라벨) ·
   representative(대표여부, 원문 '대표'/'대안')
3) 제약
   - uq_glossary_external_id : 같은 원본 ID는 한 행 (NULL은 여러 개 허용)
   - uq_glossary_natural_key : (target_lang, internal_category, term_ko) 한 조합에 한 행
   - ck_glossary_enforcement : enforcement IN ('enforced','reference')
   기존 데이터가 제약을 어기면 자동으로 고치지 않고 멈춘다(아래 DO 블록 — offline SQL에도 포함).

downgrade 한계: 200자를 넘는 값이 있으면 VARCHAR(200)으로 되돌리지 않고 멈춘다(자르지 않음).
추가한 열을 지우므로 그 열의 값은 사라진다.

Revision ID: 0005
Revises: 0004
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels = None
depends_on = None

NEW_COLUMNS = [
    ("external_id", sa.String(32)),
    ("zone", sa.String(10)),
    ("term_kind", sa.String(20)),
    ("frequency", sa.Integer()),
    ("corpus_size", sa.Integer()),
    ("corpus_version", sa.String(20)),
    ("verified_at", sa.Date()),
    ("source", sa.Text()),
    ("note", sa.Text()),
    ("version", sa.Integer()),
    ("load_flag", sa.String(4)),
    ("label", sa.String(100)),
    ("representative", sa.String(10)),
]

# 제약을 걸기 전 기존 데이터 점검. 위반이 있으면 예외로 트랜잭션 전체가 취소된다.
PRECHECK = """
DO $$
DECLARE
  dup_keys integer;
  bad_enforcement integer;
BEGIN
  SELECT count(*) INTO dup_keys FROM (
    SELECT 1 FROM glossary
    GROUP BY target_lang, internal_category, term_ko
    HAVING count(*) > 1
  ) d;
  IF dup_keys > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0005 중단 - glossary (target_lang, internal_category, term_ko) 중복 조합 '
      || dup_keys || '개. 자동 병합하지 않으므로 정리 후 다시 실행';
  END IF;

  SELECT count(*) INTO bad_enforcement FROM glossary
  WHERE enforcement NOT IN ('enforced', 'reference');
  IF bad_enforcement > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0005 중단 - glossary.enforcement 가 enforced/reference 가 아닌 행 '
      || bad_enforcement || '개. 값을 추정해 바꾸지 않으므로 확인 후 다시 실행';
  END IF;
END $$
"""

DOWN_PRECHECK = """
DO $$
DECLARE
  long_rows integer;
BEGIN
  SELECT count(*) INTO long_rows FROM glossary
  WHERE length(term_ko) > 200 OR length(term_target) > 200;
  IF long_rows > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0005 downgrade 중단 - 200자를 넘는 term_ko/term_target 행 '
      || long_rows || '개. VARCHAR(200)으로 되돌리면 값이 잘리므로 진행하지 않음';
  END IF;
END $$
"""


def upgrade() -> None:
    # 잠금을 오래 기다리며 다른 쿼리를 막지 않게 한다(실패하면 다시 실행).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(PRECHECK)
    # VARCHAR → TEXT 는 테이블을 다시 쓰지 않는다(값 변환 없음).
    op.alter_column("glossary", "term_ko", type_=sa.Text(), existing_type=sa.String(200), existing_nullable=False)
    op.alter_column("glossary", "term_target", type_=sa.Text(), existing_type=sa.String(200), existing_nullable=False)
    for name, type_ in NEW_COLUMNS:
        op.add_column("glossary", sa.Column(name, type_, nullable=True))
    op.create_unique_constraint("uq_glossary_external_id", "glossary", ["external_id"])
    op.create_unique_constraint(
        "uq_glossary_natural_key", "glossary", ["target_lang", "internal_category", "term_ko"]
    )
    op.create_check_constraint("ck_glossary_enforcement", "glossary", "enforcement IN ('enforced','reference')")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(DOWN_PRECHECK)
    op.drop_constraint("ck_glossary_enforcement", "glossary", type_="check")
    op.drop_constraint("uq_glossary_natural_key", "glossary", type_="unique")
    op.drop_constraint("uq_glossary_external_id", "glossary", type_="unique")
    for name, _type in reversed(NEW_COLUMNS):
        op.drop_column("glossary", name)
    op.alter_column("glossary", "term_target", type_=sa.String(200), existing_type=sa.Text(), existing_nullable=False)
    op.alter_column("glossary", "term_ko", type_=sa.String(200), existing_type=sa.Text(), existing_nullable=False)
