"""initial schema (30 tables)

기존 db/schema.sql(ERD_PIX_ateV5 -> PostgreSQL 변환본)을 Alembic 초기
마이그레이션으로 이관한다. 이 시점부터 스키마의 주인은 Alembic 이다.

Revision ID: 0001
Revises:
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


STATEMENTS = [
    "CREATE TABLE source_image (\n  id BIGINT NOT NULL,\n  job_id BIGINT NOT NULL,\n  upload_order INTEGER NOT NULL,\n  image_type VARCHAR(20) NOT NULL DEFAULT 'detail',\n  file_url VARCHAR(500) NOT NULL,\n  width INTEGER NOT NULL,\n  height INTEGER NOT NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)",
    "CREATE TABLE channel_spec (\n  id BIGINT NOT NULL,\n  code VARCHAR(50) NOT NULL,\n  name VARCHAR(200) NOT NULL,\n  spec_type VARCHAR(20) NOT NULL DEFAULT 'original',\n  description VARCHAR(500) NULL,\n  is_active BOOLEAN NOT NULL DEFAULT true,\n  release VARCHAR(10) NULL\n)",
    'CREATE TABLE export_artifact (\n  id BIGINT NOT NULL,\n  job_id BIGINT NOT NULL,\n  artifact_type VARCHAR(20) NOT NULL,\n  file_url VARCHAR(500) NULL,\n  is_distributable BOOLEAN NOT NULL DEFAULT true,\n  is_generated BOOLEAN NOT NULL DEFAULT false,\n  is_active BOOLEAN NOT NULL DEFAULT true,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    'CREATE TABLE section_verdict (\n  id BIGINT NOT NULL,\n  section_id BIGINT NOT NULL,\n  verdict_status VARCHAR(20) NOT NULL,\n  verdict_type VARCHAR(30) NOT NULL,\n  problem_text TEXT NULL,\n  dictionary_id BIGINT NULL,\n  alternative_expression TEXT NULL,\n  basis_article VARCHAR(300) NULL,\n  evidence_url VARCHAR(500) NULL,\n  reason TEXT NULL\n)',
    'CREATE TABLE brand (\n  id BIGINT NOT NULL,\n  seller_id BIGINT NOT NULL,\n  name_ko VARCHAR(200) NOT NULL,\n  name_en VARCHAR(200) NOT NULL,\n  brand_overview TEXT NULL,\n  core_audience TEXT NULL,\n  metadata JSONB NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    'CREATE TABLE brand_logo (\n  id BIGINT NOT NULL,\n  brand_id BIGINT NOT NULL,\n  logo_key VARCHAR(500) NOT NULL,\n  format VARCHAR(10) NOT NULL,\n  order_no INTEGER NOT NULL DEFAULT 0,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    'CREATE TABLE deliverable_section (\n  id BIGINT NOT NULL,\n  deliverable_id BIGINT NOT NULL,\n  section_id BIGINT NOT NULL,\n  stack_offset INTEGER NULL,\n  order_no INTEGER NULL\n)',
    'CREATE TABLE category_master (\n  id BIGINT NOT NULL,\n  parent_id BIGINT NULL,\n  name VARCHAR(200) NOT NULL,\n  amazon_node VARCHAR(200) NULL,\n  is_leaf BOOLEAN NOT NULL DEFAULT false,\n  path VARCHAR(500) NULL\n)',
    'CREATE TABLE edit_signal (\n  id BIGINT NOT NULL,\n  job_id BIGINT NOT NULL,\n  text_block_id BIGINT NULL,\n  signal_type VARCHAR(50) NOT NULL,\n  before_text TEXT NULL,\n  after_text TEXT NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    'CREATE TABLE audit_log (\n  id BIGINT NOT NULL,\n  actor_id BIGINT NULL,\n  actor_type VARCHAR(20) NOT NULL,\n  action_type VARCHAR(50) NOT NULL,\n  target_type VARCHAR(50) NULL,\n  target_id BIGINT NULL,\n  detail JSONB NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    "CREATE TABLE job (\n  id BIGINT NOT NULL,\n  seller_id BIGINT NOT NULL,\n  brand_id BIGINT NOT NULL,\n  parent_job_id BIGINT NULL,\n  status VARCHAR(20) NOT NULL DEFAULT 'draft',\n  current_step VARCHAR(4) NOT NULL DEFAULT 'N1',\n  user_facing_status VARCHAR(20) NULL,\n  target_country VARCHAR(8) NULL,\n  regulatory_class VARCHAR(30) NULL,\n  target_lang VARCHAR(8) NULL,\n  spec_type VARCHAR(20) NOT NULL DEFAULT 'original',\n  channel_spec_id BIGINT NULL,\n  display_category_id BIGINT NULL,\n  internal_category VARCHAR(40) NULL,\n  image_type VARCHAR(20) NULL,\n  is_saved BOOLEAN NOT NULL DEFAULT false,\n  saved_at TIMESTAMPTZ NULL,\n  expire_at TIMESTAMPTZ NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  product_name VARCHAR(200) NULL,\n  product_code VARCHAR(100) NULL\n)",
    'CREATE TABLE consent (\n  id BIGINT NOT NULL,\n  seller_id BIGINT NOT NULL,\n  job_id BIGINT NULL,\n  consent_type VARCHAR(50) NOT NULL,\n  consented_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    'CREATE TABLE job_keyword (\n  id BIGINT NOT NULL,\n  job_id BIGINT NOT NULL,\n  keyword VARCHAR(100) NOT NULL,\n  order_no INTEGER NOT NULL DEFAULT 0\n)',
    'CREATE TABLE master_data_version (\n  id BIGINT NOT NULL,\n  data_type VARCHAR(50) NOT NULL,\n  version VARCHAR(50) NOT NULL,\n  is_current BOOLEAN NOT NULL DEFAULT false,\n  loaded_by VARCHAR(100) NULL,\n  loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  note VARCHAR(500) NULL\n)',
    "CREATE TABLE deliverable (\n  id BIGINT NOT NULL,\n  job_id BIGINT NOT NULL,\n  source_image_id BIGINT NULL,\n  usage_type VARCHAR(20) NOT NULL DEFAULT 'detail',\n  image_url VARCHAR(500) NULL,\n  format VARCHAR(20) NULL,\n  color_space VARCHAR(20) NULL,\n  file_size BIGINT NULL,\n  render_status VARCHAR(10) NOT NULL DEFAULT 'pending',\n  validation_result JSONB NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)",
    'CREATE TABLE event_log (\n  id BIGINT NOT NULL,\n  job_id BIGINT NULL,\n  event_type VARCHAR(50) NOT NULL,\n  payload JSONB NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)',
    'CREATE TABLE expression_dictionary_evidence (\n  id BIGINT NOT NULL,\n  dictionary_id BIGINT NOT NULL,\n  evidence_source_type VARCHAR(50) NULL,\n  evidence_document TEXT NULL,\n  evidence_quote TEXT NULL,\n  evidence_article VARCHAR(300) NULL,\n  evidence_url VARCHAR(500) NULL,\n  is_primary BOOLEAN NOT NULL DEFAULT false\n)',
    'CREATE TABLE master_country (\n  code VARCHAR(8) NOT NULL,\n  name VARCHAR(100) NOT NULL,\n  has_regulatory_dictionary BOOLEAN NOT NULL DEFAULT false,\n  release VARCHAR(10) NULL\n)',
    'CREATE TABLE expression_dictionary (\n  id BIGINT NOT NULL,\n  external_id VARCHAR(32) NOT NULL,\n  source_expression VARCHAR(300) NULL,\n  variant_ko JSONB NULL,\n  forbidden_en JSONB NULL,\n  target_country VARCHAR(8) NOT NULL,\n  regulatory_class VARCHAR(30) NOT NULL,\n  dict_type VARCHAR(20) NOT NULL,\n  verdict_status VARCHAR(20) NOT NULL,\n  alternative_expression TEXT NULL,\n  reason TEXT NULL,\n  confirmed_date DATE NULL\n)',
    "CREATE TABLE validation_rule (\n  id BIGINT NOT NULL,\n  channel_spec_id BIGINT NOT NULL,\n  item_key VARCHAR(50) NOT NULL,\n  scope VARCHAR(20) NOT NULL,\n  comparator VARCHAR(20) NULL,\n  threshold_value VARCHAR(100) NULL,\n  unit VARCHAR(20) NULL,\n  severity VARCHAR(10) NOT NULL DEFAULT 'error',\n  source_url VARCHAR(500) NULL,\n  confirmed_date DATE NULL\n)",
    "CREATE TABLE section (\n  id BIGINT NOT NULL,\n  job_id BIGINT NOT NULL,\n  source_image_id BIGINT NOT NULL,\n  section_order INTEGER NOT NULL,\n  top_offset INTEGER NULL,\n  height INTEGER NULL,\n  bbox JSONB NULL,\n  category VARCHAR(50) NULL,\n  bucket VARCHAR(10) NOT NULL DEFAULT 'include',\n  exclusion_reason VARCHAR(30) NULL,\n  excluded_stage VARCHAR(4) NULL,\n  original_verdict JSONB NULL,\n  inpaint_status VARCHAR(10) NULL,\n  inpaint_image_url VARCHAR(500) NULL,\n  mask_image_url VARCHAR(500) NULL,\n  residual_ratio NUMERIC(5,4) NULL,\n  warning_badge VARCHAR(20) NULL,\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  image_key VARCHAR(500) NULL,\n  render_image_key VARCHAR(500) NULL\n)",
    'CREATE TABLE master_language (\n  code VARCHAR(8) NOT NULL,\n  name VARCHAR(100) NOT NULL,\n  release VARCHAR(10) NULL\n)',
    "CREATE TABLE text_block (\n  id BIGINT NOT NULL,\n  section_id BIGINT NOT NULL,\n  block_order INTEGER NOT NULL,\n  role VARCHAR(20) NOT NULL DEFAULT 'body',\n  is_excluded BOOLEAN NULL,\n  source_ko TEXT NULL,\n  source_lines JSONB NULL,\n  bbox JSONB NULL,\n  style JSONB NULL,\n  block_status VARCHAR(10) NOT NULL DEFAULT 'machine',\n  trans_1 TEXT NULL,\n  trans_2 TEXT NULL,\n  origin_block_ids JSONB NULL,\n  char_count INTEGER NULL,\n  char_limit INTEGER NULL,\n  overflow BOOLEAN NOT NULL DEFAULT false,\n  compliance_flags JSONB NULL,\n  ocr_confidence NUMERIC(5,4) NULL,\n  auto_adjust JSONB NULL,\n  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),\n  revision INTEGER NOT NULL DEFAULT 0\n)",
    "CREATE TABLE seller (\n  id BIGINT NOT NULL,\n  cognito_sub VARCHAR(255) NOT NULL,\n  email VARCHAR(255) NOT NULL,\n  account_type VARCHAR(20) NOT NULL DEFAULT 'individual',\n  created_at TIMESTAMPTZ NOT NULL DEFAULT now()\n)",
    'CREATE TABLE category_mapping (\n  id BIGINT NOT NULL,\n  category_id BIGINT NOT NULL,\n  internal_category VARCHAR(40) NOT NULL\n)',
    "CREATE TABLE job_async_task (\n  id BIGINT NOT NULL,\n  job_id BIGINT NOT NULL,\n  task_type VARCHAR(20) NOT NULL,\n  unit_type VARCHAR(20) NOT NULL,\n  unit_id BIGINT NULL,\n  status VARCHAR(10) NOT NULL DEFAULT 'pending',\n  retry_count INTEGER NOT NULL DEFAULT 0,\n  max_retry INTEGER NOT NULL DEFAULT 2,\n  error_code VARCHAR(50) NULL,\n  error_message TEXT NULL,\n  started_at TIMESTAMPTZ NULL,\n  finished_at TIMESTAMPTZ NULL,\n  revision INTEGER NULL\n)",
    'CREATE TABLE master_regulatory_class (\n  id BIGINT NOT NULL,\n  country_code VARCHAR(8) NOT NULL,\n  code VARCHAR(30) NOT NULL,\n  name VARCHAR(100) NOT NULL,\n  is_escape_hatch BOOLEAN NOT NULL DEFAULT false\n)',
    'CREATE TABLE module_spec (\n  id BIGINT NOT NULL,\n  channel_spec_id BIGINT NOT NULL,\n  module_name VARCHAR(200) NOT NULL,\n  slot_spec JSONB NULL,\n  slot_count INTEGER NULL,\n  usage_type VARCHAR(20) NULL,\n  field_type VARCHAR(100) NULL,\n  char_limit INTEGER NULL,\n  recommended_count INTEGER NULL,\n  baked_allowed BOOLEAN NOT NULL DEFAULT false,\n  source_url VARCHAR(500) NULL,\n  confirmed_date DATE NULL\n)',
    'CREATE TABLE font_map (\n  id BIGINT NOT NULL,\n  role VARCHAR(20) NOT NULL,\n  target_lang VARCHAR(8) NULL,\n  font_family VARCHAR(100) NOT NULL\n)',
    "CREATE TABLE glossary (\n  id BIGINT NOT NULL,\n  term_ko VARCHAR(200) NOT NULL,\n  term_target VARCHAR(200) NOT NULL,\n  target_lang VARCHAR(8) NOT NULL,\n  internal_category VARCHAR(40) NOT NULL,\n  enforcement VARCHAR(20) NOT NULL DEFAULT 'reference',\n  example_sentence TEXT NULL\n)",
    'ALTER TABLE source_image ADD CONSTRAINT PK_SOURCE_IMAGE PRIMARY KEY (\n  id\n)',
    'ALTER TABLE channel_spec ADD CONSTRAINT PK_CHANNEL_SPEC PRIMARY KEY (\n  id\n)',
    'ALTER TABLE export_artifact ADD CONSTRAINT PK_EXPORT_ARTIFACT PRIMARY KEY (\n  id\n)',
    'ALTER TABLE section_verdict ADD CONSTRAINT PK_SECTION_VERDICT PRIMARY KEY (\n  id\n)',
    'ALTER TABLE brand ADD CONSTRAINT PK_BRAND PRIMARY KEY (\n  id\n)',
    'ALTER TABLE brand_logo ADD CONSTRAINT PK_BRAND_LOGO PRIMARY KEY (\n  id\n)',
    'ALTER TABLE deliverable_section ADD CONSTRAINT PK_DELIVERABLE_SECTION PRIMARY KEY (\n  id\n)',
    'ALTER TABLE category_master ADD CONSTRAINT PK_CATEGORY_MASTER PRIMARY KEY (\n  id\n)',
    'ALTER TABLE edit_signal ADD CONSTRAINT PK_EDIT_SIGNAL PRIMARY KEY (\n  id\n)',
    'ALTER TABLE audit_log ADD CONSTRAINT PK_AUDIT_LOG PRIMARY KEY (\n  id\n)',
    'ALTER TABLE job ADD CONSTRAINT PK_JOB PRIMARY KEY (\n  id\n)',
    'ALTER TABLE consent ADD CONSTRAINT PK_CONSENT PRIMARY KEY (\n  id\n)',
    'ALTER TABLE job_keyword ADD CONSTRAINT PK_JOB_KEYWORD PRIMARY KEY (\n  id\n)',
    'ALTER TABLE master_data_version ADD CONSTRAINT PK_MASTER_DATA_VERSION PRIMARY KEY (\n  id\n)',
    'ALTER TABLE deliverable ADD CONSTRAINT PK_DELIVERABLE PRIMARY KEY (\n  id\n)',
    'ALTER TABLE event_log ADD CONSTRAINT PK_EVENT_LOG PRIMARY KEY (\n  id\n)',
    'ALTER TABLE expression_dictionary_evidence ADD CONSTRAINT PK_EXPRESSION_DICTIONARY_EVIDENCE PRIMARY KEY (\n  id\n)',
    'ALTER TABLE master_country ADD CONSTRAINT PK_MASTER_COUNTRY PRIMARY KEY (\n  code\n)',
    'ALTER TABLE expression_dictionary ADD CONSTRAINT PK_EXPRESSION_DICTIONARY PRIMARY KEY (\n  id\n)',
    'ALTER TABLE validation_rule ADD CONSTRAINT PK_VALIDATION_RULE PRIMARY KEY (\n  id\n)',
    'ALTER TABLE section ADD CONSTRAINT PK_SECTION PRIMARY KEY (\n  id\n)',
    'ALTER TABLE master_language ADD CONSTRAINT PK_MASTER_LANGUAGE PRIMARY KEY (\n  code\n)',
    'ALTER TABLE text_block ADD CONSTRAINT PK_TEXT_BLOCK PRIMARY KEY (\n  id\n)',
    'ALTER TABLE seller ADD CONSTRAINT PK_SELLER PRIMARY KEY (\n  id\n)',
    'ALTER TABLE category_mapping ADD CONSTRAINT PK_CATEGORY_MAPPING PRIMARY KEY (\n  id\n)',
    'ALTER TABLE job_async_task ADD CONSTRAINT PK_JOB_ASYNC_TASK PRIMARY KEY (\n  id\n)',
    'ALTER TABLE master_regulatory_class ADD CONSTRAINT PK_MASTER_REGULATORY_CLASS PRIMARY KEY (\n  id\n)',
    'ALTER TABLE module_spec ADD CONSTRAINT PK_MODULE_SPEC PRIMARY KEY (\n  id\n)',
    'ALTER TABLE font_map ADD CONSTRAINT PK_FONT_MAP PRIMARY KEY (\n  id\n)',
    'ALTER TABLE glossary ADD CONSTRAINT PK_GLOSSARY PRIMARY KEY (\n  id\n)',
]


def upgrade() -> None:
    for stmt in STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    op.execute("DROP SCHEMA public CASCADE")
    op.execute("CREATE SCHEMA public")
