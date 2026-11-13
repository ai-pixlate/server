# OCR v6 후보 채점기 구현·사전 검증

2026-11-09 · 구현/합성 테스트 완료 · 실제 CER 미산출 · 공식 기준선 전환 전

## 구현

- [ocr_v6.py](../../../../tools/baseline/ocr_v6.py): 기존 `ocr_scope.filter_section` → `v5_inputs` → `ocr_v5.score_lines` 연결. v4 정규화·S/D/I 역추적과 v5 A2 대응은 수정하지 않았다.
- [ocr_v6_preflight.py](../../../../tools/baseline/ocr_v6_preflight.py): 실제 입력 검증 전용 CLI. 채점 함수를 호출하면 실패하도록 방어하고, 기존 파일 덮어쓰기를 거부한다.
- [합성 테스트](../../../../tests/test_ocr_v6.py): 치환·삭제·삽입, 분리·병합·재정렬, 반복·빈 출력, 공통 범위, 차단, 집계, 버전 혼합 거부.

`prepare_section(scope, gt, output, links, metadata=..., ...)`는 필터와 상태만 계산한다. `score_section`은 동일 검증을 다시 수행한 뒤 준비된 경우에만 문자를 대응한다. 저장된 `ready` 플래그를 그대로 믿지 않는다. `aggregate(rows, expected_ids=...)`는 ID 누락·중복을 거부하고 섹션별/GS별/전체의 S/D/I/N 합계로 문자 가중 CER을 계산한다.

개발용 인터페이스이며 제품 API/DB 계약이 아니다. 입력은 기존 필터의 scope/GT/출력/연결 구조를 그대로 사용한다. metadata에는 `group`, `input_version`, `truth_version`, `product_version`, `product_sha`, `image_sha256`, `truth_sha256`, `output_sha256`, `policy_sha256`, `spec_sha256`가 필요하다. 원본/출력 바이트 해시 검증은 파일 어댑터에서 수행하며 순수 함수는 전달된 배열의 정규 JSON 해시도 남긴다. 실제 재현 기록은 파일 해시와 JSON 표현 해시를 혼동하지 않는다.

정책·범위·제품·입력 버전, GT/출력/연결/설정 해시, Unicode 버전, v4 정규화·v5 대응·범위 필터·v6 코드 해시를 기록한다. GS 그룹은 명시 입력하며 단순 행 평균을 사용하지 않는다. crop 도입 시 별도 `input_version`과 입력 바이트를 사용해야 하며 다른 입력·GT·정책·제품·구현을 한 집계에 섞지 못한다. 이번에 crop이나 새 출력을 생성하지 않았다.

## 차단·예외

| 조건 | 처리 |
|---|---|
| 연결 미완료·혼합 문자열·배경 겹침·범위 승인 대기 | `undetermined`, 점수 없음 |
| 대상 GT 완전성 또는 검수 근거 미확인 | `undetermined`, 점수 없음 |
| 승인된 전체 제외 | `not_applicable`, CER 없음, 감사 기록 유지 |
| 포함 범위의 정규화 GT 문자 0 | 무문자 확정 근거 부족으로 `undetermined`; 자동 비대상 처리 금지 |
| 정상 빈 OCR 배열/문자열 | 포함 GT 전부 삭제 오류로 계산 가능 |
| 사용 가능한 출력 없음 (`None`) | 다른 필수 조건 통과 시 `no_output`; 정상 빈 배열과 구분 |
| 해시·형식·연결 ID 오류 | `evaluation_failed` 또는 파일 사전 검증 실패로 중단 |

순수 함수의 `gt_complete=True`, `scope_approved=True`, `completeness_evidence`는 외부 검수 근거를 전달하는 값이다. 필터 통과로 자동 설정하지 않는다. 이번 실제 데이터 어댑터는 세 조건을 승인하지 않는다. 차단이 겹치면 범위 차단부터 표시하고 원본 실행·출처는 metadata에 유지한다.

집계는 `measured_subset_cer_percent`와 `overall_cer_percent`를 분리한다. 하나라도 차단·미측정이면 전체 CER은 null이며 미측정 GT 분모를 추정하지 않는다. 정상 측정 0건과 대상 없음은 구분한다. 후보 구현의 결과는 항상 `official=false`이며 공식 승인을 자동 생성하지 않는다. 실제 결과에는 점수 필드가 모두 null이다.

## 실행 방법

worktree 루트에서 실행한다. 검증된 로컬 Python을 사용한다. 아래 CLI에는 실제 채점 옵션이 없다. 출력 경로는 새 파일이어야 한다.

```powershell
python -m tools.baseline.ocr_v6_preflight `
  --package docs/ai-baseline/preparations/ocr-v6-readiness-20261109 `
  --truth docs/ai-baseline/references/ocr-text/ocr-text-v2-20261109/text_units.jsonl `
  --index docs/ai-baseline/evaluations/ocr-v4-v2-v1-0-1-20261109/inputs.json `
  --spec docs/ai-baseline/protocols/v1/ocr-v6-scope-draft.md `
  --image-root C:/Users/Yeram/Documents/pixlate-server `
  --output .cache/ocr-v6-preflight-new.json

python -m pytest tests/test_baseline_ocr.py tests/test_ocr_v5.py tests/test_ocr_v6.py tests/test_ocr_scope.py tests/test_ocr_scope_followup.py tests/test_ocr_scope_repair.py tests/test_ocr_scope_readiness.py -q --basetemp .cache/pytest-v6-new
```

2·3차에는 동일한 고정 입력·GT·정책/범위와 새 제품 출력 색인을 지정한다. 파일 어댑터는 기존의 JSON/JSONL 행·pointer 형식을 재사용한다. 현재 어댑터는 연결 검증 패키지의 원문 감사 기록과도 대조하므로, 새 버전은 새 출력의 연결 검증 패키지가 필요하다. 과거 출력용 감사 기록을 다른 버전에 그대로 승인 근거로 재사용하지 않는다. 실제 측정 CLI·승인 자동화는 이번 범위에 포함하지 않았다.

## 검증 결과

**144개 통과**: v4 기존 합성 회귀, v5 대응, v6 연결·집계와 공통 필터 회귀. 최초 확장 테스트는 시스템 임시 디렉터리 권한 문제로 19개 setup 오류가 발생했으며, 새 worktree 내 임시 경로를 지정한 재실행에서 전부 통과했다. 기존 테스트·제품 코드는 수정하지 않았다.

최종 실제 점검은 [preflight-final.json](preflight-final.json)이다. [preflight.json](preflight.json)은 어댑터 보완 전 점검 이력으로 보존한다. 최종 점검에서는 GT 상대 경로를 원본 루트에 결합하고 절대 경로도 실제 파일 해시로 확인했다.

| 항목 | 결과 |
|---|---:|
| ID·입력·GT·출력·연결 검증 | 112개 |
| 범위 필터 연결 완료 | 107개(대상 103·전체 제외 4) |
| 범위 연결 차단 | 5개 |
| v6 범위 최종 승인 확인 | 0개 |
| 새 포함 범위 GT 완전성 인증 | 0개 |
| 현재 근거로 채점 준비 완료 | 0개 |
| 실제 CER 계산 | 수행하지 않음 |

원본 이미지 두 경로·출력·출처 증거의 SHA-256, GT/출력 ID와 원문 감사 기록, 이전 필터 상태를 비교했다. 보호 파일 232개·원본 증거 159개 및 이전 패키지의 해시를 재검증했다(중복 포함, 합산 불가). v1/v2 동결본·v4 공식 결과·v5 후보 결과·기존 원시 증거는 변경하지 않았다. GT 완전성 미인증은 112개 전부에 오류가 있다는 의미가 아니라 **새 평가 범위에 대한 인증을 임의 부여하지 않았다는 뜻**이다.

## 남은 조건·명세 미결

1. **GS-01_002/sec_1_04:** 설문 배지 설명/로고의 세부 연결.
2. **GS-02_002/sec_1_01:** 원본에 없는 좌표형 GT 두 줄의 별도 제외 근거 또는 후속 GT 교정. 현재 동결본은 그대로 둔다.
3. **GS-01_002/sec_1_23 (`Good all goodal`)**, **GS-03_005/sec_1_01 (`3`)**: 안전한 부분 연결 증거나 별도 고정 crop 입력 방식 결정. crop은 새 실행·입력 버전이 필요하다.
4. **GS-03_014/sec_1_03 (`b3+`)**: 배경/전경 겹침은 사각 crop만으로 해결되지 않으므로 공통 처리 방식 결정 필요.
5. 포함 범위의 GT 완전성 확인·범위 승인 근거를 입력/GT/영역 해시에 결합하고 최종 버전을 고정해야 한다.

빈 GT만으로 무문자 이미지를 증명하는 별도 승인 형식은 아직 없다. 구현에서는 기존 `full_exclude` 정책 근거가 없는 0문자 GT를 보류한다. 공식화 전에 진짜 무문자와 불완전 GT를 구분하는 검수 근거를 확정해야 한다. v5 A2의 1~3줄 그룹 휴리스틱과 반복 문구 대응 한계는 그대로이며 공식 승인 완료로 표현하지 않는다. 이 두 항목을 새 계산 규칙으로 임의 해결하지 않았다.

다음 작업은 남은 연결/방법 결정 → 포함 GT 완전성 확인 → 범위·GT·명세 버전 승인 → 별도 측정 승인 순서다. H01~H05·OP01~OP07, 제품 코드, 기존 지표 정의를 변경하지 않았다. GT 수정·동결, OCR/API 호출·실제 CER·커밋·push·PR은 수행하지 않았다.
