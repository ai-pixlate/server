"""편집 저장(블록·섹션 PATCH) 멱등 기록 — edit_idempotency.

계약: docs/openapi.yaml blockPatch 「저장 공통」(FE 확정 11-03·11-04).
- 키 범위 = (판매자, 작업, 오퍼레이션, 키). 같은 요청 판별 = request_sha256(오퍼레이션·대상 ID·정규화 본문).
- 2xx 응답만 기록(no-op 200 포함), 값 변경과 같은 트랜잭션에서 쓴다. 보관 24시간(expires_at) — 지난 기록은 없는 것으로 본다.
- 작업을 취소하면 그 작업의 기록을 지운다(응답에 번역문이 들어 있음).
새 테이블만 만든다(기존 행 변경 없음).

revision 번호: 진행 중인 PR #103 이 0009(down 0008)를 쓰므로 같은 번호를 피해 0010 으로 둔다.
down_revision 은 이 PR 을 올릴 때의 develop head(0008)다. #103 이 먼저 머지되면 down_revision 을 "0009" 로 고친다.

Revision ID: 0010
Revises: 0008
"""
from typing import Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE TABLE edit_idempotency (
          seller_id      BIGINT       NOT NULL,
          job_id         BIGINT       NOT NULL REFERENCES job(id) ON DELETE CASCADE,
          operation      VARCHAR(40)  NOT NULL,
          idem_key       VARCHAR(128) NOT NULL,
          request_sha256 CHAR(64)     NOT NULL,
          status_code    SMALLINT     NOT NULL,
          response       JSONB        NOT NULL,
          created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
          expires_at     TIMESTAMPTZ  NOT NULL,
          CONSTRAINT pk_edit_idempotency PRIMARY KEY (seller_id, job_id, operation, idem_key),
          CONSTRAINT ck_edit_idem_status CHECK (status_code BETWEEN 200 AND 299),
          CONSTRAINT ck_edit_idem_sha CHECK (request_sha256 ~ '^[0-9a-f]{64}$')
        )
        """
    )
    op.execute("CREATE INDEX ix_edit_idempotency_expires ON edit_idempotency (expires_at)")
    op.execute("CREATE INDEX ix_edit_idempotency_job ON edit_idempotency (job_id)")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS edit_idempotency")
