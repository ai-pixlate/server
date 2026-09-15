# Pixate Server

AI 상세페이지 로컬라이제이션 서비스 Pixate의 백엔드 인프라 서버 저장소.

API 트래킹 문서(OpenAPI 명세·Swagger 사이트)는 `feature/api-tracking` 브랜치에서 관리한다.
문서 사이트: https://ai-pixlate.github.io/server/

## Communication

- 모든 응답과 설명은 한국어로 작성한다.

## Git Remote

- `origin`: https://github.com/ai-pixlate/server.git — 팀 공식 repository. pull/push/PR 기준.

## Branch

| 브랜치 유형 | 의미 | 비고 |
| --- | --- | --- |
| `main` | 운영 배포 | 커밋 금지 |
| `develop` | 통합 개발 | PR로만 반영 |
| `release/*` | 배포 직전 안정화 | 버그 수정만, 기능 추가 금지 |
| `feature/*` | 기능 | 개인 작업 (자유롭게) |
| `fix/*` | 버그 수정 | `develop` 또는 `release/*`로 PR |

## Commit

형식:

```
<type>: <subject>

<body>          (선택)
<footer>        (선택)
```

| 커밋 유형 | 의미 |
| --- | --- |
| `feat` | 새로운 기능 추가 |
| `fix` | 버그 수정 |
| `docs` | 문서 수정 |
| `refactor` | 코드 리팩토링 |
| `chore` | 패키지 매니저 수정, 그 외 기타 수정 (예: .gitignore) |

- type은 소문자로 쓴다.
- subject는 50자 이내, 마침표 없음, 명령형 현재시제로 쓴다.
- type 이후 subject·body·footer는 한글로 작성한다.
- body에는 변경한 내용과 이유를 쓴다. 어떻게보다 **무엇을·왜**를 설명한다.
- 한 commit에는 한 가지 문제만 담는다.
- commit 전 `git status`, `git diff`로 변경 범위를 확인한다.
- 현재 작업과 관련된 파일만 stage한다. `git add .` / `git add -A`를 습관적으로 쓰지 않는다.
- 커밋 메시지 템플릿은 `gitmessage.txt`다. 최초 1회 등록: `git config --local commit.template gitmessage.txt`

## Push / PR

- `feature/*` → `develop` PR을 기본으로 한다.
- `fix/*`는 `develop` 또는 `release/*`로 PR한다.
- PR 제목은 커밋 컨벤션을 그대로 따른다. (예: `feat: OCR 블록 그룹핑 규칙 추가`)
- PR 본문은 `.github/pull_request_template.md`를 따른다.
  - `## 작업 내용` — 무엇을 했는지 불릿으로
  - `## 변경 이유` — 왜 필요했는지 / 관련 이슈 번호
  - `## 특이사항` — 스키마 변경, 의존성 추가, 다른 레포에 영향 등
  - `## 체크리스트` — 로컬 동작 확인, 관련 문서 갱신

## Safety

- `force push`, `reset --hard`, `clean -fd`, branch 삭제는 사용자의 명시적 요청 없이 실행하지 않는다.
- `.env`, `.env.*`, secret, API key, access token, password 등은 commit하지 않는다.
