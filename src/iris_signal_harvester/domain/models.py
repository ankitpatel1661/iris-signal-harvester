"""The canonical contract between every adapter and the sink.

Every adapter may only *produce* ``CanonicalSignal``; the sink may only *consume*
it. A contract test asserts this for each adapter, which is what stops a new
source from quietly degrading the schema later.

The governing principle of this module: nothing is ever silently guessed. Where a
fact is uncertain, the uncertainty is a field — ``published_at_origin`` beside
``published_at``, ``geo_method`` and ``geo_confidence`` beside ``geom`` — so that
downstream consumers can filter on it instead of inheriting a false certainty.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "Body",
    "CanonicalSignal",
    "GeoConfidence",
    "GeoMethod",
    "GeoResolution",
    "Hit",
    "MatchResult",
    "PublishedAtOrigin",
    "RawDocument",
    "RejectCode",
    "RejectedRecord",
]


class PublishedAtOrigin(StrEnum):
    """Where ``published_at`` came from.

    The brief requires publication dates to be verified rather than assumed, so
    provenance is structural rather than documented. Ordered most to least
    trustworthy.
    """

    PAYLOAD_FIELD = "payload_field"
    HTTP_HEADER = "http_header"
    DERIVED_FROM_MEETING = "derived_from_meeting"
    UNKNOWN = "unknown"


class GeoMethod(StrEnum):
    """How the point geometry was obtained."""

    SOURCE_COORDINATES = "source_coordinates"
    CADASTRE_PARCEL = "cadastre_parcel"
    ADDRESS_GEOCODE = "address_geocode"
    MUNICIPALITY_CENTROID = "municipality_centroid"
    UNRESOLVED = "unresolved"


class GeoConfidence(StrEnum):
    """How much the geometry can be trusted. ``NONE`` implies ``geom is None``."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class RejectCode(StrEnum):
    """Stable, countable reasons a record was dead-lettered.

    These are histogrammed in the quality report, so they must never be
    free-text and never renamed without a migration note.
    """

    MALFORMED_JSON = "malformed_json"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_SOURCE_URL = "invalid_source_url"
    DATE_OUT_OF_RANGE = "date_out_of_range"
    DATE_UNPARSEABLE = "date_unparseable"
    GEO_NON_FINITE = "geo_non_finite"
    GEO_LATLON_IN_PROJECTED_FIELD = "geo_latlon_in_projected_field"
    GEO_OUT_OF_CRS_DOMAIN = "geo_out_of_crs_domain"
    GEO_OUTSIDE_NATIONAL_ENVELOPE = "geo_outside_national_envelope"
    GEO_OUTSIDE_CLAIMED_MUNICIPALITY = "geo_outside_claimed_municipality"
    GEO_REPROJECTION_FAILED = "geo_reprojection_failed"


class Body(BaseModel):
    """A council / municipal body exposed by a source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="Source-native body identifier, preserved verbatim.")
    name: str | None = None
    ags: str | None = Field(default=None, description="8-digit Amtlicher Gemeindeschlüssel.")
    ars: str | None = Field(default=None, description="12-digit Amtlicher Regionalschlüssel.")
    licence: str | None = Field(
        default=None,
        description="Declared licence, or None. Never inferred from a sibling body.",
    )
    paper_endpoint: str | None = None
    web: str | None = None


class RawDocument(BaseModel):
    """One record exactly as an adapter received it, before normalisation.

    ``payload`` is retained verbatim so the pipeline can re-derive matching,
    dates and geometry later without re-crawling hundreds of councils.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_system: str
    source_body_id: str
    source_record_id: str
    source_url: str
    payload: dict[str, Any]
    http_last_modified: datetime | None = Field(
        default=None,
        description="Last-Modified header for this resource, used as date provenance tier 2.",
    )
    licence: str | None = None


class Hit(BaseModel):
    """One taxonomy match, retained so a relevance score is always explainable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    term: str = Field(description="Canonical term as written in the taxonomy.")
    surface: str = Field(description="The text actually matched in the document.")
    field: str = Field(description="Which field the hit occurred in (title/summary/body).")
    offset: int = Field(ge=0, description="Character offset of the match in the folded field.")
    weight: float = Field(ge=0.0)
    signal_type: str


class MatchResult(BaseModel):
    """Outcome of taxonomy matching for one document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_type: str | None = None
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: tuple[Hit, ...] = ()

    @property
    def matched(self) -> bool:
        return bool(self.evidence)

    @classmethod
    def empty(cls) -> MatchResult:
        return cls()


class GeoResolution(BaseModel):
    """Outcome of geographic resolution, including an honest failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    easting: float | None = None
    northing: float | None = None
    method: GeoMethod = GeoMethod.UNRESOLVED
    confidence: GeoConfidence = GeoConfidence.NONE
    source_crs: str | None = Field(
        default=None, description="CRS as received, before reprojection. Retained for audit."
    )
    note: str | None = Field(
        default=None,
        description="Why a tier was skipped or a resolution is doubtful. Surfaced in the report.",
    )

    @property
    def resolved(self) -> bool:
        return self.easting is not None and self.northing is not None

    def model_post_init(self, _context: object) -> None:
        resolved = self.easting is not None and self.northing is not None
        if resolved and self.confidence is GeoConfidence.NONE:
            raise ValueError("a resolved geometry must carry a confidence other than 'none'")
        if not resolved and self.confidence is not GeoConfidence.NONE:
            raise ValueError("an unresolved geometry must carry confidence 'none'")


class CanonicalSignal(BaseModel):
    """The single record type loaded into ``iris_staging.signal_ingest``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    harvest_run_id: UUID

    # --- provenance: the traceability anchor -------------------------------
    source_system: str
    source_body_id: str
    source_record_id: str
    source_url: str
    source_licence: str | None = None

    # --- content -----------------------------------------------------------
    title: str
    summary: str | None = None
    document_type: str | None = None

    # --- dates: value and its provenance, always together -------------------
    published_at: datetime | None = None
    published_at_origin: PublishedAtOrigin = PublishedAtOrigin.UNKNOWN

    # --- geography ----------------------------------------------------------
    ags: str | None = None
    ars: str | None = None
    municipality_name: str | None = None
    kreis: str | None = None
    bundesland: str | None = None
    easting: float | None = None
    northing: float | None = None
    geo_method: GeoMethod = GeoMethod.UNRESOLVED
    geo_confidence: GeoConfidence = GeoConfidence.NONE
    source_crs: str | None = None

    # --- signal -------------------------------------------------------------
    signal_type: str | None = None
    matched_keywords: tuple[Hit, ...] = ()
    relevance_score: float | None = Field(default=None, ge=0.0, le=1.0)
    taxonomy_version: str

    # --- integrity ----------------------------------------------------------
    raw_payload: dict[str, Any]
    content_hash: str = Field(min_length=64, max_length=64)
    hash_version: int = 1

    @field_validator("source_url")
    @classmethod
    def _url_must_be_absolute_http_or_https(cls, value: str) -> str:
        # Real municipal systems still publish http:// links, and rejecting a record over
        # its link scheme would discard otherwise valid data, so http is accepted.
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc or value != value.strip():
            raise ValueError(f"source_url must be an absolute http(s) URL, got {value!r}")
        return value

    @field_validator("source_system", "source_body_id", "source_record_id")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank — it forms part of the natural key")
        return value

    @field_validator("ags")
    @classmethod
    def _ags_shape(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 8 or not value.isdigit()):
            raise ValueError(f"ags must be 8 digits, got {value!r}")
        return value

    def model_post_init(self, _context: object) -> None:
        resolved = self.easting is not None and self.northing is not None
        if resolved != (self.geo_confidence is not GeoConfidence.NONE):
            raise ValueError(
                "geometry presence and geo_confidence must agree: "
                f"resolved={resolved}, confidence={self.geo_confidence}"
            )
        if resolved != (self.geo_method is not GeoMethod.UNRESOLVED):
            raise ValueError(
                "geometry presence and geo_method must agree: "
                f"resolved={resolved}, method={self.geo_method}"
            )
        if (self.published_at is None) != (self.published_at_origin is PublishedAtOrigin.UNKNOWN):
            raise ValueError(
                "published_at and published_at_origin must agree: published_at is None "
                "exactly when the origin is 'unknown', "
                f"got published_at={self.published_at!r}, origin={self.published_at_origin}"
            )

    @property
    def natural_key(self) -> tuple[str, str, str]:
        """The key the unique constraint and the upsert are built on.

        ``(source_system, source_body_id, source_record_id)``. An OParl ``reference``
        is unique only within a body, so the body is part of the key; the record id
        itself is kept verbatim and never re-generated.
        """
        return (self.source_system, self.source_body_id, self.source_record_id)


class RejectedRecord(BaseModel):
    """A record that failed validation, destined for the dead-letter table.

    Rejections are rows, not log lines, so they can be counted, queried and
    replayed after a fix.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    harvest_run_id: UUID
    source_system: str
    source_body_id: str | None = None
    source_record_id: str | None = None
    reason_code: RejectCode
    reason_detail: str
    raw_payload: dict[str, Any]
