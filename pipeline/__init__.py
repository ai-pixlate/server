"""Pixlate AI 파이프라인 코드.

정본 문서는 docs/ai/ (README.md → pipeline.md → contract.md → dev.md → status.md).
이 패키지의 구조·실행법·환경은 docs/ai/dev.md, 구현 현황은 docs/ai/status.md에 있다.

- types.py   단계 간 입출력 타입 (pydantic)
- errors.py  AnalyzeError · 오류 코드 정책
- config.py  파라미터 정본(config/default.toml) 로더 · 실행 인자 override
- stages/    단계 구현 (①~③ 초기 분석부터)
- analyze.py ①→②→③을 잇는 analyze()
- run.py     단계별 실행 CLI  (python -m pipeline.run --help)
- inspect.py 결과 JSON을 이미지 위에 그리는 확인 도구
"""
