"""
The sample-data banner. Shown at the top of every render, in every state (loaded, empty,
error), whenever the page is not reading a live database. It is not dismissible on purpose:
this project's claim is that nothing is silently guessed, so a viewer must never be able to
mistake the demo for live municipal data.
"""

from __future__ import annotations

import html

import streamlit as st

from ui.data.service import Snapshot


def render_sample_banner(snapshot: Snapshot) -> None:
    if not snapshot.is_sample:
        return
    run = html.escape(snapshot.sample_run_title or "unknown run")
    reason = ""
    if snapshot.db_fallback_reason:
        reason = (
            '<div class="sample-banner-note">IRIS_DB_DSN is set but the database could not be '
            f"used ({html.escape(snapshot.db_fallback_reason)}), so the sample snapshot is shown "
            "instead.</div>"
        )
    st.html(
        f"""
        <div class="sample-banner" role="alert">
            <div class="sample-banner-title">SAMPLE DATA - NOT LIVE</div>
            <div>
                Everything on this page comes from a <b>GENERATED synthetic corpus</b>. It is
                <b>not real municipal data</b>: every title, id, date and coordinate is invented
                (only the 8-digit municipality keys are real).
                It is a <b>snapshot of a committed pipeline run</b>
                (<code>docs/samples/</code>, showing: {run}) and
                <b>no live database is connected</b>.
            </div>
            {reason}
        </div>
        """
    )
