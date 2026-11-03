# 최초 Baseline 보존 및 품질 평가 준비 점검

- 회차: `baseline-v1-0-1-gs-v1-20261030-01`
- 제품: v1.0.1 / `bfa3b700144467f4adf7d6802b7b2e93791fe6de`
- 원시 파일·manifest·이미지·평가 파일은 변경하지 않았다. 이 폴더는 보존/준비 점검의 추가 기록이며 공식 채점 결과가 아니다.
- PM 기준 자료는 미동결. 별도 초안 사본은 검수 전 상태와 null 값을 그대로 보존했다.

## 보존과 무결성

- 기존 1,980개 파일(397,960,918 bytes)의 SHA-256 목록을 새로 고정했다. 이미 등록된 manifest/object/runtime 해시와 대조했고 원시 파일의 변경 없음도 재확인한다.
- 원본 34장·최초 미리보기 67개·최종 이미지 13장: 해시·이미지 구조·크기 검증. object-index의 654개 키/383개 고유 파일 검증. 기준 초안 324개 항목의 원본 ID·해시·좌표 범위 확인.
- 모델 자산 31개·제품 설정 17개·실행 도구 16개 해시와 의존성 lock 확인. 설정/프롬프트/폰트와 도구 사본은 reproduction에 추가 보존. 실행 사전·용어집은 기존 references.json과 runtime 해시로 연결된다.
- Windows 및 승인된 의존성 차이는 기존 manifest/runtime 기록을 유지한다. 해시 확인은 비결정적인 모델 응답의 동일 재현을 보장하지 않는다.
- 로컬 백업: `C:\Users\Yeram\Documents\pixlate-server\.cache\baseline-backups\baseline-v1-0-1-gs-v1-20261030-01\20261030T042213Z\snapshot.zip`. 백업 결과·해시·전수 대조는 [백업 영수증](backup-receipt.json)에 기록한다. 같은 C: 디스크이므로 장치 장애 대비 원격/외장 백업은 아니다. .cache 정리 대상에서 제외해야 한다.
- 모델 가중치·venv·wheelhouse는 현재 로컬 자산 해시만 확인했으며 이 회차 백업에 복제하지 않았다. DB의 상품별 JSON 증거와 사전은 보존했지만 전체 PostgreSQL 복원용 덤프는 아니다.

## 실패 해석과 증거 한계

- GS-03은 job 3의 N2 분석 실패 1개 그룹이며 시도 487/488/489(최초+자동 재시도 2회)의 504 기록을 연결한다. 영향 원본은 18개지만 독립적인 quality_defect 18건이나 critical 18건을 만들지 않는다.
- 원본별 산출물 부재는 그대로 남긴다. 동결 후 H01 적용 대상이면 실패 분모에 포함하고 H02/H03의 산출물 부재는 A/B/C로 강제하지 않는다. 실패 18장을 품질 결함 중복 제거를 이유로 평가 모집단에서 제거하지 않는다.
- GS-03_013~018은 분석 루프 미도달이다. result_only N6 no_output은 최종 산출물 부재 요약이며 N6 실제 호출 18건이 아니다.
- GS-03의 임시 섹션 이미지·merge_debug 파일은 제품의 임시 디렉터리 정리로 소실돼 복원/보존 완료로 표시하지 않는다. stages.jsonl의 부분 OCR/병합 결과, dispatch/usage/console, DB 실패 증거는 남아 있다.
- 3장 AI 전체 제외는 적합성 미확정이다. PM 동결 전 not_applicable 또는 decision_wrong으로 확정하지 않는다.

## H01~H04 준비

| 지표 | 확보 자료 | 공식 평가 전 남은 일 |
|---|---|---|
| H01 | 34개 원본·최종 실행 상태·13개 최종 산출물·전체 제외 판단 증거 | PM의 원본 전체 적용/제외 기대값 동결 후 분모/분자 확정 |
| H02 | 13개 원본의 최초 미리보기 67개와 N5 수정 전 상태·N3 무수정 확인 | 섹션 순서/원본 좌표를 따라 검수; 결함 기록 후 A/B/C 계산 |
| H03 | 최종 이미지 13개·deliverable/섹션 배치·무수정 확인 | 최종 결과를 별도 검수; H02 등급 자동 복사 금지 |
| H04 | 핵심 사실 후보 199개·원본 좌표·초기/최종 증거 연결 | PM이 중요도/기대값/H04 두 체크포인트 적용 여부 확정; 실제 이미지 판독 확인 |

- machine_snapshot은 원본 단위 완성 합성 이미지가 아니라 섹션 미리보기 묶음이다. [연결 색인](evaluation-inputs.json)의 original_top_offset, preview target_id, final_placement.stack_offset으로 원본/최초/최종 좌표계를 구분한다. 기준 단위와 실제 결과 위치의 정밀 대응은 평가 시 확인한다.
- 검수 전 초안의 basis_refs 일부는 과거 파일명을 사용한다. protocol-dataset.md는 초안 폴더의 protocol-dataset.txt와 같은 해시로 확인했다. 현 회차의 동명 최신 protocol-dataset.md는 다른 해시이므로 바꿔치기하지 않는다. 동결본 반입 때 근거 파일을 별도 경로로 보존하고 정확한 경로/해시를 연결해야 한다.
- GS-01 번역 실패 경고 2건·GS-02 15건 및 폭 초과 신호는 검수 근거다. 신호만으로 품질 결함이나 심각도를 확정하지 않는다. H05는 별도 서비스 평가이며 0건으로 집계하지 않는다.

## 예산 기록

- 실제 청구액: 미확인. 단가 계산 가능한 372회 추정 $2.11574850; 미정 21회 예약 $21.84; 예산 점유 $23.95574850; $50 대비 잔여 $26.04425150.
- 393회 호출/1,367회 상한. 정상 응답 390회·504 3회. 응답에 제공된 총 토큰 1,243,650; 실패 3회의 토큰은 미확인. 미정 예약은 thoughts_token_count 미제공 정상 응답 18회와 실패 3회다. 미제공 값을 0으로 정산하거나 예약을 실제 비용으로 표시하지 않는다.
- 예산 잔여는 추가 생성 승인으로 해석하지 않는다. 청구 명세 확보 시 요청/기간 연결 가능성을 확인한 뒤 별도 정산 기록을 추가한다.

## PM 동결 후 순서

1. #93의 의견을 기준 초안에 반영하고 PM 재확인, 중요도·적용 여부·전체 제외·불확실 항목을 정리한다. 별도 references/gs-v1/ 동결본과 승인 PR/커밋·검수 근거를 확보한다.
2. 회차 원본 34장과 동결본의 ID/해시를 대조하고 기준 단위·근거 파일을 반입한다. 최초 manifest/빈 평가 파일은 이 백업으로 보존하고, 이후 정식 절차로 reference_units.jsonl 및 reference_freeze를 연결한다. 검증 없이 기존 초안을 단순 복사해 동결하지 않는다.
3. 실제 Codex 채팅 모델·설정·지침·응답 기록을 확인해 평가 세션을 등록한다. 미확인 모델 이름을 임의 입력하지 않는다.
4. H01 실행 증거와 PM 기대값 대조 → H02/H03 이미지 결함 검수 → H04 사실별 보존/판독 확인. 산출물 부재·판단 불가를 별도 처리하고 GS-03 실행 실패를 품질 결함으로 복제하지 않는다.
5. PM이 K01~K09, critical/major, 불확실 판단과 기존 규칙의 A등급 표본을 검수한다. AI 최초 기록과 사람 수정 기록을 구분해 보존한다.
6. 예외/분모·중복 제거·연결/해시를 검증한 뒤 공식 집계한다. 현재는 evaluations/defects/metrics를 채우지 않는다.

## 원본 34장 연결

| 원본 | 상태 | 원본 이미지 | 최초 미리보기 | 최종 이미지 | 초안 단위 |
|---|---|---|---:|---|---:|
| GS-01-IMG-001 | AI 전체 제외·적합성 미확정 | [원본](../artifacts/0a411962375a3d78ff41de4abb596c6cea2ed4a494a9c1f769bfa66c6eeb1065.jpg) | 0 | 없음 | 9 |
| GS-01-IMG-002 | 생성 완료 | [원본](../artifacts/babb327b014557c3ea8413375756cbdcb86e9b9dce64ba0251df179f70692549.jpg) | 23 | [최종](../artifacts/f05d0bea7bf2d46a68cbe7750782c32513097c39098cc03e71fae2363d6ec87c) | 42 |
| GS-02-IMG-001 | AI 전체 제외·적합성 미확정 | [원본](../artifacts/574dff04e7fdea3dcdc8cbc3507e7e2ca015e05ac9e6962ffcf5dfbf935bb5a4.jpg) | 0 | 없음 | 7 |
| GS-02-IMG-002 | AI 전체 제외·적합성 미확정 | [원본](../artifacts/91dee0b2018a89e3c4e595bfd4e05eaaef141198284f5add4d1c65a3feee272a.jpg) | 0 | 없음 | 7 |
| GS-02-IMG-003 | 생성 완료 | [원본](../artifacts/ca750554556880bd7437669d4bc0c3b5dc7fc8d742d5ba95a9da0efbd4cbb137.jpg) | 1 | [최종](../artifacts/35e916077143c94d14cac865a44d1982ca22328e2a0a60b360e04f20af398297) | 4 |
| GS-02-IMG-004 | 생성 완료 | [원본](../artifacts/a565751f851e61176a1dfc2b016c814e7a50867b5966454bbad2f2491f8f94a3.jpg) | 2 | [최종](../artifacts/3592354ae17a39dd27d23d6529bc7e978a4af0308be19a7f6c4fa5f4fb3e93e1) | 5 |
| GS-02-IMG-005 | 생성 완료 | [원본](../artifacts/bd7eed2473ba95571701d0e2c253b473138c9618896de0378232df9e7d8522b4.jpg) | 1 | [최종](../artifacts/b495e3abe9555c61c254b0b21b7c68c00adbb996fe33458e21eaea5d984ee239) | 4 |
| GS-02-IMG-006 | 생성 완료 | [원본](../artifacts/2a1eceb4bcfcbfa56cacf98b06e45659e81d73b841cd989fbdc380f02a31166b.jpg) | 3 | [최종](../artifacts/93231987db243e6660a3179fbdb6a9837b3f0fc87cadd8dc23eda291d4230ca6) | 5 |
| GS-02-IMG-007 | 생성 완료 | [원본](../artifacts/0d864d1e374d620caf359dbd338293f9e2b462093b5ab69f269ac8720b72ed86.jpg) | 3 | [최종](../artifacts/0cfa9cc8eebc53f2beba30a4569f73f8aaa5e0f3e2aa63f37e8b45d7aab328b3) | 17 |
| GS-02-IMG-008 | 생성 완료 | [원본](../artifacts/46cf244036432f63ee6d4ce43b6730928ac1a42049957bf0fea906bfc3bde5b1.jpg) | 6 | [최종](../artifacts/9cdb52311aaaa53a3bbb8e91ccb1ef871f180d00a1d127cb8becf4a63da3d3c5) | 13 |
| GS-02-IMG-009 | 생성 완료 | [원본](../artifacts/034bd1b15e99e2668c4eaba5706c55bc4d9a99d89275668a28bc56c480ba0910.jpg) | 7 | [최종](../artifacts/3faa2c6706b691294c712e43c1ae7a6065a3a2720895d6aeb71f19de4b3b5613) | 21 |
| GS-02-IMG-010 | 생성 완료 | [원본](../artifacts/d90d50f7c5561998d7dadbab37a869805644459b9cc560ce6af2b24e58ead8f5.jpg) | 3 | [최종](../artifacts/7383835dcd9b9b9b35bbccfb52c35dc6ececb00b4c1cab7838de1fa16e1e226c) | 6 |
| GS-02-IMG-011 | 생성 완료 | [원본](../artifacts/6e147ae35388daf40d607bcde6d0120343c24e4e208e1ff3877da2ba83249b39.jpg) | 3 | [최종](../artifacts/e3332c4559dbc3c2dcaa5a398f6a297d129fc0ad5b98d30513a65e27bb7f8dd8) | 7 |
| GS-02-IMG-012 | 생성 완료 | [원본](../artifacts/031be239854f57cbabacca94612f9f9cb2ff53ddd50f351f73431e69a96a04fe.jpg) | 1 | [최종](../artifacts/0cce9ef45a44bfac5a14057eb1803e13a6df5139232ab98629a083844660a7b9) | 7 |
| GS-02-IMG-013 | 생성 완료 | [원본](../artifacts/8cd5c87dcaa94cf0663194cbe5663563d8603def8ac92002aa0d9c2ca9896def.jpg) | 2 | [최종](../artifacts/d6b4047a86256086489d95ebf163938dc0a873742870f7911321a68a1eb2fc71) | 7 |
| GS-02-IMG-014 | 생성 완료 | [원본](../artifacts/07f12a483261b3d331f07f407200b2bd5abd8055e4ec302d3dfeb22325d6ca81.jpg) | 12 | [최종](../artifacts/cb38551b682d5cdc4ea0d24e2980bf692748863f10aacb0ae5fa4ec116e1fa47) | 30 |
| GS-03-IMG-001 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/d2c0bad4477a37946cc85bfcd5ad9e7ec5f1363802024486090e54ff34ce795c.jpg) | 0 | 없음 | 5 |
| GS-03-IMG-002 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/dc5cf0ef8e6ffa958d12301513bb170fe9581b46974edefa85b04f53b8735e20.jpg) | 0 | 없음 | 17 |
| GS-03-IMG-003 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/632923c8a59c305536b33e1e4b4a4c6ab44858497537bf262fd9d2a3284c84ec.jpg) | 0 | 없음 | 2 |
| GS-03-IMG-004 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/084e5d6d909291d0316a8d2f25b0209a52641ea142433c7fa3f7330272c7a87b.jpg) | 0 | 없음 | 9 |
| GS-03-IMG-005 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/c3ff29efa99f0d62d454b524fa50783a865efef114feae58cca2b7c5b4d9e410.jpg) | 0 | 없음 | 6 |
| GS-03-IMG-006 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/878981d535b0a4d364cba2b18c26b1cc95b47a41cfe48c2600d934a9107ca688.jpg) | 0 | 없음 | 5 |
| GS-03-IMG-007 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/853787635a81786d449ed4751b9413c01bcf6080b9d0aadbd75a183000d955e1.jpg) | 0 | 없음 | 2 |
| GS-03-IMG-008 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/5cf2e07f61f0adfe8689ffdffb8e45fac94100e5d17f2f059a0763bf2b003e2c.jpg) | 0 | 없음 | 6 |
| GS-03-IMG-009 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/b43eb392346e81b08d957c5d4731ec54e939d5f40ef6ab4b5fe98d96f22725e9.jpg) | 0 | 없음 | 3 |
| GS-03-IMG-010 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/ed95189b58e38f11e22eae4f03ce9379e371f2b5aa7db3cbfff3f43cf35a11ea.jpg) | 0 | 없음 | 10 |
| GS-03-IMG-011 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/38cad424c13d356eae6e482bb4cfc8e7998ddded6df04e073d4859b9f4ec546d.jpg) | 0 | 없음 | 4 |
| GS-03-IMG-012 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/20e1aac327245c05cf5c5f0f919f7822e974b700a11c07979545710f7138c39f.jpg) | 0 | 없음 | 7 |
| GS-03-IMG-013 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/4585ffd307e9ec9e5c042644c00d951f02d06679593569f091338c2fb2f33be0.jpg) | 0 | 없음 | 7 |
| GS-03-IMG-014 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/3e658d865927409bbddb98f7ccabf47feeab615a13796d40a0b9545e96ad5a2f.jpg) | 0 | 없음 | 8 |
| GS-03-IMG-015 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/4251c02b6e935006f5d5848ebb846e1530b0d6fb31546999dea5a7224fd60e2a.jpg) | 0 | 없음 | 6 |
| GS-03-IMG-016 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/d2493815f7b7cc96cbed23b5b64b10c25b1aa7493abc7ff5c19d028bd4152b58.jpg) | 0 | 없음 | 3 |
| GS-03-IMG-017 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/3cec5ca60131774810cca56040d4775d9376a0ebc53073b218814dd6b5b51231.jpg) | 0 | 없음 | 4 |
| GS-03-IMG-018 | GS-03 상품 분석 실패 영향 | [원본](../artifacts/d541ca2aee1d54dbd85b8ef5c434044cd8e05fe93cb58f667a189f72dc00c3be.jpg) | 0 | 없음 | 29 |
