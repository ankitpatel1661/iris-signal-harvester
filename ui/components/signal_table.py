"""
Signal table: one row per signal, with the evidence that produced it.

``source_url`` is a clickable link (traceability back to the council document) and
``matched_keywords`` is rendered as evidence. NULL text fields are displayed as 'unknown';
NULL numbers stay empty. Nothing is defaulted.
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from ui import config

MAX_ROWS_RENDERED = 5_000


def render_signal_table(df: pd.DataFrame) -> None:
    st.html(
        '<div class="section-title">Signals</div>'
        '<div class="section-sub">Filtered by the sidebar. Open the source link to verify any '
        "row against the original council document.</div>"
    )
    if df.empty:
        st.caption("No signals in the current selection.")
        return

    shown = df.head(MAX_ROWS_RENDERED)
    table = pd.DataFrame(
        {
            "Title": shown["title"],
            "Type": shown["signal_type"]
            .astype("object")
            .where(shown["signal_type"].notna(), config.UNCLASSIFIED_LABEL),
            "Score": pd.to_numeric(shown["relevance_score"], errors="coerce"),
            "Evidence (matched keywords)": shown["matched_keywords"].map(_evidence_text),
            "Municipality": _text(shown["municipality_name"]),
            "Bundesland": _text(shown["bundesland"]),
            "AGS": _text(shown["ags"]),
            "Published": shown["published_at"].map(_date_text),
            "Date origin": shown["published_at_origin"],
            "Geo method": shown["geo_method"],
            "Geo confidence": shown["geo_confidence"],
            "Source": shown["source_url"],
        }
    )
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        height=420,
        column_config={
            "Title": st.column_config.TextColumn(width="large"),
            "Score": st.column_config.ProgressColumn(
                "Score",
                min_value=0.0,
                max_value=1.0,
                format="%.2f",
                help="relevance_score (0-1). Empty = unknown.",
            ),
            "Evidence (matched keywords)": st.column_config.TextColumn(width="large"),
            "Source": st.column_config.LinkColumn(
                "Source", display_text="open source", help="source_url of the council document"
            ),
        },
    )
    if len(df) > len(shown):
        st.caption(f"Showing the first {len(shown):,} of {len(df):,} matching signals.")

    _evidence_detail(shown)


def _evidence_detail(df: pd.DataFrame) -> None:
    with st.expander("Evidence detail for one signal"):
        options = {f"#{row.id} - {row.title[:90]}": idx for idx, row in df.head(500).iterrows()}
        choice = st.selectbox("Signal", list(options), key="evidence_pick")
        if choice is None:
            return
        row = df.loc[options[choice]]
        hits = _hits(row["matched_keywords"])
        st.markdown(f"[{row['source_url']}]({row['source_url']})")
        st.caption(
            f"licence: {row['source_licence'] if pd.notna(row['source_licence']) else 'unknown'} - "
            f"first seen {_date_text(row['first_seen_at'])} - "
            f"last seen {_date_text(row['last_seen_at'])}"
        )
        if not hits:
            st.caption("No matched keywords are recorded for this signal.")
        else:
            st.dataframe(pd.DataFrame(hits), width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
def _text(series: pd.Series) -> pd.Series:
    return series.astype("object").where(series.notna(), config.UNKNOWN_LABEL)


def _date_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return config.UNKNOWN_LABEL
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _hits(value: Any) -> list[dict[str, Any]]:
    """matched_keywords is a jsonb list of hit objects (or, defensively, plain strings)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [{"term": value}]
    if not isinstance(value, list):
        return []
    return [h if isinstance(h, dict) else {"term": str(h)} for h in value]


def _evidence_text(value: Any) -> str:
    hits = _hits(value)
    if not hits:
        return "none recorded"
    seen: list[str] = []
    for hit in hits:
        term = str(hit.get("surface") or hit.get("term") or "?")
        where = hit.get("field")
        label = f"{term} [{where}]" if where else term
        if label not in seen:
            seen.append(label)
    return "; ".join(seen)
