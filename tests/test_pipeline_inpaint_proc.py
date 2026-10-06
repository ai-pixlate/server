"""⑥ 모델 전용 자식 프로세스(spawn) — 시간 제한 · 강제 중단 · 비정상 종료 (가짜 모델, 실제 LaMa 없음)."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pytest

from pipeline import config as cfgmod
from pipeline import jsonio
from pipeline import run as runmod
from pipeline.stages.inpaint import InpaintInputError, ModelNotAvailable, validate_config
from pipeline.stages.inpaint_proc import InpaintProcessError, SubprocessInpaintModel
from pipeline.types import InpaintResult
from tests.test_pipeline_inpaint_run import job

FAKE = "tests.inpaint_fakes:FakeProcModel"
T = {"init_timeout_s": 20, "infer_timeout_s": 10, "kill_grace_s": 2}


def img_mask():
    img = np.zeros((10, 12, 3), dtype=np.uint8)
    mask = np.zeros((10, 12), dtype=np.uint8)
    mask[2:4, 2:6] = 255
    return img, mask


def no_children() -> bool:
    for p in mp.active_children():  # active_children는 끝난 자식을 거둔다
        p.join(1)
    return mp.active_children() == []


def test_normal_roundtrip_and_close():
    m = SubprocessInpaintModel(FAKE, **T)
    try:
        out = m.inpaint(*img_mask())
        assert out.shape == (10, 12, 3) and (out == 7).all()
        m.inpaint(*img_mask())
        assert [c["n"] for c in m.calls] == [1, 2] and all("roundtrip_s" in c for c in m.calls)
        d = m.describe()
        assert d["name"] == "fake-proc" and d["process"]["start_method"] == "spawn" and d["process"]["infer_timeout_s"] == 10
    finally:
        term = m.close()
    assert term["reason"] == "정상 종료" and term["exitcode"] == 0 and term["killed"] is False
    assert no_children()


def test_init_timeout_stops_child():
    t0 = time.perf_counter()
    with pytest.raises(InpaintProcessError) as ei:
        SubprocessInpaintModel(FAKE, init_timeout_s=1, infer_timeout_s=10, kill_grace_s=1, factory_kwargs={"init_sleep": 30})
    assert ei.value.kind == "init_timeout"
    assert time.perf_counter() - t0 < 20
    assert ei.value.termination["reason"] == "모델 초기화 시간 초과"  # 초기화 실패에도 종료 기록
    assert no_children()


def test_init_not_available_is_model_not_available():
    with pytest.raises(ModelNotAvailable, match="가중치 없음") as ei:
        SubprocessInpaintModel(FAKE, **T, factory_kwargs={"not_available": True})
    assert ei.value.termination["reason"].startswith("초기화 응답")
    assert no_children()


def test_init_crash_is_child_exit():
    with pytest.raises(InpaintProcessError) as ei:
        SubprocessInpaintModel(FAKE, **T, factory_kwargs={"init_crash": True})
    assert ei.value.kind == "child_exit" and ei.value.termination is not None
    assert no_children()


def test_infer_timeout_kills_child_and_channel_not_reused():
    m = SubprocessInpaintModel(FAKE, init_timeout_s=20, infer_timeout_s=1, kill_grace_s=1, factory_kwargs={"sleep_on": 2, "sleep_s": 30})
    m.inpaint(*img_mask())
    with pytest.raises(InpaintProcessError) as ei:
        m.inpaint(*img_mask())
    assert ei.value.kind == "infer_timeout"
    assert m.closed and m.termination["reason"].endswith("시간 초과")
    assert m.calls[-1]["kind"] == "infer_timeout" and m.calls[-1]["n"] == 2
    assert no_children()
    with pytest.raises(InpaintProcessError) as ei2:
        m.inpaint(*img_mask())
    assert ei2.value.kind == "child_exit"


def test_child_crash_during_infer():
    m = SubprocessInpaintModel(FAKE, **T, factory_kwargs={"crash_on": 1})
    with pytest.raises(InpaintProcessError) as ei:
        m.inpaint(*img_mask())
    assert ei.value.kind == "child_exit" and m.closed
    assert no_children()


def test_child_error_keeps_process_until_close():
    m = SubprocessInpaintModel(FAKE, **T, factory_kwargs={"error_on": 1})
    with pytest.raises(InpaintProcessError, match="가짜 추론 오류") as ei:
        m.inpaint(*img_mask())
    assert ei.value.kind == "child_error" and not m.closed
    assert m.close()["exitcode"] == 0
    assert no_children()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows terminate는 즉시 강제 종료라 SIGTERM 무시 → kill 단계를 재현할 수 없다")
def test_terminate_ignored_escalates_to_kill():
    m = SubprocessInpaintModel(FAKE, init_timeout_s=20, infer_timeout_s=1, kill_grace_s=1, factory_kwargs={"ignore_term_on": 1})
    with pytest.raises(InpaintProcessError):
        m.inpaint(*img_mask())
    assert m.termination["killed"] is True
    assert no_children()


POSIX = pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP · 원시 파이프 쓰기는 POSIX에서만 재현한다(서버 Linux에서 실행)")


def _cont_later(pid: int, after: float) -> threading.Timer:
    def cont():
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass

    t = threading.Timer(after, cont)
    t.start()
    return t


@POSIX
@pytest.mark.parametrize("size", [(10, 12), (2000, 1000)])  # 작은 요청(수신 대기) · 파이프를 채우는 큰 요청(전송 막힘)
def test_deadline_covers_send_when_child_stops_reading(size):
    m = SubprocessInpaintModel(FAKE, init_timeout_s=20, infer_timeout_s=0.5, kill_grace_s=1)
    pid = m.describe()["pid"]
    os.kill(pid, signal.SIGSTOP)  # 요청을 읽지 못하게 정지 — 5초 뒤 다시 깨우는 타이머(그 전에 끝나야 한다)
    timer = _cont_later(pid, 5)
    img = np.zeros((*size, 3), dtype=np.uint8)
    mask = np.zeros(size, dtype=np.uint8)
    mask[0, 0] = 255
    t0 = time.perf_counter()
    try:
        with pytest.raises(InpaintProcessError) as ei:
            m.inpaint(img, mask)
    finally:
        timer.cancel()
    elapsed = time.perf_counter() - t0
    assert ei.value.kind == "infer_timeout"
    assert elapsed < 4, elapsed  # 마감 0.5 s + 종료 유예 1 s + kill — 5초 뒤 재개를 기다려 성공하지 않는다
    assert m.termination["killed"] is True  # 정지한 프로세스는 SIGTERM을 처리하지 못해 kill로 끝난다
    assert m.calls[-1]["kind"] == "infer_timeout"
    assert no_children()


@POSIX
def test_deadline_covers_partial_response():
    m = SubprocessInpaintModel(FAKE, init_timeout_s=20, infer_timeout_s=1, kill_grace_s=1, factory_kwargs={"partial_on": 1})
    t0 = time.perf_counter()
    with pytest.raises(InpaintProcessError) as ei:
        m.inpaint(*img_mask())
    assert ei.value.kind == "infer_timeout" and time.perf_counter() - t0 < 5
    assert m.termination["io_thread_alive"] is False  # 막혀 있던 수신이 자식 종료로 풀렸다
    assert no_children()


def test_execute_init_failure_records_termination(tmp_path):
    cfg = cfgmod.load_config()

    def factory(c):
        return SubprocessInpaintModel(FAKE, init_timeout_s=1, infer_timeout_s=10, kill_grace_s=1, factory_kwargs={"init_sleep": 30})

    started = datetime.now(timezone.utc)
    code = runmod.execute_inpaint(tmp_path / "out", [job(tmp_path, "IMG-01")], cfg, "inpaint",
                                  runmod.inpaint_run_base("inpaint", cfg, started), started, model_factory=factory)
    rec = json.loads((tmp_path / "out/run.json").read_text(encoding="utf-8"))
    assert code == 4 and [s["status"] for s in rec["sections"]] == ["not_run"]
    assert rec["model_termination"]["reason"] == "모델 초기화 시간 초과"
    assert no_children()


def test_timeout_config_validation():
    cfg = cfgmod.load_config()
    assert {k: cfg["inpaint"][k] for k in ("init_timeout_s", "infer_timeout_s", "kill_grace_s")} == {
        "init_timeout_s": 60, "infer_timeout_s": 30, "kill_grace_s": 5}
    for bad in (0, -1, True, "30", float("inf")):
        c = cfgmod.load_config()
        c["inpaint"]["infer_timeout_s"] = bad
        with pytest.raises(InpaintInputError, match="infer_timeout_s"):
            validate_config(c)


def test_execute_infer_timeout_marks_failed_and_not_run(tmp_path):
    cfg = cfgmod.load_config()
    jobs = [job(tmp_path, f"IMG-0{i}") for i in (1, 2, 3)]
    made = []

    def factory(c):
        made.append(SubprocessInpaintModel(FAKE, init_timeout_s=20, infer_timeout_s=1, kill_grace_s=1,
                                           factory_kwargs={"sleep_on": 2, "sleep_s": 30}))
        return made[-1]

    started = datetime.now(timezone.utc)
    code = runmod.execute_inpaint(tmp_path / "out", jobs, cfg, "inpaint", runmod.inpaint_run_base("inpaint", cfg, started), started,
                                  model_factory=factory)
    rec = json.loads((tmp_path / "out/run.json").read_text(encoding="utf-8"))
    assert code == 4 and rec["status"] == "failed" and len(made) == 1
    assert [s["status"] for s in rec["sections"]] == ["ok", "failed", "not_run"]
    assert rec["model_termination"]["reason"].endswith("시간 초과")
    res = jsonio.load_model(tmp_path / "out/IMG-02/inpaint/sec_1_01.json", InpaintResult)
    assert res.status == "failed" and "infer_timeout" in res.error and res.files.final_mask  # 마스크는 보존
    dbg = json.loads((tmp_path / "out/IMG-02/inpaint_debug/sec_1_01.json").read_text(encoding="utf-8"))
    assert dbg["model_call"]["kind"] == "infer_timeout" and "killed" in dbg["model_termination"]
    assert jsonio.load_model(tmp_path / "out/IMG-01/inpaint/sec_1_01.json", InpaintResult).status == "inpainted"
    assert rec["timeout_s"] == {"init_timeout_s": 60, "infer_timeout_s": 30, "kill_grace_s": 5}  # 기록은 config 값(가짜는 주입 값 사용)
    assert no_children()


def test_execute_closes_model_on_success(tmp_path):
    cfg = cfgmod.load_config()
    holder = []

    def factory(c):
        holder.append(SubprocessInpaintModel(FAKE, **T))
        return holder[0]

    started = datetime.now(timezone.utc)
    code = runmod.execute_inpaint(tmp_path / "out", [job(tmp_path, "IMG-01")], cfg, "inpaint",
                                  runmod.inpaint_run_base("inpaint", cfg, started), started, model_factory=factory)
    assert code == 0 and holder[0].closed and holder[0].termination["exitcode"] == 0
    assert no_children()
