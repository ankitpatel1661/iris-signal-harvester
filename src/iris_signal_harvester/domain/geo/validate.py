"""Coordinate validation for EPSG:25832 (ETRS89 / UTM zone 32N).

Every check either passes or raises :class:`RecordRejected` with its own
:class:`RejectCode`. Nothing here ever clamps, snaps, rounds or substitutes a
fallback point: a coordinate that fails is rejected as-is, loudly and countably.
Turning a failure into a plausible-looking row is the defect this pipeline exists
to prevent.

The check that matters most is :func:`check_not_latlon` (check 2). A latitude and
longitude written into a projected coordinate field (``x=11.57, y=48.13``) is
finite, so check 1 passes; and it is a perfectly valid point on the globe, so a
geometry-validity test passes too. Without check 2 such a record loads cleanly
and lands near the Gulf of Guinea, geometrically valid and completely wrong,
until someone plots it months later. A legitimate EPSG:25832 easting is a
six-digit number around 500,000; ``abs(x) <= 180 and abs(y) <= 90`` never is, and
this also catches the ``(0.0, 0.0)`` "null island" placeholder.

The national envelope below is a coarse approximation of Germany's outline, not
the exact BKG VG250 boundary. It exists to catch continental-scale errors, while the
per-municipality check (with tolerance) catches the finer ones.

It can be wrong in BOTH directions, and both are real:

* **False accepts.** A point just across a land border can pass: Salzburg, Kufstein,
  Venlo, Heerlen, Eupen, Enschede, Zgorzelec, Slubice and the Danish border town
  Padborg are inside the outline. The error is bounded to roughly 10 km at the Danish
  border and is larger at some borders where the real border is curved.
* **False rejects.** A point very close to the outline can be rejected. The outline
  keeps a margin of about 2 km (Rhine, Danish border) to 15 km (North Sea islands)
  around every German place we regression-test, but a place nearer than that to a
  vertex edge could still be refused. An earlier outline wrongly rejected Kehl,
  Rheinau, Willstaett, Neuried, Lindau, Borkum and Helgoland.

Both directions are pinned by ``tests/test_geo.py``. A rejected valid coordinate is a
dead-lettered record, so a new false reject should be fixed by widening the outline
there, not by relaxing the check.
"""

from __future__ import annotations

import math

from pyproj import Transformer
from shapely.geometry import Point, Polygon

from iris_signal_harvester.domain.models import RejectCode
from iris_signal_harvester.errors import RecordRejected

__all__ = [
    "E_RANGE",
    "GERMANY_ENVELOPE",
    "N_RANGE",
    "check_finite",
    "check_not_latlon",
    "validate_25832",
]

E_RANGE: tuple[float, float] = (265_000.0, 935_000.0)
N_RANGE: tuple[float, float] = (5_200_000.0, 6_120_000.0)

# Coarse outline of Germany as (lat, lon) vertices, WGS84. Approximate by design.
# This is an ENVELOPE APPROXIMATION, not the VG250 boundary (see module docstring).
_GERMANY_OUTLINE_LATLON: tuple[tuple[float, float], ...] = (
    # North Sea / Danish border (clockwise from the north-west). The Danish border is
    # approximated by a stepped line ~3-5 km north of it, so Aventoft, Niebuell, Holnis
    # and List/Ellenbogen (Sylt) pass while Toender and Soenderborg do not.
    (55.10, 8.30),
    (55.10, 8.50),
    (54.92, 8.55),
    (54.92, 9.00),
    (54.895, 9.30),
    (54.895, 9.75),
    # Baltic coast and the eastern border.
    (54.70, 11.20),
    (54.75, 13.00),
    (54.70, 13.75),
    (53.90, 14.40),
    (53.20, 14.45),
    (52.00, 14.80),
    (51.45, 15.05),
    (50.85, 15.05),
    (50.80, 14.30),
    (50.30, 12.10),
    (49.70, 12.50),
    (49.00, 13.85),
    (48.55, 13.85),
    (47.50, 13.10),
    # Alps and Lake Constance. The southern edge sits at 47.50 so Lindau (47.546) and
    # Grenzach-Wyhlen (47.54) are inside.
    (47.27, 10.40),
    (47.50, 9.70),
    (47.50, 7.50),
    # The Rhine strip, ~2 km WEST of the river so the German bank (Breisach, Kehl,
    # Rheinau, Willstaett, Neuried, Rastatt ...) is inside while Strasbourg is not.
    (48.00, 7.52),
    (48.30, 7.66),
    (48.50, 7.75),
    (48.60, 7.80),
    (48.80, 7.98),
    (49.00, 8.14),
    # Saarland / Luxembourg / Belgium / Netherlands.
    (49.10, 6.25),
    (49.50, 6.25),
    (50.10, 6.10),
    (50.80, 6.00),
    (51.00, 5.85),
    (51.90, 6.10),
    (52.50, 6.90),
    (53.30, 7.20),
    # East Frisian coast and islands (Borkum reaches ~6.6 E) and Helgoland (7.88 E).
    (53.50, 6.50),
    (53.80, 6.50),
    (54.15, 7.60),
    (54.35, 7.60),
    (54.60, 8.00),
)


def _build_envelope() -> Polygon:
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
    ring = [to_utm.transform(lon, lat) for lat, lon in _GERMANY_OUTLINE_LATLON]
    return Polygon(ring)


GERMANY_ENVELOPE: Polygon = _build_envelope()
"""Approximate national envelope in EPSG:25832 (NOT the exact VG250 boundary)."""


def check_finite(x: float, y: float) -> None:
    """Check 1: both ordinates are finite numbers."""
    if not (math.isfinite(x) and math.isfinite(y)):
        raise RecordRejected(
            RejectCode.GEO_NON_FINITE, f"coordinate is not finite: x={x!r}, y={y!r}"
        )


def check_not_latlon(x: float, y: float) -> None:
    """Check 2: the pair is not a latitude/longitude written into a projected field.

    Also catches ``(0.0, 0.0)``. See the module docstring for why this is the
    single most important check.
    """
    if abs(x) <= 180.0 and abs(y) <= 90.0:
        raise RecordRejected(
            RejectCode.GEO_LATLON_IN_PROJECTED_FIELD,
            f"(x={x!r}, y={y!r}) looks like longitude/latitude (or a zero placeholder) "
            "written into a projected-coordinate field; a real EPSG:25832 easting is "
            "a six-digit number",
        )


def validate_25832(
    x: float,
    y: float,
    *,
    country: Polygon = GERMANY_ENVELOPE,
    municipality: Polygon | None = None,
    tol_m: float,
) -> tuple[float, float]:
    """Validate an EPSG:25832 point; return it unchanged or raise ``RecordRejected``.

    Checks run in order, each with its own reject code:

    1. finite                         -> ``GEO_NON_FINITE``
    2. not lat/lon (``|x|<=180 and |y|<=90``, includes ``(0, 0)``)
                                      -> ``GEO_LATLON_IN_PROJECTED_FIELD``
    3. inside the CRS domain ``E_RANGE`` x ``N_RANGE``
                                      -> ``GEO_OUT_OF_CRS_DOMAIN``
    4. inside the national envelope   -> ``GEO_OUTSIDE_NATIONAL_ENVELOPE``;
       inside the claimed municipality buffered by ``tol_m``
                                      -> ``GEO_OUTSIDE_CLAIMED_MUNICIPALITY``

    The returned tuple is the input, untouched. The point is never clamped,
    snapped to a boundary, rounded, or replaced with a fallback.
    """
    check_finite(x, y)
    check_not_latlon(x, y)

    if not (E_RANGE[0] <= x <= E_RANGE[1] and N_RANGE[0] <= y <= N_RANGE[1]):
        raise RecordRejected(
            RejectCode.GEO_OUT_OF_CRS_DOMAIN,
            f"(x={x}, y={y}) is outside the EPSG:25832 domain E{E_RANGE} N{N_RANGE}",
        )

    point = Point(x, y)
    if not country.covers(point):
        raise RecordRejected(
            RejectCode.GEO_OUTSIDE_NATIONAL_ENVELOPE,
            f"(x={x}, y={y}) is outside the national envelope",
        )

    if municipality is not None and not municipality.buffer(tol_m).covers(point):
        raise RecordRejected(
            RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY,
            f"(x={x}, y={y}) is outside the claimed municipality (tolerance {tol_m} m)",
        )

    return (x, y)
