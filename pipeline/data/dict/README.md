# pipeline/data/dict — ③-1 · ③-1' 사전 데이터의 도구 · 스키마 · 합성 데이터

> 근거: `docs/ai-experiments/2026-09-28_03-1-judge_design-v1.md` 8절(D8, 2026-09-28 사용자 결정), `docs/ai/open-questions.md` #3 · #60.
> 전달 자료(설명서 · 규제사전 · 현지부적합사전, `docs/ai/README.md` 2.1)는 정본이 아니다. 여기의 스키마는 **개발용 잠정**이며 BE 저장 계약이 아니다.

## 무엇이 어디에 있나

| 것 | 위치 | git |
|---|---|---|
| 스키마 · 로더 · 두 뷰 | `pipeline/dictionary.py` | 포함 |
| 변환 · 검증 · 비교 도구 | `tools/build_dict.py` | 포함 |
| 합성 테스트 데이터(실제 값 아님) | `synthetic/`(`regulation.json` · `local_unsuitable.json` · `policy_rules.json` · `SHA256SUMS`) | 포함 — 테스트는 이것만 쓴다 |
| 원본 자료(xlsx 2개 · 설명서 md) | `pipeline/samples/local/dict/source/` | **제외** |
| 실제 값이 든 정규화 JSON | `pipeline/samples/local/dict/normalized/`(= config `judge.dict_dir` 기본값) | **제외** — 로컬 배치(2026-09-28 사용자 결정). 저장소 공유 범위 확인 전 |

실제 데이터를 나중에 git에 넣기로 해도 로더는 바뀌지 않는다(`judge.dict_dir` 경로만).

## 정규화 절차

```bash
python -m pipeline.data.dict.tools.build_dict build \
  --regulation pipeline/samples/local/dict/source/regulation_dict.xlsx \
  --local pipeline/samples/local/dict/source/locale_unsuitable_dict.xlsx \
  --out pipeline/samples/local/dict/normalized --date 2026-09-28 --seq 1 \
  --local-version-status mismatch_pending --local-version-note "엑셀 v5 · 설명서 v7 — 확인 대기(#50)"
python -m pipeline.data.dict.tools.build_dict verify --dir pipeline/samples/local/dict/normalized
python -m pipeline.data.dict.tools.build_dict diff --old <이전 묶음> --new <새 묶음>
```

- **허용 열은 색이 아니라 목록**(`build_dict.REG_COLUMNS` · `LOCAL_COLUMNS`)으로 정한다. 규제사전 `confidence`는 검증(`high`만 적재)에만 쓰고 출력하지 않는다. `source` · `version`(시트 전용) · 현지부적합 `판단 근거` · `kr_freq` · `kr_corpus`(내부 기록용)는 읽지 않는다 — 담당자 실명이 노트에 있다.
- 규제사전 행마다 `근거` 탭의 대표 근거(`is_primary=Y` · `rg_id` 포함 · `evidence_id` 일치)를 찾아 `quote` · `verified_at`을 붙이고 `article` · `url`이 같은지 검사한다. 보류 탭 · 백업 탭은 읽지 않는다.
- 다중값 `; `는 배열로. 열 이름은 원본 그대로(별칭) — DB 컬럼명으로 바꾸지 않는다(`docs/ai/db-map.md` 3.7의 원본 열 → DB 열 매핑은 BE 저장 설계다. 이 묶음과 DB 적재본의 변환 · 인계 계약은 `open-questions.md` #71).
- `policy_rules.json`은 xlsx에 없다. `build_dict.RULES_TEMPLATE`(설명서 §3 · §4 기반 잠정 매핑 · 규제 분류 변환 · `uncertain_bucket`)에서 만들고 **`overrides`는 빈 목록**이다. 실제 예외 쌍은 데이터 담당 확인 후에만 사람이 채운다(도구가 채우지 않는다).

## 버전 세 가지

| 것 | 의미 | 예 |
|---|---|---|
| `schema_version` | 정규화 JSON 구조 버전(`dictionary.DICT_SCHEMA_VERSION`) | `"1"` |
| `dictionary_version` · `rules_version` | 이번에 만든 데이터 묶음의 고유 식별자(`--date` · `--seq`). 같은 이름으로 내용이 바뀌지 않게 파일 SHA-256을 실행 기록에 함께 남긴다 | `regulation@2026-09-28.1` |
| `source.claimed_version` · `version_status` · `version_note` | 원본이 주장하는 버전과 그 확인 상태. 불일치는 식별자가 아니라 여기에 적는다 | `v5 (대조일 2026-09-22)` · `mismatch_pending` · "설명서는 v7…" |

## 묶음 보관과 재현성

- 정규화 JSON은 git 밖이므로 이력은 **묶음 보관**으로 대신한다: 출력 폴더(JSON 3개 + `SHA256SUMS`)를 통째로 `merge-input-v1`처럼 별도 보관하고(`docs/ai/dev.md` 5절 고정 입력본 규칙), 다른 PC에 옮긴 뒤 `verify`로 해시를 대조한다.
- 실행 기록 · `event_log.payload`에는 `dictionary_version` + 파일 SHA-256(`Dictionaries.fingerprint`) + `rules_version`을 남긴다. 어떤 실행이 어떤 묶음을 썼는지 해시로 추적한다.
- 사전이 바뀌면 `--seq`를 올려 새 묶음을 만들고 `diff`로 바뀐 열의 종류(패턴 / 맥락 / 정책 필드 / 규칙)를 확인한다. 어느 단계를 다시 돌릴지는 설계 1절 재실행 조건표(#49).

## 운영 주의 — 전달본 반영과 DB 적재본

- DB 적재 성공이나 BE 로더의 한국어 경고 해소는 번역 사용 가능을 보장하지 않는다.
- 자리표시자가 있는 대체 표현(RG-021 · RG-022)은 템플릿으로 취급한다. 최종 출력에는 검증값 치환과 원본 reason에 명시된 적용 조건 확인이 필요하다(`docs/ai/open-questions.md` #56 · #70).
- 새 전달본을 반영할 때는 원본 파일 SHA-256(전체 값은 묶음의 `source.sha256`) · 원본이 주장하는 파일 버전(`source.claimed_version` · `version_status`) · 묶음 식별자(`dictionary_version` · `rules_version`)를 기록한다. 원본 행별 `version` 열은 읽지 않으므로 묶음에 남지 않는다 — 파일 버전과 행별 버전은 다른 정보다.
- 기존 실측 묶음은 덮어쓰지 않고 보존한다(`--date` · `--seq`로 새 묶음을 만든다).
- DB 적재본(BE `expression_dictionary`)과 이 폴더의 정규화 묶음의 대응 · 버전 식별 방식은 합의 전까지 확인 필요다(#49 · #71). 이 폴더의 스키마를 BE 저장 계약으로 쓰지 않는다.

## 두 뷰

`dictionary.load_dictionaries(dir)` → `Dictionaries`. `judge_view()`는 항목 ID · 이름 · 한국어 패턴 · 맥락 2열만 노출하고(③-1은 정책 필드를 읽지 않는다), `policy_view()`는 전체 항목과 규칙을 노출한다. 테스트(`tests/test_pipeline_dictionary.py`)가 이 경계를 고정한다.

## 현재 실제 묶음(2026-09-28, 로컬)

`local@2026-09-28.1` 8행 · `regulation@2026-09-28.1` 16행(cosmetic 6 · otc 10, allowed 2 · conditional 4 · rewritable 2 · regulated 8) · `policy@2026-09-28.1`. 원본 SHA-256과 파일 해시는 묶음의 `source.sha256` · `SHA256SUMS`에 있다. 현지부적합 버전 표기 불일치(#50)는 생성 시점에 `version_status=mismatch_pending`으로 기록했다. 2026-09-29 데이터 담당 확인으로 전달본은 **v5**가 맞다 — 현재 묶음은 실측 기록이 참조하므로 다시 만들지 않고, 다음 묶음부터 `--local-version-status as_claimed`로 만든다. 원본이 주장하는 버전(v5) · `dictionary_version` · 파일 해시는 서로 다른 식별 정보다. 원본 `근거` 탭의 모노그래프 행은 `verified_at`이 비어 있어 해당 `evidence.verified_at`이 `null`이다(설명서 §9 데이터 담당 정리 항목).
