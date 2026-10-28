"""
Sidebar filter state and its application to the signal frame.

Pure pandas, no Streamlit. A NULL is never matched silently: it is its own selectable
value (``config.UNCLASSIFIED_LABEL`` / ``config.UNKNOWN_LABEL``) so filtering can show it
or exclude it explicitly. An empty selection means "do not restrict on this field".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ui import config


@dataclass
class Filters:
    signal_types: list[str] = field(default_factory=list)
    bundeslaender: list[str] = field(default_factory=list)
    confidences: list[str] = field(default_factory=list)
    min_score: float = 0.0
    include_unscored: bool = True

    @property
    def active(self) -> bool:
        return bool(
            self.signal_types
            or self.bundeslaender
            or self.confidences
            or self.min_score > 0.0
            or not self.include_unscored
        )


def label_column(series: pd.Series, null_label: str) -> pd.Series:
    """Text column with NULL shown as an explicit label (display only; the data is untouched)."""
    return series.astype("object").where(series.notna(), null_label)


def apply_filters(df: pd.DataFrame, f: Filters) -> pd.DataFrame:
    if df.empty:
        return df
    mask = pd.Series(True, index=df.index)
    if f.signal_types:
        mask &= label_column(df["signal_type"], config.UNCLASSIFIED_LABEL).isin(f.signal_types)
    if f.bundeslaender:
        mask &= label_column(df["bundesland"], config.UNKNOWN_LABEL).isin(f.bundeslaender)
    if f.confidences:
        mask &= df["geo_confidence"].isin(f.confidences)
    score = pd.to_numeric(df["relevance_score"], errors="coerce")
    unscored = score.isna()
    if f.min_score > 0.0:
        mask &= (score >= f.min_score) | (unscored & f.include_unscored)
    elif not f.include_unscored:
        mask &= ~unscored
    return df[mask]
