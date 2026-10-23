"""⑥ 모델 자식 프로세스 테스트용 가짜 모델(spawn 자식이 import한다). 정상 모델 동작이 아니라 시간 초과 · 비정상 종료 재현용이다."""
from __future__ import annotations

import os
import signal
import time

import numpy as np

from pipeline.stages.inpaint import ModelNotAvailable


class FakeProcModel:
    def __init__(self, fill: int = 7, init_sleep: float = 0, init_crash: bool = False, not_available: bool = False,
                 sleep_on: int | None = None, sleep_s: float = 0, crash_on: int | None = None, error_on: int | None = None,
                 ignore_term_on: int | None = None, partial_on: int | None = None):
        if not_available:
            raise ModelNotAvailable("가짜: 가중치 없음")
        if init_crash:
            os._exit(5)
        time.sleep(init_sleep)
        self.fill, self.sleep_on, self.sleep_s = fill, sleep_on, sleep_s
        self.crash_on, self.error_on, self.ignore_term_on, self.partial_on = crash_on, error_on, ignore_term_on, partial_on
        self.calls: list[dict] = []

    def describe(self):
        return {"name": "fake-proc", "pid": os.getpid()}

    def inpaint(self, image, mask):
        n = len(self.calls) + 1
        if n == self.ignore_term_on:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(60)
        if n == self.partial_on:  # 응답 일부만 보내고 멈춤(POSIX Connection 원시 쓰기 — 길이 머리 + 일부 바이트)
            import struct

            from pipeline.stages import inpaint_proc

            inpaint_proc._CHILD_CONN._send(struct.pack("!i", 1_000_000) + b"x" * 16)
            time.sleep(60)
        if n == self.crash_on:
            os._exit(3)
        if n == self.error_on:
            raise ValueError("가짜 추론 오류")
        if n == self.sleep_on:
            time.sleep(self.sleep_s)
        self.calls.append({"shape": list(image.shape[:2]), "mask_px": int((mask > 0).sum())})
        return np.full_like(image, self.fill)
