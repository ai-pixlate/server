"""⑥ LaMa 어댑터 — torch 없이 도는 전처리 · 후처리 · 초기화 거부 경로(손으로 정한 기대값). 실제 추론은 GPU 서버에서 따로 검증한다."""
from __future__ import annotations

import sys

import numpy as np
import pytest

from pipeline.stages import inpaint_lama as lama
from pipeline.stages.inpaint import ModelNotAvailable


def test_ceil_modulo():
    assert [lama.ceil_modulo(x, 8) for x in (1, 7, 8, 9, 16, 3470)] == [8, 8, 8, 16, 16, 3472]


def test_pad_symmetric_bottom_right_only():
    arr = np.arange(3, dtype=np.uint8).reshape(3, 1)  # 행 0, 1, 2 · 열 1개
    got = lama.pad_to_modulo(arr, 8)
    assert got.shape == (8, 8, 1)
    # symmetric: 끝 값을 한 번 더 쓰고 거꾸로 — 0 1 2 | 2 1 0 0 1
    assert got[:, 0, 0].tolist() == [0, 1, 2, 2, 1, 0, 0, 1]
    assert (got[:, :, 0] == got[:, :1, 0]).all()  # 열 방향은 같은 값 반복
    assert (got[:3, :1, 0] == arr).all()  # 원본은 왼쪽 위 그대로


def test_pad_noop_when_multiple():
    arr = np.zeros((16, 8, 3), dtype=np.uint8)
    assert lama.pad_to_modulo(arr).shape == (16, 8, 3)


def test_preprocess_shapes_scale_and_mask_threshold():
    img = np.zeros((3, 5, 3), dtype=np.uint8)
    img[0, 0] = (255, 0, 51)
    mask = np.zeros((3, 5), dtype=np.uint8)
    mask[1, 1] = 1  # 0이 아니면 삭제(iopaint mask/255 > 0)
    mask[2, 4] = 255
    x, m = lama.preprocess(img, mask)
    assert x.shape == (1, 3, 8, 8) and x.dtype == np.float32
    assert m.shape == (1, 1, 8, 8) and m.dtype == np.int64
    assert x[0, :, 0, 0].tolist() == pytest.approx([1.0, 0.0, 0.2])  # RGB 순서 유지
    assert m[0, 0, 1, 1] == 1 and m[0, 0, 2, 4] == 1 and m[0, 0, 0, 0] == 0
    assert m[0, 0, 3, 4] == 1  # symmetric 패딩으로 마지막 행(2)이 패딩 첫 행(3)에 반사된다


def test_postprocess_truncates_clips_and_crops():
    out = np.zeros((3, 8, 8), dtype=np.float32)
    out[:, 0, 0] = (-0.1, 1.2, 0.5)  # → 0, 255, 127(127.5 버림)
    out[:, 7, 7] = 1.0  # 잘려 나갈 패딩 영역
    got = lama.postprocess(out, 3, 5)
    assert got.shape == (3, 5, 3) and got.dtype == np.uint8 and got.flags["C_CONTIGUOUS"]
    assert got[0, 0].tolist() == [0, 255, 127]


def test_roundtrip_identity_model_restores_original_size():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, size=(13, 21, 3), dtype=np.uint8)
    x, _ = lama.preprocess(img, np.zeros((13, 21), dtype=np.uint8))
    back = lama.postprocess(x[0], 13, 21)  # 입력을 그대로 돌려주는 가상 출력
    assert (back == img).all()  # /255 → ×255 버림이 원래 정수를 복원하는지(색 순서 · 패딩 제거 확인)


def test_model_without_torch_is_not_available(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(ModelNotAvailable, match="torch"):
        lama.LamaModel()


def test_build_model_uses_lama_adapter_in_child_process(monkeypatch, tmp_path):
    # 모델은 spawn 자식에서 초기화된다 — 자식은 부모의 sys.modules를 물려받지 않으므로 TORCH_HOME을 빈 폴더로 돌려
    # torch가 있는 GPU 환경에서도 '가중치 없음'(없으면 'torch 없음') → ModelNotAvailable이 되게 한다
    from pipeline import config as cfgmod
    from pipeline.stages import inpaint

    monkeypatch.setenv("TORCH_HOME", str(tmp_path / "empty_torch_home"))
    with pytest.raises(ModelNotAvailable):
        inpaint.build_model(cfgmod.load_config())


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_postprocess_rejects_non_finite(bad):
    out = np.zeros((3, 8, 8), dtype=np.float32)
    out[1, 2, 2] = bad
    with pytest.raises(ValueError, match="NaN"):
        lama.postprocess(out, 3, 5)


@pytest.mark.parametrize("shape", [(3, 8, 16), (1, 8, 8), (3, 3, 5)])
def test_postprocess_rejects_wrong_shape(shape):
    with pytest.raises(ValueError, match="모양"):
        lama.postprocess(np.zeros(shape, dtype=np.float32), 3, 5)


def test_postprocess_rejects_integer_output():
    with pytest.raises(ValueError, match="dtype"):
        lama.postprocess(np.zeros((3, 8, 8), dtype=np.uint8), 3, 5)
