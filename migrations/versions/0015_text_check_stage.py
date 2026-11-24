"""번역문 검사 stage 허용 — text_check · translation_review.

be-handoff.md 8.7(0008 후속 마이그레이션): 두 검사 stage 를 함께 허용하고 단계 ↔ task_type='verify' · unit_type='section'
으로 대응시킨다. 부모 실행 트리거의 downstream 허용 단계에 두 검사를 더한다. stage 허용과 실제 실행 활성화는 분리한다 —
이 마이그레이션은 행을 만들지 않고, 어떤 검사를 돌릴지는 실행의 run_scope.active_checks 로 정한다(2차는 rule 만).

0008 의 제약·트리거 정의를 그대로 불러와 두 stage 만 더한다(정의를 다시 쓰지 않는다).
downgrade 는 두 stage 의 행이 하나라도 있으면 멈춘다.

revision 번호: develop head 0014(#122 사전 v9 · 카테고리 코드) 뒤. 0013 · 0014 는 job_async_task · 트리거를 건드리지 않는다.
진행 중인 #103 은 이 뒤(0016 이후)로 번호를 옮겨야 한다.

Revision ID: 0015
Revises: 0014
"""
import importlib.util
from pathlib import Path
from typing import Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels = None
depends_on = None

CHECK_STAGES = ("text_check", "translation_review")


def _m0008():
    path = Path(__file__).with_name("0008_execution_attempts_handoff.py")
    spec = importlib.util.spec_from_file_location("_m0008_execution_attempts_handoff", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_OLD_DOWNSTREAM = "('label', 'logo', 'inpaint', 'style', 'translate', 'preview')"
_NEW_DOWNSTREAM = "('label', 'logo', 'inpaint', 'style', 'translate', 'preview', 'text_check', 'translation_review')"
_VERIFY = " OR (stage IN ('text_check', 'translation_review') AND task_type = 'verify' AND unit_type = 'section')"


def _defs(with_checks: bool) -> tuple[str, str, str]:
    m = _m0008()
    checks = dict(m.JAT_CHECKS)
    stage = checks["ck_jat_stage"]
    k1 = checks["ck_jat_k1_attempt"]
    guard = m.JAT_GUARD
    if _OLD_DOWNSTREAM not in guard or m.STAGE_TASK_TYPE not in k1:
        raise RuntimeError("0008 정의가 예상과 다르다 — 0014 를 적용하지 않는다")
    if with_checks:
        stage = f"stage IS NULL OR {m._in('stage', m.STAGES + CHECK_STAGES)}"
        k1 = k1.replace(m.STAGE_TASK_TYPE, m.STAGE_TASK_TYPE + _VERIFY)
        guard = guard.replace(_OLD_DOWNSTREAM, _NEW_DOWNSTREAM)
    return stage, k1, guard.replace("CREATE FUNCTION job_async_task_guard()", "CREATE OR REPLACE FUNCTION job_async_task_guard()")


def _apply(with_checks: bool) -> None:
    stage, k1, guard = _defs(with_checks)
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE job_async_task DROP CONSTRAINT ck_jat_stage, DROP CONSTRAINT ck_jat_k1_attempt")
    op.execute(f"ALTER TABLE job_async_task ADD CONSTRAINT ck_jat_stage CHECK ({stage})")
    op.execute(f"ALTER TABLE job_async_task ADD CONSTRAINT ck_jat_k1_attempt CHECK ({k1})")
    op.execute(guard)


DOWN_PRECHECK = """
DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM job_async_task WHERE stage IN ('text_check', 'translation_review');
  IF n > 0 THEN
    RAISE EXCEPTION '0014 downgrade 중단 - 검사 stage 행 %개. 지우지 않는다', n;
  END IF;
END $$;
"""


def upgrade() -> None:
    _apply(True)


def downgrade() -> None:
    op.execute(DOWN_PRECHECK)
    _apply(False)
