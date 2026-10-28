"""``HarvesterClient``: the one place that touches the network.

Responsibilities, in request order:

1. robots.txt (fetched and cached per origin) -- a disallow is ``AccessRestricted``.
2. circuit breaker (per body) and retry policy (``SourceUnavailable`` only).
3. per-host token bucket, which shrinks after a 429 and honours ``Crawl-delay``.
4. conditional GET (``ETag`` / ``If-Modified-Since``); 304 becomes ``not_modified``.
5. status mapping: 401/403 -> ``AccessRestricted`` (terminal); 429, 5xx, 408,
   timeouts, transport errors -> ``SourceUnavailable``; a 2xx whose content-type is
   not JSON (or whose body is not valid JSON) -> ``SourceUnavailable`` too. A
   "soft 200" is a failure, never an empty result.

Redirects are followed by hand (never by httpx), capped at ``MAX_REDIRECTS`` hops.
Every hop is checked against the target origin's robots.txt and is charged to the
target host's rate limiter, so a 3xx can never reach a URL that was not itself
allowed. ``FetchResult.url`` is the FINAL url, so a redirect leaves a trace.

There is intentionally no code path that reacts to an access refusal by changing
headers, user-agent or timing. The User-Agent is fixed at construction from
settings and carries a contact address.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TypeVar
from urllib.robotparser import RobotFileParser

import httpx
import structlog

from iris_signal_harvester.config import Settings
from iris_signal_harvester.errors import (
    AccessRestricted,
    SourceUnavailable,
    UnexpectedResponse,
)
from iris_signal_harvester.http.ratelimit import HostRateLimiter
from iris_signal_harvester.http.resilience import (
    CircuitBreaker,
    parse_retry_after,
    with_resilience,
)

__all__ = ["FetchResult", "HarvesterClient", "UnexpectedResponse"]

log = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
T = TypeVar("T")

MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_LOGIN_PATH = re.compile(r"/(log-?in|sign-?in|sso|auth(?:enticate)?)(?:/|$|\.)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Sent:
    """A response plus where the redirect chain ended."""

    response: httpx.Response
    final_url: str
    hops: int


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of one successful GET. ``data`` is ``None`` iff ``not_modified``.

    ``url`` is the final URL after any redirects, not the URL first requested.
    """

    url: str
    status: int
    data: Any
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def _is_json_content_type(value: str | None) -> bool:
    if not value:
        return False
    media = value.split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")


class HarvesterClient:
    """Polite, resilient JSON-over-HTTP client."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        breaker_threshold: int = 5,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._breaker_threshold = breaker_threshold
        self._limiter = HostRateLimiter(settings.rate_limit_rps, clock=clock, sleep=sleep)
        self._breakers: dict[str, CircuitBreaker] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._http = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            timeout=httpx.Timeout(settings.request_timeout_s),
            follow_redirects=False,
            transport=transport,
        )

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> HarvesterClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- public API ---------------------------------------------------------

    @property
    def limiter(self) -> HostRateLimiter:
        return self._limiter

    def breaker(self, key: str) -> CircuitBreaker:
        """The circuit breaker for one body (created on first use)."""
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(key, self._breaker_threshold)
            self._breakers[key] = breaker
        return breaker

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        body_key: str | None = None,
    ) -> FetchResult:
        """GET ``url`` and return parsed JSON.

        Raises:
            AccessRestricted: 401/403, login redirect, or robots disallow. Terminal.
            SourceUnavailable: transient failure after ``max_retries`` retries.
            CircuitOpen: the body's breaker is open.
            UnexpectedResponse: some other 4xx. Not retried.
        """
        if self._settings.respect_robots:
            await self._ensure_allowed(url)

        headers = {"Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        async def attempt() -> FetchResult:
            sent = await self._send(url, headers=headers, params=params, check_robots=True)
            return self._interpret(url, sent)

        return await self._resilient(attempt, body_key)

    # -- internals ----------------------------------------------------------

    async def _resilient(self, operation: Callable[[], Awaitable[T]], body_key: str | None) -> T:
        return await with_resilience(
            operation,
            max_retries=self._settings.max_retries,
            base_delay=self._base_delay,
            max_delay=self._max_delay,
            sleep=self._sleep,
            rng=self._rng,
            breaker=self.breaker(body_key) if body_key else None,
        )

    async def _send(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str | int] | None = None,
        check_robots: bool = False,
    ) -> _Sent:
        """GET ``url``, following redirects by hand.

        Each hop is rate limited against the host it actually targets and, when
        ``check_robots`` is set, checked against that origin's robots.txt before it is
        requested. (The first URL is checked by the caller, once, outside the retry loop.)
        """
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            if hop > 0 and check_robots and self._settings.respect_robots:
                await self._ensure_allowed(current)
            await self._limiter.acquire(httpx.URL(current).host)
            try:
                response = await self._http.get(
                    current, headers=headers, params=params if hop == 0 else None
                )
            except httpx.TimeoutException as exc:
                raise SourceUnavailable(f"timeout fetching {current}: {exc!r}") from exc
            except httpx.TransportError as exc:
                raise SourceUnavailable(f"transport error fetching {current}: {exc!r}") from exc
            location = response.headers.get("Location")
            if response.status_code not in _REDIRECT_STATUSES or not location:
                return _Sent(response=response, final_url=current, hops=hop)
            if hop == MAX_REDIRECTS:
                raise UnexpectedResponse(
                    f"more than {MAX_REDIRECTS} redirects from {url}",
                    status=response.status_code,
                    url=url,
                )
            target = httpx.URL(current).join(location)
            if target.scheme not in ("http", "https") or not target.host:
                raise UnexpectedResponse(
                    f"redirect from {current} to unsupported target {location!r}",
                    status=response.status_code,
                    url=url,
                )
            log.info("redirect_followed", from_url=current, to_url=str(target), hop=hop + 1)
            current = str(target)
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    def _raise_for_transient_or_refused(self, url: str, response: httpx.Response) -> None:
        """Shared status mapping for data and robots requests."""
        status = response.status_code
        if status in (401, 403):
            raise AccessRestricted(f"HTTP {status} from {url}", status=status, url=url)
        if status == 429:
            host = httpx.URL(url).host
            new_rate = self._limiter.on_throttled(host)
            log.warning("rate_limited", host=host, new_rps=new_rate)
            raise SourceUnavailable(
                f"HTTP 429 from {url}",
                status=status,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        if status == 408 or status >= 500:
            raise SourceUnavailable(
                f"HTTP {status} from {url}",
                status=status,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )

    def _interpret(self, url: str, sent: _Sent) -> FetchResult:
        response = sent.response
        final_url = sent.final_url
        self._raise_for_transient_or_refused(final_url, response)
        status = response.status_code

        # A redirect chain that ends on a login page is a login wall, not data.
        if (
            sent.hops
            and _LOGIN_PATH.search(httpx.URL(final_url).path)
            and not _LOGIN_PATH.search(httpx.URL(url).path)
        ):
            raise AccessRestricted(
                f"redirected to login page {final_url} from {url}", status=status, url=url
            )

        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")

        if status == 304:
            return FetchResult(
                url=final_url,
                status=status,
                data=None,
                etag=etag,
                last_modified=last_modified,
                not_modified=True,
            )
        if not 200 <= status < 300:
            raise UnexpectedResponse(f"HTTP {status} from {final_url}", status=status, url=url)

        content_type = response.headers.get("Content-Type")
        if not _is_json_content_type(content_type):
            # Soft 200: an HTML error/maintenance/login page served with 200. A failure,
            # never an empty result.
            raise SourceUnavailable(
                f"HTTP {status} from {final_url} but content-type is {content_type!r}, not JSON",
                status=status,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceUnavailable(
                f"HTTP {status} from {final_url} with unparseable JSON body: {exc}", status=status
            ) from exc
        return FetchResult(
            url=final_url, status=status, data=data, etag=etag, last_modified=last_modified
        )

    # -- robots.txt ---------------------------------------------------------

    async def _ensure_allowed(self, url: str) -> None:
        parsed = httpx.URL(url)
        origin = f"{parsed.scheme}://{parsed.netloc.decode('ascii')}"
        rules = await self._robots_for(origin, parsed.host)
        if rules is not None and not rules.can_fetch(self._settings.user_agent, url):
            raise AccessRestricted(f"robots.txt at {origin} disallows {url}", url=url)

    async def _robots_for(self, origin: str, host: str) -> RobotFileParser | None:
        if origin in self._robots:
            return self._robots[origin]
        lock = self._robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._robots:
                return self._robots[origin]
            robots_url = f"{origin}/robots.txt"

            async def attempt() -> RobotFileParser | None:
                sent = await self._send(robots_url, headers={"Accept": "text/plain"})
                response = sent.response
                self._raise_for_transient_or_refused(sent.final_url, response)
                if response.status_code >= 400:
                    return None  # no robots.txt (404/410/...): nothing is disallowed
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                parser.modified()
                return parser

            rules: RobotFileParser | None = await self._resilient(attempt, None)
            if rules is not None:
                delay = rules.crawl_delay(self._settings.user_agent)
                if delay is not None and float(delay) > 0:
                    self._limiter.cap(host, 1.0 / float(delay))
                    log.info("crawl_delay_honoured", host=host, delay_s=float(delay))
            self._robots[origin] = rules
            return rules
