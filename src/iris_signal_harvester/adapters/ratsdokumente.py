"""Adapter for the briefed ``ratsdokumente.de`` REST API (integration Pattern C).

SOURCE STATUS: the briefed endpoint ``https://ratsdokumente.de/api/search`` was
verified NON-EXISTENT on 2026-10-27. The domain does not resolve (NXDOMAIN) and DENIC
reports ``Status: free``. See ``SOURCE_VERIFICATION.md``. OParl (``oparl.py``) is the
justified alternative and the primary live source.

This adapter is nevertheless retained and fixture-tested, so that if such a source is
ever stood up it can be enabled unchanged. It is NOT presented as verified against a
live endpoint: no response from the real service has ever been observed. The response
shape below is the contract as briefed, reconstructed as a plausible Pattern C
offset/limit search API, and is exercised only against mocked responses.

Assumed contract (every point is an assumption, none was observed)::

    GET {base_url}/api/search?offset=<int>&limit=<int>[&q=<text>][&modified_since=<iso>]

    200 application/json
    {
      "total": 123,                      # optional; total hits
      "offset": 0, "limit": 50,
      "results": [
        {"id": "...", "title": "...", "url": "https://...",
         "document_type": "...", "published": "...", "modified": "...",
         "municipality": {"ags": "...", "name": "..."},
         "location": {"x": 0.0, "y": 0.0, "crs": "EPSG:25832"},    # optional
         "licence": "..."}                                          # optional
      ]
    }

Pagination is ``offset``: the next cursor is ``offset + len(results)`` while that is
below ``total`` (or, with no ``total``, while the page came back full). An empty
``results`` array always ends the sweep, so a server that ignores ``offset`` cannot
loop this adapter forever. ``modified_since`` is not part of any verified contract, so
it is declared unsupported (full sweep) unless ``supports_modified_since=True`` is
passed explicitly. Dates are captured raw; ``domain/dates.py`` owns parsing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from iris_signal_harvester.adapters.base import Capabilities, Cursor, Page, malformed_entry
from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import Body, RawDocument
from iris_signal_harvester.errors import MalformedRecord, SourceUnavailable
from iris_signal_harvester.http.client import HarvesterClient

__all__ = ["RatsdokumenteAdapter"]

DEFAULT_BASE_URL = "https://ratsdokumente.de"
MAX_PAGE_SIZE = 100
BODY_ID = "ratsdokumente"


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class RatsdokumenteAdapter:
    """``SourceAdapter`` for the briefed Pattern C search API (unverified; see module docs)."""

    source_system: ClassVar[str] = "ratsdokumente"

    def __init__(
        self,
        client: HarvesterClient,
        *,
        base_url: str = DEFAULT_BASE_URL,
        page_size: int = 50,
        query: str | None = None,
        supports_modified_since: bool = False,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be within 1..{MAX_PAGE_SIZE}, got {page_size}")
        self._client = client
        self._endpoint = f"{base_url.rstrip('/')}/api/search"
        self._page_size = page_size
        self._query = query
        self._supports_modified_since = supports_modified_since

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> RatsdokumenteAdapter:
        return cls(HarvesterClient(settings), **kwargs)

    # -- SourceAdapter ------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            pagination="offset",
            supports_modified_since=self._supports_modified_since,
            supports_conditional_get=False,
            max_page_size=MAX_PAGE_SIZE,
        )

    async def discover_bodies(self) -> AsyncIterator[Body]:
        """One synthetic body: the aggregator is a single endpoint spanning many councils.

        Per-record municipality data stays in the payload for the normaliser.
        """
        yield Body(
            id=BODY_ID,
            name="Ratsdokumente.de federal aggregator",
            paper_endpoint=self._endpoint,
        )

    async def fetch_page(
        self,
        body: Body,
        cursor: Cursor | None,
        *,
        modified_since: datetime | None = None,
    ) -> Page:
        offset = cursor.offset if cursor is not None and cursor.offset is not None else 0
        params: dict[str, str | int] = {"offset": offset, "limit": self._page_size}
        if self._query:
            params["q"] = self._query
        if modified_since is not None and self._supports_modified_since:
            if modified_since.tzinfo is None:
                raise ValueError("modified_since must be timezone-aware; refusing to guess a zone")
            params["modified_since"] = modified_since.astimezone(UTC).isoformat()

        result = await self._client.get_json(self._endpoint, params=params, body_key=body.id)
        if result.not_modified:
            return Page(records=(), next_cursor=None, etag=result.etag, not_modified=True)

        doc = result.data
        if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
            raise SourceUnavailable(f"{self._endpoint}: not a search response (no 'results' array)")
        items: list[Any] = doc["results"]
        records: list[RawDocument] = []
        malformed: list[tuple[str, dict[str, Any]]] = []
        for item in items:
            try:
                records.append(self._to_raw(item, body))
            except MalformedRecord as exc:
                # One unusable record must not discard the rest of its page.
                malformed.append(malformed_entry(exc))

        consumed = offset + len(items)
        total = doc.get("total")
        if not items:
            next_cursor = None
        elif isinstance(total, int) and not isinstance(total, bool):
            next_cursor = Cursor(offset=consumed) if consumed < total else None
        else:
            next_cursor = Cursor(offset=consumed) if len(items) >= self._page_size else None
        return Page(
            records=tuple(records),
            next_cursor=next_cursor,
            etag=result.etag,
            malformed=tuple(malformed),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals ----------------------------------------------------------

    def _to_raw(self, item: Any, body: Body) -> RawDocument:
        if not isinstance(item, dict):
            raise MalformedRecord(
                f"search result is {type(item).__name__}, not an object",
                reason_code="schema_validation_failed",
                raw=item,
            )
        raw_id = item.get("id")
        record_id = str(raw_id).strip() if isinstance(raw_id, (str, int)) else ""
        source_url = _str_or_none(item.get("url"))
        if not record_id or source_url is None:
            raise MalformedRecord(
                "search result lacks 'id' or 'url'",
                reason_code="missing_required_field",
                raw=item,
            )
        licence = _str_or_none(item.get("licence")) or _str_or_none(item.get("license"))
        return RawDocument(
            source_system=self.source_system,
            source_body_id=body.id,
            source_record_id=record_id,
            source_url=source_url,
            payload=item,
            licence=licence,
        )
