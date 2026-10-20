"""분석 진행 — pipeline.analyze 진행 이벤트 · app.analysis_progress 저장(가짜 Redis · 실제 Lua)."""
from __future__ import annotations

import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest

from app import analysis_progress as ap
from pipeline import analyze as an
from pipeline.types import SourceImage


# pipeline analyze() 이벤트

def _fake_stages(monkeypatch, region_counts: dict[int, int]):
    monkeypatch.setattr(an.merge, "validate_config", lambda cfg: None)
    monkeypatch.setattr(an.ocr, "validate_config", lambda cfg: None)
    monkeypatch.setattr(an, "split_or_whole", lambda src, cfg, out, **kw: SimpleNamespace(
        sections=[f"s{src.source_image_id}-{i}" for i in range(region_counts[src.source_image_id])]))
    monkeypatch.setattr(an, "_ocr_section", lambda sec, cfg, engine, d: SimpleNamespace(regions=[sec]))
    monkeypatch.setattr(an.merge, "run", lambda sec, ocr_res, cfg, **kw: SimpleNamespace(blocks=[]))
    monkeypatch.setattr(an, "AnalyzeResult", lambda **kw: kw)


def _sources():
    return [SourceImage(source_image_id=20, upload_order=2, path="b.png"),
            SourceImage(source_image_id=10, upload_order=1, path="a.png")]


def test_analyze_emits_events_in_order_with_counters(monkeypatch, tmp_path):
    _fake_stages(monkeypatch, {10: 2, 20: 1})
    events = []
    res = an.analyze(_sources(), {}, tmp_path, use_llm=False, progress=events.append)

    assert [e.event for e in events] == [
        "split_start", "split_done", "ocr_start", "merge_start", "region_done", "ocr_start", "merge_start", "region_done",
        "split_start", "split_done", "ocr_start", "merge_start", "region_done",
    ]
    got = [(e.source_index, e.source_image_id, e.region_index, e.region_count_in_source,
            e.regions_done, e.regions_known, e.sources_split) for e in events]
    assert got == [
        (1, 10, None, None, 0, 0, 0),
        (1, 10, None, 2, 0, 2, 1),
        (1, 10, 1, 2, 0, 2, 1),
        (1, 10, 1, 2, 0, 2, 1),
        (1, 10, 1, 2, 1, 2, 1),
        (1, 10, 2, 2, 1, 2, 1),
        (1, 10, 2, 2, 1, 2, 1),
        (1, 10, 2, 2, 2, 2, 1),
        (2, 20, None, None, 2, 2, 1),
        (2, 20, None, 1, 2, 3, 2),
        (2, 20, 1, 1, 2, 3, 2),
        (2, 20, 1, 1, 2, 3, 2),
        (2, 20, 1, 1, 3, 3, 2),
    ]
    assert all(e.source_total == 2 for e in events)
    assert res["sections"] == ["s10-0", "s10-1", "s20-0"]


def test_analyze_result_unchanged_when_callback_raises(monkeypatch, tmp_path):
    _fake_stages(monkeypatch, {10: 2, 20: 1})
    base = an.analyze(_sources(), {}, tmp_path, use_llm=False)
    calls = []

    def boom(p):
        calls.append(p.event)
        raise RuntimeError("callback broken")

    res = an.analyze(_sources(), {}, tmp_path, use_llm=False, progress=boom)
    assert res == base
    assert len(calls) == 13


def test_emit_swallows_callback_exception_and_skips_none():
    kw = dict(event="split_start", source_index=1, source_total=1, source_image_id=1, region_index=None,
              region_count_in_source=None, regions_done=0, regions_known=0, sources_split=0)
    an._emit(None, **kw)

    def boom(p):
        raise ValueError("x")

    an._emit(boom, **kw)


# ProgressReporter · read — 가짜 Redis

class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.evals: list[str] = []

    def eval(self, script, numkeys, *args):
        assert numkeys == 1
        k = args[0]
        cur = self.store.get(k)
        if script == ap.SAVE_LUA:
            self.evals.append("save")
            epoch, seq, payload, px = int(args[1]), int(args[2]), args[3], int(args[4])
            if cur is not None:
                old = json.loads(cur)
                if old["epoch"] > epoch or (old["epoch"] == epoch and old["seq"] >= seq):
                    return 0
            self.store[k] = payload
            self.ttl[k] = px
            return 1
        if script == ap.TOUCH_LUA:
            self.evals.append("touch")
            if cur is None or json.loads(cur)["epoch"] != int(args[1]):
                return 0
            self.ttl[k] = int(args[2])
            return 1
        raise AssertionError("unknown script")

    def get(self, k):
        return self.store.get(k)


class BrokenRedis:
    def eval(self, *a, **k):
        raise ConnectionError("down")

    def get(self, *a, **k):
        raise ConnectionError("down")


def _progress(event="region_done", done=1):
    return an.AnalyzeProgress(event=event, source_index=1, source_total=1, source_image_id=7, region_index=1,
                              region_count_in_source=2, regions_done=done, regions_known=2, sources_split=1)


def test_on_progress_saved_on_exit_and_read_back():
    fake = FakeRedis()
    with ap.ProgressReporter("a1", 3, 1, client=fake, ttl_s=10, touch_s=0.1) as rep:
        rep.on_progress(_progress(done=1))
        rep.on_progress(_progress(done=2))
    got = ap.read("a1", client=fake)
    assert got["epoch"] == 3 and got["seq"] == 2 and got["event"] == "region_done"
    assert got["snap"]["regions_done"] == 2 and got["snap"]["source_image_id"] == 7
    assert fake.ttl[ap.key("a1")] == 10000


def test_mark_analyze_done_saves_immediately():
    fake = FakeRedis()
    with ap.ProgressReporter("a2", 1, 2, client=fake, ttl_s=10, touch_s=0.1) as rep:
        rep.on_progress(_progress())
        rep.mark_analyze_done(5)
        got = ap.read("a2", client=fake)
        assert got["event"] == "analyze_done"
        assert got["snap"] == {"regions_done": 5, "regions_known": 5, "sources_split": 2, "source_total": 2}


def test_stale_epoch_writer_is_rejected():
    fake = FakeRedis()
    with ap.ProgressReporter("a3", 5, 1, client=fake, ttl_s=10) as rep:
        rep.mark_analyze_done(1)
    with ap.ProgressReporter("a3", 4, 1, client=fake, ttl_s=10) as old:
        old.on_progress(_progress())
    assert ap.read("a3", client=fake)["epoch"] == 5


def test_broken_client_never_raises():
    with ap.ProgressReporter("a4", 1, 1, client=BrokenRedis(), ttl_s=10, touch_s=0.1) as rep:
        rep.on_progress(_progress())
        rep.mark_analyze_done(1)
    assert ap.read("a4", client=BrokenRedis()) is None


def test_read_missing_key_is_none():
    assert ap.read("nope", client=FakeRedis()) is None


def test_thread_stops_when_heartbeat_lost():
    fake = FakeRedis()
    hb = SimpleNamespace(lost=True)
    rep = ap.ProgressReporter("a5", 1, 1, hb=hb, client=fake, ttl_s=10, touch_s=0.1)
    with rep:
        rep.on_progress(_progress())
        deadline = time.monotonic() + 1.8
        while rep._thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not rep._thread.is_alive()
        assert fake.evals == []


def test_no_save_on_exit_or_analyze_done_when_heartbeat_lost():
    fake = FakeRedis()
    hb = SimpleNamespace(lost=False)
    with ap.ProgressReporter("a6", 1, 1, hb=hb, client=fake, ttl_s=10, touch_s=60) as rep:
        rep.on_progress(_progress())
        hb.lost = True
        rep.mark_analyze_done(1)
    assert fake.evals == []
    assert ap.read("a6", client=fake) is None


# 실제 Lua — PIXLATE_TEST_REDIS_URL 이 있을 때만

@pytest.fixture
def real_redis():
    url = os.getenv("PIXLATE_TEST_REDIS_URL")
    if not url:
        pytest.skip("PIXLATE_TEST_REDIS_URL 없음")
    import redis
    c = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
    try:
        c.ping()
    except Exception as e:
        pytest.skip(f"Redis 연결 실패: {e.__class__.__name__}")
    k = f"pixlate:test-progress:{uuid.uuid4().hex}"
    yield c, k
    c.delete(k)


def _save(c, k, epoch, seq):
    payload = json.dumps({"epoch": epoch, "seq": seq, "event": "x", "snap": {}})
    return c.eval(ap.SAVE_LUA, 1, k, epoch, seq, payload, 60000)


def test_real_save_lua_rules(real_redis):
    c, k = real_redis
    assert _save(c, k, 2, 5) == 1
    assert _save(c, k, 2, 5) == 0
    assert _save(c, k, 2, 4) == 0
    assert _save(c, k, 1, 99) == 0
    assert json.loads(c.get(k))["seq"] == 5
    assert _save(c, k, 3, 1) == 1
    assert json.loads(c.get(k))["epoch"] == 3
    assert 0 < c.pttl(k) <= 60000


def test_real_touch_lua_rules(real_redis):
    c, k = real_redis
    assert c.eval(ap.TOUCH_LUA, 1, k, 1, 60000) == 0
    _save(c, k, 2, 1)
    c.pexpire(k, 5000)
    assert c.eval(ap.TOUCH_LUA, 1, k, 1, 60000) == 0
    assert c.pttl(k) <= 5000
    assert c.eval(ap.TOUCH_LUA, 1, k, 2, 60000) == 1
    assert c.pttl(k) > 5000
