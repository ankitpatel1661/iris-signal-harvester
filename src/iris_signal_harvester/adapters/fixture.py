"""Fixture adapter: offline replay of committed JSON cassettes.

This is the demo path and needs no network. It is *not* a stub that fabricates
``Page`` objects: it runs the real :class:`OParlAdapter` and the real
:class:`HarvesterClient` (retry, 429/5xx mapping, soft-200 detection, JSON
validation) over an ``httpx.MockTransport`` that serves recorded HTTP exchanges. The
pages that come out are therefore produced by the production code path, and a cassette
that records a 429, a 500, truncated JSON or an HTML maintenance page provokes exactly
the exceptions a live source would.

Cassette format (``cassette_version`` 1)::

    {
      "cassette_version": 1,
      "scenario": "happy_path",
      "description": "...",
      "system_url": "https://oparl.example.org/oparl/v1.0",
      "interactions": [
        {"request":  {"method": "GET", "url": "https://..."},
         "response": {"status": 200, "headers": {...}, "json": {...}}}
      ]
    }

``response`` carries either ``json`` (serialised, ``application/json`` by default) or
``text`` (sent verbatim; set ``Content-Type`` in ``headers``).

Matching and ordering. A request matches on host, path and query, ignoring
``modified_since`` (a replay cannot filter, so incremental behaviour is not
simulated). Interactions with the same key are served in file order and the last one
repeats, so ``[429, 200]`` is "throttled once, then fine" and a lone ``500`` is
"always down". A request with no recording gets a 404, which surfaces as
``UnexpectedResponse``. Replay state lives on the adapter instance, so two fresh
instances over the same cassette behave identically.

Records are emitted with ``source_system == "fixture"``, never ``"oparl"``, so demo
data cannot collide with live data under the ``(source_system, source_body_id,
source_record_id)`` natural key.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlsplit

import httpx

from iris_signal_harvester.adapters.oparl import OParlAdapter
from iris_signal_harvester.config import Settings
from iris_signal_harvester.errors import ConfigurationError
from iris_signal_harvester.http.client import HarvesterClient

__all__ = ["CASSETTE_VERSION", "CassetteReplay", "FixtureAdapter", "load_cassette"]

CASSETTE_VERSION = 1
_IGNORED_QUERY_KEYS = frozenset({"modified_since"})
_Key = tuple[str, str, tuple[tuple[str, str], ...]]


def _key(url: str) -> _Key:
    parts = urlsplit(url)
    query = tuple(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _IGNORED_QUERY_KEYS
        )
    )
    return (parts.hostname or "", parts.path, query)


def load_cassette(path: Path) -> dict[str, Any]:
    """Read and structurally validate one cassette."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read cassette {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("cassette_version") != CASSETTE_VERSION:
        raise ConfigurationError(f"cassette {path}: unsupported or missing cassette_version")
    if not isinstance(doc.get("system_url"), str):
        raise ConfigurationError(f"cassette {path}: missing system_url")
    interactions = doc.get("interactions")
    if not isinstance(interactions, list) or not interactions:
        raise ConfigurationError(f"cassette {path}: no interactions")
    for entry in interactions:
        try:
            url = entry["request"]["url"]
            status = entry["response"]["status"]
        except (KeyError, TypeError) as exc:
            raise ConfigurationError(f"cassette {path}: malformed interaction {entry!r}") from exc
        if not isinstance(url, str) or not isinstance(status, int):
            raise ConfigurationError(f"cassette {path}: malformed interaction {entry!r}")
    return doc


class CassetteReplay:
    """Serves a cassette's recorded responses. Usable as an ``httpx`` mock handler."""

    def __init__(self, interactions: Iterable[dict[str, Any]]) -> None:
        self._queues: dict[_Key, list[dict[str, Any]]] = {}
        for entry in interactions:
            self._queues.setdefault(_key(entry["request"]["url"]), []).append(entry["response"])
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        queue = self._queues.get(_key(url))
        if not queue:
            return httpx.Response(
                404,
                json={"error": "no recorded interaction", "url": url},
            )
        recorded = queue.pop(0) if len(queue) > 1 else queue[0]
        return self._build(recorded)

    @staticmethod
    def _build(recorded: dict[str, Any]) -> httpx.Response:
        headers = dict(recorded.get("headers") or {})
        status = int(recorded["status"])
        if "json" in recorded:
            headers.setdefault("Content-Type", "application/json")
            return httpx.Response(
                status,
                headers=headers,
                content=json.dumps(recorded["json"], ensure_ascii=False).encode("utf-8"),
            )
        return httpx.Response(
            status, headers=headers, content=str(recorded.get("text", "")).encode("utf-8")
        )


async def _no_sleep(_seconds: float) -> None:
    """Replays never wait: backoff and rate-limit delays are skipped."""


class FixtureAdapter(OParlAdapter):
    """``SourceAdapter`` that replays one cassette through the production OParl path."""

    source_system: ClassVar[str] = "fixture"

    def __init__(
        self,
        settings: Settings,
        scenario: str = "happy_path",
        *,
        fixture_dir: Path | None = None,
    ) -> None:
        directory = fixture_dir if fixture_dir is not None else settings.fixture_dir
        path = directory / f"{scenario}.json"
        if not path.is_file():
            raise ConfigurationError(
                f"no cassette {scenario!r} in {directory} "
                f"(available: {', '.join(self.available_scenarios(directory)) or 'none'})"
            )
        cassette = load_cassette(path)
        self.scenario = scenario
        self.description = str(cassette.get("description", ""))
        self.replay = CassetteReplay(cassette["interactions"])
        # No network and no robots.txt for a replay; delays are skipped; jitter is seeded.
        offline = settings.model_copy(update={"respect_robots": False})
        client = HarvesterClient(
            offline,
            transport=httpx.MockTransport(self.replay.handler),
            sleep=_no_sleep,
            rng=random.Random(0),
        )
        super().__init__(client, cassette["system_url"])

    @staticmethod
    def available_scenarios(fixture_dir: Path) -> list[str]:
        """Names of the cassettes in ``fixture_dir``, sorted."""
        if not fixture_dir.is_dir():
            return []
        return sorted(p.stem for p in fixture_dir.glob("*.json"))
