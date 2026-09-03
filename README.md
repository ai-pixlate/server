# Pixate Server — API 트래킹 문서

Pixate 백엔드 API의 **단일 소스(SSOT)** 명세와 팀 공유용 문서 사이트를 관리하는 저장소입니다.

| 항목 | 위치 |
| --- | --- |
| API 명세 (SSOT) | [`docs/openapi.yaml`](docs/openapi.yaml) |
| 문서 사이트 (Swagger UI) | https://ai-pixlate.github.io/server/ |
| 문서 운영 가이드 | [`docs/README.md`](docs/README.md) |

## 브랜치 전략

| 브랜치 | 역할 |
| --- | --- |
| `develop` | **기본 브랜치.** 팀 작업이 모이는 곳. 여기에 머지되면 문서 사이트가 자동으로 갱신됩니다. |
| `main` | 릴리스 기준 브랜치. `develop` 이 안정화되면 머지합니다. |
| `feat/*`, `docs/*` | 작업 브랜치. `develop` 으로 PR 을 올립니다. |

## 문서 수정 방법 (팀원용)

```bash
git switch develop
git pull
git switch -c docs/add-xxx-endpoint

# docs/openapi.yaml 수정 후
git add docs/openapi.yaml
git commit -m "docs(api): XXX 엔드포인트 추가"
git push -u origin docs/add-xxx-endpoint
```

이후 GitHub 에서 `develop` 대상으로 PR 을 올리면 자동 검증이 돌고,
어떤 엔드포인트가 추가/삭제됐는지 PR 코멘트로 요약됩니다.

## 자동화 (GitHub Actions)

| 워크플로 | 트리거 | 하는 일 |
| --- | --- | --- |
| [`api-docs-validate.yml`](.github/workflows/api-docs-validate.yml) | `develop` 대상 PR / `develop` push | YAML 파싱 · Redocly lint · 추적 필드(`x-feature-id`, `x-release`) 점검 · PR 에 변경 엔드포인트 요약 코멘트 |
| [`deploy-api-docs.yml`](.github/workflows/deploy-api-docs.yml) | `develop` push (`docs/**`) | Swagger UI 를 GitHub Pages 로 배포 |

### 최초 1회 설정 (관리자)

1. **Settings → Pages → Source** 를 `GitHub Actions` 로 지정
2. **Settings → Branches → Default branch** 를 `develop` 으로 변경
3. (권장) **Settings → Branches → Add rule** 로 `develop` 보호 — PR 필수 + `OpenAPI 검증` 체크 통과 필수

## 추적 규약

각 오퍼레이션에 아래 확장 필드를 붙여 릴리스와 근거를 추적합니다.

- `x-feature-id` — 요구사항정의서 기능ID (예: `F-SRC-10`)
- `x-release` — `9월` / `9월 should` / `12월` / `이후`
- `x-priority` — `P0`~`P3`
- `x-mvp` — MVP 포함 여부
- `x-stub` — 향후 활성 예정(미구현) 스텁 여부

`x-feature-id` 또는 `x-release` 가 빠지면 PR 검증에서 경고가 표시됩니다.
