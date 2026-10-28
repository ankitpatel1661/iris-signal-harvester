"""
Every SQL statement the dashboard issues. All of them are SELECTs.

Functions return plain Python / pandas objects and do no formatting. NULL stays NULL here:
the presentation layer decides how to label an unknown, nothing is defaulted in the query.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from psycopg import sql

from ui import config
from ui.data.connection import fetch_all, table

SIGNAL_COLUMNS = [
    "id",
    "title",
    "document_type",
    "source_system",
    "source_url",
    "source_licence",
    "municipality_name",
    "kreis",
    "bundesland",
    "ags",
    "signal_type",
    "relevance_score",
    "matched_keywords",
    "published_at",
    "published_at_origin",
    "geo_method",
    "geo_confidence",
    "lon",
    "lat",
    "first_seen_at",
    "last_seen_at",
    "harvest_run_id",
]


def schema_ready() -> bool:
    rows = fetch_all(
        "SELECT to_regclass(%s) IS NOT NULL AS ok",
        (f"{config.DB_SCHEMA}.signal_ingest",),
    )
    return bool(rows and rows[0]["ok"])


def signal_totals() -> dict[str, Any]:
    """Whole-table counts, independent of any filter and of the row-load cap."""
    statement = sql.SQL(
        """
        SELECT count(*)                                    AS total,
               count(DISTINCT ags)                         AS councils,
               count(*) FILTER (WHERE ags IS NULL)         AS without_ags,
               count(*) FILTER (WHERE geom IS NULL)        AS without_geom
        FROM {}
        """
    ).format(table("signal_ingest"))
    return fetch_all(statement)[0]


def recent_runs(limit: int = 2) -> list[dict[str, Any]]:
    statement = sql.SQL(
        """
        SELECT id::text AS id, source_system, started_at, finished_at, status,
               taxonomy_version, records_fetched, inserted, updated, unchanged, rejected,
               notes
        FROM {}
        ORDER BY started_at DESC
        LIMIT %s
        """
    ).format(table("harvest_run"))
    return fetch_all(statement, (limit,))


def reject_total() -> int:
    statement = sql.SQL("SELECT count(*) AS n FROM {}").format(table("signal_ingest_reject"))
    return int(fetch_all(statement)[0]["n"])


def reject_histogram(run_id: str | None = None) -> pd.DataFrame:
    """Rows of ``signal_ingest_reject`` grouped by reason_code (all runs, or one run)."""
    where = sql.SQL("WHERE harvest_run_id = %s::uuid") if run_id else sql.SQL("")
    statement = sql.SQL(
        "SELECT reason_code, count(*) AS n FROM {} {} "
        "GROUP BY reason_code ORDER BY n DESC, reason_code"
    ).format(table("signal_ingest_reject"), where)
    rows = fetch_all(statement, (run_id,) if run_id else None)
    return pd.DataFrame(rows, columns=["reason_code", "n"])


def recent_rejects(run_id: str, limit: int = 50) -> pd.DataFrame:
    """Reject rows for one run, WITHOUT raw_payload (can be large; not needed for triage)."""
    statement = sql.SQL(
        """
        SELECT reason_code, reason_detail, source_system, source_body_id, source_record_id,
               rejected_at
        FROM {}
        WHERE harvest_run_id = %s::uuid
        ORDER BY rejected_at DESC, id DESC
        LIMIT %s
        """
    ).format(table("signal_ingest_reject"))
    return pd.DataFrame(fetch_all(statement, (run_id, limit)))


def signal_types() -> list[str]:
    statement = sql.SQL(
        "SELECT DISTINCT signal_type FROM {} WHERE signal_type IS NOT NULL ORDER BY 1"
    ).format(table("signal_ingest"))
    return [r["signal_type"] for r in fetch_all(statement)]


def signals(limit: int = config.SIGNAL_LOAD_LIMIT) -> pd.DataFrame:
    """Signals with WGS84 lon/lat derived from the stored EPSG:25832 point.

    ``lon``/``lat`` are NULL exactly when ``geom`` is NULL. raw_payload is deliberately not
    selected. Ordered so that, if the cap bites, the highest-scoring signals are kept.
    """
    statement = sql.SQL(
        """
        SELECT id, title, document_type, source_system, source_url, source_licence,
               municipality_name, kreis, bundesland, trim(ags) AS ags,
               signal_type, relevance_score, matched_keywords,
               published_at, published_at_origin, geo_method, geo_confidence,
               ST_X(ST_Transform(geom, 4326)) AS lon,
               ST_Y(ST_Transform(geom, 4326)) AS lat,
               first_seen_at, last_seen_at, harvest_run_id::text AS harvest_run_id
        FROM {}
        ORDER BY relevance_score DESC NULLS LAST, id
        LIMIT %s
        """
    ).format(table("signal_ingest"))
    return pd.DataFrame(fetch_all(statement, (limit,)), columns=SIGNAL_COLUMNS)
