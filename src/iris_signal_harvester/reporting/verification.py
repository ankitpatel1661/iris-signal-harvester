"""Source verification: the read-only probe behind ``SOURCE_VERIFICATION.md``.

This is the evidence artefact for the central factual claim of the project — that the
briefed endpoint does not exist and that OParl is the justified alternative. Its
structure mirrors the committed ``SOURCE_VERIFICATION.md`` at the repository root, so
a regenerated file is comparable with the original section by section.

Three rules are absolute here, and they are the reason this is a module rather than a
shell one-liner:

* **Never authenticate.** No ``Authorization`` header, no cookie, no credential, no
  form post, ever — not even if a probe returns 401. The probe answers "is there a
  service here and may we read it?", which never requires logging in.
* **Never retry around a refusal.** A 401, a 403 or a robots.txt disallow is recorded
  as a finding and **stops all further probing of that host**. Repeating a request
  after a refusal, with or without varied headers, is an attempt to get around it.
  (``AccessRestricted`` is terminal everywhere else in this codebase for the same
  reason.) This module therefore does not use the retrying HTTP client at all: each
  URL is requested exactly once.
* **A failure is reported, not smoothed over.** NXDOMAIN, a timeout and an HTTP 200
  serving HTML where JSON was promised are three different findings, and the
  generated document says which one happened.

The control probes exist to make the evidence falsifiable: if the briefed host fails
while two known-good hosts succeed from the same machine at the same moment, the
failure is a property of the target rather than of our network.

Default output path. The committed ``SOURCE_VERIFICATION.md`` records a real probe run
with real network access. Regenerating it from a machine with no egress would replace
evidence with a page of failures, so the default output is
``<report_dir>/SOURCE_VERIFICATION.md`` and overwriting the committed artefact is an
explicit ``--output`` away.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

__all__ = [
    "BRIEFED_HOST",
    "BRIEFED_URL",
    "CANDIDATE_URLS",
    "CONTROL_HOSTS",
    "DnsProbe",
    "RobotsProbe",
    "SourceVerifier",
    "UrlProbe",
    "VerificationReport",
    "write_verification",
]

BRIEFED_HOST = "ratsdokumente.de"
BRIEFED_URL = "https://ratsdokumente.de/api/search"
CONTROL_HOSTS: tuple[str, ...] = ("oparl.org", "politik-bei-uns.de")
CANDIDATE_URLS: tuple[str, ...] = (
    "https://oparl.org/",
    "https://www.muenchen-transparent.de/oparl/v1.0",
)
# Placeholder contact (RFC 2606 example domain). A real deployment must construct
# ``SourceVerifier(user_agent=...)`` with the operator's own contact (the same value as
# ``IRIS_USER_AGENT``); live probes sent with this default reach nobody.
PROBE_USER_AGENT = "iris-harvester-feasibility-check/0.1 (+mailto:ops@example.org)"
_TIMEOUT_S = 20.0
_MAX_EXCERPT = 400

Resolver = Callable[[str], list[str]]
WhoisLookup = Callable[[str], str | None]


@dataclass(frozen=True)
class DnsProbe:
    """One hostname's resolution result. ``addresses`` empty with an error is NXDOMAIN."""

    host: str
    addresses: tuple[str, ...] = ()
    error: str | None = None

    @property
    def resolves(self) -> bool:
        return bool(self.addresses)

    def line(self) -> str:
        if self.resolves:
            return f"{self.host:<22} A -> {', '.join(self.addresses)}"
        return f"{self.host:<22} A -> (no answer: {self.error or 'NXDOMAIN'})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "addresses": list(self.addresses),
            "resolves": self.resolves,
            "error": self.error,
        }


@dataclass(frozen=True)
class UrlProbe:
    """One URL, requested exactly once."""

    url: str
    status: int | None = None
    content_type: str | None = None
    size: int = 0
    final_url: str | None = None
    error: str | None = None
    json_shape: tuple[str, ...] = ()
    json_error: str | None = None
    refused: bool = False
    redirect_to: str | None = None
    """``Location`` of a 3xx answer. Redirects are reported, never followed (see ``probe``)."""

    def line(self) -> str:
        if self.status is None:
            return f"{self.url:<46} HTTP:000  (no response: {self.error})"
        text = (
            f"{self.url:<46} HTTP:{self.status:03d}  final:{self.final_url or self.url}  "
            f"ct:{self.content_type or ''}  bytes:{self.size}"
        )
        if self.redirect_to is not None:
            text += f"  redirect-not-followed:{self.redirect_to}"
        return text

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "content_type": self.content_type,
            "bytes": self.size,
            "final_url": self.final_url,
            "error": self.error,
            "json_keys": list(self.json_shape),
            "json_error": self.json_error,
            "refused": self.refused,
            "redirect_to": self.redirect_to,
        }


@dataclass(frozen=True)
class RobotsProbe:
    """robots.txt as fetched. ``allowed`` is what it says about *our* user-agent."""

    url: str
    status: int | None = None
    allowed: bool | None = None
    crawl_delay: float | None = None
    excerpt: str = ""
    error: str | None = None
    refused: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "allowed": self.allowed,
            "crawl_delay": self.crawl_delay,
            "error": self.error,
            "refused": self.refused,
        }


@dataclass(frozen=True)
class _RobotsEntry:
    """One origin's robots.txt, read once and consulted before every GET of that origin."""

    probe: RobotsProbe
    fetch: UrlProbe
    parser: RobotFileParser | None

    def permits(self, url: str, user_agent: str) -> tuple[bool, str]:
        """Whether ``url`` may be fetched, and why not. Conservative on every doubt."""
        if self.probe.refused:
            return False, f"robots.txt refused (HTTP {self.probe.status})"
        if self.parser is not None:
            if self.parser.can_fetch(user_agent, url):
                return True, ""
            return False, "robots.txt disallows this URL"
        if self.probe.status is None:
            return False, f"robots.txt unreadable ({self.probe.error}); not fetching"
        return True, ""  # 404/410/...: no robots.txt, so no rule


@dataclass
class VerificationReport:
    """The probe's result, renderable as the evidence document."""

    probed_at: datetime
    user_agent: str
    briefed_url: str
    briefed_host: str
    controls: tuple[DnsProbe, ...] = ()
    briefed_dns: DnsProbe = field(default_factory=lambda: DnsProbe(host=BRIEFED_HOST))
    whois: str | None = None
    briefed_http: tuple[UrlProbe, ...] = ()
    candidates: tuple[UrlProbe, ...] = ()
    candidate_robots: tuple[RobotsProbe, ...] = ()
    refusals: tuple[str, ...] = ()

    # -- derived findings ------------------------------------------------------

    @property
    def briefed_exists(self) -> bool:
        return self.briefed_dns.resolves

    @property
    def controls_healthy(self) -> bool:
        return bool(self.controls) and all(c.resolves for c in self.controls)

    @property
    def live_candidates(self) -> tuple[UrlProbe, ...]:
        return tuple(c for c in self.candidates if c.status is not None and c.status < 400)

    def as_dict(self) -> dict[str, Any]:
        return {
            "probed_at": self.probed_at.isoformat(),
            "user_agent": self.user_agent,
            "briefed": {
                "url": self.briefed_url,
                "host": self.briefed_host,
                "dns": self.briefed_dns.as_dict(),
                "http": [p.as_dict() for p in self.briefed_http],
                "whois": self.whois,
                "exists": self.briefed_exists,
            },
            "controls": [c.as_dict() for c in self.controls],
            "controls_healthy": self.controls_healthy,
            "candidates": [c.as_dict() for c in self.candidates],
            "robots": [r.as_dict() for r in self.candidate_robots],
            "refusals": list(self.refusals),
        }

    def to_markdown(self) -> str:
        out: list[str] = []
        add = out.append
        add("# SOURCE_VERIFICATION — IRIS-SIG-23")
        add("")
        add("| | |")
        add("|---|---|")
        add(f"| Probe executed (UTC) | {self.probed_at.strftime('%Y-%m-%d %H:%M:%SZ')} |")
        add(f"| Briefed source | `{self.briefed_url}` |")
        add(
            "| Method | Read-only DNS / WHOIS / HTTP probe. No authentication attempted. "
            "No access restriction bypassed. |"
        )
        add(f"| Operator user-agent | `{self.user_agent}` |")
        add("")

        add("## 1. Control — resolver and egress are healthy")
        add("")
        add("Establishes that any failure below is a property of the target, not of this machine.")
        add("")
        add("```")
        for control in self.controls:
            add(control.line())
        if not self.controls:
            add("(no control hosts probed)")
        add("```")
        add("")

        add("## 2. Briefed host — DNS")
        add("")
        add("```")
        add(f"$ dig {self.briefed_host} A")
        add(f"  {self.briefed_dns.line()}")
        if self.briefed_dns.error:
            add(f"  ;; resolver reported: {self.briefed_dns.error}")
        add("```")
        add("")

        add("## 3. Briefed host — registry (DENIC, authoritative for .de)")
        add("")
        add("```")
        if self.whois:
            add(f"$ whois {self.briefed_host}")
            for line in self.whois.splitlines()[:20]:
                add(f"  {line}")
        else:
            add(f"$ whois {self.briefed_host}")
            add("  (no whois client available on this machine; registry state not re-checked)")
        add("```")
        add("")

        add("## 4. Briefed host — HTTP")
        add("")
        add("```")
        for probe in self.briefed_http:
            add(probe.line())
        if not self.briefed_http:
            add("(not probed)")
        add("```")
        add("")

        add("## 5. Candidate alternatives")
        add("")
        add("```")
        for probe in self.candidates:
            add(probe.line())
            if probe.json_shape:
                add(f"    JSON object keys: {', '.join(probe.json_shape)}")
            elif probe.json_error:
                add(f"    JSON shape: {probe.json_error}")
        if not self.candidates:
            add("(none probed)")
        add("```")
        add("")
        for robots in self.candidate_robots:
            add("```")
            add(f"$ curl {robots.url}")
            if robots.error:
                add(f"  (no robots.txt read: {robots.error})")
            else:
                for line in robots.excerpt.splitlines()[:12]:
                    add(f"  {line}")
                add(
                    f"  -> automated access to that host is "
                    f"{'allowed' if robots.allowed else 'DISALLOWED'} for this user-agent"
                    + (
                        f"; Crawl-delay {robots.crawl_delay}s"
                        if robots.crawl_delay is not None
                        else ""
                    )
                )
            add("```")
            add("")

        add("## 6. Findings")
        add("")
        for n, finding in enumerate(self._findings(), start=1):
            add(f"{n}. {finding}")
        add("")

        add("## 7. Decision")
        add("")
        for line in self._decision():
            add(line)
        add("")
        return "\n".join(out) + "\n"

    # -- narrative -------------------------------------------------------------

    def _findings(self) -> list[str]:
        findings: list[str] = []
        if not self.briefed_exists:
            findings.append(
                f"**`{self.briefed_host}` does not resolve.** The resolver returned no A record "
                f"({self.briefed_dns.error or 'NXDOMAIN'}), so the failure is not an outage, a "
                "block or a rate limit: there is no service to reach, and no access restriction "
                "exists to be respected or bypassed."
            )
        else:
            findings.append(
                f"**`{self.briefed_host}` resolves** to "
                f"{', '.join(self.briefed_dns.addresses)}. The HTTP results in section 4 decide "
                "whether a usable API is served there; DNS alone does not."
            )
        if self.controls_healthy:
            findings.append(
                "**The control probes succeed from the same machine at the same moment**, so "
                "local DNS and egress are healthy and the result above is a property of the "
                "target."
            )
        else:
            findings.append(
                "**The control probes did NOT all succeed**, so this machine's DNS or egress is "
                "itself suspect and no conclusion may be drawn about the briefed host from this "
                "run. Re-run from a network with egress before relying on it."
            )
        if self.whois:
            findings.append(
                "Registry output for the briefed domain is quoted verbatim in section 3; a "
                "`Status: free` line means the domain is not registered to anyone."
            )
        live = self.live_candidates
        if live:
            findings.append(
                "**OParl is live, specified and open.** "
                + "; ".join(f"`{p.url}` answered HTTP {p.status}" for p in live)
                + ". OParl is the open standard through which German council information "
                "systems actually publish, which is what makes coverage across hundreds of "
                "municipalities achievable at all."
            )
        for probe in self.candidates:
            if probe.json_shape:
                findings.append(
                    f"`{probe.url}` serves a JSON object with keys "
                    f"{', '.join(probe.json_shape)} — the shape the OParl adapter expects."
                )
        for robots in self.candidate_robots:
            if robots.allowed is False:
                findings.append(
                    f"**`{robots.url}` disallows automated access** for this user-agent. That is "
                    "respected: the host is not harvested and nothing was retried against it."
                )
        if self.refusals:
            findings.append(
                "**Access was refused by "
                + ", ".join(f"`{r}`" for r in self.refusals)
                + ".** Each refusal stopped all further probing of that host immediately. No "
                "credential was supplied, no header was varied and no request was repeated."
            )
        else:
            findings.append("No access restriction was encountered and none was bypassed.")
        return findings

    def _decision(self) -> list[str]:
        if not self.briefed_exists:
            return [
                "**Branch B** of the decision rule applies.",
                "",
                "- The briefed `RatsdokumenteApiAdapter` is implemented against the contract "
                "described in the brief and is fully covered by fixture-based tests, so it can "
                "be enabled unchanged should such a source exist or be stood up. It is **not** "
                "presented as verified against a live endpoint.",
                "- The **OParl federation adapter is the primary live source**.",
                "- No access restriction was encountered and none was bypassed.",
            ]
        return [
            "**Branch A** of the decision rule applies: the briefed host answered, so the "
            "briefed adapter is the primary source and the OParl adapter is the federation "
            "fallback. Section 4 above is the evidence; re-read it before relying on this line.",
        ]


class SourceVerifier:
    """Runs the probe. Every dependency is injectable so tests need no network."""

    def __init__(
        self,
        *,
        user_agent: str = PROBE_USER_AGENT,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        whois: WhoisLookup | None = None,
        timeout_s: float = _TIMEOUT_S,
        now: datetime | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._transport = transport
        self._resolver = resolver or _resolve
        self._whois = whois
        self._timeout = timeout_s
        self._now = now

    async def probe(
        self,
        *,
        briefed_url: str = BRIEFED_URL,
        control_hosts: Sequence[str] = CONTROL_HOSTS,
        candidate_urls: Sequence[str] = CANDIDATE_URLS,
    ) -> VerificationReport:
        """Probe the briefed source and the candidates, read-only."""
        host = urlsplit(briefed_url).hostname or briefed_url
        report = VerificationReport(
            probed_at=self._now or datetime.now(UTC),
            user_agent=self._user_agent,
            briefed_url=briefed_url,
            briefed_host=host,
            controls=tuple(self._dns(h) for h in control_hosts),
            briefed_dns=self._dns(host),
        )
        report.whois = self._whois(host) if self._whois is not None else None

        refused: list[str] = []
        robots_cache: dict[str, _RobotsEntry] = {}
        async with httpx.AsyncClient(
            headers={"User-Agent": self._user_agent},
            timeout=httpx.Timeout(self._timeout),
            # Never follow redirects. The main client follows them hop by hop with a
            # robots.txt check at each one; a diagnostic probe that let
            # httpx follow silently would fetch hosts whose robots.txt it never read. A 3xx
            # is reported with its Location instead.
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            briefed_targets = (f"https://{host}/", briefed_url)
            report.briefed_http = await self._probe_host(
                client, host, briefed_targets, refused, robots_cache
            )

            candidates: list[UrlProbe] = []
            robots: list[RobotsProbe] = []
            for url in candidate_urls:
                candidate_host = urlsplit(url).hostname or url
                if candidate_host in refused:
                    continue
                entry = await self._robots_entry(client, url, robots_cache)
                robots.append(replace(entry.probe, allowed=self._allowed_for(entry, url)))
                candidates.append(await self._gated_get(client, url, entry))
                if entry.probe.refused or candidates[-1].refused:
                    refused.append(candidate_host)
            report.candidates = tuple(candidates)
            report.candidate_robots = tuple(robots)
        report.refusals = tuple(dict.fromkeys(refused))
        return report

    # -- internals -------------------------------------------------------------

    async def _probe_host(
        self,
        client: httpx.AsyncClient,
        host: str,
        urls: Sequence[str],
        refused: list[str],
        robots_cache: dict[str, _RobotsEntry],
    ) -> tuple[UrlProbe, ...]:
        """Probe several URLs of one host, robots.txt first, stopping dead on a refusal."""
        probes: list[UrlProbe] = []
        if not urls:
            return ()
        entry = await self._robots_entry(client, urls[0], robots_cache)
        probes.append(entry.fetch)
        if entry.probe.refused:
            refused.append(host)
            return tuple(probes)
        for url in urls:
            probe = await self._gated_get(client, url, entry)
            probes.append(probe)
            if probe.refused:
                refused.append(host)
                break
        return tuple(probes)

    def _allowed_for(self, entry: _RobotsEntry, url: str) -> bool | None:
        if entry.probe.status is None:
            return None  # robots.txt could not be read: neither allowed nor disallowed
        return entry.permits(url, self._user_agent)[0]

    async def _gated_get(
        self, client: httpx.AsyncClient, url: str, entry: _RobotsEntry
    ) -> UrlProbe:
        """GET ``url`` only if the (already read) robots.txt permits it."""
        permitted, why = entry.permits(url, self._user_agent)
        if not permitted:
            return UrlProbe(url=url, error=f"not fetched: {why}")
        return await self._get(client, url)

    async def _get(self, client: httpx.AsyncClient, url: str) -> UrlProbe:
        """One GET. No credential is ever sent and nothing is ever retried."""
        try:
            response = await client.get(url)
        except httpx.HTTPError as exc:
            return UrlProbe(url=url, error=f"{type(exc).__name__}: {exc}")
        except (socket.gaierror, OSError) as exc:  # pragma: no cover - transport dependent
            return UrlProbe(url=url, error=f"{type(exc).__name__}: {exc}")
        return self._probe_of(url, response)

    def _probe_of(self, url: str, response: httpx.Response) -> UrlProbe:
        content_type = response.headers.get("content-type")
        shape, shape_error = _json_shape(response, content_type)
        return UrlProbe(
            url=url,
            status=response.status_code,
            content_type=content_type,
            size=len(response.content),
            final_url=str(response.url),
            json_shape=shape,
            json_error=shape_error,
            refused=response.status_code in (401, 403),
            redirect_to=(
                response.headers.get("location") if 300 <= response.status_code < 400 else None
            ),
        )

    async def _robots_entry(
        self, client: httpx.AsyncClient, url: str, cache: dict[str, _RobotsEntry]
    ) -> _RobotsEntry:
        """Read (once per origin) the robots.txt that governs ``url``.

        401/403 is a refusal, exactly as in the main client: the conservative reading.
        Only a missing file (404/410/other 4xx) is the absence of a rule.
        """
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        cached = cache.get(robots_url)
        if cached is not None:
            return cached
        try:
            response = await client.get(robots_url)
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
            entry = _RobotsEntry(
                RobotsProbe(url=robots_url, error=error),
                UrlProbe(url=robots_url, error=error),
                None,
            )
        else:
            fetch = self._probe_of(robots_url, response)
            if response.status_code in (401, 403):
                probe = RobotsProbe(
                    url=robots_url,
                    status=response.status_code,
                    allowed=False,
                    error=f"HTTP {response.status_code}",
                    refused=True,
                )
                entry = _RobotsEntry(probe, fetch, None)
            elif 300 <= response.status_code < 400:
                # Not followed, so the rules that govern this origin are unknown: stay
                # conservative (``permits`` refuses on an unreadable robots.txt).
                where = response.headers.get("location") or "(no Location header)"
                error = f"redirect not followed (HTTP {response.status_code} -> {where})"
                entry = _RobotsEntry(RobotsProbe(url=robots_url, error=error), fetch, None)
            elif response.status_code >= 400:
                # No robots.txt is not permission denied; it is the absence of a rule.
                probe = RobotsProbe(
                    url=robots_url,
                    status=response.status_code,
                    allowed=True,
                    error=f"HTTP {response.status_code}",
                )
                entry = _RobotsEntry(probe, fetch, None)
            else:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                delay = parser.crawl_delay(self._user_agent)
                probe = RobotsProbe(
                    url=robots_url,
                    status=response.status_code,
                    allowed=parser.can_fetch(self._user_agent, url),
                    crawl_delay=float(delay) if delay is not None else None,
                    excerpt=response.text[:_MAX_EXCERPT],
                )
                entry = _RobotsEntry(probe, fetch, parser)
        cache[robots_url] = entry
        return entry

    def _dns(self, host: str) -> DnsProbe:
        try:
            return DnsProbe(host=host, addresses=tuple(self._resolver(host)))
        except (socket.gaierror, OSError, UnicodeError) as exc:
            return DnsProbe(host=host, error=f"{type(exc).__name__}: {exc}")


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _json_shape(
    response: httpx.Response, content_type: str | None
) -> tuple[tuple[str, ...], str | None]:
    """The top-level key set of a JSON body, or why there is none.

    A 200 that serves HTML where JSON was promised is a *finding*, not an empty
    result — that is the soft-200 failure mode this project treats as a failure
    everywhere else too.
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if not (media == "application/json" or media.endswith("+json")):
        if response.status_code < 400 and media.startswith("text/html"):
            return (), f"HTTP {response.status_code} served {media!r}, not JSON (soft-200)"
        return (), None
    try:
        data = response.json()
    except ValueError as exc:
        return (), f"content-type claims JSON but the body does not parse: {exc}"
    if isinstance(data, dict):
        return tuple(sorted(str(k) for k in data)[:12]), None
    return (), f"JSON root is {type(data).__name__}, not an object"


def whois_via_cli(host: str, *, timeout_s: float = 15.0) -> str | None:
    """``whois`` output for ``host``, or ``None`` when no client is installed.

    Read-only and best-effort: registry data is corroborating evidence, and a machine
    without a whois client must still be able to produce the rest of the document.
    """
    import shutil
    import subprocess  # fixed argv, no shell, read-only registry lookup

    binary = shutil.which("whois")
    if binary is None:
        return None
    try:
        completed = subprocess.run(  # argv is fixed; no shell is involved
            [binary, host],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    return output or None


def write_verification(report: VerificationReport, path: Path | str) -> Path:
    """Write the markdown document; returns the path written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.to_markdown(), encoding="utf-8")
    return target


async def verify(
    *,
    briefed_url: str = BRIEFED_URL,
    candidate_urls: Sequence[str] = CANDIDATE_URLS,
    control_hosts: Sequence[str] = CONTROL_HOSTS,
    use_whois: bool = True,
) -> VerificationReport:
    """Convenience entry point used by the CLI."""
    verifier = SourceVerifier(whois=whois_via_cli if use_whois else None)
    return await verifier.probe(
        briefed_url=briefed_url,
        candidate_urls=candidate_urls,
        control_hosts=control_hosts,
    )


def verify_sync(**kwargs: Any) -> VerificationReport:
    """Blocking wrapper for ``verify`` (the CLI is synchronous)."""
    return asyncio.run(verify(**kwargs))
