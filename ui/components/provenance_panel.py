"""
Provenance panel: how do we know what we claim to know?

Shows the distribution of ``published_at_origin`` (where each date came from) and of
``geo_method`` / ``geo_confidence`` (how each location was obtained). Categories with zero
rows are still drawn, so an absent method is visible as absent rather than missing.

Every category list runs best to worst, top to bottom, in the summary, the charts and the
cross-table alike (the order of ``config.*_ORDER``).
"""

from __future__ import annotations

import html
import textwrap

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui import config, theme
from ui.help_text import (
    DATE_ORIGIN_HELP,
    GEO_CONFIDENCE_NONE,
    GEO_CONFIDENCE_SUMMARY,
    GEO_METHOD_HELP,
    plain,
)

GEO_METHOD_COLORS = {
    "source_coordinates": theme.CONFIDENCE_COLORS["high"],
    "cadastre_parcel": theme.CONFIDENCE_COLORS["high"],
    "address_geocode": theme.CONFIDENCE_COLORS["medium"],
    "municipality_centroid": theme.WARNING,
    "unresolved": theme.NO_DATA_COLOR,
}
# Categories a reader should not skim past: weak or absent evidence.
DATE_CAUTION = {"unknown"}
GEO_CAUTION = {"municipality_centroid", "unresolved"}
GEO_NOTES = {"municipality_centroid": "a guess at the town, not the site"}


def render_provenance_panel(df: pd.DataFrame) -> None:
    st.html(
        '<div class="section-title">Provenance - nothing is silently guessed</div>'
        '<div class="section-sub">Every date carries its origin and every point carries the '
        "method and confidence that produced it. Unknown stays unknown. "
        "Categories run best to worst, top to bottom; hover a label for what it means.</div>"
    )
    if df.empty:
        st.caption("No signals in the current selection.")
        return

    n = len(df)
    origin = _counts(df["published_at_origin"], config.DATE_ORIGIN_ORDER)
    method = _counts(df["geo_method"], config.GEO_METHOD_ORDER)

    st.html(
        '<div class="prov-summary">'
        + _summary_column(
            "Dates",
            "where each date came from",
            origin,
            config.DATE_ORIGIN_LABELS,
            theme.DATE_ORIGIN_COLORS,
            DATE_ORIGIN_HELP,
            DATE_CAUTION,
            {},
            n,
        )
        + _summary_column(
            "Locations",
            "how each point was obtained",
            method,
            config.GEO_METHOD_LABELS,
            GEO_METHOD_COLORS,
            GEO_METHOD_HELP,
            GEO_CAUTION,
            GEO_NOTES,
            n,
        )
        + f'<div class="prov-foot">{n:,} signals in the current selection</div></div>'
    )

    left, right = st.columns(2, gap="medium")
    with left:
        st.markdown(
            "**Publication date origin** (`published_at_origin`)",
            help="Where the date came from, best first:\n\n"
            + "\n".join(f"- `{k}`: {v}" for k, v in DATE_ORIGIN_HELP.items()),
        )
        st.plotly_chart(
            _bar(
                origin,
                config.DATE_ORIGIN_LABELS,
                theme.DATE_ORIGIN_COLORS,
                DATE_ORIGIN_HELP,
            ),
            width="stretch",
            key="prov_origin",
        )
    with right:
        st.markdown(
            "**Geometry method** (`geo_method`)",
            help="How the point was obtained, best first:\n\n"
            + "\n".join(f"- `{k}`: {v}" for k, v in GEO_METHOD_HELP.items()),
        )
        st.plotly_chart(
            _bar(method, config.GEO_METHOD_LABELS, GEO_METHOD_COLORS, GEO_METHOD_HELP),
            width="stretch",
            key="prov_method",
        )

    st.markdown(
        "**`geo_method` x `geo_confidence`** - counts of signals",
        help=GEO_CONFIDENCE_SUMMARY,
    )
    cross = (
        pd.crosstab(df["geo_method"], df["geo_confidence"])
        .reindex(index=config.GEO_METHOD_ORDER, columns=config.CONFIDENCE_ORDER, fill_value=0)
        .fillna(0)
        .astype(int)
    )
    cross.index = [config.GEO_METHOD_LABELS[m] for m in cross.index]
    cross.index.name = "geo_method"
    st.dataframe(
        cross,
        width="stretch",
        column_config={
            "_index": st.column_config.TextColumn(
                "geo_method",
                help="How the point was obtained, best first (see the chart above).",
            ),
            **{
                level: st.column_config.NumberColumn(
                    level,
                    help=GEO_CONFIDENCE_SUMMARY
                    + (f" {GEO_CONFIDENCE_NONE}" if level == "none" else ""),
                )
                for level in config.CONFIDENCE_ORDER
            },
        },
    )


def _counts(series: pd.Series, order: list[str]) -> pd.Series:
    counts = series.value_counts()
    # Any value outside the documented vocabulary is kept and shown, never dropped.
    extra = [v for v in counts.index if v not in order]
    return counts.reindex(order + extra, fill_value=0)


def _summary_column(
    title: str,
    subtitle: str,
    counts: pd.Series,
    labels: dict[str, str],
    colors: dict[str, str],
    helps: dict[str, str],
    caution: set[str],
    notes: dict[str, str],
    total: int,
) -> str:
    """One scannable block: a 100% stacked bar, then one row per category (best first)."""
    segments, rows = [], []
    for key in counts.index:
        value = int(counts[key])
        pct = 100 * value / total if total else 0
        color = colors.get(key, theme.NO_DATA_COLOR)
        label = html.escape(labels.get(key, key))
        tip = html.escape(plain(helps.get(key, "")), quote=True)
        if value:
            segments.append(
                f'<span style="width:{pct:.2f}%;background:{color}" '
                f'title="{label}: {pct:.0f}% ({value:,})"></span>'
            )
        note = f" <small>{html.escape(notes[key])}</small>" if key in notes else ""
        cls = "caution" if key in caution else ("zero" if not value else "")
        rows.append(
            f'<li class="{cls}"><span class="dot" style="background:{color}"></span>'
            f'<span class="lbl" title="{tip}">{label}{note}</span>'
            f'<span class="val"><b>{pct:.0f}%</b> <small>({value:,})</small></span></li>'
        )
    return (
        '<div class="prov-col">'
        f'<div class="prov-head">{html.escape(title)} <small>{html.escape(subtitle)}</small></div>'
        f'<div class="prov-stack">{"".join(segments)}</div>'
        f'<ul class="prov-list">{"".join(rows)}</ul></div>'
    )


def _bar(
    counts: pd.Series,
    labels: dict[str, str],
    colors: dict[str, str],
    helps: dict[str, str],
) -> go.Figure:
    keys = list(counts.index)
    total = int(counts.sum()) or 1
    meaning = [
        "<br>".join(textwrap.wrap(plain(helps.get(k, "")), 48)) or "(not in the documented list)"
        for k in keys
    ]
    fig = go.Figure(
        go.Bar(
            y=[labels.get(k, k) for k in keys],
            x=[int(counts[k]) for k in keys],
            orientation="h",
            marker_color=[colors.get(k, theme.NO_DATA_COLOR) for k in keys],
            text=[f"{int(counts[k]):,} ({100 * int(counts[k]) / total:.0f}%)" for k in keys],
            textposition="outside",
            cliponaxis=False,
            customdata=[[k, m] for k, m in zip(keys, meaning, strict=True)],
            hovertemplate="%{y}: %{x}<br><i>%{customdata[0]}</i><br>%{customdata[1]}"
            "<extra></extra>",
        )
    )
    layout = theme.plotly_layout(height=270)
    layout["margin"] = {"l": 8, "r": 70, "t": 8, "b": 8}
    fig.update_layout(**layout, showlegend=False)
    # reversed: the first (best) category sits at the top, and the list reads downwards.
    fig.update_yaxes(autorange="reversed", automargin=True)
    fig.update_xaxes(showgrid=True, gridcolor=theme.BORDER, zeroline=False)
    return fig
