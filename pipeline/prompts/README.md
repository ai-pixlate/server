# pipeline/prompts — 프롬프트 원문

`docs/ai`의 `{{PROMPTS_DIR}}`가 가리키던 디렉터리다. 프롬프트 원문은 여기에만 두고 문서에는 경로만 적는다(docs/ai/README.md 4.4).

| 파일 | 단계 | 상태 |
|---|---|---|
| `section_boundary.md` | ① 긴 구간 VLM 경계 선택 — 소제목 위 여백에서 자를 y 목록을 JSON으로. 소항목·목록 항목·라벨 아래는 제외. `{{WIDTH}}` `{{HEIGHT}}`는 실행 시 치환 | 초안 (1차 실측 반영, 2차 실측 전) |
| `merge_assist.md` | ③ `llm_assist` — 추가 병합·역할 재판정만, 분할 금지 | 미작성 |
| `label.md` | ④ 제품 라벨 판정 | 미작성 |
| `translate.md` | ⑧ 로컬라이징 번역 — RAG 용어집 구축 후 기술검증 (open-questions #11) | 미작성 |

- 파일 이름은 config의 `*.prompt_path`와 맞춘다.
- 프롬프트를 바꾸면 실행 기록(`run.json`)에 파일 해시가 남는다. 버전 관리는 git이 한다.
