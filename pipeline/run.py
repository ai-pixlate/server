"""단계별 실행 CLI — 전체 서버(FastAPI·Celery·DB) 없이 특정 단계만 돌린다.

    python -m pipeline.run config  [--set 표.키=값 ...]                      # 유효 config 출력
    python -m pipeline.run split   --source IMG [--source-id N] --out DIR [--vlm-replay DIR0/split_debug.json]
                                   # ① → DIR/split.json, DIR/sections/*.png, DIR/split_debug.json. --vlm-replay는 이전 VLM 응답 재생(실험용)
    python -m pipeline.run ocr     --split DIR/split.json [--section KEY] --out DIR   # ② → DIR/ocr/<KEY>.json
    python -m pipeline.run merge   --split DIR/split.json --ocr DIR/ocr/<KEY>.json --out DIR [--no-llm | --llm-replay DIR0/merge_debug/<KEY>.json]
                                   # ③ → DIR/merge/<KEY>.json, DIR/merge_debug/<KEY>.json. --no-llm은 휴리스틱만, --llm-replay는 기록 재생
    python -m pipeline.run analyze --source IMG [--source IMG ...] --out DIR [--no-llm]  # ①→②→③ → DIR/analyze.json
    python -m pipeline.run judge   --merge DIR/merge/<KEY>.json --split DIR/split.json --out DIR [--llm-replay DIR0/judge_debug/<KEY>.json]
                                   # ③-1 검출 → 맥락 판정(LLM) → 조립 → DIR/judge/<KEY>.json, DIR/judge_debug/<KEY>.json. 판정 실패는 status=failed로 저장하고 종료 코드 4
    python -m pipeline.run judge   --merge DIR/merge/<KEY>.json --out DIR --no-llm      # ③-1a 검출만 → DIR/judge_detect/<KEY>.json
                                   # --no-llm은 "규칙만으로 최종 판정"이 아니라 "후보 검출만"이다(status detect_only, 판정 결과 아님)
    python -m pipeline.run policy  --judge DIR/judge/<KEY>.json --merge DIR/merge/<KEY>.json --regulatory-class cosmetic|otc|combination|unknown --out DIR
                                   # ③-1' → DIR/policy/<KEY>.json (section_verdict 후보 · 버킷 권고). 미완료 · 분류 누락은 권고 없이 저장하고 종료 코드 2
    python -m pipeline.run inspect --split|--ocr|--merge JSON --image IMG --out PNG  # 결과를 이미지에 그림
    python -m pipeline.run convert-split --in OLD/split.json --base DIR --out NEW/split.json  # 버전 1 → 2 (이미지 대상 유지)
    python -m pipeline.run freeze-input  --split SRC/split.json [--base DIR] --out INPUT_DIR [--meta 키=값 ...]  # 고정 입력본 생성·검증
    python -m pipeline.run verify-input  --dir INPUT_DIR [--relocated]                    # 고정 입력본 재검증

모든 하위 명령은 --config PATH(정본 대신 다른 파일)와 --set 표.키=값(값만 덮어씀)을 받는다.
실행이 끝나면 DIR/run.json에 사용한 config·입력·프롬프트 해시·시작/종료 시각·소요 시간·커밋 해시를 남긴다(계약 9장의 event_log.payload에 해당).
split·ocr·merge 파일 읽기는 pipeline.jsonio를 거친다(버전 확인). split.json만 상대 image_path를 JSON 파일 폴더 기준으로 해석·기록하고,
analyze.json(워커 인계 형식)은 버전 1 · 경로 변환 없이 그대로 쓴다(dev.md 3절).
종료 코드: 0 성공 · 2 AnalyzeError(모든 하위 명령, stderr에 JSON) · 3 미구현(단계 또는 ② 4,000px 초과 섹션) · 4 VLM 호출 실패(open-questions #25 미정, AnalyzeError 미변환).
ocr은 섹션 오류를 기록하고 계속하며 run.json에 status(ok · partial · failed)와 섹션별 결과를 남긴다(dev.md 4절).
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
from pipeline import jsonio
from pipeline.errors import ERROR_POLICY, AnalyzeError
from pipeline.vlm import VlmError
from pipeline.types import Section, SourceImage, SplitResult


def _write_json(path: Path, model) -> Path:
    return jsonio.write_model(path, model)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _write_run_record(
    out_dir: Path, stage: str, cfg: dict[str, Any], inputs: dict[str, Any], started: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    prompts_dir = Path(__file__).parent / "prompts"
    prompt_hashes = {p.name: _sha256(p) for p in sorted(prompts_dir.glob("*.md")) if p.name != "README.md"}
    ended = datetime.now(timezone.utc)
    record = {
        "stage": stage,
        "started_at": started.isoformat(timespec="seconds") if started else None,
        "ran_at": ended.isoformat(timespec="seconds"),  # 종료 시각
        "duration_s": round((ended - started).total_seconds(), 3) if started else None,
        "config": cfgmod.snapshot(cfg),
        "inputs": inputs,
        "prompt_hashes": prompt_hashes,
        **jsonio.git_state(),  # git_commit · git_dirty(추적 파일 변경 여부)
        **(extra or {}),
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
    started = datetime.now(timezone.utc)
    diag: dict[str, Any] = {}
    vlm = None
    if args.vlm_replay:
        from pipeline.vlm import ReplayBoundaryPicker

        vlm = ReplayBoundaryPicker.from_file(
            args.vlm_replay, section_split.source_fingerprint(args.source), section_split.vlm_context(cfg["section"])
        )
        diag["vlm_replay"] = str(args.vlm_replay)
    res = section_split.run(src, cfg, out / "sections", vlm=vlm, diag=diag)  # 재생 기록이 남으면 여기서 VlmReplayMismatch
    p = _write_json(out / "split.json", res)
    out.mkdir(parents=True, exist_ok=True)
    (out / "split_debug.json").write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_run_record(out, "split", cfg, {"source": args.source}, started)
    print(f"섹션 {len(res.sections)}개 → {p}")
    return 0


def _run_status(sections: dict[str, dict[str, Any]]) -> str:
    """ok: 대상 모두 성공 · partial: 일부 성공 + 실패·미실행 · failed: 성공 없음 (dev.md 4절)."""
    n_ok = sum(1 for s in sections.values() if s["status"] == "ok")
    if n_ok == len(sections):
        return "ok"
    return "partial" if n_ok else "failed"


def _error_entry(e: BaseException) -> dict[str, Any]:
    if isinstance(e, AnalyzeError):
        return {**e.to_dict(), "exception": type(e.__cause__ or e).__name__}
    return {"code": None, "retryable": None, "message": str(e), "exception": type(e).__name__}


def cmd_ocr(args) -> int:
    """섹션별로 OCR한다. 섹션 오류는 기록하고 계속, 엔진 초기화 실패는 즉시 중단(dev.md 4절)."""
    from pipeline.stages import ocr

    cfg = _load_cfg(args)
    started = datetime.now(timezone.utc)
    out = Path(args.out)
    if (out / "run.json").exists() or (out / "ocr").exists():
        raise FileExistsError(f"{out}에 이전 실행 결과(run.json 또는 ocr/)가 있음 — 실행마다 새 --out을 쓴다")
    split = jsonio.load_split(args.split)
    targets = [_find_section(split, args.section)] if args.section else split.sections
    sections: dict[str, dict[str, Any]] = {s.section_key: {"status": "not_run"} for s in targets}
    inputs = {"split": args.split, "section": args.section}
    first_error: dict[str, Any] | None = None
    exit_code = 0

    try:
        engine = ocr.build_engine(cfg)
    except Exception as e:  # noqa: BLE001 — 공통 초기화 실패: 남은 섹션은 not_run
        err = {"code": "OCR_FAILED", "retryable": ERROR_POLICY["OCR_FAILED"],
               "message": f"엔진 초기화 실패: {e}", "source_image_id": split.source_image_id,
               "exception": type(e.__cause__ or e).__name__}
        _write_run_record(out, "ocr", cfg, inputs, started,
                          {"status": "failed", "run_error": err, "sections": sections, "engine": None})
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        return 2

    for sec in targets:
        try:
            res = ocr.run(sec, cfg, engine=engine)
        except (AnalyzeError, NotImplementedError) as e:
            entry = _error_entry(e)
            if isinstance(e, NotImplementedError):
                entry["reason"] = "임시 분할 미구현"
                exit_code = exit_code or 3
            else:
                exit_code = 2
            sections[sec.section_key] = {"status": "failed", **entry}
            first_error = first_error or entry
            print(f"{sec.section_key}: 실패 {entry['code'] or entry['exception']} — {entry['message']}")
            continue
        p = _write_json(out / "ocr" / f"{sec.section_key}.json", res)
        sections[sec.section_key] = {"status": "ok", "regions": len(res.regions)}
        print(f"{sec.section_key}: 영역 {len(res.regions)}개 → {p}")

    status = _run_status(sections)
    _write_run_record(out, "ocr", cfg, inputs, started,
                      {"status": status, "run_error": None, "sections": sections, "engine": engine.info})
    if first_error:
        print(json.dumps(first_error, ensure_ascii=False), file=sys.stderr)
    print(f"상태 {status}: " + " · ".join(f"{k} {sum(1 for s in sections.values() if s['status'] == k)}" for k in ("ok", "failed", "not_run")))
    return exit_code


def cmd_merge(args) -> int:
    from pipeline.stages import merge

    cfg = _load_cfg(args)
    started = datetime.now(timezone.utc)
    out = Path(args.out)
    split = jsonio.load_split(args.split)
    ocr_res = jsonio.load_ocr(args.ocr)
    sec = _find_section(split, ocr_res.section_key)
    llm = None
    if args.llm_replay:  # --no-llm과 동시 지정은 argparse가 거부한다
        from pipeline.vlm import ReplayMergeAssistant

        llm = ReplayMergeAssistant.from_file(args.llm_replay, merge.llm_config(cfg))
    recorder = None if args.no_llm else merge.json_recorder(out / "merge_debug")
    res = merge.run(sec, ocr_res, cfg, use_llm=not args.no_llm, llm=llm, recorder=recorder)
    if llm is not None:
        llm.finish()  # 기록이 호출을 기대했는데 쓰이지 않았으면 VlmReplayMismatch
    p = _write_json(out / "merge" / f"{sec.section_key}.json", res)
    _write_run_record(
        out, "merge", cfg, {"split": args.split, "ocr": args.ocr}, started,
        extra={"use_llm": not args.no_llm, "llm_replay": args.llm_replay},
    )
    print(f"{sec.section_key}: 블록 {len(res.blocks)}개 → {p}")
    return 0


def _neighbor_texts(split: SplitResult, sec: Section, merge_path: Path, n: int) -> tuple[str | None, str | None]:
    """같은 원본 안 앞뒤 n개 섹션의 블록 텍스트(참고 문맥, D5). merge/<key>.json이 옆에 없으면 None."""
    if n <= 0:
        return None, None
    keys = [s.section_key for s in sorted(split.sections, key=lambda s: s.section_order)]
    i = keys.index(sec.section_key)

    def text_of(ks: list[str]) -> str | None:
        parts = []
        for k in ks:
            p = merge_path.parent / f"{k}.json"
            if p.exists():
                parts.append("\n".join(b.source_ko for b in jsonio.load_merge(p).blocks))
        return "\n\n".join(parts) if parts else None

    return text_of(keys[max(0, i - n):i]), text_of(keys[i + 1:i + 1 + n])


def cmd_judge(args) -> int:
    """③-1. --no-llm은 ③-1a 검출만(DetectionResult, 판정 아님). 없으면 검출 → 맥락 판정 → 조립(JudgeResult, --split 필요)."""
    from pipeline.stages import judge
    from pipeline.types import JudgeContext

    cfg = _load_cfg(args)
    started = datetime.now(timezone.utc)
    out = Path(args.out)
    merged = jsonio.load_merge(args.merge)
    dicts = judge.load_dicts(cfg)
    key = merged.section_key
    if args.no_llm:
        res = judge.detect_only(key, merged.blocks, cfg, dicts=dicts, recorder=judge.json_recorder(out / "judge_debug"))
        p = _write_json(out / "judge_detect" / f"{key}.json", res)
        _write_run_record(
            out, "judge", cfg, {"merge": args.merge}, started,
            extra={"mode": "detect_only", "use_llm": False, "dictionary_version": dicts.dictionary_version,
                   "dictionary_fingerprint": dicts.fingerprint, "match_rules_version": judge.MATCH_RULES_VERSION},
        )
        print(f"{key}: 검출 전용, 매칭 {len(res.matches)}개 · 항목 {len(res.candidates)}개 → {p} (판정 결과 아님)")
        return 0
    if not args.split:
        raise SystemExit("--split DIR/split.json 필요: 맥락 판정은 섹션 이미지를 쓴다(--no-llm이면 불필요)")
    split = jsonio.load_split(args.split)
    sec = _find_section(split, key)
    prev_t, next_t = _neighbor_texts(split, sec, Path(args.merge), int(cfg["judge"]["context_sections"]))
    ctx = JudgeContext(prev_section_text=prev_t, next_section_text=next_t)
    llm = None
    if args.llm_replay:
        from pipeline.vlm import ReplayJudgeAssistant

        llm = ReplayJudgeAssistant.from_file(args.llm_replay, judge.llm_config(cfg))
    res = judge.run(sec, merged.blocks, ctx, cfg, dicts=dicts, llm=llm, recorder=judge.json_recorder(out / "judge_debug"))
    if llm is not None:
        llm.finish()
    p = _write_json(out / "judge" / f"{key}.json", res)
    _write_run_record(
        out, "judge", cfg, {"merge": args.merge, "split": args.split}, started,
        extra={"mode": "full", "use_llm": True, "llm_replay": args.llm_replay, "status": res.status,
               "dictionary_version": dicts.dictionary_version, "dictionary_fingerprint": dicts.fingerprint,
               "match_rules_version": judge.MATCH_RULES_VERSION},
    )
    if res.status == "failed":
        print(f"{key}: 판정 실패({res.error}) → {p} (content_findings=null)", file=sys.stderr)
        return 4
    n = len(res.content_findings.findings) if res.content_findings else 0
    print(f"{key}: 판정 {res.status}, finding {n}개 · 매칭 {len(res.matches)}개 → {p}")
    return 0


def cmd_policy(args) -> int:
    """③-1'. judge/<key>.json(JudgeResult) + merge/<key>.json(블록) + 상품 규제 분류 → policy/<key>.json. 분류 누락은 input_error(종료 코드 2)."""
    from pipeline.stages import judge, policy
    from pipeline.types import JudgeContext, JudgeResult

    cfg = _load_cfg(args)
    started = datetime.now(timezone.utc)
    out = Path(args.out)
    jr = jsonio.load_model(args.judge, JudgeResult)
    merged = jsonio.load_merge(args.merge)
    if merged.section_key != jr.section_key:
        raise SystemExit(f"section_key 불일치: judge {jr.section_key} · merge {merged.section_key}")
    dicts = judge.load_dicts(cfg)
    ctx = JudgeContext(regulatory_class=args.regulatory_class)
    res = policy.run(jr, merged.blocks, ctx, cfg, dicts=dicts)
    p = _write_json(out / "policy" / f"{jr.section_key}.json", res)
    _write_run_record(
        out, "policy", cfg, {"judge": args.judge, "merge": args.merge}, started,
        extra={"regulatory_class": args.regulatory_class, "status": res.status, "dictionary_version": dicts.dictionary_version,
               "dictionary_fingerprint": dicts.fingerprint, "rules_version": dicts.rules.rules_version,
               "policy_impl_version": policy.RULES_IMPL_VERSION},
    )
    if res.status != "ok":
        print(f"{jr.section_key}: 정책 {res.status}({res.error}) → {p} (권고 없음)", file=sys.stderr)
        return 2
    print(f"{jr.section_key}: 권고 {res.bucket_recommendation}, verdict {len(res.verdicts)}개 · 충돌 {len(res.conflicts)}개 · 억제 {len(res.suppressed)}개 → {p}")
    return 0


def cmd_analyze(args) -> int:
    from pipeline.analyze import analyze

    cfg = _load_cfg(args)
    started = datetime.now(timezone.utc)
    out = Path(args.out)
    sources = [
        SourceImage(source_image_id=i + 1, upload_order=i + 1, path=p) for i, p in enumerate(args.source)
    ]
    res = analyze(sources, cfg, out, use_llm=not args.no_llm)  # AnalyzeError는 main()에서 종료 코드 2로
    p = _write_json(out / "analyze.json", res)
    _write_run_record(out, "analyze", cfg, {"sources": args.source}, started, extra={"use_llm": not args.no_llm})
    print(f"섹션 {len(res.sections)}개 · 블록 {len(res.blocks)}개 · 경고 {len(res.warnings)}개 → {p}")
    return 0


def cmd_inspect(args) -> int:
    given = [(k, v) for k, v in (("split", args.split), ("ocr", args.ocr), ("merge", args.merge)) if v]
    if len(given) != 1:
        raise SystemExit("--split / --ocr / --merge 중 하나만 지정")
    kind, path = given[0]
    if kind == "split":
        out = insp.overlay_split(args.image, jsonio.load_split(path), args.out)
    elif kind == "ocr":
        out = insp.overlay_ocr(args.image, jsonio.load_ocr(path), args.out)
    else:
        out = insp.overlay_merge(args.image, jsonio.load_merge(path), args.out)
    print(f"→ {out}")
    return 0


def _parse_meta(items: list[str] | None) -> dict[str, str]:
    meta = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--meta 형식은 키=값 — 받은 값: {item!r}")
        k, v = item.split("=", 1)
        meta[k.strip()] = v.strip()
    return meta


def cmd_convert_split(args) -> int:
    original = jsonio.convert_split(args.inp, args.out, args.base, overwrite=args.overwrite)
    print(f"버전 2로 전환 → {args.out} (이미지 대상은 그대로)")
    for key, old in original.items():
        print(f"  {key}: 원래 image_path {old}")
    return 0


def cmd_freeze_input(args) -> int:
    prov = jsonio.freeze_input(args.split, args.out, base=args.base, meta=_parse_meta(args.meta))
    print(f"고정 입력본 섹션 {len(prov['sections'])}개 → {args.out} (검증 통과, 원본 해시 불변)")
    return 0


def cmd_verify_input(args) -> int:
    res = jsonio.verify_relocated(args.dir) if args.relocated else jsonio.verify_input(args.dir)
    print(f"검증 통과: 섹션 {res['sections']}개{' (레포 밖 복사본 기준)' if args.relocated else ''}")
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
    sp.add_argument("--vlm-replay", metavar="split_debug.json", help="이전 실행의 VLM 응답을 재생(실험용). API 호출 없음")
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
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--no-llm", action="store_true", help="heuristic_v2만 실행(llm_assist 생략, 개발·실측용)")
    mode.add_argument("--llm-replay", metavar="merge_debug/<KEY>.json", help="이전 llm_assist 기록의 응답을 재생(API 호출 없음)")
    sp.set_defaults(fn=cmd_merge)

    sp = sub.add_parser("judge", help="③-1 AI 섹션 판정")
    common(sp)
    sp.add_argument("--merge", required=True, help="merge/<section_key>.json (③ 출력). 앞뒤 문맥은 같은 폴더의 이웃 섹션 파일에서 읽는다")
    sp.add_argument("--split", help="split.json (섹션 이미지 · 이웃 섹션). 맥락 판정에 필요, --no-llm이면 불필요")
    sp.add_argument("--out", required=True)
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--no-llm", action="store_true", help="③-1a 검출만 실행(후보 검출, 판정 아님 → judge_detect/)")
    mode.add_argument("--llm-replay", metavar="judge_debug/<KEY>.json", help="이전 기록의 응답을 재생(API 호출 없음)")
    sp.set_defaults(fn=cmd_judge)

    sp = sub.add_parser("policy", help="③-1' 정책 적용")
    common(sp)
    sp.add_argument("--judge", required=True, help="judge/<section_key>.json (③-1 출력, JudgeResult)")
    sp.add_argument("--merge", required=True, help="merge/<section_key>.json (③ 출력, 블록 텍스트)")
    sp.add_argument("--regulatory-class", choices=["cosmetic", "otc", "combination", "unknown"], help="상품 규제 분류(job.regulatory_class). 없으면 input_error")
    sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_policy)

    sp = sub.add_parser("analyze", help="①→②→③ 초기 분석")
    common(sp)
    sp.add_argument("--source", action="append", required=True, help="원본 이미지 (업로드 순서대로 반복)")
    sp.add_argument("--out", required=True)
    sp.add_argument("--no-llm", action="store_true", help="③을 heuristic_v2만 실행(llm_assist 생략, 개발·실측용)")
    sp.set_defaults(fn=cmd_analyze)

    sp = sub.add_parser("inspect", help="결과 JSON을 이미지 위에 그림")
    sp.add_argument("--split")
    sp.add_argument("--ocr")
    sp.add_argument("--merge")
    sp.add_argument("--image", required=True, help="split은 원본, ocr·merge는 섹션 이미지")
    sp.add_argument("--out", required=True, help="출력 PNG")
    sp.set_defaults(fn=cmd_inspect)

    sp = sub.add_parser("convert-split", help="버전 1 split.json → 버전 2 (경로 표기만, 이미지 대상 유지)")
    sp.add_argument("--in", dest="inp", required=True, help="버전 1 split.json")
    sp.add_argument("--base", required=True, help="옛 상대 경로의 기준 폴더(보통 레포 루트)")
    sp.add_argument("--out", required=True, help="버전 2 split.json")
    sp.add_argument("--overwrite", action="store_true", help="--out이 이미 있으면 덮어쓴다")
    sp.set_defaults(fn=cmd_convert_split)

    sp = sub.add_parser("freeze-input", help="고정 입력본 생성: 섹션 이미지 복사·경로 재지정·검증")
    sp.add_argument("--split", required=True, help="원본 실행의 split.json (버전 1 또는 2, 읽기만 함)")
    sp.add_argument("--base", help="버전 1일 때 옛 상대 경로의 기준 폴더")
    sp.add_argument("--out", required=True, help="입력본 폴더(비어 있어야 함)")
    sp.add_argument("--meta", action="append", metavar="키=값", help="출처 기록(실행 디렉터리·회차·선택 기준·커밋 등, 반복 가능)")
    sp.set_defaults(fn=cmd_freeze_input)

    sp = sub.add_parser("verify-input", help="고정 입력본 재검증")
    sp.add_argument("--dir", required=True, help="입력본 폴더(split.json · provenance.json)")
    sp.add_argument("--relocated", action="store_true", help="레포 밖 임시 위치로 복사해 그 복사본만으로 검증")
    sp.set_defaults(fn=cmd_verify_input)
    return p


def _safe_console() -> None:
    """콘솔 인코딩이 cp949 등이어도 메시지의 특수문자(—, → 등) 때문에 결과 저장 뒤 프로세스가 실패하지 않게 한다.
    인코딩은 그대로 두고 표현할 수 없는 문자만 '?'로 바꾼다(Windows 파이프 출력에서 재현된 문제, 2026-09-29)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # 닫힌 스트림 등
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except AnalyzeError as e:
        print(json.dumps(e.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except NotImplementedError as e:
        print(f"미구현: {e}", file=sys.stderr)
        return 3
    except VlmError as e:
        # open-questions #25 미정 — 오류 코드·재시도 정책 결정 전까지 AnalyzeError로 바꾸지 않는다.
        print(f"VLM 실패(#25 미정): {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
