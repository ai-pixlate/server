"""05_experiments.py · experiments_eval.py 공용 — 결과 저장."""
import datetime as dt
import json

import config


def save(part: str, obj) -> None:
    out = config.ROOT / "results" / dt.date.today().isoformat()
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{part}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n저장: {p}")
