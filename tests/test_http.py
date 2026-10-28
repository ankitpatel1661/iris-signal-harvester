"""HTTP core and resilience. Every test is offline: HTTP is mocked with respx and
every sleep goes through an injected fake, so nothing waits in real time."""

from __future__ import annotations

import random

import httpx
import pytest
import respx
import structlog.testing

from iris_signal_harvester.config import Settings
from iris_signal_harvester.errors import AccessRestricted, SourceUnavailable
from iris_signal_harvester.errors import UnexpectedResponse as ErrorsUnexpectedResponse
from iris_signal_harvester.http.client import HarvesterClient, UnexpectedResponse
from iris_signal_harvester.http.ratelimit import HostRateLimiter, TokenBucket
from iris_signal_harvester.http.resilience import (
    MAX_RETRY_AFTER_S,
    CircuitBreaker,
    CircuitOpen,
    full_jitter_delay,
    parse_retry_after,
    with_resilience,
)

URL = "https://oparl.example.org/oparl/v1.0/paper"


class FakeTime:
    """Fake clock + sleep: sleeping advances the clock and is recorded."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def ft() -> FakeTime:
    return FakeTime()


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "respect_robots": False,
        "rate_limit_rps": 50.0,
        "max_retries": 4,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def make_client(ft: FakeTime, **overrides: object) -> HarvesterClient:
    return HarvesterClient(
        make_settings(**overrides),
        sleep=ft.sleep,
        clock=ft.clock,
        rng=random.Random(7),
    )


JSON = {"content-type": "application/json"}


# --- status mapping and retry ------------------------------------------------


@respx.mock
async def test_429_then_success_honours_retry_after_exactly(ft: FakeTime) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"data": [1]}, headers=JSON),
        ]
    )
    async with make_client(ft) as client:
        result = await client.get_json(URL)
    assert result.data == {"data": [1]}
    assert route.call_count == 2
    # exactly the server's figure, not a computed backoff
    assert 7.0 in ft.sleeps


@respx.mock
async def test_retry_after_http_date_is_honoured() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Tue, 27 Oct 2026 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("Tue, 27 Oct 2026 11:00:00 GMT", now=now) == 0.0
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None


@respx.mock
async def test_500_then_success(ft: FakeTime) -> None:
    route = respx.get(URL).mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"ok": True}, headers=JSON),
        ]
    )
    async with make_client(ft) as client:
        result = await client.get_json(URL)
    assert result.data == {"ok": True}
    assert route.call_count == 2
    assert len(ft.sleeps) == 1  # one backoff sleep


@respx.mock
async def test_403_is_terminal_exactly_one_request_no_sleep(ft: FakeTime) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(403))
    async with make_client(ft) as client:
        with pytest.raises(AccessRestricted) as info:
            await client.get_json(URL)
    assert info.value.status == 403
    assert route.call_count == 1  # THE rule: one request, no retry
    assert ft.sleeps == []  # no delay-and-retry either
    # and the single request carried the ordinary, honest user-agent
    assert route.calls[0].request.headers["user-agent"] == make_settings().user_agent


@respx.mock
async def test_401_is_terminal_exactly_one_request(ft: FakeTime) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(401))
    async with make_client(ft) as client:
        with pytest.raises(AccessRestricted):
            await client.get_json(URL)
    assert route.call_count == 1
    assert ft.sleeps == []


async def test_with_resilience_never_retries_access_restricted() -> None:
    calls = 0

    async def op() -> None:
        nonlocal calls
        calls += 1
        raise AccessRestricted("nope", status=403)

    async def no_sleep(_: float) -> None:
        raise AssertionError("must not sleep after AccessRestricted")

    with pytest.raises(AccessRestricted):
        await with_resilience(op, max_retries=10, sleep=no_sleep)
    assert calls == 1


@respx.mock
async def test_soft_200_html_is_failure_not_empty_page(ft: FakeTime) -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(
            200, text="<html>Wartungsarbeiten</html>", headers={"content-type": "text/html"}
        )
    )
    async with make_client(ft, max_retries=2) as client:
        with pytest.raises(SourceUnavailable) as info:
            await client.get_json(URL)
    assert "not JSON" in str(info.value)
    assert route.call_count == 3  # it is a retryable failure, retried then raised


@respx.mock
async def test_truncated_json_body_is_failure(ft: FakeTime) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b'{"data": [', headers=JSON))
    async with make_client(ft, max_retries=0) as client:
        with pytest.raises(SourceUnavailable):
            await client.get_json(URL)


@respx.mock
async def test_empty_json_collection_is_a_legitimate_result(ft: FakeTime) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json={"data": []}, headers=JSON))
    async with make_client(ft) as client:
        result = await client.get_json(URL)
    assert result.data == {"data": []}
    assert not result.not_modified


@respx.mock
async def test_ld_json_content_type_accepted(ft: FakeTime) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, content=b'{"a": 1}', headers={"content-type": "application/ld+json; charset=utf-8"}
        )
    )
    async with make_client(ft) as client:
        assert (await client.get_json(URL)).data == {"a": 1}


@respx.mock
async def test_timeout_is_retryable(ft: FakeTime) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={}, headers=JSON)]
    )
    async with make_client(ft) as client:
        await client.get_json(URL)
    assert route.call_count == 2


@respx.mock
async def test_retries_are_bounded_by_max_retries(ft: FakeTime) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    async with make_client(ft, max_retries=3) as client:
        with pytest.raises(SourceUnavailable):
            await client.get_json(URL)
    assert route.call_count == 4  # 1 + 3 retries


@respx.mock
async def test_other_4xx_is_not_retried(ft: FakeTime) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    async with make_client(ft) as client:
        with pytest.raises(UnexpectedResponse):
            await client.get_json(URL)
    assert route.call_count == 1


@respx.mock
async def test_redirect_to_login_page_is_access_restricted(ft: FakeTime) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://oparl.example.org/login"})
    )
    login = respx.get("https://oparl.example.org/login").mock(
        return_value=httpx.Response(200, text="<form>", headers={"content-type": "text/html"})
    )
    async with make_client(ft) as client:
        with pytest.raises(AccessRestricted):
            await client.get_json(URL)
    assert login.call_count == 1


# --- conditional GET ---------------------------------------------------------


@respx.mock
async def test_304_yields_not_modified_and_sends_conditional_headers(ft: FakeTime) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(304, headers={"ETag": '"abc"'}))
    async with make_client(ft) as client:
        result = await client.get_json(
            URL, etag='"abc"', last_modified="Tue, 27 Oct 2026 10:00:00 GMT"
        )
    assert result.not_modified is True
    assert result.data is None
    request = route.calls[0].request
    assert request.headers["if-none-match"] == '"abc"'
    assert request.headers["if-modified-since"] == "Tue, 27 Oct 2026 10:00:00 GMT"


@respx.mock
async def test_etag_and_last_modified_are_captured(ft: FakeTime) -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            json={},
            headers={
                **JSON,
                "ETag": '"v2"',
                "Last-Modified": "Tue, 27 Oct 2026 10:00:00 GMT",
            },
        )
    )
    async with make_client(ft) as client:
        result = await client.get_json(URL)
    assert result.etag == '"v2"'
    assert result.last_modified == "Tue, 27 Oct 2026 10:00:00 GMT"


# --- user-agent --------------------------------------------------------------


@respx.mock
async def test_user_agent_carries_contact_address_from_settings(ft: FakeTime) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={}, headers=JSON))
    settings = make_settings(user_agent="TestBot/1.0 (+mailto:ops@example.org)")
    client = HarvesterClient(settings, sleep=ft.sleep, clock=ft.clock)
    async with client:
        await client.get_json(URL)
    ua = route.calls[0].request.headers["user-agent"]
    assert ua == "TestBot/1.0 (+mailto:ops@example.org)"
    assert "mailto:" in ua


def test_default_user_agent_is_contactable() -> None:
    assert "mailto:" in make_settings().user_agent


# --- robots.txt --------------------------------------------------------------


@respx.mock
async def test_robots_disallow_is_access_restricted_and_data_never_requested(
    ft: FakeTime,
) -> None:
    robots = respx.get("https://oparl.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /oparl/\n")
    )
    data = respx.get(URL).mock(return_value=httpx.Response(200, json={}, headers=JSON))
    async with make_client(ft, respect_robots=True) as client:
        with pytest.raises(AccessRestricted):
            await client.get_json(URL)
    assert robots.call_count == 1
    assert data.call_count == 0


@respx.mock
async def test_robots_is_cached_per_host(ft: FakeTime) -> None:
    robots = respx.get("https://oparl.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={}, headers=JSON))
    async with make_client(ft, respect_robots=True) as client:
        await client.get_json(URL)
        await client.get_json(URL)
    assert robots.call_count == 1


@respx.mock
async def test_missing_robots_txt_allows(ft: FakeTime) -> None:
    respx.get("https://oparl.example.org/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(URL).mock(return_value=httpx.Response(200, json={"x": 1}, headers=JSON))
    async with make_client(ft, respect_robots=True) as client:
        assert (await client.get_json(URL)).data == {"x": 1}


@respx.mock
async def test_robots_403_is_terminal_one_request(ft: FakeTime) -> None:
    robots = respx.get("https://oparl.example.org/robots.txt").mock(
        return_value=httpx.Response(403)
    )
    data = respx.get(URL).mock(return_value=httpx.Response(200, json={}, headers=JSON))
    async with make_client(ft, respect_robots=True) as client:
        with pytest.raises(AccessRestricted):
            await client.get_json(URL)
    assert robots.call_count == 1
    assert data.call_count == 0


@respx.mock
async def test_crawl_delay_is_honoured(ft: FakeTime) -> None:
    respx.get("https://oparl.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 10\n")
    )
    respx.get(URL).mock(return_value=httpx.Response(200, json={}, headers=JSON))
    async with make_client(ft, respect_robots=True, rate_limit_rps=50.0) as client:
        await client.get_json(URL)
        ft.sleeps.clear()
        await client.get_json(URL)
    assert sum(ft.sleeps) >= 9.99  # spaced by the 10s Crawl-delay, not 1/50s


# --- rate limiting -----------------------------------------------------------


async def test_token_bucket_spaces_requests(ft: FakeTime) -> None:
    bucket = TokenBucket(2.0, clock=ft.clock, sleep=ft.sleep)  # 2 rps -> 0.5s apart
    start = ft.now
    for _ in range(4):
        await bucket.acquire()
    assert ft.now - start == pytest.approx(1.5)  # first is free, then 3 x 0.5s
    assert ft.sleeps == [pytest.approx(0.5)] * 3


async def test_token_bucket_is_per_host(ft: FakeTime) -> None:
    limiter = HostRateLimiter(1.0, clock=ft.clock, sleep=ft.sleep)
    await limiter.acquire("a.example")
    await limiter.acquire("b.example")  # different host: no wait
    assert ft.sleeps == []
    await limiter.acquire("a.example")
    assert ft.sleeps == [pytest.approx(1.0)]


async def test_bucket_shrinks_after_429_for_rest_of_run(ft: FakeTime) -> None:
    limiter = HostRateLimiter(4.0, clock=ft.clock, sleep=ft.sleep)
    bucket = limiter.bucket("a.example")
    assert bucket.rate == 4.0
    limiter.on_throttled("a.example")
    assert bucket.rate == 2.0
    limiter.on_throttled("a.example")
    assert bucket.rate == 1.0
    # other hosts unaffected
    assert limiter.bucket("b.example").rate == 4.0
    # and the rate does not recover with time
    ft.now += 3600
    assert bucket.rate == 1.0


@respx.mock
async def test_429_through_client_shrinks_host_rate(ft: FakeTime) -> None:
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={}, headers=JSON),
        ]
    )
    async with make_client(ft, rate_limit_rps=4.0) as client:
        await client.get_json(URL)
        assert client.limiter.bucket("oparl.example.org").rate == 2.0


# --- backoff -----------------------------------------------------------------


def test_backoff_is_exponential_ceiling_with_full_jitter() -> None:
    rng = random.Random(1)
    for attempt, ceiling in [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (10, 30.0)]:
        draws = [full_jitter_delay(attempt, 1.0, 30.0, rng) for _ in range(300)]
        assert all(0.0 <= d <= ceiling for d in draws)
        # full jitter: draws spread over the whole [0, ceiling] range, not a narrow band
        assert min(draws) < ceiling * 0.2
        assert max(draws) > ceiling * 0.8


async def test_computed_backoff_used_when_no_retry_after() -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    attempts = 0

    async def op() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise SourceUnavailable("boom", status=500)
        return "ok"

    assert await with_resilience(op, max_retries=5, sleep=sleep, rng=random.Random(3)) == "ok"
    assert len(sleeps) == 3
    assert all(s <= c for s, c in zip(sleeps, [1.0, 2.0, 4.0], strict=True))


async def test_huge_retry_after_is_clamped_and_logged() -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    attempts = 0

    async def op() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SourceUnavailable("slow down", status=429, retry_after=86400.0)
        return "ok"

    with structlog.testing.capture_logs() as logs:
        assert await with_resilience(op, max_retries=2, sleep=sleep) == "ok"
    assert MAX_RETRY_AFTER_S == 120.0
    assert sleeps == [MAX_RETRY_AFTER_S]
    clamped = [e for e in logs if e["event"] == "retry_after_clamped"]
    assert len(clamped) == 1
    assert clamped[0]["log_level"] == "warning"
    assert clamped[0]["requested"] == 86400.0


async def test_retry_after_within_cap_is_not_clamped() -> None:
    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    attempts = 0

    async def op() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise SourceUnavailable("slow down", status=429, retry_after=MAX_RETRY_AFTER_S)
        return "ok"

    with structlog.testing.capture_logs() as logs:
        assert await with_resilience(op, max_retries=2, sleep=sleep) == "ok"
    assert sleeps == [MAX_RETRY_AFTER_S]
    assert not [e for e in logs if e["event"] == "retry_after_clamped"]


def test_unexpected_response_importable_from_both_paths() -> None:
    assert UnexpectedResponse is ErrorsUnexpectedResponse


# --- circuit breaker ---------------------------------------------------------


async def test_breaker_opens_after_n_consecutive_failures() -> None:
    breaker = CircuitBreaker("body-1", threshold=3)
    calls = 0

    async def no_sleep(_: float) -> None:
        return None

    async def failing() -> None:
        nonlocal calls
        calls += 1
        raise SourceUnavailable("down", status=503)

    for _ in range(3):
        with pytest.raises(SourceUnavailable):
            await with_resilience(failing, max_retries=0, sleep=no_sleep, breaker=breaker)
    assert breaker.is_open
    before = calls
    with pytest.raises(CircuitOpen):
        await with_resilience(failing, max_retries=0, sleep=no_sleep, breaker=breaker)
    assert calls == before  # open breaker short-circuits: operation not even attempted


async def test_breaker_success_resets_count() -> None:
    breaker = CircuitBreaker("b", threshold=2)

    async def no_sleep(_: float) -> None:
        return None

    async def fail() -> None:
        raise SourceUnavailable("x")

    async def ok() -> int:
        return 1

    with pytest.raises(SourceUnavailable):
        await with_resilience(fail, max_retries=0, sleep=no_sleep, breaker=breaker)
    await with_resilience(ok, max_retries=0, sleep=no_sleep, breaker=breaker)
    with pytest.raises(SourceUnavailable):
        await with_resilience(fail, max_retries=0, sleep=no_sleep, breaker=breaker)
    assert not breaker.is_open


async def test_access_restricted_does_not_trip_breaker() -> None:
    breaker = CircuitBreaker("b", threshold=1)

    async def refused() -> None:
        raise AccessRestricted("403", status=403)

    with pytest.raises(AccessRestricted):
        await with_resilience(refused, max_retries=3, breaker=breaker)
    assert not breaker.is_open


@respx.mock
async def test_client_breaker_is_per_body(ft: FakeTime) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    client = HarvesterClient(
        make_settings(max_retries=0), sleep=ft.sleep, clock=ft.clock, breaker_threshold=2
    )
    async with client:
        for _ in range(2):
            with pytest.raises(SourceUnavailable):
                await client.get_json(URL, body_key="body-a")
        with pytest.raises(CircuitOpen):
            await client.get_json(URL, body_key="body-a")
        assert route.call_count == 2
        # a different body is unaffected
        with pytest.raises(SourceUnavailable):
            await client.get_json(URL, body_key="body-b")
        assert route.call_count == 3
