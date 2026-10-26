"""PNG 저장 공통 함수 — 기본 저장은 Pillow 그대로 · 무압축은 uncompressed() 블록(그 스레드) 안에서만."""
import io
import threading

import numpy as np
import pytest
from PIL import Image

from pipeline.pngio import save_png, uncompressed


def _img() -> Image.Image:
    a = np.full((64, 80, 3), 250, np.uint8)
    a[16:40, 8:60] = (30, 60, 90)
    return Image.fromarray(a)


def _saved(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    save_png(im, buf)
    return buf.getvalue()


def _pillow(im: Image.Image, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG", **kw)
    return buf.getvalue()


def test_default_is_plain_pillow_save():
    im = _img()
    assert _saved(im) == _pillow(im)


def test_default_path_target_is_plain_pillow_save(tmp_path):
    im = _img()
    im.save(tmp_path / "ref.png")
    save_png(im, tmp_path / "got.png")
    assert (tmp_path / "got.png").read_bytes() == (tmp_path / "ref.png").read_bytes()


def test_uncompressed_only_inside_block():
    im = _img()
    with uncompressed():
        raw = _saved(im)
    assert raw == _pillow(im, compress_level=0)
    assert raw != _pillow(im)
    assert _saved(im) == _pillow(im)  # 블록을 나가면 기본 저장으로 돌아온다
    with Image.open(io.BytesIO(raw)) as back:
        assert np.array_equal(np.array(back), np.array(im))  # 무압축도 무손실


def test_uncompressed_restores_after_error():
    im = _img()
    with pytest.raises(RuntimeError):
        with uncompressed():
            raise RuntimeError("boom")
    assert _saved(im) == _pillow(im)


def test_uncompressed_does_not_reach_other_threads():
    im = _img()
    got: list[bytes] = []
    with uncompressed():
        t = threading.Thread(target=lambda: got.append(_saved(im)))
        t.start()
        t.join()
    assert got == [_pillow(im)]
