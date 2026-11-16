# 2026-11-16 ⑨ 조판 — 브라우저 넘침 측정 기반(자동 조정 전후 비교용)

## 1. 질문

자동 조정 실험에 앞서, 같은 입력의 조정 전후를 **합의된 브라우저 측정 기준**으로 잴 수 있는 로컬 실행 경로가 있는지 확인하고 없으면 만든다.
측정 결과에서 실제 넘침과 측정 오류를 구분할 수 있는지, 블록 영역 넘침과 섹션 · 최종 이미지 경계 잘림(Q-07)을 나눠 볼 수 있는지도 본다.
자동 조정 알고리즘 · 조정 한도 · 폰트 하한 · FE/BE 운영 연결은 범위 밖이다.
관련: `contract.md` 6.2(조판 D-01 · D-02) · 6.4(D-04 · D-05) · 6.6(Q-07) · 6.7 · `open-questions.md` #85 · #87 · #88 · #89.
앞선 조사: `2026-10-30_09-typeset_single-line-overflow.md`(Pillow 근사, 브라우저 기대값 아님).

## 2. 표본

- 같은 표본 — 베이스라인 회차 `baseline-v1-0-1-gs-v1-20261030-01`(제품 v1.0.1 `bfa3b70`)의 DB 행 보존본 `evidence/GS-01.json` · `GS-02.json`과
  `objects/`(develop에 보존 커밋됨). include 섹션 66개, 조판 대상 블록 **491개**(`_render_inputs` 조건). GS-03은 렌더가 없어 제외. 대표 표본이 아니다.
- 입력 오류(박스 범위 · style 형식) 0건. 건너뛴 것: include 아닌 섹션 6 · 조판 대상 아닌 블록 211 · 조판 블록이 없는 섹션 1.

## 3. 조건

기존 자산 확인(2026-11-16, develop `8ad34c3` · FE `ai-pixlate/client`):

| 대상 | 상태 | 이번 사용 |
|---|---|---|
| FE 공통 조판 모듈 `lib/n5/typeset-rules.ts` · `typeset-measurement.ts` · `typeset-final-output.ts` | FE `integration/second-round-20261106`에만 있다(FE develop · main 미병합). 합의 기준(D-01 · D-02 · D-04 · D-05 · Q-07 감지)을 구현한 측정 엔진 · 표시 CSS · 최종 출력 좌표 | **재사용** — 원본을 타입만 지워 그대로 호출 |
| FE harness `app/spike/typeset-measure` · e2e `n5-typeset-measurement.spec.ts` | Next dev 서버 전용 화면 · 고정 예시 입력 | 방법만 따름(DPR 2 · 픽셀 대조 0.5px) — npm 설치 · dev 서버는 쓰지 않았다 |
| 서버 렌더 `app/typeset.py` | Pillow `pillow-typeset/3`(PR #138, 넘침 = Pillow 잉크 근사, 1.1.0 범위 A). 서버 Playwright 공통 렌더는 **없다** | 참고값만(정답 아님) |
| 렌더 입력 구성 `preview_view.section_input` · `downstream._render_inputs` · `style_defaults.resolve_block` · `layout_adjust` | develop | **재사용** — 같은 함수로 보존본 행에서 `renderInput` 모양 입력을 만든다 |
| 이전 실험 도구 `typeset_overflow_probe.py` | Pillow 근사 · 보존본 해석 | `applied_layout`(보존본 적용 박스 해석)만 재사용 |

**측정 경로 구분: 최소 마크업 실험이다.** FE 공통 모듈의 코드(측정 · 판정 · 표시 CSS)는 서비스 코드 그대로지만, FE 앱 셸(Next · React ·
전역 CSS) · FE 웹폰트 로딩 · 서버 Playwright 촬영 경로가 아니다. 따라서 이 결과를 서비스(N5 미리보기 · N6 최종 렌더)와 같은 결과라고 보지 않는다.

- 도구: `docs/ai-experiments/tools/typeset_browser_measure.py`(+ `typeset_browser_measure/` 최소 페이지 · 실행기 · FE 모듈 변환). 측정 · 판정
  계산은 새로 만들지 않았다 — 실행기는 입력 변환 · 픽셀 대조 촬영 준비 · 확인용 섹션 합성만 한다. 분류 정의는 도구 머리말.
- 렌더 입력: 보존본 행에 현행 develop 구성 함수를 적용(style 8키 · 적용 박스 · 배경 = ⑥ 완료면 인페인팅 이미지). 운영 `/preview` 응답 원본이 아니다.
  style 자료 `role-style@2026-11-05.2`. 측정 크기는 ⑦ 측정값(`est_font_px`, v1.0.1 회차의 em_ratio 0.9 결과 — #85 잠정)이다.
- 폰트: 승인 설정 자료의 Pretendard v1.3.9 OTF(Regular 400 · SemiBold 600, SHA-256 검증) — 서버 Pillow와 같은 파일. FE 화면은 같은 릴리스의
  CDN 웹폰트를 쓰며 파일 동일성은 확인하지 않았다.
- 브라우저: Playwright 1.61.0 번들 Chromium 149.0.7827.55 headless, Windows 11. 판정 · 픽셀 대조 DPR 2, 보조 측정 · 섹션 합성 DPR 1,
  viewport 1400×1000. 네트워크 없이 가상 주소로 파일을 넘긴다(콘솔 오류는 `measure.json` meta).
- FE 커밋 `87aee91`(2026-11-16, `integration/second-round-20261106`) — 모듈 SHA-256은 `measure.json` meta.
- 산출물(git 제외): `docs/ai-experiments/runs/2026-11-16_09-typeset_browser-overflow-measure/final/`(`input.json` · `baseline/measure.json` ·
  `baseline/summary.json` · `baseline/review/index.html` · `baseline/sections/*.png` · `baseline/blocks/*.png` · `selfcheck/` ·
  전후 비교 시연 `derive-demo/`). 같은 폴더 바로 아래의 파일은 도구 수정 중의 첫 실행이다(결과 수치는 `final/`과 같다).

재현:

```bash
FE=<ai-pixlate/client 체크아웃, integration/second-round-20261106 87aee91>
OUT=docs/ai-experiments/runs/2026-11-16_09-typeset_browser-overflow-measure/final
python docs/ai-experiments/tools/typeset_browser_measure.py selfcheck --fe $FE --out $OUT/selfcheck
python docs/ai-experiments/tools/typeset_browser_measure.py build-input --run docs/ai-baseline/runs/baseline-v1-0-1-gs-v1-20261030-01 --out $OUT/input.json
python docs/ai-experiments/tools/typeset_browser_measure.py measure --input $OUT/input.json --fe $FE --out $OUT/baseline
```

필요한 것: Python(`requirements.txt`의 Pillow · pydantic) · `playwright` + `python -m playwright install chromium` · Node 22.13 이상(`module.stripTypeScriptTypes`).
DB · BE 서비스 · npm 설치는 필요 없다.

같은 입력의 조정 전후 비교 — 변형 입력(박스 · 배율만 바꿀 수 있다) → 측정 → 비교:

```bash
python docs/ai-experiments/tools/typeset_browser_measure.py derive --input $OUT/input.json --out $OUT/after-input.json --label <이름> \
    --set GS-01/s21/b271 fontScale=0.9 --set GS-01/s21/b258 box.h=96
python docs/ai-experiments/tools/typeset_browser_measure.py measure --input $OUT/after-input.json --fe $FE --out $OUT/after
python docs/ai-experiments/tools/typeset_browser_measure.py compare --before $OUT/baseline --after $OUT/after --out $OUT/compare
```

사람 확인(측정 오차 의심 사례를 사람 눈으로 가린다):

```bash
python docs/ai-experiments/tools/typeset_browser_review.py page --run $OUT/baseline        # → $OUT/baseline/check/index.html
python docs/ai-experiments/tools/typeset_browser_review.py aggregate --run $OUT/baseline --tsv <내려받은 TSV>
```

검토 페이지는 이미지를 넣은 HTML 한 파일이다. 사례마다 질문 하나("검정 글자가 빨간 선 안쪽에 들어가나요?" — 안에 들어감 · 밖으로 나감 ·
판단 불가)를 한 화면에 하나씩 보여 준다. 대상은 측정 오차 의심 전수(overflow_quantized 26 · overflow_subpixel 2)와 대조(overflow_confirmed 6 ·
fit 6, 키 순 같은 간격) 40건이고 순서는 섞는다. 도구 분류는 사용자가 펼칠 때만 보인다. 그림은 ① 판단용(측정한 블록을 글자색만 검정으로
바꿔 흰 바탕에 DPR 2로 촬영, 덧그림 없음) · ② 실제 화면(배경 · 글자색 그대로, 섹션 밖까지)이고, 배치 영역은 영역 바깥쪽 1px 빨간 선으로만
표시한다. 입력은 브라우저에만 저장되며 TSV로 내려받는다. 집계는 TSV의 지문 · 표본 · 판단값 · 중복을 검사하고 어기면 결과를 만들지 않는다.
검토 지문은 측정 환경(브라우저 · OS · TextMetrics 특성 · DPR · 폰트 · 실행기 · FE 모듈 · 입력) · 사례별 분류와 측정값(엔진 ink · 넘침 · 픽셀 경계) ·
검수 그림 바이트를 묶은 해시다 — 다시 측정해 하나라도 바뀌면 이전 검토는 받지 않는다(PR #140 리뷰 반영).
`baseline/review/index.html`은 491블록 전체의 측정 결과 열람용 목록이다(검토 화면이 아니다).

`compare`는 FE 모듈 · 폰트 · 브라우저 버전 · DPR · 실행기 파일 · OS(종류 · 릴리스 · 아키텍처) · 이 환경의 TextMetrics 특성(`metricsProbe`)이
다르면 비교를 거절하고, 환경 기록(OS · `metricsProbe`)이 없는 결과도 비교하지 않는다(PR #140 리뷰 반영 — OS가 다르면 글꼴 래스터라이저와
TextMetrics가 달라 환경 차이가 조정 효과로 섞인다). OS 빌드 번호는 기록만 한다. 블록마다 입력이 같은지(`same_input`) · 박스나
style이 바뀌었는지(`changed_input`)를 나누고, 입력이 같은데 결과가 다르면 `same_input_result_differs`(재현성 문제 — 조정 효과로 읽지 않음)로 둔다.

## 4. 결과

판정 기준: 넘침 여부는 FE 측정 엔진 결과(Canvas TextMetrics ink가 배치 영역을 넘는가, 경계와 같으면 정상, 허용 오차 없음) 그대로다.
아래 분류(judgement)는 그 결과를 **실제 픽셀 경계와 교차 확인한 해석**이며 판정을 바꾸지 않는다. 픽셀은 정답이 아니고 측정 방식의
차이를 드러내는 대조군이다(`contract.md` 6.2). 사람 판단이 아니다 — 아래 육안 확인은 에이전트가 그림을 본 것이다.

자체 점검(합의 사례, `selfcheck`): **14/14** — 한 줄 넓은 영역 정상 · 경계와 같음 정상 · 오른쪽/아래 0.115px 넘침 · 줄 상자만 영역 초과
(글자 크기 = 영역 높이 × 0.9) 정상 · 긴 단어 폭 넘침 · 여러 줄 높이 넘침 · j 왼쪽 넘침 · 가운데/오른쪽 정렬 · title 600 · 글리프 대체 거절 ·
폰트 미준비 거절. 기대값은 합의 기준의 결과로 적었다(오른쪽 정렬 + 음수 자간 사례는 아래 5절 협의 사항).

측정 환경 특성(측정 오류의 주원인): 이 PC의 Chromium 계열(Playwright 번들 149 · Chrome 157 · Edge 157 모두 같음)은 Canvas
`actualBoundingBox*`를 **글자 단위 정수 px**로 돌려준다(예: Pretendard 400 `P`의 ascent가 16px · 16.3px 모두 12). 도구는 이를
`metricsProbe.quantized=true`로 기록한다. 그 결과 엔진 ink가 실제 픽셀 경계보다 대체로 바깥으로 잡힌다 — 엔진이 바깥인 정도(DPR 2,
0.5px 단위): 왼쪽 0~1.5px · 오른쪽 0~2.0px · 위 0~1.0px · 아래 −0.5~1.5px(음수는 엔진이 안쪽). FE e2e(픽셀 차 ≤ 0.5px)가 통과하는 환경과 다르며, 서버가 쓸 Linux 브라우저에서는 확인하지 않았다.

491블록:

| 분류 | 건수 | 의미 |
|---|---:|---|
| fit | 52 | 엔진 정상 · 픽셀도 영역 안 |
| overflow_confirmed | **409** | 엔진 넘침이고 넘친 방향에서 픽셀도 0.5px 넘게 영역 밖 — 실제 넘침 |
| overflow_quantized | 26 | 엔진 넘침(초과 ≤ 2px)이나 픽셀은 영역 안 — 정수화로 생겼을 수 있는 넘침(판정은 넘침) |
| overflow_subpixel | 2 | 엔진 초과 ≤ 0.5px — 합의상 넘침, 픽셀로 구분 불가 |
| overflow_pixel_inside · fit_pixel_outside | 0 | 설명되지 않는 엔진 · 픽셀 불일치 |
| measure_failed | 2 | 측정 거절 — `∗`(U+2217)가 Pretendard에 없음(GS-01 #174, glyph-fallback) · FE 엔진의 baseline 탐침이 긴 문단의 줄 배치를 바꿈(GS-02 #464, baseline-probe-changed-layout) |

- 엔진 넘침 437건의 방향별: 아래 367(픽셀로도 밖 362 · 픽셀은 안 5) · 오른쪽 129(99 · 30) · **왼쪽 142(3 · 139)** · 위 0.
  왼쪽 넘침은 대부분 측정 오류다 — 줄 첫 글자의 왼쪽 잉크 경계가 정수로 반올림돼 영역 왼쪽 선을 넘는다(overflow_quantized 26건은 모두 left 정렬 블록). 넘침 여부(any)는 대부분
  아래 · 오른쪽 넘침이 함께 있어 바뀌지 않지만, 축별 상세(`width_overflow` 축 구분, #89)나 자동 조정의 축 선택에 쓰면 틀린다.
- 보조 DPR 1 측정과 넘침 여부 · 줄 수가 다른 블록 0(`dpr_sensitive` 0).
- 재현성: 같은 입력을 따로 두 번 전체 측정(도구 수정 중 첫 실행 · `final/`)한 결과가 491/491 블록에서 ink 좌표와 분류까지 같다.
  전체 측정 시간 72초(491블록 · 픽셀 대조 · 보조 DPR · 섹션 합성 66개 포함). 검토용 그림을 더한 뒤 같은 입력으로 다시 측정한 `final/`(92초)도
  분류 집계가 같다.
- 한 줄 블록(브라우저 1줄) 93: fit 34 · overflow_confirmed 43(픽셀로 확인된 방향 오른쪽 43 · 아래 7) · overflow_quantized 16.
  **fit 34건은 모두 줄 상자(글자 크기 × 1.2)가 영역 높이를 넘지만 글자는 영역 안**(`line_box_only` 34) — 한 줄이라서 넘침으로 판정되지 않는다.
  남은 한 줄 넘침 43건은 픽셀로 확인된 실제 넘침이다(예: `98%` GS-01 #72 오른쪽 11.1px, `#DarkSpotCoverage` GS-01 #271 오른쪽 약 10px).
- 이전 Pillow 분류(10-30 도구)와 대조: leading(줄 상자만 초과한 오판 후보) 40 → fit 29 · overflow_quantized 11(모두 왼쪽 정수화 한 방향만).
  브라우저 측정에서 이 40건 중 실제 넘침은 없다. width 119 → overflow_confirmed 107 · fit 8(`11.0` · `15.8%` · `-14.80%` 등 숫자 — 자간
  −0.03em 적용 차이로 추정, 원인 미확인) · 그 외 4. wrap 313 → overflow_confirmed 301 · fit 5 · overflow_quantized 5 · measure_failed 2.
- 10-30 사람 육안 검수 64사례와 대조(사람은 **Pillow 렌더**를 봤다 — 브라우저 렌더와 자간 · 줄바꿈이 다를 수 있어 정답 비교가 아니다.
  62사례는 줄 수가 같다): 밖으로 나감 12 → overflow_confirmed 12. 안에 들어감 46 → fit 32 · overflow_quantized 10 · overflow_subpixel 1 ·
  overflow_confirmed 3. 이 3건은 GS-02 #738 · GS-01 #258(10-30 기록에서 에이전트 재확인 때 영역 밖 줄이 보였던 사례 — 이번 그림에서도
  `Tone-Up` 아래 둘째 줄 `Sun Care`가 영역 밖) · GS-01 #271(픽셀로 오른쪽 9.5px 밖)이다. 판단 불가 6 → fit 1 · overflow_quantized 5.
- 섹션 · 최종 이미지 경계(블록 넘침과 별개로 계산): ink가 섹션 사각형 밖 14(오른쪽 5 · 아래 9) — 섹션 PNG를 섹션 크기로 자르면 손실될
  후보(6.6). 최종 출력 이미지(include 섹션을 순서대로 이은 높이, `buildFinalSectionOffsets`) 밖 7(오른쪽 5 · 아래 2) — Q-07 대상.
  섹션 밖이지만 블록 넘침은 아닌 경우 0. 아래로 섹션을 넘은 9건 중 7건은 다음 섹션 위로 이어져 최종 이미지 안에 남는다(섹션 단위 crop
  손실과 최종 경계 이탈이 다른 문제라는 사례). 보존본의 `deliverable_section.stack_offset`은 원본 `top_offset`이고, 최종 offset 계약(6.6,
  포함 섹션 높이 합)과 다르다 — 측정에는 계약 쪽(FE 함수)을 썼다.
- 참고값(정답 아님): 현행 develop 서버 Pillow 판정(`pillow-typeset/3`, PR #138)과 넘침 여부 일치 460/489(브라우저만 넘침 16 · Pillow만 넘침 13),
  줄 수 다름 51. 보존본 기록(v1.0.1, 줄 수 × 행간)과는 일치 442/489(기록만 넘침 42 · 브라우저만 넘침 5).
- 전후 비교 시연(조정 알고리즘이 아니다 — 수동으로 값 3개를 바꿨다): 바꾸지 않은 같은 섹션 34블록은 두 실행에서 ink 좌표까지 같았다
  (`same_input` 34, 결과 차이 0). 바꾼 3블록: GS-01 #258 영역 높이 44 → 96 · overflow_confirmed → fit, GS-01 #72 영역 폭 170 → 200 ·
  → fit, GS-01 #271 배율 1.0 → 0.9 · 오른쪽 넘침은 사라졌으나 **왼쪽 1px(정수화)로 여전히 넘침**(overflow_quantized). 그림으로는 영역 안이다.

사람 확인(사용자, 2026-11-16 — `baseline/check/index.html` 40사례 전부 검토, 지문 `e4c2aaa3b28d8e66`, TSV는 `baseline/check/`에 보존,
집계 `typeset_browser_review.py aggregate`). 질문은 "검정 글자가 빨간 선 안쪽에 들어가나요?" 하나이고 도구 분류는 기본으로 가렸다.
표본 안의 결과이며 491블록 전체로 넓히지 않는다:

| 도구 분류 | 사례 | 안에 들어감 | 밖으로 나감 | 판단 불가 |
|---|---:|---:|---:|---:|
| overflow_quantized(측정 오차 의심 — 전수) | 26 | **24** | **0** | 2 |
| overflow_subpixel(0.5px 이하 — 전수) | 2 | 2 | 0 | 0 |
| overflow_confirmed(대조) | 6 | 0 | **6** | 0 |
| fit(대조) | 6 | **6** | 0 | 0 |

- 대조 12사례는 도구 분류와 사람 판단이 모두 같다 — 질문 · 그림이 의도대로 작동했고, 픽셀로 확인된 넘침 · 정상이 사람 눈과 맞는다.
- 측정 오차 의심 28사례 중 사람이 "밖으로 나감"으로 본 사례는 0이다. overflow_quantized 26건은 사람 눈으로는 영역 안(24) 또는 판단 불가(2)였다
  — 이 PC의 정수화 측정이 만든 넘침이라는 해석과 맞는다. 판단 불가 2건은 아주 작은 숫자 한 글자 블록이다(GS-02 #446 `6` 영역 15×15px ·
  GS-02 #545 `1` 글자 6.3px — 원문과 같은 숫자. 10-30 검수의 판단 불가도 같은 종류였다).
- overflow_subpixel 2건(엔진 초과 ≤ 0.5px)은 사람 눈으로 안이다. 합의(D-02)상 넘침이 맞고, 이 결과는 허용 오차를 두는 근거가 아니다.
- 검토 지문 변경과의 관계: 이 검토는 처음 지문 방식(입력 · 분류만, `e4c2aaa3b28d8e66`)으로 받고 집계했다. 지문을 강화한 뒤 같은 입력 · 조건으로
  다시 측정한 결과, 검수 그림 980개(판단용 · 실제 화면)가 검토 시점과 SHA-256까지 같고(`baseline/check/images-at-review.sha256`), 491블록의
  측정값 · 픽셀 경계 · 분류도 모두 같았다 — 위 표는 현재 `final/baseline` 결과에 대한 검토다. 새 지문은 `bc41a54aa73be609`이고, 처음 TSV는
  새 지문 검사에서 의도대로 거절된다. 이후 측정 결과가 바뀌면 다시 검토한다.

시각 자료: `baseline/check/index.html`(사람 확인 페이지 — 3절) · `baseline/review/index.html`(전체 측정 결과 열람 — 분류 · flag 필터, 블록별 픽셀 대조 그림 · 섹션 합성 crop · 수치), `baseline/sections/*.annotated.png`
(섹션 밖까지 · 배치 영역 · ink · DOM 줄 조각 · 섹션 경계), `*.clean.png`(섹션 크기로 자른 합성), `derive-demo/compare/review/index.html`.

## 5. 판단

**보류** — 정본 · 운영 코드 · config를 바꾸지 않는다. 실험 도구로는 쓸 수 있다: 합의 기준의 FE 측정 엔진을 같은 입력의 전후에 같은 조건으로
돌리고, 배치 영역 · ink · 초과량 · 판정 · 그림을 남기며, 재현성(같은 입력 → 같은 결과)과 측정 조건 차이를 확인한다.

다만 **이 PC(Windows)의 측정 결과로 자동 조정의 효과를 결론 내리지 않는다.** TextMetrics 정수화로 왼쪽 · 오른쪽에 최대 약 2px의 넘침이
생기고, 그림으로는 들어간 블록도 넘침으로 남는다(#271 시연). 이 상태로 "넘침이 없어질 때까지 줄이는" 조정을 돌리면 왼쪽 정렬 블록이
필요 이상으로 줄어드는 편향이 생긴다. 서버 기준 환경에서 정수화가 있는지 먼저 확인해야 한다. 사람 확인에서도 정수화 의심 26건 중
밖으로 나간 사례는 없었다(안 24 · 판단 불가 2) — 이 26건은 실제 넘침이 아니라 이 환경의 측정 오류로 다룬다(표본 = 해당 분류 전수).

한 줄 블록: 브라우저 측정에서는 줄 상자 높이로 넘침이 되지 않는다(1줄 fit 34건 모두 `line_box_only`). 10-30의 leading 40건 중 실제 넘침은 없고
11건은 정수화 측정 오류다. 한 줄 넘침 43건은 픽셀로도 확인된 실제 넘침이다. 따라서 "한 줄이면 오판"이 아니라 "줄 상자 기준이면 오판"이다.

이 결과는 **최소 마크업 실험**이다. FE 공통 모듈의 측정 · 표시 코드는 같지만 FE 앱 · 웹폰트 · 서버 Playwright 촬영과 같다고 보지 않는다.

협의가 필요한 것(이 실험이 정하지 않는다):

1. 측정 기준 환경 — 판정 · 자동 조정 실험을 어느 OS · 브라우저 빌드에서 하는가. 서버 Playwright(Linux)에서 TextMetrics가 정수화되는지,
   FE 화면(Windows · macOS)과 판정이 갈리는 범위(6.2는 서버 기준). BE · FE · AI(#89 측정 규칙 버전 · 렌더 지문).
2. 오른쪽 · 가운데 정렬 + 음수 자간 — CSS 끝 자간 때문에 넉넉한 영역에서도 오른쪽으로 끝 자간 크기(글자 크기 × 0.03) 안쪽의 넘침이 될 수 있다(자체 점검
   `POINT 02` 16px 오른쪽 0.078px · 자간 0이면 정상, 표본에서 오른쪽 정렬 블록의 오른쪽 넘침 중 픽셀로는 안인 14건). 끝 자간 보정 없음(D-05) · 허용 오차 없음(D-02)의 의도된 결과인지. FE–AI 조판 협의.
3. FE 측정 엔진의 baseline 탐침이 줄 배치를 바꿔 측정을 거절한 사례(GS-02 #464) — FE 수정 대상인지. 같은 엔진을 쓰는 경로에서는 그 블록의 넘침 판정을 얻지 못한다(FE 화면 표시는 확인하지 않았다).
4. Pretendard에 없는 글자(`∗`)가 번역문에 있으면 측정이 거절된다(대체 폰트 금지, 6.4). 그때 N5 · N6 동작(측정 실패 표시 · 문자 처리)은 정하지 않았다.
5. FE 공통 모듈이 FE 통합 브랜치에만 있다 — 서버 Playwright가 같은 모듈을 쓰려면 소유 · 번들 전달(#78 · #89)이 먼저다.
6. 참고: PR #138의 서버 Pillow 판정은 ink가 섹션 이미지 밖이면 블록 넘침에도 넣는다. 계약은 블록 넘침과 경계 이탈을 독립으로 둔다(6.2 · 6.6).
   이 표본에서는 섹션 밖이면서 블록 넘침이 아닌 경우가 0이라 결과 차이는 없다. BE 확인 사항.

## 6. 반영

없음 — 정본 · 코드 · config 변경 없음. 실험 도구 · 시험(`tests/test_typeset_browser_measure_tool.py` · `test_typeset_browser_review_tool.py`) ·
이 기록만 추가했다. 2026-11-16 같은 날 사람 확인 페이지(`typeset_browser_review.py`)와 검토용 그림을 추가하고, 사용자 검토 40사례 결과를
4 · 5절에 반영했다. 같은 날 PR #140 리뷰를 반영해 전후 비교 조건에 OS · TextMetrics 특성을 넣고 검토 지문에 측정 환경 · 측정값 ·
검수 그림 해시를 넣었다(재측정 결과 · 그림은 검토 시점과 같음).
