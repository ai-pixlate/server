"""⑥ 인페인팅 모델 전용 자식 프로세스 — 시간 제한 · 강제 중단(2026-09-30, open-questions.md #75 · #72, pipeline.md 7.6절).

같은 프로세스에서는 진행 중인 CUDA 호출을 끊을 수 없으므로 모델을 **자식 프로세스 1개**에 두고 부모가 시간 제한 · 결과 저장을 맡는다.
- 시작: `multiprocessing` **spawn** — 부모의 CUDA 상태를 물려받지 않는다(부모는 torch를 import하지 않는다).
- 초기화: 자식이 `factory`(모듈:이름)를 불러 모델을 만들고 `ready`(describe 포함)를 보낸다. `init_timeout_s`(시작 ~ 응답 전체 수신)
  안에 오지 않으면 자식만 종료한다. 초기화 실패 예외에는 자식 종료 기록(`termination`)을 담아 실행 기록까지 전달한다.
- 추론: 섹션마다 요청 1회. **요청 전송 시작부터 응답 전체 수신까지 하나의 마감 시간**(`infer_timeout_s`)을 둔다 — 전송 · 수신은 감시용
  보조 스레드에서 하고 부모는 마감까지만 기다리므로, 자식이 요청을 읽지 않거나(파이프가 가득 참) 응답을 일부만 보내고 멈춰도 부모가 함께
  막히지 않는다. 마감을 넘기면 자식만 종료한다(자식이 끝나면 막힌 전송 · 수신도 오류로 풀린다). 모델을 섹션마다 다시 만들지 않는다.
- 종료: `terminate` → `kill_grace_s` 기다림 → 살아 있으면 `kill` → `join`. 강제 종료한 뒤 통신 채널은 다시 쓰지 않는다(이 객체는 닫힘).
- 자동 재시도 · 재기동 · CPU 대체 없음. 실패는 예외로 올리고 실행 계층(run.execute_inpaint)이 기록 후 실행을 멈춘다:
  초기화 — 모델 사용 불가는 `ModelNotAvailable`(종료 코드 3), 시간 초과 · 비정상 종료 · 그 밖의 오류는 `InpaintProcessError`(4).
  추론 — 시간 초과 · 비정상 종료 · 자식 쪽 오류는 `InpaintProcessError` → `inpaint.apply`가 `InpaintModelError`로 감싸 섹션 failed.
시간 제한 값은 config `[inpaint]` `init_timeout_s` · `infer_timeout_s` · `kill_grace_s`(100섹션 실측용 잠정값, 운영값 아님)에서 받는다.
"""
from __future__ import annotations

import importlib
import multiprocessing as mp
import threading
import time
import traceback
from typing import Any

import numpy as np

from pipeline.stages.inpaint import ModelNotAvailable


class InpaintProcessError(RuntimeError):
    """자식 프로세스의 시간 초과 · 비정상 종료 · 오류. kind = init_timeout · infer_timeout · child_exit · child_error."""

    def __init__(self, kind: str, message: str):
        super().__init__(f"[{kind}] {message}")
        self.kind = kind


def _resolve(factory: str):
    module, _, name = factory.partition(":")
    return getattr(importlib.import_module(module), name)


_CHILD_CONN = None  # 자식 안에서만 채워진다 — 통신 정지 재현 테스트(tests/inpaint_fakes.py)가 원시 쓰기에 쓴다


def _child_main(conn, factory: str, kwargs: dict[str, Any]) -> None:
    """자식 프로세스 본체. 메시지: ("infer", image, mask) → ("ok", out, stats) | ("error", 메시지) / ("close",) → 종료."""
    global _CHILD_CONN
    _CHILD_CONN = conn
    try:
        model = _resolve(factory)(**kwargs)
        conn.send(("ready", model.describe()))
    except ModelNotAvailable as e:
        conn.send(("not_available", str(e)))
        return
    except BaseException as e:  # noqa: BLE001 — 초기화 오류를 부모에게 그대로 전달
        conn.send(("error", f"{e.__class__.__name__}: {e}", traceback.format_exc(limit=5)))
        return
    n = 0
    while True:
        try:
            msg = conn.recv()
        except EOFError:  # 부모가 사라졌다
            return
        if msg[0] == "close":
            return
        if msg[0] == "infer":
            n += 1
            t0 = time.perf_counter()
            try:
                out = model.inpaint(msg[1], msg[2])
                calls = getattr(model, "calls", None)
                stats = dict(calls[-1]) if calls else {}
                stats.update({"n": n, "child_total_s": round(time.perf_counter() - t0, 4)})
                conn.send(("ok", out, stats))
            except BaseException as e:  # noqa: BLE001 — 추론 오류: 부모가 섹션 실패로 기록
                conn.send(("error", f"{e.__class__.__name__}: {e}", traceback.format_exc(limit=5)))


class SubprocessInpaintModel:
    """`InpaintModel` 구현 — 실제 모델은 자식 프로세스에서 돈다. 생성 = 자식 시작 + 초기화 대기."""

    def __init__(self, factory: str, *, init_timeout_s: float, infer_timeout_s: float, kill_grace_s: float,
                 factory_kwargs: dict[str, Any] | None = None):
        self.factory = factory
        self.timeouts = {"init_timeout_s": init_timeout_s, "infer_timeout_s": infer_timeout_s, "kill_grace_s": kill_grace_s}
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.termination: dict[str, Any] | None = None
        ctx = mp.get_context("spawn")
        self._conn, child_conn = ctx.Pipe(duplex=True)
        t0 = time.perf_counter()
        self._proc = ctx.Process(target=_child_main, args=(child_conn, factory, factory_kwargs or {}), daemon=True,
                                 name="pixlate-inpaint-model")
        self._proc.start()
        child_conn.close()
        try:
            msg = self._exchange(None, init_timeout_s, "init_timeout", "모델 초기화")
        except InpaintProcessError as e:
            e.termination = self.termination
            raise
        self.init_wall_s = round(time.perf_counter() - t0, 3)
        if msg[0] == "ready":
            self._describe = msg[1]
            return
        self._stop(f"초기화 응답 {msg[0]}")
        err: Exception = (ModelNotAvailable(msg[1]) if msg[0] == "not_available"
                          else InpaintProcessError("child_error", f"모델 초기화 실패: {msg[1]}"))
        err.termination = self.termination  # 초기화 실패도 종료 기록을 실행 기록으로 넘긴다
        raise err

    # -- 통신 ---------------------------------------------------------------
    def _exchange(self, payload, timeout: float, kind: str, what: str):
        """payload를 보내고(None이면 보내지 않음) 응답 하나를 **전체** 받는다 — 전송 시작부터 수신 완료까지 마감 `timeout`초.
        전송 · 수신은 보조 스레드에서 하고 이 스레드는 마감까지만 기다린다. 마감을 넘기면 자식을 끝내고(막힌 전송 · 수신은 자식 종료로
        오류가 되어 풀린다) `kind`로, 그 전에 자식이 끝나 통신이 실패하면 `child_exit`로 올린다."""
        box: dict[str, Any] = {}

        def io() -> None:
            try:
                if payload is not None:
                    self._conn.send(payload)
                box["msg"] = self._conn.recv()
            except BaseException as e:  # noqa: BLE001 — 통신 오류는 주 스레드가 분류한다
                box["err"] = e

        t = threading.Thread(target=io, name="pixlate-inpaint-io", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():  # 마감 초과 — 전송 · 계산 · 수신 어느 단계든
            alive = self._proc.is_alive()
            self._stop(f"{what} 시간 초과" if alive else f"{what} 중 자식 종료")
            t.join(self.timeouts["kill_grace_s"] + 1)  # 자식이 끝나면 통신이 오류로 풀린다
            if self.termination is not None:
                self.termination["io_thread_alive"] = t.is_alive()
            if alive:
                raise InpaintProcessError(kind, f"{what} 시간 제한 {timeout}초 초과(전송 ~ 수신) — 자식 프로세스를 종료했다")
            raise InpaintProcessError("child_exit", f"{what} 중 자식 프로세스가 끝났다(exitcode={self._proc.exitcode})")
        if "err" in box:
            self._stop(f"{what} 중 자식 종료")
            raise InpaintProcessError("child_exit", f"{what} 중 통신 실패 {box['err'].__class__.__name__}: {box['err']} "
                                                    f"(exitcode={self._proc.exitcode})")
        return box["msg"]

    def _stop(self, reason: str) -> None:
        """자식을 끝내고 채널을 닫는다. 이미 끝났으면 기록만 한다."""
        if self.closed:
            return
        self.closed = True
        grace = self.timeouts["kill_grace_s"]
        killed = False
        t0 = time.perf_counter()
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(grace)
            if self._proc.is_alive():
                self._proc.kill()
                killed = True
                self._proc.join()
        else:
            self._proc.join(0)
        try:
            self._conn.close()
        except OSError:
            pass
        self.termination = {"reason": reason, "exitcode": self._proc.exitcode, "killed": killed,
                            "stop_s": round(time.perf_counter() - t0, 3)}

    # -- InpaintModel --------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {**self._describe, "process": {"start_method": "spawn", "factory": self.factory, **self.timeouts,
                                              "init_wall_s": self.init_wall_s}}

    def inpaint(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if self.closed:
            raise InpaintProcessError("child_exit", "모델 프로세스가 이미 종료됐다 — 채널을 다시 쓰지 않는다")
        t0 = time.perf_counter()
        try:
            msg = self._exchange(("infer", image, mask), self.timeouts["infer_timeout_s"], "infer_timeout", "섹션 추론")
        except InpaintProcessError as e:
            self.calls.append({"n": len(self.calls) + 1, "error": str(e), "kind": e.kind,
                               "roundtrip_s": round(time.perf_counter() - t0, 4), "termination": self.termination})
            raise
        rt = round(time.perf_counter() - t0, 4)
        if msg[0] == "ok":
            self.calls.append({**msg[2], "roundtrip_s": rt})
            return msg[1]
        self.calls.append({"n": len(self.calls) + 1, "error": msg[1], "roundtrip_s": rt})
        raise InpaintProcessError("child_error", f"추론 실패: {msg[1]}")

    def close(self) -> dict[str, Any] | None:
        """정상 종료 요청 → kill_grace_s 안에 끝나지 않으면 강제 종료. 종료 기록을 돌려준다."""
        if not self.closed:
            grace = self.timeouts["kill_grace_s"]

            def send_close() -> None:
                try:
                    self._conn.send(("close",))
                except (OSError, EOFError, ValueError):
                    pass

            t = threading.Thread(target=send_close, daemon=True)  # 자식이 읽지 않아 전송이 막혀도 기다리지 않는다
            t.start()
            t.join(grace)
            self._proc.join(grace)
            self._stop("정상 종료" if not self._proc.is_alive() else "정상 종료 요청 무응답")
        return self.termination
