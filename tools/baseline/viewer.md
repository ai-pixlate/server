# Baseline 읽기 전용 결과 비교 뷰어

기존 [회차 자료](../../docs/ai-baseline/runs/README.md)를 정적 HTML로 묶는다. 상품·원본별 원본/최종 결과와 원본 섹션/`machine_snapshot`을 비교하며, 평가·수정·승인 기능은 없다. 모델·DB·웹 서버를 실행하지 않는다.

## 생성과 열람

저장소 루트에서 Python 3.10 이상과 Pillow가 설치된 환경을 사용한다. 기존 Baseline 격리 환경에도 필요한 라이브러리가 있다.

```powershell
python -B -m tools.baseline.viewer <회차_디렉터리> <새_출력_디렉터리>
```

최초 회차 예시:

```powershell
.cache/bl-venv-locked-20261028/Scripts/python.exe -B -m tools.baseline.viewer `
  docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 `
  .cache/baseline-viewers/first-run
```

출력의 `index.html`을 브라우저에서 연다. 상품·상태 필터와 원본 ID 검색, 섹션 선택, 폭 맞춤·100%·10~250% 확대, 독립 스크롤과 선택적인 비율 동기화를 제공한다. 비율 동기화는 원본 좌표의 일치를 의미하지 않는다. 여러 최종 산출물이 있으면 선택 목록으로 모두 보존한다.

출력은 입력과 겹치지 않는 **새 폴더**여야 한다. 기존 출력이나 공식 회차 파일을 덮어쓰지 않는다. `assets/`에 필요한 이미지를 바이트 그대로 복사하며 `index.html`, CSS, JS, `viewer-report.json`을 함께 보관한다. 출력 폴더 전체를 옮겨도 서버 없이 열람할 수 있다. 보고서에는 입력 파일의 확인 해시·로컬 위치·누락 안내가 있으므로 공유 전 로컬 경로의 공개 범위를 확인한다. 출력물은 원시 로그 전체를 대체하는 백업이 아니다.

## 후속 회차와 외부 보관

2차·3차 회차도 같은 명령의 입력과 출력 경로만 바꾼다. 상품명·원본 수·체크포인트 파일명은 manifest와 실행 증거에서 읽는다. GitHub Release 등으로 분리된 자료는 사용자가 내려받은 뒤 연결한다.

```powershell
python -B -m tools.baseline.viewer <회차_메타데이터_폴더> <새_출력_폴더> `
  --asset-root <복원된_산출물_루트> `
  --archive <다운로드된_ZIP> <기대_SHA256>
```

두 옵션은 필요한 것만 사용하며 여러 번 지정할 수 있다. 로컬 회차 → 추가 자료 루트 → ZIP 순서로 찾는다. 루트 아래의 파일은 manifest의 상대 경로를 유지해야 한다. ZIP은 상대 경로 또는 `<evaluation_run_id>/<상대 경로>`를 지원한다. ZIP 전체 해시를 먼저 확인하고 추출하지 않고 읽는다. 자동 다운로드·인증·Release 업로드·TAR는 지원하지 않는다. 파일 하나의 읽기 상한은 512 MiB다.

## 연결 근거와 상태

- `manifest.json`의 `case_inputs`, `source_keys`, `artifacts`와 `execution.jsonl`을 읽는다. 보존용 `github-file-index.json`이 있으면 해당 해시도 대조한다.
- 현재 회차의 DB 증거(`evidence/<case_id>.json` 또는 실행 기록이 참조한 JSON)에서 **같은 상품 + 원본 SHA-256**으로 원본을 연결하고, `source_image_id`와 섹션 `target_id`로 최초 미리보기를 연결한다. `state_ref`가 있으면 체크포인트의 원본 연결도 확인한다. 원본 이름·업로드 순서로 추측하지 않는다.
- 섹션 이미지의 `image_key`는 `object-index.json`과 `objects/<sha256>`로 연결한다. manifest에서 미리보기가 명시적인 부모 섹션 산출물을 참조하는 형식도 지원한다. 다른 증거 구조는 어댑터 추가가 필요할 수 있으며, 연결이 없는 원본 섹션을 임의로 잘라 만들지 않는다.
- 전체 제외는 모든 섹션의 명시적 제외 기록이 있을 때 표시한다. `no_output`만으로 전체 제외로 간주하지 않는다. 제외 적합성이나 품질 등급은 판단하지 않는다.
- 상품 실패가 여러 원본에 영향을 주면 상품 오류와 영향 원본 수를 표시한다. 원본별 독립 품질 결함 수로 계산하지 않는다. 파일 누락·연결 불명은 별도로 표시하고 원본을 결과 이미지로 대체하지 않는다.
- 기록된 파일 해시·이미지 크기 불일치, 상충하는 해시, 위험한 상대 경로는 생성을 중단한다. 존재하지 않는 파일은 누락 안내로 남긴다. 해시가 없는 증거는 관측 해시만 기록하므로 사전에 검증된 증거와 구분해야 한다.

## 검증

```powershell
python -B -m pytest tests/test_baseline_viewer.py -q -p no:cacheprovider
```

합성 자료로 서로 다른 상품·이미지 수, 부모/DB 연결, 다중 결과, 실패·제외·누락, 외부 ZIP·추가 루트, 무결성 오류, 출력 경로 보호, 입력 불변성, HTML 데이터 이스케이프를 확인한다. 실제 회차는 생성 보고서와 브라우저에서 이미지 열림·섹션 연결·확대·스크롤을 함께 확인한다. 테스트와 열람은 공식 품질 채점이 아니다.
