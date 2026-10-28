"""
File-backed demo data source: the committed sample runs in ``docs/samples/``.

It produces the same ``Snapshot`` the PostgreSQL path produces, so every component renders
unchanged. Used only when ``IRIS_DB_DSN`` is unset or the database is unreachable
(see ``service.load_snapshot``). Pure files + pandas: no network, no database, no secrets.

What the files cannot supply is left empty, never invented:

* ``first_seen_at`` / ``last_seen_at`` are not in the extracts, so they are NULL (shown as
  "unknown"),
* ``id`` is the 1-based row position in the sample CSV (the database serial is not exported),
* the reject extracts carry no ``rejected_at`` / ``source_system`` column, so the reject table
  shows ``seen_count`` instead.

WGS84 ``lon``/``lat`` are computed from the exported EPSG:25832 coordinates with the inverse
UTM (Krueger series, GRS80) formula, which matches PostGIS/PROJ to well under a millimetre
and avoids a pyproj dependency on the hosting platform. Rows with no coordinates stay NULL.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from ui import config

if TYPE_CHECKING:
    from ui.data.service import Snapshot


class SampleDataError(Exception):
    """A sample file is missing or malformed. The message is safe to show to a user."""


@dataclass(frozen=True)
class SampleRun:
    key: str
    title: str
    signals_csv: str
    rejects_csv: str
    report_json: str
    previous_report_json: str | None = None


SAMPLE_RUNS: dict[str, SampleRun] = {
    run.key: run
    for run in (
        SampleRun(
            "main",
            "corpus_main, first load",
            "signal_ingest_corpus_main.csv",
            "signal_ingest_reject_corpus_main.csv",
            "quality_report_corpus_main.json",
        ),
        SampleRun(
            "main_rerun",
            "corpus_main, re-run",
            "signal_ingest_corpus_main.csv",
            "signal_ingest_reject_corpus_main.csv",
            "quality_report_corpus_main_rerun.json",
            previous_report_json="quality_report_corpus_main.json",
        ),
        SampleRun(
            "degraded",
            "corpus_degraded (deliberately faulty input)",
            "signal_ingest_corpus_degraded.csv",
            "signal_ingest_reject_corpus_degraded.csv",
            "quality_report_corpus_degraded.json",
        ),
    )
}
DEFAULT_RUN = "main"

SIGNAL_COLUMNS = [
    "id",
    "title",
    "document_type",
    "source_system",
    "source_url",
    "source_licence",
    "municipality_name",
    "kreis",
    "bundesland",
    "ags",
    "signal_type",
    "relevance_score",
    "matched_keywords",
    "published_at",
    "published_at_origin",
    "geo_method",
    "geo_confidence",
    "lon",
    "lat",
    "first_seen_at",
    "last_seen_at",
    "harvest_run_id",
]
_TEXT_COLUMNS = [
    "title",
    "document_type",
    "source_system",
    "source_url",
    "source_licence",
    "municipality_name",
    "kreis",
    "bundesland",
    "ags",
    "signal_type",
    "published_at_origin",
    "geo_method",
    "geo_confidence",
]


# ---------------------------------------------------------------------------
# EPSG:25832 (ETRS89 / UTM zone 32N) -> WGS84 lon/lat
# ---------------------------------------------------------------------------
_A = 6378137.0
_F = 1 / 298.257222101
_K0 = 0.9996
_E0 = 500000.0
_LON0 = math.radians(9.0)


def utm32_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Inverse transverse Mercator for EPSG:25832. Returns ``(lon, lat)`` in degrees."""
    n = _F / (2 - _F)
    big_a = _A / (1 + n) * (1 + n**2 / 4 + n**4 / 64)
    beta = (
        n / 2 - 2 * n**2 / 3 + 37 * n**3 / 96 - n**4 / 360,
        n**2 / 48 + n**3 / 15 - 437 * n**4 / 1440,
        17 * n**3 / 480 - 37 * n**4 / 840,
        4397 * n**4 / 161280,
    )
    delta = (
        2 * n - 2 * n**2 / 3 - 2 * n**3 + 116 * n**4 / 45,
        7 * n**2 / 3 - 8 * n**3 / 5 - 227 * n**4 / 45,
        56 * n**3 / 15 - 136 * n**4 / 35,
        4279 * n**4 / 630,
    )
    xi_p = northing / (_K0 * big_a)
    eta_p = (easting - _E0) / (_K0 * big_a)
    xi, eta = xi_p, eta_p
    for j, b in enumerate(beta, start=1):
        xi -= b * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        eta -= b * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
    chi = math.asin(math.sin(xi) / math.cosh(eta))
    lat = chi + sum(d * math.sin(2 * j * chi) for j, d in enumerate(delta, start=1))
    lon = _LON0 + math.atan2(math.sinh(eta), math.cos(xi))
    return math.degrees(lon), math.degrees(lat)


# ---------------------------------------------------------------------------
# File access
# ---------------------------------------------------------------------------
def _read_csv(name: str) -> pd.DataFrame:
    path = config.SAMPLES_DIR / name
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise SampleDataError(f"The sample file docs/samples/{name} could not be read.") from exc


def _read_report(name: str) -> dict[str, Any]:
    path = config.SAMPLES_DIR / name
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SampleDataError(f"The sample file docs/samples/{name} could not be read.") from exc
    if not isinstance(report, dict) or report.get("verdict") not in ("PASS", "WARN", "FAIL"):
        raise SampleDataError(f"The sample file docs/samples/{name} is not a quality report.")
    return report


@cache
def _verdict(report_json: str) -> str:
    return str(_read_report(report_json)["verdict"])


def run_options() -> list[tuple[str, str]]:
    """``(key, label)`` for the run selector; the label carries the recorded verdict."""
    options: list[tuple[str, str]] = []
    for run in SAMPLE_RUNS.values():
        try:
            verdict = _verdict(run.report_json)
        except SampleDataError:
            verdict = "NOT RECORDED"
        options.append((run.key, f"{run.title} - {verdict}"))
    return options


# ---------------------------------------------------------------------------
# Building the Snapshot pieces
# ---------------------------------------------------------------------------
def _signals(csv_name: str, run_id: str) -> pd.DataFrame:
    raw = _read_csv(csv_name)
    blank = raw == ""
    frame = pd.DataFrame(index=raw.index)
    frame["id"] = range(1, len(raw) + 1)
    for column in _TEXT_COLUMNS:
        frame[column] = raw[column].astype("object").where(~blank[column], None)

    score = pd.to_numeric(raw["relevance_score"].where(~blank["relevance_score"]), errors="coerce")
    frame["relevance_score"] = score
    frame["matched_keywords"] = [
        json.loads(v) if v else None for v in raw["matched_keywords"].tolist()
    ]
    frame["published_at"] = pd.to_datetime(
        raw["published_at"].where(~blank["published_at"]), utc=True, errors="coerce"
    )

    lons: list[float | None] = []
    lats: list[float | None] = []
    for x_text, y_text in zip(raw["x_epsg25832"], raw["y_epsg25832"], strict=True):
        if x_text == "" or y_text == "":
            lons.append(None)
            lats.append(None)
            continue
        lon, lat = utm32_to_wgs84(float(x_text), float(y_text))
        lons.append(lon)
        lats.append(lat)
    frame["lon"] = pd.Series(lons, dtype="float64")
    frame["lat"] = pd.Series(lats, dtype="float64")

    # Not exported in the sample extracts: unknown, not defaulted.
    frame["first_seen_at"] = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns, UTC]")
    frame["last_seen_at"] = pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns, UTC]")
    frame["harvest_run_id"] = run_id
    return frame[SIGNAL_COLUMNS]


def _totals(signals: pd.DataFrame) -> dict[str, Any]:
    return {
        "total": len(signals),
        "councils": int(signals["ags"].nunique()),
        "without_ags": int(signals["ags"].isna().sum()),
        "without_geom": int(signals["lon"].isna().sum()),
    }


def _run_row(report: dict[str, Any]) -> dict[str, Any]:
    run = report["run"]
    rejected = sum(int(r["count"]) for r in report.get("rejections") or [])
    return {
        "id": run["run_id"],
        "source_system": run["source_system"],
        "started_at": datetime.fromisoformat(run["started_at"]),
        "finished_at": datetime.fromisoformat(run["finished_at"]),
        "status": run["status"],
        "taxonomy_version": run.get("taxonomy_version"),
        "records_fetched": int(report["funnel"]["fetched"]),
        "inserted": int(run["inserted"]),
        "updated": int(run["updated"]),
        "unchanged": int(run["unchanged"]),
        "rejected": rejected,
        "notes": run.get("notes") or [],
    }


def _reject_histogram(rejects: pd.DataFrame) -> pd.DataFrame:
    if rejects.empty:
        return pd.DataFrame(columns=["reason_code", "n"])
    counts = rejects.groupby("reason_code").size().rename("n").reset_index()
    return counts.sort_values(["n", "reason_code"], ascending=[False, True], ignore_index=True)


def load_sample_snapshot(run_key: str) -> Snapshot:
    """Load one committed sample run as a ``Snapshot``. Raises ``SampleDataError``."""
    from ui.data.service import Snapshot

    run = SAMPLE_RUNS.get(run_key) or SAMPLE_RUNS[DEFAULT_RUN]
    report = _read_report(run.report_json)
    runs = [_run_row(report)]
    if run.previous_report_json:
        runs.append(_run_row(_read_report(run.previous_report_json)))

    signals = _signals(run.signals_csv, runs[0]["id"])
    rejects = _read_csv(run.rejects_csv)
    histogram = _reject_histogram(rejects)
    latest_rejects = rejects[
        ["reason_code", "reason_detail", "source_body_id", "source_record_id", "seen_count"]
    ].head(50)

    return Snapshot(
        totals=_totals(signals),
        runs=runs,
        signals=signals,
        signal_types=sorted(signals["signal_type"].dropna().unique().tolist()),
        reject_total=len(rejects),
        reject_by_reason_all=histogram,
        reject_by_reason_latest=histogram,
        recent_rejects=latest_rejects.reset_index(drop=True),
        report=report,
        source="sample",
        sample_run_key=run.key,
        sample_run_title=run.title,
    )


def samples_dir() -> Path:
    return config.SAMPLES_DIR
