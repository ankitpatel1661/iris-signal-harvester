"""CanonicalSignal invariants enforced in the model, not only in the database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from iris_signal_harvester.domain.models import CanonicalSignal, PublishedAtOrigin


def make(**overrides: Any) -> CanonicalSignal:
    fields: dict[str, Any] = {
        "harvest_run_id": uuid.uuid4(),
        "source_system": "oparl",
        "source_body_id": "https://oparl.example.org/body/1",
        "source_record_id": "02-08/B00088",
        "source_url": "https://oparl.example.org/paper/88",
        "title": "Beschluss",
        "taxonomy_version": "2026.1",
        "raw_payload": {"id": "88"},
        "content_hash": "0" * 64,
    }
    fields.update(overrides)
    return CanonicalSignal(**fields)


# --- natural key ---------------------------------------------------------------


def test_natural_key_is_system_body_record() -> None:
    signal = make()
    assert signal.natural_key == (
        "oparl",
        "https://oparl.example.org/body/1",
        "02-08/B00088",
    )


def test_same_reference_in_two_bodies_gives_two_keys_and_keeps_the_id_verbatim() -> None:
    a = make(source_body_id="body-a")
    b = make(source_body_id="body-b")
    assert a.source_record_id == b.source_record_id == "02-08/B00088"
    assert a.natural_key != b.natural_key


def test_source_body_id_is_required() -> None:
    fields = make().model_dump()
    del fields["source_body_id"]
    with pytest.raises(ValidationError):
        CanonicalSignal(**fields)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_source_body_id_must_not_be_blank_or_none(value: str | None) -> None:
    with pytest.raises(ValidationError):
        make(source_body_id=value)


# --- the date invariant ---------------------------------------------------------


def test_date_with_unknown_origin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="published_at_origin"):
        make(
            published_at=datetime(2026, 3, 1, tzinfo=UTC),
            published_at_origin=PublishedAtOrigin.UNKNOWN,
        )


@pytest.mark.parametrize(
    "origin",
    [
        PublishedAtOrigin.PAYLOAD_FIELD,
        PublishedAtOrigin.HTTP_HEADER,
        PublishedAtOrigin.DERIVED_FROM_MEETING,
    ],
)
def test_known_origin_without_a_date_is_rejected(origin: PublishedAtOrigin) -> None:
    with pytest.raises(ValidationError, match="published_at_origin"):
        make(published_at=None, published_at_origin=origin)


def test_consistent_date_states_are_accepted() -> None:
    assert make().published_at is None  # defaults: no date, origin unknown
    dated = make(
        published_at=datetime(2026, 3, 1, tzinfo=UTC),
        published_at_origin=PublishedAtOrigin.PAYLOAD_FIELD,
    )
    assert dated.published_at is not None
