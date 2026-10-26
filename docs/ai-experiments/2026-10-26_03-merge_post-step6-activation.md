# ③ OCR 후처리 6단계 — 스위치별 기본 활성화 결정(2026-10-26)

## 1. 결정

사용자 결정(2026-10-26 채팅): 보완한 안대로 기본값을 `xycut=true` · `num_o=true` · `fullwidth=false`로 바꾼다(`open-questions.md` #86). 추가 OCR 실행 · 유료 LLM 호출 없이 기존 실험 기록과 로컬 검증으로 결정했다.

| 단계 | `merge.xycut` | `merge.num_o` | `merge.fullwidth` |
|---|---|---|---|
| 기능 채택 | 2026-10-21 · 22. 높이비 해제 10-23, 칸 묶음 LLM 병합 10-26 | 2026-10-26, 경계 규칙을 좁힘 | 2026-10-21 |
| **기본 활성화(이번)** | **켬** | **켬** — 아래 위험 수용 | **끔** |
| 실제 운영 적용 | 후속 연동 뒤(3절 조건) | 같음 | — |

## 2. 스위치별 근거 · 남은 위험 · 검증하지 않은 범위

### `merge.xycut` — 켬

- **근거**
  - 다단 배치의 읽기 순서를 바로잡는다(켬/끔 비교).
  - 가로줄의 높이비 해제는 사람 검수 13건 모두 나았다(4단계 1차).
  - 칸 묶음 LLM 병합은 이어져야 할 6건을 합쳤고, 사람이 받아들였으며, 3회 실행 모두 같았다(4 · 5차).
  - 새 59섹션에서 영역을 넘은 병합은 라벨 1블록뿐이었다.
  - 운영 코드 = 실험 patch(기록 재생 110건 동일)다.
- **남은 위험(알려진 한계로 수용)**
  - LLM 없는 경로의 과분할
  - 라벨과 주변 문구의 오병합(5차 rep3 1회)
  - 후보 영역 안 LLM 병합 감소
  - LLM 응답의 실행 변동
- **검증하지 않은 범위**
  - 고정 입력본 100섹션(3개 이미지 묶음) 밖의 이미지
  - xycut을 켠 결과로 ③-1 ~ ⑧ 하류 실행
  - ④가 잘못 합쳐진 라벨을 처리하는 방식
  - 4회 이상의 반복 실행

### `merge.num_o` — 켬(위험 수용)

- **근거**: 고정 입력본에서 규칙이 고친 8건이 모두 사람 검수에서 교정 맞음이었다(번역 대상 2 · 그 밖 6). 1:1 치환이며 블록 구성 · 역할 변화는 0이다.
- **남은 위험**: 확인된 개선은 작고, 정상 코드 훼손 위험은 남아 있다.
  - 띄어 쓴 코드 · 모델 번호가 바뀐다: `Model 4O` → `Model 40`, `4O ml` → `40 ml`. 글자만으로는 실제 숫자 0인지 영문 O인지 가를 수 없다.
  - 이런 훼손의 발생 빈도와 영향은 모른다.
  - `source_ko`가 원시 OCR과 달라져 ⑤ 매칭 · 편집 화면 원문 표시가 바뀔 수 있다.
  - **이 위험을 수용하고 켬을 권했고, 사용자가 결정했다.**
- **검증하지 않은 범위**: 표본 8건 밖의 빈도, 하류 영향.

### `merge.fullwidth` — 끔

- 현재 인식 모델(`korean_PP-OCRv5_mobile_rec`)의 문자 사전에 전각 문자가 없어, 100섹션에서 켜도 바뀌는 섹션이 0이다.
- 켜면 설정 지문만 바뀌어 불필요한 재분석이 생긴다.
- 인식 모델을 바꿀 때 다시 평가한다(4단계 결론).

## 3. 기본값 변경과 운영 적용의 분리 — 후속 적용 조건

- 기본값을 바꾸면 그 설정을 쓰는 실행은 바로 달라진다.
  - BE(`app/ai_adapters.py`)는 `PIXLATE_PIPELINE_CONFIG`가 없으면 저장소 `pipeline/config/default.toml`을 그대로 쓴다.
  - develop에는 머지 시 dev 이미지를 게시하는 CI가 있다(`5d676f4`).
- 따라서 아래 후속 연동이 끝나기 전에는 다음 중 하나를 지킨다. 이번에 BE 작업 · 요청은 하지 않았다.
  - **(가)** 이 변경의 develop 머지 · 운영 배포를 보류한다.
  - **(나)** 운영 설정에서 두 스위치를 명시적으로 끈다. `PIXLATE_PIPELINE_CONFIG`로 지정한 설정 사본을 쓰며, 저장소 설정과의 어긋남을 관리해야 한다.
- 후속 연동(완료 조건이 아니라 운영 적용 조건):
  - 보충 프롬프트(`merge_assist_region.md`)를 인계 입력 지문에 포함
  - BE 확인 · 연동: `be-handoff.md` 10.2 항목과 캐시 · 재분석 영향
  - 상위 문서 반영(`open-questions.md` 3절 F)
  - PR 전 최신 `develop` 반영과 회귀 확인

## 4. 함께 정리한 것(미커밋)

| 대상 | 내용 |
|---|---|
| 설정 | `pipeline/config/default.toml` `xycut = true` · `num_o = true` · `fullwidth = false`, 주석에 3절 조건 |
| 기본 경로 테스트 | 기본값 테스트를 새 기본값으로 바꿨다. CLI · `analyze` 진단 테스트에 "명시적으로 끔 → 진단 없음"과 "기본값 → 진단 있음"을 둘 다 넣었다. 기본 설정 그대로 LLM 경로(숫자 교정된 글자 · `region` · 보충 프롬프트 · 기록)를 확인하는 테스트를 새로 넣었다 |
| 꺼진 경로 테스트 | `tests/test_pipeline_merge.py` · `test_pipeline_merge_llm.py`의 `CFG()`가 세 스위치를 모두 끈다(기존 heuristic_v2 · llm_assist v1 회귀). CLI 재생 테스트도 같은 설정으로 재생한다. `test_pipeline_merge_post.py`에서 끈 결과와 비교하거나 끈 상태 기록을 보는 테스트는 `OFF`(세 스위치 끔)를 명시한다 |
| 실험 도구 | `merge_post_compare.py` · `merge_post_gutter_check.py` · `merge_post_step4.py`(`base_cfg` · `OFF_SETS`) · `merge_post_yonly_replay.py`가 기본값과 무관하게 실험 당시 설정을 명시한다. 1~5차는 `num_o` 없이 돌렸다 |
| 인계 예제 | `pipeline/samples/handoff/analyze.completed*.json` 2개를 다시 만들었다. `input_manifest.config.merge`의 `xycut` · `num_o`가 `true`가 되고 `input_manifest_sha256`이 바뀌었다. 예제의 블록 내용은 그대로다. **BE 확인 필요** |
| 문서 | `pipeline.md` 3 · 7.1절, `open-questions.md` #86 · 3절 F, `status.md`, `be-handoff.md` 10절 |

## 5. 검증(호출 없음)

| 확인 | 결과 |
|---|---|
| `tests/test_pipeline_merge_post.py` · `test_pipeline_merge.py` · `test_pipeline_merge_llm.py` | 277 통과 |
| `tests/test_pipeline_*.py` 전체 | 1,224 통과 · 7 건너뜀 · 1 실패. 실패는 기존 `test_sample_generator_is_reproducible`(변경 전 HEAD에서도 실패, 합성 샘플 OCR 좌표) |
| 실험 재생(기본값 변경 뒤) | 4 · 5차 `B3_yonly` 110건 · 끈 경로 99건 모두 지문 · 결과 같음 — 실험 도구가 기본값과 무관하게 재현된다 |
| 스위치 조합 휴리스틱(100섹션, 변경 전 측정) | 불변식 위반 0. xycut과 num_o를 함께 켜도 블록 구성 · 역할 간섭 0 |

**시간 측정의 범위**: 100섹션 합계 0.1~0.3초는 로컬 휴리스틱 · 문자 교정 처리만 잰 값이다. xycut을 켜면 LLM 입력(`region` · 보충 프롬프트)과 응답이 달라지므로, 전체 파이프라인의 시간 · LLM 비용은 측정하지 않았다.

이 결정으로 하류 실행 · 라벨 처리 · BE 연동을 검증 완료로 표시하지 않는다.
