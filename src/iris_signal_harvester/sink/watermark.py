"""Incremental-harvest watermarks.

Two rules protect incremental harvesting from permanent damage:

1. **Advance only on a clean body sweep.** If a sweep aborted, was truncated by
   ``max_pages_per_body`` or lost pages, the maximum ``modified`` seen is *not*
   a promise that everything older was processed, so advancing would silently
   skip records forever.
2. **Unparseable ``modified`` values never count.** Real OParl data contains
   ``-0001-11-30T00:00:00+00:53``; if that (or any implausible date) became a
   maximum, the watermark would be poisoned. ``safe_parse_for_watermark``
   swallows those; the plausible-range guard here is a second line of defence.

The watermark is monotonic: it never moves backwards.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

import structlog
from psycopg import sql

from iris_signal_harvester.config import Settings, get_settings
from iris_signal_harvester.domain.dates import safe_parse_for_watermark
from iris_signal_harvester.sink._common import DEFAULT_SCHEMA, Conn, connect, table

__all__ = [
    "AdvanceOutcome",
    "ModifiedTracker",
    "WatermarkStore",
    "is_plausible_watermark",
    "max_modified",
]

log = structlog.get_logger(__name__)

# Matches the lower bound of validation query 10 and ck_watermark_not_sentinel.
_FLOOR = datetime(1990, 1, 1, tzinfo=UTC)
_MAX_FUTURE_DAYS = 400


class AdvanceOutcome(StrEnum):
    ADVANCED = "advanced"
    UNCHANGED = "unchanged"
    SKIPPED_UNCLEAN_SWEEP = "skipped_unclean_sweep"
    SKIPPED_NO_CANDIDATE = "skipped_no_candidate"
    SKIPPED_IMPLAUSIBLE = "skipped_implausible"


def is_plausible_watermark(
    value: datetime,
    *,
    now: datetime | None = None,
    max_future_days: int = _MAX_FUTURE_DAYS,
) -> bool:
    """True if ``value`` is inside the range validation query 10 accepts."""
    current = now or datetime.now(UTC)
    return value.tzinfo is not None and _FLOOR <= value <= current + timedelta(days=max_future_days)


def max_modified(
    values: Iterable[object],
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Maximum of the *parseable and plausible* ``modified`` values, else ``None``."""
    best: datetime | None = None
    for value in values:
        parsed = safe_parse_for_watermark(value, settings or get_settings(), now=now)
        if parsed is None or not is_plausible_watermark(parsed, now=now):
            continue
        if best is None or parsed > best:
            best = parsed
    return best


@dataclass
class ModifiedTracker:
    """Accumulates ``modified`` values over one body sweep.

    ``ignored`` counts values that were present but unusable, so the report can
    say how often a source emits corrupt timestamps.
    """

    settings: Settings | None = None
    now: datetime | None = None
    maximum: datetime | None = None
    observed: int = 0
    ignored: int = 0
    _clean: bool = field(default=True, repr=False)

    def observe(self, value: object) -> None:
        if value is None:
            return
        self.observed += 1
        candidate = max_modified([value], self.settings, now=self.now)
        if candidate is None:
            self.ignored += 1
            return
        if self.maximum is None or candidate > self.maximum:
            self.maximum = candidate

    def mark_unclean(self) -> None:
        """Call on any abort, truncation, retry exhaustion or dropped page."""
        self._clean = False

    @property
    def clean(self) -> bool:
        return self._clean


class WatermarkStore:
    """Reads and advances ``harvest_watermark`` rows."""

    def __init__(self, dsn: str, schema: str = DEFAULT_SCHEMA) -> None:
        self._dsn = dsn
        self._schema = schema

    def read(self, source_system: str, body_id: str) -> datetime | None:
        query = sql.SQL(
            "SELECT last_modified_seen FROM {} WHERE source_system = %s AND body_id = %s"
        ).format(table(self._schema, "harvest_watermark"))
        with connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(query, (source_system, body_id))
            row = cur.fetchone()
        value = row[0] if row else None
        return value if isinstance(value, datetime) else None

    def advance(
        self,
        source_system: str,
        body_id: str,
        candidate: datetime | None,
        *,
        clean_sweep: bool,
        run_id: UUID | None = None,
        now: datetime | None = None,
        conn: Conn | None = None,
    ) -> AdvanceOutcome:
        """Advance the watermark if, and only if, it is safe to do so."""
        if not clean_sweep:
            log.warning("watermark_not_advanced", reason="unclean_sweep", body_id=body_id)
            return AdvanceOutcome.SKIPPED_UNCLEAN_SWEEP
        if candidate is None:
            return AdvanceOutcome.SKIPPED_NO_CANDIDATE
        if not is_plausible_watermark(candidate, now=now):
            log.warning("watermark_not_advanced", reason="implausible", candidate=str(candidate))
            return AdvanceOutcome.SKIPPED_IMPLAUSIBLE

        # GREATEST keeps the watermark monotonic; the WHERE makes a no-op write nothing.
        statement = sql.SQL(
            """
            INSERT INTO {t} AS w (source_system, body_id, last_modified_seen, last_run_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_system, body_id) DO UPDATE
               SET last_modified_seen = EXCLUDED.last_modified_seen,
                   last_run_id = EXCLUDED.last_run_id,
                   updated_at = now()
             WHERE w.last_modified_seen IS NULL
                OR w.last_modified_seen < EXCLUDED.last_modified_seen
            RETURNING 1
            """
        ).format(t=table(self._schema, "harvest_watermark"))
        params = (source_system, body_id, candidate, str(run_id) if run_id else None)
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(statement, params)
                moved = cur.fetchone() is not None
        else:
            with connect(self._dsn) as own, own.cursor() as cur:
                cur.execute(statement, params)
                moved = cur.fetchone() is not None
        return AdvanceOutcome.ADVANCED if moved else AdvanceOutcome.UNCHANGED
