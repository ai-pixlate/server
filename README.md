# Pixate Server

Pixate 백엔드 인프라 서버 레포지터리입니다.

> **API 트래킹 문서**(OpenAPI 명세·Swagger 문서 사이트)는 이 레포의 **`feature/api-tracking` 브랜치**에서 관리합니다.
> 문서 사이트: https://ai-pixlate.github.io/server/

## 브랜치

| 브랜치 유형 | 의미 | 비고 |
| --- | --- | --- |
| `main` | 운영 배포 | 커밋 금지 |
| `develop` | 통합 개발 (기본 브랜치) | PR로만 반영 |
| `release/*` | 배포 직전 안정화 | 버그 수정만, 기능 추가 금지 |
| `feature/*` | 기능 | 개인 작업 (자유롭게) |
| `fix/*` | 버그 수정 | `develop` 또는 `release/*`로 PR |
| `feature/api-tracking` | API 트래킹 문서 (OpenAPI·Swagger) | 이 저장소 전용, 장기 유지 |

## 기여

커밋·PR 컨벤션과 작업 규칙은 [AGENTS.md](AGENTS.md)를 따릅니다.

커밋 메시지 템플릿은 최초 1회 등록이 필요합니다.

```
git config --local commit.template gitmessage.txt
```
