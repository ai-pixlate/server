"""③-1 채널 정책 맥락 판정 실측 드라이버 — `blocks-input-v1` 섹션마다 운영 인계 함수(`judge_section`)로 채널 행만 판정한다(2026-11-27).

    python docs/ai-experiments/tools/judge_channel_run_driver.py --input pipeline/samples/local/blocks-input-v1 \
        --v9 <현지부적합_v9.xlsx> --out pipeline/out/judge-channel-run-v1_<date> [--sources A,B] [--limit N] [--dry-run] \
        [--price-suffix CH-02]

범위: 섹션마다 채널 호출 **1회 시도, 자동 재시도 없음**. 실패 섹션은 `channel_status=failed`로 남기고 다음 섹션을 계속 처리한다.
- 채널 행은 전달 파일 `현지부적합_채널` 탭의 「적재 · 3차」 행을 그대로 읽는다(값을 저장소에 옮기지 않는다).
- 운영 대응표(`pipeline/data/content_types.json`) · 정책 규칙(`pipeline/data/policy_rules.json`)은 건드리지 않는다. 실험용 유형 코드와
  채널 규칙 행을 메모리에서만 끼운다 — `EXPERIMENT_CODES`의 이름은 **제안이며 운영 값이 아니다**.
- 규제 행은 합성(실제 문구에 걸리지 않는 `examples.bundle`), 현지 사전은 「조회 실패」로 둬 현지 호출을 하지 않는다 — 채널 호출만 본다.
- 기존 출력 폴더는 덮어쓰지 않는다. `--dry-run`은 모델을 부르지 않고 입력 · 이미지 · 묶음 검증만 한다.
산출물: `manifest.json`(입력 · 사전 해시 · 프롬프트 해시 · 설정 · 코드) · `<원본>/<섹션>.json`(섹션 결과 · 호출 기록 · 블록 원문) ·
`sections.json` · `summary.json`.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.handoff import bundle as bundle_mod  # noqa: E402
from pipeline.handoff import examples, judgment  # noqa: E402
from pipeline.handoff.bundle import LocalType, bundle_content_sha256, check_bundle  # noqa: E402
from pipeline.run import _neighbor_texts  # noqa: E402
from pipeline.stages import judge as judge_stage  # noqa: E402
from pipeline.vlm import sha256_text  # noqa: E402

CHANNEL_SHEET, LOAD_STATUS = "현지부적합_채널", "적재 · 3차"
EXPERIMENT_CODES = {"CH-01": "channel_contact_url", "CH-02": "channel_price_stock", "CH-04": "channel_review_testimonial",
                    "CH-06": "channel_time_sensitive"}  # 실험용 제안 이름 — 운영 대응표 값이 아니다
CHANNEL_RULE = {"dict_type": "channel", "verdict_status": "policy", "alternative": "any", "emit_verdict_status": "policy", "bucket": "exclude"}
SCOPE_NOTE = "섹션마다 채널 호출 1회 시도 · 자동 재시도 없음 · 채널 행은 전달 파일의 적재 대상 · 현지 미공급 · 규제 합성 · 정답 라벨 없음"


def channel_entries(v9: Path) -> list[dict]:
    """전달 파일의 적재 대상 채널 행 → AI 묶음의 채널 행 모양(패턴은 적재 규칙과 같이 ';'로 나눈다)."""
    import openpyxl

    rows = list(openpyxl.load_workbook(v9, read_only=True, data_only=True)[CHANNEL_SHEET].iter_rows(values_only=True))
    header = [str(h) if h is not None else "" for h in rows[1]]  # 1행 안내문 · 2행 헤더
    out = []
    for r in rows[2:]:
        d = dict(zip(header, r))
        if d.get("상태") != LOAD_STATUS:
            continue
        out.append({"external_id": d["id"], "item": d["항목"], "patterns": [p.strip() for p in str(d["패턴"]).split(";") if p.strip()],
                    "verdict_status": d["판정"], "exclusion_context": d["제외하는 맥락"], "keep_context": d["제외하지 않는 맥락"],
                    "seller_message": d["셀러 문장"], "confirmed_date": str(d["verified_at"])[:10]})
    return out


class Recording:
    """실제 호출자를 감싸 호출마다 페이로드 · 응답 원문 · 토큰 · 지연 · 오류를 남긴다(결과는 바꾸지 않는다)."""

    def __init__(self, inner) -> None:
        self.inner, self.config, self.last = inner, inner.config, None

    def __call__(self, prompt, payload, image):
        t0 = time.perf_counter()
        self.last = {"payload": json.loads(payload)}
        try:
            reply = self.inner(prompt, payload, image)
        except Exception as e:  # noqa: BLE001 — 기록만 하고 그대로 다시 올린다
            self.last.update({"error": f"{e.__class__.__name__}: {e}", "duration_s": round(time.perf_counter() - t0, 3)})
            raise
        self.last.update({"response_text": reply.text, "usage": reply.usage, "finish_reason": reply.finish_reason,
                          "duration_s": round(time.perf_counter() - t0, 3)})
        return reply


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--v9", required=True, help="현지부적합 v9 전달 파일(xlsx)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sources", default=None, help="쉼표로 구분한 원본 ID(생략 시 전체)")
    ap.add_argument("--limit", type=int, default=None, help="처리할 섹션 수 상한(시험 호출용)")
    ap.add_argument("--price-suffix", default="", help="가격 접미 예외를 켤 채널 항목 ID(쉼표). 기본은 켜지 않는다")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    inp, out, v9 = Path(a.input), Path(a.out), Path(a.v9)
    if out.exists():
        print(f"오류: 출력 폴더가 이미 있다 — 덮어쓰지 않는다: {out}", file=sys.stderr)
        return 1

    cfg = cfgmod.load_config(overrides=[])
    prompt = judge_stage.validate_llm_config(cfg, need_api_key=not a.dry_run)
    entries = channel_entries(v9)
    unknown = [e["external_id"] for e in entries if e["external_id"] not in EXPERIMENT_CODES]
    if unknown:
        print(f"오류: 실험용 유형 코드가 없는 채널 행 {unknown}", file=sys.stderr)
        return 1
    price = {x for x in a.price_suffix.split(",") if x}
    b = examples.bundle(local_ok=False)
    b["channel"] = {"site_key": "amazon", "version": "channel@v9-experiment", "entries": entries}
    b["rules"]["verdict_map"].append(dict(CHANNEL_RULE))
    b["bundle_sha256"] = bundle_content_sha256(b)
    types = dataclasses.replace(bundle_mod.load_type_map(), channel=tuple(
        LocalType(EXPERIMENT_CODES[e["external_id"]], e["external_id"], e["external_id"] in price) for e in entries))
    cb = check_bundle(b, target_country="US", types=types)
    llm = None if a.dry_run else Recording(judge_stage.default_assistant(cfg))

    sources = sorted(p for p in inp.iterdir() if p.is_dir() and (p / "merge").is_dir())
    if a.sources:
        wanted = set(a.sources.split(","))
        sources = [p for p in sources if p.name in wanted]
    manifest = {
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "scope": SCOPE_NOTE,
        "input": str(inp), "v9_file": v9.name, "v9_sha256": hashlib.sha256(v9.read_bytes()).hexdigest(),
        "channel_items": [[e["external_id"], e["item"], len(e["patterns"])] for e in entries],
        "experimental_codes": EXPERIMENT_CODES, "price_suffix_exception": sorted(price),
        "prompt_path": cfg["judge"]["prompt_path"], "prompt_sha256": sha256_text(prompt),
        "judge_config": {k: cfg["judge"][k] for k in ("llm_model", "llm_temperature", "llm_timeout_s", "image_width_px", "context_sections")},
        "git": jsonio.git_state(), "adapter": judgment.JUDGE_ADAPTER_VERSION, "dry_run": a.dry_run,
    }
    out.mkdir(parents=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    by_type = {v: k for k, v in EXPERIMENT_CODES.items()}
    rows, usage_total, n = [], Counter(), 0
    for src in sources:
        split = jsonio.load_split(src / "split.json")
        order = {s.section_key: s.section_order for s in split.sections}
        for mp in sorted((src / "merge").glob("*.json"), key=lambda p: order.get(p.stem, 0)):
            if a.limit is not None and n >= a.limit:
                break
            n += 1
            merged = jsonio.load_merge(mp)
            key = merged.section_key
            sec = next(s for s in split.sections if s.section_key == key)
            row = {"source": src.name, "section": key, "blocks": len(merged.blocks), "image_exists": Path(sec.image_path).exists()}
            if a.dry_run:
                rows.append(row)
                continue
            prev_t, next_t = _neighbor_texts(split, sec, mp, int(cfg["judge"]["context_sections"]))
            t0 = time.perf_counter()
            try:
                r = judgment.judge_section(sec, merged.blocks, cb, "cosmetic", cfg, prev=prev_t, nxt=next_t, llm=llm, prompt=prompt)
            except Exception as e:  # noqa: BLE001 — 드라이버 오류(입력 · 설정)는 기록하고 계속
                row.update({"channel_status": "driver_error", "error": f"{e.__class__.__name__}: {e}"})
                rows.append(row)
                print(f"{src.name}/{key}: 드라이버 오류 {e}", file=sys.stderr)
                continue
            r.pop("_diagnostics", None)
            call = llm.last or {}
            usage = call.get("usage") or {}
            for k, v in usage.items():
                if isinstance(v, (int, float)):
                    usage_total[k] += v
            (out / src.name).mkdir(parents=True, exist_ok=True)
            (out / src.name / f"{key}.json").write_text(
                json.dumps({"result": r, "call": call, "block_texts": {x.block_key: x.source_ko for x in merged.blocks}},
                           ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
            st = {by_type[f["content_type"]]: f["status"] for f in r["content_findings"]["findings"] if f["content_type"] in by_type}
            row.update({"channel_status": r["channel_status"], "failure": r["channel_failure"], "statuses": st,
                        "bucket": r["bucket_recommendation"], "duration_s": round(time.perf_counter() - t0, 3), "usage": usage})
            rows.append(row)
            print(f"[{n}] {src.name}/{key} {r['channel_status']} {st} {row['duration_s']}s", flush=True)
    summary = {"sections": len(rows), "status": dict(Counter(r.get("channel_status", "dry_run") for r in rows)),
               "bucket": dict(Counter(str(r.get("bucket")) for r in rows)), "usage_total": dict(usage_total),
               "images_missing": [f"{r['source']}/{r['section']}" for r in rows if not r["image_exists"]]}
    (out / "sections.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
