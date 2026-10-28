# be-handoff.md — BE 워커가 부르는 AI 함수의 입력·출력과 BE 처리(공동 확인용 예제)

> 작성: 2026-10-08, `feature/be-ai-pipeline-integration` · 6절 추가 2026-10-12(D11 역할별 스타일 기본값) · 8절 추가 2026-10-12(D12 번역문 검사 연결) · 10절 추가 2026-10-23(OCR 후처리, BE 확인 미진행). 정의 코드는 `app/ai_adapters.py`(타입)·`app/flows/`(검증·저장)다.
> 성격: BE 구현이 실제로 요구·검증하는 형식의 예제와 기대 결과다. 새 계약 결정이 아니며, 유형 코드·프롬프트·모델은 AI 소유다.
> 모든 키는 BE가 준 임시 키(`sec_<id>`·`blk_<id>`)를 그대로 돌려준다. 모르는 키는 저장 실패다(D3). 파일은 절대 로컬 경로로만 주고받는다.

## 0. AI 인계 계층 연결 — PR #55 경계 채택(2026-10-08 갱신)

[PR #55 BE 확인](https://github.com/ai-pixlate/server/pull/55#issuecomment-6054998455)에 따라 AI↔BE 경계는 PR #55의 `pipeline.handoff`(`StageReport`, 5.24 봉투)로 맞췄다. 아래 2~4절의 `JudgeOutcome`·`TranslateOutcome`·BE 봉투는 **BE 내부 저장 형식**이며, BE 어댑터가 AI 보고를 이 형식으로 옮긴다.

| 단계 | BE가 부르는 AI 함수 | BE 어댑터 | 채택 전 검사 |
|---|---|---|---|
| ①②③ | `pipeline.analyze.analyze(…, fallbacks=)` | `PipelineAnalyzer.analyze_with_fallbacks` — ① 원본 전체 대체 기록을 인계 payload `split_fallbacks`·채택 기록에 보존 | 기존 `_validate_analyze` |
| ③-1·③-1′ | `pipeline.handoff.judgment.run_judgment` (섹션별 시도, `target_section_keys`=대상 섹션, `analyze_result`=분석 실행 전체 문맥) | `HandoffJudge` → `JudgeOutcome` | `validate_report` + 기존 `_persist_judge` 검사(현지 상태와 묶음 일관성 포함) |
| ⑧ | `pipeline.handoff.translation.run_translate` | `HandoffTranslator` → `TranslateOutcome`. 공급은 `context["handoff"]` | `validate_report` + 기존 채택 검사 |
| ⑥ | `pipeline.handoff.gpu.GpuInpaintRunner` (GPU 측) | `BeControlClient`(AI `ControlClient` 구현), 등록은 `WorkerControl.register_report`, 요청 구성은 `inpaint_stage_request` 공용 | 업로드 해시·시도·세대 대조 → 산출물 고정 → 고정 사본으로 `validate_report(local_paths, allowed_root)` → 채택 |

- **사전 묶음**: BE 저장 묶음(PK·근거 배열 포함)에서 `app.dictionary_bundle.to_ai_bundle()`이 PR #55 `3217142` 형식을 결정적으로 만든다. 서비스 `verdict_status`와 원값 `source_verdict_status`를 그대로, 근거는 배열 전체(`document`·`is_primary` 포함, PK 제외)를 보낸다. 대체 표현은 #71 결정 전까지 **나누지 않은 원문 문자열**(없으면 빈 배열) — AI는 "대체 표현 있음"·스냅샷에만 쓰고 ⑧ 표현 지시로 쓰지 않는다. 현지 행 ID 집합이 AI 대응표 8항목과 다르면 공급 불가(`local=null` + 사유)로 기록한다.
- **정책 규칙**: DB에 없다. 저장소 운영 규칙 `pipeline/data/policy_rules.json`(AI 소유, `policy@2026-10-08.1`)을 분석 시작 때 읽어 묶음에 고정하고 지문(`policy_rules_sha256`)도 보존한다. `PIXLATE_POLICY_RULES`는 실험용 대체 경로다. `overrides`가 있거나 `combination→otc`·`unknown→cosmetic`(D8)과 다르면 거부한다. 규제 행 조회 분류는 규칙의 `applied_classes`(`common` 포함)를 따른다.
- **⑧ 공급**: 근거 분석 실행의 저장 묶음, 섹션 전체 블록, 대상·revision, 보존 성공분(출처 시도 포함), 표현 지시, 용어집(이번 대상 블록 원문에서 선별한 용어 — `select_glossary`, [통합 D10](integration-decisions.md)). 용어집 선별 입력(`glossary_selection`: 규칙 버전·언어·적용 분류)도 함께 고정한다. 표현 지시는 #71 확정 전(`ALTERNATIVES_SPLIT=False`)에는 만들지 않는다. 묶음·용어집·지시의 지문을 시도 명세 `supply`에 고정하고 실행 때 대조해 다르면 `INPUT_CHANGED`.
- **지문**: `app.manifest`는 `pipeline.handoff.canonical`(JCS)에 위임한다. BE·AI 지문 구현은 하나다.
- **AI 실패 보고**: 블록별 결과가 있으면 항목별 실패로, 없으면 `AdapterFailed`(`TRANSLATE_<KIND>` 등)로 기록한다. 오류 코드 문자열은 task `errorCode` 후보이며 API enum이 아니다.
- **GPU 정리**: BE가 폐기를 확정하면 `handoff_status`가 `discard`를 돌려주고 AI `cleanup()`이 `verified`·`adopted`·`discard`일 때만 지운다. 인계 없이 권한을 잃은 시도만 BE가 따로 폐기 여부를 확인한다.
- **남은 것**: 번역문 검사 연결(8절) — 규칙 검사(`run_text_check`) · LLM 판정(`run_translation_review`) 모두 BE 연동 때 일괄 검토 후 연결(시점 · 순서 미정, D12 보완 2). GPU 제어 HTTP 바인딩, #71 구분 규칙 확정 후 대체 표현 배열 변환·⑧ 표현 지시 활성화.

## 1. 실행 상태 — AI 쪽이 지켜야 할 것

| 상황 | AI 반환 | BE 기록 | 화면 |
|---|---|---|---|
| 정상 | 결과 객체 | 시도 `done` | 다음 단계 |
| 처리 대상 없음 | (BE가 호출하지 않음) | `done` + `skip_reason=no_targets` | 다음 단계 |
| 실패 | 실패 상태/예외 — 빈 성공으로 바꾸지 않음 | 시도 `failed`(+ 자동 재시도 규칙) | D9 대체 규칙 |
| 구조 오류(키 누락·중복·미등록·형식) | — | 인계 `rejected`, 시도 `failed` | 재시도/오류 |

## 2. ③-1·③-1′ — `Judge.judge(section, bundle, regulatory_class, target_country) → JudgeOutcome`

입력: `JudgeSection`(섹션 이미지 절대 경로, 블록 `key`·`block_order`·`source_ko`·`role`·`bbox`·`source_lines`, 앞뒤 섹션 텍스트)과 BE 고정 사전 묶음(`app/dictionary_bundle.py`: `regulatory.entries[]`·`local.status/entries[]`, 각 항목 `external_id`·`pk`·`verdict_status`·`source_verdict_status`·`variant_ko`·`forbidden_en`·대체 표현 원문 문자열·`evidence[]`). AI는 묶음 밖 DB를 읽지 않는다(D5).

```json
{
  "schema_version": "1",
  "section_key": "sec_101",
  "regulatory": {"status": "ok", "error": null, "retryable": false},
  "local": {"status": "ok", "error": null, "retryable": false},
  "findings": [{"finding_key": "f_01", "content_type": "<유형 코드>", "status": "present",
                "evidence_block_keys": ["blk_5001"], "evidence_source": "text", "reason": "질병 치료 표방",
                "dictionary_refs": ["RG-001"]}],
  "matches": [{"match_key": "m_001", "finding_key": "f_01", "dictionary_ref": "RG-001", "block_key": "blk_5001",
               "start": 3, "end": 5, "matched_text": "치료"}],
  "verdicts": [{"verdict_key": "v_01", "finding_key": "f_01", "dictionary_ref": "RG-001", "verdict_status": "regulated",
                "bucket": "exclude", "finding_status": "present", "problem_text": "치료"}],
  "overlaps": [],
  "inspection": {"planned": ["regulatory", "local:LC-01..LC-08"], "inspected": ["regulatory", "local"], "not_inspected": []},
  "policy": {"rules_version": "policy@2026-10-08.1", "rules_sha256": "<64hex>", "impl_version": "policy-impl/1"},
  "impl": {"model": "<모델>", "prompt_sha256": "<해시>"}
}
```

기대 결과(블록 `blk_5001` = text_block.id 5001의 `source_ko`가 `"피부 치료 효과"`일 때):
- `section.content_findings` = 6필드, `evidence_block_ids: [5001]`. `section_verdict` 1행: `verdict_type=regulatory`(대체 표현 없음), `dictionary_id`=묶음의 RG-001 `pk`, 사유·조문·링크는 **묶음 스냅샷**에서.
- `bucket=exclude`, `exclusion_reason=auto_regulatory`, `excluded_stage=N3`. audit_log `analysis_result_adopted`에 매칭 원문·구간·사전 스냅샷.

| 변형 | 기대 결과 |
|---|---|
| `local.status="failed"` | 규제 결과는 저장, 섹션 `exclude`·`auto_local_failed`, 시도 `failed(LOCAL_JUDGE_FAILED)`, N3 진행 |
| 묶음 `local.status="unavailable"` + `local.status="not_inspected"` | 현지 제외 없음, 분석 실행 `error_code=LOCAL_DICT_UNAVAILABLE`, N3 진행 |
| `regulatory.status="failed", retryable=true` | 섹션 판정 재시도(N2 2회), 소진 시 N2 오류 |
| 같은 섹션에 같은 `content_type` 2건 | 거절(D9-2 섹션·항목당 1건) |
| `matched_text != source_ko[start:end]`(코드 포인트) | 거절 |
| `evidence_source="image"`인데 블록 키 있음 / `"text"`인데 없음 | 거절 |
| 묶음에 없는 `RG-…`·`LC-…` | 거절(근거 ID 제거로 성공 처리하지 않음) |
| `policy` 누락·`inspection` 빈 객체 | 거절 |

## 3. ⑧ — `Translator.translate(section_key, blocks, target_lang, context) → TranslateOutcome`

입력 블록은 섹션의 번역 가능한 블록 전체(문맥), `is_target=true`만 이번 계산 대상. 실패 대상 재시도에서는 실패 블록만 `true`다.

```json
{"schema_version": "1", "section_key": "sec_101",
 "items": [{"key": "blk_5001", "status": "ok", "text": "Soothes the look of dryness", "error": null, "glossary_ids": [12]},
           {"key": "blk_5002", "status": "failed", "text": null, "error": "timeout", "glossary_ids": []}],
 "impl": {"model": "<모델>"}}
```

| 상황 | 기대 결과 |
|---|---|
| 위 예제(일부 실패) | 5001 `trans_1` 저장·revision+1, 5002 빈 칸. 시도 `failed(TRANSLATE_PARTIAL)`, N5 진행·`translation_failed` 신호·확정 시 확인 필요 |
| 대상 전부 failed | 자동 재시도 1회 → 시도 `failed(TRANSLATE_ALL_FAILED)` → N4 오류(빈 N5 금지) |
| 대상 키 누락·중복·미등록 / ok 인데 빈 문장 | 구조 실패로 거절 |
| 그 사이 사용자가 N5에서 수정한 블록 | 자동 번역으로 덮지 않음(revision·edited 조건) |

## 4. ⑥ GPU — 제어 서비스 경유(`app/flows/gpu_worker.py`)

`acquire → (입력: 섹션 이미지 presigned GET·sha256, ③ 블록, 채택된 ④ 결과, ⑤ 결과+logo_record 실제 payload) → heartbeat → upload_url(kind, part_key) → presigned PUT → register(envelope) → status`.

```json
{"outcome": "done", "target_count": 3, "input_fingerprint": "<acquire 응답 값>", "impl_version": "pipeline.inpaint@…",
 "payload": {"status": "inpainted", "counts": {"final_px": 1532}, "regions": [], "protected_blocks": [], "model": {"name": "lama"}},
 "artifacts": [{"kind": "background", "part_key": "sec_101", "staging_key": "staging/<job>/<task>/<epoch>/background/sec_101", "declared_sha256": "<hex>"},
               {"kind": "delete_mask", "part_key": "sec_101", "staging_key": "…/delete_mask/sec_101", "declared_sha256": "<hex>"},
               {"kind": "protect_mask", "part_key": "sec_101", "staging_key": "…/protect_mask/sec_101", "declared_sha256": "<hex>"}]}
```

- `unchanged`(마스크 0): 두 마스크 업로드 + `background`는 `{"source_ref": {"key": <섹션 이미지 키>, "sha256": <acquire 입력 해시>}}`.
- 실패: `outcome="failed"`, `payload.error_code`. BE: 원본 배경으로 조판, `warning_badge=processing_failed`, 재시도 버튼 없음.
- BE가 스테이징을 내려받아 잰 같은 바이트만 `verified/…`에 고정한다. 주장 해시와 다르면 거절. `status`가 `remote_recoverable`·`discard`일 때만 로컬 자료를 지운다.

## 5. 함께 확인할 것

0절 「남은 것」과 [PR #55 BE 확인](https://github.com/ai-pixlate/server/pull/55#issuecomment-6054998455)의 AI 요청(대상 섹션 지정·근거 배열·검증기 경로·정리 상태·예제 재생성의 Unicode 버전 의존)을 따른다. ⑧ 대체 표현 구분자(#71)는 임시 공급 규칙으로 연결했으며 결정되면 변환·공급만 바꾼다. 용어집 공급은 D10 선별로 교체했다(2026-10-11).

## 6. 역할별 스타일 기본값 — BE 렌더 연결 인계(2026-10-12, D11)

[D11](integration-decisions.md)의 사용자 승인값을 AI가 설정 자료 · 폰트 · 계산 함수 · 사례로 준비했다(2026-10-12). AI 작업에서는 운영 렌더 코드를 바꾸지 않았다.

**연결 상태(2026-10-15, `origin/release/1.0.0` `f5b050a` 코드 대조 — 당시 기준)**: BE PR #71(`65f350e`)로 로더 · 폰트 검증 · `resolve_block` 규칙 · 렌더 입력 지문이 운영 렌더에 연결됐다. 실제 그리기 폰트는 아직 Pillow 내장 기본 폰트(`ImageFont.load_default`)다 — Pretendard · 역할별 굵기 적용은 release에서 BE가 진행하기로 했다([PR #72 코멘트](https://github.com/ai-pixlate/server/pull/72#issuecomment-6185856592)). 자간 · 행간 · 넘침 기준 · 기록 형식은 develop 후속이다. 항목별 상태는 6.6. 이 절의 상태 표기는 그 뒤 갱신하지 않는다 — 이후 PR #74(`32652ba`)로 Pretendard · 역할별 굵기가 반영됐고, 현재 연결 상태와 남은 작업은 `status.md` ⑨ 행이 소유한다.

### 6.1 자료 위치

| 자료 | 경로 | 소유 · 성격 |
|---|---|---|
| 설정 자료(정본) | `pipeline/data/style_defaults.json` — `defaults_version` `role-style@2026-10-12.1` | AI. 디자인 토큰(`type_tokens`, 출처 design_image)과 사용자 승인 매핑 · 보완값(`pipeline_mapping`, 출처 user_approved)을 나눠 담는다. 같은 값을 다른 파일에 따로 두지 않는다 |
| 로더 · 검증 · 계산 | `pipeline/handoff/style_defaults.py` — `load_style_defaults` · `verify_fonts` · `resolve_block` · `render_inputs` · `RULE_VERSION`(`role-style-rule/1`) | AI. 정책 규칙(`bundle.py`)처럼 BE가 import해 쓰는 것을 전제로 한다. 조판 · 측정은 하지 않는다 |
| 폰트 파일 | `pipeline/data/fonts/pretendard/` — `Pretendard-Regular.otf` · `Pretendard-SemiBold.otf` · `LICENSE.txt` | 공식 배포본 그대로 저장소에 포함(git checkout으로 배포). 검증 · 재현 도구 `pipeline/data/fonts/tools/fetch_fonts.py` |
| 사례(입력 · 기대 결과) | `pipeline/samples/style_defaults/cases.json` — 10건(8종) | AI. 기대값은 손 계산(검증 대상 코드로 생성하지 않음) |
| 참고 렌더 | `pipeline/handoff/style_reference.py` | AI 검증용. 운영 렌더 아님(6.5) |
| 테스트 | `tests/test_pipeline_style_defaults.py` | 설정 · 폰트 · 환산 · 사례 · 참고 렌더 |

### 6.2 승인값과 폰트

| 역할 | 토큰(디자인) | 기준 크기 | 굵기 · 폰트 파일 | 자간 |
|---|---|---|---|---|
| title | Heading / H2 | 32px | SemiBold(600) | −1.5% = −0.015em |
| body | Body / L | 16px | Regular(400) | −3% = −0.03em |
| caption | Body / M | 14px | Regular(400) | −1% = −0.01em |
| price | Heading / H3 | 24px | SemiBold(600) | −2% = −0.02em |
| caution | Body / L | 16px | Regular(400) | −3% = −0.03em |

공통(사용자 승인): 폰트 Pretendard · 기준 폭 375px · 기본 글자색 `#222222` · 기본 정렬 left · 행간 = 적용 크기 × 1.2.

| 파일 | 크기(byte) | SHA-256 | 메타데이터(실제 파일에서 읽음) |
|---|---|---|---|
| `Pretendard-Regular.otf` | 1,574,352 | `3ffbacde6ab8411f1d2db54bb9b1f0b3ee2a738932033722cf0388c06aed1c93` | CFF · name ID 16/17 `Pretendard`/`Regular` · OS/2 usWeightClass 400 · `Version 1.309;Glyphs 3.2 (3225)` |
| `Pretendard-SemiBold.otf` | 1,583,704 | `c89bc43027dc7cde5726e96223376f8eec09302b2fc1f8147fd5b57cfc376118` | CFF · name ID 16/17 `Pretendard`/`SemiBold` · OS/2 usWeightClass 600 · 같은 버전 |
| `LICENSE.txt` | 4,419 | `b04538c9abec39a3db75108cf0af0fd9c77032fe8aa2cf38345b4d250e98e38e` | SIL Open Font License 1.1 |

출처: 공식 저장소 `orioncactus/pretendard`의 릴리스 v1.3.9 `Pretendard-1.3.9.zip`(47,304,526 byte, SHA-256 `04be351a74d6bf7d60c480a3087e51d185485d35a52023142af1df19eb8c428a`) 안의 `public/static/` OTF와 `LICENSE.txt`. OFL은 라이선스를 함께 두는 조건으로 재배포를 허용한다 — 폰트와 `LICENSE.txt`를 같은 폴더에 둔다.

```bash
python -m pipeline.data.fonts.tools.fetch_fonts --check
```

`--check`는 배치된 파일의 크기 · SHA-256 · 굵기 · 이름 · 버전을 설정 자료와 대조한다(종료 코드 0 통과 · 1 불일치). 파일은 저장소에 포함돼 있다. 공식 배포본에서 다시 만들려면 인자 없이 실행해 공식 URL에서 받거나 `--zip <받은 zip>`으로 배치한다. zip 해시가 다르면 아무것도 쓰지 않는다. 시스템 설치 폰트로 대체하지 않는다.

### 6.3 적용 규칙 — `resolve_block`

| 입력 | 적용값 · 출처 |
|---|---|
| `is_excluded`가 false가 아님(라벨 · 로고 · 규제 제외 등) / 빈 원문 / 번역문 없음(NULL · 빈 문자열) | 조판하지 않음(`skip_reason` excluded · empty_source · no_translation). 기본값으로 되살리지 않는다 |
| ⑦ 완료, 측정값 있음 | `font_color` · `est_font_px` · `align`을 그대로 — 출처 `measured`. 단일 줄 · 동률 left(`align_basis` default_single_line · default_tie)도 측정 결과다 |
| ⑦ 완료, 일부 NULL | NULL 항목만 역할 기본값 — 출처 `role_default` |
| ⑦ 실패(style 객체 NULL) | 세 항목 모두 역할 기본값. 단계 상태는 `failed` 그대로(측정 성공으로 만들지 않음) |
| 계약 위반(⑦ 완료인데 대상 블록 style 없음, ⑦ 실패인데 style 있음, 형식 밖 값) | `StyleInputInvalid` — 기본값으로 덮지 않는다 |

- 크기: 측정 `est_font_px`는 원본 px 그대로. 기본값만 `기준 크기 × 원본 폭 / 375`(곱한 뒤 한 번 나눔, 반올림 없음). 1080px 원본의 title = 92.16, body = 46.08. 측정값에 1080/375나 ⑦ `style.em_ratio`를 다시 곱하지 않는다(사례 07).
- 자간 px = (자간 % / 100) × 적용 크기, 행간 px = 1.2 × 적용 크기 — 측정 크기에도 같은 비율을 쓴다. 폰트 · 굵기 · 자간 · 행간의 출처는 `role_config`.
- `bg_color`는 적용값에 넣지 않는다. 배경을 칠하지 않으며 배경색 기본값은 없다.
- 원본 폭: MVP 섹션은 원본 전체 폭이다(`section.bbox.w`). BE `_render_inputs`가 이미 섹션 폭을 알고 있다.

### 6.4 적용값 · 출처 기록 — AI 제안 형식(BE 확인 필요, #78)

`ResolvedBlock.to_json()`의 형식이다. 운영 저장 위치 · 키는 BE가 정한다. `text_block.style`(측정값)은 덮지 않는다.

```json
{"id": 401, "render": true, "skip_reason": null, "style_stage": "failed", "measured": null, "measured_align_basis": null,
 "applied": {"type_token": "Heading / H2", "font_family": "Pretendard", "font_face": "SemiBold", "font_weight": 600,
             "font_sha256": "c89bc430…", "font_size_px": 92.16, "font_color": "#222222", "align": "left",
             "letter_spacing_em": -0.015, "letter_spacing_px": -1.3824, "line_height_px": 110.592},
 "source": {"font_size_px": "role_default", "font_color": "role_default", "align": "role_default", "font_family": "role_config",
            "font_face": "role_config", "letter_spacing": "role_config", "line_height": "role_config"}}
```

현행 BE 렌더 기록(`applied` = `font_color` · `est_font_px` · `align`, `source` = measured · role_default)을 이 형식으로 넓히는 안을 추천한다. `align_basis`는 현재 `text_block.style`에 저장되지 않는다 — 보존 여부는 BE 결정이며, 보존하지 않아도 `align`이 NULL이 아니면 측정값으로 취급한다.

### 6.5 참고 렌더와 운영 렌더의 차이

```bash
python -m pipeline.handoff.style_reference --out pipeline/out/style-defaults-check
```

사례마다 원본 폭 × 섹션 높이의 흰 캔버스 PNG와 `layout.json`(적용값 · 줄 · 줄 폭 · 넘침)을 만든다. **AI 검증용이며 운영 렌더가 아니다. 이 렌더의 성공은 운영 연결 완료가 아니다.** 운영 렌더(BE)와 다를 수 있는 점:

| 항목 | 참고 렌더(Pillow 12.3 · Layout.BASIC · raqm 없음) | 운영 조판에서 정할 것 |
|---|---|---|
| 커닝 · 합자 | GPOS 미적용 | HarfBuzz · 브라우저 엔진이면 적용됨 → 줄 폭 차이 |
| 글리프 진행 폭 | 정수 px로 반올림(힌팅) — 20px에서 `i` = 4px | 서브픽셀 진행 폭 사용 여부 |
| 자간 | 글자 사이에만(줄 폭 = 글리프 합 + 자간 × (글자 수 − 1)) | CSS는 엔진에 따라 마지막 글자 뒤에도 더한다 |
| 세로 위치 | 반행간을 위아래로 분배, ascent/descent = FreeType 값 | 브라우저는 OS/2 typo 값을 쓸 수 있다(USE_TYPO_METRICS 설정됨). 넘침 높이는 줄 수 × 행간이라 무관 |
| 줄바꿈 | 원문 `\n` 유지 · 나머지 공백 접기 · 단어 단위 · 긴 단어 자르지 않음(CSS `white-space: pre-line` 상당) | 같은 규칙 권장(현행 `app/typeset.py`와 같다) |

사례 08의 804(여러 줄 감기)는 참고 렌더에서 한 줄 폭이 영역보다 1px 넓어 줄이 갈린다 — 엔진 차이에 민감해 기대 줄 구성을 두지 않았다. 운영 엔진에서 이 사례의 줄 구성을 기록해 두기를 권한다.

### 6.6 BE 수정 대상과 순서

2026-10-12 AI 작업에서는 아래 운영 코드를 바꾸지 않았다. 각 항목 끝의 **상태**는 2026-10-15 `origin/release/1.0.0` `f5b050a` 코드와 대조한 당시 기준이다. 현재 상태는 `status.md` ⑨ 행.

1. **`app/typeset.py` 로더 교체**: `load_role_defaults()`가 `pipeline.handoff.style_defaults.load_style_defaults()` + `verify_fonts()`를 쓰게 한다. 경로 기본값은 저장소 정본, `PIXLATE_STYLE_DEFAULTS`는 대체 경로로 두는 안을 추천한다(정책 규칙 `PIXLATE_POLICY_RULES`와 같은 방식). 폰트 검증 실패는 기존 `StyleDefaultsUnavailable`처럼 렌더 실패로 처리하고 내장 폰트로 그리지 않는다. `deploy/pixlate.env.example` 설명도 함께 고친다. **상태: 반영(PR #71 `65f350e`)** — `load_role_defaults()`가 정본 경로 + `verify_fonts()`, 실패하면 `STYLE_DEFAULTS_UNAPPROVED`로 중단. 검증 실패 시 내장 폰트로 대체하지 않는 정책은 유지한다(사용자 결정 2026-10-15, PR #72 코멘트).
2. **스타일 해석**: `resolve_style()`을 `resolve_block()`으로 대체하거나 같은 규칙으로 고친다. 원본 폭(섹션 `bbox.w`)과 섹션의 ⑦ 시도 결과(done/failed)를 넘긴다. 현재는 단계 상태 없이 style NULL만 본다. **상태: 반영(PR #71)** — `resolve_style()`이 `resolve_block()`을 부르고, ⑦ 단계 상태는 렌더 manifest의 `style_stage`, 원본 폭은 섹션 배경 폭으로 넘긴다. 계약 위반은 `STYLE_RESULT_INVALID`.
3. **실제 폰트 · 굵기 · 자간 · 행간 적용**: `_font()`의 `ImageFont.load_default` 대신 역할 굵기의 실제 OTF를 적용 크기(소수 허용)로 연다. 자간 · 행간은 적용값의 px를 쓴다(`LINE_SPACING` 상수 대신 설정 자료의 비율). **상태: 미반영** — `_font()`는 아직 `ImageFont.load_default`, 행간은 상수 1.2. 폰트(Pretendard) · 역할별 굵기는 release에서 BE 진행 예정(측정 · 그리기에 같은 폰트 객체, `RENDERER_VERSION` 갱신, 역할별 폰트 · 굵기와 특수문자 · 한글 출력 시험), 자간 · 행간은 develop 후속.
4. **넘침 계산을 실제 조판과 같은 기준으로**: 폭은 자간을 포함한 실제 줄 폭, 높이는 줄 수 × 행간 px. 높이가 영역과 같으면 넘침이 아니다. 넘쳐도 크기 축소 · 문구 축약 · 측정값 교체를 하지 않는다. 운영 엔진의 자간 끝 처리 · 커닝 기준은 #79. **상태: develop 후속**(자간 적용과 함께).
5. **입력 지문**: `app/flows/downstream.py`의 `_render_inputs`(미리보기 — 최종 렌더의 `app/flows/final.py` `_final_inputs`도 이것을 부른다)와 `_final_inputs`의 `style_defaults`를 `render_inputs(d)`(설정 버전 · SHA-256 · 폰트 SHA-256 · 규칙 버전)로 바꾸고, `RENDERER_VERSION`의 렌더 라이브러리 버전을 유지한다. `_render_section`의 대조도 같은 값으로 한다(다르면 `INPUT_CHANGED`). **상태: 반영(PR #71)** — manifest `style_defaults` = `typeset.style_inputs()`(`render_inputs`), 자료를 쓸 수 없으면 `unavailable`을 남기고 렌더 때 실패. 폰트 교체 시 `RENDERER_VERSION`을 올리는 것은 위 3의 release 작업에 포함. 지문이 달라진 기존 미리보기가 언제 다시 그려지는지는 단정하지 않는다 — 코드상 하류 실행이 `running`인 동안의 `progress()`만 새 시도를 만든다(N5 이후 · 재시도 영향은 BE 확인).
6. **미리보기 · 최종 렌더 공통**: 두 경로가 같은 로더 · 같은 `resolve_block` · 같은 조판 함수를 쓴다. 최종 렌더가 미리보기와 다른 설정을 읽을 수 없게 한다. **상태: 반영(PR #71)** — 최종 렌더도 같은 `style_inputs()` · `_render_section` · `render`를 쓴다.
7. **기록**: `text_block.style`은 측정값 그대로 둔다. 렌더 payload의 `applied` · `source`를 6.4 형식으로 넓힌다(저장 위치는 #78). **상태: develop 후속** — 현재 `applied` · `source`는 글자색 · 크기 · 정렬 세 키.
8. **테스트**: `tests/exec_fakes.style_defaults_file`(테스트 전용 3키 형식)을 새 형식에 맞추고 6.7 사례를 BE 통합 시험으로 옮긴다. N4/N5/N6 상태 전이(D9-3)는 바꾸지 않는다. **상태: 일부 반영(PR #71)** — `tests/exec_fakes`가 정본 설정 자료를 쓰고, 기본값 적용 · 지문 불일치 · 자료 불가 시험이 `tests/test_exec_units.py` · `tests/test_exec_downstream.py`에 추가됐다. 6.7 사례 전체를 통합 시험으로 옮겼는지는 BE 확인.

기존 `font_map` 테이블이 있어도 이번 연결에 새 DB 관리 기능 · 마이그레이션을 추가하지 않는 안이다(AI 쪽에서도 만들지 않았다).

### 6.7 테스트 입력 · 기대 결과 · 실행 방법

```bash
python -m pytest tests/test_pipeline_style_defaults.py
```

`cases.json`의 각 사례는 `input`(원본 폭 · 섹션 높이 · 블록: role · is_excluded · source_ko · text · style · style_stage · align_basis · box)과 블록별 `expected`(render · skip_reason · applied · source, 08은 layout)를 가진다. 숫자는 절대 오차 1e-9로 비교한다. 입력 형식은 AI 참고 형식이며 운영 저장 · API 필드가 아니다.

| 사례 | 확인하는 것 |
|---|---|
| 01_all_measured | 측정값 3개 유지 · bg_color 미사용 · default_single_line left는 측정값 |
| 02_color_null | 글자색만 #222222(role_default) |
| 03_align_null | 정렬만 left(role_default), default_tie left는 measured |
| 04_style_failed | ⑦ 실패 → 1080px title 92.16 · price 69.12 · caution 46.08, 단계 failed 유지 |
| 05_excluded_targets | 라벨 · 로고 · 빈 원문 · 번역 NULL/빈 문자열 → 조판 안 함. ⑦ 완료 · 세 항목 NULL → 모두 role_default |
| 06a · 06b · 06c | 원본 폭 375 · 860 · 1080 — 860이면 title 73.38666666666667(반올림 없음) |
| 07_fractional_measured | 측정 37.25 · 18.6 유지(배율 · em_ratio 미적용, 기본값보다 작아도 유지) |
| 08_wrap_overflow | `\n` 2줄 · 높이 48 = 넘침 아님 · 47 = 넘침 · 긴 단어 폭 넘침 · 빈 줄 유지 |

BE 통합 시험의 기대 결과(DB 필요, AI 쪽 미실행):
- 사례 입력을 `text_block` 행(`is_excluded` · `source_ko` · `trans_1` · `style` · `role` · `bbox`/`layout_bbox`)과 섹션의 ⑦ 시도 상태로 만들고 미리보기 → N5 → 최종 렌더를 돌린다.
- 미리보기 payload와 최종 렌더 `renders[*].applied`가 사례의 `applied` · `source`와 같다. `text_block.style`은 렌더 전후 같다.
- ⑦ 실패 섹션도 미리보기 · 최종 렌더가 성공하고, N5 표시가 없으며(D9-3) ⑦ 시도는 failed로 남는다.
- 설정 자료 · 폰트 파일 · 규칙 버전 중 하나를 바꾸면 고정 입력 대조에서 `INPUT_CHANGED`.
- 폰트 파일이 없거나 해시가 다르면 렌더 실패(내장 폰트로 성공하지 않음).
- 08 사례의 `text_block.overflow`가 사례 layout의 넘침과 같다(804는 운영 엔진 결과를 기록).

### 6.8 남은 것 · 검증 한계

- 운영 연결(6.6)은 1 · 2 · 5 · 6이 PR #71로 반영됐고 8은 일부다(2026-10-15 코드 대조, 당시 기준 — 현재 상태는 `status.md` ⑨ 행). 남은 것: 실제 폰트 · 굵기(release, BE 진행 예정), 자간 · 행간 · 넘침 기준 · 기록 형식(develop). 이 PC에는 BE · DB 시험 환경이 없어 AI 쪽은 단위 · 참고 렌더 검증만 했고, PR #71의 BE 시험은 실행하지 않았다.
- #78 적용값 기록 위치 · 키, #79 운영 조판 엔진의 측정 기준(자간 끝 · 커닝 · 서브픽셀)은 BE 확인이 필요하다. 폰트 파일은 저장소에 포함한다(2026-10-12 사용자 결정, #80 해결).
- 디자인 원본 이미지는 이 작업에서 직접 열람하지 않았다. 토큰 값은 사용자가 전달한 승인표 기준이다.

## 7. Linux CPU OCR 수정 인계(2026-10-12)

### 7.1 수정 범위와 현재 검증 상태

- 배포 담당 보고: develop `f8a1e67` 기반 Linux CPU OCR 이미지(Python 3.12), 합성 섹션 600×560에서 `ConvertPirAttribute2RuntimeAttribute` 오류·종료 코드 2. 실험 컨테이너에서 oneDNN만 비활성화하면 종료 코드 0·영역 5개·영역별 score 존재. 이 결과는 배포 담당의 재현이며 수정 코드의 재검증 결과가 아니다.
- 정상 `PaddleOcrEngine` 생성 경로에서 선택 장치가 `cpu`이면 OS와 관계없이 `enable_mkldnn=False`를 전달한다. Windows CPU의 기존 우회와 GPU 설정은 유지한다. monkey patch·새 환경 변수·운영 컨테이너 내부 임시 수정에 의존하지 않는다.
- 모델·인식 임계값(`text_rec_score_thresh=0.0`)·score/좌표/출력 계약은 그대로다. 의존성 변경 없음: `paddlepaddle==3.3.1`, `paddleocr==3.7.0`, `paddlex==3.7.2`.
- 로컬 회귀: `tests/test_pipeline_ocr.py` + `tests/test_pipeline_config.py` 39개 통과·실제 PaddleOCR 1개 건너뜀. Linux/Windows CPU 및 GPU 생성 인자·실행 정보·score 전달을 대역으로 검증했다. **수정 이미지의 Linux 실제 추론·메모리·시간 검증은 미실행**이다. 작업 PC의 Docker Linux 엔진에 연결할 수 없어 배포 환경에서 아래 검증이 남아 있다.

### 7.2 현재 지원 범위

- 기본 `ocr.split_threshold_px=4000` 기준으로 높이 4,000px 이하 섹션을 통째로 처리한다. 실제 이미지 크기는 Section의 width·height와 같아야 한다. 임의 폭에서 메모리 사용량을 보장한다는 의미는 아니다.
- (2026-10-12 기준) `tile_px=2000` 설정은 있지만 타일 분할·겹침 병합은 미구현(#31·#32)이었다. 높이 4,000px 초과는 `ocr.run()`이 `NotImplementedError`로 거절하고 OCR CLI는 종료 코드 3을 반환했다. **2026-10-13 타일 OCR 구현(`feature/ocr-long-section-tiling`)으로 4,000px 초과 섹션도 처리한다 — 9절.**
- 이 oneDNN 수정 검증(7.3)은 타일 처리와 분리한다. 긴 섹션 검증과 메모리 측정은 **실제 입력 폭 × 4,000px 이하 높이**의 섹션 단위로 한다. 타일 OCR의 Linux 실측은 9.3절이다.

### 7.3 배포 환경 재검증과 완료 기준

1. 배포 담당이 오류를 재현한 OCR 이미지의 Dockerfile·빌드 명령·이미지 digest, 입력 파일·SHA-256·CLI 명령을 보존한다. 저장소 루트 Dockerfile에는 OCR 의존성이 없으므로 해당 배포 OCR 이미지 빌드 방법을 사용한다. 수정 전후에 같은 입력·의존성·자원 제한을 사용한다.
2. 수정 코드를 포함해 Linux CPU 이미지를 다시 빌드한다. 컨테이너 안에서 코드를 고치거나 monkey patch하지 않는다. Python 실제 버전(`python --version`)과 `importlib.metadata.version()`으로 `paddlepaddle`·`paddleocr`·`paddlex` 설치 버전을 기록한다.
3. 동일 600×560 합성 섹션으로 OCR CLI를 실행한다. 저장소의 `pipeline/samples/synthetic_01/expected/split.json` 첫 섹션도 600×560이지만, 크기가 같다는 이유로 배포 재현 입력과 동일하다고 간주하지 않는다. 배포 입력이 이 파일인지 해시로 확인한다. 이 샘플을 사용한 명령은 다음과 같다(출력 폴더는 실행마다 새 경로).

```bash
python -m pipeline.run ocr --split pipeline/samples/synthetic_01/expected/split.json --section sec_1_01 --out pipeline/out/ocr-linux-cpu-fixed
```

4. 종료 코드 0, 인식 영역 1개 이상, 모든 영역의 score 존재·0~1 범위를 확인한다. `run.json`의 `engine.device=cpu`, `engine.enable_mkldnn=false`와 생성 설정의 같은 값, 버전 정보를 보존한다. 원래 보고된 영역 수 5개와 달라지면 차이를 조사한다.
5. 실제 입력 폭을 가진 긴 섹션(높이 4,000px 이하, 텍스트 포함)도 새 프로세스에서 실행한다. 입력 폭·높이·해시와 결과·영역별 score를 기록한다. 실제 OCR 회귀 테스트를 실행하고 실제 엔진 테스트가 건너뛰지 않았는지 확인한다.
6. 각 입력에 대해 **모델 로드를 포함하는 새 CLI 프로세스 전체**의 최대 메모리와 경과 시간을 측정한다. GNU time이 있는 이미지에서는 `/usr/bin/time -v`로 CLI를 감싸 maximum resident set size(KiB, MiB 환산 시 1024로 나눔)·elapsed time·종료 코드를 보존한다. 컨테이너 전체 최대 메모리는 프로세스 RSS와 구분해 측정 수단·한도와 함께 기록한다. 모델 다운로드 포함 여부·캐시 상태도 남긴다.

동일 합성 입력과 지원 범위의 긴 입력이 수정 이미지에서 모두 통과하고 위 기록이 확보돼야 Linux 수정 검증 완료로 판단한다. 배포 담당의 기존 최대 메모리 **2,104MiB**는 비교 참고값이며 이번 수정 실측값이나 운영 요구량이 아니다. EC2 자원 결정은 BE·배포 담당이 맡는다.

## 8. 번역문 검사 연결 제안 — BE 미합의 · 연동 전 검토(2026-10-12 · 2026-10-13 PM 검토 반영 · 2026-10-22 사용자 결정, D12)

규칙 검사 `run_text_check`(기존)와 번역문 LLM 규제 판정 `run_translation_review`(신규)를 BE 실행 계층에 연결하기 위한 **AI 측 제안**이다. **BE와 합의하지 않았으며, BE 연동 작업을 시작할 때 일괄 검토한다**(검토 항목은 8.12). 계약은 `contract.md` 5.3 · 7.1, 결정은 [통합 D12 · D12 보완 · D12 보완 2](integration-decisions.md). 이번 작업에서는 두 검사 모두 BE에 연결하지 않으며 연결 시점 · 순서는 정하지 않았다 — 「1차 통합은 규칙 검사 · LLM 판정은 12월 최종 통합」은 과거 계획(D12 보완)이다. 어느 구성이든 지원하도록 활성 검사를 명시한다(8.4). 봉투 Stage 추가 · `compliance_flags` 7.1 확장 · 요청의 라벨 · 로고 값은 BE 경계 변경이며 연동 전 검토 대상이다. PR #63의 develop 머지는 1차 릴리즈 이후다. 구현 · 검증 현황은 `status.md` ⑧ 행, 남은 검증은 `open-questions.md` #84. 아래 BE 수정 사항은 제안이며 BE가 구현 · 확인한다. `app/` · migrations · OpenAPI · FE는 AI가 수정하지 않는다.

### 8.1 요청 구성

- 두 검사 모두 섹션 단위 시도이며 대상은 섹션의 검사 가능한 블록 전체(포함 섹션 · `is_excluded=false` · 비공백 원문 · 비공백 현재 `trans_1`)다. 한 블록만 수정돼도 섹션 전체를 다시 검사한다(U2 현재 시도 대체가 깔끔하고 문맥 변경 stale과 같은 단위).
- `text_check`: 기존 형식(`targets[{block_key, revision, text}]` · 묶음 · 용어). 묶음은 그 분석 실행의 고정 묶음, 용어는 ⑧ 공급과 같은 선별 규칙.
- `translation_review`: `targets`에 라벨 · 로고 값을, 대상 밖 섹션 블록은 `context[{block_key, revision, is_product_label, is_brand_logo}]`로 준다. 라벨 · 로고는 **BE 공급 계약 후보** — DB 값을 그대로 주며 누락을 false로 채우지 않는다. 대상 밖 블록의 번역문은 주지 않는다(대상 밖인데 번역문이 있으면 BE 불변식 위반).
- 등록 때 `review_manifest(request, cfg)` · `text_check_manifest(request)`로 지문을 계산해 `expected_input_manifest_sha256`과 시도의 고정 지문에 둔다.
- **활성 검사 구성**: 실행(작업)마다 이번에 돌릴 검사를 정해 등록 · 집계에 같은 값을 쓴다 — 규칙 검사만 `("rule",)` 또는 두 검사 `("rule", "llm")`. 어느 구성을 언제 쓸지는 BE 연동 때 정한다(과거 계획: 1차 통합 규칙 검사 · 12월 최종 통합 두 검사). 비활성 검사는 등록하지 않는다. 구성을 어디에 둘지(설정 · 실행 행 등)는 BE가 정한다. 새 API 필드 · DB 스키마는 AI가 정하지 않는다.

### 8.2 등록 위치

- ⑧ 번역 채택 트랜잭션(`downstream.py`의 trans_1 저장 · revision+1)과 실패 대상 재시도 채택: **실제 저장된 문구와 증가한 revision**으로 그 섹션의 활성 검사 시도를 등록한다(활성 검사만 등록하고 비활성 검사는 등록하지 않는다).
- N5 수정(`cfm.py`): 수정 저장 · `edit_signal` · 재렌더 등록과 같은 트랜잭션에서 활성 검사 시도를 새로 등록해 이전 시도를 대체한다.
- 검사 실패 · 재시도는 번역 재시도를 만들지 않는다.
- **N4→N5**: 번역 직후 검사(활성 검사)는 N4→N5 전환 조건에 들어간다 — 검사가 끝난(채택 또는 최종 실패) 뒤 N5로 넘어간다. 재시도 후에도 최종 실패면 N4 오류로 막지 않고 해당 블록에 「⚠ 검사하지 못했습니다」를 표시해 N5로 진행한다(2026-10-12 PM 검토 — 이전 초안의 「N4→N5 집계에서 검사 stage 제외」를 대체). N5 수정 후 검사만 N5에서 돈다.

### 8.3 채택

- 현재 시도 · lease 세대 · `validate_report` 통과, 그리고 시도의 고정 지문 = 현재 입력으로 다시 계산한 지문(LLM은 섹션 문맥 포함)일 때만 보고 전체를 채택한다. 다르면 보고 전체를 거절한다(블록별 부분 채택 없음). 새 시도는 이미 등록돼 있어야 한다.
- 채택 시 자기 producer의 `compliance_flags` 항목만 교체한다(`translation_validation` · `glossary_validation` / `translation_llm_review`). 다른 producer 항목 · 번역문 · 사용자 수정문은 건드리지 않는다. flag의 `external_id` · `glossary_id`는 DB id로 변환한다.
- LLM 판정 전체(no_risk 포함) · 검사 범위 · `supplied_items`(근거 인용문 원문) · 입력 지문은 채택 인계 payload와 `audit_log.detail`에 보존한다.

### 8.4 조회 · 집계

- 블록 조회 때 `check_aggregate.aggregate_block_checks(block_key=…, check_item_ids=review.check_item_ids(bundle, 분류, 국가), active_checks=…, rule=…, llm=…)`를 부른다. 활성 검사에는 `CheckInput(run_state, attempt_manifest_sha256, current_manifest_sha256, block_result, failed_scope)`를 **반드시** 주고(시도가 없으면 `run_state='none'` — 누락된 검사로 미완료), 비활성 검사는 `None`을 준다(넘겨도 읽지 않으며 축 값은 `inactive`). `run_state`는 현재 시도의 `job_async_task.status`를 그대로(pending · running), 채택 완료는 done, 정상 생략은 skipped, 더 재시도하지 않기로 정한 최종 실패는 failed, 시도가 없으면 none. `attempt_manifest_sha256`은 시도에 고정한 지문, `current_manifest_sha256`은 현재 입력으로 다시 만든 지문이다.
- 적용 예: 규칙 검사만 활성일 때 `aggregate_block_checks(block_key="blk_1", check_item_ids=ids, active_checks=("rule",), rule=CheckInput("done", fp, fp, rule_block), llm=None)` → 규칙이 현재 입력으로 끝났으므로 `checks_settled=true`, LLM 축은 `inactive`. 두 검사가 활성(`active_checks=("rule", "llm")`)일 때 LLM이 `CheckInput("running", fp, fp)`면 false. 사례는 `tests/test_pipeline_translation_review.py`.
- 집계 결과는 상태 · 두 축 정보다. 표시 등급은 PM 결정으로 확정됐다 — 규칙 · LLM 결과는 같은 등급의 「규제 검증 결과」 한 신호다(8.11). 신호 코드 · API 필드 · `basis_summary` 표시 방식 등 코드 · API · 표시 구현만 BE · FE 조정 사항이다(`open-questions.md` #82). AI 집계값을 API 경고 코드나 확정 정책으로 바로 연결하지 않는다.

### 8.5 검수 확정

- N5 수정 후 검사가 도는 동안 확정 버튼을 비활성으로 두고, 서버도 확정 요청 시 job 잠금 아래에서 블록별 `checks_settled`(활성 검사 기준)를 확인해 false면 거절한다. 검사 채택도 같은 job 잠금 순서를 따른다. 확정이 하류 실행을 닫은 뒤 도착한 검사 결과는 거절한다. **구체 API 오류 코드 · 응답 필드 · 비활성 안내 문구는 AI가 정하지 않는다.**
- 검사 종료 뒤 미해결 규제 경고 · 검사 실패가 남으면 확인을 받고 진행하며 확인 이력을 저장한다(막지 않음, D9 · F-CFM-09). LLM 최종 실패도 같은 절차다. `checks_settled`는 처리 종료 여부일 뿐 문제없음이나 확정 허용이 아니다.

### 8.6 실패 · 재시도

- `model_call_failed` · `response_invalid`(retryable=true)는 자동 재시도 후보다. 출력 잘림(`response_invalid` + retryable=false, 분할 `output_truncated`)과 `unsupported_input`은 같은 설정 재시도로 풀리지 않는다 — 설정을 바꾸면 지문이 바뀌어 새 시도가 된다. 한도는 BE 운영값이다.
- 한도를 넘긴 최종 실패는 검사 실패로 남기고, 번역 직후든 N5 수정 후든 해당 블록에 「⚠ 검사하지 못했습니다」를 표시해 확정 시 확인 대상에 넣는다(규칙 검사 · LLM 판정 모두 같은 절차). 실패를 문제없음 · 검사 완료로 표시하지 않는다.

### 8.7 마이그레이션 · 라우팅

- 0008 후속 마이그레이션(어느 검사부터 등록하든 두 stage를 함께 허용하는 안을 추천): `STAGES`에 `text_check` · `translation_review` 추가, 단계 ↔ `task_type='verify'` · `unit_type='section'` 대응, 부모 실행 트리거의 downstream 허용 단계에 두 검사 추가.
- 디스패치(작업 태스크) · 복구(`app/recovery.py`) · 상태 집계에서 두 stage 처리.

### 8.8 배포 순서

1. 제약을 넓히는 마이그레이션 → 2. 새 stage를 처리하는 코드 배포(검사 등록 꺼 둠) → 3. 모든 워커 교체 확인 → 4. 등록 켬. 봉투 버전 `handoff@2026-10-08.1`은 유지하지만 옛 코드는 새 stage를 거절할 수 있다.
- 등록을 켜기 전부터 N5에 있던 작업은 검사 시도가 없다(`run_state=none`). 일괄 등록 · 다음 수정 때 등록 · 그동안의 확정 처리를 BE가 정한다(`open-questions.md` #83).

### 8.9 전체 취소

- 검사 요청 · 보고 payload · `non_adoptable_diagnostics` · audit 상세(번역문 · 원문 · 근거 인용 포함)를 삭제 대상에 넣는다(D9-4). 늦은 검사 결과가 삭제 자료를 다시 남기지 않게 한다.

### 8.10 API(feature/api-tracking) 반영 후보 · 검증 한계

- 후보: 블록 검사 상태 필드, 규제 신호의 탐지 방식 · 판정 · 근거 목록, 경고 확인 식별(`acknowledgedWarnings`의 blockId+code 확장 · warningKey), 검사 진행 중 확정 거절. 이름 · 형식은 BE · FE가 정한다(#82).
- 구현 · 검증 현황은 `status.md` ⑧ 행. 실제 Gemini 스키마 수용 확인 · 실측은 #84, BE 통합 시험은 BE 연결 때 한다.

### 8.11 결과 표시(2026-10-12 PM 검토)

- 규칙 · LLM 결과는 같은 등급의 「규제 검증 결과」 한 신호로 보인다. 화면에 규칙/AI 출처나 불일치 안내를 두지 않는다. 내부 근거 · 결과(두 축 · producer)는 구분해 보존한다.
- 검사 실패 블록: 「⚠ 검사하지 못했습니다」 · 확정 시 확인 대상.
- `condition_unverifiable`: 확인할 조건(`unverified_conditions`)을 포함한 조건부 경고로 보인다. 같은 블록 · 같은 사전 항목(`external_id`)의 조건부 경고가 이미 있으면 하나만 보인다.
- `insufficient_evidence` · `insufficient_context`의 표시는 12월 계획에서 정한다(미결).
- 사전 기록만 근거인 risk(`basis_summary=dictionary_only`)를 법규 위반 확정처럼 표시하지 않는다.
- 신호 코드 · warningKey · 검사 상태 API 필드는 BE · FE 조정 사항이다(#82). AI 집계값을 경고 코드 · 확정 정책으로 직접 연결하지 않는다.

### 8.12 BE 연동 전 검토 항목(2026-10-22 사용자 결정)

아래는 BE 연동 작업을 시작할 때 일괄 검토할 항목이다. 모두 AI 측 제안 · BE 미합의이며 현재 운영 계약이 아니다. 새 필드 · 정책은 여기서 정하지 않는다. AI 내부 함수 · 검증 규칙(`contract.md` 5.3)은 구현 기준으로 유지한다(D12 보완 2).

| 검토 항목 | 제안 위치 | 관련 미결 |
|---|---|---|
| 입출력 계약 — 요청 구성(대상 · 문맥 · 라벨 · 로고 값 공급) · 보고 payload · 지문 · 봉투 Stage 추가 | 8.1 · `contract.md` 5.3 · `dev.md` 3.2 | — |
| 활성 검사 지정 — 실행마다 돌릴 검사와 그 구성을 어디에 둘지 | 8.1 · 8.4 | — |
| DB · 라우팅 · 복구 — 0008 후속 마이그레이션 · 디스패치 · 복구 · 배포 순서 · 등록 활성화 전 N5 작업 | 8.7 · 8.8 | #83 |
| 결과 저장 — 자기 producer 교체 · `compliance_flags` 7.1 확장 · 채택 인계 payload · `audit_log.detail` 보존 | 8.3 · `contract.md` 7.1 · `db-map.md` | — |
| 검사와 확정의 동시성 — 같은 job 잠금 · 확정 거절 · 확정 뒤 늦은 결과 거절 · N4→N5 전환 | 8.2 · 8.5 | — |
| API · 화면 연결 — 블록 검사 상태 · 신호 코드 · 경고 확인 식별 · 확정 거절 응답 · 결과 표시 | 8.10 · 8.11 | #82 |
| 통합 테스트 — BE 통합 시험 · 실제 Gemini 스키마 수용 확인 · 실측 | 8.10 | #84 |

## 9. 긴 섹션 타일 OCR 인계(2026-10-13)

규칙 정본은 `pipeline.md` 7.9절, 실측 기록은 `docs/ai-experiments/2026-10-13_02-ocr_tile-check.md`.

### 9.1 BE에 보이는 변화

- `analyze()`는 높이 > 4,000px 섹션(① 분해 실패로 원본 전체를 한 섹션으로 쓰는 경우 포함)을 OCR 처리용 타일로 나눠 처리한다. 타일은 의미 섹션이 아니며 저장되지 않는다. `AnalyzeResult` 형식 · 버전 · 좌표계(섹션 로컬)는 바뀌지 않는다.
- 인계 `analyze`의 `unsupported_input`(② 4,000px 초과) 실패는 더 이상 생기지 않는다. 대신 **새 실패 원인**: 경계를 옮긴 보완 OCR 후에도 타일 경계 충돌이 남으면 `AnalyzeError(OCR_FAILED, retryable=False)`다(기존 코드, #33 타일 범위 결정 2026-10-13). AI 인계 어댑터 보고는 기존 매핑대로 `model_call_failed` · `retryable=false`다. 메시지는 `OCR 타일 경계 충돌 미해결 section=… — 경계 n y…~… 사유`. 새 오류 코드는 없다. 잘림 없는 판독끼리 같은 자리에서 글자가 다르거나, 버려질 판독의 숫자 값이 남길 판독과 다르면(다수결 포함) 고르지 않고 이 실패가 된다 — 이 PC 실측에서 골든 긴 이미지 9장 중 5장(대조 타일링 5장)이 이 사유로 실패했다(예: `9.9%개선` / `9.99%개선`).
- `analyze` 입력 지문(`input_manifest`)의 `config.ocr`에 `tile_*` 6키가 더해져 **같은 원본이라도 `input_manifest_sha256`이 이전과 다르다.** OCR 동작이 바뀌었으므로 이전 분석 결과를 재사용하지 않는 것이 의도다. 계약 예제 `pipeline/samples/handoff/analyze.completed*.json` 2개를 다시 만들었다(지문 · 설정 외 변화 없음).
- **BE 재시도 연결(코드 확인)** — BE 워커(`app/flows/analysis.py` `execute_analyze`)는 `analyze()`를 직접 부르고 `AnalyzeError`의 code · message · retryable을 실패 봉투로 넘긴다. 채택(`AnalyzeHandler.adopt`) → `execution.adopt` → `_retry_or_fail_run`에서 retryable이 false면 자동 재시도 없이 `fail_run`(N2 오류, 남은 시도 종료 · 채택 전 섹션 삭제 · 기존 채택 결과 보존), true면 누적 한도(`N2_MAX_RETRY=2`) 안에서 자동 재시도한다. 부분 결과는 채택되지 않는다(실패 봉투에는 payload 결과 · 산출물이 없다). 사용자 [다시 시도](N2 오류)는 새 분석 실행을 시작하는 별도 경로라 retryable과 무관하게 그대로 쓸 수 있고 [중단](N2 → N1)도 그대로다. retryable은 DB 컬럼 · API 응답에 노출되지 않는다. 이 연결은 코드 읽기로 확인했고, BE 실행 테스트(`tests/test_exec_analysis.py`에 타일 충돌 즉시 실패 · 일시 오류 한도까지 재시도 2건 추가)는 이 PC에 Postgres · BE 의존성이 없어 **실행하지 않았다.**
- **입력 지문 · 재사용 영향** — BE 분석 매니페스트의 `analyzer`(`PipelineAnalyzer.impl_version` = `section` · `ocr` · `merge` 설정 해시)가 새 `tile_*` 키로 바뀌므로 배포 후 새로 만드는 분석 시도의 `input_fingerprint`가 이전과 다르다. BE는 분석 결과를 실행 사이에 재사용하지 않고, 채택 검증은 시도에 고정된 지문끼리 대조하므로(워커는 lease의 지문을 그대로 보고) 배포 시점에 진행 중인 시도가 지문 불일치로 거절되지는 않는다. API 서버와 OCR 워커의 설정이 다르면 매니페스트의 `analyzer`와 실제 실행 버전(봉투 `impl_version`)이 달라질 수 있으므로 함께 배포한다(기존 성질).
- `out_dir/ocr_debug/<section_key>.json` 진단 파일이 타일 섹션마다 생긴다(실패해도). 개발 확인용이며 인계 형식 · artifact가 아니다. 섹션 이미지 artifact 검증 규칙은 그대로다.
- 엔진 호출 수 = 타일 수 + 경계당 최대 1회 보완(상한 타일 수 × 2 − 1). 이 PC GPU(RTX 4060 Laptop, 모델 로드 제외) 실측: 37,665px 원본 OCR 약 12초(타일 24 · 보완 7), ①~③(LLM 없음) 약 17초. 메모리는 섹션 전체 배열(예: 1000×37,665×3 ≈ 113MB) + 타일 1장 추론분이다 — 측정값 아님.

### 9.2 BE 확인 필요

- ~~타일 충돌 실패의 재시도(#33)~~ — **2026-10-13 결정 · 구현**: 타일 경계 충돌 미해결은 `OCR_FAILED` · `retryable=false`(자동 재시도 없음). 근거: 이 PC GPU에서 실패 이미지 3장을 같은 프로세스 2회 · 새 프로세스 2회 돌린 검출 · 실패 내용이 모두 같았다(Linux · CPU 미확인). 그 밖의 `OCR_FAILED`는 기존대로 재시도 가능. BE 실행 테스트로 확인하는 것은 남았다.
- **#33 나머지** — 엔진 초기화 실패 중 영구 원인(의존성 · 모델명)과 일시 원인의 구분, 엔진 출력 형식 오류, 분류되지 않은 예외는 기존 값(재시도 가능)을 유지했다. `analyze()`가 설정 오류로 던지는 `ValueError`(③ · ② 설정 선검증)는 BE에서 `ANALYZE_UNEXPECTED` · 재시도 가능으로 기록된다(기존 동작, 이번 범위 밖).
- **긴 이미지 분석 실패의 운영 처리** — 위 판독 충돌 실패는 analyze 전체 실패다. 재시도해도 같은 결과일 가능성이 높으며, 대체 처리(예: 사용자 안내 · 수동 입력)를 둘지는 정해지지 않았다.

### 9.3 남은 검증

- Linux · CPU 배포 이미지에서 긴 원본(예: 1000×37,665)의 시간 · 최대 메모리 — 7.3과 같은 방식(새 CLI 프로세스 전체, `/usr/bin/time -v`)으로 `analyze` 또는 `ocr` CLI를 측정한다.
- 기준값(`[ocr] tile_*`)은 잠정이다. 실측 표본은 골든 9장 · 이 PC GPU다.
- **1.0.0 릴리즈 전 배포 환경 실측은 생략하기로 했다(사용자 결정 2026-10-13).** 위 Linux · CPU 시간 · 최대 메모리, 배포 환경의 짧은 · 긴 섹션 · 충돌 실패 경로, BE 재시도 실행은 미검증으로 남는다. 이 기능은 기존에 미지원(거부)이던 긴 입력의 일부를 처리하도록 개선한 것이며, 긴 이미지 지원 전체의 품질 · 배포 안정성이 검증된 것이 아니다. 표본 9장의 실패 비율은 운영 전체 실패율이 아니다.

### 9.4 호출별 중복 제거 · 겹친 검출 재인식(2026-10-27, 구현 중 · 일부 실측)

규칙은 `pipeline.md` 7.9절. 구현 · 검증 상태는 `status.md` ② 행이며 이 절은 BE에 보이는 변화만 적는다.

- `AnalyzeResult` 형식 · 버전 · 좌표계, 오류 코드 · `retryable`(타일 경계 충돌 미해결 `OCR_FAILED` · `retryable=false` 포함), API · DB는 바뀌지 않는다. 새 필드 · 컬럼 · 오류 코드는 없다.
- **영역 구성이 바뀔 수 있다** — 모든 섹션(짧은 섹션 포함)에서 엔진 호출 하나가 같은 텍스트를 거의 같은 polygon으로 두 번 낸 명백한 중복은 하나만 남는다. 긴 섹션의 타일 충돌 위치에서는 채택 조건을 통과한 겹친 검출 묶음이 재인식 검출 하나로 바뀐다(영역 수 · text · score · poly가 이전과 다르다). 같은 원본이라도 이전 분석과 `source_lines` · 블록 구성이 다를 수 있다.
- **입력 지문** — `analyze` 입력 지문(`input_manifest`)의 `config.ocr`에 `dedup_*` 2키 · `overlap_rec_*` 6키가 더해져 같은 원본이라도 `input_manifest_sha256`이 이전과 다르다(9.1의 `tile_*` 추가 때와 같은 성질). 계약 예제 `pipeline/samples/handoff/analyze.completed*.json` 2개를 공식 생성 경로(`python -m pipeline.handoff.examples`)로 다시 만들었다 — 차이는 `input_manifest.config.ocr`의 새 8키와 `input_manifest_sha256`뿐이고 payload는 같다(2026-10-27 확인).
- OCR 워커는 인식 모델을 추가로 부를 수 있다(섹션당 `ocr.overlap_rec_max_calls` 상한, 타일 충돌이 있을 때만). 시간 · 메모리 영향은 측정하지 않았다.
- BE 쪽 코드 변경은 필요 없을 것으로 보나, 배포 전 같은 설정으로 API 서버 · OCR 워커를 함께 배포해야 하는 점(9.1)은 그대로다. 운영 적용 여부 · 시점은 정하지 않았다.

## 10. OCR 후처리 인계(2026-10-23) — AI 내부 구현 · BE 확인 미진행

③ 병합 안의 OCR 후처리(여백 분리 · 세로 중심 줄 정렬 · 전각 변환, `open-questions.md` #86)를 AI 내부에 구현했다. 규칙 정본은 `pipeline.md` 7.1 · 7.2절, 코드는 `pipeline/stages/merge.py`, 테스트는 `tests/test_pipeline_merge_post.py`다. **이번 작업은 BE 연동 · 검토 · 확인 요청을 하지 않았고, BE 확인을 이 구현의 선행조건으로 두지 않는다.** 아래는 향후 확인을 위한 정리이며 합의 · 연동 검증 · 운영 적용 기록이 아니다. 새 API 필드 · DB 컬럼 · BE 코드 변경은 없다.

### 10.1 기본값과 BE에 보이는 변화

- **2026-10-26부터 기본값은 `merge.xycut` · `merge.num_o` `true`, `merge.fullwidth` `false`다**(6단계 결정, `docs/ai-experiments/2026-10-26_03-merge_post-step6-activation.md`). 저장소 기본 설정(`pipeline/config/default.toml`)으로 돌리면 `analyze()` 결과(블록 · 줄 · 순서 · `source_ko`)와 `llm_assist` 입력 · 프롬프트 · 기록이 10.2대로 바뀐다.
- **운영 적용 조건(BE 확인 필요)**: develop 머지와 dev 이미지(`pixlate-api:dev-<커밋>`) 게시는 운영이 아니므로 허용한다(2026-10-26 결정) — dev 이미지는 기본값(xycut · num_o 켬)으로 돌고, 10.2 미확인 · 보충 프롬프트 지문 미반영 상태다. **운영 배포(main · release)는 10.2 확인 · 보충 프롬프트의 인계 지문 반영 등 후속 연동이 끝난 뒤로 미룬다.** 그 전에 운영 배포가 필요하면 운영 설정(`PIXLATE_PIPELINE_CONFIG`로 지정하는 설정 사본)에서 `merge.xycut` · `merge.num_o`를 `false`로 둔다. 이번에 BE 확인 · 요청은 하지 않았다.
- 세 스위치를 모두 끄면 `analyze()` 결과와 `llm_assist` 입력 · 프롬프트 · 기록 형식은 기존과 같다(AI 내부 테스트로 확인 — 기존 ③ 경로 테스트는 세 스위치를 끈 설정으로 돈다).
- 단, `[merge]`에 키 5개(`xycut` · `fullwidth` · `xycut_gap_ratio` · `line_center_tol` · `line_center_tol_min_px`, 2026-10-26에 `num_o` 1개 더)가 더해져 설정 스냅샷이 달라진다. 인계 `analyze`의 `input_manifest.config.merge`와 `input_manifest_sha256`, BE `PipelineAnalyzer.impl_version`(`section` · `ocr` · `merge` 설정 해시, `app/ai_adapters.py`)이 같은 입력이라도 이전과 다르다. 9.1과 같은 성질이며, 계약 예제 `pipeline/samples/handoff/analyze.completed*.json` 2개를 다시 만들었다(설정 · 지문 외 변화 없음). 2026-10-26 기본값 변경으로 예제의 `input_manifest.config.merge`가 `xycut: true` · `num_o: true`가 되어 다시 만들었다(예제 블록 내용은 그대로, 지문만 바뀜).

### 10.2 스위치를 켰을 때의 영향 — 향후 확인 항목

`AnalyzeResult` 버전 · 필드 · 좌표계는 그대로지만 아래 **값의 의미**가 바뀐다. 운영 적용 전에 확인할 항목이다(BE 확인 필요, 10.1 운영 적용 조건).

| 대상 | 바뀌는 것 | 확인할 것 |
|---|---|---|
| 화면 순서 | `block_order`가 섹션 안 (y, x) 순에서 후보 영역 → 가로줄 → x 읽기 순서로 바뀐다(다단 배치에서 왼쪽 칸이 먼저) | N3 · N5 목록과 편집 화면이 `block_order`를 그대로 쓰는지, 다른 정렬(bbox 등)을 섞는지 |
| 원문 표시 · 수정 | `fullwidth` · `num_o`이면 `source_ko`와 `source_lines[].text`가 전각 · 숫자 속 o/O 보정 문장이다(예: `SPF 5O+` → `SPF 50+`, 1:1 치환). 원시 OCR 텍스트는 `source_lines[].regions[].text`에 남는다 | 화면 원문 · `edit_signal.before_text` 등이 어느 값을 쓰는지. 상위 계약 `source_ko` 정의 반영은 `open-questions.md` 3절 F |
| 줄 · 문단 구성 | 같은 원본이라도 줄 묶음 · 블록 수 · 역할이 달라질 수 있다. 위아래 여백으로만 나뉜 후보 영역끼리는 LLM이 한 블록으로 합칠 수 있고(2026-10-26), 나란한 칸(x 절단)을 넘는 LLM 병합은 나뉘며 그 조각의 역할은 휴리스틱 판정이다. 출력 형식은 같다 | 블록 수 · 역할 분포가 바뀌어도 하류 생성 조건 · 표시가 그대로 동작하는지 |
| 재분석 | 켜기 전후 분석 결과는 같은 작업이라도 블록 · 키 · 텍스트가 다르다 | 한 작업 안에서 이전 분석 결과와 새 결과를 섞어 쓰는 경로(되살리기 · 재판정 · 재분석)가 없는지 |
| 입력 지문 | ③-1 `blocks_fingerprint`(블록 키 · `source_ko`), ④ `input_fingerprint`(블록 전체), BE 분석 · 하류 매니페스트의 블록 해시(`app/flows/analysis.py` · `downstream.py` `stable_hash`)가 모두 새 값이 된다. 판정 구간(span)은 새 `source_ko` 기준으로 다시 산출되며 이전 구간을 옮겨 쓰지 않는다 | 지문 불일치 때 재사용 · 거절 동작이 의도대로인지(이전 결과를 새 블록에 붙이지 않는지) |
| `input_manifest` · 캐시 | `xycut`이면 `merge.prompt_path` 뒤에 `pipeline/prompts/merge_assist_region.md`를 붙여 호출한다(`pipeline/stages/merge.py` `validate_llm_config` · `REGION_PROMPT_PATH`). 현재 인계 매니페스트의 `prompts.merge`(`pipeline/handoff/analysis.py`, `_prompt_sha(cfg["merge"]["prompt_path"])`)는 기본 프롬프트 파일 해시만 담아 이 보충 프롬프트 내용이 지문에 들어가지 않는다. **2026-10-26부터 `xycut` 기본값이 켜져 기본 실행에도 해당한다** — 보충 프롬프트만 바뀌면 같은 지문으로 이전 분석이 재사용될 수 있다 | **운영 적용 전 해결 과제**: 보충 프롬프트를 지문에 넣는 방식(예: 실제 호출에 쓰는 이어 붙인 프롬프트의 해시를 담기 — 매니페스트 키 · 값의 모양은 BE 검증 대상이라 BE와 정한다). AI 인계 코드 변경 사항이며 이번에는 하지 않았다. 바꾸면 `pipeline/samples/handoff/analyze.completed*.json`을 다시 만든다(`open-questions.md` #86) |
| 분석 출력 폴더 | 켜면 `out_dir/merge_post_debug/`에 섹션별 후처리 진단과 `block_keys.json`이 생긴다(LLM 사용 여부와 관계없이, `pipeline.md` 7.1절). `ocr_debug/`와 같은 개발 확인용이며 인계 형식 · artifact가 아니다 | BE 워커가 `out_dir`의 낯선 파일 · 폴더를 거절하거나 업로드 대상으로 집계하지 않는지 |
| 개발용 고정 입력본 · 재생 | `blocks-input-v1` 등은 꺼진 상태의 ③ 출력이다. 켠 결과로 ③-1 · ④ · ⑤ 재생 기록을 돌리면 입력 지문 불일치로 거절된다(입력본 손상 아님) | — (AI 실측 범위) |

### 10.3 남은 것

- AI: 품질 · 파라미터 확인(4단계)은 마쳤다(2026-10-26, `docs/ai-experiments/2026-10-26_03-merge_post-step4-summary.md`). 기존 병합 · 역할 분류와의 연결 검증은 LLM 없는 켬/끔 비교로(`docs/ai-experiments/2026-10-23_03-merge_post-onoff-compare.md`), 같은 후보 영역의 교차 블록 병합 점검은 사람 검수 정성 의견 반영으로(2026-10-23, `docs/ai-experiments/2026-10-23_03-merge_post-gutter-check.md` 4.1 — 제품 라벨 사례 미평가) 마쳤다. 알려진 한계: LLM 없는 경로의 과분할, 라벨과 주변 문구의 오병합 가능성, 후보 영역 안 LLM 병합 감소, OCR 중복 검출(#40). 보류 기능 실험(5단계)은 마쳤다 — 숫자 속 o/O 교정을 채택해 `merge.num_o`로 넣었고, 겹친 앞 글자 제거 · 맨 앞 따옴표 제거는 보류했다(2026-10-26, `docs/ai-experiments/2026-10-26_03-merge_post-step5.md`). 6단계에서 기본값을 `xycut` · `num_o` 켬, `fullwidth` 끔으로 바꿨다(2026-10-26, `docs/ai-experiments/2026-10-26_03-merge_post-step6-activation.md`). 하류 실행 · 라벨 처리는 검증 완료가 아니다. 보충 프롬프트의 인계 지문 반영은 후속 연동 전 해결 과제로 남는다.
- BE: 위 10.2 항목 확인과 캐시 · 재분석 영향 확인은 운영 적용 전에 따로 요청한다(10.1 운영 적용 조건). 이번에는 BE 실행 테스트 · 연동 검증 · 확인 요청을 하지 않았다.
