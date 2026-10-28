"""content_hash: sensitive to meaning, blind to volatile transport fields."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from iris_signal_harvester.domain.hashing import (
    HASH_VERSION,
    SEMANTIC_FIELDS,
    VOLATILE_FIELDS,
    content_hash,
)
from iris_signal_harvester.domain.models import (
    GeoConfidence,
    GeoMethod,
    Hit,
    PublishedAtOrigin,
)

# Pinned. If this changes, every stored content_hash in production changes with it:
# that is a HASH_VERSION bump and a migration, never a casual test edit.
PINNED_FIXTURE_DIGEST = "89e153dcb2a4d56a2de5971d56fc6ff19bad503986d80a730e9a48d2deffe4de"

# The exact canonical JSON the digest above is taken over, written out by hand so
# the byte format (sorted keys, no spaces, explicit nulls, NFKC, UTF-8) is pinned
# independently of the implementation.
PINNED_FIXTURE_CANONICAL = (
    '{"ags":"09162000","ars":null,"bundesland":"Bayern","document_type":"Beschlussvorlage",'
    '"easting":691234,"geo_confidence":"medium","geo_method":"municipality_centroid",'
    '"kreis":null,"matched_keywords":[{"field":"title","offset":0,"signal_type":"grid_expansion",'
    '"surface":"Umspannwerk","term":"Umspannwerk","weight":0.8}],'
    '"municipality_name":"München","northing":5334567.5,"published_at":"2024-03-05T10:00:00+00:00",'
    '"published_at_origin":"payload_field","relevance_score":0.8,"signal_type":"grid_expansion",'
    '"source_body_id":"body-1","source_crs":"EPSG:25832","source_licence":null,'
    '"source_record_id":"paper-42","source_system":"oparl",'
    '"source_url":"https://example.org/oparl/paper/42","summary":null,'
    '"title":"Neubau Umspannwerk Nord"}'
)


def _fixture() -> dict[str, Any]:
    return {
        "source_system": "oparl",
        "source_body_id": "body-1",
        "source_record_id": "paper-42",
        "source_url": "https://example.org/oparl/paper/42",
        "source_licence": None,
        "title": "Neubau Umspannwerk Nord",
        "summary": None,
        "document_type": "Beschlussvorlage",
        "published_at": datetime(2024, 3, 5, 10, 0, tzinfo=UTC),
        "published_at_origin": PublishedAtOrigin.PAYLOAD_FIELD,
        "ags": "09162000",
        "ars": None,
        "municipality_name": "München",
        "kreis": None,
        "bundesland": "Bayern",
        "easting": 691234.0,
        "northing": 5334567.5,
        "geo_method": GeoMethod.MUNICIPALITY_CENTROID,
        "geo_confidence": GeoConfidence.MEDIUM,
        "source_crs": "EPSG:25832",
        "signal_type": "grid_expansion",
        "matched_keywords": (
            Hit(
                term="Umspannwerk",
                surface="Umspannwerk",
                field="title",
                offset=0,
                weight=0.8,
                signal_type="grid_expansion",
            ),
        ),
        "relevance_score": 0.8,
        "taxonomy_version": "2024.1",
    }


# --- stability -------------------------------------------------------------


def test_hash_version_is_one() -> None:
    assert HASH_VERSION == 1


def test_pinned_literal_digest() -> None:
    assert content_hash(_fixture()) == PINNED_FIXTURE_DIGEST


def test_pinned_digest_matches_hand_written_canonical_json() -> None:
    """Pins the byte format itself, not just the implementation's output."""
    digest = hashlib.sha256(PINNED_FIXTURE_CANONICAL.encode("utf-8")).hexdigest()
    assert digest == PINNED_FIXTURE_DIGEST
    assert json.loads(PINNED_FIXTURE_CANONICAL).keys() == set(SEMANTIC_FIELDS)


def test_hash_is_stable_across_processes() -> None:
    """A fresh interpreter with a different PYTHONHASHSEED yields the same digest."""
    code = (
        "import sys; sys.path.insert(0, 'tests');"
        "from test_hashing import _fixture;"
        "from iris_signal_harvester.domain.hashing import content_hash;"
        "print(content_hash(_fixture()))"
    )
    digests = set()
    for seed in ("0", "1", "424242"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
        )
        digests.add(out.stdout.strip())
    assert digests == {PINNED_FIXTURE_DIGEST}


def test_hash_shape() -> None:
    digest = content_hash(_fixture())
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


# --- invariances (must NOT change the hash) --------------------------------


def test_key_reordering_does_not_change_hash() -> None:
    original = _fixture()
    reordered = dict(reversed(list(original.items())))
    assert list(reordered) != list(original)
    assert content_hash(reordered) == content_hash(original)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("modified", "2025-01-01T00:00:00+00:00"),
        ("modified", "-0001-11-30T00:00:00+00:53"),
        ("harvest_run_id", uuid4()),
        ("first_seen_at", datetime(2025, 1, 1, tzinfo=UTC)),
        ("last_seen_at", datetime(2025, 6, 1, tzinfo=UTC)),
        ("updated_at", datetime(2025, 6, 1, tzinfo=UTC)),
        ("request_id", "req-7f3a"),
        ("raw_payload", {"modified": "2025-01-01", "anything": [1, 2, 3]}),
        ("content_hash", "0" * 64),
        ("hash_version", 99),
    ],
)
def test_volatile_fields_never_affect_hash(name: str, value: object) -> None:
    base = content_hash(_fixture())
    assert content_hash({**_fixture(), name: value}) == base


def test_whitespace_only_change_in_volatile_field_is_ignored() -> None:
    a = {**_fixture(), "modified": "2024-03-05T10:00:00+00:00", "request_id": "abc"}
    b = {**_fixture(), "modified": "  2024-03-05T10:00:00+00:00 \n", "request_id": " abc  "}
    assert content_hash(a) == content_hash(b)


def test_modified_moving_forward_does_not_make_record_look_changed() -> None:
    """The idempotency property: the server touching a record is not a change."""
    first = {**_fixture(), "modified": "2024-03-05T10:00:00+00:00"}
    second = {**_fixture(), "modified": "2024-09-30T17:45:12+00:00"}
    assert content_hash(first) == content_hash(second)


def test_volatile_and_semantic_fields_are_disjoint() -> None:
    assert not VOLATILE_FIELDS & set(SEMANTIC_FIELDS)
    assert "modified" in VOLATILE_FIELDS
    assert "modified" not in SEMANTIC_FIELDS


def test_missing_key_hashes_like_explicit_none() -> None:
    explicit = _fixture()
    missing = _fixture()
    del missing["summary"]
    assert explicit["summary"] is None
    assert content_hash(missing) == content_hash(explicit)


def test_nfkc_equivalent_strings_hash_identically() -> None:
    composed = {**_fixture(), "title": "Müller Straße"}
    decomposed = {**_fixture(), "title": "Müller Straße"}
    assert composed["title"] != decomposed["title"]
    assert content_hash(composed) == content_hash(decomposed)
    # compatibility forms: fullwidth digits fold under NFKC
    assert content_hash({**_fixture(), "title": "Bau 1"}) == content_hash(
        {**_fixture(), "title": "Bau \uff11"}
    )


def test_integral_float_and_int_coordinates_agree() -> None:
    assert content_hash({**_fixture(), "easting": 691234.0}) == content_hash(
        {**_fixture(), "easting": 691234}
    )


def test_equal_instants_in_different_offsets_agree() -> None:
    utc = {**_fixture(), "published_at": datetime(2024, 3, 5, 10, 0, tzinfo=UTC)}
    berlin = {
        **_fixture(),
        "published_at": datetime(2024, 3, 5, 11, 0, tzinfo=timezone(timedelta(hours=1))),
    }
    assert content_hash(utc) == content_hash(berlin)


def test_enum_and_plain_string_agree() -> None:
    plain = {
        **_fixture(),
        "published_at_origin": "payload_field",
        "geo_method": "municipality_centroid",
    }
    assert content_hash(plain) == content_hash(_fixture())


def test_hit_model_and_its_dict_form_agree() -> None:
    as_dicts = {
        **_fixture(),
        "matched_keywords": [h.model_dump() for h in _fixture()["matched_keywords"]],
    }
    assert content_hash(as_dicts) == content_hash(_fixture())


# --- sensitivities (MUST change the hash) ----------------------------------


def test_title_change_changes_hash() -> None:
    changed = {**_fixture(), "title": "Neubau Umspannwerk Süd"}
    assert content_hash(changed) != content_hash(_fixture())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("summary", "Neue Zusammenfassung"),
        ("document_type", "Anfrage"),
        ("source_url", "https://example.org/oparl/paper/43"),
        ("source_record_id", "paper-43"),
        ("source_licence", "CC0-1.0"),
        ("published_at", datetime(2024, 3, 6, 10, 0, tzinfo=UTC)),
        ("published_at_origin", PublishedAtOrigin.HTTP_HEADER),
        ("ags", "09162001"),
        ("easting", 691235.0),
        ("northing", 5334568.5),
        ("geo_method", GeoMethod.ADDRESS_GEOCODE),
        ("geo_confidence", GeoConfidence.HIGH),
        ("source_crs", "EPSG:25833"),
        ("signal_type", "road_widening"),
        ("relevance_score", 0.9),
        ("matched_keywords", ()),
    ],
)
def test_every_semantic_change_changes_hash(name: str, value: object) -> None:
    assert name in SEMANTIC_FIELDS
    assert content_hash({**_fixture(), name: value}) != content_hash(_fixture())


def test_taxonomy_version_alone_does_not_change_hash() -> None:
    """A version bump that leaves score/type/keywords unchanged must not re-update rows."""
    assert "taxonomy_version" not in SEMANTIC_FIELDS
    bumped = {**_fixture(), "taxonomy_version": "2024.2"}
    assert content_hash(bumped) == content_hash(_fixture())


def test_published_at_provenance_alone_changes_hash() -> None:
    """Same instant, different origin: the provenance is itself part of the meaning."""
    a = {**_fixture(), "published_at_origin": PublishedAtOrigin.PAYLOAD_FIELD}
    b = {**_fixture(), "published_at_origin": PublishedAtOrigin.UNKNOWN}
    assert content_hash(a) != content_hash(b)


def test_null_is_distinct_from_empty_string_and_zero() -> None:
    base = _fixture()
    assert content_hash({**base, "summary": ""}) != content_hash({**base, "summary": None})
    assert content_hash({**base, "relevance_score": 0}) != content_hash(
        {**base, "relevance_score": None}
    )


def test_field_values_cannot_swap_between_fields() -> None:
    a = {**_fixture(), "title": "A", "summary": "B"}
    b = {**_fixture(), "title": "B", "summary": "A"}
    assert content_hash(a) != content_hash(b)


# --- refusals --------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        content_hash({**_fixture(), "easting": bad})


def test_naive_datetime_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="naive"):
        content_hash({**_fixture(), "published_at": datetime(2024, 3, 5, 10, 0)})


def test_unhashable_type_is_refused_not_stringified() -> None:
    with pytest.raises(TypeError):
        content_hash({**_fixture(), "title": object()})
