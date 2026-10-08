# be-handoff.md — BE 워커가 부르는 AI 함수의 입력·출력과 BE 처리(공동 확인용 예제)

> 작성: 2026-10-08, `feature/be-ai-pipeline-integration`. 정의 코드는 `app/ai_adapters.py`(타입)·`app/flows/`(검증·저장)다.
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
- **⑧ 공급**: 근거 분석 실행의 저장 묶음, 섹션 전체 블록, 대상·revision, 보존 성공분(출처 시도 포함), 표현 지시, 용어집(#11 확정 전 **대상 언어 전체**). 표현 지시는 #71 확정 전(`ALTERNATIVES_SPLIT=False`)에는 만들지 않는다. 묶음·용어집·지시의 지문을 시도 명세 `supply`에 고정하고 실행 때 대조해 다르면 `INPUT_CHANGED`.
- **지문**: `app.manifest`는 `pipeline.handoff.canonical`(JCS)에 위임한다. BE·AI 지문 구현은 하나다.
- **AI 실패 보고**: 블록별 결과가 있으면 항목별 실패로, 없으면 `AdapterFailed`(`TRANSLATE_<KIND>` 등)로 기록한다. 오류 코드 문자열은 task `errorCode` 후보이며 API enum이 아니다.
- **GPU 정리**: BE가 폐기를 확정하면 `handoff_status`가 `discard`를 돌려주고 AI `cleanup()`이 `verified`·`adopted`·`discard`일 때만 지운다. 인계 없이 권한을 잃은 시도만 BE가 따로 폐기 여부를 확인한다.
- **남은 것**: 번역문 독립 검사 단계(`run_text_check`) 연결, GPU 제어 HTTP 바인딩, #71 구분 규칙 확정 후 대체 표현 배열 변환·⑧ 표현 지시 활성화, #11 검색 방식 확정 후 용어집 공급 교체.

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

0절 「남은 것」과 [PR #55 BE 확인](https://github.com/ai-pixlate/server/pull/55#issuecomment-6054998455)의 AI 요청(대상 섹션 지정·근거 배열·검증기 경로·정리 상태·예제 재생성의 Unicode 버전 의존)을 따른다. ⑧ 대체 표현 구분자(#71)와 용어집 검색 방식(#11)은 임시 공급 규칙으로 연결했으며 결정되면 변환·공급만 바꾼다.
