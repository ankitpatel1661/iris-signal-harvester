"""
PyDeck map styling: base map, initial view and the encoding of geo_confidence.

Encoding (stated once here, repeated in the legend):
* colour   = signal_type
* opacity  = geo_confidence (high solid, medium translucent, low faint)
* size     = geo_confidence (high large, low small)
So a municipality-centroid guess is small and faint next to a surveyed point.
"""

from __future__ import annotations

import pydeck as pdk

from ui import config, theme

MAP_STYLE = pdk.map_styles.LIGHT

INITIAL_VIEW = pdk.ViewState(
    latitude=config.GERMANY_CENTER[0],
    longitude=config.GERMANY_CENTER[1],
    zoom=config.GERMANY_ZOOM,
    pitch=0,
    bearing=0,
)

CONFIDENCE_ALPHA = theme.CONFIDENCE_ALPHA
CONFIDENCE_RADIUS_PX = theme.CONFIDENCE_RADIUS_PX
NO_TYPE_COLOR = theme.NO_DATA_COLOR

TOOLTIP = {
    "html": """
        <div style="padding:8px;max-width:320px;">
            <div style="font-size:13px;font-weight:700;margin-bottom:6px;">{title}</div>
            <div><b>Type:</b> {signal_type}</div>
            <div><b>Municipality:</b> {municipality}</div>
            <div><b>Location method:</b> {geo_method}</div>
            <div><b>Confidence:</b> {geo_confidence}</div>
            <div><b>Score:</b> {score}</div>
        </div>
    """,
    "style": {"backgroundColor": "#111827", "color": "white", "borderRadius": "8px"},
}
