# db-map.md — 함수·API 표현 ↔ DB 표현

> 근거: 개발계획 v3.6 · 스키마 계약 문서 v3.5(2026-09-17)
> 소유 범위: 함수·API 키와 DB 컬럼 이름의 대응, 워커의 임시 식별자 변환 규칙. [계약 0장]
> 정의하지 않음: 컬럼 타입·제약·인덱스 — **BE 정본 ERD 소유.** ERD 파일 경로·버전은 `BE 확인 필요`.
> 원칙: 함수 입출력 키와 DB 컬럼 이름은 서로 다를 수 있다. JSON 내부의 키는 별도 DB 컬럼을 뜻하지 않는다. [계약 0장]
> 2026-10-06 추가 합의: `[통합 Dn]`은 `integration-decisions.md`의 기록. 아래 대응의 합의와 마이그레이션 적용을 구분한다. 실제 컬럼 타입·제약의 레포 정본은 `migrations/versions/`이며 신규 실행/시도 연결·스냅샷 위치는 미정이다.

---

## 1. 명시적 매핑 [계약 0장]

| 함수·API 표현 | DB 표현 | 비고 |
|---|---|---|
| `review_status` | `section.bucket` 및 `section.excluded_stage` | 두 DB 표현과 대응. 구체 변환 규칙은 원본 계약에 없음 |
| `user_edited` | `text_block.block_status = 'edited'` | 확인 필요 신호가 아닌 결과 메타 [계약 7장] |
| `block_key` | 저장 시 `text_block.id`로 매핑 | 함수가 반환하는 임시 식별자 |
| `section_key` | 저장 시 `section.id`로 매핑 | 함수가 반환하는 임시 식별자 |

## 2. 임시 식별자 변환 [계약 0장, 2.5, 4.1]

- 함수는 `section_key` · `block_key` · `line_key` · `region_key` · `finding_key` 같은 임시 식별자로 결과를 반환한다.
- **워커가 저장 시** `section_key` → `section.id`, `block_key` → `text_block.id`로 변환한다.
- `content_findings.evidence_block_ids`는 저장 시 `text_block.id` 목록이 된다. 함수 반환 시점의 값은 임시 `block_key`다. [계약 4.1]
- `line_key` · `region_key` · `finding_key`는 JSON 내부 식별자다. 별도 DB 컬럼 매핑은 정의하지 않으며 JSON 내부 식별자로 유지한다. [계약 0장, 2.5, 4.1]
- `line_key` · `region_key`의 유일 범위는 섹션이다. 섹션 밖에서 참조할 때는 `section_key`와 쌍으로 쓴다. 임시 키는 해당 분석 실행에 묶이며 모르는 키는 저장 실패다. BE가 검증·변환·일관된 저장을 담당한다. [`open-questions.md` #38] [통합 D3]

## 3. 참조 색인 — 계약이 의존하는 DB 이름

> 이 색인은 계약 문서가 참조하는 DB 이름을 모은 참조용 목록이다. **실제 ERD의 테이블·컬럼명과 일치 여부는 `BE 확인 필요`**이며, 불일치 시 BE 정본 ERD를 기준으로 이 매핑을 갱신한다. 타입·제약·인덱스는 여기서 정의하지 않는다.

### 3.1 `source_image`

| 이름 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `upload_order` | 원본 간 순서. 업로드 순서 = 원본 순서 | [계약 1.2, 3.1] |

### 3.2 `section`

| 이름 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `id` | `section_key`의 저장 대상 | [계약 0장] |
| `section_order` | 원본 내 섹션 순서 | [계약 1.2] |
| `top_offset` | 소속 원본 이미지 기준 세로 시작 위치 | [계약 3.1] |
| `height` | 소속 원본 이미지 기준 높이 | [계약 3.1] |
| `bbox` | BE가 원본 기준 `{x: 0, y: top_offset, w: 원본 폭, h: height}`로 파생. section 범위는 top_offset·height가 기준이며 range 필드 미추가 | [통합 D3] |
| `bucket` | 사용자의 최종 포함·제외. 하류 작업 생성 조건 `'include'` | [계약 2.4, 4.2] |
| `excluded_stage` | `review_status`의 일부 | [계약 0장] |
| `content_findings` | 정책 적용 전 AI 판정 6필드. 최초 유효 결과 없음은 NULL, 실패한 재분석은 기존 결과 보존(이번 실행 성공 아님) [통합 D6] | [계약 4.1] |
| `original_verdict` | 현재 채택한 분석 실행의 사용자 조정 전 정책 결과. 사용자 변경으로 불변, 새 분석 채택 시 교체·이전 값 audit 보존 [통합 D6] | [계약 4.2] |
| `exclusion_reason` | 자동 제외 사유 코드. API enum은 `auto_regulatory` · `auto_channel` · `auto_local_irrelevant` · `user_manual` · `restored_by_user`. 현지부적합 `needs_fix`용 코드는 설명서 제안(`auto_local_needs_check`)이며 미확정(`open-questions.md` #7) | [설명서 §3] [`docs/openapi.yaml` `ExclusionReason`] |
| `residual_ratio` | 인페인팅 잔존율. MVP 필수 아님, `NULL` 허용 | [계약 5.2] |

### 3.3 `text_block`

| 이름 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `id` | `block_key`의 저장 대상 | [계약 0장] |
| `block_order` | 섹션 내 블록 순서 | [계약 1.2] |
| `source_ko` | 병합된 한국어 원문. 빈 문자열 블록·role·원시 영역은 보존하되 빈 블록을 의미 분류·번역·텍스트 조판에 사용하지 않음 | [계약 2장] [통합 D3] |
| `source_lines` | 원시 OCR 영역·줄 구성 JSON | [계약 2.5] |
| `bbox` | 섹션 로컬 원본 블록 영역 | [계약 2장, 3.1] |
| `role` | 역할 5종 | [계약 2.1] |
| `ocr_confidence` | 구성 영역 신뢰도 최솟값, 없으면 `NULL` | [계약 2장] |
| `is_product_label` | 제품 라벨 판정. `NULL`/`false`/`true` | [계약 2.2, 2.4] |
| `is_brand_logo` | 로고 판정. NULL은 미판정이며 라벨 true일 때는 비교 생략 완료. 정상 조합은 contract.md 2.4 | [계약 2.3, 2.4] [통합 D1] |
| `is_excluded` | 합의된 자동계산은 `is_product_label OR is_brand_logo`. 앱이 직접 쓰지 않음. 현재 0003의 IS TRUE식은 NULL을 false로 만들므로 후속 마이그레이션 대기. 반영 전 하류 큐 금지 | [계약 2.4] [통합 D1] |
| `style` | 처리 대상 블록의 색·크기·정렬. 초기 분석 시 `NULL`. JSON 구조 미정 | [계약 1.2, 2장] |
| `trans_1` | 번역문 | [계약 2장, 5.1] |
| `overflow` | 최신 렌더 기준 폭·높이 초과 여부 | [계약 6.2] |
| `auto_adjust` | 사용자 배치 영역 조정 JSON (`layout_bbox`) | [계약 6.1] |
| `compliance_flags` | 현재 revision에 유효한 확인 필요 근거 JSON | [계약 7.1] |
| `revision` | 번역문 또는 배치 영역 변경 시 증가 | [계약 6.3] |
| `block_status` | `'edited'`가 `user_edited`에 대응 | [계약 0장, 7장] |

### 3.4 `section_verdict`

| 이름 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `id` | `audit_log.detail`에 생성 목록으로 기록, `compliance_flags.section_verdict_id`가 참조 | [계약 4.2, 7.1] |
| `verdict_status` · `verdict_type` · `reason` · `basis_article` · `evidence_url` | 정책 적용 결과. 허용값·매핑은 정책 계약 소유(담당 미정, `open-questions.md` #7). 설명서 §3 · §4에 매핑안이 있고 `docs/openapi.yaml`에 `VerdictStatus` 5값 · `VerdictType` 6값(파생)이 있으나 대조 · 합의 전 | [계약 4.2] [설명서 §3 · §4] |
| `dictionary_id` | 실제 `expression_dictionary` 항목을 근거로 쓴 경우에만 기록 | [계약 4.2] |
| (`rewritable`) | 서비스 판정값과 사전 원값을 구분한다. 원값은 expression_dictionary.source_verdict_status에 보존하며 누락을 추정 복원하지 않음. section_verdict의 실제 필드/API 매핑은 #7·#48 후속 합의 | [통합 D5] |

finding별 복수 사전·근거·문자 구간은 audit_log.detail 대응 배열에 보존한다. section_verdict.reason은 사전 사유이며 AI 이유와 합치지 않는다. problem_text는 대표 문구 하나다. 단수 dictionary_id와 복수 사전 대응의 구체 관계, 대표 문구 선정은 미정이다. [통합 D6]

### 3.5 `deliverable_section`

| 이름 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `stack_offset` | 최종 출력에서 재배치된 위치 | [계약 3.3] |
| `order_no` | 최종 출력 순서 | [계약 3.3] |

### 3.6 `job_async_task`

| 이름 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `revision` | 작업 대상 블록의 revision. 완료 시 현재와 다르면 결과 미반영 | [계약 6.3] |
| `status` | 번역 실패 신호의 근거 | [계약 7장, 8장] |
| `error_code` · `error_message` | 함수 오류 반환의 기록 | [계약 8장] |
| `retry_count` · `max_retry` | 재시도 판단 입력 | [계약 8장] |

초기 분석 실행은 `task_type=ocr`, `unit_type=job` 행이다. 이는 N4 대표 task_type을 translate로 정한 것이 아니다. [통합 D6]

한 proceed의 대표 행은 `unit_type=job`이며 그 id가 N4 실행을 식별한다. 개별 작업은 대표 행을 참조하고 재시도는 새 개별 행으로 남긴다. 현재 유효 시도만 집계하며 한도는 누적한다. **대표/개별 task_type, 실행·시도 연결 컬럼, 생략 완료 저장/API 표현은 아직 미정**이다. revision은 N5 수정 충돌·번역문·배치용이며 실행 식별에 사용하지 않는다. 초기 분석 실행과 N4 실행의 연결도 구현 전 명세다. [통합 D2] [통합 D3]

### 3.7 기타 참조

| 테이블·컬럼 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `brand.name_ko` · `brand.name_en` | 로고 제외 비교 대상 브랜드명 | [계약 2.3] |
| `glossary.id` | 번역 입력 용어의 참조. `compliance_flags.glossary_id`가 참조 | [계약 5.1, 7.1] |
| (`enforcement`) | 번역 입력에 함께 전달되는 **값**. DB 컬럼 존재 여부는 계약만으로 확인 불가 — 컬럼으로 전제하지 않는다 | [계약 5.1] |
| `expression_dictionary` | BE가 DB 적재본으로 실행 묶음을 만들며 원본 ID(external_id)→내부 PK와 당시 내용을 고정한다. 결과 저장은 실행 묶음 기준이며 최신 DB 변경만으로 거절·재실행하지 않는다. AI는 사전 참조로 원본 ID를 반환한다. 필드별 공급·형식·변환은 #71의 구현 전 명세 | [통합 D5] |
| `expression_dictionary.source_verdict_status` | 원본 판정값. 서비스 verdict_status와 구분하며 없으면 추정 복원하지 않는다. AI 미지원 값은 공급 전 거부(적재 허용과 별개) | [통합 D5] |
| (실행 묶음·사유·근거 스냅샷) | 분석 실행에 버전·해시·당시 근거를 보존. 판정 관련 스냅샷은 audit_log.detail, 단수 정책 결과는 section_verdict. 실행 묶음 전체의 보존 위치·기간과 JSON 키는 미정. [통합 D6] 현재 사전 행 조회로 과거 문구를 바꾸지 않음. 원본 삭제 후에도 복사한 근거 보존, 식별 관계 훼손·스냅샷 부재는 저장 실패. FK 처리·보존 기간은 미정 | [통합 D5] |
| (`exclusion_context` · `keep_context`) | 현지부적합 사전 행의 "제외하는 맥락" · "제외하지 않는 맥락" 2열 — 설명서 §3의 신설 제안. BE는 `expression_dictionary`의 같은 이름 컬럼으로 설계 승인 완료(PR #44에 구현, RDS 미적용). DB 기반 실행 묶음 공급 원칙은 확정, 필드별 공급·변환 상세는 미정(#7 · #71) [통합 D5] | [설명서 §3] [migrations 0006(PR #44)] |
| (`alternative_expression`) | 번역에 쓰일 대체 문구 또는 값 치환용 템플릿. 자리표시자가 있는 값(RG-021 · RG-022)은 완성 문구가 아니다(#56). DB는 문자열, AI 사전 묶음은 문자열 배열. 구분자·빈 값·문장 내 구분자 규칙 합의 전에는 나누지 않으며 합의 후 BE가 묶음 생성 시 한 번 변환한다(#71). [통합 D5] 상품별 검증값 · 검증 근거 · 적용 조건 충족 정보의 저장 위치와 AI 전달 방식은 BE·AI 합의 필요(#70) | [규제사전 0930] [설명서 §4] [migrations 0001] [`pipeline/dictionary.py`] |
| `job.regulatory_class` | 상품 규제 분류(API enum `cosmetic` · `otc` · `combination` · `unknown`). 규제사전 조회 키 `(target_country, regulatory_class)`의 입력 후보. ③-1 · ③-1'에 어떻게 전달할지 미정(`open-questions.md` #47) | [설명서 §4] [`docs/openapi.yaml` `RegulatoryClass`] |
| `audit_log.detail` | 정책 적용 입력 finding·판정 ID·대응·정책 식별자/버전, 검사 범위, 사전 스냅샷·원본 상태·복수 근거, 당시 source_ko·문자 구간·matched_text, 이전 판정. 판정 행이 없어도 기록. 세부 JSON 키는 미정. 기존 영역 변경 이력도 유지 | [계약 4.2, 6.3] [통합 D6] [통합 D7] |
| `edit_signal.before_text` · `after_text` | 텍스트 수정 기록 | [계약 6.3] |
| `event_log.payload` | 실행에 쓴 모델·프롬프트·용어집·정책 버전과 캐시 키 | [계약 9장] |

기존 적재 매핑은 원본 `id`→`external_id`, `variant_expressions.ko`→`variant_ko`, `variant_expressions.en`→`forbidden_en`, 본문 `verified_at`→`confirmed_date`다. `rewritable`은 서비스 `verdict_status=regulated`로 적재하고 시트 원값을 `source_verdict_status`에 보존한다. 근거 행은 `expression_dictionary_evidence`에 있다. 이는 기존 적재 매핑이며 실행 묶음의 AI 필드별 인계 형식까지 확정한 것은 아니다(#71). [migrations 0001·0006] [통합 D5]

## 4. 갱신 규칙

- ERD가 확정되거나 바뀌면 3절 색인의 이름을 ERD 기준으로 맞추고, 대응이 바뀐 행은 1절 매핑표에 추가한다.
- 계약 문서(`contract.md` 또는 원본)가 새 DB 이름에 의존하게 되면 이 색인에 행을 더한다. 이 색인은 AI↔BE 계약 추적용이며 DB 전체의 허용 목록이 아니다 — 색인에 없는 이름의 사용을 막는 규칙은 두지 않는다.
- 이 문서는 이름 대응만 다룬다. 값의 의미·계산 규칙은 `contract.md`, 흐름은 `pipeline.md`.
