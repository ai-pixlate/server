"""③-1 · ③-1' 실측 드라이버 — `blocks-input-v1` 섹션마다 judge(LLM 1회 호출 시도) → policy(가정한 분류)를 이어 돌린다(2026-09-29).

    python docs/ai-experiments/tools/judge_run_driver.py --input pipeline/samples/local/blocks-input-v1 \
        --classes docs/ai-experiments/2026-09-29_03-1-judge_class-assumption.json --out pipeline/out/judge-run-v1_<date> \
        [--sources GS-01_001,GS-01_002] [--limit N] [--dry-run] [--extra-classes combination,unknown] [--set 표.키=값]

범위(사용자 승인 2026-09-29): 100섹션에 각 **1회 호출 시도, 자동 재시도 없음**. 실패 섹션은 judge `failed` · policy `incomplete`로 집계하고 다음 섹션을
계속 처리한다. 분류는 실험용 가정(`--classes`)이며 결과에 "가정한 분류에 따른 정책 동작 실험"을 표시한다. `--extra-classes`는 같은 JudgeResult에
분류만 바꿔 정책을 추가 실행한다(LLM 호출 없음, policy_<class>/ 에 저장). 기존 출력 폴더는 덮어쓰지 않는다. `--dry-run`은 LLM을 부르지 않고 입력 ·
설정 · 사전 · 프롬프트 · 키 존재를 점검하고 페이로드 크기를 센다.
실행 기록 manifest.json: 입력 · 분류 목록(해시) · 사전 버전 · 해시 · 프롬프트 해시 · 설정 스냅샷 · 코드 버전(git · 매칭 규칙 · 정책 구현) · 범위 문구.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.run import _neighbor_texts  # noqa: E402
from pipeline.stages import judge, policy  # noqa: E402
from pipeline.types import JudgeContext  # noqa: E402
from pipeline.vlm import sha256_text  # noqa: E402

SCOPE_NOTE = "100섹션 각 1회 호출 시도 · 자동 재시도 없음 · 실패 섹션은 failed/incomplete로 집계하고 계속 · 분류는 실험용 가정(가정한 분류에 따른 정책 동작 실험)"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--classes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sources", default=None, help="쉼표로 구분한 원본 ID(생략 시 전체)")
    ap.add_argument("--limit", type=int, default=None, help="처리할 섹션 수 상한(스모크용)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--extra-classes", default="", help="같은 JudgeResult에 분류만 바꿔 정책을 추가 실행(LLM 호출 없음)")
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args(argv)

    cfg = cfgmod.load_config(overrides=a.set)
    judge.validate_config(cfg)
    policy.validate_config(cfg)
    prompt = judge.validate_llm_config(cfg, need_api_key=False)
    dicts = judge.load_dicts(cfg)
    inp, out = Path(a.input), Path(a.out)
    classes_path = Path(a.classes)
    classes = json.loads(classes_path.read_text(encoding="utf-8"))["classes"]
    extra = [c for c in a.extra_classes.split(",") if c]
    if out.exists():
        print(f"오류: 출력 폴더가 이미 있다 — 덮어쓰지 않는다: {out}", file=sys.stderr)
        return 1
    have_key = bool(os.environ.get(judge.API_KEY_ENV))
    if not a.dry_run and not have_key:
        print(f"오류: 환경변수 {judge.API_KEY_ENV}가 없다", file=sys.stderr)
        return 1

    sources = sorted(p for p in inp.iterdir() if p.is_dir() and (p / "merge").is_dir())
    if a.sources:
        wanted = set(a.sources.split(","))
        sources = [p for p in sources if p.name in wanted]
    missing = [p.name for p in sources if p.name not in classes]
    if missing:
        print(f"오류: 분류 가정이 없는 원본 {missing}", file=sys.stderr)
        return 1

    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": SCOPE_NOTE,
        "input": str(inp), "classes_file": str(classes_path), "classes_sha256": _sha(classes_path),
        "classes": {k: v["regulatory_class"] for k, v in classes.items()},
        "extra_classes": extra,
        "dictionary_version": dicts.dictionary_version, "dictionary_fingerprint": dicts.fingerprint,
        "prompt_path": cfg["judge"]["prompt_path"], "prompt_sha256": sha256_text(prompt),
        "config": cfgmod.snapshot(cfg), "git": jsonio.git_state(),
        "match_rules_version": judge.MATCH_RULES_VERSION, "policy_impl_version": policy.RULES_IMPL_VERSION,
        "dry_run": a.dry_run, "api_key_present": have_key,
    }
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    rows = []
    usage_total: Counter = Counter()
    n_done = 0
    for src in sources:
        split = jsonio.load_split(src / "split.json")
        merges = sorted((src / "merge").glob("*.json"))
        order = {s.section_key: s.section_order for s in split.sections}
        merges.sort(key=lambda p: order.get(p.stem, 0))
        cls = classes[src.name]["regulatory_class"]
        for mp in merges:
            if a.limit is not None and n_done >= a.limit:
                break
            n_done += 1
            merged = jsonio.load_merge(mp)
            key = merged.section_key
            sec = next(s for s in split.sections if s.section_key == key)
            prev_t, next_t = _neighbor_texts(split, sec, mp, int(cfg["judge"]["context_sections"]))
            ctx = JudgeContext(regulatory_class=cls, prev_section_text=prev_t, next_section_text=next_t)
            row = {"source": src.name, "section": key, "regulatory_class": cls, "blocks": len(merged.blocks)}
            if a.dry_run:
                view = dicts.judge_view()
                matches = judge.detect(key, merged.blocks, view, cfg)
                payload, _, sent = judge.build_payload(key, merged.blocks, view, ctx, cfg, matches)
                row.update({"payload_chars": len(judge.payload_text(payload)), "items_sent": len(sent), "matches": len(matches),
                            "image_exists": Path(sec.image_path).exists()})
                rows.append(row)
                continue
            t0 = time.perf_counter()
            try:
                jr = judge.run(sec, merged.blocks, ctx, cfg, dicts=dicts, recorder=judge.json_recorder(out / src.name / "judge_debug"))
            except Exception as e:  # noqa: BLE001 — 드라이버 오류(입력 · 설정)는 기록하고 계속
                row.update({"judge_status": "driver_error", "error": f"{e.__class__.__name__}: {e}", "duration_s": round(time.perf_counter() - t0, 3)})
                rows.append(row)
                print(f"{src.name}/{key}: 드라이버 오류 {e}", file=sys.stderr)
                continue
            dur = round(time.perf_counter() - t0, 3)
            jsonio.write_model(out / src.name / "judge" / f"{key}.json", jr)
            rec_path = out / src.name / "judge_debug" / f"{key}.json"
            usage = None
            if rec_path.exists():
                usage = json.loads(rec_path.read_text(encoding="utf-8")).get("usage")
            if isinstance(usage, dict):
                for k, v in usage.items():
                    if isinstance(v, (int, float)):
                        usage_total[k] += v
            pr = policy.run(jr, merged.blocks, ctx, cfg, dicts=dicts)
            jsonio.write_model(out / src.name / "policy" / f"{key}.json", pr)
            extras = {}
            for ec in extra:
                pe = policy.run(jr, merged.blocks, JudgeContext(regulatory_class=ec, prev_section_text=prev_t, next_section_text=next_t), cfg, dicts=dicts)
                jsonio.write_model(out / src.name / f"policy_{ec}" / f"{key}.json", pe)
                extras[ec] = {"status": pe.status, "bucket": pe.bucket_recommendation, "verdicts": len(pe.verdicts)}
            st = Counter(f.status for f in jr.content_findings.findings) if jr.content_findings else {}
            row.update({
                "judge_status": jr.status, "error": jr.error, "duration_s": dur, "usage": usage,
                "findings": len(jr.content_findings.findings) if jr.content_findings else None,
                "status_counts": dict(st), "matches": len(jr.matches),
                "evidence_sources": dict(Counter(f.evidence_source for f in jr.content_findings.findings)) if jr.content_findings else {},
                "policy_status": pr.status, "bucket": pr.bucket_recommendation, "verdicts": len(pr.verdicts),
                "conflicts": len(pr.conflicts), "suppressed": len(pr.suppressed),
                "dict_coverage": pr.applied.dict_coverage if pr.applied else None, "extra": extras,
            })
            rows.append(row)
            print(f"{src.name}/{key}: judge {jr.status} · policy {pr.status} {pr.bucket_recommendation or ''} · {dur}s")
        if a.limit is not None and n_done >= a.limit:
            break

    summary = {
        "sections": len(rows),
        "judge_status": dict(Counter(r.get("judge_status", "dry") for r in rows)),
        "policy_status": dict(Counter(r.get("policy_status") for r in rows if "policy_status" in r)),
        "bucket": dict(Counter(r.get("bucket") for r in rows if "bucket" in r)),
        "usage_total": dict(usage_total),
        "duration_s": {"total": round(sum(r.get("duration_s", 0) for r in rows), 1)},
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": SCOPE_NOTE,
    }
    if a.dry_run:
        summary["payload_chars"] = {"total": sum(r["payload_chars"] for r in rows), "max": max((r["payload_chars"] for r in rows), default=0)}
        summary["images_missing"] = [f"{r['source']}/{r['section']}" for r in rows if not r["image_exists"]]
    (out / "sections.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
