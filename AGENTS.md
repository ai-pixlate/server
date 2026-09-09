# Pixate Server

AI 상세페이지 로컬라이제이션 서비스 Pixate의 백엔드 인프라 서버 저장소.

API 트래킹 문서(OpenAPI 명세·Swagger 사이트)는 `api-tracking` 브랜치에서 관리한다.
문서 사이트: https://ai-pixlate.github.io/server/

## Communication

- 모든 응답과 설명은 한국어로 작성한다.

## Git Remote

- `origin`: https://github.com/ai-pixlate/server.git — 팀 공식 repository. pull/push/PR 기준.

## Branch

| 브랜치 | 용도 |
| --- | --- |
| `develop` | 백엔드 인프라 서버 개발 (기본 브랜치) |
| `main` | 릴리스 |
| `api-tracking` | API 트래킹 문서 (OpenAPI·Swagger) |

- `main`, `develop`, `api-tracking`은 직접 commit/push 금지. `develop`은 PR로만 반영한다.
- 기능 개발은 `feature/*`, 버그 수정은 `fix/*`를 사용하며 `develop`에서 분기한다.
- 브랜치명은 영어 소문자 kebab-case로 쓴다. (예: `feature/job-status-polling`)

## Commit

- 형식: `<type>: <subject>`
- type: `feat` `fix` `docs` `refactor` `chore`
- subject는 한글, 50자 이내, 마침표 없이 쓴다.
- 한 commit에는 한 가지 문제만 담는다.
- commit 전 `git status`, `git diff`로 변경 범위를 확인한다.
- 현재 작업과 관련된 파일만 stage한다. `git add .` / `git add -A`를 습관적으로 쓰지 않는다.
- 커밋 메시지 템플릿은 `gitmessage.txt`다. 최초 1회 등록: `git config --local commit.template gitmessage.txt`

## Push / PR

- `feature/*` → `develop` PR을 기본으로 한다.

## Safety

- `force push`, `reset --hard`, `clean -fd`, branch 삭제는 사용자의 명시적 요청 없이 실행하지 않는다.
- `.env`, `.env.*`, secret, API key, access token, password 등은 commit하지 않는다.
