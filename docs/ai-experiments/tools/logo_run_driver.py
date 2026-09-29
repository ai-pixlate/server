"""⑤ 브랜드 로고 제외 실측 드라이버 — `logo-input-v1` 전체 섹션에 logo.run(모델 호출 없음)을 돌린다(2026-09-29 구현, 미실행).

    python docs/ai-experiments/tools/logo_run_driver.py --input pipeline/samples/local/logo-input-v1 \
        --brand-meta pipeline/samples/local/brand-meta-v1 --out pipeline/out/logo-run-v1_<date>

범위(사용자 승인 2026-09-29, open-questions #68): ⑤ **단독** 실측. 입력본 `MANIFEST.json` `images[].sections`만 순회하고
(`split.sections` 전체가 아님), 개발 대상 전체를 사용자가 최종 포함한 섹션으로 간주하지 않는다. 입력 두 자료는 읽기만 한다.
자료 안의 옛 경로(`for_input.path`, 실행 기록 속 경로)는 출처 기록이며 실제 경로는 실행 인자를 쓴다.

사전 검사(하나라도 실패하면 판정 결과 없이 run.json status=input_error · 종료 코드 2):
  입력본 · 브랜드 자료 각각 최상위 SHA256SUMS를 뺀 실제 파일 집합 = 해시 목록(누락 · 해시 불일치 · 목록 밖 파일은 입력 오류, 지우지 않음,
  하위 SHA256SUMS는 일반 파일), 실제로 읽는 파일(MANIFEST · merge · label · brand_metadata.json)이 목록에 있음,
  브랜드 자료 for_input(name · SHA256SUMS 해시) = 이 입력본, products[].images ⊆ 입력본 원본 · not_in_input 제외 원본 ∩ 개발 대상 = ∅,
  대상 섹션 전부의 ③ · ④ · 브랜드 입력 검증(logo.validate_inputs).
판정 · 저장 · 종료 코드는 pipeline.run.execute_logo와 같다(0 성공 · 2 입력 · 설정 오류 · 4 실행 · 저장 오류, 자동 재시도 · 이어 실행 없음).
출력: <out>/run.json · <out>/<원본>/logo/<key>.json · <out>/<원본>/logo_debug/<key>.json. --out이 이미 있으면 거부(2).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from pipeline import config as cfgmod  # noqa: E402
from pipeline import jsonio  # noqa: E402
from pipeline.run import _safe_console, execute_logo, logo_input_error, logo_run_base  # noqa: E402
from pipeline.stages import logo  # noqa: E402
from pipeline.types import LabelResult  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_sums(root: Path) -> dict[str, str]:
    """sha256sum 형식(`<hash>  <path>` 또는 `<hash> *<path>`) → {상대 경로: hash}. 형식이 틀린 줄은 ValueError."""
    sums: dict[str, str] = {}
    for n, line in enumerate((root / "SHA256SUMS").read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        h, sep, rest = line.partition(" ")
        rel = rest[1:] if rest[:1] in (" ", "*") else ""
        if len(h) != 64 or not sep or not rel:
            raise ValueError(f"{root / 'SHA256SUMS'} {n}번째 줄 형식 오류")
        if rel in sums:
            raise ValueError(f"{root / 'SHA256SUMS'}에 같은 경로가 두 번 있다: {rel}")
        sums[rel] = h.lower()
    return sums


def verify_file_set(root: Path, sums: dict[str, str]) -> list[str]:
    """최상위 SHA256SUMS 자신을 뺀 실제 파일 집합 = 해시 목록(#68 수정 5). 누락 · 해시 불일치 · 목록 밖 파일을 오류 목록으로 돌려준다.
    하위 폴더의 SHA256SUMS(예 upstream 사본)는 일반 파일로 검사한다. 목록 밖 파일(desktop.ini 등)을 지우지 않는다."""
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - {"SHA256SUMS"}
    errors = [f"없음 {rel}" for rel in sorted(set(sums) - actual)]
    errors += [f"목록 밖 파일 {rel}" for rel in sorted(actual - set(sums))]
    errors += [f"해시 불일치 {rel}" for rel in sorted(set(sums) & actual) if jsonio.sha256_file(root / rel) != sums[rel]]
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="logo-input-v1 폴더")
    ap.add_argument("--brand-meta", required=True, help="brand-meta-v1 폴더(brand_metadata.json · SHA256SUMS)")
    ap.add_argument("--out", required=True, help="새 출력 폴더(이미 있으면 거부)")
    a = ap.parse_args(argv)
    _safe_console()

    started = datetime.now(timezone.utc)
    inp, bdir, out = Path(a.input), Path(a.brand_meta), Path(a.out)
    if out.exists():
        print(f"출력 폴더가 이미 있다 — 덮어쓰지 않는다: {out}", file=sys.stderr)
        return 2
    record = logo_run_base("driver", None, started)
    record["inputs"] = {"input": str(inp), "brand_meta": str(bdir)}
    try:
        cfg = cfgmod.load_config()
        record["config"] = cfgmod.snapshot(cfg)
        logo.validate_config(cfg)

        # 두 자료의 해시 목록 · 연결
        in_sums, br_sums = read_sums(inp), read_sums(bdir)
        errs = [f"입력본 {e}" for e in verify_file_set(inp, in_sums)] + [f"브랜드 자료 {e}" for e in verify_file_set(bdir, br_sums)]
        if errs:
            raise logo.LogoInputError(f"파일 집합 · 해시 검사 실패 {len(errs)}건: " + "; ".join(errs[:20]) + (" …" if len(errs) > 20 else ""))
        manifest = json.loads((inp / "MANIFEST.json").read_text(encoding="utf-8"))
        brand_meta = json.loads((bdir / "brand_metadata.json").read_text(encoding="utf-8"))
        input_sums_sha = _sha(inp / "SHA256SUMS")
        for_input = brand_meta.get("for_input") if isinstance(brand_meta, dict) else None
        record["inputs"].update({
            "input_name": manifest.get("name"), "input_sha256sums_sha256": input_sums_sha,
            "brand_version": brand_meta.get("version") if isinstance(brand_meta, dict) else None,
            "brand_sha256sums_sha256": _sha(bdir / "SHA256SUMS"), "brand_metadata_sha256": _sha(bdir / "brand_metadata.json"),
            "brand_for_input": for_input,  # 자료에 적힌 연결 정보(path는 출처 기록일 뿐 쓰지 않는다)
        })
        if not isinstance(for_input, dict) or for_input.get("name") != manifest.get("name") \
                or for_input.get("sha256sums_sha256") != input_sums_sha:
            raise logo.LogoInputError(
                f"브랜드 자료의 연결 입력본 {for_input!r}이 이 입력본(name {manifest.get('name')!r} · SHA256SUMS {input_sums_sha})과 다르다")
        if "brand_metadata.json" not in br_sums:
            raise logo.LogoInputError("brand_metadata.json이 브랜드 자료 SHA256SUMS에 없다")
        record["inputs"]["link_check"] = "ok"

        # MANIFEST images[].sections만 순회
        jobs, unlisted = [], []
        for img in manifest["images"]:
            image_id = img["image"]
            for s in img["sections"]:
                key = s["section_key"]
                rels = {"MANIFEST.json", f"{image_id}/merge/{key}.json", f"{image_id}/label/{key}.json"}
                unlisted += sorted(rels - set(in_sums))
                jobs.append({"image_id": image_id,
                             "merged": jsonio.load_merge(inp / image_id / "merge" / f"{key}.json"),
                             "label": jsonio.load_model(inp / image_id / "label" / f"{key}.json", LabelResult)})
        if unlisted:
            raise logo.LogoInputError(f"입력본 SHA256SUMS에 없는 입력 파일 {sorted(set(unlisted))[:20]}")
        # 브랜드 원본 목록 ↔ 개발 대상(#68 수정 5): products[].images는 입력본 원본만, not_in_input 제외 원본은 개발 대상과 겹치지 않아야 한다
        manifest_images = {img["image"] for img in manifest["images"]}
        if not isinstance(brand_meta, dict) or not isinstance(brand_meta.get("products"), list):
            raise logo.LogoInputError("브랜드 자료에 products 목록이 없다")
        brand_images = {x for p in brand_meta["products"] if isinstance(p, dict) for x in (p.get("images") or [])}
        outside = sorted(brand_images - manifest_images)
        if outside:
            raise logo.LogoInputError(f"브랜드 자료 products[].images에 입력본 밖 원본이 있다 {outside}")
        excluded = logo.not_in_input_images(brand_meta)
        clash = sorted(set(excluded) & manifest_images)
        if clash:
            raise logo.LogoInputError(f"브랜드 자료 not_in_input 제외 원본이 개발 대상과 겹친다 {clash}")
        record["inputs"].update({
            "targets": {"images": len(manifest["images"]), "sections": len(jobs), "blocks": sum(len(j["merged"].blocks) for j in jobs)},
            "brand_not_in_input": sorted(excluded),  # 허용한 제외 원본(개발 대상 아님)
        })
    except (ValueError, OSError, KeyError, TypeError) as e:  # 입력 · 설정 오류 — 판정 결과 없음
        return logo_input_error(out, record, started, f"{e.__class__.__name__}: {e}")
    return execute_logo(out, jobs, brand_meta, cfg, record, started)


if __name__ == "__main__":
    sys.exit(main())
