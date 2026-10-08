"""AI ↔ BE 운영 인계 어댑터 [docs/ai/integration-decisions.md D1~D9 · 5.24~5.32, docs/ai/dev.md 3.2].

단계 함수(pipeline/stages)를 바꾸지 않고, BE 워커가 부르는 요청 → 보고(StageReport) 경계를 제공한다.

- envelope.py     공통 봉투 · 실패 분류 · 산출물 목록
- canonical.py    JCS 정규 JSON · SHA-256 지문
- analysis.py     ①②③ run_analyze (분해 실패 → 원본 전체 섹션 대체)
- bundle.py       BE 고정 사전 묶음 · content_type 대응표 검증
- judgment.py     ③-1 · ③-1′ run_judgment (현지 8 + 규제 검출 1, 감싸기 억제)
- downstream.py   ④ run_label · ⑤ run_logo · ⑥ run_inpaint · ⑦ run_style
- translation.py  ⑧ run_translate · 번역문 검사 run_text_check
- gpu.py          GPU 실행기(BE 제어 인터페이스 Protocol 소비 · 갱신 · 업로드 · 정리)
- validate.py     BE 채택 전 보고 검증 validate_report
- examples.py     계약 예제 생성(pipeline/samples/handoff/)
"""
