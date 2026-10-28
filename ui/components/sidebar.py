"""Sidebar: the data controls and the signal filters. Returns the chosen ``Filters``.

Grouped top to bottom: *Data* (sample-run selector, refresh) then *Filters* (what kind of
signal, where, how well located). Every filter carries a ``help=`` tooltip.
"""

from __future__ import annotations

import streamlit as st

from ui import config
from ui.data import samples
from ui.data.filters import Filters, label_column
from ui.data.service import Snapshot, load_snapshot
from ui.help_text import GEO_CONFIDENCE_NONE, GEO_CONFIDENCE_SUMMARY, signal_type_help

SAMPLE_RUN_STATE_KEY = "sample_run_key"

RERUN_KEY = "main_rerun"

# Filter widgets are keyed with a version counter and the sample run, so "Clear filters" can
# reset them by bumping the counter, and a selection made for one run can never leak into
# another run whose options differ.
_VERSION_KEY = "flt_version"


def _key(name: str, snapshot: Snapshot) -> str:
    version = st.session_state.get(_VERSION_KEY, 0)
    return f"flt_{name}_{version}_{snapshot.sample_run_key or 'db'}"


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

        _group_title("Data")
        if snapshot.is_sample:
            _sample_run_selector()
        if st.button(
            "Refresh data",
            help=f"Re-read the data. Results are cached for {config.CACHE_TTL_S} s.",
        ):
            load_snapshot.clear()
            st.rerun()

        df = snapshot.signals
        if snapshot.error or df.empty:
            st.caption("Filters appear once signals are loaded.")
            return Filters()

        _group_title("Filters")
        st.caption("Leave a filter empty for no restriction.")
        st.button(
            "Clear filters",
            on_click=_clear_filters,
            help="Reset every filter below.",
            key="flt_clear",
        )

        type_options = sorted(label_column(df["signal_type"], config.UNCLASSIFIED_LABEL).unique())
        land_options = sorted(label_column(df["bundesland"], config.UNKNOWN_LABEL).unique())
        confidence_options = [c for c in config.CONFIDENCE_ORDER if c in set(df["geo_confidence"])]

        _subgroup_title("What")
        signal_types = st.multiselect(
            "Signal type",
            type_options,
            key=_key("types", snapshot),
            help=(
                "Which kind of planning or infrastructure topic a document is about "
                "(German municipal planning vocabulary):\n\n" + signal_type_help()
            ),
        )
        min_score = st.slider(
            "Min. relevance score",
            0.0,
            1.0,
            0.0,
            0.05,
            key=_key("score", snapshot),
            help=(
                "`relevance_score` (0-1): how strongly a document matched the taxonomy. "
                "Only signals at or above this value are kept."
            ),
        )
        include_unscored = st.checkbox(
            "Keep unscored signals",
            value=True,
            key=_key("unscored", snapshot),
            help="relevance_score is NULL for these; they are never given a default value.",
        )

        _subgroup_title("Where")
        bundeslaender = st.multiselect(
            "Bundesland",
            land_options,
            key=_key("lands", snapshot),
            help="German federal state. 'unknown' = the source did not record one.",
        )
        confidences = st.multiselect(
            "Location confidence",
            confidence_options,
            key=_key("conf", snapshot),
            help=f"`geo_confidence`. {GEO_CONFIDENCE_SUMMARY}\n\n{GEO_CONFIDENCE_NONE}",
        )

    return Filters(
        signal_types=signal_types,
        bundeslaender=bundeslaender,
        confidences=confidences,
        min_score=min_score,
        include_unscored=include_unscored,
    )


def _group_title(text: str) -> None:
    st.html(f'<div class="sidebar-group">{text}</div>')


def _subgroup_title(text: str) -> None:
    st.html(f'<div class="sidebar-subgroup">{text}</div>')


def _clear_filters() -> None:
    """Button callback: bump the version so every filter widget starts again from its default."""
    st.session_state[_VERSION_KEY] = st.session_state.get(_VERSION_KEY, 0) + 1


def show_rerun_sample() -> None:
    """Button callback: switch the sample-run selector to the re-run option."""
    st.session_state[SAMPLE_RUN_STATE_KEY] = RERUN_KEY


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
            "Committed runs over the generated corpus.\n\n"
            "- `corpus_main, first load`: the first load; verdict PASS.\n"
            "- `corpus_main, re-run`: the same corpus loaded again. Nothing is inserted or "
            "updated, so it is the only option where the re-run delta can be shown.\n"
            "- `corpus_degraded`: deliberately faulty input. It has the full rejection "
            "histogram and the FAIL verdict."
        ),
    )
