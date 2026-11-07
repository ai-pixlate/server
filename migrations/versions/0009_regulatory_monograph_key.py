"""규제사전 v12: OTC 종류 열쇠(monograph_key), OTC 종류 마스터, 교체 때 기존 행 보존(retired_at).

근거: 규제사전_구축과_작동흐름_2026-10-30 §1-3 · §3-0 · §3-3(10-21 · 10-26 PM, PRD v3.5.10 02 F-TRN-07),
10-29 백엔드 DA-3(OTC 종류 값은 코드가 아니라 마스터 테이블), 10-27 결정(v12 26행으로 전체 교체).

1) master_otc_monograph — 국가별 OTC 종류 선택지(N1 ③). (country_code, monograph_key) 한 행.
   미국 4개를 넣는다: M020 선크림 · M006 여드름 · M032 비듬·지루성 피부염·건선 · M016 피부보호제.
   master_country 는 마이그레이션이 채우지 않으므로 외래키를 걸지 않는다.
2) expression_dictionary.monograph_key VARCHAR(16) NULL
   - fk_expr_dict_monograph : (target_country, monograph_key) → master_otc_monograph. NULL 이면 검사하지 않는다.
   - ck_expr_dict_monograph_key (NOT VALID) : 규제 행은 common 이면 비우고 cosmetic · otc 면 채운다.
     현재 적재본(9/30 정정본)의 cosmetic · otc 행에는 열쇠가 없다. NOT VALID 는 이 마이그레이션 시점의
     기존 행 일괄 검사만 미룬다 — 새 INSERT 와 기존 행의 모든 UPDATE 에는 적용된다.
     · 교체가 기존 행에 retired_at 을 찍는 UPDATE 는 retired_at IS NOT NULL 조건으로 통과한다.
     · 같은 ID · 분류로 남는 기존 행의 UPDATE 는 파일의 열쇠를 함께 쓰므로 통과한다.
     · 열쇠 없이 기존 cosmetic · otc 행만 고치는 다른 UPDATE(수동 SQL 등)는 거부된다.
     실제 데이터의 교체 경로는 대상 DB(또는 그 스냅샷 복원본)에서 plan --replace 로 먼저 확인한다.
     교체(dictionary_ingest load --replace) 뒤에 VALIDATE CONSTRAINT 로 전체를 검사할 수 있다.
3) expression_dictionary.retired_at TIMESTAMPTZ NULL — 교체로 빠진 행. 지우지 않고 남긴다.
   section_verdict.dictionary_id(외래키 없음) · 근거 연결이 과거 판정의 사전 행을 계속 가리키게 하기 위해서다.
   - uq_expr_dict_external_id · uq_expr_dict_content 를 retired_at IS NULL 인 행만 대상으로 하는
     부분 UNIQUE 인덱스로 바꾼다. 같은 항목 ID 의 새 행(예: 분류가 바뀐 RG-008)을 넣을 수 있다.
   - 사전을 읽는 쪽(dictionary_bundle · dictionary_ingest)은 retired_at IS NULL 인 행만 쓴다.

downgrade 는 빠진 행이 있으면 멈춘다(항목 ID 단일 UNIQUE 를 되살릴 수 없음). 열쇠 값은 사라진다.

Revision ID: 0009
Revises: 0008
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None

# (국가, 열쇠, 한국어 표시 이름, 영문 짧은 이름, 표시 순서) — 작동흐름 §3-0 표, 10/27 PM 한국어·영문 병기
OTC_MONOGRAPHS = [
    ("US", "M020", "선크림", "Sunscreen", 1),
    ("US", "M006", "여드름", "Acne", 2),
    ("US", "M032", "비듬·지루성 피부염·건선", "Dandruff · Seborrheic Dermatitis · Psoriasis", 3),
    ("US", "M016", "피부보호제", "Skin Protectant", 4),
]

KEY_RULE = (
    "dict_type <> 'regulatory' OR retired_at IS NOT NULL"
    " OR (regulatory_class = 'common' AND monograph_key IS NULL)"
    " OR (regulatory_class IN ('cosmetic', 'otc') AND monograph_key IS NOT NULL)"
)

DOWN_PRECHECK = """
DO $$
DECLARE
  n integer;
BEGIN
  SELECT count(*) INTO n FROM expression_dictionary WHERE retired_at IS NOT NULL;
  IF n > 0 THEN
    RAISE EXCEPTION USING MESSAGE =
      '0009 downgrade 중단 - 교체로 빠진 사전 행 ' || n || '개. 항목 ID 단일 UNIQUE 를 되살릴 수 없음';
  END IF;
END $$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    table = op.create_table(
        "master_otc_monograph",
        sa.Column("country_code", sa.String(8), nullable=False),
        sa.Column("monograph_key", sa.String(16), nullable=False),
        sa.Column("name_ko", sa.String(100), nullable=False),
        sa.Column("name_en_short", sa.String(200), nullable=False),
        sa.Column("display_order", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("country_code", "monograph_key", name="pk_master_otc_monograph"),
    )
    op.bulk_insert(table, [
        {"country_code": c, "monograph_key": k, "name_ko": ko, "name_en_short": en, "display_order": o}
        for c, k, ko, en, o in OTC_MONOGRAPHS
    ])

    op.add_column("expression_dictionary", sa.Column("monograph_key", sa.String(16), nullable=True))
    op.add_column("expression_dictionary", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_expr_dict_monograph", "expression_dictionary", "master_otc_monograph",
        ["target_country", "monograph_key"], ["country_code", "monograph_key"], ondelete="RESTRICT",
    )
    op.execute(f"ALTER TABLE expression_dictionary ADD CONSTRAINT ck_expr_dict_monograph_key "
               f"CHECK ({KEY_RULE}) NOT VALID")

    op.drop_constraint("uq_expr_dict_external_id", "expression_dictionary", type_="unique")
    op.drop_constraint("uq_expr_dict_content", "expression_dictionary", type_="unique")
    op.create_index("uq_expr_dict_external_id", "expression_dictionary", ["external_id"],
                    unique=True, postgresql_where=sa.text("retired_at IS NULL"))
    op.create_index("uq_expr_dict_content", "expression_dictionary",
                    ["dict_type", "target_country", "regulatory_class", "source_expression"],
                    unique=True, postgresql_where=sa.text("retired_at IS NULL"))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(DOWN_PRECHECK)
    op.drop_index("uq_expr_dict_content", table_name="expression_dictionary")
    op.drop_index("uq_expr_dict_external_id", table_name="expression_dictionary")
    op.create_unique_constraint("uq_expr_dict_external_id", "expression_dictionary", ["external_id"])
    op.create_unique_constraint(
        "uq_expr_dict_content", "expression_dictionary",
        ["dict_type", "target_country", "regulatory_class", "source_expression"],
    )
    op.drop_constraint("ck_expr_dict_monograph_key", "expression_dictionary", type_="check")
    op.drop_constraint("fk_expr_dict_monograph", "expression_dictionary", type_="foreignkey")
    op.drop_column("expression_dictionary", "retired_at")
    op.drop_column("expression_dictionary", "monograph_key")
    op.drop_table("master_otc_monograph")
