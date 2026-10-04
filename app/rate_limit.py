"""A tiny in-memory sliding-window rate limiter.

Single-process, no Redis — fine for guarding a per-request paid LLM call
(see app.keyword_research) behind a per-IP cap when there's no logged-in
user to key a limit off of, the way FREE_TRADES_PER_MONTH in app/main.py
caps per-account usage instead. A multi-process deployment would need a
shared store; out of scope here, same "no infra beyond Postgres" approach
the rest of this app takes.
"""

import time


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float, clock=time.time):
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, list[float]] = {}

    def at_limit(self, key: str) -> bool:
        """Whether `key` is currently at the cap, without recording a hit."""
        now = self._clock()
        return sum(1 for t in self._hits.get(key, []) if now - t < self.window_seconds) >= self.limit

    def allow(self, key: str) -> bool:
        """True and records a hit if `key` is still under the limit for the
        current window; False (no hit recorded) if it's already at the cap."""
        now = self._clock()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window_seconds]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True
