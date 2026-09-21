"""단계별 실행 CLI — 전체 서버(FastAPI·Celery·DB) 없이 특정 단계만 돌린다.

    python -m pipeline.run config  [--set 표.키=값 ...]                      # 유효 config 출력
    python -m pipeline.run split   --source IMG [--source-id N] --out DIR   # ① → DIR/split.json, DIR/sections/*.png
    python -m pipeline.run ocr     --split DIR/split.json [--section KEY] --out DIR   # ② → DIR/ocr/<KEY>.json
    python -m pipeline.run merge   --split DIR/split.json --ocr DIR/ocr/<KEY>.json --out DIR  # ③ → DIR/merge/<KEY>.json
    python -m pipeline.run analyze --source IMG [--source IMG ...] --out DIR # ①→②→③ → DIR/analyze.json
    python -m pipeline.run inspect --split|--ocr|--merge JSON --image IMG --out PNG  # 결과를 이미지에 그림

모든 하위 명령은 --config PATH(정본 대신 다른 파일)와 --set 표.키=값(값만 덮어씀)을 받는다.
실행이 끝나면 DIR/run.json에 사용한 config·입력·프롬프트 해시를 남긴다(계약 9장의 event_log.payload에 해당).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline import config as cfgmod
from pipeline import inspect as insp
from pipeline.errors import AnalyzeError
from pipeline.types import MergeResult, OcrResult, Section, SourceImage, SplitResult


def _write_json(path: Path, model) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _write_run_record(out_dir: Path, stage: str, cfg: dict[str, Any], inputs: dict[str, Any]) -> None:
    prompts_dir = Path(__file__).parent / "prompts"
    prompt_hashes = {p.name: _sha256(p) for p in sorted(prompts_dir.glob("*.md")) if p.name != "README.md"}
    record = {
        "stage": stage,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": cfgmod.snapshot(cfg),
        "inputs": inputs,
        "prompt_hashes": prompt_hashes,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cfg(args) -> dict[str, Any]:
    return cfgmod.load_config(args.config, args.set or [])


def _find_section(split: SplitResult, key: str | None) -> Section:
    if key is None:
        if len(split.sections) != 1:
            raise SystemExit("--section KEY 필요: split.json에 섹션이 여러 개")
        return split.sections[0]
    for s in split.sections:
        if s.section_key == key:
            return s
    raise SystemExit(f"split.json에 없는 section_key: {key}")


# ---- 하위 명령 ---------------------------------------------------------------
def cmd_config(args) -> int:
    cfg = _load_cfg(args)
    for k, v in cfgmod.flatten(cfg).items():
        print(f"{k} = {v!r}")
    return 0


def cmd_split(args) -> int:
    from pipeline.stages import section_split

    cfg = _load_cfg(args)
    out = Path(args.out)
    src = SourceImage(source_image_id=args.source_id, upload_order=1, path=args.source)
    res = section_split.run(src, cfg, out / "sections")
    p = _write_json(out / "split.json", res)
    _write_run_record(out, "split", cfg, {"source": args.source})
    print(f"섹션 {len(res.sections)}개 → {p}")
    return 0


def cmd_ocr(args) -> int:
    from pipeline.stages import ocr

    cfg = _load_cfg(args)
    out = Path(args.out)
    split = SplitResult.model_validate_json(Path(args.split).read_text(encoding="utf-8"))
    targets = [_find_section(split, args.section)] if args.section else split.sections
    for sec in targets:
        res = ocr.run(sec, cfg)
        p = _write_json(out / "ocr" / f"{sec.section_key}.json", res)
        print(f"{sec.section_key}: 영역 {len(res.regions)}개 → {p}")
    _write_run_record(out, "ocr", cfg, {"split": args.split, "section": args.section})
    return 0


def cmd_merge(args) -> int:
    from pipeline.stages import merge

    cfg = _load_cfg(args)
    out = Path(args.out)
    split = SplitResult.model_validate_json(Path(args.split).read_text(encoding="utf-8"))
    ocr_res = OcrResult.model_validate_json(Path(args.ocr).read_text(encoding="utf-8"))
    sec = _find_section(split, ocr_res.section_key)
    res = merge.run(sec, ocr_res, cfg)
    p = _write_json(out / "merge" / f"{sec.section_key}.json", res)
    _write_run_record(out, "merge", cfg, {"split": args.split, "ocr": args.ocr})
    print(f"{sec.section_key}: 블록 {len(res.blocks)}개 → {p}")
    return 0


def cmd_analyze(args) -> int:
    from pipeline.analyze import analyze

    cfg = _load_cfg(args)
    out = Path(args.out)
    sources = [
        SourceImage(source_image_id=i + 1, upload_order=i + 1, path=p) for i, p in enumerate(args.source)
    ]
    try:
        res = analyze(sources, cfg, out)
    except AnalyzeError as e:
        print(json.dumps(e.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    p = _write_json(out / "analyze.json", res)
    _write_run_record(out, "analyze", cfg, {"sources": args.source})
    print(f"섹션 {len(res.sections)}개 · 블록 {len(res.blocks)}개 · 경고 {len(res.warnings)}개 → {p}")
    return 0


def cmd_inspect(args) -> int:
    given = [(k, v) for k, v in (("split", args.split), ("ocr", args.ocr), ("merge", args.merge)) if v]
    if len(given) != 1:
        raise SystemExit("--split / --ocr / --merge 중 하나만 지정")
    kind, path = given[0]
    text = Path(path).read_text(encoding="utf-8")
    if kind == "split":
        out = insp.overlay_split(args.image, SplitResult.model_validate_json(text), args.out)
    elif kind == "ocr":
        out = insp.overlay_ocr(args.image, OcrResult.model_validate_json(text), args.out)
    else:
        out = insp.overlay_merge(args.image, MergeResult.model_validate_json(text), args.out)
    print(f"→ {out}")
    return 0


# ---- 인자 --------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m pipeline.run", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", help="config 파일 경로 (기본: pipeline/config/default.toml)")
        sp.add_argument("--set", action="append", metavar="표.키=값", help="config 값 덮어쓰기 (실험용, 반복 가능)")

    sp = sub.add_parser("config", help="유효 config 출력")
    common(sp)
    sp.set_defaults(fn=cmd_config)

    sp = sub.add_parser("split", help="① 섹션 분해")
    common(sp)
    sp.add_argument("--source", required=True)
    sp.add_argument("--source-id", type=int, default=1)
    sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_split)

    sp = sub.add_parser("ocr", help="② 텍스트 추출")
    common(sp)
    sp.add_argument("--split", required=True, help="split.json")
    sp.add_argument("--section", help="section_key (생략 시 전체)")
    sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_ocr)

    sp = sub.add_parser("merge", help="③ 병합·역할 분류")
    common(sp)
    sp.add_argument("--split", required=True, help="split.json")
    sp.add_argument("--ocr", required=True, help="ocr/<section_key>.json")
    sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_merge)

    sp = sub.add_parser("analyze", help="①→②→③ 초기 분석")
    common(sp)
    sp.add_argument("--source", action="append", required=True, help="원본 이미지 (업로드 순서대로 반복)")
    sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_analyze)

    sp = sub.add_parser("inspect", help="결과 JSON을 이미지 위에 그림")
    sp.add_argument("--split")
    sp.add_argument("--ocr")
    sp.add_argument("--merge")
    sp.add_argument("--image", required=True, help="split은 원본, ocr·merge는 섹션 이미지")
    sp.add_argument("--out", required=True, help="출력 PNG")
    sp.set_defaults(fn=cmd_inspect)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except NotImplementedError as e:
        print(f"미구현: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
