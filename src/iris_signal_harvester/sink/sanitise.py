"""What PostgreSQL cannot store: the storage-specific serialiser, plus re-exports.

The pure, value-level logic (``scrub_nulls``, ``make_storable``, ``unstorable_reason``)
now lives in :mod:`iris_signal_harvester.domain.sanitize`. It knows nothing about
PostgreSQL, and the domain core (``domain.normalise``) needs it, so it belongs inward
of the sink: dependencies point from the sink to the domain, never the other way. See
that module for the policies (reject for canonical signals, replace for dead letters).

What stays here is :func:`dumps_jsonb`, because serialising for a ``jsonb`` column is
genuinely storage-specific. The moved names are re-exported so existing
``from iris_signal_harvester.sink.sanitise import ...`` imports keep working.
"""

from __future__ import annotations

import json
from typing import Any

from iris_signal_harvester.domain.sanitize import make_storable, scrub_nulls, unstorable_reason

__all__ = [
    "dumps_jsonb",
    "make_storable",
    "scrub_nulls",
    "unstorable_reason",
]


def dumps_jsonb(value: Any, *, sort_keys: bool = False) -> str:
    """``json.dumps`` that is guaranteed to be accepted by a ``jsonb`` column.

    The value is made storable first; ``allow_nan=False`` is the belt to that brace, so
    a future change that lets a NaN through fails here, loudly, rather than in the
    database where it takes a whole batch with it.
    """
    return json.dumps(
        make_storable(value),
        default=str,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
    )
