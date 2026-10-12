# be-handoff.md — BE 워커가 부르는 AI 함수의 입력·출력과 BE 처리(공동 확인용 예제)

> 작성: 2026-10-08, `feature/be-ai-pipeline-integration` · 6절 추가 2026-10-12(D11 역할별 스타일 기본값). 정의 코드는 `app/ai_adapters.py`(타입)·`app/flows/`(검증·저장)다.
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
- **남은 것**: 번역문 독립 검사 단계(`run_text_check`) 연결, GPU 제어 HTTP 바인딩, #71 구분 규칙 확정 후 대체 표현 배열 변환·⑧ 표현 지시 활성화.

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

[D11](integration-decisions.md)의 사용자 승인값을 AI가 설정 자료 · 폰트 · 계산 함수 · 사례로 준비했다. **운영 렌더 연결은 하지 않았다.** 현행 `app/typeset.py`의 `PIXLATE_STYLE_DEFAULTS` 로더는 역할별 `font_color` · `est_font_px` · `align`만 읽고 폰트 · 굵기 · 자간 · 기준 폭을 모른다. 새 자료를 그 로더에 넣는다고 동작하지 않는다(형식이 다르고, 기본 크기가 원본 폭에 따라 달라진다). 6.6의 BE 수정이 필요하다.

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

- 크기: 측정 `est_font_px`는 원본 px 그대로. 기본값만 `기준 크기 × 원본 폭 / 375`(곱한 뒤 한 번 나눔, 반올림 없음). 1080px 원본의 title = 92.16, body = 46.08. 측정값에 1080/375나 1.35를 곱하지 않는다(사례 07).
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

이번 AI 작업에서 아래 운영 코드는 바꾸지 않았다.

1. **`app/typeset.py` 로더 교체**: `load_role_defaults()`가 `pipeline.handoff.style_defaults.load_style_defaults()` + `verify_fonts()`를 쓰게 한다. 경로 기본값은 저장소 정본, `PIXLATE_STYLE_DEFAULTS`는 대체 경로로 두는 안을 추천한다(정책 규칙 `PIXLATE_POLICY_RULES`와 같은 방식). 폰트 검증 실패는 기존 `StyleDefaultsUnavailable`처럼 렌더 실패로 처리하고 내장 폰트로 그리지 않는다. `deploy/pixlate.env.example` 설명도 함께 고친다.
2. **스타일 해석**: `resolve_style()`을 `resolve_block()`으로 대체하거나 같은 규칙으로 고친다. 원본 폭(섹션 `bbox.w`)과 섹션의 ⑦ 시도 결과(done/failed)를 넘긴다. 현재는 단계 상태 없이 style NULL만 본다.
3. **실제 폰트 · 굵기 · 자간 · 행간 적용**: `_font()`의 `ImageFont.load_default` 대신 역할 굵기의 실제 OTF를 적용 크기(소수 허용)로 연다. 자간 · 행간은 적용값의 px를 쓴다(`LINE_SPACING` 상수 대신 설정 자료의 비율).
4. **넘침 계산을 실제 조판과 같은 기준으로**: 폭은 자간을 포함한 실제 줄 폭, 높이는 줄 수 × 행간 px. 높이가 영역과 같으면 넘침이 아니다. 넘쳐도 크기 축소 · 문구 축약 · 측정값 교체를 하지 않는다. 운영 엔진의 자간 끝 처리 · 커닝 기준은 #79.
5. **입력 지문**: `app/flows/downstream.py`의 `_render_inputs`(미리보기 — 최종 렌더의 `app/flows/final.py` `_final_inputs`도 이것을 부른다)와 `_final_inputs`의 `style_defaults`를 `render_inputs(d)`(설정 버전 · SHA-256 · 폰트 SHA-256 · 규칙 버전)로 바꾸고, `RENDERER_VERSION`의 렌더 라이브러리 버전을 유지한다. `_render_section`의 대조도 같은 값으로 한다(다르면 `INPUT_CHANGED`).
6. **미리보기 · 최종 렌더 공통**: 두 경로가 같은 로더 · 같은 `resolve_block` · 같은 조판 함수를 쓴다. 최종 렌더가 미리보기와 다른 설정을 읽을 수 없게 한다.
7. **기록**: `text_block.style`은 측정값 그대로 둔다. 렌더 payload의 `applied` · `source`를 6.4 형식으로 넓힌다(저장 위치는 #78).
8. **테스트**: `tests/exec_fakes.style_defaults_file`(테스트 전용 3키 형식)을 새 형식에 맞추고 6.7 사례를 BE 통합 시험으로 옮긴다. N4/N5/N6 상태 전이(D9-3)는 바꾸지 않는다.

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
| 07_fractional_measured | 측정 37.25 · 18.6 유지(배율 · 1.35 미적용, 기본값보다 작아도 유지) |
| 08_wrap_overflow | `\n` 2줄 · 높이 48 = 넘침 아님 · 47 = 넘침 · 긴 단어 폭 넘침 · 빈 줄 유지 |

BE 통합 시험의 기대 결과(DB 필요, AI 쪽 미실행):
- 사례 입력을 `text_block` 행(`is_excluded` · `source_ko` · `trans_1` · `style` · `role` · `bbox`/`layout_bbox`)과 섹션의 ⑦ 시도 상태로 만들고 미리보기 → N5 → 최종 렌더를 돌린다.
- 미리보기 payload와 최종 렌더 `renders[*].applied`가 사례의 `applied` · `source`와 같다. `text_block.style`은 렌더 전후 같다.
- ⑦ 실패 섹션도 미리보기 · 최종 렌더가 성공하고, N5 표시가 없으며(D9-3) ⑦ 시도는 failed로 남는다.
- 설정 자료 · 폰트 파일 · 규칙 버전 중 하나를 바꾸면 고정 입력 대조에서 `INPUT_CHANGED`.
- 폰트 파일이 없거나 해시가 다르면 렌더 실패(내장 폰트로 성공하지 않음).
- 08 사례의 `text_block.overflow`가 사례 layout의 넘침과 같다(804는 운영 엔진 결과를 기록).

### 6.8 남은 것 · 검증 한계

- 운영 연결(6.6)과 BE · DB 통합 시험은 아직이다. 이 PC에는 BE · DB 시험 환경이 없어 AI 쪽 단위 · 참고 렌더 검증만 했다.
- #78 적용값 기록 위치 · 키, #79 운영 조판 엔진의 측정 기준(자간 끝 · 커닝 · 서브픽셀)은 BE 확인이 필요하다. 폰트 파일은 저장소에 포함한다(2026-10-12 사용자 결정, #80 해결).
- 디자인 원본 이미지는 이 작업에서 직접 열람하지 않았다. 토큰 값은 사용자가 전달한 승인표 기준이다.
