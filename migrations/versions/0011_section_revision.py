"""섹션 낙관적 잠금 버전 — section.revision.

계약: docs/openapi.yaml sectionPatch(FE 확정 11-03 — 요청 `{action, revision}`).
포함 상태(bucket·exclusion_reason·excluded_stage)가 섹션 PATCH 로 실제로 바뀔 때만 +1 한다. 판정·배지·인페인트 같은 파이프라인 값은 올리지 않는다.
기존 섹션은 0 부터(NOT NULL DEFAULT 0 — 기존 행 값은 바꾸지 않고 기본값으로 채운다).

revision 번호: 착수 때 develop head(0010) 뒤에 잇는다. 진행 중인 #103(0009, down 0008)과의 순서는 머지 직전에 다시 확인한다.

Revision ID: 0011
Revises: 0010
"""
from typing import Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE section ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE section ADD CONSTRAINT ck_section_revision CHECK (revision >= 0)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE section DROP COLUMN IF EXISTS revision")
