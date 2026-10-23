-- 마스터 데이터 시드 (개발/테스트용) — 9월 MVP 기준
-- 타겟 국가 = 미국(US), 도착 언어 = 영어(en). (한국은 원본 출발지라 타겟 아님)
-- master_country · master_language 는 PK가 code 라 ON CONFLICT 로 멱등 적재.
-- 12월 확장 국가/언어는 추후 release='12월'로 추가.

INSERT INTO master_country (code, name, has_regulatory_dictionary, release) VALUES
  ('US', 'United States', true, '9월')
ON CONFLICT (code) DO NOTHING;

INSERT INTO master_language (code, name, release) VALUES
  ('en', 'English', '9월')
ON CONFLICT (code) DO NOTHING;

-- MST-03 규제 분류 (미국) — 참조 데이터(플레이스홀더)
INSERT INTO master_regulatory_class (id, country_code, code, name, is_escape_hatch) VALUES
  (1, 'US', 'cosmetic', '화장품', false),
  (2, 'US', 'otc',      '일반의약품', false)
ON CONFLICT (id) DO NOTHING;

-- MST-04 카테고리 트리 (Face > Serum, Lip Care)
INSERT INTO category_master (id, parent_id, name, is_leaf) VALUES
  (1, NULL, 'Face',     false),
  (2, 1,    'Serum',    true),
  (3, NULL, 'Lip Care', true)
ON CONFLICT (id) DO NOTHING;

-- MST-05 규격(채널)
INSERT INTO channel_spec (id, code, name, spec_type, is_active, release) VALUES
  (1, 'original', '원본 규격', 'original', true, '9월'),
  (2, 'coupang',  '쿠팡 상세', 'site',     true, '9월')
ON CONFLICT (id) DO NOTHING;

-- MST-06 규격 종속 모듈(업로드 안내용)
INSERT INTO module_spec (id, channel_spec_id, module_name, usage_type, char_limit, recommended_count, baked_allowed) VALUES
  (1, 1, '상세 메인', 'detail', 50, 10, false)
ON CONFLICT (id) DO NOTHING;

-- MST-07 마스터 버전
INSERT INTO master_data_version (id, data_type, version, is_current) VALUES
  (1, 'master_country',  'v3.4.2', true),
  (2, 'master_language', 'v3.4.2', true),
  (3, 'category_master', 'v3.4.2', true),
  (4, 'channel_spec',    'v3.4.2', true)
ON CONFLICT (id) DO NOTHING;
