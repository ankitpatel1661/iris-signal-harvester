"""Geo validation, gazetteer and tiered resolution. Offline: no test touches the network."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from pyproj import Transformer
from shapely.geometry import Point

from iris_signal_harvester.domain.geo.gazetteer import StaticGazetteer
from iris_signal_harvester.domain.geo.resolver import (
    GeoCandidates,
    GeocodeHit,
    GeoResolver,
    SourceCoordinates,
    parse_parcel_reference,
    reproject_to_25832,
    reproject_wgs84_to_25832,
)
from iris_signal_harvester.domain.geo.validate import (
    E_RANGE,
    GERMANY_ENVELOPE,
    N_RANGE,
    validate_25832,
)
from iris_signal_harvester.domain.models import GeoConfidence, GeoMethod, RejectCode
from iris_signal_harvester.errors import ConfigurationError, RecordRejected

GAZETTEER_PATH = Path(__file__).resolve().parent.parent / "config" / "gazetteer.json"
MUNICH = "09162000"
BERLIN = "11000000"
TOL = 2000.0


def _utm(lat: float, lon: float, epsg: int = 25832) -> tuple[float, float]:
    t = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    x, y = t.transform(lon, lat)
    return float(x), float(y)


MUNICH_XY = _utm(48.1372, 11.5756)
BERLIN_XY = _utm(52.5200, 13.4050)


@pytest.fixture(scope="module")
def gazetteer() -> StaticGazetteer:
    return StaticGazetteer.from_file(GAZETTEER_PATH)


def _resolver(gazetteer: StaticGazetteer, geocoder: object | None = None) -> GeoResolver:
    return GeoResolver(gazetteer, geocoder=geocoder, tolerance_m=TOL)  # type: ignore[arg-type]


class StubGeocoder:
    def __init__(self, hit: GeocodeHit | None) -> None:
        self.hit = hit
        self.calls: list[tuple[str, str | None]] = []

    def geocode(self, address: str, *, ags: str | None) -> GeocodeHit | None:
        self.calls.append((address, ags))
        return self.hit


# --- validate_25832 -----------------------------------------------------------------


class TestValidate:
    def test_valid_munich_point_passes_and_is_returned_unchanged(self, gazetteer: StaticGazetteer):
        muni = gazetteer.get(MUNICH)
        assert muni is not None
        out = validate_25832(*MUNICH_XY, municipality=muni.polygon, tol_m=TOL)
        assert out == MUNICH_XY  # not clamped, snapped or rounded

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_rejected(self, bad: float):
        with pytest.raises(RecordRejected) as e:
            validate_25832(bad, 5_334_000.0, tol_m=TOL)
        assert e.value.reason_code == RejectCode.GEO_NON_FINITE

    def test_latlon_written_into_projected_field_is_rejected(self):
        """The critical check: lat/lon would otherwise load 'cleanly' in the Gulf of Guinea."""
        lat, lon = 48.1372, 11.5756
        with pytest.raises(RecordRejected) as e:
            validate_25832(lon, lat, tol_m=TOL)  # x=lon, y=lat
        assert e.value.reason_code == RejectCode.GEO_LATLON_IN_PROJECTED_FIELD
        with pytest.raises(RecordRejected) as e:
            validate_25832(lat, lon, tol_m=TOL)  # swapped order is just as wrong
        assert e.value.reason_code == RejectCode.GEO_LATLON_IN_PROJECTED_FIELD

    def test_latlon_is_caught_before_the_crs_domain_check(self):
        # Both check 2 and check 3 would fail; the specific, diagnostic code must win.
        with pytest.raises(RecordRejected) as e:
            validate_25832(11.5756, 48.1372, tol_m=TOL)
        assert e.value.reason_code != RejectCode.GEO_OUT_OF_CRS_DOMAIN

    def test_zero_zero_is_caught_by_the_latlon_check(self):
        with pytest.raises(RecordRejected) as e:
            validate_25832(0.0, 0.0, tol_m=TOL)
        assert e.value.reason_code == RejectCode.GEO_LATLON_IN_PROJECTED_FIELD

    @pytest.mark.parametrize(
        ("x", "y"),
        [
            (100_000.0, 5_334_000.0),  # easting below domain
            (1_000_000.0, 5_334_000.0),  # easting above domain
            (691_000.0, 5_000_000.0),  # northing below domain
            (691_000.0, 6_200_000.0),  # northing above domain
            (E_RANGE[0] - 0.001, 5_334_000.0),  # just outside: still rejected, not clamped
            (691_000.0, N_RANGE[1] + 0.001),
        ],
    )
    def test_out_of_crs_domain_rejected(self, x: float, y: float):
        with pytest.raises(RecordRejected) as e:
            validate_25832(x, y, tol_m=TOL)
        assert e.value.reason_code == RejectCode.GEO_OUT_OF_CRS_DOMAIN

    @pytest.mark.parametrize(
        ("name", "lat", "lon"),
        [
            ("Prague", 50.0755, 14.4378),
            ("Zurich", 47.3769, 8.5417),
            ("Strasbourg", 48.5734, 7.7521),
            ("Luxembourg", 49.6116, 6.1319),
        ],
    )
    def test_in_domain_but_outside_germany_rejected(self, name: str, lat: float, lon: float):
        x, y = _utm(lat, lon)
        assert E_RANGE[0] <= x <= E_RANGE[1] and N_RANGE[0] <= y <= N_RANGE[1], name
        with pytest.raises(RecordRejected) as e:
            validate_25832(x, y, tol_m=TOL)
        assert e.value.reason_code == RejectCode.GEO_OUTSIDE_NATIONAL_ENVELOPE

    @pytest.mark.parametrize(
        ("lat", "lon"), [(48.8566, 2.3522), (48.2082, 16.3738), (52.2297, 21.0122)]
    )
    def test_far_foreign_cities_fall_outside_the_crs_domain(self, lat: float, lon: float):
        x, y = _utm(lat, lon)
        with pytest.raises(RecordRejected) as e:
            validate_25832(x, y, tol_m=TOL)
        assert e.value.reason_code == RejectCode.GEO_OUT_OF_CRS_DOMAIN

    # Real German places the previous outline falsely rejected, plus
    # the extremities of the country. Each is a valid source coordinate: rejecting it
    # would dead-letter good data.
    @pytest.mark.parametrize(
        ("name", "lat", "lon"),
        [
            ("Kehl", 48.5736, 7.8153),
            ("Rheinau", 48.6717, 7.9433),
            ("Willstaett", 48.5503, 7.9000),
            ("Neuried (Ortenau)", 48.4817, 7.8000),
            ("Lindau (Bodensee)", 47.5461, 9.6842),
            ("Borkum", 53.5886, 6.6676),
            ("Helgoland", 54.1833, 7.8833),
            ("Breisach", 48.0300, 7.5800),
            ("Weil am Rhein", 47.5936, 7.6064),
            ("Grenzach-Wyhlen", 47.5400, 7.6600),
            ("Freiburg", 47.9960, 7.8500),
            ("Aachen", 50.7753, 6.0839),
            ("Selfkant", 51.0000, 5.9500),
            ("Perl", 49.4673, 6.3725),
            ("Saarbruecken", 49.2402, 6.9969),
            ("Goerlitz", 51.1526, 14.9885),
            ("Flensburg", 54.7937, 9.4469),
            ("Gluecksburg", 54.8333, 9.5500),
            ("Aventoft", 54.8845, 8.6890),
            ("List auf Sylt", 55.0167, 8.4333),
            ("Westerland (Sylt)", 54.9079, 8.3039),
            ("Wangerooge", 53.7900, 7.9000),
            ("Norderney", 53.7090, 7.2200),
            ("Konstanz", 47.6633, 9.1752),
            ("Oberstdorf", 47.4094, 10.2790),
            ("Passau", 48.5667, 13.4667),
            ("Berchtesgaden", 47.6306, 13.0000),
            ("Emden", 53.3671, 7.2063),
            ("Fehmarn", 54.4400, 11.1900),
            ("Sassnitz (Ruegen)", 54.5100, 13.6400),
            ("Frankfurt (Oder)", 52.3471, 14.5506),
        ],
    )
    def test_real_german_border_and_coast_places_are_accepted(
        self, name: str, lat: float, lon: float
    ):
        x, y = _utm(lat, lon)
        assert validate_25832(x, y, tol_m=TOL) == (x, y), name

    # Places that must still be refused. The envelope is coarse in BOTH directions: it
    # does not reject a point just across a land border (Salzburg, Venlo, Slubice ...,
    # not asserted here on purpose), and these are the foreign places it does catch.
    @pytest.mark.parametrize(
        ("name", "lat", "lon"),
        [
            ("Strasbourg cathedral", 48.5819, 7.7507),
            ("Colmar", 48.0794, 7.3585),
            ("Mulhouse", 47.7508, 7.3359),
            ("Toender (DK)", 54.9333, 8.8667),
            ("Soenderborg (DK)", 54.9094, 9.7917),
            ("Luxembourg City", 49.6116, 6.1319),
        ],
    )
    def test_foreign_places_near_the_border_are_still_rejected(
        self, name: str, lat: float, lon: float
    ):
        x, y = _utm(lat, lon)
        with pytest.raises(RecordRejected) as e:
            validate_25832(x, y, tol_m=TOL)
        assert e.value.reason_code == RejectCode.GEO_OUTSIDE_NATIONAL_ENVELOPE, name

    def test_denmark_is_no_longer_accepted_35_km_deep(self):
        # The old (54.95, 10.00) vertex let Soenderborg-sized distances into Denmark.
        x, y = _utm(55.0300, 9.4200)  # Aabenraa area, ~20 km north of the border
        with pytest.raises(RecordRejected):
            validate_25832(x, y, tol_m=TOL)

    def test_the_outline_is_a_valid_simple_polygon(self):
        assert GERMANY_ENVELOPE.is_valid
        assert GERMANY_ENVELOPE.area > 3.0e11  # Germany is ~3.6e11 m2; catches a collapsed ring

    def test_envelope_contains_every_gazetteer_city(self, gazetteer: StaticGazetteer):
        data = GAZETTEER_PATH.read_text(encoding="utf-8")
        assert MUNICH in data
        for ags in (
            "09162000",
            "11000000",
            "02000000",
            "05315000",
            "06412000",
            "08111000",
            "14612000",
            "14713000",
            "05111000",
        ):
            muni = gazetteer.get(ags)
            assert muni is not None, ags
            assert GERMANY_ENVELOPE.covers(Point(*muni.centroid)), ags

    def test_outside_claimed_municipality_rejected(self, gazetteer: StaticGazetteer):
        muni = gazetteer.get(MUNICH)
        assert muni is not None
        # Berlin is inside Germany but nowhere near Munich.
        with pytest.raises(RecordRejected) as e:
            validate_25832(*BERLIN_XY, municipality=muni.polygon, tol_m=TOL)
        assert e.value.reason_code == RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY

    def test_municipality_tolerance_is_a_buffer_not_a_snap(self, gazetteer: StaticGazetteer):
        muni = gazetteer.get(MUNICH)
        assert muni is not None
        _, _, maxx, _ = muni.polygon.bounds
        cy = muni.centroid[1]
        x = maxx + 1_000.0  # 1 km beyond the edge
        assert validate_25832(x, cy, municipality=muni.polygon, tol_m=2_000.0) == (x, cy)
        with pytest.raises(RecordRejected) as e:
            validate_25832(x, cy, municipality=muni.polygon, tol_m=0.0)
        assert e.value.reason_code == RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY


# --- parcel parsing -----------------------------------------------------------------


class TestParcelParsing:
    def test_detects_reference(self):
        ref = parse_parcel_reference(
            "Bebauungsplan, Gemarkung Schwabing, Flur 12, Flurstück 345/6."
        )
        assert ref is not None
        assert (ref.gemarkung, ref.flur, ref.flurstueck) == ("Schwabing", "12", "345/6")

    def test_ascii_umlaut_and_case(self):
        ref = parse_parcel_reference("gemarkung Neu Ulm flur 3 flurstueck 99")
        assert ref is not None
        assert ref.gemarkung == "Neu Ulm"

    @pytest.mark.parametrize("text", [None, "", "Neubau Kita an der Hauptstrasse"])
    def test_no_reference(self, text: str | None):
        assert parse_parcel_reference(text) is None


# --- reprojection -------------------------------------------------------------------


class TestReprojection:
    def test_zone33_input_is_reprojected_and_source_crs_retained(self, gazetteer: StaticGazetteer):
        x33, y33 = _utm(48.1372, 11.5756, epsg=25833)
        res = _resolver(gazetteer).resolve(
            GeoCandidates(coordinates=SourceCoordinates(x33, y33, "EPSG:25833"), ags=MUNICH)
        )
        assert res.method is GeoMethod.SOURCE_COORDINATES
        assert res.confidence is GeoConfidence.HIGH
        assert res.source_crs == "EPSG:25833"
        assert res.easting == pytest.approx(MUNICH_XY[0], abs=0.01)
        assert res.northing == pytest.approx(MUNICH_XY[1], abs=0.01)
        assert (x33, y33) != (res.easting, res.northing)

    def test_zone32_input_passes_through_with_crs_retained(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(
            GeoCandidates(coordinates=SourceCoordinates(*MUNICH_XY, "EPSG:25832"), ags=MUNICH)
        )
        assert (res.easting, res.northing) == MUNICH_XY
        assert res.source_crs == "EPSG:25832"
        assert res.note is None

    def test_latlon_in_zone33_field_is_named_as_latlon(self):
        with pytest.raises(RecordRejected) as e:
            reproject_to_25832(11.5756, 48.1372, "EPSG:25833")
        assert e.value.reason_code == RejectCode.GEO_LATLON_IN_PROJECTED_FIELD

    @pytest.mark.parametrize("crs", ["EPSG:4326", "EPSG:3857", "not-a-crs"])
    def test_unsupported_crs_rejected(self, crs: str):
        with pytest.raises(RecordRejected) as e:
            reproject_to_25832(691_000.0, 5_334_000.0, crs)
        assert e.value.reason_code == RejectCode.GEO_REPROJECTION_FAILED


class TestWgs84Reprojection:
    """The OParl ``Location.geojson`` path: WGS84 lon/lat in, validated EPSG:25832 out."""

    def test_matches_pyproj(self):
        assert reproject_wgs84_to_25832(11.5756, 48.1372) == pytest.approx(MUNICH_XY, abs=1e-6)

    def test_resolver_reprojects_and_validates_a_geographic_point(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(
            GeoCandidates(
                coordinates=SourceCoordinates(11.5756, 48.1372, "EPSG:4326", geographic=True),
                ags=MUNICH,
            )
        )
        assert res.method is GeoMethod.SOURCE_COORDINATES
        assert res.confidence is GeoConfidence.HIGH
        assert res.source_crs == "EPSG:4326"
        assert res.easting == pytest.approx(MUNICH_XY[0], abs=0.01)
        assert res.northing == pytest.approx(MUNICH_XY[1], abs=0.01)

    @pytest.mark.parametrize(
        ("lon", "lat", "code"),
        [
            (math.nan, 48.1, RejectCode.GEO_NON_FINITE),
            (11.5, math.inf, RejectCode.GEO_NON_FINITE),
            (200.0, 48.1, RejectCode.GEO_OUT_OF_CRS_DOMAIN),
            (11.5, 95.0, RejectCode.GEO_OUT_OF_CRS_DOMAIN),
        ],
    )
    def test_invalid_wgs84_is_rejected(self, lon: float, lat: float, code: RejectCode):
        with pytest.raises(RecordRejected) as e:
            reproject_wgs84_to_25832(lon, lat)
        assert e.value.reason_code == code

    @pytest.mark.parametrize(
        ("lon", "lat"),
        [(0.0, 0.0), (48.1372, 11.5756), (-74.0, 40.7)],
        ids=["null-island", "swapped-lat-lon", "new-york"],
    )
    def test_placeholder_swapped_or_foreign_points_are_rejected_not_accepted(
        self, gazetteer: StaticGazetteer, lon: float, lat: float
    ):
        with pytest.raises(RecordRejected):
            _resolver(gazetteer).resolve(
                GeoCandidates(
                    coordinates=SourceCoordinates(lon, lat, "EPSG:4326", geographic=True),
                    ags=MUNICH,
                )
            )

    def test_a_valid_german_point_outside_the_claimed_municipality_is_rejected(
        self, gazetteer: StaticGazetteer
    ):
        with pytest.raises(RecordRejected) as e:
            _resolver(gazetteer).resolve(
                GeoCandidates(
                    coordinates=SourceCoordinates(13.4050, 52.5200, "EPSG:4326", geographic=True),
                    ags=MUNICH,
                )
            )
        assert e.value.reason_code == RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY

    def test_a_projected_pair_still_may_not_claim_wgs84(self):
        """The cassette ``{x, y, crs}`` shape is unchanged: EPSG:4326 there stays unsupported."""
        with pytest.raises(RecordRejected) as e:
            reproject_to_25832(11.5756, 48.1372, "EPSG:4326")
        assert e.value.reason_code == RejectCode.GEO_LATLON_IN_PROJECTED_FIELD


# --- resolver tiers -----------------------------------------------------------------


class TestResolver:
    def test_resolver_rejects_latlon_in_projected_field(self, gazetteer: StaticGazetteer):
        with pytest.raises(RecordRejected) as e:
            _resolver(gazetteer).resolve(
                GeoCandidates(
                    coordinates=SourceCoordinates(11.5756, 48.1372, "EPSG:25832"), ags=MUNICH
                )
            )
        assert e.value.reason_code == RejectCode.GEO_LATLON_IN_PROJECTED_FIELD

    def test_resolver_rejects_zero_zero(self, gazetteer: StaticGazetteer):
        with pytest.raises(RecordRejected) as e:
            _resolver(gazetteer).resolve(
                GeoCandidates(coordinates=SourceCoordinates(0.0, 0.0, "EPSG:25832"))
            )
        assert e.value.reason_code == RejectCode.GEO_LATLON_IN_PROJECTED_FIELD

    def test_coordinates_in_wrong_municipality_rejected(self, gazetteer: StaticGazetteer):
        with pytest.raises(RecordRejected) as e:
            _resolver(gazetteer).resolve(
                GeoCandidates(coordinates=SourceCoordinates(*BERLIN_XY, "EPSG:25832"), ags=MUNICH)
            )
        assert e.value.reason_code == RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY

    def test_degrade_after_rejection_is_honestly_labelled_low_not_high(
        self, gazetteer: StaticGazetteer
    ):
        res = _resolver(gazetteer).resolve(
            GeoCandidates(coordinates=SourceCoordinates(*BERLIN_XY, "EPSG:25832"), ags=MUNICH),
            degrade_on_reject=True,
        )
        assert res.method is GeoMethod.MUNICIPALITY_CENTROID
        assert res.confidence is GeoConfidence.LOW
        assert res.note is not None
        assert RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY in res.note
        muni = gazetteer.get(MUNICH)
        assert muni is not None
        assert (res.easting, res.northing) == muni.centroid  # centroid, not the bad point

    def test_degrade_without_ags_is_unresolved_not_a_fallback_point(
        self, gazetteer: StaticGazetteer
    ):
        res = _resolver(gazetteer).resolve(
            GeoCandidates(coordinates=SourceCoordinates(0.0, 0.0, "EPSG:25832")),
            degrade_on_reject=True,
        )
        assert res.method is GeoMethod.UNRESOLVED
        assert res.confidence is GeoConfidence.NONE
        assert res.easting is None and res.northing is None
        assert res.note is not None and RejectCode.GEO_LATLON_IN_PROJECTED_FIELD in res.note

    def test_parcel_reference_skips_tier_two_with_note(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(
            GeoCandidates(text="Gemarkung Schwabing, Flur 12, Flurstück 345/6", ags=MUNICH)
        )
        # Tier 2 is NOT faked: we fall to the honest low-confidence centroid.
        assert res.method is GeoMethod.MUNICIPALITY_CENTROID
        assert res.confidence is GeoConfidence.LOW
        assert res.note is not None
        assert "cadastre_parcel skipped" in res.note
        assert "ALKIS" in res.note
        assert "Schwabing" in res.note
        assert "configuration gap" in res.note

    def test_parcel_reference_and_no_ags_is_unresolved_with_note(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(
            GeoCandidates(text="Gemarkung Schwabing, Flur 12, Flurstück 345/6")
        )
        assert res.method is GeoMethod.UNRESOLVED
        assert res.note is not None and "ALKIS" in res.note

    def test_address_without_geocoder_records_note(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(
            GeoCandidates(address="Marienplatz 1, München", ags=MUNICH)
        )
        assert res.method is GeoMethod.MUNICIPALITY_CENTROID
        assert res.note is not None and "no geocoder configured" in res.note

    def test_geocoder_stub_gives_medium_confidence(self, gazetteer: StaticGazetteer):
        stub = StubGeocoder(GeocodeHit(*MUNICH_XY))
        res = _resolver(gazetteer, stub).resolve(
            GeoCandidates(address="Marienplatz 1, München", ags=MUNICH)
        )
        assert stub.calls == [("Marienplatz 1, München", MUNICH)]
        assert res.method is GeoMethod.ADDRESS_GEOCODE
        assert res.confidence is GeoConfidence.MEDIUM
        assert (res.easting, res.northing) == MUNICH_XY

    def test_geocoder_no_match_falls_to_centroid_with_note(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer, StubGeocoder(None)).resolve(
            GeoCandidates(address="Nowhere 1", ags=MUNICH)
        )
        assert res.method is GeoMethod.MUNICIPALITY_CENTROID
        assert res.note is not None and "no match" in res.note

    def test_geocoder_result_is_validated_too(self, gazetteer: StaticGazetteer):
        stub = StubGeocoder(GeocodeHit(*BERLIN_XY))
        with pytest.raises(RecordRejected) as e:
            _resolver(gazetteer, stub).resolve(GeoCandidates(address="X", ags=MUNICH))
        assert e.value.reason_code == RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY

    def test_tier1_wins_over_geocoder(self, gazetteer: StaticGazetteer):
        stub = StubGeocoder(GeocodeHit(*MUNICH_XY))
        res = _resolver(gazetteer, stub).resolve(
            GeoCandidates(
                coordinates=SourceCoordinates(*MUNICH_XY, "EPSG:25832"), address="X", ags=MUNICH
            )
        )
        assert res.method is GeoMethod.SOURCE_COORDINATES
        assert stub.calls == []

    def test_municipality_centroid_only(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(GeoCandidates(ags=MUNICH))
        muni = gazetteer.get(MUNICH)
        assert muni is not None
        assert res.method is GeoMethod.MUNICIPALITY_CENTROID
        assert res.confidence is GeoConfidence.LOW
        assert (res.easting, res.northing) == muni.centroid
        assert res.source_crs == "EPSG:25832"

    def test_unresolved_has_no_geometry_and_confidence_none(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(GeoCandidates())
        assert res.method is GeoMethod.UNRESOLVED
        assert res.confidence is GeoConfidence.NONE
        assert res.easting is None and res.northing is None
        assert not res.resolved
        assert res.note

    def test_unknown_ags_is_unresolved_with_note(self, gazetteer: StaticGazetteer):
        res = _resolver(gazetteer).resolve(GeoCandidates(ags="99999999"))
        assert res.method is GeoMethod.UNRESOLVED
        assert res.confidence is GeoConfidence.NONE
        assert res.note is not None and "99999999" in res.note


# --- gazetteer ----------------------------------------------------------------------


class TestGazetteer:
    def test_loads_real_ags_with_names(self, gazetteer: StaticGazetteer):
        muni = gazetteer.get(MUNICH)
        assert muni is not None
        assert muni.name == "München"
        assert muni.bundesland == "Bayern"
        assert muni.polygon.covers(Point(*muni.centroid))
        assert gazetteer.get("00000000") is None

    def test_bad_file_raises_configuration_error(self, tmp_path: Path):
        bad = tmp_path / "g.json"
        bad.write_text('{"crs": "EPSG:4326", "municipalities": []}', encoding="utf-8")
        with pytest.raises(ConfigurationError):
            StaticGazetteer.from_file(bad)
        with pytest.raises(ConfigurationError):
            StaticGazetteer.from_file(tmp_path / "missing.json")
