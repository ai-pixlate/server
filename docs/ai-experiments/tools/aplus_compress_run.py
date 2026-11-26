"""A+ 압축(2차 번역) 실행 도구 — 이미 만든 7칸 구성 결과(`aplus_plan.json`) 하나에 compress.run을 돌린다(2026-11-26).

    python docs/ai-experiments/tools/aplus_compress_run.py --plan <폴더>/aplus_plan.json --out pipeline/out/aplus-compress-trial_<date>_<상품> \
        [--dry-run] [--set 표.키=값]

입력 파일은 읽기만 한다. 기존 출력 폴더는 덮어쓰지 않는다. 모델 호출은 상품당 1 + `aplus_compress.max_retries`회까지다(글만 보낸다).
`--dry-run`은 모델을 부르지 않고 대상 칸과 보낼 페이로드 크기만 센다.
출력: manifest.json(입력 경로 · 해시 · 설정 · 프롬프트 해시 · 코드 버전) · aplus_compress.json(결과) · aplus_compress_debug.json(호출 기록).
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
from pipeline.aplus import compress, content  # noqa: E402
from pipeline.aplus.label import json_recorder  # noqa: E402
from pipeline.aplus.spec import load_spec  # noqa: E402
from pipeline.run import _safe_console  # noqa: E402
from pipeline.types import AplusPlan  # noqa: E402
from pipeline.vlm import sha256_text  # noqa: E402


def main() -> int:
    _safe_console()
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="7칸 구성 결과 aplus_plan.json")
    ap.add_argument("--out", required=True, help="새 출력 폴더")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--set", action="append", default=[], dest="sets", metavar="표.키=값")
    a = ap.parse_args()

    plan_path = Path(a.plan).resolve()
    plan = AplusPlan.model_validate(json.loads(plan_path.read_text(encoding="utf-8")))
    spec = load_spec()
    cfg = cfgmod.load_config(overrides=a.sets)
    targets = compress.find_targets(plan.slots or [], spec)
    total = sum(len(content.limit_checks(s.module, spec)) for s in plan.slots or [] if s.module is not None)
    print(f"글 칸 {total} · 대상(템플릿 한도 초과) {len(targets)} · 호출 최대 {1 + int(cfg[compress.TABLE]['max_retries']) if targets else 0}회")
    if a.dry_run:
        modules = {s.slot: s.module for s in plan.slots if s.module is not None}
        payload = json.dumps(compress.build_payload(modules, targets, spec), ensure_ascii=False, indent=1)
        for t in targets:
            print(f"  {t.id} 칸 {t.slot} {t.field} {t.check.chars}/{t.check.max_chars} | {t.primary}")
        print(f"페이로드 {len(payload)}자")
        return 0

    out = Path(a.out).resolve()
    if out.exists():
        print(f"이미 있는 출력 폴더는 덮어쓰지 않는다: {out}", file=sys.stderr)
        return 2
    out.mkdir(parents=True)
    prompt = compress.validate_config(cfg, need_api_key=bool(targets))
    manifest = {
        "tool": "aplus_compress_run", "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_path": str(plan_path), "plan_sha256": jsonio.sha256_file(plan_path), "spec_version": spec.version, "spec_sha256": spec.sha256,
        "config": cfg[compress.TABLE], "overrides": a.sets, "prompt_sha256": sha256_text(prompt), "git": jsonio.git_state(),
        "note": "입력 번역은 용어집 · 표현 지시 없이 만든 개발용이다. 결과는 사람 검수 전이다.",
    }
    jsonio.write_text_atomic(out / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    result = compress.run(plan, spec, cfg, recorder=json_recorder(out / "aplus_compress_debug.json"))
    jsonio.write_model(out / "aplus_compress.json", result)
    print(f"상태 {result.status} · 호출 {result.checked.calls}회" + (f" · 오류 {result.error}" if result.error else ""))
    for f in result.fields:
        print(f"  칸 {f.slot} {f.field} {f.primary_chars}/{f.max_chars} → {f.outcome}({f.reason}, {f.attempts}회)"
              f"{'' if f.answer_chars is None else f' {f.answer_chars}자'}")
        print(f"      1차: {f.primary_text}")
        if f.answer_text is not None:
            print(f"      답 : {f.answer_text}")
    done = sum(f.outcome == "compressed" for f in result.fields)
    print(f"한도 안으로 들어온 칸 {done}/{len(result.fields)}")
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
