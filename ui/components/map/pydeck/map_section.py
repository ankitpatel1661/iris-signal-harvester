"""
PyDeck map section: renders the signal map and reports what could not be placed on it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui import config

from .map_builder import build_map, view_for
from .map_legend import render_map_legend
from .signal_layer import build_signal_layer, split_located


def render_map(df: pd.DataFrame, type_colors: dict[str, str]) -> None:
    st.html(
        '<div class="section-title">Signal map</div>'
        '<div class="section-sub">Each point is one signal. A faint, small point is a '
        "low-confidence location such as a municipality centroid. Hover a point for its title, "
        "type, location method and confidence.</div>"
    )
    located, unlocated = split_located(df)

    if unlocated:
        st.html(
            f'<div class="notice"><b>{unlocated:,} signal{"s" if unlocated != 1 else ""} '
            f"{'have' if unlocated != 1 else 'has'} no resolvable location</b> and "
            "cannot be drawn. They are not placed at a default point; "
            "they remain in the table below "
            "(geo_confidence: none).</div>"
        )

    layer = build_signal_layer(located, type_colors)
    if layer is None:
        st.html(
            '<div class="notice info">No signal in the current selection has a location, so '
            "there is nothing to draw on the map.</div>"
        )
        return

    render_map_legend(type_colors, has_untyped=bool(df["signal_type"].isna().any()))
    st.pydeck_chart(
        build_map([layer], view_for(located)),
        width="stretch",
        height=config.MAP_HEIGHT_PX,
        key="signal-map",
    )
