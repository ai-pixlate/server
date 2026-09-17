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
