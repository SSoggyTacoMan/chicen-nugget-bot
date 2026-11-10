"""Tiny in-memory sliding-window rate limiter for AI requests."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock


class RateLimiter:
    def __init__(self, limit_per_minute: int, window_seconds: float = 60.0) -> None:
        self.limit = max(0, limit_per_minute)
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds). Limit 0 disables limiting."""
        if self.limit <= 0:
            return True, 0
        moment = time.monotonic() if now is None else now
        cutoff = moment - self.window
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = max(1, int(self.window - (moment - hits[0])) + 1)
                return False, retry
            hits.append(moment)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


def ai_retry_after(user_id: str) -> int:
    """0 when this user (and the workspace) may start another AI call."""
    from bot.runtime import runtime  # local import avoids a runtime<->limits cycle

    limiter = runtime.ai_limiter
    if limiter is not None:
        allowed, retry = limiter.allow(user_id)
        if not allowed:
            return retry

    shared = runtime.ai_global_limiter
    if shared is not None:
        allowed, retry = shared.allow("global")
        if not allowed:
            return retry
    return 0


# The old name is still used by a few call sites and plugins.
user_retry_after = ai_retry_after


__all__ = ["RateLimiter", "ai_retry_after", "user_retry_after"]
