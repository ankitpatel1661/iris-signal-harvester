"""Pure, value-level sanitisation of data that is destined for PostgreSQL.

``httpx.Response.json()`` is more permissive than PostgreSQL. It accepts:

* ``NaN`` / ``Infinity`` / ``-Infinity``: ``jsonb`` rejects them
  (``InvalidTextRepresentation``) and ``json.dumps`` emits them unless told not to.
* lone surrogate escapes such as ``"\\ud800"``: they decode to an unpaired surrogate
  code point that cannot be encoded as UTF-8 (``UnicodeEncodeError``).
* ``U+0000``: legal in JSON, illegal in ``text`` and ``jsonb``.

One such value in one record would otherwise fail the whole batch it travels in. Two
policies, because the two kinds of rows have different jobs:

* A **canonical signal** is a claim ("this is the record"), so a value we would have to
  invent a replacement for makes the record unstorable. :func:`unstorable_reason`
  finds it and the sink dead-letters the record as ``schema_validation_failed``.
  NUL is the exception: removing it loses nothing of value and has always been done.
* A **dead-letter row** is evidence, and refusing to store the evidence of a failure
  would hide the failure. :func:`make_storable` therefore *replaces* instead: a
  non-finite float becomes its text (``"NaN"``), a lone surrogate becomes U+FFFD.

Everything here is a pure function over Python values; nothing knows about PostgreSQL
or any I/O, which is why it lives in the domain core. The one storage-specific piece,
``dumps_jsonb``, stays in :mod:`iris_signal_harvester.sink.sanitise`.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["make_storable", "scrub_nulls", "unstorable_reason"]

_NUL = "\u0000"
_REPLACEMENT = "�"


def _has_lone_surrogate(text: str) -> bool:
    # Encoding is the cheapest exact test: it fails on, and only on, a lone surrogate.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _is_surrogate(char: str) -> bool:
    return 0xD800 <= ord(char) <= 0xDFFF


def _replace_surrogates(text: str) -> str:
    if not _has_lone_surrogate(text):
        return text
    return "".join(_REPLACEMENT if _is_surrogate(c) else c for c in text)


def make_storable(value: Any) -> Any:
    """Return ``value`` with everything PostgreSQL cannot store replaced.

    * ``U+0000`` is removed from every string (dict keys included).
    * a lone surrogate becomes U+FFFD.
    * a non-finite float becomes the string ``"NaN"`` / ``"Infinity"`` / ``"-Infinity"``.

    Containers are rebuilt, never mutated. Use this for dead-letter evidence; for a
    canonical record use :func:`unstorable_reason` and reject instead.
    """
    if isinstance(value, str):
        return _replace_surrogates(value.replace(_NUL, "")) if value else value
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if isinstance(value, dict):
        return {make_storable(k): make_storable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [make_storable(v) for v in value]
    return value


def unstorable_reason(value: Any, path: str = "$") -> str | None:
    """Describe the first non-finite number or lone surrogate in ``value``, else ``None``.

    ``path`` in the answer is ASCII-escaped, so describing a hostile key can never
    itself raise. NUL is not reported here: it is stripped, not rejected.
    """
    if isinstance(value, str):
        if _has_lone_surrogate(value):
            return f"lone UTF-16 surrogate in {path}"
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return f"non-finite number ({value!r}) in {path}"
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _has_lone_surrogate(key):
                return f"lone UTF-16 surrogate in a key of {path}"
            found = unstorable_reason(item, f"{path}.{ascii(key)[1:-1]}")
            if found is not None:
                return found
        return None
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found = unstorable_reason(item, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def scrub_nulls(value: Any) -> Any:
    """Return ``value`` with every ``U+0000`` removed from every string inside it.

    Recurses through dicts (keys included, since JSON object keys are strings too),
    lists and tuples; any other value is returned unchanged. The input is never
    mutated. Containers are rebuilt, so the result is safe to hand to ``json.dumps``.
    """
    if isinstance(value, str):
        return value.replace(_NUL, "") if _NUL in value else value
    if isinstance(value, dict):
        return {scrub_nulls(k): scrub_nulls(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_nulls(v) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_nulls(v) for v in value)
    return value
