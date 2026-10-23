"""④ 제품 라벨 판정 실측 드라이버 — `blocks-input-v1` 섹션마다 label.run(VLM 1회 호출 시도)을 돌린다(2026-09-29).

    python docs/ai-experiments/tools/label_run_driver.py --input pipeline/samples/local/blocks-input-v1 --out pipeline/out/label-run-v1_<date> \
        [--sections GS-01_001/sec_1_01,GS-02_008/sec_1_04] [--dry-run] [--no-overlay] [--set 표.키=값]

범위(사용자 승인 2026-09-29, open-questions #66): ④ **단독** 성능 실험. 입력본의 개발 대상 섹션(merge/ 가 있는 섹션, 품질 표본 12개는 입력본에 없음)만
쓰며, 개발 대상 전체를 사용자가 최종 포함한 섹션으로 간주하지 않는다. 섹션마다 **1회 호출 시도 · 자동 재시도 없음**, 실패는 기록하고 다음 섹션을
계속 처리한다. 실패 섹션은 명시적으로 `--sections`로 골라 **새 --out**에서 다시 돌린다. 기존 출력 폴더는 덮어쓰지 않는다. 입력본은 읽기만 한다.
`--dry-run`은 VLM을 부르지 않고 호출 수 · 보낼 블록 · 이미지 축소 크기 · 페이로드 크기를 센다(비용 산정 근거).
출력: manifest.json(입력 · 설정 · 프롬프트 해시 · 코드 버전 · SDK 재시도 정보) · sections.json(섹션별 상태 · 판정 수 · 사용량 · 소요 시간) ·
summary.json(ok/failed · 전체 성공 여부) · <원본>/label/<key>.json · <원본>/label_debug/<key>.json · <원본>/overlay/<key>.png.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import inspect as insp  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.run import _safe_console  # noqa: E402
from pipeline.stages import label  # noqa: E402
from pipeline.vlm import sdk_info, sha256_text  # noqa: E402

SCOPE_NOTE = ("④ 단독 성능 실험 — 개발 대상 섹션 각 1회 호출 시도 · 자동 재시도 없음 · 실패는 기록하고 계속 · "
              "개발 대상 전체를 사용자 최종 포함 섹션으로 간주하지 않음 · 정답 전수 검수 전")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def iter_sections(inp: Path, wanted: set[str] | None):
    """(원본 폴더, split, merge 경로, Section) — 원본 이름 순 → section_order 순. merge/ 가 있는 섹션만(개발 대상)."""
    for src in sorted(p for p in inp.iterdir() if p.is_dir() and (p / "merge").is_dir()):
        split = jsonio.load_split(src / "split.json")
        order = {s.section_key: s for s in split.sections}
        for mp in sorted((src / "merge").glob("*.json"), key=lambda p: order[p.stem].section_order if p.stem in order else 0):
            if wanted is not None and f"{src.name}/{mp.stem}" not in wanted:
                continue
            yield src, split, mp, order[mp.stem]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sections", default=None, help="쉼표로 구분한 <원본>/<section_key> (생략 시 개발 대상 전체)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-overlay", action="store_true")
    ap.add_argument("--set", action="append", default=[])
    a = ap.parse_args(argv)
    _safe_console()

    cfg = cfgmod.load_config(overrides=a.set)
    prompt = label.validate_llm_config(cfg, need_api_key=False)
    inp, out = Path(a.input), Path(a.out)
    if out.exists():
        print(f"오류: 출력 폴더가 이미 있다 — 덮어쓰지 않는다: {out}", file=sys.stderr)
        return 1
    have_key = bool(os.environ.get(label.API_KEY_ENV))
    if not a.dry_run and not have_key:
        print(f"오류: 환경변수 {label.API_KEY_ENV}가 없다", file=sys.stderr)
        return 1
    wanted = set(a.sections.split(",")) if a.sections else None
    targets = list(iter_sections(inp, wanted))
    if wanted is not None:
        found = {f"{s.name}/{m.stem}" for s, _, m, _ in targets}
        if wanted - found:
            print(f"오류: 입력본에 없는 섹션 {sorted(wanted - found)}", file=sys.stderr)
            return 1

    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scope": SCOPE_NOTE, "input": str(inp),
        "input_sha256sums": _sha(inp / "SHA256SUMS") if (inp / "SHA256SUMS").exists() else None,
        "sections_requested": sorted(wanted) if wanted else "all",
        "prompt_path": cfg["label"]["prompt_path"], "prompt_sha256": sha256_text(prompt),
        "config": cfgmod.snapshot(cfg), "git": jsonio.git_state(), "sdk": sdk_info(),
        "dry_run": a.dry_run, "api_key_present": have_key,
    }
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    rows = []
    usage_total: Counter = Counter()
    for src, split, mp, sec in targets:
        merged = jsonio.load_merge(mp)
        key = merged.section_key
        blank = [b.block_key for b in merged.blocks if label.is_blank(b)]
        sent = [b for b in sorted(merged.blocks, key=lambda b: b.block_order) if not label.is_blank(b)]
        row = {"source": src.name, "section": key, "blocks": len(merged.blocks), "sent": len(sent), "blank": len(blank),
               "size": [sec.width, sec.height]}
        if not a.dry_run:  # 모든 행이 같은 키를 갖게 한다 — 입력 오류(driver_error) 행도 집계에서 빠지지 않게
            row.update({"status": None, "record_status": None, "error": None, "llm_called": False, "true": None, "false_vlm": None,
                        "sent_size": None, "duration_s": None, "call_duration_s": None, "usage": None, "result": None})
        if a.dry_run:
            scale = min(1.0, cfg["label"]["long_side_px"] / max(sec.width, sec.height))
            payload, _ = label.build_payload(sec, sent)
            row.update({"call": bool(sent), "sent_size": [round(sec.width * scale), round(sec.height * scale)],
                        "payload_chars": len(label.payload_text(payload)) if sent else 0, "image_exists": Path(sec.image_path).exists()})
            rows.append(row)
            continue
        t0 = time.perf_counter()
        try:
            res = label.run(sec, merged.blocks, cfg, recorder=label.json_recorder(out / src.name / "label_debug"))
        except Exception as e:  # noqa: BLE001 — 입력 · 설정 오류는 기록하고 계속(판정 실패와 구분)
            row.update({"status": "driver_error", "error": f"{e.__class__.__name__}: {e}", "duration_s": round(time.perf_counter() - t0, 3)})
            rows.append(row)
            print(f"{src.name}/{key}: 드라이버 오류 {e}", file=sys.stderr)
            continue
        dur = round(time.perf_counter() - t0, 3)
        lp = jsonio.write_model(out / src.name / "label" / f"{key}.json", res)
        rec_path = out / src.name / "label_debug" / f"{key}.json"
        rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else {}  # 실패 결과의 기록 저장 실패는 결과 error에 남는다
        usage = rec.get("usage")
        if isinstance(usage, dict):
            for k, v in usage.items():
                if isinstance(v, (int, float)):
                    usage_total[k] += v
        if not a.no_overlay:
            insp.overlay_label(sec.image_path, merged, res, out / src.name / "overlay" / f"{key}.png")
        row.update({
            "status": res.status, "record_status": rec.get("status"), "error": res.error, "llm_called": res.checked.llm_called,
            "true": sum(1 for d in res.labels if d.is_product_label) if res.labels is not None else None,
            "false_vlm": sum(1 for d in res.labels if not d.is_product_label and d.basis == "vlm") if res.labels is not None else None,
            "sent_size": (rec.get("image") or {}).get("sent_size"), "duration_s": dur, "call_duration_s": rec.get("call_duration_s"),
            "usage": usage, "result": str(lp.relative_to(out)),
        })
        rows.append(row)
        print(f"{src.name}/{key}: {res.status} · 라벨 {row['true']} / 보냄 {len(sent)} · {dur}s")

    summary: dict = {"sections": len(rows), "scope": SCOPE_NOTE, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if a.dry_run:
        calls = [r for r in rows if r["call"]]
        summary.update({
            "calls": len(calls), "no_call_sections": len(rows) - len(calls),
            "blocks": sum(r["blocks"] for r in rows), "sent_blocks": sum(r["sent"] for r in rows), "blank_blocks": sum(r["blank"] for r in rows),
            "payload_chars": {"total": sum(r["payload_chars"] for r in rows), "max": max((r["payload_chars"] for r in rows), default=0)},
            "sent_width_min": min((r["sent_size"][0] for r in calls), default=None),
            "narrow_sent_images_lt_400px": sum(1 for r in calls if r["sent_size"][0] < 400),
            "images_missing": [f"{r['source']}/{r['section']}" for r in rows if not r["image_exists"]],
        })
    else:
        st = Counter(r["status"] for r in rows)
        call_d = [r["call_duration_s"] for r in rows if r.get("call_duration_s") is not None]
        summary.update({
            "status": dict(st), "run_status": "ok" if st.get("ok", 0) == len(rows) else ("partial" if st.get("ok") else "failed"),
            "record_status": dict(Counter(r.get("record_status") for r in rows)),
            "failed_sections": [f"{r['source']}/{r['section']}" for r in rows if r["status"] == "failed"],
            "driver_error_sections": [f"{r['source']}/{r['section']}" for r in rows if r["status"] == "driver_error"],
            "llm_calls": sum(1 for r in rows if r.get("llm_called")),
            "blocks": sum(r["blocks"] for r in rows), "true": sum(r["true"] or 0 for r in rows),
            "false_vlm": sum(r["false_vlm"] or 0 for r in rows), "blank": sum(r["blank"] for r in rows if r["status"] == "ok"),
            "usage_total": dict(usage_total),
            "call_duration_s": {"median": round(statistics.median(call_d), 2), "max": max(call_d)} if call_d else None,
            "duration_s_total": round(sum(r["duration_s"] or 0 for r in rows), 1),
        })
    (out / "sections.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
