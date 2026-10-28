"""Explanatory states for 'no database' and 'no data yet'. Never a traceback."""

from __future__ import annotations

import html

import streamlit as st

from ui import config


def render_error_state(message: str, *, sample: bool = False) -> None:
    if sample:
        st.html(
            f"""
            <div class="empty-state">
                <h3>The sample snapshot could not be loaded</h3>
                <div>{html.escape(message)}</div>
            </div>
            """
        )
        return
    st.html(
        f"""
        <div class="empty-state">
            <h3>The harvest database is not available</h3>
            <div>{html.escape(message)}</div>
            <div style="margin-top:10px;font-size:12px;">
                Set <code>{config.DSN_ENV_VAR}</code> to a PostgreSQL DSN and reload,
                e.g. <code>export {config.DSN_ENV_VAR}=postgresql://user:password@localhost:5433/iris</code>.
                The dashboard only ever runs SELECT statements.
            </div>
        </div>
        """
    )


def render_empty_state() -> None:
    st.html(
        """
        <div class="empty-state">
            <h3>Nothing has been harvested yet</h3>
            <div>The schema exists but contains no signals and no harvest runs, so there is
            nothing to chart. Nothing is shown rather than made up.</div>
            <div style="margin-top:10px;font-size:12px;">
                Run <code>make demo</code> (offline fixtures) or <code>make harvest</code>,
                then press <b>Refresh data</b>.
            </div>
        </div>
        """
    )


def render_no_match_state() -> None:
    st.html(
        """
        <div class="notice info">No signals match the current filters. Clear a filter in the
        sidebar to see more.</div>
        """
    )
