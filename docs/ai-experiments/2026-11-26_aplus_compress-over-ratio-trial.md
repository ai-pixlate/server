# 2026-11-26 A+ 압축 대상 기준(권장 길이의 150%) 시험

> 압축 대상을 「권장 길이(템플릿 한도) × `aplus_compress.over_ratio`를 넘는 칸」으로 좁히고 답도 그 길이 이하면 채택하게 바꾼 뒤 실제 모델로 처음 돌린 기록이다. 수치는 **에이전트 집계**이고, 줄인 문구가 뜻을 지키는지와 그림이 괜찮은지는 사람이 보지 않았다. 넘침은 검수 렌더(FE 렌더가 아니다) 기준이다. 결론은 아래 세 상품 범위에 한정한다.

## 1. 질문

템플릿 한도가 권장 길이라면(데이터 담당 답변 2026-11-26 — 지켜야 하는 것은 가이드 상한이다) 압축이 필요한 칸은 어디까지인가. 권장 길이의 150%를 기준으로 삼으면 압축 결과가 어떻게 되는가(`docs/ai/aplus.md` 4.4 · 4.7, `docs/ai/open-questions.md` #94).

## 2. 표본

- GS-01(한 원본) · GS-02 · GS-03의 7칸 구성 결과(`aplus-trial6_2026-11-24_<상품>/aplus_plan.json`). 읽기만 했다. 앞선 두 압축 시험과 같은 입력이다.
- 권장 길이를 넘은 칸 19개다(GS-01 8 · GS-02 5 · GS-03 6). 19칸 모두 1차 문구 그대로 가이드 상한 안이다.
- 넘침 확인에는 압축하지 않은 1차 문구로 그려 둔 검수 렌더(`aplus-review_2026-11-24_<상품>_r6/report.json`)를 썼다.

## 3. 조건

- 방식은 `docs/ai/aplus.md` 4.7(사용자 결정 2026-11-26)이다. `aplus_compress.over_ratio = 1.5`.
- 프롬프트 `pipeline/prompts/aplus_compress.md` 초안 v3(SHA-256 앞 16자 `c32457c361e972da`). 모델 `gemini-3.8-flash` · 온도 0 · 다시 묻기 최대 1회 · 호출 시간 제한 240초.
- 규제 반영을 켰다 — 로컬 정규화 사전 `regulation@2026-09-28.1`(이전 판), GS-01 `otc` · GS-02 · GS-03 `cosmetic`(에이전트가 상품 종류를 보고 준 값이며 셀러 선택값이 아니다).
- 상품마다 1회 실행. 비교 대상은 같은 날의 `2026-11-26_aplus_compress-regulation-trial.md`(권장 길이를 넘은 칸 전부가 대상 · 권장 길이 이하만 채택)다. 두 실행 모두 1회씩이라 답의 차이에는 실행마다 달라지는 몫이 섞여 있다.
- 실행 도구는 `docs/ai-experiments/tools/aplus_compress_run.py`다.

## 4. 결과

### 4.1 기준값을 고른 근거 — 압축하지 않았을 때의 넘침

판정 기준: 검수 렌더가 그 칸에 `[넘침]` 경고를 냈으면 「넘침」이다. 검수 렌더는 글이 넘치면 글자를 80%까지 줄여 그리므로, 경고는 그렇게 줄여도 넘친 경우다.

- 권장 길이를 넘은 19칸 가운데 넘침은 6칸이다. 6칸 모두 제목 칸이고 1차 문구가 권장 길이의 1.68배 이상이다(47/28 · 61/30 · 85/28 · 57/20 · 55/22 · 99/28).
- 넘침 경고가 없는 13칸은 권장 길이의 1.02~1.67배다.

| 기준(권장 길이의) | 압축 대상 | 넘친 6칸 가운데 대상 | 넘치지 않는데 대상인 칸 |
|---|---|---|---|
| 110% 초과 | 16 | 6 | 10 |
| 125% 초과 | 14 | 6 | 8 |
| 150% 초과 | 9 | 6 | 3 |
| 170% 초과 | 6 | 6 | 0 |
| 200% 초과 | 5 | 5 | 0 |

### 4.2 150% 기준으로 돌린 결과

판정 기준: 코드가 센 글자 수가 「넘으면 안 되는 길이」(권장 길이 × 1.5, 가이드 상한 이하) 이하이고, 답이 비어 있지 않고, 1차 문구에 없던 숫자와 규제 매칭이 없으면 「채택」이다. 뜻이 지켜졌는지는 이 기준에 없다.

| 상품 | 권장 길이 초과 | 대상 칸 | 채택(문구 바뀜) | 1차 번역 유지 | 모델 호출 | 호출 시간 | 사고 토큰 |
|---|---|---|---|---|---|---|---|
| GS-01 | 8 | 3 | 3 | 0 | 1 | 52초 | 6,484 |
| GS-02 | 5 | 2 | 1 | 1 | 1 | 67초 | 7,771 |
| GS-03 | 6 | 4 | 4 | 0 | 1 | 81초 | 9,563 |
| 합계 | 19 | 9 | 8 | 1 | 3 | | |

- 대상이 아닌 10칸은 권장 길이를 넘은 채 1차 번역 그대로다.
- 채택된 8칸의 답은 권장 길이의 1.07~1.47배다(앞선 실행은 0.75~1.00배). 다시 묻기와 규제로 거절된 답은 없었다.
- 남은 1칸(GS-02 칸 7)은 제품명만 31자라 넘으면 안 되는 길이 30자에 못 맞춘다고 모델이 답했다.
- 압축 뒤 규칙 검사에서 바뀐 8칸의 규제 매칭은 0건이다(같은 사전 · 같은 분류).

| 상품 | 칸 | 1차 문구 | 글자 수 / 권장 / 넘으면 안 되는 길이 | 이번 답 | 답 글자 수 | 앞선 실행의 답 |
|---|---|---|---|---|---|---|
| GS-01 | 2 2A `headline` | Gentle on skin formulated exclusively with zinc | 47 / 28 / 42 | Gentle on skin exclusively with zinc | 36 | Gentle zinc-only formula |
| GS-01 | 3 3B `headline` | Definitive soothing with a triple zinc complex | 46 / 30 / 45 | Definitive soothing with triple zinc complex | 44 | Soothing triple zinc complex |
| GS-01 | 4 4A `headline` | Helps soothe and improve skin weakened by external irritation | 61 / 30 / 45 | Helps soothe skin weakened by irritation | 40 | Helps soothe irritated skin |
| GS-02 | 2 2A `headline` | Deeply and thoroughly absorbed, delivering results beyond specialized skin treatments | 85 / 28 / 42 | Results beyond skin treatments | 30 | Results beyond treatments |
| GS-02 | 7 7A `headline` | Retinal Shot Tightening Booster Product Development Story | 57 / 20 / 30 | — | — | — (1차 유지) |
| GS-03 | 1 1B `headline` | Brightening Serum Tone Correcting Body Lotion b.clinicx | 55 / 22 / 33 | Tone Correcting Body Lotion | 27 | — (1차 유지) |
| GS-03 | 2 2A `headline` | From Pigmentation Brightening to Natural Tone-Up An Innovative Tone-Correcting One-Step Body Lotion | 99 / 28 / 42 | From Brightening to Natural Tone-Up | 35 | Tone-Correcting Body Lotion |
| GS-03 | 4 4B `items[1].title` | After 2 Weeks of Use | 20 / 12 / 18 | After 2 Weeks | 13 | — (1차 유지) |
| GS-03 | 6 6A `items[1].title` | Daily Tone Correcting Body Lotion | 33 / 20 / 30 | Tone Correcting Body Lotion | 27 | — (1차 유지) |

에이전트가 표를 읽고 적은 관찰이다. 사람 검수가 아니며 맞고 틀림의 판정이 아니다.

- GS-01 세 칸은 앞선 실행보다 1차 문구의 내용이 더 남았다(`exclusively` · `Definitive` · `weakened by`).
- GS-02 칸 2는 넘으면 안 되는 길이가 42자인데도 30자로 줄였고, 흡수에 관한 앞부분이 이번에도 빠졌다.
- **이름을 줄인 칸이 생겼다.** GS-03 칸 1은 `Brightening Serum`과 브랜드명 `b.clinicx`가 빠졌고, 칸 6은 `Daily`가 빠졌다. 앞선 두 실행에서는 모델이 줄일 수 없다고 답한 칸이다. 프롬프트의 「제품명 · 브랜드명 · 성분명은 일부만 쓰지 않는다」 기준과 어긋나 보인다.

## 5. 판단

- **보류** — 150%는 사용자 결정(2026-11-26)으로 넣은 값이고, 이 기록은 그 값의 첫 실행이다. 근거는 세 상품 19칸의 검수 렌더 넘침이며 FE 렌더로 확인한 값이 아니다.
- 이 세 상품에서는 대상이 19칸에서 9칸으로 줄었고, 채택된 답이 권장 길이 밑이 아니라 권장 길이의 1.07~1.47배에 머물렀다.
- 길이 조건이 느슨해지자 앞서 줄이지 않던 이름 칸 2개를 모델이 줄였다. 이름을 어디까지 줄여도 되는지는 정하지 않았다(#94).
- 줄인 8칸의 뜻과 그림은 사람 검수가 필요하다.

## 6. 반영

- `pipeline/config/default.toml` `aplus_compress.over_ratio = 1.5` · `docs/ai/pipeline.md` 3절 — 2026-11-26.
- `docs/ai/aplus.md` 2절 · 3절(압축 행) · 4.4(템플릿 한도는 권장 길이) · 4.7(대상 · 채택) — 2026-11-26.
- `docs/ai/open-questions.md` #94 · `docs/ai/status.md` A+ 행 · `docs/ai/dev.md` · `pipeline/prompts/README.md` — 2026-11-26.
- 산출물은 `pipeline/out/aplus-compress-trial_2026-11-26_GS-01_ratio` · `_GS-02_ratio` · `_GS-03_ratio`와 `aplus-recheck-trial_2026-11-26_<상품>_ratio`에 있다(git 제외).
