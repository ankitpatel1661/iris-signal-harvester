"""
Plain-language definitions of every domain term the dashboard shows.

One place, so the in-place tooltips (``help=``), the chart hover texts and the glossary
expander can never drift apart. The wording follows the project's own schema, taxonomy and
quality gate (``migrations/001_initial.sql``, ``domain/models.py``, ``domain/geo/``,
``config/thresholds.yaml``); nothing here is a number or a claim about the data, only what a
term means. Pure data: no Streamlit, no database.
"""

from __future__ import annotations

from collections.abc import Mapping

# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------
KPI_HELP = {
    "total": "Council documents that matched the taxonomy and were loaded into the staging table.",
    "councils": (
        "Distinct municipalities. **AGS** = *Amtlicher Gemeindeschlüssel*, the 8-digit "
        "official German municipality key."
    ),
    "latest_run": (
        "Inserted + updated + unchanged for that run. On an idempotent re-run this stays the "
        "same while inserts drop to zero."
    ),
    "rejections": (
        "Records that failed validation. They are stored as rows with their reason and "
        "payload, not logged and lost, so they can be counted and replayed."
    ),
    "verdict": (
        "PASS / WARN / FAIL from the quality gate in `config/thresholds.yaml`. "
        "The CLI exit code follows it."
    ),
}

# ---------------------------------------------------------------------------
# Provenance vocabularies (best first, same order as config.*_ORDER)
# ---------------------------------------------------------------------------
DATE_ORIGIN_HELP = {
    "payload_field": "The source stated the date itself.",
    "http_header": (
        "Taken from the record's `Last-Modified` header; a fact about the server's copy, "
        "not necessarily about publication."
    ),
    "derived_from_meeting": (
        "Inferred from the meeting the paper was tabled at. A proxy, labelled as one."
    ),
    "unknown": (
        "No date could be verified, so none is stored. On the probed endpoint most papers "
        "carry no date at all, which is why this column exists."
    ),
}

GEO_METHOD_HELP = {
    "source_coordinates": "Coordinates supplied by the source.",
    "cadastre_parcel": (
        "Resolved from a *Gemarkung / Flur / Flurstück* (cadastral district / block / parcel) "
        "reference."
    ),
    "address_geocode": "Geocoded from a street address.",
    "municipality_centroid": (
        "A representative point for the whole municipality. **A guess at the town, not the site.**"
    ),
    "unresolved": "Nothing resolvable; no geometry stored.",
}

GEO_CONFIDENCE_SUMMARY = (
    "How far the point can be trusted: high / medium / low / none, matching the geometry "
    "method (best first). Downstream scoring should filter on it rather than treat every "
    "point alike."
)
GEO_CONFIDENCE_NONE = "`none` = the signal has no geometry at all (`unresolved`)."

# ---------------------------------------------------------------------------
# Signal types: (German term, meaning)
# ---------------------------------------------------------------------------
SIGNAL_TYPES: dict[str, tuple[str, str]] = {
    "land_use_plan": (
        "Bebauungsplan / Flächennutzungsplan",
        "Formal municipal land-use planning. Often the earliest public sign that a site is "
        "being developed.",
    ),
    "wind_energy": ("Windenergieanlage / Windpark", "Wind turbine siting."),
    "pv_open_space": (
        "Freiflächenphotovoltaik / Solarpark",
        "Ground-mounted solar on open land.",
    ),
    "grid_connection": (
        "Umspannwerk / Netzverknüpfungspunkt",
        "Substations and grid connection points, often the real constraint on a project.",
    ),
    "storage": ("Batteriespeicher", "Grid-scale battery storage."),
    "heat_planning": ("Kommunale Wärmeplanung", "Statutory municipal heat planning."),
    "digital_infra": (
        "Breitbandausbau / Glasfaser / Mobilfunkmast",
        "Broadband, fibre and mobile infrastructure.",
    ),
    "transport_civils": (
        "Straßensanierung / Radwegeausbau / Ladeinfrastruktur",
        "Road, cycleway and EV-charging works.",
    ),
}

# ---------------------------------------------------------------------------
# Funnel stages, in order (keys are the labels the quality report uses)
# ---------------------------------------------------------------------------
FUNNEL_STAGE_HELP = {
    "fetched": "Records received from the source.",
    "malformed by adapter": (
        "No usable identifier, so the record cannot be processed; the rest of the page "
        "still loads. Dead-lettered."
    ),
    "rejected by normaliser": "Failed validation; dead-lettered with a reason code.",
    "below taxonomy threshold": (
        "A real council document, simply not an infrastructure signal. **Not an error.**"
    ),
    "intra-run duplicates": "The same record seen twice in one run, collapsed into one.",
    "staged for load": "Reached the staging table.",
    # Wording used when only the database (no quality report file) is available.
    "not dead-lettered": "Fetched records minus the dead-lettered ones.",
    "staged (inserted + updated + unchanged)": (
        "Rows that reached the staging table: inserted + updated + unchanged."
    ),
}

# ---------------------------------------------------------------------------
# Rejection reason codes. The first six are the project's documented codes; the rest are the
# remaining members of domain.models.RejectCode, described as domain/geo/validate.py does.
# ---------------------------------------------------------------------------
REJECT_CODE_HELP = {
    "missing_required_field": "No usable identifier or title.",
    "schema_validation_failed": "The record did not match the expected shape.",
    "date_unparseable": "The date could not be read.",
    "date_out_of_range": (
        "The date is outside the plausible range, including the real `-0001-11-30` "
        "sentinel some systems emit."
    ),
    "geo_latlon_in_projected_field": (
        "Latitude/longitude written into a projected coordinate field. Unchecked, this "
        "silently places a German record thousands of kilometres away."
    ),
    "geo_out_of_crs_domain": ("Outside the valid domain of EPSG:25832 (ETRS89 / UTM zone 32N)."),
    "malformed_json": "The record was not valid JSON.",
    "invalid_source_url": "`source_url` is not an absolute http(s) URL.",
    "geo_non_finite": "A coordinate is not a finite number (NaN or infinity).",
    "geo_outside_national_envelope": "The point lies outside Germany's national envelope.",
    "geo_outside_claimed_municipality": (
        "The point lies outside the municipality the record claims (within a set tolerance)."
    ),
    "geo_reprojection_failed": (
        "The source coordinate system is unsupported or unrecognised, or reprojection "
        "produced a non-finite result."
    ),
}

NO_DEFINITION = "No description is recorded for this code in the dashboard."

# ---------------------------------------------------------------------------
# Column and control help
# ---------------------------------------------------------------------------
TABLE_HELP = {
    "Title": "Title of the council document, as published.",
    "Type": "Signal type: which kind of infrastructure the document is about. See the glossary.",
    "Score": (
        "`relevance_score` (0-1): how strongly the document matched the taxonomy. Empty = unknown."
    ),
    "Evidence (matched keywords)": (
        "The taxonomy keywords found in the document, with the field they were found in "
        "in brackets."
    ),
    "Municipality": "Name of the municipality (Gemeinde). 'unknown' if the source gave none.",
    "Bundesland": "German federal state. 'unknown' if not recorded.",
    "AGS": (
        "*Amtlicher Gemeindeschlüssel*: the 8-digit official German municipality key. "
        "'unknown' if not recorded."
    ),
    "Published": (
        "Publication date. 'unknown' means no date could be verified; see 'Date origin' for "
        "how a stored date was obtained."
    ),
    "Date origin": "`published_at_origin`: where the date came from. Best first: "
    "payload_field, http_header, derived_from_meeting, unknown.",
    "Geo method": "`geo_method`: how the point was obtained. Best first: source_coordinates, "
    "cadastre_parcel, address_geocode, municipality_centroid (a guess at the town), unresolved.",
    "Geo confidence": "`geo_confidence`: high / medium / low / none, matching the geo method. "
    "'none' = no geometry.",
    "Source": "`source_url`: link to the original council document, to verify the row against.",
}


def bullet_list(entries: Mapping[str, str]) -> str:
    """Markdown bullet list of ``code: meaning``, for use as a ``help=`` tooltip."""
    return "\n".join(f"- `{code}`: {meaning}" for code, meaning in entries.items())


def signal_type_help() -> str:
    """Tooltip text listing every signal type with its German term and meaning."""
    return "\n".join(
        f"- `{code}` ({german}): {meaning}" for code, (german, meaning) in SIGNAL_TYPES.items()
    )


def reject_code_meaning(code: str) -> str:
    return REJECT_CODE_HELP.get(code, NO_DEFINITION)


def plain(text: str) -> str:
    """Strip the light Markdown used in tooltips (for HTML ``title=`` and Plotly hovers)."""
    return text.replace("`", "").replace("*", "")
