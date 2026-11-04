# Pixate Backend API — OpenAPI 3.1 + Swagger UI

Pixate 백엔드 API 명세(`openapi.yaml`)와 이를 로컬에서 바로 여는 Swagger UI(`index.html`)입니다.

- **정본 근거**: `API설계_Inventory_데이터흐름_v3.4.2`(엔드포인트·요청/응답의 최우선 기준) · `오케스트레이션 v0.3`(비동기·상태·에러·신호) · `CHECK_enum_허용값_v3.4.1`(enum) · `ERD_PIX_ateV5.sql`·`FK감사_V5대조_v3.4.2`(스키마 대조) · `PRD v3.4.2`(경계) · `DB워크북 v342`(대조).
- **문서에 없는 엔드포인트·필드는 추가하지 않음.** 12월·조건부의 미확정 계약은 최소 스키마 + `TODO` 주석으로 둠.

---

## 1. 실행법 (로컬)

`openapi.yaml`을 `index.html`이 `./openapi.yaml`로 로드하므로 **정적 서버**로 이 `docs/` 폴더를 띄우면 됩니다. (`file://`로 직접 열면 CORS로 스펙 로드가 막힙니다 — 반드시 HTTP 서버 사용.)

```bash
# 방법 A — Node
npx http-server docs -p 8080 -o

# 방법 B — Python
python -m http.server 8080 --directory docs
```

브라우저에서 **http://localhost:8080/** (또는 `/index.html`) 접속 → Swagger UI 렌더.
Swagger UI 자산은 `swagger-ui-dist@5.17.14` CDN(jsDelivr)에서 로드합니다(인터넷 필요).

---

## 2. 문서 작업 흐름

API 명세(`docs/openapi.yaml`)는 서버 코드와 분리해 **`feature/api-tracking` 브랜치**에서 관리합니다.
여기에 머지되면 문서 사이트(https://ai-pixlate.github.io/server/)가 자동으로 갱신됩니다.

| 브랜치 | 역할 |
| --- | --- |
| `feature/api-tracking` | **API 문서 전용 브랜치.** 머지되면 문서 사이트가 자동 배포됩니다. |
| `docs/*` | 문서 작업 브랜치. `feature/api-tracking` 으로 PR 을 올립니다. |

```bash
git switch feature/api-tracking
git pull
git switch -c docs/add-xxx-endpoint

# docs/openapi.yaml 수정 후
git add docs/openapi.yaml
git commit -m "docs(api): XXX 엔드포인트 추가"
git push -u origin docs/add-xxx-endpoint
```

이후 GitHub 에서 `feature/api-tracking` 대상으로 PR 을 올리면 자동 검증이 돌고,
어떤 엔드포인트가 추가/삭제됐는지 PR 코멘트로 요약됩니다.

### 자동화 (GitHub Actions)

| 워크플로 | 트리거 | 하는 일 |
| --- | --- | --- |
| [`api-docs-validate.yml`](../.github/workflows/api-docs-validate.yml) | `feature/api-tracking` 대상 PR / push | YAML 파싱 · Redocly lint · 추적 필드(`x-feature-id`, `x-release`) 점검 · PR 에 변경 엔드포인트 요약 코멘트 |
| [`deploy-api-docs.yml`](../.github/workflows/deploy-api-docs.yml) | `feature/api-tracking` push (`docs/**`) | Swagger UI 를 GitHub Pages 로 배포 |

수동 배포가 필요하면 Actions → Deploy API Docs → Run workflow → Branch `feature/api-tracking` 으로 실행합니다.

### 최초 1회 설정 (관리자)

1. **Settings → Pages → Source** 를 `GitHub Actions` 로 지정
2. **Settings → Environments → github-pages → Deployment branches** 에 `feature/api-tracking` 추가
3. (권장) **Settings → Branches → Add rule** 로 `develop` 보호 — PR 필수 + `OpenAPI 검증` 체크 통과 필수

### 추적 규약

각 오퍼레이션에 아래 확장 필드를 붙여 릴리스와 근거를 추적합니다.

- `x-feature-id` — 요구사항정의서 기능ID (예: `F-SRC-10`)
- `x-release` — `9월` / `9월 should` / `12월` / `이후`
- `x-priority` — `P0`~`P3`
- `x-mvp` — MVP 포함 여부
- `x-stub` — 향후 활성 예정(미구현) 스텁 여부

`x-feature-id` 또는 `x-release` 가 빠지면 PR 검증에서 경고가 표시됩니다.

---

## 3. 스펙 개요

- **OpenAPI**: 3.1.0 · 단일 파일 `openapi.yaml`
- **Base-path**: 경로는 접두어 없는 상대경로. 호스트·버전은 `servers`에서만: `https://api.pixate.example.com/v1`
- **인증**: `bearerAuth`(JWT access) 전역. refresh는 httpOnly 쿠키(`/auth/refresh`·`/auth/logout`, + CSRF 헤더 `X-Requested-With`). `/admin/**`은 역할 권한(403). 남의 리소스는 404.
- **Error**: 전 엔드포인트 공용 `Error` 래퍼 `{ error: { code, message, retryable, details?, traceId } }`.
  - `code`는 근거 문서 확인분 **6종**(VALIDATION_ERROR·NOT_FOUND·INVALID_STATE·REVISION_CONFLICT·RETRY_NOT_ALLOWED·ALL_SECTIONS_EXCLUDED)만 명시. 전체 15종은 근거(회신문 종합 1-C-5)가 이번 배치에서 제외되어 `type: string`으로 열어두고 `TODO`로 표시(불완전 enum을 박으면 정상 응답이 스키마 위반이 되므로).
- **presigned URL**: 이미지·파일 URL은 응답 시 발급(만료 5분), DB엔 S3 키만. 대상: BRD-05/06 · SRC-01/02 · SEC(preview)·INP-01 · CFM-03 · FIN-02/05 · LIB-01.
- **멱등·동시성**: `POST /jobs`는 `Idempotency-Key` 헤더 · `PATCH /jobs/{jobId}/blocks/{blockId}`는 `revision` 낙관적 잠금(409 REVISION_CONFLICT).
- **비동기 202 + taskId**: `/analyze`·`/sections/proceed`·`/render`·`/resume`·`/export/reupload-csv`·`PATCH /blocks`는 taskId 반환, 진행은 `GET /jobs/{jobId}/tasks` 폴링(2초).
- **userFacingStatus**: 영문 enum 8종(`draft·analyzing·section_review·translating·reviewing·done·failed·archived`). 한글은 FE 표시용이며 API 값 아님.
- **파생 필드**(응답 전용·미저장): `verdictType`(배지 6종 파생)·`displayTop`·`scale`·`previewHeight`·`maxOriginalWidth`·`signals[]`·`uiStatus`·`stages[]`.

### 릴리스 스코프
| 마커 | 의미 | 스펙 반영 |
|---|---|---|
| 🟢 9월 | MVP must | 요청/응답 전체 |
| 🟡 9월(should) | FIN-07 CSV 재업로드 | 전체 |
| 🔵 12월 | 로드맵 | 경로·요약 포함, 상세는 최소 스키마 + `TODO` |
| 📌 조건부 | JOB-08 resume (PM 확정 대기) | 포함, 최소 스키마 + `TODO` |

---

## 4. 오퍼레이션 개수 대조 (인벤토리 ↔ 스펙)

인벤토리 표기 = **59 오퍼레이션**(v3.4.0 57 + BRD-05 재설계 +2) · 🟢9월 46 · 🟡should 1 · 🔵12월 12 · 📌조건부 1(resume · **개수 미합산**).

| 도메인 | 인벤토리(API-*) | 스펙 | 일치 |
|---|---|---|---|
| AUTH | 6 | 6 | ✅ |
| BRD | 7 | 7 | ✅ |
| MST | 7 | 7 | ✅ |
| JOB | 8 (JOB-01~08) | 8 | ✅ |
| SRC | 4 | 4 | ✅ |
| ANL | 1 | 1 | ✅ |
| SEC | 4 | 4 | ✅ |
| INP | 2 | 2 | ✅ |
| CFM | 4 | 4 | ✅ |
| FIN | 7 | 7 | ✅ |
| LIB | 3 | 3 | ✅ |
| ADM | 7 | 7 | ✅ |
| **소계 (API-* 행)** | **60** | **60** | ✅ |
| 조건부 추가 `PATCH /jobs` | (인벤토리 §14) | 1 | 태스크 지침대로 포함 |
| **스펙 총 operations** | — | **61** | — |

- **59 = 60(API-* 행) − JOB-08 resume(📌·개수 미합산)**. 인벤토리 합계 규칙과 동일.
- **스펙 총 61 = 60 + `PATCH /jobs`**(태스크 지침: "조건부(JOB-08 resume, PATCH /jobs)를 스펙에 포함"). resume·PATCH /jobs 둘 다 📌 최소 스키마 + `TODO`.
- 릴리스 마커 집계(스펙): 🟢46 · 🟡1 · 🔵12 · 📌2(resume + PATCH /jobs). 이후 `PATCH /jobs/{jobId}`(jobPatch)는 draft-first 로 확정돼 🟢9월로 바뀌었다(2026-10-14 BE 구현).

---

## 5. 검증 결과 (redocly lint)

```
Woohoo! Your API description is valid. 🎉  — 0 errors
```

남은 **경고 1건(의도적)**:
| 경고 | 사유 |
|---|---|
| `no-server-example.com` (servers.url이 example.com) | 정본 규칙이 `https://api.pixate.example.com/v1`를 명시. 실제 호스트 확정 시 `servers`만 교체. **의도된 placeholder라 유지.** |

> `info-license`·`operation-4xx-response`·`no-unused-components` 경고는 license(Proprietary) 추가·모든 오퍼레이션 4xx 응답 보강·미사용 `ArtifactType` 제거로 해소함.

로컬 재검증:
```bash
npx @redocly/cli@1 lint docs/openapi.yaml
```

---

## 6. 미확정(TODO) 요약
스펙 내 `TODO` 주석으로 표시된 미확정 항목:
- **Error code 전체 15종** — 근거(회신문 종합 1-C-5) 확정 시 `Error.code`를 enum으로 고정.
- **🔵12월 스텁**: AUTH-06 · JOB-01 · INP-02 · LIB-02 · LIB-03 · ADM-01~07 — 상세 Request/Response 12월 확정.
- **📌조건부**: JOB-08 `resume` — PM 확정 후 계약 채움. (`PATCH /jobs/{jobId}` 는 jobPatch 로 확정·구현됨)
- **verify 노드 분리**(N2 판정 vs N4 규제검증) — 오케스트레이션 §15-1 미결(스펙엔 영향 없음).

---

## 7. 2차 통합(PRD v3.5.10) 반영 현황

기준: PRD v3.5.10 · 실행계획 v3.5.10(2차 개발 10/28~11/5, 2차 통합 11/6) · PM·FE·AI 회신(2026-10-29~11-04). 서버 비교 기준은 `develop` f85b23e.

- **계약 확정** = PRD·PM 확정 답으로 정해진 규칙. 명세 설명에 `[PRD v3.5.10 · 계약 확정]`으로 표시한다.
- **구현 대기** = 계약은 정해졌지만 서버가 아직 따르지 않는다. 명세 표시에 `· 구현 대기`를 붙인다.
- **BE 계약안** = FE가 필드 설계를 BE에 맡겼거나 FE·AI가 BE에 구체안을 요청한 항목의 BE 설계. 명세에 스키마·예시로 넣고 `[PRD v3.5.10 · BE 계약안]`으로 표시하며, 확인할 담당자(FE·AI·PM 확인)를 함께 적는다. 합의 전이라 정본 확정값이 아니다.
- **협의 중** = 필드 이름·오류 코드·동시성 계약 등 합의 전 항목. 명세에 스키마로 넣지 않는다.
- 이 표는 문서 반영 상태다. 서버 구현·배포 완료를 뜻하지 않는다.

### 7.1 반영한 규칙

| 항목 | 규칙 | 계약 | 구현 | 위치 |
|---|---|---|---|---|
| N3 포함 섹션 판정 | 목록(sectionList)도 포함 섹션의 판정 전부를 싣는다 | 확정 | 대기(현재 포함 섹션 `verdicts=[]`) | `Section.verdicts` · sectionList |
| N3 원래 순서 | 두 배열을 합쳐 `sectionOrder`로 정렬 | 확정 | 현행(필드 있음) | `Section.sectionOrder` |
| 섹션 복원 의미 | 직접 제외의 복원 = 제외 전 상태(원래 포함이면 사유 없음 — 새로운 「되살린 섹션」 배지를 붙이지 않으며 기존 판정·경고 배지는 보존), 자동 제외의 복원 = `restored_by_user`, 판정·근거·배지 보존 | 확정 | 대기(현재 항상 `restored_by_user`) | sectionPatch |
| 배치만 수정 | 「사용자수정」(`blockStatus=edited`)을 붙이지 않음 | 확정 | 대기(배치 저장은 BE 계약안 — 7.5) | `TextBlock.blockStatus` |
| 실행 취소·재실행 | 전용 API 없음 — 과거 값을 현재 revision으로 블록 PATCH에 다시 저장. 실제 값이 바뀌는 저장이 성공하면 revision 증가 | 확정 | 현행 API 사용(번역문) | blockPatch |
| 같은 값 저장 | 현재 값과 같은 값을 저장하면 revision 유지·새 재렌더 없음(`rerenderTaskId: null`) | 확정(FE) | 대기(현재 같은 값도 revision 증가·재렌더) | blockPatch · 예시 `sameValue`·`sameValueWhileRendering` |
| 저장 응답 `rerenderTaskId` | 항상 포함, `number \| null`(OpenAPI 필수 + nullable). 숫자 = 이 요청으로 만든 새 task, null = 이 요청으로 새 task를 만들지 않음. null을 no-op·렌더 완료·저장 실패 판정에 쓰지 않고, 이미 추적 중인 task는 계속 추적, done 뒤 블록·/preview 재조회. 번역문·박스·배율 Undo/Redo도 같은 규칙. `noOp` 필드 없음 | 확정(FE) | 현행은 키 항상 포함·null 가능. 같은 값 null은 대기 | `BlockPatchResponse` · 예시 `changed` |
| 재전송 응답의 화면 처리 | 멱등 재전송으로 최초 성공 응답을 다시 받으면 성공 확인으로만 쓴다. 과거 `block`으로 캐시를 덮지 않고 재조회로 동기화, 과거 task ID는 최신성 보장 아님. 응답 스키마 변경 없음 | 확정(FE — 멱등 재전송을 채택할 경우) | 화면 | blockPatch |
| 검사 중 확정 | 확정 대상 번역문에 필요한 규칙 검사가 진행 중이면 서버가 거절, 화면은 [저장하러 가기] 비활성(2차) | 확정 | 대기(검사 연결 전) | confirmReview |
| 미저장 번역문 확인창 | 저장되지 않은 번역문이 남아 있을 때 개수를 안내하는 확인창. 화면 동작이며 검사 중 거절·비활성을 해제하지 않는다 | 확정(PM) | 화면 | confirmReview |
| content.csv 배치 열 | `layout_x, layout_y, layout_w, layout_h, font_scale` 추가, 조정 안 한 항목은 빈칸 | 확정(PM) | 대기 | exportCreate |
| categoryId | leaf 항목만 허용 | 확정 | 대기 | JobCreate·JobPatch |
| g 상태 자동 재조회 | 5초 간격 최대 12회. 한도 도달 = 새 렌더 등록 미확인(실패 확정 아님), 자동 재조회를 멈추고 안내·수동 재조회 | 확정(FE 수용) | 화면 | reviewPreview |
| `/blocks.overflow` | 기존 호환을 위해 유지, 넘침 판정에는 쓰지 않음(판정은 `/preview` `render.result.overflow`) | 확정(FE) | 현행 필드 | `TextBlock.overflow` |
| 저장 처리 순서·오류 분류 | 처리 순서 1~9, 요청 형식 422 / 저장 상태 비교 400 / 상태 409, 멱등 키는 조작마다 UUID v4·재시도는 같은 키·결과 불명 동안 후속 조작 보류 | 확정(FE, 11-03) | 대기 | blockPatch 「저장 공통」 |
| 섹션 저장 | `{action, revision}` + 멱등 키, 같은 상태 200 no-op, 제외·복원 각각 Undo/Redo 한 단계, 저장 뒤 재조회 | 확정(FE, 11-03) | 대기 | sectionPatch |
| 블록 배치 구조·[+][−] 계산 | `layoutAdjust {box, fontScale}`·생략 유지·null 해제·소수 유지, s′ = s ± 0.05·박스 s′/s배(중심 기준·경계 보정·버튼 비활성·절대값 전송은 FE), 섹션 밖 400 | 확정(FE, 11-03) — 정규화는 조건부(7.5) | 대기 | blockPatch 「블록 배치」 |
| 검사 상태 5값·버튼 처리 | settled·in_progress·failed·missing·outdated, in_progress면 확정 비활성 | 확정(FE, 11-03) | 대기 | `BlockCheck` · confirmReview |
| 확인 요청 구조 | `{blockId, code, inputKey}`(translation_failed·check_failed·prohibited_expression 구분), `acknowledgedSections` | 구조 확정(FE, 11-03) — 키 기준·reason은 BE 계약안, 출력 범위는 PM | 대기 | ConfirmRequest |
| 렌더 박스 좌표 | 소수 그대로 유지, 1px 허용 오차 없음 | 확정(AI 원칙) | 대기(현재 `_render_inputs`가 정수로 자름) | `PreviewRenderBlock.box` · `LayoutBox` |
| 배율 미지정 | 적용값 1.0(`renderInput`의 `fontScale`은 null이 아님). 저장상의 null 정규화와는 별개 | 확정(AI·FE) | 대기 | `PreviewRenderBlock.style.fontScale` |
| [+][−]·옮기기 화면 규칙 | 증감량 0.05, 확대 기준점 = 박스 중심, 섹션 경계를 넘으면 크기를 유지한 채 안쪽으로 이동, 다음 확대가 섹션보다 커지면 [+] 비활성, Undo 이력 50단계, 옮길 때 섹션 가운데 세로선·가로선에 스냅. 서버는 받은 절대값만 저장(계산식은 7.5 F5) | 확정(PM — 검토 채팅이 원문 확인) | 화면 | blockPatch 「블록 배치」 |
| 2차 활성 검사 | 규칙 검사만. 번역문 LLM 규제 판정은 3차 | 확정(AI 회신) | 대기(검사 연결 전) | confirmReview 「검사 연결 경계」 |
| 이미지 경계 이탈(Q-07) — 좌표계 | 제외 반영 최종 결합 이미지. 최종 블록 y = 섹션 로컬 y + 제외 반영 출력 위치, `displayTop` 재사용 안 함. 섹션 입력이 같으면 섹션 이미지 재사용, 제외·복원 뒤 최종 합성·재판정(뒤 섹션 전체 재렌더 없음) | 확정(PM·AI 11-04) | 12월(상세 기능 — PM, 실행계획대로) | confirmReview 「이미지 경계 이탈」 · `BoundaryCheck`(계약안) |
| 이미지 경계 이탈(Q-07) — 확정 제한 | 현재 판정 진행 중 = 버튼 비활성·서버 거절 / 현재 이탈 = 차단(확인으로 못 풂) / 판정 없음·실패 = 마지막 완료 판정 사용 / 이전 이탈은 최신 판정에서 해소될 때만 해제. 섹션 렌더·판정이 실제로 대기·실행 중이면 진행 중, 렌더·입력 구성·판정 실패나 시도 없음은 판정 없음·실패(마지막 완료 판정)로 본다 — 지문 불일치만으로는 진행 중이 아니다 (BE 해석) | 확정(PM 11-04) — 2차 적용 범위·완료 판정이 없는 경우는 확인 중 | 대기 | confirmReview 「이미지 경계 이탈」 |
| 이미지 경계 이탈 — N6 이탈 시 [중단]→N5 복귀 | 렌더 실패와 같은 오류 → [중단] → 수정 보존 N5 복귀 | 정책 기록 · **3차 유지**(실행계획, 현행 중단 API는 N2·N4만) | 3차 | confirmReview 「이미지 경계 이탈」 |
| 멱등 세부 | 보관 24시간, 만료 뒤 새 요청, 잠금 대기 초과 503, `Idempotent-Replayed: true`, 결과 불명이면 같은 키·같은 본문 재시도와 그 대상의 후속 저장·Undo/Redo 보류 | 확정(FE 11-04) | 대기(CORS 노출 포함) | blockPatch 「저장 공통」 |
| 409 구분 | `error.code` + `details.reason` 조합(`Error.details` 목록), `check_not_settled`의 `registeredBlockIds` — 이후 새 사유는 따로 확인 | 확정(FE 11-04) | 대기 | `Error.details` |
| 입력 지문 비교 | A·B = 표·뷰어 일치, B·C = 미리보기 최신성, null 비교 불가, 블록이 없는 섹션은 `/preview` 안의 최신성만 | 확정(FE 11-04) | 대기 | blockList · `TextBlock.sectionInputFingerprint` |
| 배치·제외·복원과 검사 | 글자가 바뀌지 않은 박스·배율 변경과 섹션 제외·복원은 규제·강제 용어 검사를 다시 실행하지 않음(PM). 제외는 검사 대상 집합에서 빼고(결과 보존), 복원은 유효한 결과가 없을 때만 확보 — 확보는 재실행이 아니라는 정리는 BE 정리 | 확정(PM 11-04) | 대기 | confirmReview 「검사 연결 경계」 · sectionPatch |
| 렌더 입력 오류 | `STYLE_RESULT_INVALID` = AI 스타일 결과 위반 / `STYLE_DEFAULTS_UNAPPROVED` = 승인 스타일 설정·폰트·번들 자산 검증 실패 / `RENDER_INPUT_INVALID` = BE가 구성한 박스·배율·조정값 조합 오류와 잘못된 자산 참조 / 정상 입력 뒤 실행 실패 = 렌더 시도 실패 | 확정(AI 11-04) — 번들·폰트를 자산 검증 실패와 잘못된 참조로 나눈 기준은 BE 정리 | 대기 | `PreviewInputError` |
| 검사 `inputKey` 기준 | 규칙 = 블록 ID·원문·번역문·고정 사전·용어집 선별·규칙 버전 / LLM(3차) = + 섹션 문맥·모델·프롬프트·구현 버전·분할 한도 / 배치·배율·revision 제외 / 부분 채택 허용 아님 | 확정(AI 11-04) | 대기 | `BlockCheck.inputKey` |
| 조건부 경고 표시 | 공통 `prohibited_expression` + 판정 구분, 금지 「금지 표현」·조건부 「조건부 규제」(사전 `reason`의 허용 조건) | 확정(FE·PM 11-04) — 필드 `regulationVerdict`는 계약안 | 대기 | `ReviewSignal.regulationVerdict` |
| 미적용 강제 용어 | 확인 대상에서 제외(PRD F-CFM-09 I열), flag·신호 보존 | 확정(PRD · PM 재확인) | 대기 | ConfirmRequest |
| 실행 실패 섹션 출력 | 인페인팅 실패 = 원본 위에 번역문 / N4 렌더 실패 = N6에서 원본(다시 그리지 않고 새 렌더 실패로 세지 않음) / N6 = N5에서 확인한 모습 | 확정(PM 11-04) — `render_failed`를 성공 결과가 없는 섹션으로 한정한 것은 (BE 해석), 이전 성공 이미지가 있는 N5 재렌더 실패는 별도 | 대기 | ConfirmRequest |
| OTC 종류 | OTC·복합 → 화장품·잘 모름 = 삭제, OTC ↔ 복합 = 유지, 국가 변경 = 규제 분류·OTC 종류 재선택 | 확정(PM 11-04) | 대기(필드는 F 묶음 계약) | `JobPatch.regulatoryClass` |
| 임시 M020 | OTC·복합이고 종류가 없는 작업에 실제 저장(기존 작업 포함, 화장품·잘 모름 제외), 3차 필수 선택 도입 뒤 새 적용 종료·저장분 유지 | 확정(PM 11-04) — 기존 결과 유효성은 AI 확인 | 대기 | `JobPatch.regulatoryClass` |
| 브랜드 스냅샷 | 작업 생성 시점 값으로 고정, 번역·검사·재시도에서 이후 수정값을 섞지 않음 | 확정(PM 11-04) | 대기(현재 N3 [이대로 진행] 때 실행 범위에 고정) | jobCreate |
| 작업 생성 시점·동의 | 책임 동의 체크 뒤 N1 [다음]에서 `POST /jobs` → 업로드 → PATCH → analyze. `consentId`·`POST /consents` 미사용. `createdAt`은 지금은 작업 생성 시각이며, 동의 체크를 [다음]의 필수 조건으로 적용한 뒤부터 동의 시각으로도 쓴다(PM 정책) | 확정 | 서버 현행(미저장). 화면: client 8319dd6에는 동의 체크가 없고 고지 문구만 있어 위 전제가 아직 충족되지 않음 — 동의 화면은 3차(E 묶음) | jobCreate · consentCreate |

### 7.2 렌더러 전환 정책 (PM 확정 — 명세 반영은 세부 합의 뒤)

- **현재 유효한 정책(PM 11-04)**: ① 렌더러 전환 전에 대상·건수를 조사해 **테스트 작업을 취소로 정리할 수 있는지 확인하고**, 조사 결과에 따라 처리 방침을 결정한다(정리는 확정된 실행 방침이 아니다). ② 정리가 어려우면 전환 시점의 **모든 미완료 작업**을 전환 이전 작업으로 보고 이미 만든 파일의 **다운로드만** 허용한다(수정·재렌더·재시도·새 출력 생성 차단). 단 **최종 렌더가 끝난 N6 저장 대기 작업은 [저장]·취소를 허용**한다.
- 이전 결정(10-29, PM C5)의 "Pillow로 만든 전환 이전 작업은 다운로드만"은 ②의 제한 내용으로 남고, 대상 범위(PM-R0)는 위 ①·②로 대체됐다.
- HTML 렌더 안에서 공통 렌더 번들이 갱신될 때는 위 제한과 별개로 **새 미리보기를 등록**한다.
- **PM-R0·PM-R8 처리 방향(PM 11-04)**: 먼저 대상과 건수를 조사해 **테스트 작업 취소로 정리할 수 있는지** 판단한다. 정리가 어려우면 대안 =
  전환 시점의 **모든 미완료 작업**을 대상으로 하되, **최종 렌더가 끝난 N6 저장 대기 작업은 [저장]·취소를 허용**한다.
  조사 쿼리와 적용안은 `pixlate-ops/plans/2026-11-04_pm-r0-r8-investigation.md`(조사·취소·삭제·백필은 실행하지 않았다 — 실행은 별도 승인).
- 남은 협의: 차단 응답의 오류 코드(`legacy_renderer` 후보), 작업 응답의 표시 필드.

### 7.3 협의 중 — 명세에 넣지 않은 것

| 항목 | 상태 | 대상 |
|---|---|---|
| `translation_failed` 확인 기준(실패 시도 `taskId`) | BE 제안 — 처리 실패와 빈 번역문을 구분한 발생·해소 조건 정리(ConfirmRequest) | FE |
| N4 렌더 실패 섹션의 N5 표시(원본으로 보이는지) | 확인 요청 — PM 출력 정책의 전제 | FE |
| 이전 성공 이미지가 있는 N5 재렌더 실패 섹션의 확인·출력 | PM 출력 정책을 넓히지 않음 — 별도 확인 | PM |
| Q-07 확정 제한의 2차(11/6) 적용 범위, 완료 판정이 한 번도 없는 경우 | 상세 기능은 12월(PM), 앞부분 적용 시점만 확인 | PM |
| Q-07 중간 섹션 이미지에서 글자가 잘리는 경우의 검출·방지 책임 | 미결 | AI |
| 기존 작업에 M020을 기록할 때 검사 입력·기존 결과의 유효성 | 미결 | AI |
| 확정 시 추가 확인의 차수 — `check_failed`·`prohibited_expression` 필수 확인, 실행 실패 섹션 확인 | 실행계획상 F-CFM-09는 3차(L·가). 검사 중 차단·미저장 번역문 확인창(PM 2차 확정)과 규칙 검사 결과 표시(J 2차)는 이 질문에서 뺀다 | PM |
| `boundaryCheck` 새 구조(판정 대상 키·판정 입력 키·규칙 버전·basis·confirmBlock) | FE가 수용한 것은 이전 `boundaryExceeded` 구조 — 새 구조는 확인 전 | FE·AI |
| 배치 정규화(1.0·원본 bbox → null)와 3차 시스템 자동 조정 | 조건부(FE 수용) | AI |
| PM-R0·PM-R8 대상·건수 조사와 테스트 작업 정리 가능 여부 | 조사 전(쿼리 준비) | 사용자 → PM |
| 공통 렌더 번들 제공 방식·FE·BE 버전 고정 절차·불일치 때 응답과 갱신 | 협의 중(후보: GitHub Release + SHA-256 잠금) | FE·BE·AI |
| 시스템 자동 조정 구조·사용자 조정과의 적용 우선순위 | 3차 | AI |
| N2·N4 오류 중 중단 뒤 결과 재사용(현재 failed도 중단 가능, archived는 거절) | 검증 필요 | — |
| Kakao·Google 로그인(현재 간이 JWT, 소셜 로그인 구현 없음) | 계약·구현 필요 | — |
| N3 단일 배열 응답 | 후순위(FE 회신 — 필수 계약 아님) | FE |

### 7.4 `/preview` 상세 계약안 (BE 계약안 — FE 필드 설계 위임)

FE가 요구한 의미(표시 preview의 입력 · 현재 시도 상태 · 표시 preview가 현재 입력의 결과인지)를 렌더 주문서 v0.2의 `renderInput`·`inputFingerprint`·attempt/result 분리로 구체화했다. 스키마·판정 규칙·예시 10개는 reviewPreview에 있다. style 최소 필드·계산 책임·식별자·입력 구성 실패와 `/blocks`의 `sectionInputFingerprint`(FE 요청)는 7.5.

필드 제공 규칙: `required`의 키는 항상 낸다(키 생략 = 계약 위반). N5 제외 섹션은 `renderInput`·`inputFingerprint`·`inputError`·`render`가 모두 null이다. 포함 섹션은 `render`가 항상 있고, `renderInput`·`inputFingerprint`는 둘 다 값이 있다 — 단 서버가 렌더 입력을 만들 수 없을 때만 둘 다 null이고 그때 `inputError`가 있다(판정 단계 i). 이 필드 간 조건과 isCurrent·overflow 조건은 설명으로 정한다 — 스키마 조건식(`if/then/else`)은 redocly 예시 검사가 분기를 따로 엄격 평가해 정상 예시를 거부하므로 넣지 않았다. 기존 필드는 현행 서버가 이미 항상 내는 것이다. 호환 기간은 두지 않는 안이다 — 기존 FE의 추가 필드 허용과 기존 필드 의미(`renderedUrl`의 이전 성공 이미지 유지 등)의 호환성을 확인한 뒤 서버가 먼저 배포하고, FE가 그 뒤 판정을 적용한다.

| 필드 | 의미 | 상태 |
|---|---|---|
| `renderer.rendererId` | 서버 렌더 결과 식별자 — 결과 비교에만 쓴다 | 계약안. 구성 요소는 AI 확인 |
| `renderer.bundleId` | 공통 렌더 번들 식별자 — FE·서버 번들 호환 확인용(Pillow면 null) | 계약안. 제공 방식은 FE·BE·AI 협의 |
| `sections[].renderInput` | 서버가 저장한 현재 입력으로 만든 공통 렌더 입력(배경·렌더 대상 블록·박스·스타일). N5 제외 섹션은 null, 대상 0개면 `blocks=[]` | 계약안. `style` 최소 8키·null 없음(7.5, AI 확인). 박스는 소수 그대로 |
| `sections[].inputFingerprint` | 같은 응답 `renderInput`의 서버 입력 지문(SHA-256 16진수 64자, 서버 미리보기 시도의 입력 지문과 같은 계산) | 계약안. **렌더 결과를 바꾸는 입력은 모두 포함(원칙)** — 배율·정렬 근거도 그 값 또는 반영된 최종 렌더 입력으로 반드시 포함. AI·FE 협의는 표현·계산 방법만 |
| `sections[].render.attempt` | 현재 시도 `{taskId, status(pending·running·done·failed), errorCode, retryable}` 또는 null | 계약안 |
| `sections[].render.result` | 표시 중인 마지막 성공 결과 `{taskId, inputFingerprint, rendererId, overflow}` 또는 null. `overflow`는 `isCurrent=false`면 null, 대상 0개의 현재 결과면 `{}` | 계약안 |
| `sections[].render.isCurrent` | 서버 계산 — result 지문 = 현재 입력 지문(같은 응답 안) | 계약안 |
| `sections[].revision` | 섹션 revision — N5 섹션 제외·복원 요청에 쓴다 | 계약안(7.5) |
| `renderedUrl` | 포함 섹션: 표시 결과의 이미지(이전 성공 결과일 수 있음), `result=null`일 때만 null. 3.4.2의 "실패 시 null"은 최초 렌더 실패에만 맞았다(현행 서버도 재렌더 실패 때 이전 이미지 유지). N5 제외 섹션 값은 바꾸지 않고 판정에 안 씀 | 기존 필드, 의미 보강 |

지킨 조건: 현재 시도와 표시 결과 분리(진행·실패 중 이전 결과 유지) · 한 응답 = 한 시점 스냅샷 · 최신성은 revision이 아니라 지문(스타일·배경·배율·렌더러 변경 반영) · 넘침은 현재 입력에 유효한 결과의 값만 · `isCurrent`는 서버 저장 입력 기준(FE 미저장 편집과 별개) · 결과 없음(`result=null`)·시도 없음(`attempt=null`)·대상 없음(`blocks=[]`)·판정 대상 아님(`render=null`) 구분 · 번들 갱신 대기(g)와 Pillow 이전 작업 제한 구분(후자는 작업 단위 표시로).

상태 판정(섹션마다 위에서부터 처음 맞는 것, 모든 조합이 하나에 걸림)

| 단계 | 조건 | 처리 |
|---|---|---|
| x | `render=null` (N5 제외) | 렌더 판정 안 함. 기존 제외 화면 규칙 |
| i | `inputError≠null` (입력 구성 실패) | 이전 이미지는 최신 아님(없으면 없음), 넘침 미측정, 다시 시도·자동 재조회 없음, 보고. attempt가 failed여도 i가 먼저 |
| a | `isCurrent=true` | 최신. `result.overflow`로 넘침 표시 |
| b | `attempt.status` pending·running | 진행 중. 이전 이미지(없으면 없음), 넘침 미측정, attempt 추적 후 재조회 |
| c | `attempt.status=failed` | 실패. 이전 이미지(없으면 없음), 넘침 미측정, 자동 재조회 없음, `retryable`이면 다시 시도 |
| d | `attempt.status=done`, result null 또는 다른 task | 채택 직후·거절. 한 번 재조회, 그대로면 e처럼 |
| g | (attempt null 또는 result와 같은 task) result 있고 렌더러 다름 | 번들 갱신 뒤 새 시도 등록 대기. 아래 재조회 규칙 |
| e | 나머지 — 렌더러 같은데 새 시도 없음 / attempt·result 모두 null | 시도 누락(서버 결함). 한 번 재조회 후 실패처럼 표시·보고, 다시 시도 없음 |

렌더 대상 0개(`blocks=[]`)는 판정 예외가 아니다 — 결과 이미지가 배경만 그린 것일 뿐 x~e를 그대로 적용한다(현행 서버도 대상 0개 섹션을 배경만으로 그려 채택). 실패·시도 누락을 정상으로 보지 않는다.

g의 재조회(계약안, 횟수·간격은 FE 수용): 과거 task는 폴링하지 않는다. 새 시도 등록은 `/preview` 재조회로만 알 수 있으므로 ① g를 처음 본 뒤 제한된 자동 재조회(5초 간격 최대 12회), ② 기존 재조회 계기(추적 task 완료·구성 변경·화면 재진입), ③ 사용자 새로고침으로 확인한다. 서버는 HTML 번들 갱신 대상에 새 미리보기 시도를 등록한다. 등록 방식과 완료 시간 보장은 BE 배포 설계에서 정한다 — FE의 재조회 횟수는 화면의 자동 조회 종료 기준이지 서버의 등록 완료 시간 보장이 아니다. 새 시도가 보이면 b(예시 8). 한도에 도달해도 새 시도가 확인되지 않으면 자동 재조회를 멈추고 "새 렌더 등록을 아직 확인하지 못함"으로 처리한다. 이전 이미지는 최신이 아닌 상태로 유지하고 넘침은 미측정으로 표시하며, 이후 화면 재진입·사용자 새로고침 등으로 다시 확인할 수 있다. 한도 도달은 관측 정보로 기록할 수 있지만 서버가 확인한 등록 실패와 같게 취급하지 않는다. Pillow 전환 이전 작업은 작업 단위 표시로 먼저 걸러 재조회하지 않는다.

남은 결정
- AI: 행간 1.2·절대 배율·원본 정렬 우선·소수 좌표·style 최소 8키·식별자 역할·입력 구성 오류 3코드는 회신으로 정해졌다(외부 필드 이름은 계약안). 정렬 근거의 저장 위치는 구현 때 정한다.
- FE: 박스·배율 저장 구조·지문 비교 규칙·`/blocks.overflow` 판정 미사용·g 자동 재조회는 확정(정규화만 조건부). 번들 반입 방식(R-FE-6)은 협의 중.
- PM: Pillow 전환 이전 작업 — 조사 뒤 테스트 작업 정리 또는 대안(7.2). 작업 단위 표시 필드는 협의 중.
- BE: 렌더러 전환 뒤 새 시도 등록 방식과 완료 시간 보장(BE 배포 설계 — 대상 작업량·배포 절차 포함). 렌더 대상 0개 섹션의 결과는 현행대로 `result`로 내기로 정리했다(배경만 그린 이미지, `overflow={}`).

### 7.5 저장·섹션·배치·검사·확인 계약 (FE·AI·PM 회신 11-03·11-04 반영)

규칙·요청/응답 예시의 정본은 OpenAPI다 — blockPatch 「저장 공통」·「블록 배치」, sectionPatch, blockList(섹션 입력 지문), confirmReview 「검사 연결 경계」·「이미지 경계 이탈」, ConfirmRequest, `Error.details`(409 구분), 스키마 `EditIdempotencyKey`·`BlockPatch`·`LayoutAdjustPatch`·`LayoutBox`·`LayoutAdjust`·`BlockCheck`·`Section.revision`·`PreviewInputError`·`BoundaryCheck`·`ReviewSignal.regulationVerdict`·`TextBlock.sectionInputFingerprint`. 이 절은 상태·남은 질문·적용 범위만 둔다.
근거: FE 회신 11-03·11-04, AI 회신 11-03·11-04와 Notion 원문(BE–AI 기술 입출력 계약 v0.1, 합의 항목 관리표 — 11-03 대조), PM 회신 11-04. **PM 정책의 출처는 검토 채팅에 제공된 PM 원문**이고, 이 작업 채팅은 그 요약을 받아 반영했다. 요약을 옮기며 이 채팅이 덧붙인 해석은 `(BE 해석)`·`BE 정리`로 표시해 확정 사항과 구분한다.

**이번 회신(11-04)으로 닫힌 계약**

| 항목 | 확정 내용 | 근거 |
|---|---|---|
| 멱등 세부 | 보관 24시간, 만료 뒤 새 요청 처리, 잠금 대기 초과 503, `Idempotent-Replayed: true`. 결과 불명이면 같은 키·같은 본문으로 재시도하고 그 대상의 후속 저장·Undo/Redo 보류. 교차 출처에서 헤더를 읽도록 CORS 노출은 구현 대기 | FE |
| 409 구분 | `error.code` + `details.reason` 조합(목록은 `Error.details`), `check_not_settled`의 `registeredBlockIds`. 이후 새로 제안하는 사유는 따로 확인(일괄 승인 아님) | FE |
| 입력 지문 비교 | A(`/blocks`)·B(`/preview` 현재 입력) = 표·뷰어 일치, B·C(표시 결과 입력) = 미리보기 최신성, null = 비교 불가, 블록이 없는 섹션은 `/preview` 안의 최신성만 | FE |
| 배치·제외·복원과 검사 | 글자가 바뀌지 않은 박스·배율 변경과 섹션 제외·복원은 규제·강제 용어 검사를 다시 실행하지 않는다(PM). 제외는 검사 대상 집합에서 빼고(결과 보존), 복원은 유효한 결과를 다시 쓰며 유효한 결과가 없을 때만 확보 — 재실행 금지와 부딪치지 않게 한 정리는 BE 정리 | PM(P1 종료) |
| 렌더 입력 오류 | `STYLE_RESULT_INVALID`(AI 스타일 결과 위반)·`STYLE_DEFAULTS_UNAPPROVED`(승인 스타일 설정·폰트·번들 자산 검증 실패)·`RENDER_INPUT_INVALID`(BE가 구성한 박스·배율·조정값 조합 오류와 잘못된 자산 참조) — 정상 입력 뒤 브라우저 실행·측정·이미지 생성 실패는 렌더 시도 실패 | AI |
| 검사 `inputKey` 기준 | 규칙 = 블록 ID·원문·번역문·적용 고정 사전·용어집 선별·규칙 버전 / LLM(3차) = + 섹션 전체 문맥·모델·프롬프트·구현 버전·분할 한도 / 배치·배율·저장 revision 제외 / 블록별 키는 부분 채택 허용이 아님 | AI(A11 종료) |
| 조건부 경고 표시 | 공통 `prohibited_expression` + 판정 구분(`regulationVerdict` — 필드 이름·값은 계약안), 금지 「금지 표현」·조건부 「조건부 규제」, 조건부에는 사전 `reason`(허용 조건) | FE·PM(F18·P8 종료) |
| 미적용 강제 용어 | PRD대로 확인 대상에서 제외, flag·신호 보존 | PM 재확인 |
| 실행 실패 섹션 출력 | 인페인팅 실패 = 원본 위에 번역문. N4 렌더 실패 = N6에서 원본(최종 단계에서 다시 그리지 않고 새 렌더 실패로 세지 않음). N6 결과 = 셀러가 N5에서 본 모습. `render_failed` 사유를 성공 결과가 없는 섹션으로 한정한 것은 (BE 해석) | PM(P7 일부 종료) |
| Q-07 좌표계 | 제외 반영 최종 결합 이미지, 최종 블록 y = 섹션 로컬 y + 제외 반영 출력 위치, `displayTop` 재사용 안 함, 섹션 입력이 같으면 섹션 이미지 재사용, 제외·복원 뒤 최종 합성·재판정(뒤 섹션 전체 재렌더 없음) | PM·AI(F19 종료 — FE의 섹션별 선호는 채택 안 함) |
| Q-07 확정 제한 | ① 현재 판정 진행 중 = 버튼 비활성·서버 거절 ② 현재 이탈 = 차단 ③ 판정 없음·실패 = 마지막 완료 판정 사용 ④ 이전 이탈은 최신 판정에서 해소될 때만 해제. ④를 ③과 같은 규칙으로 구현하는 것, 렌더·판정의 실제 대기·실행만 진행 중으로 보고 실패·미등록은 ③으로 보내는 것은 (BE 해석) | PM(P9 일부 종료) |
| OTC | OTC·복합 → 화장품·잘 모름 = OTC 종류 삭제, OTC ↔ 복합 = 유지, 국가 변경 = 규제 분류·OTC 종류 재선택 | PM(P5 종료) |
| 임시 M020 | OTC·복합이고 종류가 없는 작업에 실제 저장(기존 작업 포함, 화장품·잘 모름 제외), 3차 필수 선택 도입 뒤 새 임시 적용 종료·저장된 M020 유지 | PM(P5 종료) |
| 브랜드 스냅샷 | 작업 생성 시점 값으로 고정, 번역·검사·재시도에서도 이후 수정값을 섞지 않음 | PM(P6 종료) |

**아직 남은 것 — 이 회신으로 닫히지 않음**

| 항목 | 현재 제안 | 답할 사람 |
|---|---|---|
| `translation_failed` 확인 기준 | 처리 실패(현재 번역 시도 failed + `trans_1` NULL + `machine`)이고 빈 번역문 저장과 다르다. 재실패를 구분하려 확인 항목에 실패 시도 `taskId`(현행 신호에 있음)를 보내고, 현재 실패 시도와 다르면 409 `acknowledgement_outdated` | FE F20 |
| 실행 실패 섹션 확인 구조 | `acknowledgedSections: [{sectionId, reason, taskId}]` 조건부 수용 — 인페인팅 실패 시도는 `/tasks`(`taskType=inpaint`·`unitType=section`·`unitId`)로 연결된다(현행 서버 확인), 렌더는 `/preview.render.attempt.taskId`, 서버가 대상·실패 종류·현재 시도를 검증 | FE F21 |
| N4 렌더 실패 섹션의 N5 표시 | 원본으로 보이는지(현행 `/preview`는 성공 결과가 없으면 `renderedUrl` null) — PM 출력 정책의 전제 | FE F22 |
| 이전 성공 이미지가 있는 N5 재렌더 실패 | PM 출력 정책을 이 경우로 넓히지 않는다 — 확인·출력을 어떻게 할지 | PM P10 |
| Q-07 적용 시점 | 상세 기능(최종 결합 판정·새 상태·`boundaryCheck`)은 12월. 확정 제한 ①~④를 2차에 먼저 적용하는지 — ①은 "렌더 진행 중 확정 허용"을 바꾼다 | PM P11 |
| Q-07 완료 판정이 한 번도 없음 | 사유 후보 `boundary_check_missing` — 막을지 | PM P12 |
| Q-07 중간 섹션 이미지의 글자 잘림 | 최종 합성으로 복구되지 않음 — 섹션 렌더 단계의 검출·방지 책임과 방식 | AI A13 |
| 기존 작업에 M020 기록 | 유효성 지문에 "고정 사전 묶음에서 실제 소비한 행"이 들어가므로 M020을 기록하면 검사 입력이 바뀐다 — 기존 결과를 `outdated`로 볼지, 기록과 함께 재검사를 등록할지 | AI A14 |
| 추가 확인 절차의 차수 | 검사 중 차단·미저장 번역문 확인창은 PM 2차 확정, 규칙 검사 결과 표시(조건부 칩 포함)는 J 2차 (BE 해석). 남은 것은 `check_failed`·`prohibited_expression` 필수 확인과 실행 실패 섹션 확인 — 실행계획상 F-CFM-09는 3차 | PM P13 |
| `boundaryCheck` 새 구조 | FE가 수용한 것은 이전 `boundaryExceeded` — 새 구조(판정 대상 키 = 현재 저장 입력의 섹션 렌더 입력 지문 기준, 판정 입력 키 = 완료 판정이 쓴 입력, 규칙 버전 renderer·composite·boundary, basis·confirmBlock)는 확인 전. 상태는 실제 진행으로 정한다 — 대기·실행 중 = in_progress, 렌더·입력 구성·판정 실패 = failed, 시도 미등록 = none(실패·미등록은 마지막 완료 판정). 지문 불일치는 `current`로 인정하지 않는 조건일 뿐 | FE F23 · AI A15 |
| 배치 정규화와 3차 시스템 조정 | 조건부 — BE 권장: 2차 정규화, 3차에는 시스템 조정이 있는 블록의 1.0·원본 박스를 "셀러 지정"으로 저장 | AI A10 |
| PM-R0·PM-R8 | 대상·건수 조사 후 테스트 작업 취소로 정리 가능한지 판단, 어려우면 대안(미완료 작업 전체 + N6 저장 대기 작업 저장·취소 허용) — 쿼리 준비, 실행 안 함 | 사용자(조사) → PM |

**적용 범위**

| 차수 | 범위 |
|---|---|
| 2차(11/6) | 저장 공통(처리 순서·422/400/409·멱등 키·CORS 노출)·같은 값 no-op·섹션 저장(revision·no-op·복원 규칙)·블록 배치 저장·`/preview` 렌더 입력·지문·`sectionInputFingerprint`·규칙 검사 연결(J — 검사 상태·유효성 지문·결과 표시, 조건부 칩 포함 (BE 해석))·검사 중 확정 거절과 버튼 비활성(PM)·미저장 번역문 확인창(PM)·현행 `translation_failed` 확인. 추가 확인 절차(`check_failed`·`prohibited_expression` 필수 확인, 실행 실패 섹션 확인)는 P13 |
| 3차 | 실행계획상 F-CFM-09 중 P13에서 2차로 정하지 않은 확인 절차, LLM 규제 판정, 시스템 자동 조정, N6 이탈 → [중단] → N5 복귀, OTC 필수 선택 화면(M020 새 적용 종료) |
| 12월 | Q-07 상세 — 최종 결합 좌표계 경계 판정·새 상태·`boundaryCheck` 응답(PM — 실행계획대로). 확정 제한 ①~④의 앞당김 여부는 P11 |

**기존 계약과의 호환·적용 순서** — 추가 필드·요청 완화가 대부분이다. 동작이 바뀌는 것: 같은 값·같은 상태 no-op, 검사 연결 뒤 새 확정 거절·확인 대상 확장, (적용되면) 경계 판정 진행 중 확정 거절.
`Idempotency-Key`·섹션 `revision`은 처음엔 선택(키가 있으면 섹션 revision 필수) → FE 적용 뒤 필수. 순서: ① 남은 질문 확인 → ② OpenAPI 표시 확정 → ③ 서버가 추가 필드·선택 헤더·no-op을 먼저 배포 → ④ FE 적용 → ⑤ 필수화·검사 연결 켜기(#83 일괄 등록과 같은 배포).

**확인 질문(남은 것만)** — 답할 부분만 적었다.

FE
- F20. `translation_failed` 확인에 실패 번역 시도의 `taskId`(`ReviewSignal.taskId`)를 함께 보내는 안 — 처리 실패와 빈 번역문 구분이 맞는지, 이 값을 보낼 수 있는지만.
- F21. `acknowledgedSections`의 `taskId` — 인페인팅은 `/tasks`에서 `taskType=inpaint`·`unitType=section`·`unitId=섹션 ID`인 현재 시도, 렌더는 `/preview.render.attempt.taskId`. 이 연결로 화면에서 값을 채울 수 있는지만.
- F22. N4 렌더 실패(성공 결과 없음) 섹션을 N5에서 실제로 원본으로 보여 주는지.
- F23. `boundaryCheck`(12월) 새 구조 — 이전에 수용한 섹션 결과 `boundaryExceeded` 대신 작업 단위 `state`·`basis`(current·last_completed·none)·`exceeded`·`confirmBlock`으로 받는다. 버튼 비활성·차단 안내를 이 값으로 그릴 수 있는지, 마지막 완료 판정(basis=last_completed)을 최신 결과와 구별해 보일 수 있는지만. 상태는 실제 진행으로 구분한다 — `in_progress`(대기·실행 중, 버튼 비활성) / `failed`(렌더·입력 구성·판정 실패) / `none`(시도 미등록) — 실패·미등록이면 마지막 완료 판정(`basis = last_completed`)으로 차단 여부를 정하고 버튼을 계속 막지 않는다. 이 구분으로 화면을 그릴 수 있는지도.

AI
- A10. 배치 정규화와 3차 시스템 조정(위 표).
- A13. 중간 섹션 이미지에서 글자가 섹션 경계에 잘리는 경우 — 섹션 렌더 단계에서 검출할지·막을지와 판정 방식.
- A14. 기존 작업에 M020을 기록할 때 검사 입력·기존 결과의 유효성 — 기존 결과를 `outdated`로 보고 재검사를 등록하는지.
- A15. `boundaryCheck` 판정 키 — 판정 대상 키 = 포함 섹션 순서·각 섹션의 **현재 렌더 입력 지문**(결과 지문이 아님)·출력 위치·최종 이미지 크기·규칙 버전, 판정 입력 키 = 완료 판정이 실제로 쓴 섹션 결과들의 입력 지문으로 같은 방식. 규칙 버전 = renderer(측정 규칙 포함)·composite(합성)·boundary(경계 측정). 이 구성과 버전 이름이 맞는지, 섹션 렌더가 끝나지 않았을 때 판정을 진행 중으로 두는 것이 맞는지만. 그리고 상태를 지문 불일치가 아니라 실제 진행으로 정하는 것(대기·실행 = in_progress, 렌더·입력 구성·판정 실패 = failed, 미등록 = none, 불일치는 current 불인정 조건)이 맞는지.

PM(정책만)
- P2. PM-R0 — 조사 결과를 본 뒤 테스트 작업 정리 또는 대안 적용(위).
- P3. PM-R8 — 같은 조사로 판단.
- P10. 이전 성공 이미지가 있는 상태에서 N5 재렌더가 실패한 섹션의 확인·N6 출력.
- P11. Q-07 확정 제한 ①~④를 2차(11/6)에 먼저 적용하는지(상세 판정은 12월) — ①은 "미리보기 재렌더 진행 중 확정 허용"을 바꾼다.
- P12. Q-07 완료 판정이 한 번도 없을 때 확정을 막는지.
- P13. 확정 시 **추가 확인 절차**의 차수 — 검사 최종 실패·미해결 규제 경고의 필수 확인(`check_failed`·`prohibited_expression`)과 실행 실패 섹션 확인(`acknowledgedSections`)을 2차(11/6)에 넣는지(실행계획상 F-CFM-09는 3차). 검사 중 차단·미저장 번역문 확인창(이미 2차 확정)과 규칙 검사 결과 표시는 이 질문에 들지 않는다.

**확인하지 못한 근거**
- PM 정책은 검토 채팅에 제공된 PM 원문이 출처다 — 이 작업 채팅은 요약을 받았고 원문을 직접 대조하지 않았다. 요약 해석은 `(BE 해석)`·`BE 정리`로 표시했다.
- Notion 원문은 11-03에 읽었고, 그 뒤 AI가 갱신한 내용은 다시 읽지 않았다.
- 최초 판정 기록이 없는 직접 제외 섹션과 렌더러 전환 대상 작업의 실제 건수(조사 쿼리 미실행).

### 7.6 구현 대기

서버(BE)
- 블록 PATCH: 같은 값 저장 = revision 유지·새 task 없음(`rerenderTaskId: null`). 현재는 같은 값도 revision+1·`edited`·재렌더.
- 블록 PATCH: 실제 값이 바뀐 저장인데 `rerenderTaskId=null`이 나오는 현행 경로 — 별도 조사·보완(`pixlate-ops/plans/2026-11-03_rerender-missing-investigation.md`).
- 블록·섹션 PATCH 공통: 처리 순서, 멱등 기록(S11, 같은 트랜잭션)·24시간 정리·취소 때 삭제, `details.reason`, 재전송 표시 헤더와 **CORS `Access-Control-Expose-Headers`에 `Idempotent-Replayed`**.
- 블록 배치: `trans1` 생략 허용, `layoutAdjust` 저장(`layout_adjust` v2), 정규화, 범위 검증(십진수, 보정 없음), 그리지 않는 블록 거절, 배치만 수정 시 `blockStatus` 유지·검사 등록 없음.
- 섹션 PATCH: `section.revision`(S12)·충돌 검사(키가 있으면 revision 필수)·같은 동작 no-op·전체 Section 응답·복원 규칙·근거 없는 복원 거절·N5 복원 때 유효한 결과가 없을 때만 렌더·검사 확보(최종 실패·재시도 횟수 보존).
- 렌더 입력: 박스 정수 절삭 제거, 셀러 박스·배율 반영, style 최소 8키·`fontScale` 적용값 1.0, `renderer.bundleId`, 입력 구성 실패 `inputError`(세 코드), `/blocks`의 `sectionInputFingerprint`.
- 검사 연결: 검사 stage 마이그레이션, 원래 흐름의 등록·확정 요청의 보조 복구, 두 지문(요청 검증 유지·유효성 지문 신설), 채택 조건, `check.state`·`check.inputKey`, 확정 거절(`registeredBlockIds`).
- 확인: 확인 항목 `inputKey` 대조, `translation_failed` `taskId` 대조(F20 뒤), `acknowledgedSections` 검증(F21 뒤), 감사 로그. `check_failed`·`prohibited_expression` 필수 확인과 `acknowledgedSections`의 차수는 P13.
- 신호: `ReviewSignal.regulationVerdict`(AI flag의 사전 판정 상태에서 채움).
- N6: N4 렌더 실패를 확인한 섹션은 원본을 쓰고 다시 그리지 않으며 새 렌더 실패로 세지 않는다(F22 뒤, 실행 실패 섹션 확인의 차수는 P13).
- OTC·M020: 규제 분류·국가 변경 때 OTC 종류 삭제·재선택 규칙, OTC·복합이고 종류가 없는 작업에 M020 저장(기존 작업 포함 — A14 뒤). 필드는 새 규제사전(F 묶음) 계약.
- 브랜드: 작업 생성 시점 스냅샷 저장(현재는 `brand_id`만 저장하고 N3 [이대로 진행] 때 실행 범위에 고정).
- Q-07(12월): 최종 결합 좌표계 경계 판정·`boundaryCheck`·확정 제한(P11에 따라 일부 앞당김).
- 렌더러 전환: 번들 갱신 대상에 새 미리보기 시도 등록, 전환 이전 작업 차단(PM-R0 조사 뒤).
- 7.1의 다른 `대기` 항목(포함 섹션 판정, 복원 의미, leaf 검증, CSV 배치 열, 검사 중 확정 거절).

화면(FE)
- `rerenderTaskId` null 처리와 Undo/Redo 공통 적용, 멱등 키 발급·재전송·결과 불명 보류.
- 배치 조작 → 절대값 요청, 섹션 안 제한, 같은 공통 코드로 로컬 미리보기.
- 섹션 revision 전송·409 처리, 검사 상태별 버튼 처리, 확인 목록(`inputKey`·`taskId`·`acknowledgedSections`), 조건부 경고 표시(`regulationVerdict`).
- `/preview` 판정 규칙(x→i→a→b→c→d→g→e), 지문 비교(A·B, B·C), g의 재조회.
- 동의 체크를 [다음]의 필수 조건으로 적용(createdAt을 동의 시각으로 쓰는 전제).

### 7.7 문서 사이트

`deploy-api-docs.yml`은 `feature/api-tracking`에 push될 때만 사이트를 갱신한다. 그 브랜치는 원격에 없으므로, 이 명세가 `develop`에 머지돼도 사이트(https://ai-pixlate.github.io/server/)는 바뀌지 않는다. 배포 방식은 별도 결정.
