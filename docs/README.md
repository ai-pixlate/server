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

API 명세(`docs/openapi.yaml`)는 서버 코드와 같은 저장소에서 관리하며, 변경은 **`develop`으로 PR**을 올려 반영합니다(서버 구현 PR이 그 계약 문구를 함께 고치거나, 문서만 담은 PR).
`develop`에 머지돼도 문서 사이트(https://ai-pixlate.github.io/server/)는 자동으로 바뀌지 않습니다 — 아래 수동 배포로 갱신합니다.

### 자동화 (GitHub Actions)

| 워크플로 | 트리거 | 하는 일 |
| --- | --- | --- |
| [`api-docs-validate.yml`](../.github/workflows/api-docs-validate.yml) | `develop`·`api-tracking` 대상 PR / push (`docs/**`) | YAML 파싱 · Redocly lint · PR 에 변경 엔드포인트 요약 코멘트 |
| [`deploy-api-docs.yml`](../.github/workflows/deploy-api-docs.yml) | `develop` 수동 실행(workflow_dispatch) · `api-tracking` push (`docs/**`) | lint 뒤 Swagger UI 를 GitHub Pages 로 배포 |

배포 경로는 이 둘뿐입니다. 그 밖의 브랜치(`release/*` 포함)에서 수동 실행하면 배포 job을 건너뜁니다.
`develop`에서 배포: Actions → Deploy API Docs → Run workflow → Branch `develop`. 원격에 `api-tracking` 브랜치는 아직 없습니다.

### 설정 (관리자)

1. **Settings → Pages → Source** = `GitHub Actions`
2. **Settings → Environments → github-pages → Deployment branches** = `develop`·`api-tracking`만(워크플로 배포 조건과 같게)
3. (권장) **Settings → Branches** 로 `develop` 보호 — PR 필수 + `OpenAPI 검증` 체크 통과 필수

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
- 2차 소셜 로그인(`fix/auth-social-contract`): AUTH-06이 start(`authSocialStart`)·완료(`authSocial`) 두 오퍼레이션이 됐다. 이 표는 3.4.x 집계 그대로이고, 현재 스펙 오퍼레이션은 63개다.
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
- **🔵12월 스텁**: JOB-01 · INP-02 · LIB-02 · LIB-03 · ADM-01~07 — 상세 Request/Response 12월 확정. (AUTH-06 소셜 로그인은 2차에 계약 확정 — 7.1)
- **📌조건부**: JOB-08 `resume` — PM 확정 후 계약 채움. (`PATCH /jobs/{jobId}` 는 jobPatch 로 확정·구현됨)
- **verify 노드 분리**(N2 판정 vs N4 규제검증) — 오케스트레이션 §15-1 미결(스펙엔 영향 없음).

## 7. 2차 통합(PRD v3.5.10 · 11/6) 확정 명세 공개 현황

`openapi.yaml`(명세 3.5.10 — PRD v3.5.10 기준 · 2차 통합 API 계약)은 2차 확정 계약 공개본이다 — FE·AI가 타입·Mock·화면 개발에 쓴다. 표기 규칙은 `openapi.yaml` 머리말 「2차 통합 반영 표기」를 따른다. 이 표의 구현 상태는 develop 기준이며 서버 배포 완료를 뜻하지 않는다.
운영 서버는 1.0.0 릴리즈 계약(명세 3.4.2) 기준이고, 2차 항목은 어느 서버(운영·dev 샌드박스)에도 아직 배포되지 않았다. 남은 계약 항목·3차 항목은 명세에 싣지 않는다(7.3·7.4). 전체 서버 구현 완료는 공개의 선행 조건이 아니다.
/preview 행은 그 구현 PR(#108) 머지를 전제로 적었다.

서버 상태 표기: `계약 확정·서버 구현 대기`(계약 확정, develop 서버 코드 없음) · `구현 완료·서버 배포 대기`(develop에 구현됨, 어느 서버에도 배포 안 됨) · `서버 배포·연동 확인 완료`(실제 서버 배포와 FE 연동까지 확인 — 지금 2차 항목 중 해당 없음) · `현행`(지금 운영 서버 동작, 3.4.2 계약) · `화면`(FE 동작). 구현 PR 안에서 남은 경로(`서버 미완료`·`미착수`)는 7.2에서 추적한다.

### 7.1 2차 공개 항목

| 항목 | 계약 근거 | 명세 위치 | 서버 상태 | 구현 PR |
|---|---|---|---|---|
| 번역문 같은 값 저장(no-op) | FE 합의 | blockPatch 「같은 값 저장」·예시 `sameValue`·`sameValueWhileRendering` | 구현 완료·서버 배포 대기 | #104 |
| 저장 공통(처리 순서·멱등 키·422/409/503·`Idempotent-Replayed`·CORS 노출) | FE 11-03·11-04 | blockPatch 「저장 공통」·`EditIdempotencyKey` | 구현 완료·서버 배포 대기 | #105(블록)·#107(섹션) |
| 저장 공통의 배치 항목(업무 검증 400·배치 요청 422·409 `not_render_target`) | FE 11-03 | blockPatch 「저장 공통」 7단계·오류 분류 | 구현 완료·서버 배포 대기 | `feature/block-layout-adjust` |
| 409 구분(`error.code` + `details.reason`) | FE 11-04 | `Error.details` | reason별 — 구현 완료·서버 배포 대기: `idempotency_key_reused`·`job_not_editable`·`n3_excluded_section`·`restore_basis_missing`·`not_render_target` / 구현 완료·서버 배포 대기(`feature/confirm-ack-task-id`): `acknowledgement_mismatch`·`acknowledgement_outdated` / 구현 완료·서버 배포 대기(기본 꺼짐): `check_not_settled` | #105·#107 / `feature/translate-check-connect` |
| 섹션 제외·복원(revision·같은 상태 변경 없음·복원 의미·전체 Section 응답·근거 없는 복원 409) | FE 11-03·PM | sectionPatch·`Section.revision`·`SectionPatch`·예시 `n3ExcludedInN5` | 구현 완료·서버 배포 대기 | #107 |
| N5 복원 때 미리보기 시도 확보 | FE 11-03·PM | sectionPatch | 구현 완료·서버 배포 대기(하류 실행 없는 섹션은 서버 미완료) | #108 |
| N5 복원 때 검사 시도 확보 | PM 11-04 | sectionPatch·confirmReview 「규칙 검사 연결」 ④ | 구현 완료·서버 배포 대기(기본 꺼짐) | `feature/translate-check-connect` |
| N5 섹션 Undo/Redo | FE 11-03 | sectionPatch | 화면 | — |
| 실행 취소·재실행 — 전용 API 없음, 현재 revision으로 다시 저장 | PRD F-CFM-03 | blockPatch·예시 `changed` | 현행 | — |
| 블록 배치 저장(`layoutAdjust`·생략/null·2차 정규화·범위 400·`not_render_target`·원자성) | FE 11-03 · 정규화 FE 수용·AI 동의 11-04 | blockPatch 「블록 배치」·`BlockPatch.layoutAdjust`·`LayoutAdjustPatch`·`LayoutBox`·`LayoutAdjust`·`TextBlock.layoutAdjust`·요청 예시 7개·응답 `layoutOnly`·오류 예시 3개 | 구현 완료·서버 배포 대기 | `feature/block-layout-adjust` |
| 배치 화면 조작([+][−] 0.05·박스 중심·경계 안쪽 이동·스냅·Undo 50단계·계산식) | PM F-CFM-15·FE 계산식 동의 | blockPatch 「블록 배치」 | 화면 | — |
| 배치만 바꾼 저장은 「사용자수정」이 아님 | PM | `TextBlock.blockStatus` | 구현 완료·서버 배포 대기 | `feature/block-layout-adjust` |
| content.csv 배치 5열 | PM(F-CFM-15 H109) | exportCreate | 구현 완료·서버 배포 대기 | `feature/block-layout-adjust` |
| 기존 배치 박스(출처 불명) — 보존·그 박스로 렌더, 해제 = 원본 bbox(`layoutAdjust` null → null이어도 revision+1·재렌더), Undo = 이전 적용 박스를 셀러 조정으로 저장, 해제 재전송 3경우 | FE 11-09 | blockPatch 「블록 배치」 | 구현 완료·서버 배포 대기(FE 구현·dev 연동 검증 전) | `feature/block-layout-adjust` |
| 글자 픽셀이 섹션 안에 없는 렌더도 넘침으로 채택(2차 기존 규칙 유지, 서버 내부 관측 기록만), 렌더러 예외는 기존 실패 경로 | PM 11-09 | `LayoutAdjustPatch.fontScale` | 구현 완료·서버 배포 대기(PM 전제인 N5 미리보기 갱신·넘침 표시의 FE 연동 확인 전) | `feature/block-layout-adjust` |
| 규칙 검사 연결(활성 검사 = 규칙 검사, 등록 단위, 등록 보장 ①~④, 보조 복구, 박스·배율·제외·복원만으로는 재검사 안 함) | PM 11-04·AI 회신 | confirmReview 「규칙 검사 연결」 | 구현 완료·서버 배포 대기(기본 꺼짐). 등록 보장 ⑤(배포 때 기존 N5 일괄 등록)는 대상 정책 미정 | `feature/translate-check-connect` |
| 검사 상태 5값·FE 처리, `check.inputKey`(불투명 문자열, 같은지만 비교) | FE 11-03·AI 11-04 | `BlockCheck`·`TextBlock.check` | 구현 완료·서버 배포 대기(기본 꺼짐)(FE 연동 전) | `feature/translate-check-connect` |
| 검사 중 확정 거절(409 `check_not_settled`·`details.blocks`·`registeredBlockIds`)·버튼 비활성 | PM 10-29·FE 11-04 | confirmReview·예시 `checkNotSettled` | 구현 완료·서버 배포 대기(기본 꺼짐). `registeredBlockIds` 문구·FE 처리 확인 대기 | `feature/translate-check-connect` |
| 검사 최종 실패는 확인 없이 확정 통과 | PM 11-04 | confirmReview·`BlockCheck.state` | 구현 완료·서버 배포 대기(기본 꺼짐) | `feature/translate-check-connect` |
| 검사 신호 연결 — 강제 용어 미적용 → `mandatory_term_unapplied`, 원판정 regulated·conditional → `prohibited_expression`(allowed·그 밖 값·원판정 없는 이전 저장분은 신호 없음), 원판정 값 저장 보존 | 기존 계약(API 6-11 확인) | confirmReview 「규칙 검사 연결」·`SignalCode` | 구현 완료·서버 배포 대기(기본 꺼짐) | `fix/check-signals` |
| 검사 신호는 settled일 때만 · 블록·코드당 1개 | API 권고(6-11) | `BlockCheck.inputKey`·confirmReview | 구현 완료·서버 배포 대기(기본 꺼짐) · **FE 확인 대기** | `fix/check-signals` |
| 셀러가 고칠 수 없는 검사 오류(용어 공급 실패·강제 용어 충돌·`input_invalid`·`bundle_invalid`·사전 묶음 변환 실패·검사 보고/요청 무결성)도 failed — 확정 통과(등록 불가 섹션도 N4에서 멈추지 않음), 다른 섹션 조회·편집 유지. 범위: 실행된 시도는 서버가 고정한 대상 블록, 섹션 공통 공급 실패는 섹션, 블록 하나의 충돌(강제 용어끼리 · 금지/허용 같은 구간)은 그 블록만 — 나머지 블록은 실제 검사(7.2 ⑨) | PM 11-10 | `BlockCheck.state`·confirmReview | 구현 완료·서버 배포 대기(기본 꺼짐) | `feature/translate-check-connect` |
| 미리보기 재렌더 중 확정 허용·넘침 경고 비차단 | PM | confirmReview | 현행 | — |
| 미저장 번역문 확인창 | PM | confirmReview | 화면 | — |
| 필수 확인 = `translation_failed`, `mandatory_term_unapplied` 제외, 블록·코드 단위 한 번 확인 | PRD F-CFM-09·PM 재확인 11-04 | ConfirmRequest | 현행 | — |
| `translation_failed` 확인 기준 `taskId`(선택 필드) | FE 11-04 | `ConfirmRequest.acknowledgedWarnings[].taskId`·`ReviewSignal.taskId`·예시 `acknowledgeTranslationFailed` | 구현 완료·서버 배포 대기(생략 = 기존 판정, 명시적 null = 422) | `feature/confirm-ack-task-id` |
| 확인 기록 감사 로그 | PM C3 | ConfirmRequest | 계약 확정·서버 구현 대기(대상·action 이름·revision 의미 미정) | 미정 |
| 섹션 실행 실패 출력 — 인페인팅 실패 = 원본 위 번역문, 이전 성공 이미지가 있는 N5 재렌더 실패 = N6에서 확정 번역문으로 다시 렌더 | PM 11-04 | confirmReview 「섹션 실행 실패의 출력」 | 현행 | — |
| 표시 결과 없이 렌더 시도가 실패한 섹션의 N5 원본 표시(화면 표시 조건 — N4 렌더 실패 판별이 아니다. N4 판별·N6 출력은 P14·P15) | PM 11-04·FE(client PR #44) | confirmReview 「섹션 실행 실패의 출력」·reviewPreview 「표시 결과가 없을 때」 | 화면 | — |
| category leaf만 허용 — 값이 실려 올 때 생성·수정에서 확인(생략·null = draft 미선택 허용), 없는 id·상위 분류 = 400 `VALIDATION_ERROR` `details.fields`(loc `body.categoryId`, reason 없음), 매핑 없는 leaf는 통과(common), 저장된 상위 분류는 보정·재검사하지 않음. 최초 분석(N1 — [중단]으로 돌아온 작업 포함)은 저장된 categoryId 필수·지금 leaf(같은 400 형식, 기존 분석 게이트 뒤·실행 생성 전), N2 실패 재시도는 기존 작업 재시도 호환을 위한 예외로 확인하지 않음 | PRD F-SRC-04 J41①② · 검증 시점·형식·최초 분석 조건 2026-11-12 결정(사용자) | `JobCreate.categoryId`·`JobPatch.categoryId` | 구현 완료·서버 배포 대기 | `fix/job-category-leaf` |
| `/master/categories` 응답을 명세의 `Category` 평면 배열(`parentId`로 연결)로 정정 — 이전 서버는 중첩 트리(`children`)라 화면이 대분류를 leaf로 볼 수 있었다 | OpenAPI masterCategories(정본) | masterCategories·`Category` | 구현 완료·서버 배포 대기 | `fix/job-category-leaf` |
| N3 포함 섹션 판정(목록도 판정 전부) | PRD F-SEC-02·F-SEC-04 | `Section.verdicts`·`SectionList`·sectionList | 계약 확정·서버 구현 대기 | 미정 |
| N3 원래 순서(`sectionOrder` 정렬)·섹션 카드 버튼 | PRD F-SEC-02·F-SEC-03 | `Section.sectionOrder`·`SectionPatch` | 현행(정렬·버튼은 화면) | — |
| OTC 종류 규칙·임시 M020·2차 M020 적용 규칙 ①~⑥ | PM 11-04·AI 11-04·11-05 | `JobPatch.regulatoryClass` | 계약 확정·서버 구현 대기(OTC 필드는 새 규제사전 작업, 백필은 대상 조사·실행 승인 뒤) | 미정(#103 계열) |
| 작업 생성 시점·책임 동의, `consentId` 미사용 | PRD F-ACC-04 | jobCreate·consentCreate | 현행(동의 체크는 화면) | — |
| `Seller.email` null 허용(소셜 계정 대비 — 계정은 제공자 + 회원번호로 구분, 이메일은 식별에 쓰지 않음), 이메일 간이 로그인(개발용)은 local 계정만 찾고 만듦 | PRD F-ACC-05 · FE 회신 11-09(타입 수용) · PM 11-09(이메일 수집·용도) | `Seller.email`·authLogin·authMe | BE 병합·dev 배포 완료(release/1.1.0)(소셜 로그인 API는 아래 「소셜 로그인」 행) | `feature/auth-account-identity`(#123) |
| 로그인 세션(S안) — **정본 「refresh 서버 상태 없음」 변경**: 로그인마다 서버 세션(Redis), access는 세션에 묶여 요청마다 확인, refresh 쿠키 회전(동시 요청 하나만 성공·30초 유예·재사용 시 그 세션 폐기·거절은 Set-Cookie 없음), 비활성 7일·최대 30일, **활동 = 로그인·refresh 성공만**(시연용 허용 — 실제 조작 기준 정책과 다름), 로그아웃 = 자격이 가리키는 살아 있는 세션만 폐기(쿠키는 서명이 맞으면 세대와 관계없이 식별), 유효한 refresh 쿠키만으로도 로그아웃(계약 — FE 요청), 이미 종료된 세션의 재로그아웃 204(FE 11-10), 검증할 수 없는 자격은 401 `invalid_token`·무변경, 같은 브라우저 재로그인은 이전 세션 폐기, 세션 없는 이전 access 401, 저장소 장애 503(사유 없음) | PM 11-09(7일·30일·서버 무효화)·11-10(다중 탭·로그아웃·refresh 활동 허용) · 2026-11-10 구현 방향 결정(BE·사용자 — PM 회신만으로 확정 아님) · FE 회신 11-10(화면 흐름 수용) | 공통 규칙 「로그인 세션」·authLogin·authRefresh·authLogout | 구현·시험 완료(#124) · BE 병합·dev 배포 완료(release/1.1.0) · production FE 세션 연결은 로컬 가짜 제공자 검증 완료(P1) · 실제 제공자·배포 HTTPS 미검증. Origin 허용 목록 검사(`PIXLATE_AUTH_ORIGINS`, 비면 거절). access 수명은 `JWT_TTL` 우선(기본 15분). 재사용 탐지 = 세대 + 서명(더 오래된 발급 refresh도 `refresh_reused`로 그 세션 폐기, 서명이 틀린 값은 아무것도 바꾸지 않음). **한계:** 같은 쿠키 동시 재로그인은 새 세션 둘(개발용 — 한 브라우저 한 계정 완전 보장 아님). **FE 11-10 합의:** refresh 뒤 `/auth/me` 확인 순서, 경합 복구 수치(대기 2초·갱신 2회·원 요청 1회·잠금 10초), `origin_not_allowed` 401, 저장소 장애 503 사유 없음, 로그아웃 결과별 처리. refresh 실패별 처리(503 = 세션 종료로 보지 않음·자동 반복 없음 — 서버 상태는 회전 전/후 실패에 따라 다름, `reused`·`invalid` = G1, `rotated` 한도 초과 = G1이지만 서버 세션 유지). 로그아웃 네 항목(같은 계정 두 세션·만료 access·유효 Bearer + 검증 불가 쿠키·자격 없음 204) 사용자 11-10 확정. 검증 불가 쿠키만 있는 로그아웃 = 401 `refresh_invalid` + 쿠키 삭제, 그 값으로는 세션 폐기 없음, FE는 대기 해제·G1·정상 로그아웃과 구분(FE 11-10 최종 합의 · 구현 완료 · 연동 검증 대기). 함께 온 소셜 결속을 서버에서 확인하면 그 결속의 진행 중 로그인·결속 세션은 정리(11-11 결함 수정 — C, 응답 동일, 결속 처리 중 저장소 장애는 503). 로그아웃 세부 중 PM 확정은 정상·다른 계정 두 세션, 나머지 네 항목은 사용자 11-10 확정, 검증 불가 쿠키만은 FE 11-10 최종 합의 | `feature/auth-session`(A #123 위) |
| 브랜드 생성 시점 스냅샷 | PM 11-04 | jobCreate | 계약 확정·서버 구현 대기 | 미정 |
| `/blocks`의 `overflow`는 유지하되 넘침 판정에 쓰지 않음 | FE | `TextBlock.overflow` | 현행 | — |
| `/preview` 공통 렌더 입력·현재 시도/표시 결과·입력 지문, `/blocks` 섹션 입력 지문·지문 A·B·C 비교 | FE 11-04·11-05 | reviewPreview 「렌더 입력과 시도·결과」·응답 예시 6개·`PreviewRender*`·`TextBlock.sectionInputFingerprint`·blockList | 구현 완료·서버 배포 대기(#108 머지 전제). 서버 미완료 1건: ① 하류 실행 없는 N5 섹션의 미리보기 확보. ② 적용 박스를 읽을 수 없는 섹션의 렌더 실패 기록은 구현(#157 · 미병합 · 서버 배포 전 — 7.2 ②) | #108 |
| `/preview` 키 제공 규칙 — `required` 키 항상, 상태별 null(외곽 8키·섹션 16키) | #100 계약안·FE 11-05 | `ReviewPreview` 「키 제공 규칙」 | 구현 완료·서버 배포 대기 | #108 |
| 배치 요청의 `trans1` 필수(현재 공개안) — 생략 허용은 후속 협의(7.3) | FE 11-03 | `BlockPatch`·blockPatch 「블록 배치」·예시 `missingTrans1` | 현행(현행 서버도 `trans1` 필수) | — |
| 서버 렌더러의 FE 공통 렌더 일치(HTML/Playwright·번들) | 실행계획 BE 8번·R-18 | `PreviewRenderer` — 현행 Pillow·`bundleId = null`·자간 미적용 | 미착수(7.2 ③) | 미정 |
| 소셜 로그인(카카오·Google) — start·완료, 자동 가입, 제공자+회원번호 식별, FE 콜백 `/login/callback/{provider}`, PKCE·state·nonce 서버 관리, 한 브라우저 한 세션 교체, 완료 응답 = `LoginResponse`(신규·기존 같은 모양), 오류 `error.code` + `details.reason` (`UNAUTHORIZED`: `state_invalid`·`provider_auth_failed`·`origin_not_allowed` / `DUPLICATE`: `login_in_progress` / `SERVICE_UNAVAILABLE`: `provider_unavailable` 또는 사유 없음), 완료 요청은 같은 코드로 재전송하지 않음, 의도적 비활성 404 · 켰지만 필수 설정 오류 503(공개 오류에 비밀·설정값 없음). **FE 보장과 서버 내부 값 구분:** 시도 수명(C 초기 기본값 10분)·점유 상한(C 초기 기본값 60초)은 서버 설정이며 공개 계약이 아니다 — 명세에는 「수명이 지나면 `state_invalid`」「409는 끝없이 이어지지 않음」「상한 뒤 늦은 제공자 응답은 세션을 만들지 않음」만 싣는다. FE 대기 종료는 FE 요청 timeout | PRD F-ACC-05 · PM 11-09·11-10 · FE F-A 회신 · FE 기술 확인 회신 11-10(F6·start·F7) · 사용자 11-10(U6, 10분·60초 초기 기본값 채택) | 공통 규칙 「소셜 로그인」·authSocialStart·authSocial·`Error.details`·`SocialStartResponse`·`SocialLoginRequest`·`LoginResponse` | 계약 확정·구현·대역 시험 완료(C #128) · BE 병합·dev 배포 완료(release/1.1.0) · production FE 세션 연결은 로컬 가짜 제공자 검증 완료(P1) · 실제 제공자·배포 HTTPS 미검증. 공개 계약(요청·응답·오류·FE 보장)과 서버 보장(브라우저 세션 교체·로그아웃 뒤 늦은 완료 차단·점유 만료·늦은 제공자 응답)을 구분 — 서버 보장은 C에서 구현해 제공자 대역·실제 Redis 경합 시험으로 확인, 브라우저 연동은 로컬 가짜 제공자로만 확인(P1)·실제 제공자 연동 검증 전. 10분·60초는 실제 제공자 코드 유효 시간·외부 호출 timeout과 대조해 C에서 검증 | `fix/auth-social-contract`(#126 명세) · `feature/auth-social`(#128 구현) |
| 간이 로그인 환경별 차단(U7) — 서버 설정 `PIXLATE_AUTH_LOCAL_LOGIN` = `enabled`·`disabled`(미설정 = `enabled`, 빈 문자열 포함 그 밖의 값 = API 기동 실패, 오류 메시지에 입력값 없음). `disabled`면 authLogin 404 `NOT_FOUND` — 새 간이 로그인 발급(판매자·세션·refresh 쿠키)만 막고 본문 필드 검증보다 먼저 판정(빈 본문·필드 누락도 404, JSON 문법 오류는 기존 422). 이미 발급된 세션(local 포함)·refresh·로그아웃·소셜 로그인은 바꾸지 않는다 | 사용자 11-13(U7 결정) | 공통 규칙 「소셜 로그인」 끝 · authLogin | 구현(이 변경)·dev 환경 미적용 — 팀원용 session 환경은 `disabled` 명시 + 외부 공개 전 FE 프록시 경유·BE 직접 모두 차단 확인. 기존 local 세션 정리는 별도 운영 작업 | `fix/auth-local-login-gate` |
| 로그인 허용 목록(시험 기간 운영 설정, 12/7 최종 발표까지 · PRD 밖) — 스위치 `PIXLATE_AUTH_ALLOWLIST` = `enabled`·`disabled`(미설정 = `disabled`, 그 밖의 값 = API 기동 실패)와 허용 이메일 목록 `PIXLATE_AUTH_ALLOWED_EMAILS`를 따로 둠. 켜짐 + 빈 목록 = 아무도 안 됨. 매 로그인(authSocial·authLogin)과 갱신(authRefresh)에서 검사, 목록 밖 = 401 `UNAUTHORIZED` + `account_not_allowed`(소셜·간이 로그인은 계정·세션을 만들지 않음, 갱신은 그 세션 폐기·Set-Cookie 없음). 목록에서 빼기만 하면 이미 발급된 access는 수명까지 유효(갱신 거절로 세션이 폐기되면 그 세션의 access도 다음 요청부터 401). 판정은 제공자 구분 없이 이메일만 비교. 서버 로그(API 계약 아님): **허용 목록 활성화 시 이메일 기록** — 스위치가 켜져 있으면 로그인·계정 생성·거절 로그에 판정·처리에 쓴 이메일을 `%r`로(한 줄 유지) 남기고, 꺼져 있으면 이메일 필드를 생략(판매자 id·제공자는 유지, 계정 생성 전 거절은 판매자 id 없음). 환경 구분 규칙이 아니라 스위치 규칙이며, 스위치를 꺼도 이미 남은 로그는 지워지지 않음 | PM 11-18(Google 테스트 중이라도 기본 범위만이면 목록 밖 계정 로그인 — 제공자 쪽으로 막을 수 없음) · FE 11-18(응답 형태 수용 — 로그인은 G1에 남아 안내, refresh 는 자동 갱신 중단·인증 초기화 뒤 G1 안내) · 사용자 11-19(이메일 로그는 허용 목록 켜짐일 때만) | 공통 규칙 「로그인 허용 목록」 · authSocial · authLogin · authRefresh | 계약 확정·구현·dev 배포(6b96562, 스위치 꺼짐) — FE 안내 분기는 FE release/1.1.0 반영 예정(반영 전에는 일반 401 처리) · 이메일 로그는 `fix/auth-login-log-email`(release 미병합) | `fix/auth-allowlist` · `fix/auth-login-log-email` |
| HTTP `error.code` 15종 정리 — 서버 코드 11개를 공통 코드와 `details.reason`·`details.fields`로 변환(7.7) | FE 11-06 | `Error.details` | 구현 완료·서버 배포 대기 | `fix/int2-error-cancel-contract` |
| 작업 취소 200 `{jobId, status}`·반복 취소 200·없는 작업/남의 작업 404 | FE 11-06 | jobCancel | 현행(서버 변경 없음, 명세·시험 보완) | `fix/int2-error-cancel-contract` |
| `isExcluded` true·false·null 구분, null만으로 새 편집 차단 정책을 만들지 않음 | FE 11-06 | `TextBlock.isExcluded` | 현행 | — |

### 7.2 서버 미완료·의존 작업 추적

응답 표현은 확정됐고 서버 처리만 남은 경로다. 공개 명세는 이 상태를 그대로 표시한다(reviewPreview 「구현 상태」·각 오퍼레이션 「2차 상태」).

| # | 항목 | 지금 동작 | 영향 | 명세 위치 | 상태 |
|---|---|---|---|---|---|
| ① | 진행 중인 하류 실행이 없는 N5 섹션의 미리보기 확보 | 시도를 만들지 않아 `render.attempt = null`로 남는다 | 화면은 원본 + 미리보기 없음 | reviewPreview 「구현 상태」·sectionPatch | 서버 미완료(#108 범위) |
| ② | 적용 박스를 읽을 수 없는 섹션의 렌더 실패 기록 | 이전: 그 섹션의 블록 저장 · 자동 진행 · N4 작업 단위 재시도가 실패 시도를 남기지 못하고 500(롤백)으로 끝났다. 이제: 미리보기 시도를 만들 자리(자동 진행 · N4 작업 단위 재시도 · 블록 저장 뒤 재렌더 · N5 복원)에서 실패 시도(`errorCode: RENDER_INPUT_INVALID` · 재시도 불가)를 남기고 대기열로 보내지 않는다. 상태 전이는 다른 미리보기 실패와 같고(D9-3), 같은 오류 상태로 반복해도 새 시도 · 재시도 횟수 소모가 없으며, 입력을 고치면 정상 미리보기로 돌아온다. 블록 저장 응답 `rerenderTaskId`는 null | 그런 데이터가 실제로 있는지는 DB를 조회하지 않아 모른다. **남은 것**: 이제 그 섹션이 있어도 N5 로 가지만, 확정(최종 렌더 입력)에서는 같은 오류가 그대로 500 이다(확정 전 필수 확인에도 없음) — 처리안은 계약이 필요해 따로 정한다. 섹션 크기를 읽을 수 없는 경우 · 박스 범위 오류(/preview 는 RENDER_INPUT_INVALID)는 이 기록 범위 밖 | reviewPreview 「구현 상태」·blockPatch | 구현(#157 · 미병합 · 서버 배포 전) |
| ③ | 서버 HTML/Playwright 공통 렌더 연결 | 현행 Pillow 조판(`bundleId = null`, 자간 미적용) | FE 로컬 렌더와 서버 이미지가 다를 수 있다 — 넘침·최신 판정은 서버 값만 쓴다 | `PreviewRenderer` | 미착수 — 번들 반입(R-FE-6)·렌더러 전환 이전 작업 처리(PM-R0)에 의존 |
| ④ | 훼손된 저장값이 `/blocks`에 그대로 나가는 문제 | `bbox` 저장값이 형식에 맞지 않으면(문자열·불리언 좌표) `/blocks`가 검사 없이 200으로 돌려줘 `TextBlock` 스키마에 어긋난다. /preview는 같은 데이터를 `inputError`로 알린다. 형식이 맞지 않는 조정값은 블록 배치 구현 이후 `layoutAdjust`·`autoAdjust`를 null로 내고 원본을 저장소에 보존한다 | 그런 데이터가 실제로 있는지는 DB를 조회하지 않아 모른다. 이를 허용하도록 스키마를 넓히지 않는다 | `TextBlock` | 미해결 — 처리 방침 미정 |
| ⑤ | 카테고리 매핑 충돌 500의 `retryable` | `retryable: true`로 나가지만 데이터를 고칠 때까지 결과가 같다 | 명세에는 현재 값을 적었다. 서버 값 변경은 하지 않았다 | jobCreate 500 | 미해결 — 서버 변경 여부 미정 |
| ⑥ | 매핑 대상 밖 400의 `details.fields` | `productName` 누락(`/analyze`)·원본 순서 변경 3종·JSON 객체가 아닌 본문(`PATCH /jobs`)에는 `details.fields`가 없고, 내보내기 구성 오류는 `details.unsupported`다 | `error.code`는 맞다. `productName`은 요청 본문의 값이 아니라 `loc`을 정할 근거가 없어 임의로 만들지 않았다 | `Error.details` | 미해결 — 형식 미정 |
| ⑦ | 정의되지 않은 메서드의 405 | 서버 프레임워크가 내는 405의 `error.code`가 `ERROR`로 15종 밖이다. HTTP 405도 명세에 없다 | 확정 매핑에 없어 바꾸지 않았다. FE가 부르는 경로에서는 나오지 않을 것으로 보지만 확인하지 않았다 | `Error.code` | 미해결 — 처리 미정 |
| ⑧ | 요청 형식 422의 문서화 범위 | 서버는 요청 형식 오류에 422 `VALIDATION_ERROR`를 내지만 응답에 422를 적은 오퍼레이션은 63개 중 6개다(sectionPatch·blockPatch·confirmReview — confirmReview는 `taskId` 형식 오류와 함께 추가 · authLogout(로그인 세션) · authSocialStart·authSocial(소셜 로그인)). 빠진 예: brandCreate | 응답 형식은 `Error`로 맞다. 오퍼레이션별 표기만 빠져 있다 | 각 오퍼레이션 | 미해결 — 처리 미정 |
| ⑨ | 확정된 검사 오류 정책(PM 11-10)의 남은 연결(검사 활성화 전 해결 — PM 추가 승인 대상 아님) | (a) 블록 범위: 블록 하나의 충돌(강제 용어끼리 · 금지/허용 같은 구간)은 그 블록만 failed 로 두고 실행 요청 대상에서 뺀다 — 같은 섹션의 나머지 블록은 실제로 검사(AI 11-11 회신: 규칙 검사에 한한 요청 대상 예외). 섹션 공통 공급 실패(후보 형식 · ID 불일치)만 섹션 단위. (b) 현재 inputKey 를 계산할 수 없으면 failed · `inputKey: null`(API 6-10 — 블록 저장 200, 확정 통과). 대상 언어가 없어 공급 기준을 정할 수 없으면 여전히 500. (c) 고정 요청 객체 저장 실패는 그 입력의 실패로 기록해 failed · 확정 통과(실행 없음 · `CHECK_REQUEST_STORE_FAILED`, API 6-10) | (a)(b)(c) 해결(로컬 구현 · 격리 검증 — 커밋 전) | `BlockCheck`·blockPatch·confirmReview | (a)(b)(c) 구현 완료·서버 배포 대기(기본 꺼짐) |

### 7.3 남은 계약 항목 (명세에 싣지 않음)

후속 협의
- 배치 요청의 `trans1` 생략 허용 — 현재 공개안은 `trans1` 필수(배치만 바꿀 때도 현재 값을 보낸다. 빠지면 422).
- `translation_failed` `taskId` 필수화 시점 — 2차 공개안은 선택 필드.

필드 미확정
- 조건부 규제 판정 구분 — 금지·조건부를 나누는 필드(`regulationVerdict` 후보)의 이름·값. 표시 방식(공통 `prohibited_expression` + 판정 구분)은 FE·PM 확정, 필드는 미정이라 2차 공개안에는 그 필드가 없다.
- `displayTop`과 제외를 반영한 출력 위치의 분리(필드 추가 또는 의미 변경).
- N3 단일 배열 응답.
- `consentId` 필드와 `POST /consents`의 제거 여부.

정책·처리 판단
- P14 — N4 렌더 실패 섹션의 N6 원본 사용과 그 적용 차수.
- P15 — 성공 이력이 없지만 N4 렌더 실패로 단정하지 않는 섹션의 확인·N6 출력(`render_failed` 판별 기준 포함). /preview의 「표시 결과 없음 + 시도 실패」는 화면 표시 조건이며 이 판별을 대신하지 않는다.
- PM-R8 이후 처리 — 최초 판정 기록이 없는 직접 제외 섹션(지금은 409 `restore_basis_missing`).
- PM-R0 — 렌더러 전환 이전 작업 처리(테스트 작업 정리 또는 차단)와 작업 단위 표시 필드.
- A13 — 중간 섹션 이미지 글자 잘림의 오류 표현과 출력 정책 연결.
- 업로드 실패로 남은 draft의 재개·취소 정책.
- 출처 불명 기존 배치 박스의 실제 존재 여부(DB 미조회)와 데이터 정리 방침 — 처리 의미는 7.1(FE 11-09 확정). 0012로 되돌리면 legacy 위에 배율만 저장한 행은 이전 서버 코드가 legacy 박스 대신 원본 bbox로 그리고, 다시 올려도 그 박스는 돌아오지 않는다(되돌리기 한계, `migrations/versions/0013_layout_adjust.py`).
- 렌더러 예외로 그리지 못하는 배율로 확정한 작업 — 최종 렌더가 실패하고 산출물을 만들지 않는다. 재시도해도 같은 입력이고, 2차에서는 전체 취소 외에 고칠 경로가 없다. 미리보기 실패의 `retryable: true`도 같은 입력이면 결과가 같다.
- 블록 배치 배포 전 의존 작업 — `docs/ai-experiments/tools/typeset_overflow_probe.py`·`style_section_align_review_page.py`의 `auto_adjust` 읽기: **도구·문서 대응 완료**(#121, develop `e5908b4`). 두 도구가 `layout_adjust`와 이전 보존본의 `auto_adjust`를 모두 읽는다(`typeset_overflow_probe.applied_layout`, 운영 렌더 입력과 같은 해석). DB 적용·서버 배포·FE 연동 완료를 뜻하지 않는다.

FE 확인
- F24 — N5 미리보기 갱신 실패 때 새 번역문의 로컬 렌더 표시.

FE 구현 (계약은 11-06 확정, 구현은 미완료)
- 생성 타입·수동 타입·Mock 갱신과 오류 처리 회귀 시험.
- 작업 취소 Mock을 200으로 맞추고 반복 취소도 200으로 처리.
- N5 어댑터의 `dto.isExcluded ?? false` 제거와 null 보존.
- `POST /jobs` 생성 결과 불명 처리 — 동일 키·동일 본문 유지, 지속되는 409에서 새 키 자동 발급 금지, 키 없는 불명 요청 자동 재전송 금지, 모든 500 일괄 자동 재시도 금지.

통합 검증
- N5에서 `isExcluded = null`이 나오는 경로와 실제 저장 허용 조건.

미확인
- 저장소 밖 스크립트·모니터링이 이전 오류 코드 값에 기대는지.

서버 내부 계산·직렬화 (외부 계약에 영향 없음)
- 소셜 로그인 시도 수명(C 초기 기본값 10분)·점유 상한(C 초기 기본값 60초) — 실제 제공자 인가 코드 유효 시간·외부 호출 timeout과 대조해 C에서 검증한다. 값이 바뀌어도 공개 계약(FE 보장)은 바뀌지 않는다.
- 소셜 로그인의 발급 직전 원자 처리(점유 재확인·결속의 마지막 시도·로그아웃 시각·브라우저 현재 세션 교체) — C(`feature/auth-social`, #128 — release/1.1.0 병합·dev 배포)에서 구현했고 제공자 대역 + 실제 Redis 시험으로 점유 만료·로그아웃·다른 로그인 시작·늦은 제공자 응답·같은 시도 동시 요청을 확인했다(`tests/test_auth_social.py`). 브라우저(FE 프록시·SameSite) 연동은 로컬 가짜 제공자로 production FE 세션 빌드에서 확인했다(P1, 11-13). 실제 제공자·배포 HTTPS 연동 검증 전이다. 검증 불가 refresh 쿠키만의 로그아웃(401 `refresh_invalid`)이 결속 처리를 건너뛰어 진행 중 로그인이 뒤늦게 세션을 만들던 결함은 11-11 C에서 고쳤다(서버 기록이 있는 결속만 근거, 완료·로그아웃 순서 양쪽 시험). 간이 로그인(authLogin)의 동시 재로그인 한계(새 세션 둘)는 결속이 없어 C에서도 그대로다(이전 계획의 「C에서 함께 해결」은 정정) — 간이 로그인을 환경별로 끄는 설정(`PIXLATE_AUTH_LOCAL_LOGIN=disabled`, 7.1 U7 행)으로 그 환경에서 새 발급을 막아 해소한다.
- 규칙 검사 `inputKey`의 서버 내부 계산 — 의미 입력(고정 사전·용어집 선별 결과·규칙 버전 등)의 세부 표현과 직렬화·해시 방식, 기존 요청 검증 지문(`input_manifest_sha256`)과의 관계. 외부 계약(불투명 문자열·같은지만 비교·null 조건·배치·배율·revision 제외)은 확정이다(`BlockCheck`).
- 공통 렌더 번들 반입·버전 고정 방식(R-FE-6) — 7.2 ③의 선행 조건.

### 7.4 3차(11/27)로 미룬 것

- 이미지 경계 이탈(Q-07) 판정 — 최종 결합 좌표계·`boundaryCheck`·확정 제한 ①~④·409 `image_boundary_exceeded`.
- 글자 픽셀 손실(전부·일부) 판정 — 2차는 넘침으로 채택한다(7.1, PM 11-09). 지금은 섹션별 이미지 안에 글자를 그린 뒤 N6에서 세로로 합치므로 섹션 경계를 벗어난 픽셀은 잘리고 옆 섹션으로 이어지지 않는다. 3차에 중간 섹션 이미지의 글자 손실 검출·방지와 최종 합성 이미지 경계 판정을 구현·검증한다. 서버의 내부 관측 기록(글자 픽셀 보임 여부)은 3차 판정 구현이나 안전성 검증 완료가 아니다.
- N6 최종 경계 판정 검증 인수조건(a~e)·관련 N6 오류 화면·N6 [중단]→N5 복귀.
- 검사 최종 실패·미해결 규제 경고의 확정 때 필수 확인(`check_failed` 신호·`inputKey` 확인 기준).
- 실행 실패 섹션 확인(`acknowledgedSections`).
- 번역문 LLM 규제 판정(F-TRN-03).
- 시스템 자동 조정(F-TRN-04)·`systemAdjust`·시스템 조정이 있을 때 셀러가 명시한 기본값 보존.
- OTC 종류 필수 선택(도입 뒤 임시 M020 새 적용 종료).

### 7.5 문서 사이트

- 배포 경로는 `.github/workflows/deploy-api-docs.yml`대로 둘뿐이다 — `develop`에서 수동 실행(workflow_dispatch), `api-tracking` 브랜치의 문서 변경 push(`docs/**`·워크플로 파일). 그 밖의 브랜치에서 수동 실행하면 배포 job을 건너뛴다. 배포 전 Redocly lint에서 명세 오류가 나면 멈춘다(경고는 통과).
- 원격에 `api-tracking` 브랜치는 아직 없다. 그래서 이 명세가 `develop`에 머지돼도 사이트(https://ai-pixlate.github.io/server/)는 자동으로 바뀌지 않고, `develop`에서 수동 실행해야 갱신된다.
- 「Try it out」: `servers`의 `https://api.pixate.example.com/v1`은 예시 주소라 Swagger 「Try it out」은 실제 서버를 호출하지 않는다. 실제 서버 주소는 아직 정하지 않았다.

### 7.6 3.5.10 보정 이력

버전은 3.5.10을 유지한다. 서버의 지금 동작을 그대로 설명하도록 명세의 오기와 누락을 고친 문서 보정이다. 서버 코드·DB는 바꾸지 않았고, 새 구현이나 서버 배포를 뜻하지 않는다. 서버 쪽 문제를 해결한 것도 아니다 — 남은 항목은 7.2 ④⑤와 7.3에서 추적한다. 오류 코드·작업 취소는 FE 회신 뒤 서버와 명세를 고쳤다(7.7).

| 날짜 | 대상 | 변경 | 이유 |
|---|---|---|---|
| 2026-11-06 | deliverableList `components[].retryAction` | enum의 문자열 `'null'` → 실제 null | 서버의 정상 응답 null을 명세가 거절했다 |
| 2026-11-06 | `TextBlock.isExcluded` | `boolean` → `boolean \| null`, 세 값의 의미 명시 | 판정 전 null은 마이그레이션 0007의 의도된 동작이다 |
| 2026-11-06 | `TextBlock.bbox` | 신규 `BlockBbox`(`number`) 사용. 섹션 `bbox`는 `Bbox`(정수) 유지 | 서버가 블록 좌표를 저장값 그대로 주므로 소수가 올 수 있다. /preview `box`·`LayoutBox`도 이미 `number`다 |
| 2026-11-06 | jobCreate 500·503 | 기존 `Error` 스키마로 문서화, 예시 추가 | 서버가 이미 내는 응답이다 |

FE 타입 재생성이 필요한 것은 `isExcluded`·`retryAction`·`TextBlock.bbox`다.

**응답 대조 기록** — 로컬 시험(develop d517ed3)이 받은 응답을 보정한 명세의 응답 스키마로 검사한 결과다. 응답 1,126건 중 적합 1,117건, 남은 불일치 9건이다.

| 남은 불일치 | 건수 | 상태 |
|---|---|---|
| 스키마 불일치 — 15종 밖 오류 코드(`JOB_NOT_FOUND` 2, `ARTIFACT_NOT_FOUND` 1) | 3 | 7.7 재대조에서 0건 |
| 미문서화 HTTP 상태 — 작업 취소 200 | 3 | 7.7 재대조에서 0건 |
| 스키마 불일치 — 시험이 일부러 훼손한 데이터(`bbox` 2, `autoAdjust` 1) | 3 | 미해결(7.2 ④) |

블록 배치 브랜치만으로 같은 시험을 다시 대조한 결과(develop 2489762 기준, 7.7 반영 전)는 응답 1,126건 중 적합 1,118건, 불일치 8건이었다. 7.7과 합친 통합 상태의 결과는 7.7 끝의 「통합 대조 기록」이다.

이 대조는 호출한 응답의 스키마 적합성만 본다. /preview 59건 통과도 전체 계약·화면 검증 완료를 뜻하지 않는다. 명세 오퍼레이션 62개 중 시험이 부른 것은 27개이고, 실제 서버와 FE 연동은 이 대조에 포함되지 않는다.

### 7.7 3.5.10 오류 코드·작업 취소 보완

버전은 3.5.10을 유지한다. FE 회신(11-06)으로 확정한 매핑을 명세에 싣고 서버를 같은 계약으로 고쳤다. 이 PR이 머지돼도 공개 명세 갱신과 실제 서버 배포는 별개이며, 실제 서버에 배포될 때까지 운영 응답은 이전 코드다.

| 이전 서버 코드 | HTTP | `error.code` | `details` |
|---|---|---|---|
| `JOB_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: job_not_found` |
| `BRAND_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: brand_not_found` |
| `SECTION_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: section_not_found` |
| `BLOCK_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: block_not_found` |
| `LOGO_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: logo_not_found` |
| `SOURCE_IMAGE_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: source_image_not_found` |
| `ARTIFACT_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: artifact_not_found` |
| `SELLER_NOT_FOUND` | 404 | `NOT_FOUND` | `reason: seller_not_found` |
| `INVALID_INPUT` | 400 | `VALIDATION_ERROR` | `fields: [{loc, msg}]` |
| `INVALID_TOKEN` | 401 | `UNAUTHORIZED` | `reason: invalid_token` |
| `RETRY_LIMIT_EXCEEDED` | 409 | `RETRY_NOT_ALLOWED` | `reason: retry_limit_exceeded` |

- HTTP 상태·`message`·`retryable`·`traceId`와 기존 `details`는 유지한다.
- 이미 `NOT_FOUND`로 나가던 경로(작업 생성의 브랜드, `PATCH /jobs/{jobId}`, 원본 순서 변경)에도 같은 `reason`을 붙였다. 없는 task는 합의된 사유가 없어 `reason` 없이 둔다.
- 없는 리소스와 남의 리소스의 응답은 `traceId`를 빼고 같다. `reason`은 리소스 종류만 나타낸다.
- `authMe`·`authRefresh`에 404(`seller_not_found`)를 문서화했다. 서버가 이미 내던 응답이다.
- 현행 실행의 재시도 한도 초과는 전에도 `RETRY_NOT_ALLOWED`였고 `reason`을 더하지 않았다. `retry_limit_exceeded`는 0008 이전 실행 행에서만 나온다.

**응답 대조 기록** — 7.6과 같은 8개 시험 파일을 이 브랜치(develop 2489762 기준)에서 다시 실행한 결과다. 응답 1,126건 중 적합 1,123건, 불일치 3건이다.

| 항목 | 7.6 | 재대조 | 상태 |
|---|---|---|---|
| 15종 밖 오류 코드 | 3 | 0 | 서버·명세 변경 후 측정값. 서버 배포 전 |
| 미문서화 작업 취소 200 | 3 | 0 | 명세 변경 후 측정값 |
| 훼손 데이터(`bbox` 2, `autoAdjust` 1) | 3 | 3 | 미해결(7.2 ④) |

이 대조는 시험이 호출한 응답의 스키마 적합성만 본다. 오류 정합성 전체가 해결됐다는 뜻이 아니다 — 7.2 ④⑤⑥⑦⑧이 남아 있고, FE 구현과 실제 서버·FE 연동은 이 대조에 포함되지 않는다.

**통합 대조 기록 (11-08)** — 7.7이 반영된 develop `ddc8043`에 블록 배치 브랜치를 병합한 커밋 `cedb608`과, 추가 시험 `tests/test_layout_adjust_upgrade_path.py`를 포함한 작업 트리에서 로컬 전체 시험을 한 번 실행하고 받은 응답을 명세 스키마로 검사했다. 그 뒤 병합한 develop `1b1edf1`(#110)은 `docs/ai/`만 바꿔 로컬 시험을 다시 돌리지 않았다. 최종 커밋은 CI로 확인한다.

| 범위 | 응답 | 적합 | 불일치 | 내용 |
|---|---|---|---|---|
| 7.6과 같은 8개 시험 파일 | 1,126 | 1,124 | 2 | 훼손 `bbox` 2(7.2 ④) |
| 전체 시험 | 2,110 | 2,106 | 4 | 훼손 `bbox` 2(7.2 ④), 미문서화 `brandCreate` 422 1(7.2 ⑧), 없는 경로 시험 호출 1 |

- 15종 밖 오류 코드·미문서화 작업 취소 200·훼손 `autoAdjust`는 0건이다.
- 전체 시험은 2,279건 통과, 10건 실패, 49건 건너뜀이다(용어집 시험 DB 지정). 실패 10건은 블록 배치가 없는 트리에서도 같다: `google` 모듈 없음 4, `test_sample_generator_is_reproducible` 1(원인 미확인), 용어집 다른 DB 위치 시험 4, `test_dictionary_db`의 head 고정값(`"0006"`) 1.
- 마이그레이션 head는 `0013` 하나다. 기존 데이터가 있는 0012에서 올리고, API로 legacy 보존·명시적 해제·셀러 조정을 거쳐 되돌린 뒤 다시 올리는 경로는 `tests/test_layout_adjust_upgrade_path.py`가 일회용 시험 DB에서 확인한다.
- 실제 DB 적용, 서버 배포, FE 연동은 이 기록에 포함되지 않는다.

### 7.8 작업 상태 10종 · 24시간 자동 취소 · 처리 시간 상한 (PRD v3.6.4 · 3차)

[PRD v3.6.4 · PM 확정(11-24 · 11-25) · 계약안 — 11/26 FE 공유 · 로컬 구현 · 미배포] 실행계획 G17 15번(archived → cancelled) · 24번(24시간 자동 취소) · 25번(처리 시간 상한) · 10번 중 「상태 구분」 — 번호는 PM 후속 번호다(실행계획 v3.6.4 G17 목록은 23번까지). 10번의 「N6 렌더 실패 → [다시 시도] · [중단](→ N5)」은 이 절에 넣지 않는다(기존 10번 일정).
이 절이 develop에 머지돼도 실제 서버 응답은 서버 배포 때 바뀐다. 계약 공유 · Mock 통과는 실제 연동 완료가 아니다.

| 구분 | 내용 | 상태(11-25) |
|---|---|---|
| PM 확정 | 상태 10종 · `cancelled` · 대상 5상태 · 24시간 · 30분 · `PROCESSING_TIMEOUT` · 삭제 범위 · 늦은 결과 미채택 · 일정 · **N6 렌더 실패 → 자동 재시도가 없거나 끝나면 바로 `failed · N6`(PM 11-25 — PRD 개정 불필요)** | 다시 묻지 않음 |
| 계약안(확정 전) | 이 절의 응답 · 전이 · 409 표 · 「마지막 움직임」으로 세는 요청 목록(④) | 11/26 확정 · FE 공유 |
| PM 확인 대기 | 작업 이벤트 나머지 목록(⑦) | 일정안은 작업 계획 |
| 로컬 구현 | `feature/job-auto-cancel` — 독립 검토 AC-01~03 수정 포함 · 경합 결함 독립 재검토 완료 | develop 병합은 11/26 FE 계약 공유 뒤 |
| 검증 | 로컬 일회용 PostgreSQL 시험만 — 실제 서버 · FE 연동 · 실제 24시간/30분 대기 없음 | 11/28 dev 공동 확인 예정 |
| 배포 | dev · 운영 모두 미배포(0017 은 서버 코드와 함께만) | 별도 승인 |

#### ① 상태 필드 3개 — 서로 다른 필드다

| API 필드 | DB | 값 | 뜻 |
|---|---|---|---|
| `status`(Job) · `jobStatus`(JOB-05) | `job.status` | 6종 `draft · processing · review · done · failed · cancelled` | 처리가 도는 중은 `processing`, 셀러 차례는 `review`(PRD 05 A32) |
| `currentStep` | `job.current_step` | `N1`~`N6` | 화면 단계. 오류는 실패 지점, 취소는 중단 지점을 유지 |
| `userFacingStatus` | `job.user_facing_status` | 10종(아래 ②) | 사용자용 상태(PRD 05 · F-JOB-04) |

`processing`은 `status` 값이고 `rendering`은 `userFacingStatus` 값이다. 새 `status` 값은 `cancelled` 하나뿐이다(`archived`의 이름 바꾸기).

#### ② 상태 대응표 (PRD 05 상태표 그대로)

| 화면 상태 | `userFacingStatus` | `status` | `currentStep` | 이번 변경 |
|---|---|---|---|---|
| 생성됨 | `draft` | `draft` | N1 | — |
| 분석중 | `analyzing` | `processing` | N2 | — |
| 섹션확인 | `section_review` | `review` | N3 | — |
| 번역중 | `translating` | `processing` | N4 | — |
| 검수 | `reviewing` | `review` | N5 | — |
| 최종 렌더 중 | `rendering` | `processing` | N6 | **새 값.** 지금은 `review · N6 · reviewing` |
| 저장 대기 | `awaiting_save` | `review` | N6 | **새 값.** 지금은 `review · N6 · reviewing`(렌더 중과 구분 안 됨) |
| 완료 | `done` | `done` | N6 | — |
| 오류 | `failed` | `failed` | 실패 지점(N2 · N4 · N6) | N6 오류는 새 경로(③) |
| 취소 | `cancelled` | `cancelled` | 중단 지점 | **이름 바꾸기.** 지금은 `archived` |

- 기존 작업의 저장 값도 바꾼다: `archived` → `cancelled`(두 필드), 렌더가 성공한 `review · N6 · reviewing` → `awaiting_save`, 렌더가 돌고 있는 것 → `processing · N6 · rendering`, 최신 최종 렌더의 현재 시도가 실패했거나 실행이 실패로 닫힌 것(한도가 남은 것 포함) → `failed · N6 · failed`(③). 최종 렌더 실행이 없는 옛 N6 행은 그대로. 마이그레이션 한 번으로 바꾼다(0017).

#### ③ 바뀌는 전이

| 계기 | 지금 | 변경 뒤 |
|---|---|---|
| CFM-04 확정(N5 → N6) | `review · N6 · reviewing`, 응답 `status: review` | `processing · N6 · rendering`, 응답 `status: processing` |
| 최종 렌더 성공 | 바뀌지 않음(`review · N6 · reviewing`) | `review · N6 · awaiting_save` |
| 최종 렌더 실패 — 재시도 가능한 첫 실패 | 자동 재시도 1회(기존 기능) | 같음 — 상태는 `processing · N6 · rendering` 그대로 |
| 최종 렌더 실패 — 자동 재시도가 없을 때(재시도 불가 실패, 또는 자동 재시도 1회 뒤 실패), 한도 남음 | 상태 그대로(`review · N6 · reviewing`). JOB-06 · FIN-01 [다시 시도] 가능(기존 기능) · 저장 409 | **`failed · N6 · failed`**(PM 확정 11-25). [다시 시도]는 JOB-05 `retryable` 에 따름(JOB-06 · FIN-01, 기존 재시도 경로) → `processing · N6 · rendering`. 저장 409 |
| 최종 렌더 실패 — 누적 한도 소진 | 실행 failed, JOB-06 · FIN-01 · 저장 409, 상태 그대로 — 나갈 길은 취소뿐(기존) | `failed · N6 · failed`, 재시도 409. [중단](→ N5)은 G17 10번 일정이라 아직 없다 — 셀러 차례라 24시간 자동 취소 대상(④) |
| 저장 대기에서 FIN-01 수동 재렌더 | 바뀌지 않음(새 최종 렌더 실행) | **409 `INVALID_STATE`**(PM 11-25 — 저장 대기 재렌더 차단, 새 실행 · 시도 없음, 사유 값은 합의 전이라 두지 않음). 렌더 오류(`failed · N6`)의 [다시 시도] · 처리 중 중복 요청(진행 중 시도 반환) · 저장 뒤(`done`) 재렌더는 그대로 |
| FIN-06 저장 | `done · N6 · done` | 같음(저장 대기에서만) |
| 처리 시간 초과(⑤) | 없음(처리 중에 계속 남음) | `failed · (그 단계) · failed` |
| 취소(JOB-04 · 자동 취소) | `archived`, 단계 유지 | `cancelled`, 단계 유지 |

#### ④ 24시간 자동 취소 (G17 24번)

- **대상 5개 상태**(PM 11-24 「사용자 차례」): 생성됨(작업이 있는 N1 — `draft · N1`) · 섹션확인(`review · N3`) · 검수(`review · N5`) · 저장 대기(`review · N6`) · 오류(`failed`). 작업이 아직 없는 N1은 대상이 없다(작업 행이 없음).
- **대상 아님:** 처리 중(`processing` — 분석중 · 번역중 · 최종 렌더 중)은 끝까지 처리하거나 ⑤로 오류가 된 때부터 센다. 완료(`done`) · 취소(`cancelled`). 시연 예외 없음.
- **기산점 = 마지막 움직임.** 아래 중 가장 늦은 때.
  - 작업 생성 시각.
  - 상태 · 단계가 바뀐 때(서버 처리가 끝나 단계가 넘어간 때 포함).
  - 그 작업을 바꾸는 요청이 성공한 때 — `/jobs/{jobId}` 아래 `POST` · `PATCH` · `DELETE` 중 아래 목록. 변경과 **같은 트랜잭션에서** 시각을 쓴다(변경이 커밋되면 시각도 커밋, 롤백되면 둘 다 없음). `PUT` 엔드포인트는 없다.

    | 센다(변경이 커밋된 때) | 세지 않는다 |
    |---|---|
    | JOB-01 `PATCH /jobs/{jobId}` · ANL-01 `POST …/analyze` · SEC-03 `PATCH …/sections/{sectionId}` · SEC-04 `POST …/sections/proceed` · CFM-02 `PATCH …/blocks/{blockId}` · CFM-04 `POST …/confirm` · JOB-06 `POST …/tasks/{taskId}/retry` · JOB-07 `POST …/abort` · FIN-01 `POST …/render` · FIN-04 `POST …/export` · FIN-06 `POST …/save` · SRC-02 `POST …/source-images` · SRC-03 `PATCH …/source-images/reorder` · SRC-04 `DELETE …/source-images/{imageId}` | 모든 `GET`(JOB-05 폴링 포함) · JOB-04 `DELETE /jobs/{jobId}`(취소 자체) · 커밋 전에 끝난 실패 응답(4xx — 상태 · revision 충돌 등) · 멱등 재전송(최초 응답 재사용, 새 커밋 없음) |

    값이 같아 바뀐 것이 없는 저장(CFM-02 no-op 등)도 요청이 성공해 커밋하면 센다. 커밋하려는 순간 그 작업이 이미 취소돼 있으면(자동 취소가 먼저 작업 행을 잠금) 변경을 커밋하지 않고 409 `INVALID_STATE`를 돌려준다 — 취소된 작업에 성공 응답을 내지 않는다.
- **판정:** 서버 점검이 마지막 움직임에서 24시간이 지난 대상 작업을 취소한다. 점검은 앞 주기가 끝난 뒤 1분 쉬고 다시 돈다 — 실제 취소 시각은 「24시간이 지난 뒤 처음 도는 점검」이며, 점검 자체에 걸린 시간 · 건너뛴 주기(아래) · 복구 프로세스가 멈춰 있던 시간만큼 늦어질 수 있다. **늦어짐의 상한은 보장하지 않는다**(보통은 1분 남짓). 그 순간 다른 요청(내보내기 · 편집 등)이 작업 행을 잡고 있으면 점검은 기다리지 않고 그 작업을 이번 주기에서 건너뛴다(다음 주기에 다시 본다 — 그 요청이 성공해 커밋하면 마지막 움직임이 새로 찍힌다). 처리 시간 상한(⑤) 점검도 같다.
- **처리 = JOB-04 취소와 같다.** `cancelled`, 단계 유지, 진행 중 처리 없음(대상이 셀러 차례뿐이라). 지우는 것과 남는 것:

| 지운다 | 남긴다 |
|---|---|
| 원본 · 섹션 · 블록(문구) · 판정 · 산출물 · 내보내기 · 수정 신호 · 키워드 · 처리 인계와 그 파일, S3 `source/` · `verified/` · `staging/` · `deliverable/` · `export/` · `inpaint/` · `input/` 접두사 | 작업 행(상태 · 멈춘 단계 · 국가 · 언어 · 규제분류 · 브랜드 · 카테고리 · 시각 — 상품명 · 상품코드는 지움) · 처리 시도 기록(오류 메시지는 지움) · 작업 이벤트 · 사용량 · 감사 로그의 행위(내용은 지움) |

  - 변경: 지금 취소는 작업 이벤트(`event_log`)도 지운다. PRD F-JOB-07 H(남는 것 = 작업 이벤트)에 맞춰 **지우지 않는다**. 이벤트에는 문구를 담지 않는다.
- **사유 기록:** 사용자 취소 / 자동 취소는 작업 이벤트에 사유로 남긴다(PM 11-24). API 응답에는 없다(아래 FE 협의 ⓐ).
- **자동 취소된 작업에 조작했을 때:** 사용자가 취소한 작업과 같다.
  - `GET /jobs/{jobId}` · JOB-05 → 200, `status`·`userFacingStatus` = `cancelled`.
  - JOB-01 PATCH · ANL-01 · SEC-03 · SEC-04 · CFM-02 · CFM-04 · FIN-01 · FIN-06 · JOB-07 → 409 `INVALID_STATE`(블록 · 섹션 PATCH는 `details.reason: job_not_editable`), JOB-06 → 409 `RETRY_NOT_ALLOWED`. 지금과 같다.
  - **변경:** SRC-02 업로드 · SRC-03 순서 · SRC-04 삭제 · FIN-04 내보내기는 지금 상태 검사가 없어 취소된 작업에도 새 데이터가 생긴다 → 409 `INVALID_STATE`(사유 없음)로 막는다. 이 검사는 작업 행 잠금 아래서 한다.
  - **FIN-04 내보내기와 취소가 겹칠 때:** 내보내기는 작업 행을 잠근 채 읽기 · 파일 업로드 · 산출물 기록 · 커밋까지 한다. 취소가 먼저 잠그면 내보내기는 409, 내보내기가 먼저면 취소는 그 커밋 뒤에 진행해 방금 만든 산출물 행 · 파일까지 지운다(어느 순서든 취소 뒤 산출물이 남지 않는다). 그동안 같은 작업의 사용자 취소(JOB-04)는 내보내기 시간만큼 기다린다. 24시간 자동 취소 점검은 기다리지 않고 그 작업을 이번 주기에서 건너뛰며, 내보내기가 성공하면 마지막 움직임이 새로 찍혀 취소되지 않는다. 복구 점검에서 작업 행을 잠그는 다른 단계(권한 만료 · 집계 복구 · 취소 잔여 정리)도 처리 중 작업에 한정되지 않으므로(예: N5 검수 · N4 오류 작업의 하류 실행도 집계 복구 대상) 같은 방식으로 잠긴 작업을 그 주기에서 건너뛴다 — 기능은 그대로이고 다음 주기에 다시 본다. 인계 · 대기 재전달은 작업 행을 잠그지 않는다. (FIN-04 는 상태 제한이 없다 — FE 가 어느 상태에서 내보내는지는 확인하지 않음.)
  - JOB-04 다시 취소 → 200(반복 취소 허용, 지금과 같음).

#### ⑤ 처리 시간 상한 30분 (G17 25번)

- **대상:** 분석중(`processing · N2`) · 번역중(`processing · N4`) · 최종 렌더 중(`processing · N6`). 원인과 상관없다 — GPU가 꺼진 경우 포함.
- **기산점:** 그 상태에 들어간 때(`status` 또는 `currentStep`이 바뀐 시각). 대기 포함. [다시 시도]로 다시 `processing`이 되면 처음부터 다시 센다.
- **판정:** 서버 점검(앞 주기가 끝난 뒤 1분 쉬고 다시 돎 — 늦어짐 상한은 ④와 같이 보장하지 않음)이 30분이 지난 작업을 `failed · (그 단계) · failed`로 보낸다. 재시도 한도 · 렌더 실패 같은 원인별 규칙이 먼저이고, 이것은 그걸로 끝나지 않은 작업을 거두는 안전망이다.
- **실패 사유:** 그 단계에서 끝나지 않은 처리 시도(대기 · 실행 중)를 `failed`로 닫고 `errorCode: PROCESSING_TIMEOUT`(task `errorCode` — 자유 텍스트 값 공간, HTTP `error.code` 아님)을 단다. JOB-05 `items[]`에 그대로 보인다. N4도 섹션 실행 실패가 아니라 작업 오류다.
  - 닫은 시도에 등록됐지만 아직 채택 전인 인계는 같은 자리에서 거절로 닫는다(`reject_reason: processing_timeout`, 산출물 미채택) — 채택 메시지 처리를 기다리지 않는다(배포 점검 gate 는 열린 인계를 busy 로 센다).
  - N4 인페인트 시도를 닫으면 인페인트 실패와 같은 대체 기록을 남긴다(원본 배경 · `inpaintStatus: failed` · `warningBadge: processing_failed`). 이때 자동 재시도 · 미리보기 · 다음 단계 전이는 만들지 않는다(오류 상태 · 수동 재시도). 번역 [다시 시도]로 N5 에 가면 그 섹션은 원본 배경에 경고가 보이고, [중단] → N3 → 다시 진행해 인페인트가 성공하면 경고는 지워진다.
- **오류 뒤(PM 11-25 변경 — 시간 초과 뒤에도 사용자 재시도 가능, 재시도로 다시 처리 중이 되면 30분을 새로 센다):**
  - N2: ANL-01로 다시 분석(새 분석 실행 · 실행별 한도 — 기존과 같음) · JOB-07 [중단] → N1.
  - N4: JOB-06(번역 · 렌더 시도 — 같은 실행 안 새 시도, 누적 한도 — 기존과 같음) · JOB-07 [중단] → N3. 끝나지 않은 시도가 인페인트면 JOB-06 대상이 아니다(기존 계약) — 그 경우의 재시도 경로는 PM 확인 중(지금은 [중단] → N3 → 다시 진행).
  - N6: 시간 초과는 시도만 실패로 닫고 최종 렌더 실행은 남긴다 → JOB-06 · FIN-01 [다시 시도]가 같은 실행 안에 새 시도를 만든다. 누적 한도 소진 · 고정 입력의 렌더러/FE 번들 불일치 · 기록 누락이면 그대로 재시도 불가(JOB-05 `retryable: false` · 409). [중단](→ N5)은 G17 10번 일정.
  - 한도를 소진한 시간 초과(N4 · N6)까지 재시도를 허용할지는 PM 확인 중 — 지금은 기존 누적 한도대로 불가.
  - 중복 재시도 요청: 새 시도는 **최대 1개**. FIN-01 은 재시도가 이미 진행 중이면 그 시도를 돌려주고(202), JOB-06 은 이미 대체된 옛 시도 id 로 다시 오면 409 `RETRY_NOT_ALLOWED`(현재 실패 시도만 재시도).
  - 모두 셀러 차례라 ④ 대상(24시간 뒤 자동 취소).
- **결과 채택과 시간 초과의 순서:** 둘 다 같은 작업 행 잠금 아래서 일어나고, 먼저 끝난 쪽이 정한다(점검은 잠근 뒤 상태를 다시 본다).
  - 30분이 지났더라도 **점검보다 정상 결과 채택이 먼저 끝나면** 그 결과를 쓴다(단계가 넘어가 처리 중이 아니므로 점검은 아무것도 하지 않는다).
  - **시간 초과가 먼저 확정되면** 그 뒤 도착한 결과는 아래처럼 거절한다.
  - 점검은 앞 주기가 끝난 뒤 60초 쉬고 다시 돌므로(`python -m app.recovery --interval 60`), 30분을 넘긴 뒤 실제 판정까지 그만큼 늦을 수 있다.
- **늦게 끝난 결과는 채택하지 않는다.** 시간 초과로 닫은 시도는 실행 권한 세대(lease epoch)를 올린다. 그 뒤 워커가 결과를 보고해도 — 서버 안 워커는 권한 없음으로, 원격 워커는 늦은 인계 거절 기록만 남기고 — 결과를 DB에 반영하지 않으며, 작업 상태 · 단계를 바꾸지 않고, 다음 단계 처리도 시작하지 않는다(오류 유지).
  - 영구 제외: 시간 초과로 닫은 시도(`failed` + `PROCESSING_TIMEOUT`)는 그 뒤 다른 시도의 [다시 시도]로 작업이 다시 `processing`이 돼도 인계 등록 · 채택 권한 인수 · 최종 채택에서 모두 거절한다(인계 `reject_reason: processing_timeout`, 산출물 미채택). 권한 만료(`LEASE_EXPIRED` 등)로 실패한 현재 시도의 늦은 인계를 처리 중에 저장 복구로 채택하는 기존 규칙과는 다르다 — 그 규칙은 그대로다.
  - 다시 시도한 다른 시도(예: 번역)가 끝나면 그 결과는 정상으로 쓰고, 그 뒤 만드는 미리보기도 기존 규칙을 따른다. 시간 초과로 닫은 인페인트의 늦은 결과는 섹션 배경 · 미리보기 입력에 들어가지 않는다.

#### ⑥ FE 협의 항목 (11/26 공유 · 11/28 확인)

- ⓐ **자동 취소 안내 문구**(PM 초안 「24시간 동안 움직임이 없어 작업이 취소되었습니다.」 → 확인 → 보관함): 지금 응답에는 사용자 취소와 자동 취소를 가르는 값이 없다. 응답에 구분 값을 둘지(새 `details.reason` 또는 필드 — 계약에 없는 값이라 PM · FE 합의 뒤에만) 「취소된 작업」 공통 안내로 둘지 정해야 한다.
- ⓑ **새 값 처리:** `cancelled` · `rendering` · `awaiting_save`. 지금 FE는 `currentStep`으로 화면을 고르고 `jobStatus`는 `failed`만 본다 → N6 `failed`(③ 자동 재시도 없는 렌더 실패 — 한도가 남으면 [다시 시도] 가능 · ⑤ 시간 초과)를 N6 화면에서 어떻게 보일지.
- ⓒ **409 처리:** 취소된 작업에 조작하면 409를 받는다. FE 11-25 회신: 현재 FE 공통 호출(`ApiRequestError`)은 HTTP status와 응답 body를 보존한다(앞서 적은 「상태 코드를 버린다」는 과거 조사 시점 서술 — 정정). 합의한 처리(FE 11-25 계약 동의 — FE 구현 · 검증 완료 아님): 409를 받으면 작업을 다시 조회해 실제 `cancelled` 또는 `archived`(전환 기간 — ⓓ)일 때만 취소 안내 · 보관함 이동, 그 밖의 409는 기존 오류 처리.
- ⓓ **호환(적용 순서):** 서버가 먼저 바뀌어도 FE는 `currentStep` 기준이라 화면 선택은 같다. `archived`와 `cancelled`를 둘 다 취소로 다루면 순서와 상관없다. FE가 먼저 바뀌면 옛 서버의 `archived` · N6 `reviewing`을 그대로 받는다.

#### ⑦ 남은 것

- (확정 11-25) N6 렌더 실패 → `failed · N6` 전이(③): **자동 재시도가 없거나 끝난 실패면 바로 `failed · N6`**, 사용자 [다시 시도]는 JOB-05 `retryable` 에 따른다. PM 결정 — PRD 개정 불필요. `retryable: false` 대표 사례: 재시도 한도 소진 · 고정 입력의 렌더러 · FE 번들 불일치 또는 기록 누락. (PM 11-25 변경: 시간 초과 자체는 더 이상 재시도 불가 사유가 아니다 — ⑤. 한도를 소진한 시간 초과는 PM 확인 중.) [중단](→ N5)은 G17 10번 일정.
- 작업 이벤트(G17 3번)의 전체 목록 — PRD에 이벤트 이름 · 시점 · 값 목록이 없다(NFR-29 종류만). 이번에는 취소(사유) · 시간 초과 오류만 남긴다.
- 적용 순서: 0017 은 이전 서버 코드와 같이 쓰면 안 된다(옛 코드는 취소를 `archived` 로만 검사하고 시각 열을 모른다). 0017 downgrade 는 열 · 값만 되돌리며 자동 취소로 지운 콘텐츠는 되살리지 않는다.
  - **DB 에 직접 붙어 app 코드를 실행하는 프로세스는 모두 같은 판으로 함께 바꾼다**: API · CPU 워커 · 복구 프로세스, 그리고 원격 GPU 호스트의 OCR 워커(N2 분석)와 GPU 워커(인페인트). 저장소 코드 기준 GPU 제어 경로는 같은 프로세스 호출(InProcess — `gpu/worker.sh` 가 `DATABASE_URL` 을 요구)이고, 옛 코드는 작업 상태 `cancelled` 를 취소로 보지 않는다. 로컬 혼합 판 검증(11-25): 옛 GPU 코드가 **취소 전에 얻은 권한**으로 취소 뒤 결과를 등록하면 rejected 인계 · 산출물 메타데이터 행이 남는다. 취소 뒤 **새 권한 획득은 막힌다**. 그 인계의 실제 채택 · S3 파일 잔존 · 옛 OCR 워커 조합은 검증하지 않았다. 「옛 GPU 워커 유지 가능」은 **성립하지 않는다**(이전 판 설명 철회).
  - 새 코드를 0017 전 DB 에 붙이면 상태 전이 호출에서 실패한다(로컬 확인). 기동 시 판 불일치를 자동으로 알아채는지는 검증하지 않았다 — 적용 뒤 `alembic_version` 을 따로 확인한다.
  - 실제 dev GPU 호스트의 제어 경로 · 실행 판은 아직 확인하지 않았다 — 확인 블록 · 혼합 판 결과는 작업 계획 문서.
- 이 삭제형 자동 취소는 「작업 보존 · 계정별 재개」 요구의 구현이 아니다(재진입은 이후).
