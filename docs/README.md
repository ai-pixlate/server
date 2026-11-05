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

## 7. 2차 통합(PRD v3.5.10 · 11/6) 계약 반영 현황

명세 표기 규칙은 `openapi.yaml` 머리말 「2차 통합 반영 표기」를 따른다. 명세에는 2차 확정 계약과 구현 상태만 싣는다. 이 표의 구현 상태는 develop 기준이며 서버 배포 완료를 뜻하지 않는다.
3차(11/27) 예정 기능과 합의 전 후보는 명세에 넣지 않는다.

| 항목 | 계약 | 명세 반영 | 구현 |
|---|---|---|---|
| 번역문 같은 값 저장(no-op) | 확정(FE) | blockPatch — develop 반영(#104) | 구현됨(#104) |
| 저장 공통(처리 순서·멱등 키·422/409/503·재전송 헤더·CORS) | 확정(FE 11-03·11-04) | blockPatch 「저장 공통」 | 구현됨 — 블록 PATCH(#105), 섹션 PATCH(섹션 저장 PR) |
| 409 구분(`error.code` + `details.reason`) | 확정(FE 11-04) | `Error.details` | 각 API 구현과 함께 |
| 작업 생성 시점·책임 동의, `consentId` 미사용 | 확정 | jobCreate · consentCreate | 서버 현행(미저장). 화면 동의 체크는 FE |
| 브랜드 생성 시점 스냅샷 | 확정(PM 11-04) | jobCreate | 구현 대기(담당 미확인) |
| OTC 종류 규칙·임시 M020 | 정책 확정(PM 11-04) | `JobPatch.regulatoryClass` | 필드는 새 규제사전 작업(#103 계열) |
| 재렌더 중 확정 허용·검사 중 확정 거절·검사 최종 실패 통과·미저장 번역문 확인창·넘침 비차단 | 확정(PM) | confirmReview | 재렌더 중 허용·넘침 비차단 = 현행, 검사 중 거절 = 규칙 검사 연결과 함께, 확인창 = FE |
| 섹션 제외·복원(revision·같은 상태 변경 없음·복원 의미·전체 Section 응답) | 확정(FE 11-03·PM) — 최초 판정 없는 섹션 복원은 PM-R8 결정 전까지 409 거절 | sectionPatch · `Section.revision` · `SectionPatch` | 구현됨(#107). N5 복원 때 미리보기 시도 확보 = 구현됨(/preview 렌더 입력 PR), 검사 시도 확보 = 규칙 검사 연결과 함께(구현 대기) |
| 블록 배치 저장(정규화·CSV 5열) · 규칙 검사 상태 · `translation_failed` `taskId` · category leaf · N3 포함 섹션 판정 | 확정 | 각 구현 PR에서 그 오퍼레이션에 함께 반영 | 구현 예정 |
| `/preview` 공통 렌더 입력·현재 시도/표시 결과·입력 지문, `/blocks` 섹션 입력 지문 | BE 계약안(FE 확인 대기) — 입력 준비 중(N4) 응답 표현·`rendererId`·`fontColor` 표기 확인 중 | reviewPreview 「렌더 입력과 시도·결과」 · `PreviewRender*` · `TextBlock.sectionInputFingerprint` | 구현됨(/preview 렌더 입력 PR). 입력 준비 중 응답 분기는 FE 합의 뒤 |
| 서버 렌더러의 FE 공통 렌더 일치(HTML/Playwright·번들) | 2차 범위(실행계획 BE 8번 · R-18) | `PreviewRenderer` — 현행 Pillow·`bundleId = null`·자간 미적용 명시 | 미착수(2차 의존 작업) |
| `regulationVerdict` | 필드 안 FE 확인 중 | 미반영 — 확정되면 그 구현 PR에서 반영 | 구현 예정 |
