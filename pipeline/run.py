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
    python -m pipeline.run label   --merge DIR/merge/<KEY>.json --split DIR/split.json --out NEW_DIR [--llm-replay DIR0/label_debug/<KEY>.json]
                                   # ④ 제품 라벨 판정 → NEW_DIR/label/<KEY>.json, NEW_DIR/label_debug/<KEY>.json. 실패는 status=failed로 저장하고 종료 코드 4.
                                   # 실행마다 새 --out(이전 run.json · label/이 있으면 거부)
    python -m pipeline.run logo    --merge DIR/merge/<KEY>.json --label DIR/label/<KEY>.json --brand-meta brand_metadata.json --image-id ID --out NEW_DIR
                                   # ⑤ 브랜드 로고 제외(모델 호출 없음) → NEW_DIR/<ID>/logo/<KEY>.json, NEW_DIR/<ID>/logo_debug/<KEY>.json.
                                   # 종료 코드 0 성공 · 2 입력 · 설정 오류 · 4 실행 · 저장 오류(open-questions #68). --out이 이미 있으면 거부
    python -m pipeline.run inpaint --mode mask-only|inpaint --split IMG/split.json --section KEY --image-id ID --merge M --label L
                                   --logo G --logo-debug GD --out NEW_DIR
                                   # ⑥ 인페인팅(개발용 v1, pipeline.md 7.6절) → NEW_DIR/<ID>/inpaint_mask/ · inpaint_bg/ · inpaint_debug/ · inpaint/.
                                   # mask-only는 마스크 · 진단만(인페인팅 아님). inpaint는 LaMa 어댑터(GPU · FP32, 가중치 미설치 · torch 없음이면 종료 코드 3).
                                   # 종료 코드 0 성공 · 2 입력 · 설정 오류 · 3 모델 사용 불가 · 4 모델 초기화 · 추론 · 저장 · 내부 오류. --out이 있거나 고정 입력본 안이면 거부
    python -m pipeline.run inspect --split|--ocr|--merge JSON --image IMG --out PNG  # 결과를 이미지에 그림
    python -m pipeline.run inspect --label DIR/label/<KEY>.json --merge DIR/merge/<KEY>.json --image IMG --out PNG
    python -m pipeline.run inspect --judge DIR/judge/<KEY>.json --merge DIR/merge/<KEY>.json [--policy DIR/policy/<KEY>.json] --image IMG --out PNG
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


def cmd_label(args) -> int:
    """④. merge/<key>.json(블록) + split.json(섹션 이미지) → label/<key>.json. 판정 실패는 status=failed로 저장하고 종료 코드 4."""
    from pipeline.stages import label
    from pipeline.vlm import sdk_info

    cfg = _load_cfg(args)
    started = datetime.now(timezone.utc)
    out = Path(args.out)
    if (out / "run.json").exists() or (out / "label").exists():
        raise FileExistsError(f"{out}에 이전 실행 결과(run.json 또는 label/)가 있음 — 실행마다 새 --out을 쓴다")
    label.validate_config(cfg)
    merged = jsonio.load_merge(args.merge)
    split = jsonio.load_split(args.split)
    sec = _find_section(split, merged.section_key)
    llm = None
    if args.llm_replay:
        from pipeline.vlm import ReplayLabelAssistant

        llm = ReplayLabelAssistant.from_file(args.llm_replay, label.llm_config(cfg))
    res = label.run(sec, merged.blocks, cfg, llm=llm, recorder=label.json_recorder(out / "label_debug"))
    if llm is not None and res.status == "ok":
        llm.finish()  # 기록은 호출을 기대했는데 이번 실행이 부르지 않았으면 VlmReplayMismatch
    p = _write_json(out / "label" / f"{sec.section_key}.json", res)
    _write_run_record(
        out, "label", cfg, {"merge": args.merge, "split": args.split}, started,
        extra={"status": res.status, "llm_called": res.checked.llm_called, "llm_replay": args.llm_replay, "sdk": sdk_info()},
    )
    if res.status == "failed":
        print(f"{sec.section_key}: 라벨 판정 실패({res.error}) → {p} (labels=null, 미판정)", file=sys.stderr)
        return 4
    n_true = sum(1 for d in res.labels if d.is_product_label)
    print(f"{sec.section_key}: 라벨 판정 ok, 블록 {len(res.labels)}개 중 라벨 {n_true}개 (호출 {'함' if res.checked.llm_called else '안 함'}) → {p}")
    return 0


# ---- ⑤ 브랜드 로고 제외 (open-questions #68) ------------------------------------------------
# 실행 계층: 파일 읽기 · 사전 검사 · 판정 호출 · 저장(임시 파일 → 교체) · run.json · 종료 코드를 맡는다.
# 종료 코드는 ⑤ 단독 방침(#68): 0 성공 · 2 입력 · 설정 오류(판정 결과 없음) · 4 실행 · 저장 오류. 자동 재시도 · 이어 실행 없음.
LOGO_SCOPE_NOTE = ("⑤ 단독 개발 v1 — 모델 호출 없음 · 개발용 결과 형식(운영 API/DB 계약 아님, #68) · 사전 검사 실패 시 판정 결과 없음 · "
                   "예기치 않은 오류나 저장 오류면 전체 중단(완료 섹션 결과는 보존, 실행은 실패) · 재실행은 새 출력 폴더")


def logo_run_base(mode: str, cfg: dict[str, Any] | None, started: datetime) -> dict[str, Any]:
    """⑤ run.json 공통 머리 — 설정 · 환경 · 코드 버전."""
    import platform
    import unicodedata

    from pipeline.stages import logo

    return {
        "stage": "logo", "mode": mode, "scope": LOGO_SCOPE_NOTE, "status": None, "error": None,
        "started_at": started.isoformat(timespec="seconds"), "ran_at": None, "duration_s": None,
        "config": cfgmod.snapshot(cfg) if cfg is not None else None,
        "normalize_rule": logo.NORMALIZE_RULE, "python": platform.python_version(), "unicode_version": unicodedata.unidata_version,
        **jsonio.git_state(),
        "inputs": {}, "counts": None, "sections": [],
    }


def _logo_finish(out: Path, record: dict[str, Any], started: datetime, status: str, error: str | None, code: int) -> int:
    """run.json을 임시 파일 → 교체로 쓰고 종료 코드를 돌려준다(#68 확정 2 · 3).
    출력 폴더 생성이나 run.json 저장 자체가 실패하면 원래 상태 · 오류와 함께 표준 오류로 알리고 종료 코드 4 — 기록을 남겼다고 보고하지 않는다."""
    ended = datetime.now(timezone.utc)
    record.update({"status": status, "error": error, "ran_at": ended.isoformat(timespec="seconds"),
                   "duration_s": round((ended - started).total_seconds(), 3)})
    c = record.get("counts")
    if isinstance(c, dict) and "sections_total" in c:
        c["sections_not_run"] = c["sections_total"] - c["sections_ok"] - c["sections_failed"]
    try:
        jsonio.write_text_atomic(out / "run.json", json.dumps(record, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001 — 폴더 생성 · 직렬화 · 저장 실패 모두 기록 없음
        print(f"⑤ run.json 저장 실패 — 실행 기록이 남지 않았다: {e.__class__.__name__}: {e} "
              f"(상태 {status}{f' · 오류 {error}' if error else ''})", file=sys.stderr)
        return 4
    if error:
        print(f"⑤ {status}: {error} (run.json에 기록)", file=sys.stderr)
    return code


# 사전 검사에서 "정상적으로 검출한 입력 · 설정 오류"로 보는 예외(#68 확정 3). LogoInputError · SchemaVersionError · pydantic ValidationError ·
# JSON · 인코딩 오류는 ValueError, 파일 없음 · 읽기 실패는 OSError, 없는 config 키 override는 ConfigKeyError. 그 밖의 예외는 내부 오류다.
LOGO_INPUT_ERRORS: tuple[type[BaseException], ...] = (ValueError, OSError, cfgmod.ConfigKeyError)


def logo_input_error(out: Path, record: dict[str, Any], started: datetime, error: str) -> int:
    """사전 검사 실패 — 판정 결과를 만들지 않고 run.json(status input_error)만 남긴다. 종료 코드 2."""
    return _logo_finish(out, record, started, "input_error", error, 2)


def logo_internal_error(out: Path, record: dict[str, Any], started: datetime, e: BaseException) -> int:
    """사전 검사(입력 로딩 · 해시 검사 · 검증 포함) 중 예상하지 못한 내부 예외 — run.json status failed · 종료 코드 4(#68 확정 3)."""
    return _logo_finish(out, record, started, "failed", f"사전 검사 중 예기치 않은 오류: {e.__class__.__name__}: {e}", 4)


def duplicate_logo_jobs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """같은 (image_id, section_key)가 두 번 이상 나오는 쌍. 같은 출력 경로에 덮어쓰지 않도록 사전 검사에서 거부한다."""
    seen: set[tuple[str, str]] = set()
    dups: set[tuple[str, str]] = set()
    for p in pairs:
        (dups if p in seen else seen).add(p)
    return sorted(dups)


def execute_logo(out: Path, jobs: list[dict[str, Any]], brand_meta: Any, cfg: dict[str, Any], record: dict[str, Any], started: datetime) -> int:
    """⑤ 실행. jobs = [{"image_id", "merged": MergeResult, "label": LabelResult}] (이미 읽은 입력).
    1) 사전 검사: 모든 job을 판정 전에 검증 — 하나라도 실패하면 판정 결과 없이 input_error(2).
    2) 판정 · 저장: <out>/<image_id>/logo_debug/<key>.json → <out>/<image_id>/logo/<key>.json 순으로 임시 파일 → 교체.
       판정 중 예기치 않은 오류는 그 섹션의 failed 결과 · 기록을 남기고 전체 중단(4). 저장 오류도 실행 실패(4). 완료 섹션 결과는 보존한다."""
    from pipeline.stages import logo

    try:
        dups = duplicate_logo_jobs([(job["image_id"], job["merged"].section_key) for job in jobs])
        if dups:
            raise logo.LogoInputError(f"같은 원본 · 섹션 작업이 두 번 이상 있다(같은 출력 경로) {dups}")
        for job in jobs:
            logo.validate_inputs(job["image_id"], job["merged"], job["label"], brand_meta, cfg)
    except logo.LogoInputError as e:
        return logo_input_error(out, record, started, f"사전 검사 실패: {e}")
    except Exception as e:  # noqa: BLE001 — 검증 코드 자체의 예기치 않은 오류
        return logo_internal_error(out, record, started, e)

    totals = {k: 0 for k in ("product_label", "empty_text", "exact_match", "no_match", "compared")}
    counts = {"sections_total": len(jobs), "sections_ok": 0, "sections_failed": 0, "blocks": 0, **totals}
    record["counts"] = counts
    for job in jobs:
        image_id, merged = job["image_id"], job["merged"]
        key = merged.section_key
        rel_result = Path(image_id) / "logo" / f"{key}.json"
        rel_debug = Path(image_id) / "logo_debug" / f"{key}.json"
        failure: str | None = None
        try:
            result, rec = logo.run(image_id, merged, job["label"], brand_meta, cfg)
        except Exception as e:  # noqa: BLE001 — 사전 검사를 통과했으므로 여기서의 오류는 예기치 않은 오류
            failure = f"판정 중 예기치 않은 오류: {e.__class__.__name__}: {e}"
            result = logo.failed_result(image_id, key, failure)
            rec = {"stage": "logo", "image_id": image_id, "section_key": key, "status": "failed", "error": failure,
                   "exception": e.__class__.__name__}
        try:
            jsonio.write_text_atomic(out / rel_debug, json.dumps(rec, ensure_ascii=False, indent=2))
            jsonio.write_text_atomic(out / rel_result, result.model_dump_json(indent=2))
        except Exception as e:  # noqa: BLE001 — 저장 오류: 성공으로 보고하지 않는다
            record["sections"].append({"image_id": image_id, "section_key": key, "status": "save_failed",
                                       "judge_status": result.status, "error": f"{e.__class__.__name__}: {e}"})
            counts["sections_failed"] += 1
            return _logo_finish(out, record, started, "failed", f"{image_id}/{key} 저장 실패: {e.__class__.__name__}: {e}", 4)
        if failure is not None:
            record["sections"].append({"image_id": image_id, "section_key": key, "status": "failed", "error": failure,
                                       "result": rel_result.as_posix(), "debug": rel_debug.as_posix()})
            counts["sections_failed"] += 1
            return _logo_finish(out, record, started, "failed", f"{image_id}/{key} {failure}", 4)
        c = rec["counts"]
        for k in totals:
            counts[k] += c[k]
        counts["blocks"] += rec["blocks"]
        counts["sections_ok"] += 1
        record["sections"].append({"image_id": image_id, "section_key": key, "status": "ok", "blocks": rec["blocks"], "counts": c,
                                   "blocks_fingerprint": rec["blocks_fingerprint"], "label_fingerprint": rec["label_fingerprint"],
                                   "result": rel_result.as_posix(), "debug": rel_debug.as_posix()})
    if counts["sections_ok"] != counts["sections_total"] or counts["sections_failed"]:  # 실패 · 미처리 섹션이 있으면 ok로 기록하지 않는다(#68 확정 1)
        return _logo_finish(out, record, started, "failed", "처리되지 않았거나 실패한 섹션이 있다", 4)
    code = _logo_finish(out, record, started, "ok", None, 0)
    if code == 0:
        print(f"⑤ ok: 섹션 {counts['sections_ok']} · 블록 {counts['blocks']} (라벨 생략 {counts['product_label']} · 비교 {counts['compared']} · "
              f"일치 {counts['exact_match']} · 빈 텍스트 {counts['empty_text']} · 불일치 {counts['no_match']}) → {out}")
    return code


def cmd_logo(args) -> int:
    """⑤ 한 섹션. merge/<key>.json + label/<key>.json + brand_metadata.json + 원본 식별자 → <out>/<image_id>/logo/<key>.json.
    상대 경로는 실행 디렉터리 기준. 이미지 · split.json은 받지 않는다. --out이 이미 있으면 거부(2)."""
    from pipeline.stages import logo
    from pipeline.types import LabelResult

    started = datetime.now(timezone.utc)
    out = Path(args.out)
    if out.exists():
        print(f"출력 폴더가 이미 있다 — 실행마다 새 --out을 쓴다: {out}", file=sys.stderr)
        return 2
    inputs = {"image_id": args.image_id, "merge": args.merge, "label": args.label, "brand_meta": args.brand_meta}
    record: dict[str, Any] = {"stage": "logo", "mode": "single", "inputs": inputs}
    try:
        record = {**logo_run_base("single", None, started), "inputs": inputs}
    except Exception as e:  # noqa: BLE001 — 기록 머리조차 못 만들면 최소 기록으로 내부 오류
        return logo_internal_error(out, record, started, e)
    try:
        cfg = _load_cfg(args)
        record["config"] = cfgmod.snapshot(cfg)
        logo.validate_config(cfg)
        paths = {"merge": Path(args.merge), "label": Path(args.label), "brand_meta": Path(args.brand_meta)}
        inputs["sha256"] = {k: jsonio.sha256_file(p) for k, p in paths.items()}
        merged = jsonio.load_merge(paths["merge"])
        label_res = jsonio.load_model(paths["label"], LabelResult)
        brand_meta = json.loads(paths["brand_meta"].read_text(encoding="utf-8"))
        if isinstance(brand_meta, dict):
            inputs["brand_version"] = brand_meta.get("version")
            inputs["brand_for_input"] = brand_meta.get("for_input")  # 자료에 적힌 연결 입력본(출처 기록)
    except LOGO_INPUT_ERRORS as e:  # 검출한 입력 · 설정 오류
        return logo_input_error(out, record, started, f"{e.__class__.__name__}: {e}")
    except Exception as e:  # noqa: BLE001 — 입력 로딩 중 예상하지 못한 내부 예외
        return logo_internal_error(out, record, started, e)
    return execute_logo(out, [{"image_id": args.image_id, "merged": merged, "label": label_res}], brand_meta, cfg, record, started)


# ---- ⑥ 인페인팅 ---------------------------------------------------------------
# 실행 계층: 파일 읽기 · 사전 검사 · 모델 초기화 · 마스크 · 추론 · 저장(임시 파일 → 교체, PNG는 다시 읽어 대조) · run.json · 종료 코드.
# 개발용 v1 형식(사용자 승인 2026-09-30, pipeline.md 7.6절 — 운영 API/DB 계약 아님): 0 성공 · 2 입력 · 설정 오류(결과 없음) ·
# 3 모델 사용 불가(어댑터 없음 · torch 미설치 · 가중치 없음, 결과 없음) · 4 모델 초기화 · 추론 · 출력 검증 · 저장 · 내부 오류. 자동 재시도 · 축소 · CPU 대체 · 이어 실행 없음.
INPAINT_SCOPE_NOTE = ("⑥ 단독 개발 v1 — 개발용 결과 형식(운영 API/DB 계약 아님, pipeline.md 7.6절) · LaMa 어댑터 = iopaint 1.6.0 재현(#72, GPU · FP32 · 원본 해상도) · "
                      "mask_only는 마스크 · 진단만(인페인팅 아님) · 사전 검사 실패 시 결과 없음 · 모델 · 저장 오류면 전체 중단"
                      "(완료 섹션 결과 보존, 실행은 실패) · 자동 재시도 · 축소 · CPU 대체 없음 · 재실행은 새 출력 폴더")
INPAINT_MODES = {"mask-only": "mask_only", "inpaint": "inpaint"}  # CLI 값 → 기록 값
# 사전 검사에서 "검출한 입력 · 설정 오류"로 보는 예외(⑤와 같은 분류): InpaintInputError · SchemaVersionError · ValidationError · JSON 오류는
# ValueError, 파일 없음 · 이미지 열기 실패는 OSError, 없는 config 키 override는 ConfigKeyError. 그 밖의 예외는 내부 오류다.
INPAINT_INPUT_ERRORS: tuple[type[BaseException], ...] = (ValueError, OSError, cfgmod.ConfigKeyError)


def inpaint_run_base(mode: str, cfg: dict[str, Any] | None, started: datetime) -> dict[str, Any]:
    """⑥ run.json 공통 머리 — 설정 · 환경 · 코드 버전. 시간 제한은 정하지 않았으므로 null로 남긴다(#72)."""
    import platform

    import numpy
    import PIL

    from pipeline.stages import inpaint

    return {
        "stage": "inpaint", "mode": mode, "scope": INPAINT_SCOPE_NOTE, "status": None, "error": None,
        "started_at": started.isoformat(timespec="seconds"), "ran_at": None, "duration_s": None,
        "config": cfgmod.snapshot(cfg) if cfg is not None else None, "raster_rule": inpaint.RASTER_RULE,
        "timeout_s": ({k: cfg["inpaint"].get(k) for k in ("init_timeout_s", "infer_timeout_s", "kill_grace_s")} if cfg else None),
        "timeout_note": "모델 자식 프로세스 시간 제한 — 100섹션 실측용 잠정값(운영값 아님, #71). mask_only는 모델을 만들지 않는다",
        "python": platform.python_version(), "numpy": numpy.__version__, "pillow": PIL.__version__, "platform": platform.platform(),
        **jsonio.git_state(),
        "inputs": {}, "model": None, "counts": None, "sections": [],
    }


def _inpaint_finish(out: Path, record: dict[str, Any], started: datetime, status: str, error: str | None, code: int) -> int:
    """run.json을 임시 파일 → 교체로 쓰고 종료 코드를 돌려준다. run.json 저장 자체가 실패하면 표준 오류로 알리고 4 — 기록을 남겼다고 보고하지 않는다."""
    ended = datetime.now(timezone.utc)
    record.update({"status": status, "error": error, "ran_at": ended.isoformat(timespec="seconds"),
                   "duration_s": round((ended - started).total_seconds(), 3)})
    secs = record.get("sections") or []
    c = record.get("counts")
    if isinstance(c, dict):
        for k in ("ok", "failed", "save_failed", "not_run"):
            c[f"sections_{k}"] = sum(1 for s in secs if s.get("status") == k)
    try:
        jsonio.write_text_atomic(out / "run.json", json.dumps(record, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001 — 폴더 생성 · 직렬화 · 저장 실패 모두 기록 없음
        print(f"⑥ run.json 저장 실패 — 실행 기록이 남지 않았다: {e.__class__.__name__}: {e} "
              f"(상태 {status}{f' · 오류 {error}' if error else ''})", file=sys.stderr)
        return 4
    if error:
        print(f"⑥ {status}: {error} (run.json에 기록)", file=sys.stderr)
    return code


def fixed_bundle_roots(path: Path) -> list[Path]:
    """path가 속한 **모든** 고정 입력본 루트 — SHA256SUMS가 있는 상위 폴더 전부(가까운 것부터). 입력본 안에 입력본이 묶인 경우
    (예 downstream-input-v1 안의 upstream_*) 바깥 입력본도 보호해야 하므로 가장 가까운 하나에서 멈추지 않는다(dev.md 5절)."""
    return [parent for parent in Path(path).resolve().parents if (parent / "SHA256SUMS").is_file()]


def inpaint_out_guard(out: Path, input_paths: list[Path]) -> str | None:
    """출력 폴더 거부 사유. 이미 있는 폴더(덮어쓰기)와 입력 파일이 속한 고정 입력본 안(입력본 변경)을 거부한다. 문제없으면 None."""
    if out.exists():
        return f"출력 폴더가 이미 있다 — 실행마다 새 --out을 쓴다: {out}"
    ro = out.resolve()
    for p in input_paths:
        for root in fixed_bundle_roots(p):
            if ro == root or root in ro.parents:
                return f"출력 폴더 {out}가 고정 입력본 {root} 안에 있다 — 입력본은 읽기 전용이다"
    return None


def safe_path_component(value: Any) -> bool:
    """출력 경로의 한 구성 요소로 쓸 수 있는 식별자인가 — 비어 있지 않은 문자열이고 경로 구분자(/ \\) · 드라이브 구분자(:) ·
    제어 문자(NUL · 줄바꿈 등, str.isprintable) · 앞뒤 공백이 없으며 '.' · '..'이 아니다. image_id · section_key를 파일 경로로 쓰기 전에
    검사한다(출력 폴더 밖 저장 방지). 최종 경로가 출력 폴더 안인지는 execute_inpaint가 따로 확인한다."""
    return (isinstance(value, str) and value != "" and value not in (".", "..") and value == value.strip() and value.isprintable()
            and not any(c in value for c in ("/", "\\", ":")))


def inside(root: Path, path: Path) -> bool:
    """path(해석한 절대 경로)가 root 안인가."""
    r, p = root.resolve(), path.resolve()
    return p == r or r in p.parents


def save_png_verified(path: Path, arr) -> None:
    """무손실 PNG를 임시 파일 → 교체로 쓰고 다시 읽어 픽셀을 대조한다. 다르면 OSError(저장 실패)."""
    import io

    import numpy as np
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    jsonio.write_bytes_atomic(path, buf.getvalue())
    with Image.open(path) as im:
        back = np.array(im)
    if back.shape != arr.shape or back.dtype != arr.dtype or not np.array_equal(back, arr):
        raise OSError(f"저장한 PNG를 다시 읽은 픽셀이 원본과 다르다: {path}")


def inpaint_output_paths(image_id: str, section_key: str) -> dict[str, Path]:
    """섹션 하나의 출력 파일(--out 기준 상대 경로). 식별자는 safe_path_component를 통과한 값이어야 한다."""
    base = Path(image_id)
    return {"final_mask": base / "inpaint_mask" / f"{section_key}.final.png",
            "protect_mask": base / "inpaint_mask" / f"{section_key}.protect.png",
            "background": base / "inpaint_bg" / f"{section_key}.png", "debug": base / "inpaint_debug" / f"{section_key}.json",
            "result": base / "inpaint" / f"{section_key}.json"}


def _last_call(model) -> dict[str, Any] | None:
    """모델이 남긴 마지막 호출 기록(dict일 때만 — JSON으로 쓸 수 있는 측정값). 없으면 None."""
    calls = getattr(model, "calls", None)
    return dict(calls[-1]) if isinstance(calls, list) and calls and isinstance(calls[-1], dict) else None


class _InpaintInitStop(Exception):
    """지연 모델 초기화 실패 — 실행 중단 신호(종료 코드 · 메시지)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def execute_inpaint(out: Path, jobs: list[dict[str, Any]], cfg: dict[str, Any], mode: str, record: dict[str, Any], started: datetime,
                    *, model_factory=None) -> int:
    """⑥ 실행. jobs = [{"image_id", "section": Section, "image_path": Path, "merged", "label", "logo", "logo_record",
    "input_paths": [읽은 입력 파일 경로 …](선택)}] (이미 읽은 입력). CLI(한 섹션)와 후속 실측 드라이버가 같은 함수를 쓴다.
    0) 출력 보호(이 함수가 보장 — 호출자에 맡기지 않는다): `out`이 이미 있거나 입력(image_path · input_paths)이 속한 고정 입력본 안이면
       아무것도 쓰지 않고 2. 통과하면 `out`을 새로 만든다(이미 있으면 2).
    1) 사전 검사: 모든 섹션의 설정 · 경로 식별자(image_id · section_key) · 출력 경로가 out 안인지 · 이미지(RGB · 크기) · 판정 · 지문 · 기하 검증
       → 하나라도 실패하면 결과 없이 input_error(2).
    2) 섹션마다 마스크 → (inpaint) 추론 · 합성 → 저장(마스크 → 배경 → 진단 → 결과). model_factory(cfg) -> InpaintModel은 inpaint 모드에서
       **처음으로 비어 있지 않은 최종 마스크를 만났을 때** 한 번 부른다(빈 마스크 섹션만 있으면 모델 없이 unchanged로 끝난다).
       기본(inpaint.build_model)은 LaMa 어댑터 — 모델 사용 불가(torch 없음 · 가중치 없음)는 ModelNotAvailable → 3, 그 밖의 초기화 실패 → 4 — 그 섹션은 파일 없이 not_run.
       모델 오류는 그 섹션 failed 결과 · 진단을 남기고 전체 중단(4), 저장 오류는 save_failed로 중단(4).
       완료 섹션 결과는 보존하고 남은 섹션은 not_run. 하나라도 ok가 아니면 전체를 ok로 기록하지 않는다."""
    import time

    import numpy as np
    from PIL import Image

    from pipeline.stages import inpaint
    from pipeline.types import InpaintCounts, InpaintFiles, InpaintResult

    guard_paths = [Path(p) for j in jobs for p in [j.get("image_path"), *j.get("input_paths", [])] if p is not None]
    guard = inpaint_out_guard(out, guard_paths)
    if guard:
        print(guard, file=sys.stderr)
        return 2
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"출력 폴더가 이미 있다 — 실행마다 새 --out을 쓴다: {out}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"출력 폴더를 만들 수 없다: {out}: {e}", file=sys.stderr)
        return 4
    record["sections"] = [{"image_id": j.get("image_id"), "section_key": getattr(j.get("section"), "section_key", None), "status": "not_run"}
                          for j in jobs]
    record["counts"] = {"sections_total": len(jobs)}
    plans: list[inpaint.SectionPlan] = []
    try:
        if mode not in INPAINT_MODES.values():
            raise inpaint.InpaintInputError(f"모드 {mode!r} — {sorted(INPAINT_MODES.values())} 중 하나")
        inpaint.validate_config(cfg)
        bad_ids = [(j.get("image_id"), getattr(j.get("section"), "section_key", None)) for j in jobs
                   if not (safe_path_component(j.get("image_id")) and safe_path_component(getattr(j.get("section"), "section_key", None)))]
        if bad_ids:
            raise inpaint.InpaintInputError(f"출력 경로로 쓸 수 없는 image_id · section_key(경로 구분자 · 상위 이동 · 드라이브 · 빈 값 금지) {bad_ids!r}")
        dups = duplicate_logo_jobs([(j["image_id"], j["section"].section_key) for j in jobs])
        if dups:
            raise inpaint.InpaintInputError(f"같은 원본 · 섹션 작업이 두 번 이상 있다(같은 출력 경로) {dups}")
        errors: list[str] = []
        for j in jobs:
            try:
                escaped = [p.as_posix() for p in inpaint_output_paths(j["image_id"], j["section"].section_key).values()
                           if not inside(out, out / p)]
                if escaped:
                    raise inpaint.InpaintInputError(f"출력 경로가 출력 폴더 밖이다 {escaped}")
                with Image.open(j["image_path"]) as im:
                    size, imode = im.size, im.mode
                if imode != "RGB":
                    raise inpaint.InpaintInputError(f"섹션 이미지 모드 {imode} — RGB만 지원(색 공간 변환 없음)")
                plans.append(inpaint.validate_inputs(j["image_id"], j["section"], size, j["merged"], j["label"], j["logo"],
                                                     j["logo_record"], cfg))
            except INPAINT_INPUT_ERRORS as e:
                errors.append(f"{j['image_id']}/{j['section'].section_key}: {e.__class__.__name__}: {e}")
        if errors:
            raise inpaint.InpaintInputError(" | ".join(errors))
    except inpaint.InpaintInputError as e:
        return _inpaint_finish(out, record, started, "input_error", f"사전 검사 실패: {e}", 2)
    except Exception as e:  # noqa: BLE001 — 검증 코드 자체의 예기치 않은 오류
        return _inpaint_finish(out, record, started, "failed", f"사전 검사 중 예기치 않은 오류: {e.__class__.__name__}: {e}", 4)

    model = None  # inpaint 모드에서 처음으로 비어 있지 않은 최종 마스크를 만날 때 초기화한다

    def finish(status: str, error: str | None, code: int) -> int:
        """모델(자식 프로세스)을 먼저 닫고 종료 기록을 남긴 뒤 run.json을 쓴다."""
        if model is not None and hasattr(model, "close"):
            try:
                record["model_termination"] = model.close()
            except Exception as e:  # noqa: BLE001 — 종료 기록 실패는 실행 결과를 바꾸지 않고 남긴다
                record["model_termination"] = {"error": f"{e.__class__.__name__}: {e}"}
        return _inpaint_finish(out, record, started, status, error, code)

    by_result = {s: 0 for s in ("masked", "inpainted", "unchanged", "failed")}
    record["counts"].update({"by_result": by_result, "final_px": 0})
    try:
        for entry, j, plan in zip(record["sections"], jobs, plans):
            image_id, key = plan.image_id, plan.section_key
            rel = inpaint_output_paths(image_id, key)
            files: dict[str, str | None] = {"final_mask": None, "protect_mask": None, "background": None}
            dbg: dict[str, Any] = {
                "stage": "inpaint", "image_id": image_id, "section_key": key, "mode": mode, "status": None, "error": None,
                "raster_rule": inpaint.RASTER_RULE, "config": plan.config, "fingerprints": dict(plan.fingerprints),
                "protected_blocks": [{"block_key": p.block_key, "reason": p.reason, "bbox": p.bbox.model_dump()} for p in plan.protected_blocks],
                "regions": None, "counts": None, "model": record["model"], "model_called": False, "inference_s": None,
            }
            counts = None
            model_called = False
            failure: str | None = None
            stage = "마스크 계산"
            try:
                with Image.open(j["image_path"]) as im:
                    image = np.array(im)
                inpaint.check_image(image, plan)
                dbg["fingerprints"]["image_file_sha256"] = jsonio.sha256_file(j["image_path"])
                dbg["fingerprints"]["image_pixels"] = inpaint.pixel_sha256(image)
                masks = inpaint.build_masks(plan)
                counts = masks.counts
                dbg["counts"], dbg["regions"] = counts, inpaint.region_diagnostics(plan, masks)
                final_u8 = masks.final.astype(np.uint8) * 255
                protect_u8 = masks.protect.astype(np.uint8) * 255
                dbg["fingerprints"]["final_mask_pixels"] = inpaint.pixel_sha256(final_u8)
                dbg["fingerprints"]["protect_mask_pixels"] = inpaint.pixel_sha256(protect_u8)
                if mode == "inpaint" and model is None and masks.final.any():  # 지연 초기화 — 빈 마스크만 있으면 모델이 필요 없다
                    try:
                        model = (model_factory or inpaint.build_model)(cfg)
                        record["model"] = dbg["model"] = model.describe()
                    except inpaint.ModelNotAvailable as e:
                        record["model_termination"] = getattr(e, "termination", None)  # 초기화 실패도 자식 종료 기록을 남긴다
                        raise _InpaintInitStop(3, f"모델 없음: {e}") from e
                    except Exception as e:  # noqa: BLE001 — 초기화 실패: CPU 대체 없음
                        record["model_termination"] = getattr(e, "termination", None)
                        raise _InpaintInitStop(4, f"모델 초기화 실패: {e.__class__.__name__}: {e}") from e
                stage = "마스크 저장"
                save_png_verified(out / rel["final_mask"], final_u8)
                files["final_mask"] = rel["final_mask"].as_posix()
                save_png_verified(out / rel["protect_mask"], protect_u8)
                files["protect_mask"] = rel["protect_mask"].as_posix()
                if mode == "inpaint":
                    stage = "추론"
                    t0 = time.perf_counter()
                    applied = inpaint.apply(image, plan, masks, model)
                    dbg["inference_s"] = round(time.perf_counter() - t0, 3) if applied.model_called else None
                    model_called = dbg["model_called"] = applied.model_called
                    if applied.model_called:
                        dbg["model_call"] = _last_call(model)  # 호출 번호 n · 구간별 시간 · 최대 메모리(첫 호출과 이후 구분용)
                    dbg["fingerprints"]["background_pixels"] = inpaint.pixel_sha256(applied.background)
                    stage = "배경 저장"
                    save_png_verified(out / rel["background"], applied.background)
                    files["background"] = rel["background"].as_posix()
            except _InpaintInitStop as e:  # 이 섹션은 파일 없이 not_run으로 두고 중단(완료 섹션은 보존)
                return finish("failed", f"{image_id}/{key} {e.message}", e.code)
            except inpaint.InpaintModelError as e:
                failure = f"{stage} 실패: {e}"
                model_called = dbg["model_called"] = True
                dbg["model_call"] = _last_call(model)
                if isinstance(getattr(model, "termination", None), dict):
                    dbg["model_termination"] = model.termination
            except Exception as e:  # noqa: BLE001
                if stage.endswith("저장"):  # 저장 오류: 성공으로 보고하지 않고 중단
                    entry.update({"status": "save_failed", "error": f"{stage} 실패: {e.__class__.__name__}: {e}"})
                    return finish("failed", f"{image_id}/{key} {stage} 실패: {e.__class__.__name__}: {e}", 4)
                failure = f"{stage} 중 예기치 않은 오류: {e.__class__.__name__}: {e}"
            if failure is not None:
                status = "failed"
            elif mode == "mask_only":
                status = "masked"
            else:
                status = "inpainted" if model_called else "unchanged"
            dbg.update({"status": status, "error": failure})
            try:
                result = InpaintResult(image_id=image_id, section_key=key, mode=mode, status=status, model_called=model_called,
                                       files=InpaintFiles(**files), counts=InpaintCounts(**counts) if counts else None, error=failure)
                jsonio.write_text_atomic(out / rel["debug"], json.dumps(dbg, ensure_ascii=False, indent=2))
                jsonio.write_text_atomic(out / rel["result"], result.model_dump_json(indent=2))
            except Exception as e:  # noqa: BLE001 — 결과 · 진단 저장 실패
                entry.update({"status": "save_failed", "result_status": status, "error": f"결과 저장 실패: {e.__class__.__name__}: {e}"})
                return finish("failed", f"{image_id}/{key} 결과 저장 실패: {e.__class__.__name__}: {e}", 4)
            by_result[status] += 1
            entry.update({"status": "failed" if failure else "ok", "result_status": status, "model_called": model_called,
                          "result": rel["result"].as_posix(), "debug": rel["debug"].as_posix(), "files": files,
                          "final_px": counts["final_px"] if counts else None, "fingerprints": dbg["fingerprints"]})
            if failure is not None:
                entry["error"] = failure
                return finish("failed", f"{image_id}/{key} {failure}", 4)
            record["counts"]["final_px"] += counts["final_px"]
    except BaseException:
        if model is not None and hasattr(model, "close"):  # 예기치 않은 예외 — 자식 프로세스를 남기지 않는다
            model.close()
        raise
    if any(s["status"] != "ok" for s in record["sections"]):  # 방어 — 위에서 이미 중단하지만 미처리 섹션을 ok로 기록하지 않는다
        return finish("failed", "처리되지 않았거나 실패한 섹션이 있다", 4)
    code = finish("ok", None, 0)
    if code == 0:
        what = "마스크만(인페인팅 아님)" if mode == "mask_only" else "인페인팅"
        print(f"⑥ ok · {what}: 섹션 {len(jobs)} · 결과 {by_result} · 최종 마스크 {record['counts']['final_px']}px → {out}")
    return code


def cmd_inpaint(args) -> int:
    """⑥ 한 섹션. split.json(섹션 이미지) + merge · label · logo · logo_debug + 원본 식별자 → <out>/<image_id>/inpaint*/.
    상대 경로는 실행 디렉터리 기준(split.json 안의 image_path는 그 JSON 폴더 기준). --out이 이미 있거나 고정 입력본 안이면 거부(2)."""
    from pipeline.stages import inpaint
    from pipeline.types import LabelResult, LogoResult

    started = datetime.now(timezone.utc)
    out = Path(args.out)
    mode = INPAINT_MODES[args.mode]
    paths = {"split": Path(args.split), "merge": Path(args.merge), "label": Path(args.label), "logo": Path(args.logo),
             "logo_debug": Path(args.logo_debug)}
    guard = inpaint_out_guard(out, list(paths.values()))
    if guard:
        print(guard, file=sys.stderr)
        return 2
    inputs: dict[str, Any] = {"image_id": args.image_id, "section": args.section, **{k: str(v) for k, v in paths.items()}}
    record: dict[str, Any] = {"stage": "inpaint", "mode": mode, "inputs": inputs}
    try:
        record = {**inpaint_run_base(mode, None, started), "inputs": inputs}
    except Exception as e:  # noqa: BLE001 — 기록 머리조차 못 만들면 최소 기록으로 내부 오류
        return _inpaint_finish(out, record, started, "failed", f"기록 준비 중 예기치 않은 오류: {e.__class__.__name__}: {e}", 4)
    try:
        cfg = _load_cfg(args)
        record["config"] = cfgmod.snapshot(cfg)
        inpaint.validate_config(cfg)
        inputs["sha256"] = {k: jsonio.sha256_file(p) for k, p in paths.items()}
        split = jsonio.load_split(paths["split"])
        found = [s for s in split.sections if s.section_key == args.section]
        if len(found) != 1:
            raise inpaint.InpaintInputError(f"split.json에 section_key {args.section!r}가 {len(found)}개 — 정확히 하나여야 한다")
        section = found[0]
        image_path = Path(section.image_path)
        inputs["image"] = str(image_path)
        inputs["sha256"]["image"] = jsonio.sha256_file(image_path)
        job = {"image_id": args.image_id, "section": section, "image_path": image_path, "input_paths": list(paths.values()),
               "merged": jsonio.load_merge(paths["merge"]), "label": jsonio.load_model(paths["label"], LabelResult),
               "logo": jsonio.load_model(paths["logo"], LogoResult),
               "logo_record": json.loads(paths["logo_debug"].read_text(encoding="utf-8"))}
    except INPAINT_INPUT_ERRORS as e:
        return _inpaint_finish(out, record, started, "input_error", f"{e.__class__.__name__}: {e}", 2)
    except Exception as e:  # noqa: BLE001 — 입력 로딩 중 예상하지 못한 내부 예외
        return _inpaint_finish(out, record, started, "failed", f"입력 로딩 중 예기치 않은 오류: {e.__class__.__name__}: {e}", 4)
    return execute_inpaint(out, [job], cfg, mode, record, started)


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
    if args.label:
        if not args.merge:
            raise SystemExit("--label에는 --merge(블록 좌표)가 필요하다")
        from pipeline.types import LabelResult

        out = insp.overlay_label(args.image, jsonio.load_merge(args.merge), jsonio.load_model(args.label, LabelResult), args.out)
        print(f"→ {out}")
        return 0
    if args.judge:
        if not args.merge:
            raise SystemExit("--judge에는 --merge(블록 좌표)가 필요하다")
        from pipeline.types import JudgeResult, PolicyResult

        judge = jsonio.load_model(args.judge, JudgeResult)
        policy = jsonio.load_model(args.policy, PolicyResult) if args.policy else None
        out = insp.overlay_judge(args.image, jsonio.load_merge(args.merge), judge, args.out, policy=policy)
        print(f"→ {out}")
        return 0
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

    sp = sub.add_parser("label", help="④ 제품 라벨 판정")
    common(sp)
    sp.add_argument("--merge", required=True, help="merge/<section_key>.json (③ 출력, 판정 대상 블록)")
    sp.add_argument("--split", required=True, help="split.json (섹션 이미지)")
    sp.add_argument("--out", required=True, help="새 출력 폴더(이전 실행 결과가 있으면 거부)")
    sp.add_argument("--llm-replay", metavar="label_debug/<KEY>.json", help="이전 기록의 응답을 재생(API 호출 없음, 입력 · 응답 다시 검증)")
    sp.set_defaults(fn=cmd_label)

    sp = sub.add_parser("logo", help="⑤ 브랜드 로고 제외(모델 호출 없음)")
    common(sp)
    sp.add_argument("--merge", required=True, help="merge/<section_key>.json (③ 출력, 판정 대상 블록)")
    sp.add_argument("--label", required=True, help="label/<section_key>.json (④ 출력, 같은 섹션 · status ok)")
    sp.add_argument("--brand-meta", required=True, help="brand_metadata.json (브랜드 자료, images 목록으로 원본 연결)")
    sp.add_argument("--image-id", required=True, help="원본 이미지 식별자(예 GS-01_001)")
    sp.add_argument("--out", required=True, help="새 출력 폴더(이미 있으면 거부)")
    sp.set_defaults(fn=cmd_logo)

    sp = sub.add_parser("inpaint", help="⑥ 인페인팅(개발용 — mask-only는 마스크만, inpaint는 LaMa GPU 추론)")
    common(sp)
    sp.add_argument("--mode", required=True, choices=sorted(INPAINT_MODES),
                    help="mask-only = 마스크 · 진단만(인페인팅 아님) / inpaint = LaMa GPU 추론(torch · 가중치가 없으면 종료 코드 3). 기본값 없음")
    sp.add_argument("--split", required=True, help="split.json (섹션 메타데이터 · 이미지)")
    sp.add_argument("--section", required=True, help="section_key")
    sp.add_argument("--image-id", required=True, help="원본 이미지 식별자(예 GS-01_001)")
    sp.add_argument("--merge", required=True, help="merge/<section_key>.json (③ 블록 · 원시 OCR 영역)")
    sp.add_argument("--label", required=True, help="label/<section_key>.json (④ 결과, status ok)")
    sp.add_argument("--logo", required=True, help="logo/<section_key>.json (⑤ 결과, status ok)")
    sp.add_argument("--logo-debug", required=True, help="logo_debug/<section_key>.json (⑤ 기록 — 블록 · ④ 지문 대조)")
    sp.add_argument("--out", required=True, help="새 출력 폴더(이미 있거나 고정 입력본 안이면 거부)")
    sp.set_defaults(fn=cmd_inpaint)

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
    sp.add_argument("--judge", help="judge/<key>.json — --merge와 함께. 매칭 후보 · finding 상태(P/A/U 색 구분) · 실패")
    sp.add_argument("--policy", help="policy/<key>.json — --judge와 함께. 권고 · verdict · 충돌")
    sp.add_argument("--label", help="label/<key>.json — --merge와 함께. 라벨 true(빨강) · false(초록) · 공백 규칙(회색) · 실패")
    sp.add_argument("--image", required=True, help="split은 원본, ocr·merge·judge·label은 섹션 이미지")
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
