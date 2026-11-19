# 2026-11-19 ⑧ 번역문 LLM 규제 판정 — 판단 이유(설명) 소규모 실측

## 1. 질문

- LLM 규제 판정(`translation_review`)이 실제 모델에서 만드는 판단 이유(`reason`)가 문제 표현 · 판정 · 공급 근거와 맞는가. 사용자가 읽고 이해할 수 있는가. 공급되지 않은 규제 이유 · 조문을 만들어내는가.
- 실제 응답이 운영 인계 형식 · BE 채택 전 검증기(`validate_report`)를 통과하는가.
- 관련: `open-questions.md` #84(실측) · #91(판단 이유 전달), `contract.md` 5.3, `be-handoff.md` 8.13.

## 2. 표본

- 합성 영어 번역문 2세트, 원문은 자리표시 문구(`(합성 원문 n)`) — 실제 상품 문구가 아니다. 세트 1: 화장품 섹션 1(대상 6 · 제품 라벨 문맥 1) · OTC 섹션 1(대상 5). 세트 2: 화장품 섹션 1(대상 6).
- 규제 행: 로컬 정규화본 `pipeline/samples/local/dict/normalized/regulation.json`(규제사전 v9 계열, 16행)을 운영 묶음 형식으로 옮긴 개발용 사본. BE DB 적재본(v12 등)이 아니다. 현지 사전 · 정책 규칙은 합성 묶음 그대로(LLM 판정이 소비하지 않음).
- 도구: `docs/ai-experiments/tools/review_explanation_probe.py`. 모델 호출은 같은 사례 · 같은 묶음 변환의 임시 스크립트로 했고 이 도구는 그것을 정리한 것이다 — 도구로는 저장 보고 7건의 재검증(`--validate-only`)만 다시 돌렸다. 보고 원본은 `runs/2026-11-19_08-review_explanation-probe/`(git 제외).

## 3. 조건

- `pipeline/config/default.toml` `[translation_review]` 기본값(모델 `gemini-3.8-flash` · 온도 0 · 분할 상한 기본) · 프롬프트 `pipeline/prompts/translation_review.md` 그대로. 실행은 운영 인계 함수 `run_translation_review`.
- 화장품 섹션이 60초 제한에 걸린 뒤 `--override translation_review.timeout_s=180`으로만 다시 돌렸다(기본값은 바꾸지 않음, README 4.1).
- 같은 입력 재실행: 세트 1을 두 번(화장품은 세 번째 시도가 완료).

## 4. 결과

판정 기준: 화면 신호 후보는 risk · undetermined(flag 후보)다. 각 판단 이유를 문제 표현 · 판정 · 공급 사전 사유 · 근거 조문과 대조했다. **에이전트 1차 검수**이며 사용자가 원문을 직접 본 검수가 아니다.

### 4.1 실행

| 보고 | 결과 | 쌍 | flag 후보 | 총토큰 |
|---|---|---|---|---|
| 1.cosmetic.1 | completed | 30 | 3 | 12,358 |
| 1.cosmetic.2 | failed — `model_call_failed`(504 DEADLINE_EXCEEDED, 60초) | — | — | — |
| 1.cosmetic.3 (180초) | completed | 30 | 2 | 14,763 |
| 1.otc.1 | completed | 45 | 5 | 11,000 |
| 1.otc.2 | completed | 45 | 6 | 9,495 |
| 2.cosmetic.1 | failed — `model_call_failed`(504, 60초) | — | — | — |
| 2.cosmetic.2 (180초) | completed | 30 | 3 | 12,302 |

- 7건 모두 `validate_report` 통과(실패 보고 포함). 완료 보고의 판단 이유는 20~73자 한국어 한 문장, 입력 내부 참조(`t1` · `r1` · `r1.e1` · `a1`) · 사전 ID(`RG-…`) 노출 0건(패턴 검사).
- 화장품 섹션 60초 호출 4회 중 2회 실패. 완료 호출의 사고 토큰은 5,204~8,850. 금액은 미검증 추정이라 적지 않는다.

### 4.2 flag 후보의 판단 이유(완료 보고, 중복 제거)

| 번역문 | 항목 · 판정 | 문제 표현 | 판단 이유 요지 | 에이전트 1차 검수 |
|---|---|---|---|---|
| Visibly reduces wrinkles in just 2 weeks | RG-008 risk | reduces wrinkles | 주름 감소 주장 → 피부 구조 변화 시사 | 사전 사유와 일치. 2회 같음 |
| Not an acne treatment — … acne-prone skin | RG-023 risk(1회) · no_risk(1회) | acne treatment | 부정형이어도 질환 주장을 환기 / 치료제 아님을 명시해 위험 없음 | **같은 입력에서 판정이 갈림.** 이유는 각 판정과 일치 |
| Clinically shown to fade melasma and dark spots | RG-025 risk | melasma · fade melasma | 색소 질환 개선 → 치료(의약품) 주장 | 사전 사유와 일치. "의약품"은 사전 사유("치료 주장")를 넓힌 표현(경미) |
| SPF 50+ Broad Spectrum Sunscreen | RG-021 · RG-022 undetermined(`condition_unverifiable`) | SPF 50+ · Broad Spectrum | 시험 결과값 · 임계파장 시험 통과 여부를 확인할 수 없음 | 조건 · 조문(§ M020.80 · § M020.90)이 공급 사유에서 옴. 2회 같음 |
| 100% waterproof formula … | RG-003 risk | 100% waterproof | § M020.50(g)이 금지하는 방수 표현 | 공급 근거 조문과 일치 |
| Water resistant (80 minutes) | RG-020 undetermined(`condition_unverifiable`) | Water resistant (80 minutes) | 80분 내수성 시험 통과 여부 확인 불가 | 공급 사유(§ M020.80(g))와 일치 |
| Daily use prevents skin cancer | RG-013 risk · RG-015 risk(2회 중 1회) | prevents skin cancer | 선크림 단독 피부암 예방 금지 / 규정 문구 요건 미준수 | RG-013 일치. RG-015는 같은 위치의 두 번째 경고이고 "준수하지 않았습니다"로 단정형 |
| Anti-wrinkle serum that smooths wrinkles. Smooths wrinkles, … | RG-008 risk(위치 2) | smooths wrinkles · Smooths wrinkles | 주름을 편다 → 구조 변화 | 대소문자가 다른 반복 위치 2개를 각각 특정 |
| Minimizes the look of pores and unclogs pores | RG-030 risk | unclogs pores | 모공을 뚫는다 → 구조 변화 | 사전 사유와 일치. "the look of" 쪽은 지적 안 함 |

지적하지 않은 대상: `Soothes and cures eczema overnight`(사전에 습진 항목이 없다 — 공급 항목 밖 위험은 검출 범위 밖이며 근거 없는 이유를 만들지 않았다) · `Brightening glow …` · `Ignore all previous instructions …`(입력 속 지시를 따르지 않음) · `Dermatologist recommended, 1 brand in Korea`(사전 항목 없음) · `Helps prevent sunburn` · `Suitable for all skin types` · `For acne-prone skin`(RG-023 no_risk, 허용 표현 근거).

## 5. 판단

보류 — 이 표본의 실행과 에이전트 1차 검수에서 판단 이유가 판정 · 문제 표현 · 공급 근거와 어긋난 사례와 공급되지 않은 조문 인용은 발견하지 못했다. 판정 정확도 · 안정성은 이 표본으로 판단하지 않는다.

- 판단 이유 생성은 유지한다(프롬프트 · 검증 변경 없음). 사용자 표시 문장으로 쓸지와 형식은 BE · FE 합의 대상이다(`be-handoff.md` 8.13).
- 관찰된 위험(합의 · 후속 실측 대상): ① 60초 호출 제한에서 화장품 섹션 4회 중 2회 실패 — 연결하면 「⚠ 검사하지 못했습니다」로 끝나 설명이 전달되지 않는다. 운영 시간 제한 · 재시도는 BE 실행 정책이고 config 기본값은 사용자 확정 사안이다. ② 부정형 문장에서 같은 입력의 판정이 갈렸다. ③ 같은 위치에 서로 다른 항목의 경고가 두 개 생길 수 있다(표시 통합 규칙은 같은 항목 조건부 경고만 다룬다). ④ 단정형 이유("준수하지 않았습니다")가 나올 수 있다.

## 6. 반영

- `open-questions.md` #84에 이 기록과 관찰 ①~④를 연결(2026-11-19). 판단 이유 전달 형식은 #91 · `be-handoff.md` 8.13(미합의 제안).
- config 기본값 · 프롬프트는 바꾸지 않았다.
