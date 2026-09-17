-- Pixate DB schema (PostgreSQL 16)
-- ERD_PIX_ateV5 (ERDCloud export) -> PostgreSQL 변환본
-- 변환: 백틱 제거 · MySQL 인라인 COMMENT 제거 · 탭 정리 · BOM 제거
-- CHECK/FK 제약은 원본이 주석으로만 갖고 있어 이번 스키마에는 미포함(추후 추가)

CREATE TABLE source_image (
  id BIGINT NOT NULL,
  job_id BIGINT NOT NULL,
  upload_order INTEGER NOT NULL,
  image_type VARCHAR(20) NOT NULL DEFAULT 'detail',
  file_url VARCHAR(500) NOT NULL,
  width INTEGER NOT NULL,
  height INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE channel_spec (
  id BIGINT NOT NULL,
  code VARCHAR(50) NOT NULL,
  name VARCHAR(200) NOT NULL,
  spec_type VARCHAR(20) NOT NULL DEFAULT 'original',
  description VARCHAR(500) NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  release VARCHAR(10) NULL
);

CREATE TABLE export_artifact (
  id BIGINT NOT NULL,
  job_id BIGINT NOT NULL,
  artifact_type VARCHAR(20) NOT NULL,
  file_url VARCHAR(500) NULL,
  is_distributable BOOLEAN NOT NULL DEFAULT true,
  is_generated BOOLEAN NOT NULL DEFAULT false,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE section_verdict (
  id BIGINT NOT NULL,
  section_id BIGINT NOT NULL,
  verdict_status VARCHAR(20) NOT NULL,
  verdict_type VARCHAR(30) NOT NULL,
  problem_text TEXT NULL,
  dictionary_id BIGINT NULL,
  alternative_expression TEXT NULL,
  basis_article VARCHAR(300) NULL,
  evidence_url VARCHAR(500) NULL,
  reason TEXT NULL
);

CREATE TABLE brand (
  id BIGINT NOT NULL,
  seller_id BIGINT NOT NULL,
  name_ko VARCHAR(200) NOT NULL,
  name_en VARCHAR(200) NOT NULL,
  brand_overview TEXT NULL,
  core_audience TEXT NULL,
  metadata JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE brand_logo (
  id BIGINT NOT NULL,
  brand_id BIGINT NOT NULL,
  logo_key VARCHAR(500) NOT NULL,
  format VARCHAR(10) NOT NULL,
  order_no INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE deliverable_section (
  id BIGINT NOT NULL,
  deliverable_id BIGINT NOT NULL,
  section_id BIGINT NOT NULL,
  stack_offset INTEGER NULL,
  order_no INTEGER NULL
);

CREATE TABLE category_master (
  id BIGINT NOT NULL,
  parent_id BIGINT NULL,
  name VARCHAR(200) NOT NULL,
  amazon_node VARCHAR(200) NULL,
  is_leaf BOOLEAN NOT NULL DEFAULT false,
  path VARCHAR(500) NULL
);

CREATE TABLE edit_signal (
  id BIGINT NOT NULL,
  job_id BIGINT NOT NULL,
  text_block_id BIGINT NULL,
  signal_type VARCHAR(50) NOT NULL,
  before_text TEXT NULL,
  after_text TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id BIGINT NOT NULL,
  actor_id BIGINT NULL,
  actor_type VARCHAR(20) NOT NULL,
  action_type VARCHAR(50) NOT NULL,
  target_type VARCHAR(50) NULL,
  target_id BIGINT NULL,
  detail JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE job (
  id BIGINT NOT NULL,
  seller_id BIGINT NOT NULL,
  brand_id BIGINT NOT NULL,
  parent_job_id BIGINT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'draft',
  current_step VARCHAR(4) NOT NULL DEFAULT 'N1',
  user_facing_status VARCHAR(20) NULL,
  target_country VARCHAR(8) NULL,
  regulatory_class VARCHAR(30) NULL,
  target_lang VARCHAR(8) NULL,
  spec_type VARCHAR(20) NOT NULL DEFAULT 'original',
  channel_spec_id BIGINT NULL,
  display_category_id BIGINT NULL,
  internal_category VARCHAR(40) NULL,
  image_type VARCHAR(20) NULL,
  is_saved BOOLEAN NOT NULL DEFAULT false,
  saved_at TIMESTAMPTZ NULL,
  expire_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  product_name VARCHAR(200) NULL,
  product_code VARCHAR(100) NULL
);

CREATE TABLE consent (
  id BIGINT NOT NULL,
  seller_id BIGINT NOT NULL,
  job_id BIGINT NULL,
  consent_type VARCHAR(50) NOT NULL,
  consented_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE job_keyword (
  id BIGINT NOT NULL,
  job_id BIGINT NOT NULL,
  keyword VARCHAR(100) NOT NULL,
  order_no INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE master_data_version (
  id BIGINT NOT NULL,
  data_type VARCHAR(50) NOT NULL,
  version VARCHAR(50) NOT NULL,
  is_current BOOLEAN NOT NULL DEFAULT false,
  loaded_by VARCHAR(100) NULL,
  loaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  note VARCHAR(500) NULL
);

CREATE TABLE deliverable (
  id BIGINT NOT NULL,
  job_id BIGINT NOT NULL,
  source_image_id BIGINT NULL,
  usage_type VARCHAR(20) NOT NULL DEFAULT 'detail',
  image_url VARCHAR(500) NULL,
  format VARCHAR(20) NULL,
  color_space VARCHAR(20) NULL,
  file_size BIGINT NULL,
  render_status VARCHAR(10) NOT NULL DEFAULT 'pending',
  validation_result JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE event_log (
  id BIGINT NOT NULL,
  job_id BIGINT NULL,
  event_type VARCHAR(50) NOT NULL,
  payload JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE expression_dictionary_evidence (
  id BIGINT NOT NULL,
  dictionary_id BIGINT NOT NULL,
  evidence_source_type VARCHAR(50) NULL,
  evidence_document TEXT NULL,
  evidence_quote TEXT NULL,
  evidence_article VARCHAR(300) NULL,
  evidence_url VARCHAR(500) NULL,
  is_primary BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE master_country (
  code VARCHAR(8) NOT NULL,
  name VARCHAR(100) NOT NULL,
  has_regulatory_dictionary BOOLEAN NOT NULL DEFAULT false,
  release VARCHAR(10) NULL
);

CREATE TABLE expression_dictionary (
  id BIGINT NOT NULL,
  external_id VARCHAR(32) NOT NULL,
  source_expression VARCHAR(300) NULL,
  variant_ko JSONB NULL,
  forbidden_en JSONB NULL,
  target_country VARCHAR(8) NOT NULL,
  regulatory_class VARCHAR(30) NOT NULL,
  dict_type VARCHAR(20) NOT NULL,
  verdict_status VARCHAR(20) NOT NULL,
  alternative_expression TEXT NULL,
  reason TEXT NULL,
  confirmed_date DATE NULL
);

CREATE TABLE validation_rule (
  id BIGINT NOT NULL,
  channel_spec_id BIGINT NOT NULL,
  item_key VARCHAR(50) NOT NULL,
  scope VARCHAR(20) NOT NULL,
  comparator VARCHAR(20) NULL,
  threshold_value VARCHAR(100) NULL,
  unit VARCHAR(20) NULL,
  severity VARCHAR(10) NOT NULL DEFAULT 'error',
  source_url VARCHAR(500) NULL,
  confirmed_date DATE NULL
);

CREATE TABLE section (
  id BIGINT NOT NULL,
  job_id BIGINT NOT NULL,
  source_image_id BIGINT NOT NULL,
  section_order INTEGER NOT NULL,
  top_offset INTEGER NULL,
  height INTEGER NULL,
  bbox JSONB NULL,
  category VARCHAR(50) NULL,
  bucket VARCHAR(10) NOT NULL DEFAULT 'include',
  exclusion_reason VARCHAR(30) NULL,
  excluded_stage VARCHAR(4) NULL,
  original_verdict JSONB NULL,
  inpaint_status VARCHAR(10) NULL,
  inpaint_image_url VARCHAR(500) NULL,
  mask_image_url VARCHAR(500) NULL,
  residual_ratio NUMERIC(5,4) NULL,
  warning_badge VARCHAR(20) NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  image_key VARCHAR(500) NULL,
  render_image_key VARCHAR(500) NULL
);

CREATE TABLE master_language (
  code VARCHAR(8) NOT NULL,
  name VARCHAR(100) NOT NULL,
  release VARCHAR(10) NULL
);

CREATE TABLE text_block (
  id BIGINT NOT NULL,
  section_id BIGINT NOT NULL,
  block_order INTEGER NOT NULL,
  role VARCHAR(20) NOT NULL DEFAULT 'body',
  is_excluded BOOLEAN NULL,
  source_ko TEXT NULL,
  source_lines JSONB NULL,
  bbox JSONB NULL,
  style JSONB NULL,
  block_status VARCHAR(10) NOT NULL DEFAULT 'machine',
  trans_1 TEXT NULL,
  trans_2 TEXT NULL,
  origin_block_ids JSONB NULL,
  char_count INTEGER NULL,
  char_limit INTEGER NULL,
  overflow BOOLEAN NOT NULL DEFAULT false,
  compliance_flags JSONB NULL,
  ocr_confidence NUMERIC(5,4) NULL,
  auto_adjust JSONB NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  revision INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE seller (
  id BIGINT NOT NULL,
  cognito_sub VARCHAR(255) NOT NULL,
  email VARCHAR(255) NOT NULL,
  account_type VARCHAR(20) NOT NULL DEFAULT 'individual',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE category_mapping (
  id BIGINT NOT NULL,
  category_id BIGINT NOT NULL,
  internal_category VARCHAR(40) NOT NULL
);

CREATE TABLE job_async_task (
  id BIGINT NOT NULL,
  job_id BIGINT NOT NULL,
  task_type VARCHAR(20) NOT NULL,
  unit_type VARCHAR(20) NOT NULL,
  unit_id BIGINT NULL,
  status VARCHAR(10) NOT NULL DEFAULT 'pending',
  retry_count INTEGER NOT NULL DEFAULT 0,
  max_retry INTEGER NOT NULL DEFAULT 2,
  error_code VARCHAR(50) NULL,
  error_message TEXT NULL,
  started_at TIMESTAMPTZ NULL,
  finished_at TIMESTAMPTZ NULL,
  revision INTEGER NULL
);

CREATE TABLE master_regulatory_class (
  id BIGINT NOT NULL,
  country_code VARCHAR(8) NOT NULL,
  code VARCHAR(30) NOT NULL,
  name VARCHAR(100) NOT NULL,
  is_escape_hatch BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE module_spec (
  id BIGINT NOT NULL,
  channel_spec_id BIGINT NOT NULL,
  module_name VARCHAR(200) NOT NULL,
  slot_spec JSONB NULL,
  slot_count INTEGER NULL,
  usage_type VARCHAR(20) NULL,
  field_type VARCHAR(100) NULL,
  char_limit INTEGER NULL,
  recommended_count INTEGER NULL,
  baked_allowed BOOLEAN NOT NULL DEFAULT false,
  source_url VARCHAR(500) NULL,
  confirmed_date DATE NULL
);

CREATE TABLE font_map (
  id BIGINT NOT NULL,
  role VARCHAR(20) NOT NULL,
  target_lang VARCHAR(8) NULL,
  font_family VARCHAR(100) NOT NULL
);

CREATE TABLE glossary (
  id BIGINT NOT NULL,
  term_ko VARCHAR(200) NOT NULL,
  term_target VARCHAR(200) NOT NULL,
  target_lang VARCHAR(8) NOT NULL,
  internal_category VARCHAR(40) NOT NULL,
  enforcement VARCHAR(20) NOT NULL DEFAULT 'reference',
  example_sentence TEXT NULL
);

ALTER TABLE source_image ADD CONSTRAINT PK_SOURCE_IMAGE PRIMARY KEY (
  id
);

ALTER TABLE channel_spec ADD CONSTRAINT PK_CHANNEL_SPEC PRIMARY KEY (
  id
);

ALTER TABLE export_artifact ADD CONSTRAINT PK_EXPORT_ARTIFACT PRIMARY KEY (
  id
);

ALTER TABLE section_verdict ADD CONSTRAINT PK_SECTION_VERDICT PRIMARY KEY (
  id
);

ALTER TABLE brand ADD CONSTRAINT PK_BRAND PRIMARY KEY (
  id
);

ALTER TABLE brand_logo ADD CONSTRAINT PK_BRAND_LOGO PRIMARY KEY (
  id
);

ALTER TABLE deliverable_section ADD CONSTRAINT PK_DELIVERABLE_SECTION PRIMARY KEY (
  id
);

ALTER TABLE category_master ADD CONSTRAINT PK_CATEGORY_MASTER PRIMARY KEY (
  id
);

ALTER TABLE edit_signal ADD CONSTRAINT PK_EDIT_SIGNAL PRIMARY KEY (
  id
);

ALTER TABLE audit_log ADD CONSTRAINT PK_AUDIT_LOG PRIMARY KEY (
  id
);

ALTER TABLE job ADD CONSTRAINT PK_JOB PRIMARY KEY (
  id
);

ALTER TABLE consent ADD CONSTRAINT PK_CONSENT PRIMARY KEY (
  id
);

ALTER TABLE job_keyword ADD CONSTRAINT PK_JOB_KEYWORD PRIMARY KEY (
  id
);

ALTER TABLE master_data_version ADD CONSTRAINT PK_MASTER_DATA_VERSION PRIMARY KEY (
  id
);

ALTER TABLE deliverable ADD CONSTRAINT PK_DELIVERABLE PRIMARY KEY (
  id
);

ALTER TABLE event_log ADD CONSTRAINT PK_EVENT_LOG PRIMARY KEY (
  id
);

ALTER TABLE expression_dictionary_evidence ADD CONSTRAINT PK_EXPRESSION_DICTIONARY_EVIDENCE PRIMARY KEY (
  id
);

ALTER TABLE master_country ADD CONSTRAINT PK_MASTER_COUNTRY PRIMARY KEY (
  code
);

ALTER TABLE expression_dictionary ADD CONSTRAINT PK_EXPRESSION_DICTIONARY PRIMARY KEY (
  id
);

ALTER TABLE validation_rule ADD CONSTRAINT PK_VALIDATION_RULE PRIMARY KEY (
  id
);

ALTER TABLE section ADD CONSTRAINT PK_SECTION PRIMARY KEY (
  id
);

ALTER TABLE master_language ADD CONSTRAINT PK_MASTER_LANGUAGE PRIMARY KEY (
  code
);

ALTER TABLE text_block ADD CONSTRAINT PK_TEXT_BLOCK PRIMARY KEY (
  id
);

ALTER TABLE seller ADD CONSTRAINT PK_SELLER PRIMARY KEY (
  id
);

ALTER TABLE category_mapping ADD CONSTRAINT PK_CATEGORY_MAPPING PRIMARY KEY (
  id
);

ALTER TABLE job_async_task ADD CONSTRAINT PK_JOB_ASYNC_TASK PRIMARY KEY (
  id
);

ALTER TABLE master_regulatory_class ADD CONSTRAINT PK_MASTER_REGULATORY_CLASS PRIMARY KEY (
  id
);

ALTER TABLE module_spec ADD CONSTRAINT PK_MODULE_SPEC PRIMARY KEY (
  id
);

ALTER TABLE font_map ADD CONSTRAINT PK_FONT_MAP PRIMARY KEY (
  id
);

ALTER TABLE glossary ADD CONSTRAINT PK_GLOSSARY PRIMARY KEY (
  id
);

