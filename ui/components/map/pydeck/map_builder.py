"""
PyDeck map builder: assembles the Deck from layers and a view state.

No Streamlit and no database calls here.
"""

from __future__ import annotations

import pandas as pd
import pydeck as pdk

from ui import config

from .map_styles import INITIAL_VIEW, MAP_STYLE, TOOLTIP


def view_for(located: pd.DataFrame) -> pdk.ViewState:
    """Centre on the data that exists; fall back to Germany when there is none."""
    if located.empty:
        return INITIAL_VIEW
    lat_min, lat_max = float(located["lat"].min()), float(located["lat"].max())
    lon_min, lon_max = float(located["lon"].min()), float(located["lon"].max())
    span = max(lat_max - lat_min, (lon_max - lon_min) * 0.65, 0.01)
    zoom = max(4.0, min(11.0, 8.2 - span * 0.9)) if span < 8 else config.GERMANY_ZOOM
    return pdk.ViewState(
        latitude=(lat_min + lat_max) / 2,
        longitude=(lon_min + lon_max) / 2,
        zoom=zoom,
        pitch=0,
        bearing=0,
    )


def build_map(layers: list[pdk.Layer], view: pdk.ViewState) -> pdk.Deck:
    return pdk.Deck(
        layers=layers,
        initial_view_state=view,
        map_style=MAP_STYLE,
        tooltip=TOOLTIP,
    )
