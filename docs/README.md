# Pixate API 문서 (트래킹 명세)

이 폴더는 Pixate 서비스의 API 트래킹 명세를 담고 GitHub Pages로 Swagger UI를 배포합니다.

- `openapi.yaml` — **단일 소스(SSOT)**. API를 추가·변경하면 이 파일만 수정합니다.
- `index.html` — `./openapi.yaml`을 불러오는 Swagger UI 페이지.
- `../.github/workflows/` — PR 검증 · Pages 자동 배포 · 팀 알림 워크플로.

## 최초 1회 설정 (관리자)

1. 이 리포에 파일을 커밋·push 합니다. (리포 루트에 `docs/`와 `.github/`가 오도록)
2. **Settings → Pages → Build and deployment → Source** 를 **GitHub Actions** 로 지정합니다.
3. `develop`에 push하면 Actions가 돌고, 완료되면 Pages URL이 생성됩니다.
   (예: `https://ai-pixlate.github.io/<repo>/`)

## 문서 업데이트

`docs/openapi.yaml`을 수정해 `develop` 대상으로 PR을 올리고 머지하면 문서가 자동 갱신됩니다.
PR·커밋 히스토리로 API 변경 이력이 그대로 추적됩니다.

## 추적 규약

각 오퍼레이션의 확장 필드로 릴리스를 추적합니다.

- `x-feature-id` — 요구사항정의서 기능ID (예: `F-SRC-10`)
- `x-release` — `9월` / `9월 should` / `12월` / `이후`
- `x-priority` — `P0`~`P3`
- `x-mvp` — MVP(9월 원본 규격 유지·미국·영어) 포함 여부
- `x-stub` — 향후 활성 예정(미구현) 스텁 여부
