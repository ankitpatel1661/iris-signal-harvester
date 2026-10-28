"""Content hashing: change detection that tracks meaning, not transport noise.

The hash must change when a record's *meaning* changes and must not change when
only volatile transport fields change. ``modified`` is excluded on purpose: the
server moves it whenever it touches a record, so including it would make every
record look changed on every run and destroy idempotency (0 inserts, 0 updates,
no ``updated_at`` movement on a second identical run).

Mechanism: the hash is taken over a *projection*. Only the keys in
``SEMANTIC_FIELDS`` are read from the input; everything else (``modified``,
``raw_payload``, ``harvest_run_id``, ``first_seen_at``, ``last_seen_at``,
``updated_at``, request ids, ...) is ignored by construction. A volatile field
added in future is therefore excluded by default; a new semantic field must be
added here deliberately, with a ``HASH_VERSION`` bump.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

__all__ = ["HASH_VERSION", "SEMANTIC_FIELDS", "VOLATILE_FIELDS", "content_hash"]

HASH_VERSION = 1

# The fields whose change means the record's meaning changed. Order is
# irrelevant to the hash (keys are sorted) but kept readable.
#
# ``taxonomy_version`` is deliberately NOT here. ``signal_type``,
# ``relevance_score`` and ``matched_keywords`` are already in the projection, so
# a taxonomy change that actually alters scoring already moves the hash. Keeping
# ``taxonomy_version`` as well would re-update every row on a version bump even
# where the score did not change: churn carrying no information, which is exactly
# what the idempotency design exists to avoid.
SEMANTIC_FIELDS: tuple[str, ...] = (
    "source_system",
    "source_body_id",
    "source_record_id",
    "source_url",
    "source_licence",
    "title",
    "summary",
    "document_type",
    "published_at",
    "published_at_origin",
    "ags",
    "ars",
    "municipality_name",
    "kreis",
    "bundesland",
    "easting",
    "northing",
    "geo_method",
    "geo_confidence",
    "source_crs",
    "signal_type",
    "matched_keywords",
    "relevance_score",
)

# Documented exclusions. Not consulted by the algorithm (the projection already
# ignores them) but exposed so tests and reviewers can assert the boundary.
VOLATILE_FIELDS: frozenset[str] = frozenset(
    {
        "modified",
        "harvest_run_id",
        "first_seen_at",
        "last_seen_at",
        "updated_at",
        "request_id",
        "raw_payload",
        "content_hash",
        "hash_version",
    }
)


def _norm_str(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _canonical(value: Any) -> Any:
    """Reduce ``value`` to JSON-native types with a single stable spelling."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, str):
        return _norm_str(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot hash non-finite float {value!r}")
        # 691234.0 and 691234 are the same coordinate; spell them identically.
        return int(value) if value.is_integer() else value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("cannot hash a naive datetime: its instant is ambiguous")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = _norm_str(str(key))
            if name in out:
                raise ValueError(f"mapping keys collide after NFKC normalisation: {name!r}")
            out[name] = _canonical(item)
        return out
    if isinstance(value, list | tuple):
        return [_canonical(item) for item in value]
    raise TypeError(f"cannot canonicalise value of type {type(value).__name__}")


def content_hash(signal_fields: Mapping[str, Any]) -> str:
    """sha256 hex digest of the canonical JSON of the semantic projection.

    Canonical JSON: sorted keys, no insignificant whitespace, NFKC-normalised
    strings, UTF-8, and *explicit nulls* (a semantic field that is missing from
    the input is hashed as ``null``, so "absent" and "None" agree and a field can
    never silently drop out of the hash).

    Raises:
        TypeError: a semantic value has a type with no canonical form.
        ValueError: a value is non-finite or a naive datetime.
    """
    projection = {name: _canonical(signal_fields.get(name)) for name in SEMANTIC_FIELDS}
    encoded = json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
