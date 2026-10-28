"""Dead-letter writer: rejected records are rows, not log lines.

Rows can be counted (validation query 7), queried and replayed after a fix.
``reason_code`` is always a ``RejectCode`` member; the migration's CHECK
constraint mirrors the enum so free text cannot be stored.

A rejection is identified by ``(source_system, source_body_id, source_record_id,
reason_code, payload_hash)``. The incremental overlap window re-fetches a bad record
on every run, so inserting blindly would store the same rejection once per run.
Instead a repeat sighting advances ``last_seen_at`` and
``seen_count`` on the existing row (``migrations/002_reject_dedup.sql``). The row keeps
the id of the run that first rejected it; a changed payload or a changed reason is a
new rejection.
"""

from __future__ import annotations

from collections.abc import Iterable

from psycopg import sql

from iris_signal_harvester.domain.models import RejectedRecord
from iris_signal_harvester.sink._common import DEFAULT_SCHEMA, Conn, table
from iris_signal_harvester.sink.sanitise import dumps_jsonb, make_storable

__all__ = ["write_rejects"]

# ``payload_hash`` is filled by a BEFORE INSERT trigger (see migration 002).
_COLUMNS = (
    "harvest_run_id",
    "source_system",
    "source_body_id",
    "source_record_id",
    "reason_code",
    "reason_detail",
    "raw_payload",
)


def write_rejects(
    conn: Conn,
    rejects: Iterable[RejectedRecord],
    *,
    schema: str = DEFAULT_SCHEMA,
) -> int:
    """Upsert rejects in the caller's connection; returns the number of rejects handled.

    Runs inside its own transaction block (a savepoint if the caller already opened
    one). A reject already on file only has its ``last_seen_at`` / ``seen_count``
    advanced. Everything is made storable first: dead letters are the records most
    likely to carry NaN, lone surrogates or NUL, and refusing to store the evidence of
    a failure would turn one bad record into a failed batch.
    """
    # Collapse repeats inside the batch first: one INSERT ... ON CONFLICT statement may not
    # touch the same row twice, and the count of sightings must still be right.
    unique: dict[tuple[object, ...], tuple[tuple[object, ...], int]] = {}
    handled = 0
    for r in rejects:
        handled += 1
        payload = dumps_jsonb(r.raw_payload)
        row = (
            str(r.harvest_run_id),
            make_storable(r.source_system),
            make_storable(r.source_body_id),
            make_storable(r.source_record_id),
            r.reason_code.value,
            make_storable(r.reason_detail),
            payload,
        )
        # Key order must not matter (jsonb normalises it), so key on a sorted dump.
        key = (
            row[1],
            row[2] or "",
            row[3] or "",
            row[4],
            dumps_jsonb(r.raw_payload, sort_keys=True),
        )
        previous = unique.get(key)
        unique[key] = (
            row if previous is None else previous[0],
            (previous[1] if previous else 0) + 1,
        )
    if not unique:
        return 0

    target = table(schema, "signal_ingest_reject")
    statement = sql.SQL(
        """
        INSERT INTO {target} AS t ({columns}, seen_count)
        VALUES ({placeholders}, %s)
        ON CONFLICT (source_system, COALESCE(source_body_id, ''),
                     COALESCE(source_record_id, ''), reason_code, payload_hash)
        DO UPDATE SET last_seen_at = now(), seen_count = t.seen_count + EXCLUDED.seen_count
        """
    ).format(
        target=target,
        columns=sql.SQL(", ").join(sql.Identifier(c) for c in _COLUMNS),
        placeholders=sql.SQL(", ").join(
            sql.SQL("%s::jsonb") if c == "raw_payload" else sql.SQL("%s") for c in _COLUMNS
        ),
    )
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(statement, [(*row, count) for row, count in unique.values()])
    return handled
