"""⑦ 스타일 추출 개발용 실행 도구 — `downstream-input-v1`의 선택한 섹션에 style.run을 돌려 StyleResult를 저장한다(2026-10-06).

    python docs/ai-experiments/tools/style_run_driver.py --input pipeline/samples/local/downstream-input-v1 \
        --out pipeline/out/style-<이름>_<date> --section GS-01_001/sec_1_01 [--section ...]
    python docs/ai-experiments/tools/style_run_driver.py --input ... --out ... --all        # MANIFEST 대상 전체(이번 범위에서는 실행하지 않음)

범위(pipeline.md 7.7절 · open-questions.md #76): ⑦ **단독** 개발용 실행. 입력본 `MANIFEST.json` `images[].sections`에 있는 대상만 실행한다
(`split.sections` 전체가 아님). 입력 = 원본 섹션 이미지(`split.json`의 image_path, ⑥ 인페인팅 결과 아님) + ③ merge + ④ label + ⑤ logo ·
logo_debug. 계산은 `pipeline.stages.style.run`만 부르며 복제하지 않는다. 입력본은 읽기만 한다. 개발 대상을 사용자 최종 포함 섹션으로 보지 않는다.

사전 검사(하나라도 실패하면 결과 없이 run.json status=input_error · 종료 코드 2):
  --out이 이미 있거나 입력본 안이면 거부(아무것도 쓰지 않음) · 최상위 SHA256SUMS를 뺀 실제 파일 집합 = 해시 목록(⑤ 드라이버와 같은 검사) ·
  MANIFEST 구조와 (원본, 섹션) 중복 없음 · 선택한 대상 ⊆ MANIFEST 대상, 선택 중복 없음 · 읽을 입력 파일이 해시 목록에 있음 ·
  원본 · 섹션 식별자가 경로 구성 요소로 안전 · 선택한 섹션이 split.json에 있음 · [style] 설정.
섹션 실행: style.run의 StyleInputError → 그 섹션 input_error, 그 밖의 예외 → failed, 결과 저장 실패 → save_failed. 기록하고 다음 섹션을
계속한다(실패를 빈 결과 · 기본값으로 바꾸지 않는다). 전체 상태 ok(모두 ok) · partial(일부 ok) · failed(ok 없음), 종료 코드 0 · 4 · 4.
run.json 저장 자체가 실패하면 표준 오류로 알리고 4. 상태 값은 실행 기록용이며 ⑦ 결과 enum · 운영 오류 코드가 아니다.
출력: <out>/run.json · <out>/<원본>/style/<section_key>.json(StyleResult 그대로, 임시 파일 → 교체 후 다시 읽어 대조).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.run import _safe_console, fixed_bundle_roots, inside, safe_path_component  # noqa: E402
from pipeline.stages import style  # noqa: E402
from pipeline.types import LabelResult, LogoResult, StyleResult  # noqa: E402

SCOPE_NOTE = ("⑦ 단독 개발 v1 — 개발용 결과 형식 StyleResult(운영 text_block.style 계약 아님, #14 · #76) · 원본 섹션 이미지 사용 · "
              "MANIFEST 대상 중 선택한 섹션만 · 실행 도구 검증이며 품질 판정이 아니다")


def _load_logo_driver():
    """⑤ 드라이버의 해시 목록 · 파일 집합 검사를 그대로 쓴다(같은 입력본 규칙)."""
    path = Path(__file__).resolve().parent / "logo_run_driver.py"
    spec = importlib.util.spec_from_file_location("logo_run_driver", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DriverInputError(ValueError):
    """사전 검사에서 검출한 입력 · 선택 오류. 결과 없이 input_error · 2."""


def out_guard(out: Path, inp: Path) -> str | None:
    """출력 폴더 거부 사유 — 이미 있는 폴더(덮어쓰기), 입력본 폴더 안, 입력본이 속한 고정 입력본 안. 문제없으면 None."""
    if out.exists():
        return f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {out}"
    ro = out.resolve()
    roots = [inp.resolve(), *fixed_bundle_roots(inp / "MANIFEST.json")]
    for root in roots:
        if ro == root or root in ro.parents:
            return f"출력 폴더 {out}가 입력본 {root} 안에 있다 — 입력본은 읽기 전용이다"
    return None


def manifest_targets(manifest: Any) -> list[tuple[str, str]]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("images"), list):
        raise DriverInputError("MANIFEST.json에 images 목록이 없다")
    targets: list[tuple[str, str]] = []
    for i, img in enumerate(manifest["images"]):
        if not isinstance(img, dict) or not isinstance(img.get("image"), str) or not img["image"] or not isinstance(img.get("sections"), list):
            raise DriverInputError(f"MANIFEST images[{i}]에 image(문자열) · sections(목록)가 없다")
        for j, s in enumerate(img["sections"]):
            if not isinstance(s, dict) or not isinstance(s.get("section_key"), str) or not s["section_key"]:
                raise DriverInputError(f"MANIFEST images[{i}].sections[{j}]에 section_key(문자열)가 없다")
            targets.append((img["image"], s["section_key"]))
    dups = sorted({t for t in targets if targets.count(t) > 1})
    if dups:
        raise DriverInputError(f"MANIFEST에 같은 원본 · 섹션이 두 번 이상 있다 {dups}")
    return targets


def parse_selection(items: list[str] | None, all_: bool, targets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """--section 원본/섹션(반복) 또는 --all → 실행 대상(MANIFEST 순서가 아니라 요청 순서, --all은 MANIFEST 순서)."""
    if all_:
        return list(targets)
    sel: list[tuple[str, str]] = []
    for item in items or []:
        image_id, sep, key = item.partition("/")
        if not sep or not image_id or not key or "/" in key:
            raise DriverInputError(f"--section 형식은 원본/섹션이다: {item!r}")
        sel.append((image_id, key))
    dups = sorted({t for t in sel if sel.count(t) > 1})
    if dups:
        raise DriverInputError(f"같은 원본 · 섹션을 두 번 선택했다 {dups}")
    unknown = [t for t in sel if t not in set(targets)]
    if unknown:
        raise DriverInputError(f"MANIFEST images[].sections에 없는 대상 {unknown} — split.json 섹션이라도 MANIFEST 대상이 아니면 실행하지 않는다")
    return sel


def input_files(image_id: str, key: str) -> dict[str, str]:
    """섹션 하나가 읽는 입력본 파일(입력본 기준 상대 경로)."""
    return {"split": f"{image_id}/split.json", "merge": f"{image_id}/merge/{key}.json", "label": f"{image_id}/label/{key}.json",
            "logo": f"{image_id}/logo/{key}.json", "logo_debug": f"{image_id}/logo_debug/{key}.json"}


def run_base(started: datetime) -> dict[str, Any]:
    import numpy
    import PIL

    return {"stage": "style", "mode": "driver", "scope": SCOPE_NOTE, "status": None, "error": None,
            "started_at": started.isoformat(timespec="seconds"), "ran_at": None, "duration_s": None, "config": None,
            "luma_rule": style.LUMA_RULE, "python": platform.python_version(), "numpy": numpy.__version__, "pillow": PIL.__version__,
            "platform": platform.platform(), **jsonio.git_state(), "inputs": {}, "selection": None, "counts": None, "sections": []}


def finish(out: Path, record: dict[str, Any], started: datetime, status: str, error: str | None, code: int) -> int:
    ended = datetime.now(timezone.utc)
    secs = record.get("sections") or []
    record.update({"status": status, "error": error, "ran_at": ended.isoformat(timespec="seconds"),
                   "duration_s": round((ended - started).total_seconds(), 3),
                   "counts": {"selected": len(secs), **{k: sum(1 for s in secs if s.get("status") == k)
                                                         for k in ("ok", "input_error", "failed", "save_failed")}}})
    try:
        jsonio.write_text_atomic(out / "run.json", json.dumps(record, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001 — 폴더 생성 · 직렬화 · 저장 실패 모두 기록 없음
        print(f"⑦ run.json 저장 실패 — 실행 기록이 남지 않았다: {e.__class__.__name__}: {e} (상태 {status})", file=sys.stderr)
        return 4
    if error:
        print(f"⑦ {status}: {error} (run.json에 기록)", file=sys.stderr)
    return code


def save_result(out: Path, rel: Path, result: StyleResult) -> None:
    """StyleResult를 임시 파일 → 교체로 쓰고 다시 읽어 같은지 확인한다. 다르면 OSError(저장 실패)."""
    path = out / rel
    if not inside(out, path):
        raise OSError(f"결과 경로가 출력 폴더 밖이다: {path}")
    jsonio.write_text_atomic(path, result.model_dump_json(indent=2))
    back = StyleResult.model_validate_json(path.read_text(encoding="utf-8"))
    if back != result:
        raise OSError(f"저장한 결과를 다시 읽은 값이 다르다: {path}")


def execute(out: Path, inp: Path, selection: list[tuple[str, str]], cfg: dict[str, Any], record: dict[str, Any], started: datetime) -> int:
    """선택한 섹션마다 style.run → 저장. 섹션 실패는 기록하고 계속한다."""
    out.mkdir(parents=True)
    splits: dict[str, Any] = {}
    for image_id, key in selection:
        files = input_files(image_id, key)
        rel = Path(image_id) / "style" / f"{key}.json"
        entry: dict[str, Any] = {"image_id": image_id, "section_key": key, "status": None, "result": None, "error": None, "inputs": None}
        record["sections"].append(entry)
        try:
            entry["inputs"] = {k: {"path": v, "sha256": jsonio.sha256_file(inp / v)} for k, v in files.items()}
            if image_id not in splits:
                splits[image_id] = jsonio.load_split(inp / files["split"])
            section = next(s for s in splits[image_id].sections if s.section_key == key)
            entry["inputs"]["section_image"] = {"path": Path(section.image_path).resolve().relative_to(inp.resolve()).as_posix(),
                                                "sha256": jsonio.sha256_file(section.image_path)}
            merged = jsonio.load_merge(inp / files["merge"])
            label = jsonio.load_model(inp / files["label"], LabelResult)
            logo = jsonio.load_model(inp / files["logo"], LogoResult)
            logo_record = json.loads((inp / files["logo_debug"]).read_text(encoding="utf-8"))
            result = style.run(image_id, section, merged, label, logo, logo_record, cfg)
        except style.StyleInputError as e:
            entry.update(status="input_error", error=f"{e.__class__.__name__}: {e}")
            continue
        except Exception as e:  # noqa: BLE001 — 섹션 실행 중 예상하지 못한 오류: 기록 후 계속(성공으로 바꾸지 않는다)
            entry.update(status="failed", error=f"{e.__class__.__name__}: {e}")
            continue
        try:
            save_result(out, rel, result)
        except Exception as e:  # noqa: BLE001 — 저장 실패는 결과 없음으로 드러낸다
            entry.update(status="save_failed", error=f"{e.__class__.__name__}: {e}")
            continue
        blocks = result.blocks
        entry.update(status="ok", result=rel.as_posix(), blocks={
            "total": len(blocks), **{s: sum(b.status == s for b in blocks) for s in ("ok", "partial", "no_text", "excluded")}})
    statuses = [s["status"] for s in record["sections"]]
    if all(s == "ok" for s in statuses):
        return finish(out, record, started, "ok", None, 0)
    bad = len([s for s in statuses if s != "ok"])
    status = "partial" if "ok" in statuses else "failed"
    return finish(out, record, started, status, f"섹션 {bad}/{len(statuses)}개가 ok가 아니다(sections 참조)", 4)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="downstream-input-v1 폴더(MANIFEST.json · SHA256SUMS)")
    ap.add_argument("--out", required=True, help="새 출력 폴더(이미 있거나 입력본 안이면 거부)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--section", action="append", help="원본/섹션(반복 가능), 예 GS-01_001/sec_1_01")
    g.add_argument("--all", action="store_true", help="MANIFEST 대상 전체")
    a = ap.parse_args(argv)
    _safe_console()

    started = datetime.now(timezone.utc)
    inp, out = Path(a.input), Path(a.out)
    reason = out_guard(out, inp)
    if reason:  # 아무것도 쓰지 않는다
        print(reason, file=sys.stderr)
        return 2
    record = run_base(started)
    record["inputs"] = {"input": str(inp)}
    try:
        cfg = cfgmod.load_config()
        record["config"] = cfgmod.snapshot(cfg)
        style.validate_config(cfg)
        ld = _load_logo_driver()
        sums = ld.read_sums(inp)
        errs = ld.verify_file_set(inp, sums)
        if errs:
            raise DriverInputError(f"파일 집합 · 해시 검사 실패 {len(errs)}건: " + "; ".join(errs[:20]) + (" …" if len(errs) > 20 else ""))
        manifest = json.loads((inp / "MANIFEST.json").read_text(encoding="utf-8"))
        targets = manifest_targets(manifest)
        selection = parse_selection(a.section, a.all, targets)
        record["inputs"].update({"input_name": manifest.get("name"), "input_sha256sums_sha256": jsonio.sha256_file(inp / "SHA256SUMS"),
                                 "file_check": "ok", "manifest_targets": len(targets)})
        record["selection"] = {"mode": "all" if a.all else "explicit", "sections": [f"{i}/{k}" for i, k in selection]}
        bad_ids = [f"{i}/{k}" for i, k in selection if not (safe_path_component(i) and safe_path_component(k))]
        if bad_ids:
            raise DriverInputError(f"경로 구성 요소로 쓸 수 없는 식별자 {bad_ids}")
        unlisted = sorted({v for i, k in selection for v in input_files(i, k).values()} - set(sums))
        if unlisted:
            raise DriverInputError(f"입력본 SHA256SUMS에 없는 입력 파일 {unlisted[:20]}")
        for image_id in dict.fromkeys(i for i, _ in selection):
            keys = {s.section_key for s in jsonio.load_split(inp / image_id / "split.json").sections}
            missing = [k for i, k in selection if i == image_id and k not in keys]
            if missing:
                raise DriverInputError(f"{image_id}/split.json에 없는 섹션 {missing}")
    except (DriverInputError, style.StyleInputError, ValueError, OSError) as e:  # 검출한 입력 · 선택 · 설정 오류 — 결과 없음
        return finish(out, record, started, "input_error", f"{e.__class__.__name__}: {e}", 2)
    except Exception as e:  # noqa: BLE001 — 사전 검사 중 예상하지 못한 내부 예외(⑤ 선례와 같이 failed · 4)
        return finish(out, record, started, "failed", f"사전 검사 내부 오류 {e.__class__.__name__}: {e}", 4)
    return execute(out, inp, selection, cfg, record, started)


if __name__ == "__main__":
    sys.exit(main())
