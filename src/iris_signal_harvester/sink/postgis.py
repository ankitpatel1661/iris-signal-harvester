"""PostGIS sink: a genuinely idempotent bulk upsert.

``PostgisSink.load`` performs, in ONE transaction:

1. ``COPY`` the batch into a temp table (temp tables are never WAL-logged).
2. ONE statement::

       INSERT ... ON CONFLICT (source_system, source_body_id, source_record_id) DO UPDATE SET ...
       WHERE t.content_hash IS DISTINCT FROM EXCLUDED.content_hash
       RETURNING (xmax = 0) AS was_insert

   The ``WHERE`` predicate is the guarantee: an unchanged record is not written
   at all — ``updated_at`` does not move and no dead tuple is produced.
3. A SEPARATE bulk ``UPDATE`` advancing ``last_seen_at`` for the staged ids.
   Liveness tracking must never contaminate mutation tracking; collapsing 2 and
   3 makes "was anything actually changed by this run?" unanswerable from data.
4. ``LoadCounts`` derived from the ``RETURNING`` rows.

``inserted`` and ``updated`` come from what PostgreSQL reports it wrote, never
from a Python-side comparison, so the numbers cannot drift from the data.
5. Records PostgreSQL cannot store (a ``NaN``, a lone surrogate) are dead-lettered as
   ``schema_validation_failed`` instead of failing the batch: see ``sink/sanitise.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import sql

from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import CanonicalSignal, RejectCode, RejectedRecord
from iris_signal_harvester.sink._common import DEFAULT_SCHEMA, Conn, connect, table
from iris_signal_harvester.sink.reject import write_rejects
from iris_signal_harvester.sink.sanitise import dumps_jsonb, make_storable, unstorable_reason

__all__ = ["LoadCounts", "PostgisSink"]

_SRID = 25832

# (column, SQL type) in staging order. ``easting``/``northing`` are staged as plain
# floats and turned into a SRID-stamped point inside the INSERT, so no geometry
# text is ever parsed and the SRID is applied in exactly one place.
_STAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("harvest_run_id", "uuid"),
    ("source_system", "text"),
    ("source_body_id", "text"),
    ("source_record_id", "text"),
    ("source_url", "text"),
    ("source_licence", "text"),
    ("title", "text"),
    ("summary", "text"),
    ("document_type", "text"),
    ("published_at", "timestamptz"),
    ("published_at_origin", "text"),
    ("ags", "text"),
    ("ars", "text"),
    ("municipality_name", "text"),
    ("kreis", "text"),
    ("bundesland", "text"),
    ("easting", "double precision"),
    ("northing", "double precision"),
    ("geo_method", "text"),
    ("geo_confidence", "text"),
    ("source_crs", "text"),
    ("signal_type", "text"),
    ("matched_keywords", "jsonb"),
    ("relevance_score", "double precision"),
    ("taxonomy_version", "text"),
    ("raw_payload", "jsonb"),
    ("content_hash", "text"),
    ("hash_version", "integer"),
)

_KEY = ("source_system", "source_body_id", "source_record_id")
_COORDS = ("easting", "northing")
# Columns copied stage -> target by name (everything except the coordinate pair).
_DIRECT = tuple(name for name, _ in _STAGE_COLUMNS if name not in _COORDS)
# Columns overwritten when the content changed. The key and first_seen_at are never rewritten.
_MUTABLE = (*(c for c in _DIRECT if c not in _KEY), "geom")


@dataclass(frozen=True)
class LoadCounts:
    """What one ``load`` actually did, as reported by PostgreSQL."""

    inserted: int
    updated: int
    unchanged: int
    duplicates_in_batch: int = 0
    rejected: int = 0
    """Signals the sink itself dead-lettered (``NaN`` / lone surrogate); not in ``staged``."""

    @property
    def staged(self) -> int:
        return self.inserted + self.updated + self.unchanged


def _json(value: object) -> str:
    # jsonb cannot hold U+0000, NaN or a lone surrogate; dumps_jsonb removes or replaces them.
    return dumps_jsonb(value)


def _unstorable(signal: CanonicalSignal) -> RejectedRecord | None:
    """A dead-letter record if ``signal`` holds a value PostgreSQL cannot store, else ``None``.

    ``httpx.Response.json()`` accepts ``NaN`` and lone-surrogate escapes; stored as they
    are, the first fails the ``jsonb`` cast and the second the UTF-8 encode, and either
    one fails every other record in the same batch. Rejecting the one record keeps the
    rest, and the evidence survives as a dead-letter row.
    """
    reason = unstorable_reason(signal.model_dump(mode="python"))
    if reason is None:
        return None
    return RejectedRecord(
        harvest_run_id=signal.harvest_run_id,
        source_system=make_storable(signal.source_system),
        source_body_id=make_storable(signal.source_body_id),
        source_record_id=make_storable(signal.source_record_id),
        reason_code=RejectCode.SCHEMA_VALIDATION_FAILED,
        reason_detail=f"not storable in PostgreSQL: {reason}",
        raw_payload=make_storable(signal.raw_payload),
    )


def _stage_row(s: CanonicalSignal) -> tuple[object, ...]:
    values: Mapping[str, object] = {
        "harvest_run_id": str(s.harvest_run_id),
        "source_system": s.source_system,
        "source_body_id": s.source_body_id,
        "source_record_id": s.source_record_id,
        "source_url": s.source_url,
        "source_licence": s.source_licence,
        "title": s.title,
        "summary": s.summary,
        "document_type": s.document_type,
        "published_at": s.published_at,
        "published_at_origin": s.published_at_origin.value,
        "ags": s.ags,
        "ars": s.ars,
        "municipality_name": s.municipality_name,
        "kreis": s.kreis,
        "bundesland": s.bundesland,
        "easting": s.easting,
        "northing": s.northing,
        "geo_method": s.geo_method.value,
        "geo_confidence": s.geo_confidence.value,
        "source_crs": s.source_crs,
        "signal_type": s.signal_type,
        "matched_keywords": _json([h.model_dump(mode="json") for h in s.matched_keywords]),
        "relevance_score": s.relevance_score,
        "taxonomy_version": s.taxonomy_version,
        "raw_payload": _json(s.raw_payload),
        "content_hash": s.content_hash,
        "hash_version": s.hash_version,
    }
    # text columns cannot hold U+0000 either; jsonb columns were already made storable by _json.
    return tuple(make_storable(values[name]) for name, _ in _STAGE_COLUMNS)


def _idents(names: Iterable[str]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(n) for n in names)


class PostgisSink:
    """Loads ``CanonicalSignal`` batches into ``<schema>.signal_ingest``."""

    def __init__(self, dsn: str, schema: str = DEFAULT_SCHEMA) -> None:
        self._dsn = dsn
        self._schema = schema

    @classmethod
    def from_settings(cls, settings: Settings) -> PostgisSink:
        """Build from settings; raises ``ConfigurationError`` if no DSN is set."""
        return cls(settings.require_dsn(), settings.db_schema)

    # -- the core ---------------------------------------------------------------

    def load(self, signals: Iterable[CanonicalSignal]) -> LoadCounts:
        """Idempotently upsert ``signals`` and report what was really written."""
        # The upsert would fail with "cannot affect row a second time" on a repeated
        # key, so de-duplicate (last occurrence wins) and report how many were dropped.
        by_key: dict[tuple[str, str, str], CanonicalSignal] = {}
        rejects: list[RejectedRecord] = []
        total = 0
        for signal in signals:
            bad = _unstorable(signal)
            if bad is not None:
                rejects.append(bad)
                continue
            total += 1
            # Key on the scrubbed values: that is what the database will see, so two
            # keys differing only by a NUL must collapse here, not collide in the upsert.
            key = signal.natural_key
            by_key[(make_storable(key[0]), make_storable(key[1]), make_storable(key[2]))] = signal
        if not by_key and not rejects:
            return LoadCounts(0, 0, 0)
        duplicates = total - len(by_key)
        batch = sorted(by_key.items())  # stable lock order across concurrent loaders

        was_insert: list[bool] = []
        with connect(self._dsn) as conn, conn.transaction():
            if rejects:
                write_rejects(conn, rejects, schema=self._schema)
            if batch:
                self._stage(conn, [signal for _, signal in batch])
                was_insert = self._upsert(conn)
                self._touch_last_seen(conn)

        inserted = sum(was_insert)
        updated = len(was_insert) - inserted
        return LoadCounts(
            inserted=inserted,
            updated=updated,
            unchanged=len(by_key) - len(was_insert),
            duplicates_in_batch=duplicates,
            rejected=len(rejects),
        )

    def _stage(self, conn: Conn, signals: list[CanonicalSignal]) -> None:
        columns = sql.SQL(", ").join(
            sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(sqltype))
            for name, sqltype in _STAGE_COLUMNS
        )
        with conn.cursor() as cur:
            # Temp tables bypass WAL, i.e. they are as cheap as UNLOGGED ones, and
            # vanish with the transaction.
            cur.execute(
                sql.SQL("CREATE TEMP TABLE _iris_stage ({}) ON COMMIT DROP").format(columns)
            )
            copy_stmt = sql.SQL("COPY _iris_stage ({}) FROM STDIN").format(
                _idents(name for name, _ in _STAGE_COLUMNS)
            )
            with cur.copy(copy_stmt) as copy:
                for signal in signals:
                    copy.write_row(_stage_row(signal))

    def _upsert(self, conn: Conn) -> list[bool]:
        target = table(self._schema, "signal_ingest")
        geom = sql.SQL(
            "CASE WHEN s.easting IS NOT NULL AND s.northing IS NOT NULL "
            "THEN ST_SetSRID(ST_MakePoint(s.easting, s.northing), {srid}) END"
        ).format(srid=sql.Literal(_SRID))
        select_list = sql.SQL(", ").join(
            [*(sql.SQL("s.{}").format(sql.Identifier(c)) for c in _DIRECT), geom]
        )
        set_list = sql.SQL(", ").join(
            [
                *(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in _MUTABLE),
                sql.SQL("updated_at = now()"),
                sql.SQL("last_seen_at = now()"),
            ]
        )
        statement = sql.SQL(
            """
            INSERT INTO {target} AS t ({insert_cols})
            SELECT {select_list} FROM _iris_stage s
            ORDER BY s.source_system, s.source_body_id, s.source_record_id
            ON CONFLICT (source_system, source_body_id, source_record_id) DO UPDATE SET {set_list}
            WHERE t.content_hash IS DISTINCT FROM EXCLUDED.content_hash
            RETURNING (xmax = 0) AS was_insert
            """
        ).format(
            target=target,
            insert_cols=_idents([*_DIRECT, "geom"]),
            select_list=select_list,
            set_list=set_list,
        )
        with conn.cursor() as cur:
            cur.execute(statement)
            return [bool(row[0]) for row in cur.fetchall()]

    def _touch_last_seen(self, conn: Conn) -> None:
        # Separate from the upsert on purpose: liveness is not mutation. Rows the
        # upsert just wrote already carry last_seen_at = now() (same transaction), so
        # the predicate skips them and this statement only touches re-observed rows.
        statement = sql.SQL(
            """
            UPDATE {target} AS t SET last_seen_at = now()
            FROM _iris_stage s
            WHERE t.source_system = s.source_system
              AND t.source_body_id = s.source_body_id
              AND t.source_record_id = s.source_record_id
              AND t.last_seen_at < now()
            """
        ).format(target=table(self._schema, "signal_ingest"))
        with conn.cursor() as cur:
            cur.execute(statement)

    # -- rejects ---------------------------------------------------------------

    def write_rejects(self, rejects: Iterable[RejectedRecord]) -> int:
        """Persist dead-lettered records; returns how many rows were written."""
        with connect(self._dsn) as conn:
            return write_rejects(conn, rejects, schema=self._schema)

    # -- run bookkeeping -------------------------------------------------------

    def record_verdict(
        self,
        run_id: UUID,
        verdict: str,
        findings: Sequence[Mapping[str, str]] = (),
    ) -> None:
        """Store the run's own quality verdict in ``harvest_run.notes['quality']``.

        ``iris-harvest report`` rebuilds a report from stored rows, which cannot recover
        everything the run measured. Carrying the verdict the run itself computed is what
        stops a rebuilt report from regrading (final-review I-5). Merged into ``notes``,
        so ``finish_run``'s own keys are untouched.
        """
        statement = sql.SQL("UPDATE {} SET notes = notes || %s::jsonb WHERE id = %s").format(
            table(self._schema, "harvest_run")
        )
        quality = {"quality": {"verdict": verdict, "findings": [dict(f) for f in findings]}}
        with connect(self._dsn) as conn:
            conn.execute(statement, (_json(quality), str(run_id)))

    def start_run(
        self,
        run_id: UUID,
        source_system: str,
        *,
        config_hash: str | None = None,
        taxonomy_version: str | None = None,
    ) -> None:
        """Register a run. ``signal_ingest`` and rejects reference it by foreign key."""
        statement = sql.SQL(
            """
            INSERT INTO {} (id, source_system, config_hash, taxonomy_version)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """
        ).format(table(self._schema, "harvest_run"))
        with connect(self._dsn) as conn:
            conn.execute(statement, (str(run_id), source_system, config_hash, taxonomy_version))

    def finish_run(
        self,
        run_id: UUID,
        *,
        status: str,
        records_fetched: int,
        counts: LoadCounts,
        rejected: int,
        notes: dict[str, Any] | None = None,
    ) -> None:
        statement = sql.SQL(
            """
            UPDATE {} SET finished_at = now(), status = %s, records_fetched = %s,
                          inserted = %s, updated = %s, unchanged = %s, rejected = %s,
                          notes = %s::jsonb
            WHERE id = %s
            """
        ).format(table(self._schema, "harvest_run"))
        params = (
            status,
            records_fetched,
            counts.inserted,
            counts.updated,
            counts.unchanged,
            rejected,
            _json(notes or {}),
            str(run_id),
        )
        with connect(self._dsn) as conn:
            conn.execute(statement, params)
