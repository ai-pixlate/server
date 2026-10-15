# 2026-10-15 ⑦ 스타일 추출 — 글자 크기 환산 비율(em_ratio) 실측

## 1. 질문

release/1.0.0 배포 서버 렌더에서 번역문 글자가 원본보다 지나치게 컸다. 조판(`app/typeset.py`)은 ⑦ `est_font_px`를 그대로 Pillow 글꼴
크기(em)로 쓰므로, ⑦의 크기 환산 `est_font_px = 영역 bbox 높이 × style.em_ratio(1.35)`가 원본 글자의 em을 바르게 추정하는지 확인한다.
관련: `pipeline.md` 단계표 ⑦ · 3절 · 7.7절, `open-questions.md` #76.

## 2. 표본

- 입력본 `downstream-input-v1`(MANIFEST 대상 100섹션, 32원본). ⑦ 처리 대상 블록(④ `is_product_label=false` · ⑤ `is_brand_logo=false`)의
  비공백 OCR 영역 1,401개. 그중 한글만으로 된 영역 553개.
- 비교 렌더: `GS-01_002/sec_1_01`(배포 서버 문제 화면과 같은 섹션) · `GS-03_002/sec_1_01` · `GS-01_002/sec_1_08` · `GS-02_014/sec_1_11`.
  배경은 `inpaint-bg-v1_2026-09-30`(LaMa 실측 결과). 대표 표본이 아니다.
- 계기: 배포 서버(release/1.0.0) 렌더 화면 1건 — `GS-01_002/sec_1_01`과 같은 원본.

## 3. 조건

- 코드: `origin/release/1.0.0` `2ef963e`의 `pipeline/stages/style.py`(계산 함수 그대로 import) · `app/typeset.py`(운영 조판 그대로).
- 도구: `docs/ai-experiments/tools/style_em_ratio_probe.py`
  - `measure` — 영역 bbox crop → ⑦과 같은 명도식 · Otsu · 외곽 다수결로 글자 집단 → 글자 픽셀이 crop 폭의 1% 이상인 행의 처음~끝 = 잉크 높이.
  - `compare` — em_ratio 후보마다 `_block_style`로 크기를 다시 계산해 ① 원문(한글)을 Pretendard Regular로 영역 위치에 다시 그린 것,
    ② (번역문을 준 섹션만) 운영 조판 `app.typeset.render`로 번역문을 그린 것을 원본과 나란히 둔다.
- 비교: em_ratio 1.35(현재) · 1.0 · 0.9 · 0.8(배포 문제 섹션) → 1.35 · 0.9(4섹션). 정렬 허용 오차 0.12 고정.
- 번역문(`GS-01_002/sec_1_01`만): 배포 서버 렌더 화면에서 옮겨 적은 근사값. 4번 블록은 화면에서 잘려 추정했다.
- 산출물(git 제외): `docs/ai-experiments/runs/2026-10-15_07-style_em-ratio/`(`measure.json` · `compare_*.png` · `translations.json`).

## 4. 결과

판정 기준: (1) 수치 — 원본 한글 글자의 em 추정값과 현재 `est_font_px`의 차이. (2) 육안 — 후보 비율로 다시 그린 원문이 원본 글자 크기와 맞는지.

| 항목 | 값 |
|---|---|
| bbox 높이 / 잉크 높이 — 한글 영역(n=553) | p10 1.161 · p25 1.212 · **p50 1.273** · p75 1.360 · p90 1.464 |
| bbox 높이 / 잉크 높이 — 라틴 대문자 영역(n=49) | p50 1.263 |
| bbox 높이 / 잉크 높이 — 전체(n=1,401) | p50 1.273 |
| Pretendard 한글 잉크 높이 / em | Regular 0.87 · SemiBold 0.89 |
| Pretendard 라틴 대문자 잉크 높이 / em | 0.71 |
| → 원본 한글 em ≈ bbox 높이 × | 1 / (1.273 × 0.87~0.89) = **0.88~0.90** |
| 현재 1.35의 과대 배율 | 1.35 / 0.89 ≈ **1.5배** |

- OCR 검출 박스(PP-OCRv5 det)는 글자 잉크보다 위아래로 약 27% 크다. 1.35는 bbox를 em보다 작은 값(대문자 높이 정도)으로 보고 키우는 비율이라
  이미 em보다 큰 bbox에 한 번 더 곱해졌다.
- 배포 문제 섹션 타이틀 "진정": bbox 156px · 잉크 124px · 원본 em ≈ 141px인데 `est_font_px` = 156 × 1.35 = 210.6px. 배포 렌더 화면의
  "Intensive" 크기와 같다(1.35 비교 렌더가 배포 화면을 재현).
- 육안(4섹션, 에이전트 비교 렌더): 0.9로 다시 그린 원문은 원본 글자 크기와 거의 같고, 1.35는 모든 섹션에서 원본보다 크게 넘친다.
  사람 검수 전이다.
- 크기와 별개로 남는 문제(이번 범위 밖):
  - 번역문이 한글보다 길어 0.9에서도 줄이 늘어 겹친다(`GS-01_002/sec_1_01` 타이틀 2줄 → 4줄, 0.8에서도 4줄).
  - 크기가 다른 줄이 한 블록이면 블록 중앙값 하나로 그려진다(`GS-03_002/sec_1_01` 타이틀의 작은 첫 줄).
  - 굵기는 추출하지 않아 원본 굵은 글자가 Regular로 그려진다.

## 5. 판단

**채택(잠정값)** — 사용자 결정(2026-10-15): `style.em_ratio` 기본값을 0.9 잠정값으로 바꾼다. 계산식(bbox 높이 × em_ratio)은 바꾸지 않는다.
최종값 · 측정 방식(잉크 높이 기준 등) · 사람 육안 검수는 `open-questions.md` #85에서 추적한다. 1.35는 개발계획 6장 값이라 상위 문서 수정이 필요하다(3절).

## 6. 반영

2026-10-15 — `pipeline/config/default.toml` `[style] em_ratio` 1.35 → 0.9, `pipeline.md` 단계표 ⑦ · 3절 · 7.7절, `open-questions.md` #85 신규 ·
3절 E 행 · #76 참조, `tests/test_pipeline_style.py`(기본값 확인 0.9, 계산식 테스트는 1.35 명시 고정), `pipeline/stages/style.py` 머리 설명,
`pipeline/handoff/style_defaults.py` · `be-handoff.md`의 "1.35를 다시 곱하지 않는다" 문구를 em_ratio 일반 표현으로.
