"""단계 JSON 파일 읽기·쓰기 — 버전 확인 · image_path 해석 · 고정 입력본 (dev.md 3·4·5절).

- 경로를 담는 파일(split.json · analyze.json)의 상대 image_path는 **그 JSON 파일이 있는 폴더 기준**이다(버전 "2").
  작업 디렉터리 기준으로 되돌아가 찾지 않는다. 절대 경로는 그대로 쓴다.
- 읽기는 반드시 load_*()를 거친다. 버전 필드가 없거나 지원하지 않는 버전이면 SchemaVersionError.
  (pydantic 기본값이 빠진 버전을 조용히 채우지 않게, 모델 검증 전에 원본 JSON을 본다.)
- 쓰기는 write_model()을 거친다. 경로는 JSON 폴더 기준 상대 경로(다른 드라이브 등 불가하면 절대 경로)로 기록한다.
- convert_split(): 버전 1 split.json의 경로 표기만 바꾼다. 이미지 대상은 그대로다.
- freeze_input(): 섹션 이미지를 입력본 폴더로 복사하고 복사본을 가리키게 한 뒤 검증한다(verify_input).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image
from pydantic import BaseModel

from pipeline.types import SCHEMA_VERSION, AnalyzeResult, MergeResult, OcrResult, SplitResult

# 타입별 읽기 허용 버전. 경로 필드가 있는 타입만 "1"을 거부한다(상대 경로 의미가 바뀌었으므로).
READ_VERSIONS: dict[type[BaseModel], frozenset[str]] = {
    SplitResult: frozenset({"2"}),
    AnalyzeResult: frozenset({"2"}),
    OcrResult: frozenset({"1", "2"}),
    MergeResult: frozenset({"1", "2"}),
}
_PATH_TYPES = (SplitResult, AnalyzeResult)
REPO_ROOT = Path(__file__).resolve().parent.parent


class SchemaVersionError(ValueError):
    pass


class InputCheckError(ValueError):
    """고정 입력본 검증 실패."""


# ---- 읽기 ---------------------------------------------------------------------------
def _check_version(raw: dict[str, Any], cls: type[BaseModel], path: Path) -> None:
    supported = sorted(READ_VERSIONS[cls])
    if "schema_version" not in raw:
        raise SchemaVersionError(f"{path}: schema_version 없음 ({cls.__name__} 지원 버전 {supported})")
    found = raw["schema_version"]
    if found in READ_VERSIONS[cls]:
        return
    hint = ""
    if found == "1" and cls is SplitResult:
        hint = " — 버전 1은 상대 경로 기준이 다르다. `python -m pipeline.run convert-split` 또는 `freeze-input`으로 전환"
    elif found == "1" and cls is AnalyzeResult:
        hint = " — 이 도구에서는 analyze.json 버전 1 전환을 지원하지 않음"
    raise SchemaVersionError(f"{path}: {cls.__name__} schema_version {found!r} 미지원 (지원 {supported}){hint}")


def resolve_image_path(image_path: str, json_dir: Path) -> str:
    p = Path(image_path)
    return str(p if p.is_absolute() else (json_dir / p).resolve())


def load_model(path: str | Path, cls: type[BaseModel]):
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    _check_version(raw, cls, path)
    model = cls.model_validate(raw)
    if isinstance(model, _PATH_TYPES):
        json_dir = path.resolve().parent
        for s in model.sections:
            s.image_path = resolve_image_path(s.image_path, json_dir)
    return model


def load_split(path: str | Path) -> SplitResult:
    return load_model(path, SplitResult)


def load_analyze(path: str | Path) -> AnalyzeResult:
    return load_model(path, AnalyzeResult)


def load_ocr(path: str | Path) -> OcrResult:
    return load_model(path, OcrResult)


def load_merge(path: str | Path) -> MergeResult:
    return load_model(path, MergeResult)


# ---- 쓰기 ---------------------------------------------------------------------------
def relative_image_path(image_path: str, json_dir: Path) -> str:
    """json_dir 기준 상대 경로(/ 구분). 상대 경로를 만들 수 없으면(다른 드라이브) 절대 경로."""
    target = Path(image_path).resolve()
    try:
        return Path(os.path.relpath(target, json_dir.resolve())).as_posix()
    except ValueError:
        return str(target)


def write_model(path: str | Path, model: BaseModel) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(model, _PATH_TYPES):
        model = model.model_copy(deep=True)
        for s in model.sections:
            s.image_path = relative_image_path(s.image_path, path.parent)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    return path


# ---- 도우미 -------------------------------------------------------------------------
def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_state(repo: Path = REPO_ROOT) -> dict[str, Any]:
    """실행 기록용 커밋 해시와 작업 트리 변경 여부. git이 없으면 None."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=repo, capture_output=True, text=True, check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": None, "git_dirty": None}
    return {"git_commit": commit, "git_dirty": dirty}


def check_continuity(split: SplitResult) -> None:
    """섹션이 원본 세로 [0, source_height)를 틈·겹침 없이 덮는지."""
    secs = sorted(split.sections, key=lambda s: s.top_offset)
    if not secs:
        raise InputCheckError("섹션이 없음")
    if secs[0].top_offset != 0:
        raise InputCheckError(f"첫 섹션 시작 {secs[0].top_offset} ≠ 0")
    for a, b in zip(secs, secs[1:]):
        if a.top_offset + a.height != b.top_offset:
            raise InputCheckError(f"{a.section_key} 끝 {a.top_offset + a.height} ≠ {b.section_key} 시작 {b.top_offset}")
    last = secs[-1]
    if last.top_offset + last.height != split.source_height:
        raise InputCheckError(f"마지막 섹션 끝 {last.top_offset + last.height} ≠ source_height {split.source_height}")
    for s in secs:
        if s.width != split.source_width:
            raise InputCheckError(f"{s.section_key} 폭 {s.width} ≠ source_width {split.source_width}")


def _read_v1_split(path: Path, base: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    """버전 1 split.json → (원본 dict, section_key → 실제 이미지 경로). 옛 상대 경로는 base 기준."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != "1":
        raise SchemaVersionError(f"{path}: 버전 1 split.json이 아님 (schema_version={raw.get('schema_version')!r})")
    targets: dict[str, Path] = {}
    for s in raw["sections"]:
        p = Path(s["image_path"])
        target = p if p.is_absolute() else base / p
        if not target.is_file():
            raise InputCheckError(f"{s['section_key']}: 이미지 없음 {target} (image_path={s['image_path']!r}, base={base})")
        targets[s["section_key"]] = target.resolve()
    return raw, targets


def _source_split(path: Path, base: Path | None) -> tuple[SplitResult, dict[str, str], str]:
    """버전 1(base 필요) 또는 2의 split.json → (모델·절대 경로, 원래 image_path, 원래 버전)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    version = raw.get("schema_version")
    original = {s["section_key"]: s["image_path"] for s in raw.get("sections", [])}
    if version == "1":
        if base is None:
            raise SchemaVersionError(f"{path}: 버전 1은 옛 상대 경로의 기준 폴더(--base)가 필요")
        raw, targets = _read_v1_split(path, base)
        raw = {**raw, "schema_version": SCHEMA_VERSION}
        split = SplitResult.model_validate(raw)
        for s in split.sections:
            s.image_path = str(targets[s.section_key])
    else:
        split = load_split(path)
        for s in split.sections:
            if not Path(s.image_path).is_file():
                raise InputCheckError(f"{s.section_key}: 이미지 없음 {s.image_path}")
    return split, original, version


# ---- 일반 전환 ----------------------------------------------------------------------
def convert_split(src: str | Path, dst: str | Path, base: str | Path, overwrite: bool = False) -> dict[str, str]:
    """버전 1 split.json을 버전 2로 쓴다. 이미지는 옮기지 않으며 결과는 원래 이미지를 가리킨다.

    반환: section_key → 원래 image_path (호출자가 기록한다).
    """
    src, dst = Path(src), Path(dst)
    if dst.exists() and not overwrite:
        raise FileExistsError(f"{dst} 이미 있음 — 덮어쓰려면 overwrite")
    raw, targets = _read_v1_split(src, Path(base))
    original = {s["section_key"]: s["image_path"] for s in raw["sections"]}
    split = SplitResult.model_validate({**raw, "schema_version": SCHEMA_VERSION})
    for s in split.sections:
        s.image_path = str(targets[s.section_key])
    write_model(dst, split)
    return original


# ---- 고정 입력본 --------------------------------------------------------------------
def _hash_tree(files: list[Path]) -> dict[str, str]:
    return {str(p): sha256_file(p) for p in files}


def verify_input(root: str | Path) -> dict[str, Any]:
    """입력본 폴더 하나(root/split.json)를 검증한다. 실패하면 InputCheckError.

    - 모든 image_path가 root 내부 파일을 가리킨다(원본 참조가 남아 있으면 실패).
    - 이미지 SHA-256이 provenance.json 기록과 같다.
    - 이미지 크기 = (width, height), 섹션 연속성.
    """
    root = Path(root).resolve()
    split = load_split(root / "split.json")
    prov = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    recorded = {s["section_key"]: s["sha256"] for s in prov["sections"]}
    check_continuity(split)
    for s in split.sections:
        p = Path(s.image_path).resolve()
        if not p.is_relative_to(root):
            raise InputCheckError(f"{s.section_key}: 입력본 밖을 가리킴 {p}")
        if not p.is_file():
            raise InputCheckError(f"{s.section_key}: 이미지 없음 {p}")
        if sha256_file(p) != recorded.get(s.section_key):
            raise InputCheckError(f"{s.section_key}: SHA-256이 provenance 기록과 다름")
        with Image.open(p) as im:
            if im.size != (s.width, s.height):
                raise InputCheckError(f"{s.section_key}: 이미지 크기 {im.size} ≠ ({s.width}, {s.height})")
    return {"sections": len(split.sections), "root": str(root)}


def verify_relocated(root: str | Path) -> dict[str, Any]:
    """입력본을 레포 밖 임시 위치로 통째 복사해 그 복사본만으로 verify_input이 통과하는지 본다."""
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="pixlate_input_") as tmp:
        moved = Path(tmp) / root.name
        shutil.copytree(root, moved)
        res = verify_input(moved)
    return {**res, "relocated": True}


def freeze_input(
    src_split: str | Path, out_dir: str | Path, base: str | Path | None = None, meta: dict[str, str] | None = None
) -> dict[str, Any]:
    """고정 입력본 생성: 섹션 PNG를 out_dir/sections/로 복사하고 복사본을 가리키는 split.json(버전 2)을 쓴다.

    원본(src_split 폴더·이미지)은 읽기만 하며 전후 해시를 비교한다. out_dir가 비어 있지 않으면 거부한다.
    출처는 out_dir/provenance.json, 원본 기록 파일은 out_dir/origin/에 둔다.
    """
    src_split, out_dir = Path(src_split).resolve(), Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} 비어 있지 않음 — 입력본은 덮어쓰지 않는다")
    base_p = Path(base).resolve() if base else None
    split, original, version = _source_split(src_split, base_p)

    origin_files = [src_split] + [src_split.parent / n for n in ("split_debug.json", "run.json") if (src_split.parent / n).is_file()]
    watched = origin_files + [Path(s.image_path) for s in split.sections]
    before = _hash_tree(watched)

    names = [Path(s.image_path).name for s in split.sections]
    if len(set(names)) != len(names):
        raise InputCheckError("섹션 이미지 파일명이 겹침 — 복사 위치를 정할 수 없음")
    (out_dir / "sections").mkdir(parents=True, exist_ok=True)
    (out_dir / "origin").mkdir(exist_ok=True)
    sections_rec = []
    for s in split.sections:
        src_img = Path(s.image_path)
        dst_img = out_dir / "sections" / src_img.name
        shutil.copy2(src_img, dst_img)
        sections_rec.append({
            "section_key": s.section_key,
            "original_image_path": original[s.section_key],
            "resolved_source": str(src_img),
            "copied_to": f"sections/{src_img.name}",
            "sha256": before[str(src_img)],
        })
        s.image_path = str(dst_img)
    for f in origin_files:
        shutil.copy2(f, out_dir / "origin" / f.name)
    write_model(out_dir / "split.json", split)

    prov: dict[str, Any] = {
        "tool": "pipeline.run freeze-input",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **git_state(),
        "source_split": str(src_split),
        "source_schema_version": version,
        "base": str(base_p) if base_p else None,
        "meta": meta or {},
        "origin_files": {f"origin/{f.name}": before[str(f)] for f in origin_files},
        "sections": sections_rec,
    }
    (out_dir / "provenance.json").write_text(json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")

    checks = {"inplace": verify_input(out_dir), "relocated": verify_relocated(out_dir)}
    after = _hash_tree(watched)
    if after != before:
        changed = sorted(k for k in before if before[k] != after.get(k))
        raise InputCheckError(f"원본이 바뀜: {changed}")
    prov["verification"] = {"ok": True, "source_unchanged": True, **checks}
    (out_dir / "provenance.json").write_text(json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")
    return prov
