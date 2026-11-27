# 2026-11-26 A+ 압축 규제 반영 시험

> 압축(`pipeline/aplus/compress.py`)에 규제 행을 준 실행을 실제 모델로 처음 돌린 기록이다. 수치는 **에이전트 집계**이고, 줄인 문구가 뜻을 지키는지는 사람이 보지 않았다. 사전은 로컬에 있던 이전 판이고 입력 번역은 용어집 · 표현 지시 없이 만든 개발용이다. 결론은 아래 세 상품 범위에 한정한다.

## 1. 질문

규제 행의 영어 표현을 모델에 주고, 1차 문구에 없던 규제 매칭이 생긴 답을 채택하지 않게 하면 압축 결과가 어떻게 되는가(`docs/ai/aplus.md` 4.7, `docs/ai/open-questions.md` #94).

## 2. 표본

- GS-01(한 원본) · GS-02 · GS-03의 7칸 구성 결과(`aplus-trial6_2026-11-24_<상품>/aplus_plan.json`). 읽기만 했다. `2026-11-26_aplus_compress-trial.md`와 같은 입력이다.
- 템플릿 한도를 넘은 칸 19개다(GS-01 8 · GS-02 5 · GS-03 6).

## 3. 조건

- 방식은 `docs/ai/aplus.md` 4.7(사용자 결정 2026-11-26)이다.
- 프롬프트 `pipeline/prompts/aplus_compress.md` 초안 v2(SHA-256 앞 16자 `58800a69fa4adde8`). 모델 `gemini-3.8-flash` · 온도 0 · 다시 묻기 최대 1회 · 호출 시간 제한 240초.
- 규제 행은 로컬 정규화 사전 `regulation@2026-09-28.1`(원본 표기 v9 · 16행)에서 골랐다 — GS-01 `otc`(규제 행 10개 · 모델에 준 표현 36개), GS-02 · GS-03 `cosmetic`(규제 행 6개 · 표현 89개). 규제 분류는 에이전트가 상품 종류를 보고 준 값이며 셀러 선택값이 아니다. `docs/ai/contract.md` 5.1.1은 규제사전 v12로의 교체를 따로 적고 있다 — 이 실행의 사전은 그 이전 판이다.
- 상품마다 1회 실행. 비교 대상은 같은 날 규제를 반영하지 않은 실행(`2026-11-26_aplus_compress-trial.md`, 프롬프트 초안 v1)이다. 두 실행 모두 1회씩이라, 답의 차이가 규제 반영 때문인지 실행마다 달라지는 것인지는 구분할 수 없다.
- 실행 도구는 `docs/ai-experiments/tools/aplus_compress_run.py`(`--regulatory-class`)다.

## 4. 결과

판정 기준: 코드가 센 글자 수가 템플릿 한도 이하이고, 답이 비어 있지 않고, 1차 문구에 없던 숫자가 없고, 1차 문구에 없던 규제 매칭이 없으면 「한도 안」이다. 뜻이 지켜졌는지는 이 기준에 없다.

| 상품 | 대상 칸 | 한도 안(문구 바뀜) | 1차 번역 유지 | 새 규제 매칭으로 거절된 답 | 모델 호출 | 호출 시간 | 사고 토큰 |
|---|---|---|---|---|---|---|---|
| GS-01 | 8 | 5 | 3 | 0 | 1 | 54초 | 5,768 |
| GS-02 | 5 | 4 | 1 | 0 | 1 | 104초 | 12,209 |
| GS-03 | 6 | 3 | 3 | 0 | 1 | 174초 | 21,230 |
| 합계 | 19 | 12 | 7 | 0 | 3 | | |

- 한도 안으로 들어온 칸과 1차 번역으로 남은 칸은 규제를 반영하지 않은 실행과 같은 칸이다. 남은 7칸은 이번에도 모두 모델이 줄일 수 없다고 답한 칸이다.
- 새 규제 매칭으로 거절된 답은 없었고 다시 묻기도 일어나지 않았다. 새 규제 매칭을 막는 경로는 실제 모델로 확인하지 못했다(가짜 모델 테스트만 있다).
- 압축 뒤 규칙 검사(`aplus_recheck_run.py`, 같은 사전 · 같은 분류)에서도 바뀐 12칸의 규제 매칭은 0건이다.
- 한도 안으로 들어온 12칸 가운데 6칸은 답이 앞선 실행과 같고 6칸은 다르다.

| 상품 | 칸 | 1차 문구 | 앞선 실행의 답 | 이번 답 |
|---|---|---|---|---|
| GS-01 | 2 2A `headline` | Gentle on skin formulated exclusively with zinc | Gentle on skin with zinc | Gentle zinc-only formula |
| GS-01 | 5 5B `items[0].body` | #WaterFull #DeepHydration #FeelsLikeMoisturizer | #WaterFull #DeepHydration #Moisturizer | #DeepHydration #FeelsLikeMoisturizer |
| GS-02 | 2 2A `headline` | Deeply and thoroughly absorbed, delivering results beyond specialized skin treatments | Beyond skin treatments | Results beyond treatments |
| GS-02 | 4 4A `headline` | 892.35% Improvement in Absorption Depth | 892.35% Absorption Depth | 892.35% Deeper Absorption |
| GS-03 | 2 2A `headline` | From Pigmentation Brightening to Natural Tone-Up An Innovative Tone-Correcting One-Step Body Lotion | From Brightening to Tone-Up | Tone-Correcting Body Lotion |
| GS-03 | 4 4B `headline` | Improvement in Blemish Marks on the Back and Chest* | Blemish Improvement on Back and Chest* | Improved Back and Chest Blemish Marks* |

에이전트가 표를 읽고 적은 관찰이다. 사람 검수가 아니며 맞고 틀림의 판정이 아니다.

- 앞선 실행에서 뜻이 줄어 보인다고 적은 칸 가운데 GS-01 칸 2(`zinc-only`) · GS-01 칸 5(`#FeelsLikeMoisturizer`) · GS-02 칸 4(`Deeper`)는 이번 답에 그 뜻이 남아 있다. GS-02 칸 2는 이번에도 흡수에 관한 앞부분이 빠졌다.
- GS-03 칸 2는 이번에 제품 유형 이름만 남았다.
- 이 차이들은 모두 사전 표현과 무관한 부분이다. 규제 반영이 만든 차이라고 볼 근거는 이 실행에 없다.

## 5. 판단

- **보류** — 규제 반영의 방식은 시험 전에 사용자 결정(2026-11-26)으로 정해졌고, 이 기록은 그 방식의 첫 실행이다.
- 이 세 상품 · 이 사전에서는 규제 반영을 넣어도 한도 안으로 들어온 칸 수가 같았고, 새 규제 매칭으로 거절된 답이 없었다. 규제 반영의 효과는 이 표본으로 말할 수 없다.
- GS-03 호출은 174초 걸렸다. 호출 시간 제한 240초 안이지만 여유가 크지 않다. 사고 단계를 줄이는 설정을 둘지는 정하지 않았다.

## 6. 반영

- `docs/ai/aplus.md` 4.7 근거 시험 · `docs/ai/status.md` A+ 행 · `pipeline/prompts/README.md` `aplus_compress.md` 행 — 2026-11-26.
- 산출물은 `pipeline/out/aplus-compress-trial_2026-11-26_GS-01_reg` · `_GS-02_reg` · `_GS-03_reg`와 `aplus-recheck-trial_2026-11-26_<상품>_reg`에 있다(git 제외).
