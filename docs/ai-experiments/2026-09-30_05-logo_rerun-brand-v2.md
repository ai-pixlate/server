# 2026-09-30 ⑤ 브랜드 자료 정정(`brand-meta-v2`) 후 재실행 · 결과 차이 검증

> 앞선 실측 기록 `2026-09-30_05-logo_run-v1.md`(`brand-meta-v1`)는 그대로 두고 이 기록을 더한다. 사용자 검수는 주요 7블록에 한정됐으며 전수 정확도 · 전체 파이프라인 완료를 뜻하지 않는다.

## 1. 질문

사용자 육안 검수(2026-09-30, `open-questions.md` #68)에서 확인한 GS-03 영문 브랜드명 `b.clinicx`를 브랜드 자료에 반영하면, 판정 코드 · 규칙 · ④ 결과를 바꾸지 않은 채 **예상한 2블록만** false → true로 바뀌는가. 재실행이 재현되고 기존 입력 · 결과가 보존되는가.

## 2. 표본

- 입력: `pipeline/samples/local/logo-input-v1/`(32원본 · 100섹션 · 948블록, v1 실측과 같음) + **`pipeline/samples/local/brand-meta-v2/`**(새 버전).
- 비교 기준: `pipeline/out/logo-run-v1_2026-09-30/`(`brand-meta-v1` 실측 1회차).

## 3. 조건

- 브랜드 자료 `brand-meta-v2`: `brand-meta-v1`을 바탕으로 GS-03 `name_en` `b.clinix` → `b.clinicx` 한 값만 바꿈(사용자 확인). 상품명 · 한글명 · 다른 그룹 · 원본 연결 · 값 상태 · `not_in_input` · `for_input`(logo-input-v1) 동일, `previous_version`(v1 `SHA256SUMS` `ab5ff003…199f`) · `changes` 추가. 파일 `brand_metadata.json` `242c64f4…82c1` · `README.md` · `SHA256SUMS`(자신의 SHA-256 `f9ccc6b4866b79f4f556465a6f893961bd1899e6d4a83b7fde5304013420c989`). v1은 변경 없음.
- 코드: 커밋 `33a7cc7`. `run.json` `git_dirty=true` — 추적 파일 변경은 문서 2개(`docs/ai/open-questions.md` · `docs/ai/status.md`, 사용자 검수 기록)뿐이며 `pipeline/` · 드라이버는 `33a7cc7`과 차이 없음(`git diff 33a7cc7 -- pipeline docs/ai-experiments/tools/logo_run_driver.py` 빈 결과). 추적되지 않은 테스트 · 검수 도구 파일은 실행 경로 밖.
- 설정 기본값, Python 3.12.10, Unicode 데이터 15.0.0. 모델 · 외부 API 호출 없음.
- 출력(Git 제외): `pipeline/out/logo-run-v2_2026-09-30/` · `pipeline/out/logo-run-v2_2026-09-30_rerun/`.

## 4. 결과

### 4.1 브랜드 자료 검사

| 검사 | 결과 |
|---|---|
| 값 차이(v1 ↔ v2 `products`) | 정확히 1건 — GS-03 `name_en` `b.clinix` → `b.clinicx`(정규화 `bclinicx`) |
| 원본 연결 · 값 상태 · `not_in_input` · `for_input` · `value_rules` | v1과 동일 |
| 파일 집합 · 해시 | `SHA256SUMS` 목록 2개 = 실제 파일(자신 제외), `sha256sum -c` 통과 |
| 입력본 연결 | `for_input.sha256sums_sha256` = logo-input-v1 `SHA256SUMS` 해시 `def7e41b…7070`. 드라이버 사전 검사 `link_check=ok` |

### 4.2 재실행

| 검사 | 결과 |
|---|---|
| 실행 | 두 회 모두 종료 코드 0 · `status=ok` · 섹션 100 ok · 실패 0 · 미처리 0 · 결과/기록 각 100개 · 임시 파일 0 |
| 집계 | 948블록 = 라벨 생략 232 · 빈 텍스트 46 · **일치 4** · **불일치 666**(비교 716) — 예상값과 같음 |
| 판정 대응 | 모든 블록에 판정 정확히 1개(`block_order` → `block_key` 순), ④ true ↔ `product_label` 일치 |
| 재현성 | v2 두 회의 `LogoResult` 100/100 · `logo_debug` 100/100 바이트 동일, `run.json`은 시각 · 소요 시간 · 경로 외 동일 |
| 입력 · 이전 결과 불변 | logo-input-v1 · brand-meta-v1 · brand-meta-v2 · v1 실측 두 폴더, 파일 1,691개 해시가 재실행 전후 동일 |

### 4.3 v1 실측 대비 블록별 차이

| 원본 / 섹션 / 블록 | v1(`brand-meta-v1`) | v2(`brand-meta-v2`) |
|---|---|---|
| GS-03_002 / sec_1_01 / blk_001 | false · `no_match` | **true · `exact_match`**(`name_en`) |
| GS-03_018 / sec_1_09 / blk_001 | false · `no_match` | **true · `exact_match`**(`name_en`) |

- 나머지 946블록의 값과 근거는 동일하다.
- 상세 기록 차이 구분: GS-03 36섹션 중 34섹션은 브랜드 값 · 정규화 값 · 브랜드 그룹 항목 지문만 다르고(판정 동일), 2섹션은 위 블록의 판정 · 근거 · 일치 이름과 건수가 다르다. GS-01 · GS-02 기록은 같다. `run.json`의 섹션 행은 위 2섹션(건수)만 다르고 입력 기록은 브랜드 버전 · 해시가 다르다.
- 사용자 확인 사례 유지: goodal true 2(GS-01_001/sec_1_01/blk_038 · GS-01_002/sec_1_24/blk_001) 그대로 true · 패키지 로고 GS-03_002/sec_1_05/blk_005는 ④ true 보호 · ⑤ null(`product_label`) 유지 · 병합 한계 2사례(GS-01_002/sec_1_23/blk_001 · GS-03_018/sec_1_08/blk_003)는 false · `no_match` 그대로(#69 수용).

## 5. 판단

채택 — 브랜드 자료 정정 결과가 예상과 일치(2블록만 변화)하고 재현 · 불변 검사를 통과했다. 판정 코드 · 규칙 변경 없음. 사용자 검수는 주요 사례 한정이며 전수 정확도 주장 아님.

## 6. 반영

`docs/ai/status.md` 1 · 2 · 3절(⑤ 현황 · 입력 자료 `brand-meta-v2`), `open-questions.md` #68(브랜드 정정 재실행 결과 추가, 2026-09-30). #69는 한계 수용 · 후속 보류 그대로. 고정 입력본 구성은 다음 작업.
