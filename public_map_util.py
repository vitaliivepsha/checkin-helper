"""Small helpers for the public, unauthenticated festival map page
(webapp_server's /map and /api/public/*): a TTL cache so a crowd opening the
link doesn't recompute the same payload per request, and a per-IP sliding
window rate limiter. In-memory, per process - enough for a read-only page
whose data is local (no Untappd quota behind any of it)."""

import time
from collections import deque


class TTLCache:
    def __init__(self, ttl_seconds: float = 30.0, max_entries: int = 200):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict = {}

    def get(self, key):
        hit = self._data.get(key)
        if hit is None:
            return None
        stored_at, value = hit
        if time.monotonic() - stored_at > self._ttl:
            del self._data[key]
            return None
        return value

    def set(self, key, value) -> None:
        if len(self._data) >= self._max:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            del self._data[oldest]
        self._data[key] = (time.monotonic(), value)


class RateLimiter:
    """allow(key, limit) is True while `key` made fewer than `limit` calls in
    the last `window_seconds`, recording this one when it returns True."""

    def __init__(self, window_seconds: float = 60.0, max_keys: int = 5000):
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= self._max_keys:
                self._prune(now)
            q = self._hits[key] = deque()
        while q and now - q[0] > self._window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self._window]:
            del self._hits[k]
        if len(self._hits) >= self._max_keys:
            self._hits.clear()
