"""
Quality panel: the latest run's funnel, the rejection histogram and the re-run delta.

When the harvester's quality report for the latest run exists on disk it supplies the full
funnel (including 'below taxonomy threshold' and 'intra-run duplicates', which are not stored
in PostgreSQL) and the findings behind the verdict. Without it the funnel is built from
``harvest_run`` alone and the part that the database cannot account for is labelled as such.
"""

from __future__ import annotations

import textwrap
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui import theme
from ui.components.sidebar import show_rerun_sample
from ui.data.service import Snapshot
from ui.help_text import (
    FUNNEL_STAGE_HELP,
    KPI_HELP,
    NO_DEFINITION,
    REJECT_CODE_HELP,
    plain,
    reject_code_meaning,
)


def render_quality_panel(snapshot: Snapshot) -> None:
    st.html(
        '<div class="section-title">Run quality</div>'
        '<div class="section-sub">What the latest harvest fetched, what it kept, what it rejected '
        "and why, and whether a re-run changed anything. "
        "The run verdict (PASS / WARN / FAIL) is the quality gate in "
        "<code>config/thresholds.yaml</code>.</div>"
    )
    run = snapshot.latest_run
    if run is None:
        st.caption("No harvest_run rows yet, so there is no funnel, histogram or delta to show.")
        return

    _funnel(snapshot, run)

    reject_col, delta_col = st.columns(2, gap="medium")
    with reject_col:
        _rejections(snapshot)
    with delta_col:
        _rerun_delta(snapshot)

    _findings(snapshot)
    _reject_sample(snapshot)


# ---------------------------------------------------------------------------
def _funnel(snapshot: Snapshot, run: dict[str, Any]) -> None:
    st.markdown(
        "**Latest run funnel** - read top to bottom",
        help=(
            "Every fetched record either reaches the staging table or leaves at one stage. "
            "Each row shows how many records are still in play after that stage. "
            "Records leaving at 'malformed by adapter' or 'rejected by normaliser' are "
            "dead-lettered. 'Below taxonomy threshold' is not an error: it is a real council "
            "document that is simply not an infrastructure signal."
        ),
    )
    report = snapshot.report
    removed: list[int | None]
    caption = ""
    if report is not None:
        funnel = report["funnel"]
        labels = ["fetched"] + [s["label"] for s in funnel["steps"]]
        values = [funnel["fetched"]] + [s["remaining"] for s in funnel["steps"]]
        removed = [None] + [s["removed"] for s in funnel["steps"]]
        # the last step is the load outcome, which repeats the staged count; drop the repeat
        if len(values) > 1 and values[-1] == values[-2]:
            labels, values, removed = labels[:-1], values[:-1], removed[:-1]
        caption = "From the harvester's quality report for this run."
        if not funnel.get("balances", True):
            caption += f" The funnel does NOT balance (residual {funnel.get('residual')})."
    else:
        fetched = int(run["records_fetched"])
        rejected = int(run["rejected"])
        loaded = int(run["inserted"]) + int(run["updated"]) + int(run["unchanged"])
        labels = ["fetched", "not dead-lettered", "staged (inserted + updated + unchanged)"]
        values = [fetched, fetched - rejected, loaded]
        removed = [None, None, None]
        unexplained = fetched - rejected - loaded
        caption = (
            "Built from harvest_run only. "
            f"{unexplained:,} fetched record(s) are neither rejected nor staged: "
            "the database does not "
            "record whether they were below the taxonomy threshold or intra-run duplicates."
            if unexplained
            else "Built from harvest_run only; it balances."
        )

    chart_col, table_col = st.columns([4, 7], gap="medium")
    with chart_col:
        fig = go.Figure(
            go.Funnel(
                y=[f"{i}. {label}" for i, label in enumerate(labels, start=1)],
                x=values,
                textinfo="value",
                marker={"color": theme.PRIMARY},
                connector={"line": {"color": theme.BORDER}},
                customdata=[plain(FUNNEL_STAGE_HELP.get(label, "")) for label in labels],
                hovertemplate="%{y}: %{x:,}<br>%{customdata}<extra></extra>",
            )
        )
        layout = theme.plotly_layout(height=max(300, 56 * len(labels)))
        layout["margin"] = {"l": 8, "r": 8, "t": 8, "b": 8}
        fig.update_layout(**layout)
        # stage 1 at the top, so the chart reads downwards like the table beside it
        fig.update_yaxes(autorange="reversed")
        st.plotly_chart(fig, width="stretch", key="quality_funnel")
    with table_col:
        has_removed = any(r is not None for r in removed)
        header = (
            "| # | Stage | Remaining | "
            + ("Removed here | " if has_removed else "")
            + "What it means |"
        )
        rule = "|--:|---|--:|" + ("--:|" if has_removed else "") + "---|"
        lines = [header, rule]
        for i, (label, value, drop) in enumerate(zip(labels, values, removed, strict=True), 1):
            cells = [str(i), f"**{label}**", f"{int(value):,}"]
            if has_removed:
                cells.append("-" if drop is None else f"{int(drop):,}")
            cells.append(FUNNEL_STAGE_HELP.get(label, NO_DEFINITION))
            lines.append("| " + " | ".join(cells) + " |")
        st.markdown("\n".join(lines))
    st.caption(caption)


def _rejections(snapshot: Snapshot) -> None:
    st.markdown(
        "**Rejections by reason_code**",
        help=(
            KPI_HELP["rejections"] + " Each bar is one `reason_code`; most frequent first. "
            "Open 'What these reason codes mean' below the chart for the definitions."
        ),
    )
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
    codes = list(hist["reason_code"])
    fig = go.Figure(
        go.Bar(
            y=codes,
            x=hist["n"],
            orientation="h",
            marker_color=theme.DANGER,
            text=hist["n"],
            textposition="outside",
            cliponaxis=False,
            customdata=[
                "<br>".join(textwrap.wrap(plain(reject_code_meaning(c)), 48)) for c in codes
            ],
            hovertemplate="%{y}: %{x}<br>%{customdata}<extra></extra>",
        )
    )
    layout = theme.plotly_layout(height=max(220, 34 * len(hist) + 60))
    layout["margin"] = {"l": 8, "r": 40, "t": 8, "b": 8}
    fig.update_layout(**layout, showlegend=False)
    fig.update_yaxes(autorange="reversed", automargin=True)
    fig.update_xaxes(showgrid=True, gridcolor=theme.BORDER, zeroline=False)
    st.plotly_chart(fig, width="stretch", key="quality_rejects")
    st.caption(f"{scope}, most frequent reason first.")

    with st.expander("What these reason codes mean"):
        rows = [
            f"| `{code}` | {int(n):,} | {REJECT_CODE_HELP.get(code, NO_DEFINITION)} |"
            for code, n in zip(hist["reason_code"], hist["n"], strict=True)
        ]
        st.markdown("\n".join(["| Reason code | Records | Meaning |", "|---|--:|---|", *rows]))


def _rerun_delta(snapshot: Snapshot) -> None:
    st.markdown(
        "**Re-run delta (last two runs)**",
        help=(
            "Compares the latest harvest run with the one before it. Both runs are recorded "
            "in the same database, so this panel needs two runs to exist. "
            "inserted = rows new to the table; updated = rows already there whose content "
            "changed; unchanged = rows already there and identical. "
            "A re-run of the same data should insert and update nothing (idempotent)."
        ),
    )
    latest, previous = snapshot.latest_run, snapshot.previous_run
    if latest is None or previous is None:
        _no_rerun_notice(snapshot)
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


def _no_rerun_notice(snapshot: Snapshot) -> None:
    if snapshot.is_sample:
        st.html(
            '<div class="notice info">Only one harvest run exists, so there is no re-run to '
            "compare against. The delta compares <b>two harvest runs stored in one "
            "database</b> (the latest against the one before it). The sample run selected "
            "here is a single run; pick <b>corpus_main, re-run</b> in the sidebar's "
            "<i>Sample run</i> selector to see the same corpus loaded a second time.</div>"
        )
        st.button(
            "Show the corpus_main re-run",
            on_click=show_rerun_sample,
            key="show_rerun_btn",
            help="Switches the sidebar's Sample run selector to 'corpus_main, re-run'.",
        )
    else:
        st.html(
            '<div class="notice info">Only one harvest run exists, so there is no re-run to '
            "compare against. The delta compares <b>two harvest runs stored in the same "
            "database</b> (the latest against the one before it); it appears once the "
            "harvester has run a second time.</div>"
        )


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
        st.caption(
            "Each finding is one rule of the quality gate (`config/thresholds.yaml`) that fired, "
            "with its level."
        )
        for item in findings:
            level = item.get("level", "?")
            st.markdown(f"- **{level}** `{item.get('rule', '?')}` - {item.get('detail', '')}")


def _reject_sample(snapshot: Snapshot) -> None:
    sample: pd.DataFrame = snapshot.recent_rejects
    if sample.empty:
        return
    with st.expander(f"Rejected records in the latest run (up to {len(sample)} shown)"):
        st.caption(
            "Dead letters: each is stored with its reason and original payload, so it can be "
            "counted and replayed. Hover a column header for what it means."
        )
        st.dataframe(
            sample.fillna("unknown"),
            width="stretch",
            hide_index=True,
            column_config={
                "reason_code": st.column_config.TextColumn(
                    help="Why the record was rejected. See 'What these reason codes mean' above."
                ),
                "reason_detail": st.column_config.TextColumn(
                    help="The specific value or check that failed for this record."
                ),
                "source_body_id": st.column_config.TextColumn(
                    help="Identifier of the council body (as given by the source)."
                ),
                "source_record_id": st.column_config.TextColumn(
                    help="Identifier of the rejected record in the source system."
                ),
                "seen_count": st.column_config.NumberColumn(
                    help="How many times this record was rejected."
                ),
            },
        )
