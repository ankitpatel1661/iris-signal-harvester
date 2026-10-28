"""``RawDocument`` -> ``CanonicalSignal``: the one place a source shape becomes the contract.

Pipeline position
-----------------
Adapters produce ``RawDocument`` (verbatim payload, no interpretation). The sink
consumes ``CanonicalSignal``. This module is the only bridge, and it is a *total*
function: every input produces either a signal, a ``RejectedRecord``, or a
below-threshold verdict. It never raises on bad data, because a single malformed
record must not kill a sweep over hundreds of councils.

Order of operations, and why it is this order
---------------------------------------------
1. **Scrub NULs.** ``U+0000`` cannot be stored in a PostgreSQL ``text`` column or
   inside a ``jsonb`` value. Scrubbing happens *first*, before anything reads the
   payload, so that the values that are matched, resolved and **hashed** are
   byte-for-byte the values that are stored. Scrubbing later — at the sink — would
   mean ``content_hash`` was computed over text that no longer exists in the
   database, and the promise that ``raw_payload`` lets us re-derive everything
   without re-fetching would be false for exactly the records most likely to be
   re-examined.
2. **Taxonomy.** Matching decides whether this document is a signal at all. It runs
   *before* dates and geography: most council documents are irrelevant, and a corrupt
   date on a document the project does not care about must not be dead-lettered. An
   irrelevant document with a bad date is ``BELOW_THRESHOLD``, never ``REJECTED``.
3. **Dates.** The provenance ladder (``domain/dates.py``), only for records that will
   be emitted. A corrupt date is a rejection, not a silent fallback to a lower tier.
4. **Geo.** The tier chain (``domain/geo/resolver.py``), also only for emitted records.
   A coordinate that fails validation is rejected; it is never clamped, snapped or
   replaced by a centroid.
5. **Hash.** Last, over the finished semantic projection.

Mapping assumptions (the honest list)
-------------------------------------
Field names differ between sources, so each canonical field reads the first present
key from a documented candidate list rather than a single hard-coded name:

===================  ==========================================================
canonical field      payload keys consulted, in order
===================  ==========================================================
title                ``name``, ``title``, ``subject``
summary              ``description``, ``abstract``, ``summary``, ``text``
document_type        ``paperType``, ``document_type``, ``documentType``, then
                     mapped through ``config/document_types.yaml`` (see below)
published_at tier 1  ``date``, ``published``, ``publishedDate``, ``datePublished``
meeting date tier 3  ``meeting[].start``, ``meetingDate``, ``meeting_date``
coordinates          ``location.{x,y,crs}`` (cassette shape), else
                     ``location.geojson`` Point in WGS84 (OParl ``Location``)
address              ``location.streetAddress`` (+ ``postalCode``, ``locality``)
===================  ==========================================================

Tier 1 accepts two ``location`` shapes, the cassette shape first:

* ``{x, y, crs}``: **not** part of OParl. It is the shape the committed geo cassettes
  use, and the briefed ``ratsdokumente`` contract uses it too, so reading it is what
  lets tier 1 be exercised offline. The CRS is mandatory (EPSG:25832 or 25833).
* ``geojson``: the real OParl ``Location`` shape. A GeoJSON Feature, or a bare
  Geometry, whose geometry is a ``Point`` in WGS84 (``[longitude, latitude]``). It is
  reprojected to EPSG:25832 and then validated exactly like every other point. Any
  other geometry type is ignored rather than guessed at. ``Paper.location`` may be a
  single object or a list of them; a list is used only when it holds exactly one
  ``Location``, because choosing among several would be a guess.

The verified live endpoint carries no ``location`` on ``Paper`` at all, so on that
endpoint this tier never fires, which is why tiers 4 and 5 dominate there.

Document types are a controlled vocabulary (``config/document_types.yaml``): the raw
``paperType`` is folded and looked up; a value that matches no alias becomes
``unmapped`` (:data:`UNMAPPED_DOCUMENT_TYPE`), and a missing/blank one stays ``None``.
The raw value is not lost: it stays in ``raw_payload`` and is also exposed on
:attr:`Normalised.document_type_raw` so the quality report can list what was unmapped.

AGS and ARS are taken from the *body*, not from the document: a council's identity is
a property of the council. A misshapen AGS (not 8 digits) is dropped to ``None`` with
a note rather than rejecting every record of that body — the defect is in one
registry field, and rejecting a whole municipality's documents over it would lose far
more information than it protects. The report counts it so it stays visible.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import ValidationError

from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.dates import resolve_published_at
from iris_signal_harvester.domain.geo.gazetteer import Gazetteer, Municipality, StaticGazetteer
from iris_signal_harvester.domain.geo.resolver import (
    GeoCandidates,
    Geocoder,
    GeoResolver,
    SourceCoordinates,
)
from iris_signal_harvester.domain.hashing import HASH_VERSION, content_hash
from iris_signal_harvester.domain.models import (
    Body,
    CanonicalSignal,
    GeoResolution,
    MatchResult,
    PublishedAtOrigin,
    RawDocument,
    RejectCode,
    RejectedRecord,
)
from iris_signal_harvester.domain.sanitize import scrub_nulls, unstorable_reason
from iris_signal_harvester.domain.taxonomy import Taxonomy, fold
from iris_signal_harvester.errors import ConfigurationError, RecordRejected

__all__ = [
    "DEFAULT_DOCUMENT_TYPES_PATH",
    "DEFAULT_GAZETTEER_PATH",
    "UNMAPPED_DOCUMENT_TYPE",
    "DocumentTypes",
    "Normalised",
    "Normaliser",
    "Verdict",
    "payload_coordinates",
    "payload_text",
]

# ``Settings`` has no gazetteer path (it is domain knowledge, not behaviour, and
# config.py is outside this task's remit), so the default lives here and the CLI can
# override it.
DEFAULT_GAZETTEER_PATH = Path("config/gazetteer.json")
DEFAULT_DOCUMENT_TYPES_PATH = Path("config/document_types.yaml")

UNMAPPED_DOCUMENT_TYPE = "unmapped"
"""``document_type`` of a row whose raw ``paperType`` matched no vocabulary entry."""

_TITLE_KEYS = ("name", "title", "subject")
_SUMMARY_KEYS = ("description", "abstract", "summary", "text")
_TYPE_KEYS = ("paperType", "document_type", "documentType")
_DATE_KEYS = ("date", "published", "publishedDate", "datePublished")
_MEETING_DATE_KEYS = ("meetingDate", "meeting_date")
_ADDRESS_KEYS = ("streetAddress", "address", "description")


class DocumentTypes:
    """The controlled document-type vocabulary, loaded from ``config/document_types.yaml``."""

    def __init__(
        self,
        mapping: Mapping[str, Sequence[str]],
        *,
        unmapped: str = UNMAPPED_DOCUMENT_TYPE,
        version: int = 1,
    ) -> None:
        self.version = version
        self.unmapped = unmapped
        self._lookup: dict[str, str] = {}
        for canonical, aliases in mapping.items():
            for spelling in (canonical, *aliases):
                key = self._key(spelling)
                if not key:
                    raise ConfigurationError(f"document type {canonical!r}: blank alias")
                previous = self._lookup.setdefault(key, canonical)
                if previous != canonical:
                    raise ConfigurationError(
                        f"document type alias {spelling!r} is ambiguous: "
                        f"{previous!r} and {canonical!r}"
                    )
        if self._key(unmapped) in self._lookup:
            raise ConfigurationError(f"{unmapped!r} is the unmapped bucket and cannot be a type")

    @staticmethod
    def _key(value: str) -> str:
        return fold(collapse_whitespace(unicodedata.normalize("NFKC", value)))

    @classmethod
    def load(cls, path: Path | str) -> DocumentTypes:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot read document types {source}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("types"), dict):
            raise ConfigurationError(f"document types {source} must be a mapping with 'types'")
        types: dict[str, list[str]] = {}
        for name, aliases in raw["types"].items():
            if aliases is None:
                aliases = []
            if not isinstance(name, str) or not isinstance(aliases, list):
                raise ConfigurationError(f"document types {source}: bad entry {name!r}")
            if not all(isinstance(a, str) for a in aliases):
                raise ConfigurationError(f"document types {source}: non-text alias under {name!r}")
            types[name] = list(aliases)
        unmapped = raw.get("unmapped", UNMAPPED_DOCUMENT_TYPE)
        version = raw.get("version", 1)
        if not isinstance(unmapped, str) or not unmapped.strip() or not isinstance(version, int):
            raise ConfigurationError(f"document types {source}: bad 'unmapped' or 'version'")
        return cls(types, unmapped=unmapped, version=version)

    @property
    def vocabulary(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._lookup.values())))

    def map(self, raw: str | None) -> str | None:
        """Canonical type; ``None`` for an absent value; ``unmapped`` for an unknown one."""
        if raw is None:
            return None
        return self._lookup.get(self._key(raw), self.unmapped)


class Verdict(StrEnum):
    """What normalisation decided about one record.

    ``BELOW_THRESHOLD`` is deliberately not a rejection: the document parsed
    perfectly and simply is not an infrastructure signal. Most council documents
    are not. Counting the two together would hide a real rejection spike inside a
    normal below-threshold rate.
    """

    EMITTED = "emitted"
    BELOW_THRESHOLD = "below_threshold"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class Normalised:
    """The outcome for one record. Exactly one of ``signal`` / ``reject`` is set."""

    verdict: Verdict
    signal: CanonicalSignal | None = None
    reject: RejectedRecord | None = None
    match: MatchResult = field(default_factory=MatchResult.empty)
    geo: GeoResolution | None = None
    notes: tuple[str, ...] = ()
    document_type_raw: str | None = None
    """The raw ``paperType`` value (whitespace-normalised), for EMITTED records only."""

    @property
    def emitted(self) -> bool:
        return self.verdict is Verdict.EMITTED

    @property
    def document_type_unmapped(self) -> bool:
        """True when an emitted record carried a document type outside the vocabulary.

        This is what the quality report counts: it is ``False`` both for mapped types
        and for an absent type (which is a null, not an unmapped value).
        """
        return (
            self.signal is not None
            and self.signal.document_type == UNMAPPED_DOCUMENT_TYPE
            and self.document_type_raw is not None
        )


_INVISIBLE = {0x200B: None, 0xFEFF: None}  # zero-width space, BOM: layout debris, not text


def collapse_whitespace(value: str) -> str:
    """Trim, collapse every internal whitespace run to one space, drop zero-width debris.

    ``str.split()`` treats all Unicode whitespace as a separator: runs of spaces, tabs,
    newlines, NBSP (U+00A0), narrow NBSP and ideographic space. A purely cosmetic edit
    to a title therefore cannot change the stored text or the content hash. The
    original text is untouched in ``raw_payload``.
    """
    return " ".join(value.translate(_INVISIBLE).split())


def _text(value: object) -> str | None:
    """A non-blank, whitespace-normalised string, or ``None``. Numbers are *not* coerced."""
    if isinstance(value, str):
        collapsed = collapse_whitespace(value)
        return collapsed or None
    return None


def _first_text(payload: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        found = _text(payload.get(key))
        if found is not None:
            return found
    return None


def _first_present(payload: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """The first value that is actually there.

    A key present with value ``null`` (``"date": null`` is the common OParl case)
    is treated as absent, so an alternative spelling further down the list still
    gets its turn instead of being shadowed by an empty one.
    """
    for key in keys:
        value = payload.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _float(value: object) -> float | None:
    """A finite-looking numeric coordinate. Strings are accepted; bools are not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _location(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    location = payload.get("location")
    return location if isinstance(location, Mapping) else None


def _geojson_point(geojson: object) -> tuple[float, float] | None:
    """``(lon, lat)`` of a GeoJSON ``Point`` (as Feature or bare Geometry), else ``None``.

    Only a Point yields a single location. A Polygon, a collection or anything
    malformed returns ``None``; no representative point is invented for it. Numbers
    must be real JSON numbers (strings are not coerced), and any altitude is ignored.
    """
    if not isinstance(geojson, Mapping):
        return None
    geometry: object = geojson
    if geojson.get("type") == "Feature":
        geometry = geojson.get("geometry")
    if not isinstance(geometry, Mapping) or geometry.get("type") != "Point":
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return None
    lon, lat = coordinates[0], coordinates[1]
    if isinstance(lon, bool) or isinstance(lat, bool):
        return None
    if not isinstance(lon, int | float) or not isinstance(lat, int | float):
        return None
    return (float(lon), float(lat))


def payload_coordinates(payload: Mapping[str, Any]) -> SourceCoordinates | None:
    """Tier-1 coordinates from ``location``: the cassette shape, else OParl GeoJSON.

    1. ``location = {x, y, crs}`` (cassette / ``ratsdokumente`` shape). The CRS is
       mandatory and never defaulted: a coordinate pair with no declared CRS is two
       numbers, not a location, and assuming EPSG:25832 is exactly the kind of silent
       guess this project refuses.
    2. ``location.geojson``, the real OParl ``Location`` member: a WGS84 Point, returned
       with ``geographic=True`` so the resolver reprojects it to EPSG:25832.

    A location with neither is ignored here, which sends the record down the tier chain
    to the municipality centroid or to honest ``unresolved``.
    """
    raw = payload.get("location")
    if isinstance(raw, list | tuple):
        # OParl's Paper.location is a list. One Location is usable; several are not,
        # because picking one of them would be a guess.
        candidates = [item for item in raw if isinstance(item, Mapping)]
        if len(candidates) != 1:
            return None
        location: Mapping[str, Any] = candidates[0]
    elif isinstance(raw, Mapping):
        location = raw
    else:
        return None
    x = _float(location.get("x"))
    y = _float(location.get("y"))
    crs = _text(location.get("crs"))
    if x is not None and y is not None and crs is not None:
        return SourceCoordinates(x=x, y=y, crs=crs)
    point = _geojson_point(location.get("geojson"))
    if point is not None:
        return SourceCoordinates(x=point[0], y=point[1], crs="EPSG:4326", geographic=True)
    return None


def payload_address(payload: Mapping[str, Any]) -> str | None:
    """A postal address from ``location``, assembled from whichever parts exist."""
    location = _location(payload)
    if location is None:
        return _first_text(payload, ("address",))
    street = _first_text(location, _ADDRESS_KEYS)
    if street is None:
        return None
    parts = [
        street,
        _text(location.get("postalCode")),
        _text(location.get("locality")) or _text(location.get("subLocality")),
    ]
    return ", ".join(p for p in parts if p)


def payload_text(payload: Mapping[str, Any]) -> dict[str, str | None]:
    """The ``{title, summary, body}`` view the taxonomy matches against.

    ``body`` is a last-resort bucket: keywords, the document type and any location
    description. Attachment (PDF) text is explicitly out of scope, so a document
    whose trigger words appear only inside an attachment will not match — a known
    limitation, not a silent one.
    """
    extras: list[str] = []
    keywords = payload.get("keyword")
    if isinstance(keywords, list):
        extras.extend(k for k in (_text(item) for item in keywords) if k is not None)
    doc_type = _first_text(payload, _TYPE_KEYS)
    if doc_type is not None:
        extras.append(doc_type)
    location = _location(payload)
    if location is not None:
        note = _text(location.get("description"))
        if note is not None:
            extras.append(note)
    return {
        "title": _first_text(payload, _TITLE_KEYS),
        "summary": _first_text(payload, _SUMMARY_KEYS),
        "body": " ".join(extras) or None,
    }


def _meeting_date(payload: Mapping[str, Any]) -> str | None:
    """A meeting date for ladder tier 3, if the payload embeds one.

    OParl's ``Paper.meeting`` is normally a list of *URLs*; only an expanded object
    carries ``start``. Nothing is fetched here to find one — that would turn one
    record into N requests — so this tier fires only when the date is already in hand.
    """
    direct = _first_text(payload, _MEETING_DATE_KEYS)
    if direct is not None:
        return direct
    meetings = payload.get("meeting")
    if isinstance(meetings, Mapping):
        meetings = [meetings]
    if not isinstance(meetings, list):
        return None
    for item in meetings:
        if isinstance(item, Mapping):
            start = _text(item.get("start")) or _text(item.get("date"))
            if start is not None:
                return start
    return None


def _digits(value: str | None, length: int) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    return candidate if len(candidate) == length and candidate.isdigit() else None


class Normaliser:
    """Turns ``RawDocument`` into ``CanonicalSignal``, or into an honest rejection."""

    def __init__(
        self,
        settings: Settings,
        taxonomy: Taxonomy,
        gazetteer: Gazetteer,
        *,
        geocoder: Geocoder | None = None,
        document_types: DocumentTypes | None = None,
    ) -> None:
        self._settings = settings
        self._document_types = document_types or DocumentTypes.load(DEFAULT_DOCUMENT_TYPES_PATH)
        self._taxonomy = taxonomy
        self._gazetteer = gazetteer
        self._resolver = GeoResolver(
            gazetteer,
            geocoder=geocoder,
            tolerance_m=settings.municipality_tolerance_m,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        gazetteer_path: Path | None = None,
        geocoder: Geocoder | None = None,
        document_types_path: Path | None = None,
    ) -> Normaliser:
        """Load the taxonomy, gazetteer and document-type vocabulary from configuration."""
        return cls(
            settings,
            Taxonomy.load(settings.taxonomy_path),
            StaticGazetteer.from_file(gazetteer_path or DEFAULT_GAZETTEER_PATH),
            geocoder=geocoder,
            document_types=DocumentTypes.load(document_types_path or DEFAULT_DOCUMENT_TYPES_PATH),
        )

    @property
    def taxonomy_version(self) -> str:
        return self._taxonomy.taxonomy_version

    # -- the one public operation ---------------------------------------------

    def normalise(
        self,
        document: RawDocument,
        body: Body,
        run_id: UUID,
        *,
        now: datetime | None = None,
    ) -> Normalised:
        """Normalise one record. Never raises for bad data.

        Returns a ``Normalised`` carrying exactly one of a ``CanonicalSignal``, a
        ``RejectedRecord``, or a ``BELOW_THRESHOLD`` verdict.
        """
        # (1) Scrub before anything reads the payload, so matched, resolved, hashed
        #     and stored values are the same bytes.
        payload: dict[str, Any] = scrub_nulls(dict(document.payload))
        notes: list[str] = []

        try:
            return self._build(document, body, run_id, payload, notes, now=now)
        except RecordRejected as exc:
            return Normalised(
                verdict=Verdict.REJECTED,
                reject=self._reject(document, body, run_id, payload, exc.reason_code, exc.detail),
                notes=tuple(notes),
            )
        except ValidationError as exc:
            # The contract itself refused the record: a schema problem, not a data
            # gate. Reported under its own code so the two are never confused.
            return Normalised(
                verdict=Verdict.REJECTED,
                reject=self._reject(
                    document,
                    body,
                    run_id,
                    payload,
                    RejectCode.SCHEMA_VALIDATION_FAILED,
                    f"CanonicalSignal rejected the record: {exc.errors(include_url=False)}",
                ),
                notes=tuple(notes),
            )

    # -- internals -------------------------------------------------------------

    def _build(
        self,
        document: RawDocument,
        body: Body,
        run_id: UUID,
        payload: dict[str, Any],
        notes: list[str],
        *,
        now: datetime | None,
    ) -> Normalised:
        source_url = _text(document.source_url)
        if source_url is None or not source_url.startswith(("http://", "https://")):
            raise RecordRejected(
                RejectCode.INVALID_SOURCE_URL.value,
                f"source_url {document.source_url!r} is not an absolute http(s) URL",
            )

        text = payload_text(payload)
        title = text["title"]
        if title is None:
            raise RecordRejected(
                RejectCode.MISSING_REQUIRED_FIELD.value,
                f"no usable title: none of {', '.join(_TITLE_KEYS)} carries text in the payload",
            )

        # (2) taxonomy. Relevance first: an irrelevant document is BELOW_THRESHOLD even if
        #     its date is corrupt, so the reject table holds only records we care about.
        match = self._taxonomy.match(text)
        if not self._taxonomy.should_emit(match):
            return Normalised(verdict=Verdict.BELOW_THRESHOLD, match=match, notes=tuple(notes))

        # (2b) values PostgreSQL cannot store. Hashing itself raises on a non-finite number
        #      (ValueError) or a lone surrogate (UnicodeEncodeError), so this has to happen
        #      BEFORE anything is hashed, and only for records we would otherwise emit: an
        #      irrelevant document with a NaN in it is still just BELOW_THRESHOLD.
        unstorable = unstorable_reason(
            {
                "payload": payload,
                "source_url": document.source_url,
                "source_record_id": document.source_record_id,
                "source_body_id": document.source_body_id,
            },
            "$",
        )
        if unstorable is not None:
            raise RecordRejected(
                RejectCode.SCHEMA_VALIDATION_FAILED.value,
                f"not storable in PostgreSQL: {unstorable}",
            )

        # (3) dates, for emitted signals only
        published_at, origin = resolve_published_at(
            _first_present(payload, _DATE_KEYS),
            document.http_last_modified,
            _meeting_date(payload),
            self._settings,
            now=now,
        )

        # (4) geo
        ags = _digits(body.ags, 8)
        if body.ags is not None and ags is None:
            notes.append(
                f"body {body.id!r} declares a misshapen AGS {body.ags!r} (expected 8 digits); "
                "dropped to NULL, so the municipality-centroid tier is unavailable"
            )
        ars = _digits(body.ars, 12)
        if body.ars is not None and ars is None:
            notes.append(
                f"body {body.id!r} declares a misshapen ARS {body.ars!r} (expected 12 digits); "
                "dropped to NULL"
            )
        geo = self._resolver.resolve(
            GeoCandidates(
                coordinates=payload_coordinates(payload),
                text=" ".join(v for v in text.values() if v) or None,
                address=payload_address(payload),
                ags=ags,
            )
        )
        municipality = self._gazetteer.get(ags) if ags is not None else None

        # (5) hash, over the finished semantic projection
        raw_doc_type = _first_text(payload, _TYPE_KEYS)
        fields = self._semantic_fields(
            document=document,
            source_url=source_url,
            title=title,
            summary=text["summary"],
            doc_type=self._document_types.map(raw_doc_type),
            published_at=published_at,
            origin=origin,
            ags=ags,
            ars=ars,
            municipality=municipality,
            payload=payload,
            geo=geo,
            match=match,
        )
        signal = CanonicalSignal(
            harvest_run_id=run_id,
            raw_payload=payload,
            content_hash=content_hash(fields),
            hash_version=HASH_VERSION,
            taxonomy_version=self._taxonomy.taxonomy_version,
            # ``matched_keywords`` is already in ``fields``: the evidence is part of
            # the semantic projection, so it is hashed as well as stored.
            **fields,
        )
        if geo.note:
            notes.append(geo.note)
        return Normalised(
            verdict=Verdict.EMITTED,
            signal=signal,
            match=match,
            geo=geo,
            notes=tuple(notes),
            document_type_raw=raw_doc_type,
        )

    def _semantic_fields(
        self,
        *,
        document: RawDocument,
        source_url: str,
        title: str,
        summary: str | None,
        doc_type: str | None,
        published_at: datetime | None,
        origin: PublishedAtOrigin,
        ags: str | None,
        ars: str | None,
        municipality: Municipality | None,
        payload: Mapping[str, Any],
        geo: GeoResolution,
        match: MatchResult,
    ) -> dict[str, Any]:
        """The semantic projection, built once and used for both hash and signal.

        Building it once is the point: a field that reached the row but not the hash
        (or the reverse) would make change detection lie.
        """
        return {
            "source_system": document.source_system,
            # Part of the natural key and required: an OParl ``reference`` is unique
            # only within its body, so the body has to be in the key.
            "source_body_id": document.source_body_id,
            "source_record_id": document.source_record_id,
            "source_url": source_url,
            "source_licence": document.licence,
            "title": title,
            "summary": summary,
            "document_type": doc_type,
            "published_at": published_at,
            "published_at_origin": origin,
            "ags": ags,
            "ars": ars,
            "municipality_name": (
                municipality.name
                if municipality is not None
                else self._payload_municipality_name(payload)
            ),
            "kreis": municipality.kreis if municipality is not None else None,
            "bundesland": municipality.bundesland if municipality is not None else None,
            "easting": geo.easting,
            "northing": geo.northing,
            "geo_method": geo.method,
            "geo_confidence": geo.confidence,
            "source_crs": geo.source_crs,
            "signal_type": match.signal_type,
            "relevance_score": match.score,
            "matched_keywords": match.evidence,
        }

    @staticmethod
    def _payload_municipality_name(payload: Mapping[str, Any]) -> str | None:
        municipality = payload.get("municipality")
        if isinstance(municipality, Mapping):
            return _text(municipality.get("name"))
        return None

    @staticmethod
    def _reject(
        document: RawDocument,
        body: Body,
        run_id: UUID,
        payload: Mapping[str, Any],
        reason_code: str,
        detail: str,
    ) -> RejectedRecord:
        try:
            code = RejectCode(reason_code)
        except ValueError:  # pragma: no cover - defensive: codes come from the enum
            code = RejectCode.SCHEMA_VALIDATION_FAILED
            detail = f"unknown reason code {reason_code!r}: {detail}"
        return RejectedRecord(
            harvest_run_id=run_id,
            source_system=document.source_system,
            source_body_id=document.source_body_id or body.id,
            source_record_id=document.source_record_id,
            reason_code=code,
            reason_detail=detail,
            raw_payload=dict(payload),
        )
