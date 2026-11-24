# PM 기준 자료 검수 요청 준비

> 검수 전 초안 · PM 회신 0건 · 기준 자료 미승인·미동결 · #93은 검수 의견 수집 후 병합 없이 종료 예정

2026-10-28 사용자가 **v1.0.1, GS-01~03 34장, H01~H04 측정 범위의 PM 승인 완료**를 확인했다. H05는 별도 서비스 평가다. 이는 아래 324개 기준 단위의 승인이나 실행 승인이 아니다. PM이 원본당 시간 제한 없이 필요한 확인·수정을 완료할 때까지 검수한다. 실측 이후의 A등급 결과 표본 검수는 이번 요청에 포함하지 않는다.

## 검토 방법과 우선순위

1. 아래 우선 검토 21장의 불확실성 ID를 먼저 확인한다. 핵심 수치·단위·부호/시험 조건, 작은 각주·전사 누락, 미백/각질층 착색/커버 톤업, 포함·제외·대체·필수 경고 기대가 우선이다. 상품별 주요 쟁점은 [기존 요약](README.md#pm-우선-확인-사항)을 사용한다.
2. 같은 원본의 나머지 기준 단위와 기본 확인 13장까지 모두 확인한다. 총 **34장·324개 단위**다. K01~09는 기준 영역의 내용·좌표 확인이며 생성 결과의 평가가 아니다.
3. 원본 링크와 기존 기대값을 함께 열어 대조하고 각 단위 ID를 사용해 이 Draft PR의 Conversation 코멘트 또는 Files changed의 해당 행 코멘트에 기록한다. 파일 편집이나 clone은 필요 없다. 아래 표는 작업 순서·바로가기이며 새로운 정답 목록이 아니다. 단위 유형/해시/파일 경로는 참고 정보로 별도 승인란을 요구하지 않는다.

## 우선 검토 21장

| 원본·이미지 | 전체 검수 목록 | 먼저 확인할 단위 |
|---|---|---|
| [GS-01_001](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-01/GS-01_001.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-01/GS-01_001.jpg) | [9개 단위](review-list.md#gs-01_001) | [GS-01_001-REF-008](review-list.md#gs-01_001-ref-008) |
| [GS-01_002](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-01/GS-01_002.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-01/GS-01_002.jpg) | [42개 단위](review-list.md#gs-01_002) | [GS-01_002-REF-015](review-list.md#gs-01_002-ref-015), [GS-01_002-REF-031](review-list.md#gs-01_002-ref-031), [GS-01_002-REF-037](review-list.md#gs-01_002-ref-037) |
| [GS-02_001](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_001.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_001.jpg) | [7개 단위](review-list.md#gs-02_001) | [GS-02_001-REF-004](review-list.md#gs-02_001-ref-004), [GS-02_001-REF-005](review-list.md#gs-02_001-ref-005) |
| [GS-02_002](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_002.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_002.jpg) | [7개 단위](review-list.md#gs-02_002) | [GS-02_002-REF-005](review-list.md#gs-02_002-ref-005) |
| [GS-02_005](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_005.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_005.jpg) | [4개 단위](review-list.md#gs-02_005) | [GS-02_005-REF-003](review-list.md#gs-02_005-ref-003) |
| [GS-02_007](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_007.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_007.jpg) | [17개 단위](review-list.md#gs-02_007) | [GS-02_007-REF-015](review-list.md#gs-02_007-ref-015) |
| [GS-02_008](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_008.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_008.jpg) | [13개 단위](review-list.md#gs-02_008) | [GS-02_008-REF-005](review-list.md#gs-02_008-ref-005), [GS-02_008-REF-012](review-list.md#gs-02_008-ref-012) |
| [GS-02_009](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_009.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_009.jpg) | [21개 단위](review-list.md#gs-02_009) | [GS-02_009-REF-020](review-list.md#gs-02_009-ref-020) |
| [GS-02_012](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_012.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_012.jpg) | [7개 단위](review-list.md#gs-02_012) | [GS-02_012-REF-006](review-list.md#gs-02_012-ref-006) |
| [GS-02_013](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_013.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_013.jpg) | [7개 단위](review-list.md#gs-02_013) | [GS-02_013-REF-006](review-list.md#gs-02_013-ref-006) |
| [GS-02_014](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_014.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_014.jpg) | [30개 단위](review-list.md#gs-02_014) | [GS-02_014-REF-011](review-list.md#gs-02_014-ref-011) |
| [GS-03_004](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_004.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_004.jpg) | [9개 단위](review-list.md#gs-03_004) | [GS-03_004-REF-007](review-list.md#gs-03_004-ref-007) |
| [GS-03_006](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_006.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_006.jpg) | [5개 단위](review-list.md#gs-03_006) | [GS-03_006-REF-004](review-list.md#gs-03_006-ref-004) |
| [GS-03_008](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_008.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_008.jpg) | [6개 단위](review-list.md#gs-03_008) | [GS-03_008-REF-005](review-list.md#gs-03_008-ref-005) |
| [GS-03_009](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_009.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_009.jpg) | [3개 단위](review-list.md#gs-03_009) | [GS-03_009-REF-002](review-list.md#gs-03_009-ref-002) |
| [GS-03_010](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_010.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_010.jpg) | [10개 단위](review-list.md#gs-03_010) | [GS-03_010-REF-009](review-list.md#gs-03_010-ref-009) |
| [GS-03_012](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_012.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_012.jpg) | [7개 단위](review-list.md#gs-03_012) | [GS-03_012-REF-006](review-list.md#gs-03_012-ref-006) |
| [GS-03_013](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_013.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_013.jpg) | [7개 단위](review-list.md#gs-03_013) | [GS-03_013-REF-006](review-list.md#gs-03_013-ref-006) |
| [GS-03_014](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_014.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_014.jpg) | [8개 단위](review-list.md#gs-03_014) | [GS-03_014-REF-007](review-list.md#gs-03_014-ref-007) |
| [GS-03_016](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_016.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_016.jpg) | [3개 단위](review-list.md#gs-03_016) | [GS-03_016-REF-001](review-list.md#gs-03_016-ref-001) |
| [GS-03_018](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_018.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_018.jpg) | [29개 단위](review-list.md#gs-03_018) | [GS-03_018-REF-024](review-list.md#gs-03_018-ref-024) |

## 기본 확인 13장

불확실성 표지가 없어도 PM 확인이 끝난 자료가 아니다. 아래 원본의 모든 기준 단위를 확인한다.

| 원본·이미지 | 전체 검수 목록 | 먼저 확인할 단위 |
|---|---|---|
| [GS-02_003](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_003.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_003.jpg) | [4개 단위](review-list.md#gs-02_003) | 전체 항목 기본 확인 |
| [GS-02_004](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_004.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_004.jpg) | [5개 단위](review-list.md#gs-02_004) | 전체 항목 기본 확인 |
| [GS-02_006](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_006.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_006.jpg) | [5개 단위](review-list.md#gs-02_006) | 전체 항목 기본 확인 |
| [GS-02_010](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_010.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_010.jpg) | [6개 단위](review-list.md#gs-02_010) | 전체 항목 기본 확인 |
| [GS-02_011](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_011.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-02/GS-02_011.jpg) | [7개 단위](review-list.md#gs-02_011) | 전체 항목 기본 확인 |
| [GS-03_001](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_001.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_001.jpg) | [5개 단위](review-list.md#gs-03_001) | 전체 항목 기본 확인 |
| [GS-03_002](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_002.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_002.jpg) | [17개 단위](review-list.md#gs-03_002) | 전체 항목 기본 확인 |
| [GS-03_003](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_003.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_003.jpg) | [2개 단위](review-list.md#gs-03_003) | 전체 항목 기본 확인 |
| [GS-03_005](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_005.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_005.jpg) | [6개 단위](review-list.md#gs-03_005) | 전체 항목 기본 확인 |
| [GS-03_007](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_007.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_007.jpg) | [2개 단위](review-list.md#gs-03_007) | 전체 항목 기본 확인 |
| [GS-03_011](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_011.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_011.jpg) | [4개 단위](review-list.md#gs-03_011) | 전체 항목 기본 확인 |
| [GS-03_015](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_015.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_015.jpg) | [6개 단위](review-list.md#gs-03_015) | 전체 항목 기본 확인 |
| [GS-03_017](https://github.com/ai-pixlate/server/blob/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_017.jpg) · [원본 크기](https://raw.githubusercontent.com/ai-pixlate/server/dc038bdfa937b3d9762e2af0a8a97929e62f5ff9/pipeline/samples/evaluation/gs-v1/GS-03/GS-03_017.jpg) | [4개 단위](review-list.md#gs-03_017) | 전체 항목 기본 확인 |

## 회신 및 완료 관리

- **승인:** 해당 ID의 기대값·위치·문맥을 확인했다는 뜻이다. 중요도와 H04 두 체크포인트 적용 여부, 정책의 원본 전체 적용 여부가 미정이면 이를 함께 명시해야 동결 준비가 끝난다. 표시된 정책 기대의 승인이 실제 AI 선택을 정답으로 만든다는 뜻은 아니다.
- **수정 필요:** 해당 ID, 수정할 값/좌표/적용 여부, 원본 또는 별도 근거를 기입한다. 반영 후 PM 재확인 전에는 완료가 아니다.
- **판단 보류:** 이유·필요한 증빙을 기록한다. 빈칸과 구분하며 정상·승인·0건으로 자동 바꾸지 않는다.
- 원본별 완료는 모든 단위의 회신과 수정 후 재확인·잔여 보류 점검이 끝난 경우에만 표시한다. PM의 GitHub 코멘트 URL이 회신 근거이며, 반영자가 단위별 회신란에 연결한 뒤 목록에서 승인/수정 필요/판단 보류/미회신을 나눠 집계한다. 현재 324개 모두 미회신이다.
- 묶음 회신 예: `PM 이름 / 날짜 / 승인 ID 목록 / 예외 ID: 수정 필요 또는 판단 보류 + 내용·근거`. 포괄적인 “확인했습니다”만으로 모든 단위에 승인 이력을 만들지 않는다. 파일을 직접 편집하기 어려우면 ID 기준 메시지로 회신해도 된다. 실제 회신 원문을 보존한다.

## 회신 반영과 동결 절차

1. PM 회신 원문·작성자·날짜·대상 초안 버전/해시를 artifacts의 별도 근거로 보존한다. 기존 초안·초기 AI 판단을 덮어쓰지 않는다. 이번 작업에서는 회신이나 승인 기록을 만들지 않는다.
2. 단위 ID별 의견을 대조해 수정안에 반영한다. 이전값→수정값·수정 이유·근거·처리 상태를 연결한다. 기존 ID는 추적 가능하게 유지하고 추가 단위는 새 ID를 부여한다. JSONL과 기존 검토 목록·색인을 같은 변경 기준으로 맞춘다.
3. PM이 수정 반영분을 재확인한다. 미회신/수정 필요/보류와 검수 완료를 분리한다. 근거 부족이 남으면 정상 정답으로 채우지 않는다. PM이 근거 부족 자체를 확인하고 기존 undetermined 등 예외 규칙으로 처리하도록 명시한 항목은 미회신과 구분해 기록하되 정상 측정으로 바꾸지 않는다. 도구의 필수 적용 여부를 확정할 수 없는 항목은 별도 결정 전까지 동결 대기로 남긴다. 범위 축소·보류 단위 제외를 임의로 확정하지 않는다.
4. **동결 완료 조건:** 34장 모든 단위의 검토 이력 확인, 수정안 재확인, 중요도·단위 중복·좌표·정책 기대·H01 원본 적용 여부·H04 양 체크포인트 적용 여부 확정, 동결 대상에 미해결 기대값이 없음, 원본/근거 해시·링크 검증, PM의 동결 범위·버전 확인. 미해결 항목 때문에 이 조건을 충족하지 못하면 동결 대기한다. 원래 범위 조정이 필요하면 별도 결정을 받는다.
5. 이후 별도 동결본을 만들고 verified_by/verified_at/verification_ref에 **실제** PM 확인 근거를 연결한다. [기존 반영 절차](README.md#초안-해석과-도구-연결)와 [초안 필드·반영 안내](README.md#초안-해석과-도구-연결)를 따라 실제 회차 ID·기준 버전·근거 경로를 결합하고 기존 도구 검증을 수행한다. 단순히 초안의 null을 채우거나 파일명을 바꿔 승격하지 않는다.

동결 이후에도 비용·실행 승인은 별도다. 이번 요청은 PM 대신 승인·동결하거나 실제 모델을 호출하는 작업이 아니다.

## GitHub 웹 회신 방법

1. 위 표에서 원본·이미지를 열고 다른 탭에서 검토표를 연다. 작은 글씨는 `원본 크기` 링크를 열어 확대한다. 로컬 clone과 이미지 다운로드는 필수가 아니다.
2. 이 Draft PR의 **Conversation → 코멘트** 또는 **Files changed → 해당 항목 행의 코멘트**에 아래 형식으로 회신한다. 범위가 명시된 ID 묶음 회신도 가능하다.

```text
항목 ID: GS-01_001-REF-002
판정: 승인 / 수정 필요 / 판단 보류 중 선택
수정 내용·원본 좌표·근거 또는 보류 이유:
PM 이름 / 회신일:
```

위 내용은 회신 형식 예시이며 실제 승인 기록이 아니다. GitHub 로그인 후 코멘트할 수 있다. 파일의 빈 회신란은 반영 담당자가 실제 코멘트 URL과 함께 갱신한다. PM이 파일을 직접 편집할 필요는 없다.

## 요청 메시지 초안

> @i-dAb-i 기준 자료 34장·324개 단위의 PM 검수를 부탁드립니다. 검수 안내의 우선 21장을 먼저 확인한 뒤 나머지 13장도 확인해 주세요. 원본/원본 크기 링크와 검토표를 웹에서 대조하고, 이 PR 코멘트에 항목 ID와 승인·수정 필요·판단 보류, 수정값·근거 또는 보류 이유를 남겨 주세요. 원본당 시간 제한은 없습니다. A등급 결과 표본 검수는 실측 후 별도입니다. 현재 모두 검수 전 초안이며 PM 검수 완료 전 병합하지 않습니다. 비용·실행 승인은 보류입니다.


## PR #94 리뷰 후 관리 결정

#93은 PM 검수 의견을 받은 뒤 **병합하지 않고 닫는다**. 지금 닫거나 자료를 동결하지 않는다. 이 폴더 7개는 #94에서도 검수 전 초안이다. PM 검수 완료 후 동결본은 `docs/ai-baseline/references/gs-v1/`의 별도 파일과 새 PR로 관리한다. 공식 회차는 승인된 동결본을 참조·복사하고 해시를 기록한다. [현재 비용·생성/채점 결정](../../cost-estimate.md#pr-제출-시-승인-상태)을 따르며 동결 전 이미지 생성은 허용하고 H01~H04 공식 채점은 동결 후 진행한다. 기존 초안의 미확인 값이나 검수란은 승인으로 채우지 않는다.
