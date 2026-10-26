# status.md — 구현 현황

> 성격: **살아 있는 문서.** 단계의 구현 상태가 바뀔 때만 1절 표의 해당 행과 2절을 고친다. 갱신 조건은 `README.md` 6.2 — 코드 검토 · 질문 답변 · 문서 대조 · 실험 중간 결과로는 갱신하지 않고, 세션 로그를 쓰지 않는다.
> 여기에는 "어디까지 됐고 다음이 무엇인지"만 적는다. 규칙은 `pipeline.md`, 계약은 `contract.md`, 구조는 `dev.md`, 미결은 `open-questions.md`, 실측 · 검수 기록은 `docs/ai-experiments/`에 둔다.
> 상태값: `미착수` / `뼈대` 배선만 / `구현 중` / `로컬 검증` 샘플 통과 / `GPU 실측` / `확정` 사용자 확정. **BE 연결**은 BE 워커(`app/`)가 그 단계의 AI 함수를 부르는지다 — 운영 품질이나 실측 완료를 뜻하지 않는다.
> 이전 원문(단계별 경위 · 세션 로그 · 당시 인수인계 메모): `archive/status-v1-log.md` — 기본으로 읽지 않는다.

---

## 1. 단계별 현황 — 2026-10-21

기준: `develop` `b62317a` 코드 정적 대조에 `release/1.0.0` `0716d57` 역병합분(⑦ `em_ratio` 잠정값 · 렌더의 역할별 기본값 사용 · `analyze()` 진행 보고 `progress` 연결)의 코드 정적 대조를 더했다. 실행 검증은 아니다. ④⑤ 판정 조합의 `is_excluded` 3값 OR 생성식(`open-questions.md` 5절 #63, 통합 D1)은 마이그레이션 0007로 DB에 반영돼 있다. 운영 처리 경로는 2026-10-13 운영 환경에서 합성 입력 1건으로 N2→N6 처리 성공을 확인했다(실제 Gemini · 학교 V100 LaMa, 렌더 시각 품질은 미달 — `deploy/integration-verification-2026-10-13.md`). 처리 성공은 품질 승인이 아니다.

| 단계 | 상태 | BE 연결 | 남은 큰 과업 | 기록 |
|---|---|---|---|---|
| ① 섹션 분해 | 로컬 검증 · 실측 5차(34장, 기본값 3개 확정) | 연결(`analyze()`) | 후속 개선 #27~#30 · `section.prompt_path` 기준 경로 통일(#41 L12) | `open-questions.md` #26 |
| ② 텍스트 추출 | 육안 확인(v1 100섹션, 정량 기준선 보류) · 타일 OCR GPU 실측(골든 9장, 기준값 잠정) | 연결(`analyze()`) | 품질 표본 정답 동결 → 기준선 측정 · 타일 기준값 확정(#31 · #32) · #33 나머지 분류 | `docs/ai-experiments/2026-09-25_02-ocr_visual-check.md` · `2026-10-13_02-ocr_tile-check.md` |
| ③ 병합 · 역할 | 로컬 검증 · `llm_assist` 실측 1회(100섹션, 큰 결함 없음). OCR 후처리(#86: 여백 분리 · 세로 중심 줄 정렬 · 전각 변환) 구현 · AI 내부 단위 테스트 · LLM 없는 실행 진단(`merge_post_debug/`) · 교차 블록 병합 점검 1회 · 켬/끔 연결 검증 1회(고정 입력본 100섹션, LLM 호출 없음 — 불변식 위반 0, 결함 후보 2건) · 사람 검수 정성 의견 반영(2026-10-23 — 위첨자 · 각주 기호와 곡선 배치 글자 순서 개선 의견, 제품 라벨 사례 미평가 · 건수 미집계, 활성화 승인 아님) · 4단계 품질 · 파라미터 확인 마침(2026-10-26 — 높이비 해제 · LLM 병합 경계 칸 묶음 채택, 활성화 승인 아님), 스위치 기본 꺼짐 | 연결(`analyze()`, LLM 기본 사용). 후처리는 꺼져 있어 BE에 보이는 동작 변화 없음(설정 지문만 변경, `be-handoff.md` 10절) | LLM 실패 처리(#37) · BE 확인(#36 · #38 H) · #39 · #40 · OCR 후처리 보류 기능 실험 · 활성화 결정(#86) | `docs/ai-experiments/2026-09-28_03-merge_llm-assist-check.md` |
| ③-1 · ③-1' 판정 · 정책 | 개발용 v1 실측 1회(100섹션) · 운영 인계 `run_judgment` 구현 | 연결(`HandoffJudge`) | 판정 방식 최종 채택(#5) · 한국어 매칭 경계(#42) · 데이터 담당 확인(#50~#59) | `docs/ai-experiments/2026-09-29_03-1-judge_run-v1.md` |
| ④ 제품 라벨 | 로컬 검증 · 실측 1회(100섹션) · 사용자 경계 검수(미탐 3블록 수정 보류) | 연결(`PipelineLabeler`) | 미탐 개선(#67) · 운영 시간 제한 · 재시도 · 오류 코드(#65) | `docs/ai-experiments/2026-09-29_04-label_run-v1.md` |
| ⑤ 브랜드 로고 | 로컬 검증 · 실측 2회 · 주요 사례 사용자 검수 | 연결(`PipelineLogo`) | 브랜드 부재 · 미확인 운영 정책(#68) · 병합 로고 한계(#69) | `docs/ai-experiments/2026-09-30_05-logo_rerun-brand-v2.md` |
| ⑥ 인페인팅 | GPU 실측(100섹션) · 사용자 육안 검수(현 수준 유지, 결함 기록) | 연결(GPU 워커 · `GpuInpaintRunner`) | 고도화(#73) · 운영 시간 제한(#75) · GPU 제어 HTTP 바인딩(BE, `open-questions.md` 0.1) | `docs/ai-experiments/2026-09-30_06-inpaint_run100-v1.md` |
| ⑦ 스타일 추출 | 로컬 검증 · 소수 표본(3섹션) 사용자 검수 · `em_ratio` 잠정값 적용(#85) | 연결(`PipelineStyler`). 렌더는 측정 NULL 항목과 ⑦ 실패에 역할별 기본값을 쓴다 | 100섹션 실측 · `text_block.style` 키(#14) · `em_ratio` 최종값(#85) | `docs/ai-experiments/2026-10-06_07-style_sample-review.md` · `2026-10-15_07-style_em-ratio.md` |
| ⑧ 번역 · 번역 후 검사 | 구현 · 가짜 모델 계약 검증 | 번역 연결(`HandoffTranslator`). **번역 후 검사(`run_text_check`) 미연결** | 번역 후 검사 BE 연결 · 실제 Gemini 실측 · 프롬프트 기술검증(#11) · 유료 키 확인 · 대안 표기(#77) | `be-handoff.md` 0절 |
| ⑨ 조판 · 렌더 | BE 소관. 역할별 기본값 · Pretendard 역할별 굵기 · 소수 크기 구현 반영 확인(`app/typeset.py`, 렌더러 버전 `pillow-typeset/2`, 코드 정적 대조 · 실행 검증 아님) | 연결(미리보기 · 최종 렌더 공통 로더 · `resolve_block` · 입력 지문) | 자간 미적용 · 행간은 설정 자료 비율이 아닌 구현 상수 1.2 · 넘침 기준 · 적용값 기록 형식(#78) · 운영 엔진 측정 기준(#79) · 6.7 사례 통합 시험 이관(BE 확인) | `be-handoff.md` 6절 |
| 1.0.0 baseline 측정 | 준비 — 평가 명세 · 측정 도구 · 로컬 환경 · 고정 평가셋 연결 준비, 공식 측정 없음 | — | 입력 · 평가 기준 자료와 회차 조건 확인 · 승인된 모델 연결 점검 · 실측 | `docs/ai-baseline/README.md` |
| 타입 · config · CLI · inspect | 로컬 검증 | — | — | `dev.md` |
| 환경 · 의존성 | `requirements-ocr.txt` 버전 고정 · 로컬 OCR(Windows CPU · GPU) 검증. `requirements-gpu.txt`의 OCR 부분은 설치 미검증 | — | `requirements-gpu.txt` 설치 검증 | `dev.md` 6절 |

## 2. 다음 착수

우선순위는 사용자가 정한다. 아래는 현재 기록된 다음 작업이며 순서를 정한 목록이 아니다.

- **③ OCR 후처리(#86)** — 4단계(품질 · 파라미터 확인)를 마쳤다(2026-10-26, `docs/ai-experiments/2026-10-26_03-merge_post-step4-summary.md`). 가로줄 `Line` 나누기의 높이비 해제(2026-10-23)와 LLM 병합 경계의 칸 묶음 확장(2026-10-26)을 채택해 반영했고, 줄 중심 허용치는 0.5 유지(값은 잠정), 전각 변환은 현재 인식 모델에서 효과 없음(모델 변경 시 재평가)으로 정리했다. 알려진 한계: LLM 없는 경로의 과분할, 라벨과 주변 문구의 오병합 가능성(④ 처리 미검증), 후보 영역 안 LLM 병합 감소, OCR 중복 검출은 #40 별도 과제. 다음은 보류 기능(숫자 속 o/O 교정 · 겹친 앞 글자 제거 · 맨 앞 따옴표 제거) 개별 실험(5단계) → 기본 활성화 결정(6단계). 후속 연동 · 배포 준비(보충 프롬프트의 인계 지문 반영, BE 확인 · 연동과 캐시 · 재분석 영향 — `be-handoff.md` 10.2, 상위 문서 반영, PR 전 최신 `develop` 반영 · 회귀 확인)는 이 작업과 분리해 진행한다.
- **1.0.0 baseline 실측 준비** — 입력 · 평가 기준 자료와 회차 조건 확인, 승인된 모델 연결 점검(`docs/ai-baseline/`).
- **BE 연결 남은 것** — 번역 후 검사(`run_text_check`) · GPU 제어 HTTP 바인딩 · 대체 표현 구분 규칙(#71) 확정 뒤 변환(`be-handoff.md` 0절) · 렌더 자간 · 행간 · 넘침 기준 · 기록 형식(⑨ 행, `be-handoff.md` 6.6).
- **⑦ `em_ratio` 최종값(#85)** — 사람 육안 검수로 잠정값을 확인하고 최종값 · 측정 방식을 정한다. 잠정값 반영 전에 ⑦을 실행한 작업은 ⑦을 다시 실행해야 새 크기가 적용된다(BE 공유 여부 확인). 크기를 맞춰도 남는 줄 늘어남 · 겹침(배치 영역 · 줄바꿈)은 조판 과제로 따로 다룬다.

현재 사용할 고정 입력본은 아래와 같다.

### ③ 개발용 고정 입력본

- 입력본: `merge-input-v1` — 사용자 생성·수령 및 로컬 배치 완료(2026-09-26).
- 로컬 위치: `pipeline/samples/local/merge-input-v1/`(Git 제외). 다른 PC에서는 폴더 전체를 확보해 같은 위치에 배치한다. 공유·백업 위치는 미확인이다.
- 실제 구성: 원본별 폴더 32개 · 섹션 이미지 110개 · OCR JSON 100개 · OCR 실행 기록 100개 · OCR 영역 1,815개. 상세 출처·환경·검수 수준은 입력본 내부 `README.md`, 포함/제외 목록은 `MANIFEST.json`을 따른다.
- 제외: 품질 표본 12개. 그중 10개는 섹션 연속성 검증을 위해 이미지만 남고 OCR은 없다. 나머지 2개는 원본 폴더 자체가 제외됐다. ③에서는 `MANIFEST.json`의 원본별 `ocr_included`만 읽고 `split.sections` 전체를 OCR 대상으로 순회하지 않는다.
- 수령본 검증: `SHA256SUMS`에 기록된 539개 파일 모두 해시 일치 · 누락 0. 목록 밖 `desktop.ini` 225개가 있어 동봉 검증기를 수령 폴더 그대로 실행하면 파일 집합 검사에서 실패한다. 입력본 원본은 수정하지 않았다.
- 이동 검증: `desktop.ini`만 제외한 레포 밖 임시 복사본에서 동봉 `tools/verify_merge_input.py`의 검증 함수 통과. 섹션 110개 이미지 크기·내부 경로·연속성·해시, OCR 100개 스키마·식별자·좌표 범위·실행 기록·입력 이미지 해시 대응, 포함/제외 목록 및 전체 파일 해시를 확인했다. 데이터 무결성 확인이며 OCR 정량 품질 승인은 아니다.
- 사용: 내부 README의 예시 ROOT를 위 로컬 위치로 바꾸어 실행한다(README의 `pipeline/out/merge-input-v1`은 생성 PC 위치). `ocr_run`의 옛 입력 경로는 출처 기록이며 현재 입력 경로로 사용하지 않는다. 다음 단계 출력은 `pipeline/out/` 아래 새 폴더에 저장한다. 재검증 시 목록 밖 `desktop.ini`로 인한 엄격 검사 실패와 실제 데이터 오류를 구분한다.

### ③ 이후 단계 고정 입력본

- 입력본: `blocks-input-v1` — 2026-09-28 생성(AI). ③-1 · ④ · ⑤ · ⑥ · ⑦ 개발용 입력(③ 블록 + 섹션 이미지 + OCR 원시 영역). 정답 데이터가 아니라 ③ `llm_assist` v1 실제 출력 1회분이다.
- 로컬 위치: `pipeline/samples/local/blocks-input-v1/`(Git 제외, 약 66MB). 다른 PC에서는 폴더 전체를 같은 위치에 복사한다. 공유 · 백업 위치는 미정이다.
- 구성: `merge-input-v1` 전체(바이트 그대로, 최상위 파일은 `upstream_merge_input_v1/`) + 섹션마다 `merge/` · `merge_debug/`(재생 가능한 LLM 기록) · `merge_run/`(실행 기록). 이미지 32 · ③ 결과 섹션 100(`ok` 99 · `skipped` 1) · 블록 948. `SHA256SUMS` 844개 파일(그 자체의 SHA-256 `f54ea91b7b39813819212c91ed63cae3a88ac15e6b4e9400cfceb989e2433af9`).
- 출처: `pipeline/out/merge-llm-v1_2026-09-28/`(커밋 `d3016ca`). 1회 실측에서 시간 초과였던 GS-01_002/sec_1_04만 같은 코드 · 설정으로 1회 재실행해 포함 — 휴리스틱 대체 없음.
- 검증: 동봉 `tools/verify_blocks_input.py --relocated` 오류 0(해시 · 섹션 이미지 · 파일 집합 · OCR 영역이 블록에서 정확히 한 번 · 상태 · `--llm-replay` 재조립 100개 바이트 일치). 재생 검사는 현재 레포의 코드 · 프롬프트를 쓰므로 ③ 코드가 바뀐 뒤의 불일치는 입력본 손상이 아니다.
- 알려진 한계(입력본 `README.md` 4절): 휴리스틱 과병합 27건 · LLM 새 과병합 3건이 남아 있고 1회 실행분이다.

### ⑤ 개발용 고정 입력본

- 입력본: `logo-input-v1` — 2026-09-29 생성(AI). ⑤ 브랜드 로고 제외 단독 개발용 입력(③ 블록 + ④ 라벨 판정 + 섹션 이미지 + OCR 원시 영역). **정답 데이터가 아니라** ④ 단독 개발 v1 실측 1회의 실제 출력이다. 개발 대상 100섹션은 사용자 최종 포함 목록이 아니다.
- 로컬 위치: `pipeline/samples/local/logo-input-v1/`(Git 제외, 약 68MB, 파일 1,056개 = 해시 목록 1,055 + `SHA256SUMS`). 다른 PC에서는 폴더 전체를 같은 위치에 복사하고 검증한다.
- 구성: `blocks-input-v1` 전체(바이트 그대로, 최상위 파일은 `upstream_blocks_input_v1/`) + 원본별 `label/`(LabelResult) · `label_debug/`(재생 기록) + `label_run/`(실측 manifest · sections · summary 사본, 당시 프롬프트 `prompt/label.md`, 실험 기록 · 검수 명세 사본) + `tools/`(1회용 생성 · 검증 스크립트) + `README.md` · `MANIFEST.json` · `SHA256SUMS`. 이미지 32 · 섹션 100 · 블록 948 = true 232 · VLM false 672 · 공백 false 44(호출 99 · 호출 없음 1).
- 출처: `pipeline/out/label-run-v1_2026-09-29/`. 실측 코드는 기준 커밋 `afc046d` 위의 **미커밋 구현**(`git_dirty` true) — 이후 커밋을 실행 커밋으로 적지 않는다. 입력본 생성 · 검증 코드 커밋 `d9e31c5`. 설정 `gemini-3.8-flash` · 긴 변 1024 · `bias=label` · temperature 0 · 60초, 프롬프트 SHA-256 `a31ffec4…`, `google-genai` 2.24.0(1회 시도). 실행 기록 속 옛 경로는 출처로만 보존하고 로드는 입력본 안 상대 경로(`split.json` `image_path` 등)로 한다.
- 검증(`python pipeline/samples/local/logo-input-v1/tools/verify_logo_input.py pipeline/samples/local/logo-input-v1 --relocated`, 원본 대조는 `--sources <blocks-input-v1> <label-run 폴더>`): 오류 0 — 전체 해시 · blocks-input-v1 복사 일치 · 블록 ↔ 판정 일대일 · 공백 규칙 · 분포 · 입력 지문 · 이미지 경로 · 크기 · bbox · 저장 응답 100개 API 없는 재생 100/100 동일 · 레포 밖 이동 로드. 원본 blocks-input-v1 · 실측 출력 해시는 작업 전후 불변. 반대 검사(판정 뒤집기 · 판정 파일 삭제)는 실패로 잡힘. 재생은 현재 레포의 ④ 코드를 쓰므로 코드가 바뀐 뒤 불일치는 입력본 손상이 아니다.
- 알려진 한계: 사용자 확인 미탐 3블록(GS-03_018/sec_1_08/blk_022 반사상 · GS-02_001/sec_1_02/blk_002 · blk_004 기프트카드)은 **false 그대로** 보존 — 기대값 true는 README · MANIFEST `user_review`의 검수 기록일 뿐(#67). ⑤ 결과에서 이 블록은 ④ 미탐의 영향으로 구분한다. 혼합 블록 실데이터 미검증 · OCR이 놓친 패키지 글자는 블록 없음 · ③ 과병합 한계는 `blocks-input-v1`과 같다.
- **브랜드 메타데이터 — 현재 `brand-meta-v2`(2026-09-30, 사용자 확인 정정판)**: 위치 `pipeline/samples/local/brand-meta-v2/`(Git 제외) — `brand_metadata.json`(`242c64f4…82c1`) · `README.md` · `SHA256SUMS`(자신의 SHA-256 `f9ccc6b4866b79f4f556465a6f893961bd1899e6d4a83b7fde5304013420c989`). v1 대비 GS-03 `name_en` `b.clinix` → `b.clinicx`만 바뀌었고(사용자 육안 검수 확인) 원본 연결 · 값 상태 · 연결 입력본(logo-input-v1)은 같다. v1은 보존하며 v1 실측의 입력 기록이다.
- **(이전) 브랜드 메타데이터 — 별도 자료 `brand-meta-v1`로 확보(2026-09-29, 사용자 제공)**: 위치 `pipeline/samples/local/brand-meta-v1/`(Git 제외) — `brand_metadata.json` · `README.md` · `SHA256SUMS`(그 자체의 SHA-256 `ab5ff003f25c1803e850cda74ddc7ff2e4e9cd74a05ebb7e25978fc7e375199f`, `brand_metadata.json` `59f085cd…6569`). 사용자가 직접 확인한 상품 그룹 3개(GS-01 구달/goodal · GS-02 셀리맥스/celimax · GS-03 비클리닉스/b.clinix)를 입력 그대로 저장했다 — 추정 · 번역 · 별칭 없음, 상품명은 추적용. `GS-0N`은 `GS-0N_` 접두 원본 전체로 풀었고 logo-input-v1 개발 대상 32개 원본이 누락 · 중복 · 상충 없이 연결된다(제외 원본 GS-02_002 · GS-03_016은 목록 밖 기록). 값 상태는 `provided` / `absent`(사용자 "없음") / `unconfirmed`(사용자 "미확인")로 두고 뒤의 둘은 null — 현재 6개 모두 `provided`. 브랜드 부재 · 미확인 시 판정 정책은 정하지 않았다. logo-input-v1은 수정하지 않았으므로 그 `MANIFEST.json` `brand_metadata.status = not_available`은 생성 시점 기록으로 남는다. ⑤ 실행 기록에는 logo-input-v1(`def7e41b…7070`)과 brand-meta-v1의 `SHA256SUMS` 해시를 함께 남긴다. 레포에는 DB `brand` 테이블 정의만 있고 시드 없음(이전 확인 그대로).
- 별도 보관본: **미확보**. G 드라이브 `최종프로젝트/실측 결과/`에는 `merge-input-v1`(폴더 · zip)만 있고 `blocks-input-v1` · `logo-input-v1` 보관본은 없다. Git 제외는 백업이 아니다.

### ⑤ 이후 하류 단계 고정 입력본

- 입력본: `downstream-input-v1` — 2026-09-30 생성(AI). ④ · ⑤ 제외 판정까지 끝난 하류 단계(⑥ · ⑦ · ⑧ 등) 공용 입력 — ③ 블록 + ④ 라벨 판정 + ⑤ 로고 판정(채택 실행) + 섹션 이미지 + OCR 원시 영역 + 활성 브랜드 자료. **정답 데이터가 아니며** 개발 대상 100섹션은 사용자 최종 포함 목록이 아니다.
- 로컬 위치: `pipeline/samples/local/downstream-input-v1/`(Git 제외, 68,953,116바이트 ≈ 69MB, 파일 1,269개 = 해시 목록 1,268 + `SHA256SUMS`). `SHA256SUMS`의 SHA-256 `b56cb929b2a235098a956b21d8e41701fb2a95e828114c1c2bba7aea91bdf975`.
- 구성: `logo-input-v1` 전체(바이트 그대로 — 원본 폴더 32개는 같은 경로, 최상위 README · MANIFEST · SHA256SUMS · tools · label_run · upstream_blocks_input_v1은 `upstream_logo_input_v1/`) + 원본별 `logo/`(LogoResult) · `logo_debug/`(채택 실행 `pipeline/out/logo-run-v2_2026-09-30/` 바이트 그대로) + `brand_meta_v2/`(활성 브랜드 자료 전체) + `logo_run/`(채택 · 재실행 `run.json` 사본, `rerun_equivalence.json` 섹션 100개 해시 대조, 실험 기록 사본 2개) + `tools/`(1회용 생성 · 검증) + `README.md` · `MANIFEST.json` · `SHA256SUMS`.
- 원천 · 버전: logo-input-v1 `def7e41b…7070` · **활성 브랜드 `brand-meta-v2`** `f9ccc6b4…c989` · ⑤ 채택 실행 `run.json` `e9ddfe0e…f3ca`(판정 실행 커밋 **`33a7cc7`**, 당시 `git_dirty=true` — 추적 변경은 문서 2개뿐) · 재실행 `run.json` `6be48921…3d49`(파일 미포함, 100/100 동일 해시). 묶음 생성 커밋 `c9315b2`(추적 변경 없음)는 판정 실행 커밋이 아니다. `brand-meta-v1` · v1 실측은 비교 이력이며 묶지 않았다(원래 위치 보존).
- 규모 · 분포: 32원본 · 100섹션 · 948블록 = `product_label` 232(null) · `empty_text` 46 · `exact_match` 4 · `no_match` 666. 사용자 확인 7블록(`MANIFEST.json` `user_review`, 주요 사례 한정 · 전수 승인 아님)과 이어받는 한계(④ 미탐 3블록 #67 · ③ 병합 · OCR · 병합 로고 #69)를 기록했다.
- 검증(`python pipeline/samples/local/downstream-input-v1/tools/verify_downstream_input.py pipeline/samples/local/downstream-input-v1 --relocated`, 원천 대조 `--sources <logo-input-v1> <brand-meta-v2> <logo-run-v2> <logo-run-v2_rerun>`, 오류 주입 `--fault-tests`, 모델 호출 없음): 제자리(원천 대조 포함) · 레포 밖 이동 모두 **오류 0** — 최상위 파일 집합 = 해시 목록(하위 SHA256SUMS 포함) · logo-input-v1 · brand-meta-v2 · 채택/재실행 결과 바이트 일치 · MANIFEST 원천 해시 · 브랜드 `for_input` · run.json 입력 해시 · 판정 실행 커밋 기록 · 32원본 브랜드 그룹 연결 · merge = label = label_debug = logo = logo_debug 파일 집합 · ③ ↔ ④ ↔ ⑤ 일대일(순서) · 입력 지문 · ④ true ⇔ ⑤ product_label/null · 분포(섹션 · 합계 · run.json) · 이미지 경로가 묶음 안 · 크기 · **묶음 안 입력만으로 ⑤ 메모리 재계산 100/100 결과 · 상세 기록 동일** · 사용자 확인 7블록 · 재실행 동일성 100/100. 오류 주입(임시 사본): 판정 파일 삭제 · 해시 불일치 · 브랜드 연결 오류 모두 검출. 검증 전후 묶음 파일 해시 불변, 원천(입력본 · 브랜드 v1/v2 · 실측 4폴더) 2,093개 파일 해시 불변. 재계산은 현재 레포의 `logo.py`를 쓰므로 코드가 바뀐 뒤 불일치는 묶음 손상이 아니다.
- 사용: 순회는 `MANIFEST.json` `images[].sections`만(split.sections 전체 아님), 연결 키는 원본 식별자 + `section_key` + `block_key`, 사용법 · 경로 구분은 묶음 `README.md`. 다른 PC에서는 폴더 전체를 같은 위치에 복사하고 위 검증을 돌린다. 하류 단계 출력은 `pipeline/out/` 새 폴더.
- 별도 보관본: **미확보**. G 드라이브 `최종프로젝트/실측 결과/`에는 `logo-input-v1.zip` · `merge-input-v1`(폴더 · zip) · `ocr_input_v1.zip` 등이 있으나 `downstream-input-v1` · `brand-meta-v2`는 없다. Git 제외 · 임시 이동 검증은 백업이 아니다.

## 3. 세션 로그

쓰지 않는다(`README.md` 6.2). 작업 경위는 커밋 · PR 본문에 남긴다. 2026-10-21 이전 로그는 `archive/status-v1-log.md` 3절.

## 4. 인수인계 메모

현재도 유효한 참고 사항만 둔다. 당시 상태를 적은 이전 메모는 `archive/status-v1-log.md` 4절.

- `test_sample_generator_is_reproducible`(샘플 생성기 재현성)는 이 브랜치 이전부터 있던 환경 의존성이다. `make_synthetic._font()`가 DejaVuSans를 못 찾으면 Pillow 기본 폰트(Aileron)로 대체돼 글자 bbox가 달라진다. Windows 등 DejaVu가 없는 PC에서 실패하며, 기대 좌표를 그 PC 기준으로 덮어쓰지 않는다. 해결은 폰트 파일을 레포에 동봉하거나 테스트를 DejaVu 있을 때만 돌리는 것 중 택일 — 미결정.
- `synthetic_01`은 영문 합성 이미지다. 한국어 OCR·역할 판정의 실제 검증은 `samples/local/` 표본으로 한다.
- ① 섹션 분해의 실측 기록(1~4차 보고서 · 정답 목록 `truth.json` · 드라이버·분석 스크립트)은 레포 밖(PoC 레포 · 로컬 스크래치패드)에 있다. `pipeline/out/`은 git 제외. 실측을 이어 가려면 `samples/local/` 34장과 `truth.json`이 필요하다.
- **OCR 고정 입력 v1 보관 방식**(5차 실측 후 결정, 레포 밖): `pipeline/out/r5/full/`과 GS-02_014의 세 실행(`_run1`·`_run2`·`_run3`)은 보고서가 참조하므로 그대로 보존하고 덮어쓰지 않는다. 별도 폴더(v1)에 33장은 r5/full, GS-02_014는 **run3**(3회 중 정답 2891을 유지한 실행)을 복사하고, 출처(실행 디렉터리 · 회차 · 선택 기준 · 실행 커밋 `6d09dfb`)를 v1 안에 기록한다. 생성은 `freeze-input`으로 한다(2026-09-23): r5/full과 run3의 `split.json`은 버전 1이므로 `--base <레포 루트>`를 주고, 출처는 `--meta`로 남긴다. 이미지 복사·경로 재지정·검증(입력본 내부 참조 · 해시 · 연속성 · 레포 밖 사본 검증 · 원본 불변)은 도구가 한다. 이 선택은 OCR 개발용이며 ① 성능의 근거로 쓰지 않는다. 규칙은 `dev.md` 5절.
- **② 기준선 측정 환경(2026-09-25)**: 이 PC의 GPU 가상환경(`paddlepaddle-gpu` 3.3.1 · CUDA 11.8)에서 잰다. CPU 환경 결과와는 프로브 4개 섹션에서 거의 같았지만(`dev.md` 6절) 기준선끼리 비교할 때는 같은 환경의 결과만 쓴다.
- **OCR 고정 입력 v1 현황(2026-09-23)**: 실측 PC에서 생성(`--meta`로 출처 기록, GS-02_014는 run3) 후 zip으로 옮겨 `pipeline/out/ocr_input/v1/`(git 제외)에 풀었다. 34장 · 섹션 112 · 높이 201~3,470px · 폭 800/830/1000. **4,000px 초과 섹션이 없어** 임시 분할은 v1로 실측 검증할 수 없다.
- **품질 표본 12개(2026-09-23 확정)**: GS-02_014/09 · GS-01_002/16 · GS-01_002/17 · GS-03_002/04 · GS-02_001/04 · GS-03_018/11 · GS-01_002/15 · GS-02_007/02 · GS-02_001/03 · GS-01_002/05 · GS-02_002/01 · GS-03_016/01 (유형·선정 이유는 1a 보고서 3절). 4,000px 초과 유형은 해당 섹션 없음. 1a 프로브 섹션(GS-02_003/01 · GS-03_005/01 · GS-02_014/04 · GS-02_009/06)과 겹치지 않는다.  1.0.0 baseline에 한해 정답 동결 전 실행 제한을 해제했다(`docs/ai-baseline/decisions.md`).
- **OCR 정답 초안 현황(2026-09-25)**: 12개 모두 Gemini(`gemini-3.8-flash`) 초안 369줄, 검수 0. 명백한 초안 오류 예: GS-02_002/01 L005 · L006은 좌표 숫자를 텍스트로 지어낸 것 — 전수 대조 필수. 도구 · 검수 규칙은 `dev.md` 5절.
