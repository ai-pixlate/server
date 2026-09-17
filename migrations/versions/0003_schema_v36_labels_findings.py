"""schema v3.6: text_block label flags + role CHECK + section content_findings

모델팀 개발계획 v3.6 반영:
1) text_block.is_product_label BOOLEAN (NULL 미판정 / false 해당없음 / true 해당)
2) text_block.is_brand_logo   BOOLEAN (동일)
   - is_excluded 를 GENERATED 로 재정의: (is_product_label IS TRUE) OR (is_brand_logo IS TRUE)
   - role 을 5종(title/body/caption/price/caution)으로 CHECK 강제 (product_label 제거)
3) section.content_findings JSONB (AI 콘텐츠 원시 판정 저장. section_verdict=정책 결과와 역할 분리)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UP = [
    # 1·2) 라벨/로고 플래그 추가
    "ALTER TABLE text_block ADD COLUMN is_product_label BOOLEAN",
    "ALTER TABLE text_block ADD COLUMN is_brand_logo BOOLEAN",
    # 기존 role='product_label' 데이터 이관(플래그 true) + role 은 재판정 전 임시로 body
    # (현재 text_block 비어 있어 대상 0건이지만 방어적으로 수행)
    "UPDATE text_block SET is_product_label = true, role = 'body' WHERE role = 'product_label'",
    # role 5종 CHECK 강제 (product_label 제외)
    "ALTER TABLE text_block ADD CONSTRAINT text_block_role_check "
    "CHECK (role IN ('title','body','caption','price','caution'))",
    # is_excluded 를 GENERATED(자동계산)로 재정의 (기존 평범한 컬럼 제거 후 재생성)
    "ALTER TABLE text_block DROP COLUMN is_excluded",
    "ALTER TABLE text_block ADD COLUMN is_excluded BOOLEAN "
    "GENERATED ALWAYS AS ((is_product_label IS TRUE) OR (is_brand_logo IS TRUE)) STORED",
    # 3) section 콘텐츠 원시 판정 저장
    "ALTER TABLE section ADD COLUMN content_findings JSONB",
]

DOWN = [
    "ALTER TABLE section DROP COLUMN content_findings",
    "ALTER TABLE text_block DROP COLUMN is_excluded",
    "ALTER TABLE text_block ADD COLUMN is_excluded BOOLEAN",
    "ALTER TABLE text_block DROP CONSTRAINT text_block_role_check",
    "ALTER TABLE text_block DROP COLUMN is_brand_logo",
    "ALTER TABLE text_block DROP COLUMN is_product_label",
]


def upgrade() -> None:
    for stmt in UP:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWN:
        op.execute(stmt)
