# contract.md — AI 파이프라인 입출력·계산 규칙 (요약본)

> 근거: 개발계획 v3.6 · 스키마 계약 문서 v3.5(2026-09-17)
> 성격: **원본 의미를 보존한 요약본.** 원본을 축약·재구성했으며 설명·배경은 뺐다. 원본과 다르게 읽히면 원본 스키마 계약 문서가 우선한다.
> 소유 범위: 모델이 산출하는 값 · 함수의 경계 · JSON 내부 구조 · 좌표계 · 제외 조건 · 캐시와 재처리 규칙 [계약 0장]. 서비스 흐름은 `pipeline.md`, 컬럼 타입·제약·인덱스는 BE 정본 ERD, 함수 키↔DB 컬럼 매핑은 `db-map.md`.
> 표기: `BE 확인 필요` — 원본에 "API가 소유"로 적힌 항목. `미정` — 원본에 없거나 "별도 정의"로 남은 항목. 어느 쪽도 값을 여기서 정하지 않는다.
> 함수·API 키와 DB 컬럼 이름은 서로 다를 수 있다. 워커가 임시 식별자를 DB 식별자로 변환해 저장한다. JSON 내부 키는 별도 DB 컬럼을 뜻하지 않는다. [계약 0장]

---

## 1. 함수 경계

실행 순서는 `pipeline.md` 1절. 여기서는 각 함수·단계가 어느 구간을 담당하는지와 경계만 정한다.

| 함수·단계 | 담당 구간 (`pipeline.md` 번호) | 절 |
|---|---|---|
| `analyze()` | ① 섹션 분해 · ② 텍스트 추출 · ③ 병합·역할 분류 | 1.1 |
| AI 섹션 판정 | ③-1 | 1.2 · 4.1 |
| 정책 적용 | ③-1' | 1.2 · 4.2 |
| 제품 라벨 판정 · 브랜드 로고 제외 | ④ · ⑤ (섹션 확인 이후) | 1.3 · 2.2 · 2.3 |
| 스타일 추출 · 인페인팅 · 번역 | ⑦ · ⑥ · ⑧ (처리 대상 확정 이후, 병렬 가능) | 1.3 · 5장 |
| 재검증 | 검수 중 번역문 수정 시 | 5.1 · 7.2 |

함수 이름은 `analyze()` 외에는 원본 계약에 없다 — 나머지는 단계 이름으로 부른다.

### 1.1 `analyze()` — 초기 분석 [계약 1.2]

수행: 섹션 분해 → 섹션별 OCR → 줄·문단 병합 및 역할 분류.

- LLM 병합 보정은 **추가 병합과 역할 재판정만** 수행하며 블록 분할은 하지 않는다.
- 수행하지 않음: 스타일 추출, AI 섹션 판정, 정책 적용, 제품 라벨 판정, 브랜드 로고 제외.
- `text_block.style`은 초기 분석 시 `NULL`.
- 입력 파일은 워커가 준비한 **로컬 경로**로 전달한다. 함수는 S3에 직접 접근하지 않는다.

출력:

| 단위 | 출력값 |
|---|---|
| 섹션 | `section_key`, `source_image_id`, `section_order`, `top_offset`, `height` — `range`는 원본 계약에 없음. 외부 계약값인지 `top_offset`·`height`에서 계산하는 내부값인지 **미정**(10장) |
| 텍스트 블록 | `block_key`, 소속 `section_key`, `source_ko`, `source_lines`, `bbox`, `role`, `ocr_confidence` |

순서 관리: 원본 간 `source_image.upload_order` · 원본 내 섹션 `section.section_order` · 섹션 내 블록 `text_block.block_order`.

### 1.2 AI 섹션 판정과 정책 적용 [계약 1.3]

- AI 섹션 판정 입력: 섹션 이미지 + 텍스트 블록(+ 필요 시 앞뒤 섹션 텍스트). **텍스트가 없는 섹션도 이미지 기반 판정 대상.**
- AI 섹션 판정 출력: 콘텐츠 특성과 확인 필요 여부(→ 4.1 `content_findings`).
- 정책 적용: 판정 결과 × 정책 데이터 → 유지·제외 권고와 확인 필요 여부(→ 4.2 `section_verdict`).
- 두 단계의 입력과 결과는 구분해 관리한다. **정책 변경만으로 AI 추론을 다시 실행하지 않는다.**

### 1.3 섹션 확인 이후 [계약 1.1, 1.4]

- 제품 라벨 판정 이후의 처리는 **포함으로 확정된 섹션**에 대해서만 수행한다.
- 라벨 또는 로고로 판정된 블록은 스타일 추출·번역·인페인팅·번역문 조판을 포함한 하류 처리에서 제외한다. 해당 원본 시각 요소는 유지한다.
- 제외 확정 섹션에는 제품 라벨 판정부터 하류 작업을 생성하지 않는다.
- 스타일 추출·인페인팅·번역은 처리 대상 확정 후, 입력이 준비되면 병렬 실행할 수 있다.

## 2. 텍스트 블록 계약 [계약 2장]

| 필드 | 정의 |
|---|---|
| `source_ko` | 병합된 블록의 한국어 원문 |
| `source_lines` | 원시 OCR 영역과 줄 구성 정보를 보존하는 JSON 배열 (2.5) |
| `bbox` | 섹션 로컬 좌표의 원본 블록 영역 `{x,y,w,h}` |
| `role` | `title` `body` `caption` `price` `caution` 중 하나 |
| `ocr_confidence` | 블록을 구성하는 OCR 영역 신뢰도의 **최솟값**. 영역이 없으면 `NULL` |
| `is_product_label` | 제품 용기·패키지에 인쇄된 텍스트 여부 |
| `is_brand_logo` | 브랜드명 전체 일치에 따른 로고 판정 여부 |
| `is_excluded` | DB가 두 판정 플래그의 OR로 자동계산 (2.4) |
| `style` | 처리 대상 블록의 글자색·배경색·글자 크기·정렬. **JSON 키 이름·값 형식·허용값은 미정**(10장) — `pipeline.md`의 `font_color` · `bg_color` · `est_font_px` · `align`은 PoC 산출 키이며 계약값 아님 |
| `trans_1` | 의미와 문맥을 보존한 번역문 |
| `overflow` | 현재 배치 영역의 폭 또는 높이 초과 여부 (6.2) |
| `auto_adjust` | 사용자 배치 영역 조정값 (6.1) |
| `compliance_flags` | 현재 revision에 유효한 확인 필요 신호의 근거 (7.1) |

### 2.1 역할 [계약 2.1]

`role`은 5종(`title` 제목 · `body` 본문 · `caption` 캡션 · `price` 가격 · `caution` 주의문구). NOT NULL, 허용값은 DB CHECK.
제품 라벨 여부는 역할과 독립. **제품명 전용 역할은 정의하지 않는다.**

### 2.2 제품 라벨 판정 [계약 2.2]

- 입력: 텍스트 블록 + 섹션 이미지.
- 제품 용기·패키지에 인쇄된 텍스트 → `is_product_label=true`.
- 라벨로 판정되더라도 원문·좌표·역할 등 분석 결과는 보존한다.

### 2.3 브랜드 로고 제외 [계약 2.3]

`text_block.source_ko` 전체와 `brand.name_ko`, `brand.name_en`을 정규화한 뒤 **완전 일치** 비교.

정규화 순서: ① NFKC → ② 소문자 → ③ 공백·문장부호 제거. 정규화 결과가 빈 문자열인 값은 비교 대상에서 제외.

- 전체 일치 → `is_brand_logo=true`. 비교가 정상 완료되고 불일치 → `false`.
- 부분 문자열 일치는 사용하지 않는다.
- 불일치 자체를 로고 매칭 실패 경고로 취급하지 않는다. OCR 실패는 인식 문제로 다룬다.
- 로고 이미지 매칭·심볼형 로고 인식은 MVP 이후.

### 2.4 미판정과 제외 조건 [계약 2.4]

두 판정 플래그: `NULL` 미판정 / `false` 해당 없음 / `true` 해당.

`is_excluded`는 자동계산이며 앱이 직접 기록하지 않는다.

```sql
is_product_label OR is_brand_logo
```

| 조건 | `is_excluded` |
|---|---|
| 하나라도 `true` | `true` |
| 둘 다 `false` | `false` |
| `true`가 없고 미판정이 남아 있음 | `NULL` |

**하류 작업 생성 조건 — 모두 만족해야 함:**

1. 섹션 확인 단계가 완료됨 — 작업 진행 상태로 판단. `section.bucket` 기본값만으로 완료를 판단하지 않는다.
2. `section.bucket = 'include'`
3. `text_block.is_excluded IS FALSE`

### 2.5 원시 OCR 정보 `source_lines` [계약 2.5]

```json
[
  {
    "line_key": "line_01",
    "text": "원문 예시",
    "bbox": {"x": 10, "y": 20, "w": 120, "h": 24},
    "regions": [
      {
        "region_key": "region_01",
        "text": "원문 예시",
        "score": 0.98,
        "poly": [[10,20],[130,20],[130,44],[10,44]],
        "bbox": {"x": 10, "y": 20, "w": 120, "h": 24}
      }
    ]
  }
]
```

- 모든 좌표는 섹션 로컬. 병합 후에도 원시 영역의 식별자·좌표·신뢰도를 보존한다.
- **인페인팅 대상 판정에는 블록 `ocr_confidence`가 아니라 원시 영역별 `score`를 사용한다.**
- 영역 `score`가 어떤 점수인지(인식 신뢰도 / 검출 신뢰도)는 상위 계약에 정의가 없다 — **미합의, BE 확인 필요**(`open-questions.md` #34). ② 구현은 현재 인식 신뢰도를 넣고 있으며(`dev.md` 3절, 구현값이지 계약 정의가 아님), 이 값은 `text_block.source_lines`로 저장되고 ⑥ 판정에 쓰이므로 정의는 BE와 합의해 정한다. ⑥의 신뢰도 0.5 기준이 검증됐다는 뜻은 아니다. [`open-questions.md` #34]

## 3. 섹션과 좌표계 [계약 3장]

### 3.1 좌표 기준

- `section.top_offset` · `section.height`는 소속 원본 이미지 기준.
- `text_block.bbox` · `source_lines` 내부 좌표는 섹션 로컬 기준.
- 원본 이미지 기준 세로 좌표 = `section.top_offset + y`.
- MVP 섹션은 원본 전체 폭을 쓰는 세로 구간.
- 원본이 여러 장이면 각 이미지의 좌표계는 독립. 원본 간 순서는 `source_image.upload_order`.

### 3.2 OCR 임시 분할

- 4,000px 초과 섹션은 OCR 실행을 위해 임시 분할할 수 있다.
- 임시 분할은 OCR 처리 단위이며 **의미 섹션으로 저장하지 않는다.**
- 타일 좌표는 원래 섹션 로컬 좌표로 복원하고, 중첩 영역의 중복 검출을 정리한 뒤 반환한다.

### 3.3 제외와 최종 배치

- 섹션 제외는 원본 좌표를 변경하지 않는다.
- 최종 출력에서 제외 섹션을 제거하고 포함 섹션을 재배치. 출력 위치·순서는 `deliverable_section.stack_offset` · `order_no`.
- 원본의 모든 섹션이 제외되면 해당 원본은 산출물에서 제외.

## 4. AI 섹션 판정과 정책 결과 [계약 4장]

### 4.1 `section.content_findings` — 정책 적용 전 AI 판정 결과

```json
{
  "schema_version": "1",
  "findings": [
    {
      "finding_key": "finding_01",
      "content_type": "before_after",
      "status": "uncertain",
      "evidence_block_ids": [101, 102],
      "evidence_source": "image_and_text",
      "reason": "비교 이미지가 있으나 전후 비교인지 추가 확인 필요"
    }
  ]
}
```

| 키 | 정의 |
|---|---|
| `schema_version` | JSON 구조 버전 |
| `finding_key` | 판정 결과 안에서 finding을 식별하는 키 |
| `content_type` | 판정 대상 콘텐츠 항목. 필드 존재는 확정, **값 목록·항목별 판정 기준은 미정(별도 정의 예정)**. `before_after`는 구조 설명용 예시 |
| `status` | `present` · `absent` · `uncertain` |
| `evidence_block_ids` | 근거 `text_block.id` 목록. 이미지에만 근거가 있으면 비어 있을 수 있음 |
| `evidence_source` | `image` · `text` · `image_and_text` |
| `reason` | 판정 사유 |

- 함수가 반환하는 임시 `block_key`는 저장 시 DB ID로 변환.
- `content_findings=NULL` = 판정 미완료. **항목 누락을 자동으로 `absent`로 해석하지 않는다.**
- 정책상 유지·제외 결정은 finding에 포함하지 않는다.

### 4.2 `section_verdict` — 정책 적용 결과

- 사용 필드: `verdict_status`, `verdict_type`, `reason`, `basis_article`, `evidence_url` 등. 허용값과 정책 결과의 매핑은 **정책 계약 소유 — 정책 계약의 담당은 미정**(10장).
- `dictionary_id`는 실제 `expression_dictionary` 항목을 근거로 쓴 경우에만 기록.
- 정책 적용 실행 정보는 `audit_log.detail`에 기록: 적용 정책 식별자·버전 / 입력 finding 스냅샷 / 생성한 `section_verdict.id` 목록 / verdict↔finding 대응.
- `section.original_verdict`는 되살린 섹션의 최초 판정 보존. 사용자의 최종 포함·제외는 `section.bucket`.
- 정책 데이터는 버전이 있는 별도 데이터. 정책 변경 시 저장된 finding으로 정책 적용 결과를 다시 계산.

## 5. 번역과 인페인팅 [계약 5장]

### 5.1 로컬라이징 번역

- 구조: `gemini-3.8-flash + RAG 용어집`.
- 입력 블록은 원본 순서와 섹션 문맥을 유지.
- 검색된 용어는 `glossary.id`, 원문·번역어, `enforcement`와 함께 전달.
- 실제 규제 매핑 데이터·RAG를 반영한 프롬프트는 기술검증 후 확정 — **미정**.
- `trans_1`은 원본 박스 폭에 맞추기 위해 강제로 축약하지 않는다. `char_limit`은 자동 축약 기준으로 쓰지 않는다.
- 미적용 강제 용어는 `compliance_flags`에 근거 기록.
- **번역문 수정 시 현재 문구에 대해 규제 표현·강제 용어 적용 여부를 다시 검증한다.**

### 5.2 인페인팅

- 마스크는 **처리 허용 블록에 속한 원시 OCR 영역**에서 생성. 영역별 `score`와 비어 있지 않은 텍스트 조건 적용.
- 제품 라벨·브랜드 로고 블록은 마스크 생성 대상에서 제외. 보호 영역을 침범하지 않도록 마스크 구성.
- 제외 섹션에는 인페인팅 작업을 생성하지 않는다.
- `section.residual_ratio`는 MVP 필수 산출값 아님, `NULL` 허용. 잔존율 측정·마스크 재팽창 재처리는 MVP 이후.
- 타임아웃 등 실행 실패 재시도는 별도 오류 처리 정책(8장).

## 6. 조판·렌더와 레이아웃 조정 [계약 6장]

### 6.1 원본 영역과 조정 영역

`text_block.bbox`는 원본 OCR 영역으로 보존. 사용자 조정 영역은 `text_block.auto_adjust`:

```json
{
  "schema_version": "1",
  "layout_bbox": {"x": 10, "y": 20, "w": 220, "h": 80},
  "updated_by": "user"
}
```

- `layout_bbox`는 섹션 로컬 좌표. 조정값이 없으면 원본 `bbox` 사용, 있으면 미리보기·최종 렌더 모두 같은 값 사용.
- JSON 갱신 시 담당하지 않는 키는 보존.
- MVP 배치 영역 조정은 섹션 내부에서만. 섹션 크기 변경은 포함하지 않는다.

### 6.2 overflow

- 현재 배치 영역에 대한 렌더 결과로 계산. 폭 또는 높이 중 하나라도 초과 → `true`.
- 초기값 `false`를 렌더 완료의 근거로 쓰지 않는다. **완료된 최신 렌더 결과에 대해서만** 표시.
- 조정 후에도 초과하면 검수 상태 유지. 자동 문구 축약은 하지 않는다.

### 6.3 revision

- 번역문 또는 배치 영역이 변경되면 `text_block.revision` 증가.
- 블록 단위 작업은 `job_async_task.revision`에 대상 블록 revision 기록. 완료 시 현재 revision과 다르면 **오래된 결과로 간주해 반영하지 않는다.**
- 여러 블록을 묶어 처리할 때는 블록별 revision을 작업 입력에 보존하고 결과 반영 시 각각 검증.
- 영역 변경 전후는 `audit_log.detail`, 텍스트 수정 기록은 `edit_signal.before_text` · `after_text`.

## 7. 확인 필요 신호와 재검증 [계약 7장]

확인 필요 신호는 저장된 근거를 조합한 API 파생값. **`SignalCode`는 API 소유 — BE 확인 필요.** AI는 근거만 저장한다.

| 신호 | 근거 |
|---|---|
| 규제 확인 필요 | `compliance_flags`, `section_verdict` |
| 강제 용어 미적용 | `compliance_flags` |
| 번역 실패 | 현재 관련 `job_async_task` 상태 |
| 인식 확인 필요 | `source_ko`, OCR 신뢰도 및 분석 경고 |
| 폭·높이 초과 | 최신 렌더의 `overflow` |
| 제품 라벨 판정 | `is_product_label` |
| 섹션 판정 | `section_verdict`, `original_verdict` |

- 관리자용 신호는 사용자 응답에서 제외. `user_edited`는 신호가 아닌 결과 메타(`text_block.block_status` 근거).
- 로고 이미지 매칭 실패·인페인팅 잔존율 신호는 MVP 이후.

### 7.1 `compliance_flags`

```json
[
  {
    "type": "regulatory",
    "producer": "translation_validation",
    "revision": 3,
    "dictionary_id": 31,
    "section_verdict_id": 201,
    "problem_text": "검증 대상 표현",
    "detected_in": "translation",
    "detected_by": "rule"
  },
  {
    "type": "mandatory_term_unapplied",
    "producer": "glossary_validation",
    "revision": 3,
    "glossary_id": 12
  }
]
```

- `type` · `producer` · `revision` 필수. `type`은 API 신호 코드와 대응.
- 근거 ID는 실제 대응 데이터가 있을 때만 기록.
- `detected_in`: `source` | `translation`. `detected_by`: `rule` | `llm`.
- 사유·조문·대체표현을 `section_verdict`에서 조회할 수 있으면 중복 저장하지 않는다.

### 7.2 재검증 규칙

- 재검증 단계는 **자신이 관리하는 항목만 교체**하고 다른 단계의 항목은 보존. 저장 시 현재 revision을 확인해 동시 갱신으로 유실되지 않게 한다.
- 오래된 revision의 신호는 현재 경고로 표시하지 않는다.
- 입력 변경이 해당 검증에 영향을 주지 않으면 유효성 확인 후 현재 revision으로 승계 가능.
- **검증이 필요한데 완료되지 않은 상태는 "문제 없음"으로 표시하지 않는다.**

## 8. 실패 처리 [계약 8장]

초기 분석 함수 오류 반환 형태:

```text
AnalyzeError(code, retryable, message, source_image_id)
```

`retryable`은 워커가 소비하는 함수 출력값. 워커 기록 필드: `job_async_task.error_code` · `error_message` · `status` · `retry_count` · `max_retry`. 재시도는 `retryable` + 오류 코드별 실행 정책 + 남은 허용 횟수를 함께 확인.

| 상황 | 오류 코드 | 재시도 |
|---|---|---|
| 깨진 파일·미지원 포맷 | `IMAGE_OPEN_FAILED` | 불가 |
| OCR 타임아웃·일시 오류 | `OCR_FAILED` | 허용 횟수 내 가능 |
| 텍스트 0개 검출 | 오류 아님 | 해당 없음 |

- 텍스트가 없는 섹션은 빈 `text_blocks`를 반환.
- 원본 전체에서 텍스트가 검출되지 않으면 분석 경고 기록.
- ① 섹션 분해의 VLM 호출 실패는 원본 계약에 없음. 대체 처리 허용 여부 · 오류·경고 코드 · 재시도 조건 **미정**(10장, `open-questions.md` #25).

## 9. 캐시와 결과 갱신 [계약 9장]

캐시는 단계별 입력과 실행 설정을 기준으로 관리.

| 상황 | 처리 |
|---|---|
| 섹션 포함·제외 토글 | 분석 결과 보존 |
| 제외 섹션 재포함 | 유효한 결과 재사용, 미실행 하류 단계는 최초 실행 |
| 정책 변경 | AI 섹션 판정 결과 재사용, 정책 결과 재계산 |
| 번역문 수정 | 규제·용어 검증 **및** 렌더 갱신 |
| 배치 영역 수정 | 렌더 갱신 |
| 라벨·로고 판정 변경 | 처리 대상 재계산, 영향받는 스타일·인페인팅·번역·렌더 결과 갱신. **실행 중인 기존 작업의 오래된 결과를 막는 방식(revision 증가 · 별도 버전 · 작업 취소)은 미정**(10장) |
| 입력·모델·프롬프트·용어집·판정 항목 정의 변경 | 관련 단계 캐시 유효성 재판단 |

- 실행에 사용한 모델·프롬프트·용어집·정책 버전 및 캐시 키는 `event_log.payload`에 기록.
- 최종 저장에는 현재 포함 상태·번역문·배치 영역·revision에 대응하는 결과만 사용.

## 10. 이 문서 밖

- 함수 키↔DB 컬럼 매핑 → `db-map.md`
- 미확정 사항 → `open-questions.md`. 원본 10.2 항목(제품명/효능 구분, 판정 항목 목록·경계, 규제·채널 정책 데이터, `section_verdict` 허용값 매핑, RAG 프롬프트) [계약 10장]에 더해 이 요약에서 드러난 **계약 공백 6건**:
  - 섹션 `range`가 외부 계약값인지 내부 계산값인지 (1.1)
  - `text_block.style` JSON 키·값 형식·허용값 (2장)
  - 출력 분할용 절단 가능 지점(섹션 경계·블록 여백 좌표)의 함수·필드·JSON 구조 — 개발계획 3.1에 AI 제공으로 되어 있으나 원본 계약에 없음
  - 라벨·로고 판정 변경 시 stale 작업 방지 방식 (9장)
  - 정책 계약(`section_verdict` 매핑)의 담당 (4.2)
  - ① 섹션 분해 VLM 호출 실패의 대체 처리 허용 여부 · 오류·경고 코드 · 재시도 조건 (8장)
- MVP 이후 범위 → `open-questions.md` [계약 10.1]
