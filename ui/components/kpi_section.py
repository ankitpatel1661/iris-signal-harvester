"""KPI row: total signals, councils, latest-run signals, rejections, latest verdict.

Each KPI is an ``st.metric`` (so its label carries a hover tooltip, ``help=``) inside a bordered
container, with the qualifying detail underneath as a caption. The verdict is coloured by CSS
(``.st-key-kpi_verdict_*`` in ``assets/styles.css``), keyed on the verdict so a colour always
means the same thing.
"""

from __future__ import annotations

import streamlit as st

from ui.data.service import Snapshot
from ui.help_text import KPI_HELP


def render_kpis(snapshot: Snapshot) -> None:
    totals = snapshot.totals
    run = snapshot.latest_run
    with st.container(key="kpi_row"):
        cols = st.columns(5, gap="small")

        with cols[0]:
            metric_card(
                "Total signals",
                f"{int(totals.get('total') or 0):,}",
                f"{int(totals.get('without_geom') or 0):,} with no resolvable location",
                tooltip=KPI_HELP["total"],
            )

        with cols[1]:
            metric_card(
                "Councils (distinct AGS)",
                f"{int(totals.get('councils') or 0):,}",
                f"{int(totals.get('without_ags') or 0):,} signals carry no AGS (not counted)",
                tooltip=KPI_HELP["councils"],
            )

        with cols[2]:
            if run is None:
                metric_card(
                    "Signals in latest run",
                    "unknown",
                    "no harvest_run row",
                    tooltip=KPI_HELP["latest_run"],
                )
            else:
                seen = int(run["inserted"]) + int(run["updated"]) + int(run["unchanged"])
                metric_card(
                    "Signals in latest run",
                    f"{seen:,}",
                    f"{int(run['inserted']):,} inserted - {int(run['updated']):,} updated - "
                    f"{int(run['unchanged']):,} unchanged",
                    tooltip=KPI_HELP["latest_run"],
                )

        with cols[3]:
            latest_rejected = int(run["rejected"]) if run else 0
            metric_card(
                "Rejections (dead letters)",
                f"{snapshot.reject_total:,}",
                f"{latest_rejected:,} in the latest run",
                tooltip=KPI_HELP["rejections"],
            )

        with cols[4]:
            report = snapshot.report
            if report is not None:
                verdict = report["verdict"]
                sub = f"quality gate, thresholds {report.get('thresholds_version', '?')}"
            else:
                verdict = "NOT RECORDED"
                sub = (
                    "no quality report file for the latest run; "
                    "verdict is not stored in the database"
                )
            metric_card(
                "Latest run verdict",
                verdict,
                sub,
                tooltip=KPI_HELP["verdict"],
                key=f"kpi_verdict_{verdict.lower().replace(' ', '_')}",
            )


def metric_card(
    title: str, value: str, subtitle: str, *, tooltip: str | None = None, key: str | None = None
) -> None:
    with st.container(border=True, key=key):
        st.metric(title, value, help=tooltip)
        st.caption(subtitle)
