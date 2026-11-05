# OCR v4 텍스트 정답 동결·채점 도구

정본은 [v4](../../docs/ai-baseline/protocols/v1/ocr-v4.md). 제품·OCR 엔진을 import하거나 실행하지 않는다. 아래 파일은 평가 도구 전용 형식이며 제품 API 계약이 아니다.

## 텍스트 동결

`python -m tools.baseline.ocr freeze --source-dir <기존 JSON 폴더> --image-root <이미지 상대 경로 기준> --spec <v4.md> --approval <승인.json> --version <버전> --out <새 폴더>`

승인 JSON 필수 필드: `reviewer`, `evidence`(사용자 발언과 대화 출처), `transcription_review_complete: true`, `errors_found: 0`, `order: "lines_array"`, `empty_truth_policy: "keep"`, `freeze_authorized: true`, `expected_sections`, `expected_lines`. `output_exposure`는 미확인 상태를 그대로 기록할 수 있다. 입력 폴더의 모든 JSON을 읽고 승인된 개수와 비교한다. bbox·겹침·v3 verified는 검사하지 않는다. 이미지 해시, text 문자열, ID 중복, 순서 보존은 검사한다. 경로는 이미지 루트 안에 있어야 한다.

새 폴더에 `text_units.jsonl`(섹션당 한 행), `approval.json`, `spec-v4.txt`(명세 원문 바이트), `README.md`, `manifest.json`을 기록한다. 기존 폴더는 덮어쓰지 않는다. manifest는 나머지 파일 해시·원본 JSON 해시·이미지 해시·동결 시각·이력을 기록한다. 미커밋 명세는 커밋 확정본으로 가장하지 않고 스냅샷 SHA-256을 기준으로 식별한다.

`python -m tools.baseline.ocr verify --truth <동결 폴더>`는 동결본 해시·ID·순서·개수를 검증한다. 동결본 자체의 일관성 검사이며 승인자의 신원을 외부 인증하는 기능은 아니다.

## 채점 입력

`python -m tools.baseline.ocr score --truth <동결 폴더> --inputs <입력 색인.json> --out <새 결과.json>`

**이 작업에서는 실제 v1.0.1 입력으로 score를 실행하지 않는다.** 합성 테스트만 실행한다.

입력 색인: `{"sections": [...]}`. 각 행의 `id`는 동결 섹션 ID이며 중복·알 수 없는 ID는 전체 실행 오류다. 누락 섹션은 undetermined로 남기고 계획 분모에서 숨기지 않는다. 경로는 입력 색인 부모 기준(절대 경로도 가능)이다.

각 정상 출력 행의 필수 필드:

- `status: "available"`, `id`, `input_sha256`(해당 실행에 사용한 이미지의 해시), `product_version`, `product_sha`(40자리), `attempt_id`.
- `model`, `config_sha256`, `environment_sha256`: 당시 모델 식별자와 보존된 설정·환경 파일 해시. 비밀정보는 넣지 않는다.
- `boundary: "pipeline.stages.ocr.run:return"`, `evidence: {path, sha256, locator}`: 해당 실행·입력 해시·순서·보정 후 반환 경계를 입증하는 보존 기록 및 구체적 위치 설명. 단순히 경계 이름을 쓰는 것만으로 실증된 것으로 보지 않는다. 담당자가 경계 근거를 먼저 확인해야 한다.
- `output: {path, sha256, pointer, line?}`: 원시 출력 파일의 전체 바이트 해시. JSON 파일은 `pointer`(JSON Pointer; 전체 객체이면 빈 문자열)로 OcrResult를 선택한다. JSONL은 1부터 시작하는 `line`을 추가한다. 선택 객체의 `regions`는 배열, 각 원소 `text`는 문자열이어야 한다. bbox·score는 무시한다. 원시 파일을 재작성할 필요가 없다.
- `status: "no_output"` 또는 `"undetermined"`인 행은 `reason`이 필수다. 확보된 제품·시도·해시 정보도 보존하며 누락 정보는 만들어 넣지 않는다.

출력 경계·입력/출력 해시 불일치·근거 누락은 undetermined, 기대 파일 없음은 no_output, 파싱·형식 실패는 evaluation_failed. 정상 빈 regions는 삭제 오류로 계산한다. 입력 색인 파싱·동결본 무결성 실패는 결과를 만들지 않고 중단한다.

## 결과와 재사용

결과 JSON은 섹션별 상태·사유·S/D/I/N·출력 문자 수·CER 비율/백분율과 원본 입력 행, 실제 읽은 출력/근거 해시를 포함한다. 전체는 measured 섹션만 문자 가중 합산하며 상태별 개수와 유효 섹션 ID를 기록한다. N=0 섹션은 not_applicable이고 출력 문자 수는 남긴다. measured 0건은 미산정이다. L008 빈 정답 한계를 결과에 기록한다.

정답 manifest·입력 색인·채점기 코드·명세 해시, Python/Unicode 버전을 남긴다. 출력은 배타 생성하여 덮어쓰지 않는다. 같은 동결 폴더와 채점기로 다음 제품 버전의 새 입력 색인을 평가한다. 기존 결과를 수정하지 않는다. 버전별 유효 섹션이 다르면 전체 수치만으로 직접 비교하지 않는다.

테스트: `python -m pytest tests/test_baseline_ocr.py -q`. 합성 이미지·정답·출력만 사용한다.

## 채점 없는 입력 검증·후속 회차 색인 생성

`python -m tools.baseline.ocr validate-inputs --truth <동결폴더> --inputs <색인.json> --out <새검증.json>`

정규화·편집거리·CER 집계를 호출하지 않는다. 출력 형식과 출처 해시만 검사하며 성공 상태 `ready`는 측정 완료가 아니다. 색인에 `output_section_key`가 있으면 선택한 OcrResult의 section_key도 일치해야 한다.

`python -m tools.baseline.ocr_prepare --truth <동결폴더> --run <회차폴더> --legacy <고정입력-v1폴더> --reproduction <회차의 reproduction폴더> --boundary <경계검토.json> --out <새준비폴더>`

경계검토 JSON은 `product_sha`, `boundary`, `wrapper_sha256`, `code_sources`(제품 커밋·경로·바이트 해시), `findings`, `limits`를 기록한다. 새로운 제품 버전은 실제 코드를 먼저 대조해 경계검토를 작성한다. 과거 경계 검토를 다른 SHA에 재사용하지 않는다.

생성기는 회차 github-file-index의 보존 해시, runtime의 계측 코드·설정 해시를 확인한다. 분할 이벤트 문맥→원본 DB ID/파일명→고정 섹션 위치/크기→DB image_key→객체 바이트 해시를 연결한다. 여기서 위치는 **입력 이미지 연결 근거**이며 정답 bbox 평가나 문자 매칭이 아니다. 같은 이름이나 좌표만으로 이미지 동일성을 확정하지 않는다. 복수 시도는 자동 선택하지 않는다.

`inputs.json`은 채점기 입력, `linkage.json`은 연결 증거·해시·후보/누락 사유, `validation.json`은 채점 없는 검증 결과다. 입력/출력 참조는 준비 폴더 기준 상대 경로다. 로컬 고정 입력의 split 경로는 출처 이력으로 기록하며, 재생성에는 원래 입력 이미지·split 자료가 필요하다. 현재 보존 형식(stages.jsonl·DB 증거·객체/파일 색인)이 없는 회차는 추측하지 않고 중단한다. 기존 폴더를 덮어쓰지 않는다.
