"""KPI row: total signals, councils, latest-run signals, rejections, latest verdict."""

from __future__ import annotations

import html

import streamlit as st

from ui import theme
from ui.data.service import Snapshot


def render_kpis(snapshot: Snapshot) -> None:
    totals = snapshot.totals
    run = snapshot.latest_run
    cols = st.columns(5, gap="medium")

    with cols[0]:
        metric_card(
            "Total signals",
            f"{int(totals.get('total') or 0):,}",
            f"{int(totals.get('without_geom') or 0):,} with no resolvable location",
        )

    with cols[1]:
        metric_card(
            "Councils (distinct AGS)",
            f"{int(totals.get('councils') or 0):,}",
            f"{int(totals.get('without_ags') or 0):,} signals carry no AGS (not counted)",
        )

    with cols[2]:
        if run is None:
            metric_card("Signals in latest run", "unknown", "no harvest_run row")
        else:
            seen = int(run["inserted"]) + int(run["updated"]) + int(run["unchanged"])
            metric_card(
                "Signals in latest run",
                f"{seen:,}",
                f"{int(run['inserted']):,} inserted - {int(run['updated']):,} updated - "
                f"{int(run['unchanged']):,} unchanged",
            )

    with cols[3]:
        latest_rejected = int(run["rejected"]) if run else 0
        metric_card(
            "Rejections (dead letters)",
            f"{snapshot.reject_total:,}",
            f"{latest_rejected:,} in the latest run",
        )

    with cols[4]:
        report = snapshot.report
        if report is not None:
            verdict = report["verdict"]
            thresholds = html.escape(str(report.get("thresholds_version", "?")))
            sub = f"quality gate, thresholds {thresholds}"
        else:
            verdict = "NOT RECORDED"
            sub = "no quality report file for the latest run; verdict is not stored in the database"
        color = theme.VERDICT_COLORS[verdict]
        metric_card(
            "Latest run verdict",
            f'<span class="verdict-pill" style="background:{color}">{html.escape(verdict)}</span>',
            sub,
            raw_value=True,
        )


def metric_card(title: str, value: str, subtitle: str, raw_value: bool = False) -> None:
    value_html = value if raw_value else html.escape(value)
    st.html(
        f"""
        <div class="kpi-card">
            <div class="kpi-title">{html.escape(title)}</div>
            <div class="kpi-value">{value_html}</div>
            <div class="kpi-sub">{html.escape(subtitle)}</div>
        </div>
        """
    )
