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
| 셀러가 고칠 수 없는 검사 오류(용어 공급 실패·강제 용어 충돌·`input_invalid`·`bundle_invalid`·사전 묶음 변환 실패·검사 보고/요청 무결성)도 failed — 확정 통과(등록 불가 섹션도 N4에서 멈추지 않음), 다른 섹션 조회·편집 유지. 범위: 실행된 시도는 서버가 고정한 대상 블록, 섹션 공통 공급 실패는 섹션, 블록 하나의 충돌(강제 용어끼리 · 금지/허용 같은 구간)은 그 블록만 — 나머지 블록은 실제 검사(7.2 ⑨) | PM 11-10 | `BlockCheck.state`·confirmReview | 구현 완료·서버 배포 대기(기본 꺼짐) | `feature/translate-check-connect` |
| 미리보기 재렌더 중 확정 허용·넘침 경고 비차단 | PM | confirmReview | 현행 | — |
| 미저장 번역문 확인창 | PM | confirmReview | 화면 | — |
| 필수 확인 = `translation_failed`, `mandatory_term_unapplied` 제외, 블록·코드 단위 한 번 확인 | PRD F-CFM-09·PM 재확인 11-04 | ConfirmRequest | 현행 | — |
| `translation_failed` 확인 기준 `taskId`(선택 필드) | FE 11-04 | `ConfirmRequest.acknowledgedWarnings[].taskId`·`ReviewSignal.taskId`·예시 `acknowledgeTranslationFailed` | 구현 완료·서버 배포 대기(생략 = 기존 판정, 명시적 null = 422) | `feature/confirm-ack-task-id` |
| 확인 기록 감사 로그 | PM C3 | ConfirmRequest | 계약 확정·서버 구현 대기(대상·action 이름·revision 의미 미정) | 미정 |
| 섹션 실행 실패 출력 — 인페인팅 실패 = 원본 위 번역문, 이전 성공 이미지가 있는 N5 재렌더 실패 = N6에서 확정 번역문으로 다시 렌더 | PM 11-04 | confirmReview 「섹션 실행 실패의 출력」 | 현행 | — |
| 표시 결과 없이 렌더 시도가 실패한 섹션의 N5 원본 표시(화면 표시 조건 — N4 렌더 실패 판별이 아니다. N4 판별·N6 출력은 P14·P15) | PM 11-04·FE(client PR #44) | confirmReview 「섹션 실행 실패의 출력」·reviewPreview 「표시 결과가 없을 때」 | 화면 | — |
| category leaf만 허용 | PRD F-SRC-04 J41② | `JobCreate.categoryId`·`JobPatch.categoryId` | 계약 확정·서버 구현 대기 | 미정 |
| N3 포함 섹션 판정(목록도 판정 전부) | PRD F-SEC-02·F-SEC-04 | `Section.verdicts`·`SectionList`·sectionList | 계약 확정·서버 구현 대기 | 미정 |
| N3 원래 순서(`sectionOrder` 정렬)·섹션 카드 버튼 | PRD F-SEC-02·F-SEC-03 | `Section.sectionOrder`·`SectionPatch` | 현행(정렬·버튼은 화면) | — |
| OTC 종류 규칙·임시 M020·2차 M020 적용 규칙 ①~⑥ | PM 11-04·AI 11-04·11-05 | `JobPatch.regulatoryClass` | 계약 확정·서버 구현 대기(OTC 필드는 새 규제사전 작업, 백필은 대상 조사·실행 승인 뒤) | 미정(#103 계열) |
| 작업 생성 시점·책임 동의, `consentId` 미사용 | PRD F-ACC-04 | jobCreate·consentCreate | 현행(동의 체크는 화면) | — |
| `Seller.email` null 허용(소셜 계정 대비 — 계정은 제공자 + 회원번호로 구분, 이메일은 식별에 쓰지 않음), 이메일 간이 로그인(개발용)은 local 계정만 찾고 만듦 | PRD F-ACC-05 · FE 회신 11-09(타입 수용) · PM 11-09(이메일 수집·용도) | `Seller.email`·authLogin·authMe | 구현 완료·서버 배포 대기(소셜 로그인 API는 아래 「소셜 로그인」 행) | `feature/auth-account-identity` |
| 로그인 세션(S안) — **정본 「refresh 서버 상태 없음」 변경**: 로그인마다 서버 세션(Redis), access는 세션에 묶여 요청마다 확인, refresh 쿠키 회전(동시 요청 하나만 성공·30초 유예·재사용 시 그 세션 폐기·거절은 Set-Cookie 없음), 비활성 7일·최대 30일, **활동 = 로그인·refresh 성공만**(시연용 허용 — 실제 조작 기준 정책과 다름), 로그아웃 = 자격이 가리키는 살아 있는 세션만 폐기(쿠키는 서명이 맞으면 세대와 관계없이 식별), 유효한 refresh 쿠키만으로도 로그아웃(계약 — FE 요청), 이미 종료된 세션의 재로그아웃 204(FE 11-10), 검증할 수 없는 자격은 401 `invalid_token`·무변경, 같은 브라우저 재로그인은 이전 세션 폐기, 세션 없는 이전 access 401, 저장소 장애 503(사유 없음) | PM 11-09(7일·30일·서버 무효화)·11-10(다중 탭·로그아웃·refresh 활동 허용) · 2026-11-10 구현 방향 결정(BE·사용자 — PM 회신만으로 확정 아님) · FE 회신 11-10(화면 흐름 수용) | 공통 규칙 「로그인 세션」·authLogin·authRefresh·authLogout | 구현·시험 완료(#124 Draft, develop 미머지)·서버 배포 대기. Origin 허용 목록 검사(`PIXLATE_AUTH_ORIGINS`, 비면 거절). access 수명은 `JWT_TTL` 우선(기본 15분). 재사용 탐지 = 세대 + 서명(더 오래된 발급 refresh도 `refresh_reused`로 그 세션 폐기, 서명이 틀린 값은 아무것도 바꾸지 않음). **한계:** 같은 쿠키 동시 재로그인은 새 세션 둘(개발용 — 한 브라우저 한 계정 완전 보장 아님). **FE 11-10 합의:** refresh 뒤 `/auth/me` 확인 순서, 경합 복구 수치(대기 2초·갱신 2회·원 요청 1회·잠금 10초), `origin_not_allowed` 401, 저장소 장애 503 사유 없음, 로그아웃 결과별 처리. refresh 실패별 처리(503 = 세션 종료로 보지 않음·자동 반복 없음 — 서버 상태는 회전 전/후 실패에 따라 다름, `reused`·`invalid` = G1, `rotated` 한도 초과 = G1이지만 서버 세션 유지). 로그아웃 네 항목(같은 계정 두 세션·만료 access·유효 Bearer + 검증 불가 쿠키·자격 없음 204) 사용자 11-10 확정. 검증 불가 쿠키만 있는 로그아웃 = 401 `refresh_invalid` + 쿠키 삭제, 그 값으로는 세션 폐기 없음, FE는 대기 해제·G1·정상 로그아웃과 구분(FE 11-10 최종 합의 · 구현 완료 · 연동 검증 대기). 함께 온 소셜 결속을 서버에서 확인하면 그 결속의 진행 중 로그인·결속 세션은 정리(11-11 결함 수정 — C, 응답 동일, 결속 처리 중 저장소 장애는 503). 로그아웃 세부 중 PM 확정은 정상·다른 계정 두 세션, 나머지 네 항목은 사용자 11-10 확정, 검증 불가 쿠키만은 FE 11-10 최종 합의 | `feature/auth-session`(A #123 위) |
| 브랜드 생성 시점 스냅샷 | PM 11-04 | jobCreate | 계약 확정·서버 구현 대기 | 미정 |
| `/blocks`의 `overflow`는 유지하되 넘침 판정에 쓰지 않음 | FE | `TextBlock.overflow` | 현행 | — |
| `/preview` 공통 렌더 입력·현재 시도/표시 결과·입력 지문, `/blocks` 섹션 입력 지문·지문 A·B·C 비교 | FE 11-04·11-05 | reviewPreview 「렌더 입력과 시도·결과」·응답 예시 6개·`PreviewRender*`·`TextBlock.sectionInputFingerprint`·blockList | 구현 완료·서버 배포 대기(#108 머지 전제). 서버 미완료 2건: ① 하류 실행 없는 N5 섹션의 미리보기 확보 ② 적용 박스를 읽을 수 없는 섹션의 렌더 실패 기록(블록 저장 500) | #108 |
| `/preview` 키 제공 규칙 — `required` 키 항상, 상태별 null(외곽 8키·섹션 16키) | #100 계약안·FE 11-05 | `ReviewPreview` 「키 제공 규칙」 | 구현 완료·서버 배포 대기 | #108 |
| 배치 요청의 `trans1` 필수(현재 공개안) — 생략 허용은 후속 협의(7.3) | FE 11-03 | `BlockPatch`·blockPatch 「블록 배치」·예시 `missingTrans1` | 현행(현행 서버도 `trans1` 필수) | — |
| 서버 렌더러의 FE 공통 렌더 일치(HTML/Playwright·번들) | 실행계획 BE 8번·R-18 | `PreviewRenderer` — 현행 Pillow·`bundleId = null`·자간 미적용 | 미착수(7.2 ③) | 미정 |
| 소셜 로그인(카카오·Google) — start·완료, 자동 가입, 제공자+회원번호 식별, FE 콜백 `/login/callback/{provider}`, PKCE·state·nonce 서버 관리, 한 브라우저 한 세션 교체, 완료 응답 = `LoginResponse`(신규·기존 같은 모양), 오류 `error.code` + `details.reason` (`UNAUTHORIZED`: `state_invalid`·`provider_auth_failed`·`origin_not_allowed` / `DUPLICATE`: `login_in_progress` / `SERVICE_UNAVAILABLE`: `provider_unavailable` 또는 사유 없음), 완료 요청은 같은 코드로 재전송하지 않음, 의도적 비활성 404 · 켰지만 필수 설정 오류 503(공개 오류에 비밀·설정값 없음). **FE 보장과 서버 내부 값 구분:** 시도 수명(C 초기 기본값 10분)·점유 상한(C 초기 기본값 60초)은 서버 설정이며 공개 계약이 아니다 — 명세에는 「수명이 지나면 `state_invalid`」「409는 끝없이 이어지지 않음」「상한 뒤 늦은 제공자 응답은 세션을 만들지 않음」만 싣는다. FE 대기 종료는 FE 요청 timeout | PRD F-ACC-05 · PM 11-09·11-10 · FE F-A 회신 · FE 기술 확인 회신 11-10(F6·start·F7) · 사용자 11-10(U6, 10분·60초 초기 기본값 채택) | 공통 규칙 「소셜 로그인」·authSocialStart·authSocial·`Error.details`·`SocialStartResponse`·`SocialLoginRequest`·`LoginResponse` | 계약 확정·구현·대역 시험 완료(C `feature/auth-social`, develop 미머지 · 서버 배포 전 — 배포 서버는 확인하지 않음). 공개 계약(요청·응답·오류·FE 보장)과 서버 보장(브라우저 세션 교체·로그아웃 뒤 늦은 완료 차단·점유 만료·늦은 제공자 응답)을 구분 — 서버 보장은 C에서 구현해 제공자 대역·실제 Redis 경합 시험으로 확인, 실제 제공자·브라우저 연동 검증 전. 10분·60초는 실제 제공자 코드 유효 시간·외부 호출 timeout과 대조해 C에서 검증 | `fix/auth-social-contract`(명세) → C 구현 PR 미정 |
| HTTP `error.code` 15종 정리 — 서버 코드 11개를 공통 코드와 `details.reason`·`details.fields`로 변환(7.7) | FE 11-06 | `Error.details` | 구현 완료·서버 배포 대기 | `fix/int2-error-cancel-contract` |
| 작업 취소 200 `{jobId, status}`·반복 취소 200·없는 작업/남의 작업 404 | FE 11-06 | jobCancel | 현행(서버 변경 없음, 명세·시험 보완) | `fix/int2-error-cancel-contract` |
| `isExcluded` true·false·null 구분, null만으로 새 편집 차단 정책을 만들지 않음 | FE 11-06 | `TextBlock.isExcluded` | 현행 | — |

### 7.2 서버 미완료·의존 작업 추적

응답 표현은 확정됐고 서버 처리만 남은 경로다. 공개 명세는 이 상태를 그대로 표시한다(reviewPreview 「구현 상태」·각 오퍼레이션 「2차 상태」).

| # | 항목 | 지금 동작 | 영향 | 명세 위치 | 상태 |
|---|---|---|---|---|---|
| ① | 진행 중인 하류 실행이 없는 N5 섹션의 미리보기 확보 | 시도를 만들지 않아 `render.attempt = null`로 남는다 | 화면은 원본 + 미리보기 없음 | reviewPreview 「구현 상태」·sectionPatch | 서버 미완료(#108 범위) |
| ② | 적용 박스를 읽을 수 없는 섹션의 렌더 실패 기록 | /preview·/blocks는 `inputError` RENDER_INPUT_INVALID로 답하고 N5 복원도 성공하지만, 그 섹션의 블록 저장은 실패 시도를 남기지 못하고 500이며 자동 진행 처리도 그 섹션에서 오류로 멈춘다 | 그런 데이터가 실제로 있는지는 DB를 조회하지 않아 모른다 | reviewPreview 「구현 상태」·blockPatch | 서버 미완료(#108 범위) |
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
- category leaf 검증 실패 응답 형식·적용 시점.
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
- 소셜 로그인의 발급 직전 원자 처리(점유 재확인·결속의 마지막 시도·로그아웃 시각·브라우저 현재 세션 교체) — C(`feature/auth-social`, develop 미머지)에서 구현했고 제공자 대역 + 실제 Redis 시험으로 점유 만료·로그아웃·다른 로그인 시작·늦은 제공자 응답·같은 시도 동시 요청을 확인했다(`tests/test_auth_social.py`). 실제 제공자·브라우저(프록시·SameSite) 연동 검증 전이다. 검증 불가 refresh 쿠키만의 로그아웃(401 `refresh_invalid`)이 결속 처리를 건너뛰어 진행 중 로그인이 뒤늦게 세션을 만들던 결함은 11-11 C에서 고쳤다(서버 기록이 있는 결속만 근거, 완료·로그아웃 순서 양쪽 시험). 간이 로그인(authLogin)의 동시 재로그인 한계(새 세션 둘)는 결속이 없어 C에서도 그대로다(이전 계획의 「C에서 함께 해결」은 정정) — 간이 로그인을 환경별로 끄는 전환 조건과 묶어 해소한다.
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
