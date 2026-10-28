#!/usr/bin/env python3
# ruff: noqa: E501  (long prose strings)
"""Regenerate every file in ``docs/samples/`` by actually running the pipeline.

Nothing in the output is hand-written: the extracts are SELECTs from a real PostGIS
database that the real ``iris-harvest harvest`` loaded, and the reports are the ones the
harvester wrote. The input is the SYNTHETIC corpus produced by ``scripts/generate_corpus.py``
(generated, seeded, not real municipal data).

Prerequisites: the project virtualenv, a PostGIS 16 server you can create databases on, and
the generated cassettes (``.venv/bin/python scripts/generate_corpus.py``). For example::

    docker run -d --rm --name iris-corpus-pg -e POSTGRES_PASSWORD=postgres \\
        -p 55437:5432 postgis/postgis:16-3.4
    .venv/bin/python scripts/make_samples.py \\
        --admin-dsn postgresql://postgres:postgres@localhost:55437/postgres
    docker rm -f iris-corpus-pg

WARNING: it runs ``DROP DATABASE IF EXISTS`` on ``iris_samples_main`` and
``iris_samples_degraded`` on the server you point it at, then recreates them.

Steps: (1) main corpus, first load; (2) the same corpus again (idempotency, PASS);
(3) the deliberately degraded corpus into a second database (FAIL, non-empty rejection
histogram); (4) the ``validate`` invariants for both; (5) CSV/JSON extracts.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parent.parent
BIN = Path(sys.executable).parent
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[")
GAZETTEER = REPO / "docs" / "samples" / "corpus_gazetteer.json"
CASSETTES = REPO / "tests" / "fixtures" / "cassettes" / "corpus"

EXTRACT_COLUMNS = [
    "source_system",
    "source_body_id",
    "source_record_id",
    "source_url",
    "source_licence",
    "title",
    "document_type",
    "published_at",
    "published_at_origin",
    "ags",
    "municipality_name",
    "kreis",
    "bundesland",
    "geo_method",
    "geo_confidence",
    "source_crs",
    "x_epsg25832",
    "y_epsg25832",
    "signal_type",
    "relevance_score",
    "matched_keywords",
    "taxonomy_version",
    "content_hash",
    "hash_version",
]


def admin_connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, autocommit=True)


def with_db(dsn: str, name: str) -> str:
    """The admin DSN pointed at database ``name`` (keeps credentials and host)."""
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{name}"


def recreate(admin_dsn: str, name: str) -> str:
    with admin_connect(admin_dsn) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return with_db(admin_dsn, name)


def run(
    cmd: list[str], dsn: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "IRIS_DB_DSN": dsn,
        "IRIS_FIXTURE_DIR": str(CASSETTES),
        **(extra_env or {}),
    }
    return subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True, check=False)


def harvest(
    dsn: str, scenario: str, report_dir: Path
) -> tuple[subprocess.CompletedProcess[str], str]:
    report_dir.mkdir(parents=True, exist_ok=True)
    proc = run(
        [
            str(BIN / "iris-harvest"),
            "harvest",
            "--source",
            "fixture",
            "--scenario",
            scenario,
            "--gazetteer",
            str(GAZETTEER),
            "--report-dir",
            str(report_dir),
        ],
        dsn,
    )
    # Drop log lines and the two report-path lines (a temp directory: noise, and not stable).
    summary = "\n".join(
        line
        for line in (proc.stdout + proc.stderr).splitlines()
        if not LOG_LINE.match(line)
        and not line.startswith("report ")
        and not line.strip().startswith("/")
    )
    if "verdict" not in summary:
        raise SystemExit(f"harvest produced no verdict:\n{proc.stdout}\n{proc.stderr}")
    return proc, summary + f"\n(exit code {proc.returncode})\n"


def latest_pair(report_dir: Path) -> tuple[Path, Path]:
    md = report_dir / "quality_latest.md"
    js = report_dir / "quality_latest.json"
    if not md.is_file() or not js.is_file():
        raise SystemExit(f"no quality_latest.* in {report_dir}")
    return md, js


def fetch_rows(dsn: str) -> list[dict[str, Any]]:
    query = """
        SELECT source_system, source_body_id, source_record_id, source_url, source_licence,
               title, document_type, published_at, published_at_origin, ags, municipality_name,
               kreis, bundesland, geo_method, geo_confidence, source_crs,
               round(ST_X(geom)::numeric, 2) AS x_epsg25832,
               round(ST_Y(geom)::numeric, 2) AS y_epsg25832,
               signal_type, relevance_score, matched_keywords, taxonomy_version,
               content_hash, hash_version, raw_payload
        FROM iris_staging.signal_ingest
        ORDER BY source_body_id, source_record_id
    """
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(query)  # type: ignore[arg-type]
        return list(cur.fetchall())


def fetch_rejects(dsn: str) -> list[dict[str, Any]]:
    query = """
        SELECT reason_code, source_body_id, source_record_id, seen_count, reason_detail
        FROM iris_staging.signal_ingest_reject
        ORDER BY reason_code, source_body_id, source_record_id
    """
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        cur.execute(query)  # type: ignore[arg-type]
        return list(cur.fetchall())


def cell(value: Any) -> Any:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([cell(row[c]) for c in columns])


def pick_examples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A small, deterministic, varied selection that includes the awkward cases."""
    chosen: dict[str, dict[str, Any]] = {}

    def add(key: str, predicate: Any) -> None:
        for row in rows:
            if row["source_record_id"] not in {
                c["source_record_id"] for c in chosen.values()
            } and predicate(row):
                chosen[key] = row
                return

    for signal in sorted({r["signal_type"] for r in rows}):
        add(
            f"signal:{signal}",
            lambda r, s=signal: (
                r["signal_type"] == s and r["geo_method"] == "municipality_centroid"
            ),
        )
    add("tier1_zone33", lambda r: r["source_crs"] == "EPSG:25833")
    add(
        "tier1_zone32",
        lambda r: r["geo_method"] == "source_coordinates" and r["source_crs"] == "EPSG:25832",
    )
    add("date_unknown", lambda r: r["published_at_origin"] == "unknown")
    add("unresolved_geo", lambda r: r["geo_method"] == "unresolved")
    add("licence_null", lambda r: r["source_licence"] is None and r["geo_method"] != "unresolved")
    add("doc_type_unmapped", lambda r: r["document_type"] == "unmapped")
    add("doc_type_null", lambda r: r["document_type"] is None)
    return list(chosen.values())


def jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (
            float(v)
            if k in ("x_epsg25832", "y_epsg25832") and v is not None
            else cell(v)
            if k not in ("matched_keywords", "raw_payload")
            else v
        )
        for k, v in row.items()
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--admin-dsn", required=True, help="DSN of a PostGIS server (database 'postgres' or any)"
    )
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "samples")
    args = ap.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="iris-samples-"))
    try:
        results: dict[str, Any] = {}
        for label, scenario, dbname in (
            ("main", "corpus_main", "iris_samples_main"),
            ("degraded", "corpus_degraded", "iris_samples_degraded"),
        ):
            dsn = recreate(args.admin_dsn, dbname)
            mig = run([str(BIN / "python"), "-m", "iris_signal_harvester.sink.migrate"], dsn)
            if mig.returncode != 0:
                raise SystemExit(f"migrate failed:\n{mig.stdout}\n{mig.stderr}")
            migrations = mig.stdout.strip()

            proc1, summary1 = harvest(dsn, scenario, work / f"{label}1")
            md, js = latest_pair(work / f"{label}1")
            shutil.copyfile(md, out / f"quality_report_{scenario}.md")
            shutil.copyfile(js, out / f"quality_report_{scenario}.json")
            texts = [summary1]
            if label == "main":
                _proc2, summary2 = harvest(dsn, scenario, work / f"{label}2")
                md2, js2 = latest_pair(work / f"{label}2")
                shutil.copyfile(md2, out / f"quality_report_{scenario}_rerun.md")
                shutil.copyfile(js2, out / f"quality_report_{scenario}_rerun.json")
                texts += ["--- second pass over identical input ---", summary2]

            val = run([str(BIN / "iris-harvest"), "validate"], dsn)
            (out / f"run_output_{scenario}.txt").write_text(
                f"migrations: {migrations}\n\n"
                + "\n".join(texts)
                + "\n--- iris-harvest validate ---\n"
                + val.stdout
                + val.stderr
                + f"(exit code {val.returncode})\n",
                encoding="utf-8",
            )

            rows = fetch_rows(dsn)
            rejects = fetch_rejects(dsn)
            write_csv(out / f"signal_ingest_{scenario}.csv", rows, EXTRACT_COLUMNS)
            write_csv(
                out / f"signal_ingest_reject_{scenario}.csv",
                rejects,
                [
                    "reason_code",
                    "source_body_id",
                    "source_record_id",
                    "seen_count",
                    "reason_detail",
                ],
            )
            if label == "main":
                examples = pick_examples(rows)
                (out / f"signal_ingest_{scenario}_examples.json").write_text(
                    json.dumps(
                        {
                            "_meta": {
                                "synthetic": True,
                                "notice": "Rows from a run over the SYNTHETIC corpus (not real municipal data); "
                                "selected to show each signal type and the awkward cases. raw_payload is the "
                                "untouched generated Paper object.",
                                "source": f"SELECT ... FROM iris_staging.signal_ingest (database {dbname})",
                                "rows": len(examples),
                            },
                            "rows": [jsonable(r) for r in examples],
                        },
                        ensure_ascii=False,
                        indent=1,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            results[label] = {
                "rows": len(rows),
                "rejects": len(rejects),
                "exit": proc1.returncode,
                "migrations": migrations,
            }
        print(json.dumps(results, indent=1))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
