"""Retry policy and circuit breaker.

The rule this module exists to enforce: only ``SourceUnavailable`` is retried.
``AccessRestricted`` (401, 403, login wall, robots disallow) is terminal. It is
never retried, and nothing here varies headers, user-agent or timing in response
to it — a retry loop on a refusal would look like an attempt to get around it.

Backoff is exponential with *full* jitter (a uniform draw from ``[0, cap]``),
which de-synchronises concurrent workers far better than a fixed jitter band.
When the server supplied ``Retry-After`` that value is honoured, but never beyond
``MAX_RETRY_AFTER_S``: a server asking for a day must not stall a worker for a day.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
)

from iris_signal_harvester.errors import HarvesterError, SourceUnavailable

__all__ = [
    "MAX_RETRY_AFTER_S",
    "CircuitBreaker",
    "CircuitOpen",
    "full_jitter_delay",
    "parse_retry_after",
    "with_resilience",
]

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]

log = structlog.get_logger(__name__)

# Upper bound on how long a server-supplied ``Retry-After`` is honoured. The fact
# that a server asked for far longer is itself worth reporting (see ``wait``).
MAX_RETRY_AFTER_S: float = 120.0


class CircuitOpen(HarvesterError):
    """The per-body circuit breaker is open. Deliberately NOT a ``SourceUnavailable``,
    so the retry policy never retries it."""

    def __init__(self, key: str, failures: int) -> None:
        super().__init__(f"circuit open for {key!r} after {failures} consecutive failures")
        self.key = key
        self.failures = failures


class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures and stays open for the run.

    There is no timed half-open state: a harvest run is short-lived, and a body
    that failed ``threshold`` operations in a row (each already retried) is
    skipped and reported rather than hammered.
    """

    def __init__(self, key: str, threshold: int = 5) -> None:
        if threshold < 1:
            raise ValueError("threshold must be >= 1")
        self.key = key
        self.threshold = threshold
        self._consecutive_failures = 0

    @property
    def is_open(self) -> bool:
        return self._consecutive_failures >= self.threshold

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def check(self) -> None:
        if self.is_open:
            raise CircuitOpen(self.key, self._consecutive_failures)

    def record_success(self) -> None:
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures == self.threshold:
            log.warning("circuit_opened", key=self.key, failures=self._consecutive_failures)


def full_jitter_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Exponential backoff with full jitter: ``uniform(0, min(cap, base * 2**(attempt-1)))``."""
    ceiling = min(cap, base * (2 ** max(attempt - 1, 0)))
    return rng.uniform(0.0, ceiling)


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta-seconds or HTTP-date) into seconds."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(0.0, seconds)


async def with_resilience(
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    breaker: CircuitBreaker | None = None,
) -> T:
    """Run ``operation``, retrying ``SourceUnavailable`` up to ``max_retries`` times.

    * ``AccessRestricted`` and every other exception propagate immediately: one call.
    * If the failure carries ``retry_after`` it is slept for exactly (clamped to
      ``MAX_RETRY_AFTER_S``, with a WARNING); otherwise the
      delay is exponential with full jitter, capped at ``max_delay``.
    * If ``breaker`` is given it is consulted before the operation; an operation
      that ends in ``SourceUnavailable`` (retries exhausted) counts as one failure,
      a success resets the count. Access refusals do not count.
    """
    if breaker is not None:
        breaker.check()
    jitter_rng = rng or random.Random()

    def wait(state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome is not None else None
        if isinstance(exc, SourceUnavailable) and exc.retry_after is not None:
            if exc.retry_after > MAX_RETRY_AFTER_S:
                log.warning(
                    "retry_after_clamped",
                    requested=exc.retry_after,
                    clamped_to=MAX_RETRY_AFTER_S,
                    status=exc.status,
                )
                return MAX_RETRY_AFTER_S
            return exc.retry_after
        return full_jitter_delay(state.attempt_number, base_delay, max_delay, jitter_rng)

    def before_sleep(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome is not None else None
        log.info(
            "retrying",
            attempt=state.attempt_number,
            reason=str(exc),
            status=getattr(exc, "status", None),
            delay=state.next_action.sleep if state.next_action is not None else None,
        )

    retrying = AsyncRetrying(
        retry=retry_if_exception_type(SourceUnavailable),
        stop=stop_after_attempt(max_retries + 1),
        wait=wait,
        sleep=sleep,
        before_sleep=before_sleep,
        reraise=True,
    )
    try:
        result: T = await retrying(operation)
    except SourceUnavailable:
        if breaker is not None:
            breaker.record_failure()
        raise
    if breaker is not None:
        breaker.record_success()
    return result
