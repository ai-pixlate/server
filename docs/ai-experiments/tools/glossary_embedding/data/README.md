# data — 실제 데이터 배치 위치 (git 제외)

이 폴더의 데이터 파일은 `.gitignore`로 올리지 않는다. 실행 전에 아래 위치에 직접 둔다.

| 경로 | 내용 | 2026-10-06 실험에 쓴 파일 (sha256) |
|---|---|---|
| `deliveries/2026-09-28/` | 데이터팀 용어집 확정본 3종 + 현지부적합 사전(음성 문항 근거) | `glossary_certification.xlsx` `a66aa4ad…` · `glossary_ingredient.xlsx` `54978a3e…` · `glossary_phrase.xlsx` `0e4117aa…` · `locale_unsuitable_dict.xlsx` `c3e8f974…` |
| `db_snapshot/` | 운영 RDS `glossary` 내보내기(내부 PK 포함, JSONL + meta) — 내보내기 명령은 상위 README | `glossary_db_20261001.jsonl` `bbf60daa…` · `glossary_db_20261001.meta.json` `b11eb829…` |
| `eval_team_v2.csv` | 평가셋 v2 — `python make_eval_team.py`가 `deliveries/<전달본>/`에서 만든다 | 892문항 |

폴더마다 `SHA256SUMS`를 함께 두면 어느 파일로 실험했는지 남는다.
