"""text_block.is_excluded 생성식을 3값 OR로 바로잡는다.

0003은 `(is_product_label IS TRUE) OR (is_brand_logo IS TRUE)`로 만들어 미판정(NULL)을 false로 바꿨다.
그러면 ④⑤가 끝나지 않은 블록도 "하류 대상(false)"으로 보여 하류 작업이 잘못 만들어질 수 있다.
통합 합의(docs/ai/contract.md 2.4, integration-decisions.md D1, open-questions.md #63)의 계산은
`is_product_label OR is_brand_logo`의 SQL 3값 논리다.

| 라벨 | 로고 | is_excluded |
|---|---|---|
| 하나라도 true | | true |
| false | false | false |
| 그 밖(NULL 포함) | | NULL |

생성 컬럼은 식만 바꿀 수 없어 지우고 다시 만든다(값은 두 플래그에서 다시 계산되므로 유실 없음).
downgrade는 0003 식으로 되돌린다.

Revision ID: 0007
Revises: 0006
"""
from typing import Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE text_block DROP COLUMN is_excluded")
    op.execute(
        "ALTER TABLE text_block ADD COLUMN is_excluded BOOLEAN "
        "GENERATED ALWAYS AS (is_product_label OR is_brand_logo) STORED"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE text_block DROP COLUMN is_excluded")
    op.execute(
        "ALTER TABLE text_block ADD COLUMN is_excluded BOOLEAN "
        "GENERATED ALWAYS AS ((is_product_label IS TRUE) OR (is_brand_logo IS TRUE)) STORED"
    )
