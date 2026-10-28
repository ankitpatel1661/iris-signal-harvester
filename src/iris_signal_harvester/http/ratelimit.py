"""Per-host async token bucket.

One bucket per host, refilled at ``rate_limit_rps``, with a burst capacity of one
so that requests to a host are *spaced* rather than fired in a salvo. A 429 from a
host shrinks that host's rate for the remainder of the run and the rate never
grows back: a server that has asked us to slow down once has told us something we
should not unlearn mid-run.

The clock and the sleep function are injectable so tests are deterministic and
never wait in real time.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

__all__ = ["HostRateLimiter", "TokenBucket"]

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

#: Never throttle below one request per 20 seconds; below that the run is stalled.
MIN_RATE_RPS = 0.05


class TokenBucket:
    """A single-host token bucket with a burst capacity of one token."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        min_rate: float = MIN_RATE_RPS,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._min_rate = min(min_rate, rate)
        self._clock = clock
        self._sleep = sleep
        self._tokens = 1.0
        self._last = clock()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    def shrink(self, factor: float = 0.5) -> float:
        """Reduce the rate for the rest of the run. Returns the new rate."""
        if not 0 < factor < 1:
            raise ValueError("factor must be in (0, 1)")
        self._rate = max(self._rate * factor, self._min_rate)
        return self._rate

    def cap(self, max_rate: float) -> float:
        """Lower the rate to at most ``max_rate`` (e.g. to honour a Crawl-delay)."""
        if max_rate <= 0:
            raise ValueError("max_rate must be positive")
        self._rate = min(self._rate, max_rate)
        self._min_rate = min(self._min_rate, self._rate)
        return self._rate

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        self._tokens = min(1.0, self._tokens + elapsed * self._rate)
        self._last = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        async with self._lock:
            self._refill(self._clock())
            if self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._rate)
                self._last = self._clock()
                self._tokens = 1.0
            self._tokens = 0.0


class HostRateLimiter:
    """Hands out one :class:`TokenBucket` per host, all starting at the same rate."""

    def __init__(
        self,
        rps: float,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rps <= 0:
            raise ValueError("rps must be positive")
        self._rps = rps
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, host: str) -> TokenBucket:
        key = host.lower()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._rps, clock=self._clock, sleep=self._sleep)
            self._buckets[key] = bucket
        return bucket

    async def acquire(self, host: str) -> None:
        await self.bucket(host).acquire()

    def on_throttled(self, host: str) -> float:
        """Record a 429 from ``host``; the host's rate is halved for the rest of the run."""
        return self.bucket(host).shrink()

    def cap(self, host: str, max_rate: float) -> float:
        return self.bucket(host).cap(max_rate)
