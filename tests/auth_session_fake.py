"""세션 저장소 대역 — PIXLATE_TEST_REDIS_URL 이 없는 환경에서 세션과 무관한 시험이 로그인할 수 있게 한다.

app.auth_sessions 가 쓰는 Redis 연산(eval 의 두 스크립트 · hmget · delete)만 같은 뜻으로 흉내 낸다.
세션 자체의 시험(tests/test_auth_session.py)은 이 대역을 쓰지 않고 실제 Redis 를 요구한다(CI 필수 시험).
"""
from __future__ import annotations

import threading

from app import auth_sessions


class FakeSessionRedis:
    def __init__(self):
        self.h: dict[str, dict[str, str]] = {}
        self.lock = threading.Lock()

    def eval(self, script, numkeys, k, *args):
        args = [str(a) for a in args]
        with self.lock:
            if script == auth_sessions._CREATE_REPLACING:
                old_k = args.pop(0)
                seller, t, abs_exp, idle, old_gen = args
                replaced = ""
                old = self.h.get(old_k) if old_gen else None
                if old and int(old_gen) in (int(old["gen"]), int(old["gen"]) - 1):
                    replaced = old["seller_id"]
                    del self.h[old_k]
                self.h[k] = {"seller_id": seller, "created_at": t, "abs_exp": abs_exp, "last_active": t, "idle_exp": idle, "gen": "1"}
                return replaced
            if script == auth_sessions._ROTATE:
                g, now, idle_ttl, grace = (int(a) for a in args)
                rec = self.h.get(k)
                if rec is None:
                    return ["invalid"]
                if now >= int(rec["abs_exp"]) or now >= int(rec["idle_exp"]):
                    del self.h[k]
                    return ["expired"]
                cur = int(rec["gen"])
                if g == cur:
                    new_idle = now + idle_ttl
                    rec.update(gen=str(cur + 1), prev_until=str(now + grace), last_active=str(now), idle_exp=str(new_idle))
                    return ["ok", rec["seller_id"], rec["created_at"], rec["abs_exp"], str(new_idle), str(cur + 1)]
                if g == cur - 1:
                    if now < int(rec.get("prev_until", 0)):
                        return ["rotated"]
                    del self.h[k]
                    return ["reused"]
                if g < cur - 1:
                    del self.h[k]
                    return ["reused_old"]
                return ["invalid"]
        raise AssertionError("unknown script")

    def hmget(self, k, *fields):
        with self.lock:
            rec = self.h.get(k) or {}
            return [rec.get(f) for f in fields]

    def delete(self, *ks):
        with self.lock:
            return sum(1 for k in ks if self.h.pop(k, None) is not None)
