"""OParl adapter: the primary live source.

Traversal is System -> ``body`` list -> per-body ``paper`` list. Shape and behaviour
below are taken from a real probe of ``https://www.muenchen-transparent.de/oparl/v1.0``
(26 bodies, OParl 1.0), not from the specification alone:

* Pagination is ``links.next`` (an absolute URL on the object itself), so
  ``Capabilities.pagination == "object_link"``. ``pagination.totalPages`` is ignored:
  the only reliable signal that a collection has ended is the absence of ``next``.
* ``modified_since`` is honoured as a query parameter. It is applied to *every*
  request of a sweep, including ``links.next`` URLs, because the server's ``next``
  link is not guaranteed to carry it forward.
* ``Body.license`` is frequently ``null``; it flows through as ``Body.licence = None``
  and ``RawDocument.licence = None``. It is never inferred from a sibling body.
* ``Paper.date`` is frequently ``null`` and ``Paper.modified`` can be the corrupt
  sentinel ``-0001-11-30T00:00:00+00:53``. Both are captured **raw** in
  ``RawDocument.payload``. Nothing here parses a date: ``domain/dates.py`` owns that,
  including the sentinel and the provenance ladder.
* ``Paper`` carries no ``location`` on this endpoint. The adapter neither requires nor
  fabricates one; whatever the server sent is preserved verbatim in the payload.
* Both ``https://schema.oparl.org/1.0/`` and ``/1.1/`` type URLs are accepted. Any
  other version is refused with :class:`UnsupportedOParlVersion` rather than parsed
  on the hope that it is compatible.

Failure semantics follow the ``Page`` contract: a transport, status or JSON failure
is an exception from the HTTP client, never an empty page. A 2xx JSON document whose
shape is not an OParl collection is also an exception (``SourceUnavailable``). A single
paper that cannot yield an identifier (or is not an object) does not fail its page: it
is reported in ``Page.malformed`` and the remaining papers are returned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog

from iris_signal_harvester.adapters.base import Capabilities, Cursor, Page, malformed_entry
from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import Body, RawDocument
from iris_signal_harvester.errors import ConfigurationError, MalformedRecord, SourceUnavailable
from iris_signal_harvester.http.client import HarvesterClient, UnexpectedResponse

__all__ = [
    "ACCEPTED_SCHEMA_PREFIXES",
    "OParlAdapter",
    "UnsupportedOParlVersion",
    "record_id_from",
]

log = structlog.get_logger(__name__)

ACCEPTED_SCHEMA_PREFIXES: tuple[str, ...] = (
    "https://schema.oparl.org/1.0/",
    "https://schema.oparl.org/1.1/",
)
MAX_PAGE_SIZE = 100


class UnsupportedOParlVersion(UnexpectedResponse):
    """The server speaks an OParl version (or a type) this adapter does not accept.

    Terminal: retrying cannot change the version the server advertises.
    """


def _accepted_type(type_url: str, kind: str) -> bool:
    return any(type_url == f"{prefix}{kind}" for prefix in ACCEPTED_SCHEMA_PREFIXES)


def _check_type(obj: dict[str, Any], kind: str, *, url: str, required: bool) -> None:
    """Accept 1.0 and 1.1 type URLs for ``kind``; refuse anything else that is declared."""
    declared = obj.get("type")
    if declared is None:
        if required:
            raise UnsupportedOParlVersion(
                f"{url}: missing 'type'; not an OParl {kind} object", status=200, url=url
            )
        return
    if not isinstance(declared, str) or not _accepted_type(declared, kind):
        raise UnsupportedOParlVersion(
            f"{url}: type {declared!r} is not an accepted OParl 1.0/1.1 {kind}",
            status=200,
            url=url,
        )


def record_id_from(item: dict[str, Any]) -> str:
    """The natural-key record id: ``Paper.id``, the dereferenceable URL, verbatim.

    OParl's ``reference`` is the human file number (Aktenzeichen / Drucksachennummer).
    It is editable and is not guaranteed unique on every system, so keying on it
    would merge two distinct papers or split one renumbered paper in two. ``id`` is the
    object's identity, so it is the key; ``reference`` stays available as an attribute
    in ``raw_payload``.

    Raises:
        MalformedRecord: ``id`` is missing or blank. A record with no identity cannot be
            a ``RawDocument``, and inventing an id would break the natural key.
    """
    ident = item.get("id")
    if isinstance(ident, str) and ident.strip():
        return ident.strip()
    raise MalformedRecord(
        "paper has no usable 'id'; OParl 'reference' is not an identity and is not used as a key",
        reason_code="missing_required_field",
        raw=item,
    )


def _with_modified_since(url: str, since: datetime | None) -> str:
    """Add ``modified_since`` to ``url`` unless it is already present."""
    if since is None:
        return url
    if since.tzinfo is None:
        raise ValueError("modified_since must be timezone-aware; refusing to guess a zone")
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "modified_since" for key, _ in query):
        return url
    query.append(("modified_since", since.astimezone(UTC).isoformat()))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class OParlAdapter:
    """``SourceAdapter`` for an OParl 1.0 / 1.1 server."""

    source_system: ClassVar[str] = "oparl"

    def __init__(self, client: HarvesterClient, system_url: str) -> None:
        self._client = client
        self._system_url = system_url

    @classmethod
    def from_settings(cls, settings: Settings, system_url: str) -> OParlAdapter:
        return cls(HarvesterClient(settings), system_url)

    # -- SourceAdapter ------------------------------------------------------

    def capabilities(self) -> Capabilities:
        return Capabilities(
            pagination="object_link",
            supports_modified_since=True,
            # fetch_page has no ETag input, so conditional GET is not usable through it.
            supports_conditional_get=False,
            max_page_size=MAX_PAGE_SIZE,
            oparl_versions=("1.0", "1.1"),
        )

    async def discover_bodies(self) -> AsyncIterator[Body]:
        system = await self._get_object(self._system_url, body_key=None)
        _check_type(system, "System", url=self._system_url, required=True)
        listing = system.get("body")
        if not isinstance(listing, str) or not listing:
            raise SourceUnavailable(f"{self._system_url}: System object has no 'body' link")

        url: str | None = listing
        seen: set[str] = set()
        while url is not None:
            if url in seen:
                raise SourceUnavailable(f"body list pagination loops at {url}")
            seen.add(url)
            doc = await self._get_object(url, body_key=None)
            for item in self._collection(doc, url):
                if not isinstance(item, dict):
                    raise MalformedRecord(
                        f"{url}: body list item is {type(item).__name__}, not an object",
                        reason_code="schema_validation_failed",
                        raw=item,
                    )
                _check_type(item, "Body", url=url, required=False)
                yield self._to_body(item)
            url = self._next_link(doc)

    async def fetch_page(
        self,
        body: Body,
        cursor: Cursor | None,
        *,
        modified_since: datetime | None = None,
    ) -> Page:
        if cursor is not None and cursor.token:
            url = cursor.token
            self._require_same_host(url, body)
        elif body.paper_endpoint:
            url = body.paper_endpoint
        else:
            raise ConfigurationError(f"body {body.id!r} exposes no paper endpoint")

        url = _with_modified_since(url, modified_since)
        result = await self._client.get_json(url, body_key=body.id)
        if result.not_modified:
            return Page(records=(), next_cursor=None, etag=result.etag, not_modified=True)

        doc = self._require_object(result.data, url)
        items = self._collection(doc, url)
        records: list[RawDocument] = []
        malformed: list[tuple[str, dict[str, Any]]] = []
        for item in items:
            try:
                records.append(self._to_raw(item, body, url))
            except MalformedRecord as exc:
                # One unusable record must not discard the rest of its page.
                malformed.append(malformed_entry(exc))
        next_url = self._next_link(doc)
        return Page(
            records=tuple(records),
            next_cursor=Cursor(token=next_url) if next_url else None,
            etag=result.etag,
            malformed=tuple(malformed),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals ----------------------------------------------------------

    async def _get_object(self, url: str, *, body_key: str | None) -> dict[str, Any]:
        result = await self._client.get_json(url, body_key=body_key)
        return self._require_object(result.data, url)

    @staticmethod
    def _require_object(data: Any, url: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise SourceUnavailable(f"{url}: expected a JSON object, got {type(data).__name__}")
        return data

    @staticmethod
    def _collection(doc: dict[str, Any], url: str) -> list[Any]:
        """The ``data`` array. Items are NOT checked here: a bad item is a per-record problem."""
        data = doc.get("data")
        if not isinstance(data, list):
            raise SourceUnavailable(f"{url}: not an OParl collection (no 'data' array)")
        return data

    @staticmethod
    def _next_link(doc: dict[str, Any]) -> str | None:
        links = doc.get("links")
        if not isinstance(links, dict):
            return None
        return _str_or_none(links.get("next"))

    @staticmethod
    def _require_same_host(url: str, body: Body) -> None:
        """A ``next`` link must stay on the host that served the collection."""
        if not body.paper_endpoint:
            return
        if urlsplit(url).hostname != urlsplit(body.paper_endpoint).hostname:
            raise UnexpectedResponse(
                f"cursor {url!r} leaves the host of {body.paper_endpoint!r}; refusing to follow",
                status=200,
                url=url,
            )

    @staticmethod
    def _to_body(item: dict[str, Any]) -> Body:
        ident = _str_or_none(item.get("id"))
        if ident is None:
            raise MalformedRecord(
                "body has no 'id'", reason_code="missing_required_field", raw=item
            )
        return Body(
            id=ident,
            name=_str_or_none(item.get("name")),
            ags=_str_or_none(item.get("ags")),
            ars=_str_or_none(item.get("rgs")),
            licence=_str_or_none(item.get("license")),
            paper_endpoint=_str_or_none(item.get("paper")),
            web=_str_or_none(item.get("web")),
        )

    def _to_raw(self, item: Any, body: Body, url: str) -> RawDocument:
        """Map one paper. Raises ``MalformedRecord`` for a record that cannot be used."""
        if not isinstance(item, dict):
            raise MalformedRecord(
                f"{url}: collection item is {type(item).__name__}, not an object",
                reason_code="schema_validation_failed",
                raw=item,
            )
        _check_type(item, "Paper", url=url, required=False)
        source_url = _str_or_none(item.get("id"))
        if source_url is None:
            raise MalformedRecord(
                "paper has no 'id'", reason_code="missing_required_field", raw=item
            )
        return RawDocument(
            source_system=self.source_system,
            source_body_id=body.id,
            source_record_id=record_id_from(item),
            source_url=source_url,
            payload=item,
            licence=body.licence,
        )
