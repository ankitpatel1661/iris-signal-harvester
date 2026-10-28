"""
IRIS Signal Harvester dashboard - entry point.

    IRIS_DB_DSN=postgresql://... streamlit run ui/app.py   # live, read-only database
    streamlit run streamlit_app.py                         # no database: committed samples

A read-only window onto iris_staging (or, with no reachable database, onto the committed
synthetic sample runs in docs/samples/, always under a persistent banner saying so).
It exists to make one claim inspectable: nothing is silently guessed.
Every date shows its origin, every point shows its method and confidence,
every rejected record shows its reason, and every unknown is labelled unknown.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st  # noqa: E402

from ui import config, theme  # noqa: E402
from ui.components.empty_state import (  # noqa: E402
    render_empty_state,
    render_error_state,
    render_no_match_state,
)
from ui.components.header import render_header  # noqa: E402
from ui.components.kpi_section import render_kpis  # noqa: E402
from ui.components.map import render_map  # noqa: E402
from ui.components.provenance_panel import render_provenance_panel  # noqa: E402
from ui.components.quality_panel import render_quality_panel  # noqa: E402
from ui.components.sample_banner import render_sample_banner  # noqa: E402
from ui.components.sidebar import SAMPLE_RUN_STATE_KEY, render_sidebar  # noqa: E402
from ui.components.signal_table import render_signal_table  # noqa: E402
from ui.data.filters import apply_filters  # noqa: E402
from ui.data.samples import DEFAULT_RUN  # noqa: E402
from ui.data.service import load_snapshot  # noqa: E402

st.set_page_config(
    page_title=config.PAGE_TITLE,
    page_icon=config.PAGE_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

css_file = Path(__file__).parent / "assets" / "styles.css"
if css_file.exists():
    st.markdown(f"<style>{css_file.read_text()}</style>", unsafe_allow_html=True)

# Which committed sample run to show (only used when no database is configured/reachable).
# The radio in the sidebar owns this key; before it exists the default run is used.
snapshot = load_snapshot(st.session_state.get(SAMPLE_RUN_STATE_KEY, DEFAULT_RUN))
filters = render_sidebar(snapshot)
render_sample_banner(snapshot)
render_header(snapshot)

if snapshot.error:
    render_error_state(snapshot.error, sample=snapshot.is_sample)
    st.stop()

if snapshot.is_empty:
    render_empty_state()
    st.stop()

render_kpis(snapshot)

if snapshot.truncated:
    st.html(
        f'<div class="notice">The table holds {int(snapshot.totals["total"]):,} signals; only the '
        f"{len(snapshot.signals):,} highest-scoring are loaded into the charts and table below "
        f"(limit {config.SIGNAL_LOAD_LIMIT:,}). KPI counts above are for the whole table.</div>"
    )

filtered = apply_filters(snapshot.signals, filters)
if filters.active:
    st.caption(
        f"Filters active: {len(filtered):,} of {len(snapshot.signals):,} loaded signals match."
    )

if filtered.empty and not snapshot.signals.empty:
    render_no_match_state()
else:
    render_provenance_panel(filtered)
    render_map(filtered, theme.signal_type_colors(snapshot.signal_types))
    render_signal_table(filtered)

render_quality_panel(snapshot)
