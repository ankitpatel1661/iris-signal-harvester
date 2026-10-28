"""Page header: title, tagline and the freshness of the data being shown."""

from __future__ import annotations

import streamlit as st

from ui import config
from ui.data.service import Snapshot


def render_header(snapshot: Snapshot) -> None:
    run = snapshot.latest_run
    eyebrow = "Read-only view of iris_staging"
    if snapshot.is_sample:
        eyebrow = "Sample snapshot of a committed run - no live database"
    if run is None:
        freshness = "No harvest run recorded"
        dot = "hdr-dot stale"
    else:
        finished = run.get("finished_at") or run.get("started_at")
        when = finished.strftime("%Y-%m-%d %H:%M UTC") if finished else "unknown time"
        freshness = f"Latest run {when} - source: {run['source_system']} - status: {run['status']}"
        dot = "hdr-dot stale" if snapshot.is_sample else "hdr-dot"

    st.html(
        f"""
        <div>
            <div class="hdr-eyebrow"><span class="{dot}"></span>{eyebrow}</div>
            <div class="hdr-title">{config.PAGE_TITLE}</div>
            <div class="hdr-sub">{config.APP_TAGLINE}</div>
            <div class="hdr-sub">{freshness}</div>
        </div>
        """
    )
