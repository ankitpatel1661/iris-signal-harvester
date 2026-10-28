"""
Quality panel: the latest run's funnel, the rejection histogram and the re-run delta.

When the harvester's quality report for the latest run exists on disk it supplies the full
funnel (including 'below taxonomy threshold' and 'intra-run duplicates', which are not stored
in PostgreSQL) and the findings behind the verdict. Without it the funnel is built from
``harvest_run`` alone and the part that the database cannot account for is labelled as such.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui import theme
from ui.data.service import Snapshot


def render_quality_panel(snapshot: Snapshot) -> None:
    st.html(
        '<div class="section-title">Run quality</div>'
        '<div class="section-sub">What the latest harvest fetched, what it kept, what it rejected '
        "and why, and whether a re-run changed anything.</div>"
    )
    run = snapshot.latest_run
    if run is None:
        st.caption("No harvest_run rows yet, so there is no funnel, histogram or delta to show.")
        return

    funnel_col, reject_col, delta_col = st.columns(3, gap="medium")
    with funnel_col:
        _funnel(snapshot, run)
    with reject_col:
        _rejections(snapshot)
    with delta_col:
        _rerun_delta(snapshot)

    _findings(snapshot)
    _reject_sample(snapshot)


# ---------------------------------------------------------------------------
def _funnel(snapshot: Snapshot, run: dict[str, Any]) -> None:
    st.markdown("**Latest run funnel**")
    report = snapshot.report
    caption = ""
    if report is not None:
        funnel = report["funnel"]
        labels = ["fetched"] + [s["label"] for s in funnel["steps"]]
        values = [funnel["fetched"]] + [s["remaining"] for s in funnel["steps"]]
        # the last step is the load outcome, which repeats the staged count; drop the repeat
        if len(values) > 1 and values[-1] == values[-2]:
            labels, values = labels[:-1], values[:-1]
        caption = "From the harvester's quality report for this run."
        if not funnel.get("balances", True):
            caption += f" The funnel does NOT balance (residual {funnel.get('residual')})."
    else:
        fetched = int(run["records_fetched"])
        rejected = int(run["rejected"])
        loaded = int(run["inserted"]) + int(run["updated"]) + int(run["unchanged"])
        labels = ["fetched", "not dead-lettered", "staged (inserted + updated + unchanged)"]
        values = [fetched, fetched - rejected, loaded]
        unexplained = fetched - rejected - loaded
        caption = (
            "Built from harvest_run only. "
            f"{unexplained:,} fetched record(s) are neither rejected nor staged: "
            "the database does not "
            "record whether they were below the taxonomy threshold or intra-run duplicates."
            if unexplained
            else "Built from harvest_run only; it balances."
        )

    fig = go.Figure(
        go.Funnel(
            y=labels,
            x=values,
            textinfo="value",
            marker={"color": theme.PRIMARY},
            connector={"line": {"color": theme.BORDER}},
        )
    )
    layout = theme.plotly_layout(height=300)
    layout["margin"] = {"l": 8, "r": 8, "t": 8, "b": 8}
    fig.update_layout(**layout)
    st.plotly_chart(fig, width="stretch", key="quality_funnel")
    st.caption(caption)


def _rejections(snapshot: Snapshot) -> None:
    st.markdown("**Rejections by reason_code**")
    scope = st.radio(
        "Scope",
        ["Latest run", "All runs"],
        horizontal=True,
        label_visibility="collapsed",
        key="reject_scope",
    )
    hist = (
        snapshot.reject_by_reason_latest if scope == "Latest run" else snapshot.reject_by_reason_all
    )
    if hist.empty:
        st.html('<div class="notice info">No rejected records in this scope.</div>')
        return
    fig = go.Figure(
        go.Bar(
            y=hist["reason_code"],
            x=hist["n"],
            orientation="h",
            marker_color=theme.DANGER,
            text=hist["n"],
            textposition="outside",
            cliponaxis=False,
        )
    )
    layout = theme.plotly_layout(height=max(220, 34 * len(hist) + 60))
    layout["margin"] = {"l": 8, "r": 40, "t": 8, "b": 8}
    fig.update_layout(**layout, showlegend=False)
    fig.update_yaxes(autorange="reversed", automargin=True)
    fig.update_xaxes(showgrid=True, gridcolor=theme.BORDER, zeroline=False)
    st.plotly_chart(fig, width="stretch", key="quality_rejects")


def _rerun_delta(snapshot: Snapshot) -> None:
    st.markdown("**Re-run delta (last two runs)**")
    latest, previous = snapshot.latest_run, snapshot.previous_run
    if latest is None or previous is None:
        st.html(
            '<div class="notice info">Only one harvest run exists, so there is no re-run to '
            "compare against.</div>"
        )
        return
    metrics = ["inserted", "updated", "unchanged"]
    fig = go.Figure()
    fig.add_bar(
        name="Previous run",
        x=metrics,
        y=[int(previous[m]) for m in metrics],
        marker_color=theme.NEUTRAL,
    )
    fig.add_bar(
        name="Latest run",
        x=metrics,
        y=[int(latest[m]) for m in metrics],
        marker_color=theme.PRIMARY,
    )
    fig.update_layout(**theme.plotly_layout(height=260), barmode="group")
    fig.update_yaxes(showgrid=True, gridcolor=theme.BORDER)
    st.plotly_chart(fig, width="stretch", key="quality_delta")

    idempotent = (
        int(latest["inserted"]) == 0
        and int(latest["updated"]) == 0
        and int(latest["unchanged"]) > 0
    )
    same_source = latest["source_system"] == previous["source_system"]
    if idempotent and same_source:
        st.caption("Latest run inserted and updated nothing: the re-run was idempotent.")
    elif not same_source:
        st.caption(
            f"The two runs used different sources ({previous['source_system']} then "
            f"{latest['source_system']}), so this is not a like-for-like re-run."
        )
    else:
        st.caption("The latest run changed data relative to the previous one.")


def _findings(snapshot: Snapshot) -> None:
    report = snapshot.report
    if report is None:
        return
    findings = report.get("findings") or []
    if not findings:
        return
    with st.expander(
        f"Quality-gate findings ({len(findings)})", expanded=report["verdict"] != "PASS"
    ):
        for item in findings:
            level = item.get("level", "?")
            st.markdown(f"- **{level}** `{item.get('rule', '?')}` - {item.get('detail', '')}")


def _reject_sample(snapshot: Snapshot) -> None:
    sample: pd.DataFrame = snapshot.recent_rejects
    if sample.empty:
        return
    with st.expander(f"Rejected records in the latest run (up to {len(sample)} shown)"):
        st.dataframe(sample.fillna("unknown"), width="stretch", hide_index=True)
