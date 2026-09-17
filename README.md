# Pixate Server

Pixate 백엔드 인프라 서버 레포지터리입니다.

> **API 트래킹 문서**(OpenAPI 명세·Swagger 문서 사이트)는 이 레포의 **`feature/api-tracking` 브랜치**에서 관리합니다.
> 문서 사이트: https://ai-pixlate.github.io/server/

## GPU 서버

OCR·인페인팅 등 GPU 작업은 학교 GPU 서버(KubeSphere · V100)에서 실행합니다.
컨테이너 이미지와 배포 방법은 [gpu/README.md](gpu/README.md)를 참고하세요.

## 기여

브랜치·커밋·PR 컨벤션과 작업 규칙은 [AGENTS.md](AGENTS.md)를 따릅니다.

| 브랜치 | 역할 |
| --- | --- |
| `feature/api-tracking` | **API 문서 전용 브랜치.** 여기에 머지되면 문서 사이트가 자동으로 갱신됩니다. |
| `develop` / `main` | 백엔드 인프라 서버 코드. |
| `docs/*` | 문서 작업 브랜치. `feature/api-tracking` 으로 PR 을 올립니다. |

커밋 메시지 템플릿은 최초 1회 등록이 필요합니다.

```
git config --local commit.template gitmessage.txt
```

## 문서 작업 흐름

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

## 자동화 (GitHub Actions)

| 워크플로 | 트리거 | 하는 일 |
| --- | --- | --- |
| [`api-docs-validate.yml`](.github/workflows/api-docs-validate.yml) | `feature/api-tracking` 대상 PR / push | YAML 파싱 · Redocly lint · 추적 필드(`x-feature-id`, `x-release`) 점검 · PR 에 변경 엔드포인트 요약 코멘트 |
| [`deploy-api-docs.yml`](.github/workflows/deploy-api-docs.yml) | `feature/api-tracking` push (`docs/**`) | Swagger UI 를 GitHub Pages 로 배포 |

### 최초 1회 설정 (관리자)

1. **Settings → Pages → Source** 를 `GitHub Actions` 로 지정
2. **Settings → Environments → github-pages → Deployment branches** 에 `feature/api-tracking` 추가
3. (권장) **Settings → Branches → Add rule** 로 `develop` 보호 — PR 필수 + `OpenAPI 검증` 체크 통과 필수

## 추적 규약

각 오퍼레이션에 아래 확장 필드를 붙여 릴리스와 근거를 추적합니다.

- `x-feature-id` — 요구사항정의서 기능ID (예: `F-SRC-10`)
- `x-release` — `9월` / `9월 should` / `12월` / `이후`
- `x-priority` — `P0`~`P3`
- `x-mvp` — MVP 포함 여부
- `x-stub` — 향후 활성 예정(미구현) 스텁 여부

`x-feature-id` 또는 `x-release` 가 빠지면 PR 검증에서 경고가 표시됩니다.
