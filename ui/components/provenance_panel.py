"""
Provenance panel: how do we know what we claim to know?

Shows the distribution of ``published_at_origin`` (where each date came from) and of
``geo_method`` / ``geo_confidence`` (how each location was obtained). Categories with zero
rows are still drawn, so an absent method is visible as absent rather than missing.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui import config, theme


def render_provenance_panel(df: pd.DataFrame) -> None:
    st.html(
        '<div class="section-title">Provenance - nothing is silently guessed</div>'
        '<div class="section-sub">Every date carries its origin and every point carries the '
        "method and confidence that produced it. Unknown stays unknown.</div>"
    )
    if df.empty:
        st.caption("No signals in the current selection.")
        return

    n = len(df)
    origin = _counts(df["published_at_origin"], config.DATE_ORIGIN_ORDER)
    method = _counts(df["geo_method"], config.GEO_METHOD_ORDER)

    st.html(
        f"""
        <div class="provenance-banner">
            <b>Dates:</b> {_pct(origin, "payload_field", n)} stated by the source,
            {_pct(origin, "http_header", n)} from an HTTP header,
            {_pct(origin, "derived_from_meeting", n)} derived from the meeting date,
            <b>{_pct(origin, "unknown", n)} unknown</b>.
            &nbsp;|&nbsp;
            <b>Locations:</b> {_pct(method, "source_coordinates", n)} source coordinates,
            {_pct(method, "cadastre_parcel", n)} cadastre parcel,
            {_pct(method, "address_geocode", n)} address geocode,
            <b>{_pct(method, "municipality_centroid", n)} municipality centroid
            (a guess at the town)</b>,
            <b>{_pct(method, "unresolved", n)} unresolved</b>.
            <span style="color:#64748B;">&nbsp;({n:,} signals in the current selection)</span>
        </div>
        """
    )

    left, right = st.columns(2, gap="medium")
    with left:
        st.markdown("**Publication date origin** (`published_at_origin`)")
        st.plotly_chart(
            _bar(
                origin,
                config.DATE_ORIGIN_ORDER,
                config.DATE_ORIGIN_LABELS,
                theme.DATE_ORIGIN_COLORS,
            ),
            width="stretch",
            key="prov_origin",
        )
    with right:
        st.markdown("**Geometry method** (`geo_method`)")
        st.plotly_chart(
            _bar(
                method,
                config.GEO_METHOD_ORDER,
                config.GEO_METHOD_LABELS,
                {
                    "source_coordinates": theme.CONFIDENCE_COLORS["high"],
                    "cadastre_parcel": theme.CONFIDENCE_COLORS["high"],
                    "address_geocode": theme.CONFIDENCE_COLORS["medium"],
                    "municipality_centroid": theme.WARNING,
                    "unresolved": theme.NO_DATA_COLOR,
                },
            ),
            width="stretch",
            key="prov_method",
        )

    st.markdown("**`geo_method` x `geo_confidence`** - counts of signals")
    cross = (
        pd.crosstab(df["geo_method"], df["geo_confidence"])
        .reindex(index=config.GEO_METHOD_ORDER, columns=config.CONFIDENCE_ORDER, fill_value=0)
        .fillna(0)
        .astype(int)
    )
    cross.index = [config.GEO_METHOD_LABELS[m] for m in cross.index]
    cross.index.name = "geo_method"
    st.dataframe(cross, width="stretch")


def _counts(series: pd.Series, order: list[str]) -> pd.Series:
    counts = series.value_counts()
    # Any value outside the documented vocabulary is kept and shown, never dropped.
    extra = [v for v in counts.index if v not in order]
    return counts.reindex(order + extra, fill_value=0)


def _pct(counts: pd.Series, key: str, total: int) -> str:
    value = int(counts.get(key, 0))
    return f"{100 * value / total:.0f}% ({value:,})" if total else "0%"


def _bar(
    counts: pd.Series,
    order: list[str],
    labels: dict[str, str],
    colors: dict[str, str],
) -> go.Figure:
    keys = list(counts.index)
    total = int(counts.sum()) or 1
    fig = go.Figure(
        go.Bar(
            y=[labels.get(k, k) for k in keys],
            x=[int(counts[k]) for k in keys],
            orientation="h",
            marker_color=[colors.get(k, theme.NO_DATA_COLOR) for k in keys],
            text=[f"{int(counts[k]):,} ({100 * int(counts[k]) / total:.0f}%)" for k in keys],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x}<extra></extra>",
        )
    )
    layout = theme.plotly_layout(height=270)
    layout["margin"] = {"l": 8, "r": 70, "t": 8, "b": 8}
    fig.update_layout(**layout, showlegend=False)
    fig.update_yaxes(autorange="reversed", automargin=True)
    fig.update_xaxes(showgrid=True, gridcolor=theme.BORDER, zeroline=False)
    return fig
