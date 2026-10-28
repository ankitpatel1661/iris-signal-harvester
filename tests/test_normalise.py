"""Normalisation: ``RawDocument`` -> ``CanonicalSignal``, or an honest rejection.

Every test here is offline. The taxonomy, the gazetteer and the thresholds are the
committed configuration files, not stubs, because the thing being tested is whether
the real configuration produces the contract the sink expects.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.hashing import content_hash
from iris_signal_harvester.domain.models import (
    Body,
    GeoConfidence,
    GeoMethod,
    PublishedAtOrigin,
    RawDocument,
    RejectCode,
)
from iris_signal_harvester.domain.normalise import (
    DocumentTypes,
    Normaliser,
    Verdict,
    payload_coordinates,
    payload_text,
)
from iris_signal_harvester.errors import ConfigurationError

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
NBSP = chr(0xA0)
FIGSP = chr(0x2007)
ZWSP = chr(0x200B)
RUN = uuid.UUID("11111111-1111-1111-1111-111111111111")

SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    taxonomy_path=REPO / "config" / "taxonomy.yaml",
    thresholds_path=REPO / "config" / "thresholds.yaml",
    bodies_path=REPO / "config" / "bodies.yaml",
)
MUNICH = Body(
    id="https://oparl.example.org/oparl/v1.0/body/1",
    name="Musterstadt",
    ags="09162000",
    ars="091620000000",
    licence="https://creativecommons.org/licenses/by/4.0/",
    paper_endpoint="https://oparl.example.org/oparl/v1.0/body/1/list/paper",
)
# A point inside the committed gazetteer's München polygon.
MUNICH_POINT = {"x": 691611.0, "y": 5334758.0, "crs": "EPSG:25832"}
PV_TITLE = "Antrag: Freiflächen-Photovoltaikanlage an der Musterstraße"


@pytest.fixture(scope="module")
def normaliser() -> Normaliser:
    return Normaliser.from_settings(SETTINGS, gazetteer_path=REPO / "config" / "gazetteer.json")


def document(**payload: Any) -> RawDocument:
    base: dict[str, Any] = {
        "id": "https://oparl.example.org/oparl/v1.0/paper/1000",
        "type": "https://schema.oparl.org/1.0/Paper",
        "reference": "01-20/B00001",
        "name": PV_TITLE,
        "paperType": "BA-Antrag",
        "date": "2026-05-12",
        "modified": "2026-06-01T09:00:00+02:00",
    }
    base.update(payload)
    last_modified = base.pop("__http_last_modified", None)
    return RawDocument(
        source_system="fixture",
        source_body_id=MUNICH.id,
        source_record_id=str(base.get("reference") or "01-20/B00001"),
        source_url=str(base.get("id") or "https://oparl.example.org/paper/1"),
        payload=base,
        http_last_modified=last_modified,
        licence=MUNICH.licence,
    )


# ---------------------------------------------------------------------------
# the happy contract
# ---------------------------------------------------------------------------


def test_emits_a_canonical_signal_with_the_body_in_the_natural_key(
    normaliser: Normaliser,
) -> None:
    outcome = normaliser.normalise(document(), MUNICH, RUN, now=NOW)
    assert outcome.verdict is Verdict.EMITTED
    signal = outcome.signal
    assert signal is not None
    # source_body_id is part of the natural key and is required: an OParl
    # `reference` is only unique within its body.
    assert signal.natural_key == ("fixture", MUNICH.id, "01-20/B00001")
    assert signal.source_body_id == MUNICH.id
    assert signal.signal_type == "pv_open_space"
    assert signal.taxonomy_version
    assert len(signal.content_hash) == 64
    assert signal.harvest_run_id == RUN
    assert signal.source_licence == MUNICH.licence


def test_municipality_attributes_come_from_the_gazetteer(normaliser: Normaliser) -> None:
    signal = normaliser.normalise(document(), MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.ags == "09162000"
    assert signal.municipality_name == "München"
    assert signal.bundesland == "Bayern"


def test_a_document_matching_nothing_is_below_threshold_not_rejected(
    normaliser: Normaliser,
) -> None:
    outcome = normaliser.normalise(
        document(name="Protokoll der Sitzung des Ältestenrates"), MUNICH, RUN, now=NOW
    )
    # The distinction the whole quality report rests on.
    assert outcome.verdict is Verdict.BELOW_THRESHOLD
    assert outcome.signal is None
    assert outcome.reject is None


# ---------------------------------------------------------------------------
# NUL scrubbing happens BEFORE hashing
# ---------------------------------------------------------------------------


def test_nuls_are_scrubbed_before_the_hash_is_computed(normaliser: Normaliser) -> None:
    """The stored payload must be exactly what was hashed.

    PostgreSQL cannot store U+0000, so the payload is scrubbed on the way in. If the
    hash were taken over the unscrubbed text, the row's `content_hash` would describe
    bytes that are not in the database, and re-deriving a record from `raw_payload`
    would produce a different hash — breaking both change detection and the promise
    that nothing needs re-fetching.
    """
    dirty = normaliser.normalise(
        document(name="Freiflächen\u0000-Photovoltaikanlage\u0000 Musterstraße"),
        MUNICH,
        RUN,
        now=NOW,
    ).signal
    clean = normaliser.normalise(
        document(name="Freiflächen-Photovoltaikanlage Musterstraße"), MUNICH, RUN, now=NOW
    ).signal
    assert dirty is not None and clean is not None
    assert "\u0000" not in dirty.title
    assert "\u0000" not in dirty.raw_payload["name"]
    assert dirty.content_hash == clean.content_hash
    # And the hash really is a function of the stored payload's text.
    assert dirty.content_hash == content_hash(
        {name: getattr(dirty, name) for name in type(dirty).model_fields}
    )


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


def test_payload_date_wins_and_sets_its_provenance(normaliser: Normaliser) -> None:
    signal = normaliser.normalise(document(), MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.published_at_origin is PublishedAtOrigin.PAYLOAD_FIELD
    assert signal.published_at is not None


def test_null_date_falls_through_to_the_http_header(normaliser: Normaliser) -> None:
    doc = document(date=None, __http_last_modified=datetime(2026, 4, 1, 6, 0, tzinfo=UTC))
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.published_at_origin is PublishedAtOrigin.HTTP_HEADER
    assert signal.published_at == datetime(2026, 4, 1, 6, 0, tzinfo=UTC)


def test_no_date_anywhere_is_null_plus_unknown_and_stays_consistent(
    normaliser: Normaliser,
) -> None:
    signal = normaliser.normalise(document(date=None), MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.published_at is None
    assert signal.published_at_origin is PublishedAtOrigin.UNKNOWN


def test_the_real_sentinel_date_is_rejected_not_substituted(normaliser: Normaliser) -> None:
    outcome = normaliser.normalise(
        document(date="-0001-11-30T00:00:00+00:53"), MUNICH, RUN, now=NOW
    )
    assert outcome.verdict is Verdict.REJECTED
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.DATE_OUT_OF_RANGE
    assert outcome.reject.harvest_run_id == RUN
    assert outcome.reject.source_body_id == MUNICH.id


def test_a_meeting_date_is_the_third_tier(normaliser: Normaliser) -> None:
    doc = document(date=None, meeting=[{"start": "12.05.2026"}])
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.published_at_origin is PublishedAtOrigin.DERIVED_FROM_MEETING


# ---------------------------------------------------------------------------
# geography
# ---------------------------------------------------------------------------


def test_tier_one_reads_the_cassette_location_object(normaliser: Normaliser) -> None:
    signal = normaliser.normalise(document(location=MUNICH_POINT), MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.SOURCE_COORDINATES
    assert signal.geo_confidence is GeoConfidence.HIGH
    assert signal.easting == pytest.approx(691611.0)
    assert signal.source_crs == "EPSG:25832"


def test_zone33_coordinates_are_reprojected_and_the_source_crs_retained(
    normaliser: Normaliser,
) -> None:
    doc = document(location={"x": 245249.44, "y": 5337222.41, "crs": "EPSG:25833"})
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.source_crs == "EPSG:25833"
    assert signal.easting is not None and 680_000 < signal.easting < 700_000


def test_an_out_of_domain_coordinate_is_rejected_never_clamped(
    normaliser: Normaliser,
) -> None:
    doc = document(location={"x": 1234567.0, "y": 9876543.0, "crs": "EPSG:25832"})
    outcome = normaliser.normalise(doc, MUNICH, RUN, now=NOW)
    assert outcome.verdict is Verdict.REJECTED
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.GEO_OUT_OF_CRS_DOMAIN
    assert outcome.signal is None


def test_latlon_written_into_the_projected_field_is_rejected(normaliser: Normaliser) -> None:
    doc = document(location={"x": 11.576, "y": 48.137, "crs": "EPSG:25832"})
    outcome = normaliser.normalise(doc, MUNICH, RUN, now=NOW)
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.GEO_LATLON_IN_PROJECTED_FIELD


def test_no_coordinates_falls_to_the_municipality_centroid(normaliser: Normaliser) -> None:
    signal = normaliser.normalise(document(), MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.MUNICIPALITY_CENTROID
    assert signal.geo_confidence is GeoConfidence.LOW


def test_without_an_ags_the_record_is_honestly_unresolved(normaliser: Normaliser) -> None:
    anonymous = Body(id="body/x", name="Unregistered")
    signal = normaliser.normalise(document(), anonymous, RUN, now=NOW).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.UNRESOLVED
    assert signal.geo_confidence is GeoConfidence.NONE
    assert signal.easting is None and signal.northing is None


def test_a_misshapen_ags_is_dropped_with_a_note_not_a_rejection(
    normaliser: Normaliser,
) -> None:
    """One bad registry field must not cost a whole municipality's documents."""
    broken = Body(id="body/y", name="Tippfehlerstadt", ags="0916200")
    outcome = normaliser.normalise(document(), broken, RUN, now=NOW)
    assert outcome.verdict is Verdict.EMITTED
    assert outcome.signal is not None
    assert outcome.signal.ags is None
    assert any("misshapen AGS" in note for note in outcome.notes)


def test_coordinates_without_a_crs_are_not_assumed_to_be_25832(
    normaliser: Normaliser,
) -> None:
    doc = document(location={"x": 691611.0, "y": 5334758.0})
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    # Falls to tier 4 rather than guessing a CRS.
    assert signal.geo_method is GeoMethod.MUNICIPALITY_CENTROID


# ---------------------------------------------------------------------------
# schema gates
# ---------------------------------------------------------------------------


def test_a_record_with_no_usable_title_is_rejected(normaliser: Normaliser) -> None:
    outcome = normaliser.normalise(document(name=None), MUNICH, RUN, now=NOW)
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.MISSING_REQUIRED_FIELD


def test_a_relative_source_url_is_rejected_with_its_own_code() -> None:
    normaliser = Normaliser.from_settings(
        SETTINGS, gazetteer_path=REPO / "config" / "gazetteer.json"
    )
    doc = RawDocument(
        source_system="fixture",
        source_body_id=MUNICH.id,
        source_record_id="X-1",
        source_url="/relative/path",
        payload={"name": PV_TITLE},
    )
    outcome = normaliser.normalise(doc, MUNICH, RUN, now=NOW)
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.INVALID_SOURCE_URL


def test_normalisation_never_raises_for_hostile_input(normaliser: Normaliser) -> None:
    """A sweep over hundreds of councils must not die on one bad record."""
    hostile = document(
        name=PV_TITLE,
        date=["not", "a", "date"],
        location={"x": "not-a-number", "y": None, "crs": 17},
        keyword={"not": "a list"},
        meeting="https://example.org/meeting/1",
    )
    outcome = normaliser.normalise(hostile, MUNICH, RUN, now=NOW)
    assert outcome.verdict in (Verdict.EMITTED, Verdict.REJECTED)


# ---------------------------------------------------------------------------
# tier 1, the real OParl Location shape (location.geojson, WGS84 Point)
# ---------------------------------------------------------------------------

# Marienplatz, München, as GeoJSON: [longitude, latitude].
GEOJSON_FEATURE = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [11.5756, 48.1372]},
}


def test_tier_one_reads_an_oparl_location_geojson_feature(normaliser: Normaliser) -> None:
    doc = document(
        location={"type": "https://schema.oparl.org/1.0/Location", "geojson": GEOJSON_FEATURE}
    )
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.SOURCE_COORDINATES
    assert signal.geo_confidence is GeoConfidence.HIGH
    assert signal.source_crs == "EPSG:4326"
    # Reprojected to EPSG:25832: it must equal what pyproj gives, not the raw degrees.
    from pyproj import Transformer

    ex, ey = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True).transform(
        11.5756, 48.1372
    )
    assert signal.easting == pytest.approx(ex, abs=0.01)
    assert signal.northing == pytest.approx(ey, abs=0.01)


def test_tier_one_reads_a_bare_geojson_point_geometry(normaliser: Normaliser) -> None:
    doc = document(
        location={"geojson": {"type": "Point", "coordinates": [11.5756, 48.1372, 519.0]}}
    )
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.SOURCE_COORDINATES
    assert signal.source_crs == "EPSG:4326"


def test_a_paper_location_list_with_one_location_is_used(normaliser: Normaliser) -> None:
    doc = document(location=[{"geojson": GEOJSON_FEATURE}])
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.SOURCE_COORDINATES


def test_a_paper_location_list_with_several_locations_is_not_guessed_at(
    normaliser: Normaliser,
) -> None:
    doc = document(location=[{"geojson": GEOJSON_FEATURE}, {"geojson": GEOJSON_FEATURE}])
    signal = normaliser.normalise(doc, MUNICH, RUN, now=NOW).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.MUNICIPALITY_CENTROID


@pytest.mark.parametrize(
    "geojson",
    [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[11, 48], [12, 48], [11, 49], [11, 48]]],
            },
        },
        {"type": "Feature", "geometry": None},
        {"type": "FeatureCollection", "features": [GEOJSON_FEATURE]},
        {"type": "Point", "coordinates": [11.5756]},
        {"type": "Point", "coordinates": ["11.5756", "48.1372"]},
        {"type": "Point", "coordinates": [True, False]},
        "not-an-object",
    ],
    ids=["polygon", "null-geometry", "collection", "one-ordinate", "strings", "bools", "string"],
)
def test_geojson_that_is_not_a_usable_point_is_ignored_not_invented(
    normaliser: Normaliser, geojson: Any
) -> None:
    signal = normaliser.normalise(
        document(location={"geojson": geojson}), MUNICH, RUN, now=NOW
    ).signal
    assert signal is not None
    assert signal.geo_method is GeoMethod.MUNICIPALITY_CENTROID


@pytest.mark.parametrize(
    ("coordinates", "code"),
    [
        ([0.0, 0.0], RejectCode.GEO_OUT_OF_CRS_DOMAIN),
        ([48.1372, 11.5756], RejectCode.GEO_OUT_OF_CRS_DOMAIN),
        ([240.0, 48.1372], RejectCode.GEO_OUT_OF_CRS_DOMAIN),
        ([13.4050, 52.5200], RejectCode.GEO_OUTSIDE_CLAIMED_MUNICIPALITY),
    ],
    ids=["null-island", "swapped-lat-lon", "longitude-out-of-range", "wrong-municipality"],
)
def test_a_bad_geojson_point_is_rejected_never_clamped(
    normaliser: Normaliser, coordinates: list[float], code: RejectCode
) -> None:
    geojson = {"type": "Point", "coordinates": coordinates}
    outcome = normaliser.normalise(document(location={"geojson": geojson}), MUNICH, RUN, now=NOW)
    assert outcome.verdict is Verdict.REJECTED
    assert outcome.reject is not None
    assert outcome.reject.reason_code is code


def test_the_cassette_shape_wins_when_both_shapes_are_present() -> None:
    found = payload_coordinates(
        {"location": {**MUNICH_POINT, "geojson": GEOJSON_FEATURE}},
    )
    assert found is not None and found.crs == "EPSG:25832" and found.geographic is False


def test_a_cassette_pair_without_a_crs_falls_through_to_geojson() -> None:
    found = payload_coordinates({"location": {"x": 1.0, "y": 2.0, "geojson": GEOJSON_FEATURE}})
    assert found is not None
    assert (found.x, found.y, found.crs, found.geographic) == (11.5756, 48.1372, "EPSG:4326", True)


def test_a_cassette_pair_declaring_wgs84_is_still_rejected(normaliser: Normaliser) -> None:
    doc = document(location={"x": 11.5756, "y": 48.1372, "crs": "EPSG:4326"})
    outcome = normaliser.normalise(doc, MUNICH, RUN, now=NOW)
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.GEO_LATLON_IN_PROJECTED_FIELD


def test_a_paper_with_no_location_at_all_does_not_fire_tier_one() -> None:
    """The probed live endpoint carries no ``location`` key on any paper."""
    assert payload_coordinates({"name": "x"}) is None
    assert payload_coordinates({"location": None}) is None
    assert payload_coordinates({"location": []}) is None


# ---------------------------------------------------------------------------
# payload mapping helpers
# ---------------------------------------------------------------------------


def test_payload_text_reads_both_oparl_and_ratsdokumente_spellings() -> None:
    assert payload_text({"name": "A"})["title"] == "A"
    assert payload_text({"title": "B"})["title"] == "B"
    assert payload_text({"description": "C"})["summary"] == "C"
    assert payload_text({"keyword": ["Energie", "PV"]})["body"] == "Energie PV"


def test_payload_coordinates_requires_a_declared_crs() -> None:
    assert payload_coordinates({"location": {"x": 1.0, "y": 2.0}}) is None
    found = payload_coordinates({"location": {"x": 1.0, "y": 2.0, "crs": "EPSG:25832"}})
    assert found is not None and found.crs == "EPSG:25832"


# ---------------------------------------------------------------------------
# document-type vocabulary
# ---------------------------------------------------------------------------


def _signal_of(normaliser: Normaliser, **payload: Any) -> Any:
    outcome = normaliser.normalise(document(**payload), MUNICH, RUN, now=NOW)
    assert outcome.verdict is Verdict.EMITTED
    return outcome


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BA-Antrag", "Antrag"),
        ("Stadtratsantrag", "Antrag"),
        ("Antrag", "Antrag"),
        ("Beschlussvorlage", "Beschlussvorlage"),
        ("  beschlussvorlage ", "Beschlussvorlage"),
        ("Vorlage", "Vorlage"),
        ("Niederschrift", "Niederschrift"),
        ("Mitteilung", "Mitteilung"),
        ("Anfrage", "Anfrage"),
        ("Bericht", "Bericht"),
        ("Änderungsantrag", "Antrag"),
        ("Aenderungsantrag", "Antrag"),  # umlaut-folded, like the taxonomy
        (f"Kleine{NBSP}Anfrage", "Anfrage"),  # NBSP inside the raw value
    ],
)
def test_known_paper_types_map_to_the_controlled_vocabulary(
    normaliser: Normaliser, raw: str, expected: str
) -> None:
    outcome = _signal_of(normaliser, paperType=raw)
    assert outcome.signal.document_type == expected
    assert outcome.document_type_unmapped is False
    # The raw value is preserved verbatim in the payload.
    assert outcome.signal.raw_payload["paperType"] == raw


def test_an_unknown_paper_type_becomes_unmapped_and_is_exposed_for_counting(
    normaliser: Normaliser,
) -> None:
    outcome = _signal_of(normaliser, paperType="Sonderdruck zur Haushaltslage")
    assert outcome.signal.document_type == "unmapped"
    assert outcome.document_type_unmapped is True
    assert outcome.document_type_raw == "Sonderdruck zur Haushaltslage"
    assert outcome.signal.raw_payload["paperType"] == "Sonderdruck zur Haushaltslage"


def test_unmapping_is_exact_not_substring_so_nothing_is_guessed(normaliser: Normaliser) -> None:
    outcome = _signal_of(normaliser, paperType="Antrag auf Anhörung zum Haushalt")
    assert outcome.signal.document_type == "unmapped"


def test_an_absent_paper_type_stays_null_and_is_not_counted_as_unmapped(
    normaliser: Normaliser,
) -> None:
    for payload in ({"paperType": None}, {"paperType": "   "}):
        outcome = _signal_of(normaliser, **payload)
        assert outcome.signal.document_type is None
        assert outcome.document_type_unmapped is False
        assert outcome.document_type_raw is None


def test_document_type_vocabulary_file_is_consistent() -> None:
    types = DocumentTypes.load(REPO / "config" / "document_types.yaml")
    required = {
        "Beschlussvorlage",
        "Vorlage",
        "Antrag",
        "Niederschrift",
        "Mitteilung",
        "Anfrage",
        "Bericht",
    }
    assert required <= set(types.vocabulary)
    assert types.unmapped == "unmapped"
    assert types.map("BA-Antrag") == types.map("Stadtratsantrag") == "Antrag"
    assert types.map(None) is None


def test_an_alias_under_two_types_is_refused_at_load_time() -> None:
    with pytest.raises(ConfigurationError, match="ambiguous"):
        DocumentTypes({"Antrag": ["Eingabe"], "Anfrage": ["eingabe"]})


def test_the_unmapped_bucket_cannot_be_a_real_type() -> None:
    with pytest.raises(ConfigurationError):
        DocumentTypes({"Antrag": ["unmapped"]})


# ---------------------------------------------------------------------------
# relevance before date
# ---------------------------------------------------------------------------


def test_an_irrelevant_document_with_a_corrupt_date_is_below_threshold_not_rejected(
    normaliser: Normaliser,
) -> None:
    outcome = normaliser.normalise(
        document(name="Haushaltssatzung 2026 Kindergarten", date="0001-01-01"),
        MUNICH,
        RUN,
        now=NOW,
    )
    assert outcome.verdict is Verdict.BELOW_THRESHOLD
    assert outcome.reject is None


def test_a_relevant_document_with_a_corrupt_date_is_still_rejected(
    normaliser: Normaliser,
) -> None:
    outcome = normaliser.normalise(document(date="0001-01-01"), MUNICH, RUN, now=NOW)
    assert outcome.verdict is Verdict.REJECTED
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.DATE_OUT_OF_RANGE


def test_an_irrelevant_document_with_a_bad_coordinate_is_below_threshold(
    normaliser: Normaliser,
) -> None:
    outcome = normaliser.normalise(
        document(
            name="Haushaltssatzung 2026 Kindergarten",
            location={"x": 11.57, "y": 48.13, "crs": "EPSG:25832"},
        ),
        MUNICH,
        RUN,
        now=NOW,
    )
    assert outcome.verdict is Verdict.BELOW_THRESHOLD


# ---------------------------------------------------------------------------
# whitespace normalisation
# ---------------------------------------------------------------------------


def test_title_whitespace_is_really_normalised(normaliser: Normaliser) -> None:
    messy = f"  Antrag:{NBSP}Freiflächen-Photovoltaikanlage \n\t an der{FIGSP}Musterstraße{ZWSP}  "
    outcome = _signal_of(normaliser, name=messy)
    assert outcome.signal.title == "Antrag: Freiflächen-Photovoltaikanlage an der Musterstraße"
    # ... while the verbatim value stays in the raw payload.
    assert outcome.signal.raw_payload["name"] == messy


def test_a_whitespace_only_title_edit_does_not_change_the_content_hash(
    normaliser: Normaliser,
) -> None:
    clean = _signal_of(normaliser, name=PV_TITLE).signal
    edited = _signal_of(normaliser, name=PV_TITLE.replace(" ", f"{NBSP}{NBSP}") + "\n").signal
    assert clean.content_hash == edited.content_hash
    assert clean.title == edited.title


def test_a_real_title_change_still_changes_the_hash(normaliser: Normaliser) -> None:
    a = _signal_of(normaliser, name=PV_TITLE).signal
    b = _signal_of(normaliser, name=PV_TITLE + " (Ergänzung)").signal
    assert a.content_hash != b.content_hash


# ---------------------------------------------------------------------------
# values PostgreSQL cannot store are rejected BEFORE hashing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("nan", {"score": float("nan")}),
        ("infinity", {"nested": {"values": [1.0, float("inf")]}}),
        ("negative infinity", {"score": float("-inf")}),
        ("surrogate in a value", {"description": "broken \ud800 text"}),
        ("surrogate in the title", {"name": PV_TITLE + " \udc80"}),
        ("surrogate in a key", {"bad\ud800key": "value"}),
    ],
)
def test_unstorable_values_are_rejected_before_hashing(
    normaliser: Normaliser, label: str, payload: dict[str, Any]
) -> None:
    """``content_hash`` raises on these; they must become a dead letter, not an exception."""
    outcome = normaliser.normalise(document(**payload), MUNICH, RUN, now=NOW)
    assert outcome.verdict is Verdict.REJECTED, label
    assert outcome.signal is None
    assert outcome.reject is not None
    assert outcome.reject.reason_code is RejectCode.SCHEMA_VALIDATION_FAILED
    assert "not storable in PostgreSQL" in outcome.reject.reason_detail


def test_the_unstorable_shapes_really_do_break_hashing() -> None:
    """Guards the premise: if hashing stopped raising, this ordering would not matter."""
    with pytest.raises((ValueError, UnicodeEncodeError)):
        content_hash({"title": float("nan")})
    with pytest.raises((ValueError, UnicodeEncodeError)):
        content_hash({"title": "x\ud800"})


def test_an_irrelevant_document_with_a_nan_is_still_below_threshold(
    normaliser: Normaliser,
) -> None:
    outcome = normaliser.normalise(
        document(name="Sitzung des Kulturausschusses", score=float("nan")), MUNICH, RUN, now=NOW
    )
    assert outcome.verdict is Verdict.BELOW_THRESHOLD
