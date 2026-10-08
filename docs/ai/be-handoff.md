# be-handoff.md — BE 워커가 부르는 AI 함수의 입력·출력과 BE 처리(공동 확인용 예제)

> 작성: 2026-10-08, `feature/be-ai-pipeline-integration`. 정의 코드는 `app/ai_adapters.py`(타입)·`app/flows/`(검증·저장)다.
> 성격: BE 구현이 실제로 요구·검증하는 형식의 예제와 기대 결과다. 새 계약 결정이 아니며, 유형 코드·프롬프트·모델은 AI 소유다.
> 모든 키는 BE가 준 임시 키(`sec_<id>`·`blk_<id>`)를 그대로 돌려준다. 모르는 키는 저장 실패다(D3). 파일은 절대 로컬 경로로만 주고받는다.

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

- ③-1: 위 출력 형식으로 A안을 내줄 수 있는지, 묶음 입력형으로 현재 개발 입력(SourceInfo·policy_rules·근거 URL 필수)을 대체할 방법.
- ⑧: 문맥 블록·대상 플래그 입력으로 충분한지, 용어집(glossary_ids)·영어 재대조 결과를 어떤 단계가 낼지.
- ①: 분해 실패 시 원본 전체 대체 섹션을 `AnalyzeResult`로 돌려줄 수 있는지, 오류 코드별 `retryable`.
