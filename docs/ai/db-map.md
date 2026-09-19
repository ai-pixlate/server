# db-map.md — 함수·API 표현 ↔ DB 표현

> 근거: 개발계획 v3.6 · 스키마 계약 문서 v3.5(2026-09-17)
> 소유 범위: 함수·API 키와 DB 컬럼 이름의 대응, 워커의 임시 식별자 변환 규칙. [계약 0장]
> 정의하지 않음: 컬럼 타입·제약·인덱스 — **BE 정본 ERD 소유.** ERD 파일 경로·버전은 `BE 확인 필요`.
> 원칙: 함수 입출력 키와 DB 컬럼 이름은 서로 다를 수 있다. JSON 내부의 키는 별도 DB 컬럼을 뜻하지 않는다. [계약 0장]

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
| `bucket` | 사용자의 최종 포함·제외. 하류 작업 생성 조건 `'include'` | [계약 2.4, 4.2] |
| `excluded_stage` | `review_status`의 일부 | [계약 0장] |
| `content_findings` | 정책 적용 전 AI 섹션 판정 결과 JSON. `NULL` = 판정 미완료 | [계약 4.1] |
| `original_verdict` | 되살린 섹션의 최초 판정 보존 | [계약 4.2] |
| `residual_ratio` | 인페인팅 잔존율. MVP 필수 아님, `NULL` 허용 | [계약 5.2] |

### 3.3 `text_block`

| 이름 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `id` | `block_key`의 저장 대상 | [계약 0장] |
| `block_order` | 섹션 내 블록 순서 | [계약 1.2] |
| `source_ko` | 병합된 한국어 원문 | [계약 2장] |
| `source_lines` | 원시 OCR 영역·줄 구성 JSON | [계약 2.5] |
| `bbox` | 섹션 로컬 원본 블록 영역 | [계약 2장, 3.1] |
| `role` | 역할 5종 | [계약 2.1] |
| `ocr_confidence` | 구성 영역 신뢰도 최솟값, 없으면 `NULL` | [계약 2장] |
| `is_product_label` | 제품 라벨 판정. `NULL`/`false`/`true` | [계약 2.2, 2.4] |
| `is_brand_logo` | 로고 판정. `NULL`/`false`/`true` | [계약 2.3, 2.4] |
| `is_excluded` | DB 자동계산 `is_product_label OR is_brand_logo`. 앱이 기록하지 않음 | [계약 2.4] |
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
| `verdict_status` · `verdict_type` · `reason` · `basis_article` · `evidence_url` | 정책 적용 결과. 허용값·매핑은 정책 계약 소유(담당 미정) | [계약 4.2] |
| `dictionary_id` | 실제 `expression_dictionary` 항목을 근거로 쓴 경우에만 기록 | [계약 4.2] |

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

### 3.7 기타 참조

| 테이블·컬럼 | 계약이 기대하는 의미 | 절 |
|---|---|---|
| `brand.name_ko` · `brand.name_en` | 로고 제외 비교 대상 브랜드명 | [계약 2.3] |
| `glossary.id` | 번역 입력 용어의 참조. `compliance_flags.glossary_id`가 참조 | [계약 5.1, 7.1] |
| (`enforcement`) | 번역 입력에 함께 전달되는 **값**. DB 컬럼 존재 여부는 계약만으로 확인 불가 — 컬럼으로 전제하지 않는다 | [계약 5.1] |
| `expression_dictionary` | `section_verdict.dictionary_id` · `compliance_flags.dictionary_id`의 참조 대상 | [계약 4.2, 7.1] |
| `audit_log.detail` | 정책 적용 실행 정보, 영역 변경 전후 | [계약 4.2, 6.3] |
| `edit_signal.before_text` · `after_text` | 텍스트 수정 기록 | [계약 6.3] |
| `event_log.payload` | 실행에 쓴 모델·프롬프트·용어집·정책 버전과 캐시 키 | [계약 9장] |

## 4. 갱신 규칙

- ERD가 확정되거나 바뀌면 3절 색인의 이름을 ERD 기준으로 맞추고, 대응이 바뀐 행은 1절 매핑표에 추가한다.
- 계약 문서(`contract.md` 또는 원본)가 새 DB 이름에 의존하게 되면 이 색인에 행을 더한다. 이 색인은 AI↔BE 계약 추적용이며 DB 전체의 허용 목록이 아니다 — 색인에 없는 이름의 사용을 막는 규칙은 두지 않는다.
- 이 문서는 이름 대응만 다룬다. 값의 의미·계산 규칙은 `contract.md`, 흐름은 `pipeline.md`.
