# pipeline/prompts — 프롬프트 원문

`docs/ai`의 `{{PROMPTS_DIR}}`가 가리키던 디렉터리다. 프롬프트 원문은 여기에만 두고 문서에는 경로만 적는다(docs/ai/README.md 4.4).

| 파일 | 단계 | 상태 |
|---|---|---|
| `section_boundary.md` | ① 긴 구간 VLM 경계 선택 — 소제목 위 여백에서 자를 y 목록을 JSON으로. 소항목·목록 항목·라벨 아래는 제외. `{{WIDTH}}` `{{HEIGHT}}`는 실행 시 치환 | 초안 (1차 실측 반영, 2차 실측 전) |
| `merge_assist.md` | ③ `llm_assist` — 추가 병합·역할 재판정만, 분할 금지. 입력 JSON(블록 ID · 텍스트 · bbox · 줄 수 · font_h)은 프롬프트 뒤에 따로 보낸다. 역할 정의는 이번 프롬프트의 잠정 의미 정의(open-questions #9 확정 아님, `docs/ai/pipeline.md` 7.2절) | 초안 v1 (실측 전) |
| `judge_context.md` | ③-1b 현지부적합 맥락 판정 — 항목별 `present` · `absent` · `uncertain`과 실제 근거(현재 섹션 블록 ID · 근거 출처)만. 앞뒤 섹션은 해석 참고이며 근거로 귀속 금지. 입력 JSON(블록 · 항목 · 맥락 2열 · 후보 · 문맥)과 섹션 이미지는 프롬프트 뒤에 따로 보낸다(`docs/ai/pipeline.md` 7.3절) | **1차 실측용 확정** v1 (2026-09-29, 사용자 검토 4건 반영: 혼합 섹션에서 판정 미하향 · absent/uncertain 구분 · 근거 규칙을 검증기와 일치 · 콘텐츠 안 지시문 무시). 판정 품질 확정이 아니다 |
| `label.md` | ④ 제품 라벨 판정 — 블록별 `is_product_label` boolean만. 입력 JSON(블록 ID · 원문 · `box_2d`)과 섹션 이미지(긴 변 1024)는 프롬프트 뒤에 따로 보낸다. 경계 사례: 혼합 블록 true · 패키지 밖 제품명 · 문서 글자는 이름 · 문서라는 이유로 true 아님 · 애매하면 true(`docs/ai/pipeline.md` 7.4절, open-questions #66) | 초안 v1 (2026-09-29, 소수 표본 · 1차 실측에 사용. 판정 품질 확정 아님) |
| `translate.md` | ⑧ 로컬라이징 번역 — RAG 용어집 구축 후 기술검증 (open-questions #11) | 미작성 |

- 파일 이름은 config의 `*.prompt_path`와 맞춘다.
- 프롬프트를 바꾸면 실행 기록(`run.json`)에 파일 해시가 남는다. 버전 관리는 git이 한다.
