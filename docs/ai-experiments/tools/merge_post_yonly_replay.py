"""③ 칸 묶음 경계 채택(2026-10-26) 구현 검증 — 기록 재생 동등성(설계 `2026-10-26_03-merge_post-yonly-adoption-design.md` 5절).

    python docs/ai-experiments/tools/merge_post_yonly_replay.py --input pipeline/samples/local/blocks-input-v1 \
        --run pipeline/out/merge-post-step4-round4_2026-10-23/B3_yonly --run pipeline/out/merge-post-step4-round5_2026-10-26/new/B3_yonly ... \
        [--off-run pipeline/samples/local/blocks-input-v1] --out OUT.json

LLM을 호출하지 않는다. 각 실행 폴더의 `merge_debug` 중 `ok` 기록마다:
1. 지금 코드로 휴리스틱 · LLM 입력을 다시 만들고, 프롬프트 지문 · LLM 입력 지문이 기록과 같은지 먼저 본다(다르면 재생하지 않고 차이로 보고).
2. 기록된 응답(`response_text`)을 돌려주는 가짜 호출자로 `merge.run`을 돌린다.
3. 최종 블록의 구성(원시 영역 목록) · 순서 · 텍스트(`source_ko` · 줄 텍스트 · 줄 순서) · 역할을 기록된 `merge/` 결과와 비교한다.
   `region_splits` · `interleaved`는 참고로 대조한다. 새 진단 필드는 비교하지 않는다.
--run은 xycut 켬(기본 config + merge.xycut=true), --off-run은 기본 config(xycut 끔)로 재생한다 — 끈 경로가 그대로인지 본다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

import merge_post_step4 as step4  # noqa: E402
from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.stages import merge  # noqa: E402
from pipeline.vlm import LlmReply, sha256_text  # noqa: E402


class Replay:
    def __init__(self, text: str) -> None:
        self.text = text
        self.config = {"model": "replay", "temperature": 0, "timeout_s": 60}

    def __call__(self, prompt: str, payload: str) -> LlmReply:
        return LlmReply(self.text, {})


def shape(res) -> list[dict]:
    return [{"regions": [r.region_key for ln in b.source_lines for r in ln.regions], "order": b.block_order,
             "source_ko": b.source_ko, "lines": [ln.text for ln in b.source_lines], "role": b.role} for b in res.blocks]


def replay_dir(inp: Path, run: Path, cfg: dict, label: str) -> dict:
    prompt = merge.validate_llm_config(cfg, need_api_key=False)
    out = {"run": str(run), "mode": label, "ok_records": 0, "equal": 0, "fingerprint_diff": [], "result_diff": [],
           "notes_diff": [], "skipped": []}
    for f in sorted(run.glob("*/merge_debug/*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        img = f.parent.parent.name
        name = f"{img}/{rec['section_key']}"
        if rec["status"] != "ok":
            out["skipped"].append({"section": name, "status": rec["status"]})
            continue
        out["ok_records"] += 1
        _, sec, ocr = step4.section_inputs(inp, name)
        heur, layout, _, _ = merge._heuristic(sec, ocr, cfg)
        payload, _ = merge.build_payload(sec, heur, layout)
        fp = {"prompt": sha256_text(prompt) == rec["prompt_sha256"],
              "payload": sha256_text(merge.payload_text(payload)) == rec["payload_sha256"]}
        if not all(fp.values()):
            out["fingerprint_diff"].append({"section": name, **fp})
            continue
        recs: list[dict] = []
        res = merge.run(sec, ocr, cfg, use_llm=True, llm=Replay(rec["response_text"]), recorder=recs.append)
        want = jsonio.load_merge(f.parent.parent / "merge" / f"{rec['section_key']}.json")
        if shape(res) == shape(want):
            out["equal"] += 1
        else:
            out["result_diff"].append(name)
        old, new = rec.get("result") or {}, recs[-1].get("result") or {}
        for k in ("region_splits", "interleaved"):
            if old.get(k) != new.get(k):
                out["notes_diff"].append({"section": name, "key": k, "recorded": old.get(k), "now": new.get(k)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True)
    ap.add_argument("--run", action="append", default=[], help="xycut 켬으로 재생할 llm-run 변형 폴더(B3_yonly)")
    ap.add_argument("--off-run", action="append", default=[], help="기본 config(xycut 끔)로 재생할 폴더")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if not step4.yonly_adopted():
        raise SystemExit("칸 묶음 경계를 채택한 코드에서만 실행한다")
    inp = Path(a.input)
    on, off = step4.base_cfg([]), cfgmod.load_config()
    merge.validate_config(off)
    results = [replay_dir(inp, Path(r), on, "xycut") for r in a.run]
    results += [replay_dir(inp, Path(r), off, "off") for r in a.off_run]
    Path(a.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in results:
        print(f"{r['mode']:5} {r['run']}: ok {r['ok_records']} · 같음 {r['equal']} · 지문 다름 {len(r['fingerprint_diff'])} · "
              f"결과 다름 {len(r['result_diff'])} · 기록 참고 차이 {len(r['notes_diff'])} · 건너뜀 {len(r['skipped'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
