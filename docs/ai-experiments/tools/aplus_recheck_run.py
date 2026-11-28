"""A+ 압축 뒤 규칙 검사 실행 도구 — 이미 만든 압축 결과(`aplus_compress.json`) 하나에 recheck.run을 돌린다(2026-11-26). 모델 호출 없음.

    python docs/ai-experiments/tools/aplus_recheck_run.py --compress <폴더>/aplus_compress.json --sources <폴더>/block_sources.json \
        --regulatory-class cosmetic --out pipeline/out/aplus-recheck-trial_<date>_<상품> [--dict-dir <정규화 사전 폴더>]

입력 파일은 읽기만 한다. 기존 출력 폴더는 덮어쓰지 않는다.
- 규제 행은 로컬 정규화 사전(`judge.dict_dir`, git 제외)의 규제사전에서 `--regulatory-class`의 적용 분류 행만 고른다(`policy_rules.json`의
  `regulatory_class_map`). 판정값은 사전의 원값 그대로 넘긴다(운영 묶음의 서비스 판정값 변환을 거치지 않는다 — `allowed`만 뜻이 쓰인다).
- 용어 목록은 주지 않는다(용어집을 아직 받지 않았다) — 강제 용어는 검사하지 않고 결과에 `terms_checked=false`로 남는다.
출력: manifest.json(입력 경로 · 해시 · 사전 버전 · 적용 분류 · 코드 버전) · aplus_recheck.json(결과) · aplus_recheck_debug.json(기록).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.aplus import recheck  # noqa: E402
from pipeline.aplus.label import json_recorder  # noqa: E402
from pipeline.dictionary import load_dictionaries  # noqa: E402
from pipeline.run import _safe_console  # noqa: E402
from pipeline.stages.text_check import RegRow  # noqa: E402
from pipeline.types import AplusCompressResult  # noqa: E402


def main() -> int:
    _safe_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--compress", required=True, help="압축 결과 aplus_compress.json")
    ap.add_argument("--sources", required=True, help="블록 키 → 한국어 원문 JSON(block_sources.json)")
    ap.add_argument("--regulatory-class", required=True, choices=["cosmetic", "otc", "combination", "unknown"], help="상품 규제 분류")
    ap.add_argument("--dict-dir", default=None, help="정규화 사전 폴더(기본: 설정 judge.dict_dir)")
    ap.add_argument("--out", required=True, help="새 출력 폴더")
    a = ap.parse_args()

    compress_path, sources_path = Path(a.compress).resolve(), Path(a.sources).resolve()
    result = AplusCompressResult.model_validate(json.loads(compress_path.read_text(encoding="utf-8")))
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    dict_dir = Path(a.dict_dir or cfgmod.load_config()["judge"]["dict_dir"])
    dict_dir = dict_dir if dict_dir.is_absolute() else ROOT / dict_dir
    dicts = load_dictionaries(dict_dir)
    applied = list(dicts.rules.regulatory_class_map[a.regulatory_class].applied_classes)
    entries = [e for e in dicts.regulation.entries if e.regulatory_class in applied]
    rows = [RegRow(e.id, e.verdict_status, tuple(e.variant_en)) for e in entries]
    targets = recheck.find_targets(result)
    print(f"압축 대상 {len(result.fields)} · 문구가 바뀐 칸 {len(targets)} · 규제 행 {len(rows)}(적용 분류 {applied}) · 용어 목록 없음")

    out = Path(a.out).resolve()
    if out.exists():
        print(f"이미 있는 출력 폴더는 덮어쓰지 않는다: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True)
    manifest = {
        "tool": "aplus_recheck_run", "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "compress_path": str(compress_path), "compress_sha256": jsonio.sha256_file(compress_path),
        "sources_path": str(sources_path), "sources_sha256": jsonio.sha256_file(sources_path),
        "dict_dir": str(dict_dir), "dictionary_version": dicts.dictionary_version, "dictionary_sha256": dicts.fingerprint,
        "dictionary_source": dicts.regulation.source.model_dump(mode="json"),
        "regulatory_class": a.regulatory_class, "applied_classes": applied, "regulation_ids": [r.external_id for r in rows],
        "terms": None, "rules": recheck.RULES, "git": jsonio.git_state(),
        "note": "규제 분류는 실행 인자로 준 값이다(셀러 선택값이 아니다). 용어 목록이 없어 강제 용어는 검사하지 않았다. 검출은 위반 확정이 아니다.",
    }
    jsonio.write_text_atomic(out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    res = recheck.run(result, sources, rows, None, recorder=json_recorder(out / "aplus_recheck_debug.json"))
    jsonio.write_model(out / "aplus_recheck.json", res)
    print(f"상태 {res.status} · 강제 용어 검사 {'함' if res.terms_checked else '안 함'}" + (f" · 오류 {res.error}" if res.error else ""))
    for f in res.fields:
        hits = ", ".join(f"{x['external_id']} «{x['problem_text']}»" for x in f.flags if x["type"] == "regulatory") or "없음"
        print(f"  칸 {f.slot} {f.field} [{f.block_key}] 규제 매칭 {hits}")
        print(f"      문구: {f.text}")
    print(f"규제 매칭이 있는 칸 {sum(any(x['type'] == 'regulatory' for x in f.flags) for f in res.fields)}/{len(res.fields)}")
    return 0 if res.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
