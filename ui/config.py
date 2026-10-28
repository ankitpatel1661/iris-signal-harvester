"""
Dashboard configuration.

Everything environment- or deployment-specific lives here. Colours and fonts live in
``theme.py``. Nothing in this module touches the network or the database.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Database (read-only; the same variable the harvester uses)
# ---------------------------------------------------------------------------
DSN_ENV_VAR = "IRIS_DB_DSN"
DB_SCHEMA = os.environ.get("IRIS_DB_SCHEMA", "iris_staging")
CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 15_000

# Query results are cached; a harvest run changes the data rarely and a stale view is cheap.
CACHE_TTL_S = 60

# Hard ceiling on signal rows pulled into the browser session. If the table is larger, the
# dashboard says so on screen instead of silently showing a prefix.
SIGNAL_LOAD_LIMIT = 50_000

# ---------------------------------------------------------------------------
# Quality-gate verdict
# ---------------------------------------------------------------------------
# PASS / WARN / FAIL is computed by the harvester (reporting/quality.py) and written next to
# the quality report. It is NOT stored in PostgreSQL. The dashboard only displays a verdict
# when a report file for the latest run's id exists; otherwise it says "not recorded".
REPORTS_DIR = Path(os.environ.get("IRIS_REPORTS_DIR", PROJECT_ROOT / "reports"))

# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
PAGE_TITLE = "IRIS Signal Harvester"
PAGE_ICON = "🛰️"
APP_TAGLINE = "German municipal council documents - nothing is silently guessed"

# ---------------------------------------------------------------------------
# Domain vocabularies (mirror migrations/001_initial.sql CHECK constraints)
# ---------------------------------------------------------------------------
CONFIDENCE_ORDER = ["high", "medium", "low", "none"]

GEO_METHOD_ORDER = [
    "source_coordinates",
    "cadastre_parcel",
    "address_geocode",
    "municipality_centroid",
    "unresolved",
]
GEO_METHOD_LABELS = {
    "source_coordinates": "Source coordinates",
    "cadastre_parcel": "Cadastre parcel",
    "address_geocode": "Address geocode",
    "municipality_centroid": "Municipality centroid (guess)",
    "unresolved": "Unresolved (no geometry)",
}

DATE_ORIGIN_ORDER = ["payload_field", "http_header", "derived_from_meeting", "unknown"]
DATE_ORIGIN_LABELS = {
    "payload_field": "Stated in the source payload",
    "http_header": "HTTP header (Last-Modified)",
    "derived_from_meeting": "Derived from the meeting date",
    "unknown": "Unknown (no date stored)",
}

# Label used wherever a NULL signal_type would otherwise be hidden.
UNCLASSIFIED_LABEL = "(no signal type)"
UNKNOWN_LABEL = "unknown"

# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------
GERMANY_CENTER = (51.1657, 10.4515)  # lat, lon
GERMANY_ZOOM = 5.2
MAP_HEIGHT_PX = 560

# ---------------------------------------------------------------------------
# Sample data (file-backed demo source, used when no database is configured/reachable)
# ---------------------------------------------------------------------------
# A committed snapshot of real pipeline runs over a GENERATED synthetic corpus. Read from the
# repository checkout; nothing is fetched. See docs/samples/README.md.
SAMPLES_DIR = PROJECT_ROOT / "docs" / "samples"
