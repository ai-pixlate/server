"""분석(①②③) 진행 상황 — 시도(attempt)별 최신 스냅샷 하나를 Redis 에 둔다.

쓰기는 ProgressReporter 의 데몬 스레드 한 곳에서만 한다(analyze() 콜백은 메모리만 바꾼다).
같은 키에 이전 lease epoch 의 쓰기가 늦게 도착해도 SAVE_LUA 가 epoch · seq 비교로 거절한다.
Redis 실패는 진행 표시만 잃을 뿐 분석을 실패시키지 않는다 — 예외는 모두 경고로 남기고 삼킨다.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time
from typing import Any

import redis

from app import execution

_log = logging.getLogger(__name__)

KEY_PREFIX = "pixlate:analysis-progress:"

SAVE_LUA = """
local cur = redis.call('GET', KEYS[1])
local epoch = tonumber(ARGV[1])
local seq = tonumber(ARGV[2])
if cur then
  local old = cjson.decode(cur)
  local oe = tonumber(old['epoch'])
  local os_ = tonumber(old['seq'])
  if oe > epoch then return 0 end
  if oe == epoch and os_ >= seq then return 0 end
end
redis.call('SET', KEYS[1], ARGV[3], 'PX', tonumber(ARGV[4]))
return 1
"""

TOUCH_LUA = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
local old = cjson.decode(cur)
if tonumber(old['epoch']) ~= tonumber(ARGV[1]) then return 0 end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""


def key(attempt_id: Any) -> str:
    return f"{KEY_PREFIX}{attempt_id}"


def _redis_url() -> str:
    return os.getenv("PIXLATE_IDEMPOTENCY_REDIS_URL") or os.getenv("CELERY_RESULT_BACKEND", "redis://pixlate-redis:6379/1")


def _client(timeout: float) -> redis.Redis:
    return redis.Redis.from_url(_redis_url(), socket_timeout=timeout, socket_connect_timeout=timeout, retry_on_timeout=False)


class ProgressReporter:
    """analyze(progress=reporter.on_progress)와 함께 컨텍스트 매니저로 쓴다. hb 는 app.flows.common.Heartbeat(선택)."""

    def __init__(self, attempt_id: Any, lease_epoch: int, source_total: int, hb: Any = None, client: Any = None,
                 ttl_s: float | None = None, touch_s: float | None = None):
        lease_ttl = execution.lease_ttl_s()
        self.attempt_id = attempt_id
        self.epoch = int(lease_epoch)
        self.source_total = source_total
        self.hb = hb
        self.client = client if client is not None else _client(0.5)
        self.ttl_s = ttl_s if ttl_s is not None else 2 * lease_ttl
        self.touch_s = touch_s if touch_s is not None else max(1, lease_ttl / 3)
        self._key = key(attempt_id)
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._seq = 0
        self._latest: tuple[int, str, dict[str, Any]] | None = None
        self._dirty = False
        self._last_io = time.monotonic()
        self._failing = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"progress-{attempt_id}", daemon=True)

    def __enter__(self) -> ProgressReporter:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        if not self._lost():
            self._flush()
        return False

    def on_progress(self, p: Any) -> None:
        with self._lock:
            self._seq += 1
            self._latest = (self._seq, p.event, dataclasses.asdict(p))
            self._dirty = True

    def mark_analyze_done(self, section_count: int) -> None:
        n = section_count
        with self._lock:
            self._seq += 1
            self._latest = (self._seq, "analyze_done", {"regions_done": n, "regions_known": n,
                                                        "sources_split": self.source_total, "source_total": self.source_total})
            self._dirty = True
        if not self._lost():
            self._flush()

    def _lost(self) -> bool:
        return self.hb is not None and bool(getattr(self.hb, "lost", False))

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            if self._lost():
                return
            with self._lock:
                dirty = self._dirty
            if dirty:
                self._flush()
            elif time.monotonic() - self._last_io >= self.touch_s:
                self._touch()

    def _flush(self) -> None:
        with self._io_lock:
            with self._lock:
                if not self._dirty or self._latest is None:
                    return
                seq, event, snap = self._latest
                self._dirty = False
            payload = json.dumps({"epoch": self.epoch, "seq": seq, "event": event, "snap": snap}, ensure_ascii=False)
            try:
                self.client.eval(SAVE_LUA, 1, self._key, self.epoch, seq, payload, int(self.ttl_s * 1000))
            except Exception as e:
                with self._lock:
                    if self._latest is not None and self._latest[0] == seq:
                        self._dirty = True
                self._warn("save", e)
                return
            self._ok()

    def _touch(self) -> None:
        with self._io_lock:
            try:
                self.client.eval(TOUCH_LUA, 1, self._key, self.epoch, int(self.ttl_s * 1000))
            except Exception as e:
                self._warn("touch", e)
                return
            self._ok()

    def _ok(self) -> None:
        self._last_io = time.monotonic()
        self._failing = False

    def _warn(self, op: str, e: Exception) -> None:
        self._last_io = time.monotonic()
        if not self._failing:
            _log.warning("analysis progress %s failed (attempt=%s): %s", op, self.attempt_id, e)
        self._failing = True


def read(attempt_id: Any, client: Any = None) -> dict | None:
    try:
        c = client if client is not None else _client(0.2)
        raw = c.get(key(attempt_id))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        return None
