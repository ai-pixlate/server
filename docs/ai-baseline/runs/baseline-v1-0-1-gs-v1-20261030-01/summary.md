# 공식 생성 종료 — 부분 실패

| 항목 | 결과 |
|---|---|
| 공식 회차 | baseline-v1-0-1-gs-v1-20261030-01 |
| 제품 | v1.0.1 / bfa3b700144467f4adf7d6802b7b2e93791fe6de |
| GS-01 | 최종 이미지 1장, AI 전체 제외 1장. N3·N5 예람 확인 완료 |
| GS-02 | 최종 이미지 12장, AI 전체 제외 2장. N3·N5 예람 확인 완료 |
| GS-03 | N2 분석 job 실패. 최초 1회 + 자동 재시도 2회 모두 504 DEADLINE_EXCEEDED. 18장 최종 이미지 없음; N3·N5 미도달 |
| 전체 34장 생성 상태 | 생성 13 / AI 전체 제외 3 / 실패 18 / 진행·확인 대기 0 |
| 체크포인트 | machine_snapshot: 13개 원본의 섹션 미리보기 67개 / service_final: 13장 |
| Gemini 요청 | 393회: HTTP 정상 응답 390회, 실패 3회. 확인 가능한 총 토큰 1,243,650; 실패 3회 토큰은 미제공 |
| 비용 | 산출 가능한 372회 단가 추정 USD 2.11574850. 미정 21회 예약 USD 21.84 유지. 예산 점유 USD 23.95574850; 실제 청구액 미확인 |
| 승인 한도 | USD 50 / 1,367회 / 호출당 예약 USD 1.04. 상한 초과 없음 |
| 실패·경고 | API 실패 요청 343·373·393. 제품 오류 ANALYZE_UNEXPECTED 및 상위 504 기록 보존. GS-01 번역 실패 2블록, GS-02 15블록을 사용자 확인 후 무수정 생성. Redis 진행률 저장 경고 보존 |
| 다음 단계 | 추가 생성·재생성 없이 종료. H01~H04 공식 채점은 PM 기준 자료 동결 후만 가능. H05 별도 서비스 평가 |

실패 18장은 GS-03 상품 단위 분석 실패로 인한 미생성이다. 각 원본에 N6 생성이 실행됐다는 의미가 아니며, 결과 요약용 N6 no_output 기록과 실제 실행 시도를 구분한다. AI 전체 제외 3장도 정답 적합성 검수 전이며 H01의 not_applicable로 확정하지 않았다.

비용 미정 21회는 정상 응답 중 thoughts_token_count 미제공 18회와 504 실패 3회다. 미제공 값을 0으로 임의 정산하지 않았다. 예약을 포함한 예산 점유는 실제 청구액이나 총 실제 사용 비용이 아니다.

자동 재시도는 동일 GS-03 job(3)의 analyze task 487 → 488 → 489로 수행됐다. 사용자·에이전트 수동 재생성이나 best-of 선택은 없으며, 채택 결과와 실패 시도의 원시 기록을 보존했다. 명시적 fallback 기록은 없다.

PM 기준 자료 미동결 상태를 유지한다. evaluation_sessions·evaluations.jsonl·defects.jsonl은 비어 있고 metrics.csv는 헤더만 있다. H01~H04 채점이나 품질 집계는 수행하지 않았다. 커밋·push·PR도 수행하지 않았다.

## 확인·증거

- [검증 결과](artifacts/completion-verification.json) / [사용자 실행 승인](artifacts/user-generation-approval.md) / [준비·테스트 근거](artifacts/preparation-evidence.json)
- [GS-01 N3](GS-01-N3-review.html) / [N3 응답](artifacts/GS-01-N3-user-response.json) / [GS-01 N5](GS-01-N5-review.html) / [N5 응답](artifacts/GS-01-N5-user-response.json)
- [GS-02 N3](GS-02-N3-review.html) / [N3 응답](artifacts/GS-02-N3-user-response.json) / [GS-02 N5](GS-02-N5-review.html) / [N5 응답](artifacts/GS-02-N5-user-response.json)
- [실행 기록](execution.jsonl) / [사용량](usage.jsonl) / [태스크 실행](dispatch.jsonl) / [단계 산출물](stages.jsonl) / [콘솔 기록](console.log)
- [GS-01 종료 상태](evidence/GS-01.json) / [GS-02 종료 상태](evidence/GS-02.json) / [GS-03 종료 상태](evidence/GS-03.json)

## 원본별 생성 상태

| 원본 | 생성 상태 | 최초 미리보기 수 | 최종 이미지 |
|---|---|---:|---|
| GS-01-IMG-001 | AI 전체 제외(적합성 미판정) | 0 | — |
| GS-01-IMG-002 | 최종 이미지 생성 | 23 | [이미지](artifacts/f05d0bea7bf2d46a68cbe7750782c32513097c39098cc03e71fae2363d6ec87c) |
| GS-02-IMG-001 | AI 전체 제외(적합성 미판정) | 0 | — |
| GS-02-IMG-002 | AI 전체 제외(적합성 미판정) | 0 | — |
| GS-02-IMG-003 | 최종 이미지 생성 | 1 | [이미지](artifacts/35e916077143c94d14cac865a44d1982ca22328e2a0a60b360e04f20af398297) |
| GS-02-IMG-004 | 최종 이미지 생성 | 2 | [이미지](artifacts/3592354ae17a39dd27d23d6529bc7e978a4af0308be19a7f6c4fa5f4fb3e93e1) |
| GS-02-IMG-005 | 최종 이미지 생성 | 1 | [이미지](artifacts/b495e3abe9555c61c254b0b21b7c68c00adbb996fe33458e21eaea5d984ee239) |
| GS-02-IMG-006 | 최종 이미지 생성 | 3 | [이미지](artifacts/93231987db243e6660a3179fbdb6a9837b3f0fc87cadd8dc23eda291d4230ca6) |
| GS-02-IMG-007 | 최종 이미지 생성 | 3 | [이미지](artifacts/0cfa9cc8eebc53f2beba30a4569f73f8aaa5e0f3e2aa63f37e8b45d7aab328b3) |
| GS-02-IMG-008 | 최종 이미지 생성 | 6 | [이미지](artifacts/9cdb52311aaaa53a3bbb8e91ccb1ef871f180d00a1d127cb8becf4a63da3d3c5) |
| GS-02-IMG-009 | 최종 이미지 생성 | 7 | [이미지](artifacts/3faa2c6706b691294c712e43c1ae7a6065a3a2720895d6aeb71f19de4b3b5613) |
| GS-02-IMG-010 | 최종 이미지 생성 | 3 | [이미지](artifacts/7383835dcd9b9b9b35bbccfb52c35dc6ececb00b4c1cab7838de1fa16e1e226c) |
| GS-02-IMG-011 | 최종 이미지 생성 | 3 | [이미지](artifacts/e3332c4559dbc3c2dcaa5a398f6a297d129fc0ad5b98d30513a65e27bb7f8dd8) |
| GS-02-IMG-012 | 최종 이미지 생성 | 1 | [이미지](artifacts/0cce9ef45a44bfac5a14057eb1803e13a6df5139232ab98629a083844660a7b9) |
| GS-02-IMG-013 | 최종 이미지 생성 | 2 | [이미지](artifacts/d6b4047a86256086489d95ebf163938dc0a873742870f7911321a68a1eb2fc71) |
| GS-02-IMG-014 | 최종 이미지 생성 | 12 | [이미지](artifacts/cb38551b682d5cdc4ea0d24e2980bf692748863f10aacb0ae5fa4ec116e1fa47) |
| GS-03-IMG-001 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-002 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-003 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-004 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-005 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-006 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-007 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-008 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-009 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-010 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-011 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-012 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-013 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-014 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-015 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-016 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-017 | 분석 job 실패·미생성 | 0 | — |
| GS-03-IMG-018 | 분석 job 실패·미생성 | 0 | — |
