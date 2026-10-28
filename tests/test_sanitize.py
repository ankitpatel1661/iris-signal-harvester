"""NUL scrubbing: PostgreSQL text and jsonb cannot hold U+0000."""

from __future__ import annotations

import uuid
from typing import Any

from iris_signal_harvester.domain.models import CanonicalSignal
from iris_signal_harvester.domain.sanitize import scrub_nulls
from iris_signal_harvester.sink.postgis import _json, _stage_row


def test_scrubs_nested_strings_keys_and_leaves_other_values_alone() -> None:
    value: Any = {
        "a\u0000b": ["x\u0000y", {"deep": "\u0000start", "n": 3, "f": 1.5, "none": None}],
        "t": ("p\u0000q", True),
    }
    assert scrub_nulls(value) == {
        "ab": ["xy", {"deep": "start", "n": 3, "f": 1.5, "none": None}],
        "t": ("pq", True),
    }


def test_does_not_mutate_its_input_and_passes_scalars_through() -> None:
    original = {"k": ["v\u0000"]}
    scrub_nulls(original)
    assert original == {"k": ["v\u0000"]}
    assert scrub_nulls(7) == 7
    assert scrub_nulls(None) is None
    assert scrub_nulls("clean") == "clean"


def test_json_serialiser_never_emits_a_nul_escape() -> None:
    assert "\\u0000" not in _json({"title": "a\u0000b", "n": [{"x": "\u0000"}]})


def test_stage_row_contains_no_nul_in_any_column() -> None:
    signal = CanonicalSignal(
        harvest_run_id=uuid.uuid4(),
        source_system="oparl",
        source_body_id="b\u00001",
        source_record_id="r\u00001",
        source_url="https://example.org/1",
        title="Neu\u0000bau",
        summary="s\u0000",
        taxonomy_version="2026.1",
        raw_payload={"name": "x\u0000y", "nested": {"l": ["a\u0000"]}},
        content_hash="0" * 64,
    )
    row = _stage_row(signal)
    assert all("\u0000" not in v for v in row if isinstance(v, str))
    assert "\\u0000" not in "".join(v for v in row if isinstance(v, str))
