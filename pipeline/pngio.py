"""PNG 저장 공통 함수 — 예제 생성에서만 무압축으로 쓸 수 있게 저장 지점을 한곳에 모은다.

- save_png()는 기본적으로 Pillow 기본 옵션 그대로 쓴다(`im.save(fp, format="PNG")`와 바이트까지 같다).
- uncompressed() 안에서만 compress_level=0으로 쓴다. 쓰는 곳은 계약 예제 생성(`pipeline.handoff.examples.generate`)뿐이다.
  같은 픽셀이라도 압축 출력이 OS마다 달라 예제 JSON의 파일 sha256이 Windows·Linux에서 어긋났다(PR #85 CI).
  무압축은 OS와 무관하게 같은 바이트가 된다. 파일은 커지지만(이번 예제 합계 약 460배) 예제 작업 폴더에만 생긴다.
- Image.save 자체는 바꾸지 않는다. 설정은 ContextVar라 uncompressed()를 연 스레드(그 컨텍스트)에만 적용된다.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import IO

from PIL import Image

_COMPRESS_LEVEL: ContextVar[int | None] = ContextVar("pixlate_png_compress_level", default=None)


def save_png(im: Image.Image, fp: str | Path | IO[bytes]) -> None:
    """im을 PNG로 쓴다. uncompressed() 밖에서는 `im.save(fp, format="PNG")`와 같다."""
    level = _COMPRESS_LEVEL.get()
    if level is None:
        im.save(fp, format="PNG")
    else:
        im.save(fp, format="PNG", compress_level=level)


@contextmanager
def uncompressed() -> Iterator[None]:
    """이 블록 안의 save_png()만 무압축(compress_level=0)으로 쓴다. 나가면 이전 설정으로 돌아간다."""
    token = _COMPRESS_LEVEL.set(0)
    try:
        yield
    finally:
        _COMPRESS_LEVEL.reset(token)
