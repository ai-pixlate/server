# dev.md — AI 파이프라인 개발 기반

> 성격: **레포 구조 결정의 정본.** 세 상위 정본(개발계획 · 스키마 계약 · PoC 요약)에 없는 사항이라 출처 태그 대상이 아니며, 이 문서가 소유한다(`README.md` 3절 예외와 같은 성격).
> 결정일: 2026-09-20 — `open-questions.md` #22 해결. 바꿀 때는 이 문서와 코드를 같은 커밋에서 고친다.
> 범위: 코드·설정·프롬프트 위치 / 단계 간 Python 타입 / 단계별 실행 / 샘플·확인 / 환경·의존성. 구현 현황과 인수인계는 `status.md`.
> 값의 의미·계산 규칙은 `contract.md`, 흐름은 `pipeline.md`. 여기서는 정하지 않는다.

---

## 0. 결정 요약

| 정할 것 | 결정 | 절 |
|---|---|---|
| AI 코드·설정·프롬프트 위치 | `pipeline/` 패키지. 설정 `pipeline/config/default.toml`, 프롬프트 `pipeline/prompts/` | 1 · 2 |
| 단계 사이의 Python 입출력 타입 | `pipeline/types.py`의 pydantic 모델. 단계 = `run(입력 타입, cfg) -> 출력 타입`, JSON으로 그대로 저장·전달 | 3 |
| 단계별 실행 방법 | `python -m pipeline.run <stage>` — 파일 입출력. 서버·DB·큐 없이 한 단계만 돈다 | 4 |
| 샘플 입력과 결과 확인 | `pipeline/samples/<이름>/` 레이아웃 + `inspect` 오버레이 PNG | 5 |
| 실행 환경·의존성 | 3단계 requirements: 로컬 기본 / OCR(CPU) / GPU 서버 | 6 |
| 구현 현황·인수인계 | `docs/ai/status.md`. 세션을 끝낼 때마다 갱신 | `status.md` |

## 1. 폴더 구조

```text
pipeline/                    AI 파이프라인 코드 (BE 코드 app/ 와 분리)
  __init__.py
  types.py                   단계 간 입출력 타입 — 3절
  errors.py                  AnalyzeError · 오류 코드 정책 [계약 8장]
  config.py                  config 로더 · --set override
  config/default.toml        실행 파라미터 정본 — 2절
  prompts/                   프롬프트 원문 (문서에는 경로만) — 2절
  vlm.py                     VLM 호출자(google-genai) · VlmError — ① 긴 구간 경계 선택. 실패 처리는 #25 미정
  stages/
    section_split.py         ① 섹션 분해
    ocr.py                   ② 텍스트 추출
    merge.py                 ③ 줄·문단 병합 + 역할 분류
  analyze.py                 analyze() = ① → ② → ③ [계약 1.2]
  run.py                     단계별 실행 CLI — 4절
  inspect.py                 결과 오버레이 — 5절
  samples/                   샘플 입력·기대 결과 — 5절
  requirements*.txt          환경별 의존성 — 6절
tests/test_pipeline_*.py     파이프라인 테스트 (루트 pytest가 함께 돈다)
gpu/                         GPU 서버 컨테이너·k8s (기존, gpu/README.md)
docs/ai/                     정본 문서
```

원칙:

- `pipeline/`은 `app/`(BE)을 import하지 않는다. 워커(`app/tasks.py`)가 `pipeline.analyze`를 부르는 방향만 허용한다. S3·DB·임시 식별자→DB id 변환은 워커 몫이다(`contract.md` 1.1, `db-map.md` 2절).
- 단계 파일 하나 = `pipeline.md` 단계표 한 행. 파일은 `run()` 하나를 노출하고 타입은 `types.py`만 쓴다.
- 후속 단계 파일명은 예약해 둔다: `judge.py`(③-1) · `policy.py`(③-1', 구현 소유자 미정 #6) · `label.py`(④) · `logo.py`(⑤) · `inpaint.py`(⑥) · `style.py`(⑦) · `translate.py`(⑧). 착수 시 타입·config 키를 함께 추가한다(7절).

## 2. 설정과 프롬프트

**config** — `pipeline/config/default.toml`이 값의 정본이다(`README.md` 4.1).

- TOML 표 이름 = `pipeline.md` 3절 키의 접두어(`[ocr]` `split_threshold_px` ↔ `ocr.split_threshold_px`). 두 곳의 키와 기본값은 항상 같아야 한다.
- 실험 중 값 변경은 `--set 표.키=값`(반복 가능)으로만 한다. 파일과 `pipeline.md` 3절은 사용자가 "확정"이라고 말할 때만 함께 고친다.
- 파일에 없는 키는 **미정**이다. override로도 만들 수 없다(`ConfigKeyError`). 미정 키를 쓰려면 먼저 `open-questions.md`를 닫는다. 현재 없는 키: ③-1 판정(#5). `[section]`의 임계값·기준값은 키는 있으나 값이 잠정이다(#26).
- 통째로 바꾸려면 `--config PATH`(실험용 사본). 사본은 커밋하지 않는다.

**prompts** — `pipeline/prompts/<단계>.md`. 파일명은 config의 `*.prompt_path`와 맞춘다. 원문은 여기에만 두고 문서에는 경로만 적는다(`README.md` 4.4).

**실행 기록** — 모든 CLI 실행은 출력 디렉터리에 `run.json`(config 스냅샷 · 입력 경로 · 프롬프트 해시 · 시작/종료 시각 `started_at`/`ran_at` · 소요 시간 `duration_s`)을 남긴다. 워커 연결 시 이 값이 `event_log.payload`로 간다[계약 9장].

## 3. 단계 간 타입 (`pipeline/types.py`)

초기 분석(①~③) 범위만 정의했다. 후속 단계 타입은 착수 시 같은 파일에 덧붙인다.

| 단계 | 함수 | 입력 | 출력 |
|---|---|---|---|
| ① 섹션 분해 | `stages.section_split.run(src, cfg, out_dir, vlm=None)` | `SourceImage` | `SplitResult` (+ 섹션 PNG 파일). `vlm`은 테스트·실험용 호출자 주입, 기본은 `vlm.GeminiBoundaryPicker`. 긴 구간이 없으면 VLM을 부르지 않는다 |
| ② 텍스트 추출 | `stages.ocr.run(section, cfg)` | `Section` | `OcrResult` |
| ③ 병합·역할 | `stages.merge.run(section, ocr, cfg)` | `Section` + `OcrResult` | `MergeResult` |
| 초기 분석 | `analyze(sources, cfg, out_dir)` | `list[SourceImage]` | `AnalyzeResult`, 실패는 `AnalyzeError` |

| 타입 | 필드 | 계약 |
|---|---|---|
| `SourceImage` | `source_image_id` `upload_order` `path`(로컬) | 1.1 · 3.1 |
| `Section` | `section_key` `source_image_id` `section_order` `top_offset` `height` `width` `image_path` | 1.1 · 3.1 |
| `SplitResult` | `source_image_id` `source_width` `source_height` `sections[]` | 1.1 |
| `OcrRegion` | `region_key` `text` `score` `poly` `bbox` | 2.5 |
| `OcrResult` | `section_key` `regions[]` — 0개면 빈 목록(오류 아님) | 2.5 · 8장 |
| `Line` | `line_key` `text` `bbox` `regions[]` | 2.5 |
| `TextBlock` | `block_key` `section_key` `block_order` `source_ko` `source_lines[]` `bbox` `role` `ocr_confidence` | 2장 · 2.1 · 2.5 |
| `MergeResult` | `section_key` `blocks[]` | 1.1 |
| `AnalyzeResult` | `sections[]` `blocks[]` `warnings[]`(`NO_TEXT_DETECTED`) | 1.1 · 8장 |
| `AnalyzeError` | `code` `retryable` `message` `source_image_id` — `IMAGE_OPEN_FAILED`(재시도 불가) · `OCR_FAILED`(가능) | 8장 |

규칙:

- 좌표는 정수 픽셀, 섹션 안은 **섹션 로컬**. 원본 세로 = `top_offset + y`. `Section.image_path`는 ②·③-1·④·⑥·⑦이 그대로 읽는 잘라낸 섹션 이미지다.
- 임시 식별자: `sec_{source_image_id}_{order:02d}` · `reg_0001` · `line_001` · `blk_001`. 한 실행 안에서만 유일하면 된다.
- 모든 모델은 `extra="forbid"` — 계약 밖 키가 조용히 섞이지 않는다. `role`은 5종 Literal.
- 계약이 미정으로 둔 값은 필드가 없다: 섹션 `range`(#13), `style`(#14). 결정되면 추가한다.
- 계약이 정한 계산만 도우미로 둔다: `ocr_confidence_of()`(영역 score 최솟값, 없으면 None) · `BBox.union()` · `BBox.from_poly()`.
- 직렬화는 `model_dump_json()` / `model_validate_json()`. 단계 사이 파일 형식 = 워커에 넘기는 형식 = 사람이 읽는 형식, 셋이 같다.
- `schema_version`은 `"1"`. 타입을 호환 불가로 바꾸면 올린다.

## 4. 단계별 실행

레포 루트에서 실행한다. 모든 하위 명령은 `--config PATH`와 `--set 표.키=값`을 받는다.

| 명령 | 하는 일 | 출력 |
|---|---|---|
| `python -m pipeline.run config [--set ...]` | 유효 config 출력 | stdout |
| `python -m pipeline.run split --source IMG --out DIR` | ① | `DIR/split.json` · `DIR/sections/<key>.png` · `DIR/split_debug.json`(경계 결정 진단: 색 전환 후보와 기각 사유 · 빈 구간 병합 · VLM 창별 호출·보정·폐기) |
| `python -m pipeline.run ocr --split DIR/split.json [--section KEY] --out DIR` | ② | `DIR/ocr/<key>.json` |
| `python -m pipeline.run merge --split DIR/split.json --ocr DIR/ocr/<key>.json --out DIR` | ③ | `DIR/merge/<key>.json` |
| `python -m pipeline.run analyze --source IMG [--source IMG2] --out DIR` | ①→②→③ | `DIR/analyze.json` |
| `python -m pipeline.run inspect --split\|--ocr\|--merge JSON --image IMG --out PNG` | 오버레이 | PNG |

- 종료 코드: `0` 성공 · `2` `AnalyzeError`(stderr에 JSON) · `3` 미구현 단계 · `4` VLM 호출 실패(`VlmError`, #25 미정이라 `AnalyzeError`로 바꾸지 않는다).
- VLM 없이 ①을 돌리려면 `--set section.long_section_px=999999`(긴 구간 없음 → 호출 안 함). `GEMINI_API_KEY`가 없으면 긴 구간에서 종료 코드 4.
- 출력 디렉터리 기본은 `pipeline/out/`(git 제외). 레이아웃은 5절과 같다.
- 부분 재실행: 이전 단계 JSON을 손으로 고쳐 다음 단계에 넣을 수 있다(예: OCR 결과의 오인식을 고치고 `merge`만 다시). 파라미터 비교는 `--set`으로 같은 입력을 여러 `--out`에 돌린다.

## 5. 샘플과 확인 방식

```text
pipeline/samples/<이름>/
  source.png                 원본 (여러 장이면 source_01.png …)
  expected/                  기대 결과 — 실행 출력 DIR도 같은 레이아웃
    split.json  sections/<key>.png  ocr/<key>.json  merge/<key>.json  [analyze.json  run.json  split_debug.json]
```

| 디렉터리 | 내용 | git |
|---|---|---|
| `samples/synthetic_01/` | 합성 원본(600×1000, 배경색 2구간, 영문) + 단계별 기대 JSON. `python -m pipeline.samples.make_synthetic`로 재생성 | 포함 |
| `samples/local/` | 실제 한국어 상세페이지 표본 | **제외** |

확인 순서: ① JSON을 읽는다(원문·역할·score) → ② `inspect`로 오버레이 PNG를 만들어 좌표를 본다 → ③ 판단. 섹션 경계가 이상하면 `split_debug.json`에서 그 y의 후보 기각 사유(`median_delta` · `bg_ratio` · `min_section`)나 VLM 폐기(`dropped`) 기록을 먼저 본다. 오버레이 색: 영역 초록 / 블록 `title` 빨강 · `body` 파랑 · `caption` 회색 · `price` 주황 · `caution` 보라 / 섹션 경계 빨간 선. 라벨은 키·숫자만 그린다(한글 폰트가 없어도 깨지지 않게).

- `expected/`는 지금 **형식 예시**다. 단계가 구현되면 같은 입력의 실행 결과와 비교하는 회귀 테스트로 승격한다.
- 실제 표본으로 한 실험의 결과·판단은 이 레포가 아니라 PoC 레포에 기록한다(`README.md` 4.1).

## 6. 환경·의존성

| 환경 | 설치 | 되는 것 | 안 되는 것 |
|---|---|---|---|
| 로컬 기본 | `pip install -r pipeline/requirements.txt` | 타입 · CLI · `inspect` · 테스트 · 샘플 재생성 · ① 섹션 분해(numpy · google-genai, VLM은 API 키 필요) | 로컬 모델 단계 실행 |
| 로컬 OCR (CPU) | `pip install -r pipeline/requirements-ocr.txt` | ② (+ ③ 휴리스틱) | ⑥ |
| GPU 서버 | `gpu/` 이미지 + `pip install -r pipeline/requirements-gpu.txt` | ⑥ 실측 · 전체 | — |

- **로컬 테스트와 GPU 실측을 구분한다.** 형식·배선·휴리스틱은 로컬에서 끝내고, GPU 서버는 ⑥ 인페인팅과 전체 통과 실측에만 쓴다. GPU 서버에서는 코드와 모델 캐시를 `/data` 아래에 둔다(`gpu/README.md`).
- `requirements-ocr.txt` · `-gpu.txt`는 아직 **설치 미검증**이다. 첫 설치에서 동작한 버전을 고정하고 `status.md`에 적는다.
- LLM·VLM 호출 단계(① 경계 선택 · ③ `llm_assist` · ③-1 · ④ · ⑧)는 API 키를 환경변수 **`GEMINI_API_KEY`**로 받는다(2026-09-21, ① 착수 시 결정). 이름은 BE·배포 담당에게 전달하고, 워커에 키를 주입하는 작업은 배포 담당과 맞춘다. 키는 커밋하지 않는다.
- Python 3.11 이상(`tomllib`). BE와 GPU 이미지는 3.12.
- 테스트: `python -m pytest tests/test_pipeline_*.py` (루트 `pytest`에도 포함된다).
- 워커에서 파이프라인을 부를 때는 루트 `requirements.txt`와 `pipeline/requirements*.txt`를 함께 설치한다. 워커 이미지 구성은 BE와 합의한다.

## 7. 새 단계를 추가할 때

1. `types.py`에 입출력 타입을 추가한다. 필드마다 `contract.md` 절을 주석으로 단다. 미정 값은 넣지 않는다.
2. `stages/<이름>.py`에 `run()`을 만든다. 모듈 docstring에 `pipeline.md` 단계표 행을 요약한다.
3. `config/default.toml`에 표를 추가하고 `pipeline.md` 3절 키와 맞춘다. 프롬프트는 `prompts/`.
4. `run.py`에 하위 명령을, 필요하면 `inspect.py`에 오버레이를 추가한다.
5. `samples/*/expected/`에 기대 결과를 추가한다.
6. `tests/test_pipeline_*.py`에 계약 검증을 추가한다.
7. `status.md`를 갱신한다.
