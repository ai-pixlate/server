"""용어집 행 활성 제외 표시 — glossary.retired_at.

교체로 빠진 행을 지우지 않고 공급에서만 뺀다. PK 를 참조하는 기록을 위해 행은 남긴다.
기존 행 값은 바꾸지 않는다. external_id · 자연키 UNIQUE 는 그대로 둔다.
downgrade 는 빠진 행이 있으면 멈춘다(열을 지우면 빠진 행이 다시 공급된다).

revision 번호: 착수 때 이 브랜치 head(0011) 뒤에 잇는다. 진행 중인 #103 · 조판 작업의 0012 와 번호가 겹치므로
나중에 머지하는 쪽이 머지 직전 최신 head 뒤로 옮긴다.

Revision ID: 0012
Revises: 0011
"""
from typing import Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels = None
depends_on = None


DOWN_PRECHECK = """
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM glossary WHERE retired_at IS NOT NULL;
  IF n > 0 THEN
    RAISE EXCEPTION '0012 downgrade 중단 - 활성에서 제외된 용어집 행 %개. 열을 지우면 다시 공급되므로 진행하지 않음', n;
  END IF;
END $$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE glossary ADD COLUMN retired_at TIMESTAMPTZ NULL")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(DOWN_PRECHECK)
    op.execute("ALTER TABLE glossary DROP COLUMN IF EXISTS retired_at")
