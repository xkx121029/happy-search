"""网关层：令牌桶限流（每 API Key / IP 一个桶）。

临界区纯 CPU、无 await，使用同步锁，避免依赖事件循环（兼容 Python 3.9）。
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, qps: int = 10, burst: int = 20) -> None:
        self.qps = max(1, qps)
        self.burst = max(self.qps, burst)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int | None]:
        """返回 (是否放行, 建议重试毫秒数)。"""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(self.burst, tokens + (now - last) * self.qps)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False, int((1 - tokens) / self.qps * 1000) + 1
            self._buckets[key] = (tokens - 1, now)
            return True, None
