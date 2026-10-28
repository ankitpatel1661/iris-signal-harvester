"""Tiered geographic resolution.

Tiers, best first::

    1 source_coordinates      high    the record's own coordinates
    2 cadastre_parcel         high    Gemarkung/Flur/Flurstueck -> ALKIS   (NOT IMPLEMENTED)
    3 address_geocode         medium  a ``Geocoder`` (injected; no network here)
    4 municipality_centroid   low     AGS -> gazetteer centroid
    5 unresolved              none    honest failure

Failure semantics (the absolute rule): a candidate that fails validation is never
clamped, snapped or replaced. By default the resolver raises ``RecordRejected``
with the specific ``RejectCode``. A caller that prefers to carry on at a lower
tier passes ``degrade_on_reject=True``; the result is then an honestly labelled
lower-tier resolution (``municipality_centroid`` / ``low``, or ``unresolved`` /
``none``) whose ``note`` records the rejection. A failed high-confidence candidate
can never come back as a high-confidence result.

Tier 2 is a configuration gap, not a data gap: the parcel reference is parsed and
reported, but without an ALKIS/WFS endpoint it cannot be resolved, so the tier is
skipped with an explanatory note rather than faked.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError
from shapely.geometry import Polygon

from iris_signal_harvester.domain.geo.gazetteer import Gazetteer, Municipality
from iris_signal_harvester.domain.geo.validate import (
    GERMANY_ENVELOPE,
    check_finite,
    check_not_latlon,
    validate_25832,
)
from iris_signal_harvester.domain.models import GeoConfidence, GeoMethod, GeoResolution, RejectCode
from iris_signal_harvester.errors import RecordRejected

__all__ = [
    "GeoCandidates",
    "GeoResolver",
    "GeocodeHit",
    "Geocoder",
    "ParcelRef",
    "SourceCoordinates",
    "parse_parcel_reference",
    "reproject_to_25832",
    "reproject_wgs84_to_25832",
]

TARGET_CRS = "EPSG:25832"
_SUPPORTED_EPSG = frozenset({25832, 25833})

_PARCEL_RE = re.compile(
    r"Gemarkung\s+(?P<gemarkung>[^,;\n]+?)\s*[,;]?\s*"
    r"Flur\s+(?P<flur>\d+[a-zA-Z]?)\s*[,;]?\s*"
    r"Flurst(?:ü|ue)ck\s+(?P<flurstueck>\d+(?:\s*/\s*\d+)?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParcelRef:
    """A parsed ``Gemarkung ... Flur ... Flurstück ...`` reference."""

    gemarkung: str
    flur: str
    flurstueck: str


@dataclass(frozen=True)
class SourceCoordinates:
    """Coordinates as supplied by the source. The CRS is mandatory: never assumed.

    ``geographic`` marks a WGS84 longitude/latitude pair (``x`` = longitude, ``y`` =
    latitude), which is what an OParl ``Location.geojson`` Point carries. Axis order
    is fixed by that flag, so there is no EPSG:4326 lat/lon ambiguity; a projected
    ``{x, y, crs}`` pair never sets it, and an ``EPSG:4326`` string there is still
    refused as an unsupported CRS.
    """

    x: float
    y: float
    crs: str
    geographic: bool = False


@dataclass(frozen=True)
class GeocodeHit:
    """A geocoder answer. ``crs`` is mandatory for the same reason as above."""

    x: float
    y: float
    crs: str = TARGET_CRS


class Geocoder(Protocol):
    """Address geocoder. Tests inject a stub; production wraps a real service."""

    def geocode(self, address: str, *, ags: str | None) -> GeocodeHit | None: ...


@dataclass(frozen=True)
class GeoCandidates:
    """Everything the source offered for locating one record."""

    coordinates: SourceCoordinates | None = None
    text: str | None = None  # free text searched for a parcel reference
    address: str | None = None
    ags: str | None = None


def parse_parcel_reference(text: str | None) -> ParcelRef | None:
    """Detect a ``Gemarkung X, Flur N, Flurstück M`` reference in free text."""
    if not text:
        return None
    m = _PARCEL_RE.search(text)
    if m is None:
        return None
    return ParcelRef(
        gemarkung=m["gemarkung"].strip(),
        flur=m["flur"],
        flurstueck=re.sub(r"\s+", "", m["flurstueck"]),
    )


@lru_cache(maxsize=8)
def _transformer(epsg: int) -> Transformer:
    return Transformer.from_crs(f"EPSG:{epsg}", TARGET_CRS, always_xy=True)


def reproject_to_25832(x: float, y: float, crs: str) -> tuple[float, float]:
    """Reproject a projected point to EPSG:25832.

    Checks 1 and 2 run on the raw input first, so a lat/lon pair or ``(0, 0)`` in
    an EPSG:25833 field is named for what it is rather than becoming an arbitrary
    reprojected number. An unsupported or unparseable CRS, or a non-finite result,
    raises ``GEO_REPROJECTION_FAILED``.
    """
    check_finite(x, y)
    check_not_latlon(x, y)
    try:
        epsg = CRS.from_user_input(crs).to_epsg()
    except (CRSError, ProjError) as exc:
        raise RecordRejected(
            RejectCode.GEO_REPROJECTION_FAILED, f"unrecognised CRS {crs!r}: {exc}"
        ) from exc
    if epsg not in _SUPPORTED_EPSG:
        raise RecordRejected(
            RejectCode.GEO_REPROJECTION_FAILED,
            f"unsupported source CRS {crs!r} (supported: EPSG:25832, EPSG:25833)",
        )
    if epsg == 25832:
        return (x, y)
    try:
        nx, ny = _transformer(epsg).transform(x, y)
    except ProjError as exc:
        raise RecordRejected(
            RejectCode.GEO_REPROJECTION_FAILED, f"reprojection from {crs!r} failed: {exc}"
        ) from exc
    if not (math.isfinite(nx) and math.isfinite(ny)):
        raise RecordRejected(
            RejectCode.GEO_REPROJECTION_FAILED,
            f"reprojection of ({x}, {y}) from {crs!r} produced a non-finite result",
        )
    return (float(nx), float(ny))


def reproject_wgs84_to_25832(lon: float, lat: float) -> tuple[float, float]:
    """Reproject a WGS84 longitude/latitude point (GeoJSON order) to EPSG:25832.

    The projected-field heuristics of :func:`reproject_to_25832` do not apply here,
    because a lon/lat pair is the *correct* shape for this input. What is checked
    instead: both ordinates finite (``GEO_NON_FINITE``), inside the WGS84 domain
    (``GEO_OUT_OF_CRS_DOMAIN``), and a finite projected result
    (``GEO_REPROJECTION_FAILED``). The result then goes through the same
    :func:`validate_25832` checks as every other point, so a ``(0, 0)`` placeholder or
    a swapped latitude/longitude lands far outside the national envelope and is
    rejected there rather than being accepted.
    """
    check_finite(lon, lat)
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise RecordRejected(
            RejectCode.GEO_OUT_OF_CRS_DOMAIN,
            f"(lon={lon}, lat={lat}) is outside the WGS84 domain "
            "(longitude -180..180, latitude -90..90)",
        )
    try:
        nx, ny = _transformer(4326).transform(lon, lat)
    except ProjError as exc:
        raise RecordRejected(
            RejectCode.GEO_REPROJECTION_FAILED, f"reprojection from WGS84 failed: {exc}"
        ) from exc
    if not (math.isfinite(nx) and math.isfinite(ny)):
        raise RecordRejected(
            RejectCode.GEO_REPROJECTION_FAILED,
            f"reprojection of (lon={lon}, lat={lat}) from WGS84 produced a non-finite result",
        )
    return (float(nx), float(ny))


class GeoResolver:
    """Runs the tier chain and returns a ``GeoResolution``."""

    def __init__(
        self,
        gazetteer: Gazetteer,
        *,
        geocoder: Geocoder | None = None,
        tolerance_m: float = 2000.0,
    ) -> None:
        self._gazetteer = gazetteer
        self._geocoder = geocoder
        self._tol = tolerance_m

    def resolve(
        self, candidates: GeoCandidates, *, degrade_on_reject: bool = False
    ) -> GeoResolution:
        notes: list[str] = []
        muni: Municipality | None = None
        if candidates.ags is not None:
            muni = self._gazetteer.get(candidates.ags)
            if muni is None:
                notes.append(
                    f"AGS {candidates.ags} not in gazetteer: municipality containment "
                    "check and centroid tier unavailable"
                )
        polygon = muni.polygon if muni is not None else None

        # Tier 1 — source coordinates (high)
        if candidates.coordinates is not None:
            c = candidates.coordinates
            try:
                x, y = self._validated(c.x, c.y, c.crs, polygon, geographic=c.geographic)
            except RecordRejected as exc:
                if not degrade_on_reject:
                    raise
                notes.append(
                    f"tier 1 source_coordinates rejected ({exc.reason_code}): {exc.detail}; "
                    "degraded to a lower tier"
                )
            else:
                return self._result(
                    x, y, GeoMethod.SOURCE_COORDINATES, GeoConfidence.HIGH, c.crs, notes
                )

        # Tier 2 — cadastre parcel (high): detect, report, skip. Never faked.
        parcel = parse_parcel_reference(candidates.text)
        if parcel is not None:
            notes.append(
                f"tier 2 cadastre_parcel skipped: parcel reference detected (Gemarkung "
                f"{parcel.gemarkung}, Flur {parcel.flur}, Flurstück {parcel.flurstueck}) but no "
                "ALKIS/WFS endpoint is configured; this is a configuration gap, not a data gap"
            )

        # Tier 3 — address geocode (medium)
        if candidates.address:
            if self._geocoder is None:
                notes.append(
                    "tier 3 address_geocode skipped: address present but no geocoder configured"
                )
            else:
                hit = self._geocoder.geocode(candidates.address, ags=candidates.ags)
                if hit is None:
                    notes.append("tier 3 address_geocode: geocoder returned no match")
                else:
                    try:
                        x, y = self._validated(hit.x, hit.y, hit.crs, polygon)
                    except RecordRejected as exc:
                        if not degrade_on_reject:
                            raise
                        notes.append(
                            f"tier 3 address_geocode result rejected ({exc.reason_code}): "
                            f"{exc.detail}; degraded to a lower tier"
                        )
                    else:
                        return self._result(
                            x, y, GeoMethod.ADDRESS_GEOCODE, GeoConfidence.MEDIUM, hit.crs, notes
                        )

        # Tier 4 — municipality centroid via AGS (low)
        if muni is not None:
            cx, cy = validate_25832(
                *muni.centroid,
                country=GERMANY_ENVELOPE,
                municipality=muni.polygon,
                tol_m=self._tol,
            )
            notes.append("municipality centroid only: not the location the document refers to")
            return self._result(
                cx, cy, GeoMethod.MUNICIPALITY_CENTROID, GeoConfidence.LOW, TARGET_CRS, notes
            )

        # Tier 5 — unresolved (none)
        if candidates.ags is None:
            notes.append("no AGS supplied: municipality centroid tier unavailable")
        notes.append("unresolved: no tier produced a trustworthy location")
        return GeoResolution(
            method=GeoMethod.UNRESOLVED,
            confidence=GeoConfidence.NONE,
            note="; ".join(notes),
        )

    def _validated(
        self,
        x: float,
        y: float,
        crs: str,
        polygon: Polygon | None,
        *,
        geographic: bool = False,
    ) -> tuple[float, float]:
        rx, ry = reproject_wgs84_to_25832(x, y) if geographic else reproject_to_25832(x, y, crs)
        return validate_25832(
            rx, ry, country=GERMANY_ENVELOPE, municipality=polygon, tol_m=self._tol
        )

    @staticmethod
    def _result(
        x: float,
        y: float,
        method: GeoMethod,
        confidence: GeoConfidence,
        source_crs: str,
        notes: list[str],
    ) -> GeoResolution:
        return GeoResolution(
            easting=x,
            northing=y,
            method=method,
            confidence=confidence,
            source_crs=source_crs,
            note="; ".join(notes) if notes else None,
        )
