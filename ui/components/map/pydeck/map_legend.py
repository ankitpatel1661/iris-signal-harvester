"""Map legend: colour = signal_type, opacity and size = geo_confidence."""

from __future__ import annotations

import html

import streamlit as st

from ui import theme
from ui.help_text import SIGNAL_TYPES, plain

from .map_styles import CONFIDENCE_ALPHA, CONFIDENCE_RADIUS_PX, NO_TYPE_COLOR


def render_map_legend(type_colors: dict[str, str], has_untyped: bool) -> None:
    types = "".join(
        f'<span title="{_type_tip(name)}">'
        f'<span class="legend-dot" style="width:11px;height:11px;background:{color}"></span>'
        f"{html.escape(name)}</span>"
        for name, color in sorted(type_colors.items())
    )
    if has_untyped:
        types += (
            f'<span><span class="legend-dot" '
            f'style="width:11px;height:11px;background:{NO_TYPE_COLOR}">'
            "</span>(no signal type)</span>"
        )

    base = theme.hex_to_rgb(theme.PRIMARY)
    confidences = "".join(
        f'<span><span class="legend-dot" style="width:{CONFIDENCE_RADIUS_PX[level] * 2}px;'
        f"height:{CONFIDENCE_RADIUS_PX[level] * 2}px;"
        f"background:rgba({base[0]},{base[1]},{base[2]},"
        f'{CONFIDENCE_ALPHA[level] / 255:.2f})"></span>'
        f"{level}</span>"
        for level in ("high", "medium", "low")
    )
    st.html(
        f"""
        <div class="legend-row"><b>Colour = signal type</b>{types}
            <span style="color:#64748B;">hover a type for its German term</span></div>
        <div class="legend-row"><b>Opacity and size = geo_confidence</b>{confidences}
            <span style="color:#64748B;">faint, small points are municipality-centroid guesses,
            not surveyed locations</span>
        </div>
        """
    )


def _type_tip(name: str) -> str:
    """Hover text for a legend entry: the German term and what the type means."""
    if name not in SIGNAL_TYPES:
        return ""
    german, meaning = SIGNAL_TYPES[name]
    return html.escape(plain(f"{german}: {meaning}"), quote=True)
