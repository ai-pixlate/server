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

기준: PRD v3.5.10 · 실행계획 v3.5.10(2차 개발 10/28~11/5, 2차 통합 11/6) · PM·FE·AI 회신(2026-10-29~11-03). 서버 비교 기준은 `develop` f85b23e.

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
| 이미지 경계 이탈(Q-07) — 현재 입력의 이탈 | 블록 넘침은 경고만. 현재 입력에서 글자가 이미지 경계 밖으로 잘리면 N5 확정 차단(경고 확인으로 못 풂, 번역문·박스·섹션 제외로 해결, 저장·재렌더 허용) | 정책 기록(PM 11-02 결정 — AI 원문에 기록, PM 원문 미수령) | 대기(판정 없음) | confirmReview 「이미지 경계 이탈」 ① · `PreviewRenderResult.boundaryExceeded`(계약안) |
| 이미지 경계 이탈 — N6 이탈 시 [중단]→N5 복귀 | 렌더 실패와 같은 오류 → [중단] → 수정 보존 N5 복귀 | 정책 기록 있음 · **2차 적용 여부 확인 필요**(실행계획상 N6 [중단]→N5는 3차, 현행 중단 API는 N2·N4만) | 2차 범위에 넣지 않음(확인 전) | confirmReview 「이미지 경계 이탈」 |
| 작업 생성 시점·동의 | 책임 동의 체크 뒤 N1 [다음]에서 `POST /jobs` → 업로드 → PATCH → analyze. `consentId`·`POST /consents` 미사용. `createdAt`은 지금은 작업 생성 시각이며, 동의 체크를 [다음]의 필수 조건으로 적용한 뒤부터 동의 시각으로도 쓴다(PM 정책) | 확정 | 서버 현행(미저장). 화면: client 8319dd6에는 동의 체크가 없고 고지 문구만 있어 위 전제가 아직 충족되지 않음 — 동의 화면은 3차(E 묶음) | jobCreate · consentCreate |

### 7.2 렌더러 전환 정책 (PM 확정 — 명세 반영은 세부 합의 뒤)

- Pillow로 만든 **전환 이전 작업**은 이미 만든 파일의 **다운로드만** 허용한다. 수정·재렌더·재시도·새 출력 생성은 막는다.
- HTML 렌더 안에서 공통 렌더 번들이 갱신될 때는 위 제한과 별개로 **새 미리보기를 등록**한다.
- 협의 중: "전환 이전 작업"의 범위(전환 시점의 모든 작업 vs Pillow 렌더 시도가 있는 작업), 차단 응답의 오류 코드, 작업 응답의 표시 필드, [저장]·취소 허용 여부.

### 7.3 협의 중 — 명세에 넣지 않은 것

| 항목 | 상태 | 대상 |
|---|---|---|
| 저장·섹션·배치·검사·확인 계약의 남은 세부(멱등 서버 세부·409 구분·섹션 입력 지문·확인 키·실행 실패 섹션 reason, 배치 정규화와 3차 시스템 조정, AI 원문 대조) | 11-03 회신으로 일부 확정, 나머지는 7.5 표 | FE·AI·PM |
| PRD F-CFM-09 ③ 실행 실패 섹션 확인 뒤 N5 확정·N6 출력 허용 범위 | **2차 확정 흐름의 미결** — 요청 구조는 FE 수용, 출력 범위는 PM(7.5 P7) | PM |
| `/preview` style 최소 필드·렌더 식별자 | BE 계약안(7.4·7.5) — 확인 대기 | AI·FE |
| 공통 렌더 번들 제공 방식·FE·BE 버전 고정 절차·불일치 때 응답과 갱신 | 협의 중(후보: GitHub Release + SHA-256 잠금 — FE가 후보로 검토 가능) | FE·BE·AI |
| 시스템 자동 조정 구조·사용자 조정과의 적용 우선순위 | 3차 | AI |
| OTC 종류 필드·국가별 선택지 정본·필수 검증·초기화, 열쇠 없는 OTC 작업의 임시 M020 적용 대상·종료 조건 | 정책 결정 대기 | PM(정책) · 데이터 · AI(반영 확인) |
| 브랜드 정보의 작업 생성 시 스냅샷(현재는 `brand_id`만 저장) | PM 정책 먼저(FE 회신) | PM → FE·BE |
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
- AI: 행간 1.2·절대 배율·원본 정렬 우선·소수 좌표 원칙은 회신으로 정해졌다. 남은 것은 style 최소 키 확인, 정렬 근거의 저장 위치와 지문 반영 형태(지문에 넣는 것 자체는 원칙), 렌더러 식별자 구성 — 7.5 A1~A3·A6.
- FE: 박스·배율 저장 구조는 FE 회신으로 확정(정규화만 조건부). 번들 반입 방식(R-FE-6)은 협의 중. `/blocks.overflow` 유지·판정 미사용과 g 자동 재조회 5초·12회는 FE 회신으로 확정. `sectionInputFingerprint`는 FE가 필요하다고 회신 — 7.5 계약안(F15).
- PM: Pillow 전환 이전 작업의 범위(PM-R0)와 그것을 알리는 작업 단위 표시 필드.
- BE: 렌더러 전환 뒤 새 시도 등록 방식과 완료 시간 보장(BE 배포 설계 — 대상 작업량·배포 절차 포함). 렌더 대상 0개 섹션의 결과는 현행대로 `result`로 내기로 정리했다(배경만 그린 이미지, `overflow={}`).

### 7.5 저장·섹션·배치·검사 계약 (FE·AI가 구체안 요청 → 11-03 회신 반영)

규칙·요청/응답 예시의 정본은 OpenAPI다 — blockPatch 설명 「저장 공통」(오류 분류)·「블록 배치」, sectionPatch 설명, blockList 설명(섹션 입력 지문 관계), confirmReview 설명 「검사 연결 경계」, ConfirmRequest 설명, `Error.details`(409 구분), 스키마 `EditIdempotencyKey`·`BlockPatch`·`LayoutAdjustPatch`·`LayoutBox`·`LayoutAdjust`·`BlockCheck`·`Section.revision`·`PreviewInputError`·`TextBlock.sectionInputFingerprint`. 이 절은 상태·확인 질문·호환·선행 구현 범위만 둔다.

**회신 근거(11-03)**
- FE: "계산·UI 제어·상태 동기화·Undo/Redo는 FE, 저장 정본·revision·멱등 검증·검사 상태·입력 지문·확인 요청 검증은 BE가 보장"을 전제로 ①~⑧ 회신.
- AI: A1~A4·A6~A9의 AI 담당 범위 기술 방향·권장안을 승인하고 Notion(BE–AI 기술 입출력 계약 v0.1, BE–AI 합의 항목 관리표)에 반영했다고 회신. **BE·FE 공동 인터페이스 최종 확정은 아니다.** 원문 두 건은 2026-11-03에 읽고 OpenAPI와 대조했다(아래 「AI 원문 대조」). A9는 AI 검출 의미·근거 보존만 승인했고 API 신호·확정 확인 정책은 BE·FE·PM 미결이다.

| 항목 | 상태 | 근거·내용 | 남은 것 |
|---|---|---|---|
| 처리 순서·오류 분류 | **확정(FE)** | 처리 순서 1~9, 요청 형식 422 / 저장 상태 비교 400 / 상태 409 | — |
| 멱등 키 생성·재사용·결과 불명 보류 | **확정(FE)** | 조작마다 UUID v4, 통신 재시도는 같은 키·같은 본문, 결과 불명 동안 후속 조작 보류, 오류별 안내·재조회는 FE | — |
| 멱등 서버 세부 | BE 계약안 | 키 범위 (판매자·작업·오퍼레이션·키), 같은 요청 판별, 2xx만 기록, 24시간, 취소 때 삭제, 만료 뒤 처리, 잠금 대기 503, 재전송 헤더 — FE가 포괄 동의했지만 항목별로 답하지 않음 | FE F13 |
| 409 구분 | BE 계약안(FE 요청에 대한 답) | `error.code` + `details.reason`(OpenAPI `Error.details` 목록). 검사 등록으로 생긴 409 = `check_not_settled` + `details.registeredBlockIds` | FE F14 |
| 섹션 저장 | **확정(FE)** | `{action, revision}` + 멱등 키, 같은 상태 200 no-op, 제외·복원 각각 Undo/Redo 한 단계, 저장 뒤 `/blocks`·`/preview` 재조회, 충돌 때 동기화·이력 정리는 FE. 원상 복원은 PM 확정 | — |
| 섹션 저장 서버 세부 | BE 계약안 | revision을 올리는 범위, 키가 있으면 revision 필수, N3·N5 복원 때 시도 확보(같은 입력의 최종 실패·재시도 횟수 보존) | — (FE가 BE 몫으로 둠) |
| 복원 근거 없는 기존 데이터 | 미결(PM) | 결정 전까지 409 `restore_basis_missing` | 행 수 확인 → PM P3 |
| 블록 배치 구조·계산 | **확정(FE)** | `layoutAdjust {box, fontScale}`, 생략 유지·null 해제, 소수 유지(고정 자릿수 없음), [+][−] s′ = s ± 0.05·박스 s′/s배·중심 기준·경계 보정·버튼 비활성·절대값 전송은 FE, 섹션 밖 400은 BE, 저장 뒤 서버 반환값으로 동기화 | — |
| [−] 비활성 조건 | BE 제안(FE가 계산식 회신에서 버튼 비활성 처리 동의) | s′ ≤ 0이면 [−] 비활성. PM 규칙에는 [+] 비활성(다음 확대가 섹션보다 큼)만 있다 | — |
| 배치 정규화(1.0·원본 bbox → null) | **조건부(FE 수용)** | FE는 null을 배율 1.0·원본 bbox로 해석하고 Undo/Redo도 정규화된 서버 상태를 씀. 3차 시스템 조정과의 관계는 BE·AI 공동 확인 전 | AI A10 |
| 검사 상태 5값·버튼 처리 | **확정(FE)** | in_progress 비활성, failed 확인 흐름, missing·outdated는 확정 요청 → 409 뒤 재조회·안내 | — |
| 검사 판단 기준(두 지문) | AI 승인(A4·A7, 원문 대조 완료) · 외부 필드는 BE 계약안 | 요청 검증 지문(`input_manifest_sha256` 유지, 현재 revision과 비교하지 않음)과 검사별 유효성 지문(실제 소비 입력만 — revision·배치·활성 검사 구성 제외)을 나눔. 규칙 검사 요청에는 언어 필드가 없다. `check.inputKey` = 유효성 지문의 블록 단위 투영. 영향받는 검사만 버전 변경 재평가 | 필드·호환 구조(BE·AI) |
| 섹션 입력 지문 | BE 계약안(FE 요청에 대한 답) | `TextBlock.sectionInputFingerprint`(A — /blocks 시점의 현재 섹션 입력, /preview `inputFingerprint`(B)와 같은 계산). 미리보기 최신성 = B·C(/preview 안), 표·뷰어 입력 일치 = A·B. A·C는 직접 비교하지 않는다. null은 비교하지 않는다. 불일치는 선후를 알려 주지 않으므로 응답 교체는 요청을 보낸 순서로, A ≠ B면 두 조회를 함께 다시 받고 그래도 다르면 섞어 쓰지 않는다 | FE F15 |
| 확인 요청 `{blockId, code, inputKey}` | 구조 **확정(FE)** · 키 기준 BE 계약안 | FE는 세 코드 구분·`acknowledgement_outdated` 때 재조회·확인창 갱신 수용. 키 구성·유효성 기준은 FE 수용 범위 밖. `translation_failed`는 키 없음(BE 권장 — 확인 내용이 입력 판본에 달려 있지 않음) | FE F16 |
| 실행 실패 섹션 확인 | 구조·UI **확정(FE)** · reason·최신성 BE 계약안 · **출력 범위 미결(PM)** | `acknowledgedSections: [{sectionId, reason, taskId}]`, reason = `inpaint_failed`·`render_failed`, 화면이 본 실패 task와 현재 실패 시도가 다르면 409 `acknowledgement_outdated`. 확인 뒤 N5 확정·N6 출력 허용 범위는 PM — 정해지기 전까지 2차 확정 흐름은 미완성 | FE F17 · PM P7 |
| `/preview` style·계산 책임·식별자·입력 구성 실패 | AI 승인(A1~A3·A6·A8, 원문 대조 완료) · 외부 필드는 BE 계약안 | 최소 8키(정렬 left·center·right, 색 대문자 #RRGGBB), 서버 = 입력 구성 / 공통 렌더 코드 = 최종 크기 계산 / FE = 미저장 값만 바꿔 로컬 미리보기. `bundleId` = 번들·폰트 자산, `rendererId` = 서버 실행 환경 + 측정 규칙 버전. `inputError`: AI 두 코드 + BE 구성 오류 `RENDER_INPUT_INVALID`(제안) | fontFace·색 정규화 책임(BE·FE), A8 코드 매핑 |
| 2차 활성 검사 | **확정(AI 회신)** | 규칙 검사만, LLM 판정 3차 | — |
| 이미지 경계 이탈(Q-07) | ① 현재 입력의 이탈 = 정책 기록대로 차단 · ②③ 미결(PM) · 응답은 BE 계약안 | ① 현재 입력에서 이탈 확인 → 409 `image_boundary_exceeded`(확인으로 못 풂). ② 현재 입력의 판정 없음·진행 중·실패 → 이탈이 아니므로 이 사유를 쓰지 않고 기존 "렌더 진행 중 확정 허용"을 유지(바꾸려면 PM). ③ 이전 입력의 이탈을 수정해 재판정 중 → BE 권장은 차단 유지(`image_boundary_unresolved` 후보), PM 확인 전 구현 안 함. N6 [중단]→N5 복귀는 2차 적용 여부 확인 필요 | PM P9 · FE·AI F19 |
| 출력 위치와 렌더 지문 | **미결** | 판정 좌표계가 섹션별 렌더 이미지면 출력 위치는 영향 없음(지문에 넣지 않음), 최종 합성 이미지 단계면 출력 위치가 입력 — 그때 앞 섹션 제외·복원 뒤 재판정만인지 재렌더까지인지 정한다. 정하기 전에는 뒤 섹션 전체 재렌더를 확정하지 않는다. 현행 `displayTop`은 N5 제외 섹션 높이까지 더한다 | FE·AI F19 |
| 미적용 강제 용어 확인 제외 | **PRD 기준 유지**(F-CFM-09 I열 "세지 않는다") | 확인 대상에서 빼는 것과 검사·근거·flag·신호 보존은 함께 한다(신호는 화면에 보인다). AI 원문(A9)의 정합성 확인 요청만으로 PRD 규칙을 바꾸지 않는다 — 상충하는 정책 결정 근거가 있을 때만 PM에게 변경 여부 확인 | (상충 근거가 나오면) PM |
| 조건부 규제 신호 | 미결(FE·PM) | `prohibited_expression` 하나로 금지·조건부를 함께 내려면 신호에 판정 상태를 실어 화면이 구별해야 한다(A9) | FE F18 · PM P8 |

**AI 원문 대조(2026-11-03, Notion 두 건)** — 대조해 문서에 반영한 차이만 적는다.
- 반영: 검사 유효성 지문에서 언어(현행 요청에 없음)·활성 검사 구성(검사별 지문에 넣지 않음)을 뺐다 / AI 내부 검사 상태 → 공개 5상태 대응을 `BlockCheck`에 적었다 / 조건부 매칭 신호 구별 조건 / style 값 형식(정렬·색) / `rendererId` 정의를 서버 실행 환경 + 측정 규칙 버전으로 고쳤다 / BE 입력 구성 오류 코드 제안. 미적용 강제 용어 제외는 PRD 기준을 유지한다(AI의 정합성 확인 요청만으로 바꾸지 않음).
- **새로 드러난 것 — Q-07 이미지 경계 이탈**: PM의 11-02 결정이 AI 원문에 기록돼 있는데 지금까지 계약안에 없었다. 현재 입력에서 이탈이 확인된 경우의 차단(①)과 `boundaryExceeded`·`image_boundary_exceeded` 계약안을 넣었고, 판정 없음·진행 중(②)·이전 입력의 이탈(③)·N6 복귀의 적용 차수·출력 위치의 좌표계는 미결로 나눴다.
- **현행 서버와의 차이**: `/preview`의 `displayTop`은 N5 제외 섹션 높이까지 더한다 — AI 원문이 요구하는 "제외를 반영한 최종 출력 위치"와 다르다. 판정·지문에는 쓰지 않도록 적었다.
- **원문이 이후 합의보다 뒤처진 곳**(AI에 갱신 요청): 0.05 증감량·Undo 50단계는 "미확정"으로 남아 있다(PM 확정 — 검토 채팅 전달) / 배치 쓰기를 "PATCH 확장인지 별도 endpoint인지 미결"로 둔다(FE가 PATCH 확장 동의) / 부분 수정·null 의미를 "BE·FE 승인 전"으로 둔다(FE 동의, 정규화만 조건부) / `rerenderTaskId`를 "예시명"으로 둔다(FE 합의로 필수·nullable 확정).
- 원문과 같아 그대로 둔 것: 요청 검증 지문 유지·flag revision은 감사 메타, 채택 조건(lease·현재 시도·취소/확정 가드 유지), 배치만 변경 → 검사 유지·렌더만 재등록, `checks_settled`는 처리 종료일 뿐 확정 허용이 아님, 늦은 결과의 재등록 금지, 기준 글자 크기 출처와 1회 계산, 박스 절대값 재확대 금지, M020 자동 대입 금지(PM 결정 전).

FE 회신이 기존 PM 원칙을 **재확인**한 부분: 박스·배율 변경만으로 규제 재검사를 하지 않는다(FE ⑦) — 다만 PM은 이 항목을 "가능성 검토"로 요청했으므로 정책 확정은 P1로 남는다. 새 정책 승인이 필요한 부분: 실행 실패 섹션 확인 뒤 출력 범위(P7).

**기존 계약과의 호환·적용 순서**

| 변경 | 기존 FE 영향 |
|---|---|
| `trans1` 생략 허용, `layoutAdjust` 추가 | 완화·추가 — 영향 없음 |
| `Idempotency-Key`, 섹션 `revision` | 처음엔 선택 — 영향 없음(키를 보낼 때만 섹션 revision 필수). 필수화 때 FE 적용 필요 |
| 섹션 PATCH 응답 = 전체 Section | 현행 6필드의 상위 집합 |
| 같은 값 저장·섹션 같은 동작 = no-op | 동작 변경 — "항상 revision +1" 가정을 빼야 한다 |
| TextBlock `layoutAdjust`·`systemAdjust`·`check`·`sectionInputFingerprint`, `/preview` 섹션 `revision`·`inputError`·`renderer.bundleId`·style 최소 키, SignalCode `check_failed` | 추가 |
| 오류 `details.reason` | 추가(HTTP 오류 코드 15종은 그대로). reason이 없는 현행 응답은 일반 상태 오류로 처리 |
| 확정 거절 `check_not_settled`, 확인 대상 `check_failed`·`prohibited_expression`, `inputKey`, `acknowledgedSections` | 검사 연결 뒤 새로 생기는 거절·확인 — FE 처리 필요 |

순서(제안): ① 남은 확인(아래 질문) → ② OpenAPI 표시를 확정으로 바꿈 → ③ 서버: 추가 필드·선택 헤더·no-op을 먼저 배포 → ④ FE 적용 → ⑤ 서버: 멱등 키·섹션 revision 필수화, 검사 연결 켜기(#83 일괄 등록과 같은 배포), 확인 대상 확장.

**PM 답 없이 가능한 선행 구현(제안 — 구현 브랜치는 승인 뒤 만든다, 이 문서 브랜치에서는 서버 코드를 고치지 않는다)**

| 묶음 | 범위 | PM 의존 | 제안 브랜치 |
|---|---|---|---|
| ① 저장 공통·섹션 저장 | 블록·섹션 PATCH 처리 순서, 멱등 기록 테이블(S11)·`section.revision`(S12) 마이그레이션, 같은 값·같은 상태 no-op, `trans1` 생략, `details.reason`, 전체 Section 응답, 복원 규칙(최초 판정 버킷 기준), N5 복원 시도 확보 | 없음 — 복원 근거 없음(PM-R8)은 409로 두고, 전환 이전 작업 차단(PM-R0)은 넣지 않는다 | `feature/int2-save-idempotency` |
| ② 블록 배치 저장 | `layout_adjust` v2(user·system) 마이그레이션, `layoutAdjust` 검증(섹션 밖 400·그리지 않는 블록 409), 정규화(2차 범위), 배치만 수정 시 상태 유지, content.csv 배치 5열 | 없음(정규화의 3차 관계는 3차 때) | `feature/int2-layout-adjust` |
| ③ 렌더 입력·조회 | 박스 정수 절삭 제거, 셀러 박스·배율 반영, `renderInput`·지문·`inputError`·`render.attempt/result` 추적, `/blocks.sectionInputFingerprint`, style 최소 키 | 없음 — 단 style 키·식별자 구성은 AI 원문 대조 뒤 고정 | `feature/int2-preview-render-input` |
| ④ 검사 연결 | 검사 stage 마이그레이션, 등록·채택·유효성 지문·`check.state`·확정 거절·확인 대상 확장 | P1(배치만 변경 재검사 생략)·P7(실패 섹션 출력 범위)은 PM — 검사 연결 자체는 AI 원문 대조 뒤 착수 | `feature/int2-check-connection` |

브랜치 이름은 제안이며, 만들기 전에 승인을 받는다(AGENTS.md).

**확인 질문(이번 회신 뒤 남은 것만)** — 이미 받은 답은 다시 묻지 않는다. 각 질문은 필요한 작업, 현재 제안, 답할 부분만 적는다.

FE
- F13. (묶음 ① 구현) 멱등 서버 세부 — 키 보관 24시간, 기록 만료 뒤 같은 키는 새 요청으로 처리(revision이 중복을 막음), 잠금 대기 초과 503(같은 키로 재시도 안전), 재전송 표시 헤더 `Idempotent-Replayed`. 회신의 포괄 동의가 이 네 가지에도 해당하는지만 답해 주면 된다. 권장: 이대로.
- F14. (묶음 ①·④) 409 구분 — `error.code` + `details.reason` 목록(OpenAPI `Error.details`)으로 원인을 나눈다. 검사 등록으로 생긴 409는 `check_not_settled` + `details.registeredBlockIds`. 새 최상위 코드 없이 이 조합으로 화면 분기가 되는지만 답해 주면 된다. 권장: 이대로.
- F15. (묶음 ③) `sectionInputFingerprint`(A) — 블록마다(같은 섹션은 같은 값) 넣고, N5 제외·입력 구성 실패면 null(null은 비교하지 않음). 미리보기 최신성은 /preview 안의 B·C로, 표와 뷰어의 입력 일치는 A·B로 본다. 지문은 선후를 알려 주지 않으므로 응답 교체는 요청을 보낸 순서로 하고, A ≠ B면 두 조회를 함께 다시 받아 그래도 다르면 섞어 쓰지 않는다. 위치(TextBlock 각 행)·null 조건·재조회 규칙만 답해 주면 된다. 권장: 이대로.
- F16. (묶음 ④) `translation_failed` 확인에는 `inputKey`를 보내지 않는다(검사 대상이 아니라 `check`가 null, 확인 내용이 입력 판본에 달려 있지 않음) — 괜찮은지만. 권장: 이대로.
- F17. (묶음 ④) 실행 실패 섹션 확인 `reason` = `inpaint_failed`·`render_failed`, 최신성 = 화면이 본 실패 `taskId`(인페인팅은 /tasks, 렌더는 /preview `render.attempt.taskId`). 이 두 값을 화면에서 얻을 수 있는지만 답해 주면 된다. 권장: 이대로.
- F18. (묶음 ④) 조건부 규제 신호 — 금지·조건부를 `prohibited_expression` 하나로 내되 신호에 판정 상태(금지/조건부)를 싣는 안과 조건부 코드를 따로 두는 안 중 화면에 맞는 쪽. 권장: 같은 코드 + 판정 상태 필드(확인 목록 형식이 그대로다).
- F19. (묶음 ③·④, BE·FE·AI) 이미지 경계 이탈(Q-07) — 현재 입력의 이탈은 `render.result.boundaryExceeded`(블록 → 이탈 여부)로 내고 확정은 409 `image_boundary_exceeded`로 막는다(판정이 없거나 진행 중이면 이 사유를 쓰지 않는다). 먼저 정할 것: 이탈 판정이 **섹션별 렌더 이미지**의 좌표계인지(섹션 경계에서 잘림 — 출력 위치 무관), **이어 붙인 최종 이미지**의 좌표계인지(출력 위치가 입력). 후자라면 앞 섹션 제외·복원 뒤 뒤 섹션을 재판정만 하면 되는지, 섹션 이미지까지 다시 그려야 하는지. 이 정의와 응답 이름만 답해 주면 된다. BE 권장: 섹션별 렌더 이미지 기준(현행 렌더 단위와 같음).

AI (원문은 11-03에 읽어 대조했다 — 남은 것만)
- 원문 갱신 요청: 0.05 증감량·Undo 50단계(PM 확정), 배치 쓰기 = 블록 PATCH 확장, 부분 수정·null 의미(FE 동의 — 정규화만 조건부), `rerenderTaskId` 필수·nullable(FE 합의)이 원문에는 아직 미확정으로 남아 있다.
- A11. (묶음 ④) `check.inputKey`를 유효성 지문의 블록 단위 투영(그 블록의 원문·번역문 해시 + 그 검사가 소비하는 섹션 공통 문맥)으로 만드는 안이 A4의 "검사별 실제 소비 입력" 원칙과 맞는지 — 특히 LLM(3차)은 섹션 전체 문맥을 소비하므로 블록 키에 문맥을 어디까지 넣을지만 답해 주면 된다. 권장: 규칙 검사는 블록 단위, LLM은 섹션 문맥 지문을 함께 넣는다.
- A12. (묶음 ③) 입력 구성 오류 — AI 두 코드(`STYLE_RESULT_INVALID`·`STYLE_DEFAULTS_UNAPPROVED`)는 그대로 두고, BE가 입력을 구성하며 찾는 오류(박스·배율·조정 조합·폰트·번들)는 `RENDER_INPUT_INVALID`로 나누는 안. 괜찮은지만.
- A10. (묶음 ② 3차 대비) 정규화(1.0·원본 bbox → null)와 3차 시스템 조정 — BE 권장: 2차는 정규화, 3차에는 시스템 조정이 있는 블록에서 셀러가 보낸 1.0·원본 박스를 정규화하지 않고 "셀러 지정"으로 저장. 이 방향이 AI의 시스템 조정 설계와 맞는지만 답해 주면 된다.

PM(정책만)
- P1. 배치만 바꾼 저장은 재검사하지 않는다 — AI 기술 합의, FE도 같은 원칙을 재확인했다. 정책으로 확정하는가?
- P2. PM-R0: 렌더러 전환 이전 작업의 범위 — 전환 시점의 모든 작업 vs Pillow 렌더 시도가 있는 작업. 저장·취소 예외(R1-a·R2)도 함께.
- P3. PM-R8: 최초 판정 기록이 없는 직접 제외 섹션을 복원할 때의 처리(대상 행이 있을 때만).
- P5. OTC 종류·국가별 선택지·필수 검증·초기화, 열쇠 없는 OTC 작업의 임시 M020 적용 대상·종료 조건.
- P6. 작업 생성 뒤 브랜드 정보가 바뀌면 기존 작업에 반영할지(브랜드 스냅샷)와 그 예외.
- P7. 실행 실패 섹션(인페인팅 실패·렌더 실패)을 확인한 뒤 N5 확정과 N6 출력을 어디까지 허용하는가(실패 섹션을 원본 그대로 출력하는지 등). PRD F-CFM-09는 "확인 후 확정"까지만 정한다.
- P8. 조건부 규제 경고의 화면 표시 — 확인 대상인 것은 PRD F-CFM-09 I열대로다(금지·조건부 매칭 모두 미해결 규제 경고, 미적용 강제 용어는 세지 않음 — 재질문 아님). 정할 것은 조건부를 금지와 구별해 보이는 방식(제목·문구, 별도 신호 코드 여부)뿐이다(FE F18과 함께).
- P9. 이미지 경계 이탈(Q-07)의 남은 범위 — ② 현재 입력의 판정이 아직 없거나 진행 중·실패일 때 확정을 막는가(지금 규칙은 "미리보기 재렌더 진행 중 확정 허용"), ③ 이전 입력에서 이탈했고 수정 뒤 재판정 중일 때 차단을 유지하는가(BE 권장: 유지), ④ 11-02 결정의 N6 이탈 → [중단] → N5 복귀가 2차(11/6) 적용인가(실행계획상 3차, 현행 중단은 N2·N4만).

**확인하지 못한 근거**
- Notion 2건은 2026-11-03에 읽었다(로그인된 세션으로 페이지 데이터를 받아 대조). 원문이 참조하는 하위 문서(HTML/CSS 조판 규칙 협의, develop BE 인계 §8, PM Q-07 결정 원문)는 따로 열지 않았다.
- PM 회신 원문은 이 작업 채팅이 받지 않았다([+][−] 화면 규칙·Undo 50단계는 검토 채팅이 원문으로 확인해 전달).
- 최초 판정 기록이 없는 직접 제외 섹션의 실제 행 수(사용자 읽기 필요).

### 7.6 구현 대기

서버(BE)
- 블록 PATCH: 같은 값 저장 = revision 유지·새 task 없음(`rerenderTaskId: null`). 현재는 같은 값도 revision+1·`edited`·재렌더.
- 블록 PATCH: **실제 값이 바뀐 저장인데 진행 중인 후속 처리 실행이나 현재 미리보기 시도가 없어 `rerenderTaskId=null`이 나오는 현행 경로**(`rerender_section`이 None을 돌려줌)는 정상 no-op이 아니다. 편집 뒤 필요한 재렌더를 보장하도록 보완한다.
- 블록·섹션 PATCH 공통(7.5): 처리 순서, 멱등 기록(S11, 같은 트랜잭션)·24시간 정리·취소 때 삭제, `details.reason`, 재전송 표시 헤더.
- 블록 배치(7.5): `trans1` 생략 허용, `layoutAdjust` 저장(`layout_adjust` v2 — `user`·`system`), 정규화, 범위 검증(십진수, 보정 없음), 그리지 않는 블록 거절, 배치만 수정 시 `blockStatus` 유지·검사 등록 없음.
- 섹션 PATCH(7.5): `section.revision`(S12)·충돌 검사(키가 있으면 revision 필수)·같은 동작 no-op·전체 Section 응답·복원 규칙·근거 없는 복원 거절·N5 복원 때 현재 지문의 시도가 없을 때만 렌더·검사 등록(같은 입력의 최종 실패·재시도 횟수 보존).
- 렌더 입력: 박스 정수 절삭 제거(`_render_inputs`), 셀러 박스·배율 반영, style 최소 8키·`fontScale` 적용값 1.0, `renderer.bundleId`, 입력 구성 실패 때 `inputError`(값을 지어내지 않음), `/blocks`의 `sectionInputFingerprint`.
- 오류: `details.reason` 목록(409 구분), `check_not_settled`의 `details.registeredBlockIds`.
- 이미지 경계 이탈(Q-07): 렌더 결과의 경계 이탈 판정과 저장, 현재 입력의 이탈 확정 차단 `image_boundary_exceeded`. 판정 없음·이전 이탈의 처리(P9)와 출력 위치·좌표계(F19)는 정해진 뒤. N6 이탈 오류와 [중단]→N5 복귀는 2차 적용 확인(P9) 뒤.
- 검사 연결(7.5): 검사 stage 마이그레이션, 원래 흐름의 등록(번역 채택·재시도·번역문 변경·복원·배포 일괄 #83)과 확정 요청의 보조 복구, 검사 입력 지문, 채택 조건, `check.state`, 확정 거절, 확인 대상 확장(`check_failed` 신호·`prohibited_expression`)·확인 항목 `inputKey` 대조와 감사 로그. 실행 실패 섹션 확인(`acknowledgedSections` — reason·taskId 대조)은 F17 회신 뒤, 확인 뒤 출력 범위는 P7 뒤.
- `/preview`: 7.4 필드 전부(항상 제공·제외 섹션 null 규칙 포함)와 섹션 `revision`. 표시 결과를 만든 시도·지문·렌더러를 섹션별로 알 수 있어야 한다(지금은 `section.render_image_key`와 블록 `overflow`만 남는다). 한 응답을 한 스냅샷으로 만들고, `isCurrent=false`면 `overflow`를 null로 낸다.
- 지문: 배율·정렬 근거 등 렌더 결과를 바꾸는 입력을 도입하는 변경에서 같은 변경으로 지문에 넣는다(현행 `_render_inputs`에는 아직 해당 입력이 없다).
- 렌더러 전환: 번들 갱신 대상에 새 미리보기 시도 등록(방식·완료 시간 보장은 배포 설계에서), Pillow 전환 이전 작업 차단.
- 7.1의 다른 `대기` 항목(포함 섹션 판정, 복원 의미, leaf 검증, CSV 배치 열, 검사 중 확정 거절).

화면(FE)
- `rerenderTaskId` null 처리(새 task 없음, 기존 추적 유지, 판정 근거로 쓰지 않음)와 Undo/Redo 공통 적용.
- 멱등 키 발급(조작마다 새 키)·같은 키 재전송·결과 불명 동안 후속 저장·Undo/Redo 보류, 재전송 응답은 성공 확인으로만 쓰고 재조회로 동기화.
- 배치 조작 → 절대값 요청([+][−]는 박스·배율 둘 다), 섹션 안 제한, 같은 공통 코드로 로컬 미리보기.
- 섹션 revision 전송·409 처리, 검사 상태별 버튼 처리(in_progress만 비활성, missing·outdated는 확정 요청으로 복구), 확인 목록에 `check_failed`·`prohibited_expression`(검사 코드는 `inputKey` 함께), 실행 실패 섹션 확인(`acknowledgedSections`, 블록 경고와 한 확인창 — F17·P7 뒤).
- `/preview` 판정 규칙(x→i→a→b→c→d→g→e) 적용, 넘침은 `render.result.overflow`로만, g의 재조회(5초 간격 최대 12회)와 종료 처리.
- 동의 체크를 [다음]의 필수 조건으로 적용(createdAt을 동의 시각으로 쓰는 전제).

### 7.7 문서 사이트

`deploy-api-docs.yml`은 `feature/api-tracking`에 push될 때만 사이트를 갱신한다. 그 브랜치는 원격에 없으므로, 이 명세가 `develop`에 머지돼도 사이트(https://ai-pixlate.github.io/server/)는 바뀌지 않는다. 배포 방식은 별도 결정.
