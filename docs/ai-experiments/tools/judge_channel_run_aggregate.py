"""③-1 채널 정책 맥락 판정 실측 집계 — 드라이버(`judge_channel_run_driver.py`) 산출물을 읽어 표를 낸다(2026-11-27).

    python docs/ai-experiments/tools/judge_channel_run_aggregate.py pipeline/out/judge-channel-run-v1_<date> \
        [--country-run pipeline/out/judge-run-v1_2026-09-29]

내는 것: 채널 상태 · 버킷 권고 · 토큰 · 지연, 항목별 판정과 근거 출처, 패턴 신호 × 판정, 실패 목록, 해당 · 불확실 목록,
신호가 떴는데 해당 아님인 건. `--country-run`을 주면 국가 축 실측(개발용 `stages/judge.py` 산출물)에서 해당 · 불확실이던
섹션의 채널 판정을 나란히 낸다. 정답 라벨이 없으므로 건수는 관찰값이며 정확도가 아니다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--country-run", default=None)
    a = ap.parse_args(argv)
    run = Path(a.run)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    codes = {v: k for k, v in json.loads((run / "manifest.json").read_text(encoding="utf-8"))["experimental_codes"].items()}
    ids = sorted(codes.values())
    status, bucket, usage, durs = Counter(), Counter(), Counter(), []
    per_item = {c: Counter() for c in ids}
    src_kind = {c: Counter() for c in ids}
    signal, hits, fails, signal_absent, with_signal = Counter(), [], [], [], 0
    results: dict[str, dict[str, str]] = {}
    for f in sorted(run.glob("*/*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        r, call = d["result"], d["call"]
        name = f"{f.parent.name}/{f.stem}"
        status[r["channel_status"]] += 1
        bucket[r["bucket_recommendation"]] += 1
        for k, v in (call.get("usage") or {}).items():
            if isinstance(v, (int, float)):
                usage[k] += v
        if "duration_s" in call:
            durs.append(call["duration_s"])
        items = {it["id"]: it for it in call["payload"]["items"]}
        with_signal += any(it["matched_patterns"] for it in items.values())
        if r["channel_status"] != "completed":
            fails.append((name, r["channel_failure"]))
            continue
        block_text = {b["id"]: b["text"].replace("\n", " ") for b in call["payload"]["blocks"]}
        results[name] = {}
        for x in r["content_findings"]["findings"]:
            c = codes.get(x["content_type"])
            if c is None:
                continue
            results[name][c] = x["status"]
            per_item[c][x["status"]] += 1
            src_kind[c][x["evidence_source"]] += 1
            pats = items[c]["matched_patterns"]
            signal[(c, x["status"], "신호 있음" if pats else "신호 없음")] += 1
            if x["status"] in ("present", "uncertain"):
                text = " / ".join(d["block_texts"][k].replace("\n", " ") for k in x["evidence_block_ids"])
                hits.append((name, c, x["status"], x["evidence_source"], pats, text[:200], x["reason"][:200]))
            elif pats:
                text = " / ".join(block_text[b][:90] for b in items[c]["candidate_blocks"])
                signal_absent.append((name, c, pats, text, x["reason"][:200]))

    print(f"섹션 {sum(status.values())}개 · 채널 상태 {dict(status)} · 버킷 권고 {dict(bucket)} · 패턴 신호가 뜬 섹션 {with_signal}개")
    print("토큰 합계", dict(usage))
    if durs:
        q = sorted(durs)
        print(f"지연(호출당) 중앙값 {statistics.median(q):.1f}s · 90% {q[max(0, int(len(q) * 0.9) - 1)]:.1f}s · 최대 {q[-1]:.1f}s · 합계 {sum(q):.0f}s")
    print("\n항목별 판정")
    for c in ids:
        print(f"  {c}: {dict(per_item[c])} · 근거 출처 {dict(src_kind[c])}")
    print("\n패턴 신호 × 판정")
    for k in sorted(signal):
        print(f"  {k[0]} {k[1]:9} {k[2]}: {signal[k]}")
    print(f"\n실패 {len(fails)}건")
    for name, fl in fails:
        print(f"  {name} {fl}")
    print(f"\n해당 · 불확실 {len(hits)}건(섹션 {len({h[0] for h in hits})}개)")
    for h in hits:
        print(f"- {h[0]} {h[1]} {h[2]} ({h[3]}) 신호={h[4]}\n    근거: {h[5]}\n    사유: {h[6]}")
    print(f"\n신호가 떴는데 해당 아님 {len(signal_absent)}건")
    for h in signal_absent:
        print(f"- {h[0]} {h[1]} 신호={h[2]}\n    후보 블록: {h[3]}\n    사유: {h[4]}")
    if a.country_run:
        old = Path(a.country_run)
        print("\n국가 축 실측에서 해당 · 불확실이던 섹션의 채널 판정")
        for f in sorted(old.glob("*/judge/*.json")):
            findings = (json.loads(f.read_text(encoding="utf-8")).get("content_findings") or {}).get("findings", [])
            got = [(x["content_type"], x["status"]) for x in findings if x["content_type"].startswith("LC-") and x["status"] in ("present", "uncertain")]
            if got:
                name = f"{f.parent.parent.name}/{f.stem}"
                print(f"- {name}: 국가 {got} → 채널 {results.get(name) or '판정 없음'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
