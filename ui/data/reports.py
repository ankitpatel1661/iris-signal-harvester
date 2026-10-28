"""
Quality-gate verdict lookup.

The PASS / WARN / FAIL verdict is produced by the harvester and stored in
``reports/quality_<run_id>.json``, not in PostgreSQL. This module reads that file (read-only)
and returns it only when it belongs to the run that was asked for. A missing or unreadable
file yields ``None``: the dashboard then says the verdict was not recorded rather than
inventing one.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ui import config

_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


def load_quality_report(run_id: str | None) -> dict[str, Any] | None:
    if not run_id or not _UUID.match(run_id):
        return None
    path = config.REPORTS_DIR / f"quality_{run_id}.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    if (report.get("run") or {}).get("run_id") != run_id:
        return None
    if report.get("verdict") not in ("PASS", "WARN", "FAIL"):
        return None
    return report
