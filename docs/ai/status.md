# status.md — 구현 현황 · 인수인계

> 성격: **살아 있는 문서.** 세션을 끝낼 때 갱신한다. 새 세션은 `README.md` 1절 순서로 읽고 이 문서 2절 "다음 착수"부터 시작한다.
> 여기에는 "어디까지 됐고 다음이 무엇인지"만 적는다. 구조 결정은 `dev.md`, 파라미터는 config·`pipeline.md` 3절, 미결은 `open-questions.md`에 둔다.
> 상태값: `미착수` / `뼈대` 배선만 / `구현 중` / `로컬 검증` 샘플 통과 / `GPU 실측` / `확정` 사용자 확정.

---

## 1. 단계별 현황 — 2026-09-22

| 단계 | 파일 | 상태 | 남은 것 |
|---|---|---|---|
| ① 섹션 분해 | `pipeline/stages/section_split.py` · `pipeline/vlm.py` | 로컬 검증 + 1~5차 실측(34장) 반영 — 배경다움 조건 · 빈 구간 병합(색 단계·VLM 보정 뒤) · 강도 기준 최소 높이 · 폭 기준 VLM 입력·창 분할 · 글자 위 색 경계 보정 · 진단 기록 · 재생·seed 실험 도구. 기본값 3개 확정(#26). 5차 채점 69/70(GS-02_014 run3 기준) · 오절단+한계 9. 섹션 분해 테스트 41개 통과(Linux) | 통합 PR → `develop`. 후속(별도 PR): #27 목록 규칙 · #28 프롬프트 실험 · #29 제목 분리 · #30 이웃 창 민감도 · VLM 실패 처리(#25 BE 합의) |
| ② 텍스트 추출 | `pipeline/stages/ocr.py` | 미착수 | PaddleOCR 연결 · 4,000px 임시 분할·좌표 복원·겹침 중복 정리 · `requirements-ocr.txt` 버전 고정 |
| ③ 병합·역할 | `pipeline/stages/merge.py` | 미착수 | `heuristic_v2` · `llm_assist` 프롬프트(`prompts/merge_assist.md`) · `source_ko` 줄 결합 규칙(#24) |
| `analyze()` | `pipeline/analyze.py` | 뼈대 — ①→②→③ 배선, 경고·오류 처리 | 워커 연결(`app/tasks.py` `run_analyze`가 `AnalyzeResult`를 `section`·`text_block` 행으로 저장) — BE 합의 |
| ③-1 · ③-1' · ④ · ⑤ · ⑥ · ⑦ · ⑧ | — | 미착수 | 파일명 예약만(`dev.md` 1절). 착수 시 타입·config 키 추가 |
| 타입·config·CLI·inspect | `pipeline/types.py` `config.py` `run.py` `inspect.py` | 로컬 검증 — 파이프라인 테스트 42개 통과(Linux). `run.json`에 시작 시각·소요 시간, `split` 명령은 `split_debug.json` 진단 | `test_sample_generator_is_reproducible`는 DejaVuSans가 없는 PC(Windows)에서 실패 — 4절 |
| 샘플 | `pipeline/samples/synthetic_01/` | 형식 예시 | 실제 한국어 표본을 `samples/local/`(git 제외)에 준비 |
| 환경 | `pipeline/requirements*.txt` | 로컬 기본만 검증 | `-ocr` · `-gpu` 설치 검증 후 버전 고정 |

## 2. 다음 착수 순서

1. **② OCR** — 로컬 CPU에서 되므로 다음 착수. `requirements-ocr.txt` 검증을 겸한다. 입력은 ① 5차 실측 결과로 만든 OCR 고정 입력 v1(4절 보관 방식). 완료 기준: `samples/local/` 표본에서 `ocr/<key>.json` + 오버레이 확인, 4,000px 초과 섹션 분할·복원 테스트. 착수 전 준비: `run.json`에 커밋 해시 기록, `split.json` `image_path`의 상대 경로 해석, `dev.md` 5절 `expected/` 용도 구분(OCR 정답은 별도).
2. **① 후속 개선(별도 PR)** — ①은 5차 실측(34장, 글자 잘림 수정 검증)까지 반영하고 통합한다. 남은 것은 (a) #28 프롬프트 실험 전에 정답 목록의 채점 기준 보완(`dev.md` 5절 대체 위치 규칙), (b) #28 프롬프트 실험(현행 비교군 포함 3조건 × 3회, VLM 호출 이미지 20장), (c) #27 목록 규칙은 채택 조건 충족 전까지 보류, (d) #29 제목 분리(그라데이션 후보·줄 간격 보정)와 #30 이웃 창 민감도는 자료 확보 후. 알려진 한계: FAQ 항목·제목 띠 일부 오절단, VLM 비결정성(실행마다 오가는 경계). 파라미터 탐색은 종료.
3. **③ 병합·역할** — 휴리스틱 먼저, LLM 보정은 프롬프트 작성 후. #24 결정 필요.
4. **`analyze()` 워커 연결** — BE와 `run_analyze` 변경 합의. `run.json` 내용을 `event_log.payload`로.
5. 그 다음 ③-1(판정 방식 #5 결정 후) · ④ · ⑤ · ⑦ · ⑥(GPU) · ⑧(규제 표 #1 이후).

## 3. 세션 로그

| 날짜 | 세션 | 한 일 | 결정·문서 |
|---|---|---|---|
| 2026-09-22 | ① 5차 실측 반영 | 글자 잘림 수정(`6d09dfb`) 검증 결과 **채택**. 검증 범위: 34장 + 4차 저장 응답 재생. 33장은 최종 경계·VLM 결과가 4차와 동일(색 후보 기록 자체는 29장에서 보정 발동으로 달라짐), GS-02_014만 8626 제거(이동 후 창 변화로 기각, #30) · 9234→9200 이동. 선별 검사+육안 범위에서 글자 획 절단 2→0건, 새 잘림 없음. 채점: 오절단+한계 10→9, 정답 검출은 VLM 3회 중 2891 유무로 68~69/70(수정과 무관한 비결정). 이 표본과 저장 응답 기준으로 추가 회귀는 관찰되지 않았다 — 일반 보장은 아님. 잔존: 제목 두 줄 분리(#29 신설, 구현 보류) · 이웃 창 민감도(#30 신설, 피해 미확인·보류). OCR 고정 입력 v1은 r5/full 기준, GS-02_014는 run3 선택(4절) | `open-questions.md` #29 · #30, `pipeline.md` 3절, `dev.md` 5절, `status.md` 1·2·4절 |
| 2026-09-22 | ① 글자 잘림 수정 | 사용자 확인: GS-02_014 9234(노란 띠 제목 "간편하게 관리" 가운데)·8626(회색 고지문 첫 줄 윗부분)에서 색 경계가 글자를 가름. 원인 추정: `ceb5555`에서 경계 앞뒤 균일 행 조건을 배경다움 비율로 바꾸며 경계 행 자체의 여백 보호가 사라짐(확정은 이전 방식 비교 필요). 수정: 글자 위에 놓인 색 경계를 반경 안 위쪽 여백 끝 행(없으면 아래쪽 여백 시작)으로 옮기고 여백이 없으면 기각(`no_blank_row`). 합성 테스트 5개 추가. **실측 미검증** — 검증 순서: 색 단계만(34장, VLM 생략) 이동·기각 경계 목록과 두 문제 이미지 확대 확인 → 전체 실행으로 정상 경계 손실·채점·글자 보존 확인. 균일 행 검사는 보조 지표이며 글자 보존 보증이 아님 | `pipeline.md` 3절 |
| 2026-09-22 | ① 통합 정리 | 사용자 판단: ①은 34장 검증 결과를 수용해 `develop`에 통합하고 ② OCR로 넘어간다. 다음 착수를 ② OCR 우선으로 바꾸고 ① 후속(#27·#28·채점 기준)은 별도 PR로. 워커 연결과 VLM 실패 정책(#25)은 별도 작업 | `status.md` 2·4절 |
| 2026-09-22 | ① 4차 실측 반영 | `blank_row_std` 8.0 단독 비교 → 6.0 확정 후보(색 단계 오절단 5건 추가, 보정 차이 ±1px). #27 분석 완료·구현 보류(변형(b) 후보, 채택 조건 2개). VLM 보정 뒤 빈 구간 병합은 102회 시도 중 성공한 100회에서 미발동 — 합성 테스트로 확인된 결함 방지 코드로 유지하되 실측 무해성 입증은 아님. 채점 기준(대체 위치 일대일)을 `dev.md` 5절에 추가 | `open-questions.md` #26·#27·#28, `dev.md` 5절, `status.md` 2절 |
| 2026-09-22 | ① 기본값 확정 | `bg_row_ratio` 0.25 · `snap_radius_px` 40 · `color_delta` 12를 현재 MVP 기본값으로 확정(사용자). 변경 금지 아님, 검증 범위 34장. #27은 구현 보류·분석만, 다음 실험은 `blank_row_std` 단독 비교 → #28 순 | `open-questions.md` 5절 #26(일부) · #27, config 주석, `pipeline.md` 3절, `status.md` 2절 |
| 2026-09-22 | ① 3차 실측 반영 | 3차(분리 실험) 결과: `bg_row_ratio` 0.25 · `snap_radius_px` 40 · `color_delta` 12 확정 후보, `blank_row_std` 독립 비교 미완료, 나머지 기준값 유지, seed 미사용. VLM 보정 뒤 빈 구간 병합 재실행(색 경계 유지·VLM 경계 삭제, `empty_merged_after_vlm`) 구현. 반경 설명 명확화. 실측 기록은 PoC 레포 | `open-questions.md` #26 분류 · #27(목록 규칙 가설) · #28(프롬프트) 추가. `pipeline.md` 3절, `dev.md` 4절 |
| 2026-09-22 | ① 실험 도구 | 2차 실측(34장) 검토 후 실험 분리 도구 추가: `split --vlm-replay`(이전 VLM 응답 재생, 원본·설정·구간·창 좌표·입력 이미지 해시·프롬프트 검증, 기록 미소진 시 실패) · `vlm_seed` 키(기본 미전송) · 진단 기록에 원본 지문·VLM 설정·프롬프트 해시. requirements 주석 ASCII화(cp949 pip). 정답 기준을 `dev.md` 5절에 기록. 알고리즘·기본값 변경 없음 | `pipeline.md` 3절(`vlm_seed`), `dev.md` 4·5·6절 |
| 2026-09-22 | ① 1차 실측 반영 | 34장 실측 결과로 경계 결정 개정: 후보 양쪽 배경다움 조건(사진·표 띠 3분할 제거) · 빈 구간 병합 · 이웃 구간 중앙값 · 강도 기준 최소 높이 · 후보 위치를 행 변화 최대점으로 · VLM 입력 폭 기준·창 분할 · 보정 폐기 진단 · `run.json` 소요 시간 · SDK 경고 억제 · 프롬프트에 소항목 제외. 실측 기록 자체는 PoC 레포 | `[section]` 키 추가·교체(`bg_row_ratio` · `vlm_width_px` · `vlm_window_px` · `vlm_window_overlap_px`, `vlm_long_side_px` 제거). `pipeline.md` 3절, `open-questions.md` #26, `dev.md` 2·4·5절 |
| 2026-09-21 | ① 코드 검증 수정 | 여백 보정을 현재 색 구간 안에서만 탐색(색 전환 양쪽 여백이 합쳐져 유효한 VLM 경계가 소실되던 문제) · Gemini 클라이언트 생성 예외도 `VlmError`로 감싸 CLI 종료 코드 4 보장. 회귀 테스트 3개 추가 | 알고리즘 기본값 · #25 정책 변경 없음. `status.md` 1·4절 |
| 2026-09-21 | ① 섹션 분해 구현 | `color_snap_vlm2` 경계 결정 구현(행 프로파일 · 색 전환 · 최소 높이 · 긴 구간 VLM · 여백 보정), `pipeline/vlm.py` · `prompts/section_boundary.md` · `[section]` config 키 · 테스트 15개 추가. `color_delta` 30→12(흰색↔연회색 26이 잡히도록) | #21 해결(5절), #26 추가. `pipeline.md` 3절·9절, `dev.md` 1·2·3·4·6절, `requirements.txt`(numpy · google-genai) |
| 2026-09-21 | ① 착수 전 미결 정리 | 섹션 분해 미결 검토. #22는 기해결 확인. VLM 실패 처리를 계약 공백으로 등록, API 키 환경변수 이름 확정 | `open-questions.md` #25 추가 · 3절 B 갱신. `contract.md` 8장·10장, `pipeline.md` 7절·9절, `dev.md` 6절(`GEMINI_API_KEY`) 갱신 |
| 2026-09-20 | 개발 기반 정리 | `pipeline/` 패키지(타입 · 오류 · config · CLI · inspect · 샘플 · 테스트) 추가. 문서 `dev.md` `status.md` 신설 | `open-questions.md` #22 해결(5절), #24 추가. `pipeline.md` 헤더·3절·⑧행·9절, `README.md` 1·3·4·6절, 루트 `AGENTS.md` `README.md` 갱신 |

## 4. 인수인계 메모

- 이 세션의 실행 환경에서 확인하지 못한 것: `requirements-ocr.txt` · `requirements-gpu.txt` 설치, GPU 서버 접속.
- `test_sample_generator_is_reproducible`(샘플 생성기 재현성)는 이 브랜치 이전부터 있던 환경 의존성이다. `make_synthetic._font()`가 DejaVuSans를 못 찾으면 Pillow 기본 폰트(Aileron)로 대체돼 글자 bbox가 달라진다. Windows 등 DejaVu가 없는 PC에서 실패하며, 기대 좌표를 그 PC 기준으로 덮어쓰지 않는다. 해결은 폰트 파일을 레포에 동봉하거나 테스트를 DejaVu 있을 때만 돌리는 것 중 택일 — 미결정.
- `synthetic_01`은 영문 합성 이미지다. 한국어 OCR·역할 판정의 실제 검증은 `samples/local/` 표본으로 한다.
- ① 섹션 분해의 실측 기록(1~4차 보고서 · 정답 목록 `truth.json` · 드라이버·분석 스크립트)은 레포 밖(PoC 레포 · 로컬 스크래치패드)에 있다. `pipeline/out/`은 git 제외. 실측을 이어 가려면 `samples/local/` 34장과 `truth.json`이 필요하다.
- **OCR 고정 입력 v1 보관 방식**(5차 실측 후 결정, 레포 밖): `pipeline/out/r5/full/`과 GS-02_014의 세 실행(`_run1`·`_run2`·`_run3`)은 보고서가 참조하므로 그대로 보존하고 덮어쓰지 않는다. 별도 폴더(v1)에 33장은 r5/full, GS-02_014는 **run3**(3회 중 정답 2891을 유지한 실행)을 복사하고, 출처(실행 디렉터리 · 회차 · 선택 기준 · 실행 커밋 `6d09dfb`)를 v1 안에 기록한다. 복사본 `split.json`의 `image_path`는 복사 위치에 맞춘다(run3 JSON은 run3 폴더의 절대 경로를 가리킨다). 이 선택은 OCR 개발용이며 ① 성능의 근거로 쓰지 않는다. 규칙은 `dev.md` 5절.
- ① 5차 검증의 범위 한정: "글자 획 절단 0건 · 회귀 없음"은 34장 표본, 4차 저장 응답 재생, 균일 행 선별 검사 + 육안 확인 범위의 결과다. 다른 표본·실제 VLM 호출에서의 보장이 아니다. 보정은 후보 1,207건 중 1,033건에서 발동하므로 실행 간 비교는 `accepted` 집합으로 한다(`dev.md` 5절).
- ①의 VLM 실패 처리(#25)는 BE 합의 전이라 `VlmError`가 그대로 전파된다(CLI 종료 코드 4). 워커 연결 시 이 정책을 먼저 정해야 한다.
- 워커(`app/tasks.py`)는 아직 스텁이며 `pipeline`을 부르지 않는다. 연결 시 큐 라우팅(`app/celery_app.py`)은 BE 소유다(루트 `README.md`).
