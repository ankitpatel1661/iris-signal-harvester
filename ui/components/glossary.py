"""The collapsible glossary: every domain term the page uses, as tables.

Collapsed by default and rendered near the top. The wording comes from ``ui/help_text.py``,
the same source as the in-place tooltips, so the two cannot disagree.
"""

from __future__ import annotations

import streamlit as st

from ui import config
from ui.help_text import (
    DATE_ORIGIN_HELP,
    FUNNEL_STAGE_HELP,
    GEO_CONFIDENCE_NONE,
    GEO_CONFIDENCE_SUMMARY,
    GEO_METHOD_HELP,
    KPI_HELP,
    REJECT_CODE_HELP,
    SIGNAL_TYPES,
)

_CORE_TERMS = [
    (
        "Signal",
        "A council document that matched the taxonomy and was loaded into the staging table.",
    ),
    (
        "AGS",
        "*Amtlicher Gemeindeschlüssel*: the 8-digit official German municipality key. "
        "'Councils' counts distinct AGS.",
    ),
    ("Bundesland", "German federal state."),
    (
        "Taxonomy / relevance score",
        "The keyword vocabulary that decides whether a council document is an infrastructure "
        "signal. `relevance_score` (0-1) is how strongly a document matched it; empty = unknown.",
    ),
    ("Dead letter", KPI_HELP["rejections"]),
    ("Quality gate", KPI_HELP["verdict"]),
    (
        "Idempotent re-run",
        "Loading the same data again changes nothing: signals in the run stay the same while "
        "inserts (and updates) drop to zero.",
    ),
    (
        "EPSG:25832",
        "ETRS89 / UTM zone 32N, the projected coordinate system the staged geometry uses. "
        "A coordinate outside its valid domain is rejected (`geo_out_of_crs_domain`). "
        "The map converts to latitude/longitude for display.",
    ),
]


def render_glossary() -> None:
    st.caption("Hover the ⓘ next to any label for its definition, or open the glossary.")
    with st.expander("What the terms mean", expanded=False):
        core, dates, places, types, funnel = st.tabs(
            ["Key terms", "Dates", "Locations", "Signal types", "Funnel and rejections"]
        )
        with core:
            _table(["Term", "Meaning"], [(f"**{t}**", m) for t, m in _CORE_TERMS])
        with dates:
            st.markdown(
                "**Publication date origin** (`published_at_origin`): where the date came "
                "from, best first."
            )
            _table(
                ["Value", "Meaning"],
                [(f"`{k}`", DATE_ORIGIN_HELP[k]) for k in config.DATE_ORIGIN_ORDER],
            )
        with places:
            st.markdown(
                "**Geometry method** (`geo_method`): how the point was obtained, best first."
            )
            _table(
                ["Value", "Meaning"],
                [(f"`{k}`", GEO_METHOD_HELP[k]) for k in config.GEO_METHOD_ORDER],
            )
            st.markdown(
                f"**`geo_confidence`** ({' / '.join(config.CONFIDENCE_ORDER)}): "
                f"{GEO_CONFIDENCE_SUMMARY} {GEO_CONFIDENCE_NONE}"
            )
        with types:
            st.markdown("German municipal planning vocabulary, one signal type each.")
            _table(
                ["Signal type", "German terms", "Meaning"],
                [(f"`{k}`", f"*{de}*", meaning) for k, (de, meaning) in SIGNAL_TYPES.items()],
            )
        with funnel:
            st.markdown("**Funnel stages**, in the order a record passes through them.")
            _table(
                ["#", "Stage", "Meaning"],
                [
                    (str(i), f"**{label}**", FUNNEL_STAGE_HELP[label])
                    for i, label in enumerate(
                        [
                            "fetched",
                            "malformed by adapter",
                            "rejected by normaliser",
                            "below taxonomy threshold",
                            "intra-run duplicates",
                            "staged for load",
                        ],
                        start=1,
                    )
                ],
            )
            st.markdown("**Rejection reason codes** (`reason_code`)")
            _table(
                ["Reason code", "Meaning"],
                [(f"`{k}`", v) for k, v in REJECT_CODE_HELP.items()],
            )


def _table(headers: list[str], rows: list[tuple[str, ...]]) -> None:
    aligns = ["---"] * len(headers)
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(aligns) + "|",
        *("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows),
    ]
    st.markdown("\n".join(lines))
