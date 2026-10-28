"""Sidebar: refresh control and the signal filters. Returns the chosen ``Filters``."""

from __future__ import annotations

import streamlit as st

from ui import config
from ui.data import samples
from ui.data.filters import Filters, label_column
from ui.data.service import Snapshot, load_snapshot

SAMPLE_RUN_STATE_KEY = "sample_run_key"


def render_sidebar(snapshot: Snapshot) -> Filters:
    with st.sidebar:
        st.html(
            """
            <div class="sidebar-brand">
                <div class="sidebar-brand-title">IRIS Signal Harvester</div>
                <div class="sidebar-brand-sub">Provenance dashboard - read only</div>
            </div>
            """
        )

        if st.button("Refresh data", help=f"Results are cached for {config.CACHE_TTL_S} s."):
            load_snapshot.clear()
            st.rerun()

        if snapshot.is_sample:
            _sample_run_selector()

        df = snapshot.signals
        if snapshot.error or df.empty:
            st.caption("Filters appear once signals are loaded.")
            return Filters()

        st.markdown("**Filters** (empty = no restriction)")

        type_options = sorted(label_column(df["signal_type"], config.UNCLASSIFIED_LABEL).unique())
        land_options = sorted(label_column(df["bundesland"], config.UNKNOWN_LABEL).unique())
        confidence_options = [c for c in config.CONFIDENCE_ORDER if c in set(df["geo_confidence"])]

        signal_types = st.multiselect("Signal type", type_options)
        bundeslaender = st.multiselect("Bundesland", land_options)
        confidences = st.multiselect(
            "Geo confidence",
            confidence_options,
            help="'none' = the signal has no geometry at all.",
        )
        min_score = st.slider("Minimum relevance score", 0.0, 1.0, 0.0, 0.05)
        include_unscored = st.checkbox(
            "Include signals with unknown score",
            value=True,
            help="relevance_score is NULL for these; they are never given a default value.",
        )

    return Filters(
        signal_types=signal_types,
        bundeslaender=bundeslaender,
        confidences=confidences,
        min_score=min_score,
        include_unscored=include_unscored,
    )


def _sample_run_selector() -> None:
    """Pick which committed sample run to show (the degraded one carries the FAIL verdict)."""
    st.html('<div class="sidebar-sample-tag">SAMPLE DATA - not live</div>')
    options = samples.run_options()
    labels = dict(options)
    st.radio(
        "Sample run",
        [key for key, _ in options],
        format_func=lambda key: labels[key],
        key=SAMPLE_RUN_STATE_KEY,
        help=(
            "Committed runs over the generated corpus. 'corpus_degraded' is the run with "
            "deliberately faulty input: it has the full rejection histogram and the FAIL verdict."
        ),
    )
