"""
Signal layer: one ScatterplotLayer point per signal that HAS a geometry.

Rows with NULL geometry are not drawn (there is nowhere to draw them) and are NOT discarded
silently: ``split_located`` returns them so the map section can report the count.
No coordinates are ever filled in.
"""

from __future__ import annotations

import pandas as pd
import pydeck as pdk

from ui import config, theme

from .map_styles import CONFIDENCE_ALPHA, CONFIDENCE_RADIUS_PX, NO_TYPE_COLOR


def split_located(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Return (rows with finite lon/lat, count of rows without a location)."""
    if df.empty:
        return df, 0
    located_mask = df["lon"].notna() & df["lat"].notna()
    return df[located_mask], int((~located_mask).sum())


def build_signal_layer(located: pd.DataFrame, type_colors: dict[str, str]) -> pdk.Layer | None:
    if located.empty:
        return None

    records = []
    for row in located.itertuples(index=False):
        confidence = row.geo_confidence
        rgb = theme.hex_to_rgb(type_colors.get(row.signal_type, NO_TYPE_COLOR))
        records.append(
            {
                "lon": float(row.lon),
                "lat": float(row.lat),
                "fill": [*rgb, CONFIDENCE_ALPHA.get(confidence, 60)],
                "radius": CONFIDENCE_RADIUS_PX.get(confidence, 4),
                "title": row.title,
                "signal_type": row.signal_type if row.signal_type else config.UNCLASSIFIED_LABEL,
                "municipality": row.municipality_name
                if row.municipality_name
                else config.UNKNOWN_LABEL,
                "geo_method": row.geo_method,
                "geo_confidence": confidence,
                "score": f"{row.relevance_score:.2f}"
                if pd.notna(row.relevance_score)
                else config.UNKNOWN_LABEL,
            }
        )

    return pdk.Layer(
        "ScatterplotLayer",
        id="signals",
        data=records,
        get_position="[lon, lat]",
        get_fill_color="fill",
        get_radius="radius",
        # pydeck evaluates plain strings as JS expressions, so the enum value is quoted.
        radius_units="'pixels'",
        stroked=False,
        filled=True,
        pickable=True,
        auto_highlight=True,
        highlight_color=[255, 215, 0, 220],
    )
