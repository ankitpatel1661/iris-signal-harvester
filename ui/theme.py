"""
Design tokens shared by the CSS file, the Plotly charts and the pydeck layers.

Palette follows the platform UI (blue primary, slate sidebar); the confidence and verdict
colours are semantic and are reused everywhere so a colour always means the same thing.
"""

from __future__ import annotations

PRIMARY = "#2563EB"
SIDEBAR = "#0F172A"
CARD = "#FFFFFF"
BACKGROUND = "#F4F7FC"
TEXT = "#111827"
MUTED = "#64748B"
BORDER = "#E5E7EB"

SUCCESS = "#16A34A"
WARNING = "#F59E0B"
DANGER = "#DC2626"
NEUTRAL = "#94A3B8"

FONT_FAMILY = "Segoe UI, -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif"

# Categorical palette for signal_type (assigned in sorted order, see ``signal_type_colors``).
SIGNAL_TYPE_PALETTE = [
    "#2563EB",
    "#DC2626",
    "#16A34A",
    "#F59E0B",
    "#7C3AED",
    "#0891B2",
    "#DB2777",
    "#65A30D",
    "#EA580C",
    "#4F46E5",
]

# Confidence: the three levels are an ordered scale, so one hue (teal) in three strengths;
# "none" is grey because it is the absence of a location, not a weak one.
CONFIDENCE_COLORS = {
    "high": "#0F766E",
    "medium": "#2DD4BF",
    "low": "#99F6E4",
    "none": "#CBD5E1",
}
# pydeck alpha (0-255) and pixel radius per confidence: surveyed points are solid and large,
# municipality-centroid guesses are faint and small.
CONFIDENCE_ALPHA = {"high": 230, "medium": 150, "low": 75}
CONFIDENCE_RADIUS_PX = {"high": 9, "medium": 7, "low": 5}

VERDICT_COLORS = {
    "PASS": SUCCESS,
    "WARN": WARNING,
    "FAIL": DANGER,
    "NOT RECORDED": NEUTRAL,
}

# published_at_origin: provenance of a date. Grey for unknown (no claim made).
DATE_ORIGIN_COLORS = {
    "payload_field": "#0F766E",
    "http_header": "#2563EB",
    "derived_from_meeting": "#F59E0B",
    "unknown": "#94A3B8",
}

NO_DATA_COLOR = "#94A3B8"


def hex_to_rgb(value: str) -> list[int]:
    value = value.lstrip("#")
    return [int(value[i : i + 2], 16) for i in (0, 2, 4)]


def signal_type_colors(types: list[str]) -> dict[str, str]:
    """Stable colour per signal type: sorted order, so a type keeps its colour across filters."""
    ordered = sorted(types)
    return {t: SIGNAL_TYPE_PALETTE[i % len(SIGNAL_TYPE_PALETTE)] for i, t in enumerate(ordered)}


def plotly_layout(height: int = 280) -> dict:
    """Common Plotly layout kwargs so every chart looks like one family."""
    return {
        "height": height,
        "margin": {"l": 8, "r": 8, "t": 28, "b": 8},
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": FONT_FAMILY, "size": 12, "color": TEXT},
        "legend": {"orientation": "h", "yanchor": "bottom", "y": -0.28, "x": 0},
    }
