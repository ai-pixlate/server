"""역할별 스타일 기본값의 폰트 파일 배치 · 검증 — 공식 배포 zip에서 필요한 파일만 꺼낸다.

    python -m pipeline.data.fonts.tools.fetch_fonts              # 공식 URL에서 받아 배치 후 검증
    python -m pipeline.data.fonts.tools.fetch_fonts --zip Z.zip  # 이미 받은 공식 zip으로 배치(네트워크 없음)
    python -m pipeline.data.fonts.tools.fetch_fonts --check      # 배치된 파일만 검증(해시 · 굵기 · 이름 메타데이터)

출처 · 해시 · 대상 경로는 모두 `pipeline/data/style_defaults.json`의 `fonts`에서 읽는다(이 파일에 값을 두지 않는다).
zip 자체의 SHA-256이 다르면 아무것도 쓰지 않는다. 꺼낸 파일도 해시를 대조한 뒤에만 제자리로 옮긴다.
시스템 설치 폰트를 찾거나 대체하지 않는다. 종료 코드: 0 성공 · 1 검증 실패 · 2 입력 오류.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from pipeline.handoff.style_defaults import (FontAssetError, StyleDefaults, StyleDefaultsError, load_style_defaults,
                                             verify_fonts)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def place_from_zip(d: StyleDefaults, zip_path: Path) -> list[Path]:
    """검증된 공식 zip에서 설정 자료에 적힌 파일만 꺼내 data 폴더에 둔다. 반환: 배치한 경로."""
    pkg = d.data.fonts.package
    got = _sha256(zip_path)
    if got != pkg.zip_sha256:
        raise FontAssetError(f"zip SHA-256 {got} ≠ 설정 {pkg.zip_sha256} — 공식 {pkg.release} 배포본이 아니다")
    wanted = [(pkg.license_member, pkg.license_file, pkg.license_sha256)]
    wanted += [(f.zip_member, f.file, f.sha256) for f in d.data.fonts.faces.values()]
    placed = []
    with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory() as tmp:
        for member, rel, sha in wanted:
            tmp_file = Path(tmp) / Path(rel).name
            with z.open(member) as src, open(tmp_file, "wb") as dst:  # zip 안의 경로를 그대로 풀지 않는다
                shutil.copyfileobj(src, dst)
            if _sha256(tmp_file) != sha:
                raise FontAssetError(f"{member}: SHA-256이 설정과 다르다")
            dest = d.base_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp_file, dest)
            placed.append(dest)
    return placed


def _safe_console() -> None:
    """cp949 콘솔에서도 특수문자 때문에 실패하지 않게 표현 못 하는 문자만 바꾼다(pipeline/run.py와 같은 처리)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--defaults", help="설정 자료 경로(기본: 저장소 정본)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--zip", type=Path, help="이미 받은 공식 배포 zip")
    g.add_argument("--check", action="store_true", help="배치된 파일만 검증")
    a = ap.parse_args(argv)
    try:
        d = load_style_defaults(a.defaults)
    except StyleDefaultsError as e:
        print(f"[입력 오류] {e}", file=sys.stderr)
        return 2
    try:
        if not a.check:
            if a.zip:
                zip_path = a.zip
                if not zip_path.is_file():
                    print(f"[입력 오류] zip 없음: {zip_path}", file=sys.stderr)
                    return 2
                placed = place_from_zip(d, zip_path)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    zip_path = Path(tmp) / "package.zip"
                    url = d.data.fonts.package.url
                    print(f"내려받는 중: {url}")
                    urllib.request.urlretrieve(url, zip_path)
                    placed = place_from_zip(d, zip_path)
            for p in placed:
                print(f"배치: {p}")
        metas = verify_fonts(d)
    except FontAssetError as e:
        print(f"[검증 실패] {e}", file=sys.stderr)
        return 1
    for face, m in metas.items():
        f = d.data.fonts.faces[face]
        print(f"확인: {face} — {m.family}/{m.subfamily} · usWeightClass {m.weight_class} · {m.outline} · {m.version} · {f.sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
