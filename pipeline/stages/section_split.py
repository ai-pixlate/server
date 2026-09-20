"""① 섹션 분해 — `color_snap_vlm2` [pipeline.md 단계표 ①].

배경색 전환으로 자르고, 색이 안 바뀌는 긴 구간만 VLM이 경계를 고른다. 입도는 소제목 단위.
원본 1장 단위로 실행하며, 섹션 이미지를 out_dir에 잘라 저장하고 SplitResult를 돌려준다.

미구현. VLM 모델명은 open-questions #21 미정이라 config에 키가 없다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from pipeline.errors import image_open_failed
from pipeline.types import Section, SourceImage, SplitResult, section_key


def open_source(src: SourceImage) -> Image.Image:
    """원본을 연다. 깨진 파일·미지원 포맷은 IMAGE_OPEN_FAILED(재시도 불가)."""
    try:
        im = Image.open(src.path)
        im.load()
        return im
    except Exception as e:  # noqa: BLE001 — Pillow 예외 종류가 다양하다
        raise image_open_failed(src.path, src.source_image_id, e) from e


def crop_sections(src: SourceImage, im: Image.Image, boundaries: list[int], out_dir: Path) -> SplitResult:
    """세로 경계 목록(오름차순, 0과 height 제외)으로 섹션 이미지를 잘라 저장한다. 경계 결정 알고리즘과 분리된 공통 부분."""
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = im.size
    edges = [0, *boundaries, h]
    sections: list[Section] = []
    for i in range(len(edges) - 1):
        top, bottom = edges[i], edges[i + 1]
        if bottom <= top:
            raise ValueError(f"경계가 오름차순이 아니다: {edges}")
        key = section_key(src.source_image_id, i + 1)
        path = out_dir / f"{key}.png"
        im.crop((0, top, w, bottom)).save(path)
        sections.append(
            Section(
                section_key=key,
                source_image_id=src.source_image_id,
                section_order=i + 1,
                top_offset=top,
                height=bottom - top,
                width=w,
                image_path=str(path),
            )
        )
    return SplitResult(source_image_id=src.source_image_id, source_width=w, source_height=h, sections=sections)


def run(src: SourceImage, cfg: dict[str, Any], out_dir: Path) -> SplitResult:
    im = open_source(src)
    raise NotImplementedError(
        "① 섹션 분해(color_snap_vlm2) 미구현 — docs/ai/status.md. 경계 결정 후 crop_sections()로 저장한다."
    )
