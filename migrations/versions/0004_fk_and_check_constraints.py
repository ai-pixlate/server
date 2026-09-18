"""DB 마감: 외래키(FK) + CHECK 제약 추가.

PK는 0001에 이미 있음. 0004는 참조 무결성(FK)과 열거값 CHECK를 채운다.
스텁 파이프라인이 source_image_id=0 을 넣는 두 FK(section/deliverable → source_image)는
실제 이미지 플로우 도입 전까지 위반이므로 제외한다(추후 마이그레이션).

Revision ID: 0004
Revises: 0003
"""
from typing import Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels = None
depends_on = None

# (제약명, 자식테이블, 자식컬럼, 부모테이블, 부모컬럼, ON DELETE)
FOREIGN_KEYS = [
    ("fk_brand_seller", "brand", "seller_id", "seller", "id", "RESTRICT"),
    ("fk_brand_logo_brand", "brand_logo", "brand_id", "brand", "id", "CASCADE"),
    ("fk_job_seller", "job", "seller_id", "seller", "id", "RESTRICT"),
    ("fk_job_brand", "job", "brand_id", "brand", "id", "RESTRICT"),
    ("fk_job_channel_spec", "job", "channel_spec_id", "channel_spec", "id", "SET NULL"),
    ("fk_job_category", "job", "display_category_id", "category_master", "id", "SET NULL"),
    ("fk_job_keyword_job", "job_keyword", "job_id", "job", "id", "CASCADE"),
    ("fk_source_image_job", "source_image", "job_id", "job", "id", "CASCADE"),
    ("fk_section_job", "section", "job_id", "job", "id", "CASCADE"),
    ("fk_text_block_section", "text_block", "section_id", "section", "id", "CASCADE"),
    ("fk_section_verdict_section", "section_verdict", "section_id", "section", "id", "CASCADE"),
    ("fk_deliverable_job", "deliverable", "job_id", "job", "id", "CASCADE"),
    ("fk_deliverable_section_deliverable", "deliverable_section", "deliverable_id", "deliverable", "id", "CASCADE"),
    ("fk_deliverable_section_section", "deliverable_section", "section_id", "section", "id", "CASCADE"),
    ("fk_export_artifact_job", "export_artifact", "job_id", "job", "id", "CASCADE"),
    ("fk_job_async_task_job", "job_async_task", "job_id", "job", "id", "CASCADE"),
    ("fk_edit_signal_job", "edit_signal", "job_id", "job", "id", "CASCADE"),
    ("fk_category_master_parent", "category_master", "parent_id", "category_master", "id", "SET NULL"),
    ("fk_category_mapping_category", "category_mapping", "category_id", "category_master", "id", "CASCADE"),
    ("fk_module_spec_channel", "module_spec", "channel_spec_id", "channel_spec", "id", "CASCADE"),
    ("fk_reg_class_country", "master_regulatory_class", "country_code", "master_country", "code", "RESTRICT"),
    ("fk_expr_dict_evidence", "expression_dictionary_evidence", "dictionary_id", "expression_dictionary", "id", "CASCADE"),
]

# (제약명, 테이블, 조건) — 코드가 쓰는 값 도메인만(위반 없음). NULL은 항상 허용.
CHECK_CONSTRAINTS = [
    ("ck_section_bucket", "section", "bucket IN ('include','exclude')"),
    ("ck_section_inpaint_status", "section", "inpaint_status IN ('pending','running','done','failed')"),
    ("ck_deliverable_render_status", "deliverable", "render_status IN ('pending','running','done','failed')"),
]


def upgrade() -> None:
    for name, table, col, ref_t, ref_c, on_delete in FOREIGN_KEYS:
        op.create_foreign_key(name, table, ref_t, [col], [ref_c], ondelete=on_delete)
    for name, table, cond in CHECK_CONSTRAINTS:
        op.create_check_constraint(name, table, cond)


def downgrade() -> None:
    for name, table, _cond in CHECK_CONSTRAINTS:
        op.drop_constraint(name, table, type_="check")
    for name, table, *_ in FOREIGN_KEYS:
        op.drop_constraint(name, table, type_="foreignkey")
