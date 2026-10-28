"""Redirect handling, probe ordering and config validation."""

from __future__ import annotations

import random
from datetime import UTC, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import CanonicalSignal
from iris_signal_harvester.errors import AccessRestricted, UnexpectedResponse
from iris_signal_harvester.http.client import MAX_REDIRECTS, HarvesterClient
from iris_signal_harvester.reporting.verification import SourceVerifier

JSON = {"content-type": "application/json"}
A = "https://a.example.org"
B = "https://b.example.net"


class FakeTime:
    def __init__(self) -> None:
        self.now = 1000.0
        self.acquired: list[str] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def make_client(ft: FakeTime, **overrides: object) -> HarvesterClient:
    values: dict[str, object] = {"respect_robots": True, "rate_limit_rps": 50.0}
    values.update(overrides)
    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type]
    client = HarvesterClient(settings, sleep=ft.sleep, clock=ft.clock, rng=random.Random(1))
    original = client.limiter.acquire

    async def spy(host: str) -> None:
        ft.acquired.append(host)
        await original(host)

    client.limiter.acquire = spy  # type: ignore[method-assign]
    return client


# --- C-1 -----------------------------------------------------------------------


@respx.mock
async def test_redirect_to_a_host_whose_robots_disallows_is_refused() -> None:
    """The review's reproduction: A allows, 302 to B, B says Disallow: /."""
    ft = FakeTime()
    respx.get(f"{A}/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\n"))
    respx.get(f"{A}/paper").mock(
        return_value=httpx.Response(302, headers={"location": f"{B}/private/data"})
    )
    robots_b = respx.get(f"{B}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    data_b = respx.get(f"{B}/private/data").mock(
        return_value=httpx.Response(200, json={"secret": 1}, headers=JSON)
    )
    async with make_client(ft) as client:
        with pytest.raises(AccessRestricted):
            await client.get_json(f"{A}/paper")
    assert robots_b.call_count == 1  # B's robots.txt WAS consulted
    assert data_b.call_count == 0  # and the disallowed URL was never fetched


@respx.mock
async def test_allowed_redirect_is_rate_limited_against_the_target_host_and_reports_final_url() -> (
    None
):
    ft = FakeTime()
    respx.get(f"{A}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{B}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{A}/paper").mock(
        return_value=httpx.Response(301, headers={"location": f"{B}/moved/paper?x=1"})
    )
    respx.get(f"{B}/moved/paper").mock(
        return_value=httpx.Response(200, json={"ok": 1}, headers=JSON)
    )
    async with make_client(ft) as client:
        result = await client.get_json(f"{A}/paper")
    assert result.url == f"{B}/moved/paper?x=1"
    assert result.data == {"ok": 1}
    assert "b.example.net" in ft.acquired and "a.example.org" in ft.acquired


@respx.mock
async def test_relative_redirect_stays_on_the_same_host() -> None:
    ft = FakeTime()
    respx.get(f"{A}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(f"{A}/old").mock(return_value=httpx.Response(308, headers={"location": "/new"}))
    respx.get(f"{A}/new").mock(return_value=httpx.Response(200, json=[1], headers=JSON))
    async with make_client(ft) as client:
        result = await client.get_json(f"{A}/old")
    assert result.url == f"{A}/new"


@respx.mock
async def test_redirect_loop_is_capped_and_not_retried() -> None:
    ft = FakeTime()
    route = respx.get(f"{A}/loop").mock(
        return_value=httpx.Response(302, headers={"location": f"{A}/loop"})
    )
    async with make_client(ft, respect_robots=False) as client:
        with pytest.raises(UnexpectedResponse):
            await client.get_json(f"{A}/loop")
    assert route.call_count == MAX_REDIRECTS + 1


@respx.mock
async def test_redirect_to_a_non_http_scheme_is_refused() -> None:
    ft = FakeTime()
    respx.get(f"{A}/paper").mock(
        return_value=httpx.Response(302, headers={"location": "file:///etc/passwd"})
    )
    async with make_client(ft, respect_robots=False) as client:
        with pytest.raises(UnexpectedResponse):
            await client.get_json(f"{A}/paper")


# --- I-13: the verification probe reads robots.txt first, 401/403 is a refusal ---------


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return routes.get(str(request.url), httpx.Response(404, text="nf"))

    transport = httpx.MockTransport(respond)
    transport.seen = seen
    return transport


def _verifier(transport: httpx.MockTransport) -> SourceVerifier:
    return SourceVerifier(
        transport=transport,
        resolver=lambda host: ["9.9.9.9"],
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_probe_does_not_fetch_a_url_robots_disallows() -> None:
    transport = _transport(
        {
            "https://closed.example.org/robots.txt": httpx.Response(
                200, text="User-agent: *\nDisallow: /\n"
            ),
        }
    )
    report = await _verifier(transport).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=(),
        candidate_urls=("https://closed.example.org/oparl",),
    )
    assert "https://closed.example.org/oparl" not in transport.seen
    assert report.candidate_robots[0].allowed is False
    assert "not fetched" in (report.candidates[0].error or "")


@pytest.mark.parametrize("status", [401, 403])
async def test_probe_treats_a_refused_robots_txt_as_a_refusal(status: int) -> None:
    transport = _transport({"https://walled.example.org/robots.txt": httpx.Response(status)})
    report = await _verifier(transport).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=(),
        candidate_urls=("https://walled.example.org/api",),
    )
    assert "https://walled.example.org/api" not in transport.seen
    assert "walled.example.org" in report.refusals
    assert report.candidate_robots[0].refused is True
    assert report.candidate_robots[0].allowed is False


async def test_probe_does_not_fetch_when_robots_is_unreachable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    report = await _verifier(httpx.MockTransport(boom)).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=(),
        candidate_urls=("https://x.example.org/api",),
    )
    assert report.candidates[0].status is None
    assert "not fetched" in (report.candidates[0].error or "")


# --- M-4, M-10, M-7 ----------------------------------------------------------------


def test_invalid_timezone_fails_at_startup() -> None:
    with pytest.raises(ValidationError, match="source_timezone"):
        Settings(_env_file=None, source_timezone="Mars/Olympus_Mons")
    assert Settings(_env_file=None, source_timezone="UTC").source_timezone == "UTC"


@pytest.mark.parametrize(
    "ua",
    [
        "bot/1.0 (+mailto:ops@corp.example)",
        "bot/1.0 (+https://corp.example/contact)",
        "bot/1.0 (+http://corp.example/contact)",
    ],
)
def test_user_agent_with_a_real_contact_is_accepted(ua: str) -> None:
    assert Settings(_env_file=None, user_agent=ua).user_agent == ua


@pytest.mark.parametrize(
    "ua",
    ["bot/1.0", "http-client/2.0", "bot (see http)", "bot mailto:", "bot (+mailto:nobody)"],
)
def test_user_agent_without_a_real_contact_is_rejected(ua: str) -> None:
    with pytest.raises(ValidationError, match="user_agent"):
        Settings(_env_file=None, user_agent=ua)


def test_default_user_agent_is_a_neutral_placeholder() -> None:
    ua = Settings(_env_file=None).user_agent
    assert "example.org" in ua
    assert "ankitpatel" not in ua


@pytest.mark.parametrize("url", ["", "ftp://x.example/a", "//x.example/a", "https://", "x.example"])
def test_source_url_must_be_an_absolute_http_url(url: str) -> None:
    validator = CanonicalSignal._url_must_be_absolute_http_or_https
    with pytest.raises(ValueError, match="absolute"):
        validator(url)


@pytest.mark.parametrize("url", ["https://x.example/a", "http://x.example/a"])
def test_source_url_accepts_http_and_https(url: str) -> None:
    validator = CanonicalSignal._url_must_be_absolute_http_or_https
    assert validator(url) == url
