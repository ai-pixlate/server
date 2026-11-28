"""작업 상태 10종 · 24시간 자동 취소 · 처리 시간 상한(PRD v3.6.4 · 실행계획 G17 15 · 24 · 25번 · 10번 상태 구분 — docs/README.md 7.8).

| 열 | 뜻 |
|---|---|
| state_entered_at | 지금 상태(status · current_step)에 들어간 시각 — 처리 시간 상한(30분) 기산점. 상태 · 단계가 바뀔 때만 새로 한다 |
| last_activity_at | 마지막 움직임 — 24시간 자동 취소 기산점. 상태 전이와 그 작업을 바꾸는 요청(2xx POST · PATCH)마다 새로 한다 |

기존 행 값 바꾸기(PRD 05 상태표에 맞춤)
- status · user_facing_status 의 `archived` → `cancelled`(G17 15번 — 이름만 바꾼다. 다른 열은 그대로).
- 확정 뒤 N6 작업(`review · N6 · reviewing`)을 최종 렌더 실행 상태로 나눈다(최신 final_render 실행 · 현재 시도 기준):
  렌더 진행 중(실행 running · 현재 시도 pending/running) → `processing · rendering` / 렌더 성공(현재 시도 done) → `review · awaiting_save` /
  렌더 실패(실행 failed 이거나 현재 시도 failed) → `failed · N6 · failed`. 최종 렌더 실행이 없는 옛 N6 행은 그대로 둔다.
- 두 시각 열은 마이그레이션 시각(now())으로 채운다 — 적용 직후 오래된 작업이 한꺼번에 자동 취소(콘텐츠 삭제)되거나 처리 중 작업이
  즉시 오류가 되지 않게, 기존 작업은 적용 시점부터 센다(24시간 · 30분 유예).

downgrade: 새 값을 이전 값으로 되돌린다 — `cancelled` → `archived`, `rendering` · `awaiting_save` · N6 오류 → `review · N6 · reviewing`
(이전 서버에는 N6 오류 상태가 없다 — 렌더가 실패한 작업은 이전처럼 N6 에 머문다). 시간 초과로 생긴 N2 · N4 오류는 이전 서버에도 있는 값이라 둔다.
자동 취소로 지운 콘텐츠는 되돌릴 수 없다(취소와 같은 삭제).

배포: 이전 서버 코드와 함께 쓰면 안 된다 — 이전 코드는 `cancelled` 를 취소로 보지 않고(`archived` 만 검사) N6 의 processing · failed 를
모른다. DB 에 직접 붙어 app 코드를 실행하는 프로세스를 모두 같은 판으로 함께 올린다 — API · CPU 워커 · 복구 프로세스와 원격 GPU 호스트의
OCR · GPU 워커(코드 기본 GPU 제어 경로는 같은 프로세스 호출이다. 로컬 혼합 판 검증: 이전 GPU 코드가 취소 전에 얻은 권한으로 취소 뒤
결과를 등록하면 rejected 인계 · 산출물 메타데이터 행이 남는다. 취소 뒤 새 권한 획득은 막힌다. 실제 채택 · S3 잔존은 미검증 —
이전 GPU 코드가 안전하다는 앞선 설명은 철회). 순서 · 혼합 판 검증은 배포 계획 문서(작업 계획 쪽)를 따른다.

Revision ID: 0017
Revises: 0016
"""
from typing import Union

from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels = None
depends_on = None

# 확정 뒤 N6 작업의 최신 최종 렌더 실행 · 현재 시도
_N6 = """
WITH r AS (
  SELECT DISTINCT ON (t.job_id) t.job_id, t.id AS run_id, t.status AS run_status
  FROM job_async_task t
  WHERE t.parent_task_id IS NULL AND t.execution_schema_version = 1 AND t.run_kind = 'final_render'
  ORDER BY t.job_id, t.id DESC
), c AS (
  SELECT r.job_id, r.run_status, a.status AS att_status
  FROM r LEFT JOIN job_async_task a ON a.parent_task_id = r.run_id AND a.is_current
)
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE job ADD COLUMN state_entered_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE job ADD COLUMN last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("UPDATE job SET status = 'cancelled' WHERE status = 'archived'")
    op.execute("UPDATE job SET user_facing_status = 'cancelled' WHERE user_facing_status = 'archived'")
    op.execute(_N6 + "UPDATE job j SET status = 'processing', user_facing_status = 'rendering' FROM c "
               "WHERE c.job_id = j.id AND j.status = 'review' AND j.current_step = 'N6' "
               "AND c.run_status = 'running' AND c.att_status IN ('pending', 'running')")
    op.execute(_N6 + "UPDATE job j SET user_facing_status = 'awaiting_save' FROM c "
               "WHERE c.job_id = j.id AND j.status = 'review' AND j.current_step = 'N6' AND c.att_status = 'done'")
    op.execute(_N6 + "UPDATE job j SET status = 'failed', user_facing_status = 'failed' FROM c "
               "WHERE c.job_id = j.id AND j.status = 'review' AND j.current_step = 'N6' "
               "AND (c.run_status = 'failed' OR c.att_status = 'failed')")
    # 주기 점검(app.recovery)이 읽는 범위
    op.execute("CREATE INDEX ix_job_processing_entered ON job (state_entered_at) WHERE status = 'processing'")
    op.execute("CREATE INDEX ix_job_last_activity ON job (last_activity_at) WHERE status IN ('draft', 'review', 'failed')")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_job_last_activity")
    op.execute("DROP INDEX IF EXISTS ix_job_processing_entered")
    op.execute("UPDATE job SET status = 'review', user_facing_status = 'reviewing' "
               "WHERE current_step = 'N6' AND (status IN ('processing', 'failed') OR user_facing_status = 'awaiting_save')")
    op.execute("UPDATE job SET status = 'archived' WHERE status = 'cancelled'")
    op.execute("UPDATE job SET user_facing_status = 'archived' WHERE user_facing_status = 'cancelled'")
    op.execute("ALTER TABLE job DROP COLUMN last_activity_at")
    op.execute("ALTER TABLE job DROP COLUMN state_entered_at")
