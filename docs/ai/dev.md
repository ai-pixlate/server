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
  jsonio.py                  단계 JSON 읽기·쓰기 — 버전 확인 · image_path 해석 · 전환 · 고정 입력본 — 3·5절
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

**실행 기록** — 모든 CLI 실행은 출력 디렉터리에 `run.json`(config 스냅샷 · 입력 경로 · 프롬프트 해시 · 시작/종료 시각 `started_at`/`ran_at` · 소요 시간 `duration_s` · 커밋 `git_commit` · 추적 파일 변경 여부 `git_dirty`)을 남긴다. git을 쓸 수 없으면 두 값은 `null`. 워커 연결 시 이 값이 `event_log.payload`로 간다[계약 9장].

## 3. 단계 간 타입 (`pipeline/types.py`)

초기 분석(①~③) 범위만 정의했다. 후속 단계 타입은 착수 시 같은 파일에 덧붙인다.

| 단계 | 함수 | 입력 | 출력 |
|---|---|---|---|
| ① 섹션 분해 | `stages.section_split.run(src, cfg, out_dir, vlm=None)` | `SourceImage` | `SplitResult` (+ 섹션 PNG 파일). `vlm`은 테스트·실험용 호출자 주입, 기본은 `vlm.GeminiBoundaryPicker`. 긴 구간이 없으면 VLM을 부르지 않는다 |
| ② 텍스트 추출 | `stages.ocr.run(section, cfg, engine=None)` | `Section` | `OcrResult`. `engine`은 테스트·실험용 호출자 주입(BGR 배열 → PaddleOCR 결과 dict), 기본은 `ocr.build_engine(cfg)`(설정별 1회 생성). 높이 > `ocr.split_threshold_px`면 `NotImplementedError`(임시 분할 #31 · #32 미구현, 조용히 통과시키지 않음) |
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
- 직렬화는 `model_dump_json()`. 단계 사이 파일 형식 = 워커에 넘기는 형식 = 사람이 읽는 형식, 셋이 같다.
- **파일 읽기·쓰기는 `pipeline/jsonio.py`를 거친다**(`load_split` · `load_analyze` · `load_ocr` · `load_merge` · `write_model`). `model_validate_json()`으로 파일을 직접 읽지 않는다 — 버전 필드가 없으면 pydantic 기본값이 현재 버전을 조용히 채우기 때문에, 로더가 모델 검증 전에 원본 JSON의 버전을 확인한다.
- **`image_path` 기준(2026-09-23 결정)**: `split.json` · `analyze.json`의 상대 `image_path`는 **그 JSON 파일이 있는 폴더 기준**이다. 절대 경로는 그대로 쓴다. 작업 디렉터리 기준으로 되돌아가 찾지 않는다(없으면 해석한 경로와 함께 실패). 쓸 때는 JSON 폴더 기준 상대 경로(`/` 구분)로 기록하고, 만들 수 없으면(다른 드라이브) 절대 경로. 메모리의 `Section.image_path`는 로더가 절대 경로로 바꾼 값이다.
- **`schema_version`** = `"2"`(2026-09-23, 위 경로 기준 변경). 타입을 호환 불가로 바꾸면 올린다. 타입별 읽기 허용 버전(`jsonio.READ_VERSIONS`):

  | 타입 | 경로 필드 | 읽기 허용 | 쓰기 |
  |---|---|---|---|
  | `SplitResult` | 있음 | `"2"` | `"2"` |
  | `AnalyzeResult` | 있음 | `"2"` — `"1"`은 이 도구에서 전환 미지원 | `"2"` |
  | `OcrResult` · `MergeResult` | 없음 | `"1"` · `"2"` | `"2"` |

  버전 필드가 없거나 표에 없는 버전이면 `SchemaVersionError`. **버전 1 `split.json`(옛 상대 경로 = 레포 루트·작업 디렉터리 기준)은 새 코드에서 읽히지 않는다** — `convert-split` 또는 `freeze-input`으로 전환한다(4절). 원본 실측 출력은 전환하지 않고 보존한다.

## 4. 단계별 실행

레포 루트에서 실행한다. 모든 하위 명령은 `--config PATH`와 `--set 표.키=값`을 받는다.

| 명령 | 하는 일 | 출력 |
|---|---|---|
| `python -m pipeline.run config [--set ...]` | 유효 config 출력 | stdout |
| `python -m pipeline.run split --source IMG --out DIR` | ① | `DIR/split.json` · `DIR/sections/<key>.png` · `DIR/split_debug.json`(경계 결정 진단: 색 전환 후보와 기각 사유 · 빈 구간 병합(`empty_merged` 색 단계 · `empty_merged_after_vlm` 보정 뒤) · VLM 창별 호출·보정·폐기) |
| `python -m pipeline.run ocr --split DIR/split.json [--section KEY] --out DIR` | ② — 실행 구조는 아래 "② 실행 구조" | `DIR/ocr/<key>.json`(성공한 섹션만) · `DIR/run.json` |
| `python -m pipeline.run merge --split DIR/split.json --ocr DIR/ocr/<key>.json --out DIR` | ③ | `DIR/merge/<key>.json` |
| `python -m pipeline.run analyze --source IMG [--source IMG2] --out DIR` | ①→②→③ | `DIR/analyze.json` |
| `python -m pipeline.run inspect --split\|--ocr\|--merge JSON --image IMG --out PNG` | 오버레이 | PNG |
| `python -m pipeline.run convert-split --in OLD/split.json --base DIR --out NEW/split.json [--overwrite]` | 버전 1 → 2 **일반 전환**. 옛 상대 경로를 `--base` 기준으로 해석(대상이 없으면 실패)해 새 JSON 위치 기준으로 다시 쓴다. **이미지는 옮기지 않으며 결과는 원래 이미지를 가리킨다.** `--out`이 있으면 `--overwrite` 없이는 거부. 원래 값은 stdout에 출력 | `NEW/split.json` |
| `python -m pipeline.run freeze-input --split SRC/split.json [--base DIR] --out INPUT_DIR [--meta 키=값 ...]` | **고정 입력본 생성**(5절). 섹션 PNG를 `INPUT_DIR/sections/`로 복사하고 복사본을 가리키게 한 뒤 검증. 원본은 읽기만 하고 전후 해시 비교. `INPUT_DIR`이 비어 있지 않으면 거부. 버전 1 원본은 `--base` 필요 | `INPUT_DIR/split.json` · `sections/` · `origin/`(원본 `split.json` · `split_debug.json` · `run.json` 사본) · `provenance.json` |
| `python -m pipeline.run verify-input --dir INPUT_DIR [--relocated]` | 고정 입력본 재검증. `--relocated`는 레포 밖 임시 위치로 통째 복사해 그 복사본만으로 검증 | stdout |

- 종료 코드: `0` 성공 · `2` `AnalyzeError`(모든 하위 명령, stderr에 JSON) · `3` 미구현(단계 또는 ② 4,000px 초과 섹션) · `4` VLM 호출 실패(`VlmError`, #25 미정이라 `AnalyzeError`로 바꾸지 않는다). 입력 파일 오류(`SchemaVersionError` · `InputCheckError` · `FileExistsError`)는 별도 코드 없이 예외로 끝난다(파이썬 기본 `1`).
- **② 실행 구조(`ocr`, 2026-09-23 결정)**: `--out`에 이전 `run.json`이나 `ocr/`가 있으면 거부한다(실행마다 새 폴더 — 이전 성공 JSON이 이번 실패 섹션의 결과처럼 남지 않게). 엔진을 먼저 한 번 만들고, **초기화가 실패하면 즉시 중단**해 모든 섹션을 `not_run`으로, 실행 수준 오류를 `run_error`로 기록하고 종료 코드 2. 그 뒤 섹션마다 실행하며 **섹션 오류(이미지 열기 · 추론 · 출력 형식)는 기록하고 다음 섹션으로 계속**한다. 실패를 빈 결과로 바꿔치지 않으며 실패 섹션의 `ocr/<key>.json`은 쓰지 않는다. `run.json` 추가 필드: `status`(`ok` 대상 모두 성공 · `partial` 일부 성공 + 실패·미실행 · `failed` 성공 없음) · `sections`(섹션별 `ok`+영역 수 / `failed`+`code` · `retryable` · `exception` · `message`(+`reason`) / `not_run`) · `run_error` · `engine`(버전 · 플랫폼 · 실제 적용 설정, 예: `enable_mkldnn` · `text_rec_score_thresh`). 종료 코드: `AnalyzeError` 실패가 있으면 2, 4,000px 초과 거부만 있으면 3, 없으면 0. stderr에는 첫 오류 JSON. `analyze()`는 계약 8장대로 첫 실패에서 `AnalyzeError`를 던진다. `retryable`은 현재 `errors.ERROR_POLICY` 기본값(`IMAGE_OPEN_FAILED` False · `OCR_FAILED` True)이며 세분은 #33 결정 후.
- VLM 없이 ①을 돌리려면 `--set section.long_section_px=999999`(긴 구간 없음 → 호출 안 함). `GEMINI_API_KEY`가 없으면 긴 구간에서 종료 코드 4.
- **VLM 응답 재생(실험용)**: `split --vlm-replay DIR0/split_debug.json`은 이전 실행의 창별 응답을 순서대로 재생하고 API를 부르지 않는다. 원본 지문(`input`) · VLM 설정(`vlm_config`) · 호출별 구간·창 좌표 · 실제 입력 이미지 픽셀 해시 · 크기 · 프롬프트 해시가 기록과 다르면 종료 코드 4로 멈추고, 기록이 다 쓰이지 않아도(호출 구성이 달라짐) 결과 파일 없이 종료 코드 4다. 보정(snap) 규칙처럼 **VLM 응답 이후** 단계만 바꾸는 비교에 쓴다. 색 경계가 바뀌어 구간·창이 달라지는 실험(`bg_row_ratio` 등)에는 쓸 수 없고, seed 효과 측정에도 쓸 수 없다(그건 실제 반복 호출).
- **재현성 실험**: `--set section.vlm_seed=N`으로 seed를 보낸다. 기본(-1)은 보내지 않는다. 같은 이미지를 조건별로 3~5회 돌려 필요한 경계의 유지와 잘못된 경계의 고착을 따로 센다.
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

- `color_candidates` 판독: 글자 위 경계 보정은 경계 행·바로 위 행이 균일 행이 아닌 **모든** 후보에서 발동한다(5차 실측 34장: 후보 1,207건 중 1,033건, 대부분 이동 뒤에도 기각). 기록의 `y`는 이동 후, `y_raw`는 이동 전 좌표이고 `snapped`가 이동 여부다. 실행 간 비교는 개별 기록이 아니라 `accepted` 집합으로 한다. 위쪽 여백 끝으로 옮긴 경계는 첫 글자 행에 놓이므로 `std[y]`가 항상 `blank_row_std`를 넘는다 — 균일 행 검사는 검토 대상 선별용이지 잘림 신호가 아니다.
- **고정 입력본**(다음 단계 개발에 쓰는 이전 단계 결과): 실험 출력 디렉터리를 덮어쓰거나 골라 바꾸지 않는다. 실험 출력은 보고서가 참조하므로 그대로 두고, 선택한 결과를 별도 폴더(`v1`, `v2` …)에 `freeze-input`으로 모은다(4절). 실행마다 어느 출력에서 왔는지(실행 디렉터리 · 여러 번 돌렸으면 어느 회차 · 선택 기준 · 실행 커밋)를 `--meta`로 `provenance.json`에 남긴다. `freeze-input`의 검증: 모든 `image_path`가 입력본 폴더 **내부**를 가리킴 · 이미지 SHA-256이 원본과 같음 · 이미지 크기 = `width`·`height` · 섹션 연속성(`top_offset` 순으로 첫 시작 0, 앞 끝 = 다음 시작, 마지막 끝 = `source_height`, 폭 = `source_width`) · 레포 밖으로 통째 복사한 사본에서 같은 검증(옮긴 폴더 기준으로 경로가 내부인지 다시 확인) · 원본 전후 해시 불변. 고정 입력본의 선택은 이전 단계 품질의 근거로 쓰지 않는다. 다음 단계의 출력은 입력본 폴더가 아닌 별도 위치에 쓴다.

- `expected/`는 지금 **형식 예시**다. 단계가 구현되면 같은 입력의 실행 결과와 비교하는 회귀 테스트로 승격한다. `expected/ocr/`는 합성 좌표로 만든 형식 예시이며 **OCR 정답이 아니다**(합성 영문 · 고정 score 0.97). 실제 표본의 OCR 정답(줄 텍스트·bbox)과 채점 명세는 PoC 레포에 둔다(`README.md` 4.1). DejaVu가 없는 PC에서 `make_synthetic`으로 `expected/`를 다시 만들면 글자 bbox가 바뀌므로, 형식 변경은 `convert-split`처럼 해당 필드만 바꾼다.
- **① 섹션 정답 기준(실측 판정용, 2026-09-22)** — 정본 계약이 아니라 표본을 채점할 때 쓰는 기준이다. 입도는 소제목 단위[`pipeline.md` 단계표 ①].
  - 독립된 설명 주제(고유한 소제목과 그에 딸린 본문·그림)는 분리한다.
  - 목록은 형식이 아니라 내용의 위계로 판단한다. POINT n · 카드 · 후기·Q&A·FAQ 항목이라도 상위 주제에 딸린 설명이면 합치고, 각각이 독립 주제면 분리한다.
  - 그림·표·각주·인증서는 그 내용을 설명하는 주제에 붙인다(제목이 그림 뒤에 오는 배치도 있다).
  - 제목만 든 색 띠는 그 아래 본문과 같은 섹션이다. 현재 구현이 못 지키는 알려진 한계(#26).
  - 여백뿐인 띠는 앞 구간에 붙인다. 맨 앞의 여백은 다음 구간에 붙인다.
  - 두 색 구간 사이의 짧은 브랜드·로고 띠는 앞 주제의 꼬리로 본다. 띠 안에 다음 주제의 제목이 있으면 다음 주제의 머리다.
  - 채점: 정답 경계마다 허용 오차 안의 실행 경계를 일대일로 짝지어 정답 검출 · 누락 · 오절단 · 알려진 한계 절단을 센다. 오절단과 한계 절단은 둘 다 실제 잘못된 절단이므로 전체 품질에서는 합산해 보인다. **하나의 의미 경계에 유효한 절단 위치가 여럿이면**(예: 사진 끝의 색 경계와 그 아래 여백 중앙의 VLM 경계) 정답에 대체 위치로 기록하고 일대일로 채점한다 — 둘 중 하나를 잡으면 정답 1, 둘 다 잡아도 정답 1이며 중복 절단은 따로 평가한다. 허용 오차를 무조건 넓히지 않는다(중간의 부적절한 절단까지 정답이 된다). 채점 기준 변경은 비교하는 모든 실행에 같이 적용한다.
  - 표본별 정답 경계 목록과 판정 근거는 `README.md` 4.1의 실험 기록 폴더에 둔다.
- 실제 표본으로 한 실험의 결과·판단은 `README.md` 4.1의 실험 기록 폴더에 기록한다.

## 6. 환경·의존성

| 환경 | 설치 | 되는 것 | 안 되는 것 |
|---|---|---|---|
| 로컬 기본 | `pip install -r pipeline/requirements.txt` | 타입 · CLI · `inspect` · 테스트 · 샘플 재생성 · ① 섹션 분해(numpy · google-genai, VLM은 API 키 필요) | 로컬 모델 단계 실행 |
| 로컬 OCR (CPU) | `pip install -r pipeline/requirements-ocr.txt` | ② (+ ③ 휴리스틱) | ⑥ |
| GPU 서버 | `gpu/` 이미지 + `pip install -r pipeline/requirements-gpu.txt` | ⑥ 실측 · 전체 | — |

- **로컬 테스트와 GPU 실측을 구분한다.** 형식·배선·휴리스틱은 로컬에서 끝내고, GPU 서버는 ⑥ 인페인팅과 전체 통과 실측에만 쓴다. GPU 서버에서는 코드와 모델 캐시를 `/data` 아래에 둔다(`gpu/README.md`).
- `requirements-ocr.txt`는 2026-09-23 검증 버전으로 고정했다: `paddlepaddle==3.3.1` · `paddleocr==3.7.0` · `paddlex==3.7.2`(Windows 11 · Python 3.12.10 · CPU). 버전을 바꾸면 ② 영역 보존(`pipeline.md` 7절)의 코드 근거를 다시 확인한다. `-gpu.txt`는 아직 **설치 미검증**이다.
- **Windows 로컬 OCR 설치**: paddle 패키지의 파일 경로가 길어 Windows 경로 길이 한도(260자)에 걸릴 수 있다(2026-09-23, 긴 임시 폴더 경로의 가상환경에서 `OSError [Errno 2]`). 짧은 가상환경 경로를 권장한다. 시스템의 Long Path 설정은 이 문서가 요구하지 않는다.
- **Windows CPU 추론의 oneDNN 우회**: 위 버전의 Windows CPU에서 `predict` 시 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support … onednn_instruction.cc`가 났고 `enable_mkldnn=False`로 동작했다(2026-09-23). 원인은 paddle 3.3.1 oneDNN 실행기 문제로 **추정**한다. ② 엔진은 **Windows에서만** oneDNN을 끄고 Linux(워커·GPU 서버)에는 일괄 적용하지 않는다. 실제 적용값은 `run.json`에 남긴다. 알고리즘 파라미터가 아니라 실행 환경 설정이므로 config 키로 두지 않는다.
- LLM·VLM 호출 단계(① 경계 선택 · ③ `llm_assist` · ③-1 · ④ · ⑧)는 API 키를 환경변수 **`GEMINI_API_KEY`**로 받는다(2026-09-21, ① 착수 시 결정). 이름은 BE·배포 담당에게 전달하고, 워커에 키를 주입하는 작업은 배포 담당과 맞춘다. 키는 커밋하지 않는다.
- Python 3.11 이상(`tomllib`). BE와 GPU 이미지는 3.12.
- `pipeline/requirements*.txt`의 주석은 ASCII로 유지한다. 일부 pip 버전은 이 파일을 로케일 인코딩(한국어 Windows는 cp949)으로 읽어 한글 주석에서 `UnicodeDecodeError`가 난다. 그래도 문제가 나면 `PYTHONUTF8=1`을 켜고 설치한다.
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
