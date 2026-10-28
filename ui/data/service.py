"""
Cached facade over ``queries`` / ``samples`` / ``reports``: the only module the components
import data from.

Data source selection (``load_snapshot``): the PostgreSQL path when ``IRIS_DB_DSN`` is set and
the database is reachable, otherwise the committed sample files in ``docs/samples/``. Both
produce the same ``Snapshot``; ``Snapshot.source`` says which one, and the page shows a
persistent banner whenever it is the samples.

``@st.cache_data`` with a short TTL (config.CACHE_TTL_S). Failures are returned as a value
(``Snapshot.error``), never raised into the page, so the UI can render an explanatory state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import streamlit as st

from ui import config
from ui.data import queries, samples
from ui.data.connection import DataUnavailable, dsn_configured, probe
from ui.data.reports import load_quality_report


@dataclass
class Snapshot:
    """Everything the page needs, loaded in one cached call."""

    error: str | None = None
    totals: dict[str, Any] = field(default_factory=dict)
    runs: list[dict[str, Any]] = field(default_factory=list)
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)
    signal_types: list[str] = field(default_factory=list)
    reject_total: int = 0
    reject_by_reason_all: pd.DataFrame = field(default_factory=pd.DataFrame)
    reject_by_reason_latest: pd.DataFrame = field(default_factory=pd.DataFrame)
    recent_rejects: pd.DataFrame = field(default_factory=pd.DataFrame)
    report: dict[str, Any] | None = None
    # Where the data came from. "database" = live iris_staging; "sample" = the committed
    # synthetic-corpus snapshot in docs/samples/. The page must say which, on every render.
    source: str = "database"
    sample_run_key: str | None = None
    sample_run_title: str | None = None
    # Set when IRIS_DB_DSN is configured but the database could not be reached.
    db_fallback_reason: str | None = None

    @property
    def is_sample(self) -> bool:
        return self.source == "sample"

    @property
    def latest_run(self) -> dict[str, Any] | None:
        return self.runs[0] if self.runs else None

    @property
    def previous_run(self) -> dict[str, Any] | None:
        return self.runs[1] if len(self.runs) > 1 else None

    @property
    def is_empty(self) -> bool:
        return not self.totals.get("total") and not self.runs

    @property
    def truncated(self) -> bool:
        total = int(self.totals.get("total") or 0)
        return total > len(self.signals)


@st.cache_data(show_spinner="Reading harvest data ...", ttl=config.CACHE_TTL_S)
def load_snapshot(sample_run: str = samples.DEFAULT_RUN) -> Snapshot:
    """Database when ``IRIS_DB_DSN`` is set and reachable, otherwise the committed samples.

    ``sample_run`` only matters on the sample path (which committed run to show).
    """
    fallback_reason: str | None = None
    if dsn_configured():
        fallback_reason = probe()
        if fallback_reason is None:
            return _load_database()
    try:
        snapshot = samples.load_sample_snapshot(sample_run)
    except samples.SampleDataError as exc:
        return Snapshot(error=str(exc), source="sample", db_fallback_reason=fallback_reason)
    snapshot.db_fallback_reason = fallback_reason
    return snapshot


def _load_database() -> Snapshot:
    """The read-only PostgreSQL path (the query logic itself is unchanged)."""
    try:
        if not queries.schema_ready():
            return Snapshot(
                error=(
                    f"The table {config.DB_SCHEMA}.signal_ingest does not exist yet "
                    "(apply migrations/001_initial.sql, then run a harvest)."
                )
            )
        runs = queries.recent_runs(2)
        latest_id = runs[0]["id"] if runs else None
        return Snapshot(
            totals=queries.signal_totals(),
            runs=runs,
            signals=queries.signals(),
            signal_types=queries.signal_types(),
            reject_total=queries.reject_total(),
            reject_by_reason_all=queries.reject_histogram(),
            reject_by_reason_latest=queries.reject_histogram(latest_id)
            if latest_id
            else pd.DataFrame(),
            recent_rejects=queries.recent_rejects(latest_id) if latest_id else pd.DataFrame(),
            report=load_quality_report(latest_id),
        )
    except DataUnavailable as exc:
        return Snapshot(error=str(exc))
