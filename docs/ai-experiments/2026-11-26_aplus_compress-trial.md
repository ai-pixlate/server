# 2026-11-26 A+ 압축(2차 번역) 시험

> 압축 단계(`pipeline/aplus/compress.py`)를 실제 모델로 처음 돌린 기록이다. 수치는 **에이전트 집계**이고, 줄인 문구가 뜻을 지키는지는 사람이 보지 않았다. 입력 번역은 용어집 · 표현 지시 없이 만든 개발용이다. 결론은 아래 세 상품 범위에 한정한다.

## 1. 질문

템플릿 한도를 넘은 글 칸의 문구를 모델이 한도 안으로 줄일 수 있는가. 몇 칸이 한도 안으로 들어오고, 못 맞춘 칸은 어떻게 되는가(`docs/ai/aplus.md` 4.4 · 4.7, `docs/ai/open-questions.md` #94).

## 2. 표본

- GS-01(한 원본) · GS-02 · GS-03의 7칸 구성 결과(2026-11-24 실행분 `aplus-trial6_2026-11-24_<상품>/aplus_plan.json`, 상품마다 1회 실행한 결과). 읽기만 했다.
- 글 칸 54개 가운데 템플릿 한도를 넘은 칸이 19개다(GS-01 25개 중 8 · GS-02 15개 중 5 · GS-03 14개 중 6). 가이드 상한을 넘은 칸은 없다.
- 넘은 칸은 모두 블록 1개짜리 칸이다.

## 3. 조건

- 방식은 `docs/ai/aplus.md` 4.7(사용자 결정 2026-11-26)이다. 비교 대상은 없다 — 한 가지 설정으로 한 번 돌렸다.
- 모델 `gemini-3.8-flash` · 온도 0 · 글만 보냄(이미지 없음) · 다시 묻기 최대 1회(`aplus_compress.max_retries = 1`) · 호출 시간 제한 120초(시험 때의 기본값).
- 프롬프트 `pipeline/prompts/aplus_compress.md` 초안 v1(SHA-256 앞 16자 `ea98c6fa98f1e753`). 규격 데이터 `aplus-spec@2026-11-24.3`.
- 상품마다 1회 실행. 같은 입력에도 응답이 달라질 수 있다.
- GS-03은 첫 실행이 시간 제한에 걸려, 시간 제한만 240초로 올려(실행 인자) 한 번 더 돌렸다. 아래 표의 GS-03은 다시 돌린 결과다.
- 실행 도구는 `docs/ai-experiments/tools/aplus_compress_run.py`다.

## 4. 결과

판정 기준: 코드가 센 글자 수(공백 포함)가 템플릿 한도 이하이고, 답이 비어 있지 않고, 1차 문구에 없던 숫자가 없으면 「한도 안」이다. 뜻이 지켜졌는지는 이 기준에 없다.

| 상품 | 대상 칸 | 한도 안(문구 바뀜) | 1차 번역 유지 | 모델 호출 | 호출 시간 | 사고 토큰 |
|---|---|---|---|---|---|---|
| GS-01 | 8 | 5 | 3 | 1 | 49초 | 5,943 |
| GS-02 | 5 | 4 | 1 | 1 | 52초 | 6,269 |
| GS-03 | 6 | 3 | 3 | 1 (+ 시간 초과 1) | 138초 | 17,901 |
| 합계 | 19 | 12 | 7 | 3 (+ 1) | | |

- 압축 뒤 템플릿 한도를 넘는 칸은 54개 가운데 7개다(전에는 19개). 가이드 상한을 넘는 칸은 전후 모두 없다.
- 한도 안으로 들어온 12칸은 모두 첫 답이 채택됐다. 다시 묻기는 한 번도 일어나지 않아, 다시 묻는 경로는 실제 모델로 확인하지 못했다(가짜 모델 테스트만 있다).
- 1차 번역으로 남은 7칸은 모두 모델이 줄일 수 없다고 답한 칸이다. 6칸은 제품명 · 브랜드명을 줄이지 않는 기준을, 1칸(한도 12자)은 기간 수치와 전후 표현을 함께 남길 수 없다는 것을 이유로 들었다.
- GS-03 첫 실행은 120초 시간 제한에 걸려 전체 실패 결과가 됐다(문구는 하나도 바뀌지 않는다 — 설계대로). 240초로 다시 돌린 호출은 138초 걸렸다.
- 호출 시간의 대부분은 모델의 사고 단계로 보인다(출력 토큰 173~396개, 사고 토큰 5,943~17,901개).

칸별 결과(「칸」 = 칸 번호 · 템플릿 · 칸 안의 자리, 「글자 수」 = 1차 문구 / 한도):

| 상품 | 칸 | 1차 문구 | 글자 수 | 모델 답 | 답 글자 수 | 결과 |
|---|---|---|---|---|---|---|
| GS-01 | 2 2A `headline` | Gentle on skin formulated exclusively with zinc | 47/28 | Gentle on skin with zinc | 24 | 바뀜 |
| GS-01 | 3 3B `headline` | Definitive soothing with a triple zinc complex | 46/30 | Soothing triple zinc complex | 28 | 바뀜 |
| GS-01 | 4 4A `headline` | Helps soothe and improve skin weakened by external irritation | 61/30 | Helps soothe irritated skin | 27 | 바뀜 |
| GS-01 | 5 5B `items[0].title` | Heartleaf Moisture Sunscreen | 28/26 | — | — | 1차 유지(이름) |
| GS-01 | 5 5B `items[0].body` | #WaterFull #DeepHydration #FeelsLikeMoisturizer | 47/40 | #WaterFull #DeepHydration #Moisturizer | 38 | 바뀜 |
| GS-01 | 5 5B `items[1].title` | Heartleaf Mineral Sunscreen | 27/26 | — | — | 1차 유지(이름) |
| GS-01 | 5 5B `items[2].title` | Heartleaf Soothing Hyaluron Sun Serum | 37/26 | — | — | 1차 유지(이름) |
| GS-01 | 5 5B `items[2].body` | #PhotoagingCare #BurstingMoistureCapsules | 41/40 | #PhotoagingCare #MoistureCapsules | 33 | 바뀜 |
| GS-02 | 2 2A `headline` | Deeply and thoroughly absorbed, delivering results beyond specialized skin treatments | 85/28 | Beyond skin treatments | 22 | 바뀜 |
| GS-02 | 3 3A `headline` | Pinhole (Hollow Core) Spicule | 29/20 | Pinhole Spicule | 15 | 바뀜 |
| GS-02 | 3 3A `body` | Designed with a hollow center, the pinhole spicules evenly deliver active ingredients deeper into the skin along their inner channels. | 134/120 | Hollow pinhole spicules evenly deliver active ingredients deeper into the skin along their inner channels. | 106 | 바뀜 |
| GS-02 | 4 4A `headline` | 892.35% Improvement in Absorption Depth | 39/30 | 892.35% Absorption Depth | 24 | 바뀜 |
| GS-02 | 7 7A `headline` | Retinal Shot Tightening Booster Product Development Story | 57/20 | — | — | 1차 유지(이름) |
| GS-03 | 1 1B `headline` | Brightening Serum Tone Correcting Body Lotion b.clinicx | 55/22 | — | — | 1차 유지(이름) |
| GS-03 | 2 2A `headline` | From Pigmentation Brightening to Natural Tone-Up An Innovative Tone-Correcting One-Step Body Lotion | 99/28 | From Brightening to Tone-Up | 27 | 바뀜 |
| GS-03 | 4 4B `headline` | Improvement in Blemish Marks on the Back and Chest* | 51/38 | Blemish Improvement on Back and Chest* | 38 | 바뀜 |
| GS-03 | 4 4B `items[1].title` | After 2 Weeks of Use | 20/12 | — | — | 1차 유지(한도 12자) |
| GS-03 | 6 6A `items[1].title` | Daily Tone Correcting Body Lotion | 33/20 | — | — | 1차 유지(이름) |
| GS-03 | 6 6A `items[2].title` | Intensive Tone-Up Body Mist | 27/20 | Tone-Up Body Mist | 17 | 바뀜 |

에이전트가 표를 읽고 적은 관찰이다. 사람 검수가 아니며 맞고 틀림의 판정이 아니다.

- 뜻이 줄거나 달라져 보이는 답이 있다.
  - GS-01 칸 2: `exclusively`가 빠져 「징크만으로 만들었다」가 「징크가 들어 있다」로 읽힌다.
  - GS-02 칸 4: `Improvement in`이 빠져 개선율이 깊이 수치처럼 읽힌다.
  - GS-01 칸 5: `#FeelsLikeMoisturizer`가 `#Moisturizer`가 됐다.
  - GS-02 칸 2: 흡수에 관한 앞부분이 모두 빠지고 「시술 이상」이라는 뒷부분만 남았다.
- 이름을 줄이지 않는 기준이 고르게 적용되지 않았다. `Pinhole (Hollow Core) Spicule`은 괄호 부분을, `Intensive Tone-Up Body Mist`는 `Intensive`를 뺐고, `Daily Tone Correcting Body Lotion`은 줄일 수 없다고 답했다.
- 완화 표현 `Helps`(GS-01 칸 4)와 각주 기호 `*`(GS-03 칸 4)는 남았다.
- 한도 안으로 들어온 답 가운데 한도를 꽉 채운 것은 1칸(38/38)이고 나머지는 한도보다 2~14자 짧다.

## 5. 판단

- **보류** — 압축의 방식(자리 · 입력 · 출력 · 채택 규칙)은 시험 전에 사용자 결정(2026-11-26)으로 정해졌고, 이 기록은 그 방식의 첫 실행이다. 프롬프트의 줄이는 기준과 설정값을 그대로 쓸지는 정하지 않았다.
- 이 세 상품에서는 한도를 넘은 19칸 가운데 12칸이 한도 안으로 들어왔다. 줄인 12칸의 문구가 쓸 만한지는 사람 검수가 필요하다.
- 남은 7칸 가운데 6칸은 이름을 줄이지 않는 기준에서 나왔다. 이 칸들을 줄이려면 그 기준을 바꾸거나 7칸 구성에서 다른 블록을 고르게 해야 하며, 둘 다 이 시험의 범위 밖이다.
- 호출 시간 제한 120초는 세 상품 가운데 한 상품에서 모자랐다. 기본값을 240초로 올린다(사용자 결정 2026-11-26). 사고 단계를 줄이는 설정을 둘지는 정하지 않았다.

## 6. 반영

- `docs/ai/aplus.md` 2절(실행 순서) · 3절(단계표 압축 행) · 4.1 · 4.7(압축 규칙) — 2026-11-26.
- `docs/ai/open-questions.md` #94 — 압축의 입력 · 출력 · 순서를 닫고 남은 것(줄이는 기준 · 뜻 보존 검수 · 다시 묻는 횟수와 시간 제한 · 가이드 상한까지 넘는 칸 · 용어집 · 표현 지시 입력)을 적었다 — 2026-11-26.
- `docs/ai/status.md` A+ 행 · 다음 착수 — 2026-11-26.
- `pipeline/config/default.toml`의 `aplus_compress.timeout_s`를 120 → 240으로 바꿨다(사용자 결정 2026-11-26). 나머지 설정값은 개발용 잠정값 그대로다.
- 산출물은 `pipeline/out/aplus-compress-trial_2026-11-26_GS-01` · `_GS-02` · `_GS-03`(시간 초과) · `_GS-03_r2`에 있다(git 제외).
