-- 마스터 데이터 시드 (개발/테스트용)
-- master_country · master_language 는 PK가 code 라 ON CONFLICT 로 멱등 적재.

INSERT INTO master_country (code, name, has_regulatory_dictionary, release) VALUES
  ('US', 'United States', true,  '9월'),
  ('JP', 'Japan',         true,  '9월'),
  ('VN', 'Vietnam',       false, '12월')
ON CONFLICT (code) DO NOTHING;

INSERT INTO master_language (code, name, release) VALUES
  ('en', 'English',    '9월'),
  ('ja', 'Japanese',   '9월'),
  ('vi', 'Vietnamese', '12월')
ON CONFLICT (code) DO NOTHING;
