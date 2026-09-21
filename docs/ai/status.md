# status.md — 구현 현황 · 인수인계

> 성격: **살아 있는 문서.** 세션을 끝낼 때 갱신한다. 새 세션은 `README.md` 1절 순서로 읽고 이 문서 2절 "다음 착수"부터 시작한다.
> 여기에는 "어디까지 됐고 다음이 무엇인지"만 적는다. 구조 결정은 `dev.md`, 파라미터는 config·`pipeline.md` 3절, 미결은 `open-questions.md`에 둔다.
> 상태값: `미착수` / `뼈대` 배선만 / `구현 중` / `로컬 검증` 샘플 통과 / `GPU 실측` / `확정` 사용자 확정.

---

## 1. 단계별 현황 — 2026-09-20

| 단계 | 파일 | 상태 | 남은 것 |
|---|---|---|---|
| ① 섹션 분해 | `pipeline/stages/section_split.py` | 뼈대 — `open_source()`(IMAGE_OPEN_FAILED) · `crop_sections()` 동작, **경계 결정 미구현** | 배경색 전환 경계 규칙(임계값 등 세부가 정본에 없으면 `open-questions.md`에 올린다) · VLM 경계 선택(모델명 #21) |
| ② 텍스트 추출 | `pipeline/stages/ocr.py` | 미착수 | PaddleOCR 연결 · 4,000px 임시 분할·좌표 복원·겹침 중복 정리 · `requirements-ocr.txt` 버전 고정 |
| ③ 병합·역할 | `pipeline/stages/merge.py` | 미착수 | `heuristic_v2` · `llm_assist` 프롬프트(`prompts/merge_assist.md`) · `source_ko` 줄 결합 규칙(#24) |
| `analyze()` | `pipeline/analyze.py` | 뼈대 — ①→②→③ 배선, 경고·오류 처리 | 워커 연결(`app/tasks.py` `run_analyze`가 `AnalyzeResult`를 `section`·`text_block` 행으로 저장) — BE 합의 |
| ③-1 · ③-1' · ④ · ⑤ · ⑥ · ⑦ · ⑧ | — | 미착수 | 파일명 예약만(`dev.md` 1절). 착수 시 타입·config 키 추가 |
| 타입·config·CLI·inspect | `pipeline/types.py` `config.py` `run.py` `inspect.py` | 로컬 검증 — 테스트 16개 통과 | — |
| 샘플 | `pipeline/samples/synthetic_01/` | 형식 예시 | 실제 한국어 표본을 `samples/local/`(git 제외)에 준비 |
| 환경 | `pipeline/requirements*.txt` | 로컬 기본만 검증 | `-ocr` · `-gpu` 설치 검증 후 버전 고정 |

## 2. 다음 착수 순서

1. **② OCR** — 로컬 CPU에서 되므로 먼저. `requirements-ocr.txt` 검증을 겸한다. 완료 기준: `samples/local/` 표본에서 `ocr/<key>.json` + 오버레이 확인, 4,000px 초과 섹션 분할·복원 테스트.
2. **① 섹션 분해** — 경계 규칙 구현. `synthetic_01`의 `expected/split.json`과 일치가 1차 기준.
3. **③ 병합·역할** — 휴리스틱 먼저, LLM 보정은 프롬프트 작성 후. #24 결정 필요.
4. **`analyze()` 워커 연결** — BE와 `run_analyze` 변경 합의. `run.json` 내용을 `event_log.payload`로.
5. 그 다음 ③-1(판정 방식 #5 결정 후) · ④ · ⑤ · ⑦ · ⑥(GPU) · ⑧(규제 표 #1 이후).

## 3. 세션 로그

| 날짜 | 세션 | 한 일 | 결정·문서 |
|---|---|---|---|
| 2026-09-20 | 개발 기반 정리 | `pipeline/` 패키지(타입 · 오류 · config · CLI · inspect · 샘플 · 테스트) 추가. 문서 `dev.md` `status.md` 신설 | `open-questions.md` #22 해결(5절), #24 추가. `pipeline.md` 헤더·3절·⑧행·9절, `README.md` 1·3·4·6절, 루트 `AGENTS.md` `README.md` 갱신 |

## 4. 인수인계 메모

- 이 세션의 실행 환경에서 확인하지 못한 것: `requirements-ocr.txt` · `requirements-gpu.txt` 설치, GPU 서버 접속.
- `synthetic_01`은 영문 합성 이미지다. 한국어 OCR·역할 판정의 실제 검증은 `samples/local/` 표본으로 한다.
- 워커(`app/tasks.py`)는 아직 스텁이며 `pipeline`을 부르지 않는다. 연결 시 큐 라우팅(`app/celery_app.py`)은 BE 소유다(루트 `README.md`).
