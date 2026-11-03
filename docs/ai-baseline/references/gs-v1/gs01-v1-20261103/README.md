# GS-01 기준 자료 부분 동결

- 기준 버전: `gs-v1-gs01-20261103-v1`
- 범위: GS-01_001 11개 + GS-01_002 47개 = **2장·58개**. GS-02·03 32장·273개는 미동결이다.
- 승인 근거: [PM 최종 코멘트](https://github.com/ai-pixlate/server/pull/93#issuecomment-6600118259), [보존 원문](pm-approval.json).
- 기존 56개 승인과 신규 REF-010 `[245,923,375,958]`·REF-011 `[624,923,756,958]` 조건을 대조했다. 두 좌표는 이전 커밋에서 이미 동일하게 반영돼 이번에 재수정하지 않았다. PM이 추가 확인 불필요로 지정한 조건을 충족한다.
- [동결 JSONL](reference_units.jsonl) SHA-256: `3b67ec85b335c96d17b72e9469567ab925139d5e863612a6edc002d8ca88967f`
- 버전·시각·대상 초안 커밋·해시: [freeze-manifest.json](freeze-manifest.json).

기존 ID·중요도·좌표·기대값·H01/H04 적용 여부를 보존했다. 검증자/시각/근거와 기준 버전, 승인에 따른 human_confirmation_required만 변경했다. expected_value.note 및 location.description의 재확인 대기 문구는 작성 당시 이력이다. 현재 상태는 freeze-manifest와 verified_*를 따른다. artifacts의 문서 사본도 당시 근거이며 현재 운영 안내가 아니다. 사본의 상대 Markdown 링크는 원래 저장소 위치를 기준으로 해석한다.

GS-01_001은 전체 제외 정답으로 H01·H04 미적용이다. GS-01_002는 전체 유지, H01 적용, critical_fact 28개의 H04 양 체크포인트 적용이다. 미국 시험/인증 증빙을 새로 확보하거나 규제 적합성을 승인한 것은 아니다.

이번 동결은 사용자가 요청한 **로컬 부분 동결**이다. 이번 보존 PR은 PM 기준 자료 승인 근거와 로컬 동결본을 공개한다. GitHub 정본 반영은 develop 병합 이후이며 AI 평가 결과 검수 완료를 뜻하지 않는다. PR #93 병합/종료는 하지 않았다. PM의 기준 자료 승인과 생성 결과의 사람 검수는 별개다. 나중에 기대값이 바뀌면 이 파일을 덮어쓰지 않고 새 기준 버전과 재평가 이력을 만든다.

부분 평가는 [별도 평가 묶음](../../../evaluations/baseline-v1-0-1-gs01-20261103/README.md)에서 수행한다. 최초 34장 회차는 수정하지 않는다.

## GitHub 보존 방식

[보존 색인·복원·PM 검수 안내](../../../evaluations/baseline-v1-0-1-gs01-20261103/PRESERVATION.md)를 따른다. 기존 증거는 같은 해시의 GitHub 파일로 연결하고 중복 업로드하지 않았다. 이 README는 웹 열람용 안내이며 최초 안내 원문은 `pre-publication-readme.txt`에 보존했다. 원시 JSONL·평가값·최초 회차는 변경하지 않았다.
