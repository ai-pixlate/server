# GS-01 Pilot Evaluation 보존·참고 안내

2026-11-04 사용자 결정: GS-01 2장 평가는 평가 도구·절차 검증과 문제 발견을 위한 **사전 검증(Pilot Evaluation)**으로 분류한다. AI 최초 결과에 대한 PM 검수·공식 확정은 진행하지 않으며 기존 검수 요청은 철회한다. PM 승인 기준 자료 58개 항목의 동결은 유지한다. 향후 GS-01~03 전체 34장 공식 평가는 별도로 진행하며, 이번 결과를 공식 성능 지표나 버전 비교의 확정 기준으로 사용하지 않는다.

## 중복 없는 보존

[preservation-index.json](preservation-index.json)은 원래 로컬 묶음의 416개 파일(112,154,981바이트)마다 SHA-256, 크기, 복원 위치, 보존 원본 경로를 기록한다. 기존 develop 증거는 `9ffabdf013de7528aa94a5be0658d4d87c84e933`에 고정한다. `PR_HEAD`는 이 PR에서 새로 보존한 파일이다. 동일한 내용은 한 곳에만 저장했다.

최초 회차의 2,028개 파일은 수정하거나 새 경로에 복사해 업로드하지 않는다. 동결본의 근거 사본과 평가 결과의 이미지·로그 사본은 기존 파일을 참조한다. 원시 평가 JSONL/manifest/집계 결과는 바이트 그대로 보존했다. 보존용 `.gitattributes`는 줄바꿈 자동 변환으로 해시가 달라지는 것을 막는다.

색인이 빠진 단순 폴더 복사로는 전체 근거가 모이지 않는다. 웹에서는 README와 아래 비교 이미지를 확인하고, 로컬 검증 시 저장소 checkout에서 다음처럼 새 출력 폴더에 복원한다. 모델/API/채점 실행은 없다.

```powershell
python docs/ai-baseline/evaluations/baseline-v1-0-1-gs01-20261103/restore-evidence.py --repository . --output .cache/gs01-evidence-restored
```

출력 아래에 원래 저장소 상대 구조가 재현된다. 기존 출력 폴더가 있거나 근거 해시가 다르면 중단한다. [보존 검증 결과](publication-validation.json)를 참고한다.

## 참고용 확인 영역 — 검수 요청 철회

- K01: [SPF/PA 시험표](evidence/views/pair-04.png), K02: [Reef Safe](evidence/views/pair-16.png), K03: [로고](evidence/views/pair-24.png)·[18 Free](evidence/views/pair-25.png).
- critical 7개·major 20개 전부. 같은 29개 결함이 최초/최종 체크포인트에 각각 기록돼 있으며 중복 합산하지 않는다. minor 2개도 아래 참고용으로 공개한다.
- H04 비보존 GS-01_002 REF-001·002·007·020·025: 제품명·용량·95% 항목·15.92%·6.78%의 정확성과 가독성의 미확정 경계 사례를 참고용으로 보존한다.
- 최초 원인 미확정: GS01-D14(한국어 잔존), D22(배지 배경 훼손), D29(N5 조건부 경고 누락). 원인 확정과 관찰된 결함 존재를 구분한다.
- 실제 채팅 모델 표시명·설정·전체 대화 내보내기 증빙 미확보. 미국 시험/인증 적합성을 확인한 것이 아니다.
- GS-01_001은 정상 전체 제외, GS-01_002만 품질 측정 분모 1장이다. GS-02·03은 미동결·미채점이며 A등급 표본은 현재 대상이 없다.

기존 PM 평가 결과 검수 요청은 철회했다. 아래 목록은 문제 발견과 향후 절차 개선을 위한 참고 자료이며 PM 회신·검수·공식 확정을 요구하지 않는다. 사람 검수 행 0건과 AI 최초 판단을 그대로 보존한다. PR #93의 기준 자료 승인 이력은 변경하지 않는다.

## AI 최초 결함 목록 — Pilot 참고용

| ID | 심각도 | 최초 원인 단계 | AI 관찰 | 근거 |
|---|---|---|---|---|
| GS01-D01 | critical | 렌더·배치 | 과대 제목이 제품명 줄과 겹치고 산뜻 밀착 번역은 오른쪽 밖으로 잘리며 인증 마크 위로 침범한다. | [비교 이미지](evidence/views/pair-00.png) |
| GS01-D02 | critical | OCR | 원본 50ml가 블록 41 source_lines/source_ko 및 trans_1에서 50m으로 누락되고 이미지에도 50m으로 표시된다. | [비교 이미지](evidence/views/pair-00.png) |
| GS01-D03 | major | 렌더·배치 | 제품명 제목이 설명 본문을 덮는다. | [비교 이미지](evidence/views/pair-01.png) |
| GS01-D04 | major | 렌더·배치 | No White Cast 해시태그가 아래 시험 각주와 겹친다. | [비교 이미지](evidence/views/pair-02.png) |
| GS01-D05 | critical | 렌더·배치 | 설문 제목이 배지 위에 겹치고 95%의 Refreshing/breathable/after use가 중첩돼 의미 연결의 가독성이 손상된다. 200명·98%·96%는 별도 본문에서 판독 가능하다. | [비교 이미지](evidence/views/pair-02.png) |
| GS01-D06 | major | 렌더·배치 | SPF/PA 표 머리글 Protection Factor가 시험값 행에 겹친다. 숫자·± 및 각주는 판독 가능하다. | [비교 이미지](evidence/views/pair-04.png) |
| GS01-D07 | major | 렌더·배치 | POINT 제목·본문이 서로 겹치며 마지막 ***** 각주의 성분 괄호 내용이 섹션 아래에서 잘린다. | [비교 이미지](evidence/views/pair-06.png) |
| GS01-D08 | major | 렌더·배치 | Certified Vegan 제목과 본문이 겹치고 animal-derived 다음 ingredients가 섹션 아래로 잘린다. | [비교 이미지](evidence/views/pair-07.png) |
| GS01-D09 | major | 렌더·배치 | 징크 제목과 설명, 원 안의 성분명·효과 설명이 중첩한다. | [비교 이미지](evidence/views/pair-07.png) |
| GS01-D10 | critical | 렌더·배치 | 긴 제목이 차트에 침범하고 15.92% 글자 일부가 흰 바탕으로 넘쳐 거의 구분되지 않는다. | [비교 이미지](evidence/views/pair-09.png) |
| GS01-D11 | minor | 렌더·배치 | 본문 마지막 skin이 아래 도해 영역까지 내려간다. 내용 판독은 가능하다. | [비교 이미지](evidence/views/pair-10.png) |
| GS01-D12 | major | 렌더·배치 | Daily Soothing Sun Care 제목이 설명 문장과 겹친다. | [비교 이미지](evidence/views/pair-11.png) |
| GS01-D13 | major | 렌더·배치 | 저자극 제목이 3개 테스트 배지를 덮고 배지별 시험 설명이 서로 겹친다. | [비교 이미지](evidence/views/pair-12.png) |
| GS01-D14 | minor | undetermined | 오른쪽 시험 배지의 인체적용 테스트 완료 한국어 일부가 남아 있다. | [비교 이미지](evidence/views/pair-12.png) |
| GS01-D15 | critical | 렌더·배치 | 사용 전/직후 차트 라벨이 서로 겹치고 6.78% 및 cooling effect의 밝은 글자가 흰 바탕으로 넘친다. 32.26 값과 after 1 use는 확대에서 판독 가능하다. | [비교 이미지](evidence/views/pair-14.png) |
| GS01-D16 | major | 렌더·배치 | 긴 제목이 제형 사진 안까지 침범하고 아래 설명이 원료적 특성 한정 각주와 겹친다. | [비교 이미지](evidence/views/pair-14.png) |
| GS01-D17 | major | 렌더·배치 | Reef Safe 제목의 Sunscreen이 본문 첫 줄과 겹친다. 불검출 시험 성분명과 각주는 판독 가능하다. | [비교 이미지](evidence/views/pair-16.png) |
| GS01-D18 | critical | 번역·현지화 | 블록 187 원문 자외선차단지수 검증 완료에 없는 verified broad spectrum SPF를 trans_1과 이미지에 추가했다. | [비교 이미지](evidence/views/pair-17.png) |
| GS01-D19 | major | 렌더·배치 | How to Use 제목이 얼굴·바디 안내 소제목과 겹친다. 상세 용법 문장은 온전히 읽힌다. | [비교 이미지](evidence/views/pair-18.png) |
| GS01-D20 | major | 렌더·배치 | 선케어 라인 제목이 영문 부제와 겹치며 제품 사진까지 침범한다. | [비교 이미지](evidence/views/pair-19.png) |
| GS01-D21 | major | 렌더·배치 | 3개 제품 설명·해시태그가 중첩하고 흰 글자가 흰 바탕으로 넘친다. | [비교 이미지](evidence/views/pair-20.png) |
| GS01-D22 | major | undetermined | 해시태그의 길쭉한 배경 배지가 불규칙한 작은 덩어리로 변형돼 남는다. 최초 원인은 중간 마스크까지 직접 대조하지 않아 미확정이다. | [비교 이미지](evidence/views/pair-21.png) |
| GS01-D23 | major | 렌더·배치 | 톤업/스틱 카드의 제목·제품명·해시태그가 중첩한다. DarkSpotCoverage는 치료 주장 없이 판독 가능하다. | [비교 이미지](evidence/views/pair-21.png) |
| GS01-D24 | major | 렌더·배치 | Sustainable Packaging/Small Steps for Nature 제목이 소개 문장과 겹친다. | [비교 이미지](evidence/views/pair-22.png) |
| GS01-D25 | major | 제품 라벨·브랜드 로고 판정 | FSC 및 SOYINK 마크 안 글자를 일반 글자로 재렌더해 원래 구성과 경계가 훼손된다. | [비교 이미지](evidence/views/pair-23.png) |
| GS01-D26 | critical | 제품 라벨·브랜드 로고 판정 | Good all goodal 보호 로고를 일반 텍스트로 다시 쓰면서 타원 윤곽 대부분이 지워지고 작은 고리만 남는다. 블록 309 is_brand_logo=false. | [비교 이미지](evidence/views/pair-24.png) |
| GS01-D27 | major | 렌더·배치 | 검증 설명 및 18 Free 제목이 각각 본문·각주와 겹친다. 18개/4분류 사실 자체는 판독 가능하다. | [비교 이미지](evidence/views/pair-24.png) |
| GS01-D28 | major | 렌더·배치 | 18 Free 목록에서 보존제 마지막 행과 가소제 제목, 계면활성제 각주와 자외선차단제 제목이 겹친다. | [비교 이미지](evidence/views/pair-25.png) |
| GS01-D29 | major | undetermined | 조건부 판정(section 4/6/17)은 DB에 존재하나 실제 N5 전달 JSON에는 translation_failed 2건과 폭 초과 신호만 있고 조건부 SPF 경고가 없다. 전달 경로의 최초 누락 위치는 미확정이다. | [비교 이미지](evidence/views/pair-04.png) |
