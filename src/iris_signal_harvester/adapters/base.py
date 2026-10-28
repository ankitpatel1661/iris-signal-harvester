"""The ``SourceAdapter`` port.

This is the entire contract a new source has to satisfy. It is deliberately
small, because every concern that *can* live above the port — retry, rate
limiting, deduplication, watermarking, validation, loading — does live above it
and is therefore written and tested once rather than per source.

``fetch_page`` is the only method that touches the network. That is what makes
the fixture adapter an honest substitute rather than a stub: it returns the same
``Page`` objects from recorded cassettes, so the sweep logic, guard rails,
normalisation, matching, geo-resolution and load exercised in the offline demo
are the production code paths.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from iris_signal_harvester.domain.models import Body, RawDocument
from iris_signal_harvester.errors import MalformedRecord

__all__ = [
    "Capabilities",
    "Cursor",
    "Page",
    "PaginationStyle",
    "SourceAdapter",
    "malformed_entry",
]

PaginationStyle = Literal["offset", "page", "link_header", "object_link", "none"]


@dataclass(frozen=True, slots=True)
class Cursor:
    """Opaque forward pointer. The adapter interprets it; the orchestrator never does.

    The orchestrator only ever compares cursors for equality (to detect a loop)
    and passes them back unmodified.
    """

    token: str | None = None
    page: int | None = None
    offset: int | None = None

    def fingerprint(self) -> str:
        return f"{self.token}|{self.page}|{self.offset}"


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a source can actually do, declared rather than assumed.

    ``supports_modified_since`` decides whether incremental harvesting is
    possible at all; where it is False the orchestrator must fall back to a full
    sweep and say so in the report rather than silently harvesting everything.
    """

    pagination: PaginationStyle
    supports_modified_since: bool
    supports_conditional_get: bool
    max_page_size: int
    oparl_versions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Page:
    """Exactly one page of results.

    A page with ``records == ()`` and ``next_cursor is None`` is a legitimate
    end-of-collection. A truncated or unparseable read is an exception, never an
    empty page — conflating the two produces a false "0 records, all good" run.

    ``malformed`` holds the individual records of this page that could not become a
    ``RawDocument`` (for example no usable identifier), as ``(reason_code, raw_payload)``
    pairs. One bad record must not discard the rest of its page, so the adapter
    reports it here instead of raising. Adapters know nothing about a
    ``harvest_run_id``, so they cannot build a ``RejectedRecord`` themselves: the
    pipeline converts each pair into a ``RejectedRecord`` (attaching the run id, the
    source system and the body) and dead-letters it. ``reason_code`` is a
    ``RejectCode`` value. A page-level failure (the body is not JSON, not a
    collection) is still an exception, never an entry here.
    """

    records: Sequence[RawDocument]
    next_cursor: Cursor | None = None
    etag: str | None = None
    not_modified: bool = False
    malformed: Sequence[tuple[str, dict[str, Any]]] = ()

    def id_fingerprint(self) -> str:
        """Stable hash of everything this page returned, records and malformed alike.

        The orchestrator compares consecutive fingerprints to detect a server that
        hands out fresh-looking cursors while re-serving the same page. Malformed
        entries have to be included: a page consisting *only* of malformed records has
        no ids at all, so fingerprinting the records alone would make every such page
        hash identically to the empty page — two different all-malformed pages would
        look the same and falsely trip the detector. Each entry is folded in as
        ``reason_code`` plus its canonically serialised payload, so the fingerprint is
        stable across runs and across key ordering.
        """
        import hashlib
        import json

        parts = sorted(r.source_record_id for r in self.records)
        parts += sorted(
            f"malformed\u001f{code}\u001f"
            + json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
            for code, payload in self.malformed
        )
        joined = "\u0000".join(parts)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def malformed_entry(exc: MalformedRecord) -> tuple[str, dict[str, Any]]:
    """Turn a per-record ``MalformedRecord`` into a ``Page.malformed`` entry.

    A raw value that is not a JSON object cannot be stored as a payload dict as-is,
    so it is wrapped under ``"value"``; nothing is dropped.
    """
    raw = exc.raw
    payload = raw if isinstance(raw, dict) else {"value": raw}
    return (exc.reason_code, payload)


@runtime_checkable
class SourceAdapter(Protocol):
    """A source of council documents.

    Implementations must raise ``SourceUnavailable`` for retryable failures and
    ``AccessRestricted`` for terminal ones. They must never return a partial page
    silently.
    """

    source_system: ClassVar[str]

    def capabilities(self) -> Capabilities:
        """Declare what this source supports. Called once per run, before sweeping."""
        ...

    def discover_bodies(self) -> AsyncIterator[Body]:
        """Yield the councils this source exposes.

        A single-endpoint source yields exactly one; the OParl adapter yields one
        per registered body.
        """
        ...

    async def fetch_page(
        self,
        body: Body,
        cursor: Cursor | None,
        *,
        modified_since: datetime | None = None,
    ) -> Page:
        """Fetch exactly one page for ``body``.

        Raises:
            SourceUnavailable: transient failure; the caller may retry.
            AccessRestricted: terminal; the caller must stop on this source.
        """
        ...

    async def aclose(self) -> None:
        """Release any transport resources."""
        ...
