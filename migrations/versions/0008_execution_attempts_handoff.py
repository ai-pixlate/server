"""실행·시도·인계 기반: 대표 실행 / 시도 행, 실행 권한(lease), 발급 기록, 인계 목록, 산출물.

근거: docs/ai/integration-decisions.md 5.28~5.32(BE·AI 합의 확정)과 D2·D6. 값 이름은 사용자 승인(2026-10-08)한
내부 값 세트를 쓴다(API에 노출하지 않는다). 운영 수치(TTL·주기·보존 기간)는 여기서 정하지 않는다.

1) job_async_task 확장 — 기존 행 하나 = "시도". 대표 실행은 parent_task_id IS NULL 인 행.
   - execution_schema_version: 기존 행 0(레거시), 새 INSERT 는 1만(트리거). 실행 중 변경 금지.
   - 대표: run_kind · run_scope / 시도: stage · attempt_no · is_current · supersedes_task_id · retry_origin
   - 실행 권한: lease_owner · lease_epoch · lease_token_hash · lease_expires_at · heartbeat_at
   - 전달: dispatch_count · last_dispatched_at(retry_count 와 별개)
   - 고정 입력: input_manifest · input_fingerprint, 정상 생략: skip_reason · target_count, 중단: cancelled_at
   - 제약 U1~U3 · K1 · K2 · K4, 단계↔task_type 대응(사용자 결정: ④⑤⑦ → section), 참조 검사 트리거
2) task_lease_grant(U7 · K6) — 원격 워커 토큰 발급 사실(해시만). 발급 정보 불변, 해시는 승인된 정리로만 NULL.
3) task_handoff(U4 · U5 · K3) — 인계 목록과 수신·검증·채택 기록.
4) task_artifact(U6 · K3 · K5) — 파일 산출물. 측정값은 BE 가 직접 잰 값.
5) job.current_analysis_task_id — 현재 채택한 초기 분석 실행(D6).
   section.analysis_task_id — 섹션을 만든 분석 실행. 새 분석은 완료·채택 전까지 화면에 노출하지 않는다(D3).
   source_image.sha256 — 업로드 시 BE 가 잰 내용 해시(5.28 입력 고정). 기존 행은 NULL(분석 시작 때 측정).

기존 데이터가 새 제약을 어기면 자동으로 고치지 않고 멈춘다.

Revision ID: 0008
Revises: 0007
"""
from typing import Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels = None
depends_on = None

RUN_KINDS = ("analysis", "downstream", "final_render")
STAGES = ("analyze", "judge", "label", "logo", "inpaint", "style", "translate", "preview", "final_render")
RETRY_ORIGINS = ("auto", "user", "recovery")
SKIP_REASONS = ("no_targets",)
TASK_STATUSES = ("pending", "running", "done", "failed", "cancelled")
TASK_TYPES = ("ocr", "section", "inpaint", "translate", "verify", "render")
HANDOFF_STATES = ("received", "verifying", "verified", "adopted", "rejected")
HANDOFF_OUTCOMES = ("done", "skipped", "failed")
ARTIFACT_STATES = ("registered", "verified", "adopted", "unadopted", "purged")
ARTIFACT_KINDS = ("section_image", "background", "delete_mask", "protect_mask", "render_image")
CLOSE_REASONS = ("handed_off", "expired", "cancelled", "superseded")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


PRECHECK = f"""
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM job_async_task WHERE NOT ({_in('status', TASK_STATUSES)});
  IF n > 0 THEN
    RAISE EXCEPTION USING MESSAGE = '0008 중단 - job_async_task.status 허용값 밖의 행 ' || n || '개. 값을 추정해 바꾸지 않음';
  END IF;
END $$
"""

JAT_COLUMNS = [
    "ADD COLUMN execution_schema_version INTEGER NOT NULL DEFAULT 0",
    "ADD COLUMN parent_task_id BIGINT NULL",
    "ADD COLUMN run_kind VARCHAR(20) NULL",
    "ADD COLUMN run_scope JSONB NULL",
    "ADD COLUMN stage VARCHAR(20) NULL",
    "ADD COLUMN attempt_no INTEGER NULL",
    "ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT true",
    "ADD COLUMN supersedes_task_id BIGINT NULL",
    "ADD COLUMN retry_origin VARCHAR(10) NULL",
    "ADD COLUMN lease_owner VARCHAR(100) NULL",
    "ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0",
    "ADD COLUMN lease_token_hash CHAR(64) NULL",
    "ADD COLUMN lease_expires_at TIMESTAMPTZ NULL",
    "ADD COLUMN heartbeat_at TIMESTAMPTZ NULL",
    "ADD COLUMN dispatch_count INTEGER NOT NULL DEFAULT 0",
    "ADD COLUMN last_dispatched_at TIMESTAMPTZ NULL",
    "ADD COLUMN input_manifest JSONB NULL",
    "ADD COLUMN input_fingerprint CHAR(64) NULL",
    "ADD COLUMN skip_reason VARCHAR(30) NULL",
    "ADD COLUMN target_count INTEGER NULL",
    "ADD COLUMN cancelled_at TIMESTAMPTZ NULL",
]

STAGE_TASK_TYPE = (
    "(stage = 'analyze' AND task_type = 'ocr' AND unit_type = 'job')"
    " OR (stage IN ('judge', 'label', 'logo', 'style') AND task_type = 'section' AND unit_type = 'section')"
    " OR (stage = 'inpaint' AND task_type = 'inpaint' AND unit_type = 'section')"
    " OR (stage = 'translate' AND task_type = 'translate' AND unit_type = 'section')"
    " OR (stage = 'preview' AND task_type = 'render' AND unit_type = 'section')"
    " OR (stage = 'final_render' AND task_type = 'render' AND unit_type = 'job')"
)
RUN_TASK_TYPE = (
    "(run_kind = 'analysis' AND task_type = 'ocr')"
    " OR (run_kind = 'downstream' AND task_type = 'translate')"
    " OR (run_kind = 'final_render' AND task_type = 'render')"
)

JAT_CHECKS = [
    ("ck_jat_status", _in("status", TASK_STATUSES)),
    ("ck_jat_schema_version", "execution_schema_version IN (0, 1)"),
    ("ck_jat_run_kind", f"run_kind IS NULL OR {_in('run_kind', RUN_KINDS)}"),
    ("ck_jat_stage", f"stage IS NULL OR {_in('stage', STAGES)}"),
    ("ck_jat_retry_origin", f"retry_origin IS NULL OR {_in('retry_origin', RETRY_ORIGINS)}"),
    ("ck_jat_skip_reason", f"skip_reason IS NULL OR {_in('skip_reason', SKIP_REASONS)}"),
    ("ck_jat_counts", "lease_epoch >= 0 AND dispatch_count >= 0"),
    # K2 — 생략 사유는 done 행에만
    ("ck_jat_k2_skip_done", "skip_reason IS NULL OR status = 'done'"),
    # K1 — 신규 시도의 필수값(NULL 우회 금지)
    (
        "ck_jat_k1_attempt",
        "execution_schema_version = 0 OR parent_task_id IS NULL OR ("
        "stage IS NOT NULL AND unit_type IS NOT NULL AND unit_id IS NOT NULL AND attempt_no IS NOT NULL"
        " AND input_manifest IS NOT NULL AND input_fingerprint IS NOT NULL AND target_count IS NOT NULL"
        " AND attempt_no >= 1 AND target_count >= 0 AND run_kind IS NULL AND run_scope IS NULL"
        " AND (unit_type <> 'job' OR unit_id = job_id)"
        f" AND ({STAGE_TASK_TYPE}))",
    ),
    # K4 — 신규 대표의 필수값
    (
        "ck_jat_k4_run",
        "execution_schema_version = 0 OR parent_task_id IS NOT NULL OR ("
        "unit_type = 'job' AND unit_id = job_id AND run_kind IS NOT NULL AND run_scope IS NOT NULL"
        " AND stage IS NULL AND attempt_no IS NULL AND supersedes_task_id IS NULL AND retry_origin IS NULL"
        " AND lease_token_hash IS NULL"
        f" AND ({RUN_TASK_TYPE}))",
    ),
    # 대표는 권한·고정 입력을 갖지 않는다 / 시도의 대표 컬럼은 K1에서 막는다
    ("ck_jat_run_no_attempt_fields", "parent_task_id IS NOT NULL OR (input_manifest IS NULL AND input_fingerprint IS NULL)"),
    ("ck_jat_lease_hash_hex", "lease_token_hash IS NULL OR lease_token_hash ~ '^[0-9a-f]{64}$'"),
    ("ck_jat_fingerprint_hex", "input_fingerprint IS NULL OR input_fingerprint ~ '^[0-9a-f]{64}$'"),
]

JAT_INDEXES = [
    # U1 — 같은 job·실행 종류의 활성 대표 중복 금지
    "CREATE UNIQUE INDEX uq_jat_u1_active_run ON job_async_task (job_id, run_kind) "
    "WHERE execution_schema_version = 1 AND parent_task_id IS NULL AND status IN ('pending', 'running')",
    # U2 — 논리 작업의 현재 시도는 하나
    "CREATE UNIQUE INDEX uq_jat_u2_current ON job_async_task (parent_task_id, stage, unit_type, unit_id) "
    "WHERE execution_schema_version = 1 AND parent_task_id IS NOT NULL AND is_current",
    # U3 — 같은 순번 시도 중복 금지
    "CREATE UNIQUE INDEX uq_jat_u3_attempt_no ON job_async_task (parent_task_id, stage, unit_type, unit_id, attempt_no) "
    "WHERE execution_schema_version = 1 AND parent_task_id IS NOT NULL",
    "CREATE INDEX ix_jat_parent ON job_async_task (parent_task_id) WHERE parent_task_id IS NOT NULL",
    "CREATE INDEX ix_jat_recovery ON job_async_task (status, lease_expires_at) "
    "WHERE execution_schema_version = 1 AND parent_task_id IS NOT NULL AND is_current",
]

JAT_GUARD = f"""
CREATE FUNCTION job_async_task_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  p RECORD;
  s RECORD;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.execution_schema_version <> 1 THEN
      RAISE EXCEPTION 'job_async_task: 새 행은 execution_schema_version=1만 허용한다(0은 0008 이전 레거시 행)';
    END IF;
  ELSE
    IF NEW.execution_schema_version <> OLD.execution_schema_version THEN
      RAISE EXCEPTION 'job_async_task: execution_schema_version은 바꿀 수 없다';
    END IF;
    IF OLD.execution_schema_version = 0 THEN
      RETURN NEW;
    END IF;
    IF NEW.job_id <> OLD.job_id OR NEW.task_type <> OLD.task_type OR NEW.unit_type <> OLD.unit_type
       OR NEW.unit_id IS DISTINCT FROM OLD.unit_id OR NEW.parent_task_id IS DISTINCT FROM OLD.parent_task_id
       OR NEW.run_kind IS DISTINCT FROM OLD.run_kind OR NEW.run_scope IS DISTINCT FROM OLD.run_scope
       OR NEW.stage IS DISTINCT FROM OLD.stage OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no
       OR NEW.supersedes_task_id IS DISTINCT FROM OLD.supersedes_task_id OR NEW.retry_origin IS DISTINCT FROM OLD.retry_origin
       OR NEW.input_manifest IS DISTINCT FROM OLD.input_manifest OR NEW.input_fingerprint IS DISTINCT FROM OLD.input_fingerprint
       OR NEW.max_retry <> OLD.max_retry OR NEW.retry_count <> OLD.retry_count THEN
      RAISE EXCEPTION 'job_async_task %: 실행·시도 식별과 고정 입력은 바꿀 수 없다(새 시도를 만든다)', OLD.id;
    END IF;
    IF OLD.is_current = false AND NEW.is_current = true THEN
      RAISE EXCEPTION 'job_async_task %: 대체된 시도를 현재 시도로 되돌릴 수 없다', OLD.id;
    END IF;
    IF OLD.status IN ('done', 'cancelled') AND NEW.status <> OLD.status THEN
      RAISE EXCEPTION 'job_async_task %: % 상태는 바꿀 수 없다', OLD.id, OLD.status;
    END IF;
    IF NEW.lease_epoch < OLD.lease_epoch THEN
      RAISE EXCEPTION 'job_async_task %: lease_epoch는 줄어들 수 없다', OLD.id;
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.parent_task_id IS NOT NULL THEN
    SELECT job_id, parent_task_id, run_kind, execution_schema_version INTO p
      FROM job_async_task WHERE id = NEW.parent_task_id;
    IF p.job_id IS DISTINCT FROM NEW.job_id OR p.parent_task_id IS NOT NULL OR p.execution_schema_version <> 1 THEN
      RAISE EXCEPTION 'job_async_task: 시도의 parent는 같은 job의 신규 대표 실행이어야 한다';
    END IF;
    IF NOT ((p.run_kind = 'analysis' AND NEW.stage IN ('analyze', 'judge'))
            OR (p.run_kind = 'downstream' AND NEW.stage IN ('label', 'logo', 'inpaint', 'style', 'translate', 'preview'))
            OR (p.run_kind = 'final_render' AND NEW.stage = 'final_render')) THEN
      RAISE EXCEPTION 'job_async_task: stage %는 실행 종류 %에 속하지 않는다', NEW.stage, p.run_kind;
    END IF;
    IF NEW.supersedes_task_id IS NULL THEN
      IF NEW.attempt_no <> 1 THEN
        RAISE EXCEPTION 'job_async_task: 첫 시도가 아니면 supersedes_task_id가 필요하다';
      END IF;
    ELSE
      SELECT parent_task_id, stage, unit_type, unit_id, attempt_no, retry_count INTO s
        FROM job_async_task WHERE id = NEW.supersedes_task_id;
      IF s.parent_task_id IS DISTINCT FROM NEW.parent_task_id OR s.stage IS DISTINCT FROM NEW.stage
         OR s.unit_type IS DISTINCT FROM NEW.unit_type OR s.unit_id IS DISTINCT FROM NEW.unit_id
         OR s.attempt_no IS DISTINCT FROM NEW.attempt_no - 1 THEN
        RAISE EXCEPTION 'job_async_task: supersedes_task_id는 같은 논리 작업의 직전 시도여야 한다';
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$
"""

GRANT_TABLE = f"""
CREATE TABLE task_lease_grant (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES job_async_task (id) ON DELETE CASCADE,
  job_id BIGINT NOT NULL REFERENCES job (id) ON DELETE CASCADE,
  generation_epoch INTEGER NOT NULL CHECK (generation_epoch > 0),
  worker_id VARCHAR(100) NOT NULL,
  token_hash CHAR(64) NULL CHECK (token_hash IS NULL OR token_hash ~ '^[0-9a-f]{{64}}$'),
  issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  initial_expires_at TIMESTAMPTZ NOT NULL,
  closed_at TIMESTAMPTZ NULL,
  close_reason VARCHAR(20) NULL CHECK (close_reason IS NULL OR {_in('close_reason', CLOSE_REASONS)}),
  security_revoked_at TIMESTAMPTZ NULL,
  security_revoke_reason VARCHAR(200) NULL,
  verification_purged_at TIMESTAMPTZ NULL,
  CONSTRAINT uq_grant_u7 UNIQUE (task_id, generation_epoch),
  CONSTRAINT ck_grant_close_pair CHECK ((closed_at IS NULL) = (close_reason IS NULL)),
  CONSTRAINT ck_grant_revoke_pair CHECK ((security_revoked_at IS NULL) = (security_revoke_reason IS NULL)),
  CONSTRAINT ck_grant_hash_or_purged CHECK ((token_hash IS NULL) = (verification_purged_at IS NOT NULL))
)
"""

GRANT_GUARD = """
CREATE FUNCTION task_lease_grant_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t RECORD;
BEGIN
  IF TG_OP = 'INSERT' THEN
    SELECT job_id, parent_task_id, execution_schema_version INTO t FROM job_async_task WHERE id = NEW.task_id;
    IF t.job_id IS DISTINCT FROM NEW.job_id OR t.parent_task_id IS NULL OR t.execution_schema_version <> 1 THEN
      RAISE EXCEPTION 'task_lease_grant: 같은 job의 신규 시도에만 발급한다(대표 실행 금지)';
    END IF;
    IF NEW.token_hash IS NULL THEN
      RAISE EXCEPTION 'task_lease_grant: 발급 시 token_hash가 필요하다';
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.task_id <> OLD.task_id OR NEW.job_id <> OLD.job_id OR NEW.generation_epoch <> OLD.generation_epoch
     OR NEW.worker_id <> OLD.worker_id OR NEW.issued_at <> OLD.issued_at OR NEW.initial_expires_at <> OLD.initial_expires_at THEN
    RAISE EXCEPTION 'task_lease_grant %: 발급 정보는 바꿀 수 없다', OLD.id;
  END IF;
  IF NEW.token_hash IS DISTINCT FROM OLD.token_hash AND NEW.token_hash IS NOT NULL THEN
    RAISE EXCEPTION 'task_lease_grant %: token_hash는 다른 값으로 바꿀 수 없다(정리 시 NULL만)', OLD.id;
  END IF;
  IF OLD.closed_at IS NOT NULL AND (NEW.closed_at IS DISTINCT FROM OLD.closed_at OR NEW.close_reason IS DISTINCT FROM OLD.close_reason) THEN
    RAISE EXCEPTION 'task_lease_grant %: 종료 기록은 바꿀 수 없다', OLD.id;
  END IF;
  IF OLD.security_revoked_at IS NOT NULL AND NEW.security_revoked_at IS DISTINCT FROM OLD.security_revoked_at THEN
    RAISE EXCEPTION 'task_lease_grant %: 보안상 폐기는 되돌릴 수 없다', OLD.id;
  END IF;
  RETURN NEW;
END $$
"""

HANDOFF_TABLE = f"""
CREATE TABLE task_handoff (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  task_id BIGINT NOT NULL REFERENCES job_async_task (id) ON DELETE CASCADE,
  job_id BIGINT NOT NULL REFERENCES job (id) ON DELETE CASCADE,
  lease_epoch INTEGER NOT NULL CHECK (lease_epoch >= 0),
  producer_grant_id BIGINT NULL REFERENCES task_lease_grant (id),
  late BOOLEAN NOT NULL DEFAULT false,
  contract_version VARCHAR(20) NOT NULL,
  impl_version VARCHAR(200) NULL,
  input_fingerprint CHAR(64) NOT NULL CHECK (input_fingerprint ~ '^[0-9a-f]{{64}}$'),
  outcome VARCHAR(10) NOT NULL CHECK ({_in('outcome', HANDOFF_OUTCOMES)}),
  target_count INTEGER NOT NULL CHECK (target_count >= 0),
  skip_reason VARCHAR(30) NULL CHECK (skip_reason IS NULL OR {_in('skip_reason', SKIP_REASONS)}),
  payload JSONB NOT NULL,
  manifest_sha256 CHAR(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{{64}}$'),
  state VARCHAR(10) NOT NULL DEFAULT 'received' CHECK ({_in('state', HANDOFF_STATES)}),
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  verified_at TIMESTAMPTZ NULL,
  adopted_at TIMESTAMPTZ NULL,
  adopted_by VARCHAR(100) NULL,
  adopted_epoch INTEGER NULL,
  adopt_attempts INTEGER NOT NULL DEFAULT 0 CHECK (adopt_attempts >= 0),
  reject_reason TEXT NULL,
  verify_result JSONB NULL,
  CONSTRAINT uq_handoff_u4 UNIQUE (task_id, lease_epoch),
  CONSTRAINT ck_handoff_skip CHECK (skip_reason IS NULL OR outcome = 'skipped'),
  CONSTRAINT ck_handoff_skipped_reason CHECK (outcome <> 'skipped' OR skip_reason IS NOT NULL),
  CONSTRAINT ck_handoff_adopted CHECK (state <> 'adopted' OR (adopted_at IS NOT NULL AND adopted_by IS NOT NULL AND adopted_epoch IS NOT NULL))
)
"""

ARTIFACT_TABLE = f"""
CREATE TABLE task_artifact (
  id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  handoff_id BIGINT NOT NULL REFERENCES task_handoff (id) ON DELETE CASCADE,
  job_id BIGINT NOT NULL REFERENCES job (id) ON DELETE CASCADE,
  kind VARCHAR(20) NOT NULL CHECK ({_in('kind', ARTIFACT_KINDS)}),
  part_key VARCHAR(100) NOT NULL,
  staging_key VARCHAR(500) NULL,
  verified_key VARCHAR(500) NULL,
  source_ref JSONB NULL,
  byte_size BIGINT NULL CHECK (byte_size IS NULL OR byte_size >= 0),
  sha256 CHAR(64) NULL CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{{64}}$'),
  declared_sha256 CHAR(64) NULL CHECK (declared_sha256 IS NULL OR declared_sha256 ~ '^[0-9a-f]{{64}}$'),
  media_type VARCHAR(50) NULL,
  width INTEGER NULL,
  height INTEGER NULL,
  state VARCHAR(12) NOT NULL DEFAULT 'registered' CHECK ({_in('state', ARTIFACT_STATES)}),
  purge_after TIMESTAMPTZ NULL,
  purged_at TIMESTAMPTZ NULL,
  CONSTRAINT uq_artifact_u6 UNIQUE (handoff_id, kind, part_key),
  CONSTRAINT ck_artifact_k5 CHECK (
    state NOT IN ('verified', 'adopted')
    OR ((verified_key IS NULL) <> (source_ref IS NULL) AND byte_size IS NOT NULL AND sha256 IS NOT NULL AND media_type IS NOT NULL)
  )
)
"""

LINK_GUARD = """
CREATE FUNCTION task_link_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  t RECORD;
  g RECORD;
  h RECORD;
BEGIN
  IF TG_TABLE_NAME = 'task_handoff' THEN
    IF TG_OP = 'UPDATE' AND (NEW.task_id <> OLD.task_id OR NEW.job_id <> OLD.job_id OR NEW.lease_epoch <> OLD.lease_epoch
        OR NEW.producer_grant_id IS DISTINCT FROM OLD.producer_grant_id OR NEW.manifest_sha256 <> OLD.manifest_sha256
        OR NEW.input_fingerprint <> OLD.input_fingerprint OR NEW.outcome <> OLD.outcome) THEN
      RAISE EXCEPTION 'task_handoff %: 등록한 인계의 식별·본문 해시는 바꿀 수 없다', OLD.id;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.state IN ('adopted', 'rejected') AND NEW.state <> OLD.state THEN
      RAISE EXCEPTION 'task_handoff %: % 상태는 바꿀 수 없다', OLD.id, OLD.state;
    END IF;
    SELECT job_id, parent_task_id INTO t FROM job_async_task WHERE id = NEW.task_id;
    IF t.job_id IS DISTINCT FROM NEW.job_id OR t.parent_task_id IS NULL THEN
      RAISE EXCEPTION 'task_handoff: 같은 job의 시도에만 인계를 등록한다(K3)';
    END IF;
    IF NEW.producer_grant_id IS NOT NULL THEN
      SELECT task_id, job_id, generation_epoch INTO g FROM task_lease_grant WHERE id = NEW.producer_grant_id;
      IF g.task_id IS DISTINCT FROM NEW.task_id OR g.job_id IS DISTINCT FROM NEW.job_id OR g.generation_epoch IS DISTINCT FROM NEW.lease_epoch THEN
        RAISE EXCEPTION 'task_handoff: producer_grant_id의 시도·job·세대가 인계와 다르다(K6)';
      END IF;
    END IF;
  ELSE
    IF TG_OP = 'UPDATE' AND (NEW.handoff_id <> OLD.handoff_id OR NEW.job_id <> OLD.job_id OR NEW.kind <> OLD.kind OR NEW.part_key <> OLD.part_key) THEN
      RAISE EXCEPTION 'task_artifact %: 산출물 식별은 바꿀 수 없다', OLD.id;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.verified_key IS NOT NULL AND NEW.verified_key IS DISTINCT FROM OLD.verified_key THEN
      RAISE EXCEPTION 'task_artifact %: 검증 키는 바꿀 수 없다', OLD.id;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.sha256 IS NOT NULL AND NEW.sha256 IS DISTINCT FROM OLD.sha256 THEN
      RAISE EXCEPTION 'task_artifact %: 측정 해시는 바꿀 수 없다', OLD.id;
    END IF;
    SELECT job_id INTO h FROM task_handoff WHERE id = NEW.handoff_id;
    IF h.job_id IS DISTINCT FROM NEW.job_id THEN
      RAISE EXCEPTION 'task_artifact: 인계와 같은 job이어야 한다(K3)';
    END IF;
  END IF;
  RETURN NEW;
END $$
"""

JOB_GUARD = """
CREATE FUNCTION job_current_analysis_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t RECORD;
BEGIN
  IF NEW.current_analysis_task_id IS NOT NULL
     AND NEW.current_analysis_task_id IS DISTINCT FROM OLD.current_analysis_task_id THEN
    SELECT job_id, parent_task_id, run_kind, execution_schema_version INTO t FROM job_async_task WHERE id = NEW.current_analysis_task_id;
    IF t.job_id IS DISTINCT FROM NEW.id OR t.parent_task_id IS NOT NULL OR t.run_kind IS DISTINCT FROM 'analysis' THEN
      RAISE EXCEPTION 'job %: current_analysis_task_id는 같은 job의 분석 대표 실행이어야 한다', NEW.id;
    END IF;
  END IF;
  RETURN NEW;
END $$
"""

SECTION_GUARD = """
CREATE FUNCTION section_analysis_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE t RECORD;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.analysis_task_id IS DISTINCT FROM OLD.analysis_task_id THEN
      RAISE EXCEPTION 'section %: analysis_task_id는 바꿀 수 없다', OLD.id;
    END IF;
    RETURN NEW;
  END IF;
  IF NEW.analysis_task_id IS NOT NULL THEN
    SELECT job_id, parent_task_id, run_kind INTO t FROM job_async_task WHERE id = NEW.analysis_task_id;
    IF t.job_id IS DISTINCT FROM NEW.job_id OR t.parent_task_id IS NOT NULL OR t.run_kind IS DISTINCT FROM 'analysis' THEN
      RAISE EXCEPTION 'section: analysis_task_id는 같은 job의 분석 대표 실행이어야 한다';
    END IF;
  END IF;
  RETURN NEW;
END $$
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(PRECHECK)
    op.execute("ALTER TABLE job_async_task " + ", ".join(JAT_COLUMNS))
    # 기존 행은 위 DEFAULT 0 으로 레거시가 되고, 이후 INSERT 기본값은 1
    op.execute("ALTER TABLE job_async_task ALTER COLUMN execution_schema_version SET DEFAULT 1")
    op.execute(
        "ALTER TABLE job_async_task ADD CONSTRAINT fk_jat_parent FOREIGN KEY (parent_task_id) "
        "REFERENCES job_async_task (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE job_async_task ADD CONSTRAINT fk_jat_supersedes FOREIGN KEY (supersedes_task_id) "
        "REFERENCES job_async_task (id) ON DELETE CASCADE"
    )
    for name, cond in JAT_CHECKS:
        op.execute(f"ALTER TABLE job_async_task ADD CONSTRAINT {name} CHECK ({cond})")
    for stmt in JAT_INDEXES:
        op.execute(stmt)
    op.execute(JAT_GUARD)
    op.execute(
        "CREATE TRIGGER trg_job_async_task_guard BEFORE INSERT OR UPDATE ON job_async_task "
        "FOR EACH ROW EXECUTE FUNCTION job_async_task_guard()"
    )

    op.execute(GRANT_TABLE)
    op.execute(GRANT_GUARD)
    op.execute(
        "CREATE TRIGGER trg_task_lease_grant_guard BEFORE INSERT OR UPDATE ON task_lease_grant "
        "FOR EACH ROW EXECUTE FUNCTION task_lease_grant_guard()"
    )
    op.execute(HANDOFF_TABLE)
    op.execute("CREATE UNIQUE INDEX uq_handoff_u5_adopted ON task_handoff (task_id) WHERE state = 'adopted'")
    op.execute("CREATE INDEX ix_handoff_state ON task_handoff (state) WHERE state IN ('received', 'verifying', 'verified')")
    op.execute(ARTIFACT_TABLE)
    op.execute("CREATE UNIQUE INDEX uq_artifact_u6_verified_key ON task_artifact (verified_key) WHERE verified_key IS NOT NULL")
    op.execute(LINK_GUARD)
    op.execute(
        "CREATE TRIGGER trg_task_handoff_guard BEFORE INSERT OR UPDATE ON task_handoff "
        "FOR EACH ROW EXECUTE FUNCTION task_link_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_task_artifact_guard BEFORE INSERT OR UPDATE ON task_artifact "
        "FOR EACH ROW EXECUTE FUNCTION task_link_guard()"
    )

    op.execute("ALTER TABLE job ADD COLUMN current_analysis_task_id BIGINT NULL")
    op.execute(
        "ALTER TABLE job ADD CONSTRAINT fk_job_current_analysis FOREIGN KEY (current_analysis_task_id) "
        "REFERENCES job_async_task (id) ON DELETE SET NULL"
    )
    op.execute(JOB_GUARD)
    op.execute(
        "CREATE TRIGGER trg_job_current_analysis_guard BEFORE UPDATE OF current_analysis_task_id ON job "
        "FOR EACH ROW EXECUTE FUNCTION job_current_analysis_guard()"
    )
    op.execute("ALTER TABLE section ADD COLUMN analysis_task_id BIGINT NULL")
    op.execute(
        "ALTER TABLE section ADD CONSTRAINT fk_section_analysis_task FOREIGN KEY (analysis_task_id) "
        "REFERENCES job_async_task (id)"
    )
    op.execute("CREATE INDEX ix_section_analysis_task ON section (analysis_task_id)")
    op.execute(SECTION_GUARD)
    op.execute(
        "CREATE TRIGGER trg_section_analysis_guard BEFORE INSERT OR UPDATE OF analysis_task_id ON section "
        "FOR EACH ROW EXECUTE FUNCTION section_analysis_guard()"
    )
    op.execute(
        "ALTER TABLE source_image ADD COLUMN sha256 CHAR(64) NULL "
        "CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$')"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE source_image DROP COLUMN sha256")
    op.execute("DROP TRIGGER trg_section_analysis_guard ON section")
    op.execute("DROP FUNCTION section_analysis_guard()")
    op.execute("DROP INDEX ix_section_analysis_task")
    op.execute("ALTER TABLE section DROP CONSTRAINT fk_section_analysis_task")
    op.execute("ALTER TABLE section DROP COLUMN analysis_task_id")
    op.execute("DROP TRIGGER trg_job_current_analysis_guard ON job")
    op.execute("DROP FUNCTION job_current_analysis_guard()")
    op.execute("ALTER TABLE job DROP CONSTRAINT fk_job_current_analysis")
    op.execute("ALTER TABLE job DROP COLUMN current_analysis_task_id")
    op.execute("DROP TABLE task_artifact")
    op.execute("DROP TABLE task_handoff")
    op.execute("DROP FUNCTION task_link_guard()")
    op.execute("DROP TABLE task_lease_grant")
    op.execute("DROP FUNCTION task_lease_grant_guard()")
    op.execute("DROP TRIGGER trg_job_async_task_guard ON job_async_task")
    op.execute("DROP FUNCTION job_async_task_guard()")
    # 신규 실행 행(버전 1)은 레거시 형태로 되돌릴 수 없으므로 지운다(대표·시도·인계 이력)
    op.execute("DELETE FROM job_async_task WHERE execution_schema_version = 1")
    for stmt in ("uq_jat_u1_active_run", "uq_jat_u2_current", "uq_jat_u3_attempt_no", "ix_jat_parent", "ix_jat_recovery"):
        op.execute(f"DROP INDEX {stmt}")
    for name, _ in reversed(JAT_CHECKS):
        op.execute(f"ALTER TABLE job_async_task DROP CONSTRAINT {name}")
    op.execute("ALTER TABLE job_async_task DROP CONSTRAINT fk_jat_supersedes")
    op.execute("ALTER TABLE job_async_task DROP CONSTRAINT fk_jat_parent")
    for col in reversed(JAT_COLUMNS):
        op.execute("ALTER TABLE job_async_task DROP COLUMN " + col.split()[2])
